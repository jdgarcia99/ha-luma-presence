"""Tests for v0.5.0: built-in alarm control."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    CONF_ALARM,
    CONF_ALARM_ACTIVATE_STATES,
    CONF_ALARM_CONTROL,
    DOMAIN,
    EVENT_ACTION,
    REASON_ALARM_VACATION,
)

from .test_v0_2_0 import OPTIONS

ALARM = "alarm_control_panel.home_alarm"
MASTER = "switch.luma_presence_simulation"
CONTROL = {CONF_ALARM_CONTROL: True}


async def _setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any] | None = None,
    alarm: str = "disarmed",
    hour: int = 12,
) -> MockConfigEntry:
    tz = dt_util.get_default_time_zone()
    freezer.move_to(dt_util.as_utc(datetime(2026, 10, 15, hour, 0, tzinfo=tz)))
    hass.states.async_set("person.mario", "home")
    hass.states.async_set(ALARM, alarm)
    entry = MockConfigEntry(
        domain=DOMAIN, title="LumaPresence", options={**OPTIONS, **(options or {})}, version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _restart(hass: HomeAssistant, entry: MockConfigEntry, alarm: str | None = None) -> None:
    """Unload, change the alarm while "down", and set up again."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    if alarm is not None:
        hass.states.async_set(ALARM, alarm)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _alarm(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(ALARM, state)
    await hass.async_block_till_done()


async def _master(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": MASTER}, blocking=True
    )
    await hass.async_block_till_done()


def _state(hass: HomeAssistant):
    return hass.states.get(MASTER)


async def test_off_by_default(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Without alarm control, arming changes nothing."""
    await _setup(hass, freezer)
    await _alarm(hass, "armed_away")
    assert _state(hass).state == STATE_OFF


async def test_arm_and_disarm(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Arming away starts the simulation; disarming stops it with the stop selector."""
    entry = await _setup(hass, freezer, CONTROL, hour=21)
    events = async_capture_events(hass, EVENT_ACTION)
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.luma_presence_stop_behavior", "option": "off_now"},
        blocking=True,
    )

    await _alarm(hass, "armed_away")
    state = _state(hass)
    assert state.state == STATE_ON
    assert state.attributes["activated_by"] == "alarm"
    assert state.attributes["activated_at"] == dt_util.utcnow().isoformat()
    started = [e for e in events if e.data["action"] == "simulation_started"]
    assert started and started[0].data["origin"] == "alarm"
    # Inside the window the simulation turns lights on.
    engine = entry.runtime_data.engine
    for _ in range(200):
        if engine.owned_lights:
            break
        await asyncio.sleep(0)
    owned = engine.owned_lights
    assert owned

    await _alarm(hass, "disarmed")
    state = _state(hass)
    assert state.state == STATE_OFF
    assert state.attributes["activated_by"] is None
    assert state.attributes["activated_at"] is None
    stopped = [e for e in events if e.data["action"] == "simulation_stopped"]
    assert stopped[-1].data["origin"] == "alarm"
    # "Turn them off at once" was applied.
    assert all(hass.states.get(light).state == STATE_OFF for light in owned)


async def test_activating_states(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Armed vacation is in the default list; armed home is not."""
    await _setup(hass, freezer, CONTROL)
    await _alarm(hass, "armed_home")
    assert _state(hass).state == STATE_OFF
    await _alarm(hass, "disarmed")
    await _alarm(hass, "armed_vacation")
    assert _state(hass).state == STATE_ON


async def test_unavailable_changes_are_ignored(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Coming back from unavailable or unknown never toggles the simulation."""
    await _setup(hass, freezer, CONTROL, alarm=STATE_UNAVAILABLE)
    await _alarm(hass, "armed_away")
    assert _state(hass).state == STATE_OFF

    await _alarm(hass, "disarmed")
    await _alarm(hass, "armed_away")
    assert _state(hass).state == STATE_ON
    await _alarm(hass, STATE_UNKNOWN)
    await _alarm(hass, "disarmed")
    assert _state(hass).state == STATE_ON


async def test_triggered_keeps_running(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Pending and triggered do not stop the simulation."""
    await _setup(hass, freezer, CONTROL)
    for state in ("arming", "armed_away", "pending", "triggered"):
        await _alarm(hass, state)
    assert _state(hass).state == STATE_ON


async def test_manual_override(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A manual off while armed holds until the next arming."""
    await _setup(hass, freezer, CONTROL)
    await _alarm(hass, "armed_away")
    await _master(hass, False)
    assert _state(hass).state == STATE_OFF

    # Same arming: an alarm going through triggered and back does not re-enable it.
    await _alarm(hass, "triggered")
    await _alarm(hass, "armed_away")
    assert _state(hass).state == STATE_OFF

    await _alarm(hass, "disarmed")
    await _alarm(hass, "arming")
    await _alarm(hass, "armed_away")
    assert _state(hass).state == STATE_ON


async def test_coexists_with_automations(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Turning on an already-on master (switch or service) is a no-op."""
    await _setup(hass, freezer, CONTROL)
    await _alarm(hass, "armed_away")
    events = async_capture_events(hass, EVENT_ACTION)
    before = _state(hass).attributes

    await _master(hass, True)
    await hass.services.async_call(DOMAIN, "start", {}, blocking=True)
    await hass.async_block_till_done()
    after = _state(hass).attributes
    assert after["activated_by"] == "alarm"
    assert after["activated_at"] == before["activated_at"]
    assert not events

    # And an automation disarming after the alarm already did is a no-op too.
    await _alarm(hass, "disarmed")
    await hass.services.async_call(DOMAIN, "stop", {}, blocking=True)
    assert _state(hass).state == STATE_OFF


# ----------------------------------------------------------------------
# Startup reconciliation
# ----------------------------------------------------------------------
async def test_startup_armed_turns_on(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Armed at startup with the master off: it turns on."""
    await _setup(hass, freezer, CONTROL, alarm="armed_away")
    assert _state(hass).state == STATE_ON
    assert _state(hass).attributes["activated_by"] == "alarm"


async def test_startup_disarmed_after_alarm_turns_off(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Disarmed while down, with the master on by the alarm: it turns off."""
    entry = await _setup(hass, freezer, CONTROL)
    await _alarm(hass, "armed_away")
    await _restart(hass, entry, alarm="disarmed")
    assert _state(hass).state == STATE_OFF


async def test_startup_keeps_manual_master(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Disarmed, with the master on by hand: it stays on, still "manual"."""
    entry = await _setup(hass, freezer, CONTROL)
    await _master(hass, True)
    await _restart(hass, entry)
    assert _state(hass).state == STATE_ON
    assert _state(hass).attributes["activated_by"] == "manual"


@pytest.mark.parametrize("master_on", [True, False])
async def test_startup_unavailable_does_nothing(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, master_on: bool
) -> None:
    """With the panel unavailable at startup nothing changes."""
    entry = await _setup(hass, freezer, CONTROL)
    if master_on:
        await _alarm(hass, "armed_away")
    await _restart(hass, entry, alarm=STATE_UNAVAILABLE)
    assert _state(hass).state == (STATE_ON if master_on else STATE_OFF)


async def test_startup_respects_override(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A manual off during this arming survives a restart."""
    entry = await _setup(hass, freezer, CONTROL)
    await _alarm(hass, "armed_away")
    await _master(hass, False)
    await _restart(hass, entry)
    assert _state(hass).state == STATE_OFF


# ----------------------------------------------------------------------
# Learning and options
# ----------------------------------------------------------------------
async def test_vacation_is_an_absence(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """With alarm control, armed vacation closes learning as an absence."""
    entry = await _setup(hass, freezer, CONTROL)
    await _alarm(hass, "armed_vacation")
    assert REASON_ALARM_VACATION in entry.runtime_data.learning.reasons


async def test_presence_validation(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Alarm control needs an alarm panel and at least one state."""
    entry = await _setup(hass, freezer)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "presence"})
    base = {"persons": ["person.mario"], "margin_before_hours": 2, "margin_after_hours": 2}

    result = await flow.async_configure(
        result["flow_id"],
        {**base, CONF_ALARM_CONTROL: True, CONF_ALARM_ACTIVATE_STATES: ["armed_away"]},
    )
    assert result["errors"] == {CONF_ALARM: "alarm_required"}

    result = await flow.async_configure(
        result["flow_id"],
        {**base, CONF_ALARM: ALARM, CONF_ALARM_CONTROL: True, CONF_ALARM_ACTIVATE_STATES: []},
    )
    assert result["errors"] == {CONF_ALARM_ACTIVATE_STATES: "activate_states_required"}

    result = await flow.async_configure(
        result["flow_id"],
        {**base, CONF_ALARM: ALARM, CONF_ALARM_CONTROL: True,
         CONF_ALARM_ACTIVATE_STATES: ["armed_away", "armed_night"]},
    )
    assert result["type"] is FlowResultType.MENU
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALARM_CONTROL] is True
    assert entry.options[CONF_ALARM_ACTIVATE_STATES] == ["armed_away", "armed_night"]
