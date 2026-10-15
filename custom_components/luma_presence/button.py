"""Button: run a night route now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import LumaEntity
from .models import LumaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button."""
    async_add_entities([TestSequenceButton(entry)])


class TestSequenceButton(LumaEntity, ButtonEntity):
    """Run one random route now, even with the simulation off."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "test_sequence", "button")

    async def async_press(self) -> None:
        """Run a route."""
        self._runtime.sequences.async_run_now()
