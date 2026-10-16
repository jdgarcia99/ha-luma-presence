"""Tests for v0.9.0: optional generative AI and diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util

from custom_components.luma_presence import ai as ai_module, dream as dream_module
from custom_components.luma_presence.ai import (
    AiAssistant,
    Modifier,
    RoomKeys,
    apply_scenario,
    area_names,
    build_keys,
    parse_scenario,
    report_payload,
    room_type,
    scenario_payload,
    selected,
)
from custom_components.luma_presence.const import (
    AI_DAILY_LIMIT,
    AI_ERROR,
    AI_INVALID,
    AI_NOT_SELECTED,
    AI_OK,
    AI_TIMEOUT,
    CONF_AI_MAX_CALLS,
    CONF_AI_REPORT,
    CONF_AI_SCENARIO_PROBABILITY,
    CONF_AI_TASK_ENTITY,
    CONF_EVENT_TYPES,
    CONF_FACADE,
    CONF_PATH,
    CONF_PERSONS,
    CONF_REPORT_NOTIFY,
    CONF_TRIGGER_ENTITY,
    SCENARIO_LOW_SCORE,
)
from custom_components.luma_presence.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.luma_presence.dream import Candidate, make_candidates
from custom_components.luma_presence.model import WEEKDAY, build_day_type

from .test_v0_6_0 import TODAY, _dream_days, _nights, _setup as _setup_dream
from .test_v0_8_0 import _setup

AI_ENTITY = "ai_task.assistant"
AI_OPTIONS = {CONF_AI_TASK_ENTITY: AI_ENTITY, CONF_AI_SCENARIO_PROBABILITY: 100}


def _ai_task(
    hass: HomeAssistant, reply: Callable[[ServiceCall], Any]
) -> list[ServiceCall]:
    """A fake ai_task.generate_data returning `reply(call)` as its data."""
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> dict[str, Any]:
        calls.append(call)
        result = reply(call)
        if asyncio.iscoroutine(result):
            result = await result
        return {"conversation_id": "1", "data": result}

    hass.services.async_register(
        "ai_task", "generate_data", _handler, supports_response=SupportsResponse.ONLY
    )
    return calls


def _keys() -> RoomKeys:
    return build_keys(
        ["salon", "dormitorio_lucia"],
        {"salon": "Salón", "dormitorio_lucia": "Dormitorio Lucía"},
    )


# ----------------------------------------------------------------------
# Pure parts
# ----------------------------------------------------------------------
def test_probability_gating_is_seeded() -> None:
    """The same night always gets the same answer; about 25 % of nights do."""
    nights = [date(2026, 1, 1) + timedelta(days=i) for i in range(400)]
    first = [selected(night, 25) for night in nights]
    assert first == [selected(night, 25) for night in nights]
    assert 0.18 < sum(first) / len(first) < 0.32
    assert not any(selected(night, 0) for night in nights)
    assert all(selected(night, 100) for night in nights)


def test_scenario_validation_and_clamping() -> None:
    """Unknown rooms dropped, values clamped, extra fields ignored, texts cut."""
    keys = _keys()
    scenario = parse_scenario(
        {
            "scenario": "x" * 100,
            "explanation": "y" * 300,
            "modifiers": [
                {
                    "room": "room_1",
                    "start_shift_min": 200,
                    "duration_factor": 5,
                    "extra_visits": 9,
                    "colour": "red",
                },
                {"room": "room_9", "extra_visits": 1},  # unknown key
                {"room": "room_2", "start_shift_min": "-90", "duration_factor": 0.1},
                {"room": "room_1"},
                {"room": "room_1"},
                {"room": "room_1"},  # more than 4
            ],
            "temperature": 3,
        },
        keys,
    )
    assert len(scenario.text) == 60 and len(scenario.explanation) == 200
    first, second = scenario.modifiers[:2]
    assert first == Modifier("salon", 60, 2.0, 3)
    assert second == Modifier("dormitorio_lucia", -60, 0.5, 0)
    assert len(scenario.modifiers) == 3  # 4 given after the cut, one unknown
    # JSON text is accepted too; output without a scenario is invalid.
    assert parse_scenario('{"scenario": "Cine", "modifiers": []}', keys).text == "Cine"
    for invalid in (
        {"modifiers": []},
        "not json",
        {"scenario": "a", "modifiers": 3},
        None,
    ):
        with pytest.raises(ValueError):
            parse_scenario(invalid, keys)


def test_apply_scenario_changes_a_copy() -> None:
    """Shift, longer visits and extra visits change only a copy of the room."""
    model = build_day_type(
        _nights(10, {"salon": [(60, 120)], "hall": [(200, 210)]}), TODAY, 21
    )
    changed = apply_scenario(model, [Modifier("salon", 30, 1.5, 2)])
    old, new = model.rooms["salon"], changed.rooms["salon"]
    first_old = next(s for s, v in enumerate(old.starts) if v)
    first_new = next(s for s, v in enumerate(new.starts) if v)
    assert first_new == first_old + 2  # 30 minutes later
    assert sum(new.starts) == pytest.approx(sum(old.starts) + 2)
    assert new.all_durations()[0][0] == pytest.approx(old.all_durations()[0][0] * 1.5)
    assert changed.rooms["hall"] is model.rooms["hall"]
    assert model.rooms["salon"] is old  # the original is untouched


def test_modified_model_generates_and_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same scenario model is used to generate and to score."""
    model = build_day_type(_nights(10, {"salon": [(60, 120)]}), TODAY, 21)
    seen: list[Any] = []
    real_generate, real_score = dream_module.generate_night, dream_module.score_night

    def _generate(m, *args, **kwargs):
        seen.append(("generate", m))
        return real_generate(m, *args, **kwargs)

    def _score(m, *args, **kwargs):
        seen.append(("score", m))
        return real_score(m, *args, **kwargs)

    monkeypatch.setattr(dream_module, "generate_night", _generate)
    monkeypatch.setattr(dream_module, "score_night", _score)
    make_candidates(model, 0, 600, 0.0, 1, 2, [], 0.0, (Modifier("salon", 0, 2.0, 0),))
    models = {id(m) for _, m in seen}
    assert len(models) == 1 and model not in [m for _, m in seen]
    used = seen[0][1]
    assert used.rooms["salon"].all_durations()[0][0] == pytest.approx(
        model.rooms["salon"].all_durations()[0][0] * 2
    )


def test_keys_replaced_by_names() -> None:
    """Room keys in AI texts become the real names, locally."""
    keys = _keys()
    assert keys.to_names("Cena en room_1 y room_2 pronto; room_10 no") == (
        "Cena en Salón y Dormitorio Lucía pronto; room_10 no"
    )
    assert room_type(["Baño"]) == "bathroom"
    assert room_type(["Living room"]) == "living"
    assert room_type(["Mario's room"]) == "bedroom"
    assert room_type(["Trastero"], route_destination=True) == "bathroom"
    assert room_type(["Trastero"]) == "other"


# ----------------------------------------------------------------------
# Privacy
# ----------------------------------------------------------------------
async def test_payloads_never_contain_names(hass: HomeAssistant, fake_lights) -> None:
    """Area names with people's names, area IDs and entity IDs are never sent."""
    areas = ar.async_get(hass)
    lucia = areas.async_get_or_create("Habitación Lucía")
    mario = areas.async_get_or_create("Mario's room")
    areas.async_update(mario.id, aliases={"Cuarto de Mario"})
    rooms = {
        lucia.id: [(60, 120)],
        mario.id: [(90, 200)],
        "light:light.lamp": [(30, 40)],
    }
    model = build_day_type(_nights(10, rooms), TODAY, 21)
    names, aliases = area_names(hass, model.rooms)
    keys = build_keys(model.rooms, names, aliases)
    assert sorted(keys.types.values()) == ["bedroom", "bedroom", "other"]

    sent = scenario_payload(model, keys, WEEKDAY, 10, 7 * 3600 + 400, True, False)
    report = report_payload(
        {
            "mode": "dream",
            "score": 80,
            "room_times": {
                lucia.id: ["20:00-21:00"],
                "light:light.lamp": ["22:00-22:10"],
            },
            "failures_by_room": {mario.id: 2},
            "deterrence_reactions": 1,
            "deterrence_facades": ["front"],
        },
        keys,
    )
    calls = _ai_task(
        hass, lambda call: {"scenario": "Cine"} if "structure" in call.data else "ok"
    )
    assistant = AiAssistant(hass, {**AI_OPTIONS, CONF_AI_REPORT: True})
    await assistant.async_scenario(TODAY, sent, keys)
    await assistant.async_report(report, keys)
    assert len(calls) == 2
    text = json.dumps(
        [sent, report, [c.data["instructions"] for c in calls]], ensure_ascii=False
    )
    for secret in (
        "lucía",
        "mario",
        lucia.id,
        mario.id,
        "light.",
        "habitación",
        "cuarto",
    ):
        assert secret.casefold() not in text.casefold(), secret
    assert sent["sunset"] == "19:00"  # rounded to 15 minutes
    assert report["failures"] and report["rooms"]


# ----------------------------------------------------------------------
# Calls: limit, timeout, failures
# ----------------------------------------------------------------------
async def test_daily_limit(hass: HomeAssistant, fake_lights) -> None:
    """At the limit, AI features are skipped for the rest of the day."""
    calls = _ai_task(hass, lambda call: {"scenario": "Cena"})
    assistant = AiAssistant(hass, {**AI_OPTIONS, CONF_AI_MAX_CALLS: 2})
    keys = _keys()
    reasons = [(await assistant.async_scenario(TODAY, {}, keys))[1] for _ in range(3)]
    assert reasons == [AI_OK, AI_OK, AI_DAILY_LIMIT]
    assert len(calls) == 2
    assert assistant.usage()["calls_today"] == 2
    # Not selected tonight: no call at all.
    assistant = AiAssistant(hass, {**AI_OPTIONS, CONF_AI_SCENARIO_PROBABILITY: 0})
    assert (await assistant.async_scenario(TODAY, {}, keys))[1] == AI_NOT_SELECTED
    assert len(calls) == 2


async def test_timeout_and_errors(
    hass: HomeAssistant, fake_lights, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow, failing or nonsensical AI means no scenario, with the reason."""
    monkeypatch.setattr(ai_module, "AI_TIMEOUT_S", 0.05)
    keys = _keys()

    async def _forever(_call: ServiceCall) -> None:
        await asyncio.Event().wait()

    _ai_task(hass, _forever)
    assistant = AiAssistant(hass, AI_OPTIONS)
    assert await assistant.async_scenario(TODAY, {}, keys) == (None, AI_TIMEOUT)

    def _fail(_call: ServiceCall) -> None:
        raise HomeAssistantError("down")

    _ai_task(hass, _fail)
    assert await assistant.async_scenario(TODAY, {}, keys) == (None, AI_ERROR)
    _ai_task(hass, lambda call: {"explanation": "no scenario"})
    assert await assistant.async_scenario(TODAY, {}, keys) == (None, AI_INVALID)
    assert assistant.usage()["last_reason"] == AI_INVALID


# ----------------------------------------------------------------------
# Dream with scenarios
# ----------------------------------------------------------------------
SCENARIO = {
    "scenario": "Cena en room_1",
    "explanation": "Invitados en room_1 hasta tarde",
    "modifiers": [{"room": "room_1", "start_shift_min": 30, "duration_factor": 1.5}],
}


async def test_dream_evening_scenario(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """A scenario changes tonight's evening and is shown with real names."""
    calls = _ai_task(hass, lambda call: SCENARIO)
    entry = await _setup_dream(
        hass, freezer, _dream_days(), options=AI_OPTIONS, master=False
    )
    runtime = entry.runtime_data
    runtime.settings.dream_min_score = 0
    plan = await runtime.dream.async_plan(runtime.engine.planning_window())
    assert len(calls) == 1
    assert plan.scenario == "Cena en salon"
    assert plan.explanation == "Invitados en salon hasta tarde"
    status = runtime.dream.status
    assert status["scenario"] == "Cena en salon" and status["scenario_reason"] == AI_OK
    assert "scenario" in status["context"]
    runtime.dream._notify()
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.luma_presence_realism").attributes["scenario"]
        == "Cena en salon"
    )
    assert hass.states.get("sensor.luma_presence_tonight_plan").attributes[
        "explanation"
    ] == ("Invitados en salon hasta tarde")
    assert runtime.reporter.report()["scenario"] == "Cena en salon"


async def test_low_score_scenario_is_discarded(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    """If the scenario's best night is not believable, a normal night is planned."""
    _ai_task(hass, lambda call: SCENARIO)
    scenarios: list[tuple] = []

    def _candidates(
        model, start, end, offset, seed, count, history, boost=0.0, scenario=()
    ):
        scenarios.append(tuple(scenario))
        spans = {next(iter(model.rooms)): [(start, start + 30)]}
        return [Candidate(seed, spans, 10.0 if scenario else 90.0, 0.0)]

    monkeypatch.setattr(dream_module, "make_candidates", _candidates)
    entry = await _setup_dream(
        hass, freezer, _dream_days(), options=AI_OPTIONS, master=False
    )
    runtime = entry.runtime_data
    plan = await runtime.dream.async_plan(runtime.engine.planning_window())
    assert [bool(s) for s in scenarios] == [True, False]
    assert plan.scenario is None and plan.score == 90.0
    assert runtime.dream.status["scenario_reason"] == SCENARIO_LOW_SCORE


async def test_morning_and_preview_untouched(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    """The wake-up segment and previews never ask the AI."""
    calls = _ai_task(hass, lambda call: SCENARIO)
    scenarios: list[tuple] = []
    real = dream_module.make_candidates

    def _candidates(*args, **kwargs):
        scenarios.append(tuple(args[9]) if len(args) > 9 else ())
        return real(*args, **kwargs)

    monkeypatch.setattr(dream_module, "make_candidates", _candidates)
    entry = await _setup_dream(
        hass, freezer, _dream_days(), options=AI_OPTIONS, master=False
    )
    runtime = entry.runtime_data
    tomorrow = TODAY + timedelta(days=1)
    tz = dt_util.get_default_time_zone()
    morning = (
        dt_util.as_utc(
            datetime(tomorrow.year, tomorrow.month, tomorrow.day, 6, 30, tzinfo=tz)
        ),
        dt_util.as_utc(
            datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 0, tzinfo=tz)
        ),
    )
    await runtime.dream.async_plan(morning)
    await runtime.dream.async_preview(runtime.engine.planning_window())
    assert calls == []
    assert all(s == () for s in scenarios)


# ----------------------------------------------------------------------
# Morning report
# ----------------------------------------------------------------------
async def test_ai_report_and_fallback(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """The AI writes the text with room keys; names are put back; errors fall back."""
    sent: list[ServiceCall] = []

    async def _notify(call: ServiceCall) -> None:
        sent.append(call)

    hass.services.async_register("notify", "test_phone", _notify)
    calls = _ai_task(hass, lambda call: "Anoche room_1 estuvo ocupado.")
    options = {
        **AI_OPTIONS,
        CONF_AI_REPORT: True,
        CONF_REPORT_NOTIFY: "notify.test_phone",
    }
    entry = await _setup(hass, freezer, options)
    reporter = entry.runtime_data.reporter
    payload = {
        **reporter.report(),
        "room_times": {"light:light.salon": ["20:00-23:00"]},
    }
    await reporter._async_notify(payload)
    assert sent[-1].data["message"] == "Anoche salon estuvo ocupado."
    assert "light.salon" not in calls[-1].data["instructions"]

    def _fail(_call: ServiceCall) -> None:
        raise HomeAssistantError("down")

    _ai_task(hass, _fail)
    await reporter._async_notify(payload)
    assert sent[-1].data["message"].startswith("Last night (sun)")


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------
async def test_diagnostics(
    hass: HomeAssistant, fake_lights, freezer: FrozenDateTimeFactory
) -> None:
    """Subentries are included; people, the notify target and the AI entity are not."""
    hass.states.async_set("binary_sensor.porche", "off", {"device_class": "motion"})
    options = {
        CONF_PERSONS: ["person.mario"],
        CONF_REPORT_NOTIFY: "notify.mobile_app_mario",
        CONF_AI_TASK_ENTITY: AI_ENTITY,
    }
    trigger = {
        CONF_TRIGGER_ENTITY: "event.camara",
        CONF_FACADE: "front",
        CONF_PATH: ["light.hall"],
        CONF_EVENT_TYPES: ["person"],
    }
    entry = await _setup(hass, freezer, options, triggers=[trigger])
    hass.states.async_set("person.lucia", "home")
    data = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(data)
    for secret in ("person.mario", "person.lucia", "mobile_app_mario", AI_ENTITY):
        assert secret not in text, secret
    sub = data["subentries"][0]
    assert sub["type"] == "deterrence_trigger"
    assert sub["data"][CONF_EVENT_TYPES] == ["person"]
    assert data["options"][CONF_PERSONS] == "**REDACTED**"
    assert "light.salon" in text  # light IDs are kept
    assert data["learning"]["stats"]["events"] == 0
    assert "events" not in data["learning"]
    assert data["ai"]["calls_today"] == 0
