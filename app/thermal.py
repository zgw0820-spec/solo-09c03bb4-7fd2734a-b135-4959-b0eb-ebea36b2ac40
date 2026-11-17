"""Persistent overheating detection from cabinet temperature samples.

Verdict model
-------------
Samples are accepted only while they still fall inside the intake window
(``THERMAL_SAMPLE_WINDOW_SECONDS``, default 120s), so the watermark
``now - window`` is the hard frontier of mutability: no sample at or before it
can ever be added afterwards. A hot streak whose first reading has crossed that
frontier is therefore *final* and its verdict is closed; streaks still wholly
newer than the frontier stay open and are presented as ``pending``.

Rules (all derived deterministically from the stored samples, so a restart
recomputes exactly the same verdicts):

* A reading at or above ``high_threshold`` (70C) is hot. Three consecutive hot
  readings trigger only when every adjacent pair is at most ``max_gap`` (60s)
  apart.
* A reading below the reset threshold (65C) breaks the streak and re-arms the
  cabinet. A reading in between (65C <= t < 70C) breaks the streak without
  re-arming: after a confirmed event another event cannot fire until a real
  cooling reading arrives.
* A late low/cooling reading that arrives while a verdict is still open breaks
  the streak, so the open event is marked ``interrupted``. Once a window is
  closed the verdict is immutable: late samples for it are rejected at intake,
  never retroactively changing the stored conclusion.
* A confirmed event creates exactly one alarm through the existing
  ``AlarmService`` (idempotent deterministic alarm number), reusing the normal
  primary/backup notification and confirmation flow.

Every state transition is a short SQLite transaction containing no network I/O.
The alarm is created only after the event verdict is committed, and the alarm
number is deterministic, so a crash between the two steps resumes without
missing an event or creating a second notification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .db import Database, iso_from_real, parse_ts, utc_now
from .models import AlarmCreate

log = logging.getLogger("alarm.thermal")


class RejectedSampleError(Exception):
    """Outside the accepted intake window (past or future)."""


class SampleConflictError(Exception):
    """Same cabinet and sampling time, different temperature."""


class CabinetNotFoundError(Exception):
    pass


def _compact_token(epoch_seconds: float) -> str:
    dt = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return dt.strftime("%Y%m%dT%H%M%S") + f"{dt.microsecond // 1000:03d}"


class ThermalService:
    def __init__(self, db: Database, settings: Settings, alarm_service):
        self.db = db
        self.settings = settings
        self.alarm_service = alarm_service

    # ------------------------------------------------------------- validation

    def _check_window(self, sampled_real: float, now_real: float) -> None:
        window = self.settings.thermal_sample_window_seconds
        if sampled_real > now_real:
            raise RejectedSampleError(
                "sampled_at 在服务器当前时间之后，拒绝未来时间的采样"
            )
        if now_real - sampled_real >= window:
            raise RejectedSampleError(
                f"采样时间距接收时刻已达到或超过 {window:g} 秒，超出有效窗口，拒绝接收"
            )

    def _contacts(self) -> tuple[dict[str, Any], dict[str, Any], int]:
        allowed = {"app", "sms", "phone", "email"}
        p_channel = self.settings.thermal_primary_channel
        b_channel = self.settings.thermal_backup_channel
        if p_channel not in allowed:
            log.warning("invalid THERMAL_PRIMARY_CONTACT_CHANNEL=%r, falling back to app", p_channel)
            p_channel = "app"
        if b_channel not in allowed:
            log.warning("invalid THERMAL_BACKUP_CONTACT_CHANNEL=%r, falling back to sms", b_channel)
            b_channel = "sms"
        primary = {"name": self.settings.thermal_primary_name, "channel": p_channel}
        if self.settings.thermal_primary_address:
            primary["address"] = self.settings.thermal_primary_address
        backup = {"name": self.settings.thermal_backup_name, "channel": b_channel}
        if self.settings.thermal_backup_address:
            backup["address"] = self.settings.thermal_backup_address
        deadline = self.settings.thermal_ack_deadline_seconds
        if not 2 <= deadline <= 30:
            log.warning(
                "THERMAL_ACK_DEADLINE_SECONDS=%s outside [2,30]; clamping so the event is not lost",
                deadline,
            )
            deadline = min(30, max(2, deadline))
        return primary, backup, deadline

    # -------------------------------------------------------------- ingestion

    def ingest_sample(
        self,
        cabinet_id: str,
        sampled_real: float,
        temperature_c: float,
        *,
        now_real: float | None = None,
    ) -> dict[str, Any]:
        """Persist one sample and reconcile open verdicts for the cabinet.

        Returns the stored sample row with a ``deduplicated`` flag. Raises
        ``RejectedSampleError`` (maps to 422) or ``SampleConflictError`` (409).
        """
        now_real = now_real if now_real is not None else parse_ts(utc_now())
        self._check_window(sampled_real, now_real)
        now_iso = utc_now()

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM temp_samples WHERE cabinet_id = ? AND sampled_real = ?",
                (cabinet_id, sampled_real),
            ).fetchone()
            if existing is not None:
                if abs(float(existing["temperature_c"]) - float(temperature_c)) < 1e-9:
                    row = dict(existing)
                    row["deduplicated"] = True
                    return row
                raise SampleConflictError(
                    f"柜号 {cabinet_id} 在该采样时刻已有温度 "
                    f"{existing['temperature_c']}°C，异温度重送 {temperature_c}°C 被拒绝"
                )

            conn.execute(
                """
                INSERT INTO temp_samples
                    (cabinet_id, sampled_at, sampled_real, temperature_c,
                     received_at, received_real)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cabinet_id,
                    iso_from_real(sampled_real),
                    sampled_real,
                    temperature_c,
                    now_iso,
                    now_real,
                ),
            )
            # Only open verdicts are reconciled on the write path; closing a
            # window belongs to the background tick so alarm creation never
            # blocks the collector.
            self._evaluate_cabinet(conn, cabinet_id, now_real, allow_confirm=False)
            row = conn.execute(
                "SELECT * FROM temp_samples WHERE cabinet_id = ? AND sampled_real = ?",
                (cabinet_id, sampled_real),
            ).fetchone()
            result = dict(row)
            result["deduplicated"] = False
            return result

    # ------------------------------------------------------------ folding core

    def _load_samples(self, conn, cabinet_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM temp_samples WHERE cabinet_id = ? ORDER BY sampled_real, id",
            (cabinet_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _initial_armed(self, conn, cabinet_id: str) -> bool:
        last_confirmed = conn.execute(
            "SELECT first_sampled_real AS since FROM thermal_events "
            " WHERE cabinet_id = ? AND status = 'confirmed' "
            " ORDER BY first_sampled_real DESC LIMIT 1",
            (cabinet_id,),
        ).fetchone()
        since = last_confirmed["since"] if last_confirmed is not None else None
        if since is None:
            return True
        cooled = conn.execute(
            "SELECT 1 FROM temp_samples WHERE cabinet_id = ? AND sampled_real > ? "
            " AND temperature_c < ? LIMIT 1",
            (cabinet_id, since, self.settings.thermal_reset_threshold_c),
        ).fetchone()
        return cooled is not None

    def _fold(
        self,
        samples: list[dict[str, Any]],
        watermark: float,
        *,
        initially_armed: bool = True,
    ) -> tuple[bool, list[list[dict[str, Any]]], list[dict[str, Any]] | None, int]:
        """Single deterministic pass over a cabinet's samples.

        Returns (armed, confirmed_triples, open_candidate_triple, streak_len).
        A triple is "confirmed" by the fold only when its first reading is at or
        behind the watermark (no later sample can ever alter it).
        """
        s = self.settings
        need = s.thermal_consecutive_required
        armed = initially_armed
        buffer: list[dict[str, Any]] = []
        confirmed: list[list[dict[str, Any]]] = []

        for sample in samples:
            temp = float(sample["temperature_c"])
            if temp >= s.thermal_high_threshold_c:
                if buffer and (
                    sample["sampled_real"] - buffer[-1]["sampled_real"]
                ) > s.thermal_max_gap_seconds:
                    # Adjacent hot readings too far apart: the previous streak is
                    # dead; this reading starts a brand new one.
                    buffer = [sample]
                else:
                    buffer.append(sample)
                if len(buffer) >= need:
                    triple = buffer[:need]
                    if triple[0]["sampled_real"] <= watermark:
                        # The window has closed on this triple: the verdict is
                        # final, so consume the streak.
                        if armed:
                            confirmed.append(triple)
                            armed = False
                        buffer = []
                    # While the window is still open the buffer is deliberately
                    # kept intact: further hot readings extend the same streak
                    # instead of destroying its pending verdict.
            elif temp < s.thermal_reset_threshold_c:
                buffer = []
                armed = True
            else:
                # 65C <= t < 70C: not hot, so it breaks any streak, but it does
                # not cool below the reset threshold, so the arm state is kept.
                buffer = []

        candidate: list[dict[str, Any]] | None = None
        if len(buffer) >= need and buffer[0]["sampled_real"] > watermark and armed:
            candidate = buffer[:need]
        return armed, confirmed, candidate, len(buffer)

    @staticmethod
    def _streak_key(cabinet_id: str, first_real: float) -> str:
        return f"{cabinet_id}|{first_real:.3f}"

    def _event_no(self, cabinet_id: str, first_real: float) -> str:
        return f"TE-{cabinet_id}-{_compact_token(first_real)}"

    def _alarm_no(self, cabinet_id: str, first_real: float) -> str:
        return f"TA-{cabinet_id}-{_compact_token(first_real)}"

    @staticmethod
    def _reading_view(sample: dict[str, Any]) -> dict[str, Any]:
        return {
            "sampled_at": sample["sampled_at"],
            "temperature_c": float(sample["temperature_c"]),
            "received_at": sample["received_at"],
        }

    def _interrupt_reason(
        self, conn, cabinet_id: str, pending_row, now_real: float
    ) -> str:
        # The streak claimed consecutiveness from its first reading, so any
        # non-hot sample at or after that first reading breaks it. Out-of-order
        # arrivals can place the breaker *between* streak readings, not just
        # after the last one, hence the lower bound is first_sampled_real.
        breaker = conn.execute(
            "SELECT temperature_c FROM temp_samples "
            " WHERE cabinet_id = ? AND sampled_real >= ? AND sampled_real <= ? "
            "   AND temperature_c < ? ORDER BY sampled_real LIMIT 1",
            (
                cabinet_id,
                pending_row["first_sampled_real"],
                now_real,
                self.settings.thermal_high_threshold_c,
            ),
        ).fetchone()
        if breaker is not None:
            if float(breaker["temperature_c"]) < self.settings.thermal_reset_threshold_c:
                return (
                    f"迟到的低温读数 {breaker['temperature_c']:g}°C（低于 "
                    f"{self.settings.thermal_reset_threshold_c:g}°C）打断了连续高温"
                )
            return (
                f"迟到读数 {breaker['temperature_c']:g}°C 低于 "
                f"{self.settings.thermal_high_threshold_c:g}°C，连续高温被打断"
            )
        return "连续高温未在判定窗口内保持（相邻间隔超限或被后续读数打断）"

    def _evaluate_cabinet(
        self, conn, cabinet_id: str, now_real: float, *, allow_confirm: bool
    ) -> list[str]:
        """Reconcile thermal event rows for one cabinet.

        With ``allow_confirm`` set (background tick), streaks whose window has
        closed are persisted as confirmed events. The write path only creates
        pending events and marks broken ones interrupted. Returns the streak
        keys that this call newly confirmed.
        """
        s = self.settings
        watermark = now_real - s.thermal_sample_window_seconds
        samples = self._load_samples(conn, cabinet_id)
        if not samples:
            return []

        initially_armed = self._initial_armed(conn, cabinet_id)
        _armed, confirmed_triples, candidate, _streak_len = self._fold(
            samples, watermark, initially_armed=initially_armed
        )

        stored = {
            row["streak_key"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM thermal_events WHERE cabinet_id = ?", (cabinet_id,)
            ).fetchall()
        }
        confirmed_keys: list[str] = []
        now_iso = utc_now()

        for triple in confirmed_triples:
            first_real = triple[0]["sampled_real"]
            last_real = triple[-1]["sampled_real"]
            key = self._streak_key(cabinet_id, first_real)
            readings_json = json.dumps(
                [self._reading_view(r) for r in triple], ensure_ascii=False
            )
            closes_real = first_real + s.thermal_sample_window_seconds
            existing = stored.get(key)
            if existing is None:
                if not allow_confirm:
                    # Should not normally happen: the tick closes windows first.
                    continue
                try:
                    conn.execute(
                        """
                        INSERT INTO thermal_events (
                            event_no, cabinet_id, streak_key, status,
                            first_sampled_at, first_sampled_real,
                            last_sampled_at, last_sampled_real,
                            window_closes_real, decided_at, decided_real,
                            trigger_readings_json, created_at, created_real
                        ) VALUES (?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._event_no(cabinet_id, first_real),
                            cabinet_id,
                            key,
                            triple[0]["sampled_at"],
                            first_real,
                            triple[-1]["sampled_at"],
                            last_real,
                            closes_real,
                            now_iso,
                            now_real,
                            readings_json,
                            now_iso,
                            now_real,
                        ),
                    )
                except sqlite3.IntegrityError:
                    log.info("thermal event for streak %s already exists", key)
                confirmed_keys.append(key)
            elif existing["status"] == "pending":
                if not allow_confirm:
                    continue
                conn.execute(
                    """
                    UPDATE thermal_events
                       SET status = 'confirmed', decided_at = ?, decided_real = ?,
                           trigger_readings_json = ?, window_closes_real = ?
                     WHERE id = ? AND status = 'pending'
                    """,
                    (now_iso, now_real, readings_json, closes_real, existing["id"]),
                )
                confirmed_keys.append(key)
            # existing confirmed/interrupted: immutable, leave untouched.

        candidate_key = (
            self._streak_key(cabinet_id, candidate[0]["sampled_real"])
            if candidate is not None
            else None
        )
        confirmed_key_set = {
            self._streak_key(cabinet_id, triple[0]["sampled_real"])
            for triple in confirmed_triples
        }

        for row in stored.values():
            if row["status"] != "pending":
                continue
            if row["streak_key"] in confirmed_key_set:
                continue  # handled above (or already confirmed)
            if candidate_key is not None and row["streak_key"] == candidate_key:
                continue  # verdict window still open, streak still intact
            reason = self._interrupt_reason(conn, cabinet_id, row, now_real)
            conn.execute(
                """
                UPDATE thermal_events
                   SET status = 'interrupted', decided_at = ?, decided_real = ?,
                       interrupted_reason = ?
                 WHERE id = ? AND status = 'pending'
                """,
                (now_iso, now_real, reason, row["id"]),
            )

        if (
            candidate is not None
            and candidate_key not in stored
            and candidate_key not in confirmed_key_set
        ):
            first_real = candidate[0]["sampled_real"]
            last_real = candidate[-1]["sampled_real"]
            try:
                conn.execute(
                    """
                    INSERT INTO thermal_events (
                        event_no, cabinet_id, streak_key, status,
                        first_sampled_at, first_sampled_real,
                        last_sampled_at, last_sampled_real,
                        window_closes_real, trigger_readings_json,
                        created_at, created_real
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._event_no(cabinet_id, first_real),
                        cabinet_id,
                        candidate_key,
                        candidate[0]["sampled_at"],
                        first_real,
                        candidate[-1]["sampled_at"],
                        last_real,
                        first_real + s.thermal_sample_window_seconds,
                        json.dumps(
                            [self._reading_view(r) for r in candidate], ensure_ascii=False
                        ),
                        now_iso,
                        now_real,
                    ),
                )
            except sqlite3.IntegrityError:
                # Another concurrent evaluation already inserted this streak.
                log.info("thermal event for streak %s already exists", candidate_key)
        return confirmed_keys

    # ------------------------------------------------------------ background

    def run_tick(self, now_real: float | None = None) -> list[str]:
        """Close due verdict windows, prune ancient raw samples, then create the
        single alarm for every newly confirmed event.

        All database reconciliation happens in one short transaction; alarm
        creation (which opens its own transaction and may later trigger network
        delivery via the existing worker) happens afterwards. A crash at any
        point simply repeats this tick: streak keys and alarm numbers are
        deterministic and every step is idempotent.
        """
        now_real = now_real if now_real is not None else parse_ts(utc_now())
        with self.db.transaction() as conn:
            cabinets = [
                r["cabinet_id"]
                for r in conn.execute(
                    "SELECT DISTINCT cabinet_id FROM temp_samples ORDER BY cabinet_id"
                ).fetchall()
            ]
            confirmed_keys: list[str] = []
            for cabinet_id in cabinets:
                confirmed_keys.extend(
                    self._evaluate_cabinet(conn, cabinet_id, now_real, allow_confirm=True)
                )
            retention_cut = now_real - self.settings.thermal_retention_seconds
            conn.execute(
                "DELETE FROM temp_samples WHERE sampled_real < ?", (retention_cut,)
            )
            # Confirmed events whose alarm creation was interrupted by an earlier
            # crash must still get their single alarm (idempotent alarm number).
            pending_links = [
                r["streak_key"]
                for r in conn.execute(
                    "SELECT streak_key FROM thermal_events "
                    " WHERE status = 'confirmed' AND alarm_no IS NULL ORDER BY id"
                ).fetchall()
            ]

        for key in dict.fromkeys([*confirmed_keys, *pending_links]):
            self._ensure_alarm(key)
        return confirmed_keys

    def _ensure_alarm(self, streak_key: str) -> None:
        row = self.db.query_one(
            "SELECT * FROM thermal_events WHERE streak_key = ?", (streak_key,)
        )
        if row is None or row["status"] != "confirmed" or row["alarm_no"] is not None:
            return
        cabinet_id = row["cabinet_id"]
        first_real = row["first_sampled_real"]
        alarm_no = self._alarm_no(cabinet_id, first_real)
        readings = json.loads(row["trigger_readings_json"])
        primary, backup, deadline = self._contacts()
        temps = "、".join(f"{r['temperature_c']:g}°C" for r in readings)
        payload = AlarmCreate(
            alarm_no=alarm_no,
            content=(
                f"供电柜 {cabinet_id} 持续过热：连续 {len(readings)} 笔采样达到 "
                f"{self.settings.thermal_high_threshold_c:g}°C 且相邻间隔不超过 "
                f"{self.settings.thermal_max_gap_seconds:g} 秒"
                f"（{row['first_sampled_at']} 至 {row['last_sampled_at']}；{temps}）"
            ),
            metadata={
                "source": "thermal_event",
                "event_no": row["event_no"],
                "cabinet_id": cabinet_id,
                "trigger_readings": readings,
                "high_threshold_c": self.settings.thermal_high_threshold_c,
                "reset_threshold_c": self.settings.thermal_reset_threshold_c,
                "max_gap_seconds": self.settings.thermal_max_gap_seconds,
            },
            primary_contact=primary,
            backup_contact=backup,
            ack_deadline_seconds=deadline,
        )
        try:
            _view, deduplicated = self.alarm_service.submit_alarm(payload)
        except Exception:
            # The event stays confirmed without an alarm link; the next tick
            # retries. Nothing is lost because the verdict is durable.
            log.exception("failed to create alarm for thermal event %s", row["event_no"])
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE thermal_events SET alarm_no = ? WHERE id = ? AND alarm_no IS NULL",
                (alarm_no, row["id"]),
            )
        log.info(
            "thermal event %s confirmed; alarm %s %s",
            row["event_no"],
            alarm_no,
            "reused" if deduplicated else "created",
        )

    # ------------------------------------------------------------ queries

    def list_cabinets(self, now_real: float | None = None) -> list[dict[str, Any]]:
        explicit = now_real is not None
        rows = self.db.query(
            "SELECT DISTINCT cabinet_id FROM temp_samples ORDER BY cabinet_id"
        )
        result = []
        for r in rows:
            if explicit:
                result.append(self.cabinet_snapshot(r["cabinet_id"], now_real=now_real))
            else:
                # Anchor the verdict watermark to this cabinet's latest receipt:
                # no sample can arrive "in the future" relative to that, and a
                # cabinet with no traffic keeps its last open verdict visible.
                latest = self.db.query_one(
                    "SELECT MAX(received_real) AS r FROM temp_samples WHERE cabinet_id = ?",
                    (r["cabinet_id"],),
                )
                anchor = latest["r"] if latest is not None else parse_ts(utc_now())
                result.append(self.cabinet_snapshot(r["cabinet_id"], now_real=anchor))
        return result

    def cabinet_snapshot(
        self, cabinet_id: str, *, now_real: float | None = None
    ) -> dict[str, Any]:
        if now_real is None:
            # Default to this cabinet's latest receipt time, not the wall clock:
            # the verdict watermark must be evaluated relative to the data the
            # system has actually seen for the cabinet.
            latest = self.db.query_one(
                "SELECT MAX(received_real) AS r FROM temp_samples WHERE cabinet_id = ?",
                (cabinet_id,),
            )
            now_real = latest["r"] if latest is not None else parse_ts(utc_now())
        with self.db.transaction() as conn:
            sample_rows = self._load_samples(conn, cabinet_id)
            if not sample_rows:
                raise CabinetNotFoundError(cabinet_id)
            watermark = now_real - self.settings.thermal_sample_window_seconds
            initially_armed = self._initial_armed(conn, cabinet_id)
            _armed_fold, _confirmed, candidate, streak_len = self._fold(
                sample_rows, watermark, initially_armed=initially_armed
            )
            armed = initially_armed and _armed_fold
            open_row = None
            if candidate is not None:
                key = self._streak_key(cabinet_id, candidate[0]["sampled_real"])
                open_row = conn.execute(
                    "SELECT * FROM thermal_events WHERE cabinet_id = ? AND streak_key = ?",
                    (cabinet_id, key),
                ).fetchone()
            latest_confirmed = conn.execute(
                "SELECT event_no FROM thermal_events "
                " WHERE cabinet_id = ? AND status = 'confirmed' ORDER BY id DESC LIMIT 1",
                (cabinet_id,),
            ).fetchone()
            in_window = [s for s in sample_rows if s["sampled_real"] > watermark]

        if open_row is not None:
            status, status_text = "pending_verdict", "过热判定待定（判定窗口尚未关闭）"
        elif not armed:
            status, status_text = "cooldown", "已触发过热，等待降至 65°C 以下重新布防"
        else:
            status, status_text = "monitoring", "监测中"

        last_sample = sample_rows[-1]
        return {
            "cabinet_id": cabinet_id,
            "status": status,
            "status_text": status_text,
            "armed": armed,
            "sample_count": len(in_window),
            "last_sample": self._reading_view(last_sample),
            "current_hot_streak": streak_len,
            "open_event": self.event_view(dict(open_row)) if open_row is not None else None,
            "latest_confirmed_event_no": (
                latest_confirmed["event_no"] if latest_confirmed is not None else None
            ),
            "window_seconds": self.settings.thermal_sample_window_seconds,
        }

    def list_events(
        self,
        *,
        cabinet_id: str | None = None,
        status_filter: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM thermal_events WHERE 1=1"
        params: list[Any] = []
        if cabinet_id:
            sql += " AND cabinet_id = ?"
            params.append(cabinet_id)
        if status_filter:
            sql += " AND status = ?"
            params.append(status_filter)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        return [self.event_view(dict(r)) for r in rows]

    def get_event(self, event_no: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM thermal_events WHERE event_no = ?", (event_no,)
        )
        if row is None:
            raise CabinetNotFoundError(event_no)
        return self.event_view(dict(row))

    def event_view(self, row: dict[str, Any]) -> dict[str, Any]:
        status_text = {
            "pending": "待定：判定窗口未关闭，结论可能被迟到读数改变",
            "confirmed": "已确认：持续过热成立，已创建关联告警",
            "interrupted": "未成立：连续高温在窗口关闭前被打断",
        }[row["status"]]
        readings = json.loads(row.get("trigger_readings_json") or "[]")
        view = {
            "event_no": row["event_no"],
            "cabinet_id": row["cabinet_id"],
            "status": row["status"],
            "status_text": status_text,
            "first_sampled_at": row["first_sampled_at"],
            "last_sampled_at": row["last_sampled_at"],
            "window_closes_at": iso_from_real(row["window_closes_real"]),
            "decided_at": row.get("decided_at"),
            "interrupted_reason": row.get("interrupted_reason"),
            "trigger_readings": readings,
            "alarm_no": row.get("alarm_no"),
            "alarm": None,
        }
        if row.get("alarm_no"):
            try:
                view["alarm"] = self.alarm_service.snapshot(row["alarm_no"])
            except Exception:  # noqa: BLE001 - event view must still render
                log.warning("linked alarm %s snapshot failed", row["alarm_no"], exc_info=True)
        return view
