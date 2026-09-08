"""ZoneCoordinator behaviour tests.

Smoke tests cover the pure read pipeline (no live triggers); behaviour tests
exercise the full action-application path (`_maybe_apply_action`) with the
pytest-freezer `freezer` fixture for time travel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    CLIMATE_ECHO_WINDOW_S,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    SIGNAL_ACTIVE_PROFILE_CHANGED,
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
    zone_coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    # Subscribed and registered for teardown for the same reason as
    # `_setup_enabled_zone` -- this fixture builds coordinators for ~25 tests,
    # and leaving it on the old unsubscribed path would preserve exactly the
    # harness-versus-production divergence this is meant to remove.
    zone_coordinator.subscribe_and_hydrate()
    _HELPER_COORDINATORS.append(zone_coordinator)
    return zone_coordinator


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


async def test_humidity_going_unavailable_mid_stream_falls_back_to_room(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Sibling case to the never-registered test above: the humidity sensor
    publishes a valid reading, then transitions to `unavailable` (sensor
    drops off the network, integration unloads, etc.). The `_read_humidity`
    `STATE_UNAVAILABLE` guard branch isn't exercised by the
    state-never-set path — it returns early on `state is None`. This pins
    the explicit-unavailable behaviour so a regression there can't slip in.
    """
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
    hass.states.async_set(humidity_entity, "60", {})
    first = await coordinator._async_update_data()
    assert first.humidity == 60.0
    # Now simulate the sensor dropping mid-stream.
    hass.states.async_set(humidity_entity, "unavailable", {})
    after = await coordinator._async_update_data()
    assert after.humidity is None
    assert after.apparent_temperature == after.room
    assert after.decision_room == after.room


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


_HELPER_COORDINATORS: list[ZoneCoordinator] = []


@pytest.fixture(autouse=True)
async def _unload_helper_coordinators(hass: HomeAssistant) -> AsyncIterator[None]:
    """Unload every coordinator the fixtures in this module built.

    The helper wires the real listeners, so a state change arms the coordinator's
    2 s debounce; a test that ends without unloading leaves that timer pending
    and HA's lingering-timer check fails it. Doing it here rather than in each
    test keeps it impossible to forget -- and `async_unload` is documented safe
    to call repeatedly, so tests that already unload explicitly are unaffected.
    """
    _HELPER_COORDINATORS.clear()
    yield
    failures: list[Exception] = []
    for coordinator in _HELPER_COORDINATORS:
        # Guarded per coordinator so one failure doesn't skip the rest and turn
        # into a cascade of confusing lingering-timer errors -- but collected
        # and re-raised below rather than swallowed. 41 tests never unload
        # themselves, so logging alone would turn a real `async_unload`
        # regression into a log line for exactly those.
        try:
            await coordinator.async_unload()
            # Also stop the coordinator's own request-refresh debouncer. In
            # production `DataUpdateCoordinator.__init__` registers
            # `config_entry.async_on_unload(self.async_shutdown)`, but only when
            # it has a config entry -- which a coordinator built directly like
            # this one does not, so nothing would ever cancel its pending
            # debounce.
            await coordinator.async_shutdown()
        except Exception as err:
            failures.append(err)
    _HELPER_COORDINATORS.clear()
    if failures:
        raise ExceptionGroup("failed to unload test coordinators", failures)


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
    # Wire the real listeners, exactly as `async_setup` does in production.
    # Without them a sensor change reaches the coordinator only through a test's
    # own `async_refresh()`, so the event-driven path these tests are supposed to
    # be covering never runs -- and `_on_climate_state_change` never fires, which
    # makes every "no manual edit was detected" assertion vacuous.
    coordinator.subscribe_and_hydrate()
    _HELPER_COORDINATORS.append(coordinator)
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


async def test_cross_mode_min_cycle_suppresses_heat_to_cool(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """heat → idle (release) → cool gets suppressed when the gap is shorter
    than `cross_mode_min_minutes`. The hysteresis decider always routes
    through idle (decide() returns idle once the room hits the band edge),
    so cross-mode tracking relies on `previous_action`: by the time the
    flip-to-cool is evaluated, `last_action` is `idle`."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # Heat fires (room well below low=19.5).
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Release to idle (room hits low band edge).
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Within the 8-min default window, the room overshoots well above
    # high+deadband_above=23.0. Cross-mode gate should suppress cool.
    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "24.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    cool_calls = [c for c in set_modes if c["hvac_mode"] == "cool"]
    assert cool_calls == [], f"Cross-mode dwell should suppress cool, got {set_modes}"
    await coordinator.async_unload()


async def test_cross_mode_min_cycle_suppresses_cool_to_heat(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Symmetric to heat→cool: cool → idle → heat suppressed within window."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # Cool fires (room well above high+deadband_above=23.0).
    hass.states.async_set(TEMP_ENTITY, "24.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Release to idle.
    hass.states.async_set(TEMP_ENTITY, "22.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Within window, room undershoots below low-deadband_below=19.2.
    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    heat_calls = [c for c in set_modes if c["hvac_mode"] == "heat"]
    assert heat_calls == [], f"Cross-mode dwell should suppress heat, got {set_modes}"
    await coordinator.async_unload()


async def test_cross_mode_min_cycle_allows_flip_after_window(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """After `cross_mode_min_minutes` elapses since release, the flip fires."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Tick past the 8-min cross-mode window.
    freezer.tick(timedelta(minutes=10))

    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "24.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    cool_calls = [c for c in set_modes if c["hvac_mode"] == "cool"]
    assert cool_calls, f"Cool should fire after the window, got {set_modes}"
    await coordinator.async_unload()


async def test_cross_mode_min_cycle_zero_disables_gate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Setting cross_mode_min_minutes to 0 restores pre-v0.5 instant-flip behaviour."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Disable the gate before any actions.
    await coordinator._store.async_update_zone("office", cross_mode_min_minutes=0)

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "24.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    cool_calls = [c for c in set_modes if c["hvac_mode"] == "cool"]
    assert cool_calls, f"With cross_mode=0 the flip should fire immediately, got {set_modes}"
    await coordinator.async_unload()


async def test_cross_mode_min_cycle_does_not_block_first_action(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Fresh-from-restart zone has no previous_action; the gate must not
    block the first heat. Regression guard against treating None as a flip."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    heat_calls = [c for c in set_modes if c["hvac_mode"] == "heat"]
    assert heat_calls, f"First heat must fire (no prior action), got {set_modes}"
    await coordinator.async_unload()


async def test_cross_mode_gate_does_not_suppress_same_mode_bounce(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """heat → idle → heat (room re-cools within the cross-mode window) is
    NOT a cross-mode flip — both the prior and the new active action are
    `heating`. The gate's `prior_active_action != decision.action` guard
    must let this through. Sanity-check against a future refactor that
    drops the inequality check and starts suppressing same-mode bounces."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Drop min_cycle_minutes to 0 so the same-mode dwell is out of the way —
    # this test isolates the cross-mode gate's inequality guard. Keep
    # cross_mode_min_minutes at its default 8 so the gate would fire if it
    # incorrectly treated same-mode bounces as flips.
    await coordinator._store.async_update_zone("office", min_cycle_minutes=0)

    # Heat fires, then releases to idle.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Within the 8-min cross-mode window (tick 2 min — well inside), the
    # room cools again and re-triggers heat. The cross-mode gate sees
    # prior_active_action=heating, decision.action=heating, and must let
    # this through because they're the same mode.
    freezer.tick(timedelta(minutes=2))
    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    heat_calls = [c for c in set_modes if c["hvac_mode"] == "heat"]
    assert heat_calls, "Same-mode bounce (heat→idle→heat) must not be gated as a cross-mode flip"
    await coordinator.async_unload()


async def test_previous_action_preserved_across_same_mode_re_commits(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A same-mode re-issue (heat is still appropriate after the min-cycle
    window expires) must NOT overwrite `previous_action`. Otherwise after
    `heat → idle → heat → idle → cool` the cross-mode gate would lose
    track of the first heat and treat the second heat→idle→cool sequence
    as the first one. Pins the `last_action != decision.action` guard at
    the commit site so a future refactor that drops it gets caught."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    store = coordinator._store

    # First heat: previous_action becomes None (was None before).
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert store.get_zone("office")["previous_action"] is None

    # Tick past the same-mode min-cycle window (default 8 min) and refresh
    # with the same heat-triggering temperature. The coordinator re-issues
    # the heat command. Critically, `previous_action` must stay None — it
    # was None before and the re-issue isn't a real transition.
    freezer.tick(timedelta(minutes=10))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    zone = store.get_zone("office")
    assert zone["last_action"] == "heating"
    assert zone["previous_action"] is None, (
        "Same-mode re-issue must not overwrite previous_action with self-reference"
    )
    await coordinator.async_unload()


async def test_previous_action_records_prior_non_idle_through_idle_release(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cross-mode gate above is the consumer; this pins the underlying
    storage invariant so a future refactor of `_maybe_apply_action`'s commit
    step that drops `previous_action` tracking gets caught here, not in the
    behavioural tests where the failure is harder to localise."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    store = coordinator._store

    # Heat fires: previous_action is still None (was None before).
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    zone = store.get_zone("office")
    assert zone["last_action"] == "heating"
    assert zone["previous_action"] is None

    # Heat → idle: previous_action becomes heating.
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    zone = store.get_zone("office")
    assert zone["last_action"] == "idle"
    assert zone["previous_action"] == "heating"
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


# ----- predictive control (v0.6) -----


def _seed_idle_drift(
    coordinator: ZoneCoordinator, *, start_temp: float, slope_per_h: float, now: datetime
) -> None:
    """Pre-populate the coordinator's in-memory samples cache with an idle
    drift segment. 16 samples at 120s spacing = 30 minutes of history.
    """
    from custom_components.comfort_band.const import ACTION_IDLE
    from custom_components.comfort_band.predictor import Sample

    slope_per_minute = slope_per_h / 60.0
    samples: list[Sample] = []
    for i in range(16):
        t = now - timedelta(seconds=120 * (15 - i))
        temp = start_temp + slope_per_minute * (120 * i / 60.0)
        samples.append(Sample(t=t, temp=temp, action=ACTION_IDLE))
    coordinator._samples_cache = samples


async def test_predicted_action_populated_when_learning_off(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """With learning_enabled=False (default), the predictor still runs in
    shadow mode: `predicted_decision` reflects what it would issue, but the
    climate calls follow hysteresis."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Steep downward idle drift; room is inside band but projection drops
    # below the deadband entry threshold.
    _seed_idle_drift(coordinator, start_temp=21.0, slope_per_h=-10.0, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.5", {})  # at low, hysteresis says idle
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Predictor anticipates heat (projection: 19.5 - 0.833 = 18.67 < 19.2).
    assert coordinator.data.predicted_decision.action == ACTION_HEAT
    # But learning is OFF -> final decision follows hysteresis (idle: 19.5 not
    # less than 19.2). No heat command issued.
    assert coordinator.data.decision.action == ACTION_IDLE
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert all(c["hvac_mode"] != HVAC_MODE_HEAT for c in set_modes), set_modes
    await coordinator.async_unload()


async def test_learning_on_anticipatory_heat_drives_climate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """With learning_enabled=True, an anticipated heat reaches climate
    earlier than hysteresis would issue it."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _seed_idle_drift(coordinator, start_temp=21.0, slope_per_h=-10.0, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.predicted_decision.action == ACTION_HEAT
    assert coordinator.data.decision.action == ACTION_HEAT
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert any(c["hvac_mode"] == HVAC_MODE_HEAT for c in set_modes), set_modes
    await coordinator.async_unload()


async def test_learning_on_anticipatory_cool_drives_climate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Symmetric to the heat-startup test: with learning ON, a steep upward
    idle drift fires anticipatory cool. Locks in the cool branch of the
    `final_decision = predicted_decision` routing."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _seed_idle_drift(coordinator, start_temp=20.0, slope_per_h=10.0, now=dt_util.utcnow())
    # Room above manual_high (default 22.5) so projection (22.5 + 0.833 = 23.33)
    # crosses the upper deadband threshold (22.5 + 0.5 = 23.0).
    hass.states.async_set(TEMP_ENTITY, "22.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.predicted_decision.action == ACTION_COOL
    assert coordinator.data.decision.action == ACTION_COOL
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert any(c["hvac_mode"] == "cool" for c in set_modes), set_modes
    await coordinator.async_unload()


async def test_learning_on_anticipatory_shutoff_releases_climate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The shutoff branch is unit-tested in test_predictor.py; this test
    locks in the coordinator's routing: with learning ON, an anticipated
    idle release reaches climate.set_hvac_mode(fan_only). Without this,
    a regression in `final_decision = predicted_decision` for the shutoff
    path would only be caught by the unit test."""
    from custom_components.comfort_band.predictor import Sample

    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    # First fire heat normally so last_action=heat and the buffer has a heat run.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_HEAT
    climate_calls.clear()

    # Seed a steep heat recovery slope so the predictor anticipates overshoot.
    # Build 10 heat samples at 120s intervals with slope +20 °C/h.
    now = dt_util.utcnow()
    slope_per_min = 20.0 / 60.0
    samples: list[Sample] = []
    for i in range(10):
        t = now - timedelta(seconds=120 * (9 - i))
        temp = 19.0 + slope_per_min * (120 * i / 60.0)
        samples.append(Sample(t=t, temp=temp, action=ACTION_HEAT))
    coordinator._samples_cache = samples
    # Room still well below manual_low=19.5 so hysteresis would keep heating;
    # projection 19.0 + 20/60*5 = 20.67 >= 19.5 -> predictor releases.
    freezer.tick(timedelta(minutes=10))  # past min_cycle window
    hass.states.async_set(TEMP_ENTITY, "19.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    assert coordinator.data.decision.action == ACTION_IDLE
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert any(c["hvac_mode"] == HVAC_MODE_FAN_ONLY for c in set_modes), set_modes
    await coordinator.async_unload()


async def test_predictor_heat_suppressed_by_same_mode_gate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Composition with v0.4 gate: an anticipated heat within the same-mode
    min-cycle window after a prior heat must still be suppressed. The
    cross-mode test covers the v0.5 gate; this locks in the v0.4 gate
    behaviour against predictor-issued decisions."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)

    # Fire a heat normally so last_action=heat with a recent last_action_at.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_HEAT

    # 3 minutes later (still within default min_cycle_minutes=8): seed a
    # steep idle drift so the predictor would anticipate heat -- but
    # current_action=ACTION_HEAT so predictor's idle/startup branch doesn't
    # fire. Hysteresis would say keep heating (room=19.0 < low). Predictor
    # decision is also heat. Same-mode gate must suppress the re-issue.
    climate_calls.clear()
    freezer.tick(timedelta(minutes=3))
    hass.states.async_set(TEMP_ENTITY, "19.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert set_modes == [], f"same-mode gate should suppress re-issue: {set_modes}"
    await coordinator.async_unload()


async def test_predictor_cool_suppressed_by_cross_mode_gate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Composition with v0.5 gate: an anticipated cool after a recent heat
    release must still be suppressed by the cross-mode min-cycle gate."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)

    # Run a heat → idle cycle so previous_action=heating, last_action=idle
    # and last_action_at sits just a few minutes ago (within the default
    # cross_mode_min_minutes=8).
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_HEAT
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    zone = coordinator._store.get_zone("office")
    assert zone["previous_action"] == "heating"

    # Now seed a steep UPWARD idle drift so the predictor wants to cool,
    # and try a refresh within the 8-min dwell window.
    climate_calls.clear()
    freezer.tick(timedelta(minutes=3))
    _seed_idle_drift(coordinator, start_temp=20.0, slope_per_h=10.0, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "22.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Predictor wants cool, but the v0.5 cross-mode gate must suppress it.
    assert coordinator.data.predicted_decision.action == ACTION_COOL
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert all(c["hvac_mode"] != "cool" for c in set_modes), set_modes
    await coordinator.async_unload()


async def test_samples_accumulate_and_persist(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A series of refreshes (with action transitions and time between them)
    should leave the rolling buffer populated and persisted to the store."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Refresh again >60s later with same temp -> rate-limit holds, same action.
    freezer.tick(timedelta(seconds=90))
    hass.states.async_set(TEMP_ENTITY, "18.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Transition to idle (different action) -- always recorded.
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    persisted = coordinator._store.get_zone("office")["samples"]
    assert len(persisted) >= 2  # at least the first heat sample + the idle transition
    actions = {s["action"] for s in persisted}
    assert "heating" in actions
    assert "idle" in actions
    await coordinator.async_unload()


async def test_manual_climate_edit_flushes_buffer(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A climate state change that doesn't match our last command (and isn't
    within the 30 s echo window) is treated as a manual edit -- flush samples
    to prevent slope-estimator poisoning."""
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # Establish a baseline command + buffer.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(coordinator._samples_cache) >= 1
    assert coordinator._last_command_state is not None

    # Simulate a manual edit well outside the echo window: someone sets
    # hvac_mode=cool externally while we last commanded heat.
    freezer.tick(timedelta(minutes=10))
    old_state = State(CLIMATE_ENTITY, "heat", {"temperature": 19.5})
    new_state = State(CLIMATE_ENTITY, "cool", {"temperature": 23.0})
    event: Event[EventStateChangedData] = Event(
        "state_changed",
        {"entity_id": CLIMATE_ENTITY, "old_state": old_state, "new_state": new_state},
    )
    coordinator._on_climate_state_change(event)
    await hass.async_block_till_done()

    assert coordinator._samples_cache == []
    assert coordinator._store.get_zone("office")["samples"] == []
    await coordinator.async_unload()


async def test_same_action_sample_throttled_after_first_persist(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Locks in the SD-card-wear mitigation: a same-action sample within
    SAMPLE_PERSIST_INTERVAL_S of the last persist must NOT touch the store.
    The in-memory cache still grows, but flash writes are bounded."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # First refresh persists immediately (no prior persist).
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    first_persisted = coordinator._store.get_zone("office")["samples"]
    assert len(first_persisted) >= 1
    assert coordinator._last_sample_persist_at is not None
    in_memory_after_first = len(coordinator._samples_cache)

    # Tick 2 minutes (well inside SAMPLE_PERSIST_INTERVAL_S=300) and refresh
    # with same temp range -> same action (heating) continues, no transition.
    # In-memory cache should grow but the persisted samples should not.
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "18.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    second_persisted = coordinator._store.get_zone("office")["samples"]
    assert second_persisted == first_persisted  # store unchanged
    assert len(coordinator._samples_cache) > in_memory_after_first  # cache grew
    await coordinator.async_unload()


async def test_action_unknown_refresh_appends_no_sample(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """When the room sensor is unavailable, hysteresis returns ACTION_UNKNOWN
    and `_maybe_apply_action` returns without issuing climate calls OR
    appending a sample (no useful temp value to record). Locks in the early-
    return at the top of the function so future refactors don't accidentally
    start recording unknown-action samples."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # No state ever set for TEMP_ENTITY → room is None → ACTION_UNKNOWN.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_UNKNOWN
    assert _calls_for(climate_calls, "set_hvac_mode") == []
    assert coordinator._samples_cache == []
    await coordinator.async_unload()


async def test_shadow_mode_still_records_samples(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """With enabled=False (shadow mode) the integration does not command
    climate, but it must still record samples so the predictor's buffer is
    populated when the user later flips `enabled=True`. Without this, every
    new install would face a ~90-min cold-start after enabling."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    # NOTE: not calling _setup_enabled_zone — leaving enabled=False.
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)

    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # No climate calls (shadow mode), but the buffer should have grown.
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert set_modes == []
    assert len(coordinator._samples_cache) >= 1
    await coordinator.async_unload()


async def test_climate_state_echo_does_not_flush(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """State changes within 30 s of our last command are echoes of our own
    write -- update the baseline but do NOT flush samples."""
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    samples_before = list(coordinator._samples_cache)
    assert len(samples_before) >= 1

    # Echo arrives 2 s after our command (well inside the 30 s window).
    freezer.tick(timedelta(seconds=2))
    old_state = State(CLIMATE_ENTITY, "off", {"temperature": None})
    new_state = State(CLIMATE_ENTITY, "heat", {"temperature": 19.5})
    event: Event[EventStateChangedData] = Event(
        "state_changed",
        {"entity_id": CLIMATE_ENTITY, "old_state": old_state, "new_state": new_state},
    )
    coordinator._on_climate_state_change(event)
    await hass.async_block_till_done()

    assert coordinator._samples_cache == samples_before
    await coordinator.async_unload()


async def test_passive_tolerance_threaded_to_predictor(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sets `passive_tolerance=0` on the zone, then drives a setup where
    the v0.7 passive branch would otherwise suppress heat. With the tunable
    at zero the comfort floor is infinitely tight, so heat must fire --
    proves the per-zone value actually reaches `predictor.decide()`."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone(
        "office", learning_enabled=True, passive_tolerance=0.0
    )
    # Room below deadband entry (hyst would fire heat) + warming slope
    # whose projection lands inside band: passive branch would suppress
    # with default tolerance, but must NOT suppress with passive_tolerance=0.
    _seed_idle_drift(coordinator, start_temp=18.0, slope_per_h=8.0, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.0", {})  # default low=19.5, db_below=0.3 -> hyst heats
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert any(c["hvac_mode"] == HVAC_MODE_HEAT for c in set_modes), set_modes
    await coordinator.async_unload()


async def test_passive_acceptance_suppresses_heat_end_to_end(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Positive-path coordinator test: with default `passive_tolerance=0.5`
    and learning ON, a room just inside the comfort floor with a warming
    idle slope should produce NO heat command -- the predictor's passive
    branch propagates all the way to climate. Catches regressions where
    the threading silently passes a hard-coded default."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    # Default low=19.5, deadband_below=0.3 -> hyst entry at 19.2. Room at
    # 19.1 (0.1 °C inside the hyst-heat zone), warming slope, default
    # passive_tolerance=0.5 (comfort floor at low-0.5=19.0; room 19.1 >=
    # 19.0). Suppression should propagate end-to-end -> no heat command.
    _seed_idle_drift(coordinator, start_temp=18.0, slope_per_h=15.0, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.1", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    # final_decision should equal predicted_decision when learning_enabled=True.
    # Asserting both fields catches a future refactor that accidentally breaks
    # the routing (e.g., always passing hyst_decision regardless of the gate).
    assert coordinator.data.decision.action == ACTION_IDLE
    set_modes = _calls_for(climate_calls, "set_hvac_mode")
    assert all(c["hvac_mode"] != HVAC_MODE_HEAT for c in set_modes), set_modes
    await coordinator.async_unload()


# ----- v0.8 MPC -----


def _seed_full_slope_data(coordinator: ZoneCoordinator, *, now: datetime) -> None:
    """Pre-populate samples covering idle / heat / cool trailing runs so MPC's
    `is_ready` check returns True. Layout (oldest → newest):
      - cool segment 60-50 min ago
      - heat segment 40-30 min ago
      - idle segment 20-10 min ago
    `_latest_run_of` walks backwards by action class, so each segment is
    recoverable independently. WLS recency weighting (τ=20 min) means the
    most recent (idle) gets full weight; older segments still produce a
    slope estimate.
    """
    from custom_components.comfort_band.const import ACTION_COOL, ACTION_HEAT, ACTION_IDLE
    from custom_components.comfort_band.predictor import Sample

    samples: list[Sample] = []
    base = now - timedelta(minutes=60)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=22.0 - 0.02 * i,
                action=ACTION_COOL,
            )
        )
    base = now - timedelta(minutes=40)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=20.0 + 0.04 * i,
                action=ACTION_HEAT,
            )
        )
    base = now - timedelta(minutes=20)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=21.0,
                action=ACTION_IDLE,
            )
        )
    coordinator._samples_cache = samples


async def test_three_way_gate_routes_to_mpc_when_enabled_and_ready(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """With learning_enabled=True, mpc_enabled=True, and full slope data,
    the final decision must equal MPC's decision (not the predictor's).

    Setup: room just above hyst deadband (no heat from hyst), positive heat
    slope (MPC's heat candidate stays in band, idle drift stays flat).
    Hyst+predictor both say idle; MPC picks heat. The divergence proves
    the gate routed to MPC.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True, mpc_enabled=True)
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is True
    # The shadow signals diverge: hyst+predictor say idle, MPC says heat.
    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    assert coordinator.data.mpc_decision.action == ACTION_HEAT
    # The gate routed to MPC's decision.
    assert coordinator.data.decision.action == ACTION_HEAT
    # v0.8 contract: MPC's heat action targets the band's *high* edge (not
    # `low`), so the climate keeps heating until MPC itself elects idle.
    # Pin this end-to-end — `test_mpc.py` covers it at the unit level but
    # only the integration path proves the high-edge value reaches climate.
    assert coordinator.data.decision.target_temp == coordinator.data.effective_high
    await coordinator.async_unload()


async def test_three_way_gate_routes_to_predictor_when_mpc_disabled(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """learning_enabled=True, mpc_enabled=False → final == predictor.
    Even with full slope data available, MPC's decision is computed in
    shadow but not used.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # mpc_ready stays exposed; MPC's shadow decision still populates.
    assert coordinator.data.mpc_ready is True
    # But the gate uses predictor (idle for this setup), not MPC (heat).
    assert coordinator.data.decision.action == coordinator.data.predicted_decision.action
    assert coordinator.data.decision.action != coordinator.data.mpc_decision.action
    await coordinator.async_unload()


async def test_three_way_gate_routes_to_predictor_when_mpc_enabled_but_not_ready(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """learning_enabled=True, mpc_enabled=True, but cold start (only idle
    samples present, recovery_heat/cool missing) → MPC silently falls back
    to predictor. Tests the cold-start UX: user opts in but MPC waits for
    data without affecting behaviour.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True, mpc_enabled=True)
    # Only idle samples — recovery_heat and recovery_cool slopes will be None.
    _seed_idle_drift(coordinator, start_temp=21.0, slope_per_h=0.0, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is False
    # mpc_decision equals predictor_decision (the fallback path).
    assert coordinator.data.mpc_decision == coordinator.data.predicted_decision
    # And the gate's `elif learning_enabled` branch governs final_decision.
    assert coordinator.data.decision == coordinator.data.predicted_decision
    await coordinator.async_unload()


async def test_three_way_gate_routes_to_hysteresis_when_learning_off(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """learning_enabled=False (default) → final == hysteresis, even if
    mpc_enabled is True (mpc_enabled is layered on learning_enabled). Locks
    in that flipping mpc_enabled alone doesn't bypass the master gate.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Deliberately set mpc_enabled=True but leave learning_enabled=False.
    await coordinator._store.async_update_zone("office", mpc_enabled=True)
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # mpc_ready exposes the data state regardless of switches.
    assert coordinator.data.mpc_ready is True
    # But the gate ignores MPC because learning is off.
    # Hyst says idle (room 19.3 > deadband entry 19.2).
    assert coordinator.data.decision.action == ACTION_IDLE
    await coordinator.async_unload()


async def test_sample_records_fan_mode_from_climate_state(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """The coordinator reads the climate's current `fan_mode` attribute and
    threads it into the appended sample. v0.9 partitions slopes by
    `(action, fan_mode)`; the data has to be in the buffer to use later.
    """
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"fan_mode": "high"})
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    fan_modes = [s.fan_mode for s in coordinator._samples_cache]
    assert "high" in fan_modes, fan_modes
    await coordinator.async_unload()


async def test_sample_records_none_fan_mode_when_attribute_missing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """When the climate entity is missing or its `fan_mode` attribute isn't
    present, samples get fan_mode=None — they don't drop or raise. Some
    climate platforms simply don't expose fan_mode at all.
    """
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Climate entity exists but no fan_mode attribute.
    hass.states.async_set(CLIMATE_ENTITY, "off", {})
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(coordinator._samples_cache) >= 1
    assert all(s.fan_mode is None for s in coordinator._samples_cache)
    await coordinator.async_unload()


async def test_target_temp_rounded_to_climate_step_0_5(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """A heat target that's not aligned to the climate's 0.5 °C step gets
    rounded before the service call. Without this, the climate platform
    coerces silently and our `_last_command_state` snapshot mismatches the
    observed state on every refresh.
    """
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"target_temp_step": 0.5})
    # Custom manual_low that doesn't align to 0.5 (19.7).
    await coordinator._store.async_update_zone("office", manual_low=19.7)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_temps = _calls_for(climate_calls, "set_temperature")
    # 19.7 / 0.5 = 39.4 → banker's rounding gives 39 → 39 * 0.5 = 19.5.
    assert any(c["temperature"] == 19.5 for c in set_temps), set_temps
    await coordinator.async_unload()


async def test_target_temp_rounded_to_climate_step_0_1(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Climate entities advertising a finer step (e.g., 0.1 °C, common on
    some Mitsubishi units) accept the precise value — no rounding loss."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"target_temp_step": 0.1})
    await coordinator._store.async_update_zone("office", manual_low=19.7)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_temps = _calls_for(climate_calls, "set_temperature")
    # 19.7 / 0.1 = 197 → 197 * 0.1 = 19.7 (modulo float epsilon).
    assert any(abs(c["temperature"] - 19.7) < 1e-6 for c in set_temps), set_temps
    await coordinator.async_unload()


async def test_target_temp_rounded_to_default_step_when_attribute_missing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """When the climate entity lacks `target_temp_step`, fall back to 0.5 °C
    (the most common HVAC resolution). Without this fallback, missing-
    attribute climates would receive precise float setpoints they may
    silently coerce.
    """
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Climate state set but no target_temp_step attribute.
    hass.states.async_set(CLIMATE_ENTITY, "off", {})
    await coordinator._store.async_update_zone("office", manual_low=19.7)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_temps = _calls_for(climate_calls, "set_temperature")
    # 19.7 rounded to 0.5 step → 19.5.
    assert any(c["temperature"] == 19.5 for c in set_temps), set_temps
    await coordinator.async_unload()


async def test_target_temp_passes_through_when_climate_reports_zero_step(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Defensive: a corrupt/0 climate attribute mustn't cause a divide-by-zero
    in `_round_to_step`. The raw setpoint should pass through unchanged.
    `step <= 0` is the explicit guard in `_round_to_step`; this pins it.
    """
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"target_temp_step": 0})
    await coordinator._store.async_update_zone("office", manual_low=19.7)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_temps = _calls_for(climate_calls, "set_temperature")
    # No rounding applied -- precise input value passes through.
    assert any(abs(c["temperature"] - 19.7) < 1e-6 for c in set_temps), set_temps
    await coordinator.async_unload()


async def test_target_temp_falls_back_to_default_step_on_nan_attribute(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """A misbehaving climate platform could advertise `target_temp_step=nan`.
    `float("nan")` succeeds, so without an explicit `math.isfinite` guard the
    NaN propagates into `_round_to_step`, where `int(round(x / nan))` raises
    ValueError and crashes `_maybe_apply_action` on every refresh.

    Pins the `math.isfinite` guard in `_target_temp_step` — NaN must fall
    back to the default 0.5 °C step.
    """
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"target_temp_step": float("nan")})
    await coordinator._store.async_update_zone("office", manual_low=19.7)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    set_temps = _calls_for(climate_calls, "set_temperature")
    # 19.7 rounded to the fallback 0.5 step -> 19.5. The key assertion is
    # that we reach this point at all (no ValueError crash).
    assert any(c["temperature"] == 19.5 for c in set_temps), set_temps
    await coordinator.async_unload()


async def test_fan_mode_change_does_not_flush_sample_buffer(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.10.1 regression: a fan-mode-only change must NOT flush the buffer.

    Pre-v0.10.1 the manual-edit detector compared `fan_mode`, so the HVAC's
    own autonomous fan modulation (or a different fan_mode in fan_only vs
    heat) flushed the learning buffer. v0.10.1 compares only `hvac_mode` +
    `target_temp`; fan_mode is still captured per-sample but no longer forces
    a flush. Here the observed state matches the baseline on hvac_mode +
    target_temp and differs ONLY in fan_mode, well outside the echo window.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Establish a baseline command + buffer.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(coordinator._samples_cache) >= 1
    before = list(coordinator._samples_cache)

    # Pin a baseline matching the incoming event on hvac_mode + target_temp,
    # differing ONLY in fan_mode; place the last command well outside the
    # echo window so the change is treated as a candidate "edit".
    coordinator._last_command_state = {"hvac_mode": "heat", "target_temp": 19.5}
    coordinator._last_command_at = dt_util.utcnow() - timedelta(minutes=10)
    old_state = State(CLIMATE_ENTITY, "heat", {"temperature": 19.5, "fan_mode": "low"})
    new_state = State(CLIMATE_ENTITY, "heat", {"temperature": 19.5, "fan_mode": "high"})
    event: Event[EventStateChangedData] = Event(
        "state_changed",
        {"entity_id": CLIMATE_ENTITY, "old_state": old_state, "new_state": new_state},
    )
    coordinator._on_climate_state_change(event)
    await hass.async_block_till_done()

    # Buffer preserved (was [] under the pre-v0.10.1 fan_mode comparison).
    assert coordinator._samples_cache == before
    await coordinator.async_unload()


async def test_autonomous_fan_change_preserves_cross_segment_buffer(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.10.1 end-to-end for the gym's production bug: with idle + heat
    samples both in the buffer, an autonomous fan-mode change must keep BOTH
    segments. Pre-v0.10.1 each idle<->heat transition's fan settle flushed the
    buffer, so it never held idle AND recovery samples at once → idle_slope
    stayed None → mpc_ready never True → MPC silently fell back to the
    reactive predictor (no schedule-lookahead pre-heat).
    """
    from homeassistant.core import Event, EventStateChangedData, State

    from custom_components.comfort_band.const import ACTION_HEAT, ACTION_IDLE

    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Seed a buffer that already spans idle + heat (what mpc_ready needs).
    _seed_heat_only_slope_data(coordinator, now=dt_util.utcnow())
    seeded_actions = {s.action for s in coordinator._samples_cache}
    assert ACTION_IDLE in seeded_actions and ACTION_HEAT in seeded_actions

    # The HVAC settles its fan to a new speed after a heat command — same
    # hvac_mode + target_temp, only fan_mode differs, outside the echo window.
    coordinator._last_command_state = {"hvac_mode": "heat", "target_temp": 19.5}
    coordinator._last_command_at = dt_util.utcnow() - timedelta(minutes=10)
    old_state = State(CLIMATE_ENTITY, "heat", {"temperature": 19.5, "fan_mode": "low"})
    new_state = State(CLIMATE_ENTITY, "heat", {"temperature": 19.5, "fan_mode": "high"})
    event: Event[EventStateChangedData] = Event(
        "state_changed",
        {"entity_id": CLIMATE_ENTITY, "old_state": old_state, "new_state": new_state},
    )
    coordinator._on_climate_state_change(event)
    await hass.async_block_till_done()

    # Both segments survive → idle_slope + recovery_heat both derivable.
    surviving = {s.action for s in coordinator._samples_cache}
    assert ACTION_IDLE in surviving and ACTION_HEAT in surviving
    await coordinator.async_unload()


async def test_mpc_ready_false_with_empty_buffer(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Fresh install / fresh restart with empty buffer → mpc_ready False.
    The binary sensor reflects this directly."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is False
    await coordinator.async_unload()


async def test_mpc_ready_true_with_full_slope_data(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Mirror of the above: with idle + heat + cool segments all having
    enough samples, mpc_ready True."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is True
    await coordinator.async_unload()


def _seed_heat_only_slope_data(coordinator: ZoneCoordinator, *, now: datetime) -> None:
    """v0.8.1 helper: pre-populate samples covering idle + heat only, leaving
    `recovery_cool` unestablished. Models a heat-only zone (winter, or fresh
    install in cold months) at the point where MPC should be eligible to
    activate under the relaxed v0.8.1 gate.
    """
    from custom_components.comfort_band.const import ACTION_HEAT, ACTION_IDLE
    from custom_components.comfort_band.predictor import Sample

    samples: list[Sample] = []
    base = now - timedelta(minutes=40)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=20.0 + 0.04 * i,
                action=ACTION_HEAT,
            )
        )
    base = now - timedelta(minutes=20)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=21.0,
                action=ACTION_IDLE,
            )
        )
    coordinator._samples_cache = samples


async def test_mpc_ready_true_with_only_idle_and_heat_slopes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.8.1 integration: a heat-only zone (no cool segment in the buffer)
    must reach `mpc_ready=True` at the coordinator level — not just at the
    `mpc.is_ready` unit-test level. Catches a regression where the
    coordinator's `is_ready` call site (e.g. passing stale slopes) could
    silently keep MPC off for unilateral-mode zones.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    _seed_heat_only_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is True
    # And the recovery_cool slope is genuinely absent (proves the relaxation,
    # not that we accidentally generated cool data).
    assert coordinator.data.thermal_slopes.recovery_cool is None
    await coordinator.async_unload()


def _seed_cool_only_slope_data(coordinator: ZoneCoordinator, *, now: datetime) -> None:
    """Symmetric to `_seed_heat_only_slope_data`: idle + cool segments, no
    heat data. Models a cool-only zone (summer install) at the point where
    v0.8.1's relaxed gate should activate MPC.
    """
    from custom_components.comfort_band.const import ACTION_COOL, ACTION_IDLE
    from custom_components.comfort_band.predictor import Sample

    samples: list[Sample] = []
    base = now - timedelta(minutes=40)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=22.0 - 0.04 * i,
                action=ACTION_COOL,
            )
        )
    base = now - timedelta(minutes=20)
    for i in range(6):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=21.0,
                action=ACTION_IDLE,
            )
        )
    coordinator._samples_cache = samples


async def test_mpc_ready_true_with_only_idle_and_cool_slopes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Symmetric to the heat-only integration test: cool-only zone (summer
    install) must reach `mpc_ready=True` at the coordinator level. The
    heat-only test on its own only proves one direction of the symmetric
    `is_ready` logic.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    _seed_cool_only_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is True
    assert coordinator.data.thermal_slopes.recovery_heat is None
    await coordinator.async_unload()


async def test_three_way_gate_routes_to_mpc_with_heat_only_slopes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.8.1 end-to-end: with `learning_enabled=True`, `mpc_enabled=True`,
    and heat-only slope data, the coordinator's three-way gate must route
    the final decision through MPC — not just expose `mpc_ready=True`.

    Pins the interaction the heat-only `mpc_ready` test alone doesn't cover:
    a future refactor that silently kept the gate falling through to
    predictor for partial-slope zones would pass the readiness assertion
    but fail this one.

    Setup: room at 19.4 (above hyst deadband entry 19.2 so hyst says idle;
    below low=19.5 but recovery_heat IS available so the room-below-band
    bail-out doesn't fire — the bail-out's `recovery_heat is None` clause
    is False). Idle slope flat; heat slope positive. MPC sees heat
    candidate scoring more time-in-band than idle drifting flat through
    low, and picks heat.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True, mpc_enabled=True)
    _seed_heat_only_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "19.4", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.mpc_ready is True
    # mpc_decision diverges from predictor — proves MPC ran, not the predictor.
    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    assert coordinator.data.mpc_decision.action == ACTION_HEAT
    # Final decision routed through MPC, not predictor.
    assert coordinator.data.decision.action == ACTION_HEAT
    await coordinator.async_unload()


async def test_safety_bailout_routes_through_predictor_when_room_outside_band(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.8.1 end-to-end for the safety bail-out path: cool-only zone with
    `mpc_ready=True`, but the room has dropped below band where MPC can't
    model heating. `mpc.plan` returns the predictor's decision unchanged;
    the gate forwards that to climate, so `decision == predicted_decision`.

    Pins that the bail-out (the main risk surface of v0.8.1) is wired
    correctly end-to-end. The unit test in test_mpc.py covers `plan`'s
    return; this one covers the coordinator's threading of that return
    through the gate.
    """
    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True, mpc_enabled=True)
    # Cool-only zone (recovery_heat=None). Room below low (default 19.5).
    _seed_cool_only_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "18.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # mpc_ready stays True — MPC is equipped to act in general, just not
    # for this specific out-of-band scenario.
    assert coordinator.data.mpc_ready is True
    assert coordinator.data.thermal_slopes.recovery_heat is None
    # Bail-out fired: mpc_decision == predicted_decision.
    assert coordinator.data.mpc_decision == coordinator.data.predicted_decision
    # And the gate forwarded that to the final decision (which the
    # coordinator hands to climate). Predictor's hysteresis fallback sees
    # room < low - deadband_below (18.5 < 19.2), so it fires heat.
    assert coordinator.data.decision == coordinator.data.predicted_decision
    assert coordinator.data.decision.action == ACTION_HEAT
    await coordinator.async_unload()


# ----- schedule lookahead via bands_per_step (v0.9.0+) -----


async def test_mpc_receives_bands_per_step_when_schedule_present(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.9.0 integration: when a zone has a scheduled profile, the
    coordinator computes per-step (low, high) over the MPC horizon and
    passes it to `mpc.plan` as `bands_per_step`. Spy on `mpc.plan` to
    confirm the wiring; assert the captured list length matches the
    horizon and that the first entry is the band active NOW.
    """
    from unittest.mock import patch

    freezer.move_to("2026-05-19 05:30:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Install a morning-ramp schedule on the home profile: overnight band
    # (16, 19) until 07:00, then morning band (20, 22) until 22:00, then
    # back to overnight.
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
            {"at": "22:00", "low": 16.0, "high": 19.0},
        ],
    )
    await coordinator._store.async_update_zone(
        "office",
        learning_enabled=True,
        mpc_enabled=True,
        mpc_horizon_minutes=60,
    )
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "17.5", {})

    with patch(
        "custom_components.comfort_band.coordinator.mpc.plan",
        wraps=__import__("custom_components.comfort_band.mpc", fromlist=["plan"]).plan,
    ) as plan_spy:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert plan_spy.called
    call_kwargs = plan_spy.call_args.kwargs
    bands_per_step = call_kwargs["bands_per_step"]
    assert bands_per_step is not None
    assert len(bands_per_step) == 60  # MPC_HORIZON / 1-min step
    # First entry is the band active right now (05:30 → overnight band).
    assert bands_per_step[0] == (16.0, 19.0)
    # An entry past 07:00 (e.g. minute 95 from 05:30) would be the
    # morning band; but our horizon is 60 min (covers 05:30 - 06:30),
    # entirely within the overnight band.
    assert bands_per_step[-1] == (16.0, 19.0)
    await coordinator.async_unload()


async def test_mpc_bands_per_step_none_when_override_active(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Override active → snapshot semantics are correct (the manual
    band holds until expiry, no schedule transitions to anticipate).
    Coordinator passes `bands_per_step=None`, MPC falls back to its
    snapshot path using `inputs.low / inputs.high`. Pins the design
    choice to keep the code path simple in the override case.
    """
    from unittest.mock import patch

    freezer.move_to("2026-05-19 05:30:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Install a schedule + activate an override; bands_per_step should
    # be None because the manual band overrides the schedule.
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
        ],
    )
    override_until = dt_util.utcnow() + timedelta(hours=2)
    await coordinator._store.async_update_zone(
        "office",
        learning_enabled=True,
        mpc_enabled=True,
        override_until=override_until.isoformat(),
    )
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "20.0", {})

    with patch(
        "custom_components.comfort_band.coordinator.mpc.plan",
        wraps=__import__("custom_components.comfort_band.mpc", fromlist=["plan"]).plan,
    ) as plan_spy:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert plan_spy.called
    assert plan_spy.call_args.kwargs["bands_per_step"] is None
    await coordinator.async_unload()


async def test_mpc_bands_per_step_none_when_no_schedule(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """No schedule configured (default install state) → bands_per_step
    is None; MPC uses the snapshot manual_low / manual_high. Symmetric
    with the override case — both are "no schedule transitions to
    anticipate" paths.
    """
    from unittest.mock import patch

    freezer.move_to("2026-05-19 05:30:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True, mpc_enabled=True)
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "20.0", {})

    with patch(
        "custom_components.comfort_band.coordinator.mpc.plan",
        wraps=__import__("custom_components.comfort_band.mpc", fromlist=["plan"]).plan,
    ) as plan_spy:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert plan_spy.called
    assert plan_spy.call_args.kwargs["bands_per_step"] is None
    await coordinator.async_unload()


# ----- band-ramp smoothing (v0.10.0) -----


async def test_band_ramp_smooths_schedule_resolve(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.10.0 integration: when ``band_ramp_minutes > 0``, the effective
    band at a moment inside the ramp window is the linear interpolation of
    the adjacent transitions — not the stepped value. Freeze 7 min before
    the 07:00 transition (16,19) → (20,22) with ramp=30; the band sits
    between the two endpoints. Pins the wiring of ``ramp_minutes`` through
    ``_resolve_schedule`` → ``schedule.resolve``.
    """
    # Pin HA timezone to UTC so the freezer time matches `dt_util.now().time()`.
    # Without this, the pytest-homeassistant fixture defaults to US/Pacific
    # and schedules resolve against an offset local clock.
    await hass.config.async_set_time_zone("UTC")
    # 06:53 → 7 min before the 07:00 transition. With ramp=30 the ramp
    # window is 06:45-07:15; 06:53 is 8 min into it (progress 8/30).
    freezer.move_to("2026-05-19 06:53:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
            {"at": "22:00", "low": 16.0, "high": 19.0},
        ],
    )
    await coordinator._store.async_update_zone(
        "office",
        band_ramp_minutes=30,
    )
    hass.states.async_set(TEMP_ENTITY, "18.5", {})

    try:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # Stepped band at 06:53 would still be (16, 19); ramped value sits
        # between the two adjacent bands. Loose bounds suffice — the exact
        # arithmetic is pinned in test_schedule.py's ramp tests.
        eff_low = coordinator.data.effective_low
        eff_high = coordinator.data.effective_high
        assert 16.0 < eff_low < 20.0, f"expected smoothed low, got {eff_low}"
        assert 19.0 < eff_high < 22.0, f"expected smoothed high, got {eff_high}"
    finally:
        await coordinator.async_unload()


async def test_band_ramp_passes_through_to_upcoming_bands(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """``band_ramp_minutes`` is forwarded to ``schedule.upcoming_bands``
    so the MPC lookahead sees the same ramp as the live decision path.
    Spy on the helper; assert the kwarg is what the coordinator stored.
    """
    from unittest.mock import patch

    freezer.move_to("2026-05-19 05:30:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
        ],
    )
    await coordinator._store.async_update_zone(
        "office",
        learning_enabled=True,
        mpc_enabled=True,
        mpc_horizon_minutes=60,
        band_ramp_minutes=30,
    )
    _seed_full_slope_data(coordinator, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "17.5", {})

    with patch(
        "custom_components.comfort_band.coordinator.schedule.upcoming_bands",
        wraps=__import__(
            "custom_components.comfort_band.schedule", fromlist=["upcoming_bands"]
        ).upcoming_bands,
    ) as ub_spy:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert ub_spy.called
    assert ub_spy.call_args.kwargs["ramp_minutes"] == 30
    await coordinator.async_unload()


async def test_band_ramp_zero_keeps_stepped_behaviour(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Default ``band_ramp_minutes=0`` preserves v0.9.x stepped behaviour:
    at 06:53 (inside what would be the ramp window if enabled), the
    effective band still equals the pre-transition value exactly.
    """
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-05-19 06:53:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
            {"at": "22:00", "low": 16.0, "high": 19.0},
        ],
    )
    # band_ramp_minutes is 0 by default; assert that explicitly to pin
    # the default rather than just relying on _setup_enabled_zone.
    assert coordinator._store.get_zone("office")["band_ramp_minutes"] == 0
    hass.states.async_set(TEMP_ENTITY, "18.5", {})

    try:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.data.effective_low == 16.0
        assert coordinator.data.effective_high == 19.0
    finally:
        await coordinator.async_unload()


async def test_band_ramp_schedules_timer_at_leading_edge(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.10.0 R5 fix: with ``band_ramp_minutes > 0`` the next-transition
    timer fires at ``t - ramp/2`` instead of at ``t``, so the leading
    half of the ramp isn't forfeited in quiet rooms. Spy on
    ``async_call_later`` (the coordinator's only timer source) and
    assert the delay matches the leading-edge target.
    """
    from unittest.mock import patch

    await hass.config.async_set_time_zone("UTC")
    # 06:00 (well before any transition); next transition is 07:00.
    freezer.move_to("2026-05-19 06:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
        ],
    )
    await coordinator._store.async_update_zone("office", band_ramp_minutes=30)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})

    captured_delays: list[float] = []

    # Patch the `async_call_later` symbol the coordinator imports. Capture
    # the delay arg of every call, then delegate to the real function so
    # the runtime stays consistent.
    from homeassistant.helpers.event import async_call_later as real_call_later

    def _spy_call_later(hass_: Any, delay: float, action: Any) -> Any:
        captured_delays.append(delay)
        return real_call_later(hass_, delay, action)

    try:
        with patch(
            "custom_components.comfort_band.coordinator.async_call_later",
            side_effect=_spy_call_later,
        ):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        # The transition timer should have been scheduled for 07:00 - 15min
        # = 06:45 (= 2700 secs from 06:00). Capped at _MAX_NEXT_TRANSITION_SECS
        # = 3600. 2700 is well within that, so the captured delay should
        # equal 2700 (within a few seconds of clock slop).
        #
        # Debounce timer also calls async_call_later, so filter to the
        # value closest to our target rather than asserting "exactly one
        # call." The non-debounce delay is the transition timer.
        assert captured_delays, "no async_call_later calls captured"
        # Look for a captured delay near 2700s (allow ±5s for clock skew).
        target = 7 * 3600 - 6 * 3600 - 15 * 60  # 2700
        matching = [d for d in captured_delays if abs(d - target) < 5]
        assert matching, (
            f"expected a transition timer scheduled around {target}s "
            f"(leading edge of 07:00 transition with ramp=30 from 06:00), "
            f"got delays {captured_delays}"
        )
    finally:
        await coordinator.async_unload()


async def test_band_ramp_inside_window_keeps_bare_transition_timer(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Guard against the re-fire loop the leading-edge subtraction would
    otherwise cause: when ``now`` is already inside the ramp window (the
    leading edge has passed), the timer falls back to waking at the bare
    transition time. Without this guard, a refresh at e.g. 06:50 would
    schedule for 06:45 (already past) → clamp to 1s → fire → resolve →
    re-schedule for 06:45 → ... in a tight loop.
    """
    from unittest.mock import patch

    await hass.config.async_set_time_zone("UTC")
    # 06:50 — inside the 07:00 ramp window (06:45-07:15 with ramp=30).
    # Transition is 10 min away; leading edge was 5 min ago.
    freezer.move_to("2026-05-19 06:50:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 16.0, "high": 19.0},
            {"at": "07:00", "low": 20.0, "high": 22.0},
        ],
    )
    await coordinator._store.async_update_zone("office", band_ramp_minutes=30)
    hass.states.async_set(TEMP_ENTITY, "18.0", {})

    captured_delays: list[float] = []
    from homeassistant.helpers.event import async_call_later as real_call_later

    def _spy_call_later(hass_: Any, delay: float, action: Any) -> Any:
        captured_delays.append(delay)
        return real_call_later(hass_, delay, action)

    try:
        with patch(
            "custom_components.comfort_band.coordinator.async_call_later",
            side_effect=_spy_call_later,
        ):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        # Bare-transition target is 07:00 - 06:50 = 600s. With the guard,
        # the timer stays at 600s (not 600 - 900 = -300 clamped to 1).
        target = 600  # 10 min x 60
        matching = [d for d in captured_delays if abs(d - target) < 5]
        assert matching, (
            f"expected bare-transition timer at {target}s (already inside "
            f"ramp window so the guard suppresses the leading-edge subtract), "
            f"got delays {captured_delays}"
        )
        # And no delay near 1s (which would indicate the guard misfired).
        # 1s is the floor `max(..., 1.0)` we'd hit without the guard.
        # _DEBOUNCE_SECS may also be small — verify the floor case explicitly.
        # (No assertion here — debounce timer can be ~0.3s. The positive
        # `matching` assertion above is the load-bearing check.)
    finally:
        await coordinator.async_unload()


# ----- v0.12.0: persisted idle slope (MPC-readiness through a heating chase) -----


def _seed_recovery_heat_run(coordinator: ZoneCoordinator, *, now: datetime) -> None:
    """Seed a heat-only run: a recovery_heat slope is recoverable but there are
    NO idle samples, so the *live* idle slope is None. This is the gym's
    morning-heating-chase shape — the room is heating hard with no sustained
    idle window for the estimator to learn passive heat loss from.
    """
    from custom_components.comfort_band.const import ACTION_HEAT
    from custom_components.comfort_band.predictor import Sample

    samples: list[Sample] = []
    base = now - timedelta(minutes=30)
    for i in range(8):
        samples.append(
            Sample(
                t=base + timedelta(minutes=2 * i),
                temp=19.0 + 0.05 * i,
                action=ACTION_HEAT,
            )
        )
    coordinator._samples_cache = samples


async def test_live_idle_slope_is_persisted(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When the live window yields an idle slope, it is written to storage so a
    later heating chase can borrow it. Source is reported as "live"."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    # Persist is gated on learning_enabled (the cache only feeds MPC).
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _seed_idle_drift(coordinator, start_temp=21.0, slope_per_h=-0.5, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "21.0", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "live"
    assert state.idle_slope_cached_age_min is None
    assert state.thermal_slopes.idle is not None
    zone = coordinator._store.get_zone("office")
    # The persisted value equals the live estimate, with a timestamp set.
    assert zone["persisted_idle_slope"] == state.thermal_slopes.idle
    assert zone["persisted_idle_slope_at"] is not None


async def test_persist_skipped_for_non_learning_zone(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A pure-hysteresis zone (learning off — the fixture default) still
    computes the idle slope for the thermal_slope sensor, but does NOT persist
    it: the cache only feeds MPC, so persisting would be wasted SD-card writes."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    _seed_idle_drift(coordinator, start_temp=21.0, slope_per_h=-0.5, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "21.0", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "live"
    assert state.thermal_slopes.idle is not None  # computed for the sensor...
    zone = coordinator._store.get_zone("office")
    assert zone["persisted_idle_slope"] is None  # ...but not written to storage
    assert zone["persisted_idle_slope_at"] is None


async def test_cached_idle_slope_keeps_mpc_ready_during_heating_chase(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The headline gym scenario: the live window has only a recovery_heat run
    (live idle is None), but a recent persisted idle slope is substituted so
    `mpc_ready` stays True. Without the substitution MPC would go un-ready
    exactly when pre-heat is needed."""
    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    _seed_recovery_heat_run(coordinator, now=now)
    # A passive heat-loss slope learned overnight, 5 min old.
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at=(now - timedelta(minutes=5)).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "cached"
    assert state.idle_slope_cached_age_min == pytest.approx(5.0, abs=0.1)
    # The effective slopes carry the cached idle (tagged so the sensor shows it)
    # alongside the live recovery slope -> is_ready is satisfied.
    assert state.thermal_slopes.idle == -0.004
    assert state.thermal_slopes.method_idle == "cached"
    assert state.thermal_slopes.recovery_heat is not None
    assert state.mpc_ready is True


async def test_expired_persisted_idle_slope_is_ignored_and_cleared(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A persisted idle slope older than the max age must NOT be substituted
    (MPC stays un-ready) and must be cleared from storage so it can't resurface
    on a later refresh."""
    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    _seed_recovery_heat_run(coordinator, now=now)
    # 25 h old -> beyond PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES (24 h).
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at=(now - timedelta(hours=25)).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "none"
    assert state.thermal_slopes.idle is None
    assert state.mpc_ready is False
    zone = coordinator._store.get_zone("office")
    assert zone["persisted_idle_slope"] is None
    assert zone["persisted_idle_slope_at"] is None


async def test_manual_edit_flush_clears_persisted_idle_slope(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A manual climate edit invalidates the learned thermal model, so the
    persisted idle slope is dropped alongside the sample buffer."""
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-05-19 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # Establish a baseline command, then plant a persisted idle slope.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._last_command_state is not None
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at=dt_util.utcnow().isoformat(),
    )

    # Manual edit well outside the echo window -> flush.
    freezer.tick(timedelta(minutes=10))
    event: Event[EventStateChangedData] = Event(
        "state_changed",
        {
            "entity_id": CLIMATE_ENTITY,
            "old_state": State(CLIMATE_ENTITY, "heat", {"temperature": 19.5}),
            "new_state": State(CLIMATE_ENTITY, "cool", {"temperature": 23.0}),
        },
    )
    coordinator._on_climate_state_change(event)
    await hass.async_block_till_done()

    zone = coordinator._store.get_zone("office")
    assert zone["persisted_idle_slope"] is None
    assert zone["persisted_idle_slope_at"] is None
    await coordinator.async_unload()


async def test_cached_idle_without_recovery_stays_not_ready(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cached idle slope alone must NOT force readiness: `mpc.is_ready`
    also needs a recovery slope. With an empty live window (no recovery) but a
    fresh persisted idle, the source is "cached" yet `mpc_ready` stays False —
    the cache fills a gap, it doesn't fabricate a whole model."""
    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    coordinator._samples_cache = []  # no live slopes at all (no recovery either)
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at=(now - timedelta(minutes=5)).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "cached"
    assert state.thermal_slopes.idle == -0.004
    assert state.thermal_slopes.recovery_heat is None
    assert state.thermal_slopes.recovery_cool is None
    assert state.mpc_ready is False


async def test_persisted_idle_slope_write_is_throttled(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The persisted-idle-slope write is throttled to <=1 per
    SAMPLE_PERSIST_INTERVAL_S (300 s): a refresh within the window must not
    advance the timestamp; one past it must."""
    freezer.move_to("2026-05-19 12:00:00+00:00")
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _seed_idle_drift(coordinator, start_temp=21.0, slope_per_h=-0.5, now=dt_util.utcnow())
    hass.states.async_set(TEMP_ENTITY, "21.0", {})

    await coordinator._async_update_data()
    first_at = coordinator._store.get_zone("office")["persisted_idle_slope_at"]
    assert first_at is not None

    # A refresh 60 s later (< 300 s) must NOT rewrite the timestamp.
    freezer.tick(timedelta(seconds=60))
    await coordinator._async_update_data()
    assert coordinator._store.get_zone("office")["persisted_idle_slope_at"] == first_at

    # Past the interval, the next refresh refreshes the timestamp.
    freezer.tick(timedelta(seconds=300))
    await coordinator._async_update_data()
    assert coordinator._store.get_zone("office")["persisted_idle_slope_at"] != first_at


async def test_thermal_slope_sensor_exposes_idle_source_attributes(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cached-idle diagnostics actually reach the thermal_slope sensor's
    `extra_state_attributes` (guards against a plumbing typo between ZoneState
    and the sensor)."""
    from custom_components.comfort_band.sensor import ThermalSlopeSensor

    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    _seed_recovery_heat_run(coordinator, now=now)
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at=(now - timedelta(minutes=5)).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    attrs = ThermalSlopeSensor(coordinator).extra_state_attributes
    assert attrs["idle_slope_source"] == "cached"
    assert attrs["idle_slope_cached_age_min"] == pytest.approx(5.0, abs=0.1)
    # The cached value also surfaces as the displayed idle_slope (x60 -> °C/h).
    assert attrs["idle_slope"] == pytest.approx(-0.004 * 60.0, abs=1e-6)


async def test_naive_persisted_timestamp_is_dropped_not_crashed(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A naive (tz-less) persisted timestamp — only reachable via a corrupt /
    hand-edited store — must not crash the refresh on the aware/naive datetime
    subtraction. It's treated as unusable and cleared."""
    freezer.move_to("2026-05-19 07:00:00+00:00")
    coordinator._samples_cache = []
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at="2026-05-19T06:55:00",  # naive: no tz offset
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})

    state = await coordinator._async_update_data()  # must not raise

    assert state.idle_slope_source == "none"
    assert state.thermal_slopes.idle is None
    zone = coordinator._store.get_zone("office")
    assert zone["persisted_idle_slope"] is None
    assert zone["persisted_idle_slope_at"] is None


async def test_cached_idle_does_not_leak_into_reactive_predictor(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cached idle slope is an MPC-only concern: it must NOT reach the v0.7
    predictor's passive-drift branch, or it could silently suppress a reactive
    heat call off a stale slope on a predictor-only zone.

    Scenario engineered so that IF the predictor saw the cached idle, passive-
    drift acceptance would fire (room within passive_tolerance below the band,
    a strongly *warming* cached idle projects recovery into band) and the
    predicted action would flip HEAT -> IDLE. With the live slopes (idle=None,
    empty buffer) the branch is unreachable, so the predictor must still say
    HEAT. The thermal_slope sensor still shows the cached value (it drives MPC).
    """
    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    coordinator._samples_cache = []  # live idle is None
    # Strongly warming cached idle (would trigger passive-heat-suppression if
    # it leaked): projected = 19.1 + 0.12*5 = 19.7 >= low(19.5).
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=0.12,
        persisted_idle_slope_at=(now - timedelta(minutes=5)).isoformat(),
    )
    # 19.1 <= low(19.5) - deadband_below(0.3) -> hysteresis wants HEAT; and
    # 19.1 >= low - passive_tolerance(0.5) = 19.0 -> passive branch eligible.
    hass.states.async_set(TEMP_ENTITY, "19.1", {})

    state = await coordinator._async_update_data()

    # Cache is genuinely present (would have suppressed if it leaked)...
    assert state.idle_slope_source == "cached"
    assert state.thermal_slopes.idle == 0.12
    # ...but the predictor ran on live slopes, so it did NOT suppress the heat.
    assert state.predicted_decision.action == ACTION_HEAT


async def test_cached_idle_routes_final_decision_to_mpc(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """End-to-end (the literal purpose of the feature): with learning + MPC on,
    a heating chase makes live idle None, but a fresh cached idle keeps
    mpc_ready True — so MPC plans off (cached idle + live recovery) and the
    three-way gate forwards MPC's heat as the FINAL decision, instead of
    reactively falling back to the predictor."""
    freezer.move_to("2026-05-19 07:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True, mpc_enabled=True)
    now = dt_util.utcnow()
    _seed_recovery_heat_run(coordinator, now=now)  # live idle None, recovery_heat present
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.01,  # passive cooling toward ambient
        persisted_idle_slope_at=(now - timedelta(minutes=5)).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.3", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.idle_slope_source == "cached"
    assert coordinator.data.mpc_ready is True
    # MPC elects heat (room below band, cached idle drifts further out, the live
    # recovery slope brings it back) and the gate forwards it as the decision.
    assert coordinator.data.mpc_decision.action == ACTION_HEAT
    assert coordinator.data.decision.action == ACTION_HEAT
    await coordinator.async_unload()


async def test_cached_idle_at_exact_max_age_is_still_used(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At exactly PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES the cached value is still
    used (expiry is strict `>`), not dropped — pins the `>` vs `>=` choice."""
    from custom_components.comfort_band.const import PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES

    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    _seed_recovery_heat_run(coordinator, now=now)
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.004,
        persisted_idle_slope_at=(
            now - timedelta(minutes=PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES)
        ).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "cached"
    assert state.idle_slope_cached_age_min == pytest.approx(
        float(PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES), abs=0.1
    )
    # Boundary is inclusive -> the value is NOT cleared from storage.
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.004


async def test_cached_idle_of_zero_is_substituted(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A persisted idle slope of exactly 0.0 (a perfectly flat room) is a valid
    cached value: the resolver keys off `is None`, not truthiness, so 0.0 must
    still substitute and satisfy mpc_ready."""
    freezer.move_to("2026-05-19 07:00:00+00:00")
    now = dt_util.utcnow()
    _seed_recovery_heat_run(coordinator, now=now)
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=0.0,
        persisted_idle_slope_at=(now - timedelta(minutes=5)).isoformat(),
    )
    hass.states.async_set(TEMP_ENTITY, "19.2", {})

    state = await coordinator._async_update_data()

    assert state.idle_slope_source == "cached"
    assert state.thermal_slopes.idle == 0.0
    assert state.mpc_ready is True


# ----- v0.13.0: deterministic fan-boost -----


def _register_climate_with_fan(
    hass: HomeAssistant, *, fan_modes: list[str] | None, fan_mode: str | None
) -> None:
    """Register the office climate entity with `fan_modes` + current `fan_mode`.

    The behaviour-test helpers (`_setup_enabled_zone`) never register a climate
    state, so the fan-boost guards fail closed by default — tests that exercise
    fan control must register one explicitly. `fan_modes=None` omits the
    attribute entirely (simulates a fanless unit)."""
    attrs: dict[str, Any] = {}
    if fan_modes is not None:
        attrs["fan_modes"] = fan_modes
    if fan_mode is not None:
        attrs["fan_mode"] = fan_mode
    hass.states.async_set(CLIMATE_ENTITY, "off", attrs)


async def _enable_fan_control(
    coordinator: ZoneCoordinator, *, active: str | None = None, idle: str | None = None
) -> None:
    await coordinator._store.async_update_zone(
        "office", fan_control_enabled=True, active_fan_mode=active, idle_fan_mode=idle
    )


async def test_fan_boost_commands_active_mode_on_heat(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Heating with fan control on commands the configured active fan mode."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="low")

    hass.states.async_set(TEMP_ENTITY, "18.0", {})  # below band -> heat
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_HEAT
    fan_calls = _calls_for(climate_calls, "set_fan_mode")
    assert any(c["fan_mode"] == "high" for c in fan_calls), fan_calls


async def test_fan_boost_commands_active_mode_on_cool(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """The shared 'active' fan covers cooling too (not just heating)."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="low")

    hass.states.async_set(TEMP_ENTITY, "24.0", {})  # above band -> cool
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_COOL
    fan_calls = _calls_for(climate_calls, "set_fan_mode")
    assert any(c["fan_mode"] == "high" for c in fan_calls), fan_calls


async def test_fan_boost_commands_idle_mode_on_release(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Releasing to idle (fan_only) commands the configured idle fan mode."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="high")

    hass.states.async_set(TEMP_ENTITY, "20.0", {})  # in band -> idle/fan_only
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_IDLE
    fan_calls = _calls_for(climate_calls, "set_fan_mode")
    assert any(c["fan_mode"] == "low" for c in fan_calls), fan_calls


async def test_fan_boost_off_by_default_issues_no_fan_command(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """With fan_control_enabled off (the default), no set_fan_mode is issued
    even with active/idle modes configured — zero behaviour change."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # Modes set but the master switch stays OFF.
    await coordinator._store.async_update_zone(
        "office", active_fan_mode="high", idle_fan_mode="low"
    )
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="low")

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_fan_mode") == []


async def test_fan_boost_skips_when_desired_equals_current(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """No redundant set_fan_mode when the fan is already at the desired mode."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="high")

    hass.states.async_set(TEMP_ENTITY, "18.0", {})  # heat, desired "high" == current
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls_for(climate_calls, "set_fan_mode") == []


async def test_fan_boost_skips_mode_not_in_fan_modes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """A stored mode the unit no longer lists is skipped (no command, no raise)."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="turbo", idle="low")  # "turbo" not offered
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="low")

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_fan_mode") == []


async def test_fan_boost_skips_when_climate_has_no_fan_modes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """A fanless / unavailable climate (no fan_modes attribute) is left alone."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    _register_climate_with_fan(hass, fan_modes=None, fan_mode=None)  # no fan support

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_fan_mode") == []


async def test_fan_boost_skip_on_none_active_still_commands_idle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Asymmetric config: active_fan_mode=None (don't touch the fan while
    heating) but idle_fan_mode set — heating issues nothing, idle still
    commands the quiet fan. This is the user's primary 'low while idle' case."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active=None, idle="low")
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="high")

    # Heating with active=None -> no fan command.
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls_for(climate_calls, "set_fan_mode") == []

    # Release to idle -> idle_fan_mode is still commanded.
    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    assert any(c["fan_mode"] == "low" for c in _calls_for(climate_calls, "set_fan_mode"))


async def test_fan_boost_not_issued_in_shadow_mode(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Shadow mode (enabled=False) issues no climate commands at all, fan
    control included."""
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    # enabled stays False (shadow); fan control on.
    await store.async_update_zone(
        "office", fan_control_enabled=True, active_fan_mode="high", idle_fan_mode="low"
    )
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="low")

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_fan_mode") == []


async def test_fan_boost_suppressed_by_min_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A same-action re-commit suppressed by the min-cycle gate suppresses the
    fan command too (the gate returns before set_hvac_mode)."""
    freezer.move_to("2026-04-25 10:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="low")

    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(_calls_for(climate_calls, "set_fan_mode")) >= 1  # initial heat fan

    # 5 min later (same heat action, within the 8-min default) -> suppressed.
    climate_calls.clear()
    freezer.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _calls_for(climate_calls, "set_hvac_mode") == []
    assert _calls_for(climate_calls, "set_fan_mode") == []


async def test_fan_boost_commands_integer_fan_mode_as_string(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """A unit reporting integer fan modes/levels still gets commanded: the
    stored string '3' matches the str-coerced fan_modes [str(1),str(2),str(3)]."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="3", idle="1")
    hass.states.async_set(CLIMATE_ENTITY, "heat", {"fan_modes": [1, 2, 3], "fan_mode": 1})

    hass.states.async_set(TEMP_ENTITY, "18.0", {})  # heat -> active "3", current 1
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert any(c["fan_mode"] == "3" for c in _calls_for(climate_calls, "set_fan_mode"))


async def test_fan_boost_redundant_guard_handles_integer_fan_mode(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """The redundant-skip guard must fire even when the unit reports an INTEGER
    `fan_mode`: the current level is str-coerced before comparison, so a unit
    already at the desired level isn't re-commanded every cycle (the guarantee
    a prior review flagged would otherwise break for integer-fan-mode units)."""
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="3", idle="1")
    # Already at level 3, reported as a bare int.
    hass.states.async_set(CLIMATE_ENTITY, "heat", {"fan_modes": [1, 2, 3], "fan_mode": 3})

    hass.states.async_set(TEMP_ENTITY, "18.0", {})  # heat -> desired "3" == current 3
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls_for(climate_calls, "set_hvac_mode")  # the action WAS applied...
    assert _calls_for(climate_calls, "set_fan_mode") == []  # ...but the fan is already correct


# ----- v0.14.0: shared-schedule band resolution -----


async def _assign_shared(coordinator: ZoneCoordinator, name: str, low: float, high: float) -> str:
    """Create a shared schedule with an all-day band on the home profile and
    assign the office zone to it. Returns the shared id."""
    store = coordinator._store
    sid = await store.async_add_shared_schedule(name)
    await store.async_set_shared_schedule(sid, "home", [{"at": "00:00", "low": low, "high": high}])
    await store.async_set_zone_schedule_id("office", sid)
    return sid


async def test_assigned_zone_resolves_shared_band(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """An assigned zone resolves its band from the shared schedule, not its own
    (default-empty) schedules / manual band."""
    await _assign_shared(coordinator, "Bedrooms", 21.0, 24.0)
    hass.states.async_set(TEMP_ENTITY, "22.0", {})
    state = await coordinator._async_update_data()
    assert (state.sched_low, state.sched_high) == (21.0, 24.0)
    assert (state.effective_low, state.effective_high) == (21.0, 24.0)
    await coordinator.async_unload()


async def test_dangling_schedule_id_falls_back_to_own_then_manual(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """A schedule_id pointing at a deleted shared schedule must not raise — it
    falls back to the zone's own schedules, then the manual band."""
    # Simulate corruption: a schedule_id that doesn't exist (bypasses the
    # validating setter the way a hand-edited / deleted-out-from-under store
    # would).
    await coordinator._store.async_update_zone("office", schedule_id="ghost")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    state = await coordinator._async_update_data()  # must not raise
    # Office has no own schedule -> manual band (default 19.5 / 22.5).
    assert (state.sched_low, state.sched_high) == (19.5, 22.5)


async def test_override_wins_over_shared_schedule(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """A per-zone override still forces the manual band even when assigned to a
    shared schedule."""
    await _assign_shared(coordinator, "Bedrooms", 21.0, 24.0)
    await coordinator.async_start_override(low=17.0, high=19.0, hours=2)
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    state = await coordinator._async_update_data()
    assert state.override_active is True
    assert (state.effective_low, state.effective_high) == (17.0, 19.0)
    await coordinator.async_unload()


async def test_unassign_restores_own_schedule(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """Clearing the assignment (schedule_id=None) reverts to the zone's own
    schedule / manual band."""
    await _assign_shared(coordinator, "Bedrooms", 21.0, 24.0)
    await coordinator._store.async_set_zone_schedule_id("office", None)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    state = await coordinator._async_update_data()
    assert (state.sched_low, state.sched_high) == (19.5, 22.5)  # back to manual


async def test_assigned_zone_resolves_per_active_profile(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """A shared schedule is profile-aware: flipping the active profile resolves
    the matching per-profile band."""
    store = coordinator._store
    sid = await store.async_add_shared_schedule("Bedrooms")
    await store.async_set_shared_schedule(sid, "home", [{"at": "00:00", "low": 21.0, "high": 24.0}])
    await store.async_set_shared_schedule(sid, "away", [{"at": "00:00", "low": 15.0, "high": 28.0}])
    await store.async_set_zone_schedule_id("office", sid)
    hass.states.async_set(TEMP_ENTITY, "22.0", {})

    state = await coordinator._async_update_data()
    assert (state.sched_low, state.sched_high) == (21.0, 24.0)  # home active by default

    await store.async_set_active_profile("away")
    state = await coordinator._async_update_data()
    assert (state.sched_low, state.sched_high) == (15.0, 28.0)  # away band
    await coordinator.async_unload()


async def test_assigned_to_empty_shared_schedule_uses_manual_not_own(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """When a zone is assigned to a shared schedule that EXISTS but has no slot
    for the active/default profile, its own (now-dormant) schedule is skipped —
    band resolution falls straight to the manual band. (Contrast the dangling-id
    case, which treats the zone as effectively unassigned: own -> manual.)"""
    store = coordinator._store
    # Give the zone an OWN schedule that would resolve to a distinctive band...
    await store.async_set_zone_schedule(
        "office", "home", [{"at": "00:00", "low": 5.0, "high": 9.0}]
    )
    # ...then assign it to an empty shared schedule (no profile slots at all).
    sid = await store.async_add_shared_schedule("Bedrooms")
    await store.async_set_zone_schedule_id("office", sid)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})

    state = await coordinator._async_update_data()
    # Not the own-schedule band (5/9) — the dormant own schedule is bypassed —
    # but the manual default (19.5/22.5).
    assert (state.sched_low, state.sched_high) == (19.5, 22.5)


async def test_profile_rename_keeps_assigned_zone_on_shared_band(
    hass: HomeAssistant, coordinator: ZoneCoordinator
) -> None:
    """v0.14.1: renaming the active profile rekeys the shared schedule's slot,
    so an assigned zone keeps resolving the shared band — it does NOT silently
    fall back to its manual band (the bug this release fixes)."""
    await _assign_shared(coordinator, "Bedrooms", 21.0, 24.0)
    await coordinator._store.async_rename_profile("home", "casa")
    hass.states.async_set(TEMP_ENTITY, "22.0", {})

    state = await coordinator._async_update_data()
    # Still the shared band (now keyed "casa"), not the manual fallback.
    assert (state.sched_low, state.sched_high) == (21.0, 24.0)
    await coordinator.async_unload()


# ---------------------------------------------------------------------------
# v0.16.0: room-sensor availability logging.
# ---------------------------------------------------------------------------


async def test_losing_the_sensor_is_logged_even_when_idle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The incident that prompted v0.16.0 had the zone idle when its sensor died.

    Nothing was commanded, so the control path stayed quiet and the room drifted
    for hours with nothing in the log. The edge is logged once -- not on every
    refresh -- and again when the sensor returns.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        for _ in range(3):
            freezer.tick(timedelta(minutes=1))
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    assert sum("is unavailable" in r.message for r in caplog.records) == 1
    await coordinator.async_unload()


async def test_flapping_sensor_does_not_flood_the_log(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A weak-mesh sensor crosses this boundary hundreds of times an hour.

    Unlatched, that measured 8,352 lines a day. This is a warning, so it needs a
    floor.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            freezer.tick(timedelta(seconds=30))
            hass.states.async_set(TEMP_ENTITY, "unavailable", {})
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            freezer.tick(timedelta(seconds=30))
            hass.states.async_set(TEMP_ENTITY, "20.0", {})
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    # Two-sided on purpose. An upper bound alone passes a throttle that logs
    # once and then never again -- which would silence a permanent outage that
    # followed the flapping. Over 30 minutes at one line per 5 minutes per
    # direction, expect roughly six.
    losses = sum("is unavailable" in r.message for r in caplog.records)
    assert 4 <= losses <= 8, losses
    await coordinator.async_unload()


async def test_each_direction_gets_its_own_log_budget(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recovery line must not spend the outage line's allowance.

    With one shared timestamp, a drop followed shortly by a recovery left the
    recovery throttled away -- and the pending edge is only re-offered on the
    next refresh, which for this coordinator may never come: a zone with no
    schedule has no timer, and a settled sensor emits no further state changes.
    The record then ends on "unavailable" for a sensor that is fine, and the
    mirror case ends on "reporting again" for a room that has gone dark.

    Short drop-then-recover is the ordinary shape of a weak mesh link, so it is
    the case the throttle has to get right rather than the exception.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        # Well inside the throttle window, so a shared budget would swallow this.
        freezer.tick(timedelta(seconds=20))
        hass.states.async_set(TEMP_ENTITY, "21.0", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sum("is unavailable" in r.message for r in caplog.records) == 1
    assert sum("reporting again" in r.message for r in caplog.records) == 1, [
        r.message for r in caplog.records
    ]
    await coordinator.async_unload()


async def test_no_warning_while_home_assistant_is_still_starting(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Don't cry wolf while the sensor's own integration is still loading.

    Integrations load in parallel, so this zone can easily refresh before the
    sensor has published anything -- routine for MQTT, Zigbee2MQTT,
    Matter/Thread and ESPHome, which is exactly the hardware the incident behind
    this release involved. Warning then is a guaranteed false positive on every
    restart, on the one channel the release is about.

    The edge is left un-recorded rather than swallowed, so a sensor that really
    is dead is still announced once the system is up.
    """
    from homeassistant.core import CoreState

    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    original = hass.state
    hass.set_state(CoreState.starting)
    caplog.clear()
    try:
        with caplog.at_level(logging.INFO):
            # The sensor's integration hasn't published yet.
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert not any("is unavailable" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

        # Once HA is up and the sensor is still missing, it is announced.
        hass.set_state(CoreState.running)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            freezer.tick(timedelta(minutes=1))
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert sum("is unavailable" in r.message for r in caplog.records) == 1
    finally:
        hass.set_state(original)
    await coordinator.async_unload()


async def test_a_backwards_clock_step_does_not_silence_the_log(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clock correction must not starve the throttle.

    An RTC-less Pi boots with the wrong time and corrects against NTP shortly
    after -- and the correlated case is the one that matters, since the power cut
    that rebooted it may well be what took the sensors out. A negative elapsed
    time satisfies a bare `< interval`, so the log would stay silent until the
    clock caught up.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # One outage, logged, which stamps the budget.
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The clock jumps backwards past the stamp.
    freezer.move_to("2026-07-30 10:00:00+00:00")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sum("is unavailable" in r.message for r in caplog.records) == 1, [
        r.message for r in caplog.records
    ]
    await coordinator.async_unload()


async def test_a_shadow_mode_zone_does_not_warn(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A zone that commands nothing hasn't stopped controlling.

    Zones ship in shadow mode, so on a fresh multi-zone install every one of
    them is disabled -- warning that each "cannot control" on a mesh hiccup
    would be noise about control that was never happening. Still recorded, just
    not at warning level; the binary sensor is unaffected either way.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert store.get_zone("office")["enabled"] is False

    caplog.clear()
    with caplog.at_level(logging.INFO):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not any(r.levelno >= logging.WARNING for r in caplog.records), [
        r.message for r in caplog.records
    ]
    assert sum("shadow mode" in r.message for r in caplog.records) == 1
    assert coordinator.data.sensor_available is False
    await coordinator.async_unload()


async def test_a_nan_reading_counts_as_no_reading(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """`nan` parses as a float but can't be compared, so it isn't a reading.

    Every comparison against NaN is False, so hysteresis would read the room as
    below band and heat it indefinitely, while `sensor_available` stayed True so
    nothing alerted.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    for value in ("nan", "NaN", "inf", "-inf"):
        hass.states.async_set(TEMP_ENTITY, value, {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data.sensor_available is False, value
    await coordinator.async_unload()


# ---------------------------------------------------------------------------
# The event-driven path itself. This coordinator has `update_interval=None`, so
# every decision it ever makes is triggered by a state change -- yet for a long
# time the harness built coordinators without subscribing, and every test drove
# them with an explicit `async_refresh()`. That is a different code path from
# production, and it hid a real defect through five review rounds. These two
# tests exist to fail if the wiring is ever dropped again.
# ---------------------------------------------------------------------------


async def test_a_sensor_change_alone_drives_a_decision(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A room-temp change must reach the coordinator on its own.

    No `async_refresh()` anywhere in this test: the sensor moving below band is
    the only input, and the resulting climate command is proof the subscription
    is live. Every other test in this file drives refreshes by hand, so without
    this one the harness could stop subscribing entirely and stay green.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    climate_calls.clear()

    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await hass.async_block_till_done()
    # Room-temp changes are debounced 2 s before a refresh is requested.
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert any(
        c["hvac_mode"] == HVAC_MODE_HEAT for c in _calls_for(climate_calls, "set_hvac_mode")
    ), climate_calls
    # The refresh really ran off the event, not off stale state.
    assert coordinator.data.room == 16.0
    assert coordinator.data.decision.action == ACTION_HEAT


async def test_a_climate_change_alone_reaches_the_manual_edit_detector(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Somebody using the wall remote must reach the detector by itself.

    The detector's own logic is covered by tests that call
    `_on_climate_state_change` with a hand-built event, which proves the logic
    but not that anything is subscribed to deliver one. This closes that gap
    from the other side: the only input is the climate entity changing.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._samples_cache, "expected a sample to have been recorded"

    # Someone picks up the remote, well outside the echo window.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 30))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_COOL, {"temperature": 17.0})
    await hass.async_block_till_done()

    assert coordinator._samples_cache == [], "a manual edit should flush the buffer"


async def test_setup_hydrates_the_buffer_before_the_first_refresh(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Restoring the learned samples must happen before anything appends one.

    `_append_sample` persists the whole list, and on a fresh coordinator
    `_last_sample_persist_at is None`, so the first append writes immediately
    with no throttle. Refresh before hydrating and the store is truncated to
    that one sample -- the thermal model wiped on every restart and
    `mpc.is_ready` permanently False. The ordering used to be self-evident
    inside a single method; it is a contract between two now, so it needs a test.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", enabled=True)

    seeded = [
        {
            "t": (dt_util.utcnow() - timedelta(minutes=n)).isoformat(),
            "temp": 20.0 + n * 0.1,
            "action": ACTION_IDLE,
            "fan_mode": None,
        }
        for n in range(8, 0, -1)
    ]
    await store.async_update_zone("office", samples=seeded)

    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    _HELPER_COORDINATORS.append(coordinator)
    coordinator.subscribe_and_hydrate()
    assert len(coordinator._samples_cache) == len(seeded)

    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(coordinator._samples_cache) >= len(seeded)
    assert len(store.get_zone("office")["samples"]) >= len(seeded)


async def test_a_profile_change_alone_re_evaluates_the_zone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switching profile must reach every zone on its own.

    `subscribe_and_hydrate` wires three things, and the profile signal was the
    one with no coverage at all -- deleting the subscription passed the whole
    suite. Without it a home/away switch wouldn't re-evaluate a zone until its
    sensor happened to move, which on a settled room can be a long time.

    Note the drain step below: a room-temp change arms a 2 s debounce that an
    explicit `async_refresh()` does not cancel, so advancing the clock later
    would fire *that* and refresh the zone for the wrong reason. A first draft
    of this test passed with the signal subscription deleted for exactly that
    reason.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    store = coordinator._store
    await store.async_set_zone_schedule(
        "office", "home", [{"at": "00:00", "low": 18.0, "high": 24.0}]
    )
    await store.async_set_zone_schedule(
        "office", "away", [{"at": "00:00", "low": 21.5, "high": 24.0}]
    )
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "20.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Drain the pending sensor debounce so it can't supply the refresh below.
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    climate_calls.clear()

    # The signal is now the only thing that can trigger a re-evaluation --
    # sent exactly as `profiles.py` sends it.
    await store.async_set_active_profile("away")
    async_dispatcher_send(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, "away")
    await hass.async_block_till_done()
    # Its handler requests a refresh through the coordinator's own debouncer.
    freezer.tick(timedelta(seconds=15))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert coordinator.data.decision.action == ACTION_HEAT
    assert any(
        c["hvac_mode"] == HVAC_MODE_HEAT for c in _calls_for(climate_calls, "set_hvac_mode")
    ), climate_calls


async def test_subscribing_twice_does_not_leak_a_listener(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A second subscribe must not orphan the first set of listeners.

    Each unsub handle is stored in a single attribute, so re-wiring without
    guarding would overwrite it and leave the original registration alive with
    nothing holding its handle -- and `async_unload`, documented as cancelling
    every active subscription, could then never cancel it. HA's own
    lingering-check does not catch a stray `state_changed` listener, so it would
    fail silently, leaving a torn-down coordinator able to command a live
    climate entity.

    Unreachable today (production subscribes once), but the wiring is a public
    entry point now, so the guard is worth pinning.
    """
    freezer.move_to("2026-07-30 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    first_unsub = coordinator._unsub_state

    coordinator.subscribe_and_hydrate()
    assert coordinator._unsub_state is first_unsub, "re-wired over the live listener"

    refreshes = 0
    real_update = coordinator._async_update_data

    async def _count() -> Any:
        nonlocal refreshes
        refreshes += 1
        return await real_update()

    coordinator._async_update_data = _count  # type: ignore[method-assign]
    await coordinator.async_unload()

    # After unload nothing may still be listening.
    hass.states.async_set(TEMP_ENTITY, "17.0", {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=10))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert refreshes == 0, f"a listener survived unload and drove {refreshes} refreshes"


# ---------------------------------------------------------------------------
# Control-path bookkeeping: what the store records must match what the unit was
# actually told to do. `last_action` drives both dwell gates and every later
# decision, so a wrong value there is not cosmetic.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raised",
    [
        HomeAssistantError(
            "Set temperature action was used with the target temperature "
            "parameter but the entity does not support it"
        ),
        # Not a `HomeAssistantError`: the guard is deliberately broad because a
        # cloud unit's timeout would otherwise escape into the fire-and-forget
        # apply task, where nothing is waiting to catch it.
        TimeoutError("the cloud never answered"),
    ],
    ids=["rejected", "timed-out"],
)
async def test_a_failed_setpoint_still_records_the_started_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    raised: Exception,
) -> None:
    """`set_hvac_mode` landing is what commits the zone, not the setpoint.

    The unit starts conditioning on `set_hvac_mode`. Recording that only after
    `set_temperature` meant any raise in between left the unit heating while the
    store still said otherwise -- and `last_action` is what the min-cycle and
    cross-mode dwells key off, so the zone's own bookkeeping stayed wrong from
    then on.

    Not a hypothetical fault: an entity advertising only
    TARGET_TEMPERATURE_RANGE -- Ecobee, Nest, many `heat_cool` mini-splits --
    makes Home Assistant itself raise for a plain `temperature`, so on that
    whole class of hardware every heat/cool entry raised and `last_action` never
    became correct at all.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    async def _reject_setpoint(call: Any) -> None:
        raise raised

    hass.services.async_register("climate", "set_temperature", _reject_setpoint)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert any(
        c["hvac_mode"] == HVAC_MODE_HEAT for c in _calls_for(climate_calls, "set_hvac_mode")
    ), climate_calls
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT
    # And the sample is still recorded, so the run isn't invisible to the model.
    assert coordinator._samples_cache
    # A setpoint that never reached the unit is not something the unit may
    # report, so it must not join what the manual-edit detector accepts.
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_HEAT}


async def test_a_dropped_command_is_not_recorded(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A clean return from the service call is not proof of delivery.

    Home Assistant answers a call it cannot deliver by skipping the entity and
    returning normally. That premise was established out of band, on a throwaway
    probe with a real climate platform, since the service stub these tests use
    does no availability filtering of its own: an available entity received
    `set_hvac_mode`, an unavailable one received nothing and nothing was raised.
    What this test covers is what the coordinator does about it. Recording the
    action anyway leaves the
    store describing something the unit never did, which the dwell gates and
    every later decision then trust.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["last_action"] != ACTION_HEAT

    # It retries once the unit is reachable again.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(TEMP_ENTITY, "15.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT


async def test_a_slow_units_late_echo_is_not_a_manual_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """What we asked for has to be recorded, or a slow unit's echo is a hand edit.

    A slow or cloud-backed unit still reports its previous state immediately
    after the call, so comparing only against the live snapshot made the unit's
    own echo -- arriving outside `CLIMATE_ECHO_WINDOW_S` -- a mismatch, read as
    somebody editing the thermostat by hand. That flushes the sample buffer and
    clears the persisted idle slope, so MPC lost readiness on every command to
    such a unit.

    The fix is the second value, not a different first one: the baseline stays
    on what the entity reports (pinning our intent there was tried, for both
    fields, and measured worse for both), and `_commanded_state` carries what we
    asked for alongside it.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # The unit has not caught up: it still reports the mode it was in before.
    # Deliberately not the setpoint we are about to command: if the stale
    # attribute and our own value coincide, every variant of the baseline
    # expression looks identical and the assertion below proves nothing.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 17.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    assert coordinator._samples_cache

    commanded = _calls_for(climate_calls, "set_hvac_mode")[-1]["hvac_mode"]
    setpoint = _calls_for(climate_calls, "set_temperature")[-1]["temperature"]
    # What we asked for is recorded, so the unit is allowed to report it later
    # however long it takes -- the lagging value it shows now is not the only
    # thing the listener will accept.
    assert coordinator._commanded_state == {
        "hvac_mode": commanded,
        "target_temp": setpoint,
    }

    # It finally catches up, well outside the echo window.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, commanded, {"temperature": setpoint})
    await hass.async_block_till_done()

    assert coordinator._samples_cache, "the unit's own echo flushed the buffer"
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02

    # What the unit was showing beforehand does not thereby become acceptable
    # forever. The baseline is read while the unit is still lagging, so it holds
    # the *old* setpoint -- and an occupant putting the thermostat back to
    # exactly that is the likeliest hand edit there is. Once the unit has agreed
    # with us the old value has to stop being accepted. Fed as a synthetic
    # event, like the other manual-edit tests, so no refresh can interleave.
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    await hass.async_block_till_done()
    reverted: Event[EventStateChangedData] = Event(
        "state_changed",
        {
            "entity_id": CLIMATE_ENTITY,
            "old_state": State(CLIMATE_ENTITY, commanded, {"temperature": setpoint}),
            "new_state": State(CLIMATE_ENTITY, commanded, {"temperature": 17.0}),
        },
    )
    coordinator._on_climate_state_change(reverted)
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_unit_that_snaps_the_setpoint_is_not_a_manual_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A unit that reports a coerced setpoint must not look hand-edited.

    `ClimateEntity.state_attributes` puts `temperature` through `display_temp`,
    which rounds to the entity's own `precision` -- independent of the
    `target_temp_step` this integration rounds to, and absent entirely from
    `capability_attributes` when the platform publishes no step. So a
    whole-degree thermostat commanded 19.5 reports 20, forever.

    Pinning the baseline to our commanded value alone therefore mismatches on
    every later attribute update, and a same-mode re-commit is exactly the case
    the echo path cannot rescue: nothing the unit reports changes, so no state
    event fires for the echo branch to correct the baseline with.

    That the detector still fires on a real edit is pinned by
    `test_manual_climate_edit_flushes_buffer`, whose edit matches neither the
    baseline nor what was commanded.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)

    # A whole-degree unit: it snaps whatever we send to the nearest integer.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    setpoint = _calls_for(climate_calls, "set_temperature")[-1]["temperature"]
    assert setpoint == 19.5, "the test needs a setpoint the unit has to snap"

    # Its echo, inside the window, reports the snapped value.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    assert coordinator._samples_cache

    # Past the min-cycle the same decision is re-committed. The unit is already
    # in heat at its snapped setpoint, so it reports nothing new and no echo
    # arrives.
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "18.1", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(_calls_for(climate_calls, "set_temperature")) >= 2, "no re-commit"

    # An ordinary attribute update, well outside the echo window.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(
        CLIMATE_ENTITY,
        HVAC_MODE_HEAT,
        {"temperature": 20.0, "current_temperature": 18.4},
    )
    await hass.async_block_till_done()

    assert coordinator._samples_cache, "the unit's snapped setpoint read as a manual edit"
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_an_idle_release_stops_offering_the_old_setpoint(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """An idle release sends no setpoint, so it must leave none behind.

    What we commanded is rebuilt on every apply rather than updated, because a
    release carries no `target_temp` at all -- the unit keeps whatever it had.
    Carrying the previous cycle's setpoint over would leave the manual-edit
    detector accepting a value nobody has asked for since.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 19.5}

    # Back inside the band -> release to idle, which sends no setpoint.
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(TEMP_ENTITY, "20.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_FAN_ONLY}


async def test_shadow_mode_stops_vouching_for_the_last_command(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A zone switched to shadow mode commands nothing, so it expects nothing.

    Left standing, the command from before the switch would go on being accepted
    by the manual-edit detector for as long as the zone stayed in shadow -- and
    in shadow mode every climate change is somebody else's by definition.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._commanded_state is not None

    await coordinator._store.async_update_zone("office", enabled=False)
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(TEMP_ENTITY, "17.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not _calls_for(climate_calls, "set_hvac_mode")[1:], "shadow mode commanded"
    assert coordinator._commanded_state is None

    # And that is what makes the wall remote visible again: the unit was still
    # lagging when the zone went to shadow, so somebody setting it to exactly
    # what Comfort Band last asked for is now an edit like any other. Fed as a
    # synthetic event, like the other manual-edit tests, so no refresh can
    # interleave with it.
    from homeassistant.core import Event, EventStateChangedData, State

    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    edit: Event[EventStateChangedData] = Event(
        "state_changed",
        {
            "entity_id": CLIMATE_ENTITY,
            "old_state": State(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {}),
            "new_state": State(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5}),
        },
    )
    coordinator._on_climate_state_change(edit)
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_lagging_units_other_attributes_are_not_a_manual_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A unit that has not adopted the mode yet still publishes everything else.

    `current_temperature` on its own MQTT topic is the ubiquitous case, and ZHA,
    Z2M and ESPHome all report per-attribute too. Each of those events carries
    the mode the unit is still in, so recording the mode we *asked* for as the
    baseline made every one of them a hand edit -- flushing the buffer and the
    persisted idle slope several times an hour, worse than before this work.
    What we asked for is accepted from `_commanded_state`; the baseline has to
    stay on what the unit actually reports.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    # The unit lags: it is still in fan_only when we command heat.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 17.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    assert coordinator._samples_cache

    # Well outside the echo window it pushes a room reading, and nothing else.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(
        CLIMATE_ENTITY,
        HVAC_MODE_FAN_ONLY,
        {"temperature": 17.0, "current_temperature": 16.2},
    )
    await hass.async_block_till_done()

    assert coordinator._samples_cache, "a room reading read as a manual edit"
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is not None


async def test_a_unit_that_reconnects_during_the_call_is_recorded(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The delivery check reads both sides of the call, and needs to.

    `entity.available` is what Home Assistant filters on, and the state machine
    can lag it: a bridge that reconnects just as the call is dispatched leaves
    `unavailable` in the machine while the entity itself is back, so the command
    *is* delivered. The integration's own post-command write then publishes the
    real state. Judging on the before-state alone would discard that commit --
    and `last_action` unset for a running heat cycle is the unbounded
    re-command this check exists to prevent.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})

    async def _reconnects(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))
        hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {})

    hass.services.async_register("climate", "set_hvac_mode", _reconnects)
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_hvac_mode"), "nothing was commanded"
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT


async def test_a_setpoint_a_unit_will_never_take_is_not_shouted_about(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """This fault is permanent, so its warning has to be throttled.

    An entity advertising only TARGET_TEMPERATURE_RANGE refuses a plain
    setpoint on every apply, for the life of the zone -- it is a property of the
    hardware, not a passing fault. Unthrottled that is hundreds of WARNING lines
    a day. And the budget is per-fault: once a setpoint lands, the next failure
    is news again rather than sitting inside a stale window.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)

    async def _refuse(call: Any) -> None:
        raise HomeAssistantError("the entity does not support it")

    async def _accept(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))

    hass.services.async_register("climate", "set_temperature", _refuse)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for n in range(8):
            freezer.tick(timedelta(minutes=1))
            hass.states.async_set(TEMP_ENTITY, f"{16.0 + n * 0.1:.1f}", {})
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    # Two-sided: an upper bound alone passes a throttle that logs once and then
    # never again, which would hide the fault returning on a different unit.
    warnings = sum("own setpoint" in r.getMessage() for r in caplog.records)
    assert 2 <= warnings <= 3, warnings

    # A setpoint lands, so the fault is over...
    hass.services.async_register("climate", "set_temperature", _accept)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(TEMP_ENTITY, "16.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # ...and its return is announced, well inside the throttle window.
    hass.services.async_register("climate", "set_temperature", _refuse)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set(TEMP_ENTITY, "16.8", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sum("own setpoint" in r.getMessage() for r in caplog.records) == 1, [
        r.getMessage() for r in caplog.records
    ]


async def test_a_second_hand_edit_is_not_excused_by_the_first(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once an edit is detected, our command stops vouching for anything.

    Left standing it would accept, field by field, a mixture of what the
    occupant just set and what we last asked for -- a state neither of us ever
    chose. Concretely: they switch the unit to cool 24 (caught, correctly), then
    a minute later put the mode back to heat and keep their own 24. The mode
    matches our command, the setpoint matches what they just set, so nothing
    fires and the unit heats the room to 24 with the learned model still fitted
    to our own cycle -- for the whole cross-mode dwell, since nothing
    re-commands in the meantime.

    Synthetic events, like the other manual-edit tests, so no refresh can
    interleave with them.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 19.5}

    def _edit(from_state: tuple[str, float], to_state: tuple[str, float]) -> None:
        event: Event[EventStateChangedData] = Event(
            "state_changed",
            {
                "entity_id": CLIMATE_ENTITY,
                "old_state": State(CLIMATE_ENTITY, from_state[0], {"temperature": from_state[1]}),
                "new_state": State(CLIMATE_ENTITY, to_state[0], {"temperature": to_state[1]}),
            },
        )
        coordinator._on_climate_state_change(event)

    # They switch it to cool 24 at the wall.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    _edit((HVAC_MODE_HEAT, 19.5), (HVAC_MODE_COOL, 24.0))
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None

    # Then put the mode back, keeping their setpoint.
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    freezer.tick(timedelta(minutes=1))
    _edit((HVAC_MODE_COOL, 24.0), (HVAC_MODE_HEAT, 24.0))
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_an_undelivered_command_leaves_nothing_to_be_trusted(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A command the unit never received must leave no trace behind it.

    Neither half of the manual-edit detector's state may describe it: our intent
    is not what the unit is reporting, and it is not something the unit may
    report either, because it never arrived. Recording it made the entity's own
    unchanged state a hand edit the moment it came back.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["last_action"] != ACTION_HEAT
    # Nothing at all: the entity's first appearance is an initial state-added
    # event, which the detector adopts nothing from, and the apply path added
    # nothing of its own.
    assert coordinator._last_command_state is None
    assert coordinator._commanded_state is None


@pytest.mark.parametrize(
    ("raised", "needle"),
    [
        (HomeAssistantError("the unit will not take that fan mode"), "set_fan_mode"),
        # Not a `HomeAssistantError`, so `_maybe_command_fan` doesn't catch it
        # and the wrapper in `_maybe_apply_action` does. One fault, one budget,
        # whichever site catches it -- so both sites need the throttle.
        (TimeoutError("the cloud never answered"), "could not set its fan mode"),
    ],
    ids=["refused", "timed-out"],
)
async def test_a_fan_mode_a_unit_will_never_take_is_not_shouted_about(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    raised: Exception,
    needle: str,
) -> None:
    """A fan mode a unit will never take can repeat forever, so it is throttled.

    `_maybe_command_fan` only skips the call when the unit already reports the
    mode we want, so a stored fan mode it advertises but will not accept is
    retried on every single apply -- one WARNING a refresh, indefinitely. And
    the budget is per fault: once a fan command lands, the next failure is news
    again rather than sitting inside a stale window.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _enable_fan_control(coordinator, active="high", idle="low")
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    _register_climate_with_fan(hass, fan_modes=["low", "mid", "high"], fan_mode="mid")

    async def _refuse(call: Any) -> None:
        raise raised

    async def _accept(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))

    hass.services.async_register("climate", "set_fan_mode", _refuse)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for n in range(8):
            freezer.tick(timedelta(minutes=1))
            hass.states.async_set(TEMP_ENTITY, f"{16.0 + n * 0.1:.1f}", {})
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    # Two-sided: an upper bound alone passes a throttle that logs once and then
    # never again, which would hide the fault returning.
    warnings = sum(needle in r.getMessage() for r in caplog.records)
    assert 2 <= warnings <= 3, warnings

    # A fan command lands, so the fault is over...
    hass.services.async_register("climate", "set_fan_mode", _accept)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(TEMP_ENTITY, "16.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # ...and its return is announced, well inside the throttle window.
    hass.services.async_register("climate", "set_fan_mode", _refuse)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set(TEMP_ENTITY, "16.8", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sum(needle in r.getMessage() for r in caplog.records) == 1, [
        r.getMessage() for r in caplog.records
    ]


async def test_a_flushed_edit_is_only_reported_once(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """After an edit is caught, the occupant's own state becomes the baseline.

    Otherwise every subsequent attribute the unit publishes -- a room reading a
    minute later, on a unit now doing what the occupant asked -- mismatches the
    superseded baseline and is reported as another edit, re-flushing a buffer
    that is already empty and re-clearing a slope that is already gone.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    def _observe(state: str, temp: float) -> None:
        event: Event[EventStateChangedData] = Event(
            "state_changed",
            {
                "entity_id": CLIMATE_ENTITY,
                "old_state": State(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5}),
                "new_state": State(CLIMATE_ENTITY, state, {"temperature": temp}),
            },
        )
        coordinator._on_climate_state_change(event)

    # The edit, caught.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    _observe(HVAC_MODE_COOL, 24.0)
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None

    # The unit then goes on publishing that same state, as units do.
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    for _ in range(3):
        freezer.tick(timedelta(minutes=1))
        _observe(HVAC_MODE_COOL, 24.0)
        await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_setpoint_the_unit_stops_reporting_is_not_an_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A setpoint that vanishes is an absence of information, not a value.

    Nobody can clear a dial: a physical remote always sets a number, and no
    dial position unsets a target. (One service call can -- `set_temperature`
    with only the low/high pair nulls a single target on an entity advertising
    both features -- which the coordinator comment names as the one miss this
    rule accepts.) What `None` does mean is that the platform
    has no target right now -- `state_attributes` publishes `temperature` only
    while `TARGET_TEMPERATURE` is in `supported_features`, and several
    integrations vary that by mode, so a unit reaching `fan_only` simply stops
    carrying one. This integration commands `fan_only` on every idle release,
    so judging that as an edit flushed the learned model on exactly the release
    it had just asked for.

    v0.17.0 flushed here, on the argument that discarding learned data is the
    cheaper mistake. It isn't, once the case is named: the flush fires on an
    ordinary release rather than on anything anybody did.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Back inside the band -> idle release, which sends no setpoint.
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "20.5", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_FAN_ONLY}

    def _observe(attrs: dict[str, Any]) -> None:
        event: Event[EventStateChangedData] = Event(
            "state_changed",
            {
                "entity_id": CLIMATE_ENTITY,
                "old_state": State(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 19.5}),
                "new_state": State(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, attrs),
            },
        )
        coordinator._on_climate_state_change(event)

    # It stops carrying a target now that it is only fanning.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    _observe({})
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02

    # The field is not thereby ignored: a setpoint that *moves* is still an edit.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe({"temperature": 24.0})
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_climate_entity_that_does_not_exist_yet_is_not_an_outage(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The delivery check is narrowed to `unavailable`, not to a missing entity.

    An entity absent from the state machine is a misconfiguration, or a platform
    that has not finished loading -- either way not the transient bridge blip the
    check exists for. Widening it to cover `None` would make a zone whose climate
    loads after this integration record nothing at all until it appears, so the
    commit stands and the ordinary machinery deals with the rest.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    assert hass.states.get(CLIMATE_ENTITY) is None, "the entity must be absent"

    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_hvac_mode"), "nothing was commanded"
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT


async def _heat_then_go_offline(
    hass: HomeAssistant,
    coordinator: ZoneCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Command heat against a healthy unit, then take the unit off the network.

    Leaves the zone re-trying a dropped command, a planted idle slope to watch,
    and the outage's own state event well outside the echo window.
    """
    # Short min-cycle, or the retry below is suppressed before it ever reaches
    # the delivery check and the outage never re-enters the dropped path.
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    freezer.tick(timedelta(minutes=5))
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()
    hass.states.async_set(TEMP_ENTITY, "15.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_a_wall_edit_during_an_outage_is_caught_on_reconnect(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A dropped command must not hold the echo window open across an outage.

    Nothing is committed on that path, so the same-mode gate never arms and the
    zone re-enters it on every refresh. Re-stamping the window each time meant
    that for any room sensor reporting faster than `CLIMATE_ECHO_WINDOW_S` the
    window never closed for the length of the outage -- so the reconnect
    carrying somebody's wall edit was absorbed as an echo of ours instead of
    compared against the baseline. Measured at a 30-second sensor, that missed
    the edit every time, where the previous release caught it every time.
    Nothing was delivered, so there is no echo of ours to wait for.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _heat_then_go_offline(hass, coordinator, freezer)

    # It comes back a few seconds later -- well inside the window the retry
    # would have re-armed -- carrying somebody else's settings.
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_COOL, {"temperature": 24.0})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_unit_that_comes_back_as_we_left_it_is_not_an_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Going unavailable is the network, not somebody at the wall.

    `{unavailable, None}` matches nothing, so comparing it flushed the learned
    model on every bridge blip -- and the blip is exactly when the model is
    worth keeping, because nothing about the room changed. Ignoring the
    transition also leaves the baseline describing the last state the unit was
    really in, which is what the reconnect then gets judged against.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await _heat_then_go_offline(hass, coordinator, freezer)
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02

    # And it returns doing exactly what it was doing before.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_an_entity_that_appears_during_the_call_is_recorded(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Absent at dispatch is not unavailable at dispatch.

    A climate platform still loading is absent from the state machine, and
    Home Assistant does not filter on that -- it filters on `entity.available`,
    which an entity that is not there yet cannot fail. Reading a missing entity
    as an outage would discard the commit for a call that may well have landed,
    and the entity registering as `unavailable` a moment later is an ordinary
    startup sequence, not evidence about the call.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    assert hass.states.get(CLIMATE_ENTITY) is None, "the entity must start absent"

    async def _appears(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))
        hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})

    hass.services.async_register("climate", "set_hvac_mode", _appears)
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _calls_for(climate_calls, "set_hvac_mode"), "nothing was commanded"
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT


async def test_a_unit_that_blips_through_unknown_is_not_an_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """`unknown` is the same non-state as `unavailable`, by the same route.

    Home Assistant writes it whenever `ClimateEntity.hvac_mode` is None -- the
    ordinary shape of an MQTT climate that is reachable again but has not
    received its mode topic yet, and of anything that clears its mode before
    first data. Covering only `unavailable` left that class flushing twice per
    blip: once on the way out, and once on the way back, because the first
    comparison leaves the baseline holding `unknown`.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    def _observe(previous: str, state: str, attrs: dict[str, Any]) -> None:
        event: Event[EventStateChangedData] = Event(
            "state_changed",
            {
                "entity_id": CLIMATE_ENTITY,
                "old_state": State(CLIMATE_ENTITY, previous, {"temperature": 19.5}),
                "new_state": State(CLIMATE_ENTITY, state, attrs),
            },
        )
        coordinator._on_climate_state_change(event)

    # An entity in `unknown` is *available*, so it goes on publishing its
    # attributes -- the setpoint is still there and unchanged.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe(HVAC_MODE_HEAT, STATE_UNKNOWN, {"temperature": 19.5})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe(STATE_UNKNOWN, HVAC_MODE_HEAT, {"temperature": 19.5})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02

    # And one coming up cold, reporting neither, says nothing rather than
    # reading as somebody having cleared the dial.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe(HVAC_MODE_HEAT, STATE_UNKNOWN, {})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_an_outage_does_not_erase_what_the_unit_was_last_doing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Ignoring the transition has to mean ignoring it, baseline included.

    Adopting `{unavailable, None}` on the way past would be worse than
    comparing it: the reconnect is then judged against a state no unit ever
    reports. It shows up on any unit whose report differs from what we asked
    for -- one that snaps 19.5 to its own whole degree, say -- because then the
    commanded side cannot cover for the lost baseline either.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # A whole-degree unit: we command 19.5, it reports 20.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    def _observe(previous: str, state: str, attrs: dict[str, Any]) -> None:
        event: Event[EventStateChangedData] = Event(
            "state_changed",
            {
                "entity_id": CLIMATE_ENTITY,
                "old_state": State(CLIMATE_ENTITY, previous, {"temperature": 20.0}),
                "new_state": State(CLIMATE_ENTITY, state, attrs),
            },
        )
        coordinator._on_climate_state_change(event)

    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe(HVAC_MODE_HEAT, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe(STATE_UNAVAILABLE, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_backwards_clock_step_does_not_hide_a_manual_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """An RTC-less Pi correcting against NTP must not open the echo window.

    A negative elapsed time satisfies a bare `< window`, so every climate change
    would be taken for an echo of ours until the clock caught up -- and a wall
    edit made in that stretch is adopted silently.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    freezer.move_to("2026-09-07 11:00:00+00:00")
    edit: Event[EventStateChangedData] = Event(
        "state_changed",
        {
            "entity_id": CLIMATE_ENTITY,
            "old_state": State(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5}),
            "new_state": State(CLIMATE_ENTITY, HVAC_MODE_COOL, {"temperature": 24.0}),
        },
    )
    coordinator._on_climate_state_change(edit)
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_blink_while_commanding_does_not_become_the_baseline(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The apply path may not record a non-state either.

    Plenty of integrations end `async_set_hvac_mode` by refreshing the device,
    and that refresh can briefly mark a just-commanded unit unreachable -- so
    the state read after the calls is sometimes `unavailable`. Adopting it as
    the baseline is the same mistake the listener refuses to make: the reconnect
    is then judged against a state no unit ever reports. The unit here snaps
    19.5 to its own whole degree, so what it reports differs from what we asked
    and the commanded side cannot cover for a lost baseline.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    # Its echo, inside the window: the snapped setpoint becomes the baseline.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()

    async def _accepts_then_blinks(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))
        hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})

    hass.services.async_register("climate", "set_hvac_mode", _accepts_then_blinks)
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "15.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT

    # It comes back doing exactly what it was doing.
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_dial_turned_while_the_mode_is_unknown_is_still_an_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """`unknown` costs us the mode, and only the mode.

    An entity whose `hvac_mode` is None is still available, so it publishes its
    attributes as usual -- and the setpoint is exactly where a wall edit shows.
    Treating `unknown` as a non-state and skipping the whole observation was
    measured swallowing three consecutive dial turns on an MQTT climate whose
    mode topic had not arrived.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    turned: Event[EventStateChangedData] = Event(
        "state_changed",
        {
            "entity_id": CLIMATE_ENTITY,
            "old_state": State(CLIMATE_ENTITY, STATE_UNKNOWN, {"temperature": 19.5}),
            "new_state": State(CLIMATE_ENTITY, STATE_UNKNOWN, {"temperature": 26.0}),
        },
    )
    coordinator._on_climate_state_change(turned)
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_setpoint_that_never_left_is_not_offered_as_a_baseline(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The last-resort baseline obeys the same rule as the commanded side.

    With no baseline yet and an unreadable climate there is nothing to leave
    standing, so the apply path writes its own intent. The setpoint half of that
    has to be what actually landed: a `set_temperature` that raised never
    reached the unit, and offering its value here would let a wall edit that
    happens to land on it pass unnoticed -- an entity advertising only a
    temperature range makes Home Assistant itself raise, so that pairing is
    ordinary hardware, not a corner.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    assert hass.states.get(CLIMATE_ENTITY) is None, "the entity must start absent"

    async def _reject_setpoint(call: Any) -> None:
        raise HomeAssistantError(
            "Set temperature action was used with the target temperature "
            "parameter but the entity does not support it"
        )

    hass.services.async_register("climate", "set_temperature", _reject_setpoint)
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    # The entity appears, and somebody sets it to exactly the setpoint that
    # never reached it.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 21.0})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] is None


async def test_a_restart_mid_blip_does_not_flush_the_restored_model(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """`unknown` with nothing to compare against is not a baseline either.

    An MQTT climate with an availability topic and a non-retained mode topic
    comes up `unavailable`, then `unknown`, then its real mode -- before this
    integration has commanded anything. Adopting the `unknown` would leave the
    baseline holding a value the unit can never report again, so the mode
    arriving a moment later reads as a hand edit and flushes the buffer just
    restored from disk.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    from custom_components.comfort_band import predictor

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=-0.02,
        samples=[{"t": "2026-09-07T11:30:00+00:00", "temp": 19.0, "action": ACTION_IDLE}],
    )
    coordinator._samples_cache = predictor.load_samples(
        coordinator._store.get_zone("office")["samples"]
    )
    assert coordinator._samples_cache

    def _observe(previous: str, state: str, attrs: dict[str, Any]) -> None:
        event: Event[EventStateChangedData] = Event(
            "state_changed",
            {
                "entity_id": CLIMATE_ENTITY,
                "old_state": State(CLIMATE_ENTITY, previous, {}),
                "new_state": State(CLIMATE_ENTITY, state, attrs),
            },
        )
        coordinator._on_climate_state_change(event)

    _observe(STATE_UNAVAILABLE, STATE_UNKNOWN, {"temperature": 19.5})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    _observe(STATE_UNKNOWN, HVAC_MODE_HEAT, {"temperature": 19.5})
    await hass.async_block_till_done()

    assert coordinator._samples_cache, "the restored buffer was flushed"
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


@pytest.mark.parametrize(
    "raised",
    [
        HomeAssistantError("that mode is not supported by this entity"),
        # Not a `HomeAssistantError`: the guard is deliberately broad because a
        # cloud unit's timeout is the case it was written for, and there is
        # nothing waiting to catch it in the fire-and-forget apply task.
        TimeoutError("the cloud never answered"),
    ],
    ids=["refused", "timed-out"],
)
async def test_a_mode_command_that_raises_records_nothing_and_warns_sparingly(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    raised: Exception,
) -> None:
    """The call the commitment rests on had no guard at all.

    A raise escaped into the fire-and-forget apply task, so there was no line
    naming the zone -- only Home Assistant's own "Task exception was never
    retrieved" -- and nothing was committed, which is right but leaves the
    same-mode gate disarmed so the fault re-enters on every refresh. The
    warning is throttled for that reason, and the budget is per fault: once a
    mode command lands, the next failure is news again.

    Reachable from any cloud unit's timeout today, and from Home Assistant
    2025.4 for a mode the entity does not advertise -- which this integration
    risks on every idle release, since it commands `fan_only`.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})

    async def _refuse(call: Any) -> None:
        raise raised

    async def _accept(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))

    hass.services.async_register("climate", "set_hvac_mode", _refuse)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for n in range(8):
            freezer.tick(timedelta(minutes=1))
            hass.states.async_set(TEMP_ENTITY, f"{16.0 + n * 0.1:.1f}", {})
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["last_action"] is None
    warnings = sum("could not command" in r.getMessage() for r in caplog.records)
    assert 2 <= warnings <= 3, warnings
    # Named, because several of these stringify to a bare translation key and
    # the traceback that used to carry the type is gone.
    assert any(type(raised).__name__ in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]

    # A mode command lands, so the fault is over -- and its return is announced
    # rather than sitting inside the stale budget.
    hass.services.async_register("climate", "set_hvac_mode", _accept)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(TEMP_ENTITY, "16.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    hass.services.async_register("climate", "set_hvac_mode", _refuse)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set(TEMP_ENTITY, "16.8", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert sum("could not command" in r.getMessage() for r in caplog.records) == 1, [
        r.getMessage() for r in caplog.records
    ]


async def test_a_mode_raise_is_contained_and_a_cancellation_is_not(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The point of the guard is that nothing escapes the apply task.

    Applies are fire-and-forget, so before this an ordinary cloud error left
    Home Assistant logging its own anonymous "Task exception was never
    retrieved" once per refresh, with no zone, entity or mode in it.

    The catch is broad on purpose -- a timeout is not a `HomeAssistantError` --
    but not unbounded: `CancelledError` is a `BaseException`, so shutdown and
    reload still cancel an apply in flight rather than having it swallowed and
    logged as a fault.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})

    async def _refuse(call: Any) -> None:
        raise HomeAssistantError("the confirming poll timed out")

    hass.services.async_register("climate", "set_hvac_mode", _refuse)

    created: list[Any] = []
    original = hass.async_create_task

    def _capture(target: Any, *args: Any, **kwargs: Any) -> Any:
        task = original(target, *args, **kwargs)
        created.append(task)
        return task

    hass.async_create_task = _capture  # type: ignore[method-assign]
    try:
        hass.states.async_set(TEMP_ENTITY, "16.0", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    finally:
        hass.async_create_task = original  # type: ignore[method-assign]

    assert created, "the apply is dispatched as a task; nothing was captured"
    escaped = [t.exception() for t in created if t.done() and not t.cancelled()]
    assert not any(escaped), escaped

    # A cancellation is not a fault, and must not be reported as one.
    async def _cancelled(call: Any) -> None:
        raise asyncio.CancelledError

    hass.services.async_register("climate", "set_hvac_mode", _cancelled)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        freezer.tick(timedelta(minutes=10))
        hass.states.async_set(TEMP_ENTITY, "21.0", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not [r for r in caplog.records if "could not command" in r.getMessage()]


async def test_a_first_ever_apply_that_is_dropped_leaves_no_window(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """There is nothing to restore before anything has been commanded.

    A zone whose climate is unreachable at the very first apply hands back the
    `None` it started with, leaving no window -- so the reconnect is compared
    rather than absorbed, which is what catches somebody who moved the dial
    while it was off the network.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["last_action"] is None
    assert coordinator._last_command_at is None


async def test_the_baseline_prefers_what_the_unit_reports_to_what_it_reported(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Carrying a setpoint forward is for an absent one, not a changed one.

    The snapshot takes what the entity is reporting whenever it reports
    anything; the carry-forward is the fallback. Preferring the carried value
    unconditionally would freeze the baseline at whatever it held when the
    entity first appeared, and every later report of the unit's own value would
    read as a hand edit.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    assert hass.states.get(CLIMATE_ENTITY) is None, "the entity must start absent"

    # First apply with no entity at all: the baseline falls back to our intent.
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._last_command_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 19.5}

    # The entity appears holding its own value. Its arrival is a state-added
    # event, which the detector adopts nothing from.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 23.0})
    await hass.async_block_till_done()

    # The next apply reads it, and must record what it says rather than the
    # 19.5 it is carrying.
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "15.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._last_command_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 23.0}

    # So the unit going on reporting it is not an edit.
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 23.0, "fan_mode": "low"})
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_mode_raise_writes_nothing_at_all(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The guard catches and reports. It does not revise anything.

    Seven attempts have tried to say something useful here about whether the
    command arrived -- vouch for the mode, hand the echo window back, hand it
    back only on retries -- and every one was measured either flushing the
    learned model on a unit that had taken the mode, or swallowing a wall edit.
    The window is the wrong instrument for the question and `_commanded_state`
    cannot hold two values per field, so until one of those changes the honest
    thing is to touch none of it. This pins that, because the next attempt will
    look like an improvement.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    # An outage first, so there is a live budget belonging to another fault for
    # the raise below to leave alone.
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert set(coordinator._command_warn_logged_at) == {"dropped"}

    # The unit is back, but nothing has landed yet, so that outage's budget is
    # still live when the mode call starts raising.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    await hass.async_block_till_done()

    before = (
        coordinator._last_command_at,
        dict(coordinator._last_command_state or {}),
        dict(coordinator._commanded_state or {}),
        dict(coordinator._store.get_zone("office")),
    )

    async def _refuse(call: Any) -> None:
        raise HomeAssistantError("the confirming poll timed out")

    hass.services.async_register("climate", "set_hvac_mode", _refuse)
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE

    # The stamp is written before the call and left where the raise found it;
    # everything the detector reads, and the store, is untouched.
    assert coordinator._last_command_at == dt_util.utcnow()
    assert dict(coordinator._last_command_state or {}) == before[1]
    assert dict(coordinator._commanded_state or {}) == before[2]
    assert dict(coordinator._store.get_zone("office")) == before[3]
    # Its own budget is spent; the one belonging to the earlier outage is
    # neither cleared nor spent by it.
    assert set(coordinator._command_warn_logged_at) == {"dropped", "mode"}


async def test_a_mode_raise_leaves_an_earlier_commands_vouch_alone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A raise must not disturb what the last landed command asked for.

    Recording the attempted mode reads as additive and is not: the commanded
    state holds one value per field, so writing the mode discards the one a
    command that actually landed put there -- and a lagging unit's own
    acknowledgement of that command then matches neither side and flushes the
    learned model. Idle releases bypass both dwell gates by design, so the
    raising apply can follow the delivered one within seconds.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # The unit lags: still reporting fan_only/21.0 when the heat command lands,
    # so the baseline holds neither the mode nor the setpoint we asked for and
    # only the commanded side can cover its echo.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 19.5}
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    # Seconds later the room is back in band, and the release's mode call fails.
    async def _refuse(call: Any) -> None:
        raise HomeAssistantError("the confirming poll timed out")

    hass.services.async_register("climate", "set_hvac_mode", _refuse)
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator._commanded_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 19.5}

    # The unit finally acknowledges the heat command it was really given.
    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 15))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_superseded_dropped_apply_revises_nothing_either(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dropped-command path hands the window back under the same rule.

    Both undelivered paths write from values read before their own await, so
    both have to check they are still the current apply. A flapping bridge
    gives the losing one an unavailable entity on either side of a call that
    hung right through a command another apply landed in the meantime.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=0)

    gate = asyncio.Event()
    seen: list[dict[str, Any]] = []

    async def _first_call_hangs(call: Any) -> None:
        seen.append(dict(call.data))
        climate_calls.append((call.service, dict(call.data)))
        if len(seen) == 1:
            await gate.wait()

    hass.services.async_register("climate", "set_hvac_mode", _first_call_hangs)

    async def _pump() -> None:
        for _ in range(200):
            await asyncio.sleep(0)

    # The loser starts while the unit is off the network, and parks in the call.
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await _pump()
    assert len(seen) == 1, seen

    # The unit comes back, the winner lands a command and arms the window.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
    await _pump()
    hass.states.async_set(TEMP_ENTITY, "15.9", {})
    await coordinator.async_refresh()
    await _pump()
    assert len(seen) == 2, seen
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT
    armed_by_the_winner = coordinator._last_command_at
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    # It drops off again, and only now does the parked call return -- so the
    # loser sees an unavailable entity on both sides and takes the dropped path.
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    await _pump()
    gate.set()
    await _pump()

    assert coordinator._last_command_at is armed_by_the_winner

    # And the winner's own echo, still inside the window it armed, is absorbed.
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_unit_that_took_the_mode_anyway_is_not_a_manual_edit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A raise does not mean the unit never got it.

    On the Home Assistant pinned here, a raise from `set_hvac_mode` is a
    platform or cloud error -- an unadvertised mode still only warns until
    2025.4 -- and that is precisely the class where the command usually did
    land: the confirming poll timed out, the unit applied the mode, and it
    published a second later. Every attempt at closing the window on the way out
    of the raise -- on every failing apply, or on every one after the first of a
    run -- flushed the learned model when that publish arrived, at up to 57 an
    hour. So the window is left exactly as an unguarded raise left it.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 19.5})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    async def _refuse(call: Any) -> None:
        raise HomeAssistantError("the confirming poll timed out")

    hass.services.async_register("climate", "set_hvac_mode", _refuse)
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE

    # It had applied the release after all, and says so a few seconds later.
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 19.5})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_an_outage_leaves_a_live_echo_window_alone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dropped-command path hands the window back the same way.

    It restores rather than clears for the same reason the mode-raise path
    does: a command from moments earlier may still be echoing, and that window
    is its own. Clearing it would make that command's echo -- the coerced
    setpoint a lagging unit reports late -- read as a hand edit.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=0)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)

    # Five seconds later, still inside that command's window, the unit drops off
    # and the retry is dropped at dispatch.
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()
    hass.states.async_set(TEMP_ENTITY, "15.9", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT

    # The first command's own echo lands, snapped to the unit's whole degree,
    # still inside the window it armed.
    freezer.tick(timedelta(seconds=15))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 20.0})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_setpoint_the_unit_stops_reporting_is_not_stored_as_a_baseline(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The absence rule has to hold where the baseline is written, too.

    A unit that varies `TARGET_TEMPERATURE` by mode reports no target while it
    is fanning. Storing that `None` puts a baseline in place the listener's own
    carry-forward can no longer rescue, because the *next* transition is judged
    against it -- so the trip back out of `fan_only` flushed, once per heat/idle
    cycle on that hardware.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    # The unit holds its own 21.0, not the 19.5 we ask for.
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 21.0})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._last_command_state == {"hvac_mode": HVAC_MODE_HEAT, "target_temp": 21.0}

    # Released to fan_only, where it reports no target at all. The baseline has
    # to keep the target it had rather than record the absence.
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator._last_command_state == {
        "hvac_mode": HVAC_MODE_FAN_ONLY,
        "target_temp": 21.0,
    }

    # Heat again, and this time the unit lags: it is still fanning when the
    # snapshot is taken, and reports the mode and its own target well after the
    # window has closed. Nothing about that is somebody at the wall.
    await coordinator._store.async_update_zone("office", persisted_idle_slope=-0.02)
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_HEAT

    freezer.tick(timedelta(seconds=CLIMATE_ECHO_WINDOW_S + 60))
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_HEAT, {"temperature": 21.0})
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["persisted_idle_slope"] == -0.02


async def test_a_climate_with_no_setpoint_before_any_command_is_safe(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Carrying a setpoint forward needs something to carry it from.

    A zone that has not commanded yet -- freshly set up, or in shadow mode --
    has no baseline, and the entity it is watching may well report no target:
    `fan_only` on a platform that varies `supported_features` by mode is
    exactly that. The carry-forward runs on every observation, so without its
    guard that is a `TypeError` inside a state-change callback, where nothing
    is waiting to catch it.
    """
    from homeassistant.core import Event, EventStateChangedData, State

    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    assert coordinator._last_command_state is None

    event: Event[EventStateChangedData] = Event(
        "state_changed",
        {
            "entity_id": CLIMATE_ENTITY,
            "old_state": State(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {}),
            "new_state": State(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"current_temperature": 20.0}),
        },
    )
    coordinator._on_climate_state_change(event)
    await hass.async_block_till_done()

    assert coordinator._last_command_state == {
        "hvac_mode": HVAC_MODE_FAN_ONLY,
        "target_temp": None,
    }


async def test_a_mode_fault_and_an_outage_do_not_share_a_warning_budget(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two faults, two budgets -- or the second one goes unreported.

    They cannot occur in the same apply (a raising call never reaches the
    delivery check), but they occur minutes apart on a flaky bridge, well
    inside one throttle interval.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # The unit is unreachable, so the command is dropped at dispatch.
        hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
        hass.states.async_set(TEMP_ENTITY, "16.0", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # It comes back, and now the call itself raises.
        async def _refuse(call: Any) -> None:
            raise HomeAssistantError("that mode is not supported by this entity")

        hass.services.async_register("climate", "set_hvac_mode", _refuse)
        hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"temperature": 21.0})
        freezer.tick(timedelta(minutes=2))
        hass.states.async_set(TEMP_ENTITY, "15.9", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sum("was dropped" in r.getMessage() for r in caplog.records) == 1, [
        r.getMessage() for r in caplog.records
    ]
    assert sum("could not command" in r.getMessage() for r in caplog.records) == 1, [
        r.getMessage() for r in caplog.records
    ]


async def test_an_undeliverable_command_records_nothing_and_warns_sparingly(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A climate outage must leave the store and the buffer alone, and not shout.

    Scoped to what the apply path does about it. What the detector makes of the
    entity's own transition to `unavailable` is a separate question, covered by
    `test_a_unit_that_comes_back_as_we_left_it_is_not_an_edit`.

    No sample is recorded: we don't know what an unreachable unit is doing, and
    guessing displaces data we already have. Labelling under `last_action` was
    tried and measured -- with `last_action=heating` and a unit that has really
    stopped, an hour of good idle drift becomes a heat run the v0.15.0 sign
    guard rejects, so the idle slope reads None where it would have been
    learned.

    And the warning is throttled, because nothing is committed on this path so
    the same-mode gate never arms and every refresh re-enters it.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})
    before = list(coordinator._samples_cache)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for n in range(8):
            freezer.tick(timedelta(minutes=1))
            hass.states.async_set(TEMP_ENTITY, f"{16.0 + n * 0.1:.1f}", {})
            await coordinator.async_refresh()
            await hass.async_block_till_done()

    assert coordinator._samples_cache == before, "an outage must not add samples"
    assert coordinator._store.get_zone("office")["last_action"] != ACTION_HEAT
    # Two-sided: an upper bound alone passes a throttle that logs once and then
    # never again, which would silence a later outage entirely.
    warnings = sum("was dropped" in r.getMessage() for r in caplog.records)
    assert 2 <= warnings <= 3, warnings


async def test_a_backwards_clock_step_does_not_silence_the_dropped_command_log(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clock correction must not starve this throttle either.

    Same fault as `test_a_backwards_clock_step_does_not_silence_the_log`, and
    the same correlated cause: the power cut that rebooted an RTC-less Pi is
    also what took the HVAC's bridge down. A negative elapsed time satisfies a
    bare `< interval`, so nothing would be said about a unit that is dropping
    every command until the clock caught up.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})

    # One dropped command, logged, which stamps the budget.
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The clock jumps backwards past the stamp.
    freezer.move_to("2026-09-07 10:00:00+00:00")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hass.states.async_set(TEMP_ENTITY, "15.9", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert sum("was dropped" in r.getMessage() for r in caplog.records) == 1, [
        r.getMessage() for r in caplog.records
    ]


async def test_a_new_outage_is_announced_after_the_unit_recovers(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The throttle must not carry across a recovery.

    Otherwise a second outage shortly after the first is silent -- and a unit
    that drops out repeatedly is exactly the one worth hearing about.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    # A short min-cycle so the second outage can land inside the 5-minute
    # throttle window without the same-mode gate suppressing the command first.
    await coordinator._store.async_update_zone("office", min_cycle_minutes=1)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})

    async def _drop_then_recover(unavailable: bool, temp: str) -> None:
        hass.states.async_set(
            CLIMATE_ENTITY, STATE_UNAVAILABLE if unavailable else HVAC_MODE_FAN_ONLY, {}
        )
        hass.states.async_set(TEMP_ENTITY, temp, {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await _drop_then_recover(True, "16.0")
        # The unit comes back and a command lands, which clears the latch.
        freezer.tick(timedelta(seconds=30))
        await _drop_then_recover(False, "15.9")
        assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT
        # It drops out again, well inside the 5-minute throttle window -- so
        # only the latch clearing on the successful command can let this speak.
        # Past the (shortened) min-cycle, or the same-mode gate would suppress
        # the command and it would never reach the delivery check at all.
        freezer.tick(timedelta(minutes=2))
        await _drop_then_recover(True, "15.8")

    assert sum("was dropped" in r.getMessage() for r in caplog.records) == 2, [
        r.getMessage() for r in caplog.records
    ]


async def test_a_unit_that_blinks_after_accepting_is_still_recorded(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Availability is judged at dispatch, not after the call returns.

    Plenty of integrations end `async_set_hvac_mode` by refreshing the device,
    and that refresh can briefly mark a just-commanded unit unreachable. Reading
    availability only afterwards cannot tell that from a genuinely dropped call
    -- and discarding the commit there is worse than the bug it was meant to
    fix: nothing is ever recorded, so the min-cycle guard never arms and the
    zone re-commands on every single refresh.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    async def _accept_then_blink(call: Any) -> None:
        climate_calls.append(("set_hvac_mode", dict(call.data)))
        # Delivered -- and the unit drops off its bridge on the way back.
        hass.states.async_set(CLIMATE_ENTITY, STATE_UNAVAILABLE, {})

    hass.services.async_register("climate", "set_hvac_mode", _accept_then_blink)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert any(
        c["hvac_mode"] == HVAC_MODE_HEAT for c in _calls_for(climate_calls, "set_hvac_mode")
    ), climate_calls
    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT


async def test_a_fan_failure_does_not_abort_the_rest_of_the_apply(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The fan command is a refinement, not a precondition.

    `_maybe_command_fan` catches only `HomeAssistantError`, so a cloud unit's
    timeout escaped into the fire-and-forget apply task -- taking the setpoint,
    the echo baseline and the sample with it, for a cycle that was already
    running.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    await coordinator._store.async_update_zone(
        "office", fan_control_enabled=True, active_fan_mode="high"
    )

    async def _fan_timeout(call: Any) -> None:
        raise TimeoutError("cloud gateway did not respond")

    hass.services.async_register("climate", "set_fan_mode", _fan_timeout)
    hass.states.async_set(
        CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"fan_modes": ["low", "high"], "fan_mode": "low"}
    )
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT
    # The setpoint still went out, and the sample was still recorded.
    assert _calls_for(climate_calls, "set_temperature"), climate_calls
    assert coordinator._samples_cache


async def test_delivery_is_judged_before_the_call_not_after(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only a unit that was already unreachable had its command dropped.

    Home Assistant filters on availability at dispatch, so the state *before*
    the call is what decides delivery. This pins the pre-call half specifically:
    with the entity available beforehand the commit must stand no matter what
    the entity reports afterwards.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)

    async def _accept_then_vanish(call: Any) -> None:
        climate_calls.append(("set_hvac_mode", dict(call.data)))
        hass.states.async_remove(CLIMATE_ENTITY)

    hass.services.async_register("climate", "set_hvac_mode", _accept_then_vanish)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator._store.get_zone("office")["last_action"] == ACTION_HEAT


async def test_the_commit_lands_before_the_setpoint_call(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Ordering, not just the guard, is what makes the commit safe.

    `except Exception` doesn't catch `BaseException`, and the apply task is
    cancelled on shutdown (it is created on `hass`, so `async_stop` cancels it)
    -- so if the commit sat after the setpoint call, a cancellation there would
    still leave the unit conditioning with nothing recorded. Pinning the order rather than the
    guard: with the commit moved back below the setpoint, this fails.
    """
    freezer.move_to("2026-09-07 12:00:00+00:00")
    coordinator = await _setup_enabled_zone(hass, climate_calls)
    seen: list[str | None] = []

    async def _observe(call: Any) -> None:
        # What the store already says at the moment the setpoint is issued.
        seen.append(coordinator._store.get_zone("office")["last_action"])
        climate_calls.append(("set_temperature", dict(call.data)))

    hass.services.async_register("climate", "set_temperature", _observe)
    hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {})
    hass.states.async_set(TEMP_ENTITY, "16.0", {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert seen == [ACTION_HEAT], seen
