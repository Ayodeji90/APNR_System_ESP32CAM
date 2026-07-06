"""
ANPR System — Database Layer (SQLite)

Creates and manages the SQLite database with tables:
  - vehicles  (whitelist / access control)
  - events    (plate recognition log)
  - settings  (runtime key-value store)
"""

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Dict, Any, Generator

from src.config import AppConfig, resolve_path

logger = logging.getLogger(__name__)

# ── SQL Schemas ─────────────────────────────────────────────
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vehicles (
    plate_text   TEXT PRIMARY KEY,
    owner_name   TEXT DEFAULT '',
    access_level TEXT DEFAULT 'resident',
    active       INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL DEFAULT (datetime('now')),
    plate_text            TEXT    DEFAULT '',
    decision              TEXT    NOT NULL DEFAULT 'UNKNOWN',
    ocr_confidence        REAL    DEFAULT 0.0,
    detection_confidence  REAL    DEFAULT 0.0,
    image_path            TEXT    DEFAULT '',
    note                  TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS telegram_commands (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    chat_id   INTEGER NOT NULL,
    command   TEXT NOT NULL,
    args      TEXT DEFAULT '',
    result    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pending_commands (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    command   TEXT NOT NULL,
    source    TEXT DEFAULT 'telegram',
    acknowledged INTEGER DEFAULT 0
);
"""


def _levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings (insertions/deletions/substitutions)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,        # deletion
                cur[j - 1] + 1,     # insertion
                prev[j - 1] + (ca != cb),  # substitution
            ))
        prev = cur
    return prev[-1]


class Database:
    """Thin wrapper around SQLite for the ANPR system."""

    def __init__(self, cfg: AppConfig):
        self.db_path = resolve_path(cfg, cfg.paths.database)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    # ── Connection helpers ──────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that yields a connection and always closes it."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            logger.info("Database initialised at %s", self.db_path)

    # ── Vehicle CRUD ────────────────────────────────────────
    def add_vehicle(
        self,
        plate_text: str,
        owner_name: str = "",
        access_level: str = "resident",
    ) -> None:
        """Insert or update a vehicle in the whitelist."""
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO vehicles (plate_text, owner_name, access_level, active)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(plate_text) DO UPDATE SET
                       owner_name   = excluded.owner_name,
                       access_level = excluded.access_level,
                       active       = 1""",
                (plate_text.upper().strip(), owner_name, access_level),
            )
            conn.commit()
            logger.info("Vehicle added/updated: %s", plate_text)

    def remove_vehicle(self, plate_text: str) -> None:
        """Soft-delete a vehicle (set active = 0)."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE vehicles SET active = 0 WHERE plate_text = ?",
                (plate_text.upper().strip(),),
            )
            conn.commit()
            logger.info("Vehicle deactivated: %s", plate_text)

    def delete_vehicle(self, plate_text: str) -> None:
        """Hard-delete a vehicle from the whitelist."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM vehicles WHERE plate_text = ?",
                (plate_text.upper().strip(),),
            )
            conn.commit()
            logger.info("Vehicle deleted: %s", plate_text)

    def is_whitelisted(self, plate_text: str) -> bool:
        """Return True if the plate is in the whitelist and active."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM vehicles WHERE plate_text = ? AND active = 1",
                (plate_text.upper().strip(),),
            ).fetchone()
            return row is not None

    def get_all_vehicles(self) -> List[Dict[str, Any]]:
        """Return all active vehicles."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM vehicles WHERE active = 1 ORDER BY plate_text"
            ).fetchall()
            return [dict(r) for r in rows]

    def find_whitelist_match(
        self, plate_text: str, max_distance: int = 0
    ) -> Optional[str]:
        """
        Find a whitelisted plate matching *plate_text*.

        Exact match first; if none and max_distance > 0, return the closest
        active plate within *max_distance* edits (Levenshtein). This absorbs
        the 1–2 character OCR errors typical of stylised plates in low light.
        Returns the matched whitelist plate, or None.
        """
        plate = plate_text.upper().strip()
        if not plate:
            return None
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT plate_text FROM vehicles WHERE active = 1"
            ).fetchall()
        plates = [r["plate_text"] for r in rows]

        if plate in plates:
            return plate
        if max_distance <= 0:
            return None

        best, best_dist = None, max_distance + 1
        for wp in plates:
            d = _levenshtein(plate, wp)
            if d < best_dist:
                best, best_dist = wp, d
        return best if best_dist <= max_distance else None

    # ── Event Logging ───────────────────────────────────────
    def log_event(
        self,
        plate_text: str,
        decision: str,
        ocr_confidence: float = 0.0,
        detection_confidence: float = 0.0,
        image_path: str = "",
        note: str = "",
    ) -> int:
        """Insert an event record. Returns the new row id."""
        with self._connection() as conn:
            cur = conn.execute(
                """INSERT INTO events
                   (plate_text, decision, ocr_confidence,
                    detection_confidence, image_path, note)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (plate_text, decision, ocr_confidence,
                 detection_confidence, image_path, note),
            )
            conn.commit()
            row_id = cur.lastrowid
            logger.info(
                "Event logged [%s]: plate=%s decision=%s conf=%.2f",
                row_id, plate_text, decision, ocr_confidence,
            )
            return row_id

    def get_recent_events(
        self, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return the most recent events, newest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_event_count(self) -> int:
        """Return total number of events."""
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM events").fetchone()
            return row["cnt"]

    def get_today_event_count(self) -> int:
        """Return number of events logged today."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM events WHERE timestamp LIKE ?",
                (f"{today}%",),
            ).fetchone()
            return row["cnt"]

    # ── Telegram Command Audit Log ───────────────────────────
    def log_telegram_command(
        self,
        chat_id: int,
        command: str,
        args: str = "",
        result: str = "",
    ) -> None:
        """Record a Telegram command in the audit log."""
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO telegram_commands
                   (chat_id, command, args, result)
                   VALUES (?, ?, ?, ?)""",
                (int(chat_id), command, args, result),
            )
            conn.commit()
            logger.debug(
                "Telegram command logged: chat=%s cmd=%s args=%s",
                chat_id, command, args,
            )

    def get_recent_telegram_commands(self, limit: int = 20) -> list:
        """Return most recent Telegram commands, newest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM telegram_commands ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Settings ────────────────────────────────────────────
    def get_setting(self, key: str, default: str = "") -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
            conn.commit()

    # ── Pending Commands (ESP32 push-model) ──────────────────
    def queue_command(self, command: str, source: str = "telegram") -> int:
        """Queue a command for the ESP32 to pick up on its next heartbeat.
        Returns the new row id."""
        with self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO pending_commands (command, source) VALUES (?, ?)",
                (command, source),
            )
            conn.commit()
            logger.info("Pending command queued: %s (source=%s)", command, source)
            return cur.lastrowid

    def get_pending_commands(self) -> list:
        """Return all unacknowledged pending commands, oldest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_commands WHERE acknowledged = 0 ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def acknowledge_command(self, command_id: int) -> None:
        """Mark a pending command as acknowledged by the ESP32."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE pending_commands SET acknowledged = 1 WHERE id = ?",
                (command_id,),
            )
            conn.commit()

    def acknowledge_all_commands(self) -> None:
        """Mark all pending commands as acknowledged."""
        with self._connection() as conn:
            conn.execute("UPDATE pending_commands SET acknowledged = 1")
            conn.commit()

