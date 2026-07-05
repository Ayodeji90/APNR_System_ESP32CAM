"""
ANPR System — Camera Frame Holder (Push-Model)

In the push-based architecture, images are captured by the ESP32 firmware
and sent to the server via POST /api/detect.  This module stores the
latest received frame for use by other components (e.g. Telegram /snapshot).
"""

import logging
from typing import Optional
from datetime import datetime

import cv2
import numpy as np

from src.config import AppConfig

logger = logging.getLogger(__name__)


class CameraService:
    """Stores the latest frame received from the ESP32 via /api/detect."""

    def __init__(self, cfg: AppConfig):
        self.width = cfg.camera.resolution_width
        self.height = cfg.camera.resolution_height
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_time: Optional[datetime] = None

        logger.info(
            "Camera frame holder initialised (ESP32 pushes images via /api/detect)"
        )

    def update_frame(self, frame: np.ndarray) -> None:
        """Store a frame received from the ESP32."""
        self._latest_frame = frame.copy()
        self._frame_time = datetime.now()

    def capture_frame(self) -> np.ndarray:
        """Return the last received frame, or a blank frame if none available."""
        if self._latest_frame is not None:
            return self._latest_frame.copy()
        logger.warning("No frame available — returning blank")
        return np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def capture_best_frame(self) -> np.ndarray:
        """In push-model, just return the latest frame (ESP32 sends best)."""
        return self.capture_frame()

    @property
    def has_frame(self) -> bool:
        return self._latest_frame is not None

    @property
    def frame_age_seconds(self) -> float:
        """How old the latest frame is, in seconds."""
        if self._frame_time is None:
            return float("inf")
        return (datetime.now() - self._frame_time).total_seconds()

    def cleanup(self) -> None:
        logger.info("Camera service stopped.")
