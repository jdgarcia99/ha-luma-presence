"""Sensors: status, next events, last action, learning progress and light lists."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    CONF_REAR_LIGHTS,
    MODE_DREAM,
    MODE_REPLAY,
    STATUSES,
)
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
            NextSequenceSensor(entry),
            ReplaySourceSensor(entry),
            RealismSensor(entry),
            TonightPlanSensor(entry),
            DreamModelSensor(entry),
            LastActionSensor(entry),
            LearningDaysSensor(entry),
            SimulationLightsSensor(entry),
            LearningLightsSensor(entry),
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


class NextSequenceSensor(LumaEntity, SensorEntity):
    """Next scheduled night route."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "next_sequence", "sensor")

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the next route."""
        return self._runtime.sequences.next_run

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the names of the configured routes."""
        return {"routes": [route.name for route in self._runtime.sequences.routes]}


class ReplaySourceSensor(LumaEntity, SensorEntity):
    """Night that Replay reproduces tonight."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:history"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "replay_source", "sensor")

    def _status(self) -> dict[str, Any] | None:
        if self._runtime.settings.mode != MODE_REPLAY:
            return None
        return self._runtime.replay.status

    @property
    def native_value(self) -> date | None:
        """Return the start date of the source night."""
        status = self._status()
        return status["source"] if status else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why this night was chosen, or why Replay fell back."""
        status = self._status() or {}
        return {
            "reason": status.get("reason"),
            "events": status.get("events", 0),
            "eligible_nights": status.get("eligible_nights", 0),
            "by_room": self._runtime.settings.replay_by_room,
        }


class RealismSensor(LumaEntity, SensorEntity):
    """Realism score of tonight's Dream plan."""

    _attr_icon = "mdi:creation"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "realism", "sensor")

    def _status(self) -> dict[str, Any]:
        dream = self._runtime.dream
        if self._runtime.settings.mode != MODE_DREAM or dream is None:
            return {}
        return dream.status

    @property
    def native_value(self) -> float | None:
        """Return the score (0-100)."""
        return self._status().get("score")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what ran tonight and why."""
        status = self._status()
        return {
            "source": status.get("source"),
            "reason": status.get("reason"),
            "replay_reason": status.get("replay_reason"),
            "candidates": status.get("candidates", 0),
            "similarity_max": status.get("similarity_max"),
            "day_type": status.get("day_type"),
        }


class TonightPlanSensor(LumaEntity, SensorEntity):
    """Tonight's plan (Replay or Dream), or a Dream preview."""

    _attr_icon = "mdi:timeline-clock-outline"
    _unrecorded_attributes = frozenset({"timeline", "rooms", "summary"})

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "tonight_plan", "sensor")

    @property
    def native_value(self) -> int | None:
        """Return the number of transitions inside the window."""
        player = self._runtime.player
        if player is None or player.shown is None:
            return None
        return len(player.shown.in_window())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the compact timeline, rooms and summary."""
        player = self._runtime.player
        plan = player.shown if player else None
        if plan is None:
            return {"preview": False}
        return {
            "night": plan.night.isoformat(),
            "source": plan.source,
            "preview": plan.preview,
            "summary": plan.summary(),
            "rooms": plan.rooms,
            "timeline": plan.compact(),
        }


class DreamModelSensor(LumaEntity, SensorEntity):
    """Valid learned nights in the Dream room model."""

    _attr_icon = "mdi:home-analytics"
    _unrecorded_attributes = frozenset({"weekday", "weekend"})

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "model", "sensor")

    @property
    def native_value(self) -> int | None:
        """Return the valid nights used by the model."""
        dream = self._runtime.dream
        if dream is None or dream.model is None:
            return None
        return sum(len(m.nights) for m in dream.model.day_types.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the model summary per day type."""
        dream = self._runtime.dream
        return dream.summary() if dream else {}


class LastActionSensor(LumaEntity, SensorEntity):
    """Last action performed by the simulation."""

    _attr_icon = "mdi:history"

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "last_action", "sensor")

    @property
    def native_value(self) -> str | None:
        """Return a short description of the last action."""
        action = self._runtime.controller.last_action
        if action is None:
            return None
        if action["entity_id"]:
            return f"{action['action']}: {action['entity_id']}"
        return action["action"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw action."""
        return dict(self._runtime.controller.last_action or {})


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


class SimulationLightsSensor(LumaEntity, SensorEntity):
    """Lights the simulation may control, split by facade."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lightbulb-group"
    _unrecorded_attributes = frozenset({"front", "rear", "interior"})

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "simulation_lights", "sensor")

    @property
    def native_value(self) -> int:
        """Return the number of simulation lights."""
        return len(self._runtime.config.get(CONF_LIGHTS, []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the lights of each facade."""
        config = self._runtime.config
        front = list(config.get(CONF_FRONT_LIGHTS, []))
        rear = list(config.get(CONF_REAR_LIGHTS, []))
        interior = [
            e for e in config.get(CONF_LIGHTS, []) if e not in front and e not in rear
        ]
        return {"front": front, "rear": rear, "interior": interior}


class LearningLightsSensor(LumaEntity, SensorEntity):
    """Entities whose real usage is learned."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lightbulb-auto"
    _unrecorded_attributes = frozenset({"entities", "auto_excluded"})

    def __init__(self, entry: LumaConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "learning_lights", "sensor")

    @property
    def native_value(self) -> int:
        """Return the number of learned entities."""
        return len(self._runtime.learning.entities)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the learned entities and those filtered out automatically."""
        learning = self._runtime.learning
        return {
            "entities": list(learning.entities),
            "auto_excluded": dict(learning.auto_excluded),
        }
