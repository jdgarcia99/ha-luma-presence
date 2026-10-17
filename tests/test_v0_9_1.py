"""Tests for v0.9.1: replacing devices without pain."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.components.repairs import models as repairs_models
from homeassistant.components.repairs.const import DOMAIN as REPAIRS_DOMAIN
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.luma_presence.const import (
    CONF_ALARM,
    CONF_ALARM_CONTROL,
    CONF_DARKNESS_SENSOR,
    CONF_DESTINATION,
    CONF_EVENT_TYPES,
    CONF_FACADE,
    CONF_FRONT_LIGHTS,
    CONF_LEARN_EXTRA,
    CONF_LIGHTS,
    CONF_ORIGIN,
    CONF_PATH,
    CONF_REAR_LIGHTS,
    CONF_TRIGGER_ENTITY,
    DOMAIN,
    EVENT_ENTITY_RENAMED,
    FACADE_REAR,
    SUBENTRY_DETERRENCE,
    SUBENTRY_ROUTE,
)
from custom_components.luma_presence.learning import learning_store

from .conftest import FRONT, LIGHTS
from .test_v0_2_0 import OPTIONS

OLD = "switch.kitchen_light"
NEW = "switch.kitchen_light_new"
BATHROOM = "light.bano"
MOTION = "binary_sensor.patio_motion"
ROUTE = {CONF_ORIGIN: OLD, CONF_PATH: ["light.hall"], CONF_DESTINATION: BATHROOM}
TRIGGER = {
    CONF_TRIGGER_ENTITY: MOTION,
    CONF_FACADE: FACADE_REAR,
    CONF_PATH: [OLD, "light.salon"],
    CONF_EVENT_TYPES: [],
}
# The kitchen light of the real case: a simulation light and a rear light.
KITCHEN_OPTIONS = {
    **OPTIONS,
    CONF_LIGHTS: [*LIGHTS, OLD],
    CONF_REAR_LIGHTS: [OLD],
}


def _days(entity_id: str) -> dict[str, Any]:
    return {
        "2026-10-01": {
            "exposure": [[0, 86400]],
            "events": [
                [entity_id, "on", 72000, "cocina"],
                ["light.salon", "on", 73000, "salon"],
                [entity_id, "off", 75000, "cocina"],
            ],
        }
    }


async def _setup(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    options: dict[str, Any] | None = None,
    routes: list[dict[str, Any]] | None = None,
    triggers: list[dict[str, Any]] | None = None,
    learned: str | None = OLD,
) -> MockConfigEntry:
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    hass.states.async_set(BATHROOM, STATE_OFF)
    hass.states.async_set(MOTION, STATE_OFF, {"device_class": "motion"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LumaPresence",
        data={},
        options=options or KITCHEN_OPTIONS,
        version=3,
        subentries_data=[
            *(
                ConfigSubentryData(
                    data=data, subentry_type=SUBENTRY_ROUTE, title="Bath", unique_id=None
                )
                for data in routes or []
            ),
            *(
                ConfigSubentryData(
                    data=data,
                    subentry_type=SUBENTRY_DETERRENCE,
                    title="Patio",
                    unique_id=None,
                )
                for data in triggers or []
            ),
        ],
    )
    entry.add_to_hass(hass)
    if learned:
        hass_storage[f"{DOMAIN}.{entry.entry_id}.learning"] = {
            "version": 1,
            "minor_version": 1,
            "key": f"{DOMAIN}.{entry.entry_id}.learning",
            "data": {"days": _days(learned), "gate": {}},
        }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _subentry(entry: MockConfigEntry, subentry_type: str) -> dict[str, Any] | None:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == subentry_type:
            return dict(subentry.data)
    return None


def _events_of(entry: MockConfigEntry, entity_id: str) -> int:
    return sum(
        1
        for day in entry.runtime_data.learning.days.values()
        for record in day["events"]
        if record[0] == entity_id
    )


def _issue_id(entry: MockConfigEntry, entity_id: str) -> str:
    return f"missing_entity_{entry.entry_id}_{entity_id}"


async def _repair(hass: HomeAssistant, issue_id: str) -> tuple[Any, dict[str, Any]]:
    """Start the fix flow of an issue."""
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    manager = hass.data[REPAIRS_DOMAIN]["flow_manager"]
    # Recent Home Assistant versions take the issue in the context; older
    # ones (2025.3 to 2026.2 at least) have no RepairsFlowContext and read it
    # from the flow data.
    context_type = getattr(repairs_models, "RepairsFlowContext", None)
    if "issue_id" in getattr(context_type, "__annotations__", {}):
        result = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    else:
        result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    return manager, result


# ----------------------------------------------------------------------
# Options flow
# ----------------------------------------------------------------------
async def test_lights_removed_from_facades_with_note(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """The real case: replacing the kitchen light in Simulation lights saves."""
    entry = await _setup(hass, hass_storage)
    hass.states.async_set(NEW, STATE_OFF)
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    assert result["description_placeholders"]["notice"] == ""
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "simulation_lights"}
    )
    result = await options.async_configure(result["flow_id"], {CONF_LIGHTS: [*LIGHTS, NEW]})
    assert result["type"] is FlowResultType.MENU
    notice = result["description_placeholders"]["notice"]
    assert notice.startswith("Removed from facades")
    assert OLD in notice

    result = await options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_LIGHTS] == [*LIGHTS, NEW]
    assert entry.options[CONF_REAR_LIGHTS] == []
    assert entry.options[CONF_FRONT_LIGHTS] == FRONT


async def test_save_never_jumps_to_another_step(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """A Save that still fails shows the error and the section on the menu."""
    # Stored options that break a rule (a light on both facades).
    entry = await _setup(
        hass,
        hass_storage,
        options={**OPTIONS, CONF_FRONT_LIGHTS: FRONT, CONF_REAR_LIGHTS: FRONT},
    )
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    notice = result["description_placeholders"]["notice"]
    assert "**Facades**" in notice
    assert "both a front and a rear light" in notice
    # The notice is shown once.
    result = await options.async_configure(result["flow_id"], {"next_step_id": "dream"})
    result = await options.async_configure(result["flow_id"], {})
    assert result["description_placeholders"]["notice"] == ""


async def test_facades_mark_missing_entities(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """Facades list every simulation light; missing ones are marked."""
    entry = await _setup(hass, hass_storage)  # OLD has no state
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "facades"})
    assert result["step_id"] == "facades"
    selector = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key == CONF_REAR_LIGHTS
    )
    labels = {option["value"]: option["label"] for option in selector.config["options"]}
    assert labels[OLD] == f"{OLD} (missing)"
    assert labels["light.salon"] == "salon (light.salon)"
    # Unticking it works.
    result = await options.async_configure(
        result["flow_id"], {CONF_FRONT_LIGHTS: FRONT, CONF_REAR_LIGHTS: []}
    )
    assert result["type"] is FlowResultType.MENU


# ----------------------------------------------------------------------
# Missing entities
# ----------------------------------------------------------------------
async def test_missing_check_covers_routes_and_triggers(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """One fixable issue per missing entity, wherever it is used."""
    entry = await _setup(
        hass,
        hass_storage,
        options={**OPTIONS, CONF_DARKNESS_SENSOR: "sensor.gone_lux"},
        routes=[{**ROUTE, CONF_ORIGIN: "light.salon", CONF_PATH: ["light.gone_path"]}],
        triggers=[{**TRIGGER, CONF_TRIGGER_ENTITY: "binary_sensor.gone", CONF_PATH: []}],
        learned=None,
    )
    registry = ir.async_get(hass)
    for entity_id in ("sensor.gone_lux", "light.gone_path", "binary_sensor.gone"):
        issue = registry.async_get_issue(DOMAIN, _issue_id(entry, entity_id))
        assert issue is not None, entity_id
        assert issue.is_fixable
        assert issue.translation_key == "missing_entity"
        assert issue.data == {"entry_id": entry.entry_id, "entity_id": entity_id}
    assert registry.async_get_issue(DOMAIN, _issue_id(entry, "light.salon")) is None
    assert registry.async_get_issue(DOMAIN, f"missing_entities_{entry.entry_id}") is None

    # The entity comes back: its issue goes away on the next check.
    hass.states.async_set("light.gone_path", STATE_OFF)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, _issue_id(entry, "light.gone_path")) is None
    assert registry.async_get_issue(DOMAIN, _issue_id(entry, "binary_sensor.gone"))


# ----------------------------------------------------------------------
# Repair flow
# ----------------------------------------------------------------------
async def test_repair_replace(hass: HomeAssistant, fake_lights, hass_storage) -> None:
    """Replace updates the options, every subentry and the learned data."""
    entry = await _setup(hass, hass_storage, routes=[ROUTE], triggers=[TRIGGER])
    assert _events_of(entry, OLD) == 2
    issue_id = _issue_id(entry, OLD)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    manager, result = await _repair(hass, issue_id)
    assert result["type"] is FlowResultType.MENU
    usages = result["description_placeholders"]["usages"]
    for text in (
        "Simulation light",
        "Rear facade light",
        "Origin of the route “Bath”",
        "Path of the deterrence trigger “Patio”",
        "Learned data: 2 events",
    ):
        assert text in usages

    result = await manager.async_configure(result["flow_id"], {"next_step_id": "replace"})
    assert result["step_id"] == "replace"
    # Wrong family, or an entity that does not exist either.
    result = await manager.async_configure(result["flow_id"], {"replacement": MOTION})
    assert result["errors"] == {"replacement": "replacement_not_allowed"}
    result = await manager.async_configure(result["flow_id"], {"replacement": "light.nope"})
    assert result["errors"] == {"replacement": "replacement_missing"}
    # The destination of the same route: the route would be invalid.
    result = await manager.async_configure(result["flow_id"], {"replacement": BATHROOM})
    assert result["errors"] == {"base": "replacement_conflict"}

    hass.states.async_set(NEW, STATE_OFF)
    result = await manager.async_configure(result["flow_id"], {"replacement": NEW})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_LIGHTS] == [*LIGHTS, NEW]
    assert entry.options[CONF_REAR_LIGHTS] == [NEW]
    assert _subentry(entry, SUBENTRY_ROUTE)[CONF_ORIGIN] == NEW
    assert _subentry(entry, SUBENTRY_DETERRENCE)[CONF_PATH] == [NEW, "light.salon"]
    # Reloaded, with the history under the new entity.
    assert entry.runtime_data.config[CONF_LIGHTS] == [*LIGHTS, NEW]
    assert _events_of(entry, NEW) == 2
    assert _events_of(entry, OLD) == 0
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_repair_remove_everywhere(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """Remove drops it from the options, routes and triggers; learned data stays."""
    entry = await _setup(
        hass,
        hass_storage,
        options={**KITCHEN_OPTIONS, CONF_LEARN_EXTRA: [OLD]},
        routes=[ROUTE],
        triggers=[TRIGGER],
    )
    issue_id = _issue_id(entry, OLD)
    manager, result = await _repair(hass, issue_id)
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "remove"})
    assert result["step_id"] == "remove"
    # Before confirming: the route goes (it is its origin), the trigger stays
    # (it is only on its path).
    assert result["description_placeholders"]["consequences"] == (
        "- The night route “Bath” (it is its origin or destination) is deleted."
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_LIGHTS] == LIGHTS
    assert entry.options[CONF_REAR_LIGHTS] == []
    assert entry.options[CONF_LEARN_EXTRA] == []
    assert _subentry(entry, SUBENTRY_ROUTE) is None  # it was the origin
    assert _subentry(entry, SUBENTRY_DETERRENCE)[CONF_PATH] == ["light.salon"]
    assert _events_of(entry, OLD) == 2
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_repair_remove_lists_destructive_consequences(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """The remove form names every route and trigger it deletes, before confirming."""
    gone = "binary_sensor.gone"
    entry = await _setup(
        hass,
        hass_storage,
        options=OPTIONS,
        triggers=[
            {**TRIGGER, CONF_TRIGGER_ENTITY: gone, CONF_PATH: []},
            {**TRIGGER, CONF_PATH: []},  # another sensor: kept
        ],
        learned=None,
    )
    manager, result = await _repair(hass, _issue_id(entry, gone))
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "remove"})
    assert result["step_id"] == "remove"
    assert result["description_placeholders"]["consequences"] == (
        "- The deterrence trigger “Patio” (it fires it) is deleted."
    )
    # Nothing is deleted until it is confirmed.
    assert len(entry.subentries) == 2

    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert [s.data[CONF_TRIGGER_ENTITY] for s in entry.subentries.values()] == [MOTION]


async def test_repair_remove_alarm_and_last_light(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """The only simulation light cannot be removed; the alarm panel can."""
    entry = await _setup(
        hass,
        hass_storage,
        options={
            **OPTIONS,
            CONF_LIGHTS: [OLD],
            CONF_FRONT_LIGHTS: [],
            CONF_REAR_LIGHTS: [],
            CONF_ALARM: "alarm_control_panel.gone",
            CONF_ALARM_CONTROL: True,
        },
        learned=None,
    )
    manager, result = await _repair(hass, _issue_id(entry, OLD))
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "remove"})
    assert result["description_placeholders"]["consequences"] == (
        "- Nothing else: no route or deterrence trigger is deleted."
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "remove_last_light"}

    manager, result = await _repair(hass, _issue_id(entry, "alarm_control_panel.gone"))
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "remove"})
    assert result["description_placeholders"]["consequences"] == (
        "- Alarm control is turned off (it is the alarm panel)."
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert CONF_ALARM not in entry.options
    assert entry.options[CONF_ALARM_CONTROL] is False


async def test_repair_aborts_when_entity_is_back(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """The entity comes back while the fix flow is open."""
    entry = await _setup(hass, hass_storage)
    issue_id = _issue_id(entry, OLD)
    manager, result = await _repair(hass, issue_id)
    hass.states.async_set(OLD, STATE_OFF)
    await hass.async_block_till_done()
    # Coming back clears the issue by itself.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "replace"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entity_back"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# ----------------------------------------------------------------------
# Entity ID renames
# ----------------------------------------------------------------------
async def test_registry_rename_is_followed(
    hass: HomeAssistant, fake_lights, hass_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming an entity ID updates options, subentries and learned data."""
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", "demo_relay", "kitchen", suggested_object_id="kitchen_light")
    hass.states.async_set(OLD, STATE_OFF)
    entry = await _setup(hass, hass_storage, routes=[ROUTE], triggers=[TRIGGER])
    events = async_capture_events(hass, EVENT_ENTITY_RENAMED)
    reloads: list[str] = []
    reload = hass.config_entries.async_reload

    async def _counting_reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return await reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _counting_reload)

    registry.async_update_entity(OLD, new_entity_id=NEW)
    await hass.async_block_till_done()

    # Two subentries and the options changed: one reload.
    assert reloads == [entry.entry_id]

    assert entry.options[CONF_LIGHTS] == [*LIGHTS, NEW]
    assert entry.options[CONF_REAR_LIGHTS] == [NEW]
    assert _subentry(entry, SUBENTRY_ROUTE)[CONF_ORIGIN] == NEW
    assert _subentry(entry, SUBENTRY_DETERRENCE)[CONF_PATH] == [NEW, "light.salon"]
    assert entry.runtime_data.config[CONF_REAR_LIGHTS] == [NEW]  # reloaded
    assert _events_of(entry, NEW) == 2
    assert _events_of(entry, OLD) == 0
    assert [event.data for event in events] == [
        {"entry_id": entry.entry_id, "old_entity_id": OLD, "new_entity_id": NEW}
    ]


async def test_registry_rename_learning_only(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """An entity only in the learned data is renamed there, without a reload."""
    registry = er.async_get(hass)
    registry.async_get_or_create("light", "hue", "lamp", suggested_object_id="lampara")
    entry = await _setup(hass, hass_storage, options=OPTIONS, learned="light.lampara")
    runtime = entry.runtime_data
    events = async_capture_events(hass, EVENT_ENTITY_RENAMED)

    registry.async_update_entity("light.lampara", new_entity_id="light.lampara_salon")
    await hass.async_block_till_done()

    assert entry.runtime_data is runtime  # not reloaded
    assert _events_of(entry, "light.lampara_salon") == 2
    assert len(events) == 1
    # Saved for the next start.
    await runtime.learning.async_shutdown()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.learning"]["data"]["days"]
    assert stored["2026-10-01"]["events"][0][0] == "light.lampara_salon"


async def test_registry_rename_unloaded_entry(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """An entry that is not loaded follows the rename too, store included."""
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", "demo_relay", "kitchen", suggested_object_id="kitchen_light")
    hass.states.async_set(OLD, STATE_OFF)
    entry = await _setup(hass, hass_storage, routes=[ROUTE])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    registry.async_update_entity(OLD, new_entity_id=NEW)
    await hass.async_block_till_done()

    assert entry.options[CONF_REAR_LIGHTS] == [NEW]
    assert _subentry(entry, SUBENTRY_ROUTE)[CONF_ORIGIN] == NEW
    data = await learning_store(hass, entry.entry_id).async_load()
    names = {record[0] for record in data["days"]["2026-10-01"]["events"]}
    assert names == {NEW, "light.salon"}


async def test_rename_to_missing_id_clears_issue(
    hass: HomeAssistant, fake_lights, hass_storage
) -> None:
    """The recommended path: give the new entity the old entity ID."""
    registry = er.async_get(hass)
    entry = await _setup(hass, hass_storage)
    issue_id = _issue_id(entry, OLD)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    registry.async_get_or_create(
        "switch", "demo_relay", "gen3", suggested_object_id="kitchen_light_new"
    )
    hass.states.async_set(NEW, STATE_OFF)
    registry.async_update_entity(NEW, new_entity_id=OLD)
    await hass.async_block_till_done()
    # The integration adds the entity back under its new (the old) ID.
    hass.states.async_remove(NEW)
    hass.states.async_set(OLD, STATE_OFF)
    await hass.async_block_till_done()

    assert entry.options[CONF_REAR_LIGHTS] == [OLD]
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
