"""Base entity for LumaPresence."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VERSION, signal_update
from .models import LumaConfigEntry


class LumaEntity(Entity):
    """Common base: one device per entry, refreshed through the dispatcher."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: LumaConfigEntry, key: str, platform: str) -> None:
        """Initialize the entity."""
        self._runtime = entry.runtime_data
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self.entity_id = f"{platform}.{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="jdgarcia99",
            model="LumaPresence",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to engine and learning updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._runtime.entry_id),
                self.async_write_ha_state,
            )
        )
