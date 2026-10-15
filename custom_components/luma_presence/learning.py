"""Learning gate: collects real (human) light usage while the house is lived in.

Phase 1 only collects and stores the data; the Dream mode will consume it.

Rules
-----
* Learning stops while the simulation is enabled, the alarm is armed away,
  nobody is home, or the user paused it manually.
* The simulation enabled while someone is home is a test, not an absence:
  learning pauses as with a manual pause, with no return margin.
* Events are kept in a pending buffer for ``margin_before`` hours before
  being committed, so the hours just before a departure can be discarded.
* After a return, learning stays closed for ``margin_after`` hours. After
  an absence longer than 48 h the whole day of the return is excluded.
* Excluded time is never counted as "no activity": every stored day keeps
  the exposure intervals actually observed, so statistics are normalised
  by observed time only.
* The learned entities come from the learning scope (see ``scope.py``) and
  are recomputed when the entity, device, area or floor registry changes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.const import (
    STATE_HOME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ABSENCE_REASONS,
    COMMIT_INTERVAL,
    CONF_ALARM,
    CONF_ALARM_ACTIVATE_STATES,
    CONF_ALARM_CONTROL,
    CONF_HUMAN_ONLY,
    CONF_MARGIN_AFTER,
    CONF_MARGIN_BEFORE,
    CONF_PERSONS,
    DEFAULT_ALARM_ACTIVATE_STATES,
    DEFAULT_ALARM_CONTROL,
    DEFAULT_HUMAN_ONLY,
    DEFAULT_MARGIN_AFTER,
    DEFAULT_MARGIN_BEFORE,
    DOMAIN,
    LONG_ABSENCE,
    REASON_ALARM,
    REASON_ALARM_VACATION,
    REASON_AWAY,
    REASON_MARGIN,
    REASON_PAUSED,
    REASON_SIMULATION,
    REASON_SIMULATION_TEST,
    SCOPE_DEBOUNCE_S,
    STORE_MAX_DAYS,
    STORE_VERSION,
    VALID_DAY_MIN_EXPOSURE_S,
    signal_update,
)
from .models import LumaSettings
from .scope import entity_area_id, resolve_learning_scope

if TYPE_CHECKING:
    from .engine import SimulationEngine

_LOGGER = logging.getLogger(__name__)

_PRESENCE_UNKNOWN = (STATE_UNKNOWN, STATE_UNAVAILABLE)
_REGISTRY_EVENTS = (
    er.EVENT_ENTITY_REGISTRY_UPDATED,
    dr.EVENT_DEVICE_REGISTRY_UPDATED,
    ar.EVENT_AREA_REGISTRY_UPDATED,
    fr.EVENT_FLOOR_REGISTRY_UPDATED,
)


def _parse(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


def parse_event(record: list[Any]) -> tuple[str, str, int, str | None]:
    """Read a stored event: ``[entity_id, state, seconds]``, plus the area since v0.2.0."""
    area_id = record[3] if len(record) > 3 else None
    return record[0], record[1], record[2], area_id


class LearningGate:
    """Decides when house activity is real and stores it per local day."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict[str, Any],
        settings: LumaSettings,
        engine: SimulationEngine,
    ) -> None:
        """Initialize the learning gate."""
        self.hass = hass
        self._entry_id = entry_id
        self._settings = settings
        self._engine = engine
        self._config = config
        self.entities: list[str] = []
        # Indoor lights left out automatically: {entity_id: reason}
        self.auto_excluded: dict[str, str] = {}
        self._persons: list[str] = list(config.get(CONF_PERSONS, []))
        self._alarm: str | None = config.get(CONF_ALARM) or None
        # With alarm control, armed vacation is an absence too.
        self._vacation = bool(
            config.get(CONF_ALARM_CONTROL, DEFAULT_ALARM_CONTROL)
        ) and AlarmControlPanelState.ARMED_VACATION in config.get(
            CONF_ALARM_ACTIVATE_STATES, DEFAULT_ALARM_ACTIVATE_STATES
        )
        self._margin_before = timedelta(
            hours=float(config.get(CONF_MARGIN_BEFORE, DEFAULT_MARGIN_BEFORE))
        )
        self._margin_after = timedelta(
            hours=float(config.get(CONF_MARGIN_AFTER, DEFAULT_MARGIN_AFTER))
        )
        self._human_only = bool(config.get(CONF_HUMAN_ONLY, DEFAULT_HUMAN_ONLY))

        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.learning"
        )
        # {"YYYY-MM-DD": {"exposure": [[s, e], ...],
        #                 "events": [[entity, state, s, area], ...]}}
        # Events stored before v0.2.0 have no area; read them with parse_event().
        self._days: dict[str, dict[str, list[Any]]] = {}
        self._absence_since: datetime | None = None
        self._blocked_until: datetime | None = None
        self._segment_start: datetime | None = None
        self._pending: list[tuple[str, str, datetime, str | None]] = []

        self.reasons: set[str] = set()
        self._started = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_lights: CALLBACK_TYPE | None = None
        self._unsub_margin: CALLBACK_TYPE | None = None
        self._scope_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=SCOPE_DEBOUNCE_S,
            immediate=False,
            function=self._async_refresh_scope,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        """Load stored statistics and gate state."""
        data = await self._store.async_load()
        if not data:
            return
        self._days = data.get("days", {})
        gate = data.get("gate", {})
        self._absence_since = _parse(gate.get("absence_since"))
        self._blocked_until = _parse(gate.get("blocked_until"))

    async def async_start(self) -> None:
        """Start listening once Home Assistant is running."""
        self._async_refresh_scope()
        self._unsubs.extend(
            self.hass.bus.async_listen(event_type, self._handle_registry_update)
            for event_type in _REGISTRY_EVENTS
        )
        watched = [*self._persons, *([self._alarm] if self._alarm else [])]
        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, self._handle_presence)
            )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._handle_tick, COMMIT_INTERVAL)
        )
        self._unsubs.append(self._engine.add_enabled_listener(self.async_evaluate))
        self._started = True
        self.async_evaluate()

    async def async_shutdown(self) -> None:
        """Commit what we have and stop listening."""
        if self._started and self._segment_start is not None:
            self._commit_until(dt_util.utcnow())
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._scope_debouncer.async_shutdown()
        self._untrack_lights()
        self._cancel_margin_timer()
        self._started = False
        await self._store.async_save(self._data_to_save())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """Return True while activity is being learned."""
        return self._segment_start is not None

    @property
    def days(self) -> dict[str, dict[str, list[Any]]]:
        """Learned days: {"YYYY-MM-DD": {"exposure": [...], "events": [...]}}."""
        return self._days

    @property
    def blocked_until(self) -> datetime | None:
        """End of the current return margin, if any."""
        return self._blocked_until

    def stats(self) -> dict[str, Any]:
        """Summary of the stored learning data."""
        today = dt_util.now().date()
        valid = partial = events = 0
        first: date | None = None
        for key, day in self._days.items():
            day_date = date.fromisoformat(key)
            first = day_date if first is None else min(first, day_date)
            events += len(day["events"])
            if day_date >= today:
                continue  # today is still incomplete
            exposure = sum(end - start for start, end in day["exposure"])
            if exposure >= VALID_DAY_MIN_EXPOSURE_S:
                valid += 1
            else:
                partial += 1
        span = (today - first).days if first else 0
        return {
            "valid_days": valid,
            "partial_days": partial,
            "excluded_days": max(span - valid, 0),
            "events": events,
            "first_day": first.isoformat() if first else None,
        }

    @callback
    def async_forget(self, start: date, end: date) -> int:
        """Delete the learned data of a date range (inclusive)."""
        if end < start:
            start, end = end, start
        removed = [k for k in self._days if start <= date.fromisoformat(k) <= end]
        for key in removed:
            del self._days[key]
        pending_from = dt_util.as_local(dt_util.utcnow() - self._margin_before).date()
        if self._segment_start is not None and end >= pending_from:
            self._pending.clear()
            self._segment_start = dt_util.utcnow()
        self._save()
        self._notify()
        return len(removed)

    @callback
    def async_evaluate(self) -> None:
        """Re-evaluate whether the gate should be open."""
        if not self._started:
            return
        now = dt_util.utcnow()
        reasons: set[str] = set()
        if self._engine.enabled:
            reasons.add(REASON_SIMULATION)
        alarm = self.hass.states.get(self._alarm) if self._alarm else None
        if alarm is not None and alarm.state == AlarmControlPanelState.ARMED_AWAY:
            reasons.add(REASON_ALARM)
        if (
            self._vacation
            and alarm is not None
            and alarm.state == AlarmControlPanelState.ARMED_VACATION
        ):
            reasons.add(REASON_ALARM_VACATION)
        if self._persons and self._everybody_away():
            reasons.add(REASON_AWAY)
        if self._settings.learning_paused:
            reasons.add(REASON_PAUSED)
        if (
            reasons & ABSENCE_REASONS == {REASON_SIMULATION}
            and self._absence_since is None
            and self._somebody_home()
        ):
            # A test with people at home: pause learning, it is not an absence.
            reasons = (reasons - {REASON_SIMULATION}) | {REASON_SIMULATION_TEST}

        if reasons & ABSENCE_REASONS:
            if self._absence_since is None:
                self._absence_since = now
            if self._segment_start is not None:
                # Drop the last hours before the departure.
                self._pending.clear()
                self._segment_start = None
        elif reasons:
            # Manual pause or test: keep what was observed until now.
            if self._segment_start is not None:
                self._commit_until(now)
                self._segment_start = None
        else:
            if self._absence_since is not None:
                blocked = now + self._margin_after
                if now - self._absence_since >= LONG_ABSENCE:
                    end_of_today = dt_util.start_of_local_day(
                        dt_util.now().date() + timedelta(days=1)
                    )
                    blocked = max(blocked, dt_util.as_utc(end_of_today))
                self._blocked_until = blocked
                self._absence_since = None
            if self._blocked_until is not None and now < self._blocked_until:
                reasons.add(REASON_MARGIN)
                self._schedule_margin_timer(self._blocked_until)
            else:
                self._blocked_until = None
                if self._segment_start is None:
                    self._segment_start = now

        self.reasons = reasons
        self._save()
        self._notify()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    @callback
    def _handle_light(self, event: Event[EventStateChangedData]) -> None:
        if self._segment_start is None:
            return
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or old.state == new.state:
            return
        if old.state not in (STATE_ON, STATE_OFF) or new.state not in (STATE_ON, STATE_OFF):
            return
        context = new.context
        if self._engine.is_own_context(context.id):
            return
        if self._human_only and context.parent_id is not None:
            return  # triggered by an automation or script
        entity_id = event.data["entity_id"]
        self._pending.append(
            (entity_id, new.state, new.last_changed, entity_area_id(self.hass, entity_id))
        )

    @callback
    def _handle_registry_update(self, _event: Event) -> None:
        self._scope_debouncer.async_schedule_call()

    @callback
    def _handle_presence(self, _event: Event[EventStateChangedData]) -> None:
        self.async_evaluate()

    @callback
    def _handle_tick(self, now: datetime) -> None:
        if self._segment_start is not None:
            self._commit_until(now - self._margin_before)

    @callback
    def _handle_margin_end(self, _now: datetime) -> None:
        self._unsub_margin = None
        self.async_evaluate()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @callback
    def _async_refresh_scope(self) -> None:
        """Resolve the learning scope and follow its entities."""
        scope = resolve_learning_scope(self.hass, self._config)
        self.auto_excluded = scope.reasons
        if scope.entities != self.entities or self._unsub_lights is None:
            self.entities = scope.entities
            self._untrack_lights()
            if self.entities:
                self._unsub_lights = async_track_state_change_event(
                    self.hass, self.entities, self._handle_light
                )
        self._notify()

    def _untrack_lights(self) -> None:
        if self._unsub_lights is not None:
            self._unsub_lights()
            self._unsub_lights = None

    def _somebody_home(self) -> bool:
        return any(
            (state := self.hass.states.get(person)) is not None
            and state.state == STATE_HOME
            for person in self._persons
        )

    def _everybody_away(self) -> bool:
        for person in self._persons:
            state = self.hass.states.get(person)
            if state is None or state.state in _PRESENCE_UNKNOWN:
                return False  # unknown presence never closes the gate
            if state.state == STATE_HOME:
                return False
        return True

    def _schedule_margin_timer(self, when: datetime) -> None:
        self._cancel_margin_timer()
        self._unsub_margin = async_track_point_in_utc_time(
            self.hass, self._handle_margin_end, when
        )

    def _cancel_margin_timer(self) -> None:
        if self._unsub_margin is not None:
            self._unsub_margin()
            self._unsub_margin = None

    def _commit_until(self, cutoff: datetime) -> None:
        if self._segment_start is None or cutoff <= self._segment_start:
            return
        self._add_exposure(self._segment_start, cutoff)
        keep: list[tuple[str, str, datetime, str | None]] = []
        for pending in self._pending:
            if pending[2] < cutoff:
                self._add_event(*pending)
            else:
                keep.append(pending)
        self._pending = keep
        self._segment_start = cutoff
        self._save()
        self._notify()

    def _day(self, day: date) -> dict[str, list[Any]]:
        key = day.isoformat()
        if key not in self._days:
            self._days[key] = {"exposure": [], "events": []}
            if len(self._days) > STORE_MAX_DAYS:
                for old in sorted(self._days)[: len(self._days) - STORE_MAX_DAYS]:
                    del self._days[old]
        return self._days[key]

    def _add_exposure(self, start: datetime, end: datetime) -> None:
        """Store an observed interval, split at local midnights."""
        current = start
        while current < end:
            local_day = dt_util.as_local(current).date()
            day_start = dt_util.as_utc(dt_util.start_of_local_day(local_day))
            next_day = dt_util.as_utc(
                dt_util.start_of_local_day(local_day + timedelta(days=1))
            )
            segment_end = min(end, next_day)
            begin_s = int((current - day_start).total_seconds())
            end_s = int((segment_end - day_start).total_seconds())
            exposure = self._day(local_day)["exposure"]
            if exposure and exposure[-1][1] == begin_s:
                exposure[-1][1] = end_s
            else:
                exposure.append([begin_s, end_s])
            current = segment_end

    def _add_event(
        self, entity_id: str, state: str, when: datetime, area_id: str | None
    ) -> None:
        local = dt_util.as_local(when)
        day_start = dt_util.start_of_local_day(local.date())
        seconds = int((local - day_start).total_seconds())
        self._day(local.date())["events"].append([entity_id, state, seconds, area_id])

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "days": self._days,
            "gate": {
                "absence_since": self._absence_since.isoformat()
                if self._absence_since
                else None,
                "blocked_until": self._blocked_until.isoformat()
                if self._blocked_until
                else None,
            },
        }

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 60)

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self._entry_id))
