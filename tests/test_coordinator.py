"""ZoneCoordinator behaviour tests.

Smoke tests cover the pure read pipeline (no live triggers); behaviour tests
exercise the full action-application path (`_maybe_apply_action`) with the
pytest-freezer `freezer` fixture for time travel.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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
