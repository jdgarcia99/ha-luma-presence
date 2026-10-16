"""Tests for v0.2.0: facades, learning scope, options menu and migration."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    CONF_ALARM,
    CONF_FRONT_LIGHTS,
    CONF_HUMAN_ONLY,
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    CONF_MARGIN_AFTER,
    CONF_MARGIN_BEFORE,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    DOMAIN,
)
from custom_components.luma_presence.learning import parse_event
from custom_components.luma_presence.scope import (
    resolve_learning_entities,
    resolve_learning_scope,
)

from .conftest import FRONT, LIGHTS

REAR = ["light.hall"]
V1_OPTIONS: dict[str, Any] = {
    CONF_LIGHTS: LIGHTS,
    CONF_FRONT_LIGHTS: FRONT,
    CONF_PERSONS: ["person.mario"],
    CONF_ALARM: "alarm_control_panel.home_alarm",
    CONF_MARGIN_BEFORE: 2,
    CONF_MARGIN_AFTER: 2,
    CONF_HUMAN_ONLY: True,
}
OPTIONS: dict[str, Any] = {
    **V1_OPTIONS,
    CONF_REAR_LIGHTS: REAR,
    CONF_LEARN_FLOORS: [],
    CONF_LEARN_AREAS: [],
    CONF_LEARN_EXTRA: [],
    CONF_LEARN_EXCLUDE: [],
}


async def _setup(
    hass: HomeAssistant, options: dict[str, Any] | None = None, version: int = 2
) -> MockConfigEntry:
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LumaPresence",
        data={},
        options=options or OPTIONS,
        version=version,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def house(hass: HomeAssistant) -> dict[str, str]:
    """A ground floor with a living room, a kitchen without floor and a garden."""
    floors = fr.async_get(hass)
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    ground = floors.async_create("Planta baja")
    living = areas.async_create("Salon", floor_id=ground.floor_id)
    kitchen = areas.async_create("Cocina")
    garden = areas.async_create("Jardin")

    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)

    def light(unique_id: str, area_id: str | None = None, **kwargs: Any) -> str:
        # A device entity needs a name of its own to have an area of its own.
        entry = entities.async_get_or_create(
            "light", "test", unique_id, original_name=unique_id, **kwargs
        )
        if area_id:
            entities.async_update_entity(entry.entity_id, area_id=area_id)
        return entry.entity_id

    device = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("test", "lamp")}
    )
    devices.async_update_device(device.id, area_id=living.id)
    garden_device = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("test", "garden")}
    )
    devices.async_update_device(garden_device.id, area_id=garden.id)

    return {
        "floor": ground.floor_id,
        "living": living.id,
        "kitchen": kitchen.id,
        "garden_area": garden.id,
        # Area from its device.
        "lamp": light("lamp", device_id=device.id),
        # Own area overrides the device area: outdoor.
        "moved": light("moved", garden.id, device_id=device.id),
        # Own area, kitchen is selected as an area.
        "ceiling": light("ceiling", kitchen.id),
        # Device in the garden, entity moved indoors.
        "porch_inside": light("porch_inside", living.id, device_id=garden_device.id),
        "garden": light("garden", garden.id),
        "led": light("led", living.id, entity_category=EntityCategory.DIAGNOSTIC),
        "disabled": light(
            "disabled", living.id, disabled_by=er.RegistryEntryDisabler.USER
        ),
        "hidden": light("hidden", living.id, hidden_by=er.RegistryEntryHider.USER),
        "unwanted": light("unwanted", living.id),
        "no_area": light("no_area"),
        "relay": entities.async_get_or_create("switch", "test", "relay").entity_id,
    }


# ----------------------------------------------------------------------
# Learning scope
# ----------------------------------------------------------------------
async def test_resolve_learning_entities(
    hass: HomeAssistant, house: dict[str, str]
) -> None:
    """Floors expand to areas; filters, extras, excludes and simulation lights."""
    options = {
        CONF_LIGHTS: ["light.front_door"],
        CONF_LEARN_FLOORS: [house["floor"]],
        CONF_LEARN_AREAS: [house["kitchen"]],
        CONF_LEARN_EXTRA: [house["relay"], house["no_area"]],
        CONF_LEARN_EXCLUDE: [house["unwanted"]],
    }
    scope = resolve_learning_scope(hass, options)
    assert scope.entities == sorted(
        [
            house["lamp"],
            house["ceiling"],
            house["porch_inside"],
            house["relay"],
            house["no_area"],
            "light.front_door",
        ]
    )
    assert scope.auto_excluded == sorted([house["led"], house["disabled"], house["hidden"]])
    assert resolve_learning_entities(hass, options) == scope.entities


async def test_resolve_without_indoor_areas(
    hass: HomeAssistant, house: dict[str, str]
) -> None:
    """Without floors or areas only the extras and simulation lights are learned."""
    options = {
        CONF_LIGHTS: ["light.front_door", house["led"]],
        CONF_LEARN_EXTRA: [house["garden"]],
    }
    scope = resolve_learning_scope(hass, options)
    # Simulation lights are always learned, even if they would be filtered.
    assert scope.entities == sorted(["light.front_door", house["led"], house["garden"]])
    assert scope.auto_excluded == []


def test_parse_event_accepts_old_records() -> None:
    """Events stored before v0.2.0 have no area."""
    assert parse_event(["light.a", "on", 10]) == ("light.a", "on", 10, None)
    assert parse_event(["light.a", "off", 20, "salon"]) == ("light.a", "off", 20, "salon")


# ----------------------------------------------------------------------
# Engine facades
# ----------------------------------------------------------------------
def _set(hass: HomeAssistant, on: list[str]) -> None:
    for entity_id in LIGHTS:
        hass.states.async_set(entity_id, STATE_ON if entity_id in on else STATE_OFF)


async def test_facade_minimums_are_honoured(hass: HomeAssistant, fake_lights) -> None:
    """Front first, then rear, are turned on before anything else."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    settings = engine.settings
    settings.min_lights, settings.max_lights = 0, 4
    settings.min_front_lights, settings.min_rear_lights = 1, 1

    _set(hass, [])
    await engine._async_step()
    assert hass.states.get(FRONT[0]).state == STATE_ON
    await engine._async_step()
    assert hass.states.get(REAR[0]).state == STATE_ON


async def test_facade_never_dropped_below_minimum(
    hass: HomeAssistant, fake_lights
) -> None:
    """Facade lights at their minimum are only switched off above max_lights."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    settings = engine.settings
    settings.min_lights, settings.max_lights = 0, 4
    settings.min_front_lights, settings.min_rear_lights = 1, 1

    for _ in range(30):
        _set(hass, LIGHTS)
        await engine._async_step()
        assert hass.states.get(FRONT[0]).state == STATE_ON
        assert hass.states.get(REAR[0]).state == STATE_ON

    # Above max_lights with nothing else to switch off: a facade light goes.
    settings.max_lights = 1
    _set(hass, [FRONT[0], REAR[0]])
    await engine._async_step()
    on = [e for e in (FRONT[0], REAR[0]) if hass.states.get(e).state == STATE_ON]
    assert len(on) == 1


# ----------------------------------------------------------------------
# Options flow
# ----------------------------------------------------------------------
async def test_options_menu(hass: HomeAssistant, fake_lights) -> None:
    """The menu edits a working copy; Save validates and writes it."""
    entry = await _setup(hass)
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "save" in result["menu_options"]

    # Facades: overlap is rejected.
    result = await options.async_configure(result["flow_id"], {"next_step_id": "facades"})
    assert result["step_id"] == "facades"
    result = await options.async_configure(
        result["flow_id"], {CONF_FRONT_LIGHTS: FRONT, CONF_REAR_LIGHTS: FRONT}
    )
    assert result["errors"] == {"base": "facade_overlap"}
    result = await options.async_configure(
        result["flow_id"], {CONF_FRONT_LIGHTS: FRONT, CONF_REAR_LIGHTS: REAR}
    )
    assert result["type"] is FlowResultType.MENU

    # Learning goes through the preview and back to the menu.
    result = await options.async_configure(result["flow_id"], {"next_step_id": "learning"})
    result = await options.async_configure(
        result["flow_id"], {CONF_LEARN_EXTRA: ["switch.boiler"], CONF_HUMAN_ONLY: False}
    )
    assert result["step_id"] == "learning_preview"
    assert result["description_placeholders"]["count"] == str(len(LIGHTS) + 1)
    assert "`switch.boiler`" in result["description_placeholders"]["entities"]
    result = await options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU

    # Dropping a rear light from the simulation lights is caught on Save.
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "simulation_lights"}
    )
    result = await options.async_configure(
        result["flow_id"], {CONF_LIGHTS: ["light.salon", "light.bedroom"]}
    )
    assert result["type"] is FlowResultType.MENU
    assert entry.options[CONF_LIGHTS] == LIGHTS  # nothing written yet
    result = await options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["step_id"] == "facades"
    assert result["errors"] == {CONF_REAR_LIGHTS: "facade_not_subset"}

    result = await options.async_configure(result["flow_id"], {CONF_FRONT_LIGHTS: FRONT})
    result = await options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_LIGHTS] == ["light.salon", "light.bedroom"]
    assert entry.options[CONF_FRONT_LIGHTS] == FRONT
    assert entry.options[CONF_REAR_LIGHTS] == []
    assert entry.options[CONF_LEARN_EXTRA] == ["switch.boiler"]
    assert entry.options[CONF_HUMAN_ONLY] is False
    assert entry.options[CONF_ALARM] == OPTIONS[CONF_ALARM]


async def test_options_require_lights(hass: HomeAssistant, fake_lights) -> None:
    """Simulation lights cannot be emptied."""
    entry = await _setup(hass)
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "simulation_lights"}
    )
    result = await options.async_configure(result["flow_id"], {})
    assert result["errors"] == {CONF_LIGHTS: "no_lights"}


# ----------------------------------------------------------------------
# Migration
# ----------------------------------------------------------------------
async def test_migration_from_version_1(hass: HomeAssistant, fake_lights) -> None:
    """A v0.1 entry gets the new options and keeps the old ones."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Planta baja")
    upper = floors.async_create("Planta piso")

    entry = await _setup(hass, V1_OPTIONS, version=1)
    assert entry.version == 3  # then migrated on to the v0.7.0 options
    for key, value in V1_OPTIONS.items():
        assert entry.options[key] == value
    assert entry.options[CONF_REAR_LIGHTS] == []
    assert sorted(entry.options[CONF_LEARN_FLOORS]) == sorted(
        [ground.floor_id, upper.floor_id]
    )
    assert entry.options[CONF_LEARN_AREAS] == []
    assert entry.options[CONF_LEARN_EXTRA] == []
    assert entry.options[CONF_LEARN_EXCLUDE] == []


async def test_old_learning_data_is_kept(hass: HomeAssistant, fake_lights, hass_storage) -> None:
    """Days stored by v0.1 stay readable after the upgrade."""
    hass_storage["luma_presence.old_entry.learning"] = {
        "version": 1,
        "key": "luma_presence.old_entry.learning",
        "data": {
            "days": {
                "2026-10-01": {
                    "exposure": [[0, 86400]],
                    "events": [["light.hall", "on", 3600]],
                }
            },
            "gate": {},
        },
    }
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="old_entry", options=V1_OPTIONS, version=1
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    learning = entry.runtime_data.learning
    assert learning.stats()["events"] == 1
    assert learning.stats()["valid_days"] == 1
    event = learning._days["2026-10-01"]["events"][0]
    assert parse_event(event) == ("light.hall", "on", 3600, None)


# ----------------------------------------------------------------------
# Learning gate and sensors
# ----------------------------------------------------------------------
async def test_learning_follows_registry_changes(
    hass: HomeAssistant,
    fake_lights,
    house: dict[str, str],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new indoor light is learned after a registry change, with its area."""
    t0 = dt_util.as_utc(datetime(2026, 10, 15, 8, 0, tzinfo=dt_util.get_default_time_zone()))
    freezer.move_to(t0)
    entry = await _setup(hass, {**OPTIONS, CONF_LEARN_FLOORS: [house["floor"]]})
    learning = entry.runtime_data.learning
    assert house["lamp"] in learning.entities
    assert house["led"] in learning.auto_excluded

    new = er.async_get(hass).async_get_or_create("light", "test", "new").entity_id
    er.async_get(hass).async_update_entity(new, area_id=house["living"])
    await hass.async_block_till_done()
    assert new not in learning.entities  # debounced

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert new in learning.entities
    assert hass.states.get("sensor.luma_presence_learning_lights").state == str(
        len(learning.entities)
    )

    hass.states.async_set(new, STATE_OFF)
    hass.states.async_set(new, STATE_ON, context=Context(user_id="u"))
    await hass.async_block_till_done()
    assert learning._pending[-1][0] == new
    assert learning._pending[-1][3] == house["living"]

    freezer.move_to(t0 + timedelta(hours=3))
    learning._handle_tick(dt_util.utcnow())
    assert learning._days["2026-10-15"]["events"] == [
        [new, STATE_ON, 8 * 3600, house["living"]]
    ]


async def test_light_sensors_and_device(hass: HomeAssistant, fake_lights) -> None:
    """Diagnostic sensors list the lights; the device shows the manifest version."""
    entry = await _setup(hass)

    simulation = hass.states.get("sensor.luma_presence_simulation_lights")
    assert simulation.state == str(len(LIGHTS))
    assert simulation.attributes["front"] == FRONT
    assert simulation.attributes["rear"] == REAR
    assert simulation.attributes["interior"] == ["light.salon", "switch.oficina"]

    learning = hass.states.get("sensor.luma_presence_learning_lights")
    assert learning.state == str(len(LIGHTS))
    assert learning.attributes["entities"] == sorted(LIGHTS)
    assert learning.attributes["auto_excluded"] == {}

    registry = er.async_get(hass)
    for entity_id in (
        "sensor.luma_presence_simulation_lights",
        "sensor.luma_presence_learning_lights",
    ):
        assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get("number.luma_presence_min_rear_lights").state == "0"

    devices = dr.async_get(hass)
    identifier = (DOMAIN, entry.entry_id)
    # async_get_device is deprecated since Home Assistant 2026.10.
    if hasattr(devices, "async_get_device_by_identifier"):
        device = devices.async_get_device_by_identifier(identifier, entry.entry_id)
    else:
        device = devices.async_get_device(identifiers={identifier})
    manifest = Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json"
    assert device.sw_version == json.loads(manifest.read_text())["version"]
