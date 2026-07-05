"""
ANPR System — Actuator Controller (ESP32-CAM Edition)

Sends HTTP commands to the ESP32-CAM edge device to control the
barrier servo motor and/or relay module.
Falls back to simulator mode when the ESP32 is unreachable.
"""

import time
import logging
import threading
from typing import Optional

import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)


class ActuatorController:
    """Controls servo motor for barrier and relay for gate motor via ESP32 HTTP API."""

    def __init__(self, cfg: AppConfig):
        self.open_angle = cfg.actuator.servo_open_angle
        self.closed_angle = cfg.actuator.servo_closed_angle
        self.open_duration = cfg.actuator.open_duration_sec
        self.use_servo = cfg.actuator.use_servo
        self.use_relay = cfg.actuator.use_relay

        self._open_url = cfg.esp32.barrier_open_url
        self._close_url = cfg.esp32.barrier_close_url
        self._request_timeout = cfg.esp32.request_timeout_sec
        self._auth_headers = cfg.esp32.auth_headers

        self._close_timer: Optional[threading.Timer] = None
        self._barrier_open = False
        self._online: Optional[bool] = None

        logger.info(
            "Actuator configured — ESP32 endpoints: open=%s close=%s  servo=%s relay=%s",
            self._open_url, self._close_url, self.use_servo, self.use_relay,
        )

    # ── HTTP helpers ────────────────────────────────────────
    def _post(self, url: str, label: str) -> bool:
        """Send POST to ESP32 endpoint. Returns True on success."""
        if not url:
            logger.debug("[SIM] %s (no URL configured)", label)
            return False
        try:
            resp = requests.post(
                url, timeout=self._request_timeout, headers=self._auth_headers
            )
            resp.raise_for_status()
            if self._online is not True:
                self._online = True
                logger.info("ESP32 actuator online.")
            logger.debug("%s → OK (%d)", label, resp.status_code)
            return True
        except requests.RequestException as e:
            if self._online is not False:
                self._online = False
                logger.warning("ESP32 actuator unreachable at %s: %s", url, e)
            logger.debug("[SIM] %s (ESP32 offline)", label)
            return False

    # ── Public API ──────────────────────────────────────────
    def open_barrier(self) -> None:
        """Open the barrier / activate the gate."""
        if self._barrier_open:
            logger.debug("Barrier already open — resetting close timer.")
            self._cancel_close_timer()
        else:
            logger.info("Opening barrier …")
            self._post(self._open_url, "barrier open")
            self._barrier_open = True

        # Schedule auto-close
        self._close_timer = threading.Timer(
            self.open_duration, self._auto_close
        )
        self._close_timer.daemon = True
        self._close_timer.start()
        logger.info(
            "Barrier open — auto-close in %ds", self.open_duration
        )

    def close_barrier(self) -> None:
        """Close the barrier / deactivate the gate."""
        self._cancel_close_timer()
        logger.info("Closing barrier …")
        self._post(self._close_url, "barrier close")
        self._barrier_open = False

    @property
    def is_open(self) -> bool:
        return self._barrier_open

    # ── Internal ────────────────────────────────────────────
    def _auto_close(self) -> None:
        logger.info("Auto-close timer fired.")
        self.close_barrier()

    def _cancel_close_timer(self) -> None:
        if self._close_timer and self._close_timer.is_alive():
            self._close_timer.cancel()
            self._close_timer = None

    # ── Cleanup ─────────────────────────────────────────────
    def cleanup(self) -> None:
        self._cancel_close_timer()
        if self._barrier_open:
            self._post(self._close_url, "barrier close (cleanup)")
        self._barrier_open = False
        logger.info("Actuator service stopped.")
