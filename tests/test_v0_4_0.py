"""Tests for v0.4.0: Replay mode."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    DOMAIN,
    EVENT_ACTION,
    REASON_ALARM,
    REASON_AWAY,
    REASON_SIMULATION_TEST,
    REPLAY_MOST_RECENT,
    REPLAY_NO_DATA,
    REPLAY_NOT_ENOUGH_EVENTS,
    REPLAY_NOT_ENOUGH_EXPOSURE,
    REPLAY_SAME_DAY_TYPE,
    REPLAY_SAME_WEEKDAY,
)
from custom_components.luma_presence.monitor import ROLE_REPLAY
from custom_components.luma_presence.replay import (
    NightData,
    SourceEvent,
    area_targets,
    build_timeline,
    choose_source,
    night_data,
)

from .conftest import until
from .test_v0_2_0 import OPTIONS

DAY = 86400
TONIGHT = date(2026, 10, 15)  # a Thursday
WINDOW = (7.5 * 3600, 12.5 * 3600)  # 19:30 to 00:30 on the night clock


def clock(hour: int, minute: int = 0) -> int:
    return hour * 3600 + minute * 60


def _days(first: date, last: date) -> dict[str, dict[str, list[Any]]]:
    """Fully observed days without events."""
    days = {}
    day = first
    while day <= last:
        days[day.isoformat()] = {"exposure": [[0, DAY]], "events": []}
        day += timedelta(days=1)
    return days


def _add_night(days: dict, night: date, light: str = "light.a") -> None:
    """Two events inside the window of a night."""
    days[night.isoformat()]["events"] += [
        [light, STATE_ON, clock(21), None],
        [light, STATE_OFF, clock(22), None],
    ]


def _choose(days: dict, previous: date | None = None, min_events: int = 2):
    return choose_source(days, TONIGHT, WINDOW, min_events, previous, lambda _e: None)


# ----------------------------------------------------------------------
# Source selection
# ----------------------------------------------------------------------
def test_source_preference() -> None:
    """Same weekday, then same day type, then the most recent night."""
    days = _days(date(2026, 9, 20), TONIGHT)
    for night in (date(2026, 10, 1), date(2026, 10, 8), date(2026, 10, 10),
                  date(2026, 10, 13), date(2026, 10, 14)):
        _add_night(days, night)

    choice = _choose(days)
    assert (choice.source, choice.reason) == (date(2026, 10, 8), REPLAY_SAME_WEEKDAY)
    assert choice.events == 2
    assert choice.eligible == 5

    # Same weekday 14 days ago still counts.
    days["2026-10-08"]["events"] = []
    choice = _choose(days)
    assert (choice.source, choice.reason) == (date(2026, 10, 1), REPLAY_SAME_WEEKDAY)

    # Then the most recent night of the same type (weekday: Sunday-Thursday).
    days["2026-10-01"]["events"] = []
    choice = _choose(days)
    assert (choice.source, choice.reason) == (date(2026, 10, 14), REPLAY_SAME_DAY_TYPE)

    # Only a weekend night (Saturday) left: the most recent eligible night.
    days["2026-10-13"]["events"] = days["2026-10-14"]["events"] = []
    choice = _choose(days)
    assert (choice.source, choice.reason) == (date(2026, 10, 10), REPLAY_MOST_RECENT)


def test_source_not_twice_in_a_row() -> None:
    """Last night's source is skipped unless it is the only eligible one."""
    days = _days(date(2026, 9, 20), TONIGHT)
    _add_night(days, date(2026, 10, 8))
    _add_night(days, date(2026, 10, 1))
    assert _choose(days, previous=date(2026, 10, 8)).source == date(2026, 10, 1)

    days["2026-10-01"]["events"] = []
    assert _choose(days, previous=date(2026, 10, 8)).source == date(2026, 10, 8)


def test_exposure_and_event_rules() -> None:
    """80 % of the window must be observed, with enough events; fallback reasons."""
    days = _days(date(2026, 10, 14), TONIGHT)
    _add_night(days, date(2026, 10, 14))
    # Observed until 20:00, and the next day from midnight: 20 % of the window.
    days["2026-10-14"]["exposure"] = [[0, clock(20)]]
    choice = _choose(days)
    assert (choice.source, choice.reason) == (None, REPLAY_NOT_ENOUGH_EXPOSURE)

    # Observed from 20:30: exactly 80 % of the window.
    days["2026-10-14"]["exposure"] = [[0, clock(19)], [clock(20, 30), DAY]]
    assert _choose(days).source == date(2026, 10, 14)

    assert _choose(days, min_events=3).reason == REPLAY_NOT_ENOUGH_EVENTS
    assert _choose({}).reason == REPLAY_NO_DATA
    # A night needs both of its day records.
    assert _choose({"2026-10-14": days["2026-10-14"]}).reason == REPLAY_NO_DATA


def test_night_spans_midnight() -> None:
    """A night collects the evening of one day and the morning of the next."""
    days = _days(date(2026, 10, 7), date(2026, 10, 9))
    days["2026-10-07"]["events"] = [["light.p", STATE_ON, clock(22), None]]
    days["2026-10-08"]["events"] = [
        ["light.m", STATE_ON, clock(9), "kitchen"],
        ["light.a", STATE_ON, clock(23), "salon"],
    ]
    days["2026-10-09"]["events"] = [
        ["light.a", STATE_OFF, clock(1)],  # stored before v0.2.0: no area
        ["light.b", STATE_ON, clock(13), None],  # next night
    ]
    data = night_data(days, date(2026, 10, 8), lambda _e: "resolved")
    assert [(e.seconds, e.entity_id, e.area_id) for e in data.events] == [
        (clock(11), "light.a", "salon"),
        (clock(13), "light.a", "resolved"),
    ]
    assert data.events_between(*WINDOW) == 1
    assert data.coverage(*WINDOW) == 1.0
    # State at 12:00: morning of the night's first day, then the day before.
    assert data.base == {"light.p": (STATE_ON, "resolved"), "light.m": (STATE_ON, "kitchen")}


# ----------------------------------------------------------------------
# Mapping and timeline
# ----------------------------------------------------------------------
def _night(*events: tuple[float, str, str, str | None], base=None) -> NightData:
    return NightData(
        date(2026, 10, 8),
        [SourceEvent(*event) for event in events],
        [(0, DAY)],
        base or {},
    )


def _build(data: NightData, *, by_room: bool = True, targets=None, jitter: float = 0, seed: int = 1):
    return build_timeline(
        data,
        by_room=by_room,
        targets=targets or {},
        allowed=lambda entity_id: entity_id != "light.fridge",
        seed=seed,
        jitter_minutes=jitter,
    )


def _changes(timeline) -> list[tuple[float, str, str, str]]:
    return [(t.seconds, t.target, t.state, t.original) for t in timeline.transitions]


def test_area_targets_prefer_facades() -> None:
    """Front first, then rear, then interior; ties by configuration order."""
    areas = {"light.int1": "salon", "light.int2": "salon", "light.rear": "salon",
             "light.front": "salon", "light.k1": "kitchen", "light.k2": "kitchen"}
    lights = ["light.int1", "light.int2", "light.rear", "light.front", "light.k1", "light.k2"]
    targets = area_targets(lights, ["light.front"], ["light.rear"], areas.get)
    assert targets == {"salon": "light.front", "kitchen": "light.k1"}
    targets = area_targets(lights, [], ["light.rear"], areas.get)
    assert targets["salon"] == "light.rear"


def test_mapping_by_room() -> None:
    """Rooms with simulation lights use them; others keep the original light."""
    data = _night(
        (clock(8), "light.lamp", STATE_ON, "salon"),
        (clock(9), "light.bath", STATE_ON, "bathroom"),
        (clock(10), "light.porch", STATE_ON, None),
        (clock(11), "light.fridge", STATE_ON, "kitchen"),
    )
    timeline = _build(data, targets={"salon": "light.front"})
    assert _changes(timeline) == [
        (clock(8), "light.front", STATE_ON, "light.lamp"),
        (clock(9), "light.bath", STATE_ON, "light.bath"),
        (clock(10), "light.porch", STATE_ON, "light.porch"),
    ]

    # By room off: exactly the same light.
    timeline = _build(data, by_room=False, targets={"salon": "light.front"})
    assert [c[1] for c in _changes(timeline)] == ["light.lamp", "light.bath", "light.porch"]


def test_aggregation_follows_room_occupancy() -> None:
    """On with the first source light of the room, off with the last."""
    data = _night(
        (clock(8), "light.a", STATE_ON, "salon"),
        (clock(9), "light.b", STATE_ON, "salon"),
        (clock(10), "light.a", STATE_OFF, "salon"),
        (clock(11), "light.b", STATE_OFF, "salon"),
        base={"light.c": (STATE_ON, "kitchen")},
    )
    timeline = _build(data, targets={"salon": "light.front", "kitchen": "light.k"})
    assert _changes(timeline) == [
        (clock(8), "light.front", STATE_ON, "light.a"),
        (clock(11), "light.front", STATE_OFF, "light.b"),
    ]
    assert timeline.initial == {"light.k": STATE_ON, "light.front": STATE_OFF}
    assert timeline.state_at(clock(9))["light.front"] == STATE_ON
    assert timeline.state_at(clock(12))["light.front"] == STATE_OFF


def test_jitter_keeps_order() -> None:
    """Jitter is reproducible and keeps each light's order, a minute apart."""
    events = [
        (clock(8) + i * 120, "light.a", STATE_ON if i % 2 == 0 else STATE_OFF, None)
        for i in range(10)
    ]
    data = _night(*events)
    first = _build(data, by_room=False, jitter=10, seed=42)
    assert _changes(first) == _changes(_build(data, by_room=False, jitter=10, seed=42))
    assert _changes(first) != _changes(_build(data, by_room=False, jitter=10, seed=7))

    times = [t.seconds for t in first.transitions]
    states = [t.state for t in first.transitions]
    assert all(b - a >= 60 for a, b in pairwise(times))
    assert states == [STATE_ON, STATE_OFF] * 5
    assert all(abs(t - e[0]) <= 600 + 60 * 10 for t, e in zip(times, events, strict=True))


# ----------------------------------------------------------------------
# Playback through the engine
# ----------------------------------------------------------------------
SOURCE = date(2026, 10, 8)


def _learned() -> dict[str, dict[str, list[Any]]]:
    days = _days(date(2026, 10, 7), TONIGHT)
    days["2026-10-08"]["events"] = [
        ["light.bedroom", STATE_ON, clock(18), None],  # before the window
        ["light.salon", STATE_ON, clock(21), None],
        ["light.salon", STATE_OFF, clock(22), None],
        ["light.hall", STATE_ON, clock(23, 30), None],
    ]
    days["2026-10-09"]["events"] = [
        ["light.hall", STATE_OFF, clock(0, 10), None],
        ["switch.oficina", STATE_ON, clock(0, 40), None],  # after the window end
    ]
    return days


def _local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return dt_util.as_utc(
        datetime(day.year, day.month, day.day, hour, minute, second,
                 tzinfo=dt_util.get_default_time_zone())
    )


async def _move(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()
    for _ in range(20):
        await asyncio.sleep(0)


async def _setup_replay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    learned: dict | None,
    *,
    away: bool = True,
) -> MockConfigEntry:
    freezer.move_to(_local(TONIGHT, 12))
    hass.states.async_set("person.mario", "not_home" if away else "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "armed_away" if away else "disarmed")
    entry = MockConfigEntry(domain=DOMAIN, title="LumaPresence", options=OPTIONS, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    if learned is not None:
        runtime.learning._days = learned
    settings = runtime.settings
    settings.delayed_start = settings.delayed_end = False
    settings.replay_jitter = 0
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.luma_presence_mode", "option": "replay"}, blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
    )
    await hass.async_block_till_done()
    return entry


def _is_on(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id).state == STATE_ON


async def test_replay_plays_the_source_night(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Initial state at window start, transitions on time, nothing after the end."""
    entry = await _setup_replay(hass, freezer, _learned())
    runtime = entry.runtime_data
    events = async_capture_events(hass, EVENT_ACTION)
    start = runtime.engine.next_event

    await _move(hass, freezer, start + timedelta(seconds=1))
    await until(hass, lambda: _is_on(hass, "light.bedroom"))
    assert not _is_on(hass, "light.salon")
    sensor = hass.states.get("sensor.luma_presence_replay_source")
    assert sensor.state == SOURCE.isoformat()
    assert sensor.attributes["reason"] == REPLAY_SAME_WEEKDAY
    assert sensor.attributes["events"] == 4
    assert sensor.attributes["by_room"] is True
    # The monitor watches tonight's Replay lights.
    assert (ROLE_REPLAY, None) in runtime.monitor._roles["light.salon"]

    await _move(hass, freezer, _local(TONIGHT, 21, 0, 30))
    await until(hass, lambda: _is_on(hass, "light.salon"))
    last = hass.states.get("sensor.luma_presence_last_action").attributes
    assert last["mode"] == "replay"
    assert last["entity_id"] == "light.salon"

    await _move(hass, freezer, _local(TONIGHT, 22, 0, 30))
    await until(hass, lambda: not _is_on(hass, "light.salon"))

    next_day = TONIGHT + timedelta(days=1)
    for when in (_local(TONIGHT, 23, 30, 30), _local(next_day, 0, 10, 30),
                 _local(next_day, 0, 31), _local(next_day, 0, 45)):
        await _move(hass, freezer, when)
    played = [(e.data["action"], e.data["entity_id"]) for e in events]
    assert ("on", "light.hall") in played
    assert ("off", "light.hall") in played
    assert ("on", "switch.oficina") not in played
    assert runtime.engine.status != "simulating"


async def test_real_absence_switches_off_lights_left_on(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """While the house is empty, a light left on follows the source night."""
    entry = await _setup_replay(hass, freezer, _learned())
    learning = entry.runtime_data.learning
    assert {REASON_ALARM, REASON_AWAY} <= learning.reasons
    # Someone left it on when leaving: LumaPresence does not own it.
    hass.states.async_set("light.salon", STATE_ON)
    assert entry.runtime_data.controller.owner_of("light.salon") is None

    # Off at the window start, as on the source night until 21:00...
    await _move(hass, freezer, entry.runtime_data.engine.next_event + timedelta(seconds=1))
    await until(hass, lambda: _is_on(hass, "light.bedroom"))
    await until(hass, lambda: not _is_on(hass, "light.salon"))
    # ...then on at 21:00 and off at 22:00.
    await _move(hass, freezer, _local(TONIGHT, 21, 0, 30))
    await until(hass, lambda: _is_on(hass, "light.salon"))
    await _move(hass, freezer, _local(TONIGHT, 22, 0, 30))
    await until(hass, lambda: not _is_on(hass, "light.salon"))


async def test_test_with_people_home_keeps_ownership_rule(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """With people at home, Replay only switches off what the simulation switched on."""
    entry = await _setup_replay(hass, freezer, _learned(), away=False)
    learning = entry.runtime_data.learning
    assert learning.reasons == {REASON_SIMULATION_TEST}
    hass.states.async_set("light.salon", STATE_ON)

    await _move(hass, freezer, entry.runtime_data.engine.next_event + timedelta(seconds=1))
    await until(hass, lambda: _is_on(hass, "light.bedroom"))
    for when in (_local(TONIGHT, 21, 0, 30), _local(TONIGHT, 22, 0, 30)):
        await _move(hass, freezer, when)
        assert _is_on(hass, "light.salon")
    assert entry.runtime_data.controller.owner_of("light.salon") is None


async def test_locked_light_gets_its_state_after_unlock(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A light used by a route keeps its Replay state until the route releases it."""
    entry = await _setup_replay(hass, freezer, _learned())
    controller = entry.runtime_data.controller
    await _move(hass, freezer, entry.runtime_data.engine.next_event + timedelta(seconds=1))
    await until(hass, lambda: _is_on(hass, "light.bedroom"))

    controller.lock(["light.salon"])
    await _move(hass, freezer, _local(TONIGHT, 21, 0, 30))
    assert not _is_on(hass, "light.salon")

    controller.unlock(["light.salon"])
    await hass.async_block_till_done()
    await until(hass, lambda: _is_on(hass, "light.salon"))


async def test_restart_rebuilds_the_same_night(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """After a restart inside the window, the same source and timeline continue."""
    entry = await _setup_replay(hass, freezer, _learned())
    runtime = entry.runtime_data
    runtime.settings.replay_jitter = 10
    await _move(hass, freezer, runtime.engine.next_event + timedelta(seconds=1))
    await until(hass, lambda: runtime.replay.status is not None)
    saved = dict(runtime.replay._saved)
    plan = await runtime.replay.async_plan(runtime.engine._current_window(dt_util.utcnow()))

    await _move(hass, freezer, _local(TONIGHT, 21, 30))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    runtime.settings.replay_jitter = 10
    window = runtime.engine._current_window(dt_util.utcnow())
    assert window is not None
    assert runtime.replay._saved == saved
    replanned = await runtime.replay.async_plan(window)
    assert replanned.source == SOURCE
    assert replanned.timeline.transitions == plan.timeline.transitions


async def test_fallback_runs_sun_mode(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Without learned nights, Replay behaves like Sun mode and says why."""
    entry = await _setup_replay(hass, freezer, None)
    runtime = entry.runtime_data
    sun_start = runtime.engine.next_event
    await _move(hass, freezer, sun_start + timedelta(seconds=1))
    # Sun mode's first step keeps the front lights on.
    await until(hass, lambda: _is_on(hass, "light.bedroom"))
    assert runtime.engine.status == "simulating"
    sensor = hass.states.get("sensor.luma_presence_replay_source")
    assert sensor.state == "unknown"
    assert sensor.attributes["reason"] == REPLAY_NO_DATA
