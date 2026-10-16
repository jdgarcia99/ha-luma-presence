"""LumaPresence: presence simulation for Home Assistant."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    floor_registry as fr,
    issue_registry as ir,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .alarm import AlarmController
from .ambience import AmbienceEngine
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAY_TYPE,
    ATTR_END_DATE,
    ATTR_LIGHTS_BEHAVIOR,
    ATTR_ROUTE,
    ATTR_START_DATE,
    CONF_ALARM,
    CONF_ALARM_ACTIVATE_STATES,
    CONF_ALARM_CONTROL,
    CONF_AMBIENCE_PLAYERS,
    CONF_COVERS,
    CONF_FRONT_LIGHTS,
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    CONF_MORNING_END,
    CONF_MORNING_WORKDAYS_ONLY,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    CONF_REPORT_NOTIFY,
    CONF_UNAVAILABLE_ALERT_MINUTES,
    CONF_VERIFY_TIMEOUT,
    DEFAULT_ALARM_ACTIVATE_STATES,
    DEFAULT_ALARM_CONTROL,
    DEFAULT_UNAVAILABLE_ALERT_MINUTES,
    DEFAULT_VERIFY_TIMEOUT,
    DOMAIN,
    ORIGIN_SERVICE,
    SERVICE_DREAM_PREVIEW,
    SERVICE_FORGET_PERIOD,
    SERVICE_GET_HEATMAP,
    SERVICE_RUN_SEQUENCE,
    SERVICE_START,
    SERVICE_STOP,
    STOP_BEHAVIORS,
    SUBENTRY_DETERRENCE,
    SUBENTRY_ROUTE,
)
from .context import ContextProvider
from .covers import CoverEngine
from .deterrence import DeterrenceEngine, DeterrenceTrigger
from .dream import DreamEngine
from .engine import SimulationEngine
from .learning import LearningGate
from .light_control import LightController
from .model import DAY_TYPES
from .models import LumaConfigEntry, LumaRuntime, LumaSettings, Route
from .monitor import (
    ROLE_DREAM,
    ROLE_REPLAY,
    ROLE_ROUTE,
    ROLE_SIMULATION,
    AvailabilityMonitor,
    async_delete_issues,
)
from .plan import PlanPlayer
from .replay import ReplayEngine
from .report import NightReporter
from .scope import entity_area_id
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
DREAM_PREVIEW_SCHEMA = vol.Schema(_BASE_SCHEMA)
HEATMAP_SCHEMA = vol.Schema({**_BASE_SCHEMA, vol.Required(ATTR_DAY_TYPE): vol.In(DAY_TYPES)})
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
            await runtime.engine.async_set_enabled(True, origin=ORIGIN_SERVICE)

    async def _stop(call: ServiceCall) -> None:
        for runtime in _runtimes(hass, call):
            await runtime.engine.async_set_enabled(
                False,
                stop_behavior=call.data.get(ATTR_LIGHTS_BEHAVIOR),
                origin=ORIGIN_SERVICE,
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

    async def _dream_preview(call: ServiceCall) -> ServiceResponse:
        runtime = _runtimes(hass, call)[0]
        assert runtime.dream is not None
        plan = await runtime.dream.async_preview(runtime.engine.planning_window())
        if plan is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="dream_not_ready",
                translation_placeholders={
                    "reason": str(runtime.dream.preview_status.get("reason")),
                },
            )
        return {
            "night": plan.night.isoformat(),
            "score": plan.score,
            "reason": plan.reason,
            "summary": plan.summary(),
            "rooms": plan.rooms,
            "timeline": plan.compact(),
            **{
                key: runtime.dream.preview_status.get(key)
                for key in ("candidates", "similarity_max", "day_type")
            },
        }

    async def _get_heatmap(call: ServiceCall) -> ServiceResponse:
        runtime = _runtimes(hass, call)[0]
        assert runtime.dream is not None
        return await runtime.dream.async_heatmap(call.data[ATTR_DAY_TYPE])

    hass.services.async_register(
        DOMAIN,
        SERVICE_DREAM_PREVIEW,
        _dream_preview,
        schema=DREAM_PREVIEW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_HEATMAP,
        _get_heatmap,
        schema=HEATMAP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
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
    triggers = [
        DeterrenceTrigger.from_data(subentry.subentry_id, subentry.title, subentry.data)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_DETERRENCE
    ]
    settings = LumaSettings()
    controller = LightController(
        hass,
        entry.entry_id,
        float(config.get(CONF_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT)),
    )
    # Loaded now: restoring the master switch reads its saved origin.
    await controller.async_load()
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
    player = PlanPlayer(hass, entry.entry_id, controller, learning)
    replay = ReplayEngine(
        hass,
        entry.entry_id,
        config,
        settings,
        controller,
        learning,
        player,
        on_lights=lambda lights: monitor.async_set_role_lights(ROLE_REPLAY, lights),
    )
    await replay.async_load()
    dream = DreamEngine(
        hass,
        entry.entry_id,
        config,
        settings,
        learning,
        player,
        on_lights=lambda lights: monitor.async_set_role_lights(ROLE_DREAM, lights),
    )
    await dream.async_load()
    context = ContextProvider(
        hass,
        config,
        on_change=lambda: _context_changed(),
    )

    @callback
    def _context_changed() -> None:
        """Darkness or workdays changed: windows move, the daylight guard acts.

        The daylight guard ends routes and deterrence holds; it never blocks
        covers, which only move to their new times.
        """
        sequences.async_check_daylight()
        deterrence.async_check_daylight()
        covers.async_refresh()
        hass.async_create_task(
            engine.async_settings_changed(), f"{DOMAIN}_context_{entry.entry_id}"
        )

    controller.daylight = sequences.daylight = context.daylight
    engine.replay = replay
    engine.dream = dream
    engine.player = player
    engine.context = replay.context = dream.context = context

    def alternatives(entity_id: str) -> list[str]:
        """Other simulation lights of a light's room, in room-mapping order."""
        if (area := entity_area_id(hass, entity_id)) is None:
            return []
        front = set(config.get(CONF_FRONT_LIGHTS, []))
        rear = set(config.get(CONF_REAR_LIGHTS, []))
        lights = list(config.get(CONF_LIGHTS, []))
        same_room = [
            (0 if light in front else 1 if light in rear else 2, index, light)
            for index, light in enumerate(lights)
            if light != entity_id and entity_area_id(hass, light) == area
        ]
        return [light for _, _, light in sorted(same_room)]

    player.alternatives = sequences.alternatives = alternatives
    player.map_by_room = lambda: settings.map_by_room
    covers = CoverEngine(
        hass, entry.entry_id, config, settings, controller, learning, player, context, replay
    )
    if covers.covers:
        player.covers_for = covers.plan_items
    ambience = AmbienceEngine(hass, entry.entry_id, config, controller, engine, player)
    deterrence = DeterrenceEngine(
        hass, entry.entry_id, config, settings, controller, engine, player, triggers
    )
    ambience.daylight = deterrence.daylight = context.daylight
    deterrence.dark_now = context.dark_now
    reporter = NightReporter(
        hass,
        entry.entry_id,
        settings,
        controller,
        player,
        config.get(CONF_REPORT_NOTIFY),
        dream,
    )
    alarm: AlarmController | None = None
    if config.get(CONF_ALARM_CONTROL, DEFAULT_ALARM_CONTROL) and config.get(CONF_ALARM):
        alarm = AlarmController(
            hass,
            entry.entry_id,
            config[CONF_ALARM],
            config.get(CONF_ALARM_ACTIVATE_STATES, DEFAULT_ALARM_ACTIVATE_STATES),
            engine,
            controller,
        )
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
        replay=replay,
        alarm=alarm,
        context=context,
        player=player,
        dream=dream,
        reporter=reporter,
        covers=covers,
        ambience=ambience,
        deterrence=deterrence,
    )

    # Entities restore their settings while the platforms are set up.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_start(_hass: HomeAssistant) -> None:
        route_lights = [light for route in routes for light in route.lights]
        path_lights = [light for trigger in triggers for light in trigger.path]
        _check_entities(
            hass,
            entry,
            config,
            [
                *route_lights,
                *path_lights,
                *(trigger.entity_id for trigger in triggers),
            ],
        )
        await controller.async_start(
            [*engine.lights, *route_lights, *path_lights, *covers.covers]
        )
        context.async_start()
        await engine.async_start()
        dream.async_start()
        await sequences.async_start()
        reporter.async_start()
        covers.async_start()
        deterrence.async_start()
        await learning.async_start()
        await monitor.async_start()
        ambience.async_start()
        # Last: the master switch is restored and every engine has started.
        if alarm is not None:
            await alarm.async_start()

    entry.async_on_unload(async_at_started(hass, _async_start))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> bool:
    """Migrate an old config entry."""
    if entry.version > 3:
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
    if entry.version == 2:
        # v0.7.0: the morning window became the wake-up mode.
        options = {
            key: value
            for key, value in entry.options.items()
            if key not in (CONF_MORNING_END, CONF_MORNING_WORKDAYS_ONLY)
        }
        hass.config_entries.async_update_entry(entry, options=options, version=3)
        _LOGGER.info("Migrated %s to version 3", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LumaConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = entry.runtime_data
    if runtime.deterrence is not None:
        await runtime.deterrence.async_shutdown()
    if runtime.ambience is not None:
        await runtime.ambience.async_shutdown()
    if runtime.covers is not None:
        runtime.covers.async_shutdown()
    await runtime.sequences.async_shutdown()
    await runtime.engine.async_shutdown()
    await runtime.learning.async_shutdown()
    await runtime.controller.async_shutdown()
    await runtime.monitor.async_shutdown()
    runtime.replay.async_shutdown()
    if runtime.dream is not None:
        runtime.dream.async_shutdown()
    if runtime.reporter is not None:
        runtime.reporter.async_shutdown()
    if runtime.player is not None:
        runtime.player.async_shutdown()
    if runtime.context is not None:
        runtime.context.async_shutdown()
    if runtime.alarm is not None:
        await runtime.alarm.async_shutdown()
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
    hass: HomeAssistant, entry: LumaConfigEntry, config: dict, extra: list[str]
) -> None:
    """Raise a repair issue if configured entities no longer exist."""
    entities = [
        *config.get(CONF_LIGHTS, []),
        *config.get(CONF_PERSONS, []),
        *config.get(CONF_COVERS, []),
        *config.get(CONF_AMBIENCE_PLAYERS, []),
        *dict.fromkeys(extra),
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
