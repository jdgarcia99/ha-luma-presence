"""Tests for v0.6.0: Dream mode core."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
import random
import time
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.const import STATE_OFF, STATE_ON, SUN_EVENT_SUNRISE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    CONF_REPORT_NOTIFY,
    DOMAIN,
    DREAM_BEST,
    DREAM_LOW_SCORE,
    DREAM_NOT_ENOUGH_NIGHTS,
    EVENT_ACTION,
    EVENT_NIGHT_REPORT,
    REPLAY_NO_DATA,
)
from custom_components.luma_presence.dream import (
    Candidate,
    _weighted_ks,
    choose_candidate,
    generate_night,
    jaccard,
    make_candidates,
    room_slots,
    score_night,
)
from custom_components.luma_presence.model import (
    EVENING,
    WEEKDAY,
    WEEKEND,
    NightRooms,
    build_day_type,
    day_type,
    weighted_quantile,
)
from custom_components.luma_presence.monitor import ROLE_DREAM

from .test_v0_2_0 import OPTIONS
from .test_v0_4_0 import _learned

TODAY = date(2026, 10, 15)  # a Thursday
DAY = 86400
OFFSET = 6 * 3600  # axis minute 0 at 18:00 (sunset at 19:00)


def _night(
    night: date, rooms: dict[str, list[tuple[float, float]]], length: int = 720
) -> NightRooms:
    return NightRooms(
        night=night,
        offset=OFFSET,
        length=length,
        exposure=[(0.0, float(length))],
        intervals={room: [(a, b, True) for a, b in spans] for room, spans in rooms.items()},
        light_minutes={room: {f"light.{room}": sum(b - a for a, b in spans)} for room, spans in rooms.items()},
    )


def _nights(count: int, rooms: dict[str, list[tuple[float, float]]], first_age: int = 1) -> list[NightRooms]:
    return [_night(TODAY - timedelta(days=first_age + i), rooms) for i in range(count)]


# ----------------------------------------------------------------------
# Room model
# ----------------------------------------------------------------------
def test_day_types() -> None:
    """Friday and Saturday nights are weekend nights."""
    assert [day_type(date(2026, 10, d)) for d in range(11, 18)] == [
        WEEKDAY, WEEKDAY, WEEKDAY, WEEKDAY, WEEKDAY, WEEKEND, WEEKEND,
    ]  # Sunday 11 ... Saturday 17


def test_rates_durations_and_transitions() -> None:
    """Start rate per slot, durations by period, transitions and concurrency."""
    model = build_day_type(
        _nights(10, {"salon": [(65, 185)], "cocina": [(190, 220)]}), TODAY, 21
    )
    salon = model.rooms["salon"]
    # Starts at minute 65 (slot 4): 5 idle minutes plus the start minute.
    assert salon.start_rate[4] == pytest.approx(1 / 6)
    assert salon.start_rate[3] == 0
    assert salon.starts[4] == pytest.approx(1)
    assert salon.occupancy[5] == pytest.approx(1.0)
    assert [v for v, _ in salon.durations[EVENING]] == [120] * 10
    # The kitchen always starts within 10 minutes after the living room.
    assert salon.follow_prob == pytest.approx(1.0)
    assert salon.follow_to == {"cocina": 1.0}
    assert model.rooms["cocina"].follow_prob == 0.0
    assert model.concurrency_cap == 1
    assert len(model.nights) == 10
    assert model.atypical == []


def test_recency_decay() -> None:
    """Recent nights weigh more than old ones."""
    old = _nights(10, {"salon": [(60, 120)]}, first_age=60)
    recent = _nights(3, {"salon": [(60, 240)]}, first_age=1)
    model = build_day_type(old + recent, TODAY, 7)
    durations = model.rooms["salon"].durations[EVENING]
    assert weighted_quantile(durations, 0.5) == 180
    # With a very long half-life, the many old nights win.
    model = build_day_type(old + recent, TODAY, 10000)
    assert weighted_quantile(model.rooms["salon"].durations[EVENING], 0.5) == 60


def test_atypical_nights() -> None:
    """A night far from the others counts 30 %."""
    nights = [
        _night(TODAY - timedelta(days=1 + i), {"salon": [(60, 120 + 2 * i)]}) for i in range(10)
    ]
    party = _night(TODAY - timedelta(days=20), {"salon": [(0, 700)], "cocina": [(0, 700)]})
    model = build_day_type([*nights, party], TODAY, 10000)
    assert model.atypical == [party.night]
    weights = {v: w for v, w in model.rooms["salon"].durations[EVENING]}
    assert weights[700] == pytest.approx(0.3, abs=0.01)
    assert weights[60] == pytest.approx(1.0, abs=0.01)


# ----------------------------------------------------------------------
# Generator, score, novelty
# ----------------------------------------------------------------------
def _period(_minute: int) -> str:
    return EVENING


def test_generator_respects_cap_window_and_seed() -> None:
    """Never more rooms than the cap, only inside the window, reproducible."""
    busy = {f"r{i}": [(30 + 40 * j, 50 + 40 * j) for j in range(10)] for i in range(4)}
    model = build_day_type(_nights(10, {"a": [(0, 0.5)], **busy}), TODAY, 21)
    model.concurrency_cap = 2
    spans = generate_night(model, 60, 400, seed=5, period_at=_period)
    for minute in range(60, 400):
        assert sum(any(a <= minute < b for a, b in items) for items in spans.values()) <= 2
    assert all(60 <= a < b <= 400 for items in spans.values() for a, b in items)
    assert spans == generate_night(model, 60, 400, seed=5, period_at=_period)
    assert spans != generate_night(model, 60, 400, seed=6, period_at=_period)


def test_score_prefers_realistic_nights() -> None:
    """A night generated from the model scores above a random one."""
    model = build_day_type(
        _nights(14, {"salon": [(120, 300)], "cocina": [(150, 170)], "bano": [(320, 330)]}),
        TODAY,
        21,
    )
    realistic = generate_night(model, 60, 480, seed=1, period_at=_period)
    rng = random.Random(3)
    noise: dict[str, list[tuple[int, int]]] = {}
    for room in ("salon", "cocina", "bano", "garaje", "terraza"):
        start = rng.randint(60, 400)
        noise[room] = [(start, start + rng.randint(5, 80))]
    good, parts = score_night(model, realistic, 60, 480)
    bad, _ = score_night(model, noise, 60, 480)
    assert good > bad
    assert set(parts) == {"occupancy", "durations", "concurrency", "visits"}
    assert 0 <= bad <= good <= 100


def test_novelty_and_choice() -> None:
    """Too similar candidates are rejected; otherwise the best one runs as low_score."""
    a = {"salon": [(0, 60)]}
    assert room_slots(a) == {("salon", s) for s in range(4)}
    assert jaccard(room_slots(a), room_slots({"salon": [(0, 30)]})) == 0.5

    copy = Candidate(1, a, 90, similarity=0.95)
    fresh = Candidate(2, {"salon": [(100, 160)]}, 75, similarity=0.1)
    chosen, reason = choose_candidate([copy, fresh], min_score=60, max_similarity=0.7)
    assert (chosen, reason) == (fresh, DREAM_BEST)

    chosen, reason = choose_candidate([copy, fresh], min_score=80, max_similarity=0.7)
    assert (chosen, reason) == (copy, DREAM_LOW_SCORE)


def test_weighted_ks_matches_its_definition() -> None:
    """The linear sweep gives the same statistic as the brute-force definition."""

    def brute(a, b):
        if not a or not b:
            return 1.0 if (a or b) else 0.0
        ta, tb = sum(w for _, w in a), sum(w for _, w in b)
        return max(
            abs(sum(w for v, w in a if v <= p) / ta - sum(w for v, w in b if v <= p) / tb)
            for p in {v for v, _ in a} | {v for v, _ in b}
        )

    rng = random.Random(1)
    for _ in range(200):
        a = [(rng.randint(1, 60), rng.random()) for _ in range(rng.randint(0, 30))]
        b = [(rng.randint(1, 60), rng.random()) for _ in range(rng.randint(0, 30))]
        assert _weighted_ks(a, b) == pytest.approx(brute(a, b), abs=1e-12)


def test_planning_is_fast() -> None:
    """120 nights, 15 rooms, 20 candidates: well under 2 seconds."""
    rng = random.Random(7)
    nights = []
    for age in range(1, 121):
        rooms = {}
        for k in range(15):
            spans, minute = [], rng.randint(0, 120)
            while minute < 700:
                length = rng.randint(5, 90)
                spans.append((minute, min(minute + length, 720)))
                minute += length + rng.randint(10, 200)
            rooms[f"room{k}"] = spans
        nights.append(_night(TODAY - timedelta(days=age), rooms))

    started = time.perf_counter()
    model = build_day_type(nights, TODAY, 21)
    candidates = make_candidates(model, 85, 390, OFFSET, 123, 20, [])
    elapsed = time.perf_counter() - started
    assert len(model.nights) == 120 and len(model.rooms) == 15
    assert len(candidates) == 20
    assert elapsed < 2, f"model and 20 candidates took {elapsed:.2f} s"


# ----------------------------------------------------------------------
# Integration
# ----------------------------------------------------------------------
def clock(hour: int, minute: int = 0) -> int:
    return hour * 3600 + minute * 60


def _dream_days() -> dict[str, dict[str, list[Any]]]:
    """Five weeks of nights: living room 20:00-23:00, hall 21:00-21:30."""
    days = {}
    day = date(2026, 9, 10)
    while day <= TODAY:
        days[day.isoformat()] = {
            "exposure": [[0, DAY]],
            "events": [
                ["light.salon", STATE_ON, clock(20), None],
                ["light.hall", STATE_ON, clock(21), None],
                ["light.hall", STATE_OFF, clock(21, 30), None],
                ["light.salon", STATE_OFF, clock(23), None],
            ],
        }
        day += timedelta(days=1)
    return days


def _local(day: date, hour: int, minute: int = 0) -> datetime:
    return dt_util.as_utc(
        datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt_util.get_default_time_zone())
    )


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


async def _move(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()
    for _ in range(20):
        await asyncio.sleep(0)


async def _setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    learned: dict | None,
    *,
    mode: str = "dream",
    options: dict[str, Any] | None = None,
    hour: int = 12,
    master: bool = True,
) -> MockConfigEntry:
    freezer.move_to(_local(TODAY, hour))
    hass.states.async_set("person.mario", "not_home")
    hass.states.async_set("alarm_control_panel.home_alarm", "armed_away")
    entry = MockConfigEntry(
        domain=DOMAIN, title="LumaPresence", options={**OPTIONS, **(options or {})}, version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    if learned is not None:
        runtime.learning._days = learned
        await runtime.dream.async_rebuild()
    settings = runtime.settings
    settings.delayed_start = settings.delayed_end = False
    settings.dream_min_days = 7
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.luma_presence_mode", "option": mode}, blocking=True,
    )
    if master:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
        )
    await hass.async_block_till_done()
    return entry


def _is_on(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id).state == STATE_ON


async def test_dream_plays_a_night(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """With enough nights, Dream plans and plays tonight."""
    entry = await _setup(hass, freezer, _dream_days())
    runtime = entry.runtime_data
    assert runtime.dream.model.valid_nights(WEEKDAY) >= 7
    model_sensor = hass.states.get("sensor.luma_presence_model")
    assert int(model_sensor.state) >= 14
    assert model_sensor.attributes["weekday"]["nights"] >= 7

    await _move(hass, freezer, runtime.engine.next_event + timedelta(seconds=1))
    await _until(lambda: runtime.player.plan is not None)
    plan = runtime.player.plan
    assert plan.source == "dream"
    assert plan.transitions
    realism = hass.states.get("sensor.luma_presence_realism")
    assert float(realism.state) == plan.score
    assert realism.attributes["source"] == "dream"
    assert realism.attributes["reason"] in (DREAM_BEST, DREAM_LOW_SCORE)
    assert realism.attributes["candidates"] == 20
    assert realism.attributes["day_type"] == WEEKDAY
    tonight = hass.states.get("sensor.luma_presence_tonight_plan")
    assert tonight.attributes["preview"] is False
    assert tonight.attributes["summary"]
    assert int(tonight.state) == len(plan.in_window())
    # Rooms of their own (no areas) map to the same lights.
    assert set(plan.timeline.lights) <= {"light.salon", "light.hall"}
    assert any((ROLE_DREAM, None) in roles for roles in runtime.monitor._roles.values())


async def test_plan_survives_a_restart(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """After a restart the same plan continues."""
    entry = await _setup(hass, freezer, _dream_days())
    runtime = entry.runtime_data
    await _move(hass, freezer, runtime.engine.next_event + timedelta(seconds=1))
    await _until(lambda: runtime.player.plan is not None)
    before = runtime.player.plan

    await _move(hass, freezer, _local(TODAY, 21, 15))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    await _until(lambda: runtime.player.plan is not None)
    after = runtime.player.plan
    assert after.transitions == before.transitions
    assert after.seed == before.seed
    assert after.score == before.score


async def test_fallback_chain(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Without data Dream falls back to Replay, then to Sun mode, with reasons."""
    entry = await _setup(hass, freezer, None)
    runtime = entry.runtime_data
    await _move(hass, freezer, runtime.engine.next_event + timedelta(seconds=1))
    await _until(lambda: _is_on(hass, "light.bedroom"))  # Sun mode's first step
    realism = hass.states.get("sensor.luma_presence_realism")
    assert realism.state == "unknown"
    assert realism.attributes["source"] == "fallback"
    assert realism.attributes["reason"] == DREAM_NOT_ENOUGH_NIGHTS
    assert realism.attributes["replay_reason"] == REPLAY_NO_DATA


async def test_fallback_to_replay(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """With a usable source night but too few nights for Dream, Replay runs."""
    entry = await _setup(hass, freezer, _learned())
    runtime = entry.runtime_data
    await _move(hass, freezer, runtime.engine.next_event + timedelta(seconds=1))
    await _until(lambda: runtime.player.plan is not None)
    assert runtime.player.plan.source == "replay"
    realism = hass.states.get("sensor.luma_presence_realism")
    assert realism.attributes["source"] == "replay"
    assert realism.attributes["reason"] == DREAM_NOT_ENOUGH_NIGHTS


async def test_preview_switches_nothing(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """The button and the service generate a plan without playing it."""
    entry = await _setup(hass, freezer, _dream_days(), master=False)
    events = async_capture_events(hass, EVENT_ACTION)

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.luma_presence_dream_night"}, blocking=True
    )
    await hass.async_block_till_done()
    tonight = hass.states.get("sensor.luma_presence_tonight_plan")
    assert tonight.attributes["preview"] is True
    assert int(tonight.state) > 0
    response = await hass.services.async_call(
        DOMAIN, "dream_preview", {}, blocking=True, return_response=True
    )
    assert response["timeline"] and response["summary"]
    assert response["candidates"] == 20
    assert not events
    assert entry.runtime_data.player.plan is None  # nothing planned for real
    assert entry.runtime_data.dream.status == {}


async def test_heatmap_shape(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Two room x hour matrices for dashboards."""
    await _setup(hass, freezer, _dream_days(), master=False)
    heatmap = await hass.services.async_call(
        DOMAIN, "get_heatmap", {"day_type": "weekday"}, blocking=True, return_response=True
    )
    assert heatmap["day_type"] == "weekday"
    assert len(heatmap["hours"]) == 24 and heatmap["hours"][0] == 12
    rooms = len(heatmap["rooms"])
    assert rooms == 2
    for matrix in ("real", "dreamed"):
        assert len(heatmap[matrix]) == rooms
        assert all(len(row) == 24 for row in heatmap[matrix])
    # The living room is busy at 21:00 (index 9 from 12:00) on real nights.
    salon = heatmap["rooms"].index(hass.states.get("light.salon").name)
    assert heatmap["real"][salon][9] == 1.0


async def test_morning_report(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """At sunrise after a simulated night: an event and a notification."""
    sent: list[ServiceCall] = []

    async def _notify(call: ServiceCall) -> None:
        sent.append(call)

    hass.services.async_register("notify", "test_phone", _notify)
    entry = await _setup(
        hass, freezer, None, mode="sun", hour=21,
        options={CONF_REPORT_NOTIFY: "notify.test_phone"},
    )
    reports = async_capture_events(hass, EVENT_NIGHT_REPORT)
    controller = entry.runtime_data.controller
    # Wait until the switch is recorded, not only until the light is on.
    await _until(
        lambda: (controller.last_action or {}).get("entity_id") == "light.bedroom"
    )

    sunrise = get_astral_event_next(hass, SUN_EVENT_SUNRISE, dt_util.utcnow())
    await _move(hass, freezer, _local(TODAY + timedelta(days=1), 1))  # window ended
    assert not reports
    await _move(hass, freezer, sunrise + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert len(reports) == 1
    report = reports[0].data
    assert report["entry_id"] == entry.entry_id
    assert report["mode"] == "sun"
    assert report["night"] == TODAY.isoformat()
    assert report["lights_switched"] >= 1
    assert "light.bedroom" in report["lights"]
    assert report["switches"] >= 2  # on, then off at the window end
    assert report["failures"] == 0
    assert len(sent) == 1
    assert "sun" in sent[0].data["message"]
