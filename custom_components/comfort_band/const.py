"""Constants for Comfort Band."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "comfort_band"

LOGGER: Final = logging.getLogger(__package__)

# Config entry kinds — every ConfigEntry stores `data["kind"]` as one of these.
ENTRY_KIND_ZONE: Final = "zone"
ENTRY_KIND_PROFILE_MANAGER: Final = "profile_manager"

# Config keys (used in both ConfigFlow and OptionsFlow).
CONF_KIND: Final = "kind"
CONF_ZONE_NAME: Final = "zone_name"
CONF_CLIMATE_ENTITY: Final = "climate_entity"
CONF_TEMP_SENSOR: Final = "temp_sensor"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
CONF_DEADBAND_BELOW: Final = "deadband_below"
CONF_DEADBAND_ABOVE: Final = "deadband_above"
CONF_MIN_CYCLE_MINUTES: Final = "min_cycle_minutes"
CONF_OVERRIDE_HOURS: Final = "override_hours"

# Defaults — see plan §Decisions locked.
DEFAULT_DEADBAND_BELOW: Final = 0.3
DEFAULT_DEADBAND_ABOVE: Final = 0.5
DEFAULT_MIN_CYCLE_MINUTES: Final = 8
DEFAULT_CROSS_MODE_MIN_MINUTES: Final = DEFAULT_MIN_CYCLE_MINUTES
DEFAULT_OVERRIDE_HOURS: Final = 3

# Predictive control (v0.6+): per-zone rolling-window thermal-slope estimator.
# `lookahead_minutes` is the horizon over which the predictor projects the
# current slope forward; conservative starting point matched to typical
# HVAC time-to-effect.
DEFAULT_LOOKAHEAD_MINUTES: Final = 5
LOOKAHEAD_MIN: Final = 2
LOOKAHEAD_MAX: Final = 15

# Sample buffer: time-based cap, rate-limit for like-actioned appends, and a
# count cap as defence-in-depth against clock skew filling the buffer.
SAMPLE_WINDOW_MINUTES: Final = 90
SAMPLE_MIN_INTERVAL_S: Final = 60
SAMPLE_MAX_COUNT: Final = 200

# How often the coordinator persists the in-memory sample buffer. The buffer
# is appended ~1/min (SAMPLE_MIN_INTERVAL_S), but writing the whole sample
# list to .storage every minute would amplify flash wear on SD-card-backed
# HA installs (the majority on Pi / HAOS). Action transitions always persist
# immediately (they are segment boundaries the slope estimator relies on);
# same-action samples persist at most once per SAMPLE_PERSIST_INTERVAL_S.
SAMPLE_PERSIST_INTERVAL_S: Final = 300

# How long after our own `climate.set_*` calls we ignore observed climate
# state changes. set_hvac_mode + set_temperature are two sequential awaits
# and a slow climate (cloud-backed, mesh-routed, etc.) can take many seconds
# to acknowledge the second one -- the listener needs to absorb both echoes.
CLIMATE_ECHO_WINDOW_S: Final = 30

# Slope estimator: minimum samples per segment before WLS produces a slope;
# exponential recency weight time constant; epsilon below which a slope is
# treated as "flat" (predictor falls through to hysteresis).
SLOPE_MIN_SAMPLES: Final = 4
SLOPE_WEIGHT_TAU_MINUTES: Final = 20.0
SLOPE_EPSILON_PER_HOUR: Final = 0.05

# v0.12.0: persisted idle slope. The idle (passive heat-loss) rate is a
# slow-changing thermal property, so we remember the last good idle slope
# beyond the 90-min sample window. When a heating-dominated room chases a
# rising morning band, the live window can only produce 1-3 min idle blips
# (below SLOPE_MIN_SAMPLES) and any sustained overnight idle ages out --
# leaving `mpc.is_ready` False exactly when pre-heat is needed. Substituting
# the remembered idle slope keeps MPC ready through the chase. The max age
# is generous enough to bridge overnight -> morning but expires day-to-day so
# a stale value can't mislead MPC indefinitely (e.g. after a window is left
# open, furniture moved, season change).
PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES: Final = 24 * 60

# Passive drift acceptance (v0.7+). When hysteresis would fire heat / cool
# because the room has crossed the deadband, but the predictor's slope says
# we'll return to band within `lookahead_minutes`, the predictor stays idle.
# Two guards apply:
#   - the forecast must move the room by at least
#     PASSIVE_FORECAST_MOVEMENT_MIN_C toward the band (defends against
#     false-positive suppression on sensor jitter, where a barely-non-flat
#     slope would otherwise look like "recovery in progress"); and
#   - the room must be within `passive_tolerance` °C of the band edge
#     (per-zone comfort floor, surfaced as a `number` entity; 0 disables).
PASSIVE_FORECAST_MOVEMENT_MIN_C: Final = 0.1
DEFAULT_PASSIVE_TOLERANCE_C: Final = 0.5
PASSIVE_TOLERANCE_MIN: Final = 0.0
PASSIVE_TOLERANCE_MAX: Final = 2.0

# Model-predictive control (v0.8+). At each refresh, MPC enumerates a small
# action space ({idle, heat, cool} in v0.8; will grow to include fan modes in
# v0.9.x), simulates each forward over `mpc_horizon_minutes` using the
# per-action slopes from v0.6, and picks the action that maximises projected
# time-in-band. See `mpc.py` for the planner. Cold-start gate (v0.8.1+):
# requires `idle_slope` and at least one recovery slope. Otherwise the
# coordinator falls back to the v0.7 predictor silently.
#
# v0.9.0: default horizon bumped 20 → 60 to give MPC enough lookahead for
# pre-heat / pre-cool decisions before scheduled band transitions (paired
# with the `bands_per_step` schedule lookahead in `mpc.plan`). 20 min was
# too short — typical pre-heat needs 30-60 min of foresight even when the
# schedule transition itself is visible. MAX stays at 60: slopes are
# estimated from a 90-minute sample window (`SAMPLE_WINDOW_MINUTES`); a
# longer horizon would extrapolate beyond the data window and compound
# slope-estimation error in the cost function. Existing zones keep their
# explicit `mpc_horizon_minutes` value via `storage.py`'s presence-keyed
# backfill — only freshly-created zones pick up the new default.
DEFAULT_MPC_HORIZON_MINUTES: Final = 60
MPC_HORIZON_MIN: Final = 10
MPC_HORIZON_MAX: Final = 60
# Time step used by `mpc.simulate` when integrating forward. 1.0 minute keeps
# the simulation cheap (≤60 iterations per action per refresh) and matches the
# resolution of the underlying slope estimator (which produces °C/minute).
MPC_SIMULATION_STEP_MINUTES: Final = 1.0

# Band-ramp smoothing (v0.10.0+). When > 0, schedule transitions are
# smoothed by linearly interpolating the (low, high) band edges within
# ±ramp/2 of each transition's time. Per-zone via `number.{zone}_band_
# ramp_minutes`. Defaults to 0 (instant step transitions — the v0.9.x
# behaviour) so existing zones see no change on upgrade. A 30-minute
# ramp smooths a 4 °C jump to ~0.14 °C/min instead of a wall, giving
# HVAC time to ease into the new setpoint rather than chasing a sudden
# deficit. The interpolation lives in `schedule.resolve` and
# `schedule.upcoming_bands` so MPC / predictor / hysteresis all see the
# smoothed band naturally via `effective_low` / `effective_high`.
DEFAULT_BAND_RAMP_MINUTES: Final = 0
BAND_RAMP_MINUTES_MIN: Final = 0
BAND_RAMP_MINUTES_MAX: Final = 120
BAND_RAMP_MINUTES_STEP: Final = 5

# Number entity bounds (matches the legacy input_number ranges).
TEMP_MIN: Final = 16.0
TEMP_MAX: Final = 26.0
TEMP_STEP: Final = 0.5

# Profiles.
# DEFAULT_PROFILE is the *seed* default for fresh installs. After install the
# live default is tracked per-store as `default_profile`, which moves with
# renames — see `ComfortBandStore.default_profile`.
DEFAULT_PROFILE: Final = "home"
BUILTIN_PROFILES: Final = ("home", "away")
# Hard cap on user-defined profiles. Generous (50 is far beyond any
# realistic household), but prevents an unbounded-create loop from bloating
# the .storage file.
MAX_PROFILES: Final = 50

# Action labels (returned by hysteresis.decide; surfaced via the current_action sensor).
ACTION_HEAT: Final = "heating"
ACTION_COOL: Final = "cooling"
ACTION_IDLE: Final = "idle"
ACTION_UNKNOWN: Final = "unknown"

# HVAC mode strings the coordinator passes to climate.set_hvac_mode.
# Kept here (rather than importing HVACMode from HA) so hysteresis.py stays
# pure-Python with stdlib-only imports.
HVAC_MODE_HEAT: Final = "heat"
HVAC_MODE_COOL: Final = "cool"
HVAC_MODE_FAN_ONLY: Final = "fan_only"

# Storage.
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = "comfort_band.data"

# Comfort-feedback log (v0.11.0). Kept in a SEPARATE Store from the main zone
# data so the append-only feedback history never bloats (or risks corrupting)
# the core config. Capped to the most-recent N entries to bound disk + memory.
FEEDBACK_STORAGE_VERSION: Final = 1
FEEDBACK_STORAGE_KEY: Final = "comfort_band.feedback"
FEEDBACK_MAX_ENTRIES: Final = 2000
FEEDBACK_LABELS: Final = ("too_hot", "just_right", "too_cold")

# Platforms forwarded by each ConfigEntry kind.
PLATFORMS_ZONE: Final = (
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
)
PLATFORMS_PROFILE_MANAGER: Final = (Platform.SELECT,)

# Signals (async_dispatcher).
SIGNAL_ACTIVE_PROFILE_CHANGED: Final = f"{DOMAIN}_active_profile_changed"
SIGNAL_PROFILE_LIST_CHANGED: Final = f"{DOMAIN}_profile_list_changed"
SIGNAL_ZONE_SCHEDULE_CHANGED: Final = f"{DOMAIN}_zone_schedule_changed"
