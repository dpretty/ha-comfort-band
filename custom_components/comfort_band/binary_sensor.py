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
    decision (v0.8.1+: `idle_slope` present AND at least one of
    `recovery_heat` / `recovery_cool`). Heat-only zones reach this after
    idle + heat segments accumulate; cool-only zones after idle + cool;
    fully-equipped zones once all three are available.

    During cold start (fresh install, fresh restart with empty buffer, or
    post-flush after a manual climate edit), the required slopes will be
    None and MPC falls back to the predictor silently. This sensor exposes
    the gate so the user can see *why* MPC isn't firing during the warm-up
    window. Without it, "I flipped mpc_enabled but the room behaviour
    didn't change" would be hidden in debug logs.

    The per-refresh safety bail-out (room outside band on a side whose
    recovery slope is missing) does NOT flip this sensor — `mpc_ready`
    means "MPC is equipped to consider acting," not "MPC is acting this
    refresh." During a bail-out `plan` returns the predictor's decision
    unchanged, so `sensor.{zone}_mpc_action` and `sensor.{zone}_predicted_action`
    will simply hold the same value for that refresh.

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
