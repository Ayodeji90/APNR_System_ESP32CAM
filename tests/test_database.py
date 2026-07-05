"""Tests for src.database — SQLite database layer."""

import os
import pytest

from src.config import load_config, AppConfig, PathsConfig
from src.database import Database


@pytest.fixture
def db(tmp_path):
    """Create a Database instance backed by a temp directory."""
    cfg = AppConfig(
        paths=PathsConfig(
            database=str(tmp_path / "test.db"),
            events_dir=str(tmp_path / "events"),
        ),
        base_dir=str(tmp_path),
    )
    return Database(cfg)


class TestVehicleCRUD:
    def test_add_and_whitelist(self, db):
        db.add_vehicle("ABC123", "John Doe", "resident")
        assert db.is_whitelisted("ABC123") is True

    def test_case_insensitive(self, db):
        db.add_vehicle("abc123")
        assert db.is_whitelisted("ABC123") is True

    def test_not_whitelisted(self, db):
        assert db.is_whitelisted("XYZ999") is False

    def test_remove_vehicle(self, db):
        db.add_vehicle("DEF456")
        assert db.is_whitelisted("DEF456") is True
        db.remove_vehicle("DEF456")
        assert db.is_whitelisted("DEF456") is False

    def test_delete_vehicle(self, db):
        db.add_vehicle("GHI789")
        db.delete_vehicle("GHI789")
        assert db.is_whitelisted("GHI789") is False

    def test_get_all_vehicles(self, db):
        db.add_vehicle("A1")
        db.add_vehicle("B2")
        db.add_vehicle("C3")
        vehicles = db.get_all_vehicles()
        plates = [v["plate_text"] for v in vehicles]
        assert "A1" in plates
        assert "B2" in plates
        assert "C3" in plates

    def test_update_existing_vehicle(self, db):
        db.add_vehicle("UPD1", "Old Name", "resident")
        db.add_vehicle("UPD1", "New Name", "staff")
        vehicles = db.get_all_vehicles()
        upd = [v for v in vehicles if v["plate_text"] == "UPD1"][0]
        assert upd["owner_name"] == "New Name"
        assert upd["access_level"] == "staff"


class TestEventLogging:
    def test_log_event(self, db):
        row_id = db.log_event(
            plate_text="TEST1",
            decision="ALLOW",
            ocr_confidence=85.0,
            detection_confidence=0.8,
            image_path="events/2024-01-01/test.jpg",
        )
        assert row_id > 0

    def test_get_recent_events(self, db):
        db.log_event("A1", "ALLOW")
        db.log_event("B2", "DENY")
        db.log_event("C3", "UNKNOWN")
        events = db.get_recent_events(limit=2)
        assert len(events) == 2
        # Most recent first
        assert events[0]["plate_text"] == "C3"

    def test_event_count(self, db):
        assert db.get_event_count() == 0
        db.log_event("X", "ALLOW")
        db.log_event("Y", "DENY")
        assert db.get_event_count() == 2


class TestSettings:
    def test_get_set_setting(self, db):
        db.set_setting("threshold", "50")
        assert db.get_setting("threshold") == "50"

    def test_default_setting(self, db):
        assert db.get_setting("missing", "default") == "default"

    def test_update_setting(self, db):
        db.set_setting("key1", "value1")
        db.set_setting("key1", "value2")
        assert db.get_setting("key1") == "value2"


class TestPendingCommands:
    def test_queue_and_get_command(self, db):
        db.queue_command("open", "telegram")
        commands = db.get_pending_commands()
        assert len(commands) == 1
        assert commands[0]["command"] == "open"
        assert commands[0]["source"] == "telegram"

    def test_acknowledge_command(self, db):
        cmd_id = db.queue_command("close")
        commands = db.get_pending_commands()
        assert len(commands) == 1
        
        db.acknowledge_command(cmd_id)
        commands_after = db.get_pending_commands()
        assert len(commands_after) == 0

    def test_acknowledge_all_commands(self, db):
        db.queue_command("open")
        db.queue_command("close")
        assert len(db.get_pending_commands()) == 2
        
        db.acknowledge_all_commands()
        assert len(db.get_pending_commands()) == 0
