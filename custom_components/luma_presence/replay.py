"""Replay mode: reproduce a real night of the house.

Replay plays back what LumaPresence learned (see ``learning.py``), not the
recorder: only nights when the house was occupied and only human actions,
kept in LumaPresence's own storage whatever the recorder's retention. So it
never replays an empty house or a previous simulation.

Times use a *night clock*: seconds since 12:00 on the day the night starts.
A night spans two learned day records, and an event at 21:14 on the source
night plays at about 21:14 tonight.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
import random
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FRONT_LIGHTS,
    CONF_LIGHTS,
    CONF_REAR_LIGHTS,
    DOMAIN,
    MODE_REPLAY,
    REPLAY_MIN_COVERAGE,
    REPLAY_MOST_RECENT,
    REPLAY_NO_DATA,
    REPLAY_NOT_ENOUGH_EVENTS,
    REPLAY_NOT_ENOUGH_EXPOSURE,
    REPLAY_SAME_DAY_TYPE,
    REPLAY_SAME_DAY_TYPE_DAYS,
    REPLAY_SAME_WEEKDAY,
    REPLAY_SAME_WEEKDAY_DAYS,
    STORE_VERSION,
    signal_update,
)
from .learning import LearningGate, parse_event
from .light_control import LightController
from .models import LumaSettings
from .plan import (
    NOON_S,
    NightPlan,
    PlanPlayer,
    Timeline,
    Transition,
    apply_jitter,
    night_of,
    to_night_seconds,
)
from .scope import entity_area_id, resolve_learning_entities

_LOGGER = logging.getLogger(__name__)

AreaResolver = Callable[[str], str | None]


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return state.name if state is not None else entity_id


def is_weekend_night(night: date) -> bool:
    """Friday, Saturday and Sunday nights are weekend nights."""
    return night.weekday() >= 4


# ----------------------------------------------------------------------
# Source nights
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class SourceEvent:
    """A learned event on the night clock."""

    seconds: float
    entity_id: str
    state: str
    area_id: str | None


@dataclass
class NightData:
    """What was learned on one night."""

    night: date
    events: list[SourceEvent]
    exposure: list[tuple[float, float]]
    # State and area of each light at 12:00, from the last earlier event.
    base: dict[str, tuple[str, str | None]]

    def coverage(self, start: float, end: float) -> float:
        """Share of [start, end) that was observed."""
        if end <= start:
            return 0.0
        covered = sum(
            max(0.0, min(e, end) - max(s, start)) for s, e in self.exposure
        )
        return covered / (end - start)

    def events_between(self, start: float, end: float) -> int:
        """Number of events in [start, end)."""
        return sum(1 for event in self.events if start <= event.seconds < end)


def night_data(
    days: Mapping[str, Mapping[str, list[Any]]],
    night: date,
    area_of: AreaResolver,
) -> NightData | None:
    """Collect a night from its two day records, or None if one is missing."""
    first = days.get(night.isoformat())
    second = days.get((night + timedelta(days=1)).isoformat())
    if first is None or second is None:
        return None

    def parsed(day: Mapping[str, list[Any]] | None) -> list[tuple[str, str, int, str | None]]:
        records = [parse_event(record) for record in (day or {}).get("events", [])]
        return sorted(records, key=lambda record: record[2])

    def area(entity_id: str, area_id: str | None) -> str | None:
        # Events stored before v0.2.0 have no area: use today's registry.
        return area_id if area_id is not None else area_of(entity_id)

    events = [
        SourceEvent(seconds - NOON_S, entity_id, state, area(entity_id, area_id))
        for entity_id, state, seconds, area_id in parsed(first)
        if seconds >= NOON_S
    ] + [
        SourceEvent(seconds + NOON_S, entity_id, state, area(entity_id, area_id))
        for entity_id, state, seconds, area_id in parsed(second)
        if seconds < NOON_S
    ]
    exposure = [
        (max(s, NOON_S) - NOON_S, e - NOON_S)
        for s, e in first.get("exposure", [])
        if e > NOON_S
    ] + [
        (s + NOON_S, min(e, NOON_S) + NOON_S)
        for s, e in second.get("exposure", [])
        if s < NOON_S
    ]
    base: dict[str, tuple[str, str | None]] = {}
    previous = days.get((night - timedelta(days=1)).isoformat())
    morning = [record for record in parsed(first) if record[2] < NOON_S]
    for entity_id, state, _seconds, area_id in [*parsed(previous), *morning]:
        base[entity_id] = (state, area(entity_id, area_id))
    return NightData(night, events, exposure, base)


@dataclass(frozen=True)
class SourceChoice:
    """The chosen source night, or why there is none."""

    source: date | None
    reason: str
    events: int
    eligible: int


def choose_source(
    days: Mapping[str, Mapping[str, list[Any]]],
    tonight: date,
    window: tuple[float, float],
    min_events: int,
    previous: date | None,
    area_of: AreaResolver,
) -> SourceChoice:
    """Pick the source night for tonight's window (on the night clock)."""
    start, end = window
    any_data = any_exposure = False
    eligible: list[tuple[date, int]] = []
    nights = sorted(
        {date.fromisoformat(key) for key in days} - {tonight}, reverse=True
    )
    for night in nights:
        if night >= tonight or (data := night_data(days, night, area_of)) is None:
            continue
        any_data = True
        if data.coverage(start, end) < REPLAY_MIN_COVERAGE:
            continue
        any_exposure = True
        if (count := data.events_between(start, end)) < min_events:
            continue
        eligible.append((night, count))

    if not eligible:
        if not any_data:
            reason = REPLAY_NO_DATA
        elif not any_exposure:
            reason = REPLAY_NOT_ENOUGH_EXPOSURE
        else:
            reason = REPLAY_NOT_ENOUGH_EVENTS
        return SourceChoice(None, reason, 0, 0)

    # Never the same source two nights in a row, unless it is the only one.
    pool = [item for item in eligible if item[0] != previous] or eligible
    tiers: tuple[tuple[str, Callable[[date], bool]], ...] = (
        (
            REPLAY_SAME_WEEKDAY,
            lambda night: night.weekday() == tonight.weekday()
            and (tonight - night).days <= REPLAY_SAME_WEEKDAY_DAYS,
        ),
        (
            REPLAY_SAME_DAY_TYPE,
            lambda night: is_weekend_night(night) == is_weekend_night(tonight)
            and (tonight - night).days <= REPLAY_SAME_DAY_TYPE_DAYS,
        ),
        (REPLAY_MOST_RECENT, lambda night: True),
    )
    for reason, matches in tiers:
        candidates = [item for item in pool if matches(item[0])]
        if candidates:
            night, count = max(candidates)
            return SourceChoice(night, reason, count, len(eligible))
    raise AssertionError("the last tier always matches")  # pragma: no cover


# ----------------------------------------------------------------------
# Timeline
# ----------------------------------------------------------------------
def area_targets(
    lights: list[str],
    front: Iterable[str],
    rear: Iterable[str],
    area_of: AreaResolver,
) -> dict[str, str]:
    """Simulation light that stands for each area: front, rear, then interior."""
    front, rear = set(front), set(rear)
    ranked = sorted(
        enumerate(lights),
        key=lambda item: (
            0 if item[1] in front else 1 if item[1] in rear else 2,
            item[0],
        ),
    )
    targets: dict[str, str] = {}
    for _, light in ranked:
        if (area_id := area_of(light)) is not None:
            targets.setdefault(area_id, light)
    return targets


def build_timeline(
    data: NightData,
    *,
    by_room: bool,
    targets: Mapping[str, str],
    allowed: Callable[[str], bool],
    seed: int,
    jitter_minutes: float,
) -> Timeline:
    """Map the source lights to tonight's lights and apply the jitter.

    With `by_room`, an event targets the simulation light standing for its
    area, or the original light when the area has none. Several source lights
    on one target follow the room's occupancy: on with the first, off with
    the last.
    """

    def target_of(entity_id: str, area_id: str | None) -> str | None:
        if not allowed(entity_id):
            return None
        if by_room and area_id is not None and area_id in targets:
            return targets[area_id]
        return entity_id

    sources_on: dict[str, set[str]] = {}
    initial: dict[str, str] = {}
    source_state: dict[str, str] = {}

    for entity_id, (state, area_id) in data.base.items():
        source_state[entity_id] = state
        if (target := target_of(entity_id, area_id)) is None:
            continue
        on = sources_on.setdefault(target, set())
        if state == STATE_ON:
            on.add(entity_id)
        initial[target] = STATE_ON if on else STATE_OFF

    raw: dict[str, list[Transition]] = {}
    for event in data.events:
        if event.state not in (STATE_ON, STATE_OFF):
            continue
        if source_state.get(event.entity_id) == event.state:
            continue
        source_state[event.entity_id] = event.state
        if (target := target_of(event.entity_id, event.area_id)) is None:
            continue
        initial.setdefault(target, STATE_OFF)
        on = sources_on.setdefault(target, set())
        was_on = bool(on)
        if event.state == STATE_ON:
            on.add(event.entity_id)
        else:
            on.discard(event.entity_id)
        if bool(on) != was_on:
            raw.setdefault(target, []).append(
                Transition(event.seconds, target, event.state, event.entity_id)
            )

    return Timeline(initial, apply_jitter(raw, seed, jitter_minutes))


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
@dataclass
class ReplayPlan:
    """Tonight's playback."""

    night: date
    source: date
    window: tuple[float, float]
    timeline: Timeline
    seed: int = 0
    names: dict[str, str] = field(default_factory=dict)

    def to_night_plan(self) -> NightPlan:
        """The plan the player executes."""
        return NightPlan(
            night=self.night,
            window=self.window,
            timeline=self.timeline,
            source=MODE_REPLAY,
            seed=self.seed,
            names=self.names,
        )


class ReplayEngine:
    """Plans and plays Replay nights for the simulation engine."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: Mapping[str, Any],
        settings: LumaSettings,
        controller: LightController,
        learning: LearningGate,
        player: PlanPlayer,
        on_lights: Callable[[list[str]], None] | None = None,
    ) -> None:
        """Initialize the replay engine.

        `on_lights` is told which lights Replay uses tonight.
        """
        self.hass = hass
        self._entry_id = entry_id
        self._config = config
        self._settings = settings
        self._controller = controller
        self._learning = learning
        self._on_lights = on_lights
        self._player = player
        self.status: dict[str, Any] | None = None
        self._saved: dict[str, Any] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.replay"
        )

    async def async_load(self) -> None:
        """Load tonight's choice, kept across restarts."""
        self._saved = await self._store.async_load() or {}
        if self._saved:
            self.status = self._status(self._saved.get("source"))

    @callback
    def async_shutdown(self) -> None:
        """Nothing to release: the player owns the unlock listener."""

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    async def async_plan(self, window: tuple[datetime, datetime]) -> ReplayPlan | None:
        """Choose tonight's source and build the timeline; None means fall back."""
        night = night_of(dt_util.as_local(window[0]))
        bounds = (
            to_night_seconds(dt_util.as_local(window[0]), night),
            to_night_seconds(dt_util.as_local(window[1]), night),
        )
        areas: dict[str, str | None] = {}

        def area_of(entity_id: str) -> str | None:
            if entity_id not in areas:
                areas[entity_id] = entity_area_id(self.hass, entity_id)
            return areas[entity_id]

        days = self._learning.days
        if self._saved.get("night") != night.isoformat():
            # A new night: choose again. After a restart the saved choice is kept.
            saved = self._saved
            yesterday = (night - timedelta(days=1)).isoformat()
            previous = (
                date.fromisoformat(saved["source"])
                if saved.get("night") == yesterday and saved.get("source")
                else None
            )
            choice = choose_source(
                days, night, bounds, self._settings.replay_min_events, previous, area_of
            )
            self._saved = {
                "night": night.isoformat(),
                "source": choice.source.isoformat() if choice.source else None,
                "seed": random.getrandbits(32),
                "reason": choice.reason,
                "events": choice.events,
                "eligible_nights": choice.eligible,
            }
            await self._store.async_save(self._saved)

        source = self._saved.get("source")
        data = (
            night_data(days, date.fromisoformat(source), area_of) if source else None
        )
        if data is None:
            if source:  # its data was forgotten since the choice
                self._saved = {**self._saved, "source": None, "reason": REPLAY_NO_DATA}
            self.status = self._status(None)
            self._set_lights([])
            self._notify()
            _LOGGER.debug("Replay falls back to Sun mode: %s", self._saved.get("reason"))
            return None

        scope = set(resolve_learning_entities(self.hass, self._config))
        registry = er.async_get(self.hass)

        def allowed(entity_id: str) -> bool:
            entry = registry.async_get(entity_id)
            return entity_id in scope and (entry is None or not entry.disabled)

        lights = list(self._config.get(CONF_LIGHTS, []))
        timeline = build_timeline(
            data,
            by_room=self._settings.map_by_room,
            targets={
                area_id: light
                for area_id, light in area_targets(
                    lights,
                    self._config.get(CONF_FRONT_LIGHTS, []),
                    self._config.get(CONF_REAR_LIGHTS, []),
                    area_of,
                ).items()
                if allowed(light)
            },
            allowed=allowed,
            seed=int(self._saved["seed"]),
            jitter_minutes=self._settings.replay_jitter,
        )
        self.status = self._status(source)
        self._set_lights(timeline.lights)
        plan = ReplayPlan(
            night,
            data.night,
            bounds,
            timeline,
            seed=int(self._saved["seed"]),
            names={light: _name(self.hass, light) for light in timeline.lights},
        )
        self._player.set_plan(plan.to_night_plan())
        self._notify()
        return plan

    def _status(self, source: str | None) -> dict[str, Any]:
        return {
            "source": date.fromisoformat(source) if source else None,
            "reason": self._saved.get("reason"),
            "events": self._saved.get("events", 0) if source else 0,
            "eligible_nights": self._saved.get("eligible_nights", 0),
            "by_room": self._settings.map_by_room,
        }

    @callback
    def _set_lights(self, lights: list[str]) -> None:
        if self._on_lights is not None:
            self._on_lights(lights)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    async def async_play(self, plan: ReplayPlan) -> None:
        """Play tonight's timeline until cancelled by the end of the window."""
        await self._player.async_play(plan.to_night_plan())

    def _real_absence(self) -> bool:
        """Return True while the house is really empty (see PlanPlayer)."""
        return self._player.real_absence()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))
