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
        # 1-decimal rounding matches RoomTemperatureSensor — both sources
        # feed the band gauge and the card's "Feels like" line, which
        # subtract them to decide whether to render the row. Keeping the
        # precision identical prevents a permanent sub-0.1 °C delta from
        # forcing the row visible at every humidity value.
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


class ThermalSlopeSensor(ComfortBandZoneEntity, SensorEntity):
    """Current learned thermal-response slope (°C/h, signed).

    The state value is whichever per-action slope matches the zone's
    *previously committed* `last_action` (idle when last_action is
    idle/unknown). It lags the current refresh's decision by one cycle
    because `last_action` is updated by `_maybe_apply_action` running as
    a follow-up task -- after the snapshot the sensor reads. The lag is
    intentional: the displayed slope corresponds to the action that
    actually accumulated samples, not the one we just decided to take.

    The per-action slopes (idle, recovery_heat, recovery_cool) plus buffer
    bookkeeping are exposed via attributes for the card / debugging.

    Returns None (HA "unknown") when the relevant segment has fewer than
    SLOPE_MIN_SAMPLES samples or the WLS denominator is singular -- the
    first ~5-10 min after install/restart is expected to be unknown.
    """

    # HA has no constant for °C/h (no device class covers rate quantities);
    # the bare-string unit is the accepted pattern. Don't try to "fix" with
    # a nonexistent UnitOfTemperature.CELSIUS_PER_HOUR -- it doesn't exist.
    _attr_native_unit_of_measurement = "°C/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "thermal_slope")

    @property
    def native_value(self) -> float | None:
        slopes = self.coordinator.data.thermal_slopes
        last_action = self.coordinator.data.zone["last_action"]
        current = slopes.for_action(last_action)
        return None if current is None else round(current * 60.0, 3)

    @property
    def extra_state_attributes(self) -> dict[str, float | int | str | None]:
        data = self.coordinator.data
        s = data.thermal_slopes
        return {
            "idle_slope": None if s.idle is None else round(s.idle * 60.0, 3),
            # v0.12.0: where the idle slope came from this refresh. "live" =
            # estimated from the current sample window; "cached" = substituted
            # from the persisted last-good value because the live window only
            # had idle blips (MPC stays ready through a heating chase);
            # "none" = no idle slope available. `idle_slope_cached_age_min` is
            # the age of the cached value in minutes (null unless "cached").
            "idle_slope_source": data.idle_slope_source,
            "idle_slope_cached_age_min": data.idle_slope_cached_age_min,
            "recovery_slope_heat": (
                None if s.recovery_heat is None else round(s.recovery_heat * 60.0, 3)
            ),
            "recovery_slope_cool": (
                None if s.recovery_cool is None else round(s.recovery_cool * 60.0, 3)
            ),
            "sample_count": s.sample_count,
            "window_minutes": s.window_minutes,
            "last_updated": None if s.last_updated is None else s.last_updated.isoformat(),
            # v0.9.1 diagnostics. The aggregate `sample_count` above
            # spans all three actions; per-segment fields below show how
            # many samples are actually behind each slope estimate.
            # `std_dev_*` is the population standard deviation (°C) of
            # the segment's temperature samples — a value near 0 over
            # many samples signals the sensor is reporting one quantized
            # value throughout the window (the slope estimate is then
            # unreliable, regardless of sample count). `method_*` records
            # which estimator produced the slope; currently always "wls"
            # or "none", reserved string for future fallback methods.
            "sample_count_idle": s.sample_count_idle,
            "sample_count_recovery_heat": s.sample_count_recovery_heat,
            "sample_count_recovery_cool": s.sample_count_recovery_cool,
            "std_dev_idle": s.std_dev_idle,
            "std_dev_recovery_heat": s.std_dev_recovery_heat,
            "std_dev_recovery_cool": s.std_dev_recovery_cool,
            "method_idle": s.method_idle,
            "method_recovery_heat": s.method_recovery_heat,
            "method_recovery_cool": s.method_recovery_cool,
        }


class PredictedActionSensor(ComfortBandZoneEntity, SensorEntity):
    """What the predictor would issue right now (always populated, regardless
    of learning_enabled). Lets users shadow-compare against `current_action`
    before flipping the learning switch on.

    Marked DIAGNOSTIC because it's a debug/shadow signal, not the primary
    "what's the HVAC doing" state (`current_action` plays that role and is
    NOT diagnostic). Users opted into predictive control will see this
    sensor under "Diagnostic" on the device card.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "predicted_action")
        self._attr_options = [ACTION_HEAT, ACTION_COOL, ACTION_IDLE, ACTION_UNKNOWN]

    @property
    def native_value(self) -> str:
        return self.coordinator.data.predicted_decision.action


class MpcActionSensor(ComfortBandZoneEntity, SensorEntity):
    """What MPC would issue right now (always populated, regardless of
    `mpc_enabled`). Mirrors `PredictedActionSensor` but for the v0.8
    layer — same shadow-mode discipline so users can flip `mpc_enabled`
    once the value tracks expectation. Falls back to the predictor's
    decision when `mpc_ready` is False (cold start), so the sensor stays
    meaningful during warm-up.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "mpc_action")
        self._attr_options = [ACTION_HEAT, ACTION_COOL, ACTION_IDLE, ACTION_UNKNOWN]

    @property
    def native_value(self) -> str:
        return self.coordinator.data.mpc_decision.action


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
            ThermalSlopeSensor(coordinator),
            PredictedActionSensor(coordinator),
            MpcActionSensor(coordinator),
        ]
    )
