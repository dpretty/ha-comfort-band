"""v0.20.0: a slope is not fitted across an outage.

The sample buffer only grows while the zone can see what it is learning: the
room sensor reporting, the climate entity reachable (v0.17.0 appends nothing
for a dropped command), Home Assistant running. When any of those stops, the
buffer simply has a hole in it -- and the predictor joined the runs either
side of the hole by action label alone, so a slope could be fitted across an
hour in which nothing was seen. In production on 24 Sep 2026 a cool run from
10:45 to 11:10 was joined to a sample at 12:13, across the hour in which the
climate entity was unreachable. The unit had kept cooling, so the recovery
slope was right by luck.
A run now stops at any gap of more than SAMPLE_MAX_GAP_MINUTES.

Every test here drives an enabled zone through the real listeners, because
what is under test is what the zone learns from the samples it takes itself.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    IDLE_SETTLE_MINUTES,
    SAMPLE_MAX_GAP_MINUTES,
    SLOPE_MIN_SAMPLES,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"
CLIMATE_ENTITY = "climate.office_hvac"

# How often the room sensor reports in these tests: the cadence of the
# battery sensors in the incidents behind v0.18.0 and v0.19.0.
REPORT = timedelta(minutes=5)
LIMIT = timedelta(minutes=SAMPLE_MAX_GAP_MINUTES)

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


def _coordinator(hass: HomeAssistant, store: ComfortBandStore) -> ZoneCoordinator:
    """A zone wired to its listeners and hydrated from `store`, as setup and a
    restart both do."""
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    return coordinator


async def _enabled_zone(hass: HomeAssistant, **zone: Any) -> ZoneCoordinator:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", enabled=True, learning_enabled=True, **zone)
    return _coordinator(hass, store)


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Drain pending dispatch (which arms the debounce), then advance the
    clock and run every timer that came due -- see the fallback module."""
    await hass.async_block_till_done()
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _report(hass: HomeAssistant, freezer: FrozenDateTimeFactory, temp: float) -> None:
    """The room sensor reports, and the zone refreshes on it."""
    hass.states.async_set(TEMP_ENTITY, f"{temp:.2f}", {})
    await _settle(hass, freezer, REPORT)


def _set_climate(hass: HomeAssistant, mode: str, temperature: float | None = None) -> None:
    attrs: dict[str, Any] = {}
    if temperature is not None:
        attrs["temperature"] = temperature
    hass.states.async_set(CLIMATE_ENTITY, mode, attrs)


def _unit_follows(hass: HomeAssistant, climate_calls: list[tuple[str, dict[str, Any]]]) -> None:
    """The unit reports what it was last told: a mode, and a setpoint if one
    went with it."""
    modes = [data["hvac_mode"] for srv, data in climate_calls if srv == "set_hvac_mode"]
    setpoints = [data["temperature"] for srv, data in climate_calls if srv == "set_temperature"]
    _set_climate(hass, modes[-1], setpoints[-1] if setpoints else None)


def _times(coordinator: ZoneCoordinator, action: str) -> list[str]:
    return [s.t.strftime("%H:%M") for s in coordinator._samples_cache if s.action == action]


async def test_a_cool_cycle_does_not_resume_across_a_climate_outage(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The production case, where the unit did not keep cooling: it lost its
    power with its network connection, and an hour later its auto-restart
    brought it back in `cool` with the room a degree warmer. It reconnects in
    the mode it was left in, so nothing tells the zone. Joined across the
    outage, the cool run's two halves fitted as a room that cooling barely
    moved -- or warmed -- so the recovery slope was rejected, then too
    shallow, until the samples from before the outage had aged out of the
    window: MPC sat out the first twenty minutes and planned with the wrong
    rate for the next ten, while the unit was cooling at its usual rate.
    After an outage the run starts again, and MPC is back on the resumed
    cycle's fourth sample. That is also the price when the unit *did* keep
    cooling, as it did in production: nothing in the buffer tells the two
    apart."""
    freezer.move_to("2026-09-24 10:40:00+00:00")
    coordinator = await _enabled_zone(hass, mpc_enabled=True, manual_low=20.0, manual_high=22.0)
    # A settled idle slope in the cache, so MPC's readiness turns on the
    # recovery slope alone.
    await coordinator._store.async_update_zone(
        "office",
        persisted_idle_slope=0.2 / 60,
        persisted_idle_slope_at=(dt_util.utcnow() - timedelta(minutes=20)).isoformat(),
    )
    _set_climate(hass, HVAC_MODE_FAN_ONLY, 22.0)

    # Cooling from 10:45 to 11:10 at 1.2 °C/h, the unit following each command.
    for temp in (23.0, 22.9, 22.8, 22.7, 22.6, 22.5):
        await _report(hass, freezer, temp)
        _unit_follows(hass, climate_calls)
        assert coordinator.data.decision.action == ACTION_COOL
    assert coordinator.data.mpc_ready is True
    assert _times(coordinator, ACTION_COOL) == [
        "10:45",
        "10:50",
        "10:55",
        "11:00",
        "11:05",
        "11:10",
    ]

    # The unit drops off the network, and off the mains with it. The room
    # warms by a degree an hour; every command to the unit is dropped, so
    # nothing is sampled.
    _set_climate(hass, STATE_UNAVAILABLE)
    for k in range(1, 13):
        await _report(hass, freezer, 22.5 + 1.0 / 12 * k)
    assert _times(coordinator, ACTION_COOL)[-1] == "11:10"

    # Back at 12:12 by its own auto-restart, in the mode it was left in. The
    # room reports at 12:13, still above the band, and the zone cools on.
    await _settle(hass, freezer, timedelta(minutes=2))
    _set_climate(hass, HVAC_MODE_COOL, 20.0)
    hass.states.async_set(TEMP_ENTITY, "23.53", {})
    await _settle(hass, freezer, timedelta(minutes=1))
    assert _times(coordinator, ACTION_COOL)[-1] == "12:13"
    assert coordinator._samples_cache[-1].t - coordinator._samples_cache[-2].t > LIMIT

    # The first refresh to see a sample from after the outage. The run is
    # that one sample: no recovery slope, so MPC is not ready, and the
    # reactive predictor cools a room still above its band.
    await _report(hass, freezer, 23.43)
    slopes = coordinator.data.thermal_slopes
    assert slopes.sample_count_recovery_cool == 1
    assert slopes.recovery_cool is None
    assert slopes.method_recovery_cool == "none"
    assert coordinator.data.mpc_ready is False
    assert coordinator.data.decision.action == ACTION_COOL

    for temp in (23.33, 23.23):
        await _report(hass, freezer, temp)
        assert coordinator.data.mpc_ready is False

    # Four samples of the resumed cycle: the rate the unit is cooling at now.
    await _report(hass, freezer, 23.13)
    slopes = coordinator.data.thermal_slopes
    assert slopes.sample_count_recovery_cool == SLOPE_MIN_SAMPLES
    assert slopes.recovery_cool is not None
    assert slopes.recovery_cool * 60.0 == pytest.approx(-1.2, abs=0.01)
    assert coordinator.data.mpc_ready is True


async def test_an_idle_slope_is_not_fitted_across_a_sensor_outage(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The room sensor drops off its mesh for 25 minutes while the zone idles
    -- long past the grace period, and the unit publishes no reading to stand
    in. When it returns the room is 0.6 °C cooler, and it holds there.
    Joined, the idle run's settled part reached back across the outage, and
    the two levels fitted as passive cooling at up to 0.9 °C/h, though
    neither side of the outage showed any drift at all. It was live for forty
    minutes, fading as the samples from before the outage left the part of
    the run that was fitted, and written to the cache stamped as measured
    after the outage: had the zone left idle in that time, MPC could have
    planned with it until the next settled idle slope replaced it, for up to
    a day. Split, the samples after the outage are fitted on their own once
    there are four of them, and until then the cache keeps what was measured
    before."""
    freezer.move_to("2026-09-24 08:00:00+00:00")
    coordinator = await _enabled_zone(hass)
    _set_climate(hass, HVAC_MODE_FAN_ONLY, 22.5)

    # An hour of level idle. The last digit alternates so that every report
    # is a state change.
    for k in range(13):
        await _report(hass, freezer, 21.0 + 0.01 * (k % 2))
    assert coordinator.data.idle_slope_source == "live"
    last_before = coordinator._samples_cache[-1].t

    # The sensor goes dark. The zone can neither sample nor command, and what
    # the cache holds is the last drift measured before the outage.
    hass.states.async_set(TEMP_ENTITY, STATE_UNAVAILABLE, {})
    await _settle(hass, freezer, timedelta(minutes=20))
    assert coordinator.data.room is None
    zone = coordinator.get_zone_data()
    measured = (zone["persisted_idle_slope"], zone["persisted_idle_slope_at"])
    assert measured[0] is not None
    assert abs(measured[0] * 60.0) < 0.05
    assert measured[1] == last_before.isoformat()

    # Back, 0.6 °C lower. The refresh this report triggers samples it; the
    # next is the first to fit a buffer with a sample from after the gap.
    await _report(hass, freezer, 20.4)
    resumed = coordinator._samples_cache[-1].t
    assert resumed - last_before > LIMIT
    await _report(hass, freezer, 20.41)
    assert coordinator.data.idle_slope_source == "cached"
    assert coordinator.data.thermal_slopes.idle == measured[0]
    assert coordinator.data.thermal_slopes.sample_count_idle == 1
    zone = coordinator.get_zone_data()
    assert (zone["persisted_idle_slope"], zone["persisted_idle_slope_at"]) == measured

    # The cache stands in, unchanged, until there are four samples from after
    # the outage -- twenty minutes at this cadence. The idle stretch began long
    # before the outage, so they are past its settle window already.
    for k in range(20):
        if coordinator.data.idle_slope_source != "cached":
            break
        zone = coordinator.get_zone_data()
        assert (zone["persisted_idle_slope"], zone["persisted_idle_slope_at"]) == measured
        await _report(hass, freezer, 20.4 + 0.01 * (k % 2))
    assert dt_util.utcnow() - resumed == SLOPE_MIN_SAMPLES * REPORT

    # And the slope it then gives is fitted from what was seen after the
    # outage alone, and is what the cache now holds.
    slopes = coordinator.data.thermal_slopes
    assert coordinator.data.idle_slope_source == "live"
    assert slopes.sample_count_idle == SLOPE_MIN_SAMPLES
    assert slopes.idle is not None
    assert abs(slopes.idle * 60.0) < 0.05
    assert slopes.idle_measured_at is not None
    behind = [s for s in coordinator._samples_cache if s.t <= slopes.idle_measured_at]
    assert behind[-SLOPE_MIN_SAMPLES].t == resumed
    zone = coordinator.get_zone_data()
    assert zone["persisted_idle_slope_at"] == slopes.idle_measured_at.isoformat()


async def test_a_heat_cycle_is_not_joined_across_a_restart(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Home Assistant goes down for forty minutes in the middle of a heat
    cycle -- an update, a reboot -- and nothing is sampled, by construction.
    The buffer comes back from disk as it was last written, and the next heat
    sample used to be joined to it as though the forty minutes had been
    watched. No restart-specific handling: a restart leaves a gap like any
    other outage, and the run starts again after it."""
    freezer.move_to("2026-09-24 06:00:00+00:00")
    coordinator = await _enabled_zone(hass, manual_low=21.0, manual_high=23.0)
    _set_climate(hass, HVAC_MODE_FAN_ONLY, 22.0)

    # A cold morning: heating, 1.8 °C/h, well short of the band.
    for temp in (19.0, 19.15, 19.3, 19.45, 19.6):
        await _report(hass, freezer, temp)
        _unit_follows(hass, climate_calls)
        assert coordinator.data.decision.action == ACTION_HEAT
    heated = _times(coordinator, ACTION_HEAT)
    assert heated == ["06:05", "06:10", "06:15", "06:20", "06:25"]
    assert coordinator.data.thermal_slopes.recovery_heat is not None

    # Shut down. The zone's samples were written as they were taken (each is
    # at least the persist interval after the last), so the store has them all.
    store = coordinator._store
    _COORDINATORS.remove(coordinator)
    await coordinator.async_unload()
    await coordinator.async_shutdown()
    await _settle(hass, freezer, timedelta(minutes=40))

    # Back up: the buffer is restored, and the zone heats on.
    coordinator = _coordinator(hass, store)
    assert _times(coordinator, ACTION_HEAT) == heated
    for temp in (19.9, 20.0):
        await _report(hass, freezer, temp)
        _unit_follows(hass, climate_calls)
    assert coordinator._samples_cache[-2].t - coordinator._samples_cache[-3].t > LIMIT
    slopes = coordinator.data.thermal_slopes
    assert slopes.sample_count_recovery_heat == 1
    assert slopes.recovery_heat is None


async def test_a_sensor_that_loses_the_odd_report_keeps_its_runs_whole(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A zone that never loses sight of its room is untouched. A battery
    sensor on a busy mesh loses a report now and then, and once in a while two
    in a row -- here a ten- and a fifteen-minute gap in a five-minute cadence.
    Two in a row was the longest gap with nothing dark inside a run in the
    history the limit was chosen from, and the limit spans it with two minutes
    to spare: the idle run is fitted whole, every settled sample of it behind
    the slope."""
    freezer.move_to("2026-09-24 08:00:00+00:00")
    coordinator = await _enabled_zone(hass)
    _set_climate(hass, HVAC_MODE_FAN_ONLY, 22.5)

    temp = 21.0
    for lost in (0, 0, 0, 1, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 1, 0, 0, 0):
        for _ in range(lost):
            temp -= 0.2 / 12
            await _settle(hass, freezer, REPORT)
        temp -= 0.2 / 12
        await _report(hass, freezer, temp)
    gaps = [b.t - a.t for a, b in pairwise(coordinator._samples_cache)]
    assert max(gaps) == 3 * REPORT < LIMIT

    # One more refresh, on the buffer as it now stands.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    samples = coordinator._samples_cache
    settled = [s for s in samples if s.t >= samples[0].t + timedelta(minutes=IDLE_SETTLE_MINUTES)]
    slopes = coordinator.data.thermal_slopes
    assert coordinator.data.idle_slope_source == "live"
    assert slopes.sample_count_idle == len(settled)
    assert slopes.idle is not None
    assert slopes.idle * 60.0 == pytest.approx(-0.2, abs=0.01)
