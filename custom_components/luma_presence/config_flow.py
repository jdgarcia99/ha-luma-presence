"""Config and options flow for LumaPresence."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ALARM,
    CONF_FRONT_LIGHTS,
    CONF_HUMAN_ONLY,
    CONF_LIGHTS,
    CONF_MARGIN_AFTER,
    CONF_MARGIN_BEFORE,
    CONF_PERSONS,
    DEFAULT_HUMAN_ONLY,
    DEFAULT_MARGIN_AFTER,
    DEFAULT_MARGIN_BEFORE,
    DEFAULT_NAME,
    DOMAIN,
)

_LIGHT_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["light", "switch"], multiple=True)
)
_HOURS_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0, max=24, step=0.5, unit_of_measurement="h", mode=NumberSelectorMode.BOX
    )
)


def _options_schema(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_LIGHTS, default=defaults.get(CONF_LIGHTS, [])): _LIGHT_SELECTOR,
        vol.Optional(
            CONF_FRONT_LIGHTS, default=defaults.get(CONF_FRONT_LIGHTS, [])
        ): _LIGHT_SELECTOR,
        vol.Optional(CONF_PERSONS, default=defaults.get(CONF_PERSONS, [])): EntitySelector(
            EntitySelectorConfig(domain="person", multiple=True)
        ),
        vol.Optional(
            CONF_ALARM, description={"suggested_value": defaults.get(CONF_ALARM)}
        ): EntitySelector(EntitySelectorConfig(domain="alarm_control_panel")),
        vol.Required(
            CONF_MARGIN_BEFORE,
            default=defaults.get(CONF_MARGIN_BEFORE, DEFAULT_MARGIN_BEFORE),
        ): _HOURS_SELECTOR,
        vol.Required(
            CONF_MARGIN_AFTER,
            default=defaults.get(CONF_MARGIN_AFTER, DEFAULT_MARGIN_AFTER),
        ): _HOURS_SELECTOR,
        vol.Required(
            CONF_HUMAN_ONLY, default=defaults.get(CONF_HUMAN_ONLY, DEFAULT_HUMAN_ONLY)
        ): BooleanSelector(),
    }


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    lights = set(user_input.get(CONF_LIGHTS) or [])
    front = set(user_input.get(CONF_FRONT_LIGHTS) or [])
    if not lights:
        errors[CONF_LIGHTS] = "no_lights"
    elif not front <= lights:
        errors[CONF_FRONT_LIGHTS] = "front_not_subset"
    return errors


class LumaPresenceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First and only step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                options = dict(user_input)
                title = options.pop(CONF_NAME, DEFAULT_NAME)
                return self.async_create_entry(title=title, data={}, options=options)
        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
                ): TextSelector(),
                **_options_schema(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return LumaPresenceOptionsFlow()


class LumaPresenceOptionsFlow(OptionsFlow):
    """Edit lights, people, alarm and learning margins."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)
        defaults = user_input or dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_options_schema(defaults)),
            errors=errors,
        )
