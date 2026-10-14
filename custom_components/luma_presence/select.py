"""Selects: simulation mode and stop behaviour."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import MODES, STOP_BEHAVIORS
from .entity import LumaEntity
from .models import LumaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects."""
    async_add_entities(
        [
            LumaSelect(entry, "mode", MODES, None),
            LumaSelect(entry, "stop_behavior", STOP_BEHAVIORS, EntityCategory.CONFIG),
        ]
    )


class LumaSelect(LumaEntity, SelectEntity, RestoreEntity):
    """Select backed by LumaSettings."""

    def __init__(
        self,
        entry: LumaConfigEntry,
        key: str,
        options: list[str],
        category: EntityCategory | None,
    ) -> None:
        """Initialize the select."""
        super().__init__(entry, key, "select")
        self._attr_options = list(options)
        self._attr_entity_category = category

    async def async_added_to_hass(self) -> None:
        """Restore the stored option."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state in self.options:
            setattr(self._runtime.settings, self._key, last.state)

    @property
    def current_option(self) -> str:
        """Return the selected option."""
        return getattr(self._runtime.settings, self._key)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        setattr(self._runtime.settings, self._key, option)
        await self._runtime.async_setting_changed(self._key)
        self.async_write_ha_state()
