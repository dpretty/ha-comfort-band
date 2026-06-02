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
from typing import Any, NotRequired, TypedDict, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

__all__ = [
    "ComfortBandStore",
    "SerializedSample",
    "StoredData",
    "StoredProfile",
    "StoredProfileSchedule",
    "StoredSharedSchedule",
    "StoredTransition",
    "StoredZone",
]

from .const import (
    BUILTIN_PROFILES,
    DEFAULT_BAND_RAMP_MINUTES,
    DEFAULT_CROSS_MODE_MIN_MINUTES,
    DEFAULT_DEADBAND_ABOVE,
    DEFAULT_DEADBAND_BELOW,
    DEFAULT_LOOKAHEAD_MINUTES,
    DEFAULT_MIN_CYCLE_MINUTES,
    DEFAULT_MPC_HORIZON_MINUTES,
    DEFAULT_OVERRIDE_HOURS,
    DEFAULT_PASSIVE_TOLERANCE_C,
    DEFAULT_PROFILE,
    MAX_PROFILES,
    MAX_SHARED_SCHEDULES,
    OWN_SCHEDULE_LABEL,
    SIGNAL_SHARED_SCHEDULE_CHANGED,
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


class StoredSharedSchedule(TypedDict):
    """v0.14.0: a store-level, profile-aware schedule that any number of zones
    can be assigned to (via `StoredZone.schedule_id`) so grouped rooms target
    the same band and don't fight.

    `name` is the user-facing label (mutable, unique case-insensitively). The
    dict KEY in `StoredData.shared_schedules` is a stable slug id *divorced*
    from the name, so rename is a pure `name` update with zero zone-pointer
    fan-out. `schedules` is byte-identical to a zone's own `schedules`
    (per-profile), so home/away still differ for assigned rooms.
    """

    name: str
    schedules: dict[str, StoredProfileSchedule]


class SerializedSample(TypedDict):
    """On-disk shape for the v0.6 thermal sample buffer. Persisted in
    `StoredZone["samples"]`. The `predictor` module consumes this via
    `sample_from_dict`/`sample_to_dict`; the schema lives here so the
    storage layer doesn't depend on the prediction layer.

    `fan_mode` is captured from the climate entity's `fan_mode` attribute at
    sample time. v0.8 records but does not consume it — slope estimation
    still ignores fan_mode and produces one slope per `(action,)`. v0.9 will
    partition the slope estimator by `(action, fan_mode)` so MPC's action
    space can include per-fan-mode candidates. `NotRequired` keeps v0.7
    payloads schema-valid on load; `sample_from_dict` treats a missing key
    as `None`.
    """

    t: str  # ISO-8601 UTC
    temp: float
    action: str
    fan_mode: NotRequired[str | None]


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
    # Dwell between heat↔cool flips. Default tracks `min_cycle_minutes` at
    # zone creation but is independently tunable. Set to 0 to restore the
    # pre-v0.5 "mode flips fire immediately" behaviour.
    cross_mode_min_minutes: int
    # The action that was current immediately before `last_action`. Needed
    # to detect cross-mode flips that go through idle (heat → idle → cool),
    # since by the time the coordinator sees the flip-to-cool, the persisted
    # `last_action` is already `idle`.
    previous_action: str | None
    # Rolling sample buffer for the v0.6 predictive controller. Capped to
    # ~90 minutes of refreshes (see SAMPLE_WINDOW_MINUTES + SAMPLE_MAX_COUNT
    # in const.py). Survives restart so the slope estimator doesn't need to
    # warm up from scratch.
    samples: list[SerializedSample]
    # Horizon over which the predictor projects the current slope forward.
    # Exposed as `number.{zone}_lookahead_minutes`. Default 5; range [2, 15].
    lookahead_minutes: int
    # Comfort floor for the v0.7 passive drift acceptance. When the room is
    # outside the band but the predictor's slope says we'll return within
    # `lookahead_minutes`, the predictor suppresses heat / cool -- but only
    # while the deviation is within `passive_tolerance` °C of the band edge.
    # 0.0 disables the feature (predictor always defers to hysteresis when
    # the room is already outside the band). Exposed as
    # `number.{zone}_passive_tolerance`. Default 0.5; range [0.0, 2.0].
    passive_tolerance: float
    # Gates the v0.8 model-predictive controller. When ON *and* learning is
    # also ON *and* MPC has `idle_slope` plus at least one recovery slope
    # (v0.8.1 relaxed gate; v0.8.0 required all three), `mpc.plan` replaces
    # `predictor.decide` as the source of the final decision. Default OFF
    # so existing v0.7 users see no behaviour change on upgrade;
    # `sensor.{zone}_mpc_action` populates regardless so shadow-comparison
    # is possible without flipping the gate.
    mpc_enabled: bool
    # Horizon over which `mpc.simulate` projects each candidate action's
    # outcome (minutes). Exposed as `number.{zone}_mpc_horizon_minutes`.
    # Default 60 (v0.9.0+, was 20 in v0.8.x); range [10, 60]. Decoupled
    # from `lookahead_minutes` (used
    # by the predictor for single-decision anticipation): MPC scores whole
    # cycles, predictor scores the next decision moment.
    mpc_horizon_minutes: int
    # Schedule-transition smoothing window (v0.10.0+). When 0 (default),
    # band transitions are instant steps — the v0.9.x behaviour. When
    # > 0, the (low, high) band edges interpolate linearly within
    # ±ramp/2 of each transition's time, so a 4 °C overnight setback
    # rise at 06:00 becomes a 30-minute shoulder (05:45-06:15) instead
    # of a wall. Implemented in `schedule.resolve` /
    # `schedule.upcoming_bands` so MPC / predictor / hysteresis all see
    # the smoothed band naturally. Exposed as
    # `number.{zone}_band_ramp_minutes`, range [0, 120].
    band_ramp_minutes: int
    enabled: bool
    # Gates the v0.6 predictive controller: when ON, `predictor.decide()`'s
    # anticipated action replaces `hysteresis.decide()`'s reactive one as the
    # final decision forwarded to climate. Default OFF; predicted_action
    # sensor populates regardless so users can shadow-compare first.
    learning_enabled: bool
    # When True, hysteresis decisions consume the *apparent* temperature
    # instead of the raw room reading. Falls back to room temp automatically
    # when no humidity reading is available.
    use_apparent_temperature: bool
    last_action_at: str | None
    last_action: str | None
    # v0.12.0: last good idle (passive heat-loss) slope, persisted across the
    # 90-min sample window so MPC readiness survives a long heating chase when
    # the live window can only produce short idle blips. °C/min, signed
    # (negative = cooling toward ambient). None = none learned yet.
    # `persisted_idle_slope_at` is the ISO-8601 UTC timestamp of that slope,
    # used to expire it after `PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES`.
    persisted_idle_slope: float | None
    persisted_idle_slope_at: str | None
    # v0.13.0: deterministic fan-boost (opt-in, per zone). When
    # `fan_control_enabled` is True, the coordinator commands the climate's
    # fan mode by action — `active_fan_mode` while heating/cooling,
    # `idle_fan_mode` while idle (fan-only). Stored as the fan-mode *string*
    # (e.g. "high" / "4"), chosen from the climate's live `fan_modes`, so a
    # unit re-ordering its modes can't silently change the level. None = don't
    # command that side (so enabling does nothing until the user picks, and an
    # idle-only config is possible). One shared "active" fan covers both heat
    # and cool; separate heat/cool fans are a future refinement.
    fan_control_enabled: bool
    active_fan_mode: str | None
    idle_fan_mode: str | None
    # v0.14.0: when non-None, this zone resolves its scheduled band from the
    # shared schedule with this id (StoredData.shared_schedules) instead of its
    # own `schedules` above. None = "Own schedule". A dangling id (shared
    # schedule deleted) falls back safely to the own schedule at resolution
    # time. The zone's own `schedules` stays dormant while assigned and is
    # restored losslessly on unassign.
    schedule_id: str | None


class StoredProfile(TypedDict):
    name: str
    description: str


class StoredData(TypedDict):
    zones: dict[str, StoredZone]
    profiles: dict[str, StoredProfile]
    # v0.14.0 named shared schedules, keyed by stable slug id. A zone with a
    # non-None `schedule_id` resolves its band from the matching entry here
    # instead of its own `schedules` (see `coordinator._async_update_data`).
    shared_schedules: dict[str, StoredSharedSchedule]
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
        "shared_schedules": {},
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
        "cross_mode_min_minutes": DEFAULT_CROSS_MODE_MIN_MINUTES,
        "previous_action": None,
        "samples": [],
        "lookahead_minutes": DEFAULT_LOOKAHEAD_MINUTES,
        "passive_tolerance": DEFAULT_PASSIVE_TOLERANCE_C,
        "mpc_enabled": False,
        "mpc_horizon_minutes": DEFAULT_MPC_HORIZON_MINUTES,
        "band_ramp_minutes": DEFAULT_BAND_RAMP_MINUTES,
        "enabled": False,
        "learning_enabled": False,
        "use_apparent_temperature": False,
        "last_action_at": None,
        "last_action": None,
        "persisted_idle_slope": None,
        "persisted_idle_slope_at": None,
        "fan_control_enabled": False,
        "active_fan_mode": None,
        "idle_fan_mode": None,
        "schedule_id": None,
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
        # Defensive: if active_profile points at a profile that no longer
        # exists (corruption, hand-edited .storage), fall back to the default
        # so subsequent `extra_state_attributes` / coordinator reads don't
        # raise KeyError on every state push.
        if raw.get("active_profile") not in raw.get("profiles", {}):
            raw["active_profile"] = raw["default_profile"]
            migrated = True
        # v0.13 → v0.14: named shared schedules. Top-level collection, default
        # empty so existing installs see no change. Presence-keyed like the
        # per-zone fields below.
        if "shared_schedules" not in raw:
            raw["shared_schedules"] = {}
            migrated = True
        # Per-zone backfill: every new field added since v0.3 gets a safe
        # default if absent. Each `if "field" not in zone` branch is
        # independent so a single load can migrate v0.3 → v0.8 in one pass.
        #
        # STORAGE_VERSION intentionally stays at 1: field additions with
        # safe defaults are forward-compatible (old payload + missing
        # field → backfill yields a valid new payload). A version bump
        # would force an async_migrate_func code path that we don't need
        # — the in-place defaulting here covers every load.
        for zone in raw.get("zones", {}).values():
            if "learning_enabled" not in zone:
                zone["learning_enabled"] = False
                migrated = True
            if "use_apparent_temperature" not in zone:
                zone["use_apparent_temperature"] = False
                migrated = True
            # v0.4 → v0.5: default the cross-mode dwell to the zone's
            # existing min_cycle_minutes so users who tuned same-mode dwell
            # carry that preference into cross-mode automatically. Falls
            # back to DEFAULT_MIN_CYCLE_MINUTES only if min_cycle_minutes
            # is itself somehow absent (paranoid).
            if "cross_mode_min_minutes" not in zone:
                zone["cross_mode_min_minutes"] = zone.get(
                    "min_cycle_minutes", DEFAULT_MIN_CYCLE_MINUTES
                )
                migrated = True
            if "previous_action" not in zone:
                zone["previous_action"] = None
                migrated = True
            # v0.5 → v0.6: predictive control. Buffer starts empty (warms up
            # over the first ~90 min of refreshes); lookahead seeds to the
            # conservative default. Both safe to backfill on every load.
            if "samples" not in zone:
                zone["samples"] = []
                migrated = True
            if "lookahead_minutes" not in zone:
                zone["lookahead_minutes"] = DEFAULT_LOOKAHEAD_MINUTES
                migrated = True
            # v0.6 → v0.7: passive drift acceptance. Conservative 0.5 °C floor
            # so first-ship behaviour suppresses only marginal hysteresis trips;
            # users can widen via `number.{zone}_passive_tolerance` or disable
            # by setting it to 0.
            if "passive_tolerance" not in zone:
                zone["passive_tolerance"] = DEFAULT_PASSIVE_TOLERANCE_C
                migrated = True
            # v0.7 → v0.8: MPC. Default OFF so existing learning_enabled users
            # continue to see the v0.7 predictor's behaviour. Horizon seeds
            # to `DEFAULT_MPC_HORIZON_MINUTES` (60 from v0.9.0, was 20 in
            # v0.8.x); users tune via the `number.{zone}_mpc_horizon_minutes`
            # entity. Backfill is presence-keyed (`not in zone`) so explicit
            # values from older versions are preserved. Sample-level fan_mode
            # backfill is implicit via NotRequired + sample_from_dict.get().
            if "mpc_enabled" not in zone:
                zone["mpc_enabled"] = False
                migrated = True
            if "mpc_horizon_minutes" not in zone:
                zone["mpc_horizon_minutes"] = DEFAULT_MPC_HORIZON_MINUTES
                migrated = True
            # v0.9 → v0.10: band-ramp smoothing. Default 0 (stepped
            # transitions — current behaviour) so existing users see no
            # change on upgrade. Users opt in by bumping the value via
            # `number.{zone}_band_ramp_minutes`. Presence-keyed backfill
            # preserves any explicit value if a user-edited store ships
            # the key already.
            if "band_ramp_minutes" not in zone:
                zone["band_ramp_minutes"] = DEFAULT_BAND_RAMP_MINUTES
                migrated = True
            # v0.11 → v0.12: persisted idle slope. Default None/None so a
            # freshly-upgraded zone learns its idle slope from live samples
            # before any cached substitution kicks in. The integration always
            # writes the two keys together, but each is backfilled
            # independently so a hand-edited / partially-written store with
            # only one key present can't leave the other absent — the
            # coordinator subscripts both keys directly, so a missing one would
            # raise KeyError and fail the whole refresh.
            if "persisted_idle_slope" not in zone:
                zone["persisted_idle_slope"] = None
                migrated = True
            if "persisted_idle_slope_at" not in zone:
                zone["persisted_idle_slope_at"] = None
                migrated = True
            # v0.12 → v0.13: deterministic fan-boost. Default OFF / unset so
            # existing zones see no behaviour change on upgrade — the feature
            # does nothing until the user enables it and picks fan modes.
            # Backfilled independently (each key on its own check) for the same
            # corrupt-store robustness as the persisted-idle-slope keys above.
            if "fan_control_enabled" not in zone:
                zone["fan_control_enabled"] = False
                migrated = True
            if "active_fan_mode" not in zone:
                zone["active_fan_mode"] = None
                migrated = True
            if "idle_fan_mode" not in zone:
                zone["idle_fan_mode"] = None
                migrated = True
            # v0.13 → v0.14: shared-schedule assignment. Default None ("Own
            # schedule") so existing zones keep resolving their own schedules.
            if "schedule_id" not in zone:
                zone["schedule_id"] = None
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

    async def async_clone_profile(self, source: str, target: str, description: str = "") -> None:
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
            # Consistent with the sibling mutators (clone/rename) which both
            # raise KeyError on unknown names. ProfileRegistry catches and
            # re-raises as ServiceValidationError for the service layer.
            raise KeyError(name)
        del self._data["profiles"][name]
        # Clean up orphan per-zone schedules — otherwise dead-weight on disk.
        for zone in self._data["zones"].values():
            zone["schedules"].pop(name, None)
        if self._data["active_profile"] == name:
            self._data["active_profile"] = self._data["default_profile"]
        await self.async_save()

    # ----- shared schedules (v0.14.0) -----

    def list_shared_schedule_ids(self) -> list[str]:
        return sorted(self._data["shared_schedules"].keys())

    def has_shared_schedule(self, shared_id: str) -> bool:
        return shared_id in self._data["shared_schedules"]

    def get_shared_schedule(self, shared_id: str) -> StoredSharedSchedule:
        if shared_id not in self._data["shared_schedules"]:
            raise KeyError(shared_id)
        return copy.deepcopy(self._data["shared_schedules"][shared_id])

    def get_shared_schedule_slot(
        self, shared_id: str, profile_name: str
    ) -> StoredProfileSchedule | None:
        """The `{baseline, current}` for one (shared schedule, profile), or None."""
        if shared_id not in self._data["shared_schedules"]:
            raise KeyError(shared_id)
        schedule = self._data["shared_schedules"][shared_id]["schedules"].get(profile_name)
        return copy.deepcopy(schedule) if schedule is not None else None

    def zones_using_shared_schedule(self, shared_id: str) -> list[str]:
        """Zone names currently assigned to this shared schedule (sorted)."""
        return sorted(
            name for name, zone in self._data["zones"].items() if zone["schedule_id"] == shared_id
        )

    def get_shared_schedule_name(self, shared_id: str) -> str | None:
        """The display name, or None if the id is unknown (dangling pointer)."""
        s = self._data["shared_schedules"].get(shared_id)
        return s["name"] if s is not None else None

    def shared_schedule_summaries(self) -> list[dict[str, Any]]:
        """Lightweight `[{id, name, members}]` per shared schedule (no transition
        deep-copy), sorted by name — drives the assignment select's attributes so
        the card reads everything from `hass.states`."""
        zones = self._data["zones"]
        out: list[dict[str, Any]] = [
            {
                "id": sid,
                "name": s["name"],
                "members": sorted(z for z, zone in zones.items() if zone["schedule_id"] == sid),
            }
            for sid, s in self._data["shared_schedules"].items()
        ]
        out.sort(key=lambda d: str(d["name"]).casefold())
        return out

    def _new_shared_schedule_id(self, name: str) -> str:
        """A stable, collision-free slug id for a new shared schedule. The id is
        divorced from the name (so rename never moves zone pointers)."""
        base = slugify(name) or "schedule"
        existing = self._data["shared_schedules"]
        if base not in existing:
            return base
        i = 2
        while f"{base}_{i}" in existing:
            i += 1
        return f"{base}_{i}"

    def _clean_shared_name(self, name: str) -> str:
        """Normalise + validate a shared-schedule display name: strip surrounding
        whitespace, reject blank, and reject the reserved "Own schedule" label
        (which would collide with the assignment select's unassigned sentinel)."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Shared schedule name cannot be empty")
        if cleaned.casefold() == OWN_SCHEDULE_LABEL.casefold():
            raise ValueError(f"{OWN_SCHEDULE_LABEL!r} is a reserved name")
        return cleaned

    def _shared_name_taken(self, name: str, *, exclude_id: str | None = None) -> bool:
        lowered = name.casefold()
        return any(
            sid != exclude_id and s["name"].casefold() == lowered
            for sid, s in self._data["shared_schedules"].items()
        )

    async def async_add_shared_schedule(
        self,
        name: str,
        schedules: dict[str, StoredProfileSchedule] | None = None,
    ) -> str:
        """Create a shared schedule; returns its generated id. `schedules` seeds
        the per-profile transition sets (deep-copied); None = empty."""
        name = self._clean_shared_name(name)
        if self._shared_name_taken(name):
            raise ValueError(f"A shared schedule named {name!r} already exists")
        if len(self._data["shared_schedules"]) >= MAX_SHARED_SCHEDULES:
            raise ValueError(
                f"Cannot create more than {MAX_SHARED_SCHEDULES} shared schedules "
                f"(currently {len(self._data['shared_schedules'])})"
            )
        shared_id = self._new_shared_schedule_id(name)
        self._data["shared_schedules"][shared_id] = {
            "name": name,
            "schedules": copy.deepcopy(schedules) if schedules is not None else {},
        }
        await self.async_save()
        return shared_id

    async def async_rename_shared_schedule(self, shared_id: str, new_name: str) -> None:
        if shared_id not in self._data["shared_schedules"]:
            raise KeyError(shared_id)
        new_name = self._clean_shared_name(new_name)
        if self._shared_name_taken(new_name, exclude_id=shared_id):
            raise ValueError(f"A shared schedule named {new_name!r} already exists")
        # id is stable + divorced from name, so this is a pure `name` update —
        # no zone `schedule_id` pointers move (unlike profile rename).
        self._data["shared_schedules"][shared_id]["name"] = new_name
        await self.async_save()

    async def async_remove_shared_schedule(self, shared_id: str) -> list[str]:
        """Delete a shared schedule and unassign every zone that referenced it
        (back to "Own schedule"). Returns the affected zone names so the caller
        can refresh their coordinators."""
        if shared_id not in self._data["shared_schedules"]:
            raise KeyError(shared_id)
        affected = self.zones_using_shared_schedule(shared_id)
        for name in affected:
            self._data["zones"][name]["schedule_id"] = None
        del self._data["shared_schedules"][shared_id]
        await self.async_save()
        return affected

    async def async_set_shared_schedule(
        self,
        shared_id: str,
        profile_name: str,
        baseline: list[StoredTransition],
        current: list[StoredTransition] | None = None,
    ) -> None:
        """Write the `{baseline, current}` for one (shared schedule, profile)
        and fire SIGNAL_SHARED_SCHEDULE_CHANGED so the WS layer pushes to every
        subscriber of this id. Mirrors `async_set_zone_schedule`."""
        if shared_id not in self._data["shared_schedules"]:
            raise KeyError(shared_id)
        if profile_name not in self._data["profiles"]:
            raise ValueError(f"Profile {profile_name!r} does not exist")
        persisted: StoredProfileSchedule = {
            "baseline": copy.deepcopy(baseline),
            "current": copy.deepcopy(current) if current is not None else copy.deepcopy(baseline),
        }
        self._data["shared_schedules"][shared_id]["schedules"][profile_name] = persisted
        await self.async_save()
        async_dispatcher_send(
            self._hass,
            SIGNAL_SHARED_SCHEDULE_CHANGED,
            shared_id,
            profile_name,
            copy.deepcopy(persisted),
        )

    async def async_set_zone_schedule_id(self, zone_name: str, shared_id: str | None) -> None:
        """Assign a zone to a shared schedule (or None = "Own schedule").
        Validates the id exists."""
        if zone_name not in self._data["zones"]:
            raise KeyError(zone_name)
        if shared_id is not None and shared_id not in self._data["shared_schedules"]:
            raise ValueError(f"Shared schedule {shared_id!r} does not exist")
        self._data["zones"][zone_name]["schedule_id"] = shared_id
        await self.async_save()
