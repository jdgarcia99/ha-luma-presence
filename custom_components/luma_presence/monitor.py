"""Repair issues for lights that stay unavailable.

Every simulation light and every route light is watched. When one stays
``unavailable`` or ``unknown`` for the configured number of minutes, a Repairs
issue is raised and an event is fired; both are cleared as soon as the light
reports any other state. After a restart the clock starts at startup.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_LIGHT_AVAILABLE, EVENT_LIGHT_UNAVAILABLE

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (STATE_UNAVAILABLE, STATE_UNKNOWN)
ROLE_SIMULATION = "simulation"
ROLE_ROUTE = "route"
ROLE_REPLAY = "replay"
_DEFAULT_LABELS = {
    ROLE_SIMULATION: "simulation light",
    ROLE_ROUTE: "route",
    ROLE_REPLAY: "replay",
}


def issue_prefix(entry_id: str) -> str:
    """Prefix of the issue ids of one config entry."""
    return f"light_unavailable_{entry_id}_"


@callback
def async_delete_issues(
    hass: HomeAssistant, entry_id: str, keep: set[str] | None = None
) -> None:
    """Delete the unavailable-light issues of an entry, except those in `keep`."""
    prefix = issue_prefix(entry_id)
    keep = keep or set()
    for domain, issue_id in list(ir.async_get(hass).issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(prefix)
            and issue_id.removeprefix(prefix) not in keep
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


class AvailabilityMonitor:
    """Raises a Repairs issue for each light that stays unavailable."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        minutes: float,
        roles: dict[str, list[tuple[str, str | None]]],
    ) -> None:
        """Initialize the monitor.

        `roles` maps each light to its roles: (ROLE_SIMULATION, None),
        (ROLE_ROUTE, route name) or (ROLE_REPLAY, None).
        """
        self.hass = hass
        self._entry_id = entry_id
        self._threshold = timedelta(minutes=max(minutes, 0))
        self._roles = roles
        self._labels = dict(_DEFAULT_LABELS)
        self._since: dict[str, datetime] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._raised: set[str] = set()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._started = False

    @property
    def enabled(self) -> bool:
        """Return False when the alert time is 0."""
        return self._threshold > timedelta(0)

    @property
    def unavailable_since(self) -> dict[str, datetime]:
        """Lights currently unavailable, and since when."""
        return dict(self._since)

    async def async_start(self) -> None:
        """Start watching the lights once Home Assistant is running."""
        if not self.enabled:
            async_delete_issues(self.hass, self._entry_id)
            return
        await self._async_load_labels()
        registry = ir.async_get(self.hass)
        prefix = issue_prefix(self._entry_id)
        now = dt_util.utcnow()
        unavailable: set[str] = set()
        for entity_id in self._roles:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state in _UNAVAILABLE:
                unavailable.add(entity_id)
                # An issue raised before the restart stays until the light is back.
                if registry.async_get_issue(DOMAIN, prefix + entity_id) is not None:
                    self._raised.add(entity_id)
                self._start_clock(entity_id, now)
        async_delete_issues(self.hass, self._entry_id, keep=unavailable)
        self._started = True
        self._track()

    @callback
    def async_set_role_lights(self, role: str, lights: list[str]) -> None:
        """Replace the lights that have a role without a name (e.g. Replay)."""
        for entity_id in list(self._roles):
            kept = [item for item in self._roles[entity_id] if item[0] != role]
            if kept:
                self._roles[entity_id] = kept
            else:
                del self._roles[entity_id]
                self._forget(entity_id)
        for entity_id in lights:
            self._roles.setdefault(entity_id, []).append((role, None))
        if not self._started:
            return
        now = dt_util.utcnow()
        for entity_id in lights:
            state = self.hass.states.get(entity_id)
            if (
                entity_id not in self._since
                and state is not None
                and state.state in _UNAVAILABLE
            ):
                self._start_clock(entity_id, now)
        self._track()

    async def async_shutdown(self) -> None:
        """Stop watching; issues stay until the light is back."""
        self._started = False
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        for unsub in self._timers.values():
            unsub()
        self._timers.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _async_load_labels(self) -> None:
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        for role in _DEFAULT_LABELS:
            key = f"component.{DOMAIN}.selector.light_role.options.{role}"
            if label := translations.get(key):
                self._labels[role] = label

    @callback
    def _track(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._roles:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._roles), self._handle_state
            )

    @callback
    def _forget(self, entity_id: str) -> None:
        """Stop watching a light that no longer has any role."""
        if unsub := self._timers.pop(entity_id, None):
            unsub()
        self._since.pop(entity_id, None)
        if entity_id in self._raised:
            self._raised.discard(entity_id)
            ir.async_delete_issue(
                self.hass, DOMAIN, issue_prefix(self._entry_id) + entity_id
            )

    def _roles_text(self, entity_id: str) -> str:
        parts = []
        for role, name in self._roles[entity_id]:
            label = self._labels[role]
            parts.append(f"{label} {name}" if name else label)
        return ", ".join(parts)

    def _name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is not None and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is not None and (entry.name or entry.original_name):
            return str(entry.name or entry.original_name)
        return entity_id

    @callback
    def _start_clock(self, entity_id: str, since: datetime) -> None:
        self._since[entity_id] = since
        self._timers[entity_id] = async_track_point_in_utc_time(
            self.hass,
            partial(self._handle_threshold, entity_id),
            since + self._threshold,
        )

    @callback
    def _handle_state(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        if new is None or entity_id not in self._roles:
            return  # removed: reported by the missing entities issue
        if new.state in _UNAVAILABLE:
            if entity_id not in self._since:
                self._start_clock(entity_id, new.last_changed)
            return
        if unsub := self._timers.pop(entity_id, None):
            unsub()
        since = self._since.pop(entity_id, None)
        if entity_id in self._raised:
            self._raised.discard(entity_id)
            ir.async_delete_issue(
                self.hass, DOMAIN, issue_prefix(self._entry_id) + entity_id
            )
            self.hass.bus.async_fire(
                EVENT_LIGHT_AVAILABLE,
                {
                    "entry_id": self._entry_id,
                    "entity_id": entity_id,
                    "since": dt_util.as_local(since).isoformat() if since else None,
                    "roles": self._roles_text(entity_id),
                },
            )

    @callback
    def _handle_threshold(self, entity_id: str, _now: datetime) -> None:
        self._timers.pop(entity_id, None)
        state = self.hass.states.get(entity_id)
        if (
            entity_id not in self._roles
            or state is None
            or state.state not in _UNAVAILABLE
            or entity_id not in self._since
            or entity_id in self._raised
        ):
            return
        since = dt_util.as_local(self._since[entity_id])
        roles = self._roles_text(entity_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_prefix(self._entry_id) + entity_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="light_unavailable",
            translation_placeholders={
                "entity": self._name(entity_id),
                "entity_id": entity_id,
                "since": since.strftime("%Y-%m-%d %H:%M"),
                "roles": roles,
            },
        )
        self._raised.add(entity_id)
        _LOGGER.warning("%s has been unavailable since %s", entity_id, since)
        self.hass.bus.async_fire(
            EVENT_LIGHT_UNAVAILABLE,
            {
                "entry_id": self._entry_id,
                "entity_id": entity_id,
                "since": since.isoformat(),
                "roles": roles,
            },
        )
