"""Night sequences: every now and then someone seems to get up, walk along a
route (for example bedroom, hallway, bathroom), stay there a while and go
back to bed.

The window runs from the sequence start time until the next sunrise, only
while the master switch and the sequences switch are both on. Route lights
are locked while a route runs, so the simulation engine counts them but
never toggles them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
import logging
import random
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, SUN_EVENT_SUNRISE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DWELL_MAX,
    CONF_DWELL_MIN,
    CONF_STEP_DELAY_MAX,
    CONF_STEP_DELAY_MIN,
    DEFAULT_DWELL_MAX,
    DEFAULT_DWELL_MIN,
    DEFAULT_STEP_DELAY_MAX,
    DEFAULT_STEP_DELAY_MIN,
    DOMAIN,
    MAX_START_JITTER_MIN,
    OWNER_SEQUENCE,
    STEP_DESTINATION,
    STEP_DWELL,
    STEP_ORIGIN,
    STEP_PATH,
    STEP_RETURN,
    signal_update,
)
from .engine import SimulationEngine
from .light_control import LightController
from .models import LumaSettings, Route

_LOGGER = logging.getLogger(__name__)

# Every wait of a route goes through here, so tests can patch it.
_sleep = asyncio.sleep


def _range(
    config: Mapping[str, Any], low_key: str, high_key: str, defaults: tuple[float, float]
) -> tuple[float, float]:
    low, high = sorted(
        max(float(config.get(key, default)), 0.0)
        for key, default in zip((low_key, high_key), defaults, strict=True)
    )
    return low, high


class SequenceEngine:
    """Schedules and runs night routes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: Mapping[str, Any],
        settings: LumaSettings,
        controller: LightController,
        engine: SimulationEngine,
        routes: list[Route],
    ) -> None:
        """Initialize the sequence engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._settings = settings
        self._controller = controller
        self._engine = engine
        self._routes = routes
        # Seconds between two steps, and minutes at the destination.
        self._step_delay = _range(
            config,
            CONF_STEP_DELAY_MIN,
            CONF_STEP_DELAY_MAX,
            (DEFAULT_STEP_DELAY_MIN, DEFAULT_STEP_DELAY_MAX),
        )
        self._dwell = _range(
            config, CONF_DWELL_MIN, CONF_DWELL_MAX, (DEFAULT_DWELL_MIN, DEFAULT_DWELL_MAX)
        )

        self.next_run: datetime | None = None
        self.route: Route | None = None
        self.step: str | None = None

        self._started = False
        self._task: asyncio.Task[None] | None = None
        self._off_task: asyncio.Task[None] | None = None
        self._last_route_id: str | None = None
        # Same-room fallback for route lights that do not obey (set by the
        # integration), and the stand-ins used by the running route.
        self.alternatives: Callable[[str], list[str]] | None = None
        self._stand_ins: list[str] = []
        self._jitter: dict[date, float] = {}
        self._unsub_run: CALLBACK_TYPE | None = None
        self._unsub_end: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def routes(self) -> list[Route]:
        """Configured routes."""
        return list(self._routes)

    @property
    def running(self) -> bool:
        """Return True while a route runs."""
        return self._task is not None

    @property
    def active(self) -> bool:
        """Return True when sequences may be scheduled."""
        return self._engine.enabled and self._settings.sequences_enabled

    async def async_start(self) -> None:
        """Start scheduling once Home Assistant is running."""
        self._unsubs.append(self._engine.add_enabled_listener(self._handle_enabled))
        self._unsubs.append(self._controller.add_failure_listener(self._handle_failure))
        self._started = True
        # A route interrupted by a restart is not resumed: its lights go off.
        if self._controller.owned_on(OWNER_SEQUENCE):
            self._turn_off_gradually(None)
        self._reschedule()

    async def async_shutdown(self) -> None:
        """Stop timers and tasks without touching the lights."""
        self._started = False
        self._cancel_timers()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in (self._task, self._off_task):
            if task is not None:
                task.cancel()
        self._task = self._off_task = None
        self.route = self.step = None

    async def async_settings_changed(self, key: str) -> None:
        """Recompute the schedule after a sequence setting changed."""
        if not self._started:
            return
        if key == "sequences_enabled" and not self._settings.sequences_enabled:
            self._stop_route(gradual=True)
        self._reschedule(keep_next=key in ("sequence_min_interval", "sequence_max_interval"))

    def has_route(self, name: str) -> bool:
        """Return True if a route has this name."""
        return any(route.name == name for route in self._routes)

    @callback
    def async_run_now(self, name: str | None = None) -> Route:
        """Run one route now, whatever the master switch and the window."""
        if self.running:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="sequence_running"
            )
        if name is not None:
            route = next((r for r in self._routes if r.name == name), None)
            if route is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_route",
                    translation_placeholders={"route": name},
                )
        elif (route := self._choose_route()) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_routes"
            )
        self._start_route(route)
        return route

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _window(self, day: date) -> tuple[datetime, datetime] | None:
        """Return the (start, end) UTC window that starts on a local date."""
        tz = dt_util.get_default_time_zone()
        start = dt_util.as_utc(
            datetime.combine(day, self._settings.sequence_start_time, tzinfo=tz)
        )
        end = get_astral_event_next(self.hass, SUN_EVENT_SUNRISE, start)
        if end is None or end <= start:
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

    def _first_run(self, start: datetime) -> datetime:
        """Window start plus the random start delay, fixed per night."""
        if not self._settings.delayed_start:
            return start
        day = dt_util.as_local(start).date()
        if day not in self._jitter:
            self._jitter[day] = random.random() * MAX_START_JITTER_MIN
            limit = day - timedelta(days=3)
            for old in [d for d in self._jitter if d < limit]:
                del self._jitter[old]
        return start + timedelta(minutes=self._jitter[day])

    def _interval(self) -> timedelta:
        low, high = sorted(
            (self._settings.sequence_min_interval, self._settings.sequence_max_interval)
        )
        return timedelta(minutes=random.uniform(max(low, 1), max(high, 1)))

    @callback
    def _reschedule(self, *, keep_next: bool = False) -> None:
        kept = self.next_run if keep_next else None
        self._cancel_timers()
        if not self._started or not self.active:
            self._notify()
            return
        now = dt_util.utcnow()
        if (window := self._current_window(now)) is not None:
            start, end = window
            self._unsub_end = async_track_point_in_utc_time(
                self.hass, self._handle_window_end, end
            )
            run = kept if kept is not None and now < kept < end else None
            if run is None:
                first = self._first_run(start)
                # After a restart or a change inside the window, wait one interval.
                run = first if first > now else now + self._interval()
            if run < end:
                self._schedule_run(run)
                return
        if (window := self._next_window(now)) is not None:
            self._schedule_run(self._first_run(window[0]))
        self._notify()

    @callback
    def _schedule_run(self, when: datetime) -> None:
        self.next_run = when
        self._unsub_run = async_track_point_in_utc_time(
            self.hass, self._handle_run, when
        )
        self._notify()

    @callback
    def _cancel_timers(self) -> None:
        for unsub in (self._unsub_run, self._unsub_end):
            if unsub is not None:
                unsub()
        self._unsub_run = self._unsub_end = None
        self.next_run = None

    @callback
    def _handle_run(self, now: datetime) -> None:
        self._unsub_run = None
        if self.active and not self.running and self._current_window(now) is not None:
            if (route := self._choose_route()) is not None:
                self._start_route(route)
            else:
                _LOGGER.debug("No usable night route; waiting for the next slot")
        self.next_run = now + self._interval()
        self._reschedule(keep_next=True)

    @callback
    def _handle_window_end(self, _now: datetime) -> None:
        self._unsub_end = None
        self._stop_route(gradual=True)
        self._reschedule()

    @callback
    def _handle_enabled(self) -> None:
        if not self._engine.enabled:
            # The engine switches the route lights off with the stop selector.
            self._cancel_route()
        self._reschedule()

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    def _is_usable(self, route: Route) -> bool:
        for entity_id in (route.origin, route.destination):
            state = self.hass.states.get(entity_id)
            if state is None or state.state not in (STATE_ON, STATE_OFF):
                return False
        return True

    def _choose_route(self) -> Route | None:
        """Random usable route, not the previous one when there is another."""
        usable = [route for route in self._routes if self._is_usable(route)]
        if len(usable) > 1:
            usable = [route for route in usable if route.route_id != self._last_route_id]
        return random.choice(usable) if usable else None

    @callback
    def _start_route(self, route: Route) -> None:
        self._last_route_id = route.route_id
        self.route = route
        self.step = None
        self._task = self.hass.async_create_background_task(
            self._async_run_route(route),
            f"{DOMAIN}_sequence_{self._entry_id}",
            eager_start=False,
        )
        self._notify()

    async def _async_run_route(self, route: Route) -> None:
        controller = self._controller
        controller.lock(route.lights)
        self._stand_ins = []
        try:
            controller.record(None, "sequence_started", route=route.name)
            outward = [
                (STEP_ORIGIN, route.origin),
                *((STEP_PATH, light) for light in route.path),
                (STEP_DESTINATION, route.destination),
            ]
            for index, (step, light) in enumerate(outward):
                if index:
                    await self._step_wait()
                self._set_step(step)
                if not self._is_on(light):
                    await controller.async_switch(
                        light, True, OWNER_SEQUENCE, route=route.name
                    )

            self._set_step(STEP_DWELL)
            await _sleep(random.uniform(*self._dwell) * 60)

            # Lights standing in for ones that did not obey go off first.
            back = [
                *self._stand_ins,
                route.destination,
                *reversed(route.path),
                route.origin,
            ]
            self._set_step(STEP_RETURN)
            for index, light in enumerate(back):
                if index:
                    await self._step_wait()
                # Ownership rule: only lights this route turned on go off.
                if controller.owner_of(light) == OWNER_SEQUENCE and self._is_on(light):
                    await controller.async_switch(light, False, route=route.name)
            controller.record(None, "sequence_finished", route=route.name)
        except Exception:  # never leave the lights locked
            _LOGGER.exception("Unexpected error in night route %s", route.name)
        finally:
            controller.unlock([*route.lights, *self._stand_ins])
            self._stand_ins = []
            if self._task is asyncio.current_task():
                self._task = None
                self.route = self.step = None
            self._notify()

    @callback
    def _handle_failure(self, entity_id: str, turn_on: bool, owner: str) -> None:
        """A route light did not turn on: light another one of its room."""
        route = self.route
        if (
            route is None
            or not self.running
            or owner != OWNER_SEQUENCE
            or not turn_on
            or entity_id not in route.lights
            or self.alternatives is None
        ):
            return
        for alternative in self.alternatives(entity_id):
            state = self.hass.states.get(alternative)
            if (
                alternative in route.lights
                or alternative in self._stand_ins
                or alternative in self._controller.failed_tonight
                or self._controller.is_locked(alternative)
                or state is None
                or state.state != STATE_OFF
            ):
                continue
            self._controller.lock([alternative])
            self._stand_ins.append(alternative)
            self.hass.async_create_task(
                self._controller.async_switch(
                    alternative, True, OWNER_SEQUENCE, route=route.name, original=entity_id
                ),
                f"{DOMAIN}_sequence_fallback_{self._entry_id}",
            )
            return

    async def _step_wait(self) -> None:
        await _sleep(random.uniform(*self._step_delay))

    def _is_on(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_ON

    @callback
    def _set_step(self, step: str) -> None:
        self.step = step
        self._notify()

    @callback
    def _cancel_route(self) -> asyncio.Task[None] | None:
        """Cancel the running route; its lights are left as they are."""
        task = self._task
        if task is not None:
            task.cancel()
            self._task = None
            self.route = self.step = None
            self._notify()
        return task

    @callback
    def _stop_route(self, *, gradual: bool) -> None:
        """Cancel the running route and switch its lights off gradually."""
        name = self.route.name if self.route else None
        task = self._cancel_route()
        if gradual and (task is not None or self._controller.owned_on(OWNER_SEQUENCE)):
            self._turn_off_gradually(task, route=name)

    @callback
    def _turn_off_gradually(
        self, cancelled: asyncio.Task[None] | None, **extra: Any
    ) -> None:
        if self._off_task is not None:
            self._off_task.cancel()
        self._off_task = self.hass.async_create_background_task(
            self._async_turn_off(cancelled, extra),
            f"{DOMAIN}_sequence_off_{self._entry_id}",
            eager_start=False,
        )

    async def _async_turn_off(
        self, cancelled: asyncio.Task[None] | None, extra: dict[str, Any]
    ) -> None:
        try:
            if cancelled is not None:
                # Let the cancelled route finish its last switch first.
                await asyncio.wait([cancelled])
            await self._controller.async_turn_off_gradually(
                self._controller.owned_on(OWNER_SEQUENCE),
                self._settings.stop_interval,
                **{k: v for k, v in extra.items() if v is not None},
            )
        finally:
            if self._off_task is asyncio.current_task():
                self._off_task = None

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))
