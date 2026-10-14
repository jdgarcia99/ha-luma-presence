"""Fixtures for LumaPresence tests."""

from __future__ import annotations

import pytest

from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

LIGHTS = ["light.salon", "light.hall", "switch.oficina", "light.bedroom"]
FRONT = ["light.bedroom"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in all tests."""
    return


@pytest.fixture
async def fake_lights(hass: HomeAssistant) -> list[str]:
    """Create lights whose turn_on/turn_off services keep the caller context."""
    await hass.config.async_set_time_zone("Europe/Madrid")
    hass.config.latitude = 40.4
    hass.config.longitude = -3.7
    assert await async_setup_component(hass, "homeassistant", {})

    for entity_id in LIGHTS:
        hass.states.async_set(entity_id, STATE_OFF)

    def _make(state: str):
        async def _handler(call: ServiceCall) -> None:
            ids = call.data["entity_id"]
            for entity_id in [ids] if isinstance(ids, str) else ids:
                hass.states.async_set(entity_id, state, context=call.context)

        return _handler

    for domain in ("light", "switch"):
        hass.services.async_register(domain, "turn_on", _make("on"))
        hass.services.async_register(domain, "turn_off", _make("off"))
    return LIGHTS
