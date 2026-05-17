"""ZoneCoordinator behaviour tests.

Smoke tests cover the pure read pipeline (no live triggers); behaviour tests
exercise the full action-application path (`_maybe_apply_action`) with the
pytest-freezer `freezer` fixture for time travel.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator, ZoneState
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"  # external sensor; non-colliding with comfort_band's mirror
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


async def test_use_apparent_temperature_swaps_decision_input(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """With a humidity sensor configured AND `use_apparent_temperature` ON,
    the hysteresis decider sees the Steadman value, not the raw room temp."""
    humidity_entity = "sensor.office_humidity"
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", use_apparent_temperature=True)
    coordinator = ZoneCoordinator(
        hass,
        store,
        "office",
        CLIMATE_ENTITY,
        TEMP_ENTITY,
        humidity_entity_id=humidity_entity,
    )
    # 27 °C room + 85 % RH → apparent ≈ 30 °C — above the default 22.5 high
    # band. Without the switch the decider would see 27 (cool either way),
    # but with humidity boost it's clearly above and the asserted band makes
    # the swap test the discriminating value.
    hass.states.async_set(TEMP_ENTITY, "27.0", {})
    hass.states.async_set(humidity_entity, "85", {})
    state = await coordinator._async_update_data()
    assert state.room == 27.0
    assert state.humidity == 85.0
    # Apparent stored on the state alongside the raw room reading.
    assert state.apparent_temperature is not None
    assert state.apparent_temperature > state.room
    # `decision_room` is the value that was actually fed into hysteresis.
    assert state.decision_room == state.apparent_temperature


async def test_use_apparent_temperature_off_uses_raw_room(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Default behaviour: hysteresis sees the raw room reading even when a
    humidity sensor is configured."""
    humidity_entity = "sensor.office_humidity"
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    # `use_apparent_temperature` is False by default.
    coordinator = ZoneCoordinator(
        hass,
        store,
        "office",
        CLIMATE_ENTITY,
        TEMP_ENTITY,
        humidity_entity_id=humidity_entity,
    )
    hass.states.async_set(TEMP_ENTITY, "27.0", {})
    hass.states.async_set(humidity_entity, "85", {})
    state = await coordinator._async_update_data()
    # Apparent is still computed and surfaced; just not used for decisions.
    assert state.apparent_temperature is not None
    assert state.apparent_temperature > state.room
    assert state.decision_room == state.room  # NOT the apparent value


async def test_use_apparent_falls_back_to_room_when_humidity_unavailable(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The point of the safety net: flipping `use_apparent_temperature` ON
    must still produce sensible decisions when the humidity sensor goes
    offline. `compute(T, None) -> T`, so decision_room === room."""
    humidity_entity = "sensor.office_humidity"
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", use_apparent_temperature=True)
    coordinator = ZoneCoordinator(
        hass,
        store,
        "office",
        CLIMATE_ENTITY,
        TEMP_ENTITY,
        humidity_entity_id=humidity_entity,
    )
    hass.states.async_set(TEMP_ENTITY, "21.5", {})
    # No humidity sensor state at all — equivalent to unavailable.
    state = await coordinator._async_update_data()
    assert state.humidity is None
    assert state.apparent_temperature == state.room
    assert state.decision_room == state.room


async def test_schedule_fallback_follows_renamed_default_profile(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """When the active profile has no schedule, fall back to the *default
    profile's* schedule — even after that profile has been renamed."""
    # Seed a schedule on "home" then rename home -> weekday. The active
    # profile is still "home" (now renamed), so the active key also
    # changes; switch active to "away" (which has no schedule) to exercise
    # the fallback path.
    store = coordinator._store
    await store.async_set_zone_schedule(
        "office", "home", [{"at": "00:00", "low": 21.0, "high": 23.0}]
    )
    await store.async_rename_profile("home", "weekday")
    await store.async_set_active_profile("away")  # away has no schedule
    hass.states.async_set(TEMP_ENTITY, "22.0", {})
    try:
        state = await coordinator._async_update_data()
        # Should fall back to "weekday" (the renamed default), not the manual band.
        assert state.effective_low == 21.0
        assert state.effective_high == 23.0
    finally:
        # The schedule update schedules a transition-timer; cancel it so
        # pytest-homeassistant-custom-component's "lingering timer" guard
        # doesn't trip in teardown.
        await coordinator.async_unload()


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


# ----- behaviour: full action application -----


def _calls_for(
    climate_calls: list[tuple[str, dict[str, Any]]], service: str
) -> list[dict[str, Any]]:
    return [data for srv, data in climate_calls if srv == service]


async def _setup_enabled_zone(
    hass: HomeAssistant, climate_calls: list[tuple[str, dict[str, Any]]]
) -> ZoneCoordinator:
    """Add an `office` zone, enable it (non-shadow), return its coordinator."""
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    # Force enabled before any refresh fires.
    await store.async_update_zone("office", enabled=True)
    return coordinator


async def test_active_heat_then_release_to_idle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # Drop temp well below manual_low (=19.5 by default) to enter heat.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_HEAT
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    set_temps = _calls_for(climate_calls, "set_temperature")
    assert any(c["hvac_mode"] == HVAC_MODE_HEAT for c in set_modes), set_modes
    assert any(c["temperature"] == 19.5 for c in set_temps), set_temps

    # Raise temp to band edge -> release to idle (fan_only).
    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_IDLE
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert any(c["hvac_mode"] == HVAC_MODE_FAN_ONLY for c in set_modes), set_modes


async def test_min_cycle_suppresses_same_action_re_issue(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    initial_set_mode_calls = len(_calls_for(climate_calls, "set_hvac_mode"))
    assert initial_set_mode_calls >= 1  # the initial heat fire

    # 5 min later (still heating, same action, within 8 min default) -> no re-fire.
    freezer.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(_calls_for(climate_calls, "set_hvac_mode")) == initial_set_mode_calls

    # Different action (idle) fires immediately even within the min-cycle window.
    # Raise temp above low to release heat -> idle/fan_only.
    hass.states.async_set(TEMP_ENTITY, "20.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    final_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert any(c["hvac_mode"] == HVAC_MODE_FAN_ONLY for c in final_modes), final_modes
    await coordinator.async_unload()


async def test_override_starts_then_expires(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-04-25 10:00:00+00:00")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()  # baseline

    # Start a 1-hour override at a different band; this also overwrites the
    # manual band, since async_start_override(low=, high=) is the user-driven
    # "I want this temperature for the next N hours" path.
    await coordinator.async_start_override(low=22.0, high=24.0, hours=1)
    assert coordinator.data.override_active
    assert coordinator.data.effective_low == 22.0
    assert coordinator.data.effective_high == 24.0

    # 90 min later -> override has expired. With no schedule, effective falls
    # back to the (now updated) manual band.
    freezer.tick(timedelta(minutes=90))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.data.override_active
    assert coordinator.data.override_until is None
    assert coordinator.data.effective_low == 22.0  # manual_low after start_override
    assert coordinator.data.effective_high == 24.0  # manual_high after start_override
    await coordinator.async_unload()


async def test_cancel_override_immediately_clears_it(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()

    await coordinator.async_start_override(low=22.0, high=24.0, hours=4)
    assert coordinator.data.override_active

    await coordinator.async_cancel_override()
    assert not coordinator.data.override_active
    assert coordinator.data.override_until is None
    await coordinator.async_unload()
