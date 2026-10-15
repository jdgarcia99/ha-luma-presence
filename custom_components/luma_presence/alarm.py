"""Alarm control: arming the alarm starts the simulation, disarming stops it.

Only used when the owner enables it. Changes coming from ``unavailable`` or
``unknown`` are ignored, so a panel reconnecting never toggles the
simulation. Turning the master on when it is on (or off when it is off) does
nothing, so the owner's own arm/disarm automations keep working alongside.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging

from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, ORIGIN_ALARM
from .engine import SimulationEngine
from .light_control import LightController

_LOGGER = logging.getLogger(__name__)

_UNKNOWN = (STATE_UNAVAILABLE, STATE_UNKNOWN)
_OVERRIDE = "alarm_override"


class AlarmController:
    """Turns the master switch on and off from the alarm panel."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        alarm_entity: str,
        activate_states: Iterable[str],
        engine: SimulationEngine,
        controller: LightController,
    ) -> None:
        """Initialize the alarm controller."""
        self.hass = hass
        self._entry_id = entry_id
        self._alarm = alarm_entity
        self._activate = frozenset(activate_states)
        self._engine = engine
        self._controller = controller
        self._unsubs: list[CALLBACK_TYPE] = []

    @property
    def override(self) -> bool:
        """True after the master was turned off by hand while armed."""
        return bool(self._controller.get_value(_OVERRIDE))

    async def async_start(self) -> None:
        """Follow the alarm and reconcile what happened while HA was down."""
        self._unsubs.append(
            async_track_state_change_event(self.hass, [self._alarm], self._handle_alarm)
        )
        self._unsubs.append(self._engine.add_enabled_listener(self._handle_master))
        await self._async_reconcile()

    async def async_shutdown(self) -> None:
        """Stop following the alarm."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _is_rearm_boundary(self, state: str) -> bool:
        """States that end an arming, so a manual override no longer applies.

        Disarmed, or an armed state that does not activate the simulation.
        Pending, triggered or arming are part of the same arming.
        """
        return state == AlarmControlPanelState.DISARMED or (
            state.startswith("armed_") and state not in self._activate
        )

    async def _async_reconcile(self) -> None:
        state = self.hass.states.get(self._alarm)
        if state is None or state.state in _UNKNOWN:
            return  # do nothing until the panel reports a real state
        if self._is_rearm_boundary(state.state):
            self._set_override(False)
        if state.state in self._activate:
            if not self._engine.enabled and not self.override:
                _LOGGER.info("Alarm %s is armed: starting the simulation", self._alarm)
                await self._engine.async_set_enabled(True, origin=ORIGIN_ALARM)
        elif (
            state.state == AlarmControlPanelState.DISARMED
            and self._engine.enabled
            and self._engine.activated_by == ORIGIN_ALARM
        ):
            # Disarmed while Home Assistant was down.
            _LOGGER.info("Alarm %s is disarmed: stopping the simulation", self._alarm)
            await self._engine.async_set_enabled(False, origin=ORIGIN_ALARM)

    @callback
    def _handle_alarm(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or old.state in _UNKNOWN or new.state in _UNKNOWN:
            return
        if self._is_rearm_boundary(new.state):
            self._set_override(False)
        if new.state in self._activate and old.state not in self._activate:
            if self.override:
                return  # turned off by hand during this arming
            self._set_enabled(True)
        elif new.state == AlarmControlPanelState.DISARMED:
            self._set_enabled(False)
        # Arming, pending, triggered and other armed states: no change.

    @callback
    def _handle_master(self) -> None:
        if self._engine.last_origin == ORIGIN_ALARM:
            return
        state = self.hass.states.get(self._alarm)
        armed = state is not None and state.state in self._activate
        if not self._engine.enabled and armed:
            # Turned off by hand while armed: stay off until the next arming.
            self._set_override(True)
        elif self._engine.enabled:
            self._set_override(False)

    @callback
    def _set_override(self, value: bool) -> None:
        if value != self.override:
            self._controller.set_value(_OVERRIDE, True if value else None)

    @callback
    def _set_enabled(self, enabled: bool) -> None:
        self.hass.async_create_task(
            self._engine.async_set_enabled(enabled, origin=ORIGIN_ALARM),
            f"{DOMAIN}_alarm_{self._entry_id}",
        )
