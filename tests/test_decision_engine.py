"""Tests for src.decision_engine — Access decision logic."""

import pytest

from src.config import AppConfig, DetectionConfig, PathsConfig
from src.database import Database
from src.decision_engine import DecisionEngine, Decision


@pytest.fixture
def db(tmp_path):
    cfg = AppConfig(
        paths=PathsConfig(
            database=str(tmp_path / "test.db"),
            events_dir=str(tmp_path / "events"),
        ),
        base_dir=str(tmp_path),
    )
    database = Database(cfg)
    database.add_vehicle("ABC123", "John", "resident")
    database.add_vehicle("XYZ789", "Jane", "staff")
    return database


@pytest.fixture
def engine(tmp_path, db):
    cfg = AppConfig(
        detection=DetectionConfig(
            min_detection_confidence=0.5,
            min_ocr_confidence=60,
        ),
        paths=PathsConfig(
            database=str(tmp_path / "test.db"),
            events_dir=str(tmp_path / "events"),
        ),
        base_dir=str(tmp_path),
    )
    return DecisionEngine(cfg, db)


class TestDecisionEngine:
    def test_allow_whitelisted(self, engine):
        result = engine.decide("ABC123", ocr_confidence=90, detection_confidence=0.8)
        assert result.decision == Decision.ALLOW

    def test_deny_not_whitelisted(self, engine):
        result = engine.decide("NOTHERE", ocr_confidence=90, detection_confidence=0.8)
        assert result.decision == Decision.DENY

    def test_unknown_empty_plate(self, engine):
        result = engine.decide("", ocr_confidence=90, detection_confidence=0.8)
        assert result.decision == Decision.UNKNOWN

    def test_unknown_low_detection_conf(self, engine):
        result = engine.decide("ABC123", ocr_confidence=90, detection_confidence=0.1)
        assert result.decision == Decision.UNKNOWN

    def test_unknown_low_ocr_conf_not_whitelisted(self, engine):
        # Low OCR confidence on an UNKNOWN plate → UNKNOWN (uncertain read).
        result = engine.decide("NOTHERE1", ocr_confidence=20, detection_confidence=0.8)
        assert result.decision == Decision.UNKNOWN

    def test_allow_whitelisted_ignores_low_ocr_conf(self, engine):
        # A whitelist match is stronger evidence than Tesseract's confidence,
        # which is unreliable on stylised plates — so it opens even at low conf.
        result = engine.decide("ABC123", ocr_confidence=4, detection_confidence=0.8)
        assert result.decision == Decision.ALLOW

    def test_boundary_detection_conf(self, engine):
        # Exactly at threshold
        result = engine.decide("ABC123", ocr_confidence=90, detection_confidence=0.5)
        assert result.decision == Decision.ALLOW

    def test_boundary_ocr_conf(self, engine):
        # Exactly at threshold
        result = engine.decide("ABC123", ocr_confidence=60, detection_confidence=0.8)
        assert result.decision == Decision.ALLOW

    def test_deny_has_reason(self, engine):
        result = engine.decide("UNKNOWN1", ocr_confidence=90, detection_confidence=0.8)
        assert "NOT on the whitelist" in result.reason
