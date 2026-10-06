"""v0.22.0: MPC holds a cycle it is still planning on a borrowed slope.

MPC scores constant actions over its horizon by time in band, with nothing
charged for switching, so near the room temperature at which cooling and
idling score the same it switches bang-bang. v0.21.0 let a cycle MPC starts
plan with the previous cycle's slope until it can be fitted, but MPC could
still end the cycle there -- a refresh later, once the room had crossed back
-- and a cycle that short lends nothing to the next one, which then found no
slope at all. The headline pair below runs one zone through both.

Every test drives an enabled zone through the real listeners. The room answers
what the unit was last told, `lag` reports late, so the zone closes its own
loop as in production. v0.21.0's behaviour is the same code with the two
v0.22.0 changes taken out (`_as_v021`): the hold in `mpc.plan`, which keys on
`ThermalSlopes.carried_over`, and the carry-over's minimum span.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comfort_band import predictor
from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    CARRY_OVER_MIN_SPAN_MINUTES,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    SLOPE_MIN_SAMPLES,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator, ZoneState
from custom_components.comfort_band.predictor import Sample, ThermalSlopes
from custom_components.comfort_band.storage import ComfortBandStore

TEMP_ENTITY = "sensor.office_temp"
CLIMATE_ENTITY = "climate.office_hvac"

# A battery sensor's cadence, and a room that cools at 2.4 °C/h while the unit
# cools and warms at 1.2 °C/h while it idles -- per report.
FIVE_MINUTES = timedelta(minutes=5)
DRIFT_5 = {HVAC_MODE_COOL: -0.2, HVAC_MODE_FAN_ONLY: 0.1}
# The same room at a sample a minute.
ONE_MINUTE = timedelta(minutes=1)
DRIFT_1 = {HVAC_MODE_COOL: -0.04, HVAC_MODE_FAN_ONLY: 0.02}
# What the zone remembers of its idle drift: 1.2 °C/h, in °C/min.
IDLE_SLOPE = 0.02

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


@dataclass
class _Refresh:
    """What one refresh decided, and on what."""

    room: float
    low: float
    high: float
    action: str
    mpc: str
    ready: bool
    cool_method: str
    cool_samples: int


class _Room:
    """The room and its unit: the sensor reports every `every`, the zone
    refreshes on it, the unit takes up whatever it was told, and the room
    moves by `drift` for the mode the unit was told `lag` reports earlier."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        coordinator: ZoneCoordinator,
        climate_calls: list[tuple[str, dict[str, Any]]],
        *,
        temp: float,
        every: timedelta,
        drift: dict[str, float],
        lag: int = 0,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.coordinator = coordinator
        self.calls = climate_calls
        self.temp = temp
        self.every = every
        self.drift = drift
        self.mode = HVAC_MODE_FAN_ONLY
        self.told = [self.mode] * (lag + 1)
        self.log: list[_Refresh] = []
        hass.states.async_set(CLIMATE_ENTITY, self.mode, {"temperature": 22.0})

    async def report(self) -> ZoneState:
        self.hass.states.async_set(TEMP_ENTITY, f"{self.temp:.3f}", {})
        await _settle(self.hass, self.freezer, self.every)
        modes = [d["hvac_mode"] for srv, d in self.calls if srv == "set_hvac_mode"]
        setpoints = [d["temperature"] for srv, d in self.calls if srv == "set_temperature"]
        if modes:
            self.mode = modes[-1]
        self.hass.states.async_set(
            CLIMATE_ENTITY, self.mode, {"temperature": setpoints[-1] if setpoints else 22.0}
        )
        self.told = [*self.told[1:], self.mode]
        self.temp += self.drift[self.told[0]]
        state = self.coordinator.data
        assert state.decision_room is not None
        slopes = state.thermal_slopes
        self.log.append(
            _Refresh(
                room=state.decision_room,
                low=state.effective_low,
                high=state.effective_high,
                action=state.decision.action,
                mpc=state.mpc_decision.action,
                ready=state.mpc_ready,
                cool_method=slopes.method_recovery_cool,
                cool_samples=slopes.sample_count_recovery_cool,
            )
        )
        return state

    def modes_since(self, mark: int) -> list[str]:
        return [d["hvac_mode"] for srv, d in self.calls[mark:] if srv == "set_hvac_mode"]


def _at(log: list[_Refresh], refresh: _Refresh) -> int:
    """Where `refresh` -- that very entry, not an equal one -- sits in `log`."""
    return next(i for i, r in enumerate(log) if r is refresh)


def _cycles(log: list[_Refresh]) -> list[list[_Refresh]]:
    """The cool cycles in a run of refreshes: the refreshes each one cooled for."""
    cycles: list[list[_Refresh]] = []
    for prev, cur in zip([None, *log], log, strict=False):
        if cur.action == ACTION_COOL:
            if prev is None or prev.action != ACTION_COOL:
                cycles.append([])
            cycles[-1].append(cur)
    return cycles


async def _zone(hass: HomeAssistant, *, mpc: bool = True, name: str = "office") -> ZoneCoordinator:
    """A zone on a 19-21 °C band that remembers an idle drift of 1.2 °C/h, as
    it would from an earlier idle stretch -- so MPC is ready as soon as a
    cycle leaves a recovery slope -- read in UTC, so the band is the clock's."""
    await hass.config.async_set_time_zone("UTC")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone(name)
    await store.async_update_zone(
        name,
        enabled=True,
        learning_enabled=True,
        mpc_enabled=mpc,
        persisted_idle_slope=IDLE_SLOPE,
        persisted_idle_slope_at=dt_util.utcnow().isoformat(),
    )
    await store.async_set_zone_schedule(
        name, "home", baseline=[{"at": "00:00", "low": 19.0, "high": 21.0}]
    )
    coordinator = ZoneCoordinator(hass, store, name, CLIMATE_ENTITY, TEMP_ENTITY)
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    return coordinator


@contextmanager
def _as_v021() -> Iterator[None]:
    """The same code with v0.22.0's two changes taken out: MPC no longer sees
    which slopes are borrowed, so it holds nothing, and a young run's own fit
    replaces the borrowed one as soon as it has the samples for one."""
    with (
        patch.object(ThermalSlopes, "carried_over", lambda self, action: False),
        patch.object(predictor, "CARRY_OVER_MIN_SPAN_MINUTES", 0),
    ):
        yield


async def _warm_room(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    climate_calls: list[tuple[str, dict[str, Any]]],
    *,
    mpc: bool = True,
    reports: int = 48,
    rerun: bool = False,
) -> _Room:
    """Four hours of a room that starts a degree above its band, at a battery
    sensor's cadence. The first cycle is the reactive path's until it has a
    slope, then MPC's, and it ends below where MPC switches -- about 20.35 °C
    here, where an hour of cooling and of idling keep the room in its band for
    as long as each other -- so MPC starts the next cycle as the room warms
    back through that point, on the slope the first one left. `rerun` runs
    the same room again a day later, under a zone of another name, for tests
    that compare two runs."""
    freezer.move_to("2026-09-25 12:00:00+00:00" if rerun else "2026-09-24 12:00:00+00:00")
    coordinator = await _zone(hass, mpc=mpc, name="office_rerun" if rerun else "office")
    room = _Room(
        hass,
        freezer,
        coordinator,
        climate_calls,
        temp=22.0,
        every=FIVE_MINUTES,
        drift=DRIFT_5,
    )
    for _ in range(reports):
        await room.report()
    return room


async def test_without_the_hold_mpc_chatters_and_then_loses_the_room(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.21.0. The first cycle runs long enough to fit. MPC starts the next
    at its switching point and plans it with the first cycle's slope, but a
    refresh later the room has crossed back, idling scores better, and MPC
    ends it: a cycle of one sample. Ten minutes on it starts another, which
    finds only that one-sample run before it -- nothing to borrow -- so MPC
    is no longer ready, and the reactive path ends that cycle too. From there
    nothing the zone runs is long enough to fit: the reactive path lets the
    room warm out of its band and then cools it for a sample or three, MPC
    never comes back, and the room spends more than half the rest of the run
    above its band."""
    with _as_v021():
        room = await _warm_room(hass, freezer, climate_calls)

    cycles = _cycles(room.log)
    first, second, third = cycles[0], cycles[1], cycles[2]
    assert len(first) >= SLOPE_MIN_SAMPLES
    # MPC starts the second on the first one's slope, and ends it itself a
    # refresh later.
    assert len(second) == 1
    assert second[0].ready
    assert second[0].mpc == ACTION_COOL
    after_second = room.log[_at(room.log, second[0]) + 1]
    assert after_second.action == ACTION_IDLE
    assert after_second.ready
    assert after_second.cool_method == "previous"
    # The third has nothing to borrow, and the reactive path ends it.
    assert len(third) == 1
    after_third = room.log[_at(room.log, third[0]) + 1]
    assert not after_third.ready
    assert after_third.cool_method == "none"
    assert after_third.action == ACTION_IDLE
    # MPC never comes back, every cycle after is too short to fit, and the
    # room spends more than half the rest of the run above its band.
    rest = room.log[_at(room.log, after_third) :]
    assert not any(r.ready for r in rest)
    assert all(len(c) < SLOPE_MIN_SAMPLES for c in cycles[2:])
    above = sum(r.room > r.high for r in rest)
    assert 2 * above > len(rest)


async def test_mpc_holds_a_cycle_until_it_has_a_slope_of_its_own(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """v0.22.0, same room. MPC starts the second cycle at the same point, and
    a refresh later idling scores better just as it did -- but the cycle is
    still planned on the borrowed slope, so MPC holds it until the run has a
    slope of its own: four samples, fifteen minutes at this cadence. Then it
    ends it, below its switching point, and the next cycle has a cycle long
    enough to fit before it. Every cycle MPC starts runs to four samples, MPC
    stays ready from the first fit to the end, and the room stays in its
    band."""
    room = await _warm_room(hass, freezer, climate_calls)

    cycles = _cycles(room.log)
    if room.log[-1].action == ACTION_COOL:
        cycles.pop()  # still running when the run ends
    assert len(cycles[0]) >= SLOPE_MIN_SAMPLES
    first_fit = next(i for i, r in enumerate(room.log) if r.ready)
    assert all(r.ready for r in room.log[first_fit:])
    assert len(cycles) >= 4
    for cycle in cycles[1:]:
        assert cycle[0].mpc == ACTION_COOL
        # Held while its slope is borrowed: the refreshes after the start
        # have one, two and three samples behind them.
        assert [r.cool_method for r in cycle[1:SLOPE_MIN_SAMPLES]] == ["previous"] * (
            SLOPE_MIN_SAMPLES - 1
        )
        assert [r.cool_samples for r in cycle[1:SLOPE_MIN_SAMPLES]] == list(
            range(1, SLOPE_MIN_SAMPLES)
        )
    # Held at least once against a better-scoring idle: not just a cycle
    # that would have run on anyway.
    held = [r for c in cycles[1:] for r in c[1:] if r.cool_method == "previous"]
    assert any(r.room < cycles[1][0].room for r in held)
    # And no longer: each ends at the first refresh with a slope of its own.
    assert all(len(c) == SLOPE_MIN_SAMPLES for c in cycles[1:])
    settled = room.log[_at(room.log, cycles[1][0]) :]
    assert all(r.low <= r.room <= r.high for r in settled)


async def test_the_two_runs_part_where_v021_ends_a_borrowed_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The same room, run as v0.21.0 and as v0.22.0 side by side: identical up
    to the refresh after MPC starts its second cycle, and that refresh is the
    first difference -- v0.21.0 ends the cycle, v0.22.0 holds it."""
    with _as_v021():
        old = await _warm_room(hass, freezer, climate_calls, reports=20)
    for coordinator in _COORDINATORS:
        await coordinator.async_unload()
        await coordinator.async_shutdown()
    _COORDINATORS.clear()
    climate_calls.clear()
    new = await _warm_room(hass, freezer, climate_calls, reports=20, rerun=True)

    split = next(i for i, (a, b) in enumerate(zip(old.log, new.log, strict=True)) if a != b)
    second_start = _at(old.log, _cycles(old.log)[1][0])
    assert split == second_start + 1
    assert old.log[split].action == ACTION_IDLE
    assert new.log[split].action == ACTION_COOL
    assert new.log[split].mpc == ACTION_COOL
    assert new.log[split].cool_method == "previous"
    assert old.log[split].room == new.log[split].room


async def test_a_zone_not_on_mpc_controls_exactly_as_before(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The hold is MPC's alone. A zone on the reactive path runs the same room
    command for command as it did on v0.21.0, through cycles planned -- by
    its MPC shadow -- on a borrowed slope."""
    with _as_v021():
        old = await _warm_room(hass, freezer, climate_calls, mpc=False)
    old_calls = list(climate_calls)
    for coordinator in _COORDINATORS:
        await coordinator.async_unload()
        await coordinator.async_shutdown()
    _COORDINATORS.clear()
    climate_calls.clear()
    new = await _warm_room(hass, freezer, climate_calls, mpc=False, rerun=True)

    assert climate_calls == old_calls
    assert [r.action for r in new.log] == [r.action for r in old.log]
    assert [r.room for r in new.log] == [r.room for r in old.log]
    assert any(r.action == ACTION_COOL and r.cool_method == "previous" for r in new.log)


async def test_a_held_cycle_still_stops_at_the_band_edge(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The hold never keeps a cycle running past the band edge it is driving
    the room towards. Here the unit cools much faster on its second cycle
    than on its first -- a higher fan speed, say -- so the borrowed slope is
    far too slow, and the room reaches the bottom of the band while the cycle
    is still held. It is released at the first refresh at or past the edge,
    on the borrowed slope, and the room is back inside its band at the next
    report. The zone has a heat slope from a cycle earlier on, so MPC does
    not simply defer to the predictor below the band: the hold's own edge
    rule is what lets the cycle go."""
    freezer.move_to("2026-09-24 12:00:00+00:00")
    coordinator = await _zone(hass)
    now = dt_util.utcnow()
    coordinator._samples_cache = [
        Sample(t=now - timedelta(minutes=20 - 3 * i), temp=18.2 + 0.12 * i, action=ACTION_HEAT)
        for i in range(5)
    ]
    room = _Room(
        hass,
        freezer,
        coordinator,
        climate_calls,
        temp=22.0,
        every=FIVE_MINUTES,
        drift=dict(DRIFT_5),
    )
    while not (len(_cycles(room.log)) == 1 and room.log[-1].action == ACTION_IDLE):
        await room.report()
    room.drift[HVAC_MODE_COOL] = -0.7
    while len(_cycles(room.log)) < 2:
        await room.report()
    mark = len(climate_calls)
    while room.log[-1].action == ACTION_COOL:
        state = await room.report()

    end = room.log[-1]
    held = _cycles(room.log)[1]
    assert state.thermal_slopes.recovery_heat is not None
    assert end.room <= end.low
    assert end.cool_method == "previous"
    assert end.cool_samples < SLOPE_MIN_SAMPLES
    assert end.ready
    assert end.mpc == ACTION_IDLE
    assert all(r.cool_method == "previous" for r in held[1:])
    assert room.modes_since(mark) == [HVAC_MODE_FAN_ONLY]
    nxt = await room.report()
    assert nxt.decision_room is not None
    assert nxt.decision_room >= nxt.effective_low


async def test_a_cycle_held_while_its_commands_raise_is_let_go(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A cloud unit whose mode call times out but takes the command all the
    same. Nothing is recorded for a call that raises, so once the same-mode
    min-cycle has run out and the held cycle is re-commanded, its run stops
    growing: it would stay young, and held, for as long as the cycle it
    borrows from stayed fittable -- cooling the room to the bottom of its band
    and then flipping between cool and idle there, refresh after refresh.
    Once the run has had no sample for longer than a gap in sampling, it
    borrows nothing (as a run resumed after that gap would not), MPC is no
    longer ready, and the reactive path lets the room back into its band."""
    freezer.move_to("2026-09-24 12:00:00+00:00")
    coordinator = await _zone(hass)
    room = _Room(
        hass,
        freezer,
        coordinator,
        climate_calls,
        temp=22.0,
        every=FIVE_MINUTES,
        drift=dict(DRIFT_5),
    )
    while len(_cycles(room.log)) < 2:
        await room.report()
    held_from = len(room.log) - 1

    async def _taken_but_raises(call: Any) -> None:
        climate_calls.append((call.service, dict(call.data)))
        raise HomeAssistantError("cloud timeout")

    hass.services.async_register("climate", "set_hvac_mode", _taken_but_raises)
    mark = len(climate_calls)
    for _ in range(14):
        await room.report()

    after = room.log[held_from:]
    let_go = next(i for i, r in enumerate(after) if r.action == ACTION_IDLE)
    # Held on the borrowed slope while samples came, and for a gap after.
    assert all(r.cool_method == "previous" for r in after[1:let_go])
    assert not after[let_go].ready
    assert after[let_go].cool_method == "none"
    # Let go inside the band, and never back to cooling: the room comes back
    # up through the band instead of sitting on its bottom edge.
    assert all(r.low < r.room for r in after)
    assert all(r.action == ACTION_IDLE for r in after[let_go:])
    modes = room.modes_since(mark)
    assert HVAC_MODE_FAN_ONLY in modes
    assert HVAC_MODE_COOL not in modes[modes.index(HVAC_MODE_FAN_ONLY) :]
    assert after[-1].room > after[let_go].room


async def _cool_room_at_one_minute(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    climate_calls: list[tuple[str, dict[str, Any]]],
    *,
    lag: int,
) -> tuple[_Room, int]:
    """The room at a sample a minute, with a unit that keeps the room drifting
    the old way for `lag` minutes after it is told to cool. Runs it until MPC
    starts its second cycle -- the first one it plans on a borrowed slope --
    and returns the room and how many commands had gone out by then."""
    freezer.move_to("2026-09-24 12:00:00+00:00")
    coordinator = await _zone(hass)
    room = _Room(
        hass,
        freezer,
        coordinator,
        climate_calls,
        temp=21.6,
        every=ONE_MINUTE,
        drift=DRIFT_1,
        lag=lag,
    )
    for _ in range(240):
        await room.report()
        if len(_cycles(room.log)) == 2:
            break
    started = room.log[-1]
    assert started.action == ACTION_COOL
    assert started.mpc == ACTION_COOL
    assert started.ready
    assert started.low < started.room < started.high
    # A cycle the reactive path saw no reason for: MPC's own.
    assert coordinator.data.predicted_decision.action == ACTION_IDLE
    return room, len(climate_calls)


async def test_at_a_one_minute_cadence_a_slow_unit_keeps_its_cycle_past_the_first_fit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """At a sample a minute a young run has four samples three minutes in, and
    a unit that has not moved the room by then leaves them drifting the old
    way. On v0.21.0 that fit replaced the borrowed slope; the sign guard
    discarded it, MPC lost the cycle's slope, and the reactive path ended a
    cycle with the room inside its band at its fourth minute. Now a run also
    has to have been watched for CARRY_OVER_MIN_SPAN_MINUTES before its own
    fit is used: until then MPC goes on planning with the borrowed slope, and
    holding the cycle on it. By ten minutes the unit has been cooling for
    seven, the run's own fit has the right sign, and MPC runs the cycle on it
    and ends it itself."""
    room, mark = await _cool_room_at_one_minute(hass, freezer, climate_calls, lag=3)

    young = []
    while True:
        state = await room.report()
        young.append(room.log[-1])
        if state.thermal_slopes.method_recovery_cool != "previous":
            break
    # The wrong-way fit at four samples, and the ones after it, never reach
    # MPC: the cycle is held on the borrowed slope for ten minutes.
    borrowed, own = young[:-1], young[-1]
    assert [r.cool_samples for r in borrowed] == list(range(1, len(young)))
    assert all(r.action == ACTION_COOL and r.ready for r in borrowed)
    assert own.cool_method == "wls"
    assert own.cool_samples == CARRY_OVER_MIN_SPAN_MINUTES + 1
    # Its own fit, right way round, keeps MPC ready -- and MPC ends the cycle
    # on it, the room by now well below where it would start one.
    assert own.ready
    assert own.action == ACTION_IDLE
    assert own.mpc == ACTION_IDLE
    assert own.room < young[0].room
    modes = room.modes_since(mark)
    assert modes.count(HVAC_MODE_FAN_ONLY) == 1
    assert modes[-1] == HVAC_MODE_FAN_ONLY


async def test_at_a_one_minute_cadence_v021_lost_the_cycle_at_its_first_fit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The v0.21.0 limit the test above lifts, kept as it was pinned there:
    three refreshes on the carried slope, then the fourth sample makes the
    run fittable, its fit is the wrong way round, the guard discards it, and
    the reactive path releases a room inside its band."""
    with _as_v021():
        room, mark = await _cool_room_at_one_minute(hass, freezer, climate_calls, lag=3)
        for samples in range(1, SLOPE_MIN_SAMPLES):
            await room.report()
            assert room.log[-1].cool_samples == samples
            assert room.log[-1].cool_method == "previous"
            assert room.log[-1].ready
            assert room.log[-1].action == ACTION_COOL
        await room.report()

    first_fit = room.log[-1]
    assert first_fit.cool_samples == SLOPE_MIN_SAMPLES
    assert first_fit.cool_method == "rejected"
    assert not first_fit.ready
    assert first_fit.low < first_fit.room < first_fit.high
    assert first_fit.action == ACTION_IDLE
    assert room.modes_since(mark) == [HVAC_MODE_FAN_ONLY]


async def test_a_unit_that_has_not_moved_the_room_in_ten_minutes_loses_the_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The sign guard still has the last word on the fit MPC uses. A unit that
    is still not moving the room when the run has been watched for
    CARRY_OVER_MIN_SPAN_MINUTES fits the wrong way round then, the guard
    discards that fit, nothing is carried over a fit old enough to stand, and
    the reactive path releases the cycle -- at its eleventh sample, rather
    than its fourth."""
    room, mark = await _cool_room_at_one_minute(hass, freezer, climate_calls, lag=15)

    while room.log[-1].action == ACTION_COOL:
        await room.report()
        assert len(room.log) < 500

    lost = room.log[-1]
    assert lost.cool_method == "rejected"
    assert lost.cool_samples == CARRY_OVER_MIN_SPAN_MINUTES + 1
    assert not lost.ready
    assert lost.low < lost.room < lost.high
    held = room.log[-lost.cool_samples : -1]
    assert all(r.cool_method == "previous" and r.action == ACTION_COOL for r in held)
    # Released once, at the end. (The cool before it is the re-commit the
    # same-mode min-cycle allows once it has run out.)
    modes = room.modes_since(mark)
    assert modes.count(HVAC_MODE_FAN_ONLY) == 1
    assert modes[-1] == HVAC_MODE_FAN_ONLY
