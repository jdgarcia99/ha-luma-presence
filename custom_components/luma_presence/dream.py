"""Dream mode: new, believable nights generated from the room model.

Each night several candidates are simulated minute by minute from the
model of tonight's day type, scored for realism against it, filtered for
novelty against the last nights, and the best one becomes tonight's plan.
Everything runs locally.

Realism score (0-100):

    100 * (0.4 * occupancy + 0.2 * durations + 0.2 * concurrency + 0.2 * visits)

* occupancy: 1 - sum |expected - candidate| / sum (expected + candidate), per
  room and 15-minute slot of the window (normalised L1 distance);
* durations: 1 - KS statistic between the candidate's visit durations and
  the learned ones;
* concurrency: 1 - normalised L1 distance between the expected and the
  candidate number of occupied rooms per slot;
* visits: share of rooms whose number of visits falls in the expected
  range (expected starts in the window +/- 2 standard deviations, Poisson).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
import math
import random
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_event_date, get_astral_event_next
from homeassistant.util import dt as dt_util

from .ai import (
    AiAssistant,
    Modifier,
    RoomKeys,
    apply_scenario,
    area_names,
    build_keys,
    scenario_payload,
)
from .const import (
    CONF_DREAM_CANDIDATES,
    CONF_DREAM_MAX_SIMILARITY,
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    CONF_REAR_LIGHTS,
    DEFAULT_DREAM_CANDIDATES,
    DEFAULT_DREAM_MAX_SIMILARITY,
    DOMAIN,
    DREAM_BEST,
    DREAM_LOW_SCORE,
    DREAM_NO_WINDOW,
    DREAM_NOT_ENOUGH_NIGHTS,
    MODE_DREAM,
    MORNING_AXIS_MIN,
    MORNING_AXIS_START_H,
    SCENARIO_LOW_SCORE,
    STORE_VERSION,
    signal_update,
)
from .context import ContextProvider
from .learning import LearningGate
from .model import (
    DAY_TYPES,
    FOLLOW_MIN,
    MORNING_AXIS_OFFSET,
    SLOT_MIN,
    SLOTS,
    DayTypeModel,
    RoomModel,
    RoomStats,
    boost_model,
    build_model,
    day_type,
    morning_rooms,
    night_rooms,
    overlap,
    period_of,
    room_key,
    slot_of,
)
from .models import LumaSettings
from .plan import (
    NIGHT_START,
    NightPlan,
    PlanPlayer,
    Timeline,
    Transition,
    apply_jitter,
    night_of,
    to_night_seconds,
)
from .replay import NightData, area_targets, night_data
from .scope import entity_area_id, resolve_learning_entities

_LOGGER = logging.getLogger(__name__)

HISTORY_NIGHTS = 7
HEATMAP_NIGHTS = 7
SCORE_WEIGHTS = (0.4, 0.2, 0.2, 0.2)

Spans = dict[str, list[tuple[int, int]]]


# ----------------------------------------------------------------------
# Generator
# ----------------------------------------------------------------------
def _sample_duration(stats: RoomStats, period: str, rng: random.Random) -> int:
    samples = stats.durations.get(period) or stats.all_durations()
    if not samples:
        return 30
    value = rng.choices([v for v, _ in samples], [w for _, w in samples])[0]
    # Smooth the empirical distribution a little.
    return max(1, round(value * rng.uniform(0.85, 1.15)))


def generate_night(
    model: DayTypeModel,
    start: int,
    end: int,
    seed: int,
    period_at: Callable[[int], str],
) -> Spans:
    """Simulate the window [start, end) minute by minute (axis minutes)."""
    rng = random.Random(seed)
    rooms = sorted(model.rooms)
    cap = model.concurrency_cap
    occupied: dict[str, tuple[int, int]] = {}
    result: Spans = {room: [] for room in rooms}

    # Rooms already occupied at the window start.
    first = slot_of(start)
    order = rooms[:]
    rng.shuffle(order)
    for room in order:
        if len(occupied) < cap and rng.random() < model.rooms[room].occupancy[first]:
            duration = _sample_duration(model.rooms[room], period_at(start), rng)
            occupied[room] = (start, start + max(1, round(duration * rng.random())))

    recent: list[tuple[int, str]] = []
    for minute in range(start, end):
        for room, (began, until) in list(occupied.items()):
            if until <= minute:
                result[room].append((began, until))
                del occupied[room]
                recent.append((minute, room))
        recent = [(t, r) for t, r in recent if minute - t < FOLLOW_MIN]
        slot = slot_of(minute)
        idle = [room for room in rooms if room not in occupied]
        rng.shuffle(idle)
        for room in idle:
            if len(occupied) >= cap:
                break
            chance = model.rooms[room].start_rate[slot]
            for _, ended in recent:
                if ended != room:
                    stats = model.rooms[ended]
                    chance += stats.follow_prob * stats.follow_to.get(room, 0.0) / FOLLOW_MIN
            if rng.random() < chance:
                duration = _sample_duration(model.rooms[room], period_at(minute), rng)
                occupied[room] = (minute, minute + duration)
    for room, (began, until) in occupied.items():
        result[room].append((began, min(until, end)))
    return {room: spans for room, spans in result.items() if spans}


# ----------------------------------------------------------------------
# Realism score and novelty
# ----------------------------------------------------------------------
def _slots(start: int, end: int) -> range:
    return range(slot_of(start), slot_of(end - 1) + 1)


def _weighted_ks(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Kolmogorov-Smirnov statistic between two weighted samples.

    One sweep over both sorted samples: O(n log n) for the sort, then linear.
    """
    if not a or not b:
        return 1.0 if (a or b) else 0.0
    a, b = sorted(a), sorted(b)
    total_a = sum(w for _, w in a)
    total_b = sum(w for _, w in b)
    i = j = 0
    cum_a = cum_b = worst = 0.0
    while i < len(a) or j < len(b):
        point = min(
            a[i][0] if i < len(a) else math.inf, b[j][0] if j < len(b) else math.inf
        )
        while i < len(a) and a[i][0] == point:
            cum_a += a[i][1]
            i += 1
        while j < len(b) and b[j][0] == point:
            cum_b += b[j][1]
            j += 1
        worst = max(worst, abs(cum_a / total_a - cum_b / total_b))
    return worst


def score_night(
    model: DayTypeModel,
    spans: Spans,
    start: int,
    end: int,
    learned: list[tuple[float, float]] | None = None,
) -> tuple[float, dict[str, float]]:
    """Realism score (0-100) of a night and its four parts (0-1).

    `learned` (all learned durations) can be passed to reuse it across candidates.
    """
    slots = _slots(start, end)
    rooms = set(model.rooms) | set(spans)

    distance = total = 0.0
    for room in rooms:
        stats = model.rooms.get(room)
        for s in slots:
            lo, hi = max(s * SLOT_MIN, start), min((s + 1) * SLOT_MIN, end)
            if hi <= lo:
                continue
            expected = stats.occupancy[s] if stats else 0.0
            actual = overlap(spans.get(room, []), lo, hi) / (hi - lo)
            distance += abs(expected - actual)
            total += expected + actual
    occupancy = 1 - distance / total if total else 1.0

    if learned is None:
        learned = learned_durations(model)
    generated = [(float(e - s), 1.0) for items in spans.values() for s, e in items]
    durations = 1 - _weighted_ks(generated, learned)

    distance = total = 0.0
    for s in slots:
        middle = min(max(s * SLOT_MIN + SLOT_MIN / 2, start), end - 1)
        actual = sum(any(a <= middle < b for a, b in items) for items in spans.values())
        expected = model.concurrency[s]
        distance += abs(expected - actual)
        total += max(expected, actual)
    concurrency = 1 - distance / total if total else 1.0

    checked = within = 0
    for room in rooms:
        stats = model.rooms.get(room)
        expected = sum(stats.starts[s] for s in slots) if stats else 0.0
        visits = len(spans.get(room, []))
        if expected < 0.25 and not visits:
            continue
        checked += 1
        spread = 2 * math.sqrt(expected)
        if math.floor(expected - spread) <= visits <= math.ceil(expected + spread):
            within += 1
    visits_part = within / checked if checked else 1.0

    parts = {
        "occupancy": occupancy,
        "durations": durations,
        "concurrency": concurrency,
        "visits": visits_part,
    }
    score = 100 * sum(w * v for w, v in zip(SCORE_WEIGHTS, parts.values(), strict=True))
    return round(score, 1), parts


def learned_durations(model: DayTypeModel) -> list[tuple[float, float]]:
    """Every learned visit duration of a day type, with its weight."""
    return [item for stats in model.rooms.values() for item in stats.all_durations()]


def room_slots(spans: Spans) -> set[tuple[str, int]]:
    """Occupied (room, slot) pairs of a night."""
    return {
        (room, s)
        for room, items in spans.items()
        for a, b in items
        for s in range(slot_of(a), slot_of(max(a, b - 1)) + 1)
    }


def jaccard(a: set[Any], b: set[Any]) -> float:
    """Jaccard similarity of two sets."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


@dataclass
class Candidate:
    """A generated night."""

    seed: int
    spans: Spans
    score: float
    similarity: float


def period_function(offset: float) -> Callable[[int], str]:
    """Coarse period of an axis minute, for a night whose axis starts at `offset`."""

    def period_at(minute: int) -> str:
        return period_of((offset / 60 + minute + 12 * 60) % (24 * 60))

    return period_at


def make_candidates(
    model: DayTypeModel,
    start: int,
    end: int,
    offset: float,
    base_seed: int,
    count: int,
    history: list[set[tuple[str, int]]],
    boost: float = 0.0,
    scenario: tuple[Modifier, ...] = (),
) -> list[Candidate]:
    """Generate, score and compare `count` candidates (pure: runs in an executor).

    With a weather `boost` or an AI `scenario`, the same modified model
    generates and scores the candidates, so these nights are not rejected as
    unrealistic.
    """
    model = boost_model(model, boost, offset)
    if scenario:
        model = apply_scenario(model, scenario)
    period_at = period_function(offset)
    learned = learned_durations(model)
    candidates: list[Candidate] = []
    for index in range(max(count, 1)):
        seed = (base_seed + index) % 2**32
        spans = generate_night(model, start, end, seed, period_at)
        score, _parts = score_night(model, spans, start, end, learned)
        slots = room_slots(spans)
        similarity = max((jaccard(slots, old) for old in history), default=0.0)
        candidates.append(Candidate(seed, spans, score, similarity))
    return candidates


def dreamed_hours(
    model: DayTypeModel, rooms: list[str], offset: float, length: int
) -> list[list[float]]:
    """Average occupancy per room and hour of 7 dreamed nights (pure)."""
    period_at = period_function(offset)
    dreamed = [[0.0] * 24 for _ in rooms]
    for index in range(HEATMAP_NIGHTS):
        spans = generate_night(model, 0, length, index, period_at)
        for row, room in enumerate(rooms):
            for hour in range(24):
                lo = (hour * 3600 - offset) / 60
                dreamed[row][hour] += overlap(spans.get(room, []), lo, lo + 60) / 60
    return [[round(v / HEATMAP_NIGHTS, 3) for v in row] for row in dreamed]


def mornings_from_nights(
    nights: list[tuple[NightData, float, float]],
    today: date,
    half_life: float,
    built_at: datetime,
    kinds: dict[date, str] | None = None,
    night_type_source: str | None = None,
) -> RoomModel:
    """The morning model: the mornings after the learned nights (pure).

    A morning's type is the type of the night before it.
    """
    rooms = [morning_rooms(data) for data, _, _ in nights]
    return build_model(
        [r for r in rooms if r is not None],
        today,
        half_life,
        built_at,
        kinds,
        night_type_source,
    )


def model_from_nights(
    nights: list[tuple[NightData, float, float]],
    today: date,
    half_life: float,
    built_at: datetime,
    kinds: dict[date, str] | None = None,
    night_type_source: str | None = None,
) -> RoomModel:
    """Turn learned nights (with their sun times) into the model (pure)."""
    rooms = [night_rooms(data, sunset, sunrise, room_key) for data, sunset, sunrise in nights]
    return build_model(
        [r for r in rooms if r is not None],
        today,
        half_life,
        built_at,
        kinds,
        night_type_source,
    )


def choose_candidate(
    candidates: list[Candidate], min_score: float, max_similarity: float
) -> tuple[Candidate, str]:
    """Best candidate that is new enough and realistic enough, else the best one."""
    good = [c for c in candidates if c.similarity <= max_similarity and c.score >= min_score]
    if good:
        return max(good, key=lambda c: (c.score, -c.seed)), DREAM_BEST
    return max(candidates, key=lambda c: (c.score, -c.seed)), DREAM_LOW_SCORE


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
class DreamEngine:
    """Builds the room model and plans Dream nights."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: Mapping[str, Any],
        settings: LumaSettings,
        learning: LearningGate,
        player: PlanPlayer,
        on_lights: Callable[[list[str]], None] | None = None,
    ) -> None:
        """Initialize the dream engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._config = config
        self._settings = settings
        self._learning = learning
        self._player = player
        self._on_lights = on_lights
        self.model: RoomModel | None = None
        self.morning_model: RoomModel | None = None
        self.status: dict[str, Any] = {}
        # Status of the last preview, kept apart from tonight's.
        self.preview_status: dict[str, Any] = {}
        self._saved: dict[str, Any] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._rebuilds = 0  # only the latest rebuild may set the model
        # Holidays and weather (set by the integration).
        self.context: ContextProvider | None = None
        # Optional AI scenarios, and the areas of route destinations (bathrooms).
        self.ai: AiAssistant | None = None
        self.destination_areas: Callable[[], set[str]] = set
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.dream"
        )

    async def async_load(self) -> None:
        """Load tonight's plan and the recent nights, kept across restarts."""
        self._saved = await self._store.async_load() or {}
        self.status = dict(self._saved.get("status", {}))

    @callback
    def async_start(self) -> None:
        """Build the model now and again every day at 12:00."""
        self._schedule_rebuild()
        self._unsub = async_track_time_change(
            self.hass, self._handle_noon, hour=12, minute=0, second=0
        )

    @callback
    def async_shutdown(self) -> None:
        """Stop the daily rebuild."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_noon(self, _now: datetime) -> None:
        self._schedule_rebuild()

    @callback
    def _schedule_rebuild(self) -> None:
        self.hass.async_create_background_task(
            self.async_rebuild(), f"{DOMAIN}_dream_model_{self._entry_id}"
        )

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------
    def _sun(self, night: date) -> tuple[float, float] | None:
        sunset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, night)
        if sunset is None:
            return None
        sunrise = get_astral_event_next(self.hass, SUN_EVENT_SUNRISE, sunset)
        if sunrise is None:
            return None
        return (
            to_night_seconds(dt_util.as_local(sunset), night),
            to_night_seconds(dt_util.as_local(sunrise), night),
        )

    async def async_rebuild(self) -> RoomModel:
        """Rebuild the room model from the learned days.

        Registries and sun times are read here; the model itself is built in
        an executor, as it is pure computation.
        """
        self._rebuilds += 1
        rebuild = self._rebuilds
        days = self._learning.days
        areas: dict[str, str | None] = {}

        def area_of(entity_id: str) -> str | None:
            if entity_id not in areas:
                areas[entity_id] = entity_area_id(self.hass, entity_id)
            return areas[entity_id]

        today = dt_util.now().date()
        nights: list[tuple[NightData, float, float]] = []
        for key in list(days):
            night = date.fromisoformat(key)
            if night >= today or (data := night_data(days, night, area_of)) is None:
                continue
            if (sun := self._sun(night)) is None:
                continue
            nights.append((data, *sun))
        kinds: dict[date, str] = {}
        source = None
        if self.context is not None and nights:
            # Learned nights get the same holiday-aware type as tonight.
            await self.context.async_ensure_holidays(
                min(data.night for data, _, _ in nights), today + timedelta(days=2)
            )
            kinds = {data.night: self.context.night_type(data.night) for data, _, _ in nights}
            source = self.context.night_type_source
        built_at = dt_util.utcnow()
        model = await self.hass.async_add_executor_job(
            model_from_nights,
            nights,
            today,
            self._settings.dream_half_life,
            built_at,
            kinds,
            source,
        )
        mornings = await self.hass.async_add_executor_job(
            mornings_from_nights,
            nights,
            today,
            self._settings.dream_half_life,
            built_at,
            kinds,
            source,
        )
        if rebuild == self._rebuilds:  # a newer rebuild wins over an older one
            self.model = model
            self.morning_model = mornings
            self._notify()
        return model

    def morning_first_on(self, kind: str) -> time | None:
        """Learned first switch-on of the mornings of a type (20th percentile)."""
        model = self.morning_model.day_types.get(kind) if self.morning_model else None
        if model is None or model.first_on is None or not model.nights:
            return None
        minutes = MORNING_AXIS_START_H * 60 + int(model.first_on)
        return time(minutes // 60, minutes % 60)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    async def async_plan(
        self, window: tuple[datetime, datetime], *, preview: bool = False
    ) -> NightPlan | None:
        """Plan tonight's Dream night; None means fall back to Replay."""
        night = night_of(dt_util.as_local(window[0]))
        bounds = (
            to_night_seconds(dt_util.as_local(window[0]), night),
            to_night_seconds(dt_util.as_local(window[1]), night),
        )
        # The morning window is a second segment of the same night.
        morning = dt_util.as_local(window[0]).time() < NIGHT_START
        key = "morning_plan" if morning else "plan"
        context: list[str] = ["morning"] if morning else []
        if self.context is not None:
            await self.context.async_ensure_holidays(night - timedelta(days=1), night + timedelta(days=2))
            kind = self.context.night_type(night)
            if kind != day_type(night):
                context.append("holiday")
        else:
            kind = day_type(night)
        saved_plan = self._saved.get(key)
        if not preview and saved_plan and saved_plan.get("night") == night.isoformat():
            # After a restart: tonight's plan was already chosen.
            plan = NightPlan.from_dict(saved_plan)
            self.status = dict(self._saved.get("status", {}))
            self._player.set_plan(plan)
            self._set_lights(plan.timeline.lights)
            self._notify()
            return plan

        if self.model is None or self.morning_model is None:
            await self.async_rebuild()
        model = self.morning_model if morning else self.model
        assert model is not None
        day_model = model.day_types.get(kind)
        nights = model.valid_nights(kind)
        if day_model is None or nights < self._settings.dream_min_days:
            self._set_status(
                None,
                DREAM_NOT_ENOUGH_NIGHTS,
                kind,
                nights=nights,
                preview=preview,
                context=context,
            )
            return None
        if morning:
            # Mornings use their own clock axis (05:00-10:00).
            offset, length = float(MORNING_AXIS_OFFSET), MORNING_AXIS_MIN
        else:
            if (sun := self._sun(night)) is None:
                self._set_status(None, DREAM_NO_WINDOW, kind, preview=preview)
                return None
            offset = sun[0] - 60 * 60
            length = min(int((sun[1] - offset) // 60), SLOTS * SLOT_MIN)
        start = max(0, math.ceil((bounds[0] - offset) / 60))
        end = min(length, math.floor((bounds[1] - offset) / 60))
        if morning and day_model.last_off is not None and day_model.last_off > start:
            end = min(end, math.ceil(day_model.last_off))  # the house empties then
        if end <= start:
            self._set_status(None, DREAM_NO_WINDOW, kind, preview=preview)
            return None

        history = [
            {(room, slot) for room, slot in item["slots"]}
            for item in self._saved.get("history", [])
        ]
        boost = 0.0
        bad_weather = self.context is not None and bool(self.context.weather()["bad"])
        if bad_weather and self.context is not None and self.context.boost > 0:
            boost = self.context.boost
            context.append("bad_weather")
        # An AI scenario changes tonight's evening (never the morning, never a preview).
        scenario = None
        scenario_reason: str | None = None
        keys: RoomKeys | None = None
        if not morning and not preview and self.ai is not None and self.ai.enabled:
            keys = self.room_keys(day_model)
            payload = scenario_payload(
                day_model,
                keys,
                kind,
                night.month,
                offset + 60 * 60,
                bad_weather,
                "holiday" in context,
                southern=self.hass.config.latitude < 0,
            )
            scenario, scenario_reason = await self.ai.async_scenario(night, payload, keys)
        base_seed = random.getrandbits(32)
        count = int(self._config.get(CONF_DREAM_CANDIDATES, DEFAULT_DREAM_CANDIDATES))
        max_similarity = float(
            self._config.get(CONF_DREAM_MAX_SIMILARITY, DEFAULT_DREAM_MAX_SIMILARITY)
        )
        candidates = await self.hass.async_add_executor_job(
            make_candidates,
            day_model,
            start,
            end,
            offset,
            base_seed,
            count,
            history,
            boost,
            scenario.modifiers if scenario else (),
        )
        chosen, reason = choose_candidate(
            candidates, self._settings.dream_min_score, max_similarity
        )
        if scenario is not None and chosen.score < self._settings.dream_min_score:
            # Not believable enough: a normal night instead.
            scenario, scenario_reason = None, SCENARIO_LOW_SCORE
            candidates = await self.hass.async_add_executor_job(
                make_candidates, day_model, start, end, offset, base_seed, count, history, boost
            )
            chosen, reason = choose_candidate(
                candidates, self._settings.dream_min_score, max_similarity
            )
        plan = self._build_plan(night, bounds, offset, chosen, reason, day_model)
        plan.preview = preview
        if scenario is not None and keys is not None:
            plan.scenario = keys.to_names(scenario.text)
            plan.explanation = keys.to_names(scenario.explanation)
            context.append("scenario")
        self._set_status(
            chosen.score,
            reason,
            kind,
            candidates=len(candidates),
            similarity_max=round(chosen.similarity, 2),
            preview=preview,
            context=context,
            boost=boost,
            scenario=plan.scenario,
            explanation=plan.explanation,
            scenario_reason=scenario_reason,
        )
        if not preview:
            history_items = list(self._saved.get("history", []))
            if not morning:  # novelty compares evenings
                history_items = [
                    *history_items,
                    {"night": night.isoformat(), "slots": sorted(room_slots(chosen.spans))},
                ][-HISTORY_NIGHTS:]
            self._saved = {
                **self._saved,
                key: plan.as_dict(),
                "history": history_items,
                "status": self.status,
            }
            await self._store.async_save(self._saved)
            self._set_lights(plan.timeline.lights)
        self._player.set_plan(plan)
        self._notify()
        return plan

    def _build_plan(
        self,
        night: date,
        bounds: tuple[float, float],
        offset: float,
        chosen: Candidate,
        reason: str,
        model: DayTypeModel,
    ) -> NightPlan:
        """Map rooms to tonight's lights and build the plan."""
        scope = set(resolve_learning_entities(self.hass, self._config))
        registry = er.async_get(self.hass)

        def allowed(entity_id: str) -> bool:
            entry = registry.async_get(entity_id)
            return entity_id in scope and (entry is None or not entry.disabled)

        targets = {
            area_id: light
            for area_id, light in area_targets(
                list(self._config.get(CONF_LIGHTS, [])),
                self._config.get(CONF_FRONT_LIGHTS, []),
                self._config.get(CONF_REAR_LIGHTS, []),
                lambda e: entity_area_id(self.hass, e),
            ).items()
            if allowed(light)
        }

        def target_of(room: str) -> str | None:
            if room.startswith("light:"):
                light = room.removeprefix("light:")
                return light if allowed(light) else None
            if self._settings.map_by_room and room in targets:
                return targets[room]
            # Otherwise the light used most in that room.
            stats = model.rooms.get(room)
            for light, _minutes in sorted(
                (stats.lights if stats else {}).items(), key=lambda item: -item[1]
            ):
                if allowed(light):
                    return light
            return None

        by_target: dict[str, list[tuple[int, int, str]]] = {}
        for room, spans in chosen.spans.items():
            if (target := target_of(room)) is None:
                continue
            by_target.setdefault(target, []).extend((a, b, room) for a, b in spans)

        raw: dict[str, list[Transition]] = {}
        for target, items in by_target.items():
            merged: list[list[Any]] = []
            for a, b, room in sorted(items):
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b, room])
            for a, b, room in merged:
                raw.setdefault(target, []).extend(
                    [
                        Transition(offset + a * 60, target, STATE_ON, None, room),
                        Transition(offset + b * 60, target, STATE_OFF, None, room),
                    ]
                )
        transitions = apply_jitter(raw, chosen.seed, self._settings.replay_jitter)
        names = {room: self._room_name(room) for room in chosen.spans}
        return NightPlan(
            night=night,
            window=bounds,
            timeline=Timeline({target: STATE_OFF for target in raw}, transitions),
            source=MODE_DREAM,
            seed=chosen.seed,
            score=chosen.score,
            reason=reason,
            names=names,
        )

    def room_keys(self, model: DayTypeModel) -> RoomKeys:
        """Opaque keys for tonight's rooms, busiest first (for the AI)."""
        names, aliases = area_names(self.hass, model.rooms)
        return build_keys(
            model.rooms,
            names,
            aliases,
            self.destination_areas(),
            order=lambda room: (-sum(model.rooms[room].occupancy), room),
        )

    def _room_name(self, room: str) -> str:
        if room.startswith("light:"):
            light = room.removeprefix("light:")
            state = self.hass.states.get(light)
            return state.name if state else light
        area = ar.async_get(self.hass).async_get_area(room)
        return area.name if area else room

    async def async_preview(self, window: tuple[datetime, datetime] | None) -> NightPlan | None:
        """Generate tonight's plan without executing it."""
        if window is None:
            return None
        return await self.async_plan(window, preview=True)

    @callback
    def note_fallback(self, source: str, replay_reason: str | None) -> None:
        """Dream could not plan tonight: say what ran instead."""
        self.status = {**self.status, "source": source, "replay_reason": replay_reason}
        self._notify()

    @callback
    def _set_status(
        self, score: float | None, reason: str, kind: str, *, preview: bool, **extra: Any
    ) -> None:
        status = {
            "score": score,
            "source": MODE_DREAM if score is not None else None,
            "reason": reason,
            "day_type": kind,
            "candidates": extra.get("candidates", 0),
            "similarity_max": extra.get("similarity_max"),
            "nights": extra.get("nights"),
            "context": extra.get("context", []),
            "boost_applied": bool(extra.get("boost")),
            "scenario": extra.get("scenario"),
            "explanation": extra.get("explanation"),
            "scenario_reason": extra.get("scenario_reason"),
        }
        if preview:
            self.preview_status = status
        else:
            self.status = status

    @callback
    def _set_lights(self, lights: list[str]) -> None:
        if self._on_lights is not None:
            self._on_lights(lights)

    # ------------------------------------------------------------------
    # Heatmap
    # ------------------------------------------------------------------
    async def async_heatmap(self, kind: str) -> dict[str, Any]:
        """Real vs dreamed average occupancy per room and hour (from 12:00)."""
        model = self.model or await self.async_rebuild()
        day_model = model.day_types.get(kind)
        hours = [(12 + h) % 24 for h in range(24)]
        if day_model is None or not day_model.rooms:
            return {"day_type": kind, "hours": hours, "rooms": [], "real": [], "dreamed": []}
        rooms = sorted(day_model.rooms, key=self._room_name)
        night = dt_util.now().date()
        night_type = self.context.night_type if self.context is not None else day_type
        while night_type(night) != kind:
            night += timedelta(days=1)
        dreamed = [[0.0] * 24 for _ in rooms]
        if (sun := self._sun(night)) is not None:
            offset = sun[0] - 60 * 60
            length = min(int((sun[1] - offset) // 60), SLOTS * SLOT_MIN)
            dreamed = await self.hass.async_add_executor_job(
                dreamed_hours, day_model, rooms, offset, length
            )
        return {
            "day_type": kind,
            "hours": hours,
            "rooms": [self._room_name(room) for room in rooms],
            "real": [[round(v, 3) for v in day_model.rooms[room].hours] for room in rooms],
            "dreamed": dreamed,
        }

    def summary(self) -> dict[str, Any]:
        """Model summary for the model sensor."""
        model = self.model
        if model is None:
            return {}
        mornings = self.morning_model
        return {
            "built_at": model.built_at.isoformat(),
            "night_type_source": model.night_type_source,
            "mornings": {
                kind: {
                    "nights": len(dm.nights),
                    "first_on": str(self.morning_first_on(kind) or "") or None,
                }
                for kind, dm in (mornings.day_types.items() if mornings else [])
            },
            **{
                kind: {
                    "nights": len(dm.nights),
                    "rooms": sorted(self._room_name(r) for r in dm.rooms),
                    "atypical_nights": [n.isoformat() for n in dm.atypical],
                }
                for kind, dm in model.day_types.items()
                if kind in DAY_TYPES
            },
        }

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))
