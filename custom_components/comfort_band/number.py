"""Per-zone number entities.

Eight tunables are exposed as `number.{zone}_<key>`. Two of them
(`manual_low`, `manual_high`) trigger an override on every UI write,
matching the legacy automation's "context.user_id is not none" semantics.
The other six (`override_hours`, `deadband_below`, `deadband_above`,
`min_cycle_minutes`, `cross_mode_min_minutes`, `lookahead_minutes`) are
simple persisted tunables.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LOOKAHEAD_MAX, LOOKAHEAD_MIN, TEMP_MAX, TEMP_MIN, TEMP_STEP
from .coordinator import ZoneCoordinator
from .entity import ComfortBandZoneEntity


@dataclass(frozen=True, kw_only=True)
class _NumberSpec:
    key: str
    field: str  # StoredZone field
    min_value: float
    max_value: float
    step: float
    unit: str | None = None
    device_class: NumberDeviceClass | None = None
    triggers_override: bool = False


_SPECS: tuple[_NumberSpec, ...] = (
    _NumberSpec(
        key="manual_low",
        field="manual_low",
        min_value=TEMP_MIN,
        max_value=TEMP_MAX,
        step=TEMP_STEP,
        unit=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        triggers_override=True,
    ),
    _NumberSpec(
        key="manual_high",
        field="manual_high",
        min_value=TEMP_MIN,
        max_value=TEMP_MAX,
        step=TEMP_STEP,
        unit=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        triggers_override=True,
    ),
    _NumberSpec(
        key="override_hours",
        field="override_hours",
        min_value=1,
        max_value=12,
        step=1,
        unit=UnitOfTime.HOURS,
    ),
    _NumberSpec(
        key="deadband_below",
        field="deadband_below",
        min_value=0.1,
        max_value=2.0,
        step=0.1,
        unit=UnitOfTemperature.CELSIUS,
    ),
    _NumberSpec(
        key="deadband_above",
        field="deadband_above",
        min_value=0.1,
        max_value=2.0,
        step=0.1,
        unit=UnitOfTemperature.CELSIUS,
    ),
    _NumberSpec(
        key="min_cycle_minutes",
        field="min_cycle_minutes",
        min_value=0,
        max_value=60,
        step=1,
        unit=UnitOfTime.MINUTES,
    ),
    _NumberSpec(
        key="cross_mode_min_minutes",
        field="cross_mode_min_minutes",
        min_value=0,
        max_value=60,
        step=1,
        unit=UnitOfTime.MINUTES,
    ),
    _NumberSpec(
        key="lookahead_minutes",
        field="lookahead_minutes",
        min_value=LOOKAHEAD_MIN,
        max_value=LOOKAHEAD_MAX,
        step=1,
        unit=UnitOfTime.MINUTES,
    ),
)


class ComfortBandNumber(ComfortBandZoneEntity, NumberEntity):
    """One per `_NumberSpec`. Mode-of-write is decided by spec."""

    def __init__(self, coordinator: ZoneCoordinator, spec: _NumberSpec) -> None:
        super().__init__(coordinator, spec.key)
        self._spec = spec
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        if spec.unit is not None:
            self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class is not None:
            self._attr_device_class = spec.device_class

    @property
    def native_value(self) -> float:
        return self.coordinator.data.zone[self._spec.field]  # type: ignore[literal-required,no-any-return]

    async def async_set_native_value(self, value: float) -> None:
        if self._spec.field == "manual_low":
            await self.coordinator.async_set_manual_low(value)
            return
        if self._spec.field == "manual_high":
            await self.coordinator.async_set_manual_high(value)
            return
        await self.coordinator.async_set_param(self._spec.field, value)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities(ComfortBandNumber(coordinator, spec) for spec in _SPECS)
