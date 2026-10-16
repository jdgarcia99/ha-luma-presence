"""Context: holidays, weather and darkness.

* Workdays come from the Workday integration (``workday.check_date``, cached
  per date) when a workday sensor is set; otherwise holidays come from an
  optional calendar: any all-day event, or any event covering local 12:00,
  makes that date a holiday. They decide the night type (``plan.day_type``)
  for tonight and for learned nights, and the morning type. Calendar events
  are fetched once per range and cached for the day; if neither can be read,
  the plain weekday rule is used.
* Bad weather is read from an optional weather entity when a window starts.
* Darkness: an optional illuminance sensor moves the evening window start
  to the moment it has stayed below the threshold for 5 minutes, clamped
  around sunset, ends the wake-up window when it has been light for 5
  minutes, and tells the daylight guard when it is day. Once decided, a
  start or end is kept for that evening or morning.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
    BAD_WEATHER_CONDITIONS,
    COLD_BELOW_C,
    CONF_BAD_WEATHER_BOOST,
    CONF_DARKNESS_EARLIEST,
    CONF_DARKNESS_LATEST,
    CONF_DARKNESS_SENSOR,
    CONF_DARKNESS_THRESHOLD,
    CONF_HOLIDAY_CALENDAR,
    CONF_WEATHER_ENTITY,
    CONF_WORKDAY_SENSOR,
    DARKNESS_CONFIRM,
    DEFAULT_BAD_WEATHER_BOOST,
    DEFAULT_DARKNESS_EARLIEST,
    DEFAULT_DARKNESS_LATEST,
    DEFAULT_DARKNESS_THRESHOLD,
    NIGHT_TYPE_CALENDAR,
    NIGHT_TYPE_CALENDAR_UNAVAILABLE,
    NIGHT_TYPE_RULE,
    NIGHT_TYPE_WORKDAY,
    NIGHT_TYPE_WORKDAY_UNAVAILABLE,
    START_CLAMPED_EARLIEST,
    START_CLAMPED_LATEST,
    START_DARKNESS,
    START_SENSOR_UNAVAILABLE,
)
from .plan import day_type, is_workday

_LOGGER = logging.getLogger(__name__)

_UNKNOWN = (None, STATE_UNAVAILABLE, STATE_UNKNOWN)
NOON = time(12, 0)


def holidays_from_events(events: list[Mapping[str, Any]]) -> set[date]:
    """Dates that are holidays: all-day events, or events covering local 12:00."""
    holidays: set[date] = set()
    for event in events:
        start, end = str(event.get("start", "")), str(event.get("end", ""))
        if not start:
            continue
        if "T" not in start and " " not in start:
            # All-day event: [start, end) dates.
            first = date.fromisoformat(start)
            last = date.fromisoformat(end) if end else first + timedelta(days=1)
            day = first
            while day < max(last, first + timedelta(days=1)):
                holidays.add(day)
                day += timedelta(days=1)
            continue
        began = dt_util.parse_datetime(start)
        ended = dt_util.parse_datetime(end) if end else None
        if began is None or ended is None:
            continue
        began, ended = dt_util.as_local(began), dt_util.as_local(ended)
        day = began.date()
        while day <= ended.date():
            noon = datetime.combine(day, NOON, tzinfo=dt_util.get_default_time_zone())
            if began <= noon < ended:
                holidays.add(day)
            day += timedelta(days=1)
    return holidays


class ContextProvider:
    """Holidays, weather and darkness for one LumaPresence entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: Mapping[str, Any],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the context provider."""
        self.hass = hass
        # Workday is preferred over the calendar.
        self._workday: str | None = config.get(CONF_WORKDAY_SENSOR) or None
        self._calendar: str | None = (
            None if self._workday else config.get(CONF_HOLIDAY_CALENDAR) or None
        )
        self._workdays: dict[date, bool] = {}
        self.workday_ok = True
        self._weather: str | None = config.get(CONF_WEATHER_ENTITY) or None
        self.boost = float(config.get(CONF_BAD_WEATHER_BOOST, DEFAULT_BAD_WEATHER_BOOST)) / 100
        self._on_change = on_change
        self._unsub_noon: CALLBACK_TYPE | None = None
        self._holidays: set[date] = set()
        self._range: tuple[date, date] | None = None
        self._fetched_on: date | None = None
        self.calendar_ok = True
        sensor = config.get(CONF_DARKNESS_SENSOR) or None
        self.darkness = (
            DarknessTracker(
                hass,
                sensor,
                float(config.get(CONF_DARKNESS_THRESHOLD, DEFAULT_DARKNESS_THRESHOLD)),
                timedelta(minutes=float(config.get(CONF_DARKNESS_EARLIEST, DEFAULT_DARKNESS_EARLIEST))),
                timedelta(minutes=float(config.get(CONF_DARKNESS_LATEST, DEFAULT_DARKNESS_LATEST))),
                on_change,
            )
            if sensor
            else None
        )

    @callback
    def async_start(self) -> None:
        """Follow the darkness sensor; read the holidays now and every day."""
        if self.darkness is not None:
            self.darkness.async_start()
        if self._calendar is not None or self._workday is not None:
            self._refresh_holidays()
            self._unsub_noon = async_track_time_change(
                self.hass, self._handle_noon, hour=12, minute=5, second=0
            )

    @callback
    def async_shutdown(self) -> None:
        """Stop following the darkness sensor and the calendar."""
        if self.darkness is not None:
            self.darkness.async_shutdown()
        if self._unsub_noon is not None:
            self._unsub_noon()
            self._unsub_noon = None

    @callback
    def _handle_noon(self, _now: datetime) -> None:
        self._refresh_holidays()

    @callback
    def _refresh_holidays(self) -> None:
        async def _refresh() -> None:
            today = dt_util.now().date()
            await self.async_ensure_holidays(today - timedelta(days=30), today + timedelta(days=2))
            if self._on_change is not None:
                self._on_change()  # morning windows depend on holidays

        self.hass.async_create_background_task(_refresh(), "luma_presence_holidays")

    # ------------------------------------------------------------------
    # Holidays and night type
    # ------------------------------------------------------------------
    @property
    def night_type_source(self) -> str:
        """workday / calendar / weekday_rule, or *_unavailable."""
        if self._workday is not None:
            return NIGHT_TYPE_WORKDAY if self.workday_ok else NIGHT_TYPE_WORKDAY_UNAVAILABLE
        if self._calendar is None:
            return NIGHT_TYPE_RULE
        return NIGHT_TYPE_CALENDAR if self.calendar_ok else NIGHT_TYPE_CALENDAR_UNAVAILABLE

    @property
    def workdays(self) -> dict[date, bool] | None:
        """Dates classified by Workday, or None when it is not usable."""
        return self._workdays if self._workday and self.workday_ok else None

    def daylight(self) -> bool:
        """True when the illuminance sensor says it is day (daylight guard)."""
        return self.darkness is not None and self.darkness.daylight()

    @property
    def holidays(self) -> set[date] | None:
        """Known holidays, or None when there is no usable calendar."""
        return self._holidays if self._calendar and self.calendar_ok else None

    def night_type(self, night: date) -> str:
        """Type of the night starting on a date (workdays and holidays included)."""
        return day_type(night, self.holidays, self.workdays)

    def is_workday(self, day: date) -> bool:
        """Whether a date is a workday (the type of its morning)."""
        return is_workday(day, self.holidays, self.workdays)

    async def async_ensure_holidays(self, first: date, last: date) -> None:
        """Know the workdays or holidays of [first, last]."""
        if self._workday is not None:
            await self._async_ensure_workdays(first, last)
            return
        if self._calendar is None:
            return
        today = dt_util.now().date()
        if (
            self._fetched_on == today
            and self._range is not None
            and self._range[0] <= first
            and last <= self._range[1]
        ):
            return
        if self._range is not None and self._fetched_on == today:
            first, last = min(first, self._range[0]), max(last, self._range[1])
        state = self.hass.states.get(self._calendar)
        if state is None or state.state in _UNKNOWN:
            self._unavailable("calendar entity unavailable")
            return
        tz = dt_util.get_default_time_zone()
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": self._calendar,
                    "start_date_time": datetime.combine(first, time(0), tzinfo=tz).isoformat(),
                    "end_date_time": datetime.combine(
                        last + timedelta(days=1), time(0), tzinfo=tz
                    ).isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            self._unavailable(str(err))
            return
        events = (response or {}).get(self._calendar, {}).get("events", [])
        self._holidays = holidays_from_events(events)
        self._range = (first, last)
        self._fetched_on = today
        self.calendar_ok = True

    async def _async_ensure_workdays(self, first: date, last: date) -> None:
        """Ask Workday about each date not cached yet (classification is per date)."""
        state = self.hass.states.get(str(self._workday))
        if state is None or state.state in _UNKNOWN:
            self._workday_unavailable("workday sensor unavailable")
            return
        missing = []
        day = first
        while day <= last:
            if day not in self._workdays:
                missing.append(day)
            day += timedelta(days=1)
        if not missing:
            self.workday_ok = True
            return

        async def _check(day: date) -> tuple[date, bool]:
            response = await self.hass.services.async_call(
                "workday",
                "check_date",
                {"entity_id": self._workday, "check_date": day.isoformat()},
                blocking=True,
                return_response=True,
            )
            return day, bool((response or {})[str(self._workday)]["workday"])

        try:
            results = await asyncio.gather(*(_check(day) for day in missing))
        except (HomeAssistantError, KeyError, TypeError, ValueError) as err:
            self._workday_unavailable(str(err))
            return
        self._workdays.update(results)
        self.workday_ok = True

    def _workday_unavailable(self, why: str) -> None:
        if self.workday_ok:
            _LOGGER.warning("Workday sensor %s not usable (%s)", self._workday, why)
        self.workday_ok = False

    def _unavailable(self, why: str) -> None:
        if self.calendar_ok:
            _LOGGER.warning("Holiday calendar %s not usable (%s)", self._calendar, why)
        self.calendar_ok = False
        self._fetched_on = None

    # ------------------------------------------------------------------
    # Weather
    # ------------------------------------------------------------------
    def weather(self) -> dict[str, Any]:
        """Current condition and whether it counts as bad weather."""
        if self._weather is None:
            return {"condition": None, "bad": False}
        state = self.hass.states.get(self._weather)
        if state is None or state.state in _UNKNOWN:
            return {"condition": None, "bad": False}
        temperature: float | None = None
        with suppress(TypeError, ValueError):
            temperature = float(state.attributes.get(ATTR_TEMPERATURE))
            if state.attributes.get("temperature_unit") == UnitOfTemperature.FAHRENHEIT:
                temperature = (temperature - 32) * 5 / 9
        bad = state.state in BAD_WEATHER_CONDITIONS or (
            temperature is not None and temperature < COLD_BELOW_C
        )
        return {"condition": state.state, "temperature": temperature, "bad": bad}


class DarknessTracker:
    """Decides when evenings start and mornings end from an illuminance sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        sensor: str,
        threshold: float,
        earliest: timedelta,
        latest: timedelta,
        on_change: Callable[[], None] | None,
    ) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.sensor = sensor
        self._threshold = threshold
        self._earliest = earliest
        self._latest = latest
        self._on_change = on_change
        self._below_since: datetime | None = None
        self._above_since: datetime | None = None
        self._available = False
        self._evenings: dict[date, tuple[datetime, str]] = {}
        self._mornings: dict[date, datetime] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_timer: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Read the current value and follow changes."""
        self._update(self.hass.states.get(self.sensor), dt_util.utcnow())
        self._unsubs.append(
            async_track_state_change_event(self.hass, [self.sensor], self._handle_state)
        )

    @callback
    def async_shutdown(self) -> None:
        """Stop following the sensor."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @property
    def available(self) -> bool:
        """True while the sensor reports a number."""
        return self._available

    @callback
    def _handle_state(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        self._update(new, new.last_changed if new is not None else dt_util.utcnow())
        if self._on_change is not None:
            self._on_change()

    @callback
    def _update(self, state: Any, when: datetime) -> None:
        value: float | None = None
        if state is not None and state.state not in _UNKNOWN:
            with suppress(ValueError):
                value = float(state.state)
        self._available = value is not None
        if value is None:
            self._below_since = self._above_since = None
        elif value < self._threshold:
            self._above_since = None
            if self._below_since is None:
                self._below_since = when
                self._wake_at(when + DARKNESS_CONFIRM)
        else:
            self._below_since = None
            if self._above_since is None:
                self._above_since = when
                self._wake_at(when + DARKNESS_CONFIRM)

    @callback
    def _wake_at(self, when: datetime) -> None:
        """Re-evaluate the windows once a change has lasted 5 minutes."""
        if self._unsub_timer is not None:
            self._unsub_timer()

        @callback
        def _fire(_now: datetime) -> None:
            self._unsub_timer = None
            if self._on_change is not None:
                self._on_change()

        self._unsub_timer = async_track_point_in_utc_time(self.hass, _fire, when)

    def evening_start(self, day: date, sunset: datetime) -> tuple[datetime, str]:
        """Start of the evening window of a day (UTC), and why.

        Before darkness is confirmed the latest allowed time is returned
        provisionally, so the engine waits for it; darkness moves it earlier.
        """
        if day in self._evenings:
            return self._evenings[day]
        lo, hi = sunset + self._earliest, sunset + self._latest
        now = dt_util.utcnow()
        if not self._available:
            return sunset, START_SENSOR_UNAVAILABLE
        # Only a darkness that began around this evening counts (not last
        # night's, nor today's for tomorrow's evening).
        if self._below_since is not None and self._below_since >= lo - timedelta(hours=12):
            confirmed = self._below_since + DARKNESS_CONFIRM
            start = max(confirmed, lo)
            if start <= hi:
                reason = START_DARKNESS if confirmed >= lo else START_CLAMPED_EARLIEST
                if now >= start:
                    self._evenings[day] = (start, reason)
                return start, reason
        if now >= hi:
            self._evenings[day] = (hi, START_CLAMPED_LATEST)
        return hi, START_CLAMPED_LATEST

    def daylight(self) -> bool:
        """True once it has stayed above the threshold for 5 minutes."""
        return (
            self._available
            and self._above_since is not None
            and dt_util.utcnow() >= self._above_since + DARKNESS_CONFIRM
        )

    def morning_end(self, day: date, start: datetime, end: datetime) -> datetime:
        """End of a wake-up window: when it has been light for 5 minutes,
        at the latest `end` (sunrise plus the allowed extension)."""
        if day in self._mornings:
            return self._mornings[day]
        if (
            self._available
            and self._above_since is not None
            and self._above_since >= start - timedelta(hours=12)
        ):
            light = max(self._above_since + DARKNESS_CONFIRM, start)
            if light < end:
                if dt_util.utcnow() >= light:
                    self._mornings[day] = light
                return light
        return end

    def prune(self, before: date) -> None:
        """Forget decisions about old days."""
        for store in (self._evenings, self._mornings):
            for day in [d for d in store if d < before]:
                del store[day]
