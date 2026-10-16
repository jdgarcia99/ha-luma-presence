"""Covers: blinds, shutters and curtains open in the morning and close at dusk.

Covers move only during a real absence (never during a simulation test),
whether or not a light window is running, and the daylight guard never
blocks them: opening them by day is the point.

* Schedule: open at the workday or non-workday time (never before sunrise);
  close when the evening darkness decision is made (sunset without a
  sensor), plus a random delay per cover.
* Learned: human open/close changes are learned like lights. Replay uses
  the times of its source night (closing) and of the next morning
  (opening); otherwise the times are sampled per cover and day type
  (median ± spread). A cover with fewer than 7 observed days uses the
  schedule, and says so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
import random
import statistics
from typing import TYPE_CHECKING, Any

from homeassistant.const import SUN_EVENT_SUNRISE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_change,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COVERS,
    CONF_COVERS_CLOSE_DELAY,
    CONF_COVERS_MODE,
    CONF_COVERS_OPEN_NONWORKDAY,
    CONF_COVERS_OPEN_POSITION,
    CONF_COVERS_OPEN_WORKDAY,
    COVER_CLOSE,
    COVER_DREAM,
    COVER_NOT_ENOUGH_DAYS,
    COVER_OPEN,
    COVER_REPLAY,
    COVER_SCHEDULE,
    COVERS_LEARNED,
    COVERS_MIN_DAYS,
    DEFAULT_COVERS_CLOSE_DELAY,
    DEFAULT_COVERS_MODE,
    DEFAULT_COVERS_OPEN_NONWORKDAY,
    DEFAULT_COVERS_OPEN_POSITION,
    DEFAULT_COVERS_OPEN_WORKDAY,
    DOMAIN,
    MODE_REPLAY,
    START_PENDING,
    signal_update,
)
from .light_control import LightController
from .models import LumaSettings
from .plan import clock_text, night_of, to_night_seconds

if TYPE_CHECKING:
    from .context import ContextProvider
    from .learning import LearningGate
    from .plan import PlanPlayer
    from .replay import ReplayEngine

_LOGGER = logging.getLogger(__name__)

# A learned morning opening happens between 04:00 and 13:00, an evening
# closing from 15:00 on.
MORNING_FROM_S = 4 * 3600
MORNING_TO_S = 13 * 3600
EVENING_FROM_S = 15 * 3600
# Moves more than this late (Home Assistant was down) are skipped.
MISSED_AFTER = timedelta(minutes=30)
SAME_TYPE_MIN_DAYS = 3


def cover_value(state: Any) -> str | int | None:
    """Learned value of a settled cover state: "open", "closed" or a position."""
    if state is None or state.state not in ("open", "closed"):
        return None
    if state.state == "closed":
        return "closed"
    position = state.attributes.get("current_position")
    if position is None or int(position) >= 100:
        return "open"
    if int(position) <= 0:
        return "closed"
    return int(position)


def observations(
    days: Mapping[str, Mapping[str, list[Any]]], cover: str
) -> dict[date, tuple[int | None, int | None]]:
    """Per learned day: first opening in the morning and first closing in the evening."""
    result: dict[date, tuple[int | None, int | None]] = {}
    for key, day in days.items():
        opened: int | None = None
        closed: int | None = None
        for record in day.get("covers", []):
            entity_id, value, seconds = record[0], record[1], int(record[2])
            if entity_id != cover:
                continue
            is_closed = value == "closed" or value == 0
            if (
                not is_closed
                and opened is None
                and MORNING_FROM_S <= seconds < MORNING_TO_S
            ):
                opened = seconds
            if is_closed and closed is None and seconds >= EVENING_FROM_S:
                closed = seconds
        if opened is not None or closed is not None:
            result[date.fromisoformat(key)] = (opened, closed)
    return result


def sample_time(values: list[int], seed: str) -> int:
    """Median ± spread (median absolute deviation), reproducible per seed."""
    median = statistics.median(values)
    spread = statistics.median(abs(v - median) for v in values)
    return int(median + random.Random(seed).uniform(-spread, spread))


@dataclass(frozen=True)
class CoverMove:
    """A planned move of one cover."""

    at: datetime
    cover: str
    action: str  # open / close
    source: str  # schedule / replay / dream / schedule_not_enough_days
    day: date
    pending: bool = False  # the evening darkness is not decided yet

    def as_dict(self) -> dict[str, Any]:
        """For the plan sensor."""
        return {
            "entity_id": self.cover,
            "action": self.action,
            "at": self.at.isoformat(),
            "source": self.source,
        }


class CoverEngine:
    """Plans and executes the moves of the configured covers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: Mapping[str, Any],
        settings: LumaSettings,
        controller: LightController,
        learning: LearningGate,
        player: PlanPlayer,
        context: ContextProvider,
        replay: ReplayEngine | None = None,
    ) -> None:
        """Initialize the cover engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._settings = settings
        self._controller = controller
        self._learning = learning
        self._player = player
        self._context = context
        self._replay = replay
        self.covers: list[str] = list(config.get(CONF_COVERS) or [])
        self.mode = str(config.get(CONF_COVERS_MODE, DEFAULT_COVERS_MODE))
        self._position = int(
            config.get(CONF_COVERS_OPEN_POSITION, DEFAULT_COVERS_OPEN_POSITION)
        )
        self._open_workday = _time(
            config.get(CONF_COVERS_OPEN_WORKDAY), DEFAULT_COVERS_OPEN_WORKDAY
        )
        self._open_nonworkday = _time(
            config.get(CONF_COVERS_OPEN_NONWORKDAY), DEFAULT_COVERS_OPEN_NONWORKDAY
        )
        self._close_delay = max(
            float(config.get(CONF_COVERS_CLOSE_DELAY, DEFAULT_COVERS_CLOSE_DELAY)), 0.0
        )
        self.moves: list[CoverMove] = []
        self._done: set[tuple[date, str, str]] = set()
        self._delays: dict[tuple[date, str], float] = {}
        self._observed: dict[str, dict[date, tuple[int | None, int | None]]] = {}
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._started = False
        # Real absence (set by the integration; the player's rule by default).
        self.real_absence: Callable[[], bool] = player.real_absence

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def async_start(self) -> None:
        """Plan the next moves and follow the day."""
        if not self.covers:
            return
        self._started = True
        # Learned data changes slowly: refresh the plan every day at noon.
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._handle_noon, hour=12, minute=10, second=0
            )
        )
        self.async_refresh()

    @callback
    def async_shutdown(self) -> None:
        """Stop the timers."""
        self._started = False
        self._cancel_timer()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _handle_noon(self, _now: datetime) -> None:
        self._observed.clear()
        self.async_refresh()

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    @callback
    def async_refresh(self) -> None:
        """Recompute the upcoming moves (context, mode or learned data changed)."""
        if not self._started:
            return
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date()
        moves: list[CoverMove] = []
        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            for cover in self.covers:
                for move in (self._open_move(cover, day), self._close_move(cover, day)):
                    if (
                        move is not None
                        and (move.day, cover, move.action) not in self._done
                    ):
                        moves.append(move)
        self.moves = sorted(moves, key=lambda m: (m.at, m.cover))
        self._prune(today)
        self._attach_to_plan()
        self._schedule(now)
        self._notify()

    def upcoming(self) -> list[CoverMove]:
        """Moves still to come, within the next day."""
        now = dt_util.utcnow()
        return [
            m
            for m in self.moves
            if now - MISSED_AFTER < m.at <= now + timedelta(days=1)
        ]

    def _observed_days(self, cover: str) -> dict[date, tuple[int | None, int | None]]:
        if cover not in self._observed:
            self._observed[cover] = observations(self._learning.days, cover)
        return self._observed[cover]

    def _learned_source(self, cover: str) -> str | None:
        """Replay or Dream when learning is usable for this cover, else a fallback."""
        if self.mode != COVERS_LEARNED:
            return None
        if len(self._observed_days(cover)) < COVERS_MIN_DAYS:
            return COVER_NOT_ENOUGH_DAYS
        if self._settings.mode == MODE_REPLAY and self._replay_source() is not None:
            return COVER_REPLAY
        return COVER_DREAM

    def _replay_source(self) -> date | None:
        status = self._replay.status if self._replay is not None else None
        return status.get("source") if status else None

    def _open_move(self, cover: str, day: date) -> CoverMove | None:
        sunrise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, day)
        source = self._learned_source(cover) or COVER_SCHEDULE
        seconds: int | None = None
        if source == COVER_REPLAY and (night := self._replay_source()) is not None:
            # The morning after the source night.
            seconds = self._observed_days(cover).get(
                night + timedelta(days=1), (None, None)
            )[0]
        if source in (COVER_REPLAY, COVER_DREAM) and seconds is None:
            source = COVER_DREAM
            workday = self._context.is_workday(day)
            values = self._values(
                cover, 0, lambda d: self._context.is_workday(d) == workday
            )
            seconds = sample_time(values, f"{day}-{cover}-open") if values else None
        if seconds is not None:
            at = _at(day, seconds)
        else:
            source = source if source == COVER_NOT_ENOUGH_DAYS else COVER_SCHEDULE
            clock = (
                self._open_workday
                if self._context.is_workday(day)
                else self._open_nonworkday
            )
            at = dt_util.as_utc(
                datetime.combine(day, clock, tzinfo=dt_util.get_default_time_zone())
            )
            # Never before sunrise.
            if sunrise is not None:
                at = max(at, sunrise)
        return CoverMove(at, cover, COVER_OPEN, source, day)

    def _close_move(self, cover: str, day: date) -> CoverMove | None:
        source = self._learned_source(cover) or COVER_SCHEDULE
        seconds: int | None = None
        if source == COVER_REPLAY and (night := self._replay_source()) is not None:
            seconds = self._observed_days(cover).get(night, (None, None))[1]
        if source in (COVER_REPLAY, COVER_DREAM) and seconds is None:
            source = COVER_DREAM
            night_type = self._context.night_type(day)
            values = self._values(
                cover, 1, lambda d: self._context.night_type(d) == night_type
            )
            seconds = sample_time(values, f"{day}-{cover}-close") if values else None
        if seconds is not None:
            return CoverMove(_at(day, seconds), cover, COVER_CLOSE, source, day)
        source = source if source == COVER_NOT_ENOUGH_DAYS else COVER_SCHEDULE
        decision = self._context.evening_decision(day)
        if decision is None:
            return None
        start, reason = decision
        return CoverMove(
            start + timedelta(minutes=self._delay(day, cover)),
            cover,
            COVER_CLOSE,
            source,
            day,
            pending=reason == START_PENDING,
        )

    def _values(
        self, cover: str, index: int, same_type: Callable[[date], bool]
    ) -> list[int]:
        observed = self._observed_days(cover)
        all_values = [v[index] for v in observed.values() if v[index] is not None]
        same = [
            v[index]
            for d, v in observed.items()
            if v[index] is not None and same_type(d)
        ]
        return same if len(same) >= SAME_TYPE_MIN_DAYS else all_values  # type: ignore[return-value]

    def _delay(self, day: date, cover: str) -> float:
        """Random closing delay in minutes, fixed per day and cover."""
        key = (day, cover)
        if key not in self._delays:
            self._delays[key] = random.uniform(0, self._close_delay)
        return self._delays[key]

    def _prune(self, today: date) -> None:
        limit = today - timedelta(days=3)
        self._done = {k for k in self._done if k[0] >= limit}
        self._delays = {k: v for k, v in self._delays.items() if k[0] >= limit}

    @callback
    def _attach_to_plan(self) -> None:
        """Show tonight's moves in tonight's plan."""
        plan = self._player.plan
        if plan is not None:
            plan.covers = self.plan_items(plan.night)

    def plan_items(self, night: date) -> list[list[str]]:
        """[time, cover, action, source] of a night (its evening and next morning)."""
        items = []
        for move in self.moves:
            local = dt_util.as_local(move.at)
            if night_of(local) == night or (
                move.action == COVER_OPEN and move.day == night + timedelta(days=1)
            ):
                items.append(
                    [
                        clock_text(to_night_seconds(local, night)),
                        move.cover,
                        move.action,
                        move.source,
                    ]
                )
        return items

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    @callback
    def _schedule(self, now: datetime) -> None:
        self._cancel_timer()
        future = [m.at for m in self.moves if m.at > now]
        due = [m for m in self.moves if m.at <= now]
        if due:
            self.hass.async_create_task(
                self._async_run_due(), f"{DOMAIN}_covers_{self._entry_id}"
            )
        if future:
            self._unsub_timer = async_track_point_in_utc_time(
                self.hass, self._handle_timer, min(future)
            )

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _handle_timer(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.async_refresh()

    async def _async_run_due(self) -> None:
        now = dt_util.utcnow()
        for move in [m for m in self.moves if m.at <= now]:
            key = (move.day, move.cover, move.action)
            if key in self._done:
                continue
            if move.pending:
                continue  # waiting for the darkness decision
            self._done.add(key)
            if now - move.at > MISSED_AFTER:
                continue  # Home Assistant was down: not caught up
            if not self.real_absence():
                _LOGGER.debug("Not moving %s: the house is not empty", move.cover)
                continue
            state = self.hass.states.get(move.cover)
            if state is None or state.state in ("unavailable", "unknown"):
                continue
            await self._controller.async_move_cover(
                move.cover, move.action, self._position, source=move.source
            )
        self.moves = [
            m for m in self.moves if (m.day, m.cover, m.action) not in self._done
        ]
        self._notify()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))


def _at(day: date, seconds: int) -> datetime:
    """UTC time of a clock time (seconds after local midnight) on a date."""
    naive = datetime.combine(day, time()) + timedelta(seconds=seconds)
    return dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))


def _time(value: Any, default: str) -> time:
    try:
        return time.fromisoformat(str(value or default))
    except ValueError:
        return time.fromisoformat(default)
