"""Room model for Dream mode.

Built only from the learned days (data the learning gate accepted). A room
is occupied while at least one of its learned lights is on; its area comes
from the stored event, else the registry, and lights without an area form
a room of their own ("light:<entity_id>").

Time runs on an axis relative to each night's sunset, in 15-minute slots
from 60 minutes before sunset to the next sunrise (at most 18 hours), so the
model follows the seasons. Nights are split by day type (weekday nights
start Sunday to Thursday, weekend nights Friday and Saturday) and weighted
by recency; atypical nights count less.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
import math
from statistics import median

from homeassistant.const import STATE_ON

from .const import REPLAY_MIN_COVERAGE
from .plan import DAY_S, DAY_TYPES, NOON_S, WEEKDAY, WEEKEND, day_type  # noqa: F401  (day types are re-exported for Dream)
from .replay import NightData

SLOT_MIN = 15
BEFORE_SUNSET_MIN = 60
MAX_AXIS_MIN = 18 * 60
SLOTS = MAX_AXIS_MIN // SLOT_MIN
FOLLOW_MIN = 10
ATYPICAL_Z = 3.0
ATYPICAL_WEIGHT = 0.3
ATYPICAL_MIN_NIGHTS = 5
CONCURRENCY_PERCENTILE = 0.95

EVENING = "evening"
LATE_NIGHT = "late_night"
SMALL_HOURS = "small_hours"

Interval = tuple[float, float]


def period_of(clock_minute: float) -> str:
    """Coarse period of a local clock time (minutes since midnight)."""
    minute = clock_minute % (24 * 60)
    if 12 * 60 <= minute < 23 * 60:
        return EVENING
    if minute >= 23 * 60 or minute < 2 * 60:
        return LATE_NIGHT
    return SMALL_HOURS


def room_key(entity_id: str, area_id: str | None) -> str:
    """Room of a light: its area, or a room of its own."""
    return area_id if area_id else f"light:{entity_id}"


def slot_of(minute: float) -> int:
    """Slot of an axis minute."""
    return min(max(int(minute // SLOT_MIN), 0), SLOTS - 1)


def overlap(intervals: Iterable[Interval], start: float, end: float) -> float:
    """Total length of the intervals inside [start, end)."""
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e in intervals)


def _merge(intervals: list[tuple[float, float, bool]]) -> list[tuple[float, float, bool]]:
    merged: list[tuple[float, float, bool]] = []
    for start, end, inside in sorted(intervals):
        if merged and start <= merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], end), last[2])
        else:
            merged.append((start, end, inside))
    return merged


def weighted_quantile(samples: list[tuple[float, float]], q: float) -> float:
    """Quantile of (value, weight) samples."""
    samples = sorted(s for s in samples if s[1] > 0)
    if not samples:
        return 0.0
    total = sum(w for _, w in samples)
    running = 0.0
    for value, weight in samples:
        running += weight
        if running >= q * total - 1e-9:
            return value
    return samples[-1][0]


# ----------------------------------------------------------------------
# One learned night on the axis
# ----------------------------------------------------------------------
@dataclass
class NightRooms:
    """Room occupancy of one learned night, on the sunset axis (minutes)."""

    night: date
    offset: float  # night clock seconds of axis minute 0
    length: int  # axis minutes until sunrise (at most 18 h)
    exposure: list[Interval]
    # room -> (start, end, started inside the axis)
    intervals: dict[str, list[tuple[float, float, bool]]]
    light_minutes: dict[str, dict[str, float]] = field(default_factory=dict)

    def clock_minute(self, minute: float) -> float:
        """Local clock minute (since midnight) of an axis minute."""
        return (self.offset / 60 + minute + NOON_S / 60) % (24 * 60)

    def coverage(self) -> float:
        """Share of the axis that was observed."""
        return overlap(self.exposure, 0, self.length) / self.length if self.length else 0.0

    def occupied_at(self, minute: float) -> int:
        """Number of rooms occupied at an axis minute."""
        return sum(
            any(s <= minute < e for s, e, _ in spans) for spans in self.intervals.values()
        )

    def total_occupied(self) -> float:
        """Occupied room-minutes of the night."""
        return sum(e - s for spans in self.intervals.values() for s, e, _ in spans)

    def max_concurrency(self) -> int:
        """Most rooms occupied at once (checked every slot)."""
        return max(
            (self.occupied_at(s * SLOT_MIN + SLOT_MIN / 2) for s in range(SLOTS)),
            default=0,
        )


def night_rooms(
    data: NightData,
    sunset: float,
    sunrise: float,
    room_of: Callable[[str, str | None], str] = room_key,
) -> NightRooms | None:
    """Turn a learned night into room occupancy on its sunset axis.

    `sunset` and `sunrise` are night clock seconds.
    """
    offset = sunset - BEFORE_SUNSET_MIN * 60
    length = int(min(sunrise - offset, MAX_AXIS_MIN * 60) // 60)
    if length <= 0:
        return None

    state: dict[str, str] = {}
    on_since: dict[str, float] = {}
    rooms: dict[str, str] = {}
    spans: dict[str, list[tuple[float, float]]] = {}
    for entity_id, (value, area_id) in data.base.items():
        state[entity_id] = value
        rooms[entity_id] = room_of(entity_id, area_id)
        if value == STATE_ON:
            on_since[entity_id] = 0.0
    for event in data.events:
        if state.get(event.entity_id) == event.state:
            continue
        state[event.entity_id] = event.state
        rooms[event.entity_id] = room_of(event.entity_id, event.area_id)
        if event.state == STATE_ON:
            on_since[event.entity_id] = event.seconds
        elif (began := on_since.pop(event.entity_id, None)) is not None:
            spans.setdefault(event.entity_id, []).append((began, event.seconds))
    for entity_id, began in on_since.items():
        spans.setdefault(entity_id, []).append((began, float(DAY_S)))

    def axis(seconds: float) -> float:
        return (seconds - offset) / 60

    by_room: dict[str, list[tuple[float, float, bool]]] = {}
    light_minutes: dict[str, dict[str, float]] = {}
    for entity_id, light_spans in spans.items():
        room = rooms[entity_id]
        for begin, end in light_spans:
            a, b = axis(begin), axis(end)
            if b <= 0 or a >= length:
                continue
            clipped = (max(a, 0.0), min(b, float(length)))
            by_room.setdefault(room, []).append((*clipped, a >= 0))
            minutes = light_minutes.setdefault(room, {})
            minutes[entity_id] = minutes.get(entity_id, 0.0) + clipped[1] - clipped[0]
    exposure = [
        (max(axis(s), 0.0), min(axis(e), float(length)))
        for s, e in data.exposure
        if axis(e) > 0 and axis(s) < length
    ]
    return NightRooms(
        night=data.night,
        offset=offset,
        length=length,
        exposure=exposure,
        intervals={room: _merge(items) for room, items in by_room.items()},
        light_minutes=light_minutes,
    )


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------
@dataclass
class RoomStats:
    """What a room does on one day type."""

    start_rate: list[float]  # starts per idle observed minute (at risk), per slot
    starts: list[float]  # expected starts per night, per slot
    occupancy: list[float]  # expected occupied share, per slot
    durations: dict[str, list[tuple[float, float]]]  # period -> (minutes, weight)
    follow_prob: float  # P(another room starts within 10 min | this one ended)
    follow_to: dict[str, float]  # which room, normalised
    hours: list[float]  # occupied share per hour from 12:00 (24 values)
    lights: dict[str, float]  # weighted minutes per learned light

    def all_durations(self) -> list[tuple[float, float]]:
        """Durations of every period."""
        return [item for items in self.durations.values() for item in items]


@dataclass
class DayTypeModel:
    """The model of one day type."""

    nights: list[date]
    atypical: list[date]
    rooms: dict[str, RoomStats]
    concurrency_cap: int
    concurrency: list[float]  # expected occupied rooms per slot


@dataclass
class RoomModel:
    """The model of every day type."""

    built_at: datetime
    day_types: dict[str, DayTypeModel]
    # Where the night types came from (calendar / weekday_rule / calendar_unavailable).
    night_type_source: str | None = None

    def valid_nights(self, kind: str) -> int:
        """Valid nights used for a day type."""
        model = self.day_types.get(kind)
        return len(model.nights) if model else 0


def _robust_outliers(values: dict[date, float]) -> set[date]:
    if len(values) < ATYPICAL_MIN_NIGHTS:
        return set()
    middle = median(values.values())
    mad = median(abs(v - middle) for v in values.values())
    if mad == 0:
        return set()
    return {
        night for night, v in values.items() if abs(0.6745 * (v - middle) / mad) > ATYPICAL_Z
    }


def build_day_type(nights: list[NightRooms], today: date, half_life: float) -> DayTypeModel:
    """Estimate one day type from its valid nights."""
    weights = {
        n.night: 0.5 ** (max((today - n.night).days, 0) / max(half_life, 0.1)) for n in nights
    }
    atypical = _robust_outliers({n.night: n.total_occupied() for n in nights}) | (
        _robust_outliers({n.night: float(n.max_concurrency()) for n in nights})
    )
    for night in atypical:
        weights[night] *= ATYPICAL_WEIGHT

    rooms = sorted({room for n in nights for room in n.intervals})
    exposed = {n.night: [overlap(n.exposure, s * SLOT_MIN, (s + 1) * SLOT_MIN) for s in range(SLOTS)] for n in nights}
    exposed_w = [sum(weights[n.night] * exposed[n.night][s] for n in nights) for s in range(SLOTS)]
    nights_w = [
        sum(weights[n.night] for n in nights if exposed[n.night][s] > 0) for s in range(SLOTS)
    ]

    stats: dict[str, RoomStats] = {}
    for room in rooms:
        occ = [0.0] * SLOTS
        idle = [0.0] * SLOTS
        starts = [0.0] * SLOTS
        durations: dict[str, list[tuple[float, float]]] = {}
        ended = followed = 0.0
        follow_to: dict[str, float] = {}
        hour_occ = [0.0] * 24
        hour_exp = [0.0] * 24
        lights: dict[str, float] = {}
        for n in nights:
            w = weights[n.night]
            spans = n.intervals.get(room, [])
            plain = [(s, e) for s, e, _ in spans]
            for s in range(SLOTS):
                lo, hi = s * SLOT_MIN, (s + 1) * SLOT_MIN
                if not exposed[n.night][s]:
                    continue
                inside = sum(
                    overlap(n.exposure, max(a, lo), min(b, hi)) for a, b in plain if b > lo and a < hi
                )
                occ[s] += w * inside
                idle[s] += w * (exposed[n.night][s] - inside)
            for start, end, started_inside in spans:
                if started_inside:
                    starts[slot_of(start)] += w
                    period = period_of(n.clock_minute(start))
                    durations.setdefault(period, []).append((end - start, w))
                if end < n.length:
                    ended += w
                    nxt = [
                        (s2, r2)
                        for r2, other in n.intervals.items()
                        if r2 != room
                        for s2, _, inside2 in other
                        if inside2 and end < s2 <= end + FOLLOW_MIN
                    ]
                    if nxt:
                        followed += w
                        first = min(nxt)[1]
                        follow_to[first] = follow_to.get(first, 0.0) + w
            for hour in range(24):
                lo = (hour * 3600 - n.offset) / 60
                hi = lo + 60
                exp = overlap(n.exposure, lo, hi)
                hour_exp[hour] += w * exp
                hour_occ[hour] += w * sum(
                    overlap(n.exposure, max(a, lo), min(b, hi)) for a, b in plain if b > lo and a < hi
                )
            for light, minutes in n.light_minutes.get(room, {}).items():
                lights[light] = lights.get(light, 0.0) + w * minutes
        total_follow = sum(follow_to.values())
        stats[room] = RoomStats(
            # The start minute itself was "at risk" too.
            start_rate=[
                starts[s] / (idle[s] + starts[s]) if idle[s] + starts[s] > 0 else 0.0
                for s in range(SLOTS)
            ],
            starts=[starts[s] / nights_w[s] if nights_w[s] > 0 else 0.0 for s in range(SLOTS)],
            occupancy=[occ[s] / exposed_w[s] if exposed_w[s] > 0 else 0.0 for s in range(SLOTS)],
            durations=durations,
            follow_prob=followed / ended if ended > 0 else 0.0,
            follow_to={r: v / total_follow for r, v in follow_to.items()} if total_follow else {},
            hours=[hour_occ[h] / hour_exp[h] if hour_exp[h] > 0 else 0.0 for h in range(24)],
            lights=lights,
        )

    samples: list[tuple[float, float]] = []
    profile = [0.0] * SLOTS
    for s in range(SLOTS):
        middle = s * SLOT_MIN + SLOT_MIN / 2
        total = weight = 0.0
        for n in nights:
            if exposed[n.night][s] <= 0:
                continue
            count = n.occupied_at(middle)
            samples.append((count, weights[n.night]))
            total += weights[n.night] * count
            weight += weights[n.night]
        profile[s] = total / weight if weight else 0.0
    cap = max(1, math.ceil(weighted_quantile(samples, CONCURRENCY_PERCENTILE)))
    return DayTypeModel(
        nights=sorted(n.night for n in nights),
        atypical=sorted(atypical),
        rooms=stats,
        concurrency_cap=cap,
        concurrency=profile,
    )


def build_model(
    nights: list[NightRooms],
    today: date,
    half_life: float,
    built_at: datetime,
    kinds: Mapping[date, str] | None = None,
    night_type_source: str | None = None,
) -> RoomModel:
    """Build the model from the valid learned nights.

    `kinds` gives each night's type (holidays included); else the plain rule.
    """
    valid = [n for n in nights if n.coverage() >= REPLAY_MIN_COVERAGE]

    def kind_of(night: date) -> str:
        return kinds[night] if kinds and night in kinds else day_type(night)

    return RoomModel(
        built_at=built_at,
        day_types={
            kind: build_day_type([n for n in valid if kind_of(n.night) == kind], today, half_life)
            for kind in DAY_TYPES
        },
        night_type_source=night_type_source,
    )


def boost_model(model: DayTypeModel, boost: float, offset: float) -> DayTypeModel:
    """Copy of a model whose two rooms busiest in the evening start more often."""
    if boost <= 0 or not model.rooms:
        return model

    def evening_occupancy(stats: RoomStats) -> float:
        return sum(
            value
            for slot, value in enumerate(stats.occupancy)
            if period_of((offset / 60 + slot * SLOT_MIN + 12 * 60) % (24 * 60)) == EVENING
        )

    busiest = sorted(model.rooms, key=lambda r: (-evening_occupancy(model.rooms[r]), r))[:2]
    rooms = dict(model.rooms)
    concurrency = list(model.concurrency)
    factor = 1 + boost
    for room in busiest:
        stats = rooms[room]
        # More starts mean more visits and more occupied time: the expected
        # values the score uses change with the rate, so boosted nights are
        # judged against what a bad-weather night looks like.
        occupancy = [min(value * factor, 1.0) for value in stats.occupancy]
        for slot, (old, new) in enumerate(zip(stats.occupancy, occupancy, strict=True)):
            concurrency[slot] += new - old
        rooms[room] = replace(
            stats,
            start_rate=[min(rate * factor, 1.0) for rate in stats.start_rate],
            starts=[value * factor for value in stats.starts],
            occupancy=occupancy,
        )
    return replace(model, rooms=rooms, concurrency=concurrency)
