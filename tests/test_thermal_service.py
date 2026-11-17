"""Direct service tests for thermal verdict logic with injected clocks.

These bypass HTTP and wall-clock waits: every call passes an explicit
``now_real``, so 120s windows, 60s gaps and restart recovery are all
deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.db import Database
from app.service import AlarmService
from app.thermal import (
    RejectedSampleError,
    SampleConflictError,
    ThermalService,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "thermal.db"),
        gateway_url="http://gw.test",
        thermal_sample_window_seconds=120.0,
        thermal_high_threshold_c=70.0,
        thermal_reset_threshold_c=65.0,
        thermal_consecutive_required=3,
        thermal_max_gap_seconds=60.0,
        # Simulated clocks sit far from wall time; never prune in these tests.
        thermal_retention_seconds=10**11,
    )


@pytest.fixture
def services(tmp_path):
    db = Database(_settings(tmp_path).database_path)
    settings = _settings(tmp_path)
    alarm_service = AlarmService(db)
    thermal = ThermalService(db, settings, alarm_service)
    yield db, thermal, alarm_service
    db.close()


def _ingest(thermal: ThermalService, cabinet: str, t: float, temp: float, now: float | None = None):
    return thermal.ingest_sample(cabinet, t, temp, now_real=now)


def test_out_of_order_samples_produce_same_verdict(services):
    db, thermal, alarm = services
    now = 1_000.0
    # Delivered out of order: t2, t0, t1 then t3, t4.
    _ingest(thermal, "C1", 980.0, 72.0, now)
    _ingest(thermal, "C1", 960.0, 71.0, now)
    _ingest(thermal, "C1", 970.0, 75.0, now)
    snap = thermal.cabinet_snapshot("C1", now_real=now)
    assert snap["current_hot_streak"] == 3
    assert snap["status"] == "pending_verdict"

    # Closing after the first reading's window edge confirms exactly one event.
    thermal.run_tick(now_real=960.0 + 120.0)
    events = thermal.list_events(cabinet_id="C1")
    assert len(events) == 1
    assert events[0]["status"] == "confirmed"
    # Readings are presented in sampling order, regardless of arrival order.
    assert [r["sampled_at"] for r in events[0]["trigger_readings"]] == [
        datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        for t in (960.0, 970.0, 980.0)
    ]


def test_restart_recomputes_verdict_and_creates_alarm_once(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    thermal = ThermalService(db, settings, AlarmService(db))
    now = 2_000.0
    for t, temp in [(1900.0, 71.0), (1920.0, 72.0), (1940.0, 73.0)]:
        _ingest(thermal, "C2", t, temp, now)
    db.close()

    # Simulate a restart: fresh objects, same SQLite file, then close the window.
    db2 = Database(settings.database_path)
    alarm2 = AlarmService(db2)
    thermal2 = ThermalService(db2, settings, alarm2)
    thermal2.run_tick(now_real=1900.0 + 120.0)
    events = thermal2.list_events(cabinet_id="C2")
    assert len(events) == 1 and events[0]["status"] == "confirmed"
    alarm_no = events[0]["alarm_no"]
    assert alarm_no is not None

    # A second restart and further ticks must not duplicate anything.
    db2.close()
    db3 = Database(settings.database_path)
    alarm3 = AlarmService(db3)
    thermal3 = ThermalService(db3, settings, alarm3)
    thermal3.run_tick(now_real=5_000.0)
    thermal3.run_tick(now_real=5_001.0)
    events = thermal3.list_events(cabinet_id="C2")
    assert len(events) == 1
    assert events[0]["alarm_no"] == alarm_no
    rows = db3.query("SELECT alarm_no FROM alarms")
    assert [r["alarm_no"] for r in rows] == [alarm_no]
    db3.close()


def test_crash_between_verdict_and_alarm_creation_is_resumed(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    alarm_service = AlarmService(db)
    thermal = ThermalService(db, settings, alarm_service)
    now = 3_000.0
    for t, temp in [(2900.0, 71.0), (2920.0, 72.0), (2940.0, 73.0)]:
        _ingest(thermal, "C3", t, temp, now)

    # Confirm the verdict in a transaction but skip alarm creation, mimicking a
    # crash right after the window-closing commit.
    with db.transaction() as conn:
        thermal._evaluate_cabinet(conn, "C3", 2900.0 + 120.0, allow_confirm=True)
    row = db.query_one("SELECT * FROM thermal_events WHERE cabinet_id = 'C3'")
    assert row["status"] == "confirmed" and row["alarm_no"] is None

    # Next tick resumes and creates the single alarm.
    thermal.run_tick(now_real=3_100.0)
    row = db.query_one("SELECT * FROM thermal_events WHERE cabinet_id = 'C3'")
    assert row["alarm_no"] is not None
    assert db.query_one("SELECT COUNT(*) AS n FROM alarms")["n"] == 1
    # Another tick does not create a second alarm.
    thermal.run_tick(now_real=3_200.0)
    assert db.query_one("SELECT COUNT(*) AS n FROM alarms")["n"] == 1


def test_ingest_conflict_and_window_errors(services):
    db, thermal, alarm = services
    now = 4_000.0
    _ingest(thermal, "C4", 3990.0, 70.0, now)  # boundary temperature counts as hot

    with pytest.raises(SampleConflictError):
        _ingest(thermal, "C4", 3990.0, 71.0, now)

    # Identical resend is accepted as a deduplicated no-op.
    again = _ingest(thermal, "C4", 3990.0, 70.0, now)
    assert again["deduplicated"] is True
    assert db.query_one("SELECT COUNT(*) AS n FROM temp_samples WHERE cabinet_id = 'C4'")["n"] == 1

    with pytest.raises(RejectedSampleError):
        _ingest(thermal, "C4", now - 121.0, 50.0, now)
    with pytest.raises(RejectedSampleError):
        _ingest(thermal, "C4", now + 1.0, 50.0, now)


def test_exactly_seventy_degrees_triggers_and_exactly_sixty_five_does_not_reset(services):
    db, thermal, alarm = services
    now = 5_000.0
    # Three readings at exactly 70C with small gaps.
    for t in (4900.0, 4920.0, 4940.0):
        _ingest(thermal, "C5", t, 70.0, now)
    thermal.run_tick(now_real=4900.0 + 120.0)
    assert len(thermal.list_events(cabinet_id="C5", status_filter="confirmed")) == 1

    # Exactly 65C must NOT re-arm (rule: strictly below 65C).
    _ingest(thermal, "C5", 4960.0, 65.0, now)
    for t, temp in [(4970.0, 80.0), (4980.0, 81.0), (4990.0, 82.0)]:
        _ingest(thermal, "C5", t, temp, now)
    thermal.run_tick(now_real=5000.0)
    assert len(thermal.list_events(cabinet_id="C5", status_filter="confirmed")) == 1

    # 64.9C re-arms; the next triple fires a new event with a new alarm. These
    # samples arrive at a later receipt time but stay within the 120s window.
    later = 5_100.0
    _ingest(thermal, "C5", 5010.0, 64.9, later)
    for t, temp in [(5020.0, 90.0), (5040.0, 91.0), (5060.0, 92.0)]:
        _ingest(thermal, "C5", t, temp, later)
    # The new streak's first reading (5020) closes at 5140.
    thermal.run_tick(now_real=5140.0)
    confirmed = thermal.list_events(cabinet_id="C5", status_filter="confirmed")
    assert len(confirmed) == 2
    assert len({e["alarm_no"] for e in confirmed}) == 2


def test_fourth_hot_reading_extends_the_pending_streak(services):
    """A fourth qualifying hot reading must extend, not destroy, the open
    pending verdict; the event is still confirmed from the original triple once
    the window closes, and no extra event is created."""
    db, thermal, alarm = services
    now = 5_500.0
    for t, temp in [(5400.0, 71.0), (5410.0, 72.0), (5420.0, 73.0), (5430.0, 74.0)]:
        _ingest(thermal, "C4H", t, temp, now)

    snap = thermal.cabinet_snapshot("C4H", now_real=now)
    assert snap["status"] == "pending_verdict"
    assert snap["current_hot_streak"] == 4
    events = thermal.list_events(cabinet_id="C4H")
    assert len(events) == 1 and events[0]["status"] == "pending"
    assert len(events[0]["trigger_readings"]) == 3

    # Window closes on the first reading (5400 + 120): exactly one confirmed
    # event, exactly one alarm.
    thermal.run_tick(now_real=5400.0 + 120.0)
    events = thermal.list_events(cabinet_id="C4H")
    assert len(events) == 1 and events[0]["status"] == "confirmed"
    assert events[0]["alarm_no"] is not None
    assert db.query_one("SELECT COUNT(*) AS n FROM alarms")["n"] == 1


def test_pending_then_interrupted_then_new_pending(services):
    db, thermal, alarm = services
    now = 6_000.0
    for t, temp in [(5900.0, 71.0), (5920.0, 72.0), (5940.0, 73.0)]:
        _ingest(thermal, "C6", t, temp, now)
    assert thermal.cabinet_snapshot("C6", now_real=now)["open_event"] is not None

    # Late cool reading inside the open window interrupts.
    _ingest(thermal, "C6", 5950.0, 60.0, now)
    events = thermal.list_events(cabinet_id="C6")
    assert len(events) == 1 and events[0]["status"] == "interrupted"
    assert thermal.cabinet_snapshot("C6", now_real=now)["open_event"] is None

    # A fresh hot triple creates a brand new pending event (interrupted one stays).
    for t, temp in [(5960.0, 90.0), (5970.0, 91.0), (5980.0, 92.0)]:
        _ingest(thermal, "C6", t, temp, now)
    events = thermal.list_events(cabinet_id="C6")
    assert len(events) == 2
    statuses = {e["status"] for e in events}
    assert statuses == {"pending", "interrupted"}
    thermal.run_tick(now_real=5960.0 + 120.0)
    confirmed = thermal.list_events(cabinet_id="C6", status_filter="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0]["trigger_readings"][0]["temperature_c"] == 90.0


def test_tick_idempotent_with_multiple_cabinets(services):
    db, thermal, alarm = services
    now = 7_000.0
    for cabinet in ("CA", "CB", "CC"):
        for t, temp in [(6900.0, 71.0), (6920.0, 72.0), (6940.0, 73.0)]:
            _ingest(thermal, cabinet, t, temp, now)
    keys = thermal.run_tick(now_real=6900.0 + 120.0)
    assert len(keys) == 3
    assert sorted(k.split("|")[0] for k in keys) == ["CA", "CB", "CC"]
    assert thermal.run_tick(now_real=8_000.0) == []
    assert db.query_one("SELECT COUNT(*) AS n FROM thermal_events WHERE status='confirmed'")["n"] == 3
    assert db.query_one("SELECT COUNT(*) AS n FROM alarms")["n"] == 3
    assert db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE role='primary'")["n"] == 3


def test_thermal_alarm_uses_configured_contacts_and_deadline(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "custom.db"),
        thermal_primary_name="主班 王工",
        thermal_primary_channel="phone",
        thermal_primary_address="10086",
        thermal_backup_name="备班 赵工",
        thermal_backup_channel="email",
        thermal_backup_address="b@example.com",
        thermal_ack_deadline_seconds=7,
    )
    db = Database(settings.database_path)
    alarm_service = AlarmService(db)
    thermal = ThermalService(db, settings, alarm_service)
    now = 8_000.0
    for t, temp in [(7900.0, 71.0), (7920.0, 72.0), (7940.0, 73.0)]:
        _ingest(thermal, "CC", t, temp, now)
    thermal.run_tick(now_real=7900.0 + 120.0)
    event = thermal.list_events(cabinet_id="CC")[0]
    view = alarm_service.snapshot(event["alarm_no"])
    assert view["primary_contact"] == {
        "name": "主班 王工",
        "channel": "phone",
        "address": "10086",
    }
    assert view["backup_contact"] == {
        "name": "备班 赵工",
        "channel": "email",
        "address": "b@example.com",
    }
    assert view["ack_deadline_seconds"] == 7
    db.close()
