"""Tests for src.sensor — Ultrasonic sensor driver (simulator mode)."""

import pytest

from src.config import AppConfig, SensorConfig
from src.sensor import UltrasonicSensor


@pytest.fixture
def cfg():
    return AppConfig(
        sensor=SensorConfig(
            trigger_pin=23,
            echo_pin=24,
            distance_threshold_cm=50,
            confirmation_readings=3,
            reading_interval_sec=0.0,  # no delay in tests
        ),
    )


@pytest.fixture
def sensor(cfg):
    return UltrasonicSensor(cfg)


class TestSimulatorDistance:
    def test_default_distance_is_far(self, sensor):
        assert sensor.get_distance() == 100.0

    def test_set_simulator_distance(self, sensor):
        sensor.set_simulator_distance(25.0)
        assert sensor.get_distance() == 25.0

    def test_set_zero_distance(self, sensor):
        sensor.set_simulator_distance(0.0)
        assert sensor.get_distance() == 0.0


class TestVehiclePresent:
    def test_vehicle_present_when_close(self, sensor):
        sensor.set_simulator_distance(30.0)  # below 50cm threshold
        assert sensor.vehicle_present() is True

    def test_vehicle_not_present_when_far(self, sensor):
        sensor.set_simulator_distance(100.0)  # above 50cm threshold
        assert sensor.vehicle_present() is False

    def test_vehicle_present_at_threshold(self, sensor):
        sensor.set_simulator_distance(50.0)  # exactly at threshold (not below)
        assert sensor.vehicle_present() is False

    def test_vehicle_present_just_below_threshold(self, sensor):
        sensor.set_simulator_distance(49.9)
        assert sensor.vehicle_present() is True


class TestCleanup:
    def test_cleanup_no_error(self, sensor):
        sensor.cleanup()  # should not raise in simulator mode
