"""Learning scope: which entities LumaPresence learns from.

Room lights are not only ``light.*`` entities: many real lamps are relays
exposed as ``switch.*``, and many ``light.*`` entities are not room lights at
all (status LEDs, appliance lights). The scope therefore starts from the
``light.*`` entities of the indoor areas, filters out anything that does not
look like a real lamp, and lets the owner add or remove entities by hand.

Smart plugs and relays often expose their status LED as a ``light`` on the
same device as the plug's ``switch``; those indicator LEDs are left out too.
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
    CONF_AUTO_EXCLUDE_INDICATORS,
    CONF_LEARN_AREAS,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LEARN_FLOORS,
    CONF_LIGHTS,
    DEFAULT_AUTO_EXCLUDE_INDICATORS,
    EXCLUDED_CATEGORY,
    EXCLUDED_DISABLED,
    EXCLUDED_HIDDEN,
    EXCLUDED_INDICATOR,
)


@dataclass
class LearningScope:
    """Resolved learning scope."""

    entities: list[str] = field(default_factory=list)
    # Indoor lights left out automatically, and why (EXCLUDED_* reasons).
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def auto_excluded(self) -> list[str]:
        """Indoor lights left out automatically."""
        return sorted(self.reasons)


def _exclusion_reason(
    entry: er.RegistryEntry, switch_devices: set[str], indicators: bool
) -> str | None:
    if entry.disabled:
        return EXCLUDED_DISABLED
    if entry.hidden:
        return EXCLUDED_HIDDEN
    if entry.entity_category is not None:
        return EXCLUDED_CATEGORY
    if indicators and entry.device_id in switch_devices:
        return EXCLUDED_INDICATOR
    return None


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
    entities = er.async_get(hass).entities.values()
    indicators = bool(
        options.get(CONF_AUTO_EXCLUDE_INDICATORS, DEFAULT_AUTO_EXCLUDE_INDICATORS)
    )
    # Devices with a switch: a light on them is the switch's status LED.
    switch_devices = {
        entry.device_id
        for entry in entities
        if entry.domain == "switch" and entry.device_id
    }
    candidates: set[str] = set()
    dropped: dict[str, str] = {}
    if indoor:
        for entry in entities:
            if entry.domain != "light" or _entry_area_id(devices, entry) not in indoor:
                continue
            if reason := _exclusion_reason(entry, switch_devices, indicators):
                dropped[entry.entity_id] = reason
            else:
                candidates.add(entry.entity_id)

    # Extras and simulation lights always win over the automatic exclusions.
    selected = (
        candidates
        | set(options.get(CONF_LEARN_EXTRA) or [])
        | set(options.get(CONF_LIGHTS) or [])
    )
    selected -= set(options.get(CONF_LEARN_EXCLUDE) or [])
    return LearningScope(
        entities=sorted(selected),
        reasons={
            entity_id: dropped[entity_id]
            for entity_id in sorted(dropped)
            if entity_id not in selected
        },
    )


def resolve_learning_entities(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> list[str]:
    """Return the sorted list of entities to learn from."""
    return resolve_learning_scope(hass, options).entities
