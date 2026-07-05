"""
ANPR System — Actuator Controller (Push-Model)

In the push-based architecture, barrier commands are sent to the ESP32
via pending commands in the database.  The ESP32 picks them up during
its periodic heartbeat.  This module provides the interface that
Telegram commands and the web dashboard use.
"""

import logging

from src.config import AppConfig

logger = logging.getLogger(__name__)


class ActuatorController:
    """Queues barrier commands for the ESP32 to pick up via heartbeat."""

    def __init__(self, cfg: AppConfig, db=None):
        self.open_duration = cfg.actuator.open_duration_sec
        self._db = db
        self._barrier_open = False

        logger.info(
            "Actuator controller initialised (commands queued for ESP32 heartbeat)"
        )

    def set_db(self, db) -> None:
        """Set the database reference (for deferred initialisation)."""
        self._db = db

    def open_barrier(self) -> None:
        """Queue an 'open' command for the ESP32."""
        logger.info("Queuing barrier OPEN command …")
        if self._db:
            self._db.queue_command("open", source="server")
        self._barrier_open = True

    def close_barrier(self) -> None:
        """Queue a 'close' command for the ESP32."""
        logger.info("Queuing barrier CLOSE command …")
        if self._db:
            self._db.queue_command("close", source="server")
        self._barrier_open = False

    @property
    def is_open(self) -> bool:
        return self._barrier_open

    def cleanup(self) -> None:
        if self._barrier_open and self._db:
            self._db.queue_command("close", source="cleanup")
        self._barrier_open = False
        logger.info("Actuator service stopped.")
