"""Per-zone switch entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZoneCoordinator
from .entity import ComfortBandZoneEntity


class EnabledSwitch(ComfortBandZoneEntity, SwitchEntity):
    """Master kill: while OFF the integration is in shadow mode -- it computes
    decisions and logs intent but does NOT call climate.set_hvac_mode.
    Default is OFF so the legacy YAML keeps driving until per-zone cutover.
    """

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "enabled")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.zone["enabled"]

    async def async_turn_on(self, **_kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(False)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities([EnabledSwitch(coordinator)])
