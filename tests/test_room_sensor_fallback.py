"""v0.18.0: a stand-in reading keeps the zone controlling while its room
sensor is dark.

The incident: two Thread routers were switched off, the battery sensors that
hung off them left the mesh, and the two zones -- both just parked in
`fan_only` -- commanded nothing for the rest of the night. v0.16.0 made that
visible; this makes the zone carry on from the climate entity's own reading
(or a configured fallback sensor) once the outage has outlasted a grace period.

Every test here builds an *enabled* zone and drives it through the real
listeners, because the property under test is that a zone whose sensor emits
nothing still wakes up: by the grace timer, and by changes in the stand-in.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.comfort_band.const import (
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    CONF_CLIMATE_ENTITY,
    CONF_FALLBACK_TEMP_SENSOR,
    CONF_FALLBACK_TO_CLIMATE,
    CONF_KIND,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    DOMAIN,
    ENTRY_KIND_ZONE,
    FALLBACK_DEADBAND_EXTRA,
    FALLBACK_GRACE_S,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    IDLE_SETTLE_MINUTES,
    ROOM_SOURCE_CLIMATE,
    ROOM_SOURCE_FALLBACK_SENSOR,
    ROOM_SOURCE_NONE,
    ROOM_SOURCE_PRIMARY,
    SENSOR_EDGE_LOG_INTERVAL_S,
    SLOPE_MIN_SAMPLES,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator
from custom_components.comfort_band.hysteresis import HysteresisDecision
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"
FALLBACK_ENTITY = "sensor.office_backup_temp"
CLIMATE_ENTITY = "climate.office_hvac"

# Past the grace period plus the timer's one-second margin.
PAST_GRACE = timedelta(seconds=FALLBACK_GRACE_S + 5)
# Enough for the 2 s state-change debounce *and* the coordinator's own 10 s
# request-refresh cooldown (a second request inside it is deferred, not
# dropped) -- so consecutive waits in one test each see their refresh land.
PAST_DEBOUNCE = timedelta(seconds=15)
# Idle samples a test drives, one a minute, to have a live idle slope: the
# settle window the idle slope leaves out (v0.19.0), then ten minutes of it.
IDLE_SAMPLES = IDLE_SETTLE_MINUTES + 10

_COORDINATORS: list[ZoneCoordinator] = []


@pytest.fixture(autouse=True)
async def _unload_coordinators(hass: HomeAssistant) -> Any:
    """Same contract as the coordinator test module: the helper wires the real
    listeners, so a coordinator left standing holds a pending timer that HA's
    lingering-timer check would fail the test on."""
    _COORDINATORS.clear()
    yield
    for coordinator in _COORDINATORS:
        await coordinator.async_unload()
        await coordinator.async_shutdown()
    _COORDINATORS.clear()


async def _enabled_zone(
    hass: HomeAssistant,
    *,
    fallback_temp_entity_id: str | None = None,
    fallback_to_climate: bool = True,
) -> ZoneCoordinator:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", enabled=True)
    coordinator = ZoneCoordinator(
        hass,
        store,
        "office",
        CLIMATE_ENTITY,
        TEMP_ENTITY,
        fallback_temp_entity_id=fallback_temp_entity_id,
        fallback_to_climate=fallback_to_climate,
    )
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    return coordinator


def _set_climate(hass: HomeAssistant, current_temperature: float | None, mode: str) -> None:
    attrs: dict[str, Any] = {"temperature": 21.0}
    if current_temperature is not None:
        attrs["current_temperature"] = current_temperature
    hass.states.async_set(CLIMATE_ENTITY, mode, attrs)


def _modes(climate_calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [data["hvac_mode"] for srv, data in climate_calls if srv == "set_hvac_mode"]


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Let pending event dispatch run, then advance the frozen clock and let
    every timer that came due run.

    The drain comes first on purpose: a state change is dispatched to the
    coordinator's listeners via `call_soon`, and the listener is what arms
    the debounce timer. Tick first and that timer is armed *after* the clock
    has moved, due at a moment the harness has already passed -- it would
    only fire on some later tick, which a test's final wait never provides.
    """
    await hass.async_block_till_done()
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _healthy_idle_zone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    climate_calls: list[tuple[str, dict[str, Any]]],
    **kwargs: Any,
) -> ZoneCoordinator:
    """An enabled zone that has just been parked in `fan_only` by its own
    reading -- the state both incident zones were in when their sensor died."""
    freezer.move_to("2026-09-15 08:40:00+00:00")
    coordinator = await _enabled_zone(hass, **kwargs)
    _set_climate(hass, 21.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})  # inside the 19.5-22.5 default band
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    climate_calls.clear()
    return coordinator


# ---------------------------------------------------------------------------
# The grace window: nothing changes until the outage has lasted.
# ---------------------------------------------------------------------------


async def test_inside_the_grace_window_the_zone_behaves_as_before(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    # The unit's own sensor says the room is cold, but the sensor only just
    # dropped: a blip must not swap sources.
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)

    assert coordinator.data.sensor_available is False
    assert coordinator.data.room is None
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    assert coordinator.data.fallback_active is False
    assert coordinator.data.decision.action == ACTION_UNKNOWN
    assert _modes(climate_calls) == []


async def test_stand_in_changes_inside_the_grace_window_do_not_refresh(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Inside the window the zone must behave exactly as it did before
    v0.18.0, and that includes not waking for readings it is not yet allowed
    to use. The grace timer's own refresh is what engages a stand-in, and it
    reads whatever the stand-in says at that moment."""
    coordinator = await _healthy_idle_zone(
        hass, freezer, climate_calls, fallback_temp_entity_id=FALLBACK_ENTITY
    )
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    refreshes = 0
    original = coordinator._async_update_data

    async def counting() -> Any:
        nonlocal refreshes
        refreshes += 1
        return await original()

    coordinator._async_update_data = counting  # type: ignore[method-assign]

    for reading in ("18.0", "17.5", "17.0"):
        hass.states.async_set(FALLBACK_ENTITY, reading, {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
    for reading in (20.0, 19.0, 18.0):
        _set_climate(hass, reading, HVAC_MODE_FAN_ONLY)
        await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 0
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    assert _modes(climate_calls) == []

    # Only the clock moves past the boundary: one refresh, the timer's.
    await _settle(hass, freezer, PAST_GRACE)
    assert refreshes == 1
    assert coordinator.data.room_source == ROOM_SOURCE_FALLBACK_SENSOR
    assert coordinator.data.room == 17.0
    assert HVAC_MODE_HEAT in _modes(climate_calls)


async def test_the_grace_timer_alone_wakes_the_zone_and_the_climate_reading_takes_over(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The whole point: a dark sensor emits no events and an idle zone has no
    schedule timer, so the grace timer must be what brings the zone back."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.decision.action == ACTION_UNKNOWN

    # No state change of any kind -- only the clock moves.
    await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.sensor_available is False, "the configured sensor is still dark"
    assert coordinator.data.room == 15.0
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.fallback_active is True
    assert coordinator.data.decision.action == ACTION_HEAT
    assert HVAC_MODE_HEAT in _modes(climate_calls)


async def test_the_configured_fallback_sensor_beats_the_climate_reading(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await _healthy_idle_zone(
        hass, freezer, climate_calls, fallback_temp_entity_id=FALLBACK_ENTITY
    )
    hass.states.async_set(FALLBACK_ENTITY, "17.5", {})
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room == 17.5
    assert coordinator.data.room_source == ROOM_SOURCE_FALLBACK_SENSOR
    assert coordinator.data.decision.action == ACTION_HEAT


async def test_the_climate_reading_covers_for_a_fallback_sensor_that_is_also_dark(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await _healthy_idle_zone(
        hass, freezer, climate_calls, fallback_temp_entity_id=FALLBACK_ENTITY
    )
    hass.states.async_set(FALLBACK_ENTITY, "unavailable", {})
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.decision.action == ACTION_HEAT


async def test_no_stand_in_when_the_climate_reading_is_switched_off(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Opting out restores the pre-v0.18.0 behaviour exactly."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls, fallback_to_climate=False)
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room is None
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    assert coordinator.data.decision.action == ACTION_UNKNOWN
    assert _modes(climate_calls) == []


async def test_a_missing_or_non_finite_climate_reading_is_no_reading(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)

    for bad in (None, "nan", "inf", "warm"):
        hass.states.async_set(CLIMATE_ENTITY, HVAC_MODE_FAN_ONLY, {"current_temperature": bad})
        await _settle(hass, freezer, PAST_GRACE)
        assert coordinator.data.room_source == ROOM_SOURCE_NONE, bad
        assert coordinator.data.decision.action == ACTION_UNKNOWN, bad
    assert _modes(climate_calls) == []


# ---------------------------------------------------------------------------
# Living on the stand-in.
# ---------------------------------------------------------------------------


async def _zone_on_the_climate_reading(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    climate_calls: list[tuple[str, dict[str, Any]]],
    **kwargs: Any,
) -> ZoneCoordinator:
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls, **kwargs)
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert HVAC_MODE_HEAT in _modes(climate_calls)
    climate_calls.clear()
    return coordinator


async def test_a_change_in_the_climate_reading_drives_the_next_decision(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the sensor dark, the unit's own reading is the only thing that
    tracks the room -- so a change in it, and nothing else, must reach the
    decider. Here the room warms up to band and the heat is released."""
    coordinator = await _zone_on_the_climate_reading(hass, freezer, climate_calls)

    _set_climate(hass, 20.0, HVAC_MODE_HEAT)  # at the band's low edge: release
    await _settle(hass, freezer, PAST_DEBOUNCE)

    assert coordinator.data.room == 20.0
    assert coordinator.data.decision.action == ACTION_IDLE
    assert HVAC_MODE_FAN_ONLY in _modes(climate_calls)


async def test_a_change_in_the_fallback_sensor_drives_the_next_decision(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The fallback sensor's events go through the same filtered listener as
    the room sensor's; once it is the reading in use they must get through,
    or the zone would sit on whatever the grace timer read until something
    else woke it."""
    coordinator = await _healthy_idle_zone(
        hass, freezer, climate_calls, fallback_temp_entity_id=FALLBACK_ENTITY
    )
    hass.states.async_set(FALLBACK_ENTITY, "17.5", {})
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_FALLBACK_SENSOR
    assert HVAC_MODE_HEAT in _modes(climate_calls)
    climate_calls.clear()

    hass.states.async_set(FALLBACK_ENTITY, "20.5", {})  # back in band: release
    await _settle(hass, freezer, PAST_DEBOUNCE)

    assert coordinator.data.room == 20.5
    assert coordinator.data.room_source == ROOM_SOURCE_FALLBACK_SENSOR
    assert coordinator.data.decision.action == ACTION_IDLE
    assert HVAC_MODE_FAN_ONLY in _modes(climate_calls)


async def test_the_units_own_echo_does_not_refresh_the_zone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every command changes the climate's mode and setpoint; only the reading
    is a reason to refresh, or a zone on the stand-in would refresh on its own
    echo after every command."""
    coordinator = await _zone_on_the_climate_reading(hass, freezer, climate_calls)
    refreshes = 0
    original = coordinator._async_update_data

    async def counting() -> Any:
        nonlocal refreshes
        refreshes += 1
        return await original()

    coordinator._async_update_data = counting  # type: ignore[method-assign]

    # Same reading, different mode/setpoint: the unit acknowledging our heat.
    hass.states.async_set(
        CLIMATE_ENTITY, HVAC_MODE_HEAT, {"current_temperature": 15.0, "temperature": 19.5}
    )
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 0

    hass.states.async_set(
        CLIMATE_ENTITY, HVAC_MODE_HEAT, {"current_temperature": 16.0, "temperature": 19.5}
    )
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 1


async def test_stand_in_deadbands_are_wider(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reading that would start heat from the room sensor sits inside the
    widened deadband on the stand-in; one past the extra margin does not."""
    freezer.move_to("2026-09-15 08:40:00+00:00")
    coordinator = await _enabled_zone(hass)
    zone = coordinator.get_zone_data()
    low, deadband = zone["manual_low"], zone["deadband_below"]
    just_below = low - deadband - 0.1
    well_below = low - deadband - FALLBACK_DEADBAND_EXTRA - 0.1

    # Sanity: from the room sensor, `just_below` heats.
    _set_climate(hass, 21.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, str(just_below), {})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.decision.action == ACTION_HEAT

    # Park it idle again, then go dark with the stand-in at `just_below`.
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.decision.action == ACTION_IDLE
    _set_climate(hass, just_below, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.decision.action == ACTION_IDLE

    _set_climate(hass, well_below, HVAC_MODE_FAN_ONLY)
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.decision.action == ACTION_HEAT


async def test_no_samples_are_recorded_from_a_stand_in(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The learned model describes the configured sensor. Commands go out on
    the stand-in; nothing is appended to the buffer."""
    coordinator = await _zone_on_the_climate_reading(hass, freezer, climate_calls)
    # The stand-in committed heat as it engaged, so the first refresh after
    # that flushes (test_a_stand_in_cycle_does_not_bleed_into_the_idle_slope);
    # what is under test here is that nothing is appended from then on.
    _set_climate(hass, 15.5, HVAC_MODE_HEAT)
    await _settle(hass, freezer, timedelta(minutes=2))
    before = list(coordinator._samples_cache)
    assert before == []

    for reading in (16.0, 16.5, 17.0):
        _set_climate(hass, reading, HVAC_MODE_HEAT)
        await _settle(hass, freezer, timedelta(minutes=2))

    assert coordinator._samples_cache == before

    # ...and sampling resumes the moment the configured sensor is back.
    hass.states.async_set(TEMP_ENTITY, "17.2", {})
    await _settle(hass, freezer, timedelta(minutes=2))
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    assert len(coordinator._samples_cache) == len(before) + 1


async def test_a_stand_in_cycle_does_not_bleed_into_the_idle_slope(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Nothing is sampled from a stand-in, so a heat cycle it commands leaves
    a gap in the buffer -- and the predictor joined runs by action label
    alone. Without a flush before hand-back, the idle samples on either side
    of the outage become one idle run spanning a real heat cycle: the idle
    slope reads as strong passive warming, is persisted, and a learning zone
    cools inside the deadband on the strength of it. The incident shape
    exactly: idle, a router reboot's worth of stand-in heat, idle again.

    Since v0.20.0 a run also stops at a gap longer than SAMPLE_MAX_GAP_MINUTES,
    which this outage is, so it would now be split without the flush too. The
    flush still matters for an outage short enough to be spanned -- the grace
    period and one short cycle fit inside the limit -- and is what this pins."""
    freezer.move_to("2026-09-15 08:40:00+00:00")
    coordinator = await _enabled_zone(hass)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _set_climate(hass, 20.0, HVAC_MODE_FAN_ONLY)
    # Forty minutes of flat idle samples (the last digit alternates so every
    # write is a state change; samples are rate-limited to one a minute).
    for i in range(IDLE_SAMPLES):
        hass.states.async_set(TEMP_ENTITY, "20.0" if i % 2 == 0 else "20.02", {})
        await _settle(hass, freezer, timedelta(seconds=61))
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator.data.idle_slope_source == "live"
    assert len(coordinator._samples_cache) == IDLE_SAMPLES

    # The sensor dies with the unit's own reading at 15. Inside the grace
    # window the buffer is intact and still the zone's own; what it has
    # persisted by the end of that window is the value that must survive.
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    zone = coordinator.get_zone_data()
    persisted_before = zone["persisted_idle_slope"]
    persisted_at_before = zone["persisted_idle_slope_at"]
    assert persisted_before is not None and persisted_at_before is not None
    assert abs(persisted_before) < 0.01, "flat, as sampled"
    samples_in_store = zone["samples"]
    assert len(samples_in_store) > 0

    # The stand-in takes over and heats. The engage refresh itself flushes
    # nothing -- the heat it decides on is committed by the apply task it
    # spawns, and until an action changes the gap joins nothing.
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.decision.action == ACTION_HEAT
    assert coordinator.get_zone_data()["last_action"] == ACTION_HEAT
    assert len(coordinator._samples_cache) == IDLE_SAMPLES
    assert coordinator.get_zone_data()["samples"] == samples_in_store

    # The first refresh to find the heat committed is what flushes.
    _set_climate(hass, 16.0, HVAC_MODE_HEAT)
    await _settle(hass, freezer, timedelta(minutes=4))
    assert coordinator._samples_cache == []
    assert coordinator.get_zone_data()["samples"] == []

    # The room warms on the stand-in over twenty minutes and is released.
    for reading in (17.0, 18.0, 19.0, 20.0):
        _set_climate(hass, reading, HVAC_MODE_HEAT)
        await _settle(hass, freezer, timedelta(minutes=4))
    assert coordinator.data.decision.action == ACTION_IDLE

    # The sensor returns: the room really is warmer now, and the zone is idle.
    hass.states.async_set(TEMP_ENTITY, "21.6", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    hass.states.async_set(TEMP_ENTITY, "21.58", {})
    await _settle(hass, freezer, timedelta(seconds=61))

    # The pre-outage idle rate survives untouched -- and unrefreshed: a stamp
    # that advanced would mean the frozen buffer had been persisted from.
    zone = coordinator.get_zone_data()
    assert zone["persisted_idle_slope"] == persisted_before
    assert zone["persisted_idle_slope_at"] == persisted_at_before
    # Only the post-hand-back samples form the idle run -- too few for a live
    # slope, so MPC runs on the cached pre-outage value rather than a phantom.
    assert [s.temp for s in coordinator._samples_cache] == [21.6, 21.58]
    slopes = coordinator.data.thermal_slopes
    # The run this refresh found is one sample, and inside its settle window
    # at that, so nothing is behind a live estimate.
    assert slopes.sample_count_idle == 0
    assert slopes.sample_count_idle < SLOPE_MIN_SAMPLES
    assert coordinator.data.idle_slope_source == "cached"
    assert slopes.idle == persisted_before

    # Above `high` but inside the deadband: hysteresis idles, and with no
    # warming slope to project along, so does the predictor.
    hass.states.async_set(TEMP_ENTITY, "22.8", {})
    await _settle(hass, freezer, timedelta(seconds=61))
    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    assert coordinator.data.decision.action == ACTION_IDLE


async def test_a_sensor_slow_to_publish_after_a_restart_keeps_the_restored_model(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A Thread or Matter sensor after a power cut, or a deep-sleep ESPHome
    node, can take longer than the grace period to publish after a restart,
    so on such a zone the stand-in engages on nearly every boot. It finds the
    room inside the band and holds the idle the zone was restored in: no
    cycle was commanded, so the gap joins nothing that was not one run
    already, and the buffer restored from disk has to survive -- flushing it
    here would wipe the learned model on every restart, the bug class
    test_a_restart_mid_blip_does_not_flush_the_restored_model guards."""
    from custom_components.comfort_band import predictor

    freezer.move_to("2026-09-15 08:40:00+00:00")
    coordinator = await _enabled_zone(hass)
    restored = [
        {"t": "2026-09-15T08:20:00+00:00", "temp": 21.0, "action": ACTION_IDLE},
        {"t": "2026-09-15T08:25:00+00:00", "temp": 20.9, "action": ACTION_IDLE},
        {"t": "2026-09-15T08:30:00+00:00", "temp": 20.8, "action": ACTION_IDLE},
    ]
    await coordinator._store.async_update_zone(
        "office",
        samples=restored,
        last_action=ACTION_IDLE,
        last_action_at="2026-09-15T08:00:00+00:00",
    )
    coordinator._samples_cache = predictor.load_samples(restored)
    # The unit is up; the room sensor has not published yet.
    _set_climate(hass, 21.0, HVAC_MODE_FAN_ONLY)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data.sensor_available is False
    assert coordinator.data.decision.action == ACTION_UNKNOWN

    # The grace period ends first: the stand-in engages, idles, and holds.
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.decision.action == ACTION_IDLE
    _set_climate(hass, 21.2, HVAC_MODE_FAN_ONLY)
    await _settle(hass, freezer, timedelta(minutes=2))
    assert coordinator.data.decision.action == ACTION_IDLE
    assert [s.temp for s in coordinator._samples_cache] == [21.0, 20.9, 20.8]
    assert coordinator.get_zone_data()["samples"] == restored

    # The sensor publishes: sampling carries on from where the restart left it.
    hass.states.async_set(TEMP_ENTITY, "21.1", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    assert [s.temp for s in coordinator._samples_cache] == [21.0, 20.9, 20.8, 21.1]


async def test_a_stand_in_that_changes_the_action_flushes_exactly_once(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The flush keys on the committed action differing from the one in force
    at engagement, and that stays true for every refresh of a heat cycle --
    and again for a second cycle after the stand-in has released back to the
    idle it started from. One outage is one gap, so one flush."""
    coordinator = await _zone_on_the_climate_reading(hass, freezer, climate_calls)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        # A heat cycle, released; then a second one, released. The unit
        # reports the mode it was last commanded, as a real one would.
        for reading, reported_mode, expected in (
            (16.0, HVAC_MODE_HEAT, ACTION_HEAT),
            (20.0, HVAC_MODE_HEAT, ACTION_IDLE),
            (17.0, HVAC_MODE_FAN_ONLY, ACTION_HEAT),
            (20.0, HVAC_MODE_HEAT, ACTION_IDLE),
        ):
            _set_climate(hass, reading, reported_mode)
            await _settle(hass, freezer, timedelta(minutes=9))
            assert coordinator.data.decision.action == expected, reading
            assert coordinator._samples_cache == []
        hass.states.async_set(TEMP_ENTITY, "21.0", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)

    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
    assert len([m for m in messages if "sample buffer flushed" in m]) == 1, messages
    assert [s.temp for s in coordinator._samples_cache] == [21.0]


async def test_the_persisted_idle_slope_stamp_does_not_advance_on_a_stand_in(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the buffer kept through a stand-in that idles, the live idle slope
    is still the pre-outage one -- nothing is measured on a stand-in. The
    stamp has to say when the value was measured, so re-persisting it on
    every stand-in refresh would carry it past its expiry on the strength
    of nothing."""
    freezer.move_to("2026-09-15 08:40:00+00:00")
    coordinator = await _enabled_zone(hass)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    _set_climate(hass, 20.0, HVAC_MODE_FAN_ONLY)
    for i in range(IDLE_SAMPLES):
        hass.states.async_set(TEMP_ENTITY, "20.0" if i % 2 == 0 else "20.02", {})
        await _settle(hass, freezer, timedelta(seconds=61))
    assert coordinator.data.idle_slope_source == "live"

    # The sensor dies with the unit reading inside the band: the stand-in
    # will idle, so the buffer is kept and the live slope stays computable.
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    persisted_at_before = coordinator.get_zone_data()["persisted_idle_slope_at"]
    assert persisted_at_before is not None

    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.decision.action == ACTION_IDLE
    assert len(coordinator._samples_cache) == IDLE_SAMPLES
    # Each refresh is past the persist throttle, so only the guard holds it.
    for reading in (20.2, 20.4, 20.6):
        _set_climate(hass, reading, HVAC_MODE_FAN_ONLY)
        await _settle(hass, freezer, timedelta(minutes=6))
        assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
        assert coordinator.data.idle_slope_source == "live"
        assert coordinator.get_zone_data()["persisted_idle_slope_at"] == persisted_at_before


async def test_a_learning_zone_runs_plain_hysteresis_on_a_stand_in(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The predictor's anticipation extrapolates the current reading along
    slopes learned from a different sensor, so it is bypassed on a stand-in.
    Stubbed so the predictor visibly disagrees with hysteresis."""
    from custom_components.comfort_band import coordinator as coordinator_module

    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    await coordinator._store.async_update_zone("office", learning_enabled=True)
    predictor_says_idle = HysteresisDecision(
        action=ACTION_IDLE, target_mode=HVAC_MODE_FAN_ONLY, target_temp=None
    )
    monkeypatch.setattr(
        coordinator_module.predictor, "decide", lambda *args, **kwargs: predictor_says_idle
    )

    # From the room sensor, learning is in charge: 15 °C, yet idle.
    hass.states.async_set(TEMP_ENTITY, "15.0", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.decision.action == ACTION_IDLE

    # On the stand-in, the same 15 °C heats.
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    assert coordinator.data.predicted_decision.action == ACTION_IDLE, "shadow value still shown"
    assert coordinator.data.decision.action == ACTION_HEAT


# ---------------------------------------------------------------------------
# Handing back.
# ---------------------------------------------------------------------------


async def test_the_configured_sensor_takes_back_over_immediately(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await _zone_on_the_climate_reading(hass, freezer, climate_calls)

    hass.states.async_set(TEMP_ENTITY, "22.0", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)

    assert coordinator.data.sensor_available is True
    assert coordinator.data.room == 22.0
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    assert coordinator.data.decision.action == ACTION_IDLE
    # The engage refresh committed heat and this hand-back refresh is the
    # first to see it, so it is also the last chance to flush before the
    # apply task it spawns appends the first post-hand-back sample.
    assert [s.temp for s in coordinator._samples_cache] == [22.0]


async def test_a_second_outage_waits_out_the_grace_period_again(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The clock restarts on every recovery, so a sensor that flaps never
    swaps sources at each flap."""
    coordinator = await _zone_on_the_climate_reading(hass, freezer, climate_calls)
    hass.states.async_set(TEMP_ENTITY, "22.0", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY

    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    await _settle(hass, freezer, timedelta(seconds=FALLBACK_GRACE_S / 2))
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE


async def test_fallback_sensor_changes_are_ignored_while_the_configured_sensor_reports(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A healthy zone must not gain refreshes -- and samples -- from a sensor
    that is not driving it."""
    coordinator = await _healthy_idle_zone(
        hass, freezer, climate_calls, fallback_temp_entity_id=FALLBACK_ENTITY
    )
    # The helper's own state-set armed a debounce; let it land before counting.
    await _settle(hass, freezer, PAST_DEBOUNCE)
    refreshes = 0
    original = coordinator._async_update_data

    async def counting() -> Any:
        nonlocal refreshes
        refreshes += 1
        return await original()

    coordinator._async_update_data = counting  # type: ignore[method-assign]

    for reading in ("18.0", "18.5", "19.0"):
        hass.states.async_set(FALLBACK_ENTITY, reading, {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 0

    hass.states.async_set(TEMP_ENTITY, "20.5", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 1


async def test_the_climate_reading_is_ignored_while_the_configured_sensor_reports(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The climate entity's reading is the most frequent event in the system,
    and every refresh on a healthy zone is a sample. The one gate in the
    climate listener is all that keeps 'no new refreshes, no new samples'
    true for zones that never lose their sensor."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    # The helper's own state-set armed a debounce; let it land before counting.
    await _settle(hass, freezer, PAST_DEBOUNCE)
    samples_before = list(coordinator._samples_cache)
    refreshes = 0
    original = coordinator._async_update_data

    async def counting() -> Any:
        nonlocal refreshes
        refreshes += 1
        return await original()

    coordinator._async_update_data = counting  # type: ignore[method-assign]

    for reading in (20.0, 19.0, 18.0):
        _set_climate(hass, reading, HVAC_MODE_FAN_ONLY)
        await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 0
    assert coordinator._samples_cache == samples_before

    hass.states.async_set(TEMP_ENTITY, "20.5", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert refreshes == 1


# ---------------------------------------------------------------------------
# What the log says.
# ---------------------------------------------------------------------------


async def test_the_hand_over_and_the_hand_back_are_each_logged_once(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)
        for reading in (15.5, 16.0, 16.5):
            _set_climate(hass, reading, HVAC_MODE_HEAT)
            await _settle(hass, freezer, timedelta(minutes=1))
        hass.states.async_set(TEMP_ENTITY, "21.0", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)

    messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
    outage = [m for m in messages if "is unavailable" in m]
    handover = [m for m in messages if "controlling from" in m]
    handback = [m for m in messages if "stand-in climate released" in m]
    assert len(outage) == 1 and "stand-in reading takes over in" in outage[0], outage
    assert len(handover) == 1, handover
    assert TEMP_ENTITY in handover[0] and CLIMATE_ENTITY in handover[0]
    assert len(handback) == 1, handback
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY


async def _blip_then_die(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    climate_calls: list[tuple[str, dict[str, Any]]],
    climate_reading: float | None,
) -> ZoneCoordinator:
    """The blip spends the drop-direction log budget; the real outage that
    follows inside the throttle window is withheld, and re-offered by whatever
    refresh comes next."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    _set_climate(hass, climate_reading, HVAC_MODE_FAN_ONLY)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    return coordinator


def _outage_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.message
        for r in caplog.records
        if r.name.endswith("comfort_band")
        and r.message.startswith(f"office: room sensor {TEMP_ENTITY} is unavailable")
    ]


def _grace_over_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name.endswith("comfort_band") and "grace period over" in r.message
    ]


async def test_a_withheld_outage_line_says_how_long_is_actually_left(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator = await _blip_then_die(hass, freezer, climate_calls, 15.0)
        # Budget back, grace timer not yet due: a refresh of any other kind
        # re-offers the edge 20 s short of the boundary.
        await _settle(hass, freezer, timedelta(seconds=FALLBACK_GRACE_S - 20))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    outage = _outage_lines(caplog)
    assert len(outage) == 2, outage
    assert f"takes over in {FALLBACK_GRACE_S} s" in outage[0]
    assert "takes over in 20 s" in outage[1]


async def test_a_withheld_outage_line_re_offered_at_the_boundary_does_not_promise_a_wait(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """For a zone with no schedule the next refresh is the grace timer's --
    the very one that engages the stand-in -- so the re-offered line must not
    promise a hand-over in 300 s directly above the hand-over itself."""
    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator = await _blip_then_die(hass, freezer, climate_calls, 15.0)
        await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
    messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
    outage = _outage_lines(caplog)
    assert len(outage) == 2, outage
    assert "takes over in" not in outage[1] and "is taking over" in outage[1], outage
    handover = [m for m in messages if "controlling from" in m]
    assert len(handover) == 1, handover
    assert messages.index(outage[1]) < messages.index(handover[0])


async def test_a_withheld_outage_line_past_the_boundary_with_nothing_to_read(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stand-in is allowed but has no reading: no count-down to promise,
    and no hand-over to announce."""
    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator = await _blip_then_die(hass, freezer, climate_calls, None)
        await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    outage = _outage_lines(caplog)
    assert len(outage) == 2, outage
    assert "takes over in" not in outage[1] and "becomes available" in outage[1], outage
    # That line already says no stand-in has a reading; the grace-period line
    # is not put directly underneath it saying the same thing.
    assert _grace_over_lines(caplog) == []


async def test_a_grace_period_that_ends_with_nothing_to_read_is_said_once_per_outage(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The outage line promised a stand-in in N s. A unit that publishes no
    `current_temperature` -- the climate entity behind an IR blaster, say --
    gives the boundary refresh nothing to engage, and it used to log nothing
    at all: the record's last word was a hand-over that never happened,
    followed by hours of no control."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    _set_climate(hass, None, HVAC_MODE_FAN_ONLY)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        outage = _outage_lines(caplog)
        assert len(outage) == 1 and "takes over in" in outage[0], outage
        assert _grace_over_lines(caplog) == []

        # No state change of any kind -- only the clock moves.
        await _settle(hass, freezer, PAST_GRACE)
        assert coordinator.data.room_source == ROOM_SOURCE_NONE
        assert coordinator.data.decision.action == ACTION_UNKNOWN
        missing = _grace_over_lines(caplog)
        assert len(missing) == 1, [r.message for r in missing]
        assert missing[0].levelno == logging.WARNING
        assert f"no stand-in has a reading ({CLIMATE_ENTITY} current_temperature)" in (
            missing[0].message
        )
        assert missing[0].message.endswith(f"until it or room sensor {TEMP_ENTITY} reports")

        # Later refreshes in the same outage do not repeat it. Nothing wakes
        # this zone on its own any more, so one is forced.
        await _settle(hass, freezer, timedelta(minutes=10))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert len(_grace_over_lines(caplog)) == 1

        # The sensor returns. No stand-in engaged, so there is none to release.
        hass.states.async_set(TEMP_ENTITY, "21.0", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
        messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
        assert [m for m in messages if "released" in m] == []

        # A second outage that outlasts the grace period says it again.
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)
    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    assert len(_grace_over_lines(caplog)) == 2


@pytest.mark.parametrize(
    ("fallback_to_climate", "climate_reading", "tried"),
    [
        (False, 21.0, FALLBACK_ENTITY),
        (True, None, f"{FALLBACK_ENTITY}, {CLIMATE_ENTITY} current_temperature"),
    ],
    ids=["climate-reading-switched-off", "both-tried"],
)
async def test_the_grace_period_line_names_what_was_tried(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    fallback_to_climate: bool,
    climate_reading: float | None,
    tried: str,
) -> None:
    """A reading the zone is not allowed to use is not one it tried."""
    coordinator = await _healthy_idle_zone(
        hass,
        freezer,
        climate_calls,
        fallback_temp_entity_id=FALLBACK_ENTITY,
        fallback_to_climate=fallback_to_climate,
    )
    hass.states.async_set(FALLBACK_ENTITY, "unavailable", {})
    _set_climate(hass, climate_reading, HVAC_MODE_FAN_ONLY)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    missing = _grace_over_lines(caplog)
    assert len(missing) == 1, [r.message for r in missing]
    assert f"no stand-in has a reading ({tried});" in missing[0].message


async def test_the_grace_period_line_is_info_in_shadow_mode(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    await coordinator._store.async_update_zone("office", enabled=False)
    _set_climate(hass, None, HVAC_MODE_FAN_ONLY)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)

    assert coordinator.data.room_source == ROOM_SOURCE_NONE
    missing = _grace_over_lines(caplog)
    assert len(missing) == 1, [r.message for r in missing]
    assert missing[0].levelno == logging.INFO
    assert missing[0].message.endswith("reports (zone is in shadow mode)")


async def test_a_stand_in_that_turns_up_after_the_grace_period_line_is_handed_over_to(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The grace-period line is about an outage in which no stand-in engaged.
    Once one does, its story is told by the hand-over and stand-in-lost lines,
    and the grace-period line stays said once."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    _set_climate(hass, None, HVAC_MODE_FAN_ONLY)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)
        assert len(_grace_over_lines(caplog)) == 1

        # The unit starts publishing its reading: the ordinary hand-over.
        _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
        await _settle(hass, freezer, PAST_DEBOUNCE)
        assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
        assert coordinator.data.decision.action == ACTION_HEAT

        # ...and when it goes dark again, that is the stand-in's own line.
        hass.states.async_set(CLIMATE_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        assert coordinator.data.room_source == ROOM_SOURCE_NONE

    messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
    assert len([m for m in messages if "controlling from" in m]) == 1, messages
    assert len([m for m in messages if "unavailable too" in m]) == 1, messages
    assert len(_grace_over_lines(caplog)) == 1, messages


async def test_the_grace_period_line_waits_for_home_assistant_to_start(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The availability edge is withheld while Home Assistant is starting,
    because the sensor's own integration may not have published yet -- and
    the climate entity's may not have either, so this line waits with it
    rather than name a reading that is merely late."""
    from homeassistant.core import CoreState

    freezer.move_to("2026-09-15 08:40:00+00:00")
    coordinator = await _enabled_zone(hass)
    _set_climate(hass, None, HVAC_MODE_FAN_ONLY)
    original = hass.state
    hass.set_state(CoreState.starting)
    caplog.clear()
    try:
        with caplog.at_level(logging.INFO):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            await _settle(hass, freezer, PAST_GRACE)
            assert coordinator.data.room_source == ROOM_SOURCE_NONE
            assert _outage_lines(caplog) == []
            assert _grace_over_lines(caplog) == []

            # Once up, the first refresh re-offers the withheld outage line,
            # which already says no stand-in has a reading; the next refresh
            # is the one that says what was tried.
            hass.set_state(CoreState.running)
            await _settle(hass, freezer, timedelta(minutes=1))
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            outage = _outage_lines(caplog)
            assert len(outage) == 1 and "becomes available" in outage[0], outage
            assert _grace_over_lines(caplog) == []
            await _settle(hass, freezer, timedelta(minutes=1))
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert len(_grace_over_lines(caplog)) == 1
    finally:
        hass.set_state(original)


async def test_a_stand_in_that_goes_dark_is_announced_and_still_released(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stand-in was the only thing keeping the zone going, so losing it is
    a return to the silent night and is said at the same level as the outage.
    When the room sensor finally returns, the stand-in's story is closed the
    same way as if it had lasted -- the latch alone would skip that line,
    because from its point of view nothing was standing in any more."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)
        assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
        hass.states.async_set(CLIMATE_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        assert coordinator.data.room_source == ROOM_SOURCE_NONE
        assert coordinator.data.decision.action == ACTION_UNKNOWN
        hass.states.async_set(TEMP_ENTITY, "21.0", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)

    records = [r for r in caplog.records if r.name.endswith("comfort_band")]
    lost = [r for r in records if "unavailable too" in r.message]
    assert len(lost) == 1, [r.message for r in lost]
    assert lost[0].levelno == logging.WARNING
    assert CLIMATE_ENTITY in lost[0].message and TEMP_ENTITY in lost[0].message
    # A stand-in did engage, so the grace period did not end with nothing to
    # read: that line is for an outage in which none ever did.
    assert _grace_over_lines(caplog) == []
    handback = [r.message for r in records if "stand-in climate released" in r.message]
    assert len(handback) == 1, handback
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY


async def test_a_stand_in_that_flaps_is_throttled_like_the_sensor_edge(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A climate entity blinking unavailable while the room sensor is dark
    refreshes the zone on every blink -- its reading flips between a value
    and None -- and every blink is a hand-over edge. Unthrottled, that is one
    WARNING per blink for as long as it lasts. The bound is the availability
    edge's: one line per direction per interval, and a throttled edge is
    re-offered by the next refresh once the budget returns rather than lost."""
    coordinator = await _healthy_idle_zone(hass, freezer, climate_calls)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
        hass.states.async_set(TEMP_ENTITY, "unavailable", {})
        await _settle(hass, freezer, PAST_DEBOUNCE)
        await _settle(hass, freezer, PAST_GRACE)
        assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
        for _ in range(6):
            hass.states.async_set(CLIMATE_ENTITY, "unavailable", {})
            await _settle(hass, freezer, PAST_DEBOUNCE)
            assert coordinator.data.room_source == ROOM_SOURCE_NONE
            _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
            await _settle(hass, freezer, PAST_DEBOUNCE)
            assert coordinator.data.room_source == ROOM_SOURCE_CLIMATE
        messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
        assert len([m for m in messages if "controlling from" in m]) == 1, messages
        assert len([m for m in messages if "unavailable too" in m]) == 1, messages

        # The re-engagement the throttle withheld is still true, so once the
        # budget is back the next refresh -- a reading change, not a new edge
        # -- is what says so.
        await _settle(hass, freezer, timedelta(seconds=SENSOR_EDGE_LOG_INTERVAL_S))
        _set_climate(hass, 16.0, HVAC_MODE_FAN_ONLY)
        await _settle(hass, freezer, PAST_DEBOUNCE)

    messages = [r.message for r in caplog.records if r.name.endswith("comfort_band")]
    assert len([m for m in messages if "controlling from" in m]) == 2, messages
    assert len([m for m in messages if "unavailable too" in m]) == 1, messages


# ---------------------------------------------------------------------------
# Wiring: options flow -> coordinator -> entity attributes.
# ---------------------------------------------------------------------------


def _zone_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="zone:office",
        title="Comfort Band: office",
        data={
            CONF_KIND: ENTRY_KIND_ZONE,
            CONF_ZONE_NAME: "office",
            CONF_CLIMATE_ENTITY: CLIMATE_ENTITY,
            CONF_TEMP_SENSOR: TEMP_ENTITY,
        },
        options=options or {},
    )


async def test_the_climate_reading_is_allowed_by_default_and_options_override_it(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    assert coordinator.fallback_temp_entity_id is None
    assert coordinator.fallback_to_climate is True

    hass.config_entries.async_update_entry(
        entry,
        options={CONF_FALLBACK_TEMP_SENSOR: FALLBACK_ENTITY, CONF_FALLBACK_TO_CLIMATE: False},
    )
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    assert coordinator.fallback_temp_entity_id == FALLBACK_ENTITY
    assert coordinator.fallback_to_climate is False


async def test_the_options_flow_persists_and_clears_the_fallback_fields(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert init_result["type"] == FlowResultType.FORM
    schema_keys = {str(key) for key in init_result["data_schema"].schema}
    assert {CONF_FALLBACK_TEMP_SENSOR, CONF_FALLBACK_TO_CLIMATE} <= schema_keys

    result = await hass.config_entries.options.async_configure(
        init_result["flow_id"],
        {
            CONF_TEMP_SENSOR: TEMP_ENTITY,
            CONF_FALLBACK_TEMP_SENSOR: FALLBACK_ENTITY,
            CONF_FALLBACK_TO_CLIMATE: False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_FALLBACK_TEMP_SENSOR] == FALLBACK_ENTITY
    assert entry.options[CONF_FALLBACK_TO_CLIMATE] is False

    # Emptying the selector must clear the sensor, not fall through to the
    # previous value -- same normalisation the humidity field relies on.
    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        init_result["flow_id"],
        {CONF_TEMP_SENSOR: TEMP_ENTITY, CONF_FALLBACK_TO_CLIMATE: True},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_FALLBACK_TEMP_SENSOR] is None
    assert entry.options[CONF_FALLBACK_TO_CLIMATE] is True


async def test_the_options_flow_refuses_the_room_sensor_as_its_own_fallback(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Both fields share a selector, so the form must be the thing that says
    no. A refused submit persists nothing -- not the options, and not the
    sample flush a sensor swap would otherwise have done -- and the form
    stays open for a corrected submit."""
    from homeassistant.data_entry_flow import FlowResultType

    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = hass.data[DOMAIN].store
    await store.async_update_zone(
        "office",
        samples=[{"t": "2026-09-15T08:40:00+00:00", "room": 21.0, "action": ACTION_IDLE}],
    )

    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    # A swap to a new sensor *and* naming it as its own fallback: the swap
    # alone would flush the buffer, so this is where a refusal that ran the
    # flush first would show.
    result = await hass.config_entries.options.async_configure(
        init_result["flow_id"],
        {CONF_TEMP_SENSOR: FALLBACK_ENTITY, CONF_FALLBACK_TEMP_SENSOR: FALLBACK_ENTITY},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_FALLBACK_TEMP_SENSOR: "fallback_same_as_temp_sensor"}
    await hass.async_block_till_done()
    assert entry.options == {}
    assert len(hass.data[DOMAIN].store.get_zone("office")["samples"]) == 1

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TEMP_SENSOR: TEMP_ENTITY, CONF_FALLBACK_TEMP_SENSOR: FALLBACK_ENTITY},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_FALLBACK_TEMP_SENSOR] == FALLBACK_ENTITY


async def test_a_sensor_cannot_stand_in_for_itself(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The listener tells the two sensors apart by entity_id, so a fallback
    that *is* the room sensor would have every one of its changes filed as a
    fallback-sensor change and dropped while it is healthy: a zone that never
    refreshes, never commands, and says nothing. The coordinator refuses the
    fallback instead, whatever path configured it."""
    with caplog.at_level(logging.WARNING):
        coordinator = await _healthy_idle_zone(
            hass, freezer, climate_calls, fallback_temp_entity_id=TEMP_ENTITY
        )
    assert coordinator.fallback_temp_entity_id is None
    refused = [
        r.message
        for r in caplog.records
        if r.name.endswith("comfort_band") and "cannot stand in for itself" in r.message
    ]
    assert len(refused) == 1 and TEMP_ENTITY in refused[0], refused

    # The room sensor's own changes still drive the zone.
    hass.states.async_set(TEMP_ENTITY, "15.0", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    assert coordinator.data.room == 15.0
    assert coordinator.data.room_source == ROOM_SOURCE_PRIMARY
    assert coordinator.data.decision.action == ACTION_HEAT
    assert HVAC_MODE_HEAT in _modes(climate_calls)


async def test_changing_the_fallback_fields_does_not_flush_samples(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Nothing is ever sampled from a stand-in, so there is no mixing to avoid."""
    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = hass.data[DOMAIN].store
    await store.async_update_zone(
        "office",
        samples=[{"t": "2026-09-15T08:40:00+00:00", "room": 21.0, "action": ACTION_IDLE}],
    )

    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        init_result["flow_id"],
        {CONF_TEMP_SENSOR: TEMP_ENTITY, CONF_FALLBACK_TEMP_SENSOR: FALLBACK_ENTITY},
    )
    await hass.async_block_till_done()
    assert len(hass.data[DOMAIN].store.get_zone("office")["samples"]) == 1


async def test_the_room_temperature_sensor_names_its_source(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-09-15 08:40:00+00:00")
    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    _set_climate(hass, 15.0, HVAC_MODE_FAN_ONLY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.office_room_temperature")
    assert state is not None
    assert state.state == "21.0"
    assert state.attributes["source"] == ROOM_SOURCE_PRIMARY
    assert state.attributes["fallback_temp_sensor"] is None
    assert state.attributes["fallback_to_climate"] is True

    hass.states.async_set(TEMP_ENTITY, "unavailable", {})
    await _settle(hass, freezer, PAST_DEBOUNCE)
    await _settle(hass, freezer, PAST_GRACE)

    state = hass.states.get("sensor.office_room_temperature")
    assert state is not None
    assert state.state == "15.0"
    assert state.attributes["source"] == ROOM_SOURCE_CLIMATE
    # The problem signal is about the configured sensor, and it is still dark.
    problem = hass.states.get("binary_sensor.office_room_sensor_unavailable")
    assert problem is not None and problem.state == "on"
