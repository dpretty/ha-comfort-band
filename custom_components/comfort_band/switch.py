"""Per-zone switch entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZoneCoordinator
from .entity import ComfortBandZoneEntity


class EnabledSwitch(ComfortBandZoneEntity, SwitchEntity):
    """Master kill: while OFF the integration is in shadow mode -- it computes
    decisions and logs intent but does NOT call climate.set_hvac_mode.
    Default is OFF so the legacy YAML keeps driving until per-zone cutover.

    Deliberately NOT marked EntityCategory.CONFIG: this is the zone's
    primary operational control and should render as a top-level entity
    on the device page, not under the collapsed "Configuration" section
    where the learning / use-apparent toggles live.
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


class LearningEnabledSwitch(ComfortBandZoneEntity, SwitchEntity):
    """Master gate for the v0.6 predictive controller. Default OFF -- same
    shadow-mode posture as `enabled`. When ON, `predictor.decide()`'s
    anticipated action replaces `hysteresis.decide()`'s reactive one as the
    final decision forwarded to climate. The `predicted_action` sensor
    populates regardless so users can shadow-compare before flipping it.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "learning_enabled")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.zone["learning_enabled"]

    async def async_turn_on(self, **_kwargs: Any) -> None:
        await self.coordinator.async_set_learning_enabled(True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        await self.coordinator.async_set_learning_enabled(False)


class UseApparentTemperatureSwitch(ComfortBandZoneEntity, SwitchEntity):
    """When ON, hysteresis decisions consume the apparent (humidity-adjusted)
    temperature instead of the raw room reading. Falls back to room temp
    automatically when no humidity reading is available, so it's safe to
    leave on even if the humidity sensor is flaky. Default OFF -- opt-in
    behaviour change.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "use_apparent_temperature")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.zone["use_apparent_temperature"]

    async def async_turn_on(self, **_kwargs: Any) -> None:
        await self.coordinator.async_set_use_apparent_temperature(True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        await self.coordinator.async_set_use_apparent_temperature(False)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    async_add_entities(
        [
            EnabledSwitch(coordinator),
            LearningEnabledSwitch(coordinator),
            UseApparentTemperatureSwitch(coordinator),
        ]
    )
