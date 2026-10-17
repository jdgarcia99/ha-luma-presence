"""Where LumaPresence uses each entity, and replacing or removing one.

Entities are referenced from the options, the night routes and deterrence
triggers (config subentries) and the learned data. This module is used by:

* the missing-entity Repairs issues: one fixable issue per configured
  entity that no longer exists, to replace it or remove it everywhere;
* the entity registry listener: when an entity ID is renamed, LumaPresence
  follows it automatically.

A change is applied in one go: the learned data first (the reload saves
it), then the subentries and the options, with a single reload at the end.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from .config_flow import (
    _validate_facades,
    _validate_lights,
    _validate_route,
    _validate_trigger,
)
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_ALARM,
    CONF_ALARM_CONTROL,
    CONF_AMBIENCE_MEDIA,
    CONF_AMBIENCE_PLAYERS,
    CONF_COVERS,
    CONF_DARKNESS_SENSOR,
    CONF_DESTINATION,
    CONF_EVENT_TYPES,
    CONF_FRONT_LIGHTS,
    CONF_HOLIDAY_CALENDAR,
    CONF_LEARN_EXCLUDE,
    CONF_LEARN_EXTRA,
    CONF_LIGHTS,
    CONF_ORIGIN,
    CONF_PATH,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    CONF_TRIGGER_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_WORKDAY_SENSOR,
    DOMAIN,
    EVENT_ENTITY_RENAMED,
    SUBENTRY_DETERRENCE,
    SUBENTRY_ROUTE,
    TRIGGER_DEVICE_CLASSES,
)
from .learning import async_rename_in_store, count_entity_events

_LOGGER = logging.getLogger(__name__)

# Options holding a list of entity IDs, and a single entity ID.
LIST_OPTIONS = (
    CONF_LIGHTS,
    CONF_FRONT_LIGHTS,
    CONF_REAR_LIGHTS,
    CONF_LEARN_EXTRA,
    CONF_LEARN_EXCLUDE,
    CONF_PERSONS,
    CONF_COVERS,
    CONF_AMBIENCE_PLAYERS,
)
SINGLE_OPTIONS = (
    CONF_ALARM,
    CONF_DARKNESS_SENSOR,
    CONF_WORKDAY_SENSOR,
    CONF_HOLIDAY_CALENDAR,
    CONF_WEATHER_ENTITY,
    CONF_AI_TASK_ENTITY,
)

# Uses in the subentries (the options use their own keys).
USAGE_ROUTE_ORIGIN = "route_origin"
USAGE_ROUTE_PATH = "route_path"
USAGE_ROUTE_DESTINATION = "route_destination"
USAGE_TRIGGER = "trigger"
USAGE_TRIGGER_PATH = "trigger_path"
LIGHT_USAGES = frozenset(
    {
        CONF_LIGHTS,
        CONF_FRONT_LIGHTS,
        CONF_REAR_LIGHTS,
        CONF_LEARN_EXTRA,
        CONF_LEARN_EXCLUDE,
        USAGE_ROUTE_ORIGIN,
        USAGE_ROUTE_PATH,
        USAGE_ROUTE_DESTINATION,
        USAGE_TRIGGER_PATH,
    }
)

# One issue per missing entity (one issue for all of them before v0.9.1).
LEGACY_MISSING_ISSUE = "missing_entities_{entry_id}"
MISSING_ISSUE_PREFIX = "missing_entity_{entry_id}_"

# Entries whose update listener must not reload: a change is being applied.
_APPLYING: set[str] = set()


@dataclass(frozen=True)
class Usage:
    """One place where an entity is used."""

    key: str  # an option key or one of the USAGE_* keys
    name: str | None = None  # the route or deterrence trigger


@dataclass
class Replacement:
    """The options and subentries after replacing (or removing) an entity."""

    options: dict[str, Any]
    # subentry_id -> (title, data), or None to remove the subentry
    subentries: dict[str, tuple[str, dict[str, Any]] | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def entity_usages(entry: ConfigEntry) -> dict[str, list[Usage]]:
    """Every entity in the options and subentries, and where it is used."""
    usages: dict[str, list[Usage]] = {}

    def add(entity_id: Any, usage: Usage) -> None:
        if entity_id:
            usages.setdefault(str(entity_id), []).append(usage)

    options = entry.options
    for key in LIST_OPTIONS:
        for entity_id in options.get(key) or []:
            add(entity_id, Usage(key))
    for key in SINGLE_OPTIONS:
        add(options.get(key), Usage(key))
    for subentry in entry.subentries.values():
        data, name = subentry.data, subentry.title
        if subentry.subentry_type == SUBENTRY_ROUTE:
            add(data.get(CONF_ORIGIN), Usage(USAGE_ROUTE_ORIGIN, name))
            for light in data.get(CONF_PATH) or []:
                add(light, Usage(USAGE_ROUTE_PATH, name))
            add(data.get(CONF_DESTINATION), Usage(USAGE_ROUTE_DESTINATION, name))
        elif subentry.subentry_type == SUBENTRY_DETERRENCE:
            add(data.get(CONF_TRIGGER_ENTITY), Usage(USAGE_TRIGGER, name))
            for light in data.get(CONF_PATH) or []:
                add(light, Usage(USAGE_TRIGGER_PATH, name))
    return usages


def replacement_filter(entity_id: str, usages: Iterable[Usage]) -> list[dict[str, Any]]:
    """Entities that may replace `entity_id`: the same family as its uses."""
    keys = {usage.key for usage in usages}
    if USAGE_TRIGGER in keys:
        return [
            {"domain": "binary_sensor", "device_class": TRIGGER_DEVICE_CLASSES},
            {"domain": "event"},
        ]
    if keys & LIGHT_USAGES:
        return [{"domain": ["light", "switch"]}]
    if CONF_DARKNESS_SENSOR in keys:
        return [{"domain": "sensor", "device_class": "illuminance"}]
    if CONF_WORKDAY_SENSOR in keys:
        return [{"domain": "binary_sensor", "integration": "workday"}]
    return [{"domain": split_entity_id(entity_id)[0]}]


def replacement_domains(entity_id: str, usages: Iterable[Usage]) -> set[str]:
    """Domains allowed by replacement_filter()."""
    domains: set[str] = set()
    for item in replacement_filter(entity_id, usages):
        domain = item["domain"]
        domains.update([domain] if isinstance(domain, str) else domain)
    return domains


def _swap(values: Iterable[str], old: str, new: str | None) -> list[str]:
    """Replace (or drop) `old` in a list, keeping the order and no duplicates."""
    swapped = [new if value == old else value for value in values]
    return list(dict.fromkeys(value for value in swapped if value is not None))


def _swap_media(text: str, old: str, new: str | None) -> str:
    """Rename (or drop) the "media_player.x = media" line of a player."""
    lines = []
    for line in text.splitlines():
        player, sep, content = line.partition("=")
        if sep and player.strip() == old:
            if new is None:
                continue
            line = f"{new} = {content.strip()}"
        lines.append(line)
    return "\n".join(lines)


def plan_replacement(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old: str,
    new: str | None,
    validate: bool = True,
) -> Replacement:
    """Options and subentries with `old` replaced by `new` (None: removed).

    Removing the origin or destination of a route, or the entity of a
    deterrence trigger, removes that route or trigger.
    """
    options = dict(entry.options)
    for key in LIST_OPTIONS:
        if old in (options.get(key) or []):
            options[key] = _swap(options[key], old, new)
    for key in SINGLE_OPTIONS:
        if options.get(key) == old:
            if new is not None:
                options[key] = new
            else:
                options.pop(key)
                if key == CONF_ALARM:
                    options[CONF_ALARM_CONTROL] = False
    if media := options.get(CONF_AMBIENCE_MEDIA):
        options[CONF_AMBIENCE_MEDIA] = _swap_media(str(media), old, new)
    plan = Replacement(options=options)

    for subentry in entry.subentries.values():
        data, title = dict(subentry.data), subentry.title
        path = list(data.get(CONF_PATH) or [])
        if subentry.subentry_type == SUBENTRY_ROUTE:
            ends = (CONF_ORIGIN, CONF_DESTINATION)
            if new is None and any(data.get(key) == old for key in ends):
                plan.subentries[subentry.subentry_id] = None
                continue
            for key in ends:
                if data.get(key) == old:
                    data[key] = new
            if old in path:
                data[CONF_PATH] = _swap(path, old, new)
            if validate and data != subentry.data and _validate_route(data):
                plan.errors = {"base": "replacement_conflict"}
        elif subentry.subentry_type == SUBENTRY_DETERRENCE:
            if data.get(CONF_TRIGGER_ENTITY) == old:
                if new is None:
                    plan.subentries[subentry.subentry_id] = None
                    continue
                data[CONF_TRIGGER_ENTITY] = new
                if not new.startswith("event."):
                    data[CONF_EVENT_TYPES] = []
                if title == old:
                    title = new
            if old in path:
                data[CONF_PATH] = _swap(path, old, new)
            if validate and data != subentry.data and _validate_trigger(hass, data):
                plan.errors = {"base": "replacement_conflict"}
        else:
            continue
        if data != subentry.data or title != subentry.title:
            plan.subentries[subentry.subentry_id] = (title, data)

    if validate and not plan.errors:
        if _validate_lights(options):
            plan.errors = {"base": "remove_last_light"}
        elif _validate_facades(options):
            plan.errors = {"base": "replacement_conflict"}
    return plan


def removal_consequences(
    entry: ConfigEntry, entity_id: str, plan: Replacement
) -> list[tuple[str, str]]:
    """What removing an entity deletes or turns off, beyond the entity itself.

    (message key, route or trigger name): the routes and deterrence triggers
    that are deleted, and alarm control if it is turned off.
    """
    consequences = []
    for subentry_id, change in plan.subentries.items():
        if change is None:
            subentry = entry.subentries[subentry_id]
            key = (
                "removes_route"
                if subentry.subentry_type == SUBENTRY_ROUTE
                else "removes_trigger"
            )
            consequences.append((key, subentry.title))
    if entry.options.get(CONF_ALARM) == entity_id and entry.options.get(CONF_ALARM_CONTROL):
        consequences.append(("removes_alarm_control", ""))
    return consequences


def reload_suppressed(entry_id: str) -> bool:
    """True while a replacement is being written (it reloads once at the end)."""
    return entry_id in _APPLYING


@callback
def async_apply(hass: HomeAssistant, entry: ConfigEntry, plan: Replacement) -> bool:
    """Write the subentries and the options, then reload the entry once."""
    changed = False
    _APPLYING.add(entry.entry_id)
    try:
        for subentry_id, change in plan.subentries.items():
            if change is None:
                changed |= hass.config_entries.async_remove_subentry(entry, subentry_id)
            else:
                title, data = change
                changed |= hass.config_entries.async_update_subentry(
                    entry, entry.subentries[subentry_id], title=title, data=data
                )
        changed |= hass.config_entries.async_update_entry(entry, options=plan.options)
    finally:
        _APPLYING.discard(entry.entry_id)
    if changed and entry.state is ConfigEntryState.LOADED:
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return changed


def _runtime(entry: ConfigEntry) -> Any:
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return getattr(entry, "runtime_data", None)


def learned_event_count(entry: ConfigEntry, entity_id: str) -> int:
    """Learned events of an entity (0 if the entry is not loaded)."""
    if (runtime := _runtime(entry)) is None:
        return 0
    return count_entity_events(runtime.learning.days, entity_id)


async def async_rename_learned(
    hass: HomeAssistant, entry: ConfigEntry, old: str, new: str
) -> int:
    """Move the learned events (and the light ownership) of `old` to `new`."""
    if (runtime := _runtime(entry)) is not None:
        runtime.controller.async_rename(old, new)
        return runtime.learning.async_rename_entity(old, new)
    return await async_rename_in_store(hass, entry.entry_id, old, new)


async def async_replace(
    hass: HomeAssistant, entry: ConfigEntry, old: str, new: str | None, plan: Replacement
) -> int:
    """Apply a replacement (or removal); return the learned events moved.

    Removing an entity keeps its learned events: they still describe how
    the house was used, and Replay and Dream map them by room.
    """
    learned = await async_rename_learned(hass, entry, old, new) if new else 0
    async_apply(hass, entry, plan)
    return learned


async def async_follow_rename(
    hass: HomeAssistant, entry: ConfigEntry, old: str, new: str
) -> None:
    """An entity ID was renamed in Home Assistant: follow it everywhere."""
    plan = plan_replacement(hass, entry, old, new, validate=False)
    changed = bool(plan.subentries) or plan.options != dict(entry.options)
    learned = await async_replace(hass, entry, old, new, plan)
    if changed or learned:
        _LOGGER.info(
            "%s: followed the rename of %s to %s (%s learned events)",
            entry.title,
            old,
            new,
            learned,
        )
        hass.bus.async_fire(
            EVENT_ENTITY_RENAMED,
            {"entry_id": entry.entry_id, "old_entity_id": old, "new_entity_id": new},
        )


@callback
def async_watch_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Check the configured entities now, and again when a missing one appears.

    A missing entity can come back: a device back online, or a new device
    whose entity is given the old entity ID (the recommended way to replace
    a device). Its issue then disappears without a restart.
    """
    if not (missing := async_check_entities(hass, entry)):
        return

    @callback
    def _appeared(event: Event[EventStateChangedData]) -> None:
        if event.data["new_state"] is not None:
            async_check_entities(hass, entry)

    entry.async_on_unload(async_track_state_change_event(hass, missing, _appeared))


@callback
def async_check_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """One fixable Repairs issue per configured entity that no longer exists."""
    missing = [
        entity_id for entity_id in entity_usages(entry) if hass.states.get(entity_id) is None
    ]
    ir.async_delete_issue(hass, DOMAIN, LEGACY_MISSING_ISSUE.format(entry_id=entry.entry_id))
    prefix = MISSING_ISSUE_PREFIX.format(entry_id=entry.entry_id)
    for entity_id in missing:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{prefix}{entity_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="missing_entity",
            translation_placeholders={"title": entry.title, "entity_id": entity_id},
            data={"entry_id": entry.entry_id, "entity_id": entity_id},
        )
    async_delete_missing_issues(hass, entry.entry_id, keep=missing)
    return missing


@callback
def async_delete_missing_issues(
    hass: HomeAssistant, entry_id: str, keep: Iterable[str] = ()
) -> None:
    """Delete the missing-entity issues of an entry, except those in `keep`."""
    prefix = MISSING_ISSUE_PREFIX.format(entry_id=entry_id)
    keep = set(keep)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(prefix)
            and issue_id.removeprefix(prefix) not in keep
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    if not keep:
        ir.async_delete_issue(hass, DOMAIN, LEGACY_MISSING_ISSUE.format(entry_id=entry_id))
