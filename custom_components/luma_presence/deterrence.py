"""Deterrence: when someone is detected outside at night during an absence,
the house reacts like people waking up. Lights only: never sirens, locks or
doors.

Active during a real absence (alarm armed away or vacation, or nobody home)
while it is dark, from the evening darkness decision until the daylight
guard ends the night, whether or not a simulation window is running.

Reaction: after 2-6 s one light of the trigger's facade goes on, after
10-25 s the path lights in order, they stay on for 3-8 minutes, and then the
lights are handed back: to tonight's plan or the simulation if one is
running, otherwise switched off gradually.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import random
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DETERRENCE_COOLDOWN,
    CONF_DETERRENCE_ENABLED,
    CONF_DETERRENCE_MAX,
    CONF_FACADE,
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    CONF_PATH,
    CONF_REAR_LIGHTS,
    CONF_TRIGGER_ENTITY,
    DAYLIGHT_OFF_MAX_S,
    DEFAULT_DETERRENCE_COOLDOWN,
    DEFAULT_DETERRENCE_ENABLED,
    DEFAULT_DETERRENCE_MAX,
    DETERRENCE_FIRST_DELAY_S,
    DETERRENCE_HOLD_MIN,
    DETERRENCE_PATH_DELAY_S,
    DETERRENCE_STEP_DELAY_S,
    DOMAIN,
    EVENT_DETERRENCE,
    FACADE_FRONT,
    OWNER_DETERRENCE,
    OWNER_SIMULATION,
    signal_update,
)
from .light_control import LightController
from .models import LumaSettings
from .plan import night_of, to_night_seconds
from .scope import entity_area_id

if TYPE_CHECKING:
    from .engine import SimulationEngine
    from .plan import PlanPlayer

_LOGGER = logging.getLogger(__name__)

# Every wait of a reaction goes through here, so tests can patch it.
_sleep = asyncio.sleep

_UNKNOWN = (None, STATE_UNAVAILABLE, STATE_UNKNOWN)
# A light the plan switches on within this margin of now is plausible.
PLAUSIBLE_MARGIN_S = 3600
# Seconds between lights when handing them back by switching them off.
HAND_BACK_INTERVAL_S = 25


@dataclass(frozen=True)
class DeterrenceTrigger:
    """A sensor or doorbell outside, its facade and the lights toward the door."""

    trigger_id: str
    name: str
    entity_id: str
    facade: str
    path: tuple[str, ...]

    @classmethod
    def from_data(
        cls, trigger_id: str, name: str, data: Mapping[str, Any]
    ) -> DeterrenceTrigger:
        """Build a trigger from the data of its config subentry."""
        return cls(
            trigger_id=trigger_id,
            name=name,
            entity_id=data[CONF_TRIGGER_ENTITY],
            facade=data.get(CONF_FACADE, FACADE_FRONT),
            path=tuple(data.get(CONF_PATH) or ()),
        )


class DeterrenceEngine:
    """Reacts to people outside the empty house at night."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: Mapping[str, Any],
        settings: LumaSettings,
        controller: LightController,
        engine: SimulationEngine,
        player: PlanPlayer,
        triggers: list[DeterrenceTrigger],
    ) -> None:
        """Initialize the deterrence engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._settings = settings
        self._controller = controller
        self._engine = engine
        self._player = player
        self.triggers = triggers
        self.enabled = bool(
            config.get(CONF_DETERRENCE_ENABLED, DEFAULT_DETERRENCE_ENABLED)
        )
        self._cooldown = timedelta(
            minutes=float(
                config.get(CONF_DETERRENCE_COOLDOWN, DEFAULT_DETERRENCE_COOLDOWN)
            )
        )
        self._max = int(config.get(CONF_DETERRENCE_MAX, DEFAULT_DETERRENCE_MAX))
        lights = list(config.get(CONF_LIGHTS) or [])
        front = set(config.get(CONF_FRONT_LIGHTS) or [])
        rear = set(config.get(CONF_REAR_LIGHTS) or [])
        # Simulation lights of each facade, in configuration order.
        self._facades = {
            FACADE_FRONT: [light for light in lights if light in front],
            "rear": [light for light in lights if light in rear],
        }
        # Set by the integration: real absence, darkness and the daylight guard.
        self.real_absence: Callable[[], bool] = player.real_absence
        self.dark_now: Callable[[], bool] = lambda: True
        self.daylight: Callable[[], bool] = lambda: False

        self.last: dict[str, Any] | None = None
        self.reactions_tonight = 0
        self._night = night_of(dt_util.as_local(dt_util.utcnow()))
        self._last_by_trigger: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._off_task: asyncio.Task[None] | None = None
        self._trigger: DeterrenceTrigger | None = None
        self._held: list[str] = []  # lights this reaction switched on
        self._locked: list[str] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """Return True while a reaction runs."""
        return self._task is not None

    @callback
    def async_start(self) -> None:
        """Follow the triggers."""
        self._unsubs.append(self._controller.add_failure_listener(self._handle_failure))
        # A reaction interrupted by a restart is not resumed: its lights go off.
        if self._controller.owned_on(OWNER_DETERRENCE):
            self._turn_off(
                self._controller.owned_on(OWNER_DETERRENCE), HAND_BACK_INTERVAL_S
            )
        if self.enabled and self.triggers:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    [t.entity_id for t in self.triggers],
                    self._handle_trigger,
                )
            )

    async def async_shutdown(self) -> None:
        """Stop without touching the lights."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in (self._task, self._off_task):
            if task is not None:
                task.cancel()
        self._task = self._off_task = None

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------
    def active(self) -> bool:
        """Deterrence works during a real absence while it is dark."""
        return self.enabled and self.real_absence() and self.dark_now()

    @callback
    def _handle_trigger(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None:
            return
        entity_id = event.data["entity_id"]
        if entity_id.startswith("event."):
            # An event entity's state is the time of its last event.
            fired = (
                new.state not in _UNKNOWN
                and old.state not in _UNKNOWN
                and new.state != old.state
            )
        else:
            fired = old.state == STATE_OFF and new.state == STATE_ON
        if not fired:
            return
        trigger = next((t for t in self.triggers if t.entity_id == entity_id), None)
        if trigger is None or not self.active():
            return
        now = dt_util.utcnow()
        self._new_night()
        if (
            last := self._last_by_trigger.get(trigger.trigger_id)
        ) and now - last < self._cooldown:
            _LOGGER.debug("Deterrence: %s is cooling down", trigger.name)
            return
        if self.reactions_tonight >= self._max:
            _LOGGER.debug("Deterrence: nightly limit reached")
            return
        if self.running:
            return
        self._last_by_trigger[trigger.trigger_id] = now
        self._start(trigger, test=False)

    @callback
    def async_test(self) -> None:
        """Run the reaction of the first trigger now, whatever the absence."""
        if self.daylight():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="daylight"
            )
        if not self.triggers:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_deterrence_triggers"
            )
        if self.running:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="deterrence_running"
            )
        self._new_night()
        self._start(self.triggers[0], test=True)

    @callback
    def _new_night(self) -> None:
        night = night_of(dt_util.as_local(dt_util.utcnow()))
        if night != self._night:
            self._night = night
            self.reactions_tonight = 0

    @callback
    def _start(self, trigger: DeterrenceTrigger, *, test: bool) -> None:
        self.reactions_tonight += 1
        self._trigger = trigger
        self._task = self.hass.async_create_background_task(
            self._async_react(trigger, test),
            f"{DOMAIN}_deterrence_{self._entry_id}",
            eager_start=False,
        )
        self._notify()

    # ------------------------------------------------------------------
    # Reaction
    # ------------------------------------------------------------------
    def _usable_off(self, light: str) -> bool:
        state = self.hass.states.get(light)
        return (
            state is not None
            and state.state == STATE_OFF
            and not self._controller.is_locked(light)
            and light not in self._controller.failed_tonight
            and light not in self._held
        )

    def choose_light(self, facade: str) -> str | None:
        """An off light of the facade; a room the plan uses about now first."""
        candidates = [
            light for light in self._facades.get(facade, []) if self._usable_off(light)
        ]
        if not candidates:
            return None
        local = dt_util.as_local(dt_util.utcnow())
        planned = self._player.planned_near(
            to_night_seconds(local, night_of(local)), PLAUSIBLE_MARGIN_S
        )
        if planned:
            areas = {entity_area_id(self.hass, light) for light in planned} - {None}
            for light in candidates:
                if light in planned or entity_area_id(self.hass, light) in areas:
                    return light
        return candidates[0]

    async def _async_react(self, trigger: DeterrenceTrigger, test: bool) -> None:
        controller = self._controller
        self._held = []
        try:
            await _sleep(random.uniform(*DETERRENCE_FIRST_DELAY_S))
            first = self.choose_light(trigger.facade)
            lights = [light for light in (first, *trigger.path) if light is not None]
            self._lock(lights)
            self.last = {
                "time": dt_util.utcnow(),
                "trigger": trigger.entity_id,
                "facade": trigger.facade,
                "lights": lights,
            }
            self.hass.bus.async_fire(
                EVENT_DETERRENCE,
                {
                    "entry_id": self._entry_id,
                    "trigger": trigger.entity_id,
                    "facade": trigger.facade,
                    "lights": lights,
                    "time": self.last["time"].isoformat(),
                    "test": test,
                },
            )
            controller.record(
                None,
                "deterrence",
                trigger=trigger.entity_id,
                facade=trigger.facade,
                test=test,
            )
            if first is not None:
                await self._async_on(first, trigger)
            if trigger.path:
                await _sleep(random.uniform(*DETERRENCE_PATH_DELAY_S))
                for index, light in enumerate(trigger.path):
                    if index:
                        await _sleep(random.uniform(*DETERRENCE_STEP_DELAY_S))
                    await self._async_on(light, trigger)
            await _sleep(random.uniform(*DETERRENCE_HOLD_MIN) * 60)
            self._hand_back()
        except asyncio.CancelledError:
            raise
        except Exception:  # never leave the lights locked
            _LOGGER.exception("Unexpected error in the deterrence reaction")
        finally:
            self._unlock()
            if self._task is asyncio.current_task():
                self._task = None
                self._trigger = None
            self._notify()

    async def _async_on(self, light: str, trigger: DeterrenceTrigger) -> None:
        state = self.hass.states.get(light)
        if state is None or state.state != STATE_OFF:
            return  # already on (or unavailable): nothing to hand back
        self._held.append(light)
        await self._controller.async_switch(
            light, True, OWNER_DETERRENCE, trigger=trigger.entity_id
        )

    @callback
    def _hand_back(self) -> None:
        """Lights go back to tonight's plan or the simulation, else off."""
        off: list[str] = []
        simulating = self._engine.status == "simulating"
        for light in self._held:
            if self._controller.owner_of(light) != OWNER_DETERRENCE:
                continue  # someone took it over
            desired = self._player.desired_state(light)
            if desired == STATE_ON or (
                desired is None
                and simulating
                and not self._player.active
                and light in self._engine.lights
            ):
                # The plan wants it on, or the Sun/Time loop takes it over.
                self._controller.transfer(light, OWNER_SIMULATION)
            else:
                off.append(light)
        self._unlock()
        if off:
            self._turn_off(off, HAND_BACK_INTERVAL_S)

    @callback
    def async_check_daylight(self) -> None:
        """Daylight guard: end the reaction, its lights off within 3 minutes."""
        if not self.daylight():
            return
        lights = self._controller.owned_on(OWNER_DETERRENCE)
        if self._task is not None:
            self._task.cancel()
            self._task = None
            self._trigger = None
        self._unlock()
        if lights:
            self._turn_off(lights, DAYLIGHT_OFF_MAX_S / max(len(lights) - 1, 1))
        self._notify()

    @callback
    def _turn_off(self, lights: list[str], interval: float) -> None:
        if self._off_task is not None:
            self._off_task.cancel()

        async def _off() -> None:
            try:
                await self._controller.async_turn_off_gradually(
                    [
                        light
                        for light in lights
                        if self._controller.owner_of(light) == OWNER_DETERRENCE
                    ],
                    interval,
                )
            finally:
                if self._off_task is asyncio.current_task():
                    self._off_task = None

        self._off_task = self.hass.async_create_background_task(
            _off(), f"{DOMAIN}_deterrence_off_{self._entry_id}", eager_start=False
        )

    @callback
    def _lock(self, lights: list[str]) -> None:
        self._controller.lock(lights)
        self._locked.extend(lights)

    @callback
    def _unlock(self) -> None:
        if self._locked:
            locked, self._locked = self._locked, []
            self._controller.unlock(locked)

    @callback
    def _handle_failure(self, entity_id: str, turn_on: bool, owner: str) -> None:
        """A light did not turn on: another off light of the same facade."""
        trigger = self._trigger
        if (
            trigger is None
            or not self.running
            or owner != OWNER_DETERRENCE
            or not turn_on
            or entity_id not in self._held
        ):
            return
        if (alternative := self.choose_light(trigger.facade)) is None:
            return
        self._lock([alternative])
        self._held.append(alternative)
        if self.last is not None:
            self.last = {**self.last, "lights": [*self.last["lights"], alternative]}
        self.hass.async_create_task(
            self._controller.async_switch(
                alternative,
                True,
                OWNER_DETERRENCE,
                trigger=trigger.entity_id,
                original=entity_id,
            ),
            f"{DOMAIN}_deterrence_fallback_{self._entry_id}",
        )

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))
