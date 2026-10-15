"""Config and options flow for LumaPresence.

The initial setup walks through the steps in sequence. The options flow
shows them as a menu: every step edits a working copy of the options and
returns to the menu, and only Save writes the entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
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
    TextSelector,
)

from .const import (
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
    DEFAULT_HUMAN_ONLY,
    DEFAULT_MARGIN_AFTER,
    DEFAULT_MARGIN_BEFORE,
    DEFAULT_NAME,
    DOMAIN,
    PREVIEW_MAX_ENTITIES,
)
from .scope import entity_area_id, resolve_learning_entities

STEP_SIMULATION_LIGHTS = "simulation_lights"
STEP_FACADES = "facades"
STEP_LEARNING = "learning"
STEP_LEARNING_PREVIEW = "learning_preview"
STEP_PRESENCE = "presence"
STEP_SAVE = "save"
MENU_OPTIONS = [
    STEP_SIMULATION_LIGHTS,
    STEP_FACADES,
    STEP_LEARNING,
    STEP_LEARNING_PREVIEW,
    STEP_PRESENCE,
    STEP_SAVE,
]

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
    STEP_PRESENCE: {CONF_PERSONS: [], CONF_ALARM: None},
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
        CONF_PERSONS: [],
        CONF_MARGIN_BEFORE: DEFAULT_MARGIN_BEFORE,
        CONF_MARGIN_AFTER: DEFAULT_MARGIN_AFTER,
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
                CONF_MARGIN_BEFORE,
                default=options.get(CONF_MARGIN_BEFORE, DEFAULT_MARGIN_BEFORE),
            ): _HOURS_SELECTOR,
            vol.Required(
                CONF_MARGIN_AFTER,
                default=options.get(CONF_MARGIN_AFTER, DEFAULT_MARGIN_AFTER),
            ): _HOURS_SELECTOR,
        }
    )


def _validate_lights(options: Mapping[str, Any]) -> dict[str, str]:
    if not options.get(CONF_LIGHTS):
        return {CONF_LIGHTS: "no_lights"}
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
    """Markdown list of the entities that will be learned."""
    entities = resolve_learning_entities(hass, options)
    areas = ar.async_get(hass)
    registry = er.async_get(hass)
    lines = []
    for entity_id in entities[:PREVIEW_MAX_ENTITIES]:
        state = hass.states.get(entity_id)
        entry = registry.async_get(entity_id)
        name = (
            (state.name if state else None)
            or (entry.name or entry.original_name if entry else None)
            or entity_id
        )
        line = f"- {name} (`{entity_id}`)"
        if (area_id := entity_area_id(hass, entity_id)) and (
            area := areas.async_get_area(area_id)
        ):
            line += f" · {area.name}"
        lines.append(line)
    if len(entities) > PREVIEW_MAX_ENTITIES:
        lines.append("- …")
    return {
        "count": str(len(entities)),
        "shown": str(min(len(entities), PREVIEW_MAX_ENTITIES)),
        "entities": "\n".join(lines) or "-",
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
        """People, alarm panel and learning margins."""
        if user_input is not None:
            self._update(STEP_PRESENCE, user_input)
            return await self._async_after(STEP_PRESENCE)
        return self.async_show_form(
            step_id=STEP_PRESENCE, data_schema=_presence_schema(self._options)
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
