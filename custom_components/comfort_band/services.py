"""Service registration.

Schedule mutators (set/add/update/remove transition), override control
(start/cancel), legacy importer, profile switch, profile CRUD
(create/clone/rename/delete), and (v0.14.0) shared-schedule CRUD +
assignment (create/rename/delete_shared_schedule, assign_schedule). All
zone-scoped services are keyed by zone *slug* (not entity_id) so the call
site matches the storage key. Schemas use voluptuous + entity selectors so
Developer Tools renders sensible UI.

The schedule mutators target EITHER a zone's own schedule (`zone`) or a shared
schedule (`shared_id`) — exactly one of the two — via `_ScheduleTarget`; the
`zone` form is unchanged for back-compat. All run a normalize pass before
persisting and raise ServiceValidationError on malformed input rather than
letting the user write garbage to the store. CRUD handlers wrap (KeyError,
ValueError) from the registry/store as ServiceValidationError so users see a
consistent error class regardless of which layer rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN, FEEDBACK_LABELS, LOGGER
from .feedback import FeedbackEntry
from .legacy import read_legacy_hourly_schedule
from .schedule import (
    Transition,
    normalize_schedule,
    schedule_from_dict,
    schedule_to_dict,
)

if TYPE_CHECKING:
    from . import ComfortBandData
    from .coordinator import ZoneCoordinator
    from .storage import ComfortBandStore, StoredProfileSchedule, StoredTransition


SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_ADD_TRANSITION = "add_transition"
SERVICE_UPDATE_TRANSITION = "update_transition"
SERVICE_REMOVE_TRANSITION = "remove_transition"
SERVICE_START_OVERRIDE = "start_override"
SERVICE_CANCEL_OVERRIDE = "cancel_override"
SERVICE_SET_PROFILE = "set_profile"
SERVICE_IMPORT_LEGACY = "import_legacy"
SERVICE_CREATE_PROFILE = "create_profile"
SERVICE_CLONE_PROFILE = "clone_profile"
SERVICE_RENAME_PROFILE = "rename_profile"
SERVICE_DELETE_PROFILE = "delete_profile"
SERVICE_RECORD_FEEDBACK = "record_feedback"
SERVICE_CREATE_SHARED_SCHEDULE = "create_shared_schedule"
SERVICE_RENAME_SHARED_SCHEDULE = "rename_shared_schedule"
SERVICE_DELETE_SHARED_SCHEDULE = "delete_shared_schedule"
SERVICE_ASSIGN_SCHEDULE = "assign_schedule"

# HH:MM, hours 00-23 only. The previous `[0-2]\d` admitted 24:00-29:59, which
# passed the schema then raised a raw ValueError in time.fromisoformat.
_TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

_TRANSITION_SCHEMA = vol.Schema(
    {
        vol.Required("at"): vol.Match(_TIME_RE),
        vol.Required("low"): vol.Coerce(float),
        vol.Required("high"): vol.Coerce(float),
    }
)

# v0.14.0: schedule mutators target EITHER a zone's own schedule (`zone`) OR a
# shared schedule (`shared_id`). Both are Optional in the schema; the handler
# enforces "exactly one of" (voluptuous can't express it cleanly). Keeping
# `zone` as a top-level field preserves back-compat with the pre-v0.14 card and
# every existing call.
_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("zone"): cv.string,
        vol.Optional("shared_id"): cv.string,
        vol.Required("profile"): cv.string,
        vol.Required("transitions"): vol.All(cv.ensure_list, [_TRANSITION_SCHEMA]),
    }
)

_TRANSITION_KEY_SCHEMA = vol.Schema(
    {
        vol.Optional("zone"): cv.string,
        vol.Optional("shared_id"): cv.string,
        vol.Required("profile"): cv.string,
        vol.Required("at"): vol.Match(_TIME_RE),
        vol.Required("low"): vol.Coerce(float),
        vol.Required("high"): vol.Coerce(float),
    }
)

_REMOVE_TRANSITION_SCHEMA = vol.Schema(
    {
        vol.Optional("zone"): cv.string,
        vol.Optional("shared_id"): cv.string,
        vol.Required("profile"): cv.string,
        vol.Required("at"): vol.Match(_TIME_RE),
    }
)

_START_OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
        vol.Optional("low"): vol.Coerce(float),
        vol.Optional("high"): vol.Coerce(float),
        vol.Optional("hours"): vol.Coerce(float),
    }
)

_CANCEL_OVERRIDE_SCHEMA = vol.Schema({vol.Required("zone"): cv.string})

_RECORD_FEEDBACK_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
        vol.Required("label"): vol.In(FEEDBACK_LABELS),
    }
)

_SET_PROFILE_SCHEMA = vol.Schema({vol.Required("profile"): cv.string})

_IMPORT_LEGACY_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
        vol.Required("source_zone_name"): cv.string,
    }
)

# Length caps prevent a malicious / accidental LAN client from bloating
# the .storage file with a multi-MB profile name. 64 chars is well over
# the human-readable range; 256 for descriptions allows a sentence.
_PROFILE_NAME = vol.All(cv.string, vol.Length(min=1, max=64))
_PROFILE_DESCRIPTION = vol.All(cv.string, vol.Length(max=256))

_CREATE_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _PROFILE_NAME,
        vol.Optional("description", default=""): _PROFILE_DESCRIPTION,
    }
)

_CLONE_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("source"): _PROFILE_NAME,
        vol.Required("target"): _PROFILE_NAME,
        vol.Optional("description", default=""): _PROFILE_DESCRIPTION,
    }
)

_RENAME_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("old"): _PROFILE_NAME,
        vol.Required("new"): _PROFILE_NAME,
    }
)

_DELETE_PROFILE_SCHEMA = vol.Schema({vol.Required("name"): _PROFILE_NAME})

# v0.14.0 named shared schedules. `name` reuses the profile-name cap.
_CREATE_SHARED_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _PROFILE_NAME,
        # Optional: deep-copy this zone's own schedules (all profiles) as the
        # new shared schedule's starting point.
        vol.Optional("seed_from_zone"): cv.string,
    }
)

_RENAME_SHARED_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("shared_id"): cv.string,
        vol.Required("name"): _PROFILE_NAME,
    }
)

_DELETE_SHARED_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("shared_id"): cv.string,
        # Refuse-by-default if any zone is assigned; cascade unassigns them.
        vol.Optional("cascade", default=False): cv.boolean,
    }
)

_ASSIGN_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
        # Omit or pass null to clear back to "Own schedule".
        vol.Optional("shared_id"): vol.Any(cv.string, None),
    }
)


def _data(hass: HomeAssistant) -> ComfortBandData:
    return hass.data[DOMAIN]  # type: ignore[no-any-return]


def _store(hass: HomeAssistant) -> ComfortBandStore:
    return _data(hass).store


def _coordinator(hass: HomeAssistant, zone_name: str) -> ZoneCoordinator:
    data = _data(hass)
    entry_id = data.zone_slug_to_entry_id.get(zone_name)
    if entry_id is None:
        raise ServiceValidationError(f"Unknown zone: {zone_name!r}")
    coordinator = data.zone_coordinators.get(entry_id)
    if coordinator is None:
        raise ServiceValidationError(f"Zone {zone_name!r} has no live coordinator")
    return coordinator


@dataclass(frozen=True)
class _ScheduleTarget:
    """A validated schedule-mutation target: a zone's own schedule OR a shared
    schedule, for one profile. Exactly one of `zone`/`shared_id` is set."""

    profile: str
    zone: str | None = None
    shared_id: str | None = None


def _target_from_call(hass: HomeAssistant, call: ServiceCall) -> _ScheduleTarget:
    """Validate the (zone | shared_id) + profile of a schedule-mutation call."""
    zone = call.data.get("zone")
    shared_id = call.data.get("shared_id")
    profile = call.data["profile"]
    if (zone is None) == (shared_id is None):
        raise ServiceValidationError("Provide exactly one of 'zone' or 'shared_id'")
    store = _store(hass)
    if profile not in store.list_profiles():
        raise ServiceValidationError(f"Unknown profile: {profile!r}")
    if zone is not None:
        if not store.has_zone(zone):
            raise ServiceValidationError(f"Unknown zone: {zone!r}")
        return _ScheduleTarget(profile=profile, zone=zone)
    assert shared_id is not None  # narrowed by the exactly-one check above
    if not store.has_shared_schedule(shared_id):
        raise ServiceValidationError(f"Unknown shared schedule: {shared_id!r}")
    return _ScheduleTarget(profile=profile, shared_id=shared_id)


def _target_label(target: _ScheduleTarget) -> str:
    if target.zone is not None:
        return f"zone {target.zone!r} / profile {target.profile!r}"
    return f"shared schedule {target.shared_id!r} / profile {target.profile!r}"


def _target_get(hass: HomeAssistant, target: _ScheduleTarget) -> StoredProfileSchedule | None:
    store = _store(hass)
    if target.zone is not None:
        return store.get_zone_schedule(target.zone, target.profile)
    assert target.shared_id is not None  # narrowed by _target_from_call
    return store.get_shared_schedule_slot(target.shared_id, target.profile)


async def _target_set(
    hass: HomeAssistant, target: _ScheduleTarget, transitions: list[StoredTransition]
) -> None:
    """Persist the transitions for the target and refresh affected coordinators.
    For a shared schedule, refreshes EVERY assigned zone (the store already
    fired SIGNAL_SHARED_SCHEDULE_CHANGED for the websocket push)."""
    store = _store(hass)
    if target.zone is not None:
        await store.async_set_zone_schedule(target.zone, target.profile, transitions)
        await _refresh_zone_if_active(hass, target.zone)
        return
    assert target.shared_id is not None  # narrowed by _target_from_call
    await store.async_set_shared_schedule(target.shared_id, target.profile, transitions)
    for zone_name in store.zones_using_shared_schedule(target.shared_id):
        await _refresh_zone_if_active(hass, zone_name)


def _parse_time(value: str) -> time:
    """Parse an HH:MM `at` value, surfacing a clean ServiceValidationError rather
    than a raw ValueError. The schema's `_TIME_RE` already rejects bad values;
    this is defence-in-depth so any that slip through degrade gracefully."""
    try:
        return time.fromisoformat(value)
    except ValueError as err:
        raise ServiceValidationError(
            f"Invalid time {value!r}; expected HH:MM (00:00-23:59)"
        ) from err


def _to_transitions(raw: list[dict[str, Any]]) -> list[Transition]:
    parsed = [
        Transition(
            at=_parse_time(item["at"]),
            low=float(item["low"]),
            high=float(item["high"]),
        )
        for item in raw
    ]
    return normalize_schedule(parsed)


def _to_stored(transitions: list[Transition]) -> list[StoredTransition]:
    return schedule_to_dict(transitions)  # type: ignore[return-value]


async def _refresh_zone_if_active(hass: HomeAssistant, zone_name: str) -> None:
    """Best-effort refresh after a mutation; silently no-op if zone isn't loaded."""
    data = _data(hass)
    entry_id = data.zone_slug_to_entry_id.get(zone_name)
    if entry_id is None:
        return
    coordinator = data.zone_coordinators.get(entry_id)
    if coordinator is not None:
        await coordinator.async_refresh()


async def async_register_services(hass: HomeAssistant) -> None:
    """Idempotently register all comfort_band services."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    async def _set_schedule(call: ServiceCall) -> None:
        target = _target_from_call(hass, call)
        transitions = _to_transitions(call.data["transitions"])
        await _target_set(hass, target, _to_stored(transitions))

    async def _add_transition(call: ServiceCall) -> None:
        target = _target_from_call(hass, call)
        new_transition = Transition(
            at=_parse_time(call.data["at"]),
            low=float(call.data["low"]),
            high=float(call.data["high"]),
        )
        existing = _target_get(hass, target)
        existing_list = schedule_from_dict(existing["current"]) if existing else []
        merged = normalize_schedule([*existing_list, new_transition])
        await _target_set(hass, target, _to_stored(merged))

    async def _update_transition(call: ServiceCall) -> None:
        target = _target_from_call(hass, call)
        at = _parse_time(call.data["at"])
        existing = _target_get(hass, target)
        if existing is None:
            raise ServiceValidationError(f"{_target_label(target)} has no schedule yet")
        existing_list = schedule_from_dict(existing["current"])
        if not any(t.at == at for t in existing_list):
            raise ServiceValidationError(
                f"No transition at {at.isoformat()} in {_target_label(target)}"
            )
        replacement = Transition(
            at=at,
            low=float(call.data["low"]),
            high=float(call.data["high"]),
        )
        merged = normalize_schedule([t for t in existing_list if t.at != at] + [replacement])
        await _target_set(hass, target, _to_stored(merged))

    async def _remove_transition(call: ServiceCall) -> None:
        target = _target_from_call(hass, call)
        at = _parse_time(call.data["at"])
        existing = _target_get(hass, target)
        if existing is None:
            raise ServiceValidationError(f"{_target_label(target)} has no schedule yet")
        existing_list = schedule_from_dict(existing["current"])
        remaining = [t for t in existing_list if t.at != at]
        if len(remaining) == len(existing_list):
            raise ServiceValidationError(
                f"No transition at {at.isoformat()} in {_target_label(target)}"
            )
        await _target_set(hass, target, _to_stored(remaining))

    async def _start_override(call: ServiceCall) -> None:
        zone_name = call.data["zone"]
        coordinator = _coordinator(hass, zone_name)
        await coordinator.async_start_override(
            low=call.data.get("low"),
            high=call.data.get("high"),
            hours=call.data.get("hours"),
        )

    async def _cancel_override(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call.data["zone"])
        await coordinator.async_cancel_override()

    async def _record_feedback(call: ServiceCall) -> None:
        # Append a comfort-feedback data point enriched with the band/action
        # in effect right now, so the v3 learning loop can correlate without
        # re-deriving context. Routed through `_coordinator` for the same
        # unknown-zone ServiceValidationError every other service raises; a
        # resolved coordinator has always completed its first refresh, so
        # `.data` is populated (it's only None pre-first-refresh, before the
        # zone is registered in `zone_coordinators`).
        zone_name = call.data["zone"]
        coordinator = _coordinator(hass, zone_name)
        state = coordinator.data
        entry: FeedbackEntry = {
            "zone": zone_name,
            "timestamp": dt_util.utcnow().isoformat(),
            "label": call.data["label"],
            "room_temp": state.room,
            "low": state.effective_low,
            "high": state.effective_high,
            "action": state.decision.action,
        }
        await _data(hass).feedback_store.async_append(entry)

    async def _set_profile(call: ServiceCall) -> None:
        await _data(hass).profile_registry.async_set_active(call.data["profile"])

    async def _create_profile(call: ServiceCall) -> None:
        name = call.data["name"].strip()
        if not name:
            raise ServiceValidationError("Profile name cannot be empty")
        try:
            await _data(hass).profile_registry.async_create(name, call.data["description"])
        except (KeyError, ValueError) as err:
            # async_add_profile only raises ValueError today; broad catch
            # mirrors the other three CRUD handlers and is defensive
            # against future storage changes.
            raise ServiceValidationError(str(err)) from err

    async def _clone_profile(call: ServiceCall) -> None:
        source = call.data["source"].strip()
        target = call.data["target"].strip()
        if not source:
            raise ServiceValidationError("Source profile name cannot be empty")
        if not target:
            raise ServiceValidationError("Target profile name cannot be empty")
        try:
            await _data(hass).profile_registry.async_clone(source, target, call.data["description"])
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def _rename_profile(call: ServiceCall) -> None:
        old = call.data["old"].strip()
        new = call.data["new"].strip()
        if not old:
            raise ServiceValidationError("Old profile name cannot be empty")
        if not new:
            raise ServiceValidationError("New profile name cannot be empty")
        try:
            await _data(hass).profile_registry.async_rename(old, new)
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def _delete_profile(call: ServiceCall) -> None:
        name = call.data["name"].strip()
        if not name:
            raise ServiceValidationError("Profile name cannot be empty")
        try:
            await _data(hass).profile_registry.async_delete(name)
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def _import_legacy(call: ServiceCall) -> None:
        zone_name = call.data["zone"]
        source = call.data["source_zone_name"]
        store = _store(hass)
        if not store.has_zone(zone_name):
            raise ServiceValidationError(f"Unknown zone: {zone_name!r}")
        try:
            transitions = read_legacy_hourly_schedule(hass, source)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        # Importer writes to the default profile (originally "home"; tracks
        # renames so this still works after the user renames home → weekday).
        default = _data(hass).profile_registry.default
        await store.async_set_zone_schedule(zone_name, default, _to_stored(transitions))
        LOGGER.info(
            "Imported legacy schedule for %s from input_number.%s_hour_*: %d transitions",
            zone_name,
            source,
            len(transitions),
        )
        await _refresh_zone_if_active(hass, zone_name)

    async def _create_shared_schedule(call: ServiceCall) -> None:
        try:
            await _data(hass).shared_schedule_registry.async_create(
                call.data["name"], call.data.get("seed_from_zone")
            )
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def _rename_shared_schedule(call: ServiceCall) -> None:
        try:
            await _data(hass).shared_schedule_registry.async_rename(
                call.data["shared_id"], call.data["name"]
            )
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    async def _delete_shared_schedule(call: ServiceCall) -> None:
        try:
            affected = await _data(hass).shared_schedule_registry.async_delete(
                call.data["shared_id"], call.data["cascade"]
            )
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err
        # Unassigned zones (cascade) changed band source -> refresh them.
        for zone_name in affected:
            await _refresh_zone_if_active(hass, zone_name)

    async def _assign_schedule(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call.data["zone"])
        try:
            await coordinator.async_set_schedule_assignment(call.data.get("shared_id"))
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=_SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_TRANSITION, _add_transition, schema=_TRANSITION_KEY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_TRANSITION,
        _update_transition,
        schema=_TRANSITION_KEY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_TRANSITION,
        _remove_transition,
        schema=_REMOVE_TRANSITION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_OVERRIDE, _start_override, schema=_START_OVERRIDE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_OVERRIDE,
        _cancel_override,
        schema=_CANCEL_OVERRIDE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECORD_FEEDBACK, _record_feedback, schema=_RECORD_FEEDBACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PROFILE, _set_profile, schema=_SET_PROFILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_LEGACY, _import_legacy, schema=_IMPORT_LEGACY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CREATE_PROFILE, _create_profile, schema=_CREATE_PROFILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLONE_PROFILE, _clone_profile, schema=_CLONE_PROFILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RENAME_PROFILE, _rename_profile, schema=_RENAME_PROFILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_PROFILE, _delete_profile, schema=_DELETE_PROFILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_SHARED_SCHEDULE,
        _create_shared_schedule,
        schema=_CREATE_SHARED_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RENAME_SHARED_SCHEDULE,
        _rename_shared_schedule,
        schema=_RENAME_SHARED_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SHARED_SCHEDULE,
        _delete_shared_schedule,
        schema=_DELETE_SHARED_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ASSIGN_SCHEDULE, _assign_schedule, schema=_ASSIGN_SCHEDULE_SCHEMA
    )
