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
from .profiles import ProfileRegistry
from .storage import ComfortBandStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class ComfortBandData:
    """Shared per-integration state. One instance lives in `hass.data[DOMAIN]`."""

    store: ComfortBandStore
    profile_registry: ProfileRegistry
    zone_coordinators: dict[str, ZoneCoordinator] = field(default_factory=dict)
    zone_slug_to_entry_id: dict[str, str] = field(default_factory=dict)


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """One-shot integration setup. Cleans Session A placeholders + boots state."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if CONF_KIND not in entry.data:
            LOGGER.info("Removing Session A placeholder config entry %s", entry.entry_id)
            hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
    await _ensure_shared_data(hass)
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

        coordinator = ZoneCoordinator(
            hass,
            data.store,
            zone_name,
            entry.data[CONF_CLIMATE_ENTITY],
            entry.data[CONF_TEMP_SENSOR],
        )
        await coordinator.async_setup()
        data.zone_coordinators[entry.entry_id] = coordinator
        data.zone_slug_to_entry_id[zone_name] = entry.entry_id
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_ZONE)
        return True

    LOGGER.error("Unknown ConfigEntry kind %r on %s", kind, entry.entry_id)
    return False


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
    data = ComfortBandData(store=store, profile_registry=registry)
    hass.data[DOMAIN] = data
    return data
