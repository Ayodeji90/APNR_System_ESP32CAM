"""
ANPR System — Web Dashboard + ESP32 API (Flask)

Provides:
  - Web dashboard for status, event logs, and whitelist management
  - REST API for ESP32 push-model (POST /api/detect, POST /api/heartbeat)
"""

import os
import sys
import json
import logging
import secrets
from datetime import datetime
from functools import wraps

import cv2
import numpy as np

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, send_from_directory, Response,
)

# Add project root to path so we can import src.*
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.config import load_config, setup_logging, resolve_path
from src.database import Database
from src.plate_detector import PlateDetector
from src.ocr_engine import OcrEngine
from src.decision_engine import DecisionEngine, Decision
from src.telegram_bot import TelegramNotifier

logger = logging.getLogger(__name__)

# ── Flask app ───────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
# Load config & DB (module-level so routes can use them)
_cfg = load_config()
_db = Database(_cfg)

# Secret key: use config value, or auto-generate a random one
app.secret_key = _cfg.web.secret_key or secrets.token_hex(32)

# ── ANPR processing components (lazy-initialised) ──────────
_detector = None
_ocr = None
_decision_engine = None
_notifier = None

# Latest ESP32 status (updated by heartbeat)
_esp32_status = {
    "online": False,
    "last_seen": None,
    "uptime_sec": 0,
    "free_heap": 0,
    "wifi_rssi": 0,
    "barrier_open": False,
    "distance_cm": 999.0,
}

# Latest captured frame (for /snapshot command fallback)
_latest_frame = None
_latest_frame_time = None


def _get_detector():
    global _detector
    if _detector is None:
        _detector = PlateDetector(_cfg)
    return _detector


def _get_ocr():
    global _ocr
    if _ocr is None:
        _ocr = OcrEngine(_cfg)
    return _ocr


def _get_decision_engine():
    global _decision_engine
    if _decision_engine is None:
        _decision_engine = DecisionEngine(_cfg, _db)
    return _decision_engine


def _get_notifier():
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier(_cfg)
    return _notifier


# ── Basic Auth (Dashboard) ─────────────────────────────────
def _check_auth(username: str, password: str) -> bool:
    """Validate credentials against config."""
    return (
        username == _cfg.web.dashboard_username
        and password == _cfg.web.dashboard_password
    )


def _auth_required(f):
    """Decorator that enforces HTTP Basic Auth when credentials are configured."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Skip auth if no username/password configured
        if not _cfg.web.dashboard_username or not _cfg.web.dashboard_password:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Login required.",
                401,
                {"WWW-Authenticate": 'Basic realm="ANPR Dashboard"'},
            )
        return f(*args, **kwargs)
    return decorated


# ── ESP32 API Key Auth ──────────────────────────────────────
def _esp32_auth_required(f):
    """Decorator that checks X-Api-Key header for ESP32 endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-Api-Key", "")
        if not _cfg.esp32.api_key_valid(api_key):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════
#  ESP32 PUSH-MODEL API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/api/detect", methods=["POST"])
@_esp32_auth_required
def api_detect():
    """
    Receive a JPEG image from the ESP32, run ANPR pipeline, return decision.

    Expected: multipart/form-data with:
      - 'image': JPEG file
      - 'distance_cm' (optional): float
    Returns: JSON {"action": "open"|"deny"|"unknown", "plate": "...", "reason": "..."}
    """
    global _latest_frame, _latest_frame_time

    # Get the uploaded image
    if "image" not in request.files:
        return jsonify({"error": "no image provided"}), 400

    image_file = request.files["image"]
    image_bytes = image_file.read()

    if len(image_bytes) < 100:
        return jsonify({"error": "image too small"}), 400

    # Decode JPEG to OpenCV frame
    frame = cv2.imdecode(
        np.frombuffer(image_bytes, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if frame is None:
        return jsonify({"error": "invalid image data"}), 400

    # Store for /snapshot fallback
    _latest_frame = frame.copy()
    _latest_frame_time = datetime.now()

    distance_cm = request.form.get("distance_cm", type=float, default=0.0)
    logger.info(
        "ESP32 detection request — image=%d bytes, distance=%.1f cm",
        len(image_bytes), distance_cm,
    )

    # ── Run ANPR pipeline ───────────────────────────────────
    detector = _get_detector()
    ocr = _get_ocr()
    decision_engine = _get_decision_engine()
    notifier = _get_notifier()

    plate_text = ""
    ocr_conf = 0.0
    detection_conf = 0.0
    decision = Decision.UNKNOWN
    reason = ""

    # Retry loop (same image, different preprocessing)
    max_retries = _cfg.detection.max_retries
    for attempt in range(max_retries + 1):
        # Detect plate region
        plate_crop, det_conf = detector.detect(frame)
        if plate_crop is None:
            if attempt < max_retries:
                logger.info("No plate found (attempt %d/%d) — retrying", attempt + 1, max_retries)
                continue
            # FALLBACK: If we couldn't find a perfect rectangle after all retries,
            # just pass the entire image to the OCR engine!
            logger.warning("Falling back to full-frame OCR (no plate border detected)")
            plate_crop = frame
            det_conf = 0.1

        detection_conf = det_conf

        # OCR — standard first, then enhanced if low confidence
        text, conf = ocr.read_plate(plate_crop, enhanced=False)
        if conf < _cfg.detection.min_ocr_confidence:
            text2, conf2 = ocr.read_plate(plate_crop, enhanced=True)
            if conf2 > conf:
                text, conf = text2, conf2

        plate_text = text
        ocr_conf = conf

        # Decision
        result = decision_engine.decide(plate_text, ocr_conf, detection_conf)
        decision = result.decision
        reason = result.reason
        break

    # ── Save evidence image ─────────────────────────────────
    image_path = ""
    try:
        events_dir = resolve_path(_cfg, _cfg.paths.events_dir)
        today = datetime.now().strftime("%Y-%m-%d")
        day_dir = os.path.join(events_dir, today)
        os.makedirs(day_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%H%M%S_%f")
        plate_safe = plate_text if plate_text else "UNKNOWN"
        filename = f"{plate_safe}_{timestamp}.jpg"
        filepath = os.path.join(day_dir, filename)

        cv2.imwrite(filepath, frame)
        image_path = os.path.relpath(filepath, _cfg.base_dir)
        logger.info("Evidence saved: %s", filepath)
    except Exception as e:
        logger.error("Failed to save evidence image: %s", e)

    # ── Log to database ─────────────────────────────────────
    _db.log_event(
        plate_text=plate_text,
        decision=decision.value,
        ocr_confidence=ocr_conf,
        detection_confidence=detection_conf,
        image_path=image_path,
        note=reason,
    )

    # ── Telegram notification ───────────────────────────────
    try:
        abs_image_path = ""
        if image_path:
            abs_image_path = resolve_path(_cfg, image_path)
        notifier.notify_event(
            plate=plate_text,
            decision=decision.value,
            ocr_conf=ocr_conf,
            detection_conf=detection_conf,
            image_path=abs_image_path,
        )
    except Exception as e:
        logger.error("Telegram notification failed: %s", e)

    # ── Return decision to ESP32 ────────────────────────────
    action = {
        Decision.ALLOW: "open",
        Decision.DENY: "deny",
        Decision.UNKNOWN: "unknown",
    }.get(decision, "unknown")

    logger.info(
        "Detection result: plate='%s' decision=%s action=%s",
        plate_text, decision.value, action,
    )

    return jsonify({
        "action": action,
        "plate": plate_text,
        "reason": reason,
        "ocr_confidence": round(ocr_conf, 1),
        "detection_confidence": round(detection_conf, 2),
    })


@app.route("/api/heartbeat", methods=["POST"])
@_esp32_auth_required
def api_heartbeat():
    """
    Receive periodic status from the ESP32 and return any pending commands.

    Expected JSON: {"uptime_sec", "free_heap", "wifi_rssi", "barrier_open", "distance_cm"}
    Returns: {"status": "ok", "commands": [{"id": 1, "command": "open"}, ...]}
    """
    global _esp32_status

    data = request.get_json(silent=True) or {}

    _esp32_status = {
        "online": True,
        "last_seen": datetime.now().isoformat(),
        "uptime_sec": data.get("uptime_sec", 0),
        "free_heap": data.get("free_heap", 0),
        "wifi_rssi": data.get("wifi_rssi", 0),
        "barrier_open": data.get("barrier_open", False),
        "distance_cm": data.get("distance_cm", 999.0),
    }

    # Store in database settings for persistence
    _db.set_setting("esp32_status", json.dumps(_esp32_status))

    # Fetch and return pending commands
    pending = _db.get_pending_commands()
    commands = [{"id": c["id"], "command": c["command"]} for c in pending]

    # Acknowledge all returned commands
    for c in pending:
        _db.acknowledge_command(c["id"])

    logger.debug(
        "ESP32 heartbeat: uptime=%ds heap=%d rssi=%d commands=%d",
        data.get("uptime_sec", 0),
        data.get("free_heap", 0),
        data.get("wifi_rssi", 0),
        len(commands),
    )

    return jsonify({
        "status": "ok",
        "commands": commands,
    })


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
@_auth_required
def dashboard():
    """Main dashboard — system status + recent events."""
    recent = _db.get_recent_events(limit=10)
    total_events = _db.get_event_count()
    today_events = _db.get_today_event_count()
    vehicle_count = len(_db.get_all_vehicles())
    return render_template(
        "dashboard.html",
        events=recent,
        total_events=total_events,
        today_events=today_events,
        vehicle_count=vehicle_count,
        esp32_status=_esp32_status,
    )


@app.route("/events")
@_auth_required
def events():
    """Paginated event log."""
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 20
    offset = (page - 1) * per_page
    page_events = _db.get_recent_events(limit=per_page, offset=offset)
    return render_template(
        "events.html",
        events=page_events,
        page=page,
        has_more=len(page_events) >= per_page,
    )


@app.route("/vehicles")
@_auth_required
def vehicles():
    """Vehicle whitelist management."""
    all_vehicles = _db.get_all_vehicles()
    return render_template("vehicles.html", vehicles=all_vehicles)


@app.route("/vehicles/add", methods=["POST"])
@_auth_required
def add_vehicle():
    """Add a plate to the whitelist."""
    plate = request.form.get("plate_text", "").strip().upper()
    owner = request.form.get("owner_name", "").strip()
    access = request.form.get("access_level", "resident").strip()
    if plate:
        _db.add_vehicle(plate, owner, access)
        flash(f"Vehicle {plate} added to whitelist.", "success")
    else:
        flash("Plate number is required.", "error")
    return redirect(url_for("vehicles"))


@app.route("/vehicles/remove", methods=["POST"])
@_auth_required
def remove_vehicle():
    """Remove a plate from the whitelist."""
    plate = request.form.get("plate_text", "").strip().upper()
    if plate:
        _db.delete_vehicle(plate)
        flash(f"Vehicle {plate} removed from whitelist.", "success")
    return redirect(url_for("vehicles"))


# ── Dashboard API endpoints ─────────────────────────────────
@app.route("/api/status")
@_auth_required
def api_status():
    """JSON system status."""
    return jsonify({
        "status": "running",
        "total_events": _db.get_event_count(),
        "today_events": _db.get_today_event_count(),
        "vehicle_count": len(_db.get_all_vehicles()),
        "esp32": _esp32_status,
    })


@app.route("/api/events")
@_auth_required
def api_events():
    """JSON event list."""
    limit = request.args.get("limit", 50, type=int)
    events = _db.get_recent_events(limit=limit)
    return jsonify(events)


# ── Serve event images ──────────────────────────────────────
@app.route("/images/<path:filepath>")
@_auth_required
def serve_image(filepath):
    """Serve captured event images."""
    events_dir = resolve_path(_cfg, _cfg.paths.events_dir)
    return send_from_directory(events_dir, filepath)


# ── Entry point ─────────────────────────────────────────────
def run_dashboard():
    """Start the Flask dashboard."""
    setup_logging(_cfg)
    logger.info("Starting ANPR Web Dashboard on %s:%d", _cfg.web.host, _cfg.web.port)
    app.run(
        host=_cfg.web.host,
        port=_cfg.web.port,
        debug=_cfg.web.debug,
    )


if __name__ == "__main__":
    run_dashboard()
