"""Config and options flow for LumaPresence.

The initial setup walks through the steps in sequence. The options flow
shows them as a menu: every step edits a working copy of the options and
returns to the menu, and only Save writes the entry.

Night routes are config subentries, added from the integration page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    FloorSelector,
    FloorSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

from .const import (
    ALARM_ACTIVATE_CHOICES,
    CONF_ALARM,
    CONF_ALARM_ACTIVATE_STATES,
    CONF_ALARM_CONTROL,
    CONF_AUTO_EXCLUDE_INDICATORS,
    CONF_BAD_WEATHER_BOOST,
    CONF_DARKNESS_EARLIEST,
    CONF_DARKNESS_LATEST,
    CONF_DARKNESS_SENSOR,
    CONF_DARKNESS_THRESHOLD,
    CONF_DESTINATION,
    CONF_DREAM_CANDIDATES,
    CONF_DREAM_MAX_SIMILARITY,
    CONF_DWELL_MAX,
    CONF_DWELL_MIN,
    CONF_FRONT_LIGHTS,
    CONF_HOLIDAY_CALENDAR,
    CONF_HUMAN_ONLY,
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    CONF_MARGIN_AFTER,
    CONF_MARGIN_BEFORE,
    CONF_MORNING_ENABLED,
    CONF_MORNING_END,
    CONF_MORNING_START,
    CONF_MORNING_WORKDAYS_ONLY,
    CONF_ORIGIN,
    CONF_PATH,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    CONF_REPORT_NOTIFY,
    CONF_STEP_DELAY_MAX,
    CONF_STEP_DELAY_MIN,
    CONF_UNAVAILABLE_ALERT_MINUTES,
    CONF_VERIFY_TIMEOUT,
    CONF_WEATHER_ENTITY,
    DEFAULT_ALARM_ACTIVATE_STATES,
    DEFAULT_ALARM_CONTROL,
    DEFAULT_AUTO_EXCLUDE_INDICATORS,
    DEFAULT_BAD_WEATHER_BOOST,
    DEFAULT_DARKNESS_EARLIEST,
    DEFAULT_DARKNESS_LATEST,
    DEFAULT_DARKNESS_THRESHOLD,
    DEFAULT_DREAM_CANDIDATES,
    DEFAULT_DREAM_MAX_SIMILARITY,
    DEFAULT_DWELL_MAX,
    DEFAULT_DWELL_MIN,
    DEFAULT_HUMAN_ONLY,
    DEFAULT_MARGIN_AFTER,
    DEFAULT_MARGIN_BEFORE,
    DEFAULT_MORNING_ENABLED,
    DEFAULT_MORNING_END,
    DEFAULT_MORNING_START,
    DEFAULT_MORNING_WORKDAYS_ONLY,
    DEFAULT_NAME,
    DEFAULT_STEP_DELAY_MAX,
    DEFAULT_STEP_DELAY_MIN,
    DEFAULT_UNAVAILABLE_ALERT_MINUTES,
    DEFAULT_VERIFY_TIMEOUT,
    DOMAIN,
    EXCLUDED_INDICATOR,
    PREVIEW_MAX_ENTITIES,
    SUBENTRY_ROUTE,
)
from .scope import entity_area_id, resolve_learning_scope

STEP_SIMULATION_LIGHTS = "simulation_lights"
STEP_FACADES = "facades"
STEP_LEARNING = "learning"
STEP_LEARNING_PREVIEW = "learning_preview"
STEP_PRESENCE = "presence"
STEP_SEQUENCES = "sequences"
STEP_CONTEXT = "context"
STEP_DREAM = "dream"
STEP_MONITORING = "monitoring"
STEP_SAVE = "save"
MENU_OPTIONS = [
    STEP_SIMULATION_LIGHTS,
    STEP_FACADES,
    STEP_LEARNING,
    STEP_LEARNING_PREVIEW,
    STEP_PRESENCE,
    STEP_CONTEXT,
    STEP_SEQUENCES,
    STEP_DREAM,
    STEP_MONITORING,
    STEP_SAVE,
]
PREVIEW_MAX_INDICATORS = 20

_LIGHT_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["light", "switch"], multiple=True)
)
_HOURS_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0, max=24, step=0.5, unit_of_measurement="h", mode=NumberSelectorMode.BOX
    )
)

# Optional fields of each step and their value when left empty.
_EMPTY_VALUES: dict[str, dict[str, Any]] = {
    STEP_SIMULATION_LIGHTS: {CONF_LIGHTS: []},
    STEP_FACADES: {CONF_FRONT_LIGHTS: [], CONF_REAR_LIGHTS: []},
    STEP_LEARNING: {
        CONF_LEARN_FLOORS: [],
        CONF_LEARN_AREAS: [],
        CONF_LEARN_EXTRA: [],
        CONF_LEARN_EXCLUDE: [],
    },
    STEP_PRESENCE: {CONF_PERSONS: [], CONF_ALARM: None, CONF_ALARM_ACTIVATE_STATES: []},
    STEP_MONITORING: {CONF_REPORT_NOTIFY: ""},
    STEP_CONTEXT: {
        CONF_DARKNESS_SENSOR: None,
        CONF_HOLIDAY_CALENDAR: None,
        CONF_WEATHER_ENTITY: None,
    },
}


def _default_options(hass: HomeAssistant) -> dict[str, Any]:
    return {
        CONF_LIGHTS: [],
        CONF_FRONT_LIGHTS: [],
        CONF_REAR_LIGHTS: [],
        CONF_LEARN_FLOORS: [
            floor.floor_id for floor in fr.async_get(hass).async_list_floors()
        ],
        CONF_LEARN_AREAS: [],
        CONF_LEARN_EXTRA: [],
        CONF_LEARN_EXCLUDE: [],
        CONF_HUMAN_ONLY: DEFAULT_HUMAN_ONLY,
        CONF_AUTO_EXCLUDE_INDICATORS: DEFAULT_AUTO_EXCLUDE_INDICATORS,
        CONF_PERSONS: [],
        CONF_MARGIN_BEFORE: DEFAULT_MARGIN_BEFORE,
        CONF_MARGIN_AFTER: DEFAULT_MARGIN_AFTER,
        CONF_ALARM_CONTROL: DEFAULT_ALARM_CONTROL,
        CONF_ALARM_ACTIVATE_STATES: list(DEFAULT_ALARM_ACTIVATE_STATES),
        CONF_UNAVAILABLE_ALERT_MINUTES: DEFAULT_UNAVAILABLE_ALERT_MINUTES,
        CONF_DREAM_CANDIDATES: DEFAULT_DREAM_CANDIDATES,
        CONF_DREAM_MAX_SIMILARITY: DEFAULT_DREAM_MAX_SIMILARITY,
    }


def _suggested(options: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {"suggested_value": options.get(key)}


def _lights_schema(options: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Optional(CONF_LIGHTS, description=_suggested(options, CONF_LIGHTS)): (
            _LIGHT_SELECTOR
        ),
    }


def _facades_schema(options: Mapping[str, Any]) -> vol.Schema:
    selector = EntitySelector(
        EntitySelectorConfig(
            include_entities=list(options.get(CONF_LIGHTS) or []), multiple=True
        )
    )
    return vol.Schema(
        {
            vol.Optional(
                CONF_FRONT_LIGHTS, description=_suggested(options, CONF_FRONT_LIGHTS)
            ): selector,
            vol.Optional(
                CONF_REAR_LIGHTS, description=_suggested(options, CONF_REAR_LIGHTS)
            ): selector,
        }
    )


def _learning_schema(options: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_LEARN_FLOORS, description=_suggested(options, CONF_LEARN_FLOORS)
            ): FloorSelector(FloorSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_LEARN_AREAS, description=_suggested(options, CONF_LEARN_AREAS)
            ): AreaSelector(AreaSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_LEARN_EXTRA, description=_suggested(options, CONF_LEARN_EXTRA)
            ): _LIGHT_SELECTOR,
            vol.Optional(
                CONF_LEARN_EXCLUDE, description=_suggested(options, CONF_LEARN_EXCLUDE)
            ): _LIGHT_SELECTOR,
            vol.Required(
                CONF_AUTO_EXCLUDE_INDICATORS,
                default=options.get(
                    CONF_AUTO_EXCLUDE_INDICATORS, DEFAULT_AUTO_EXCLUDE_INDICATORS
                ),
            ): BooleanSelector(),
            vol.Required(
                CONF_HUMAN_ONLY,
                default=options.get(CONF_HUMAN_ONLY, DEFAULT_HUMAN_ONLY),
            ): BooleanSelector(),
        }
    )


def _presence_schema(options: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_PERSONS, description=_suggested(options, CONF_PERSONS)): (
                EntitySelector(EntitySelectorConfig(domain="person", multiple=True))
            ),
            vol.Optional(CONF_ALARM, description=_suggested(options, CONF_ALARM)): (
                EntitySelector(EntitySelectorConfig(domain="alarm_control_panel"))
            ),
            vol.Required(
                CONF_ALARM_CONTROL,
                default=options.get(CONF_ALARM_CONTROL, DEFAULT_ALARM_CONTROL),
            ): BooleanSelector(),
            vol.Optional(
                CONF_ALARM_ACTIVATE_STATES,
                description={
                    "suggested_value": options.get(
                        CONF_ALARM_ACTIVATE_STATES, DEFAULT_ALARM_ACTIVATE_STATES
                    )
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=ALARM_ACTIVATE_CHOICES,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                    translation_key="alarm_state",
                )
            ),
            vol.Required(
                CONF_MARGIN_BEFORE,
                default=options.get(CONF_MARGIN_BEFORE, DEFAULT_MARGIN_BEFORE),
            ): _HOURS_SELECTOR,
            vol.Required(
                CONF_MARGIN_AFTER,
                default=options.get(CONF_MARGIN_AFTER, DEFAULT_MARGIN_AFTER),
            ): _HOURS_SELECTOR,
        }
    )


def _number(maximum: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, max=maximum, step=1, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


# (option, default, maximum, unit)
_SEQUENCE_OPTIONS = (
    (CONF_STEP_DELAY_MIN, DEFAULT_STEP_DELAY_MIN, 600, "s"),
    (CONF_STEP_DELAY_MAX, DEFAULT_STEP_DELAY_MAX, 600, "s"),
    (CONF_DWELL_MIN, DEFAULT_DWELL_MIN, 120, "min"),
    (CONF_DWELL_MAX, DEFAULT_DWELL_MAX, 120, "min"),
)


def _sequences_schema(options: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(key, default=options.get(key, default)): _number(maximum, unit)
            for key, default, maximum, unit in _SEQUENCE_OPTIONS
        }
    )


def _monitoring_schema(options: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_UNAVAILABLE_ALERT_MINUTES,
                default=options.get(
                    CONF_UNAVAILABLE_ALERT_MINUTES, DEFAULT_UNAVAILABLE_ALERT_MINUTES
                ),
            ): _number(1440, "min"),
            vol.Optional(
                CONF_REPORT_NOTIFY, description=_suggested(options, CONF_REPORT_NOTIFY)
            ): TextSelector(),
            vol.Required(
                CONF_VERIFY_TIMEOUT,
                default=options.get(CONF_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT),
            ): _number(60, "s"),
        }
    )


def _context_schema(options: Mapping[str, Any]) -> vol.Schema:
    def minutes(low: int, high: int) -> NumberSelector:
        return NumberSelector(
            NumberSelectorConfig(
                min=low, max=high, step=5, unit_of_measurement="min", mode=NumberSelectorMode.BOX
            )
        )

    def entity(**config: Any) -> EntitySelector:
        return EntitySelector(EntitySelectorConfig(**config))

    return vol.Schema(
        {
            vol.Optional(
                CONF_DARKNESS_SENSOR, description=_suggested(options, CONF_DARKNESS_SENSOR)
            ): entity(domain="sensor", device_class="illuminance"),
            vol.Required(
                CONF_DARKNESS_THRESHOLD,
                default=options.get(CONF_DARKNESS_THRESHOLD, DEFAULT_DARKNESS_THRESHOLD),
            ): _number(2000, "lx"),
            vol.Required(
                CONF_DARKNESS_EARLIEST,
                default=options.get(CONF_DARKNESS_EARLIEST, DEFAULT_DARKNESS_EARLIEST),
            ): minutes(-180, 180),
            vol.Required(
                CONF_DARKNESS_LATEST,
                default=options.get(CONF_DARKNESS_LATEST, DEFAULT_DARKNESS_LATEST),
            ): minutes(-180, 180),
            vol.Required(
                CONF_MORNING_ENABLED,
                default=options.get(CONF_MORNING_ENABLED, DEFAULT_MORNING_ENABLED),
            ): BooleanSelector(),
            vol.Required(
                CONF_MORNING_START,
                default=options.get(CONF_MORNING_START, DEFAULT_MORNING_START),
            ): TimeSelector(),
            vol.Required(
                CONF_MORNING_END,
                default=options.get(CONF_MORNING_END, DEFAULT_MORNING_END),
            ): TimeSelector(),
            vol.Required(
                CONF_MORNING_WORKDAYS_ONLY,
                default=options.get(CONF_MORNING_WORKDAYS_ONLY, DEFAULT_MORNING_WORKDAYS_ONLY),
            ): BooleanSelector(),
            vol.Optional(
                CONF_HOLIDAY_CALENDAR, description=_suggested(options, CONF_HOLIDAY_CALENDAR)
            ): entity(domain="calendar"),
            vol.Optional(
                CONF_WEATHER_ENTITY, description=_suggested(options, CONF_WEATHER_ENTITY)
            ): entity(domain="weather"),
            vol.Required(
                CONF_BAD_WEATHER_BOOST,
                default=options.get(CONF_BAD_WEATHER_BOOST, DEFAULT_BAD_WEATHER_BOOST),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=50, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


def _validate_context(options: Mapping[str, Any]) -> dict[str, str]:
    earliest = float(options.get(CONF_DARKNESS_EARLIEST, DEFAULT_DARKNESS_EARLIEST))
    latest = float(options.get(CONF_DARKNESS_LATEST, DEFAULT_DARKNESS_LATEST))
    if earliest > latest:
        return {CONF_DARKNESS_LATEST: "darkness_range"}
    start = str(options.get(CONF_MORNING_START, DEFAULT_MORNING_START))
    end = str(options.get(CONF_MORNING_END, DEFAULT_MORNING_END))
    if options.get(CONF_MORNING_ENABLED) and start >= end:
        return {CONF_MORNING_END: "morning_range"}
    return {}


def _dream_schema(options: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_DREAM_CANDIDATES,
                default=options.get(CONF_DREAM_CANDIDATES, DEFAULT_DREAM_CANDIDATES),
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=100, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_DREAM_MAX_SIMILARITY,
                default=options.get(CONF_DREAM_MAX_SIMILARITY, DEFAULT_DREAM_MAX_SIMILARITY),
            ): NumberSelector(
                NumberSelectorConfig(min=0, max=1, step=0.05, mode=NumberSelectorMode.SLIDER)
            ),
        }
    )


def _validate_lights(options: Mapping[str, Any]) -> dict[str, str]:
    if not options.get(CONF_LIGHTS):
        return {CONF_LIGHTS: "no_lights"}
    return {}


def _validate_presence(options: Mapping[str, Any]) -> dict[str, str]:
    if not options.get(CONF_ALARM_CONTROL):
        return {}
    if not options.get(CONF_ALARM):
        return {CONF_ALARM: "alarm_required"}
    if not options.get(CONF_ALARM_ACTIVATE_STATES):
        return {CONF_ALARM_ACTIVATE_STATES: "activate_states_required"}
    return {}


def _validate_facades(options: Mapping[str, Any]) -> dict[str, str]:
    lights = set(options.get(CONF_LIGHTS) or [])
    front = set(options.get(CONF_FRONT_LIGHTS) or [])
    rear = set(options.get(CONF_REAR_LIGHTS) or [])
    if not front <= lights:
        return {CONF_FRONT_LIGHTS: "facade_not_subset"}
    if not rear <= lights:
        return {CONF_REAR_LIGHTS: "facade_not_subset"}
    if front & rear:
        return {"base": "facade_overlap"}
    return {}


def _preview_placeholders(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> dict[str, str]:
    """Markdown lists of the entities that will be learned and of the
    indicator LEDs left out automatically."""
    scope = resolve_learning_scope(hass, options)
    entities = scope.entities
    areas = ar.async_get(hass)
    registry = er.async_get(hass)

    def line(entity_id: str) -> str:
        state = hass.states.get(entity_id)
        entry = registry.async_get(entity_id)
        name = (
            (state.name if state else None)
            or (entry.name or entry.original_name if entry else None)
            or entity_id
        )
        text = f"- {name} (`{entity_id}`)"
        if (area_id := entity_area_id(hass, entity_id)) and (
            area := areas.async_get_area(area_id)
        ):
            text += f" · {area.name}"
        return text

    lines = [line(entity_id) for entity_id in entities[:PREVIEW_MAX_ENTITIES]]
    if len(entities) > PREVIEW_MAX_ENTITIES:
        lines.append("- …")
    indicators = [e for e, why in scope.reasons.items() if why == EXCLUDED_INDICATOR]
    indicator_lines = [line(entity_id) for entity_id in indicators[:PREVIEW_MAX_INDICATORS]]
    if len(indicators) > PREVIEW_MAX_INDICATORS:
        indicator_lines.append("- …")
    return {
        "count": str(len(entities)),
        "shown": str(min(len(entities), PREVIEW_MAX_ENTITIES)),
        "entities": "\n".join(lines) or "-",
        "indicators": "\n".join(indicator_lines) or "-",
    }


class _LumaSteps:
    """Steps shared by the config flow and the options flow."""

    hass: HomeAssistant
    _options: dict[str, Any]

    async def _async_after(self, step_id: str) -> ConfigFlowResult:
        """Continue after a step has been submitted."""
        raise NotImplementedError

    def _update(self, step_id: str, user_input: dict[str, Any]) -> None:
        self._options.update(_EMPTY_VALUES.get(step_id, {}))
        self._options.update(user_input)
        if not self._options.get(CONF_ALARM):
            self._options.pop(CONF_ALARM, None)

    async def async_step_facades(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Front and rear lights."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._options, **_EMPTY_VALUES[STEP_FACADES], **user_input}
            errors = _validate_facades(candidate)
            if not errors:
                self._update(STEP_FACADES, user_input)
                return await self._async_after(STEP_FACADES)
        return self.async_show_form(
            step_id=STEP_FACADES,
            data_schema=_facades_schema({**self._options, **(user_input or {})}),
            errors=errors,
        )

    async def async_step_learning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Learning floors, areas, extra and excluded entities."""
        if user_input is not None:
            self._update(STEP_LEARNING, user_input)
            return await self.async_step_learning_preview()
        return self.async_show_form(
            step_id=STEP_LEARNING, data_schema=_learning_schema(self._options)
        )

    async def async_step_learning_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read-only list of the entities that will be learned."""
        if user_input is not None:
            return await self._async_after(STEP_LEARNING_PREVIEW)
        return self.async_show_form(
            step_id=STEP_LEARNING_PREVIEW,
            data_schema=vol.Schema({}),
            description_placeholders=_preview_placeholders(self.hass, self._options),
        )

    async def async_step_presence(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """People, alarm panel, alarm control and learning margins."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._options, **_EMPTY_VALUES[STEP_PRESENCE], **user_input}
            errors = _validate_presence(candidate)
            if not errors:
                self._update(STEP_PRESENCE, user_input)
                return await self._async_after(STEP_PRESENCE)
        return self.async_show_form(
            step_id=STEP_PRESENCE,
            data_schema=_presence_schema({**self._options, **(user_input or {})}),
            errors=errors,
        )


class LumaPresenceConfigFlow(_LumaSteps, ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._title = DEFAULT_NAME
        self._options = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name and simulation lights."""
        if not self._options:
            self._options = _default_options(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_lights(user_input)
            if not errors:
                self._title = user_input.get(CONF_NAME) or DEFAULT_NAME
                self._options[CONF_LIGHTS] = user_input[CONF_LIGHTS]
                return await self.async_step_facades()
        defaults = {**self._options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, self._title)
                ): TextSelector(),
                **_lights_schema(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_after(self, step_id: str) -> ConfigFlowResult:
        if step_id == STEP_FACADES:
            return await self.async_step_learning()
        if step_id == STEP_LEARNING_PREVIEW:
            return await self.async_step_presence()
        return self.async_create_entry(title=self._title, data={}, options=self._options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return LumaPresenceOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Night routes are subentries of the entry."""
        return {SUBENTRY_ROUTE: RouteSubentryFlow}


class LumaPresenceOptionsFlow(_LumaSteps, OptionsFlow):
    """Edit the options from a menu; only Save writes the entry."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._options = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu."""
        if not self._options:
            self._options = {
                **_default_options(self.hass),
                **self.config_entry.options,
            }
        return self.async_show_menu(step_id="init", menu_options=MENU_OPTIONS)

    async def async_step_simulation_lights(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Simulation lights."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_lights(user_input)
            if not errors:
                self._update(STEP_SIMULATION_LIGHTS, user_input)
                return await self.async_step_init()
        return self.async_show_form(
            step_id=STEP_SIMULATION_LIGHTS,
            data_schema=vol.Schema(_lights_schema(self._options)),
            errors=errors,
        )

    async def async_step_sequences(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step delays and time spent at the destination of night routes."""
        if user_input is not None:
            self._update(STEP_SEQUENCES, user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id=STEP_SEQUENCES, data_schema=_sequences_schema(self._options)
        )

    async def async_step_context(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Darkness, morning window, holidays and weather."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**self._options, **_EMPTY_VALUES[STEP_CONTEXT], **user_input}
            errors = _validate_context(candidate)
            if not errors:
                self._update(STEP_CONTEXT, user_input)
                return await self.async_step_init()
        return self.async_show_form(
            step_id=STEP_CONTEXT,
            data_schema=_context_schema({**self._options, **(user_input or {})}),
            errors=errors,
        )

    async def async_step_dream(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dream candidates and novelty."""
        if user_input is not None:
            self._update(STEP_DREAM, user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id=STEP_DREAM, data_schema=_dream_schema(self._options)
        )

    async def async_step_monitoring(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Repair issues for lights that stay unavailable."""
        if user_input is not None:
            self._update(STEP_MONITORING, user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id=STEP_MONITORING, data_schema=_monitoring_schema(self._options)
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate everything and write the entry."""
        if errors := _validate_lights(self._options):
            return self.async_show_form(
                step_id=STEP_SIMULATION_LIGHTS,
                data_schema=vol.Schema(_lights_schema(self._options)),
                errors=errors,
            )
        if errors := _validate_facades(self._options):
            return self.async_show_form(
                step_id=STEP_FACADES,
                data_schema=_facades_schema(self._options),
                errors=errors,
            )
        return self.async_create_entry(data=self._options)

    async def _async_after(self, step_id: str) -> ConfigFlowResult:
        return await self.async_step_init()


# ----------------------------------------------------------------------
# Night routes
# ----------------------------------------------------------------------
# Dragging to reorder multiple entities needs Home Assistant 2025.8 or later.
_REORDER: dict[str, Any] = (
    {"reorder": True}
    if "reorder"
    in EntitySelectorConfig.__required_keys__ | EntitySelectorConfig.__optional_keys__
    else {}
)
_ROUTE_LIGHT = EntitySelector(EntitySelectorConfig(domain=["light", "switch"]))
_ROUTE_PATH = EntitySelector(
    EntitySelectorConfig(domain=["light", "switch"], multiple=True, **_REORDER)
)
_ROUTE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): TextSelector(),
        vol.Required(CONF_ORIGIN): _ROUTE_LIGHT,
        vol.Optional(CONF_PATH): _ROUTE_PATH,
        vol.Required(CONF_DESTINATION): _ROUTE_LIGHT,
    }
)


def _validate_route(user_input: Mapping[str, Any]) -> dict[str, str]:
    origin = user_input[CONF_ORIGIN]
    destination = user_input[CONF_DESTINATION]
    path = list(user_input.get(CONF_PATH) or [])
    if origin == destination:
        return {CONF_DESTINATION: "same_origin_destination"}
    lights = [origin, *path, destination]
    if len(set(lights)) != len(lights):
        return {CONF_PATH: "duplicate_light"}
    return {}


def _route_data(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_ORIGIN: user_input[CONF_ORIGIN],
        CONF_PATH: list(user_input.get(CONF_PATH) or []),
        CONF_DESTINATION: user_input[CONF_DESTINATION],
    }


class RouteSubentryFlow(ConfigSubentryFlow):
    """Add or edit a night route."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a route."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_route(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=_route_data(user_input)
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _ROUTE_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a route."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_route(user_input)
            if not errors:
                # handler[0] is the entry id; _get_entry() needs Home Assistant 2025.4.
                entry = self.hass.config_entries.async_get_known_entry(self.handler[0])
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=user_input[CONF_NAME],
                    data=_route_data(user_input),
                )
        current = {CONF_NAME: subentry.title, **subentry.data}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _ROUTE_SCHEMA, user_input or current
            ),
            errors=errors,
        )
