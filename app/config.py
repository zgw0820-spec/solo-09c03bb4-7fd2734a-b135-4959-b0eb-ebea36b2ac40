"""Runtime configuration for the alarm API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    database_path: str = field(default_factory=lambda: _str("DATABASE_PATH", "./data/alarms.db"))
    gateway_url: str = field(default_factory=lambda: _str("GATEWAY_URL", "http://127.0.0.1:9000"))
    # POST timeouts are deliberately short: a slow gateway must not block the
    # API, and an ambiguous outcome is resolved by looking the receipt up.
    post_timeout_seconds: float = field(default_factory=lambda: _float("GATEWAY_TIMEOUT_SECONDS", 2.0))
    lookup_timeout_seconds: float = field(default_factory=lambda: _float("GATEWAY_LOOKUP_TIMEOUT_SECONDS", 1.0))
    worker_interval_seconds: float = field(default_factory=lambda: _float("WORKER_INTERVAL_SECONDS", 0.1))
    delivery_lease_seconds: float = field(default_factory=lambda: _float("DELIVERY_LEASE_SECONDS", 10.0))
    max_backoff_seconds: float = field(default_factory=lambda: _float("DELIVERY_MAX_BACKOFF_SECONDS", 1.0))
    delivery_concurrency: int = field(default_factory=lambda: _int("DELIVERY_CONCURRENCY", 8))
    # Consecutive "not found" gateway lookups tolerated before re-posting an
    # unresolved notification. Guards against racing an original POST that a
    # slow gateway is still processing.
    not_found_misses_before_repost: int = field(
        default_factory=lambda: _int("NOT_FOUND_MISSES_BEFORE_REPOST", 3)
    )
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "info"))
    # When true, status reads resolve ambiguous receipts with the gateway before
    # rendering. Read-only: it never posts.
    reconcile_on_read: bool = field(default_factory=lambda: _bool("RECONCILE_ON_READ", True))
    # A single API instance owns the SQLite file, so on startup any in_flight
    # row was interrupted by the restart and may be reconciled immediately.
    # Set to false if running multiple replicas against one database.
    recover_inflight_on_startup: bool = field(
        default_factory=lambda: _bool("RECOVER_INFLIGHT_ON_STARTUP", True)
    )

    # ----------------------------------------------------------- thermal rules
    # Only samples whose sampled_at is within this many seconds of receipt are
    # accepted. The same value defines how long a conclusion window stays open:
    # once the oldest sample of a candidate streak falls outside the window, no
    # further sample can change that streak, so its verdict becomes final.
    thermal_sample_window_seconds: float = field(
        default_factory=lambda: _float("THERMAL_SAMPLE_WINDOW_SECONDS", 120.0)
    )
    # A sample at or above this temperature counts as "hot".
    thermal_high_threshold_c: float = field(
        default_factory=lambda: _float("THERMAL_HIGH_THRESHOLD_C", 70.0)
    )
    # After a confirmed event the cabinet must produce a reading strictly below
    # this temperature before another event may trigger (hysteresis re-arm).
    thermal_reset_threshold_c: float = field(
        default_factory=lambda: _float("THERMAL_RESET_THRESHOLD_C", 65.0)
    )
    # Consecutive hot samples required to trigger an event.
    thermal_consecutive_required: int = field(
        default_factory=lambda: _int("THERMAL_CONSECUTIVE_REQUIRED", 3)
    )
    # Maximum gap between adjacent samples in a qualifying hot streak.
    thermal_max_gap_seconds: float = field(
        default_factory=lambda: _float("THERMAL_MAX_GAP_SECONDS", 60.0)
    )
    # How often the background loop closes expired verdict windows and creates
    # the single alarm for confirmed thermal events.
    thermal_tick_interval_seconds: float = field(
        default_factory=lambda: _float("THERMAL_TICK_INTERVAL_SECONDS", 0.2)
    )
    # Raw samples older than now minus this retention are pruned; every reading
    # that belongs to a thermal event is also stored on the event and kept.
    thermal_retention_seconds: float = field(
        default_factory=lambda: _float("THERMAL_RETENTION_SECONDS", 3650 * 24 * 3600.0)
    )

    # Contacts and confirmation deadline used when a thermal event creates its
    # one alarm. The manual POST /alarms endpoint still takes these per request.
    thermal_primary_name: str = field(
        default_factory=lambda: _str("THERMAL_PRIMARY_CONTACT_NAME", "主值班员")
    )
    thermal_primary_channel: str = field(
        default_factory=lambda: _str("THERMAL_PRIMARY_CONTACT_CHANNEL", "app")
    )
    thermal_primary_address: str = field(
        default_factory=lambda: _str("THERMAL_PRIMARY_CONTACT_ADDRESS", "")
    )
    thermal_backup_name: str = field(
        default_factory=lambda: _str("THERMAL_BACKUP_CONTACT_NAME", "备班")
    )
    thermal_backup_channel: str = field(
        default_factory=lambda: _str("THERMAL_BACKUP_CONTACT_CHANNEL", "sms")
    )
    thermal_backup_address: str = field(
        default_factory=lambda: _str("THERMAL_BACKUP_CONTACT_ADDRESS", "")
    )
    thermal_ack_deadline_seconds: int = field(
        default_factory=lambda: _int("THERMAL_ACK_DEADLINE_SECONDS", 10)
    )
    # Test-only escape hatch: when true, POST samples accept an
    # X-Test-Now-Epoch header overriding the receipt time. Always off in
    # production; the test suite enables it via Settings.
    allow_test_clock: bool = field(default_factory=lambda: _bool("ALLOW_TEST_CLOCK", False))
