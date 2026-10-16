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
CONF_ALARM_CONTROL: Final = "alarm_control"
CONF_ALARM_ACTIVATE_STATES: Final = "alarm_activate_states"

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
DEFAULT_ALARM_CONTROL: Final = False

# Alarm states that may turn the simulation on
ALARM_ACTIVATE_CHOICES: Final = [
    "armed_away",
    "armed_vacation",
    "armed_home",
    "armed_night",
    "armed_custom_bypass",
]
DEFAULT_ALARM_ACTIVATE_STATES: Final = ["armed_away", "armed_vacation"]

# Who turned the master switch on or off
ORIGIN_ALARM: Final = "alarm"
ORIGIN_MANUAL: Final = "manual"
ORIGIN_SERVICE: Final = "service"
ORIGIN_RESTORE: Final = "restore"
ORIGINS: Final = [ORIGIN_ALARM, ORIGIN_MANUAL, ORIGIN_SERVICE, ORIGIN_RESTORE]
# Only in events: the window ended, or a setting moved it.
ORIGIN_SCHEDULE: Final = "schedule"

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

# Deterrence triggers (config subentries, v0.8.0)
SUBENTRY_DETERRENCE: Final = "deterrence_trigger"
CONF_TRIGGER_ENTITY: Final = "trigger_entity"
CONF_FACADE: Final = "facade"
# Event entities only: react to these event types (empty: any event).
CONF_EVENT_TYPES: Final = "event_types"
FACADE_FRONT: Final = "front"
FACADE_REAR: Final = "rear"
FACADES: Final = [FACADE_FRONT, FACADE_REAR]
TRIGGER_DEVICE_CLASSES: Final = ["occupancy", "motion", "presence"]

# Which part of LumaPresence turned a light on (or moved a cover)
OWNER_SIMULATION: Final = "simulation"
OWNER_SEQUENCE: Final = "sequence"
OWNER_DETERRENCE: Final = "deterrence"
OWNER_COVERS: Final = "covers"
OWNER_AMBIENCE: Final = "ambience"

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
MODE_DREAM: Final = "dream"
MODES: Final = [MODE_SUN, MODE_TIME, MODE_REPLAY, MODE_DREAM]

# Dream mode
CONF_DREAM_CANDIDATES: Final = "dream_candidates"
CONF_DREAM_MAX_SIMILARITY: Final = "dream_max_similarity"
CONF_REPORT_NOTIFY: Final = "report_notify"
DEFAULT_DREAM_CANDIDATES: Final = 20
DEFAULT_DREAM_MAX_SIMILARITY: Final = 0.7

# Context (v0.7.0): darkness, morning window, holidays, weather
CONF_DARKNESS_SENSOR: Final = "darkness_sensor"
CONF_DARKNESS_THRESHOLD: Final = "darkness_threshold_lx"
CONF_DARKNESS_EARLIEST: Final = "darkness_earliest_min"
CONF_DARKNESS_LATEST: Final = "darkness_latest_min"
CONF_MORNING_ENABLED: Final = "morning_enabled"
CONF_MORNING_LEARNED_ENABLED: Final = "morning_learned_enabled"
CONF_MORNING_START: Final = "morning_start"
CONF_MORNING_LATEST: Final = "morning_latest_min"
CONF_MORNING_NONWORKDAY_MODE: Final = "morning_nonworkday_mode"
CONF_MORNING_NONWORKDAY_START: Final = "morning_nonworkday_start"
# Replaced by the wake-up options in config entry version 3.
CONF_MORNING_END: Final = "morning_end"
CONF_MORNING_WORKDAYS_ONLY: Final = "morning_workdays_only"
CONF_WORKDAY_SENSOR: Final = "workday_sensor"
CONF_HOLIDAY_CALENDAR: Final = "holiday_calendar"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_BAD_WEATHER_BOOST: Final = "bad_weather_boost"
CONF_VERIFY_TIMEOUT: Final = "verify_timeout"
DEFAULT_DARKNESS_THRESHOLD: Final = 50.0
DEFAULT_DARKNESS_EARLIEST: Final = -60
DEFAULT_DARKNESS_LATEST: Final = 30
DEFAULT_MORNING_ENABLED: Final = False
DEFAULT_MORNING_LEARNED_ENABLED: Final = True
DEFAULT_MORNING_START: Final = "06:30:00"
DEFAULT_MORNING_LATEST: Final = 30
# Wake-up on non-workdays in Sun and Time modes (v0.7.1).
NONWORKDAY_LATER: Final = "later"
NONWORKDAY_SKIP: Final = "skip"
NONWORKDAY_MODES: Final = [NONWORKDAY_LATER, NONWORKDAY_SKIP]
DEFAULT_MORNING_NONWORKDAY_MODE: Final = NONWORKDAY_LATER
DEFAULT_MORNING_NONWORKDAY_START: Final = "08:30:00"
# Which rule decided the wake-up start (context sensor).
MORNING_RULE_WORKDAY: Final = "workday"
MORNING_RULE_NONWORKDAY_LATER: Final = "nonworkday_later"
MORNING_RULE_NONWORKDAY_SKIP: Final = "nonworkday_skip"
MORNING_RULE_LEARNED: Final = "learned"
WAKE_DELAY_MAX_MIN: Final = 10
# The daylight guard switches the lights off within this many seconds.
DAYLIGHT_OFF_MAX_S: Final = 180
# Clock axis of the learned mornings (Dream), and the learned first switch-on.
MORNING_AXIS_START_H: Final = 5
MORNING_AXIS_MIN: Final = 5 * 60
MORNING_FIRST_ON_QUANTILE: Final = 0.2
DEFAULT_BAD_WEATHER_BOOST: Final = 20
DEFAULT_VERIFY_TIMEOUT: Final = 10
DARKNESS_CONFIRM: Final = timedelta(minutes=5)
BAD_WEATHER_CONDITIONS: Final = frozenset(
    {"rainy", "pouring", "snowy", "snowy-rainy", "lightning", "lightning-rainy", "hail"}
)
COLD_BELOW_C: Final = 8.0
UNRESPONSIVE_FAILURES: Final = 3

# Covers (v0.8.0)
CONF_COVERS: Final = "covers"
CONF_COVERS_MODE: Final = "covers_mode"
CONF_COVERS_OPEN_POSITION: Final = "covers_open_position"
CONF_COVERS_OPEN_WORKDAY: Final = "covers_open_workday"
CONF_COVERS_OPEN_NONWORKDAY: Final = "covers_open_nonworkday"
CONF_COVERS_CLOSE_DELAY: Final = "covers_close_delay_min"
COVERS_LEARNED: Final = "learned"
COVERS_SCHEDULE: Final = "schedule"
COVERS_MODES: Final = [COVERS_SCHEDULE, COVERS_LEARNED]
DEFAULT_COVERS_MODE: Final = COVERS_SCHEDULE
DEFAULT_COVERS_OPEN_POSITION: Final = 100
DEFAULT_COVERS_OPEN_WORKDAY: Final = "08:00:00"
DEFAULT_COVERS_OPEN_NONWORKDAY: Final = "09:30:00"
DEFAULT_COVERS_CLOSE_DELAY: Final = 20
# Never simulated: awnings can be damaged by wind; doors, garages and gates
# are a security risk.
FORBIDDEN_COVER_CLASSES: Final = frozenset({"awning", "garage", "gate", "door"})
COVERS_MIN_DAYS: Final = 7
# Covers take a while to move: how long to wait for them to obey.
COVER_VERIFY_TIMEOUT: Final = 90
# Why each cover moves when it does
COVER_SCHEDULE: Final = "schedule"
COVER_REPLAY: Final = "replay"
COVER_DREAM: Final = "dream"
COVER_NOT_ENOUGH_DAYS: Final = "schedule_not_enough_days"
COVER_OPEN: Final = "open"
COVER_CLOSE: Final = "close"

# Ambience (v0.8.0)
CONF_AMBIENCE_PLAYERS: Final = "ambience_players"
CONF_AMBIENCE_MEDIA: Final = "ambience_media"
CONF_AMBIENCE_VOLUME: Final = "ambience_volume"
CONF_AMBIENCE_QUIET_AFTER: Final = "ambience_quiet_after"
DEFAULT_AMBIENCE_VOLUME: Final = 0.15
DEFAULT_AMBIENCE_QUIET_AFTER: Final = "23:30:00"

# Deterrence (v0.8.0)
CONF_DETERRENCE_ENABLED: Final = "deterrence_enabled"
CONF_DETERRENCE_COOLDOWN: Final = "deterrence_cooldown_min"
CONF_DETERRENCE_MAX: Final = "deterrence_max_per_night"
DEFAULT_DETERRENCE_ENABLED: Final = False
DEFAULT_DETERRENCE_COOLDOWN: Final = 10
DEFAULT_DETERRENCE_MAX: Final = 6
DETERRENCE_FIRST_DELAY_S: Final = (2.0, 6.0)
DETERRENCE_PATH_DELAY_S: Final = (10.0, 25.0)
DETERRENCE_STEP_DELAY_S: Final = (2.0, 5.0)
DETERRENCE_HOLD_MIN: Final = (3.0, 8.0)
EVENT_DETERRENCE: Final = f"{DOMAIN}_deterrence"

# Generative AI (v0.9.0, optional)
CONF_AI_TASK_ENTITY: Final = "ai_task_entity"
CONF_AI_SCENARIO_PROBABILITY: Final = "ai_scenario_probability"
CONF_AI_REPORT: Final = "ai_report"
CONF_AI_MAX_CALLS: Final = "ai_max_calls_per_day"
DEFAULT_AI_SCENARIO_PROBABILITY: Final = 25
DEFAULT_AI_REPORT: Final = False
DEFAULT_AI_MAX_CALLS: Final = 4
AI_TIMEOUT_S: Final = 20
# Why an AI feature did (not) run
AI_OK: Final = "ok"
AI_DISABLED: Final = "ai_disabled"
AI_NOT_SELECTED: Final = "not_selected"
AI_DAILY_LIMIT: Final = "ai_daily_limit"
AI_TIMEOUT: Final = "ai_timeout"
AI_ERROR: Final = "ai_error"
AI_INVALID: Final = "ai_invalid_output"
SCENARIO_LOW_SCORE: Final = "scenario_low_score"
# Why the evening window starts when it does
START_DARKNESS: Final = "darkness"
START_SUNSET: Final = "sunset"
START_CLAMPED_EARLIEST: Final = "clamped_earliest"
START_CLAMPED_LATEST: Final = "clamped_latest"
START_SENSOR_UNAVAILABLE: Final = "sensor_unavailable"
# The evening start is not decided yet (waiting for darkness or the latest time).
START_PENDING: Final = "pending"
START_MORNING: Final = "morning"
START_REASONS: Final = [
    START_DARKNESS,
    START_SUNSET,
    START_CLAMPED_EARLIEST,
    START_CLAMPED_LATEST,
    START_SENSOR_UNAVAILABLE,
    START_PENDING,
    START_MORNING,
]
# Where tonight's night type comes from
NIGHT_TYPE_CALENDAR: Final = "calendar"
NIGHT_TYPE_RULE: Final = "weekday_rule"
NIGHT_TYPE_CALENDAR_UNAVAILABLE: Final = "calendar_unavailable"
NIGHT_TYPE_WORKDAY: Final = "workday"
NIGHT_TYPE_WORKDAY_UNAVAILABLE: Final = "workday_unavailable"
# Why tonight's Dream plan is what it is, or why Dream fell back
DREAM_BEST: Final = "best_candidate"
DREAM_LOW_SCORE: Final = "low_score"
DREAM_NOT_ENOUGH_NIGHTS: Final = "not_enough_nights"
DREAM_NO_WINDOW: Final = "no_window"
DREAM_REASONS: Final = [DREAM_BEST, DREAM_LOW_SCORE, DREAM_NOT_ENOUGH_NIGHTS, DREAM_NO_WINDOW]
# What ran tonight in Dream mode
SOURCE_DREAM: Final = "dream"
SOURCE_REPLAY: Final = "replay"
SOURCE_FALLBACK: Final = "fallback"
SOURCES: Final = [SOURCE_DREAM, SOURCE_REPLAY, SOURCE_FALLBACK]

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
EVENT_NIGHT_REPORT: Final = f"{DOMAIN}_night_report"

MAX_START_JITTER_MIN: Final = 30
MAX_END_JITTER_MIN: Final = 30
OWN_CONTEXT_MAXLEN: Final = 1000

# Learning gate
REASON_SIMULATION: Final = "simulation_enabled"
REASON_ALARM: Final = "alarm_armed_away"
# Armed vacation always means an empty house.
REASON_ALARM_VACATION: Final = "alarm_armed_vacation"
REASON_AWAY: Final = "nobody_home"
REASON_PAUSED: Final = "paused"
REASON_MARGIN: Final = "return_margin"
# Simulation turned on with someone at home: a pause, not an absence.
REASON_SIMULATION_TEST: Final = "simulation_test"
ABSENCE_REASONS: Final = frozenset(
    {REASON_SIMULATION, REASON_ALARM, REASON_ALARM_VACATION, REASON_AWAY}
)

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
SERVICE_DREAM_PREVIEW: Final = "dream_preview"
SERVICE_GET_HEATMAP: Final = "get_heatmap"
ATTR_DAY_TYPE: Final = "day_type"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_ROUTE: Final = "route"
ATTR_LIGHTS_BEHAVIOR: Final = "lights_behavior"
ATTR_START_DATE: Final = "start_date"
ATTR_END_DATE: Final = "end_date"


def signal_update(entry_id: str) -> str:
    """Dispatcher signal used to refresh the entities of one entry."""
    return f"{DOMAIN}_update_{entry_id}"
