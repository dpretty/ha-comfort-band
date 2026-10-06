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
# v0.18.0 room-sensor fallback. Both are OptionsFlow-only (never in `data`):
# the optional stand-in sensor, and whether the climate entity's own
# `current_temperature` may serve as the last resort.
CONF_FALLBACK_TEMP_SENSOR: Final = "fallback_temp_sensor"
CONF_FALLBACK_TO_CLIMATE: Final = "fallback_to_climate"
DEFAULT_FALLBACK_TO_CLIMATE: Final = True
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
# v0.20.0: the longest gap between consecutive samples that one run may span
# (inclusive). Nothing is sampled while the room sensor is dark, while the
# climate entity is unreachable (except in shadow mode or while a min-cycle
# gate holds) or while Home Assistant is down, and the runs either side
# of such a gap used to be joined by action label alone -- a slope
# fitted across time nobody watched. A zone samples whenever its room reading
# (or its humidity sensor, if it has one) changes, at most once a minute: every
# 293 seconds for the battery sensors it was measured on. Ten days of five
# zones' history put every gap inside a run with nothing dark at 14.7 minutes or
# less -- two lost reports -- and every longer one at an outage of the room
# sensor or the climate entity. The one join among those that misled, a room
# that rose 0.6 °C while its sensor was dark and read as warming at 1.5 °C/h
# against the 0.4 seen afterwards, spanned 19.6 minutes. Seventeen sits between
# two and three lost reports from a sensor reporting every five minutes, so
# neither lands on the edge (fifteen sits exactly on two of them). A restart's
# gap also includes up to SAMPLE_PERSIST_INTERVAL_S of samples taken but not yet
# written to disk, so this bounds how short a restart must be for a run to
# survive it as well. Home Assistant passes on only a change, though, so a zone
# whose readings change less often than this -- a coarse sensor with no humidity
# sensor, in a steady room or a slow heat or cool cycle -- is split at every
# quiet stretch, and such cycles get no recovery slope.
SAMPLE_MAX_GAP_MINUTES: Final = 17

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

# How often a zone may log that its room sensor came or went. A sensor on a
# weak mesh or a dying battery crosses that boundary hundreds of times an hour,
# so it needs a floor. Applied per direction, so the ceiling is two lines per
# interval: five minutes makes a flapping sensor cheap while still reporting a
# real dropout promptly.
SENSOR_EDGE_LOG_INTERVAL_S: Final = 300
# Same budget, separate name: the command-path warnings (a mode call that
# raises, an undeliverable command, a setpoint the unit won't take, a fan mode
# it won't take) repeat for as long as the fault lasts, and three of the four
# can be permanent: an entity advertising only a temperature *range* raises for
# a plain setpoint forever, a stored fan mode a unit advertises but refuses is
# retried on every apply, and from Home Assistant 2025.4 a mode the entity does
# not advertise raises rather than warning.
# The budget is per fault episode, not per hour: each key is cleared when a call
# of its own kind next succeeds, so a unit that fails intermittently still gets
# a line per episode. That is the trade for announcing a return promptly. The
# mode key is cleared a little later than that -- past the delivery check, since
# a clean return is not proof of delivery -- for reasons the coordinator gives
# at the point it clears it.
COMMAND_WARN_INTERVAL_S: Final = 300

# v0.18.0 room-sensor fallback. How long the configured room sensor must be
# continuously unavailable before the zone starts controlling from a stand-in
# reading. Five minutes rides out the mesh blips the edge-log throttle above
# exists for: switching sources on every blip would feed hysteresis a step
# between two readings with different offsets, and the sampler must never see
# that step at all. During the grace window the zone behaves exactly as it did
# before v0.18.0 -- no reading, no command.
FALLBACK_GRACE_S: Final = 300
# Added to both deadbands while a stand-in reading drives control. A climate
# entity's own sensor sits at the indoor unit and typically reports whole
# degrees, so the same deadbands that suit a room sensor short-cycle on it.
FALLBACK_DEADBAND_EXTRA: Final = 0.5

# Where the room reading driving control came from. Surfaced as the `source`
# attribute of `sensor.{zone}_room_temperature`.
ROOM_SOURCE_PRIMARY: Final = "primary"
ROOM_SOURCE_FALLBACK_SENSOR: Final = "fallback_sensor"
ROOM_SOURCE_CLIMATE: Final = "climate"
ROOM_SOURCE_NONE: Final = "none"
# The sources that mean a stand-in is driving control.
ROOM_SOURCES_STAND_IN: Final = frozenset({ROOM_SOURCE_FALLBACK_SENSOR, ROOM_SOURCE_CLIMATE})

# Slope estimator: minimum samples per segment before WLS produces a slope;
# exponential recency weight time constant; epsilon below which a slope is
# treated as "flat" (predictor falls through to hysteresis).
SLOPE_MIN_SAMPLES: Final = 4
SLOPE_WEIGHT_TAU_MINUTES: Final = 20.0
SLOPE_EPSILON_PER_HOUR: Final = 0.05

# v0.22.0: how long a new heat or cool run must have been watched, as well as
# how many samples it needs, before its own fit replaces the previous cycle's
# slope MPC borrowed for it (`predictor.carry_over_recovery_slopes`). Four
# samples take fifteen minutes at the five-minute cadence of a battery sensor,
# so this rarely matters there (twice in ten days of five zones' history, when
# extra samples gave a running cycle four inside ten minutes); at a sample a
# minute they take three, which is less than many units need to move the room at
# all: a slow unit's fit that early came out the wrong way round and lost MPC
# the cycle. Only a run still running waits: an ended run's fit stands.
# Simulated from 97 recorded MPC starts at a one-minute cadence, ten minutes
# took the cycles lost that way from as many as 83, depending on how slowly the
# unit came up, to at most 1. Fifteen cut starts there by up to a fifth, but ran
# the unit 1.4-1.8 points more of the time for a little less time in band, and
# at a five-minute cadence it would make a fourth sample at 14.7 minutes too
# young.
CARRY_OVER_MIN_SPAN_MINUTES: Final = 10

# v0.19.0: how much of the start of an idle stretch the idle slope leaves out
# (since v0.20.0, measured from where the stretch began, across any gap). An
# idle run almost always begins at the release of a heat or cool cycle, and for
# a while after that the room is still answering the cycle rather than drifting:
# the air relaxes back toward furniture and walls the cycle never reached, and
# after cooling the fan re-evaporates the water left on the coil, which a zone
# on apparent temperature reads as warming. Ten days of two zones' history put
# the first live idle slope after a cool release at a median of about +1.4 °C/h
# (-0.9 after a heat release), against roughly +/-0.25 half an hour on. A zone
# that cycles every twenty minutes only ever saw that aftermath, persisted it as
# its passive rate, and pre-cooled on a cold night on the strength of it.
IDLE_SETTLE_MINUTES: Final = 30

# v0.12.0: persisted idle slope. The idle (passive heat-loss) rate is a
# slow-changing thermal property, so we remember the last good idle slope
# beyond the 90-min sample window. When a heating-dominated room chases a
# rising morning band, the live window can only produce 1-3 min idle blips
# (below SLOPE_MIN_SAMPLES) and any sustained overnight idle ages out --
# leaving `mpc.is_ready` False exactly when pre-heat is needed. Substituting
# the remembered idle slope keeps MPC ready through the chase. The max age
# is generous enough to bridge overnight -> morning but expires day-to-day so
# a stale value can't mislead MPC indefinitely (e.g. after a window is left
# open, furniture moved, season change). Since v0.19.0 the age runs from the
# newest sample behind the value, not from when it was last written.
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

# Named shared schedules (v0.14.0). A shared schedule is a store-level,
# profile-aware schedule that any number of zones can be assigned to (via
# `StoredZone.schedule_id`) so grouped rooms target the same band. Same
# generous cap rationale as MAX_PROFILES.
MAX_SHARED_SCHEDULES: Final = 50

# The per-zone schedule-assignment select's "unassigned" sentinel option. Also a
# reserved shared-schedule name (a real schedule named this would collide with
# the sentinel in the option list), so create/rename refuse it.
OWN_SCHEDULE_LABEL: Final = "Own schedule"

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
    Platform.SELECT,
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
# v0.14.0 named shared schedules. SHARED_SCHEDULE_CHANGED fires on a content
# edit (carries the shared-schedule id + profile) so the WS layer can push to
# every subscriber of that id and every assigned zone refreshes; the additive
# sibling of SIGNAL_ZONE_SCHEDULE_CHANGED (the per-zone path is left untouched).
# SHARED_SCHEDULE_LIST_CHANGED fires on create/rename/delete so the per-zone
# assignment selects re-render their option list.
SIGNAL_SHARED_SCHEDULE_CHANGED: Final = f"{DOMAIN}_shared_schedule_changed"
SIGNAL_SHARED_SCHEDULE_LIST_CHANGED: Final = f"{DOMAIN}_shared_schedule_list_changed"
