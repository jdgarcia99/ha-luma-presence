"""LumaPresence: presence simulation for Home Assistant."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    floor_registry as fr,
    issue_registry as ir,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_END_DATE,
    ATTR_LIGHTS_BEHAVIOR,
    ATTR_START_DATE,
    CONF_ALARM,
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    DOMAIN,
    SERVICE_FORGET_PERIOD,
    SERVICE_START,
    SERVICE_STOP,
    STOP_BEHAVIORS,
)
from .engine import SimulationEngine
from .learning import LearningGate
from .models import LumaConfigEntry, LumaRuntime, LumaSettings

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_BASE_SCHEMA = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
START_SCHEMA = vol.Schema(_BASE_SCHEMA)
STOP_SCHEMA = vol.Schema(
    {**_BASE_SCHEMA, vol.Optional(ATTR_LIGHTS_BEHAVIOR): vol.In(STOP_BEHAVIORS)}
)
FORGET_SCHEMA = vol.Schema(
    {
        **_BASE_SCHEMA,
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Required(ATTR_END_DATE): cv.date,
    }
)


def _runtimes(hass: HomeAssistant, call: ServiceCall) -> list[LumaRuntime]:
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and (entry_id is None or entry.entry_id == entry_id)
    ]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entry")
    return [entry.runtime_data for entry in entries]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services."""

    async def _start(call: ServiceCall) -> None:
        for runtime in _runtimes(hass, call):
            await runtime.engine.async_set_enabled(True)

    async def _stop(call: ServiceCall) -> None:
        for runtime in _runtimes(hass, call):
            await runtime.engine.async_set_enabled(
                False, stop_behavior=call.data.get(ATTR_LIGHTS_BEHAVIOR)
            )

    async def _forget(call: ServiceCall) -> None:
        for runtime in _runtimes(hass, call):
            removed = runtime.learning.async_forget(
                call.data[ATTR_START_DATE], call.data[ATTR_END_DATE]
            )
            _LOGGER.info("Forgot %s learned days", removed)

    hass.services.async_register(DOMAIN, SERVICE_START, _start, schema=START_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP, _stop, schema=STOP_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_FORGET_PERIOD, _forget, schema=FORGET_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> bool:
    """Set up LumaPresence from a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    config = {**entry.data, **entry.options}
    settings = LumaSettings()
    engine = SimulationEngine(hass, entry.entry_id, config, settings)
    learning = LearningGate(hass, entry.entry_id, config, settings, engine)
    await learning.async_load()
    entry.runtime_data = LumaRuntime(
        hass=hass,
        entry_id=entry.entry_id,
        version=str(integration.version),
        config=config,
        settings=settings,
        engine=engine,
        learning=learning,
    )

    # Entities restore their settings while the platforms are set up.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_start(_hass: HomeAssistant) -> None:
        _check_entities(hass, entry, config)
        await engine.async_start()
        await learning.async_start()

    entry.async_on_unload(async_at_started(hass, _async_start))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> bool:
    """Migrate an old config entry."""
    if entry.version > 2:
        return False  # downgrade from a future version
    if entry.version == 1:
        # v0.2.0 adds the rear facade and the learning scope.
        floors = [floor.floor_id for floor in fr.async_get(hass).async_list_floors()]
        options = {
            CONF_REAR_LIGHTS: [],
            CONF_LEARN_FLOORS: floors,
            CONF_LEARN_AREAS: [],
            CONF_LEARN_EXTRA: [],
            CONF_LEARN_EXCLUDE: [],
            **entry.options,
        }
        hass.config_entries.async_update_entry(entry, options=options, version=2)
        _LOGGER.info("Migrated %s to version 2", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = entry.runtime_data
    await runtime.engine.async_shutdown()
    await runtime.learning.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> None:
    """Clean up the repair issue when the entry is deleted."""
    ir.async_delete_issue(hass, DOMAIN, f"missing_entities_{entry.entry_id}")


async def _async_update_listener(hass: HomeAssistant, entry: LumaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _check_entities(hass: HomeAssistant, entry: LumaConfigEntry, config: dict) -> None:
    """Raise a repair issue if configured entities no longer exist."""
    entities = [*config.get(CONF_LIGHTS, []), *config.get(CONF_PERSONS, [])]
    if alarm := config.get(CONF_ALARM):
        entities.append(alarm)
    missing = [entity_id for entity_id in entities if hass.states.get(entity_id) is None]
    issue_id = f"missing_entities_{entry.entry_id}"
    if missing:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="missing_entities",
            translation_placeholders={
                "title": entry.title,
                "entities": ", ".join(missing),
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
