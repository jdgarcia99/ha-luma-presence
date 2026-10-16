"""Optional generative AI: evening scenarios for Dream and the morning report text.

Privacy contract (enforced here):

* Rooms are sent as opaque keys (``room_1`` ... ``room_n``) with a generic
  room type inferred locally (bedroom, bathroom, kitchen, living, office,
  hall, other). The key -> area mapping never leaves Home Assistant.
* Sent, and nothing else: night type, month and season, sunset rounded to
  15 minutes, bad-weather and holiday flags, and per room key its type,
  expected occupancy per hour, typical start and end hour and typical
  visit duration; for the report, the simulated night with room keys.
* Never sent: entity IDs, area or room names, person names or states, raw
  learned events, dates of real days, location, alarm states, camera or
  trigger entities.

Everything that builds payloads or modifies the model is pure; only the
service call is I/O.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
import json
import logging
import random
import re
from statistics import median
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util

from .const import (
    AI_DAILY_LIMIT,
    AI_DISABLED,
    AI_ERROR,
    AI_INVALID,
    AI_NOT_SELECTED,
    AI_OK,
    AI_TIMEOUT,
    AI_TIMEOUT_S,
    CONF_AI_MAX_CALLS,
    CONF_AI_REPORT,
    CONF_AI_SCENARIO_PROBABILITY,
    CONF_AI_TASK_ENTITY,
    DEFAULT_AI_MAX_CALLS,
    DEFAULT_AI_REPORT,
    DEFAULT_AI_SCENARIO_PROBABILITY,
)
from .model import SLOT_MIN, DayTypeModel, RoomStats

_LOGGER = logging.getLogger(__name__)

SCENARIO_MAX = 60
EXPLANATION_MAX = 200
REPORT_MAX = 600
MAX_MODIFIERS = 4
SHIFT_RANGE = (-60, 60)
DURATION_RANGE = (0.5, 2.0)
EXTRA_RANGE = (0, 3)
TYPICAL_OCCUPANCY = 0.1

ROOM_TYPES = ("bedroom", "bathroom", "kitchen", "living", "office", "hall", "other")
# Checked in this order: "living room" and "bathroom" before plain "room".
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "bathroom",
        (
            "baño",
            "bano",
            "aseo",
            "bathroom",
            "toilet",
            "wc",
            "ducha",
            "lavabo",
            "shower",
        ),
    ),
    ("kitchen", ("cocina", "kitchen", "office de cocina")),
    (
        "living",
        (
            "salón",
            "salon",
            "sala",
            "living",
            "lounge",
            "comedor",
            "dining",
            "estar",
            "tv",
        ),
    ),
    ("office", ("despacho", "oficina", "estudio", "office", "study", "workroom")),
    (
        "hall",
        (
            "pasillo",
            "entrada",
            "recibidor",
            "hall",
            "corridor",
            "escalera",
            "stair",
            "porch",
        ),
    ),
    (
        "bedroom",
        (
            "dormitorio",
            "habitación",
            "habitacion",
            "cuarto",
            "alcoba",
            "bedroom",
            "room",
        ),
    ),
)
_KEY = re.compile(r"\broom_(\d+)\b")

LANGUAGES = {
    "es": "Spanish",
    "en": "English",
    "ca": "Catalan",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
}


# ----------------------------------------------------------------------
# Room keys
# ----------------------------------------------------------------------
def room_type(names: Iterable[str], route_destination: bool = False) -> str:
    """Generic type of a room from its name and aliases (computed locally)."""
    text = " ".join(names).casefold()
    for kind, words in _KEYWORDS:
        if any(word in text for word in words):
            return kind
    return "bathroom" if route_destination else "other"


@dataclass(frozen=True)
class RoomKeys:
    """Opaque keys for the rooms of one AI call. Never sent: only the keys."""

    rooms: dict[str, str]  # key -> room id (area id or "light:<entity>")
    types: dict[str, str]  # key -> generic room type
    names: dict[str, str]  # key -> display name, for local replacement only

    def key_of(self, room: str) -> str | None:
        """Key of a room id."""
        return next((key for key, value in self.rooms.items() if value == room), None)

    def to_names(self, text: str) -> str:
        """Replace room keys by the real room names, locally."""
        return _KEY.sub(lambda m: self.names.get(m.group(0), m.group(0)), text)


def build_keys(
    rooms: Iterable[str],
    names: Mapping[str, str],
    aliases: Mapping[str, Iterable[str]] | None = None,
    destinations: Iterable[str] = (),
    order: Callable[[str], Any] | None = None,
) -> RoomKeys:
    """room_1..room_n for `rooms` (in `order`), with their generic types."""
    destinations = set(destinations)
    ordered = sorted(rooms, key=order) if order else sorted(rooms)
    keys: dict[str, str] = {}
    types: dict[str, str] = {}
    display: dict[str, str] = {}
    for index, room in enumerate(ordered, start=1):
        key = f"room_{index}"
        keys[key] = room
        name = names.get(room, room)
        types[key] = room_type(
            [name, *((aliases or {}).get(room, ()))], room in destinations
        )
        display[key] = name
    return RoomKeys(keys, types, display)


def area_names(
    hass: HomeAssistant, rooms: Iterable[str]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Names and aliases of rooms (areas, or lights without an area)."""
    registry = ar.async_get(hass)
    names: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    for room in rooms:
        if room.startswith("light:"):
            state = hass.states.get(room.removeprefix("light:"))
            names[room] = state.name if state else room.removeprefix("light:")
        elif (area := registry.async_get_area(room)) is not None:
            names[room] = area.name
            aliases[room] = list(area.aliases)
        else:
            names[room] = room
    return names, aliases


# ----------------------------------------------------------------------
# Payloads (pure)
# ----------------------------------------------------------------------
def season(month: int, southern: bool = False) -> str:
    """Meteorological season of a month."""
    seasons = ("winter", "spring", "summer", "autumn")
    index = (month % 12) // 3
    return seasons[(index + 2) % 4] if southern else seasons[index]


def round_clock(seconds_since_noon: float) -> str:
    """Local clock "HH:MM" of a night clock value, rounded to 15 minutes."""
    minutes = int(round((seconds_since_noon / 60) / 15) * 15 + 12 * 60) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def room_summary(stats: RoomStats, kind: str) -> dict[str, Any]:
    """What is sent about one room: type, hourly occupancy, typical hours and visit."""
    hours = {
        f"{(12 + hour) % 24:02d}": round(value, 1)
        for hour, value in enumerate(stats.hours)
        if round(value, 1) > 0
    }
    busy = [
        hour for hour, value in enumerate(stats.hours) if value >= TYPICAL_OCCUPANCY
    ]
    durations = [value for value, _ in stats.all_durations()]
    return {
        "type": kind,
        "occupancy_by_hour": hours,
        "typical_start_hour": (12 + busy[0]) % 24 if busy else None,
        "typical_end_hour": (12 + busy[-1] + 1) % 24 if busy else None,
        "typical_visit_min": round(median(durations)) if durations else None,
    }


def scenario_payload(
    model: DayTypeModel,
    keys: RoomKeys,
    night_type: str,
    month: int,
    sunset_s: float | None,
    bad_weather: bool,
    holiday: bool,
    southern: bool = False,
) -> dict[str, Any]:
    """Everything a scenario request sends."""
    return {
        "night_type": night_type,
        "month": month,
        "season": season(month, southern),
        "sunset": round_clock(sunset_s) if sunset_s is not None else None,
        "bad_weather": bad_weather,
        "holiday": holiday,
        "rooms": {
            key: room_summary(model.rooms[room], keys.types[key])
            for key, room in keys.rooms.items()
            if room in model.rooms
        },
    }


def report_payload(report: Mapping[str, Any], keys: RoomKeys) -> dict[str, Any]:
    """The simulated night for the report text, with room keys only."""
    return {
        "mode": report.get("mode"),
        "source": report.get("source"),
        "realism": report.get("score"),
        "scenario": report.get("scenario"),
        "rooms": {
            key: {
                "type": keys.types[key],
                "times": report.get("room_times", {}).get(room, []),
            }
            for key, room in keys.rooms.items()
        },
        "routes_run": report.get("routes_run", 0),
        "deterrence": {
            "reactions": report.get("deterrence_reactions", 0),
            "facades": sorted(set(report.get("deterrence_facades", []))),
        },
        "covers_moved": report.get("cover_moves", 0),
        "audio_sessions": report.get("ambience_sessions", 0),
        "failures": {
            key: count
            for key, room in keys.rooms.items()
            if (count := report.get("failures_by_room", {}).get(room))
        },
    }


# ----------------------------------------------------------------------
# Scenario validation and model changes (pure)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Modifier:
    """A change to one room's evening."""

    room: str  # room id (already mapped from the key)
    start_shift_min: int = 0
    duration_factor: float = 1.0
    extra_visits: int = 0


@dataclass(frozen=True)
class Scenario:
    """A validated scenario."""

    text: str
    explanation: str
    modifiers: tuple[Modifier, ...]


def _clamp(value: Any, low: float, high: float, kind: type) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0 if kind is int else 1.0
    return kind(min(max(number, low), high))


def parse_scenario(data: Any, keys: RoomKeys) -> Scenario:
    """Validate the AI output: unknown rooms dropped, values clamped, extras ignored."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as err:
            raise ValueError("not json") from err
    if not isinstance(data, Mapping) or not str(data.get("scenario") or "").strip():
        raise ValueError("no scenario")
    raw = data.get("modifiers") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as err:
            raise ValueError("modifiers") from err
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("modifiers")  # noqa: TRY004  (invalid output, like the others)
    modifiers: list[Modifier] = []
    for item in raw[:MAX_MODIFIERS]:
        if (
            not isinstance(item, Mapping)
            or (room := keys.rooms.get(str(item.get("room")))) is None
        ):
            continue  # unknown room key: rejected
        modifiers.append(
            Modifier(
                room=room,
                start_shift_min=_clamp(
                    item.get("start_shift_min", 0), *SHIFT_RANGE, int
                ),
                duration_factor=round(
                    _clamp(item.get("duration_factor", 1.0), *DURATION_RANGE, float), 2
                ),
                extra_visits=_clamp(item.get("extra_visits", 0), *EXTRA_RANGE, int),
            )
        )
    return Scenario(
        text=str(data["scenario"]).strip()[:SCENARIO_MAX],
        explanation=str(data.get("explanation") or "").strip()[:EXPLANATION_MAX],
        modifiers=tuple(modifiers),
    )


def apply_scenario(model: DayTypeModel, modifiers: Iterable[Modifier]) -> DayTypeModel:
    """Copy of an evening model with the scenario applied (pure).

    Start rates move by the shift (whole 15-minute slots), visits get longer
    or shorter, and extra visits raise the starts in the room's usual slots.
    Expected occupancy and concurrency follow, so the same model can score.
    """
    rooms = dict(model.rooms)
    concurrency = list(model.concurrency)
    for modifier in modifiers:
        stats = rooms.get(modifier.room)
        if stats is None:
            continue
        shift = round(modifier.start_shift_min / SLOT_MIN)

        def shifted(values: list[float], by: int = shift) -> list[float]:
            size = len(values)
            return [values[s - by] if 0 <= s - by < size else 0.0 for s in range(size)]

        start_rate = shifted(stats.start_rate)
        starts = shifted(stats.starts)
        occupancy = shifted(stats.occupancy)
        factor = modifier.duration_factor
        occupancy = [min(value * factor, 1.0) for value in occupancy]
        if modifier.extra_visits > 0 and (expected := sum(starts)) > 0:
            boost = 1 + modifier.extra_visits / expected
            start_rate = [min(rate * boost, 1.0) for rate in start_rate]
            starts = [value * boost for value in starts]
            occupancy = [min(value * boost, 1.0) for value in occupancy]
        for slot, (old, new) in enumerate(zip(stats.occupancy, occupancy, strict=True)):
            concurrency[slot] = max(concurrency[slot] + new - old, 0.0)
        rooms[modifier.room] = replace(
            stats,
            start_rate=start_rate,
            starts=starts,
            occupancy=occupancy,
            durations={
                period: [(value * factor, weight) for value, weight in items]
                for period, items in stats.durations.items()
            },
        )
    return replace(model, rooms=rooms, concurrency=concurrency)


def selected(night: date, probability: float) -> bool:
    """Whether a night gets a scenario (reproducible per night)."""
    return random.Random(f"scenario-{night.isoformat()}").random() * 100 < probability


# ----------------------------------------------------------------------
# Service calls
# ----------------------------------------------------------------------
_STRUCTURE = {
    "scenario": {
        "description": f"Short name of tonight's scenario (at most {SCENARIO_MAX} characters).",
        "required": True,
        "selector": {"text": {}},
    },
    "modifiers": {
        "description": f"At most {MAX_MODIFIERS} changes to the usual evening of a room.",
        "required": False,
        "selector": {
            "object": {
                "multiple": True,
                "fields": {
                    "room": {"selector": {"text": {}}, "required": True},
                    "start_shift_min": {
                        "selector": {"number": {"min": -60, "max": 60}}
                    },
                    "duration_factor": {
                        "selector": {"number": {"min": 0.5, "max": 2.0, "step": 0.1}}
                    },
                    "extra_visits": {"selector": {"number": {"min": 0, "max": 3}}},
                },
            }
        },
    },
    "explanation": {
        "description": f"One sentence explaining the scenario (at most {EXPLANATION_MAX} characters).",
        "required": True,
        "selector": {"text": {}},
    },
}


class AiAssistant:
    """Calls ai_task for scenarios and report texts, within the daily limit."""

    def __init__(self, hass: HomeAssistant, config: Mapping[str, Any]) -> None:
        """Initialize the assistant."""
        self.hass = hass
        self.entity: str | None = config.get(CONF_AI_TASK_ENTITY) or None
        self.probability = float(
            config.get(CONF_AI_SCENARIO_PROBABILITY, DEFAULT_AI_SCENARIO_PROBABILITY)
        )
        self.report_enabled = bool(config.get(CONF_AI_REPORT, DEFAULT_AI_REPORT))
        self.max_calls = int(config.get(CONF_AI_MAX_CALLS, DEFAULT_AI_MAX_CALLS))
        self._day: date | None = None
        self.calls_today = 0
        self.last_reason: str | None = None
        self.last_scenario: str | None = None

    @property
    def enabled(self) -> bool:
        """AI features are off without an ai_task entity."""
        return self.entity is not None

    def usage(self) -> dict[str, Any]:
        """AI usage today (diagnostics)."""
        self._roll_day()
        return {
            "enabled": self.enabled,
            "calls_today": self.calls_today,
            "max_calls_per_day": self.max_calls,
            "last_reason": self.last_reason,
            "scenario_probability": self.probability,
            "report": self.report_enabled,
        }

    def _roll_day(self) -> None:
        today = dt_util.now().date()
        if today != self._day:
            self._day = today
            self.calls_today = 0

    def language(self) -> str:
        """The household language, in English words, for the prompt."""
        code = (self.hass.config.language or "en").split("-")[0]
        return LANGUAGES.get(code, code)

    async def _async_generate(
        self, task: str, instructions: str, structure: dict[str, Any] | None = None
    ) -> tuple[Any, str]:
        """One ai_task call: (data, reason). Counts towards the daily limit."""
        if not self.enabled:
            return None, AI_DISABLED
        self._roll_day()
        if self.calls_today >= self.max_calls:
            return None, AI_DAILY_LIMIT
        self.calls_today += 1
        data: dict[str, Any] = {
            "task_name": task,
            "entity_id": self.entity,
            "instructions": instructions,
        }
        if structure is not None:
            data["structure"] = structure
        try:
            async with asyncio.timeout(AI_TIMEOUT_S):
                response = await self.hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    data,
                    blocking=True,
                    return_response=True,
                )
        except TimeoutError:
            return None, AI_TIMEOUT
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("The AI task failed: %s", err)
            return None, AI_ERROR
        return (response or {}).get("data"), AI_OK

    async def async_scenario(
        self, night: date, payload: dict[str, Any], keys: RoomKeys
    ) -> tuple[Scenario | None, str]:
        """A scenario for tonight's evening, or why there is none."""
        if not self.enabled:
            return None, self._note(AI_DISABLED)
        if not selected(night, self.probability):
            return None, self._note(AI_NOT_SELECTED)
        instructions = (
            "You plan the lights of an occupied home for one evening so that it looks lived in. "
            "Propose a plausible scenario for this household tonight (for example guests for "
            "dinner, a movie night, someone going to bed early) as small changes to the usual "
            "evening of at most four rooms. Use only the room keys given. start_shift_min moves "
            "the room's usual activity (-60 to 60 minutes), duration_factor scales its visits "
            "(0.5 to 2.0), extra_visits adds visits in its usual hours (0 to 3). Write "
            f"'scenario' and 'explanation' in {self.language()}, refer to rooms only by their "
            "keys, and keep them short.\n\nTonight (JSON):\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        data, reason = await self._async_generate(
            "LumaPresence scenario", instructions, _STRUCTURE
        )
        if reason != AI_OK:
            return None, self._note(reason)
        try:
            scenario = parse_scenario(data, keys)
        except ValueError:
            return None, self._note(AI_INVALID)
        self.last_scenario = scenario.text
        return scenario, self._note(AI_OK)

    async def async_report(self, payload: dict[str, Any], keys: RoomKeys) -> str | None:
        """Natural-language report text with real room names, or None."""
        if not self.enabled or not self.report_enabled:
            return None
        instructions = (
            f"Write a short morning report (at most {REPORT_MAX} characters, plain text, in "
            f"{self.language()}) for the owner of a home whose presence simulation ran last "
            "night. Say what was simulated, how realistic it was and anything worth attention. "
            "Refer to rooms only by their keys (room_1, room_2...).\n\nLast night (JSON):\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        data, reason = await self._async_generate(
            "LumaPresence morning report", instructions
        )
        self._note(reason)
        if reason != AI_OK or not isinstance(data, str) or not data.strip():
            return None
        return keys.to_names(data.strip())[:REPORT_MAX]

    def _note(self, reason: str) -> str:
        self.last_reason = reason
        return reason
