"""Switches: master simulation switch, night sequences and boolean settings."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import LumaEntity
from .models import LumaConfigEntry

# (entity key, entity category, setting name when it differs from the key)
SETTING_SWITCHES: tuple[tuple[str, EntityCategory | None, str | None], ...] = (
    ("late_arrival", EntityCategory.CONFIG, None),
    ("delayed_start", EntityCategory.CONFIG, None),
    ("delayed_end", EntityCategory.CONFIG, None),
    ("learning_paused", None, None),
    ("sequences", EntityCategory.CONFIG, "sequences_enabled"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches."""
    entities: list[SwitchEntity] = [LumaMasterSwitch(entry)]
    entities.extend(
        LumaSettingSwitch(entry, key, category, setting)
        for key, category, setting in SETTING_SWITCHES
    )
    async_add_entities(entities)


class LumaMasterSwitch(LumaEntity, SwitchEntity, RestoreEntity):
    """Master switch: enables or disables the presence simulation."""

    _attr_icon = "mdi:home-lightbulb-outline"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the master switch."""
        super().__init__(entry, "simulation", "switch")

    async def async_added_to_hass(self) -> None:
        """Restore the previous state so a restart resumes the simulation."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._runtime.engine.restore_enabled(last.state == STATE_ON)

    @property
    def is_on(self) -> bool:
        """Return True if the simulation is enabled."""
        return self._runtime.engine.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the lights currently owned by the simulation."""
        return {"owned_lights": self._runtime.engine.owned_lights}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the simulation."""
        await self._runtime.engine.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the simulation (lights handled per the stop selector)."""
        await self._runtime.engine.async_set_enabled(False)


class LumaSettingSwitch(LumaEntity, SwitchEntity, RestoreEntity):
    """Boolean setting stored in LumaSettings."""

    def __init__(
        self,
        entry: LumaConfigEntry,
        key: str,
        category: EntityCategory | None,
        setting: str | None = None,
    ) -> None:
        """Initialize the setting switch."""
        super().__init__(entry, key, "switch")
        self._attr_entity_category = category
        self._setting = setting or key

    async def async_added_to_hass(self) -> None:
        """Restore the stored value."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state in (
            "on",
            "off",
        ):
            setattr(self._runtime.settings, self._setting, last.state == STATE_ON)

    @property
    def is_on(self) -> bool:
        """Return the setting value."""
        return bool(getattr(self._runtime.settings, self._setting))

    async def _async_set(self, value: bool) -> None:
        setattr(self._runtime.settings, self._setting, value)
        await self._runtime.async_setting_changed(self._setting)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(False)
