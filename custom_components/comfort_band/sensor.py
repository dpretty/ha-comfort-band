"""Per-zone sensor entities."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    DOMAIN,
)
from .coordinator import ZoneCoordinator
from .entity import ComfortBandZoneEntity


class _TemperatureSensor(ComfortBandZoneEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT


class EffectiveLowSensor(_TemperatureSensor):
    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "effective_low")

    @property
    def native_value(self) -> float:
        return self.coordinator.data.effective_low


class EffectiveHighSensor(_TemperatureSensor):
    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "effective_high")

    @property
    def native_value(self) -> float:
        return self.coordinator.data.effective_high


class RoomTemperatureSensor(_TemperatureSensor):
    """Diagnostic mirror of the source sensor; lets the card read everything
    from the comfort_band namespace.

    Also exposes the configured `humidity_sensor` entity_id as an attribute
    so the card's Settings tab can show the current value without a
    separate WS round-trip. None when not configured.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "room_temperature")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.room

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"humidity_sensor": self.coordinator.humidity_entity_id}


class ApparentTemperatureSensor(_TemperatureSensor):
    """Steadman 1994 apparent temperature ("feels like"). Equals room temp
    when no humidity sensor is configured or its reading is unavailable.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "apparent_temperature")

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.apparent_temperature
        return None if value is None else round(value, 1)


class OverrideEndsSensor(ComfortBandZoneEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "override_ends")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.override_until


class CurrentActionSensor(ComfortBandZoneEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "current_action")
        self._attr_options = [ACTION_HEAT, ACTION_COOL, ACTION_IDLE, ACTION_UNKNOWN]

    @property
    def native_value(self) -> str:
        return self.coordinator.data.decision.action


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities(
        [
            EffectiveLowSensor(coordinator),
            EffectiveHighSensor(coordinator),
            RoomTemperatureSensor(coordinator),
            ApparentTemperatureSensor(coordinator),
            OverrideEndsSensor(coordinator),
            CurrentActionSensor(coordinator),
        ]
    )
