"""Tests for src.actuator — Actuator controller (simulator mode)."""

import pytest
import time

from src.config import AppConfig, ActuatorConfig
from src.actuator import ActuatorController


@pytest.fixture
def cfg():
    return AppConfig(
        actuator=ActuatorConfig(
            servo_pin=18,
            relay_pin=25,
            servo_open_angle=90,
            servo_closed_angle=0,
            open_duration_sec=1,  # short for tests
            use_servo=True,
            use_relay=True,
        ),
    )


@pytest.fixture
def actuator(cfg):
    act = ActuatorController(cfg)
    yield act
    act.cleanup()


class TestBarrierState:
    def test_initially_closed(self, actuator):
        assert actuator.is_open is False

    def test_open_barrier(self, actuator):
        actuator.open_barrier()
        assert actuator.is_open is True

    def test_close_barrier(self, actuator):
        actuator.open_barrier()
        assert actuator.is_open is True
        actuator.close_barrier()
        assert actuator.is_open is False

    def test_double_open_no_error(self, actuator):
        actuator.open_barrier()
        actuator.open_barrier()  # should not raise
        assert actuator.is_open is True

    def test_close_when_already_closed(self, actuator):
        actuator.close_barrier()  # should not raise
        assert actuator.is_open is False


class TestAutoClose:
    def test_auto_close_fires(self, actuator):
        actuator.open_barrier()
        assert actuator.is_open is True
        time.sleep(1.5)  # open_duration_sec=1, plus margin
        assert actuator.is_open is False


class TestAngleToDuty:
    def test_zero_degrees(self, actuator):
        assert actuator._angle_to_duty(0) == pytest.approx(2.0)

    def test_90_degrees(self, actuator):
        assert actuator._angle_to_duty(90) == pytest.approx(7.0)

    def test_180_degrees(self, actuator):
        assert actuator._angle_to_duty(180) == pytest.approx(12.0)


class TestCleanup:
    def test_cleanup_closes_barrier(self, cfg):
        act = ActuatorController(cfg)
        act.open_barrier()
        act.cleanup()
        assert act.is_open is False
