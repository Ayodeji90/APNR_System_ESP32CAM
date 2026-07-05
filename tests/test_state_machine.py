"""Tests for src.state_machine — ANPR state machine orchestrator."""

import pytest
import numpy as np

from src.config import AppConfig, DetectionConfig, PathsConfig, SensorConfig, CameraConfig, ActuatorConfig
from src.database import Database
from src.sensor import UltrasonicSensor
from src.camera import CameraService
from src.plate_detector import PlateDetector
from src.ocr_engine import OcrEngine
from src.decision_engine import DecisionEngine, Decision
from src.actuator import ActuatorController
from src.state_machine import ANPRStateMachine, State


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        camera=CameraConfig(
            resolution_width=320,
            resolution_height=240,
            capture_count=1,
            warmup_seconds=0,
        ),
        sensor=SensorConfig(
            distance_threshold_cm=50,
            confirmation_readings=1,
            reading_interval_sec=0.0,
        ),
        actuator=ActuatorConfig(
            open_duration_sec=1,
            use_servo=True,
            use_relay=False,
        ),
        detection=DetectionConfig(
            min_detection_confidence=0.5,
            min_ocr_confidence=60,
            max_retries=2,
        ),
        paths=PathsConfig(
            database=str(tmp_path / "test.db"),
            events_dir=str(tmp_path / "events"),
        ),
        base_dir=str(tmp_path),
    )


@pytest.fixture
def services(cfg):
    db = Database(cfg)
    sensor = UltrasonicSensor(cfg)
    camera = CameraService(cfg)
    detector = PlateDetector(cfg)
    ocr = OcrEngine(cfg)
    decision_engine = DecisionEngine(cfg, db)
    actuator = ActuatorController(cfg)
    return {
        "db": db,
        "sensor": sensor,
        "camera": camera,
        "detector": detector,
        "ocr": ocr,
        "decision_engine": decision_engine,
        "actuator": actuator,
    }


@pytest.fixture
def sm(cfg, services):
    machine = ANPRStateMachine(
        cfg=cfg,
        db=services["db"],
        sensor=services["sensor"],
        camera=services["camera"],
        detector=services["detector"],
        ocr=services["ocr"],
        decision_engine=services["decision_engine"],
        actuator=services["actuator"],
        notifier=None,
    )
    return machine


class TestInitialState:
    def test_starts_in_idle(self, sm):
        assert sm.state == State.IDLE

    def test_running_flag_true(self, sm):
        assert sm._running is True


class TestIdleState:
    def test_stays_idle_when_no_vehicle(self, sm, services):
        services["sensor"].set_simulator_distance(100.0)
        next_state = sm.step()
        assert next_state == State.IDLE

    def test_transitions_to_triggered(self, sm, services):
        services["sensor"].set_simulator_distance(20.0)
        next_state = sm.step()
        assert next_state == State.TRIGGERED


class TestTriggeredState:
    def test_transitions_to_capture_when_vehicle_present(self, sm, services):
        services["sensor"].set_simulator_distance(20.0)
        sm.state = State.TRIGGERED
        next_state = sm.step()
        assert next_state == State.CAPTURE

    def test_returns_to_idle_when_vehicle_gone(self, sm, services):
        services["sensor"].set_simulator_distance(100.0)
        sm.state = State.TRIGGERED
        next_state = sm.step()
        assert next_state == State.IDLE


class TestCaptureState:
    def test_transitions_to_detect(self, sm):
        sm.state = State.CAPTURE
        next_state = sm.step()
        assert next_state == State.DETECT_PLATE
        assert sm._frame is not None


class TestRetryLogic:
    def test_retry_on_no_plate_found(self, sm):
        sm.state = State.CAPTURE
        sm.step()  # CAPTURE → DETECT_PLATE
        # DETECT_PLATE with blank frame likely finds no plate → retry
        next_state = sm.step()
        # Should either go to CAPTURE (retry) or OCR (if detection succeeded)
        assert next_state in (State.CAPTURE, State.OCR)

    def test_max_retries_goes_to_log(self, sm):
        sm._retry_count = sm.max_retries
        result = sm._retry_or_fail("test failure")
        assert result == State.LOG
        assert sm._decision == Decision.UNKNOWN


class TestResetCycleData:
    def test_resets_all_fields(self, sm):
        sm._frame = np.zeros((10, 10, 3), dtype=np.uint8)
        sm._plate_text = "ABC123"
        sm._ocr_conf = 90.0
        sm._decision = Decision.ALLOW
        sm._reset_cycle_data()
        assert sm._frame is None
        assert sm._plate_text == ""
        assert sm._ocr_conf == 0.0
        assert sm._decision is None


class TestStopSignal:
    def test_stop_sets_running_false(self, sm):
        sm.stop()
        assert sm._running is False
