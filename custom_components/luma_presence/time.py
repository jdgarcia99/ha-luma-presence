"""Time entities: Time mode window and start of the night sequences."""

from __future__ import annotations

from contextlib import suppress
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import LumaEntity
from .models import LumaConfigEntry

TIMES = ("start_time", "end_time", "late_end_time", "sequence_start_time")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the time entities."""
    async_add_entities(LumaTime(entry, key) for key in TIMES)


class LumaTime(LumaEntity, TimeEntity, RestoreEntity):
    """Time backed by LumaSettings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: LumaConfigEntry, key: str) -> None:
        """Initialize the time entity."""
        super().__init__(entry, key, "time")

    async def async_added_to_hass(self) -> None:
        """Restore the stored value."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        with suppress(ValueError):
            setattr(self._runtime.settings, self._key, time.fromisoformat(last.state))

    @property
    def native_value(self) -> time:
        """Return the configured time."""
        return getattr(self._runtime.settings, self._key)

    async def async_set_value(self, value: time) -> None:
        """Change the time."""
        setattr(self._runtime.settings, self._key, value)
        await self._runtime.async_setting_changed(self._key)
        self.async_write_ha_state()
