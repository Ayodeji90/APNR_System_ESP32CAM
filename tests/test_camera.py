"""Tests for src.camera — Camera capture service (simulator/fallback mode)."""

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
            capture_count=3,
            warmup_seconds=0,
        ),
    )


@pytest.fixture
def camera(cfg):
    return CameraService(cfg)


class TestCaptureFrame:
    def test_returns_numpy_array(self, camera):
        frame = camera.capture_frame()
        assert isinstance(frame, np.ndarray)

    def test_frame_has_correct_shape(self, camera):
        frame = camera.capture_frame()
        assert len(frame.shape) == 3  # H, W, C
        assert frame.shape[2] == 3   # BGR channels

    def test_frame_is_uint8(self, camera):
        frame = camera.capture_frame()
        assert frame.dtype == np.uint8


class TestCaptureBestFrame:
    def test_returns_numpy_array(self, camera):
        frame = camera.capture_best_frame()
        assert isinstance(frame, np.ndarray)

    def test_best_frame_has_correct_channels(self, camera):
        frame = camera.capture_best_frame()
        assert frame.shape[2] == 3


class TestLaplacianVariance:
    def test_blank_image_low_sharpness(self):
        blank = np.zeros((100, 100, 3), dtype=np.uint8)
        score = CameraService._laplacian_variance(blank)
        assert score == 0.0

    def test_noisy_image_higher_sharpness(self):
        noisy = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        score = CameraService._laplacian_variance(noisy)
        assert score > 0.0


class TestCleanup:
    def test_cleanup_no_error(self, camera):
        camera.cleanup()  # should not raise
