"""Tests for v0.7.0: context (darkness, morning, holidays, weather) and verification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    CONF_BAD_WEATHER_BOOST,
    CONF_DARKNESS_SENSOR,
    CONF_HOLIDAY_CALENDAR,
    CONF_MORNING_ENABLED,
    CONF_MORNING_END,
    CONF_MORNING_START,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    DREAM_BEST,
    DREAM_LOW_SCORE,
    REPLAY_SAME_DAY_TYPE,
    START_CLAMPED_EARLIEST,
    START_CLAMPED_LATEST,
    START_DARKNESS,
    START_SENSOR_UNAVAILABLE,
)
from custom_components.luma_presence.context import (
    DarknessTracker,
    holidays_from_events,
)
from custom_components.luma_presence.dream import (
    Candidate,
    choose_candidate,
    make_candidates,
    score_night,
)
from custom_components.luma_presence.model import (
    WEEKDAY,
    WEEKEND,
    NightRooms,
    boost_model,
    build_day_type,
)
from custom_components.luma_presence.plan import (
    NightPlan,
    Timeline,
    Transition,
    day_type,
    is_workday,
    to_night_seconds,
)
from custom_components.luma_presence.replay import choose_source

from .test_v0_2_0 import OPTIONS

TODAY = date(2026, 10, 15)  # Thursday
HOLIDAY = date(2026, 10, 21)  # a Wednesday
CALENDAR = "calendar.festivos"
LUX = "sensor.lux"
WEATHER = "weather.casa"


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


def _calendar(hass: HomeAssistant, events: list[dict[str, str]]) -> list[ServiceCall]:
    """A calendar whose get_events returns `events`."""
    calls: list[ServiceCall] = []

    async def _get_events(call: ServiceCall) -> dict[str, Any]:
        calls.append(call)
        return {CALENDAR: {"events": events}}

    hass.states.async_set(CALENDAR, STATE_OFF)
    hass.services.async_register(
        "calendar", "get_events", _get_events, supports_response=SupportsResponse.ONLY
    )
    return calls


async def _setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any] | None = None,
    hour: int = 12,
    day: date = TODAY,
) -> MockConfigEntry:
    freezer.move_to(_local(day, hour))
    hass.states.async_set("person.mario", "not_home")
    hass.states.async_set("alarm_control_panel.home_alarm", "armed_away")
    entry = MockConfigEntry(
        domain=DOMAIN, title="LumaPresence", options={**OPTIONS, **(options or {})}, version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    settings = entry.runtime_data.settings
    settings.delayed_start = settings.delayed_end = False
    return entry


# ----------------------------------------------------------------------
# Night type and holidays
# ----------------------------------------------------------------------
def test_night_type_with_holidays() -> None:
    """The eve of a holiday is a weekend night; the holiday's night is not."""
    holidays = {HOLIDAY}
    assert day_type(HOLIDAY - timedelta(days=1)) == WEEKDAY
    assert day_type(HOLIDAY - timedelta(days=1), holidays) == WEEKEND
    assert day_type(HOLIDAY, holidays) == WEEKDAY  # Thursday is a workday
    assert day_type(date(2026, 10, 16), holidays) == WEEKEND  # Friday, as before
    assert not is_workday(HOLIDAY, holidays)
    assert is_workday(HOLIDAY + timedelta(days=1), holidays)
    assert not is_workday(date(2026, 10, 17))  # Saturday


def test_holidays_from_events() -> None:
    """All-day events and events covering midday are holidays."""
    holidays = holidays_from_events(
        [
            {"start": "2026-10-12", "end": "2026-10-13"},
            {"start": "2026-12-24T10:00:00+01:00", "end": "2026-12-24T14:00:00+01:00"},
            {"start": "2026-12-25T18:00:00+01:00", "end": "2026-12-25T20:00:00+01:00"},
            {"start": "2026-12-31", "end": "2027-01-02"},
        ]
    )
    assert holidays == {
        date(2026, 10, 12),
        date(2026, 12, 24),
        date(2026, 12, 31),
        date(2027, 1, 1),
    }


def test_replay_source_uses_night_types() -> None:
    """A holiday eve counts as a weekend night when choosing a Replay source."""
    days = {}
    day = date(2026, 9, 20)
    while day <= TODAY:
        days[day.isoformat()] = {"exposure": [[0, 86400]], "events": []}
        day += timedelta(days=1)
    eve = date(2026, 10, 6)  # Tuesday before a holiday
    days[eve.isoformat()]["events"] = [
        ["light.a", STATE_ON, 21 * 3600, None],
        ["light.a", STATE_OFF, 22 * 3600, None],
    ]
    tonight = date(2026, 10, 16)  # Friday: weekend night
    holidays = {eve + timedelta(days=1)}
    choice = choose_source(
        days, tonight, (27000, 45000), 2, None, lambda _e: None,
        lambda night: day_type(night, holidays),
    )
    assert (choice.source, choice.reason) == (eve, REPLAY_SAME_DAY_TYPE)


async def test_learned_nights_follow_the_calendar(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Dream classifies learned nights with the holidays, in one calendar call."""
    calls = _calendar(hass, [{"start": "2026-10-07", "end": "2026-10-08"}])
    entry = await _setup(hass, freezer, {CONF_HOLIDAY_CALENDAR: CALENDAR})
    await hass.async_block_till_done(wait_background_tasks=True)
    runtime = entry.runtime_data
    days = {}
    day = date(2026, 9, 25)
    while day <= TODAY:
        days[day.isoformat()] = {"exposure": [[0, 86400]], "events": [
            ["light.salon", STATE_ON, 20 * 3600, None], ["light.salon", STATE_OFF, 22 * 3600, None],
        ]}
        day += timedelta(days=1)
    runtime.learning._days = days
    calls.clear()
    model = await runtime.dream.async_rebuild()
    assert date(2026, 10, 6) in model.day_types[WEEKEND].nights  # eve of the holiday
    assert date(2026, 10, 7) in model.day_types[WEEKDAY].nights  # the holiday itself
    assert model.night_type_source == "calendar"
    assert len(calls) <= 1  # one range, cached for the day
    await runtime.dream.async_rebuild()
    assert len(calls) <= 1
    attributes = hass.states.get("sensor.luma_presence_model").attributes
    assert attributes["night_type_source"] == "calendar"


async def test_calendar_unavailable_falls_back(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Without a readable calendar the plain rule is used, and it is shown."""
    _calendar(hass, [{"start": "2026-10-07", "end": "2026-10-08"}])
    hass.states.async_set(CALENDAR, STATE_UNAVAILABLE)
    entry = await _setup(hass, freezer, {CONF_HOLIDAY_CALENDAR: CALENDAR})
    await hass.async_block_till_done(wait_background_tasks=True)
    runtime = entry.runtime_data
    runtime.learning._days = {
        "2026-10-06": {"exposure": [[0, 86400]], "events": []},
        "2026-10-07": {"exposure": [[0, 86400]], "events": []},
    }
    model = await runtime.dream.async_rebuild()
    assert model.night_type_source == "calendar_unavailable"
    assert runtime.context.night_type(date(2026, 10, 6)) == WEEKDAY
    context = hass.states.get("sensor.luma_presence_context")
    assert context.attributes["night_type_source"] == "calendar_unavailable"


# ----------------------------------------------------------------------
# Weather
# ----------------------------------------------------------------------
def _night(night: date, rooms: dict[str, list[tuple[float, float]]]) -> NightRooms:
    return NightRooms(
        night, 6 * 3600, 720, [(0.0, 720.0)],
        {room: [(a, b, True) for a, b in spans] for room, spans in rooms.items()},
    )


def test_boosted_model_generates_and_scores() -> None:
    """A busier bad-weather night passes the boosted scorer, not the plain one."""
    nights = []
    for i in range(14):
        rooms: dict[str, list[tuple[float, float]]] = {"bano": [(300, 310)]}
        if i % 2 == 0:
            rooms["salon"] = [(100, 220)]
        if i % 3:
            rooms["cocina"] = [(150, 190)]
        nights.append(_night(TODAY - timedelta(days=1 + i), rooms))
    plain = build_day_type(nights, TODAY, 21)
    boosted = boost_model(plain, 0.5, 6 * 3600)
    assert boosted.rooms["salon"].start_rate[6] == pytest.approx(plain.rooms["salon"].start_rate[6] * 1.5)
    assert boosted.rooms["bano"] is plain.rooms["bano"]  # only the two busiest rooms

    rainy = {"salon": [(100, 220)], "cocina": [(150, 190)], "bano": [(300, 310)]}
    with_boost, _ = score_night(boosted, rainy, 60, 480)
    without, _ = score_night(plain, rainy, 60, 480)
    assert with_boost > without
    threshold = (with_boost + without) / 2
    assert choose_candidate([Candidate(1, rainy, with_boost, 0)], threshold, 0.7)[1] == DREAM_BEST
    assert choose_candidate([Candidate(1, rainy, without, 0)], threshold, 0.7)[1] == DREAM_LOW_SCORE

    # make_candidates scores with the same boosted model it generates from.
    candidates = make_candidates(plain, 60, 480, 6 * 3600, 5, 3, [], 0.5)
    for candidate in candidates:
        assert candidate.score == score_night(boosted, candidate.spans, 60, 480)[0]


async def test_bad_weather_raises_min_lights(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Sun mode keeps one more light on, never above the maximum."""
    hass.states.async_set(WEATHER, "rainy", {"temperature": 14})
    entry = await _setup(hass, freezer, {CONF_WEATHER_ENTITY: WEATHER}, hour=21)
    runtime = entry.runtime_data
    engine = runtime.engine
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
    )
    await hass.async_block_till_done()
    assert engine.weather_boost == 1
    settings = engine.settings
    settings.min_front_lights = settings.min_rear_lights = 0
    settings.min_lights, settings.max_lights = 2, 5
    for entity_id in fake_lights:
        hass.states.async_set(entity_id, STATE_ON if entity_id in ("light.salon", "light.hall") else STATE_OFF)
    await engine._async_step()
    assert sum(hass.states.get(e).state == STATE_ON for e in fake_lights) == 3

    # Capped at the maximum.
    settings.max_lights = 2
    for entity_id in fake_lights:
        hass.states.async_set(entity_id, STATE_ON if entity_id in ("light.salon", "light.hall") else STATE_OFF)
    for _ in range(10):
        await engine._async_step()
    assert sum(hass.states.get(e).state == STATE_ON for e in fake_lights) == 2
    context = hass.states.get("sensor.luma_presence_context")
    assert context.state == "modified"
    assert context.attributes["bad_weather"] is True
    assert context.attributes["boost_applied"] is True


async def test_replay_ignores_the_weather(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Replay plays a real night: the modifier is not applied, and it says so."""
    hass.states.async_set(WEATHER, "sunny", {"temperature": 3})  # cold
    entry = await _setup(hass, freezer, {CONF_WEATHER_ENTITY: WEATHER}, hour=21)
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.luma_presence_mode", "option": "replay"}, blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.engine.weather_boost == 0
    context = hass.states.get("sensor.luma_presence_context")
    assert context.attributes["bad_weather"] is True
    assert context.attributes["boost_applied"] is False
    assert context.attributes["boost_skipped"] == "replay"


# ----------------------------------------------------------------------
# Darkness
# ----------------------------------------------------------------------
async def test_darkness_tracker_rules(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Darkness start, clamping to both bounds, and the unavailable fallback."""
    sunset = _local(TODAY, 19, 30)
    tracker = DarknessTracker(hass, LUX, 50, timedelta(minutes=-60), timedelta(minutes=30), None)

    hass.states.async_set(LUX, STATE_UNAVAILABLE)
    freezer.move_to(_local(TODAY, 18))
    tracker.async_start()
    assert tracker.evening_start(TODAY, sunset) == (sunset, START_SENSOR_UNAVAILABLE)

    # Bright at 18:00; dark at 19:10: starts at 19:15 (5 minutes later).
    hass.states.async_set(LUX, "300")
    await hass.async_block_till_done()
    assert tracker.evening_start(TODAY, sunset) == (_local(TODAY, 20), START_CLAMPED_LATEST)
    freezer.move_to(_local(TODAY, 19, 10))
    hass.states.async_set(LUX, "20")
    await hass.async_block_till_done()
    freezer.move_to(_local(TODAY, 19, 16))
    assert tracker.evening_start(TODAY, sunset) == (_local(TODAY, 19, 15), START_DARKNESS)
    # Decided: a passing cloud does not move it.
    hass.states.async_set(LUX, "400")
    await hass.async_block_till_done()
    assert tracker.evening_start(TODAY, sunset) == (_local(TODAY, 19, 15), START_DARKNESS)

    # Already dark before the earliest start: starts at the earliest start.
    tomorrow = TODAY + timedelta(days=1)
    freezer.move_to(_local(tomorrow, 18, 0))
    hass.states.async_set(LUX, "10")
    await hass.async_block_till_done()
    freezer.move_to(_local(tomorrow, 18, 31))
    assert tracker.evening_start(tomorrow, _local(tomorrow, 19, 30)) == (
        _local(tomorrow, 18, 30),
        START_CLAMPED_EARLIEST,
    )
    tracker.async_shutdown()


async def test_darkness_starts_the_evening(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """The engine waits for darkness and starts as soon as it is confirmed."""
    hass.states.async_set(LUX, "500")
    entry = await _setup(hass, freezer, {CONF_DARKNESS_SENSOR: LUX}, hour=18)
    engine = entry.runtime_data.engine
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
    )
    await hass.async_block_till_done()
    assert engine.status == "waiting"
    next_event = hass.states.get("sensor.luma_presence_next_event")
    assert next_event.attributes["start_reason"] == START_CLAMPED_LATEST
    latest = engine.next_event

    await _move(hass, freezer, _local(TODAY, 19, 0))
    hass.states.async_set(LUX, "10")
    await hass.async_block_till_done()
    await _move(hass, freezer, _local(TODAY, 19, 5))
    await _until(lambda: engine.status == "simulating")
    assert dt_util.utcnow() < latest
    assert engine.evening_start_reason() == START_DARKNESS
    context = hass.states.get("sensor.luma_presence_context")
    assert context.attributes["evening_start_reason"] == START_DARKNESS
    assert context.state == "modified"


# ----------------------------------------------------------------------
# Morning window
# ----------------------------------------------------------------------
async def test_morning_window(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Workdays only, ends at sunrise, none when already light or on holidays."""
    _calendar(hass, [{"start": HOLIDAY.isoformat(), "end": (HOLIDAY + timedelta(days=1)).isoformat()}])
    entry = await _setup(
        hass, freezer, {CONF_MORNING_ENABLED: True, CONF_HOLIDAY_CALENDAR: CALENDAR}
    )
    engine = entry.runtime_data.engine
    friday = date(2026, 10, 16)
    start, end, reason = engine._morning(friday)
    assert (start, reason) == (_local(friday, 6, 30), "morning")
    # Sunrise (about 08:20) comes before the morning end.
    assert time(8, 0) < dt_util.as_local(end).time() < time(8, 30)
    assert engine._morning(date(2026, 10, 17)) is None  # Saturday
    await entry.runtime_data.context.async_ensure_holidays(TODAY, HOLIDAY)
    assert engine._morning(HOLIDAY) is None
    assert engine._morning(HOLIDAY + timedelta(days=1)) is not None

    # After sunrise there is no morning window.
    engine._morning_start = time(9, 0)
    engine._morning_end = time(10, 0)
    assert engine._morning(friday) is None

    # The engine schedules it and Replay/Dream treat it as the same night.
    engine._morning_start, engine._morning_end = time(6, 30), time(8, 30)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
    )
    await _move(hass, freezer, _local(friday, 3))
    assert engine.next_event == _local(friday, 6, 30)
    assert engine.next_event_reason == "morning"
    morning = engine.morning_window()
    assert to_night_seconds(dt_util.as_local(morning[0]), TODAY) == 18.5 * 3600


async def test_morning_options_validation(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """The morning end must follow its start."""
    entry = await _setup(hass, freezer)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "context"})
    result = await flow.async_configure(
        result["flow_id"],
        {CONF_MORNING_ENABLED: True, CONF_MORNING_START: "08:00:00", CONF_MORNING_END: "07:00:00"},
    )
    assert result["errors"] == {CONF_MORNING_END: "morning_range"}
    result = await flow.async_configure(
        result["flow_id"],
        {CONF_MORNING_ENABLED: True, CONF_BAD_WEATHER_BOOST: 30,
         CONF_MORNING_START: "06:00:00", CONF_MORNING_END: "07:30:00"},
    )
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert entry.options[CONF_MORNING_END] == "07:30:00"


# ----------------------------------------------------------------------
# Actuator verification
# ----------------------------------------------------------------------
def _disobey(hass: HomeAssistant, broken: set[str]) -> list[str]:
    """light.turn_on ignores the `broken` lights; returns the calls made."""
    calls: list[str] = []

    async def _turn_on(call: ServiceCall) -> None:
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            calls.append(entity_id)
            if entity_id not in broken:
                hass.states.async_set(entity_id, STATE_ON, context=call.context)

    hass.services.async_register("light", "turn_on", _turn_on)
    return calls


async def test_verification_retries_and_counts(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Retry once, then a failure; the command itself never waits."""
    entry = await _setup(hass, freezer)
    controller = entry.runtime_data.controller
    calls = _disobey(hass, {"light.salon"})

    assert await controller.async_switch("light.salon", True) is True  # did not wait
    assert calls == ["light.salon"]
    assert not controller.failure_counts
    await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=11))
    assert calls == ["light.salon", "light.salon"]  # retried
    await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=11))
    assert controller.failure_counts == {"light.salon": 1}
    assert "light.salon" in controller.failed_tonight
    assert controller.owner_of("light.salon") is None

    # A light that obeys is fine.
    assert await controller.async_switch("light.hall", True)
    await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=30))
    assert "light.hall" not in controller.failure_counts

    # Sun mode never picks it again tonight.
    engine = entry.runtime_data.engine
    engine.settings.min_front_lights = engine.settings.min_rear_lights = 0
    engine.settings.min_lights, engine.settings.max_lights = 4, 4
    calls.clear()
    for _ in range(5):
        await engine._async_step()
    assert "light.salon" not in calls


async def test_unresponsive_issue(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Three failures in a night raise an issue; a night without failures clears it."""
    entry = await _setup(hass, freezer)
    controller = entry.runtime_data.controller
    issue_id = f"light_unresponsive_{entry.entry_id}_light.salon"
    for _ in range(3):
        controller._failed("light.salon", True, "simulation")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == "light_unresponsive"
    assert controller.end_night() == {"light.salon": 3}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert controller.end_night() == {}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_plan_falls_back_to_the_same_room(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A Replay/Dream light that does not obey is replaced by its room mate."""
    entry = await _setup(hass, freezer, hour=21)
    runtime = entry.runtime_data
    player = runtime.player
    player.alternatives = lambda entity_id: ["light.hall"] if entity_id == "light.salon" else []
    _disobey(hass, {"light.salon"})
    night = TODAY
    now = to_night_seconds(dt_util.as_local(dt_util.utcnow()), night)
    plan = NightPlan(
        night=night,
        window=(now - 60, now + 3600),
        timeline=Timeline(
            {"light.salon": STATE_OFF},
            [Transition(now + 60, "light.salon", STATE_ON, None, "salon")],
        ),
        source="dream",
        seed=1,
    )
    task = hass.async_create_background_task(player.async_play(plan), "test_play")
    await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=61))
    await _until(lambda: runtime.controller.last_action is not None)
    await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=11))
    await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=11))
    await _until(lambda: hass.states.get("light.hall").state == STATE_ON)
    assert hass.states.get("light.salon").state == STATE_OFF
    last = runtime.controller.last_action
    assert (last["entity_id"], last.get("original")) == ("light.hall", "light.salon")
    task.cancel()


async def test_report_lists_failures(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """The morning report carries failures per light."""
    entry = await _setup(hass, freezer)
    runtime = entry.runtime_data
    runtime.controller._failed("light.salon", True, "simulation")
    runtime.controller._failed("light.salon", True, "simulation")
    runtime.reporter._active = True
    report = runtime.reporter.report()
    assert report["failures"] == {"light.salon": 2}
    runtime.reporter._handle_sunrise()
    assert runtime.controller.failure_counts == {}
