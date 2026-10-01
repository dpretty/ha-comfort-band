"""v0.19.0: the idle slope is learned from a settled room, not from the
aftermath of the cycle before it -- and is stamped with when it was measured.

The incident: a zone on apparent temperature cycled cool -> fan_only all
evening. Each fan_only stretch began with the fan blowing across a coil still
wet from the cool cycle, and the humidity that put back read, in apparent
temperature, as the room warming at 1.5-3 °C/h while the raw temperature sat
flat. Learned as passive drift, it had the predictor start the next cool cycle
early and MPC hold cooling inside the band -- which dried the air again for the
next stretch to rebound from. One such value was then carried through an hour
in which the unit was unreachable, re-stamped as current the whole time, and
handed to MPC as the cache "10 min old": it cooled the room on a 5 °C night.

Every test here drives an enabled zone through the real listeners, because
what is under test is what the zone learns from the samples it takes itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_IDLE,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    SAMPLE_WINDOW_MINUTES,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"
HUMIDITY_ENTITY = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_hvac"

# How often the room sensor reports in these tests: the cadence of the
# battery sensors the incident involved.
REPORT = timedelta(minutes=5)

_COORDINATORS: list[ZoneCoordinator] = []


@pytest.fixture(autouse=True)
async def _unload_coordinators(hass: HomeAssistant) -> Any:
    """Same contract as the other event-driven modules: a coordinator left
    standing holds a pending timer that HA's lingering-timer check fails."""
    _COORDINATORS.clear()
    yield
    for coordinator in _COORDINATORS:
        await coordinator.async_unload()
        await coordinator.async_shutdown()
    _COORDINATORS.clear()


async def _enabled_zone(
    hass: HomeAssistant, *, humidity_entity_id: str | None = None, **zone: Any
) -> ZoneCoordinator:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", enabled=True, learning_enabled=True, **zone)
    coordinator = ZoneCoordinator(
        hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY, humidity_entity_id=humidity_entity_id
    )
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    return coordinator


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Drain pending dispatch (which arms the debounce), then advance the
    clock and run every timer that came due -- see the fallback module."""
    await hass.async_block_till_done()
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _report(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    temp: float,
    humidity: float | None = None,
) -> None:
    """The room sensor reports, and the zone refreshes on it."""
    hass.states.async_set(TEMP_ENTITY, f"{temp:.2f}", {})
    if humidity is not None:
        hass.states.async_set(HUMIDITY_ENTITY, f"{humidity:.2f}", {})
    await _settle(hass, freezer, REPORT)


def _set_climate(hass: HomeAssistant, mode: str, temperature: float | None = None) -> None:
    attrs: dict[str, Any] = {}
    if temperature is not None:
        attrs["temperature"] = temperature
    hass.states.async_set(CLIMATE_ENTITY, mode, attrs)


def _modes(climate_calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [data["hvac_mode"] for srv, data in climate_calls if srv == "set_hvac_mode"]


async def test_a_cool_cycles_aftermath_is_not_learned_as_passive_warming(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The incident's evening, one cycle of it. The raw temperature does not
    move once the unit is released; the apparent temperature climbs 0.9 °C in
    fifteen minutes on humidity alone. Fitted as passive drift that was +2.8
    °C/h: live, persisted, and enough for the predictor to start the next cool
    cycle with the room inside its deadband. It is the cycle's, not the
    room's, so none of it is learned -- and once the room has settled, what
    it is really doing is."""
    freezer.move_to("2026-09-24 10:15:00+00:00")
    coordinator = await _enabled_zone(
        hass,
        humidity_entity_id=HUMIDITY_ENTITY,
        use_apparent_temperature=True,
        mpc_enabled=True,
        manual_low=19.0,
        manual_high=20.8,
    )

    # Warm and humid: apparent 21.7, past the cool entry at 20.8 + 0.5.
    await _report(hass, freezer, 20.6, 64.0)
    assert coordinator.data.decision.action == ACTION_COOL
    # Five minutes of cooling dries the air: apparent 20.4, released.
    await _report(hass, freezer, 20.1, 55.0)
    assert coordinator.data.decision.action == ACTION_IDLE
    assert _modes(climate_calls) == [HVAC_MODE_COOL, HVAC_MODE_FAN_ONLY]

    # fan_only across the wet coil: the room holds at 20.1 while the humidity
    # climbs back.
    for humidity in (58.0, 61.0, 64.0, 66.5):
        await _report(hass, freezer, 20.1, humidity)
    assert coordinator.data.room == 20.1
    assert coordinator.data.decision_room == pytest.approx(21.25, abs=0.01)

    # Inside the deadband, and nothing learned to anticipate from: the whole
    # stretch is inside the settle window.
    assert coordinator.data.thermal_slopes.idle is None
    assert coordinator.data.thermal_slopes.sample_count_idle == 0
    assert coordinator.data.idle_slope_source == "none"
    assert coordinator.get_zone_data()["persisted_idle_slope"] is None
    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    assert coordinator.data.decision.action == ACTION_IDLE
    assert _modes(climate_calls).count(HVAC_MODE_COOL) == 1

    # The humidity levels off and the room cools slowly, as a room does on a
    # cold night. Past the settle window that is what the zone learns.
    for k in range(1, 7):
        await _report(hass, freezer, 20.1 - 0.2 / 12 * k, 66.5)
    slopes = coordinator.data.thermal_slopes
    assert coordinator.data.idle_slope_source == "live"
    assert slopes.idle is not None
    assert -0.5 < slopes.idle * 60.0 < 0.0
    assert coordinator.get_zone_data()["persisted_idle_slope"] == slopes.idle
    assert coordinator.data.decision.action == ACTION_IDLE
    assert _modes(climate_calls).count(HVAC_MODE_COOL) == 1


async def test_mpc_does_not_cool_a_room_inside_its_band_on_a_cycles_aftermath(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The MPC half of the incident. A zone with a settled idle slope in its
    cache (a slow overnight fall) cools, long enough for MPC to have a
    recovery slope, and is released into the band. The aftermath then climbs
    on humidity alone. Fitted as the live idle slope that is +1.9 °C/h, and
    MPC -- projecting the room out of the top of the band within the minute
    -- cools a room that is inside it. It is not the room's drift, so MPC
    plans with the one it has, and leaves the room alone."""
    freezer.move_to("2026-09-24 11:00:00+00:00")
    coordinator = await _enabled_zone(
        hass,
        humidity_entity_id=HUMIDITY_ENTITY,
        use_apparent_temperature=True,
        mpc_enabled=True,
        manual_low=19.0,
        manual_high=21.0,
    )
    overnight = -0.15 / 60
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=overnight,
        persisted_idle_slope_at=(dt_util.utcnow() - timedelta(minutes=20)).isoformat(),
    )

    # Apparent 21.7 -> 21.1 over twenty minutes of cooling: the fifth report
    # is the first refresh with a cool run long enough for a recovery slope.
    for temp, humidity in ((20.5, 65.0), (20.45, 64.0), (20.4, 63.0), (20.35, 62.0), (20.3, 61.0)):
        await _report(hass, freezer, temp, humidity)
        assert coordinator.data.decision.action == ACTION_COOL
    assert coordinator.data.mpc_ready is True
    # Dried to apparent 20.4 and released, by MPC as much as by hysteresis.
    await _report(hass, freezer, 20.1, 56.0)
    assert coordinator.data.mpc_decision.action == ACTION_IDLE
    assert coordinator.data.decision.action == ACTION_IDLE
    released = len(climate_calls)

    # The aftermath: the room holds at 20.1 while the humidity climbs back,
    # to apparent 20.98 -- still inside the band.
    for humidity in (58.0, 60.0, 62.0, 63.0):
        await _report(hass, freezer, 20.1, humidity)
    assert coordinator.data.decision_room == pytest.approx(20.98, abs=0.01)
    assert coordinator.data.decision_room < 21.0

    assert coordinator.data.mpc_ready is True
    assert coordinator.data.idle_slope_source == "cached"
    assert coordinator.data.thermal_slopes.idle == overnight
    assert coordinator.data.mpc_decision.action == ACTION_IDLE
    assert coordinator.data.decision.action == ACTION_IDLE
    assert HVAC_MODE_COOL not in _modes(climate_calls[released:])


async def test_an_unreachable_unit_does_not_make_an_old_idle_slope_look_new(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The incident's outage. While the unit is unreachable every command is
    dropped and nothing is sampled, so the buffer stops growing and nothing
    ages out of it: the idle run from before the cool cycle stays in it and
    stays "live". It used to be written back with each refresh's time for the
    whole hour, and when the unit returned and its first sample pruned the
    run, the cache took over claiming to be minutes old. The stamp is when
    the drift was measured, so it does not move -- and the cache says so."""
    freezer.move_to("2026-09-24 08:30:00+00:00")
    coordinator = await _enabled_zone(hass, mpc_enabled=True)
    _set_climate(hass, HVAC_MODE_FAN_ONLY, 22.5)

    # Fifty minutes of idle, drifting slowly down: a settled idle slope.
    for k in range(11):
        await _report(hass, freezer, 21.0 - 0.02 * k)
    assert coordinator.data.idle_slope_source == "live"

    # The room warms past the band and the unit cools. The refresh that starts
    # it is the last to see a new idle sample: from here the idle run is
    # complete, and what is stored is its final measurement.
    await _report(hass, freezer, 23.2)
    assert coordinator.data.decision.action == ACTION_COOL
    _set_climate(hass, HVAC_MODE_COOL, 22.5)
    last_idle = [s for s in coordinator._samples_cache if s.action == ACTION_IDLE][-1]
    zone = coordinator.get_zone_data()
    measured = (zone["persisted_idle_slope"], zone["persisted_idle_slope_at"])
    assert measured[0] is not None
    assert measured[1] == last_idle.t.isoformat()

    # The idle run is still in the window, so its slope is still live -- but
    # nothing new about idle drift is being measured.
    for temp in (23.0, 22.9, 22.8):
        await _report(hass, freezer, temp)
        assert coordinator.data.idle_slope_source == "live"
        zone = coordinator.get_zone_data()
        assert (zone["persisted_idle_slope"], zone["persisted_idle_slope_at"]) == measured

    # The network drops the unit for an hour and a quarter; the room sensor
    # keeps reporting. Every command is dropped, nothing is sampled.
    _set_climate(hass, STATE_UNAVAILABLE)
    frozen = list(coordinator._samples_cache)
    with patch.object(
        coordinator._store, "async_update_zone", wraps=coordinator._store.async_update_zone
    ) as writes:
        for k in range(15):
            # The last digit alternates so every report is a state change.
            await _report(hass, freezer, 22.8 + 0.02 * (k % 2))
            assert coordinator._samples_cache == frozen
            assert coordinator.data.idle_slope_source == "live"
    assert not [c for c in writes.call_args_list if "persisted_idle_slope_at" in c.kwargs]
    zone = coordinator.get_zone_data()
    assert (zone["persisted_idle_slope"], zone["persisted_idle_slope_at"]) == measured

    # The unit is back. Its first sample prunes the idle run, which is now
    # older than the window, and the cache takes over -- saying how old the
    # measurement really is, not how recently it was last written.
    _set_climate(hass, HVAC_MODE_COOL, 22.5)
    await _report(hass, freezer, 22.9)
    assert not [s for s in coordinator._samples_cache if s.action == ACTION_IDLE]
    await _report(hass, freezer, 22.7)
    assert coordinator.data.idle_slope_source == "cached"
    assert coordinator.data.thermal_slopes.idle == measured[0]
    real_age = (dt_util.utcnow() - last_idle.t).total_seconds() / 60.0
    assert real_age > SAMPLE_WINDOW_MINUTES
    assert coordinator.data.idle_slope_cached_age_min == pytest.approx(real_age, abs=0.1)
