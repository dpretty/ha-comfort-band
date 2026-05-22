"""Per-zone binary_sensor entities."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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


class MpcReadyBinarySensor(ComfortBandZoneEntity, BinarySensorEntity):
    """True when MPC has the slope data it needs to produce a meaningful
    decision (idle + recovery_heat + recovery_cool all present).

    During cold start (fresh install, fresh restart with empty buffer, or
    post-flush after a manual climate edit), one or more slopes will be
    None — MPC falls back to the predictor's decision silently in that
    case. This sensor exposes the gate so the user can see *why* MPC isn't
    firing during the warm-up window. Without it the answer to "I flipped
    mpc_enabled but the room behaviour didn't change" would be hidden in
    debug logs.

    Marked DIAGNOSTIC: it's a state-of-the-controller signal, not a
    primary entity for automation.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "mpc_ready")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.mpc_ready


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities(
        [
            OverrideActiveBinarySensor(coordinator),
            MpcReadyBinarySensor(coordinator),
        ]
    )
