"""Service registration.

Eight services, all keyed by zone *slug* (not entity_id) so the call site
matches the storage key. Schemas use voluptuous + entity selectors so
Developer Tools renders sensible UI.

Schedule mutators all run a normalize pass before persisting; they raise
ServiceValidationError on malformed input rather than letting the user
write garbage to the store.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, LOGGER
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
    from .storage import ComfortBandStore, StoredTransition


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

_TIME_RE = r"^[0-2]\d:[0-5]\d$"

_TRANSITION_SCHEMA = vol.Schema(
    {
        vol.Required("at"): vol.Match(_TIME_RE),
        vol.Required("low"): vol.Coerce(float),
        vol.Required("high"): vol.Coerce(float),
    }
)

_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
        vol.Required("profile"): cv.string,
        vol.Required("transitions"): vol.All(cv.ensure_list, [_TRANSITION_SCHEMA]),
    }
)

_TRANSITION_KEY_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
        vol.Required("profile"): cv.string,
        vol.Required("at"): vol.Match(_TIME_RE),
        vol.Required("low"): vol.Coerce(float),
        vol.Required("high"): vol.Coerce(float),
    }
)

_REMOVE_TRANSITION_SCHEMA = vol.Schema(
    {
        vol.Required("zone"): cv.string,
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


def _validate_zone_and_profile(hass: HomeAssistant, zone_name: str, profile: str) -> None:
    store = _store(hass)
    if not store.has_zone(zone_name):
        raise ServiceValidationError(f"Unknown zone: {zone_name!r}")
    if profile not in store.list_profiles():
        raise ServiceValidationError(f"Unknown profile: {profile!r}")


def _to_transitions(raw: list[dict[str, Any]]) -> list[Transition]:
    parsed = [
        Transition(
            at=time.fromisoformat(item["at"]),
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
    """Idempotently register all 8 services."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    async def _set_schedule(call: ServiceCall) -> None:
        zone_name = call.data["zone"]
        profile = call.data["profile"]
        transitions = _to_transitions(call.data["transitions"])
        _validate_zone_and_profile(hass, zone_name, profile)
        await _store(hass).async_set_zone_schedule(zone_name, profile, _to_stored(transitions))
        await _refresh_zone_if_active(hass, zone_name)

    async def _add_transition(call: ServiceCall) -> None:
        zone_name = call.data["zone"]
        profile = call.data["profile"]
        new_transition = Transition(
            at=time.fromisoformat(call.data["at"]),
            low=float(call.data["low"]),
            high=float(call.data["high"]),
        )
        _validate_zone_and_profile(hass, zone_name, profile)
        store = _store(hass)
        existing = store.get_zone_schedule(zone_name, profile)
        existing_list = schedule_from_dict(existing["current"]) if existing else []
        merged = normalize_schedule([*existing_list, new_transition])
        await store.async_set_zone_schedule(zone_name, profile, _to_stored(merged))
        await _refresh_zone_if_active(hass, zone_name)

    async def _update_transition(call: ServiceCall) -> None:
        zone_name = call.data["zone"]
        profile = call.data["profile"]
        target = time.fromisoformat(call.data["at"])
        _validate_zone_and_profile(hass, zone_name, profile)
        store = _store(hass)
        existing = store.get_zone_schedule(zone_name, profile)
        if existing is None:
            raise ServiceValidationError(
                f"Zone {zone_name!r} profile {profile!r} has no schedule yet"
            )
        existing_list = schedule_from_dict(existing["current"])
        if not any(t.at == target for t in existing_list):
            raise ServiceValidationError(
                f"No transition at {target.isoformat()} in {zone_name}/{profile}"
            )
        replacement = Transition(
            at=target,
            low=float(call.data["low"]),
            high=float(call.data["high"]),
        )
        merged = normalize_schedule([t for t in existing_list if t.at != target] + [replacement])
        await store.async_set_zone_schedule(zone_name, profile, _to_stored(merged))
        await _refresh_zone_if_active(hass, zone_name)

    async def _remove_transition(call: ServiceCall) -> None:
        zone_name = call.data["zone"]
        profile = call.data["profile"]
        target = time.fromisoformat(call.data["at"])
        _validate_zone_and_profile(hass, zone_name, profile)
        store = _store(hass)
        existing = store.get_zone_schedule(zone_name, profile)
        if existing is None:
            raise ServiceValidationError(
                f"Zone {zone_name!r} profile {profile!r} has no schedule yet"
            )
        existing_list = schedule_from_dict(existing["current"])
        remaining = [t for t in existing_list if t.at != target]
        if len(remaining) == len(existing_list):
            raise ServiceValidationError(
                f"No transition at {target.isoformat()} in {zone_name}/{profile}"
            )
        await store.async_set_zone_schedule(zone_name, profile, _to_stored(remaining))
        await _refresh_zone_if_active(hass, zone_name)

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

    async def _set_profile(call: ServiceCall) -> None:
        await _data(hass).profile_registry.async_set_active(call.data["profile"])

    async def _create_profile(call: ServiceCall) -> None:
        name = call.data["name"].strip()
        if not name:
            raise ServiceValidationError("Profile name cannot be empty")
        try:
            await _data(hass).profile_registry.async_create(name, call.data["description"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def _clone_profile(call: ServiceCall) -> None:
        source = call.data["source"].strip()
        target = call.data["target"].strip()
        if not source:
            raise ServiceValidationError("Source profile name cannot be empty")
        if not target:
            raise ServiceValidationError("Target profile name cannot be empty")
        try:
            await _data(hass).profile_registry.async_clone(
                source, target, call.data["description"]
            )
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
        try:
            await _data(hass).profile_registry.async_delete(call.data["name"].strip())
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
