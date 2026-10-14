"""Numbers: light limits and timings."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import LumaEntity
from .models import LumaConfigEntry


@dataclass(frozen=True)
class _NumberDef:
    key: str
    minimum: float
    maximum: float
    step: float
    unit: str | None
    integer: bool


NUMBERS: tuple[_NumberDef, ...] = (
    _NumberDef("min_lights", 0, 20, 1, None, True),
    _NumberDef("max_lights", 0, 20, 1, None, True),
    _NumberDef("min_front_lights", 0, 10, 1, None, True),
    _NumberDef("min_dwell", 1, 120, 1, UnitOfTime.MINUTES, False),
    _NumberDef("max_dwell", 1, 120, 1, UnitOfTime.MINUTES, False),
    _NumberDef("stop_interval", 0, 600, 5, UnitOfTime.SECONDS, False),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the numbers."""
    async_add_entities(LumaNumber(entry, definition) for definition in NUMBERS)


class LumaNumber(LumaEntity, RestoreNumber):
    """Number backed by LumaSettings."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: LumaConfigEntry, definition: _NumberDef) -> None:
        """Initialize the number."""
        super().__init__(entry, definition.key, "number")
        self._def = definition
        self._attr_native_min_value = definition.minimum
        self._attr_native_max_value = definition.maximum
        self._attr_native_step = definition.step
        self._attr_native_unit_of_measurement = definition.unit

    async def async_added_to_hass(self) -> None:
        """Restore the stored value."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._store(last.native_value)

    def _store(self, value: float) -> None:
        setattr(
            self._runtime.settings,
            self._key,
            int(value) if self._def.integer else float(value),
        )

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return getattr(self._runtime.settings, self._key)

    async def async_set_native_value(self, value: float) -> None:
        """Change the value."""
        self._store(value)
        await self._runtime.async_setting_changed(self._key)
        self.async_write_ha_state()
