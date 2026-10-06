"""v0.21.0: a cycle MPC starts keeps the recovery slope it was started with.

The incident: a zone on MPC, cooling only at the time, had a recovery slope
from a twenty-sample cool cycle earlier in its buffer, and MPC started a cool
cycle with the room inside the band. The commit appended one cooling sample,
and a slope comes from the latest run alone -- so the next refresh, 58
seconds later, found a cool run of one sample, no recovery slope, and MPC no
longer ready. The reactive predictor took over, and hysteresis released the
cooling because the room was inside the band. `binary_sensor.{zone}_mpc_ready`
went off with it, and stayed off until a reactive cool cycle long enough to
fit came along. Min-cycle never came into it: idle releases pass through
unchecked so that a cycle can always stop.

Every test here drives an enabled zone through the real listeners. The room
answers what the unit was last told -- it cools while the unit cools and
warms while it idles -- so the zone is closing its own loop, as in production.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_IDLE,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    SLOPE_MIN_SAMPLES,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator
from custom_components.comfort_band.sensor import ThermalSlopeSensor
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"
CLIMATE_ENTITY = "climate.office_hvac"

# The incident's cadence: a room sensor reporting about once a minute.
REPORT = timedelta(minutes=1)
# How fast the room moves (°C per report) under what the unit was last told:
# 2.4 °C/h down while cooling, 0.9 °C/h up while idle.
DRIFT = {HVAC_MODE_COOL: -0.04, HVAC_MODE_FAN_ONLY: 0.015}

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


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Drain pending dispatch (which arms the debounce), then advance the
    clock and run every timer that came due -- see the fallback module."""
    await hass.async_block_till_done()
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


class _Room:
    """The room and its unit: the sensor reports, the zone refreshes on it,
    the unit takes up whatever it was told, and the room moves a report's
    worth before the next one -- in the mode the unit was told `lag` reports
    earlier, so a unit slow to come up keeps the room drifting the old way
    for that long."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        climate_calls: list[tuple[str, dict[str, Any]]],
        temp: float,
        lag: int = 0,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.calls = climate_calls
        self.temp = temp
        self.mode = HVAC_MODE_FAN_ONLY
        self.told = [self.mode] * (lag + 1)
        hass.states.async_set(CLIMATE_ENTITY, self.mode, {"temperature": 22.0})

    async def report(self) -> None:
        self.hass.states.async_set(TEMP_ENTITY, f"{self.temp:.3f}", {})
        await _settle(self.hass, self.freezer, REPORT)
        modes = [d["hvac_mode"] for srv, d in self.calls if srv == "set_hvac_mode"]
        setpoints = [d["temperature"] for srv, d in self.calls if srv == "set_temperature"]
        if modes:
            self.mode = modes[-1]
        self.hass.states.async_set(
            CLIMATE_ENTITY, self.mode, {"temperature": setpoints[-1] if setpoints else 22.0}
        )
        self.told = [*self.told[1:], self.mode]
        self.temp += DRIFT[self.told[0]]

    def modes_since(self, mark: int) -> list[str]:
        return [d["hvac_mode"] for srv, d in self.calls[mark:] if srv == "set_hvac_mode"]


async def _zone_on_mpc(hass: HomeAssistant) -> ZoneCoordinator:
    """A zone on MPC whose band widens at 19:00 -- warmer is fine in the
    evening -- read in UTC, so the schedule's times are the clock's."""
    await hass.config.async_set_time_zone("UTC")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone("office", enabled=True, learning_enabled=True, mpc_enabled=True)
    await store.async_set_zone_schedule(
        "office",
        "home",
        baseline=[
            {"at": "00:00", "low": 19.0, "high": 21.0},
            {"at": "19:00", "low": 19.0, "high": 22.0},
        ],
    )
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY)
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    return coordinator


async def test_a_cycle_mpc_starts_is_not_released_on_the_next_refresh(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The incident, one cycle of it. The zone cools a warm room into its band
    before it has an idle slope, so the predictor runs that cycle, and it
    leaves a recovery slope in the buffer. MPC comes ready half an hour on,
    once the idle stretch has settled, finds the room well above where it would
    stop cooling, and starts a cool cycle the reactive path saw no reason for.
    On the next refresh it used to lose the recovery slope to the one-sample
    run, and the cycle was released: a cycle of one minute, and `mpc_ready`
    off. Now that refresh plans with the slope the cycle was started with, and
    -- with a unit that moves the room as soon as it is told -- MPC runs its
    cycle until it ends it itself."""
    freezer.move_to("2026-09-24 18:30:00+00:00")
    coordinator = await _zone_on_mpc(hass)
    room = _Room(hass, freezer, climate_calls, temp=21.6)

    # Above the evening's first band, so the zone cools. No idle slope yet --
    # nothing cached, and the live one waits out its settle window -- so MPC
    # is not ready, and the predictor runs the cycle and ends it early.
    await room.report()
    assert coordinator.data.decision.action == ACTION_COOL
    for _ in range(30):
        if coordinator.data.decision.action != ACTION_COOL:
            break
        assert not coordinator.data.mpc_ready
        await room.report()
    assert coordinator.data.decision.action == ACTION_IDLE
    measured = coordinator.data.thermal_slopes
    assert measured.method_recovery_cool == "wls"
    assert measured.sample_count_recovery_cool >= SLOPE_MIN_SAMPLES
    assert measured.recovery_cool is not None

    # Idle, warming. At 19:00 the band widens, and once the idle stretch has
    # settled MPC has both slopes: the room is well inside the band, but it
    # would leave it within the hour, so MPC starts cooling.
    for _ in range(60):
        await room.report()
        if coordinator.data.decision.action == ACTION_COOL:
            break
    started = coordinator.data
    assert started.mpc_ready
    assert started.mpc_decision.action == ACTION_COOL
    assert started.predicted_decision.action == ACTION_IDLE
    assert started.decision_room is not None
    assert started.decision_room < started.effective_high
    mark = len(climate_calls)
    assert room.mode == HVAC_MODE_COOL

    # The next refresh sees a cool run of one sample. It used to release the
    # cycle here. MPC plans with the slope it started the cycle with, and goes
    # on cooling.
    await room.report()
    nxt = coordinator.data
    assert nxt.mpc_ready
    assert nxt.decision.action == ACTION_COOL
    assert HVAC_MODE_FAN_ONLY not in room.modes_since(mark)
    assert nxt.thermal_slopes.sample_count_recovery_cool == 1
    assert nxt.thermal_slopes.method_recovery_cool == "previous"
    assert nxt.thermal_slopes.recovery_cool == pytest.approx(measured.recovery_cool)
    attrs = ThermalSlopeSensor(coordinator).extra_state_attributes
    assert attrs["method_recovery_cool"] == "previous"
    assert attrs["sample_count_recovery_cool"] == 1

    # MPC holds the cycle -- ready throughout, its own slope taking over once
    # the run can be fitted -- and ends it itself.
    minutes = 1
    for _ in range(60):
        if coordinator.data.decision.action != ACTION_COOL:
            break
        assert coordinator.data.mpc_ready
        await room.report()
        minutes += 1
    assert coordinator.data.decision.action == ACTION_IDLE
    assert coordinator.data.mpc_decision.action == ACTION_IDLE
    assert coordinator.data.thermal_slopes.method_recovery_cool == "wls"
    assert minutes > SLOPE_MIN_SAMPLES
    # Released once, at the end. (The cool in between is the re-commit the
    # same-mode min-cycle allows once it has run out.)
    modes = room.modes_since(mark)
    assert modes.count(HVAC_MODE_FAN_ONLY) == 1
    assert modes[-1] == HVAC_MODE_FAN_ONLY


# The v0.21.0 limit pinned here -- at a sample a minute, a slow unit's first fit,
# three minutes in, came out the wrong way round and lost MPC the cycle -- was
# lifted in v0.22.0 by the carry-over's minimum span. It is kept, as v0.21.0
# behaved, beside the tests of what replaced it in test_mpc_hysteresis.py.
