"""Per-zone binary_sensor entities."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
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


class RoomSensorUnavailableBinarySensor(ComfortBandZoneEntity, BinarySensorEntity):
    """True while the zone's configured temperature sensor isn't reporting.

    A zone with no room reading cannot control: the decider returns `unknown`
    and the room is left to drift. On its own that is silent -- the failure that
    prompted this entity went unnoticed for hours while a bedroom sat several
    degrees below its band, because nothing surfaced "this zone has stopped
    controlling."

    Deliberately a first-class (non-diagnostic) `problem` sensor rather than an
    attribute: `on` means "this room has no reading to control from", which is
    exactly the condition worth a notification. (A zone still in shadow mode
    wasn't controlling either way -- the entity is just as true there, it simply
    matters less, which is why only the log distinguishes them.) Pair it with a `for:` of a few
    minutes to ride out routine sensor blips:

        - trigger: state
          entity_id: binary_sensor.mbr_room_sensor_unavailable
          to: "on"
          for: "00:05:00"

    Tracks the *configured external* sensor specifically, so it stays a truthful
    health signal for the device that actually needs attention.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "room_sensor_unavailable")

    @property
    def available(self) -> bool:
        """Stay available as long as there is any state worth reporting.

        `CoordinatorEntity` ties availability to the last refresh succeeding, so
        a failed refresh -- a read-only SD card, say -- would turn this entity
        `unavailable` rather than `on`, and an automation triggering `to: "on"`
        would silently never fire at exactly the moment the zone stopped
        controlling.

        Only in the alarming direction, though: `data` is refreshed only on
        success, so if refreshes start failing while the sensor is still healthy
        the snapshot would keep asserting `off` -- confidently claiming "no
        problem" about a room that has since gone dark. A stale `on` is worth
        keeping; a stale `off` is worse than `unavailable`.
        """
        return self.coordinator.data is not None and (
            self.coordinator.last_update_success or not self.coordinator.data.sensor_available
        )

    @property
    def is_on(self) -> bool:
        return not self.coordinator.data.sensor_available


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities(
        [
            OverrideActiveBinarySensor(coordinator),
            MpcReadyBinarySensor(coordinator),
            RoomSensorUnavailableBinarySensor(coordinator),
        ]
    )
