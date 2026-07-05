"""Tests for src.actuator — Actuator controller (push-model)."""

import pytest

from src.config import AppConfig, ActuatorConfig
from src.actuator import ActuatorController

class DummyDB:
    def __init__(self):
        self.commands = []
    
    def queue_command(self, cmd, source=""):
        self.commands.append((cmd, source))


@pytest.fixture
def cfg():
    return AppConfig(
        actuator=ActuatorConfig(
            servo_open_angle=90,
            servo_closed_angle=0,
            open_duration_sec=10,
            use_servo=True,
            use_relay=True,
        ),
    )


@pytest.fixture
def dummy_db():
    return DummyDB()


@pytest.fixture
def actuator(cfg, dummy_db):
    act = ActuatorController(cfg, db=dummy_db)
    yield act
    act.cleanup()


class TestBarrierState:
    def test_initially_closed(self, actuator):
        assert actuator.is_open is False

    def test_open_barrier_queues_command(self, actuator, dummy_db):
        actuator.open_barrier()
        assert actuator.is_open is True
        assert len(dummy_db.commands) == 1
        assert dummy_db.commands[0][0] == "open"

    def test_close_barrier_queues_command(self, actuator, dummy_db):
        actuator.open_barrier()
        assert actuator.is_open is True
        actuator.close_barrier()
        assert actuator.is_open is False
        assert len(dummy_db.commands) == 2
        assert dummy_db.commands[1][0] == "close"


class TestCleanup:
    def test_cleanup_closes_barrier_if_open(self, actuator, dummy_db):
        actuator.open_barrier()
        actuator.cleanup()
        assert actuator.is_open is False
        assert len(dummy_db.commands) == 2
        assert dummy_db.commands[1][0] == "close"
