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
DEFAULT_OVERRIDE_HOURS: Final = 3

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
