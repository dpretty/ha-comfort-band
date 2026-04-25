"""Per-zone binary_sensor entities."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZoneCoordinator
from .entity import ComfortBandZoneEntity


class OverrideActiveBinarySensor(ComfortBandZoneEntity, BinarySensorEntity):
    """True while override_until > now()."""

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "override_active")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.override_active


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities([OverrideActiveBinarySensor(coordinator)])
