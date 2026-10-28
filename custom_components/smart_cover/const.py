"""Constants for the Smart Cover integration."""

DOMAIN = "smart_cover"

CONF_TARGET_COVER = "target_cover"
CONF_WINDOW_SENSOR = "window_sensor"
CONF_TILT_SENSOR = "tilt_sensor"
CONF_TILT_CLOSED_VALUE = "tilt_closed_value"
CONF_TILT_TOLERANCE = "tilt_tolerance"
# Pre-0.5.0 single threshold ("tilted above N degrees"). Only read by the
# config entry migration in __init__.py, which turns it into
# tilt_closed_value 0 + tilt_tolerance N — the same classification.
LEGACY_CONF_TILT_THRESHOLD = "tilt_threshold"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_MANUAL_WEATHER_CONDITION = "manual_weather_condition"
CONF_RAIN_POSITION_LIMIT = "rain_position_limit"
CONF_VASISTAS_POSITION_LIMIT = "vasistas_position_limit"
CONF_CLOSED_POSITION_OFFSET = "closed_position_offset"
CONF_PRESETS = "presets"
CONF_WINDOW_DEBOUNCE = "window_debounce"
CONF_AUTO_RECALIBRATE_ON_RESTART = "auto_recalibrate_on_restart"
CONF_RESTART_RECALIBRATION_MIN_DELTA = "restart_recalibration_min_delta"
CONF_RECALIBRATION_STEP_SECONDS = "recalibration_step_seconds"

# What the rotation sensor reads with the sash vertical — shut, or open
# as a casement — and how far a reading may stray from it and still
# count as vertical. Anything above that band is vasistas.
DEFAULT_TILT_CLOSED_VALUE = 0
DEFAULT_TILT_TOLERANCE = 0.2
DEFAULT_MANUAL_WEATHER_CONDITION = "sunny"

# ----------------------------------------------------------------------
# Window positions, as classified by WindowTracker (window.py).
#
#   closed  -> the sash is shut: the open/close sensor reads closed.
#   tilted  -> vasistas/top-hung: the sash reads OPEN and the rotation
#              sensor is above tilt_closed_value + tilt_tolerance. A
#              tilted window is open as far as air is concerned; only the
#              rain caps treat it as a lesser exposure than a fully open
#              one.
#   open    -> casement/fully open: the sash reads open and the rotation
#              is not above that band. A reading BELOW the band is not a
#              position a tilt-and-turn window can be in; it stays "open" because
#              that is the stricter rain cap.
#   unknown -> no window sensor configured, or it has not reported.
#
# The open/close sensor is authoritative for open vs closed and is read
# FIRST: rotation above the threshold while the sash reads closed is not
# a physical state a tilt-and-turn window can be in, so it is not treated as one.
# ----------------------------------------------------------------------
WINDOW_CLOSED = "closed"
WINDOW_TILTED = "tilted"
WINDOW_OPEN = "open"
WINDOW_UNKNOWN = "unknown"
# The values the window sensor entity can report, in the order shown in
# the UI. "unknown" is deliberately absent: an enum sensor reports it
# through Home Assistant's own unknown state instead of as an option.
WINDOW_POSITION_OPTIONS = [WINDOW_CLOSED, WINDOW_TILTED, WINDOW_OPEN]

# ----------------------------------------------------------------------
# Anti-bounce window for the settled window position (see window.py).
#
# The raw window position reacts instantly to the sensors; the STABLE one
# only adopts a new position once it has held for this many seconds.
# Anything shorter never becomes stable at all and leaves no trace in
# the previous-position history — which is the whole point: going from
# vasistas to fully open on a tilt-and-turn window requires passing through
# "closed" for a few seconds, and that pass-through must not read as a
# real closure. Same mechanism absorbs a reed contact bouncing while a
# window that is already open gets handled.
#
# Consumers that want a longer confirmation than this stack their own on
# top (an automation's `for:`), so keep this at the shortest value that
# reliably swallows a manoeuvre.
# ----------------------------------------------------------------------
DEFAULT_WINDOW_DEBOUNCE = 10

# ----------------------------------------------------------------------
# Rain limits: maximum cover opening while it's raining, by window
# position. Not a safe/unsafe switch — three levels of exposure, each
# with its own cap:
#
#   window fully open (casement) -> water gets in, keep the cover almost
#       shut: DEFAULT_RAIN_POSITION_LIMIT.
#   window vasistas (top-hung)   -> the gap is up high, so the cover is
#       allowed to sit higher: DEFAULT_VASISTAS_POSITION_LIMIT.
#   window closed                -> water never gets in, no cap at all.
#
# Both are FINAL/calibrated percentages (see closed_position_offset) and
# both are per-cover options — these are only the defaults for a newly
# created entry. The vasistas one is only meaningful when that cover has
# a tilt_sensor: without a rotation reading vasistas can't be told apart
# from a fully open window, so the stricter limit applies.
# ----------------------------------------------------------------------
DEFAULT_RAIN_POSITION_LIMIT = 15
DEFAULT_VASISTAS_POSITION_LIMIT = 40
# 0 = no calibration, i.e. identical to raw target_cover behaviour.
DEFAULT_CLOSED_POSITION_OFFSET = 0
# Preset percentages offered as quick-access buttons, both on the
# Lovelace card (unless the card config overrides them) and as this
# entity's own "presets" attribute.
DEFAULT_PRESETS = [0, 25, 50, 75, 100]

# Standard Home Assistant weather condition values (same set a real
# weather.* entity can report) — the fixed-value options for
# manual_weather_condition.
ALL_WEATHER_CONDITIONS = [
    "clear-night",
    "cloudy",
    "exceptional",
    "fog",
    "hail",
    "lightning",
    "lightning-rainy",
    "partlycloudy",
    "pouring",
    "rainy",
    "snowy",
    "snowy-rainy",
    "sunny",
    "windy",
    "windy-variant",
]

# Special manual_weather_condition value meaning "read weather_entity
# instead of using a fixed condition". Only offered as a choice when
# weather_entity is set — see config_flow.py.
MANUAL_CONDITION_SENSOR = "sensor"

# weather.* states considered "rain" for cover-safety purposes.
RAIN_WEATHER_STATES = [
    "rainy",
    "pouring",
    "hail",
    "snowy",
    "snowy-rainy",
    "lightning-rainy",
]

# Custom service names exposed by the entity. Standard movement
# (open/close/set_position/stop) is NOT listed here: the entity is a
# regular CoverEntity, so those are handled natively by the `cover`
# domain's own open_cover/close_cover/set_cover_position/stop_cover.
SERVICE_CLOSE_COMPLETELY = "close_completely"
SERVICE_RECALIBRATE_MANUAL = "recalibrate_manual"
SERVICE_REQUEST_RECALIBRATION = "request_recalibration"
# Automation-facing counterpart to the standard set_cover_position: a
# hint the entity clamps to whatever rain limit is in force right now,
# and remembers if it can't be fully honored yet — see
# entity.async_request_position.
SERVICE_REQUEST_POSITION = "request_position"
SERVICE_SET_RECALIBRATION_ENABLED = "set_recalibration_enabled"
SERVICE_SET_RAIN_CONTROL_ENABLED = "set_rain_control_enabled"
SERVICE_ACTIVATE_RAIN_PROTECTION = "activate_rain_protection"
SERVICE_RAMP_TO_POSITION = "ramp_to_position"

ATTR_ENABLED = "enabled"
ATTR_POSITION = "position"
ATTR_START_POSITION = "start_position"
ATTR_END_TIME = "end_time"
ATTR_STEPS = "steps"

# Duration of each close/open/restore step during recalibration: long
# enough for the cover to travel end to end. Per-cover config option (see
# CONF_RECALIBRATION_STEP_SECONDS) — this is only the default for a newly
# created entry.
DEFAULT_RECALIBRATION_STEP_SECONDS = 30

# ----------------------------------------------------------------------
# Auto-recalibration after a Home Assistant restart (e.g. a power
# outage): if enabled, the cover runs a full recalibration cycle the
# first time an automation's request_position asks for a "long" move
# after this HA session started — see entity.py,
# async_added_to_hass/async_request_position. Per-cover config option,
# on by default. Manual commands (open/close/set_position) never
# trigger this: only the automation-facing request_position does.
# ----------------------------------------------------------------------
DEFAULT_AUTO_RECALIBRATE_ON_RESTART = True
# Minimum |delta| between the cover's current position and a
# request_position ask for that ask to count as "long" enough to
# piggyback the recalibration cycle on. Keeps a tiny post-restart
# nudge from turning into a full recalibration. Per-cover config option
# (see CONF_RESTART_RECALIBRATION_MIN_DELTA) — this is only the default
# for a newly created entry.
#
# Both sides of the delta are CALIBRATED/FINAL percentages — the
# "public" 0-100 scale everyone outside this integration speaks (see
# entity.py's calibration docstring) — never the physical cover's raw
# scale, which additionally spans the closed_position_offset sealing
# stretch below it. Comparing raw values would let that stretch's own
# width count towards the threshold, which has nothing to do with how
# far the shutter itself actually travels.
DEFAULT_RESTART_RECALIBRATION_MIN_DELTA = 25

# Default number of intermediate position updates for ramp_to_position
# when the caller doesn't specify one.
DEFAULT_RAMP_STEPS = 10

# Smallest movement a ramp step is allowed to make, in CALIBRATED
# percentage points. A ramp whose span is too short for the requested
# number of steps runs fewer, larger steps instead, and an intermediate
# step that would move the cover by less than this is skipped: tiny
# moves are noisy and wear the motor without visibly changing the light.
# The final step always runs, so the target is still reached exactly.
MIN_RAMP_STEP_SIZE = 5

# Custom Lovelace card, served as a static resource — see __init__.py.
FRONTEND_URL_PATH = "/smart_cover_frontend"
FRONTEND_JS_FILENAME = "smart-cover-card.js"
LOVELACE_CARD_TYPE = "smart-cover-card"
