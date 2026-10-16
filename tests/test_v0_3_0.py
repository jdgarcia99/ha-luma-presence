"""Tests for v0.3.0: night routes, sequence engine and shared light control."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigSubentryData,
)
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    SUN_EVENT_SUNRISE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from custom_components.luma_presence import sequences as sequences_module
from custom_components.luma_presence.const import (
    CONF_DESTINATION,
    CONF_DWELL_MAX,
    CONF_DWELL_MIN,
    CONF_LEARN_EXTRA,
    CONF_ORIGIN,
    CONF_PATH,
    CONF_STEP_DELAY_MAX,
    CONF_STEP_DELAY_MIN,
    DOMAIN,
    EVENT_ACTION,
    OWNER_SEQUENCE,
    SUBENTRY_ROUTE,
)

from .conftest import until
from .test_v0_2_0 import OPTIONS

BEDROOM = "light.dormitorio"
HALL = "light.pasillo"
LANDING = "light.distribuidor"
BATHROOM = "light.bano"
TOILET = "light.aseo"
ROUTE_LIGHTS = [BEDROOM, HALL, LANDING, BATHROOM, TOILET]

BATH_ROUTE = ("Bathroom", {CONF_ORIGIN: BEDROOM, CONF_PATH: [HALL, LANDING], CONF_DESTINATION: BATHROOM})
TOILET_ROUTE = ("Toilet", {CONF_ORIGIN: BEDROOM, CONF_PATH: [], CONF_DESTINATION: TOILET})
TIMING = {CONF_STEP_DELAY_MIN: 10, CONF_STEP_DELAY_MAX: 10, CONF_DWELL_MIN: 2, CONF_DWELL_MAX: 2}


async def _setup(
    hass: HomeAssistant,
    routes: list[tuple[str, dict[str, Any]]] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    for entity_id in ROUTE_LIGHTS:
        if hass.states.get(entity_id) is None:
            hass.states.async_set(entity_id, STATE_OFF)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LumaPresence",
        data={},
        options={**OPTIONS, **TIMING, **(options or {})},
        version=2,
        subentries_data=[
            ConfigSubentryData(
                data=data, subentry_type=SUBENTRY_ROUTE, title=name, unique_id=None
            )
            for name, data in routes or []
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _is_on(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id).state == STATE_ON


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make every route wait return at once, recording its length."""
    recorded: list[float] = []

    async def _no_wait(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(sequences_module, "_sleep", _no_wait)
    return recorded


@pytest.fixture
def blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every route wait block, so a route stays in progress."""

    async def _forever(_seconds: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(sequences_module, "_sleep", _forever)


def _switches(events) -> list[tuple[str, str]]:
    return [(e.data["action"], e.data["entity_id"]) for e in events if e.data["entity_id"]]


# ----------------------------------------------------------------------
# Route subentries
# ----------------------------------------------------------------------
async def test_route_subentry_flow(hass: HomeAssistant, fake_lights) -> None:
    """Routes are created and reconfigured as subentries, with validation."""
    entry = await _setup(hass)
    subentries = hass.config_entries.subentries

    result = await subentries.async_init(
        (entry.entry_id, SUBENTRY_ROUTE), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await subentries.async_configure(
        result["flow_id"],
        {"name": "Bathroom", CONF_ORIGIN: BEDROOM, CONF_DESTINATION: BEDROOM},
    )
    assert result["errors"] == {CONF_DESTINATION: "same_origin_destination"}
    result = await subentries.async_configure(
        result["flow_id"],
        {
            "name": "Bathroom",
            CONF_ORIGIN: BEDROOM,
            CONF_PATH: [HALL, BEDROOM],
            CONF_DESTINATION: BATHROOM,
        },
    )
    assert result["errors"] == {CONF_PATH: "duplicate_light"}
    result = await subentries.async_configure(
        result["flow_id"],
        {"name": "Bathroom", CONF_ORIGIN: BEDROOM, CONF_PATH: [HALL], CONF_DESTINATION: BATHROOM},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    (subentry,) = entry.subentries.values()
    assert subentry.title == "Bathroom"
    assert dict(subentry.data) == {
        CONF_ORIGIN: BEDROOM,
        CONF_PATH: [HALL],
        CONF_DESTINATION: BATHROOM,
    }
    # Adding a route reloads the entry, which picks it up.
    assert [r.name for r in entry.runtime_data.sequences.routes] == ["Bathroom"]
    assert hass.states.get("sensor.luma_presence_next_sequence").attributes["routes"] == [
        "Bathroom"
    ]

    result = await subentries.async_init(
        (entry.entry_id, SUBENTRY_ROUTE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await subentries.async_configure(
        result["flow_id"],
        {"name": "Toilet", CONF_ORIGIN: TOILET, CONF_DESTINATION: TOILET},
    )
    assert result["errors"] == {CONF_DESTINATION: "same_origin_destination"}
    result = await subentries.async_configure(
        result["flow_id"],
        {"name": "Toilet", CONF_ORIGIN: BEDROOM, CONF_DESTINATION: TOILET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    subentry = entry.subentries[subentry.subentry_id]
    assert subentry.title == "Toilet"
    assert subentry.data[CONF_PATH] == []
    assert subentry.data[CONF_DESTINATION] == TOILET
    assert [r.name for r in entry.runtime_data.sequences.routes] == ["Toilet"]


# ----------------------------------------------------------------------
# Route execution
# ----------------------------------------------------------------------
async def test_route_order_and_timings(
    hass: HomeAssistant, fake_lights, waits: list[float]
) -> None:
    """Lights go on along the path and off in reverse order, with the waits."""
    entry = await _setup(hass, [BATH_ROUTE])
    sequences = entry.runtime_data.sequences
    controller = entry.runtime_data.controller
    events = async_capture_events(hass, EVENT_ACTION)

    sequences.async_run_now()
    await sequences._task
    await hass.async_block_till_done()

    assert _switches(events) == [
        ("on", BEDROOM),
        ("on", HALL),
        ("on", LANDING),
        ("on", BATHROOM),
        ("off", BATHROOM),
        ("off", LANDING),
        ("off", HALL),
        ("off", BEDROOM),
    ]
    assert {e.data["route"] for e in events} == {"Bathroom"}
    # Three steps out, the dwell (minutes), three steps back.
    assert waits == [10, 10, 10, 120, 10, 10, 10]
    assert not any(_is_on(hass, light) for light in ROUTE_LIGHTS)
    assert not sequences.running
    assert not any(controller.is_locked(light) for light in ROUTE_LIGHTS)
    assert hass.states.get("sensor.luma_presence_last_action").attributes["route"] == "Bathroom"


async def test_route_leaves_prelit_lights_on(
    hass: HomeAssistant, fake_lights, waits: list[float]
) -> None:
    """A light that was already on is not switched, and stays on."""
    hass.states.async_set(BEDROOM, STATE_ON)
    entry = await _setup(hass, [BATH_ROUTE])
    sequences = entry.runtime_data.sequences
    events = async_capture_events(hass, EVENT_ACTION)

    sequences.async_run_now()
    await sequences._task
    await hass.async_block_till_done()

    assert BEDROOM not in [entity_id for _, entity_id in _switches(events)]
    assert _is_on(hass, BEDROOM)
    assert not _is_on(hass, BATHROOM)


async def test_unavailable_routes_are_skipped(hass: HomeAssistant, fake_lights) -> None:
    """Routes whose origin or destination is unavailable never run."""
    entry = await _setup(hass, [BATH_ROUTE, TOILET_ROUTE])
    sequences = entry.runtime_data.sequences

    hass.states.async_set(TOILET, STATE_UNAVAILABLE)
    for _ in range(10):
        sequences._last_route_id = None
        assert sequences._choose_route().name == "Bathroom"

    hass.states.async_set(BEDROOM, STATE_UNAVAILABLE)
    assert sequences._choose_route() is None
    with pytest.raises(ServiceValidationError):
        sequences.async_run_now()


async def test_no_route_twice_in_a_row(hass: HomeAssistant, fake_lights) -> None:
    """Consecutive routes differ when there is more than one."""
    third = ("Kitchen", {CONF_ORIGIN: HALL, CONF_PATH: [], CONF_DESTINATION: LANDING})
    entry = await _setup(hass, [BATH_ROUTE, TOILET_ROUTE, third])
    sequences = entry.runtime_data.sequences

    previous = None
    for _ in range(30):
        route = sequences._choose_route()
        assert route.route_id != previous
        previous = sequences._last_route_id = route.route_id

    # With a single route, it simply repeats.
    sequences._routes = sequences.routes[:1]
    sequences._last_route_id = sequences.routes[0].route_id
    assert sequences._choose_route() is sequences.routes[0]


# ----------------------------------------------------------------------
# Locks
# ----------------------------------------------------------------------
async def test_locked_lights_are_counted_but_never_toggled(
    hass: HomeAssistant, fake_lights
) -> None:
    """The simulation counts a light locked by a route but never switches it."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    controller = entry.runtime_data.controller
    settings = engine.settings
    settings.min_front_lights = settings.min_rear_lights = 0
    controller.lock(["light.salon"])

    # The locked light is on and fills the maximum: nothing else goes on.
    settings.min_lights, settings.max_lights = 0, 1
    for _ in range(20):
        for entity_id in fake_lights:
            hass.states.async_set(entity_id, STATE_ON if entity_id == "light.salon" else STATE_OFF)
        await engine._async_step()
        assert [e for e in fake_lights if _is_on(hass, e)] == ["light.salon"]

    # The locked light is the only one off: it is never turned on.
    settings.min_lights, settings.max_lights = 4, 4
    for _ in range(20):
        for entity_id in fake_lights:
            hass.states.async_set(entity_id, STATE_OFF if entity_id == "light.salon" else STATE_ON)
        await engine._async_step()
        assert not _is_on(hass, "light.salon")

    controller.unlock(["light.salon"])
    await engine._async_step()
    assert _is_on(hass, "light.salon")


# ----------------------------------------------------------------------
# Window and scheduling
# ----------------------------------------------------------------------
async def test_sequence_window(
    hass: HomeAssistant, fake_lights, waits: list[float], freezer: FrozenDateTimeFactory
) -> None:
    """Routes run from the start time to sunrise, only with master and sequences on."""
    tz = dt_util.get_default_time_zone()
    freezer.move_to(dt_util.as_utc(datetime(2026, 10, 15, 23, 0, tzinfo=tz)))
    entry = await _setup(hass, [BATH_ROUTE])
    runtime = entry.runtime_data
    sequences = runtime.sequences
    # Keep the main simulation out of the way.
    runtime.settings.mode = "time"
    runtime.settings.start_time, runtime.settings.end_time = time(12), time(13)

    async def turn(entity_id: str, on: bool) -> None:
        await hass.services.async_call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
        )
        await hass.async_block_till_done()

    await turn("switch.luma_presence_delayed_start", False)
    await turn("switch.luma_presence_simulation", True)
    assert sequences.next_run is None  # sequences are off by default
    await turn("switch.luma_presence_sequences", True)
    first = dt_util.as_utc(datetime(2026, 10, 16, 0, 45, tzinfo=tz))
    assert sequences.next_run == first
    assert hass.states.get("sensor.luma_presence_next_sequence").state == first.isoformat()

    # Master off: nothing scheduled.
    await turn("switch.luma_presence_simulation", False)
    assert sequences.next_run is None
    await turn("switch.luma_presence_simulation", True)
    assert sequences.next_run == first

    # The first route runs at the start time; the next one 45-90 minutes later.
    events = async_capture_events(hass, EVENT_ACTION)
    freezer.move_to(first)
    async_fire_time_changed(hass, first)
    await hass.async_block_till_done()
    await until(hass, lambda: not sequences.running)
    assert ("on", BATHROOM) in _switches(events)
    assert first + timedelta(minutes=45) <= sequences.next_run <= first + timedelta(minutes=90)

    # The window ends at sunrise; then the next night is scheduled.
    sunrise = get_astral_event_next(hass, SUN_EVENT_SUNRISE, first)
    assert sequences._current_window(first) == (first, sunrise)
    freezer.move_to(sunrise)
    async_fire_time_changed(hass, sunrise)
    await hass.async_block_till_done()
    assert sequences.next_run == first + timedelta(days=1)


# ----------------------------------------------------------------------
# Cancellation
# ----------------------------------------------------------------------
async def test_master_off_cancels_route(
    hass: HomeAssistant, fake_lights, blocked: None, freezer: FrozenDateTimeFactory
) -> None:
    """Master off cancels the route; its lights follow the stop selector."""
    tz = dt_util.get_default_time_zone()
    freezer.move_to(dt_util.as_utc(datetime(2026, 10, 15, 12, 0, tzinfo=tz)))
    entry = await _setup(hass, [BATH_ROUTE])
    sequences = entry.runtime_data.sequences
    controller = entry.runtime_data.controller
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.luma_presence_simulation"}, blocking=True
    )

    sequences.async_run_now("Bathroom")
    await until(hass, lambda: _is_on(hass, BEDROOM))
    assert hass.states.get("binary_sensor.luma_presence_sequence_running").state == STATE_ON
    assert controller.owner_of(BEDROOM) == OWNER_SEQUENCE

    await hass.services.async_call(DOMAIN, "stop", {"lights_behavior": "off_now"}, blocking=True)
    await hass.async_block_till_done()
    assert not sequences.running
    assert not _is_on(hass, BEDROOM)
    await until(hass, lambda: not controller.is_locked(BEDROOM))
    assert hass.states.get("binary_sensor.luma_presence_sequence_running").state == STATE_OFF


async def test_master_off_keep_on(
    hass: HomeAssistant, fake_lights, blocked: None, freezer: FrozenDateTimeFactory
) -> None:
    """With "keep them on", the interrupted route's lights stay on."""
    tz = dt_util.get_default_time_zone()
    freezer.move_to(dt_util.as_utc(datetime(2026, 10, 15, 12, 0, tzinfo=tz)))
    entry = await _setup(hass, [BATH_ROUTE])
    sequences = entry.runtime_data.sequences
    await hass.services.async_call(DOMAIN, "start", {}, blocking=True)

    sequences.async_run_now()
    await until(hass, lambda: _is_on(hass, BEDROOM))
    await hass.services.async_call(DOMAIN, "stop", {"lights_behavior": "keep_on"}, blocking=True)
    await hass.async_block_till_done()
    assert not sequences.running
    assert _is_on(hass, BEDROOM)
    assert entry.runtime_data.controller.owner_of(BEDROOM) is None


async def test_sequences_off_cancels_route(
    hass: HomeAssistant, fake_lights, blocked: None
) -> None:
    """Turning sequences off cancels the route and switches its lights off."""
    entry = await _setup(hass, [BATH_ROUTE])
    sequences = entry.runtime_data.sequences
    for service in ("turn_on", "turn_off"):
        if service == "turn_off":
            sequences.async_run_now()
            await until(hass, lambda: _is_on(hass, BEDROOM))
        await hass.services.async_call(
            "switch", service, {"entity_id": "switch.luma_presence_sequences"}, blocking=True
        )
    assert not sequences.running
    await until(hass, lambda: sequences._off_task is not None)
    await sequences._off_task
    assert not _is_on(hass, BEDROOM)


# ----------------------------------------------------------------------
# Test button and service
# ----------------------------------------------------------------------
async def test_button_runs_with_master_off(
    hass: HomeAssistant, fake_lights, waits: list[float]
) -> None:
    """The test button runs a route with the master switch off and keeps learning open."""
    entry = await _setup(hass, [BATH_ROUTE], {CONF_LEARN_EXTRA: ROUTE_LIGHTS})
    runtime = entry.runtime_data
    assert not runtime.engine.enabled
    assert runtime.learning.is_open
    events = async_capture_events(hass, EVENT_ACTION)

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.luma_presence_test_sequence"}, blocking=True
    )
    task = runtime.sequences._task
    assert task is not None
    await task
    await hass.async_block_till_done()

    assert ("on", BATHROOM) in _switches(events)
    assert runtime.learning.is_open
    assert runtime.learning.reasons == set()
    assert not runtime.engine.enabled
    # The route's changes are LumaPresence's own, so learning ignores them.
    assert not [p for p in runtime.learning._pending if p[0] in ROUTE_LIGHTS]


async def test_run_sequence_service(
    hass: HomeAssistant, fake_lights, waits: list[float]
) -> None:
    """The service runs a route by name and rejects unknown names."""
    entry = await _setup(hass, [BATH_ROUTE, TOILET_ROUTE])
    sequences = entry.runtime_data.sequences
    events = async_capture_events(hass, EVENT_ACTION)

    await hass.services.async_call(DOMAIN, "run_sequence", {"route": "Toilet"}, blocking=True)
    await sequences._task
    await hass.async_block_till_done()
    assert ("on", TOILET) in _switches(events)
    assert ("on", BATHROOM) not in _switches(events)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "run_sequence", {"route": "Garage"}, blocking=True
        )
