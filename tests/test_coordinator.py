"""Smoke tests for ZoneCoordinator -- full action-application coverage in commit 6."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator, ZoneState
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_room_temperature"
CLIMATE_ENTITY = "climate.office_hvac"


@pytest.fixture
async def coordinator(hass: HomeAssistant, hass_storage: dict[str, Any]) -> ZoneCoordinator:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    return ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)


async def test_returns_zone_state_with_room_reading(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    hass.states.async_set(TEMP_ENTITY, "21.5", {"unit_of_measurement": "°C"})
    state = await coordinator._async_update_data()
    assert isinstance(state, ZoneState)
    assert state.room == 21.5
    assert state.sensor_available is True
    # Default zone has manual_low=19.5, manual_high=22.5; no schedule -> use manual.
    assert state.effective_low == 19.5
    assert state.effective_high == 22.5
    # 21.5 is well inside [19.5, 22.5] -> idle.
    assert state.decision.action == ACTION_IDLE
    assert state.enabled is False  # shadow-mode default


async def test_room_unavailable_yields_unknown_decision(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    state = await coordinator._async_update_data()
    assert state.room is None
    assert state.sensor_available is False
    assert state.decision.action == ACTION_UNKNOWN
    assert state.decision.target_mode is None


async def test_missing_sensor_state_yields_unknown(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    # No state ever set for TEMP_ENTITY.
    state = await coordinator._async_update_data()
    assert state.room is None
    assert state.decision.action == ACTION_UNKNOWN


async def test_well_below_band_decides_heat(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    hass.states.async_set(TEMP_ENTITY, "18.0", {})  # well below manual_low=19.5
    state = await coordinator._async_update_data()
    assert state.decision.action == ACTION_HEAT
    assert state.decision.target_temp == 19.5


async def test_well_above_band_decides_cool(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    hass.states.async_set(TEMP_ENTITY, "24.0", {})  # well above manual_high=22.5
    state = await coordinator._async_update_data()
    assert state.decision.action == ACTION_COOL
    assert state.decision.target_temp == 22.5


async def test_shadow_mode_does_not_call_climate(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """With enabled=False, _maybe_apply_action returns without calling services."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    from homeassistant.core import ServiceCall

    async def record(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))

    hass.services.async_register("climate", "set_hvac_mode", record)
    hass.services.async_register("climate", "set_temperature", record)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})  # would trigger heat

    state = await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert state.decision.action == ACTION_HEAT
    assert state.enabled is False
    assert calls == []
