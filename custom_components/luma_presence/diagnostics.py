"""Diagnostics: the state of every part of LumaPresence, for bug reports.

People, the notify target, the AI task entity and the location are
redacted, and so is every person entity ID found anywhere in the data.
Light and area IDs are kept: they are needed to debug. Learned events are
never included, only counts.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_AI_TASK_ENTITY, CONF_PERSONS, CONF_REPORT_NOTIFY
from .models import LumaConfigEntry
from .plan import night_of

TO_REDACT = {
    CONF_PERSONS,
    CONF_REPORT_NOTIFY,
    CONF_AI_TASK_ENTITY,
    "latitude",
    "longitude",
}


def _plain(value: Any) -> Any:
    """JSON-friendly values (dates as ISO text)."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [_plain(v) for v in value]
        return sorted(items, key=str) if isinstance(value, set | frozenset) else items
    return value


def _redact_values(value: Any, secrets: set[str]) -> Any:
    """Replace any string that is (or contains) a redacted entity ID."""
    if isinstance(value, str):
        return REDACTED if any(secret in value for secret in secrets) else value
    if isinstance(value, dict):
        return {k: _redact_values(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_values(v, secrets) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LumaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    engine = runtime.engine
    controller = runtime.controller
    context = runtime.context
    player = runtime.player
    dream = runtime.dream
    night = night_of(dt_util.as_local(dt_util.utcnow()))

    plan = player.plan if player else None
    morning = engine.morning_window()
    deterrence = runtime.deterrence
    data: dict[str, Any] = {
        "version": runtime.version,
        "options": dict(entry.options),
        "subentries": [
            {"type": sub.subentry_type, "title": sub.title, "data": dict(sub.data)}
            for sub in entry.subentries.values()
        ],
        "engine": {
            "mode": runtime.settings.mode,
            "status": engine.status,
            "enabled": engine.enabled,
            "activated_by": engine.activated_by,
            "next_event": engine.next_event,
            "next_event_kind": engine.next_event_kind,
            "next_event_reason": engine.next_event_reason,
            "owned_lights": {
                light: controller.owner_of(light) for light in controller.owned()
            },
            "locked": sorted(e for e in controller._locks if controller.is_locked(e)),
        },
        "sequences": {
            "enabled": runtime.settings.sequences_enabled,
            "running": runtime.sequences.running,
            "route": runtime.sequences.route.name if runtime.sequences.route else None,
            "step": runtime.sequences.step,
            "next_run": runtime.sequences.next_run,
            "routes": len(runtime.sequences.routes),
        },
        "learning": {
            "open": runtime.learning.is_open,
            "reasons": sorted(runtime.learning.reasons),
            "blocked_until": runtime.learning.blocked_until,
            "stats": runtime.learning.stats(),
            "entities": len(runtime.learning.entities),
        },
        "model": dream.summary() if dream else {},
        "tonight": {
            "plan": (
                {
                    "night": plan.night,
                    "source": plan.source,
                    "score": plan.score,
                    "reason": plan.reason,
                    "scenario": plan.scenario,
                    "explanation": plan.explanation,
                    "summary": plan.summary(),
                    "rooms": plan.rooms,
                    "covers": plan.covers,
                }
                if plan
                else None
            ),
            "realism": dict(dream.status) if dream else {},
            "replay": dict(runtime.replay.status or {}),
        },
        "context": {
            "night_type": context.night_type(night) if context else None,
            "night_type_source": context.night_type_source if context else None,
            "weather": context.weather() if context else None,
            "daylight": context.daylight() if context else False,
            "evening_start_reason": engine.evening_start_reason(),
            "morning_window": list(morning) if morning else None,
        },
        "verification": {
            "failures_total": controller.failures,
            "failures_tonight": dict(controller.failure_counts),
            "failed_tonight": sorted(controller.failed_tonight),
        },
        "covers": (
            [move.as_dict() for move in runtime.covers.upcoming()]
            if runtime.covers and runtime.covers.covers
            else []
        ),
        "last_night_report": runtime.reporter.last_report if runtime.reporter else None,
        "last_deterrence": (
            {
                **(deterrence.last or {}),
                "reactions_tonight": deterrence.reactions_tonight,
            }
            if deterrence
            else None
        ),
        "ai": runtime.ai.usage() if runtime.ai else None,
    }
    data = _plain(data)
    data = async_redact_data(data, TO_REDACT)
    persons = set(entry.options.get(CONF_PERSONS) or []) | {
        state.entity_id for state in hass.states.async_all("person")
    }
    if notify := entry.options.get(CONF_REPORT_NOTIFY):
        persons.add(str(notify))
    if ai_entity := entry.options.get(CONF_AI_TASK_ENTITY):
        persons.add(str(ai_entity))
    return _redact_values(data, persons)
