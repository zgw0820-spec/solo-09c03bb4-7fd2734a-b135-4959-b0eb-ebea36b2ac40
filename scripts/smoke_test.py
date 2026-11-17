#!/usr/bin/env python3
"""End-to-end smoke test against a running API and gateway.

Assumes:
    gateway:  $GATEWAY_URL   (default http://127.0.0.1:9000)
    api:      $API_URL       (default http://127.0.0.1:8000)

It exercises the paths that are hard to see in unit tests:
  1. submit -> awaiting_confirmation -> confirm  (no escalation)
  2. submit -> escalate exactly once
  3. slow gateway: unknown receipt must not escalate; recovery must not double-page
  4. dropped receipt: gateway accepted, API must resolve via lookup, not re-post

Exits non-zero on the first violated assertion. Run it with -I.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:9000").rstrip("/")
RUN = str(int(time.time()))


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except ValueError:
            return exc.code, {}


def put_config(**config) -> None:
    status, _ = call("PUT", f"{GATEWAY}/config", config)
    assert status == 200, f"gateway config rejected: {status}"


def payload(alarm_no: str, deadline: int = 2) -> dict:
    return {
        "alarm_no": alarm_no,
        "content": "供电柜 A07 温度 78°C，超过告警阈值",
        "metadata": {"cabinet": "A07"},
        "primary_contact": {"name": "主值班员 张工", "channel": "app"},
        "backup_contact": {"name": "备班 李工", "channel": "sms", "address": "+8613800000000"},
        "ack_deadline_seconds": deadline,
    }


def wait_for(alarm_no: str, predicate, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        _, last = call("GET", f"{API}/alarms/{alarm_no}")
        if last and predicate(last):
            return last
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting on {alarm_no}: {json.dumps(last, ensure_ascii=False)}")


def gateway_records(alarm_no: str) -> list[dict]:
    _, body = call("GET", f"{GATEWAY}/notifications?alarm_no={alarm_no}")
    return body.get("items", [])


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok: {message}")


def main() -> int:
    put_config(slow_ms=0, drop_receipt=False, reject=False)

    print("[1] confirmation prevents escalation")
    status, body = call("POST", f"{API}/alarms", payload(f"{RUN}-CONFIRM", 3))
    check(status == 201, "alarm submitted")
    wait_for(f"{RUN}-CONFIRM", lambda v: v["status"] == "awaiting_confirmation")
    status, body = call(
        "POST", f"{API}/alarms/{RUN}-CONFIRM/confirm", {"confirmed_by": "张工"}
    )
    check(status == 200 and body["status"] == "confirmed", "confirmation recorded")
    time.sleep(3.5)
    _, body = call("GET", f"{API}/alarms/{RUN}-CONFIRM")
    check(body["status"] == "confirmed", "still confirmed after deadline")
    check(len(body["notifications"]) == 1, "no backup notification created")
    check(gateway_records(f"{RUN}-CONFIRM") and len(gateway_records(f"{RUN}-CONFIRM")) == 1,
          "gateway saw exactly one (primary) page")

    print("[2] overdue alarm escalates exactly once")
    call("POST", f"{API}/alarms", payload(f"{RUN}-ESC", 2))
    view = wait_for(f"{RUN}-ESC", lambda v: v["status"] == "escalated")
    time.sleep(1.5)
    _, view = call("GET", f"{API}/alarms/{RUN}-ESC")
    backups = [n for n in view["notifications"] if n["role"] == "backup"]
    check(len(backups) == 1, "exactly one backup notification row")
    records = [r for r in gateway_records(f"{RUN}-ESC") if r["role"] == "backup"]
    check(len(records) == 1, "backup paged exactly once at the gateway")

    print("[3] dropped receipt: no false escalation, recovery does not re-page")
    put_config(slow_ms=0, drop_receipt=True, drop_hold_ms=2500)
    call("POST", f"{API}/alarms", payload(f"{RUN}-DROP", 2))
    view = wait_for(
        f"{RUN}-DROP",
        lambda v: any(n["state"] in {"unknown", "in_flight"} for n in v["notifications"]),
    )
    check(view["primary_accepted_at"] is None, "no accepted receipt -> clock not started")
    _, view = call("GET", f"{API}/alarms/{RUN}-DROP")
    check(view["primary_accepted_at"] is None, "unknown receipt is never treated as delivered")
    time.sleep(2.5)
    _, view = call("GET", f"{API}/alarms/{RUN}-DROP")
    check(view["primary_accepted_at"] is not None, "receipt resolved via gateway lookup")
    primaries = [r for r in gateway_records(f"{RUN}-DROP") if r["role"] == "primary"]
    check(len(primaries) == 1, "receipt recovery did not duplicate the primary page")
    put_config(slow_ms=0, drop_receipt=False, reject=False)

    print("[4] same alarm retry is idempotent; changed content is rejected")
    status, _ = call("POST", f"{API}/alarms", payload(f"{RUN}-CONFIRM", 3))
    check(status == 200, "identical retry returns the existing alarm")
    changed = payload(f"{RUN}-CONFIRM", 3)
    changed["content"] = "完全不同的告警内容"
    status, _ = call("POST", f"{API}/alarms", changed)
    check(status == 409, "different content for the same alarm number is rejected")

    print("[5] notifications are queryable by number")
    _, view = call("GET", f"{API}/alarms/{RUN}-ESC")
    notification_no = view["notifications"][0]["notification_no"]
    status, single = call("GET", f"{API}/notifications/{notification_no}")
    check(status == 200 and single["notification_no"] == notification_no, "lookup by notification number")

    print("[6] cabinet samples: intake rules, idempotency and pending verdict")
    from datetime import UTC, datetime

    def iso_at(offset_seconds: float) -> str:
        return (
            datetime.fromtimestamp(datetime.now(UTC).timestamp() + offset_seconds, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    cabinet = f"{RUN}-CAB"
    status, body = call(
        "POST", f"{API}/cabinets/{cabinet}/samples",
        {"sampled_at": iso_at(5), "temperature_c": 50.0},
    )
    check(status == 422, "future sample is rejected")
    stamps = []
    for offset, temp in [(-30, 71.0), (-20, 74.0), (-10, 78.0)]:
        stamp = iso_at(offset)
        stamps.append(stamp)
        status, body = call(
            "POST", f"{API}/cabinets/{cabinet}/samples",
            {"sampled_at": stamp, "temperature_c": temp},
        )
        check(status == 202, f"hot sample at offset {offset}s accepted")
    status, body = call(
        "POST", f"{API}/cabinets/{cabinet}/samples",
        {"sampled_at": stamps[-1], "temperature_c": 78.0},
    )
    check(status == 200 and body.get("deduplicated") is True, "identical resend is idempotent")
    status, _ = call(
        "POST", f"{API}/cabinets/{cabinet}/samples",
        {"sampled_at": stamps[-1], "temperature_c": 79.0},
    )
    check(status == 409, "same instant with different temperature conflicts")
    status, cabinet_view = call("GET", f"{API}/cabinets/{cabinet}")
    check(
        status == 200 and cabinet_view["status"] == "pending_verdict",
        "cabinet shows a pending verdict while its window is open",
    )
    status, events = call("GET", f"{API}/thermal-events?cabinet_id={cabinet}")
    check(
        status == 200 and events["total"] == 1 and events["items"][0]["status"] == "pending",
        "thermal event is pending and carries the open verdict",
    )
    check(
        len(events["items"][0]["trigger_readings"]) == 3,
        "the pending event records the three trigger readings",
    )

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
