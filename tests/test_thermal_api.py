"""End-to-end overheating detection through the HTTP API.

The thermal background loop is paused and verdict windows are closed by driving
``ThermalService.run_tick`` with an injected clock, so the 120s window semantics
are tested deterministically without real waiting. One test additionally runs
the real background loop with a shortened window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.config import Settings
from tests.conftest import ApiHarness
from tests.fake_gateway import FakeGateway


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
async def thermal_api(tmp_path, fake_gateway: FakeGateway):
    async with ApiHarness(tmp_path, fake_gateway) as harness:
        # Deterministic tests drive run_tick and receipt time themselves; keep
        # the full 120s window so simulated timelines never auto-close early.
        harness.app.state.dispatcher.thermal_paused = True
        # The endpoint reads app.state.settings; enable the test-clock header
        # there (the production default keeps ALLOW_TEST_CLOCK off).
        harness.app.state.settings = Settings(
            **{**harness.settings.__dict__, "allow_test_clock": True}
        )
        harness.t0 = 1_700_000_000.0  # fixed simulated epoch, independent of wall clock
        yield harness


async def _sample(
    api: ApiHarness,
    cabinet: str,
    offset: float,
    temp: float,
    *,
    received_at: float | None = None,
):
    # By default the sample is received "now" on the simulated clock; tests may
    # pass an explicit receipt time to model late arrivals.
    now_epoch = api.t0 if received_at is None else received_at
    return await api.client.post(
        f"/cabinets/{cabinet}/samples",
        json={"sampled_at": iso(api.t0 + offset), "temperature_c": temp},
        headers={"X-Test-Now-Epoch": str(now_epoch)},
    )


def _tick(api: ApiHarness, now_real: float) -> list[str]:
    return api.app.state.thermal.run_tick(now_real=now_real)


async def _wait_count(api: ApiHarness, role: str, count: int, timeout: float = 6.0) -> bool:
    """Poll the fake gateway until it holds ``count`` notifications of a role."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(api.fake.role_notifications(role)) == count:
            return True
        await asyncio.sleep(0.02)
    return len(api.fake.role_notifications(role)) == count


async def test_sample_window_bounds_reject_old_and_future_samples(thermal_api: ApiHarness):
    api = thermal_api
    accepted = await _sample(api, "C-WIN", offset=-10, temp=50.0)
    assert accepted.status_code == 202, accepted.text

    too_old = await _sample(api, "C-WIN", offset=-121, temp=50.0)
    assert too_old.status_code == 422
    assert "窗口" in too_old.json()["detail"]

    future = await _sample(api, "C-WIN", offset=5, temp=50.0)
    assert future.status_code == 422

    # Exactly 120s old has reached the window boundary and is refused; 119s is in.
    boundary = await _sample(api, "C-WIN", offset=-120, temp=50.0)
    assert boundary.status_code == 422
    inside = await _sample(api, "C-WIN", offset=-119, temp=50.0)
    assert inside.status_code in (200, 202), inside.text


async def test_naive_datetime_is_rejected(thermal_api: ApiHarness):
    api = thermal_api
    naive = datetime.fromtimestamp(api.t0 - 1).replace(tzinfo=None).isoformat(timespec="milliseconds")
    resp = await api.client.post(
        "/cabinets/C-NAIVE/samples",
        json={"sampled_at": naive, "temperature_c": 71.0},
    )
    assert resp.status_code == 422


async def test_duplicate_sample_is_idempotent_and_different_temperature_conflicts(thermal_api: ApiHarness):
    api = thermal_api
    first = await _sample(api, "C-DUP", offset=-5, temp=72.5)
    assert first.status_code == 202
    assert first.json()["deduplicated"] is False

    retry = await _sample(api, "C-DUP", offset=-5, temp=72.5)
    assert retry.status_code == 200
    assert retry.json()["deduplicated"] is True

    conflict = await _sample(api, "C-DUP", offset=-5, temp=73.0)
    assert conflict.status_code == 409

    cabinet = (await api.client.get("/cabinets/C-DUP")).json()
    assert cabinet["sample_count"] == 1  # identical resend stored once, conflict refused


async def test_three_hot_readings_show_pending_until_window_closes(thermal_api: ApiHarness):
    api = thermal_api
    for offset, temp in [(-30, 71.0), (-20, 75.0), (-10, 80.0)]:
        resp = await _sample(api, "C-PEND", offset=offset, temp=temp)
        assert resp.status_code == 202

    cabinet = (await api.client.get("/cabinets/C-PEND")).json()
    assert cabinet["status"] == "pending_verdict"
    assert cabinet["current_hot_streak"] == 3
    assert cabinet["open_event"] is not None
    assert cabinet["open_event"]["status"] == "pending"
    assert len(cabinet["open_event"]["trigger_readings"]) == 3

    events = (await api.client.get("/thermal-events?cabinet_id=C-PEND")).json()
    assert events["total"] == 1
    assert events["items"][0]["status"] == "pending"
    assert events["items"][0]["alarm_no"] is None


async def test_window_close_confirms_event_and_creates_exactly_one_alarm(thermal_api: ApiHarness):
    api = thermal_api
    times = [-30, -20, -10]
    for offset, temp in zip(times, [71.0, 72.0, 73.0]):
        await _sample(api, "C-FIRE", offset=offset, temp=temp)

    # First reading at t0-30 closes at t0+90.
    assert _tick(api, api.t0 + 89) == []
    view = (await api.client.get("/cabinets/C-FIRE")).json()
    assert view["status"] == "pending_verdict"

    keys = _tick(api, api.t0 + 90)
    assert len(keys) == 1
    assert keys[0].startswith("C-FIRE|")
    assert abs(float(keys[0].split("|")[1]) - (api.t0 - 30)) < 0.01

    events = (await api.client.get("/thermal-events?cabinet_id=C-FIRE")).json()
    assert events["total"] == 1
    event = events["items"][0]
    assert event["status"] == "confirmed"
    assert event["alarm_no"] is not None
    assert [r["temperature_c"] for r in event["trigger_readings"]] == [71.0, 72.0, 73.0]

    # The linked alarm uses the existing escalation machinery.
    alarm = (await api.client.get(f"/alarms/{event['alarm_no']}")).json()
    assert alarm["alarm_no"] == event["alarm_no"]
    assert alarm["metadata"]["event_no"] == event["event_no"]
    assert alarm["ack_deadline_seconds"] == 10  # THERMAL_ACK_DEADLINE_SECONDS default
    assert alarm["primary_contact"]["name"] == "主值班员"
    assert alarm["backup_contact"]["channel"] == "sms"

    # Repeated ticks must not create a second event or alarm.
    assert _tick(api, api.t0 + 200) == []
    assert (await api.client.get("/thermal-events?cabinet_id=C-FIRE")).json()["total"] == 1
    # The thermal loop is paused, but the normal delivery worker still pages.
    assert await _wait_count(api, "primary", 1)
    await asyncio.sleep(0.3)
    assert len(api.fake.role_notifications("primary")) == 1
    alarms = (await api.client.get("/alarms")).json()
    thermal_alarms = [a for a in alarms["items"] if a["alarm_no"] == event["alarm_no"]]
    assert len(thermal_alarms) == 1

    # Event endpoint embeds the linked alarm snapshot.
    detail = (await api.client.get(f"/thermal-events/{event['event_no']}")).json()
    assert detail["alarm"] is not None
    assert detail["alarm"]["alarm_no"] == event["alarm_no"]


async def test_late_low_reading_interrupts_open_streak(thermal_api: ApiHarness):
    api = thermal_api
    for offset, temp in [(-30, 71.0), (-20, 72.0), (-10, 73.0)]:
        await _sample(api, "C-INT", offset=offset, temp=temp)
    assert (await api.client.get("/cabinets/C-INT")).json()["status"] == "pending_verdict"

    # An out-of-order cool reading at -15 breaks the streak while open.
    resp = await _sample(api, "C-INT", offset=-15, temp=60.0)
    assert resp.status_code == 202

    cabinet = (await api.client.get("/cabinets/C-INT")).json()
    assert cabinet["status"] == "monitoring"
    # The cool reading sits between the old hot readings and the later one: the
    # original streak is broken, only the last hot reading starts a fresh count.
    assert cabinet["current_hot_streak"] == 1
    assert cabinet["open_event"] is None

    events = (await api.client.get("/thermal-events?cabinet_id=C-INT")).json()
    assert events["total"] == 1
    assert events["items"][0]["status"] == "interrupted"
    assert events["items"][0]["alarm_no"] is None
    assert "低温" in events["items"][0]["interrupted_reason"]

    # After the window closes there is still no alarm, and the verdict stands.
    _tick(api, api.t0 + 200)
    assert api.fake.role_notifications("primary") == []
    again = (await api.client.get("/thermal-events?cabinet_id=C-INT")).json()
    assert again["items"][0]["status"] == "interrupted"


async def test_closed_verdict_cannot_be_rewritten_by_old_samples(thermal_api: ApiHarness):
    api = thermal_api
    for offset, temp in [(-30, 71.0), (-20, 72.0), (-10, 73.0)]:
        await _sample(api, "C-LOCK", offset=offset, temp=temp)
    _tick(api, api.t0 + 91)  # closes the window at t0+90
    confirmed = (await api.client.get("/thermal-events?cabinet_id=C-LOCK")).json()
    assert confirmed["items"][0]["status"] == "confirmed"
    event_no = confirmed["items"][0]["event_no"]

    # A cool reading for t0-25 only arrives at t0+100 (age 125s): intake refuses
    # it, so the stored (closed) verdict cannot be rewritten by an old sample.
    late = await _sample(
        api, "C-LOCK", offset=-25, temp=50.0, received_at=api.t0 + 100
    )
    assert late.status_code == 422
    assert (await api.client.get(f"/thermal-events/{event_no}")).json()["status"] == "confirmed"
    assert await _wait_count(api, "primary", 1)
    assert len(api.fake.role_notifications("primary")) == 1


async def test_gap_over_60_seconds_breaks_the_hot_streak(thermal_api: ApiHarness):
    api = thermal_api
    # Adjacent gaps: 30s (ok), then 70s (> 60s, streak breaks). All samples stay
    # inside the 120s intake window.
    for offset, temp in [(-100, 71.0), (-70, 72.0), (-1, 73.0)]:
        resp = await _sample(api, "C-GAP", offset=offset, temp=temp)
        assert resp.status_code == 202

    cabinet = (await api.client.get("/cabinets/C-GAP")).json()
    assert cabinet["status"] == "monitoring"
    assert cabinet["current_hot_streak"] == 1  # only the last reading counts

    events = (await api.client.get("/thermal-events?cabinet_id=C-GAP")).json()
    assert events["total"] == 0
    _tick(api, api.t0 + 200)
    assert api.fake.role_notifications("primary") == []


async def test_band_reading_65_to_70_breaks_streak_but_does_not_rearm(thermal_api: ApiHarness):
    api = thermal_api
    # First confirmed event.
    for offset, temp in [(-115, 71.0), (-105, 72.0), (-95, 73.0)]:
        await _sample(api, "C-ARM", offset=offset, temp=temp)
    _tick(api, api.t0 + 6)  # closes at t0+5
    assert (await api.client.get("/cabinets/C-ARM")).json()["status"] == "cooldown"

    # A reading inside [65, 70) breaks streaks but does not re-arm.
    await _sample(api, "C-ARM", offset=-20, temp=67.0)
    for offset, temp in [(-15, 80.0), (-10, 81.0), (-5, 82.0)]:
        await _sample(api, "C-ARM", offset=offset, temp=temp)
    _tick(api, api.t0 + 200)
    events = (await api.client.get("/thermal-events?cabinet_id=C-ARM&status=confirmed")).json()
    assert events["total"] == 1, "no new event while waiting for a sub-65C reading"

    # A genuinely cool reading re-arms, and the next triple fires.
    await _sample(api, "C-ARM", offset=-4, temp=64.0)
    for offset, temp in [(-3, 90.0), (-2, 91.0), (-1, 92.0)]:
        await _sample(api, "C-ARM", offset=offset, temp=temp)
    _tick(api, api.t0 + 300)
    events = (await api.client.get("/thermal-events?cabinet_id=C-ARM&status=confirmed")).json()
    assert events["total"] == 2
    alarm_nos = {e["alarm_no"] for e in events["items"]}
    assert len(alarm_nos) == 2


async def test_background_loop_closes_window_automatically(tmp_path, fake_gateway: FakeGateway):
    import httpx

    from app.main import create_app
    from tests.fake_gateway import build_fake_gateway_app

    settings = Settings(
        database_path=str(tmp_path / "alarms.db"),
        gateway_url="http://gateway.test",
        post_timeout_seconds=1.0,
        lookup_timeout_seconds=0.5,
        worker_interval_seconds=0.02,
        delivery_lease_seconds=2.0,
        max_backoff_seconds=0.05,
        reconcile_on_read=True,
        thermal_sample_window_seconds=1.0,
        thermal_tick_interval_seconds=0.05,
    )
    gateway_app = build_fake_gateway_app(fake_gateway)
    app = create_app(settings)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    app.state.gateway._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app), base_url=settings.gateway_url
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    )
    try:
        t0 = datetime.now(UTC).timestamp()
        for offset, temp in [(-0.6, 71.0), (-0.4, 72.0), (-0.2, 73.0)]:
            resp = await client.post(
                "/cabinets/C-AUTO/samples",
                json={"sampled_at": iso(t0 + offset), "temperature_c": temp},
            )
            assert resp.status_code == 202

        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            events = (await client.get("/thermal-events?cabinet_id=C-AUTO")).json()
            if events["items"] and events["items"][0]["status"] == "confirmed":
                break
            await asyncio.sleep(0.05)
        events = (await client.get("/thermal-events?cabinet_id=C-AUTO")).json()
        assert events["total"] == 1 and events["items"][0]["status"] == "confirmed"
        # Wait for the normal delivery worker to page the primary exactly once.
        deadline = asyncio.get_running_loop().time() + 8
        while (
            asyncio.get_running_loop().time() < deadline
            and not fake_gateway.role_notifications("primary")
        ):
            await asyncio.sleep(0.05)
        assert len(fake_gateway.role_notifications("primary")) == 1
        await asyncio.sleep(0.5)
        assert len(fake_gateway.role_notifications("primary")) == 1
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


async def test_out_of_order_arrival_still_produces_pending_verdict(thermal_api: ApiHarness):
    api = thermal_api
    # Arrive in reverse order: last reading first, first reading last.
    for offset, temp in [(-10, 78.0), (-20, 74.0), (-30, 71.0)]:
        resp = await _sample(api, "C-OOO", offset=offset, temp=temp)
        assert resp.status_code == 202
    cabinet = (await api.client.get("/cabinets/C-OOO")).json()
    assert cabinet["status"] == "pending_verdict"
    event = cabinet["open_event"]
    assert event is not None
    # Readings must be ordered by sampling time regardless of arrival order.
    assert [r["temperature_c"] for r in event["trigger_readings"]] == [71.0, 74.0, 78.0]


async def test_test_clock_header_is_rejected_when_disabled(tmp_path, fake_gateway: FakeGateway):
    import httpx

    from app.main import create_app

    settings = Settings(
        database_path=str(tmp_path / "noclock.db"),
        gateway_url="http://gateway.test",
        allow_test_clock=False,  # production default
    )
    app = create_app(settings)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    )
    try:
        t0 = 1_700_000_000.0
        resp = await client.post(
            "/cabinets/C-NC/samples",
            json={"sampled_at": iso(t0 - 5), "temperature_c": 71.0},
            headers={"X-Test-Now-Epoch": str(t0)},
        )
        assert resp.status_code == 400
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


async def test_cabinets_are_independent_and_unknown_cabinet_404(thermal_api: ApiHarness):
    api = thermal_api
    for offset, temp in [(-10, 71.0), (-8, 72.0), (-6, 73.0)]:
        await _sample(api, "C-A", offset=offset, temp=temp)
    await _sample(api, "C-B", offset=-6, temp=40.0)

    listing = (await api.client.get("/cabinets")).json()
    ids = {c["cabinet_id"] for c in listing["items"]}
    assert ids == {"C-A", "C-B"}
    by_id = {c["cabinet_id"]: c for c in listing["items"]}
    assert by_id["C-A"]["status"] == "pending_verdict"
    assert by_id["C-B"]["status"] == "monitoring"

    assert (await api.client.get("/cabinets/NOPE")).status_code == 404
    assert (await api.client.get("/thermal-events/TE-NOPE")).status_code == 404
