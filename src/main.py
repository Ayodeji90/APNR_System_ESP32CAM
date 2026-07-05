"""
ANPR System — Main Entry Point (Push-Model)

In the push-based architecture, the ESP32 sends images to the server.
All ANPR processing happens in the Flask endpoint (/api/detect).
This entry point starts:
  - Flask web server (dashboard + ESP32 API)
  - Telegram command handler (background thread)

Usage:
    python -m src.main
    python -m src.main --config /path/to/config.yaml
"""

import sys
import signal
import logging
import argparse
import threading

from src.config import load_config, setup_logging
from src.database import Database
from src.sensor import UltrasonicSensor
from src.camera import CameraService
from src.actuator import ActuatorController
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
    logger.info("║  ANPR Gate System — Push Model Starting  ║")
    logger.info("╚══════════════════════════════════════════╝")

    # ── Initialise core services ─────────────────────────────
    db = Database(cfg)
    sensor = UltrasonicSensor(cfg)
    camera = CameraService(cfg)
    actuator = ActuatorController(cfg, db=db)

    # ── Telegram layer (optional — graceful no-op if disabled) ─
    notifier = TelegramNotifier(cfg)
    cmd_handler = TelegramCommandHandler(cfg, db, actuator, camera, None)

    # ── Graceful shutdown handler ────────────────────────────
    def shutdown(signum, frame):
        logger.info("Received signal %s — shutting down …", signum)
        cmd_handler.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Cleanup old event images ──────────────────────────────
    cleanup_old_events(cfg)

    # ── Start Telegram command listener (daemon thread) ──────
    cmd_handler.start()
    notifier.notify_boot()

    # ── Start Flask web server (blocking) ────────────────────
    # Import the Flask app and configure it
    from web.app import app

    logger.info(
        "Starting Flask server on %s:%d — "
        "ESP32 pushes to POST /api/detect and POST /api/heartbeat",
        cfg.web.host, cfg.web.port,
    )
    logger.info("Dashboard at http://%s:%d", cfg.web.host, cfg.web.port)
    logger.info("═══ ANPR System ready — waiting for ESP32 data ═══")

    try:
        app.run(
            host=cfg.web.host,
            port=cfg.web.port,
            debug=cfg.web.debug,
            use_reloader=False,  # Don't reload in production
        )
    finally:
        logger.info("Cleaning up resources …")
        actuator.cleanup()
        sensor.cleanup()
        camera.cleanup()
        logger.info("ANPR system stopped.")


if __name__ == "__main__":
    main()
