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
    ATTR_ROUTE,
    ATTR_START_DATE,
    CONF_ALARM,
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    CONF_UNAVAILABLE_ALERT_MINUTES,
    DEFAULT_UNAVAILABLE_ALERT_MINUTES,
    DOMAIN,
    SERVICE_FORGET_PERIOD,
    SERVICE_RUN_SEQUENCE,
    SERVICE_START,
    SERVICE_STOP,
    STOP_BEHAVIORS,
    SUBENTRY_ROUTE,
)
from .engine import SimulationEngine
from .learning import LearningGate
from .light_control import LightController
from .models import LumaConfigEntry, LumaRuntime, LumaSettings, Route
from .monitor import (
    ROLE_ROUTE,
    ROLE_SIMULATION,
    AvailabilityMonitor,
    async_delete_issues,
)
from .sequences import SequenceEngine

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
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
RUN_SEQUENCE_SCHEMA = vol.Schema({**_BASE_SCHEMA, vol.Optional(ATTR_ROUTE): cv.string})
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

    async def _run_sequence(call: ServiceCall) -> None:
        runtimes = _runtimes(hass, call)
        if (name := call.data.get(ATTR_ROUTE)) is not None:
            runtimes = [r for r in runtimes if r.sequences.has_route(name)]
            if not runtimes:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_route",
                    translation_placeholders={"route": name},
                )
        for runtime in runtimes:
            runtime.sequences.async_run_now(name)

    hass.services.async_register(DOMAIN, SERVICE_START, _start, schema=START_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP, _stop, schema=STOP_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_FORGET_PERIOD, _forget, schema=FORGET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RUN_SEQUENCE, _run_sequence, schema=RUN_SEQUENCE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> bool:
    """Set up LumaPresence from a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    config = {**entry.data, **entry.options}
    routes = [
        Route.from_data(subentry.subentry_id, subentry.title, subentry.data)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_ROUTE
    ]
    settings = LumaSettings()
    controller = LightController(hass, entry.entry_id)
    monitor = AvailabilityMonitor(
        hass,
        entry.entry_id,
        float(
            config.get(
                CONF_UNAVAILABLE_ALERT_MINUTES, DEFAULT_UNAVAILABLE_ALERT_MINUTES
            )
        ),
        _light_roles(config, routes),
    )
    engine = SimulationEngine(hass, entry.entry_id, config, settings, controller)
    sequences = SequenceEngine(
        hass, entry.entry_id, config, settings, controller, engine, routes
    )
    learning = LearningGate(hass, entry.entry_id, config, settings, engine)
    await learning.async_load()
    entry.runtime_data = LumaRuntime(
        hass=hass,
        entry_id=entry.entry_id,
        version=str(integration.version),
        config=config,
        settings=settings,
        controller=controller,
        engine=engine,
        sequences=sequences,
        learning=learning,
        monitor=monitor,
    )

    # Entities restore their settings while the platforms are set up.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_start(_hass: HomeAssistant) -> None:
        route_lights = [light for route in routes for light in route.lights]
        _check_entities(hass, entry, config, route_lights)
        await controller.async_start([*engine.lights, *route_lights])
        await engine.async_start()
        await sequences.async_start()
        await learning.async_start()
        await monitor.async_start()

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
    await runtime.sequences.async_shutdown()
    await runtime.engine.async_shutdown()
    await runtime.learning.async_shutdown()
    await runtime.controller.async_shutdown()
    await runtime.monitor.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> None:
    """Clean up the repair issue when the entry is deleted."""
    ir.async_delete_issue(hass, DOMAIN, f"missing_entities_{entry.entry_id}")
    async_delete_issues(hass, entry.entry_id)


def _light_roles(
    config: dict, routes: list[Route]
) -> dict[str, list[tuple[str, str | None]]]:
    """Roles of each watched light: simulation light and/or route lights."""
    roles: dict[str, list[tuple[str, str | None]]] = {
        light: [(ROLE_SIMULATION, None)] for light in config.get(CONF_LIGHTS, [])
    }
    for route in routes:
        for light in dict.fromkeys(route.lights):
            roles.setdefault(light, []).append((ROLE_ROUTE, route.name))
    return roles


async def _async_update_listener(hass: HomeAssistant, entry: LumaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _check_entities(
    hass: HomeAssistant, entry: LumaConfigEntry, config: dict, route_lights: list[str]
) -> None:
    """Raise a repair issue if configured entities no longer exist."""
    entities = [
        *config.get(CONF_LIGHTS, []),
        *config.get(CONF_PERSONS, []),
        *dict.fromkeys(route_lights),
    ]
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
