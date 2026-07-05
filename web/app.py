"""
ANPR System — Web Dashboard (Flask)

Provides a local web interface for:
  - System status overview
  - Event log browsing
  - Vehicle whitelist management
"""

import os
import sys
import logging
import secrets
from functools import wraps

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


# ── Basic Auth ─────────────────────────────────────────────
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


# ── Routes ──────────────────────────────────────────────────
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


# ── API endpoints ───────────────────────────────────────────
@app.route("/api/status")
@_auth_required
def api_status():
    """JSON system status."""
    return jsonify({
        "status": "running",
        "total_events": _db.get_event_count(),
        "today_events": _db.get_today_event_count(),
        "vehicle_count": len(_db.get_all_vehicles()),
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
