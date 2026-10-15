"""Light control shared by the simulation and the night sequences.

Ownership rule: a light is owned only while nobody else has touched it. The
controller remembers which part of LumaPresence turned each light on, and
releases ownership as soon as someone else changes the light, so stopping
never touches a light a person switched on.

Every change made by LumaPresence carries a context created here, so the
learning gate can ignore them whichever engine made them.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable, Iterable
import logging
import random
from typing import Any

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_ACTION,
    OWN_CONTEXT_MAXLEN,
    OWNER_SIMULATION,
    STORE_VERSION,
    signal_update,
)

_LOGGER = logging.getLogger(__name__)

_IGNORED_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


class LightController:
    """Switches lights for LumaPresence and tracks who owns them."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the controller."""
        self.hass = hass
        self._entry_id = entry_id
        self.last_action: dict[str, Any] | None = None
        # Switch commands that failed since start (for the night report).
        self.failures = 0

        # entity_id -> (owner, context id of the change that turned it on)
        self._owned: dict[str, tuple[str, str]] = {}
        self._own_ids: deque[str] = deque(maxlen=OWN_CONTEXT_MAXLEN)
        self._own_set: set[str] = set()
        self._locks: Counter[str] = Counter()
        self._unlock_listeners: list[Callable[[list[str]], None]] = []
        self._data: dict[str, Any] = {}
        self._values: dict[str, Any] = {}
        self._unsub_state: CALLBACK_TYPE | None = None
        # Same storage key as v0.2, when the engine kept the owned lights.
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.engine"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        """Load the stored data, before the entities are restored."""
        self._data = await self._store.async_load() or {}
        self._values = dict(self._data.get("values", {}))

    def get_value(self, key: str) -> Any:
        """A value kept across restarts (master switch origin, alarm override)."""
        return self._values.get(key)

    @callback
    def set_value(self, key: str, value: Any) -> None:
        """Keep a value across restarts."""
        if value is None:
            self._values.pop(key, None)
        else:
            self._values[key] = value
        self._save()

    async def async_start(self, entities: Iterable[str]) -> None:
        """Restore ownership and follow the lights LumaPresence may switch."""
        tracked = sorted(set(entities))
        owned = self._data.get("owned", [])
        if isinstance(owned, list):  # v0.2: a list of simulation lights
            owned = dict.fromkeys(owned, OWNER_SIMULATION)
        for entity_id, owner in owned.items():
            state = self.hass.states.get(entity_id)
            if entity_id in tracked and state is not None and state.state == STATE_ON:
                # Context ids do not survive a restart; keep ownership by entity.
                self._owned[entity_id] = (owner, "")
        if tracked:
            self._unsub_state = async_track_state_change_event(
                self.hass, tracked, self._handle_light_change
            )

    async def async_shutdown(self) -> None:
        """Stop following the lights and save ownership."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        await self._store.async_save(self._data_to_save())

    # ------------------------------------------------------------------
    # Ownership and locks
    # ------------------------------------------------------------------
    def owned(self, owner: str | None = None) -> list[str]:
        """Lights owned by LumaPresence, optionally by one owner only."""
        return sorted(
            entity_id
            for entity_id, (who, _) in self._owned.items()
            if owner is None or who == owner
        )

    def owned_on(self, owner: str | None = None) -> list[str]:
        """Owned lights that are currently on."""
        return [
            entity_id
            for entity_id in self.owned(owner)
            if (state := self.hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
        ]

    def owner_of(self, entity_id: str) -> str | None:
        """Return which part of LumaPresence owns a light."""
        owned = self._owned.get(entity_id)
        return owned[0] if owned else None

    @callback
    def release(self, owner: str | None = None) -> None:
        """Forget ownership without touching the lights."""
        for entity_id in self.owned(owner):
            del self._owned[entity_id]
        self._save()

    def is_own_context(self, context_id: str) -> bool:
        """Return True if a context was created by LumaPresence."""
        return context_id in self._own_set

    @callback
    def lock(self, entities: Iterable[str]) -> None:
        """Reserve lights for a running route."""
        self._locks.update(entities)

    @callback
    def unlock(self, entities: Iterable[str]) -> None:
        """Release lights reserved by lock()."""
        entities = list(entities)
        self._locks.subtract(entities)
        self._locks = +self._locks  # drop counts that reached zero
        if released := [e for e in dict.fromkeys(entities) if not self.is_locked(e)]:
            for listener in list(self._unlock_listeners):
                listener(released)

    @callback
    def add_unlock_listener(
        self, listener: Callable[[list[str]], None]
    ) -> CALLBACK_TYPE:
        """Call `listener` with the lights released by unlock()."""
        self._unlock_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._unlock_listeners:
                self._unlock_listeners.remove(listener)

        return _remove

    def is_locked(self, entity_id: str) -> bool:
        """Return True while a running route reserves a light."""
        return self._locks[entity_id] > 0

    # ------------------------------------------------------------------
    # Switching
    # ------------------------------------------------------------------
    async def async_switch(
        self,
        entity_id: str,
        turn_on: bool,
        owner: str = OWNER_SIMULATION,
        **extra: Any,
    ) -> bool:
        """Turn a light on or off; turning it on makes `owner` its owner."""
        context = Context()
        self._remember_context(context.id)
        if turn_on:
            self._owned[entity_id] = (owner, context.id)
        try:
            await self.hass.services.async_call(
                "homeassistant",
                SERVICE_TURN_ON if turn_on else SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                context=context,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch %s: %s", entity_id, err)
            self.failures += 1
            if turn_on:
                self._owned.pop(entity_id, None)
            return False
        if not turn_on:
            self._owned.pop(entity_id, None)
        self.record(entity_id, STATE_ON if turn_on else STATE_OFF, **extra)
        self._save()
        return True

    async def async_turn_off_gradually(
        self, lights: list[str], max_interval: float, **extra: Any
    ) -> None:
        """Turn owned lights off one by one, in random order."""
        lights = list(lights)
        random.shuffle(lights)
        for index, entity_id in enumerate(lights):
            if entity_id not in self._owned:
                continue  # someone took over this light
            await self.async_switch(entity_id, False, **extra)
            if index < len(lights) - 1 and max_interval > 0:
                await asyncio.sleep(random.uniform(0, max_interval))

    @callback
    def record(self, entity_id: str | None, action: str, **extra: Any) -> None:
        """Publish an action to the last action sensor and the event bus."""
        self.last_action = {
            "time": dt_util.utcnow().isoformat(),
            "entity_id": entity_id,
            "action": action,
            **extra,
        }
        self.hass.bus.async_fire(
            EVENT_ACTION, {"entry_id": self._entry_id, **self.last_action}
        )
        self._notify()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @callback
    def _handle_light_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id not in self._owned:
            return
        new = event.data["new_state"]
        old = event.data["old_state"]
        if new is None:
            self._owned.pop(entity_id, None)
            self._save()
            return
        if new.context.id in self._own_set:
            return
        if new.state in _IGNORED_STATES or (old is not None and old.state in _IGNORED_STATES):
            return  # a device dropping offline is not a person touching it
        if old is None or old.state != new.state or new.context.user_id is not None:
            self._owned.pop(entity_id, None)
            self._save()

    def _remember_context(self, context_id: str) -> None:
        if len(self._own_ids) == self._own_ids.maxlen:
            self._own_set.discard(self._own_ids[0])
        self._own_ids.append(context_id)
        self._own_set.add(context_id)

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "owned": {entity_id: who for entity_id, (who, _) in self._owned.items()},
            "values": self._values,
        }

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 10)
