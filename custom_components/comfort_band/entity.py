"""Entity base classes.

`ComfortBandZoneEntity` is the parent of every per-zone entity (number,
sensor, binary_sensor, button, switch). Owns the unique-id pattern, the
DeviceInfo (one device per zone, rendered as a service device in the
HA UI), and the translation key wiring.

`ComfortBandProfileEntity` is the base of the single global profile-manager
entity (the `select.comfort_band_active_profile`). Subscribes to the
active-profile dispatcher signal in `async_added_to_hass` so the entity
re-renders when the user (or a service) flips profiles.
"""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_ACTIVE_PROFILE_CHANGED
from .coordinator import ZoneCoordinator


class ComfortBandZoneEntity(CoordinatorEntity[ZoneCoordinator]):
    """Base for per-zone entities. Subclasses set `_attr_translation_key`."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ZoneCoordinator, key: str) -> None:
        super().__init__(coordinator)
        zone_name = coordinator.zone_name
        self._attr_unique_id = f"{zone_name}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"zone:{zone_name}")},
            name=zone_name.replace("_", " ").title(),
            manufacturer="Comfort Band",
            entry_type=DeviceEntryType.SERVICE,
        )


class ComfortBandProfileEntity(Entity):
    """Base for the singleton profile-manager entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, key: str) -> None:
        self._attr_unique_id = f"profile_manager_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "profile_manager")},
            name="Comfort Band Profiles",
            manufacturer="Comfort Band",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Re-render when the active profile changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ACTIVE_PROFILE_CHANGED,
                self._handle_active_changed,
            )
        )

    @callback
    def _handle_active_changed(self, _new_active: str) -> None:
        self.async_write_ha_state()
