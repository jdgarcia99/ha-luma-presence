"""Constants for LumaPresence."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "luma_presence"
DEFAULT_NAME: Final = "LumaPresence"

# Configuration (stored in the config entry options)
CONF_LIGHTS: Final = "lights"
CONF_FRONT_LIGHTS: Final = "front_lights"
CONF_REAR_LIGHTS: Final = "rear_lights"
CONF_PERSONS: Final = "persons"
CONF_ALARM: Final = "alarm_entity"
CONF_MARGIN_BEFORE: Final = "margin_before_hours"
CONF_MARGIN_AFTER: Final = "margin_after_hours"
CONF_HUMAN_ONLY: Final = "learn_only_human"
CONF_LEARN_FLOORS: Final = "learn_floors"
CONF_LEARN_AREAS: Final = "learn_areas"
CONF_LEARN_EXTRA: Final = "learn_extra"
CONF_LEARN_EXCLUDE: Final = "learn_exclude"
CONF_AUTO_EXCLUDE_INDICATORS: Final = "learn_auto_exclude_indicators"
CONF_UNAVAILABLE_ALERT_MINUTES: Final = "unavailable_alert_minutes"

CONF_STEP_DELAY_MIN: Final = "step_delay_min"
CONF_STEP_DELAY_MAX: Final = "step_delay_max"
CONF_DWELL_MIN: Final = "dwell_min"
CONF_DWELL_MAX: Final = "dwell_max"

DEFAULT_MARGIN_BEFORE: Final = 2.0
DEFAULT_MARGIN_AFTER: Final = 2.0
DEFAULT_HUMAN_ONLY: Final = True
DEFAULT_STEP_DELAY_MIN: Final = 10.0
DEFAULT_STEP_DELAY_MAX: Final = 30.0
DEFAULT_DWELL_MIN: Final = 2.0
DEFAULT_DWELL_MAX: Final = 5.0
DEFAULT_AUTO_EXCLUDE_INDICATORS: Final = True
DEFAULT_UNAVAILABLE_ALERT_MINUTES: Final = 60

# Why the learning scope left out an indoor light
EXCLUDED_DISABLED: Final = "disabled"
EXCLUDED_HIDDEN: Final = "hidden"
EXCLUDED_CATEGORY: Final = "entity_category"
EXCLUDED_INDICATOR: Final = "indicator_led"

# Night routes (config subentries)
SUBENTRY_ROUTE: Final = "route"
CONF_ORIGIN: Final = "origin"
CONF_PATH: Final = "path"
CONF_DESTINATION: Final = "destination"

# Which part of LumaPresence turned a light on
OWNER_SIMULATION: Final = "simulation"
OWNER_SEQUENCE: Final = "sequence"

# Steps of a running route
STEP_ORIGIN: Final = "origin"
STEP_PATH: Final = "path"
STEP_DESTINATION: Final = "destination"
STEP_DWELL: Final = "dwell"
STEP_RETURN: Final = "return"

# Simulation modes (Dream arrives in a later phase)
MODE_SUN: Final = "sun"
MODE_TIME: Final = "time"
MODE_REPLAY: Final = "replay"
MODES: Final = [MODE_SUN, MODE_TIME, MODE_REPLAY]

# Why Replay picked its source night, or why it fell back to Sun mode
REPLAY_SAME_WEEKDAY: Final = "same_weekday"
REPLAY_SAME_DAY_TYPE: Final = "same_day_type"
REPLAY_MOST_RECENT: Final = "most_recent"
REPLAY_NO_DATA: Final = "fallback_no_data"
REPLAY_NOT_ENOUGH_EVENTS: Final = "fallback_not_enough_events"
REPLAY_NOT_ENOUGH_EXPOSURE: Final = "fallback_not_enough_exposure"
REPLAY_REASONS: Final = [
    REPLAY_SAME_WEEKDAY,
    REPLAY_SAME_DAY_TYPE,
    REPLAY_MOST_RECENT,
    REPLAY_NO_DATA,
    REPLAY_NOT_ENOUGH_EVENTS,
    REPLAY_NOT_ENOUGH_EXPOSURE,
]
REPLAY_MIN_COVERAGE: Final = 0.8
REPLAY_SAME_WEEKDAY_DAYS: Final = 14
REPLAY_SAME_DAY_TYPE_DAYS: Final = 28
REPLAY_MIN_GAP_S: Final = 60

# What to do with the lights the simulation turned on when it is stopped
STOP_KEEP_ON: Final = "keep_on"
STOP_OFF_NOW: Final = "off_now"
STOP_OFF_GRADUAL: Final = "off_gradual"
STOP_KEEP_IF_NIGHT: Final = "keep_if_night"
STOP_BEHAVIORS: Final = [STOP_KEEP_ON, STOP_OFF_NOW, STOP_OFF_GRADUAL, STOP_KEEP_IF_NIGHT]

# Engine status
STATUS_IDLE: Final = "idle"
STATUS_WAITING: Final = "waiting"
STATUS_SIMULATING: Final = "simulating"
STATUS_STOPPING: Final = "stopping"
STATUSES: Final = [STATUS_IDLE, STATUS_WAITING, STATUS_SIMULATING, STATUS_STOPPING]

EVENT_START: Final = "start"
EVENT_END: Final = "end"

EVENT_ACTION: Final = f"{DOMAIN}_action"
EVENT_LIGHT_UNAVAILABLE: Final = f"{DOMAIN}_light_unavailable"
EVENT_LIGHT_AVAILABLE: Final = f"{DOMAIN}_light_available"

MAX_START_JITTER_MIN: Final = 30
MAX_END_JITTER_MIN: Final = 30
OWN_CONTEXT_MAXLEN: Final = 1000

# Learning gate
REASON_SIMULATION: Final = "simulation_enabled"
REASON_ALARM: Final = "alarm_armed_away"
REASON_AWAY: Final = "nobody_home"
REASON_PAUSED: Final = "paused"
REASON_MARGIN: Final = "return_margin"
# Simulation turned on with someone at home: a pause, not an absence.
REASON_SIMULATION_TEST: Final = "simulation_test"
ABSENCE_REASONS: Final = frozenset({REASON_SIMULATION, REASON_ALARM, REASON_AWAY})

LONG_ABSENCE: Final = timedelta(hours=48)
COMMIT_INTERVAL: Final = timedelta(minutes=15)
VALID_DAY_MIN_EXPOSURE_S: Final = 16 * 3600
STORE_VERSION: Final = 1
STORE_MAX_DAYS: Final = 400
SCOPE_DEBOUNCE_S: Final = 5.0
PREVIEW_MAX_ENTITIES: Final = 60

# Services
SERVICE_START: Final = "start"
SERVICE_STOP: Final = "stop"
SERVICE_FORGET_PERIOD: Final = "forget_period"
SERVICE_RUN_SEQUENCE: Final = "run_sequence"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_ROUTE: Final = "route"
ATTR_LIGHTS_BEHAVIOR: Final = "lights_behavior"
ATTR_START_DATE: Final = "start_date"
ATTR_END_DATE: Final = "end_date"


def signal_update(entry_id: str) -> str:
    """Dispatcher signal used to refresh the entities of one entry."""
    return f"{DOMAIN}_update_{entry_id}"
