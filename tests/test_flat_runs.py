"""v0.22.1: a run whose readings are all the same fits exactly flat.

A slope is a weighted least-squares fit over the latest run of an action. When
every reading in the run is the same, the fit is 0 in exact arithmetic, but in
floating point its numerator is the difference of two products that are equal
but for rounding, so the slope came out as that rounding -- about 1e-15 °C/min
either way, with its sign set by the weights, which move with the clock. The
v0.15.0 sign guard compared it against exactly zero, so it discarded a flat
heat run at one refresh and let it through at the next, and MPC then planned
with heating as something that holds the room where it is: below the band
that loses to any idle drift upwards, and MPC idled there.

A run whose readings never change needs refreshes that leave the reading
alone. Here the room's temperature never changes -- a sensor that reports only
on a change bigger than the room makes, on a night the unit can only just hold
it -- and the zone refreshes either on its humidity sensor, which it watches
with apparent temperature off as well as on, or on a temperature sensor with
`force_update`, which reports the same value every time.

Every test drives an enabled zone through the real listeners. v0.22.0's
behaviour is the same code with its fit (`_as_v022`): the readings fitted as
they come rather than relative to the first.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comfort_band import predictor
from custom_components.comfort_band.const import (
    ACTION_HEAT,
    ACTION_IDLE,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    SLOPE_MIN_SAMPLES,
    SLOPE_WEIGHT_TAU_MINUTES,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator
from custom_components.comfort_band.predictor import Sample
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"
HUMIDITY_ENTITY = "sensor.office_humidity"
CLIMATE_ENTITY = "climate.office_hvac"

# A battery sensor's cadence, and a room held 0.35 °C under a 19.2-21.0 band
# -- past its 0.3 °C deadband -- whose idle drift, remembered from earlier, is
# a slow rise of 0.3 °C/h (in °C/min).
REPORT = timedelta(minutes=5)
ROOM = 18.85
LOW, HIGH = 19.2, 21.0
IDLE_SLOPE = 0.3 / 60
# Far above the rounding of a flat fit, far below any slope a reading makes.
ROUNDING = 1e-12

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


def _uncentred_wls_slope(segment: list[Sample], *, now: datetime) -> float | None:
    """v0.22.0's `_wls_slope`: the same fit, with the readings as they come."""
    if len(segment) < SLOPE_MIN_SAMPLES:
        return None
    t_oldest = segment[0].t
    s_w = s_wx = s_wy = s_wxx = s_wxy = 0.0
    for sample in segment:
        x = (sample.t - t_oldest).total_seconds() / 60.0
        y = sample.temp
        age_min = max((now - sample.t).total_seconds() / 60.0, 0.0)
        w = math.exp(-age_min / SLOPE_WEIGHT_TAU_MINUTES)
        s_w += w
        s_wx += w * x
        s_wy += w * y
        s_wxx += w * x * x
        s_wxy += w * x * y
    denom = s_w * s_wxx - s_wx * s_wx
    if abs(denom) < 1e-9:
        return None
    return (s_w * s_wxy - s_wx * s_wy) / denom


@contextmanager
def _as_v022() -> Iterator[None]:
    with patch.object(predictor, "_wls_slope", _uncentred_wls_slope):
        yield


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Drain pending dispatch (which arms the debounce), then advance the
    clock and run every timer that came due -- see the fallback module."""
    await hass.async_block_till_done()
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@dataclass
class _Refresh:
    """What one refresh decided, and on what."""

    action: str
    ready: bool
    heat: float | None
    heat_method: str
    heat_samples: int


async def _held_room(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    climate_calls: list[tuple[str, dict[str, Any]]],
    trigger: str,
) -> list[_Refresh]:
    """An hour and a half of a zone on MPC whose room reads 18.85 °C at every
    refresh, heating or not. The unit takes up whatever it was last told."""
    freezer.move_to("2026-10-01 06:00:00+00:00")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_update_zone(
        "office",
        enabled=True,
        learning_enabled=True,
        mpc_enabled=True,
        manual_low=LOW,
        manual_high=HIGH,
        persisted_idle_slope=IDLE_SLOPE,
        persisted_idle_slope_at=dt_util.utcnow().isoformat(),
    )
    coordinator = ZoneCoordinator(
        hass,
        store,
        "office",
        CLIMATE_ENTITY,
        TEMP_ENTITY,
        humidity_entity_id=HUMIDITY_ENTITY if trigger == "humidity" else None,
    )
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    mode = HVAC_MODE_FAN_ONLY
    hass.states.async_set(CLIMATE_ENTITY, mode, {"temperature": 22.0})
    log: list[_Refresh] = []
    for k in range(18):
        if trigger == "humidity":
            hass.states.async_set(TEMP_ENTITY, f"{ROOM:.2f}", {})
            hass.states.async_set(HUMIDITY_ENTITY, f"{50 + k % 2:.1f}", {})
        else:
            hass.states.async_set(TEMP_ENTITY, f"{ROOM:.2f}", {}, force_update=True)
        await _settle(hass, freezer, REPORT)
        modes = [d["hvac_mode"] for srv, d in climate_calls if srv == "set_hvac_mode"]
        if modes:
            mode = modes[-1]
        hass.states.async_set(CLIMATE_ENTITY, mode, {"temperature": 22.0})
        state = coordinator.data
        assert state.decision_room == ROOM
        slopes = state.thermal_slopes
        log.append(
            _Refresh(
                action=state.decision.action,
                ready=state.mpc_ready,
                heat=slopes.recovery_heat,
                heat_method=slopes.method_recovery_heat,
                heat_samples=slopes.sample_count_recovery_heat,
            )
        )
    return log


def _modes(climate_calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [d["hvac_mode"] for srv, d in climate_calls if srv == "set_hvac_mode"]


@pytest.mark.parametrize("trigger", ["humidity", "force_update"])
async def test_before_v0_22_1_mpc_released_heating_below_the_band_on_rounding(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    trigger: str,
) -> None:
    """v0.22.0. The reactive path heats. At the fourth sample the heat run
    has a fit of its own, and the rounding comes out positive: MPC is ready,
    an hour of heating at that slope scores no time in band and nor does an
    hour of idling, but idling ends nearer the band, so MPC releases the
    heating 0.35 °C under it. Two refreshes on, the same run's rounding comes
    out negative, the guard discards it, and the reactive path heats again
    -- and the next cycle borrows the flat one's rounding. Over an hour and a
    half the room never moves, the heating is released three times, and the
    zone idles below its band for a third of the refreshes."""
    with _as_v022():
        log = await _held_room(hass, freezer, climate_calls, trigger)

    first_fit = log[SLOPE_MIN_SAMPLES]
    assert first_fit.heat_samples == SLOPE_MIN_SAMPLES
    assert first_fit.heat_method == "wls"
    assert first_fit.heat is not None
    assert 0.0 < first_fit.heat < ROUNDING
    assert first_fit.ready
    assert first_fit.action == ACTION_IDLE
    # Every refresh that idled was planned on rounding, and the same run was
    # discarded in between.
    idled = [r for r in log if r.action == ACTION_IDLE]
    assert 3 * len(idled) == len(log)
    assert all(r.ready and r.heat is not None and 0.0 < r.heat < ROUNDING for r in idled)
    assert any(r.heat_method == "rejected" and r.action == ACTION_HEAT for r in log)
    assert _modes(climate_calls).count(HVAC_MODE_FAN_ONLY) == 3


@pytest.mark.parametrize("trigger", ["humidity", "force_update"])
async def test_a_flat_heat_run_is_discarded_at_every_refresh(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    trigger: str,
) -> None:
    """v0.22.1, same room. From the fourth sample on the heat run fits
    exactly 0, which the guard discards at every refresh. MPC has no recovery
    slope, so it is never ready, and the reactive path heats the room for the
    whole hour and a half without a single release."""
    log = await _held_room(hass, freezer, climate_calls, trigger)

    assert all(r.action == ACTION_HEAT for r in log)
    assert not any(r.ready for r in log)
    fitted = [r for r in log if r.heat_samples >= SLOPE_MIN_SAMPLES]
    assert len(fitted) == len(log) - SLOPE_MIN_SAMPLES
    assert all(r.heat_method == "rejected" and r.heat is None for r in fitted)
    assert set(_modes(climate_calls)) == {HVAC_MODE_HEAT}
