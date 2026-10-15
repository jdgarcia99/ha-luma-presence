"""Simulation engine for LumaPresence (phase 1: Sun and Time modes)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
import random
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_OFF, STATE_ON, SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.sun import (
    get_astral_event_date,
    get_astral_event_next,
    is_up,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    CONF_REAR_LIGHTS,
    DOMAIN,
    EVENT_END,
    EVENT_START,
    MAX_END_JITTER_MIN,
    MAX_START_JITTER_MIN,
    MODE_REPLAY,
    MODE_SUN,
    OWNER_SIMULATION,
    STATUS_IDLE,
    STATUS_SIMULATING,
    STATUS_STOPPING,
    STATUS_WAITING,
    STOP_KEEP_IF_NIGHT,
    STOP_KEEP_ON,
    STOP_OFF_GRADUAL,
    STOP_OFF_NOW,
    signal_update,
)
from .light_control import LightController
from .models import LumaSettings

if TYPE_CHECKING:
    from .replay import ReplayEngine

_LOGGER = logging.getLogger(__name__)


class SimulationEngine:
    """Schedules simulation windows and drives the lights inside them.

    Lights are switched through the shared LightController, which applies the
    ownership rule. Lights locked by a running route are counted for the
    limits but never toggled.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict[str, Any],
        settings: LumaSettings,
        controller: LightController,
    ) -> None:
        """Initialize the engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._lights: list[str] = list(config.get(CONF_LIGHTS, []))
        self._front: set[str] = set(config.get(CONF_FRONT_LIGHTS, []))
        self._rear: set[str] = set(config.get(CONF_REAR_LIGHTS, []))
        self.settings = settings
        self.controller = controller
        # Set once the learning gate exists; plays Replay nights.
        self.replay: ReplayEngine | None = None

        self.enabled = False
        self.next_event: datetime | None = None
        self.next_event_kind: str | None = None

        self._started = False
        self._sim_task: asyncio.Task[None] | None = None
        self._sim_mode: str | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._jitter: dict[date, tuple[float, float]] = {}
        self._enabled_listeners: list[Callable[[], None]] = []

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
    def lights(self) -> list[str]:
        """Lights the simulation may control."""
        return list(self._lights)

    @property
    def owned_lights(self) -> list[str]:
        """Lights currently owned by the simulation."""
        return self.controller.owned(OWNER_SIMULATION)

    @property
    def last_action(self) -> dict[str, Any] | None:
        """Last action of LumaPresence."""
        return self.controller.last_action

    def is_own_context(self, context_id: str) -> bool:
        """Return True if a context was created by LumaPresence."""
        return self.controller.is_own_context(context_id)

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
        self._started = True
        if self.enabled:
            await self._async_reschedule()
        self._notify()

    async def async_shutdown(self) -> None:
        """Stop all timers and tasks without touching the lights."""
        self._started = False
        self._cancel_timer()
        for task in (self._sim_task, self._stop_task):
            if task is not None:
                task.cancel()
        self._sim_task = None
        self._stop_task = None

    async def async_set_enabled(
        self, enabled: bool, *, stop_behavior: str | None = None
    ) -> None:
        """Turn the master switch on or off."""
        if enabled == self.enabled:
            return
        self.enabled = enabled
        # Listeners run first: a running route is cancelled before its lights
        # are handled below together with the simulation's lights.
        for listener in list(self._enabled_listeners):
            listener()
        if self._started:
            if enabled:
                await self._async_reschedule()
            else:
                self._cancel_timer()
                await self._async_end_simulation(
                    stop_behavior or self.settings.stop_behavior, all_owners=True
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
            if self._sim_task is not None and self._sim_mode != self.settings.mode:
                # The mode changed inside the window: restart with the new one.
                self._sim_task.cancel()
                self._sim_task = None
            if self._sim_task is None:
                self._start_simulation(window)
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
        if settings.mode in (MODE_SUN, MODE_REPLAY):
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
    def _start_simulation(self, window: tuple[datetime, datetime]) -> None:
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None
        self.controller.record(None, "simulation_started", mode=self.settings.mode)
        self._sim_mode = self.settings.mode
        self._sim_task = self.hass.async_create_background_task(
            self._async_run(window),
            f"{DOMAIN}_simulation_{self._entry_id}",
            eager_start=False,
        )

    async def _async_run(self, window: tuple[datetime, datetime]) -> None:
        if (
            self.settings.mode == MODE_REPLAY
            and self.replay is not None
            and (plan := await self.replay.async_plan(window)) is not None
        ):
            await self.replay.async_play(plan)
            return
        # Other modes, or Replay without a usable source night: random loop
        # (with the Replay window, that is exactly Sun mode).
        await self._async_simulation_loop()

    async def _async_simulation_loop(self) -> None:
        while True:
            try:
                await self._async_step()
            except Exception:  # keep the simulation alive
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
        # Locked lights belong to a running route: counted, never toggled.
        on = [e for e in available if states[e].state == STATE_ON]  # type: ignore[union-attr]
        off = [e for e in available if e not in on]
        free_off = [e for e in off if not self.controller.is_locked(e)]

        max_on = max(settings.max_lights, 0)
        min_on = min(max(settings.min_lights, 0), max_on)
        # Front first, then rear.
        facades = (
            (self._front, max(settings.min_front_lights, 0)),
            (self._rear, max(settings.min_rear_lights, 0)),
        )

        # Lights that may be turned off without dropping a facade below its minimum.
        protected: set[str] = set()
        facade_target: str | None = None
        for members, minimum in facades:
            facade_on = [e for e in on if e in members]
            facade_off = [e for e in free_off if e in members]
            if len(facade_on) <= minimum:
                protected.update(facade_on)
            if facade_target is None and facade_off and len(facade_on) < minimum:
                facade_target = random.choice(facade_off)
        free_on = [e for e in on if not self.controller.is_locked(e)]
        removable = [e for e in free_on if e not in protected]

        target: str | None = None
        turn_on = False
        if facade_target is not None and len(on) < max_on:
            target, turn_on = facade_target, True
        elif len(on) > max_on:
            if candidates := removable or free_on:
                target = random.choice(candidates)
        elif len(on) < min_on and free_off:
            target, turn_on = random.choice(free_off), True
        else:
            pool: list[str] = []
            if len(on) > min_on:
                pool.extend(removable)
            if len(on) < max_on:
                pool.extend(free_off)
            if pool:
                target = random.choice(pool)
                turn_on = target in free_off

        if target is not None:
            await self._async_switch(target, turn_on)

    async def _async_end_simulation(
        self, behavior: str, *, all_owners: bool = False
    ) -> None:
        """Stop the loop and handle the lights the simulation owns.

        With `all_owners`, the lights of an interrupted route are handled too.
        """
        if self._sim_task is not None:
            self._sim_task.cancel()
            self._sim_task = None
            self.controller.record(None, "simulation_stopped")
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None

        if behavior == STOP_KEEP_IF_NIGHT:
            behavior = STOP_OFF_GRADUAL if is_up(self.hass) else STOP_KEEP_ON

        owner = None if all_owners else OWNER_SIMULATION
        lights = self.controller.owned_on(owner)
        if behavior == STOP_KEEP_ON or not lights:
            self.controller.release(owner)
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
            await self.controller.async_turn_off_gradually(
                lights, self.settings.stop_interval
            )
        finally:
            if self._stop_task is asyncio.current_task():
                self._stop_task = None
                self._notify()

    async def _async_switch(self, entity_id: str, turn_on: bool) -> bool:
        return await self.controller.async_switch(entity_id, turn_on, OWNER_SIMULATION)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))
