"""Per-zone button entities."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZoneCoordinator
from .entity import ComfortBandZoneEntity


class CancelOverrideButton(ComfortBandZoneEntity, ButtonEntity):
    """Press -> immediately end the active override (if any)."""

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "cancel_override")

    async def async_press(self) -> None:
        await self.coordinator.async_cancel_override()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities([CancelOverrideButton(coordinator)])
