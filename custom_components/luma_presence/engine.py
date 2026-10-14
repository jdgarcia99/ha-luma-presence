"""Simulation engine for LumaPresence (phase 1: Sun and Time modes)."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timedelta
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
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
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
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import (
    get_astral_event_date,
    get_astral_event_next,
    is_up,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    DOMAIN,
    EVENT_END,
    EVENT_START,
    MAX_END_JITTER_MIN,
    MAX_START_JITTER_MIN,
    MODE_SUN,
    OWN_CONTEXT_MAXLEN,
    STATUS_IDLE,
    STATUS_SIMULATING,
    STATUS_STOPPING,
    STATUS_WAITING,
    STOP_KEEP_IF_NIGHT,
    STOP_KEEP_ON,
    STOP_OFF_GRADUAL,
    STOP_OFF_NOW,
    STORE_VERSION,
    signal_update,
)
from .models import LumaSettings

_LOGGER = logging.getLogger(__name__)

_IGNORED_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


class SimulationEngine:
    """Schedules simulation windows and drives the lights inside them.

    Ownership rule: the engine only remembers lights it turned on itself.
    As soon as someone else changes one of those lights, ownership is
    released, so stopping the simulation never touches a light a person
    switched on.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict[str, Any],
        settings: LumaSettings,
    ) -> None:
        """Initialize the engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._lights: list[str] = list(config.get(CONF_LIGHTS, []))
        self._front: set[str] = set(config.get(CONF_FRONT_LIGHTS, []))
        self.settings = settings

        self.enabled = False
        self.next_event: datetime | None = None
        self.next_event_kind: str | None = None
        self.last_action: dict[str, Any] | None = None

        self._started = False
        self._sim_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_state: CALLBACK_TYPE | None = None
        self._owned: dict[str, str] = {}
        self._own_ids: deque[str] = deque(maxlen=OWN_CONTEXT_MAXLEN)
        self._own_set: set[str] = set()
        self._jitter: dict[date, tuple[float, float]] = {}
        self._enabled_listeners: list[Callable[[], None]] = []
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.engine"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def status(self) -> str:
        """Return the current engine status."""
        if self._sim_task is not None:
            return STATUS_SIMULATING
        if self._stop_task is not None:
            return STATUS_STOPPING
        if self.enabled:
            return STATUS_WAITING
        return STATUS_IDLE

    @property
    def owned_lights(self) -> list[str]:
        """Lights currently owned by the simulation."""
        return sorted(self._owned)

    def is_own_context(self, context_id: str) -> bool:
        """Return True if a context was created by this engine."""
        return context_id in self._own_set

    @callback
    def add_enabled_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback fired when the master switch changes."""
        self._enabled_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._enabled_listeners:
                self._enabled_listeners.remove(listener)

        return _remove

    @callback
    def restore_enabled(self, enabled: bool) -> None:
        """Restore the master switch state before the engine starts."""
        self.enabled = enabled

    async def async_start(self) -> None:
        """Start the engine once Home Assistant is running."""
        data = await self._store.async_load() or {}
        for entity_id in data.get("owned", []):
            state = self.hass.states.get(entity_id)
            if entity_id in self._lights and state is not None and state.state == STATE_ON:
                # Context ids do not survive a restart; keep ownership by entity.
                self._owned[entity_id] = ""
        self._unsub_state = async_track_state_change_event(
            self.hass, self._lights, self._handle_light_change
        )
        self._started = True
        if self.enabled:
            await self._async_reschedule()
        self._notify()

    async def async_shutdown(self) -> None:
        """Stop all timers and tasks without touching the lights."""
        self._started = False
        self._cancel_timer()
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        for task in (self._sim_task, self._stop_task):
            if task is not None:
                task.cancel()
        self._sim_task = None
        self._stop_task = None
        await self._store.async_save(self._data_to_save())

    async def async_set_enabled(
        self, enabled: bool, *, stop_behavior: str | None = None
    ) -> None:
        """Turn the master switch on or off."""
        if enabled == self.enabled:
            return
        self.enabled = enabled
        for listener in list(self._enabled_listeners):
            listener()
        if self._started:
            if enabled:
                await self._async_reschedule()
            else:
                self._cancel_timer()
                await self._async_end_simulation(
                    stop_behavior or self.settings.stop_behavior
                )
        self._notify()

    async def async_settings_changed(self) -> None:
        """Recompute the schedule after a schedule-related setting changed."""
        if self._started and self.enabled:
            await self._async_reschedule()
        self._notify()

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    async def _async_reschedule(self) -> None:
        self._cancel_timer()
        if not self.enabled:
            return
        now = dt_util.utcnow()
        self._prune_jitter()
        if (window := self._current_window(now)) is not None:
            if self._sim_task is None:
                self._start_simulation()
            self._schedule(window[1], EVENT_END)
        else:
            if self._sim_task is not None:
                # A setting change moved us outside the window.
                await self._async_end_simulation(STOP_OFF_GRADUAL)
            if (window := self._next_window(now)) is not None:
                self._schedule(window[0], EVENT_START)
        self._notify()

    @callback
    def _schedule(self, when: datetime, kind: str) -> None:
        self.next_event = when
        self.next_event_kind = kind
        self._unsub_timer = async_track_point_in_utc_time(
            self.hass, self._handle_timer, when
        )

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        self.next_event = None
        self.next_event_kind = None

    @callback
    def _handle_timer(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.hass.async_create_task(
            self._async_handle_timer(), f"{DOMAIN}_timer_{self._entry_id}"
        )

    async def _async_handle_timer(self) -> None:
        if self.next_event_kind == EVENT_END and self._sim_task is not None:
            # Scheduled end: the house is still empty, fade out gradually.
            await self._async_end_simulation(STOP_OFF_GRADUAL)
        await self._async_reschedule()

    def _jitter_for(self, day: date) -> tuple[float, float]:
        """Random start/end offsets in minutes, fixed per window date."""
        if day not in self._jitter:
            self._jitter[day] = (random.random(), random.random())
        start_r, end_r = self._jitter[day]
        start = start_r * MAX_START_JITTER_MIN if self.settings.delayed_start else 0.0
        end = end_r * MAX_END_JITTER_MIN if self.settings.delayed_end else 0.0
        return start, end

    def _prune_jitter(self) -> None:
        limit = dt_util.now().date() - timedelta(days=3)
        for day in [d for d in self._jitter if d < limit]:
            del self._jitter[day]

    def _window(self, day: date) -> tuple[datetime, datetime] | None:
        """Return the (start, end) UTC window that starts on a local date."""
        settings = self.settings
        tz = dt_util.get_default_time_zone()
        if settings.mode == MODE_SUN:
            start = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, day)
            if start is None:
                return None
        else:
            start = dt_util.as_utc(datetime.combine(day, settings.start_time, tzinfo=tz))

        end_time = settings.late_end_time if settings.late_arrival else settings.end_time
        end_day = dt_util.as_local(start).date()
        end = dt_util.as_utc(datetime.combine(end_day, end_time, tzinfo=tz))
        if end <= start:
            end = dt_util.as_utc(
                datetime.combine(end_day + timedelta(days=1), end_time, tzinfo=tz)
            )

        start_jitter, end_jitter = self._jitter_for(day)
        start += timedelta(minutes=start_jitter)
        end += timedelta(minutes=end_jitter)

        # The simulation always ends at sunrise at the latest.
        sunrise = get_astral_event_next(self.hass, SUN_EVENT_SUNRISE, start)
        if sunrise is not None:
            end = min(end, sunrise)
        if end <= start:
            return None
        return start, end

    def _current_window(self, now: datetime) -> tuple[datetime, datetime] | None:
        today = dt_util.as_local(now).date()
        for offset in (-1, 0):
            window = self._window(today + timedelta(days=offset))
            if window is not None and window[0] <= now < window[1]:
                return window
        return None

    def _next_window(self, now: datetime) -> tuple[datetime, datetime] | None:
        today = dt_util.as_local(now).date()
        for offset in (0, 1, 2):
            window = self._window(today + timedelta(days=offset))
            if window is not None and window[0] > now:
                return window
        return None

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    @callback
    def _start_simulation(self) -> None:
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None
        self._record(None, "simulation_started")
        self._sim_task = self.hass.async_create_background_task(
            self._async_simulation_loop(),
            f"{DOMAIN}_simulation_{self._entry_id}",
            eager_start=False,
        )

    async def _async_simulation_loop(self) -> None:
        while True:
            try:
                await self._async_step()
            except Exception:  # noqa: BLE001 - keep the simulation alive
                _LOGGER.exception("Unexpected error in simulation step")
            low, high = sorted((self.settings.min_dwell, self.settings.max_dwell))
            await asyncio.sleep(random.uniform(max(low, 0.5), max(high, 0.5)) * 60)

    async def _async_step(self) -> None:
        """Turn one light on or off, respecting the configured limits."""
        settings = self.settings
        states = {entity_id: self.hass.states.get(entity_id) for entity_id in self._lights}
        available = [
            e for e, st in states.items() if st is not None and st.state in (STATE_ON, STATE_OFF)
        ]
        if not available:
            return
        on = [e for e in available if states[e].state == STATE_ON]  # type: ignore[union-attr]
        off = [e for e in available if e not in on]
        front_on = [e for e in on if e in self._front]
        front_off = [e for e in off if e in self._front]

        max_on = max(settings.max_lights, 0)
        min_on = min(max(settings.min_lights, 0), max_on)
        min_front = max(settings.min_front_lights, 0)

        # Lights that may be turned off without dropping below the front minimum.
        removable = [
            e for e in on if not (e in self._front and len(front_on) <= min_front)
        ]

        target: str | None = None
        turn_on = False
        if front_off and len(front_on) < min_front and len(on) < max_on:
            target, turn_on = random.choice(front_off), True
        elif len(on) > max_on:
            target = random.choice(removable or on)
        elif len(on) < min_on and off:
            target, turn_on = random.choice(off), True
        else:
            pool: list[str] = []
            if len(on) > min_on:
                pool.extend(removable)
            if len(on) < max_on:
                pool.extend(off)
            if pool:
                target = random.choice(pool)
                turn_on = target in off

        if target is not None:
            await self._async_switch(target, turn_on)

    async def _async_end_simulation(self, behavior: str) -> None:
        """Stop the loop and handle the lights the simulation owns."""
        if self._sim_task is not None:
            self._sim_task.cancel()
            self._sim_task = None
            self._record(None, "simulation_stopped")
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None

        if behavior == STOP_KEEP_IF_NIGHT:
            behavior = STOP_OFF_GRADUAL if is_up(self.hass) else STOP_KEEP_ON

        lights = self._owned_lights_on()
        if behavior == STOP_KEEP_ON or not lights:
            self._owned.clear()
            self._save()
        elif behavior == STOP_OFF_NOW:
            for entity_id in lights:
                await self._async_switch(entity_id, False)
        else:
            self._stop_task = self.hass.async_create_background_task(
                self._async_gradual_off(lights),
                f"{DOMAIN}_stop_{self._entry_id}",
                eager_start=False,
            )
        self._notify()

    async def _async_gradual_off(self, lights: list[str]) -> None:
        try:
            random.shuffle(lights)
            for index, entity_id in enumerate(lights):
                if entity_id not in self._owned:
                    continue  # someone took over this light
                await self._async_switch(entity_id, False)
                if index < len(lights) - 1 and self.settings.stop_interval > 0:
                    await asyncio.sleep(random.uniform(0, self.settings.stop_interval))
        finally:
            if self._stop_task is asyncio.current_task():
                self._stop_task = None
                self._notify()

    def _owned_lights_on(self) -> list[str]:
        return [
            entity_id
            for entity_id in self._owned
            if (state := self.hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
        ]

    async def _async_switch(self, entity_id: str, turn_on: bool) -> bool:
        context = Context()
        self._remember_context(context.id)
        if turn_on:
            self._owned[entity_id] = context.id
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
            if turn_on:
                self._owned.pop(entity_id, None)
            return False
        if not turn_on:
            self._owned.pop(entity_id, None)
        self._record(entity_id, STATE_ON if turn_on else STATE_OFF)
        self._save()
        return True

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _remember_context(self, context_id: str) -> None:
        if len(self._own_ids) == self._own_ids.maxlen:
            self._own_set.discard(self._own_ids[0])
        self._own_ids.append(context_id)
        self._own_set.add(context_id)

    @callback
    def _record(self, entity_id: str | None, action: str) -> None:
        self.last_action = {
            "time": dt_util.utcnow().isoformat(),
            "entity_id": entity_id,
            "action": action,
        }
        self.hass.bus.async_fire(
            f"{DOMAIN}_action", {"entry_id": self._entry_id, **self.last_action}
        )
        self._notify()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))

    def _data_to_save(self) -> dict[str, Any]:
        return {"owned": list(self._owned)}

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 10)
