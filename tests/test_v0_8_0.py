"""Tests for v0.8.0: covers, ambience and deterrence."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import STATE_OFF, STATE_ON, SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from custom_components.luma_presence import deterrence as deterrence_module
from custom_components.luma_presence.config_flow import (
    _allowed_covers,
    _validate_covers,
)
from custom_components.luma_presence.const import (
    CONF_AMBIENCE_MEDIA,
    CONF_AMBIENCE_PLAYERS,
    CONF_COVERS,
    CONF_COVERS_CLOSE_DELAY,
    CONF_COVERS_MODE,
    CONF_COVERS_OPEN_WORKDAY,
    CONF_DARKNESS_SENSOR,
    CONF_DETERRENCE_COOLDOWN,
    CONF_DETERRENCE_ENABLED,
    CONF_DETERRENCE_MAX,
    CONF_EVENT_TYPES,
    CONF_FACADE,
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    CONF_PATH,
    CONF_TRIGGER_ENTITY,
    CONF_VERIFY_TIMEOUT,
    CONF_WORKDAY_SENSOR,
    COVER_DREAM,
    COVER_NOT_ENOUGH_DAYS,
    COVER_REPLAY,
    COVER_SCHEDULE,
    DOMAIN,
    EVENT_DETERRENCE,
    OWNER_DETERRENCE,
    OWNER_SIMULATION,
    SUBENTRY_DETERRENCE,
)
from custom_components.luma_presence.covers import cover_value, observations
from custom_components.luma_presence.deterrence import DeterrenceTrigger

from .conftest import until
from .test_v0_2_0 import OPTIONS
from .test_v0_7_0 import FRIDAY, HOLIDAY, LUX, TODAY, WORKDAY, _local, _move, _workday

SATURDAY = FRIDAY + timedelta(days=1)
BLIND = "cover.persiana_salon"
CURTAIN = "cover.cortina"
AWNING = "cover.toldo"
GARAGE = "cover.garaje"
PLAYER = "media_player.salon"
TV_LIGHT = "light.tv_salon"
PORCH = "binary_sensor.porche"
DOORBELL = "event.timbre"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _covers(hass: HomeAssistant, obey: bool = True) -> list[ServiceCall]:
    """Covers whose services keep the caller context."""
    calls: list[ServiceCall] = []
    hass.states.async_set(
        BLIND,
        "closed",
        {"device_class": "shutter", "current_position": 0, "supported_features": 15},
    )
    hass.states.async_set(CURTAIN, "closed", {"device_class": "curtain"})
    hass.states.async_set(AWNING, "closed", {"device_class": "awning"})
    hass.states.async_set(GARAGE, "closed", {"device_class": "garage"})

    def _set(entity_id: str, state: str, position: int, context: Any) -> None:
        attributes = dict(hass.states.get(entity_id).attributes)
        if "current_position" in attributes:
            attributes["current_position"] = position
        hass.states.async_set(entity_id, state, attributes, context=context)

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)
        if not obey:
            return
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            if call.service == "close_cover":
                _set(entity_id, "closed", 0, call.context)
            elif call.service == "open_cover":
                _set(entity_id, "open", 100, call.context)
            else:
                _set(entity_id, "open", int(call.data["position"]), call.context)

    for service in ("open_cover", "close_cover", "set_cover_position"):
        hass.services.async_register("cover", service, _handler)
    return calls


def _areas(hass: HomeAssistant) -> dict[str, Any]:
    """Living room: a TV backlight (a simulation light) and the media player."""
    salon = ar.async_get(hass).async_get_or_create("Salon")
    entities = er.async_get(hass)
    for domain, object_id in (("light", "tv_salon"), ("media_player", "salon")):
        entry = entities.async_get_or_create(
            domain, "test", object_id, suggested_object_id=object_id
        )
        entities.async_update_entity(entry.entity_id, area_id=salon.id)
    hass.states.async_set(TV_LIGHT, STATE_OFF)
    return {CONF_LIGHTS: [*OPTIONS[CONF_LIGHTS], TV_LIGHT]}


def _media(hass: HomeAssistant) -> list[ServiceCall]:
    calls: list[ServiceCall] = []
    hass.states.async_set(PLAYER, "idle", {"volume_level": 0.4, "source": "TV"})

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)
        state = hass.states.get(PLAYER)
        attributes = dict(state.attributes)
        value = state.state
        if call.service == "volume_set":
            attributes["volume_level"] = call.data["volume_level"]
        elif call.service == "play_media":
            value = "playing"
        elif call.service == "media_stop":
            value = "idle"
        elif call.service == "turn_off":
            value = "off"
        elif call.service == "select_source":
            attributes["source"] = call.data["source"]
        hass.states.async_set(PLAYER, value, attributes, context=call.context)

    for service in (
        "volume_set",
        "play_media",
        "media_stop",
        "turn_off",
        "select_source",
    ):
        hass.services.async_register("media_player", service, _handler)
    return calls


async def _setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any] | None = None,
    hour: int = 12,
    day: date = TODAY,
    minute: int = 0,
    triggers: list[dict[str, Any]] | None = None,
    absent: bool = True,
) -> MockConfigEntry:
    freezer.move_to(_local(day, hour, minute))
    hass.states.async_set("person.mario", "not_home" if absent else "home")
    hass.states.async_set(
        "alarm_control_panel.home_alarm", "armed_away" if absent else "disarmed"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LumaPresence",
        options={**OPTIONS, **(options or {})},
        version=3,
        subentries_data=[
            ConfigSubentryData(
                data=data,
                subentry_type=SUBENTRY_DETERRENCE,
                title=data[CONF_TRIGGER_ENTITY],
                unique_id=None,
            )
            for data in triggers or []
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    settings = entry.runtime_data.settings
    settings.delayed_start = settings.delayed_end = False
    return entry


async def _turn_on(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.luma_presence_simulation"},
        blocking=True,
    )
    await hass.async_block_till_done()


def _on(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id).state == STATE_ON


# ----------------------------------------------------------------------
# Covers
# ----------------------------------------------------------------------
async def test_forbidden_cover_classes(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Awnings, garages, gates and doors are never offered nor accepted."""
    _covers(hass)
    hass.states.async_set("cover.cancela", "closed", {"device_class": "gate"})
    hass.states.async_set("cover.puerta", "closed", {"device_class": "door"})
    assert _allowed_covers(hass) == [CURTAIN, BLIND]
    for forbidden in (AWNING, GARAGE, "cover.cancela", "cover.puerta"):
        assert _validate_covers(hass, {CONF_COVERS: [BLIND, forbidden]}) == {
            CONF_COVERS: "cover_forbidden_class"
        }
    assert _validate_covers(hass, {CONF_COVERS: [BLIND, CURTAIN]}) == {}

    # The options step stores the allowed covers.
    entry = await _setup(hass, freezer)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "covers"})
    result = await flow.async_configure(result["flow_id"], {CONF_COVERS: [BLIND]})
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert entry.options[CONF_COVERS] == [BLIND]


async def test_cover_schedule_open_times(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Workday and non-workday opening times, never before sunrise."""
    _covers(hass)
    _workday(hass, {HOLIDAY})
    entry = await _setup(
        hass, freezer, {CONF_COVERS: [BLIND], CONF_WORKDAY_SENSOR: WORKDAY}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    runtime = entry.runtime_data
    await runtime.context.async_ensure_holidays(TODAY, HOLIDAY + timedelta(days=1))
    covers = runtime.covers
    # 08:00 on a workday is before sunrise in October: it waits for sunrise.
    sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, FRIDAY)
    move = covers._open_move(BLIND, FRIDAY)
    assert (move.at, move.source) == (sunrise, COVER_SCHEDULE)
    assert covers._open_move(BLIND, SATURDAY).at == _local(SATURDAY, 9, 30)
    assert covers._open_move(BLIND, HOLIDAY).at == _local(HOLIDAY, 9, 30)
    covers._open_workday = covers._open_workday.replace(hour=9)
    assert covers._open_move(BLIND, FRIDAY).at == _local(FRIDAY, 9)


async def test_cover_close_at_darkness_decision(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Covers close when the evening darkness is decided (sunset without a sensor)."""
    _covers(hass)
    hass.states.async_set(CURTAIN, "open", {"device_class": "curtain"})
    entry = await _setup(
        hass, freezer, {CONF_COVERS: [CURTAIN], CONF_COVERS_CLOSE_DELAY: 0}
    )
    sunset = get_astral_event_date(hass, SUN_EVENT_SUNSET, TODAY)
    assert entry.runtime_data.covers._close_move(CURTAIN, TODAY).at == sunset
    await hass.config_entries.async_remove(entry.entry_id)

    hass.states.async_set(LUX, "500")
    entry = await _setup(
        hass,
        freezer,
        {CONF_COVERS: [CURTAIN], CONF_COVERS_CLOSE_DELAY: 0, CONF_DARKNESS_SENSOR: LUX},
        hour=18,
    )
    covers = entry.runtime_data.covers
    assert covers._close_move(CURTAIN, TODAY).pending
    await _move(hass, freezer, _local(TODAY, 19, 10))
    hass.states.async_set(LUX, "10")
    await hass.async_block_till_done()
    await _move(hass, freezer, _local(TODAY, 19, 15) + timedelta(seconds=1))
    await until(hass, lambda: hass.states.get(CURTAIN).state == "closed")
    assert dt_util.utcnow() < sunset + timedelta(minutes=30)


async def test_covers_learned_and_fallback(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Learned times per day type; Replay's source night; schedule under 7 days."""
    _covers(hass)
    entry = await _setup(
        hass, freezer, {CONF_COVERS: [BLIND], CONF_COVERS_MODE: "learned"}
    )
    runtime = entry.runtime_data
    covers = runtime.covers

    def learn(days: int) -> None:
        learned: dict[str, Any] = {}
        for offset in range(1, days + 1):
            day = TODAY - timedelta(days=offset)
            opening = 7 * 3600 + 1800 if day.weekday() < 5 else 10 * 3600
            learned[day.isoformat()] = {
                "exposure": [[0, 86400]],
                "events": [],
                "covers": [
                    [BLIND, "open", opening, None],
                    [BLIND, "closed", 20 * 3600, None],
                ],
            }
        runtime.learning._days = learned
        covers._observed.clear()

    learn(6)
    assert covers._open_move(BLIND, FRIDAY).source == COVER_NOT_ENOUGH_DAYS
    learn(21)
    move = covers._open_move(BLIND, FRIDAY)
    assert (move.at, move.source) == (_local(FRIDAY, 7, 30), COVER_DREAM)
    assert covers._open_move(BLIND, SATURDAY).at == _local(SATURDAY, 10)
    assert covers._close_move(BLIND, TODAY).at == _local(TODAY, 20)

    # Replay: the times of its source night and of the next morning.
    source = TODAY - timedelta(days=7)
    runtime.learning._days[source.isoformat()]["covers"][1][2] = 21 * 3600
    runtime.learning._days[(source + timedelta(days=1)).isoformat()]["covers"][0][2] = (
        7 * 3600
    )
    covers._observed.clear()
    runtime.settings.mode = "replay"
    runtime.replay.status = {"source": source}
    move = covers._close_move(BLIND, TODAY)
    assert (move.at, move.source) == (_local(TODAY, 21), COVER_REPLAY)
    assert covers._open_move(BLIND, FRIDAY).at == _local(FRIDAY, 7)


async def test_covers_move_only_on_real_absence(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Covers open on a real absence, even by day; never during a test."""
    calls = _covers(hass)
    hass.states.async_set(LUX, "900")
    options = {
        CONF_COVERS: [BLIND],
        CONF_COVERS_OPEN_WORKDAY: "09:00:00",
        CONF_DARKNESS_SENSOR: LUX,
    }
    entry = await _setup(hass, freezer, options, hour=8, day=FRIDAY)
    runtime = entry.runtime_data
    await _move(hass, freezer, _local(FRIDAY, 8, 30))
    assert runtime.context.daylight()  # the daylight guard does not block covers
    await _move(hass, freezer, _local(FRIDAY, 9) + timedelta(seconds=1))
    await until(hass, lambda: hass.states.get(BLIND).state == "open")
    assert calls[-1].service == "open_cover"
    assert runtime.controller.is_own_context(hass.states.get(BLIND).context.id)
    report = runtime.reporter.report()
    assert (report["cover_moves"], report["covers"]) == (1, [BLIND])
    # The plan sensor lists the next moves.
    runtime.covers._notify()
    await hass.async_block_till_done()
    plan = hass.states.get("sensor.luma_presence_tonight_plan")
    assert plan.attributes["covers"][0]["entity_id"] == BLIND
    await hass.config_entries.async_remove(entry.entry_id)

    # Someone at home testing the simulation: covers stay as they are.
    hass.states.async_set(
        BLIND,
        "closed",
        {"device_class": "shutter", "current_position": 0, "supported_features": 15},
    )
    calls.clear()
    entry = await _setup(hass, freezer, options, hour=8, day=SATURDAY, absent=False)
    await _turn_on(hass)
    assert "simulation_test" in entry.runtime_data.learning.reasons
    await _move(hass, freezer, _local(SATURDAY, 9, 30) + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert hass.states.get(BLIND).state == "closed"
    assert calls == []


async def test_cover_learning_records_human_moves(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Human moves are learned; LumaPresence's own moves are not."""
    _covers(hass)
    entry = await _setup(hass, freezer, {CONF_COVERS: [BLIND]}, absent=False)
    runtime = entry.runtime_data
    assert runtime.learning.is_open
    attributes = {"device_class": "shutter", "supported_features": 15}
    hass.states.async_set(BLIND, "opening", {**attributes, "current_position": 0})
    hass.states.async_set(BLIND, "open", {**attributes, "current_position": 60})
    await runtime.controller.async_move_cover(BLIND, "close")
    await hass.async_block_till_done()
    pending = [(e[0], e[1]) for e in runtime.learning._pending]
    assert pending == [(BLIND, 60)]
    assert cover_value(hass.states.get(BLIND)) == "closed"
    runtime.learning._commit_until(dt_util.utcnow() + timedelta(seconds=1))
    stored = runtime.learning.days[TODAY.isoformat()]
    assert [c[:2] for c in stored["covers"]] == [[BLIND, 60]]
    assert stored["events"] == []
    # Morning openings (04:00-13:00) and evening closings (from 15:00).
    learned = {
        "2026-10-01": {
            "covers": [
                [BLIND, "closed", 3 * 3600, None],
                [BLIND, 60, 8 * 3600, None],
                [CURTAIN, "open", 8 * 3600, None],
                [BLIND, "closed", 14 * 3600, None],
                [BLIND, "closed", 20 * 3600, None],
            ]
        }
    }
    assert observations(learned, BLIND) == {date(2026, 10, 1): (8 * 3600, 20 * 3600)}


# ----------------------------------------------------------------------
# Ambience
# ----------------------------------------------------------------------
async def test_ambience_sessions(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Plays while the room is occupied, stops when idle or quiet, restores the player."""
    lights = _areas(hass)
    calls = _media(hass)
    options = {
        **lights,
        CONF_AMBIENCE_PLAYERS: [PLAYER],
        CONF_AMBIENCE_MEDIA: "http://radio.example/stream",
    }
    entry = await _setup(hass, freezer, options, hour=21)
    runtime = entry.runtime_data
    runtime.engine._async_step = AsyncMock()  # no random lights
    await _turn_on(hass)
    assert runtime.engine.status == "simulating"
    assert not calls

    await runtime.controller.async_switch(TV_LIGHT, True, OWNER_SIMULATION)
    await until(hass, lambda: hass.states.get(PLAYER).state == "playing")
    assert [(c.service, c.data.get("volume_level")) for c in calls[:1]] == [
        ("volume_set", 0.15)
    ]
    assert calls[1].data["media_content_id"] == "http://radio.example/stream"

    # The room becomes idle: stop and restore.
    await runtime.controller.async_switch(TV_LIGHT, False)
    await until(
        hass,
        lambda: (
            hass.states.get(PLAYER).attributes["source"] == "TV"
            and hass.states.get(PLAYER).state == "idle"
        ),
    )
    assert hass.states.get(PLAYER).attributes["volume_level"] == 0.4

    # Again, until the quiet time.
    await runtime.controller.async_switch(TV_LIGHT, True, OWNER_SIMULATION)
    await until(hass, lambda: hass.states.get(PLAYER).state == "playing")
    await _move(hass, freezer, _local(TODAY, 23, 30))
    await until(hass, lambda: hass.states.get(PLAYER).state == "idle")
    assert runtime.ambience.quiet()
    assert hass.states.get(PLAYER).attributes["volume_level"] == 0.4
    assert runtime.reporter.report()["ambience_sessions"] == 2


async def test_ambience_needs_a_real_absence(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A simulation test with people at home plays nothing."""
    lights = _areas(hass)
    calls = _media(hass)
    options = {**lights, CONF_AMBIENCE_PLAYERS: [PLAYER], CONF_AMBIENCE_MEDIA: "radio"}
    entry = await _setup(hass, freezer, options, hour=21, absent=False)
    runtime = entry.runtime_data
    runtime.engine._async_step = AsyncMock()
    await _turn_on(hass)
    await runtime.controller.async_switch(TV_LIGHT, True, OWNER_SIMULATION)
    await hass.async_block_till_done()
    assert calls == []


# ----------------------------------------------------------------------
# Deterrence
# ----------------------------------------------------------------------
TRIGGER = {CONF_TRIGGER_ENTITY: PORCH, CONF_FACADE: "front", CONF_PATH: ["light.hall"]}
DETERRENCE = {CONF_DETERRENCE_ENABLED: True}


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record every wait of a reaction; the hold blocks until released."""
    state: dict[str, Any] = {"waits": [], "release": asyncio.Event()}

    async def _sleep(seconds: float) -> None:
        state["waits"].append(seconds)
        if seconds >= 60:
            await state["release"].wait()

    monkeypatch.setattr(deterrence_module, "_sleep", _sleep)
    return state


def _porch(hass: HomeAssistant, value: str) -> None:
    hass.states.async_set(PORCH, value, {"device_class": "motion"})


async def _detect(hass: HomeAssistant) -> None:
    _porch(hass, STATE_ON)
    await hass.async_block_till_done()
    _porch(hass, STATE_OFF)
    await hass.async_block_till_done()


async def test_deterrence_reaction_between_windows(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """At 02:00, between the evening window and the morning: facade light, path, hold, off."""
    _porch(hass, STATE_OFF)
    events = []
    hass.bus.async_listen(EVENT_DETERRENCE, events.append)
    entry = await _setup(
        hass, freezer, DETERRENCE, hour=2, day=FRIDAY, triggers=[TRIGGER]
    )
    runtime = entry.runtime_data
    deterrence = runtime.deterrence
    assert runtime.engine.status == "idle" and deterrence.active()

    await _detect(hass)
    await until(hass, lambda: len(waits["waits"]) == 3)
    assert _on(hass, "light.bedroom") and _on(hass, "light.hall")
    first, path_delay, hold = waits["waits"]
    assert 2 <= first <= 6 and 10 <= path_delay <= 25 and 180 <= hold <= 480
    assert runtime.controller.owner_of("light.bedroom") == OWNER_DETERRENCE
    assert runtime.controller.is_locked("light.bedroom")
    assert events[0].data["lights"] == ["light.bedroom", "light.hall"]
    assert events[0].data["facade"] == "front" and events[0].data["trigger"] == PORCH
    sensor = hass.states.get("sensor.luma_presence_last_deterrence")
    assert sensor.attributes["reactions_tonight"] == 1
    assert sensor.attributes["lights"] == ["light.bedroom", "light.hall"]

    # Nothing running: the lights go off after the hold.
    waits["release"].set()
    await until(hass, lambda: not deterrence.running)
    for _ in range(4):
        await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=30))
    await until(
        hass, lambda: not _on(hass, "light.bedroom") and not _on(hass, "light.hall")
    )
    assert not runtime.controller.is_locked("light.bedroom")
    assert runtime.reporter.report()["deterrence_reactions"] == 1


async def test_deterrence_inactive_by_day_or_at_home(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """No reaction in daylight, in the afternoon, or with people at home."""
    _porch(hass, STATE_OFF)
    entry = await _setup(hass, freezer, DETERRENCE, hour=15, triggers=[TRIGGER])
    deterrence = entry.runtime_data.deterrence
    assert not deterrence.active()  # afternoon, before the evening gets dark
    await _detect(hass)
    assert not deterrence.running and deterrence.reactions_tonight == 0
    await hass.config_entries.async_remove(entry.entry_id)

    entry = await _setup(
        hass, freezer, DETERRENCE, hour=23, triggers=[TRIGGER], absent=False
    )
    assert not entry.runtime_data.deterrence.active()  # someone at home
    await hass.config_entries.async_remove(entry.entry_id)

    hass.states.async_set(LUX, "900")
    entry = await _setup(
        hass,
        freezer,
        {**DETERRENCE, CONF_DARKNESS_SENSOR: LUX},
        hour=7,
        day=FRIDAY,
        triggers=[TRIGGER],
    )
    deterrence = entry.runtime_data.deterrence
    assert deterrence.active()  # morning, not yet light for 5 minutes
    await _move(hass, freezer, _local(FRIDAY, 7, 6))
    assert not deterrence.active()  # the daylight guard ended the night


async def test_deterrence_cooldown_and_limit(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """A trigger waits for its cooldown; reactions per night are limited."""
    waits["release"].set()
    _porch(hass, STATE_OFF)
    options = {**DETERRENCE, CONF_DETERRENCE_COOLDOWN: 10, CONF_DETERRENCE_MAX: 2}
    entry = await _setup(hass, freezer, options, hour=23, triggers=[TRIGGER])
    deterrence = entry.runtime_data.deterrence
    await _detect(hass)
    await until(
        hass, lambda: deterrence.reactions_tonight == 1 and not deterrence.running
    )
    await _move(hass, freezer, _local(TODAY, 23, 5))
    await _detect(hass)
    assert deterrence.reactions_tonight == 1  # cooling down
    await _move(hass, freezer, _local(TODAY, 23, 11))
    await _detect(hass)
    await until(
        hass, lambda: deterrence.reactions_tonight == 2 and not deterrence.running
    )
    await _move(hass, freezer, _local(TODAY, 23, 30))
    await _detect(hass)
    assert deterrence.reactions_tonight == 2  # nightly limit
    # A new night.
    await _move(hass, freezer, _local(FRIDAY, 23))
    await _detect(hass)
    await until(hass, lambda: deterrence.reactions_tonight == 1)


async def test_deterrence_hands_back_to_the_plan(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """Lights the plan wants on stay on for the simulation; the others go off."""
    _porch(hass, STATE_OFF)
    entry = await _setup(hass, freezer, DETERRENCE, hour=23, triggers=[TRIGGER])
    runtime = entry.runtime_data
    runtime.player._active = True
    runtime.player.desired_state = lambda light: (
        STATE_ON if light == "light.bedroom" else STATE_OFF
    )
    await _detect(hass)
    await until(hass, lambda: _on(hass, "light.bedroom") and _on(hass, "light.hall"))
    waits["release"].set()
    await until(hass, lambda: not runtime.deterrence.running)
    for _ in range(4):
        await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=30))
    await until(hass, lambda: not _on(hass, "light.hall"))
    assert _on(hass, "light.bedroom")
    assert runtime.controller.owner_of("light.bedroom") == OWNER_SIMULATION


async def test_deterrence_same_facade_fallback(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """A facade light that does not obey is replaced by another of the facade."""
    _porch(hass, STATE_OFF)

    async def _ignore(call: ServiceCall) -> None:
        """switch.oficina, the first front light in configuration order, ignores us."""

    hass.services.async_register("switch", "turn_on", _ignore)
    options = {
        **DETERRENCE,
        CONF_FRONT_LIGHTS: ["light.bedroom", "switch.oficina"],
        CONF_VERIFY_TIMEOUT: 5,
    }
    entry = await _setup(
        hass, freezer, options, hour=23, triggers=[{**TRIGGER, CONF_PATH: []}]
    )
    runtime = entry.runtime_data
    await _detect(hass)
    await until(hass, lambda: "switch.oficina" in runtime.deterrence._held)
    for _ in range(3):
        await _move(hass, freezer, dt_util.utcnow() + timedelta(seconds=6))
    await until(hass, lambda: _on(hass, "light.bedroom"))
    assert runtime.controller.failure_counts["switch.oficina"] == 1
    assert runtime.controller.owner_of("light.bedroom") == OWNER_DETERRENCE
    assert "light.bedroom" in runtime.deterrence.last["lights"]


async def test_test_deterrence_button(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """The button reacts without an absence, but never by day."""
    entry = await _setup(hass, freezer, {}, hour=12)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.luma_presence_test_deterrence"},
            blocking=True,
        )
    assert err.value.translation_key == "no_deterrence_triggers"
    await hass.config_entries.async_remove(entry.entry_id)

    _porch(hass, STATE_OFF)
    hass.states.async_set(LUX, "900")
    entry = await _setup(
        hass,
        freezer,
        {CONF_DARKNESS_SENSOR: LUX},
        hour=12,
        triggers=[TRIGGER],
        absent=False,
    )
    await _move(hass, freezer, _local(TODAY, 12, 6))
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.luma_presence_test_deterrence"},
            blocking=True,
        )
    assert err.value.translation_key == "daylight"
    hass.states.async_set(LUX, "5")
    await hass.async_block_till_done()
    await _move(hass, freezer, _local(TODAY, 12, 12))
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.luma_presence_test_deterrence"},
        blocking=True,
    )
    await until(hass, lambda: _on(hass, "light.bedroom"))
    assert entry.runtime_data.deterrence.running


async def test_deterrence_trigger_subentry(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Only motion, occupancy or presence sensors, or event entities."""
    entry = await _setup(hass, freezer)
    hass.states.async_set("binary_sensor.puerta", STATE_OFF, {"device_class": "door"})
    _porch(hass, STATE_OFF)
    hass.states.async_set(DOORBELL, "unknown", {"event_types": ["ring"]})
    manager = hass.config_entries.subentries
    result = await manager.async_init(
        (entry.entry_id, SUBENTRY_DETERRENCE), context={"source": "user"}
    )
    result = await manager.async_configure(
        result["flow_id"],
        {CONF_TRIGGER_ENTITY: "binary_sensor.puerta", CONF_FACADE: "front"},
    )
    assert result["errors"] == {CONF_TRIGGER_ENTITY: "trigger_not_allowed"}
    result = await manager.async_configure(
        result["flow_id"],
        {
            CONF_TRIGGER_ENTITY: DOORBELL,
            CONF_FACADE: "rear",
            CONF_PATH: ["light.salon"],
        },
    )
    # An event entity: its own event types are offered.
    assert result["step_id"] == "event_types"
    selector = result["data_schema"].schema[CONF_EVENT_TYPES]
    assert selector.config["options"] == ["ring"]
    result = await manager.async_configure(
        result["flow_id"], {CONF_EVENT_TYPES: ["ring"]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    triggers = entry.runtime_data.deterrence.triggers
    assert [(t.entity_id, t.facade, t.path, t.event_types) for t in triggers] == [
        (DOORBELL, "rear", ("light.salon",), ("ring",))
    ]

    # A binary sensor has no event types and no second step.
    result = await manager.async_init(
        (entry.entry_id, SUBENTRY_DETERRENCE), context={"source": "user"}
    )
    result = await manager.async_configure(
        result["flow_id"],
        {CONF_TRIGGER_ENTITY: PORCH, CONF_FACADE: "front"},
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_EVENT_TYPES] == []


CAMERA = "event.camara_entrada"


def _camera(hass: HomeAssistant, stamp: int, event_type: str) -> None:
    hass.states.async_set(
        CAMERA,
        f"2026-10-15T23:00:{stamp:02d}.000+00:00",
        {"event_types": ["person", "vehicle", "animal"], "event_type": event_type},
    )


async def test_deterrence_event_type_filter(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, waits
) -> None:
    """With event types, only those events react; without them, any event."""
    waits["release"].set()
    _camera(hass, 0, "person")
    camera = {
        CONF_TRIGGER_ENTITY: CAMERA,
        CONF_FACADE: "front",
        CONF_EVENT_TYPES: ["person"],
    }
    entry = await _setup(
        hass,
        freezer,
        {**DETERRENCE, CONF_DETERRENCE_COOLDOWN: 0},
        hour=23,
        triggers=[camera],
    )
    deterrence = entry.runtime_data.deterrence
    for stamp, kind in ((1, "vehicle"), (2, "animal")):
        _camera(hass, stamp, kind)
        await hass.async_block_till_done()
    assert deterrence.reactions_tonight == 0  # cars and animals are ignored
    _camera(hass, 3, "person")
    await until(
        hass, lambda: deterrence.reactions_tonight == 1 and not deterrence.running
    )
    await hass.config_entries.async_remove(entry.entry_id)

    # No event types: any event reacts.
    entry = await _setup(
        hass,
        freezer,
        {**DETERRENCE, CONF_DETERRENCE_COOLDOWN: 0},
        hour=23,
        triggers=[{**camera, CONF_EVENT_TYPES: []}],
    )
    deterrence = entry.runtime_data.deterrence
    _camera(hass, 4, "vehicle")
    await until(hass, lambda: deterrence.reactions_tonight == 1)


def test_trigger_accepts() -> None:
    """The filter applies to event entities only."""
    state = type("State", (), {"attributes": {"event_type": "vehicle"}})()
    camera = DeterrenceTrigger("1", "cam", CAMERA, "front", (), ("person",))
    assert not camera.accepts(state)
    assert DeterrenceTrigger("2", "cam", CAMERA, "front", ()).accepts(state)
    # A binary sensor ignores any stored event types.
    assert DeterrenceTrigger("3", "porch", PORCH, "front", (), ("person",)).accepts(
        state
    )
