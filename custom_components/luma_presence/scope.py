"""Learning scope: which entities LumaPresence learns from.

Room lights are not only ``light.*`` entities: many real lamps are relays
exposed as ``switch.*``, and many ``light.*`` entities are not room lights at
all (status LEDs, appliance lights). The scope therefore starts from the
``light.*`` entities of the indoor areas, filters out anything that does not
look like a real lamp, and lets the owner add or remove entities by hand.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .const import (
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
)


@dataclass
class LearningScope:
    """Resolved learning scope."""

    entities: list[str] = field(default_factory=list)
    # Indoor lights dropped because they are disabled, hidden or not a lamp.
    auto_excluded: list[str] = field(default_factory=list)


def entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the area of an entity, falling back to the area of its device."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    return _entry_area_id(dr.async_get(hass), entry)


def _entry_area_id(
    devices: dr.DeviceRegistry, entry: er.RegistryEntry
) -> str | None:
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := devices.async_get(entry.device_id)) is not None:
        return device.area_id
    return None


def _indoor_areas(hass: HomeAssistant, options: Mapping[str, Any]) -> set[str]:
    floors = set(options.get(CONF_LEARN_FLOORS) or [])
    areas = set(options.get(CONF_LEARN_AREAS) or [])
    if floors:
        areas.update(
            area.id
            for area in ar.async_get(hass).async_list_areas()
            if area.floor_id in floors
        )
    return areas


def resolve_learning_scope(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> LearningScope:
    """Resolve the entities to learn from, and those filtered out automatically."""
    indoor = _indoor_areas(hass, options)
    devices = dr.async_get(hass)
    candidates: set[str] = set()
    dropped: set[str] = set()
    if indoor:
        for entry in er.async_get(hass).entities.values():
            if entry.domain != "light" or _entry_area_id(devices, entry) not in indoor:
                continue
            if entry.disabled or entry.hidden or entry.entity_category is not None:
                dropped.add(entry.entity_id)
            else:
                candidates.add(entry.entity_id)

    selected = (
        candidates
        | set(options.get(CONF_LEARN_EXTRA) or [])
        | set(options.get(CONF_LIGHTS) or [])
    )
    selected -= set(options.get(CONF_LEARN_EXCLUDE) or [])
    return LearningScope(
        entities=sorted(selected), auto_excluded=sorted(dropped - selected)
    )


def resolve_learning_entities(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> list[str]:
    """Return the sorted list of entities to learn from."""
    return resolve_learning_scope(hass, options).entities
