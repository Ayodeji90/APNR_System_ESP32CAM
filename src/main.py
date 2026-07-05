"""
ANPR System — Main Entry Point

Initialises all services and runs the state machine.
Usage:
    python -m src.main
    python -m src.main --config /path/to/config.yaml
"""

import sys
import signal
import logging
import argparse

from src.config import load_config, setup_logging
from src.database import Database
from src.sensor import UltrasonicSensor
from src.camera import CameraService
from src.plate_detector import PlateDetector
from src.ocr_engine import OcrEngine
from src.decision_engine import DecisionEngine
from src.actuator import ActuatorController
from src.state_machine import ANPRStateMachine
from src.telegram_bot import TelegramNotifier
from src.command_handler import TelegramCommandHandler
from src.cleanup import cleanup_old_events

logger = logging.getLogger(__name__)


def main() -> None:
    # ── Parse CLI args ──────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="ANPR Gate System — Automatic Number Plate Recognition"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to config.yaml (default: project root)",
    )
    args = parser.parse_args()

    # ── Load config ─────────────────────────────────────────
    cfg = load_config(args.config)
    setup_logging(cfg)

    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║    ANPR Gate System — Starting Up …      ║")
    logger.info("╚══════════════════════════════════════════╝")

    # ── Initialise core services ─────────────────────────────
    db = Database(cfg)
    sensor = UltrasonicSensor(cfg)
    camera = CameraService(cfg)
    detector = PlateDetector(cfg)
    ocr = OcrEngine(cfg)
    decision_engine = DecisionEngine(cfg, db)
    actuator = ActuatorController(cfg)

    # ── Telegram layer (optional — graceful no-op if disabled) ─
    notifier = TelegramNotifier(cfg)
    cmd_handler = TelegramCommandHandler(cfg, db, actuator, camera, None)

    # ── Build state machine ──────────────────────────────────
    sm = ANPRStateMachine(
        cfg=cfg,
        db=db,
        sensor=sensor,
        camera=camera,
        detector=detector,
        ocr=ocr,
        decision_engine=decision_engine,
        actuator=actuator,
        notifier=notifier,
    )
    # Wire state machine reference into command handler
    cmd_handler._sm = sm

    # ── Graceful shutdown handler ────────────────────────────
    def shutdown(signum, frame):
        logger.info("Received signal %s — shutting down …", signum)
        sm.stop()
        cmd_handler.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Cleanup old event images ──────────────────────────────
    cleanup_old_events(cfg)

    # ── Start Telegram command listener (daemon thread) ──────
    cmd_handler.start()
    notifier.notify_boot()   # Send "system online" message if Telegram enabled

    # ── Run state machine (blocking) ────────────────────────
    try:
        sm.run()
    finally:
        logger.info("Cleaning up resources …")
        actuator.cleanup()
        sensor.cleanup()
        camera.cleanup()
        logger.info("ANPR system stopped.")


if __name__ == "__main__":
    main()
