"""Tests for v0.7.1: wake-up on non-workdays and the pending evening start."""

from __future__ import annotations

from datetime import date, timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date

from custom_components.luma_presence.const import (
    CONF_DARKNESS_SENSOR,
    CONF_MORNING_ENABLED,
    CONF_MORNING_NONWORKDAY_MODE,
    CONF_MORNING_NONWORKDAY_START,
    CONF_MORNING_START,
    CONF_WORKDAY_SENSOR,
    MORNING_RULE_LEARNED,
    MORNING_RULE_NONWORKDAY_LATER,
    MORNING_RULE_NONWORKDAY_SKIP,
    MORNING_RULE_WORKDAY,
    START_CLAMPED_LATEST,
    START_DARKNESS,
    START_PENDING,
)

from .test_v0_7_0 import (
    FRIDAY,
    HOLIDAY,
    LUX,
    TODAY,
    WORKDAY,
    _local,
    _move,
    _setup,
    _workday,
)

SATURDAY = FRIDAY + timedelta(days=1)
LATER = {
    CONF_MORNING_ENABLED: True,
    CONF_MORNING_NONWORKDAY_MODE: "later",
    CONF_MORNING_NONWORKDAY_START: "07:45:00",
}


def _sensor(hass: HomeAssistant, entry) -> dict:
    entry.runtime_data.dream._notify()
    return hass.states.get("sensor.luma_presence_context").attributes


async def test_nonworkday_later(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Sun/Time: workdays wake up at morning_start, non-workdays later."""
    entry = await _setup(hass, freezer, LATER)
    engine = entry.runtime_data.engine
    assert engine._morning(FRIDAY)[0] == _local(FRIDAY, 6, 30)
    assert engine._morning_rule(FRIDAY) == MORNING_RULE_WORKDAY
    assert engine._morning(SATURDAY)[0] == _local(SATURDAY, 7, 45)
    assert engine._morning_rule(SATURDAY) == MORNING_RULE_NONWORKDAY_LATER
    # The end is unchanged: at sunrise without a sensor.
    sunrise = get_astral_event_date(hass, "sunrise", SATURDAY)
    assert engine._morning(SATURDAY)[1] == sunrise
    # Thursday noon: the next wake-up is Friday's, a workday.
    window = _sensor(hass, entry)["morning_window"]
    assert window["rule"] == MORNING_RULE_WORKDAY
    assert window["start"] == _local(FRIDAY, 6, 30).isoformat()


async def test_nonworkday_default_after_sunrise(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Default (later, 08:30): after sunrise in October, so no wake-up that day."""
    entry = await _setup(hass, freezer, {CONF_MORNING_ENABLED: True})
    engine = entry.runtime_data.engine
    assert engine._nonworkday_mode == "later"
    assert engine._morning(FRIDAY) is not None
    assert engine._morning(SATURDAY) is None


async def test_holiday_is_a_nonworkday(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A holiday (here from Workday) wakes up like a weekend."""
    _workday(hass, {HOLIDAY})
    entry = await _setup(hass, freezer, {**LATER, CONF_WORKDAY_SENSOR: WORKDAY})
    await hass.async_block_till_done(wait_background_tasks=True)
    await entry.runtime_data.context.async_ensure_holidays(
        TODAY, HOLIDAY + timedelta(days=1)
    )
    engine = entry.runtime_data.engine
    eve = HOLIDAY - timedelta(days=1)
    assert engine._morning(eve)[0] == _local(eve, 6, 30)
    assert engine._morning(HOLIDAY)[0] == _local(HOLIDAY, 7, 45)
    assert engine._morning_rule(HOLIDAY) == MORNING_RULE_NONWORKDAY_LATER


async def test_nonworkday_skip(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """skip: no wake-up on non-workdays; the context sensor says why."""
    entry = await _setup(
        hass, freezer, {**LATER, CONF_MORNING_NONWORKDAY_MODE: "skip"}, day=FRIDAY
    )
    engine = entry.runtime_data.engine
    assert engine._morning(FRIDAY) is not None
    assert engine._morning(SATURDAY) is None
    # Friday noon: the next morning is Saturday's, skipped.
    assert _sensor(hass, entry)["morning_window"] == {
        "start": None,
        "end": None,
        "rule": MORNING_RULE_NONWORKDAY_SKIP,
    }


async def test_learned_modes_unaffected(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Replay and Dream follow what they learned, whatever the non-workday rule."""
    entry = await _setup(hass, freezer, {**LATER, CONF_MORNING_NONWORKDAY_MODE: "skip"})
    engine = entry.runtime_data.engine
    for mode in ("dream", "replay"):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.luma_presence_mode", "option": mode},
            blocking=True,
        )
        # Nothing learned yet: morning_start, also on Saturday.
        assert engine._morning(SATURDAY)[0] == _local(SATURDAY, 6, 30)
        assert engine._morning_rule(SATURDAY) == MORNING_RULE_LEARNED
    assert _sensor(hass, entry)["morning_window"]["rule"] == MORNING_RULE_LEARNED


async def test_nonworkday_start_validation(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """With `later`, the non-workday start must be later than morning_start."""
    entry = await _setup(hass, freezer)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "context"})
    result = await flow.async_configure(
        result["flow_id"],
        {CONF_MORNING_START: "07:00:00", CONF_MORNING_NONWORKDAY_START: "07:00:00"},
    )
    assert result["errors"] == {
        CONF_MORNING_NONWORKDAY_START: "nonworkday_start_not_later"
    }
    # With skip the time is not used: accepted.
    result = await flow.async_configure(
        result["flow_id"],
        {
            CONF_MORNING_START: "07:00:00",
            CONF_MORNING_NONWORKDAY_MODE: "skip",
            CONF_MORNING_NONWORKDAY_START: "07:00:00",
        },
    )
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert entry.options[CONF_MORNING_NONWORKDAY_MODE] == "skip"


async def test_pending_start_reason(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """`pending` until the evening start is decided, then the real reason."""
    hass.states.async_set(LUX, "500")
    entry = await _setup(hass, freezer, {CONF_DARKNESS_SENSOR: LUX}, hour=18)
    engine = entry.runtime_data.engine
    # Simulation off and still bright: not decided.
    assert engine.evening_start_reason() == START_PENDING
    entry.runtime_data.dream._notify()
    context = hass.states.get("sensor.luma_presence_context")
    assert context.attributes["evening_start_reason"] == START_PENDING
    assert context.state == "normal"

    # Dark from 19:00: pending until confirmed, then darkness.
    await _move(hass, freezer, _local(TODAY, 19, 0))
    hass.states.async_set(LUX, "10")
    await hass.async_block_till_done()
    await _move(hass, freezer, _local(TODAY, 19, 3))
    assert engine.evening_start_reason() == START_PENDING
    await _move(hass, freezer, _local(TODAY, 19, 6))
    assert engine.evening_start_reason() == START_DARKNESS

    # Another evening that stays bright: the latest bound, once it is reached.
    tomorrow = TODAY + timedelta(days=1)
    hass.states.async_set(LUX, "500")
    await _move(hass, freezer, _local(tomorrow, 18, 0))
    assert engine.evening_start_reason() == START_PENDING
    await _move(hass, freezer, _local(tomorrow, 21, 0))
    assert engine.evening_start_reason() == START_CLAMPED_LATEST


def test_dates() -> None:
    """The dates used above are what the tests assume."""
    assert FRIDAY.weekday() == 4
    assert SATURDAY == date(2026, 10, 17)
    assert HOLIDAY.weekday() == 2
