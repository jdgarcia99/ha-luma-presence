"""Simulation engine for LumaPresence (phase 1: Sun and Time modes)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
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
    CONF_MORNING_ENABLED,
    CONF_MORNING_LATEST,
    CONF_MORNING_LEARNED_ENABLED,
    CONF_MORNING_START,
    CONF_REAR_LIGHTS,
    DAYLIGHT_OFF_MAX_S,
    DEFAULT_MORNING_ENABLED,
    DEFAULT_MORNING_LATEST,
    DEFAULT_MORNING_LEARNED_ENABLED,
    DEFAULT_MORNING_START,
    DOMAIN,
    EVENT_END,
    EVENT_START,
    MAX_END_JITTER_MIN,
    MAX_START_JITTER_MIN,
    MODE_DREAM,
    MODE_REPLAY,
    MODE_SUN,
    MODE_TIME,
    ORIGIN_MANUAL,
    ORIGIN_RESTORE,
    ORIGIN_SCHEDULE,
    OWNER_SIMULATION,
    SOURCE_FALLBACK,
    SOURCE_REPLAY,
    START_MORNING,
    START_SUNSET,
    STATUS_IDLE,
    STATUS_SIMULATING,
    STATUS_STOPPING,
    STATUS_WAITING,
    STOP_KEEP_IF_NIGHT,
    STOP_KEEP_ON,
    STOP_OFF_GRADUAL,
    STOP_OFF_NOW,
    WAKE_DELAY_MAX_MIN,
    signal_update,
)
from .light_control import LightController
from .models import LumaSettings
from .plan import WEEKDAY, WEEKEND

if TYPE_CHECKING:
    from .context import ContextProvider
    from .dream import DreamEngine
    from .plan import PlanPlayer
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
        # Wake-up window: Sun/Time use morning_start; Replay/Dream the learned
        # first switch-on of the morning (never earlier than morning_start).
        self._morning_enabled = bool(config.get(CONF_MORNING_ENABLED, DEFAULT_MORNING_ENABLED))
        self._morning_learned = bool(
            config.get(CONF_MORNING_LEARNED_ENABLED, DEFAULT_MORNING_LEARNED_ENABLED)
        )
        self._morning_start = _time(config.get(CONF_MORNING_START), DEFAULT_MORNING_START)
        self._morning_latest = timedelta(
            minutes=float(config.get(CONF_MORNING_LATEST, DEFAULT_MORNING_LATEST))
        )
        # Master switched on inside a wake-up window: (day, earliest start).
        self._wake_delay: tuple[date, datetime] | None = None
        self.settings = settings
        self.controller = controller
        # Set once the learning gate exists; plays Replay nights.
        self.replay: ReplayEngine | None = None
        self.dream: DreamEngine | None = None
        self.player: PlanPlayer | None = None
        self.context: ContextProvider | None = None

        self.enabled = False
        # Who turned the master switch on, and when (cleared when it turns off).
        self.activated_by: str | None = None
        self.activated_at: datetime | None = None
        # Origin of the last change of the master switch, on or off.
        self.last_origin: str | None = None
        self.next_event: datetime | None = None
        self.next_event_kind: str | None = None
        # Why the next (or current) window starts when it does.
        self.next_event_reason: str | None = None
        self.window_reason: str | None = None
        # +1 to min_lights tonight in Sun/Time mode when the weather is bad.
        self.weather_boost = 0

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
        """Restore the master switch state before the engine starts.

        The origin saved before the restart is kept (so a disarm while Home
        Assistant was down can still be told apart); "restore" is used only
        when none was saved.
        """
        self.enabled = enabled
        if not enabled:
            self._set_origin(None)
            return
        saved = self.controller.get_value("master") or {}
        at = dt_util.parse_datetime(saved["at"]) if saved.get("at") else None
        self._set_origin(saved.get("origin") or ORIGIN_RESTORE, at)

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
        self,
        enabled: bool,
        *,
        stop_behavior: str | None = None,
        origin: str = ORIGIN_MANUAL,
    ) -> None:
        """Turn the master switch on or off.

        Turning it on when it is on, or off when it is off, does nothing, so
        user automations doing the same as the alarm control never conflict.
        """
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.last_origin = origin
        self._set_origin(origin if enabled else None)
        # Listeners run first: a running route is cancelled before its lights
        # are handled below together with the simulation's lights.
        for listener in list(self._enabled_listeners):
            listener()
        if self._started:
            if enabled:
                self._wake_delay = None
                now = dt_util.utcnow()
                today = dt_util.as_local(now).date()
                if (window := self._morning(today)) is not None and window[0] <= now < window[1]:
                    # Leaving before dawn: start soon, after a random delay.
                    delay = timedelta(minutes=random.uniform(0, WAKE_DELAY_MAX_MIN))
                    self._wake_delay = (today, now + delay)
                await self._async_reschedule()
            else:
                self._cancel_timer()
                await self._async_end_simulation(
                    stop_behavior or self.settings.stop_behavior,
                    all_owners=True,
                    origin=origin,
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
            self.next_event_reason = self.window_reason
            self._schedule(window[1], EVENT_END)
        else:
            if self._sim_task is not None or (
                self._daylight() and self.controller.owned_on()
            ):
                # Outside the window: a setting changed, or it is day (the
                # daylight guard switches the lights off within 3 minutes).
                await self._async_end_simulation(
                    STOP_OFF_GRADUAL,
                    all_owners=self._daylight(),
                    max_total=DAYLIGHT_OFF_MAX_S if self._daylight() else None,
                )
            if (window := self._next_window(now)) is not None:
                self._schedule(window[0], EVENT_START)
            else:
                self.next_event_reason = None
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
        self.next_event_reason = None

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

    def _evening(
        self, day: date, mode: str | None = None
    ) -> tuple[datetime, datetime, str] | None:
        """Evening window starting on a local date: (start, end, start reason)."""
        settings = self.settings
        tz = dt_util.get_default_time_zone()
        reason = START_SUNSET
        if (mode or settings.mode) in (MODE_SUN, MODE_REPLAY, MODE_DREAM):
            start = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, day)
            if start is None:
                return None
            if self.context is not None and self.context.darkness is not None:
                start, reason = self.context.darkness.evening_start(day, start)
        else:
            start = dt_util.as_utc(datetime.combine(day, settings.start_time, tzinfo=tz))
            reason = "time"

        end_time = settings.late_end_time if settings.late_arrival else settings.end_time
        end_day = dt_util.as_local(start).date()
        end = dt_util.as_utc(datetime.combine(end_day, end_time, tzinfo=tz))
        if end <= start:
            end = dt_util.as_utc(
                datetime.combine(end_day + timedelta(days=1), end_time, tzinfo=tz)
            )

        # The random start delay applies after darkness too.
        start_jitter, end_jitter = self._jitter_for(day)
        start += timedelta(minutes=start_jitter)
        end += timedelta(minutes=end_jitter)

        # The simulation always ends at sunrise at the latest.
        sunrise = get_astral_event_next(self.hass, SUN_EVENT_SUNRISE, start)
        if sunrise is not None:
            end = min(end, sunrise)
        if end <= start:
            return None
        return start, end, reason

    def _morning(self, day: date) -> tuple[datetime, datetime, str] | None:
        """Wake-up window of a local date: from the start until it is light."""
        learned = self.settings.mode in (MODE_REPLAY, MODE_DREAM)
        if not (self._morning_learned if learned else self._morning_enabled):
            return None
        start_time = self._morning_start
        if learned and self.dream is not None:
            first = self.dream.morning_first_on(self.morning_type(day))
            if first is not None and first > start_time:
                start_time = first
        tz = dt_util.get_default_time_zone()
        start = dt_util.as_utc(datetime.combine(day, start_time, tzinfo=tz))
        sunrise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, day)
        if sunrise is None:
            return None
        tracker = self.context.darkness if self.context is not None else None
        if tracker is not None and tracker.available:
            # Until it is light; if it stays dark, up to sunrise + latest.
            end = tracker.morning_end(day, start, sunrise + self._morning_latest)
        else:
            end = sunrise
        if self._wake_delay is not None and self._wake_delay[0] == day:
            start = max(start, self._wake_delay[1])
        if end <= start:
            return None
        return start, end, START_MORNING

    def morning_type(self, day: date) -> str:
        """Type of the morning of a date: weekday when it is a workday."""
        return WEEKDAY if self._is_workday(day) else WEEKEND

    def _daylight(self) -> bool:
        return self.context is not None and self.context.daylight()

    def _is_workday(self, day: date) -> bool:
        if self.context is not None:
            return self.context.is_workday(day)
        return day.weekday() < 5

    def _windows(
        self, day: date, mode: str | None, evening_only: bool
    ) -> list[tuple[datetime, datetime, str]]:
        windows = [self._evening(day, mode)]
        if not evening_only:
            windows.append(self._morning(day))
        return [w for w in windows if w is not None]

    def evening_start_reason(self) -> str | None:
        """Why tonight's (current or next) evening window starts when it does."""
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date()
        for offset in (-1, 0, 1):
            window = self._evening(today + timedelta(days=offset))
            if window is not None and now < window[1]:
                return window[2]
        return None

    def morning_window(self) -> tuple[datetime, datetime] | None:
        """Current or next morning window (today or tomorrow)."""
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date()
        for offset in (0, 1):
            window = self._morning(today + timedelta(days=offset))
            if window is not None and now < window[1]:
                return window[0], window[1]
        return None

    def _window(self, day: date, mode: str | None = None) -> tuple[datetime, datetime] | None:
        """Evening window (start, end) that starts on a local date."""
        window = self._evening(day, mode)
        return (window[0], window[1]) if window else None

    def _current_window(
        self, now: datetime, mode: str | None = None, evening_only: bool = False
    ) -> tuple[datetime, datetime] | None:
        if self._daylight():
            return None  # daylight guard: never simulate while it is day
        today = dt_util.as_local(now).date()
        for offset in (-1, 0):
            for start, end, reason in self._windows(today + timedelta(days=offset), mode, evening_only):
                if start <= now < end:
                    self.window_reason = reason
                    return start, end
        return None

    def _next_window(
        self, now: datetime, mode: str | None = None, evening_only: bool = False
    ) -> tuple[datetime, datetime] | None:
        today = dt_util.as_local(now).date()
        upcoming = [
            window
            for offset in (0, 1, 2)
            for window in self._windows(today + timedelta(days=offset), mode, evening_only)
            if window[0] > now
        ]
        if not upcoming:
            return None
        start, end, reason = min(upcoming)
        self.next_event_reason = reason
        return start, end

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    @callback
    def _start_simulation(self, window: tuple[datetime, datetime]) -> None:
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None
        self.controller.record(
            None,
            "simulation_started",
            mode=self.settings.mode,
            origin=self.activated_by,
        )
        self._sim_mode = self.settings.mode
        self.weather_boost = (
            1
            if self.settings.mode in (MODE_SUN, MODE_TIME)
            and self.context is not None
            and self.context.weather()["bad"]
            else 0
        )
        self._sim_task = self.hass.async_create_background_task(
            self._async_run(window),
            f"{DOMAIN}_simulation_{self._entry_id}",
            eager_start=False,
        )

    async def _async_run(self, window: tuple[datetime, datetime]) -> None:
        if self.settings.mode == MODE_DREAM and self.dream is not None:
            await self._async_run_dream(window)
            return
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

    async def _async_run_dream(self, window: tuple[datetime, datetime]) -> None:
        """Dream, else Replay, else Sun mode; each fallback says why."""
        assert self.dream is not None and self.player is not None
        if (plan := await self.dream.async_plan(window)) is not None:
            await self.player.async_play(plan)
            return
        if self.replay is not None:
            if (replay_plan := await self.replay.async_plan(window)) is not None:
                self.dream.note_fallback(SOURCE_REPLAY, None)
                await self.replay.async_play(replay_plan)
                return
            replay_reason = (self.replay.status or {}).get("reason")
        else:
            replay_reason = None
        self.dream.note_fallback(SOURCE_FALLBACK, replay_reason)
        await self._async_simulation_loop()

    def planning_window(self) -> tuple[datetime, datetime] | None:
        """Tonight's Sun-style window (current or next), for Dream previews."""
        now = dt_util.utcnow()
        return self._current_window(now, MODE_SUN, evening_only=True) or self._next_window(
            now, MODE_SUN, evening_only=True
        )

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
        # Lights that did not obey tonight are not chosen again.
        free_off = [
            e
            for e in off
            if not self.controller.is_locked(e) and e not in self.controller.failed_tonight
        ]

        max_on = max(settings.max_lights, 0)
        # Bad weather: one more light (never above the maximum).
        min_on = min(max(settings.min_lights + self.weather_boost, 0), max_on)
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
        self,
        behavior: str,
        *,
        all_owners: bool = False,
        origin: str = ORIGIN_SCHEDULE,
        max_total: float | None = None,
    ) -> None:
        """Stop the loop and handle the lights the simulation owns.

        With `all_owners`, the lights of an interrupted route are handled too.
        `origin` says who stopped it: the master switch origin, or "schedule".
        `max_total` caps the duration of a gradual switch-off (seconds).
        """
        if self._sim_task is not None:
            self._sim_task.cancel()
            self._sim_task = None
            self.controller.record(None, "simulation_stopped", origin=origin)
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
            interval = self.settings.stop_interval
            if max_total is not None and len(lights) > 1:
                interval = min(interval, max_total / (len(lights) - 1))
            self._stop_task = self.hass.async_create_background_task(
                self._async_gradual_off(lights, interval),
                f"{DOMAIN}_stop_{self._entry_id}",
                eager_start=False,
            )
        self._notify()

    async def _async_gradual_off(self, lights: list[str], interval: float) -> None:
        try:
            await self.controller.async_turn_off_gradually(lights, interval)
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
    def _set_origin(self, origin: str | None, at: datetime | None = None) -> None:
        self.activated_by = origin
        self.activated_at = (at or dt_util.utcnow()) if origin else None
        self.controller.set_value(
            "master",
            {"origin": origin, "at": self.activated_at.isoformat()}
            if origin and self.activated_at
            else None,
        )

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))


def _time(value: Any, default: str) -> time:
    """Parse "HH:MM[:SS]" options."""
    try:
        return time.fromisoformat(str(value or default))
    except ValueError:
        return time.fromisoformat(default)
