"""SQLite persistence layer.

The database is the single source of truth: every state transition is committed
before any external side effect (notification delivery) is attempted, and every
side effect carries an idempotency key (the notification number) so that a
restart resumes exactly where it stopped.

All transactions are short and never contain network I/O, so a slow gateway
call cannot hold the database lock or delay an unrelated confirmation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    alarm_no                  TEXT PRIMARY KEY,
    content_hash              TEXT NOT NULL,
    payload_json              TEXT NOT NULL,
    primary_contact           TEXT NOT NULL,
    backup_contact            TEXT NOT NULL,
    ack_deadline_seconds      INTEGER NOT NULL,
    created_at                TEXT NOT NULL,
    created_real              REAL NOT NULL,
    -- Set only once the gateway has confirmed that the primary notification
    -- was accepted. No confirmed acceptance -> no timer -> no escalation.
    primary_accepted_at       TEXT,
    primary_accepted_real     REAL,
    primary_ack_deadline_at   TEXT,
    primary_ack_deadline_real REAL,
    confirmed_at              TEXT,
    confirmed_real            REAL,
    confirmed_by              TEXT,
    escalated_at              TEXT,
    escalated_real            REAL
);

CREATE TABLE IF NOT EXISTS notifications (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    alarm_no           TEXT NOT NULL REFERENCES alarms(alarm_no),
    notification_no    TEXT NOT NULL UNIQUE,
    role               TEXT NOT NULL CHECK (role IN ('primary', 'backup')),
    contact_json       TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    -- Everything needed to (re)post this notification after a restart.
    payload_json       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    created_real       REAL NOT NULL,
    -- pending   : persisted, not yet handed to the gateway
    -- in_flight : POST is on the wire, outcome not yet known
    -- unknown   : timeout / transport error / ambiguous reply; needs lookup
    -- accepted  : gateway confirmed acceptance
    -- failed    : gateway definitively rejected (no blind retry)
    -- cancelled : confirmation arrived before this backup was dispatched
    state              TEXT NOT NULL CHECK (
                           state IN ('pending','in_flight','unknown','accepted','failed','cancelled')
                       ),
    gateway_status     TEXT,
    detail             TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    -- Consecutive gateway lookups that answered "no such notification".
    -- Distinguishes "the POST never arrived" (safe to re-post) from "the POST
    -- is still being processed by a slow gateway" (must keep waiting).
    lookup_misses      INTEGER NOT NULL DEFAULT 0,
    created_by         TEXT NOT NULL,  -- initial | escalation | recovery | retry
    history_json       TEXT NOT NULL DEFAULT '[]',
    accepted_at        TEXT,
    accepted_real      REAL,
    last_attempt_at    TEXT,
    lease_until        REAL,
    next_attempt_at    REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_alarm_role
    ON notifications(alarm_no, role);

CREATE INDEX IF NOT EXISTS ix_notifications_due
    ON notifications(state, next_attempt_at);

CREATE INDEX IF NOT EXISTS ix_alarms_deadline
    ON alarms(primary_ack_deadline_real);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alarm_no    TEXT NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT
);

-- Cabinet temperature samples. (cabinet_id, sampled_real) is the natural
-- idempotency key: an exact re-send (same temperature) is a no-op, a
-- re-send with a different temperature is rejected before insert.
CREATE TABLE IF NOT EXISTS temp_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cabinet_id      TEXT NOT NULL,
    sampled_at      TEXT NOT NULL,
    sampled_real    REAL NOT NULL,
    temperature_c   REAL NOT NULL,
    received_at     TEXT NOT NULL,
    received_real   REAL NOT NULL,
    UNIQUE(cabinet_id, sampled_real)
);

CREATE INDEX IF NOT EXISTS ix_temp_samples_cabinet_time
    ON temp_samples(cabinet_id, sampled_real);

-- Persistent overheating verdicts. A streak_key deterministically identifies a
-- hot streak (cabinet + sampled time of its first reading), so re-evaluating
-- the same samples after a restart can never create a second event or alarm.
CREATE TABLE IF NOT EXISTS thermal_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_no            TEXT NOT NULL UNIQUE,
    cabinet_id          TEXT NOT NULL,
    streak_key          TEXT NOT NULL,
    -- pending    : three qualifying hot readings seen, verdict window still open
    -- confirmed  : window closed; the single linked alarm is (being) created
    -- interrupted: a late low/cooling reading broke the streak before closing
    status              TEXT NOT NULL CHECK (status IN ('pending','confirmed','interrupted')),
    first_sampled_at    TEXT NOT NULL,
    first_sampled_real  REAL NOT NULL,
    last_sampled_at     TEXT NOT NULL,
    last_sampled_real   REAL NOT NULL,
    -- The verdict becomes immutable once this watermark has been reached.
    window_closes_real   REAL NOT NULL,
    decided_at          TEXT,
    decided_real        REAL,
    trigger_readings_json TEXT NOT NULL DEFAULT '[]',
    alarm_no            TEXT,
    interrupted_reason  TEXT,
    created_at          TEXT NOT NULL,
    created_real        REAL NOT NULL,
    UNIQUE(cabinet_id, streak_key)
);

CREATE INDEX IF NOT EXISTS ix_thermal_events_cabinet
    ON thermal_events(cabinet_id, id);

CREATE INDEX IF NOT EXISTS ix_thermal_events_status
    ON thermal_events(status);
"""


class Database:
    """Synchronous SQLite wrapper with one serialised writer."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Additive, idempotent migrations for databases created by older builds."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(notifications)").fetchall()
        }
        if "lookup_misses" not in columns:
            self._conn.execute(
                "ALTER TABLE notifications ADD COLUMN lookup_misses INTEGER NOT NULL DEFAULT 0"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def utc_now() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def iso_from_real(value: float) -> str:
    dt = datetime.fromtimestamp(value, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        try:
            # Python 3.11 accepts a trailing "Z"; other ISO-8601 variants are
            # accepted too so gateway timestamps need no preprocessing.
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
