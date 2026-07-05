"""
ANPR System — Ultrasonic Sensor Driver (ESP32-CAM Edition)

Queries the ESP32-CAM edge device for HC-SR04 distance readings over HTTP.
Falls back to a simulator when the ESP32 is unreachable.
"""

import time
import logging
from typing import Optional

import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)


class UltrasonicSensor:
    """HC-SR04 distance sensor — readings fetched from ESP32 over HTTP."""

    def __init__(self, cfg: AppConfig):
        self.threshold_cm = cfg.sensor.distance_threshold_cm
        self.confirmation_readings = cfg.sensor.confirmation_readings
        self.reading_interval = cfg.sensor.reading_interval_sec

        self._sensor_url = cfg.esp32.sensor_url
        self._request_timeout = cfg.esp32.request_timeout_sec
        self._auth_headers = cfg.esp32.auth_headers

        self._simulator_distance: float = 100.0  # default far away
        self._online: Optional[bool] = None  # None = not yet checked

        logger.info(
            "Ultrasonic sensor configured — ESP32 endpoint: %s  threshold=%dcm",
            self._sensor_url, self.threshold_cm,
        )

    # ── Core reading ────────────────────────────────────────
    def get_distance(self) -> float:
        """
        Return distance in centimetres from the ESP32 HC-SR04 sensor.

        Falls back to simulator mode if the ESP32 is unreachable.
        """
        if not self._sensor_url:
            logger.debug("No ESP32 sensor URL configured — using simulator.")
            return self._simulator_distance

        try:
            resp = requests.get(
                self._sensor_url,
                timeout=self._request_timeout,
                headers=self._auth_headers,
            )
            resp.raise_for_status()
            data = resp.json()
            distance = float(data.get("distance_cm", 999.0))
            logger.debug("Distance reading: %.1f cm", distance)
            if self._online is not True:
                self._online = True
                logger.info("ESP32 sensor online — live readings active.")
            return round(distance, 1)
        except requests.RequestException as e:
            if self._online is not False:
                self._online = False
                logger.warning(
                    "ESP32 sensor unreachable at %s: %s — using simulator.",
                    self._sensor_url, e,
                )
            return self._simulator_distance
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Bad response from ESP32 sensor: %s", e)
            return self._simulator_distance

    # ── Presence detection ──────────────────────────────────
    def vehicle_present(self) -> bool:
        """
        Return True if *confirmation_readings* consecutive distance
        readings are below *threshold_cm*.
        """
        consecutive = 0
        for _ in range(self.confirmation_readings + 2):
            dist = self.get_distance()
            if dist < self.threshold_cm:
                consecutive += 1
                if consecutive >= self.confirmation_readings:
                    logger.info("Vehicle detected at %.1f cm", dist)
                    return True
            else:
                consecutive = 0
            time.sleep(self.reading_interval)
        return False

    # ── Simulator helpers ───────────────────────────────────
    def set_simulator_distance(self, cm: float) -> None:
        """Set the simulated distance (for testing without hardware)."""
        self._simulator_distance = cm

    # ── Cleanup ─────────────────────────────────────────────
    def cleanup(self) -> None:
        logger.info("Sensor service stopped.")
