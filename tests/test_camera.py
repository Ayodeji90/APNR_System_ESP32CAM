"""Tests for src.camera — Camera frame holder (push-model)."""

import pytest
import numpy as np

from src.config import AppConfig, CameraConfig
from src.camera import CameraService


@pytest.fixture
def cfg():
    return AppConfig(
        camera=CameraConfig(
            resolution_width=320,
            resolution_height=240,
        ),
    )


@pytest.fixture
def camera(cfg):
    return CameraService(cfg)


class TestCaptureFrame:
    def test_returns_blank_when_empty(self, camera):
        frame = camera.capture_frame()
        assert isinstance(frame, np.ndarray)
        assert frame.shape == (240, 320, 3)
        assert np.sum(frame) == 0

    def test_update_and_capture(self, camera):
        test_frame = np.ones((240, 320, 3), dtype=np.uint8) * 128
        camera.update_frame(test_frame)
        
        frame = camera.capture_frame()
        assert isinstance(frame, np.ndarray)
        assert frame.shape == (240, 320, 3)
        assert frame[0, 0, 0] == 128

    def test_has_frame_property(self, camera):
        assert camera.has_frame is False
        camera.update_frame(np.zeros((240, 320, 3), dtype=np.uint8))
        assert camera.has_frame is True


class TestFrameAge:
    def test_frame_age_infinity_when_empty(self, camera):
        assert camera.frame_age_seconds == float("inf")
        
    def test_frame_age_updates(self, camera):
        camera.update_frame(np.zeros((240, 320, 3), dtype=np.uint8))
        assert camera.frame_age_seconds >= 0.0
        assert camera.frame_age_seconds < 1.0


class TestCleanup:
    def test_cleanup_no_error(self, camera):
        camera.cleanup()  # should not raise
