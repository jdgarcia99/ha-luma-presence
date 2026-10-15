"""Night plans and the player that executes them.

A ``NightPlan`` is the single representation of what happens tonight:
initial states and timed transitions per target, wherever they come from
(Replay of a learned night, a Dream night, later covers or media). The
``PlanPlayer`` executes any plan through the shared ``LightController``.

Times use the *night clock*: seconds since 12:00 on the day the night
starts, so clock times are kept across DST changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
import logging
import random
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    OWNER_SIMULATION,
    REASON_ALARM,
    REASON_ALARM_VACATION,
    REASON_AWAY,
    REPLAY_MIN_GAP_S,
)
from .light_control import LightController

if TYPE_CHECKING:
    from .learning import LearningGate

_LOGGER = logging.getLogger(__name__)

NIGHT_START = time(12, 0)
NOON_S = 12 * 3600
DAY_S = 24 * 3600
PLAN_SENSOR_MAX_ITEMS = 200

# Learning gate reasons that mean the house is really empty.
REAL_ABSENCE_REASONS = frozenset({REASON_ALARM, REASON_ALARM_VACATION, REASON_AWAY})


# ----------------------------------------------------------------------
# Night clock
# ----------------------------------------------------------------------
def night_of(local: datetime) -> date:
    """Return the date on which the night containing a local time starts."""
    day = local.date()
    return day if local.time() >= NIGHT_START else day - timedelta(days=1)


def to_night_seconds(local: datetime, night: date) -> float:
    """Night clock of a local time (wall clock, so DST keeps clock times)."""
    return (
        local.replace(tzinfo=None) - datetime.combine(night, NIGHT_START)
    ).total_seconds()


def from_night_seconds(night: date, seconds: float) -> datetime:
    """UTC time of a night clock value on a given night."""
    naive = datetime.combine(night, NIGHT_START) + timedelta(seconds=seconds)
    return dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))


def clock_text(seconds: float) -> str:
    """Local clock time "HH:MM" of a night clock value."""
    minutes = int((NOON_S + seconds) // 60) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ----------------------------------------------------------------------
# Timeline
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Transition:
    """A target changes state at a night clock time.

    `original` is the light the change comes from (Replay) and `room` the
    room it stands for (Dream), when they are known.
    """

    seconds: float
    target: str
    state: str
    original: str | None = None
    room: str | None = None


@dataclass
class Timeline:
    """Initial states at 12:00 and the transitions of the night."""

    initial: dict[str, str]
    transitions: list[Transition]

    @property
    def lights(self) -> list[str]:
        """All targets of the night."""
        return sorted(set(self.initial) | {t.target for t in self.transitions})

    def state_at(self, seconds: float) -> dict[str, str]:
        """Desired state of each target at a night clock time."""
        states = dict(self.initial)
        for transition in self.transitions:
            if transition.seconds > seconds:
                break
            states[transition.target] = transition.state
        return states


def apply_jitter(
    raw: dict[str, list[Transition]], seed: int, jitter_minutes: float
) -> list[Transition]:
    """Shift each transition randomly, keeping each target's order a minute apart."""
    rng = random.Random(seed)
    spread = max(jitter_minutes, 0) * 60
    transitions: list[Transition] = []
    for target in sorted(raw):
        previous: float | None = None
        for transition in raw[target]:
            seconds = transition.seconds + (rng.uniform(-spread, spread) if spread else 0)
            seconds = min(max(seconds, 0.0), DAY_S - 1)
            if previous is not None:
                seconds = max(seconds, previous + REPLAY_MIN_GAP_S)
            previous = seconds
            transitions.append(replace(transition, seconds=seconds))
    transitions.sort(key=lambda t: (t.seconds, t.target))
    return transitions


# ----------------------------------------------------------------------
# Night plan
# ----------------------------------------------------------------------
@dataclass
class NightPlan:
    """What happens tonight, whatever its source."""

    night: date
    window: tuple[float, float]
    timeline: Timeline
    source: str  # replay / dream / fallback
    seed: int
    score: float | None = None
    scenario: str | None = None  # v0.9.0
    reason: str | None = None
    preview: bool = False
    # Display names of the rooms or targets, for the summary.
    names: dict[str, str] = field(default_factory=dict)

    @property
    def transitions(self) -> list[Transition]:
        """Transitions of the night."""
        return self.timeline.transitions

    def state_at(self, seconds: float) -> dict[str, str]:
        """Desired state of each target at a night clock time."""
        return self.timeline.state_at(seconds)

    def in_window(self) -> list[Transition]:
        """Transitions played inside the window."""
        start, end = self.window
        return [t for t in self.transitions if start < t.seconds < end]

    @property
    def rooms(self) -> list[str]:
        """Rooms (or targets) used tonight, by display name."""
        keys = {t.room or t.target for t in self.transitions}
        keys |= {k for k, v in self.timeline.initial.items() if v == STATE_ON}
        return sorted({self.names.get(key, key) for key in keys})

    def summary(self) -> str:
        """Short text: "Salon 20:10–23:40 · Kitchen 21:05–21:30"."""
        start, end = self.window
        spans: list[tuple[float, str]] = []
        state = self.state_at(start)
        open_at: dict[str, float] = {
            target: start for target, value in state.items() if value == STATE_ON
        }
        room_of: dict[str, str] = {}
        for transition in self.transitions:
            if transition.room:
                room_of[transition.target] = transition.room
            if not start < transition.seconds < end:
                continue
            if transition.state == STATE_ON:
                open_at.setdefault(transition.target, transition.seconds)
            elif (began := open_at.pop(transition.target, None)) is not None:
                spans.append((began, self._span(transition.target, began, transition.seconds, room_of)))
        for target, began in open_at.items():
            spans.append((began, self._span(target, began, end, room_of)))
        return " · ".join(text for _, text in sorted(spans))

    def _span(self, target: str, start: float, end: float, room_of: dict[str, str]) -> str:
        key = room_of.get(target, target)
        return f"{self.names.get(key, key)} {clock_text(start)}–{clock_text(end)}"

    def compact(self) -> list[list[str]]:
        """[time, target, state] of the window, for the plan sensor."""
        return [
            [clock_text(t.seconds), t.target, t.state]
            for t in self.in_window()[:PLAN_SENSOR_MAX_ITEMS]
        ]

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return {
            "night": self.night.isoformat(),
            "window": list(self.window),
            "initial": self.timeline.initial,
            "transitions": [asdict(t) for t in self.transitions],
            "source": self.source,
            "seed": self.seed,
            "score": self.score,
            "scenario": self.scenario,
            "reason": self.reason,
            "names": self.names,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NightPlan:
        """Rebuild a stored plan."""
        return cls(
            night=date.fromisoformat(data["night"]),
            window=(float(data["window"][0]), float(data["window"][1])),
            timeline=Timeline(
                dict(data["initial"]),
                [Transition(**item) for item in data["transitions"]],
            ),
            source=data["source"],
            seed=int(data["seed"]),
            score=data.get("score"),
            scenario=data.get("scenario"),
            reason=data.get("reason"),
            names=dict(data.get("names", {})),
        )


# ----------------------------------------------------------------------
# Player
# ----------------------------------------------------------------------
class PlanPlayer:
    """Executes night plans through the light controller.

    At the start (or after a restart) the current state is reconciled with the
    plan; then each transition inside the window is applied on time. Lights
    locked by a route get their state when released. The ownership rule
    applies, except that during a real absence any light of the plan may be
    switched off, as in the other modes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        controller: LightController,
        learning: LearningGate,
    ) -> None:
        """Initialize the player."""
        self.hass = hass
        self._entry_id = entry_id
        self._controller = controller
        self._learning = learning
        # Tonight's plan, and a Dream preview shown until a real plan replaces it.
        self.plan: NightPlan | None = None
        self.preview: NightPlan | None = None
        self._pending: dict[str, str] = {}
        self._active = False
        self._mode: str | None = None
        self._unsub_unlock: CALLBACK_TYPE = controller.add_unlock_listener(
            self._handle_unlock
        )

    @callback
    def async_shutdown(self) -> None:
        """Stop listening to unlocks."""
        self._unsub_unlock()

    @property
    def shown(self) -> NightPlan | None:
        """The plan the plan sensor shows: a preview, else tonight's plan."""
        return self.preview or self.plan

    @callback
    def set_plan(self, plan: NightPlan | None) -> None:
        """Set tonight's plan, or show a preview without touching it."""
        if plan is not None and plan.preview:
            self.preview = plan
        else:
            self.plan = plan
            self.preview = None

    def real_absence(self) -> bool:
        """Return True while the house is really empty."""
        return bool(self._learning.reasons & REAL_ABSENCE_REASONS)

    async def async_play(self, plan: NightPlan) -> None:
        """Play a plan until cancelled by the end of the window."""
        start, end = plan.window
        self.set_plan(plan)
        self._mode = plan.source
        self._active = True
        self._pending.clear()
        try:
            # At the window start, or after a restart: reconcile the current state.
            now = max(
                to_night_seconds(dt_util.as_local(dt_util.utcnow()), plan.night), start
            )
            for target, state in sorted(plan.state_at(now).items()):
                await self._async_apply(target, state, None)
            for transition in plan.transitions:
                if transition.seconds <= now:
                    continue
                if transition.seconds >= end:
                    break  # after the window end
                at = from_night_seconds(plan.night, transition.seconds)
                if (delay := (at - dt_util.utcnow()).total_seconds()) > 0:
                    await asyncio.sleep(delay)
                await self._async_apply(
                    transition.target, transition.state, transition.original
                )
            await asyncio.Event().wait()  # stay active until the window ends
        finally:
            self._active = False
            self._pending.clear()

    async def _async_apply(self, target: str, desired: str, original: str | None) -> None:
        state = self.hass.states.get(target)
        if state is None or state.state not in (STATE_ON, STATE_OFF):
            return  # unavailable now: skip
        if self._controller.is_locked(target):
            # A route is using it: apply the desired state once it is released.
            self._pending[target] = desired
            return
        self._pending.pop(target, None)
        extra: dict[str, Any] = {"mode": self._mode}
        if original is not None and original != target:
            extra["original"] = original
        if desired == STATE_ON and state.state == STATE_OFF:
            await self._controller.async_switch(target, True, OWNER_SIMULATION, **extra)
        elif (
            desired == STATE_OFF
            and state.state == STATE_ON
            and (
                self._controller.owner_of(target) == OWNER_SIMULATION
                or self.real_absence()
            )
        ):
            await self._controller.async_switch(target, False, **extra)

    @callback
    def _handle_unlock(self, entities: list[str]) -> None:
        if not self._active:
            return
        for entity_id in entities:
            if (desired := self._pending.pop(entity_id, None)) is not None:
                self.hass.async_create_task(
                    self._async_apply(entity_id, desired, None),
                    f"{DOMAIN}_plan_unlock_{self._entry_id}",
                )
