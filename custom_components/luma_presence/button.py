"""Buttons: run a night route now, and dream a night without playing it."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import LumaEntity
from .models import LumaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button."""
    async_add_entities([TestSequenceButton(entry), DreamNightButton(entry)])


class TestSequenceButton(LumaEntity, ButtonEntity):
    """Run one random route now, even with the simulation off."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "test_sequence", "button")

    async def async_press(self) -> None:
        """Run a route."""
        self._runtime.sequences.async_run_now()


class DreamNightButton(LumaEntity, ButtonEntity):
    """Generate tonight's Dream plan without executing it."""

    _attr_icon = "mdi:creation"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "dream_night", "button")

    async def async_press(self) -> None:
        """Show a Dream plan for tonight in the plan sensor."""
        runtime = self._runtime
        assert runtime.dream is not None
        plan = await runtime.dream.async_preview(runtime.engine.planning_window())
        if plan is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="dream_not_ready",
                translation_placeholders={
                    "reason": str(runtime.dream.preview_status.get("reason"))
                },
            )
