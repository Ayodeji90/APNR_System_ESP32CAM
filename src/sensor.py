"""
ANPR System — Sensor Data Holder (Push-Model)

In the push-based architecture, the ultrasonic sensor is read directly
by the ESP32 firmware.  This module stores the latest reading received
via the /api/heartbeat endpoint for display and query purposes.
"""

import logging

from src.config import AppConfig

logger = logging.getLogger(__name__)


class UltrasonicSensor:
    """Stores the latest distance reading received from the ESP32."""

    def __init__(self, cfg: AppConfig):
        self.threshold_cm = cfg.sensor.distance_threshold_cm
        self._last_distance: float = 999.0

        logger.info(
            "Sensor data holder initialised — threshold=%dcm (ESP32 handles detection)",
            self.threshold_cm,
        )

    def update_distance(self, distance_cm: float) -> None:
        """Update the cached distance from an ESP32 heartbeat."""
        self._last_distance = distance_cm

    def get_distance(self) -> float:
        """Return the last known distance reading."""
        return self._last_distance

    def vehicle_present(self) -> bool:
        """Return True if the last known distance is below the threshold."""
        return self._last_distance < self.threshold_cm

    def cleanup(self) -> None:
        logger.info("Sensor service stopped.")
