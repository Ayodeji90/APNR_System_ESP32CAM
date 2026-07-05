"""
ANPR System — Event-Driven State Machine

Orchestrates the full workflow:
  IDLE → TRIGGERED → CAPTURE → DETECT_PLATE → OCR → DECIDE → ACTUATE → LOG → IDLE

Includes retry logic for detection failures and enhanced OCR preprocessing.
"""

import os
import time
import logging
from enum import Enum, auto
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from src.config import AppConfig, resolve_path
from src.database import Database
from src.sensor import UltrasonicSensor
from src.camera import CameraService
from src.plate_detector import PlateDetector
from src.ocr_engine import OcrEngine
from src.decision_engine import DecisionEngine, Decision
from src.actuator import ActuatorController

# Optional — only imported if available
try:
    from src.telegram_bot import TelegramNotifier
except ImportError:
    TelegramNotifier = None  # type: ignore

logger = logging.getLogger(__name__)


class State(Enum):
    IDLE = auto()
    TRIGGERED = auto()
    CAPTURE = auto()
    DETECT_PLATE = auto()
    OCR = auto()
    DECIDE = auto()
    ACTUATE = auto()
    LOG = auto()


class ANPRStateMachine:
    """
    Core state machine that drives the ANPR gate system.

    Each call to ``step()`` advances through one state transition.
    Call ``run()`` for a continuous loop.
    """

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        sensor: UltrasonicSensor,
        camera: CameraService,
        detector: PlateDetector,
        ocr: OcrEngine,
        decision_engine: DecisionEngine,
        actuator: ActuatorController,
        notifier=None,   # Optional TelegramNotifier
    ):
        self.cfg = cfg
        self.db = db
        self.sensor = sensor
        self.camera = camera
        self.detector = detector
        self.ocr = ocr
        self.decision_engine = decision_engine
        self.actuator = actuator
        self.notifier = notifier

        self.state = State.IDLE
        self.max_retries = cfg.detection.max_retries
        self._running = True

        # Per-cycle working data
        self._frame: Optional[np.ndarray] = None
        self._plate_crop: Optional[np.ndarray] = None
        self._detection_conf: float = 0.0
        self._plate_text: str = ""
        self._ocr_conf: float = 0.0
        self._decision: Optional[Decision] = None
        self._decision_reason: str = ""
        self._retry_count: int = 0

    # ── State transitions ───────────────────────────────────
    def step(self) -> State:
        """Execute the current state and transition to the next."""
        handler = {
            State.IDLE: self._handle_idle,
            State.TRIGGERED: self._handle_triggered,
            State.CAPTURE: self._handle_capture,
            State.DETECT_PLATE: self._handle_detect,
            State.OCR: self._handle_ocr,
            State.DECIDE: self._handle_decide,
            State.ACTUATE: self._handle_actuate,
            State.LOG: self._handle_log,
        }
        fn = handler.get(self.state, self._handle_idle)
        next_state = fn()
        self.state = next_state
        return next_state

    # ── IDLE ────────────────────────────────────────────────
    def _handle_idle(self) -> State:
        """Wait for vehicle presence."""
        if self.sensor.vehicle_present():
            logger.info("▶ Vehicle detected — transitioning to TRIGGERED")
            return State.TRIGGERED
        time.sleep(0.2)  # polling delay
        return State.IDLE

    # ── TRIGGERED ───────────────────────────────────────────
    def _handle_triggered(self) -> State:
        """Confirm vehicle is still present before starting capture."""
        logger.info("▶ Confirming vehicle presence …")
        if self.sensor.vehicle_present():
            self._retry_count = 0
            self._reset_cycle_data()
            return State.CAPTURE
        logger.info("Vehicle left before confirmation — back to IDLE")
        return State.IDLE

    # ── CAPTURE ─────────────────────────────────────────────
    def _handle_capture(self) -> State:
        """Capture the best frame from the camera."""
        logger.info("▶ Capturing frames …")
        self._frame = self.camera.capture_best_frame()
        return State.DETECT_PLATE

    # ── DETECT_PLATE ────────────────────────────────────────
    def _handle_detect(self) -> State:
        """Detect plate region in the captured frame."""
        logger.info("▶ Detecting plate …")
        if self._frame is None:
            return self._retry_or_fail("No frame available")

        self._plate_crop, self._detection_conf = self.detector.detect(
            self._frame
        )

        if self._plate_crop is None:
            return self._retry_or_fail("No plate region found")

        return State.OCR

    # ── OCR ─────────────────────────────────────────────────
    def _handle_ocr(self) -> State:
        """Read text from the detected plate crop."""
        logger.info("▶ Running OCR …")
        if self._plate_crop is None:
            return self._retry_or_fail("Plate crop is None")

        # First attempt: standard preprocessing
        self._plate_text, self._ocr_conf = self.ocr.read_plate(
            self._plate_crop, enhanced=False
        )

        # If confidence is low, try enhanced preprocessing
        if self._ocr_conf < self.cfg.detection.min_ocr_confidence:
            logger.info(
                "Low OCR confidence (%.1f) — trying enhanced preprocessing",
                self._ocr_conf,
            )
            text2, conf2 = self.ocr.read_plate(
                self._plate_crop, enhanced=True
            )
            if conf2 > self._ocr_conf:
                self._plate_text = text2
                self._ocr_conf = conf2
                logger.info(
                    "Enhanced OCR improved: '%s' conf=%.1f",
                    text2, conf2,
                )

        return State.DECIDE

    # ── DECIDE ──────────────────────────────────────────────
    def _handle_decide(self) -> State:
        """Make access decision based on OCR results."""
        logger.info("▶ Making decision …")
        result = self.decision_engine.decide(
            self._plate_text,
            self._ocr_conf,
            self._detection_conf,
        )
        self._decision = result.decision
        self._decision_reason = result.reason
        return State.ACTUATE

    # ── ACTUATE ─────────────────────────────────────────────
    def _handle_actuate(self) -> State:
        """Open or keep closed the barrier based on decision."""
        if self._decision == Decision.ALLOW:
            logger.info("▶ ACCESS GRANTED — opening barrier")
            self.actuator.open_barrier()
        else:
            logger.info(
                "▶ ACCESS %s — barrier stays closed",
                self._decision.value if self._decision else "UNKNOWN",
            )

        return State.LOG

    # ── LOG ─────────────────────────────────────────────────
    def _handle_log(self) -> State:
        """Save evidence image and log the event to the database."""
        logger.info("▶ Logging event …")

        # Save image
        image_path = self._save_event_image()

        # Log to DB
        self.db.log_event(
            plate_text=self._plate_text,
            decision=self._decision.value if self._decision else "UNKNOWN",
            ocr_confidence=self._ocr_conf,
            detection_confidence=self._detection_conf,
            image_path=image_path,
            note=self._decision_reason,
        )

        # Telegram notification (non-blocking, fire-and-forget)
        # Sent here (after image save) so the image_path is available
        if self.notifier:
            abs_image_path = ""
            if image_path:
                abs_image_path = resolve_path(self.cfg, image_path)
            self.notifier.notify_event(
                plate=self._plate_text,
                decision=self._decision.value if self._decision else "UNKNOWN",
                ocr_conf=self._ocr_conf,
                detection_conf=self._detection_conf,
                image_path=abs_image_path,
            )

        self._reset_cycle_data()
        logger.info("▶ Cycle complete — returning to IDLE\n")
        return State.IDLE

    # ── Retry logic ─────────────────────────────────────────
    def _retry_or_fail(self, reason: str) -> State:
        self._retry_count += 1
        if self._retry_count <= self.max_retries:
            logger.warning(
                "Retry %d/%d: %s — recapturing …",
                self._retry_count, self.max_retries, reason,
            )
            return State.CAPTURE
        logger.warning(
            "Max retries (%d) reached: %s — logging as UNKNOWN",
            self.max_retries, reason,
        )
        self._decision = Decision.UNKNOWN
        self._decision_reason = f"Failed after {self.max_retries} retries: {reason}"
        return State.LOG

    # ── Image saving ────────────────────────────────────────
    def _save_event_image(self) -> str:
        """Save the captured frame to data/events/YYYY-MM-DD/plate_timestamp.jpg"""
        if self._frame is None:
            return ""

        events_dir = resolve_path(self.cfg, self.cfg.paths.events_dir)
        today = datetime.now().strftime("%Y-%m-%d")
        day_dir = os.path.join(events_dir, today)
        os.makedirs(day_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%H%M%S_%f")
        plate_safe = self._plate_text if self._plate_text else "UNKNOWN"
        filename = f"{plate_safe}_{timestamp}.jpg"
        filepath = os.path.join(day_dir, filename)

        cv2.imwrite(filepath, self._frame)
        logger.info("Evidence saved: %s", filepath)

        # Return relative path for DB storage
        return os.path.relpath(filepath, self.cfg.base_dir)

    # ── Reset ───────────────────────────────────────────────
    def _reset_cycle_data(self) -> None:
        self._frame = None
        self._plate_crop = None
        self._detection_conf = 0.0
        self._plate_text = ""
        self._ocr_conf = 0.0
        self._decision = None
        self._decision_reason = ""

    # ── Main loop ───────────────────────────────────────────
    def run(self) -> None:
        """Run the state machine loop until stopped."""
        logger.info("═══ ANPR State Machine started ═══")
        while self._running:
            try:
                self.step()
            except KeyboardInterrupt:
                logger.info("Interrupted — shutting down …")
                break
            except Exception as e:
                logger.exception("Unhandled error in state %s: %s", self.state, e)
                self.state = State.IDLE
                self._reset_cycle_data()
                time.sleep(1)  # avoid tight error loop

    def stop(self) -> None:
        """Signal the state machine to stop."""
        self._running = False
