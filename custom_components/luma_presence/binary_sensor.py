"""Binary sensors: simulation running, night sequence running and learning active."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATUS_SIMULATING
from .entity import LumaEntity
from .models import LumaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    async_add_entities(
        [SimulatingSensor(entry), SequenceRunningSensor(entry), LearningSensor(entry)]
    )


class SimulatingSensor(LumaEntity, BinarySensorEntity):
    """On while the simulation loop is running."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "simulating", "binary_sensor")

    @property
    def is_on(self) -> bool:
        """Return True while simulating."""
        return self._runtime.engine.status == STATUS_SIMULATING


class SequenceRunningSensor(LumaEntity, BinarySensorEntity):
    """On while a night route runs."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:walk"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "sequence_running", "binary_sensor")

    @property
    def is_on(self) -> bool:
        """Return True while a route runs."""
        return self._runtime.sequences.running

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the running route and its current step."""
        sequences = self._runtime.sequences
        return {
            "route": sequences.route.name if sequences.route else None,
            "step": sequences.step,
        }


class LearningSensor(LumaEntity, BinarySensorEntity):
    """On while real activity is being learned."""

    _attr_icon = "mdi:brain"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "learning_active", "binary_sensor")

    @property
    def is_on(self) -> bool:
        """Return True while the learning gate is open."""
        return self._runtime.learning.is_open

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain why learning is paused."""
        learning = self._runtime.learning
        blocked = learning.blocked_until
        return {
            "paused_reasons": sorted(learning.reasons),
            "blocked_until": blocked.isoformat() if blocked else None,
        }
