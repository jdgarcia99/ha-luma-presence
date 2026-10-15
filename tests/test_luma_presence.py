"""Phase 1 tests for LumaPresence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.luma_presence.const import (
    CONF_ALARM,
    CONF_FRONT_LIGHTS,
    CONF_HUMAN_ONLY,
    CONF_LIGHTS,
    CONF_MARGIN_AFTER,
    CONF_MARGIN_BEFORE,
    CONF_PERSONS,
    CONF_REAR_LIGHTS,
    DOMAIN,
    REASON_ALARM,
    REASON_MARGIN,
    REASON_SIMULATION,
    STOP_KEEP_ON,
    STOP_OFF_NOW,
)

from .conftest import FRONT, LIGHTS

OPTIONS = {
    CONF_LIGHTS: LIGHTS,
    CONF_FRONT_LIGHTS: FRONT,
    CONF_PERSONS: ["person.mario"],
    CONF_ALARM: "alarm_control_panel.home_alarm",
    CONF_MARGIN_BEFORE: 2,
    CONF_MARGIN_AFTER: 2,
    CONF_HUMAN_ONLY: True,
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set("person.mario", "home")
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    entry = MockConfigEntry(domain=DOMAIN, title="LumaPresence", data={}, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow(hass: HomeAssistant) -> None:
    """The setup steps validate the lights and facades and create the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Casa", CONF_LIGHTS: []}
    )
    assert result["errors"] == {CONF_LIGHTS: "no_lights"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Casa", CONF_LIGHTS: LIGHTS}
    )
    assert result["step_id"] == "facades"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FRONT_LIGHTS: FRONT, CONF_REAR_LIGHTS: FRONT}
    )
    assert result["errors"] == {"base": "facade_overlap"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FRONT_LIGHTS: FRONT, CONF_REAR_LIGHTS: ["light.hall"]}
    )
    assert result["step_id"] == "learning"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HUMAN_ONLY: True}
    )
    assert result["step_id"] == "learning_preview"
    assert result["description_placeholders"]["count"] == str(len(LIGHTS))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "presence"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PERSONS: OPTIONS[CONF_PERSONS],
            CONF_ALARM: OPTIONS[CONF_ALARM],
            CONF_MARGIN_BEFORE: 2,
            CONF_MARGIN_AFTER: 2,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Casa"
    assert result["options"][CONF_LIGHTS] == LIGHTS
    assert result["options"][CONF_REAR_LIGHTS] == ["light.hall"]
    assert result["options"][CONF_ALARM] == OPTIONS[CONF_ALARM]


async def test_entities_created(hass: HomeAssistant, fake_lights) -> None:
    """All entities exist with the expected ids and defaults."""
    await _setup(hass)
    assert hass.states.get("switch.luma_presence_simulation").state == STATE_OFF
    assert hass.states.get("select.luma_presence_mode").state == "sun"
    assert hass.states.get("select.luma_presence_stop_behavior").state == "keep_if_night"
    assert hass.states.get("number.luma_presence_max_lights").state == "5"
    assert hass.states.get("time.luma_presence_start_time").state == "18:30:00"
    assert hass.states.get("sensor.luma_presence_status").state == "idle"
    assert hass.states.get("binary_sensor.luma_presence_learning_active").state == STATE_ON


async def test_time_mode_simulation_and_ownership(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Inside the window the simulation turns lights on; stopping respects ownership."""
    freezer.move_to(dt_util.as_utc(datetime(2026, 10, 15, 21, 0, tzinfo=dt_util.get_default_time_zone())))
    entry = await _setup(hass)
    engine = entry.runtime_data.engine

    await hass.services.async_call("select", "select_option",
        {"entity_id": "select.luma_presence_mode", "option": "time"}, blocking=True)
    await hass.services.async_call("switch", "turn_off",
        {"entity_id": "switch.luma_presence_delayed_start"}, blocking=True)
    await hass.services.async_call("switch", "turn_on",
        {"entity_id": "switch.luma_presence_simulation"}, blocking=True)
    await hass.async_block_till_done()
    for _ in range(10):  # let the background simulation task run its first step
        await asyncio.sleep(0)

    assert hass.states.get("sensor.luma_presence_status").state == "simulating"
    assert hass.states.get("sensor.luma_presence_next_event").attributes["kind"] == "end"
    # min_front_lights = 2 but only one front light exists: it must be on first.
    assert hass.states.get("light.bedroom").state == STATE_ON
    assert engine.owned_lights == ["light.bedroom"]

    # Learning is closed while the simulation is enabled.
    assert REASON_SIMULATION in entry.runtime_data.learning.reasons

    # A person turns on another light and touches an owned light.
    hass.states.async_set("light.hall", STATE_ON, context=Context(user_id="abc"))
    await engine._async_switch("light.salon", True)
    hass.states.async_set("light.salon", STATE_OFF)
    hass.states.async_set("light.salon", STATE_ON)
    await hass.async_block_till_done()
    assert engine.owned_lights == ["light.bedroom"]

    await hass.services.async_call(DOMAIN, "stop", {"lights_behavior": STOP_OFF_NOW}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("switch.luma_presence_simulation").state == STATE_OFF
    assert hass.states.get("light.bedroom").state == STATE_OFF
    assert hass.states.get("light.hall").state == STATE_ON
    assert hass.states.get("light.salon").state == STATE_ON
    assert hass.states.get("sensor.luma_presence_status").state == "idle"


async def test_outside_window_waits(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Outside the window the engine waits for the next start."""
    tz = dt_util.get_default_time_zone()
    freezer.move_to(dt_util.as_utc(datetime(2026, 10, 15, 12, 0, tzinfo=tz)))
    entry = await _setup(hass)
    await hass.services.async_call("select", "select_option",
        {"entity_id": "select.luma_presence_mode", "option": "time"}, blocking=True)
    await hass.services.async_call("switch", "turn_off",
        {"entity_id": "switch.luma_presence_delayed_start"}, blocking=True)
    await hass.services.async_call("switch", "turn_on",
        {"entity_id": "switch.luma_presence_simulation"}, blocking=True)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    assert engine.status == "waiting"
    assert engine.next_event_kind == "start"
    assert dt_util.as_local(engine.next_event).hour == 18
    assert dt_util.as_local(engine.next_event).minute == 30
    assert all(hass.states.get(e).state == STATE_OFF for e in LIGHTS)

    await hass.services.async_call(DOMAIN, "stop", {"lights_behavior": STOP_KEEP_ON}, blocking=True)


async def test_sun_window_ends_by_sunrise(hass: HomeAssistant, fake_lights) -> None:
    """Sun mode window starts at sunset and never passes sunrise."""
    entry = await _setup(hass)
    engine = entry.runtime_data.engine
    engine.settings.late_arrival = True
    engine.settings.late_end_time = engine.settings.late_end_time.replace(hour=11)
    window = engine._window(datetime(2026, 10, 15).date())
    assert window is not None
    start, end = window
    assert 18 <= dt_util.as_local(start).hour <= 20
    assert dt_util.as_local(end).hour <= 9  # capped by sunrise


async def test_learning_gate(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Human events are learned; departures discard the margin; returns block learning."""
    tz = dt_util.get_default_time_zone()
    t0 = dt_util.as_utc(datetime(2026, 10, 15, 8, 0, tzinfo=tz))
    freezer.move_to(t0)
    entry = await _setup(hass)
    learning = entry.runtime_data.learning
    assert learning.is_open

    # Automation-made change is ignored, human change is kept.
    hass.states.async_set("light.salon", STATE_ON, context=Context(parent_id="auto"))
    hass.states.async_set("light.hall", STATE_ON, context=Context(user_id="u"))
    await hass.async_block_till_done()
    assert [p[0] for p in learning._pending] == ["light.hall"]

    # Three hours later the event is older than the 2 h margin and is committed.
    freezer.move_to(t0 + timedelta(hours=3))
    learning._handle_tick(dt_util.utcnow())
    # The fake lights have no area.
    assert learning._days["2026-10-15"]["events"] == [["light.hall", "on", 8 * 3600, None]]
    assert learning._days["2026-10-15"]["exposure"] == [[8 * 3600, 9 * 3600]]

    # New event, then departure: it is discarded.
    hass.states.async_set("light.hall", STATE_OFF, context=Context(user_id="u"))
    hass.states.async_set("alarm_control_panel.home_alarm", "armed_away")
    await hass.async_block_till_done()
    assert not learning.is_open
    assert REASON_ALARM in learning.reasons
    assert len(learning._days["2026-10-15"]["events"]) == 1

    # Back after 3 days: return margin until the end of the return day.
    freezer.move_to(t0 + timedelta(days=3))
    hass.states.async_set("alarm_control_panel.home_alarm", "disarmed")
    await hass.async_block_till_done()
    assert not learning.is_open
    assert REASON_MARGIN in learning.reasons
    assert dt_util.as_local(learning.blocked_until).date().isoformat() == "2026-10-19"

    # Forgetting the period removes the learned day.
    await hass.services.async_call(DOMAIN, "forget_period",
        {"start_date": "2026-10-15", "end_date": "2026-10-15"}, blocking=True)
    assert "2026-10-15" not in learning._days


async def test_unload(hass: HomeAssistant, fake_lights) -> None:
    """The entry unloads cleanly."""
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
