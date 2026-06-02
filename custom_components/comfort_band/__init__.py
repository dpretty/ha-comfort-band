"""Comfort Band integration entry points.

Two ConfigEntry kinds (see `entry.data["kind"]`):
  - `zone`: per-room band controller. Owns a ZoneCoordinator that watches
    the room-temp sensor and (in non-shadow mode) drives the climate entity.
  - `profile_manager`: singleton, owns the global `select.active_profile`.

Shared state lives in `hass.data[DOMAIN]` as a `ComfortBandData` dataclass:
the single Store, the ProfileRegistry that wraps it, and the per-zone
coordinators indexed by entry_id (with a slug index for service lookups).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_KIND,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    DOMAIN,
    ENTRY_KIND_PROFILE_MANAGER,
    ENTRY_KIND_ZONE,
    LOGGER,
    PLATFORMS_PROFILE_MANAGER,
    PLATFORMS_ZONE,
)
from .coordinator import ZoneCoordinator
from .feedback import FeedbackStore
from .profiles import ProfileRegistry
from .services import async_register_services
from .shared_schedules import SharedScheduleRegistry
from .storage import ComfortBandStore
from .ws import async_register_ws_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class ComfortBandData:
    """Shared per-integration state. One instance lives in `hass.data[DOMAIN]`."""

    store: ComfortBandStore
    profile_registry: ProfileRegistry
    shared_schedule_registry: SharedScheduleRegistry
    feedback_store: FeedbackStore
    zone_coordinators: dict[str, ZoneCoordinator] = field(default_factory=dict)
    zone_slug_to_entry_id: dict[str, str] = field(default_factory=dict)


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """One-shot integration setup. Cleans Session A placeholders + boots state."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if CONF_KIND not in entry.data:
            LOGGER.info("Removing Session A placeholder config entry %s", entry.entry_id)
            hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
    await _ensure_shared_data(hass)
    await async_register_services(hass)
    async_register_ws_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Branch on entry kind. Per-zone entries get a coordinator wired up."""
    kind = entry.data.get(CONF_KIND)
    if kind is None:
        # Belt-and-braces -- async_setup should have removed it.
        LOGGER.warning("Skipping placeholder entry %s (no kind)", entry.entry_id)
        return False

    data = await _ensure_shared_data(hass)

    if kind == ENTRY_KIND_PROFILE_MANAGER:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_PROFILE_MANAGER)
        return True

    if kind == ENTRY_KIND_ZONE:
        zone_name: str = entry.data[CONF_ZONE_NAME]
        if not data.store.has_zone(zone_name):
            await data.store.async_add_zone(zone_name)

        # Room-temp sensor (required) — v0.9.2 extended the OptionsFlow to
        # let users swap it on an existing zone. Resolution: options-first
        # (OptionsFlow edit) falling back to data (original add-time
        # choice). Unlike humidity below, the temp key is always present
        # in either options OR data so a plain `.get(..., data[...])`
        # fallback is safe.
        temp_entity_id = entry.options.get(CONF_TEMP_SENSOR, entry.data[CONF_TEMP_SENSOR])
        # Humidity sensor is optional; OptionsFlow can also set / clear it
        # post-hoc. The OptionsFlow always writes the key (possibly None)
        # so "key present in options" means "user has edited this", even
        # when they're clearing the value. Without the explicit `in` check
        # below, a `.get(..., entry.data...)` fallback would silently
        # re-apply the data value, defeating the clear path.
        if CONF_HUMIDITY_SENSOR in entry.options:
            humidity_entity_id = entry.options[CONF_HUMIDITY_SENSOR]
        else:
            humidity_entity_id = entry.data.get(CONF_HUMIDITY_SENSOR)
        coordinator = ZoneCoordinator(
            hass,
            data.store,
            zone_name,
            entry.data[CONF_CLIMATE_ENTITY],
            temp_entity_id,
            humidity_entity_id=humidity_entity_id,
        )
        await coordinator.async_setup()
        data.zone_coordinators[entry.entry_id] = coordinator
        data.zone_slug_to_entry_id[zone_name] = entry.entry_id
        # Reload on OptionsFlow save so a sensor change (temp or humidity)
        # picks up without a HA restart.
        entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_ZONE)
        return True

    LOGGER.error("Unknown ConfigEntry kind %r on %s", kind, entry.entry_id)
    return False


async def _async_reload_on_options_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a ConfigEntry. Mirror of async_setup_entry."""
    kind = entry.data.get(CONF_KIND)
    if kind == ENTRY_KIND_PROFILE_MANAGER:
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS_PROFILE_MANAGER)
    if kind == ENTRY_KIND_ZONE:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS_ZONE)
        if unloaded:
            data: ComfortBandData = hass.data[DOMAIN]
            coordinator = data.zone_coordinators.pop(entry.entry_id, None)
            if coordinator is not None:
                await coordinator.async_unload()
                data.zone_slug_to_entry_id.pop(coordinator.zone_name, None)
        return unloaded
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Permanently delete an entry's persisted state. Called after async_unload."""
    if entry.data.get(CONF_KIND) != ENTRY_KIND_ZONE:
        return
    data: ComfortBandData | None = hass.data.get(DOMAIN)
    if data is None:
        return
    zone_name = entry.data.get(CONF_ZONE_NAME)
    if zone_name:
        await data.store.async_remove_zone(zone_name)


async def _ensure_shared_data(hass: HomeAssistant) -> ComfortBandData:
    """Idempotently create the shared store + registry on first access."""
    existing = hass.data.get(DOMAIN)
    if isinstance(existing, ComfortBandData):
        return existing
    store = ComfortBandStore(hass)
    await store.async_load()
    registry = ProfileRegistry(hass, store)
    shared_schedule_registry = SharedScheduleRegistry(hass, store)
    feedback_store = FeedbackStore(hass)
    await feedback_store.async_load()
    data = ComfortBandData(
        store=store,
        profile_registry=registry,
        shared_schedule_registry=shared_schedule_registry,
        feedback_store=feedback_store,
    )
    hass.data[DOMAIN] = data
    return data
