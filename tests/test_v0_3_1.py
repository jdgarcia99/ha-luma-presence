"""Tests for v0.3.1: indicator LEDs, unavailable-light issues and learning tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    CONF_AUTO_EXCLUDE_INDICATORS,
    CONF_DESTINATION,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    CONF_ORIGIN,
    CONF_PATH,
    CONF_PERSONS,
    CONF_UNAVAILABLE_ALERT_MINUTES,
    DOMAIN,
    EVENT_LIGHT_AVAILABLE,
    EVENT_LIGHT_UNAVAILABLE,
    REASON_ALARM,
    REASON_AWAY,
    REASON_MARGIN,
    REASON_SIMULATION,
    REASON_SIMULATION_TEST,
    SUBENTRY_ROUTE,
)
from custom_components.luma_presence.scope import resolve_learning_scope

from .test_v0_2_0 import OPTIONS

TZ_DATE = (2026, 10, 15)
BEDROOM = "light.dormitorio"
BATHROOM = "light.bano"
BEDROOM_ROUTE = ("Lucía", {CONF_ORIGIN: "light.bedroom", CONF_PATH: [], CONF_DESTINATION: BATHROOM})


async def _setup(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    routes: list[tuple[str, dict[str, Any]]] | None = None,
) -> MockConfigEntry:
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    for entity_id in (BEDROOM, BATHROOM):
        if hass.states.get(entity_id) is None:
            hass.states.async_set(entity_id, STATE_OFF)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LumaPresence",
        options={**OPTIONS, **(options or {})},
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


def _local(hour: int, minute: int = 0) -> datetime:
    return dt_util.as_utc(
        datetime(*TZ_DATE, hour, minute, tzinfo=dt_util.get_default_time_zone())
    )


def _issue(hass: HomeAssistant, entry: MockConfigEntry, entity_id: str):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"light_unavailable_{entry.entry_id}_{entity_id}"
    )


# ----------------------------------------------------------------------
# Indicator LEDs
# ----------------------------------------------------------------------
@pytest.fixture
async def plugs(hass: HomeAssistant) -> dict[str, str]:
    """A smart plug with a relay and a status LED, and a lamp without switches."""
    floor = fr.async_get(hass).async_create("Planta baja")
    living = ar.async_get(hass).async_create("Salon", floor_id=floor.floor_id)
    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    def device(name: str) -> str:
        entry = devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("test", name)}
        )
        devices.async_update_device(entry.id, area_id=living.id)
        return entry.id

    plug, lamp = device("fridge_plug"), device("lamp")
    return {
        "floor": floor.floor_id,
        "relay": entities.async_get_or_create(
            "switch", "test", "fridge_relay", device_id=plug
        ).entity_id,
        "led": entities.async_get_or_create(
            "light", "test", "fridge_led", device_id=plug
        ).entity_id,
        "lamp": entities.async_get_or_create(
            "light", "test", "lamp", device_id=lamp
        ).entity_id,
    }


async def test_indicator_leds_are_excluded(
    hass: HomeAssistant, plugs: dict[str, str]
) -> None:
    """A light on a device with a switch is an indicator LED, unless listed as extra."""
    options: dict[str, Any] = {CONF_LIGHTS: [], CONF_LEARN_FLOORS: [plugs["floor"]]}

    # Default (option missing): on.
    scope = resolve_learning_scope(hass, options)
    assert scope.entities == [plugs["lamp"]]
    assert scope.reasons == {plugs["led"]: "indicator_led"}

    # Listed as extra: learned anyway.
    scope = resolve_learning_scope(hass, {**options, CONF_LEARN_EXTRA: [plugs["led"]]})
    assert scope.entities == sorted([plugs["lamp"], plugs["led"]])
    assert scope.reasons == {}

    # Option off: kept.
    scope = resolve_learning_scope(hass, {**options, CONF_AUTO_EXCLUDE_INDICATORS: False})
    assert scope.entities == sorted([plugs["lamp"], plugs["led"]])


async def test_exclusion_reasons_in_sensor_and_preview(
    hass: HomeAssistant, fake_lights, plugs: dict[str, str]
) -> None:
    """The sensor says why each light was left out; the preview lists indicator LEDs."""
    hidden = er.async_get(hass).async_get_or_create(
        "light", "test", "hidden", hidden_by=er.RegistryEntryHider.USER
    )
    er.async_get(hass).async_update_entity(
        hidden.entity_id, area_id=ar.async_get(hass).async_get_area_by_name("Salon").id
    )
    entry = await _setup(hass, {CONF_LEARN_FLOORS: [plugs["floor"]]})

    attributes = hass.states.get("sensor.luma_presence_learning_lights").attributes
    assert attributes["auto_excluded"] == {
        plugs["led"]: "indicator_led",
        hidden.entity_id: "hidden",
    }

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learning_preview"}
    )
    placeholders = result["description_placeholders"]
    assert f"`{plugs['led']}`" in placeholders["indicators"]
    assert hidden.entity_id not in placeholders["indicators"]
    assert f"`{plugs['lamp']}`" in placeholders["entities"]


# ----------------------------------------------------------------------
# Unavailable lights
# ----------------------------------------------------------------------
async def test_unavailable_light_issue(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """An issue and an event after the threshold; both cleared on recovery."""
    freezer.move_to(_local(10))
    entry = await _setup(hass, routes=[BEDROOM_ROUTE])
    unavailable = async_capture_events(hass, EVENT_LIGHT_UNAVAILABLE)
    available = async_capture_events(hass, EVENT_LIGHT_AVAILABLE)

    hass.states.async_set("light.bedroom", STATE_UNAVAILABLE)
    hass.states.async_set(BATHROOM, STATE_UNKNOWN)
    await hass.async_block_till_done()

    freezer.move_to(_local(10, 59))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "light.bedroom") is None
    assert not unavailable

    freezer.move_to(_local(11, 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    issue = _issue(hass, entry, "light.bedroom")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert issue.translation_key == "light_unavailable"
    assert issue.translation_placeholders == {
        "entity": "light.bedroom",
        "entity_id": "light.bedroom",
        "since": "2026-10-15 10:00",
        "roles": "simulation light, route Lucía",
    }
    # Route lights are watched too.
    route_issue = _issue(hass, entry, BATHROOM)
    assert route_issue.translation_placeholders["roles"] == "route Lucía"
    assert {e.data["entity_id"] for e in unavailable} == {"light.bedroom", BATHROOM}
    event = next(e for e in unavailable if e.data["entity_id"] == "light.bedroom")
    assert event.data["entry_id"] == entry.entry_id
    assert event.data["roles"] == "simulation light, route Lucía"
    assert event.data["since"].startswith("2026-10-15T10:00")

    hass.states.async_set("light.bedroom", STATE_OFF)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "light.bedroom") is None
    assert [e.data["entity_id"] for e in available] == ["light.bedroom"]
    assert _issue(hass, entry, BATHROOM) is not None


async def test_short_outage_raises_nothing(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A light back before the threshold raises no issue and no event."""
    freezer.move_to(_local(10))
    entry = await _setup(hass)
    events = async_capture_events(hass, EVENT_LIGHT_UNAVAILABLE)
    hass.states.async_set("light.salon", STATE_UNAVAILABLE)
    freezer.move_to(_local(10, 30))
    hass.states.async_set("light.salon", STATE_ON)
    freezer.move_to(_local(12))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "light.salon") is None
    assert not events


async def test_threshold_zero_disables(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """With 0 minutes no issue is ever raised."""
    freezer.move_to(_local(10))
    entry = await _setup(hass, {CONF_UNAVAILABLE_ALERT_MINUTES: 0})
    hass.states.async_set("light.salon", STATE_UNAVAILABLE)
    freezer.move_to(_local(20))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "light.salon") is None


async def test_clock_starts_at_startup(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A light unavailable before a restart waits the full threshold after it."""
    freezer.move_to(_local(6))
    hass.states.async_set("light.salon", STATE_UNAVAILABLE)
    freezer.move_to(_local(10))
    entry = await _setup(hass, {CONF_UNAVAILABLE_ALERT_MINUTES: 30})
    assert _issue(hass, entry, "light.salon") is None

    freezer.move_to(_local(10, 29))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "light.salon") is None

    freezer.move_to(_local(10, 31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    issue = _issue(hass, entry, "light.salon")
    assert issue.translation_placeholders["since"] == "2026-10-15 10:00"


# ----------------------------------------------------------------------
# Learning: tests with people at home
# ----------------------------------------------------------------------
async def _master(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": "switch.luma_presence_simulation"},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_simulation_test_with_people_home(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A test keeps what was observed and leaves no return margin."""
    freezer.move_to(_local(8))
    entry = await _setup(hass)
    learning = entry.runtime_data.learning
    hass.states.async_set("light.hall", STATE_ON, context=Context(user_id="u"))
    await hass.async_block_till_done()

    freezer.move_to(_local(9))
    await _master(hass, True)
    assert learning.reasons == {REASON_SIMULATION_TEST}
    assert not learning.is_open
    # The pending event (younger than margin_before) was committed, not dropped.
    assert learning._days["2026-10-15"]["events"] == [["light.hall", "on", 8 * 3600, None]]
    attributes = hass.states.get("binary_sensor.luma_presence_learning_active").attributes
    assert attributes["paused_reasons"] == [REASON_SIMULATION_TEST]

    freezer.move_to(_local(9, 30))
    await _master(hass, False)
    assert learning.is_open
    assert learning.reasons == set()
    assert learning.blocked_until is None


@pytest.mark.parametrize(
    ("entity_id", "state", "reason"),
    [
        ("person.mario", "not_home", REASON_AWAY),
        ("alarm_control_panel.home_alarm", "armed_away", REASON_ALARM),
    ],
)
async def test_test_becomes_absence(
    hass: HomeAssistant,
    fake_lights,
    freezer: FrozenDateTimeFactory,
    entity_id: str,
    state: str,
    reason: str,
) -> None:
    """Everybody leaving or arming away during a test starts a real absence."""
    freezer.move_to(_local(8))
    entry = await _setup(hass)
    learning = entry.runtime_data.learning
    await _master(hass, True)
    assert learning.reasons == {REASON_SIMULATION_TEST}

    freezer.move_to(_local(9))
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()
    assert {REASON_SIMULATION, reason} <= learning.reasons
    assert REASON_SIMULATION_TEST not in learning.reasons

    # Coming back home with the simulation still on is still the absence.
    freezer.move_to(_local(12))
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    await hass.async_block_till_done()
    assert learning.reasons == {REASON_SIMULATION}

    await _master(hass, False)
    assert REASON_MARGIN in learning.reasons
    assert learning.blocked_until == _local(14)


async def test_no_persons_keeps_absence_rules(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Without people configured the simulation is always an absence."""
    freezer.move_to(_local(8))
    entry = await _setup(hass, {CONF_PERSONS: []})
    learning = entry.runtime_data.learning
    await _master(hass, True)
    assert learning.reasons == {REASON_SIMULATION}

    freezer.move_to(_local(9))
    await _master(hass, False)
    assert REASON_MARGIN in learning.reasons
    assert learning.blocked_until == _local(11)
