"""Single-Store persistence layer for Comfort Band.

All zones, profiles, and the active-profile pointer live in one Store keyed
`comfort_band.data` (version 1). Tightly coupled because:

- The active profile flips every zone at once (one atomic write).
- Total payload is KB-sized (per-zone schedules + a handful of numbers).
- One file is easier to back up, inspect, and migrate than many.

`ComfortBandStore` is the only thing the rest of the integration touches —
the raw `Store` object is private. Every accessor returns a deep copy so a
caller mutation never bleeds into in-memory state without going through a
mutator method (which writes to disk before returning).
"""

from __future__ import annotations

import copy
from typing import Any, TypedDict, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

__all__ = [
    "ComfortBandStore",
    "StoredData",
    "StoredProfile",
    "StoredProfileSchedule",
    "StoredTransition",
    "StoredZone",
]

from .const import (
    BUILTIN_PROFILES,
    DEFAULT_DEADBAND_ABOVE,
    DEFAULT_DEADBAND_BELOW,
    DEFAULT_MIN_CYCLE_MINUTES,
    DEFAULT_OVERRIDE_HOURS,
    DEFAULT_PROFILE,
    MAX_PROFILES,
    SIGNAL_ZONE_SCHEDULE_CHANGED,
    STORAGE_KEY,
    STORAGE_VERSION,
    TEMP_MAX,
    TEMP_MIN,
)


class StoredTransition(TypedDict):
    at: str  # "HH:MM"
    low: float
    high: float


class StoredProfileSchedule(TypedDict):
    baseline: list[StoredTransition]
    current: list[StoredTransition]


class StoredZone(TypedDict):
    zone_name: str
    schedules: dict[str, StoredProfileSchedule]
    manual_low: float
    manual_high: float
    override_hours: int
    override_until: str | None
    deadband_below: float
    deadband_above: float
    min_cycle_minutes: int
    enabled: bool
    last_action_at: str | None
    last_action: str | None


class StoredProfile(TypedDict):
    name: str
    description: str


class StoredData(TypedDict):
    zones: dict[str, StoredZone]
    profiles: dict[str, StoredProfile]
    active_profile: str
    # Rename-aware fallback target. Initialised to DEFAULT_PROFILE on first
    # load; updated when that profile is renamed. Used by `async_remove_profile`
    # to refuse deletion and by `ProfileRegistry.async_delete` to fall back to
    # when the active profile is removed.
    default_profile: str


_BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "home": "Default schedule when the house is occupied.",
    "away": "Cooler heating / warmer cooling for an empty house.",
}


def _default_data() -> StoredData:
    return {
        "zones": {},
        "profiles": {
            name: {"name": name, "description": _BUILTIN_DESCRIPTIONS.get(name, "")}
            for name in BUILTIN_PROFILES
        },
        "active_profile": DEFAULT_PROFILE,
        "default_profile": DEFAULT_PROFILE,
    }


def _default_zone(zone_name: str) -> StoredZone:
    midpoint = (TEMP_MIN + TEMP_MAX) / 2
    return {
        "zone_name": zone_name,
        "schedules": {},
        "manual_low": midpoint - 1.5,
        "manual_high": midpoint + 1.5,
        "override_hours": DEFAULT_OVERRIDE_HOURS,
        "override_until": None,
        "deadband_below": DEFAULT_DEADBAND_BELOW,
        "deadband_above": DEFAULT_DEADBAND_ABOVE,
        "min_cycle_minutes": DEFAULT_MIN_CYCLE_MINUTES,
        "enabled": False,
        "last_action_at": None,
        "last_action": None,
    }


class ComfortBandStore:
    """Wraps `Store[StoredData]` with typed accessors and copy-on-read isolation."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[StoredData] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: StoredData = _default_data()
        self._loaded = False

    async def async_load(self) -> None:
        """Read from disk; default skeleton on first run. Idempotent.

        Old payloads (v0.1) didn't have `default_profile`. If it's missing we
        seed it from DEFAULT_PROFILE if that profile still exists; otherwise
        fall back to the alphabetically-first profile and persist.
        """
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if loaded is not None:
            self._data = loaded
        # `loaded` may be a v0.1 payload that doesn't have all fields of
        # StoredData yet — cast to a plain dict to check + patch.
        raw = cast(dict[str, Any], self._data)
        migrated = False
        if "default_profile" not in raw:
            profiles = raw.get("profiles") or {}
            if DEFAULT_PROFILE in profiles:
                raw["default_profile"] = DEFAULT_PROFILE
            elif profiles:
                raw["default_profile"] = sorted(profiles.keys())[0]
            else:
                # Empty profile list is a corrupted store; reseed builtins.
                raw["profiles"] = {
                    name: {"name": name, "description": _BUILTIN_DESCRIPTIONS.get(name, "")}
                    for name in BUILTIN_PROFILES
                }
                raw["default_profile"] = DEFAULT_PROFILE
            migrated = True
        if migrated:
            await self._store.async_save(self._data)
        self._loaded = True

    async def async_save(self) -> None:
        """Persist current state. Mutator methods call this; callers usually don't."""
        await self._store.async_save(self._data)

    # ----- whole-store accessor -----

    @property
    def data(self) -> StoredData:
        """Deep copy of the entire store payload."""
        return copy.deepcopy(self._data)

    # ----- zones -----

    def list_zones(self) -> list[str]:
        return sorted(self._data["zones"].keys())

    def has_zone(self, zone_name: str) -> bool:
        return zone_name in self._data["zones"]

    def get_zone(self, zone_name: str) -> StoredZone:
        return copy.deepcopy(self._data["zones"][zone_name])

    async def async_add_zone(self, zone_name: str) -> StoredZone:
        if zone_name in self._data["zones"]:
            raise ValueError(f"Zone {zone_name!r} already exists")
        self._data["zones"][zone_name] = _default_zone(zone_name)
        await self.async_save()
        return self.get_zone(zone_name)

    async def async_remove_zone(self, zone_name: str) -> None:
        self._data["zones"].pop(zone_name, None)
        await self.async_save()

    async def async_update_zone(self, zone_name: str, **fields: object) -> StoredZone:
        """Patch a zone with one or more typed fields. Unknown keys raise."""
        if zone_name not in self._data["zones"]:
            raise KeyError(zone_name)
        zone = cast(dict[str, Any], self._data["zones"][zone_name])
        for key, value in fields.items():
            if key not in zone:
                raise KeyError(f"Unknown zone field: {key}")
            zone[key] = value
        await self.async_save()
        return self.get_zone(zone_name)

    # ----- zone schedules -----

    def get_zone_schedule(self, zone_name: str, profile_name: str) -> StoredProfileSchedule | None:
        if zone_name not in self._data["zones"]:
            raise KeyError(zone_name)
        schedule = self._data["zones"][zone_name]["schedules"].get(profile_name)
        return copy.deepcopy(schedule) if schedule is not None else None

    async def async_set_zone_schedule(
        self,
        zone_name: str,
        profile_name: str,
        baseline: list[StoredTransition],
        current: list[StoredTransition] | None = None,
    ) -> None:
        if zone_name not in self._data["zones"]:
            raise KeyError(zone_name)
        if profile_name not in self._data["profiles"]:
            raise ValueError(f"Profile {profile_name!r} does not exist")
        # Independent baseline/current lists so caller mutation doesn't alias.
        persisted: StoredProfileSchedule = {
            "baseline": copy.deepcopy(baseline),
            "current": copy.deepcopy(current) if current is not None else copy.deepcopy(baseline),
        }
        self._data["zones"][zone_name]["schedules"][profile_name] = persisted
        await self.async_save()
        # The sibling SIGNAL_ACTIVE_PROFILE_CHANGED is fired from ProfileRegistry
        # (a wrapper) to keep the store notification-unaware. There is no
        # analogous wrapper for schedule writes — services.py mutates the store
        # directly — so firing here covers every call site without adding an
        # empty pass-through layer. Separate deep-copy keeps listener mutations
        # from aliasing in-memory state.
        async_dispatcher_send(
            self._hass,
            SIGNAL_ZONE_SCHEDULE_CHANGED,
            zone_name,
            profile_name,
            copy.deepcopy(persisted),
        )

    # ----- profiles -----

    def list_profiles(self) -> list[str]:
        return sorted(self._data["profiles"].keys())

    def get_profile(self, name: str) -> StoredProfile:
        if name not in self._data["profiles"]:
            raise KeyError(name)
        return copy.deepcopy(self._data["profiles"][name])

    @property
    def active_profile(self) -> str:
        return self._data["active_profile"]

    @property
    def default_profile(self) -> str:
        return self._data["default_profile"]

    async def async_set_active_profile(self, name: str) -> None:
        if name not in self._data["profiles"]:
            raise ValueError(f"Profile {name!r} does not exist")
        self._data["active_profile"] = name
        await self.async_save()

    async def async_add_profile(self, name: str, description: str = "") -> None:
        if name in self._data["profiles"]:
            raise ValueError(f"Profile {name!r} already exists")
        if len(self._data["profiles"]) >= MAX_PROFILES:
            raise ValueError(
                f"Cannot create more than {MAX_PROFILES} profiles "
                f"(currently {len(self._data['profiles'])})"
            )
        self._data["profiles"][name] = {"name": name, "description": description}
        await self.async_save()

    async def async_clone_profile(
        self, source: str, target: str, description: str = ""
    ) -> None:
        if source not in self._data["profiles"]:
            raise KeyError(source)
        if target in self._data["profiles"]:
            raise ValueError(f"Profile {target!r} already exists")
        if len(self._data["profiles"]) >= MAX_PROFILES:
            raise ValueError(
                f"Cannot create more than {MAX_PROFILES} profiles "
                f"(currently {len(self._data['profiles'])})"
            )
        self._data["profiles"][target] = {"name": target, "description": description}
        for zone in self._data["zones"].values():
            src_schedule = zone["schedules"].get(source)
            if src_schedule is not None:
                zone["schedules"][target] = copy.deepcopy(src_schedule)
        await self.async_save()

    async def async_rename_profile(self, old: str, new: str) -> None:
        if old not in self._data["profiles"]:
            raise KeyError(old)
        if old == new:
            return
        if new in self._data["profiles"]:
            raise ValueError(f"Profile {new!r} already exists")
        profile = self._data["profiles"].pop(old)
        profile["name"] = new
        self._data["profiles"][new] = profile
        for zone in self._data["zones"].values():
            if old in zone["schedules"]:
                zone["schedules"][new] = zone["schedules"].pop(old)
        if self._data["active_profile"] == old:
            self._data["active_profile"] = new
        if self._data["default_profile"] == old:
            self._data["default_profile"] = new
        await self.async_save()

    async def async_remove_profile(self, name: str) -> None:
        # `default_profile` is the rename-aware fallback target. It moves with
        # renames, so this check works whether or not "home" still exists.
        if name == self._data["default_profile"]:
            raise ValueError(f"Cannot delete the default profile {name!r}")
        if name not in self._data["profiles"]:
            return
        del self._data["profiles"][name]
        # Clean up orphan per-zone schedules — otherwise dead-weight on disk.
        for zone in self._data["zones"].values():
            zone["schedules"].pop(name, None)
        if self._data["active_profile"] == name:
            self._data["active_profile"] = self._data["default_profile"]
        await self.async_save()
