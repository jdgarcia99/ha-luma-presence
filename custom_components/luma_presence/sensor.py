"""Sensors: status, next event, last action and learning progress."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATUSES
from .entity import LumaEntity
from .models import LumaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    async_add_entities(
        [
            StatusSensor(entry),
            NextEventSensor(entry),
            LastActionSensor(entry),
            LearningDaysSensor(entry),
        ]
    )


class StatusSensor(LumaEntity, SensorEntity):
    """Engine status."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATUSES)

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "status", "sensor")

    @property
    def native_value(self) -> str:
        """Return the status."""
        return self._runtime.engine.status


class NextEventSensor(LumaEntity, SensorEntity):
    """Next scheduled start or end of the simulation."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "next_event", "sensor")

    @property
    def native_value(self) -> datetime | None:
        """Return the next event time."""
        return self._runtime.engine.next_event

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the kind of the next event."""
        return {"kind": self._runtime.engine.next_event_kind}


class LastActionSensor(LumaEntity, SensorEntity):
    """Last action performed by the simulation."""

    _attr_icon = "mdi:history"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "last_action", "sensor")

    @property
    def native_value(self) -> str | None:
        """Return a short description of the last action."""
        action = self._runtime.engine.last_action
        if action is None:
            return None
        if action["entity_id"]:
            return f"{action['action']}: {action['entity_id']}"
        return action["action"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw action."""
        return dict(self._runtime.engine.last_action or {})


class LearningDaysSensor(LumaEntity, SensorEntity):
    """Number of complete days learned."""

    _attr_icon = "mdi:calendar-check"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "learning_days", "sensor")

    @property
    def native_value(self) -> int:
        """Return the number of valid learning days."""
        return self._runtime.learning.stats()["valid_days"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the learning summary."""
        return self._runtime.learning.stats()
