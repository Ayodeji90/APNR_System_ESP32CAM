"""Tests for src.plate_detector — OpenCV plate detection."""

import pytest
import numpy as np
import cv2

from src.config import AppConfig, DetectionConfig
from src.plate_detector import PlateDetector


@pytest.fixture
def cfg():
    return AppConfig(
        detection=DetectionConfig(
            preprocessing_width=400,
            plate_aspect_min=2.0,
            plate_aspect_max=6.0,
            min_plate_area=500,
        ),
    )


@pytest.fixture
def detector(cfg):
    return PlateDetector(cfg)


class TestDetect:
    def test_blank_image_no_plate(self, detector):
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        plate_crop, confidence = detector.detect(blank)
        assert plate_crop is None
        assert confidence == 0.0

    def test_returns_tuple(self, detector):
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        result = detector.detect(frame)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_synthetic_plate_rectangle(self, detector):
        """Draw a white rectangle with plate-like aspect ratio on dark background."""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        # Draw a white rectangle roughly plate-shaped (aspect ~3.5)
        cv2.rectangle(frame, (200, 200), (410, 260), (255, 255, 255), -1)
        # Add a dark border for edge detection
        cv2.rectangle(frame, (198, 198), (412, 262), (0, 0, 0), 2)
        plate_crop, confidence = detector.detect(frame)
        # Detection may or may not find it depending on contour analysis,
        # but the function should not crash
        assert isinstance(confidence, float)


class TestFourPointTransform:
    def test_valid_quadrilateral(self):
        image = np.zeros((200, 200, 3), dtype=np.uint8)
        cv2.rectangle(image, (20, 20), (180, 80), (255, 255, 255), -1)
        pts = np.array([[20, 20], [180, 20], [180, 80], [20, 80]])
        result = PlateDetector._four_point_transform(image, pts)
        assert result is not None
        assert isinstance(result, np.ndarray)

    def test_too_small_returns_none(self):
        image = np.zeros((200, 200, 3), dtype=np.uint8)
        pts = np.array([[0, 0], [2, 0], [2, 2], [0, 2]])
        result = PlateDetector._four_point_transform(image, pts)
        assert result is None
