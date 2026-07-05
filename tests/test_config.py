"""Tests for src.config — Configuration loader."""

import os
import tempfile
import pytest
import yaml

from src.config import load_config, resolve_path, AppConfig


class TestLoadConfig:
    """Test loading config from YAML and defaults."""

    def test_load_defaults_when_no_file(self):
        """If config file is missing, defaults should be used."""
        cfg = load_config("/nonexistent/path/config.yaml")
        assert isinstance(cfg, AppConfig)
        assert cfg.camera.resolution_width == 640
        assert cfg.sensor.distance_threshold_cm == 50
        assert cfg.actuator.servo_open_angle == 90
        assert cfg.detection.max_retries == 3
        assert cfg.ocr.engine == "tesseract"

    def test_load_from_yaml(self, tmp_path):
        """Values in YAML should override defaults."""
        config_data = {
            "camera": {"resolution_width": 1280},
            "sensor": {"distance_threshold_cm": 30},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))
        assert cfg.camera.resolution_width == 1280
        assert cfg.sensor.distance_threshold_cm == 30
        # Unset values should keep defaults
        assert cfg.camera.resolution_height == 480
        assert cfg.actuator.open_duration_sec == 10

    def test_base_dir_is_set(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}")
        cfg = load_config(str(config_file))
        assert cfg.base_dir == str(tmp_path)


class TestResolvePath:
    def test_relative_path(self):
        cfg = AppConfig(base_dir="/home/pi/anpr")
        result = resolve_path(cfg, "data/db/anpr.db")
        assert result == "/home/pi/anpr/data/db/anpr.db"

    def test_absolute_path(self):
        cfg = AppConfig(base_dir="/home/pi/anpr")
        result = resolve_path(cfg, "/tmp/test.db")
        assert result == "/tmp/test.db"
