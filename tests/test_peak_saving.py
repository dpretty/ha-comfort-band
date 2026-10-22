"""v0.24.0 peak saving, end to end through the coordinator.

While the zone's peak entity is on, the band relaxes and the zone runs plain
hysteresis on it; ahead of a start the entity announces in `next_event`, the
band steps toward the useful edge. Every test drives a zone through the real
listeners (`subscribe_and_hydrate`), so a change to the peak entity reaches
the zone the way it does in production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.comfort_band import mpc
from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    CONF_CLIMATE_ENTITY,
    CONF_KIND,
    CONF_PEAK_ENTITY,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    DOMAIN,
    ENTRY_KIND_ZONE,
    FALLBACK_GRACE_S,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    PEAK_STATE_ACTIVE,
    PEAK_STATE_NO_SIGNAL,
    PEAK_STATE_OFF,
    PEAK_STATE_OVERRIDE,
    PEAK_STATE_PRECONDITIONING,
    PEAK_STATE_WAITING,
    PEAK_STATES,
    ROOM_SOURCE_CLIMATE,
)
from custom_components.comfort_band.coordinator import ZoneCoordinator
from custom_components.comfort_band.hysteresis import idle_decision
from custom_components.comfort_band.predictor import Sample
from custom_components.comfort_band.storage import ComfortBandStore, StoredTransition

TEMP_ENTITY = "sensor.office_temp"
CLIMATE_ENTITY = "climate.office_hvac"
PEAK_ENTITY = "schedule.peak_period"
START = "2026-06-15 16:00:00+00:00"
# Long enough for an entity change to reach the zone: the 2 s debounce, and
# the coordinator's own 10 s cooldown between requested refreshes.
QUIET = timedelta(seconds=11)

_COORDINATORS: list[ZoneCoordinator] = []


@pytest.fixture(autouse=True)
def _climate_services(climate_calls: list[tuple[str, dict[str, Any]]]) -> None:
    """Every zone here is enabled, so give its commands somewhere to land."""


@pytest.fixture(autouse=True)
async def _unload_coordinators(hass: HomeAssistant) -> AsyncIterator[None]:
    """A coordinator left standing holds pending timers (the refresh debounce,
    and here the peak timer) that HA's lingering-timer check fails."""
    _COORDINATORS.clear()
    yield
    for coordinator in _COORDINATORS:
        await coordinator.async_unload()
        await coordinator.async_shutdown()
    _COORDINATORS.clear()


async def _zone(
    hass: HomeAssistant,
    *,
    peak_entity: str | None = PEAK_ENTITY,
    schedule: list[StoredTransition] | None = None,
    **fields: Any,
) -> ZoneCoordinator:
    """An enabled `office` zone on a 20-23 °C manual band with peak saving on,
    offsets 1.0 (heat) and 1.5 (cool), and a 60-minute lead -- read in UTC,
    so the clock is the band's. `fields` override any of that."""
    await hass.config.async_set_time_zone("UTC")
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    settings: dict[str, Any] = {
        "enabled": True,
        "manual_low": 20.0,
        "manual_high": 23.0,
        "peak_saving_enabled": True,
        "peak_heat_offset": 1.0,
        "peak_cool_offset": 1.5,
        "peak_precondition_minutes": 60,
    }
    settings.update(fields)
    await store.async_update_zone("office", **settings)
    if schedule is not None:
        await store.async_set_zone_schedule("office", "home", baseline=schedule)
    coordinator = ZoneCoordinator(
        hass, store, "office", CLIMATE_ENTITY, TEMP_ENTITY, peak_entity_id=peak_entity
    )
    coordinator.subscribe_and_hydrate()
    _COORDINATORS.append(coordinator)
    return coordinator


def _peak(hass: HomeAssistant, state: str, next_event: object = None, **attrs: Any) -> None:
    attributes: dict[str, Any] = dict(attrs)
    if next_event is not None:
        attributes["next_event"] = next_event
    hass.states.async_set(PEAK_ENTITY, state, attributes)


def _climate(hass: HomeAssistant, mode: str = HVAC_MODE_FAN_ONLY, **attrs: Any) -> None:
    hass.states.async_set(CLIMATE_ENTITY, mode, {"temperature": 22.0, **attrs})


async def _refresh(hass: HomeAssistant, coordinator: ZoneCoordinator) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def _settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Drain pending dispatch (which arms the debounce), then advance the
    clock and run every timer that came due."""
    await hass.async_block_till_done()
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _calls(climate_calls: list[tuple[str, dict[str, Any]]], service: str) -> list[dict[str, Any]]:
    return [data for srv, data in climate_calls if srv == service]


@contextmanager
def _spy_plan() -> Iterator[MagicMock]:
    """Record what the coordinator hands `mpc.plan`, without changing it."""
    with patch("custom_components.comfort_band.coordinator.mpc.plan", wraps=mpc.plan) as spy:
        yield spy


def _heat_run(now: datetime, *, ago_minutes: int, temp: float) -> list[Sample]:
    """Six heat samples two minutes apart, rising 1.2 °C/h, the first
    `ago_minutes` before `now`: long enough to fit, and to lend."""
    base = now - timedelta(minutes=ago_minutes)
    return [
        Sample(t=base + timedelta(minutes=2 * i), temp=temp + 0.04 * i, action=ACTION_HEAT)
        for i in range(6)
    ]


async def _recently_heated(coordinator: ZoneCoordinator, *, action: str = ACTION_HEAT) -> None:
    """The zone ran `action` an hour ago and has idled since."""
    await coordinator._store.async_update_zone(
        "office",
        last_action=ACTION_IDLE,
        previous_action=action,
        last_action_at=dt_util.utcnow().isoformat(),
        last_active_at=(dt_util.utcnow() - timedelta(hours=1)).isoformat(),
    )


# ----- off is exactly as before -----


@pytest.mark.parametrize("peak_entity", [None, PEAK_ENTITY])
async def test_with_peak_saving_off_nothing_changes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    peak_entity: str | None,
) -> None:
    """Switched off (or with no entity), a zone in a peak, inside a lead time,
    with a schedule, plans with the schedule's band and steps exactly as
    before, and arms no timer."""
    freezer.move_to(START)
    schedule: list[StoredTransition] = [
        {"at": "00:00", "low": 20.0, "high": 23.0},
        {"at": "16:30", "low": 21.0, "high": 24.0},
    ]
    coordinator = await _zone(
        hass,
        peak_entity=peak_entity,
        schedule=schedule,
        peak_saving_enabled=peak_entity is None,
    )
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=20))
    hass.states.async_set(TEMP_ENTITY, "19.0", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    state = coordinator.data
    expected = coordinator._compute_bands_per_step(
        coordinator._store.get_zone_schedule("office", "home"), 60
    )
    assert spy.call_args.kwargs["bands_per_step"] == expected
    assert (state.effective_low, state.effective_high) == (20.0, 23.0)
    assert state.peak_state == PEAK_STATE_OFF
    assert (state.peak_low_adjustment, state.peak_high_adjustment) == (0.0, 0.0)
    assert state.decision.action == ACTION_HEAT
    assert coordinator._unsub_peak_timer is None

    # With the lead start still ahead, where a timer would be armed if peak
    # saving were on, none is.
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=90))
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_OFF
    assert coordinator._unsub_peak_timer is None

    _peak(hass, "on")
    await _refresh(hass, coordinator)
    assert (coordinator.data.effective_low, coordinator.data.effective_high) == (20.0, 23.0)
    assert coordinator.data.peak_state == PEAK_STATE_OFF


async def test_without_a_peak_entity_nothing_is_watched(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    coordinator = await _zone(hass, peak_entity=None)
    assert coordinator._unsub_peak is None


async def test_with_no_schedule_the_lookahead_stays_off_when_nothing_is_shaped(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    _peak(hass, "off")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    assert spy.call_args.kwargs["bands_per_step"] is None


# ----- during a peak -----


@pytest.mark.parametrize("peak_on", [False, True])
async def test_a_peak_runs_plain_hysteresis_even_with_mpc_ready(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    peak_on: bool,
) -> None:
    """MPC, ready and enabled, heats a room its idle drift would take out of
    the band within a quarter of an hour: at 20.3 in a 20-23 band, and -- in
    shadow -- at 19.3 in the 19-24.5 band of a peak, where hysteresis, which is
    in charge, starts nothing until 18.7."""
    freezer.move_to(START)
    now = dt_util.utcnow()
    coordinator = await _zone(
        hass,
        learning_enabled=True,
        mpc_enabled=True,
        persisted_idle_slope=-0.02,
        persisted_idle_slope_at=now.isoformat(),
    )
    coordinator._samples_cache = _heat_run(now, ago_minutes=64, temp=20.0)
    _climate(hass)
    _peak(hass, "on" if peak_on else "off")
    hass.states.async_set(TEMP_ENTITY, "19.3" if peak_on else "20.3", {})
    await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.mpc_ready is True
    assert state.mpc_decision.action == ACTION_HEAT
    if not peak_on:
        assert state.decision.action == ACTION_HEAT
        assert state.peak_state == PEAK_STATE_WAITING
        return
    assert state.peak_state == PEAK_STATE_ACTIVE
    assert (state.effective_low, state.effective_high) == (19.0, 24.5)
    assert (state.peak_low_adjustment, state.peak_high_adjustment) == (-1.0, 1.5)
    assert state.decision.action == ACTION_IDLE
    assert all(c["hvac_mode"] != HVAC_MODE_HEAT for c in _calls(climate_calls, "set_hvac_mode"))


async def test_past_the_relaxed_floor_it_heats_to_the_relaxed_floor(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    coordinator = await _zone(hass)
    _climate(hass)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "18.8", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.decision.action == ACTION_IDLE  # 18.8 is above 19 - 0.3

    hass.states.async_set(TEMP_ENTITY, "18.6", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] == 19.0


async def test_the_relaxed_floor_stops_at_the_units_minimum(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Home Assistant refuses a setpoint below the unit's `min_temp`, and the
    unit would then keep heating to the setpoint it had."""
    coordinator = await _zone(hass, peak_heat_offset=3.0)
    _climate(hass, min_temp=18.0, max_temp=30.0)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "17.5", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.effective_low == 18.0
    assert coordinator.data.peak_low_adjustment == -2.0
    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] == 18.0


async def test_a_peak_releases_a_cycle_mpc_is_holding_on_a_borrowed_slope(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A heat cycle a minute old, planned on the previous cycle's slope: MPC
    would rather idle at 22 in a 20-23 band, but holds the cycle until it has
    a slope of its own (v0.22.0). The peak starting ends it at once."""
    freezer.move_to(START)
    now = dt_util.utcnow()
    coordinator = await _zone(hass, learning_enabled=True, mpc_enabled=True)
    idle_start = now - timedelta(minutes=48)
    coordinator._samples_cache = [
        *_heat_run(now, ago_minutes=60, temp=21.5),
        *[
            Sample(t=idle_start + timedelta(minutes=2 * i), temp=22.0, action=ACTION_IDLE)
            for i in range(23)
        ],
        Sample(t=now - timedelta(minutes=1), temp=22.0, action=ACTION_HEAT),
    ]
    await coordinator._store.async_update_zone(
        "office",
        last_action=ACTION_HEAT,
        previous_action=ACTION_IDLE,
        last_action_at=(now - timedelta(minutes=1)).isoformat(),
        last_active_at=(now - timedelta(minutes=1)).isoformat(),
    )
    _climate(hass, HVAC_MODE_HEAT, temperature=23.0)
    _peak(hass, "off")
    hass.states.async_set(TEMP_ENTITY, "22.0", {})
    await _refresh(hass, coordinator)
    held = coordinator.data
    assert held.thermal_slopes.method_recovery_heat == "previous"
    assert held.decision.action == ACTION_HEAT  # the hold, without a peak

    climate_calls.clear()
    _peak(hass, "on")
    await _settle(hass, freezer, QUIET)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_ACTIVE
    assert state.mpc_decision.action == ACTION_HEAT  # MPC, in shadow, would still hold
    assert state.decision.action == ACTION_IDLE
    assert [c["hvac_mode"] for c in _calls(climate_calls, "set_hvac_mode")] == [HVAC_MODE_FAN_ONLY]


async def test_a_peak_starts_and_ends_on_the_entitys_own_events(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """No refresh but the ones the entities' own changes trigger. In the peak
    the room falls past the relaxed floor and the zone heats to 19. When the
    peak ends inside the minimum cycle, the heat is not re-issued with the
    normal floor until the cycle's minimum has run -- the same-mode gate, as
    for any re-issue -- and then it is."""
    freezer.move_to(START)
    coordinator = await _zone(hass)
    _climate(hass)
    _peak(hass, "off")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_WAITING

    ends = dt_util.utcnow() + timedelta(hours=3)
    _peak(hass, "on", next_event=ends)
    await _settle(hass, freezer, QUIET)
    assert coordinator.data.peak_state == PEAK_STATE_ACTIVE
    assert coordinator.data.peak_ends == ends

    climate_calls.clear()
    hass.states.async_set(TEMP_ENTITY, "18.5", {})
    await _settle(hass, freezer, QUIET)
    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] == 19.0
    _climate(hass, HVAC_MODE_HEAT, temperature=19.0)
    climate_calls.clear()

    _peak(hass, "off")
    await _settle(hass, freezer, QUIET)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_WAITING
    assert state.peak_ends is None
    assert (state.effective_low, state.effective_high) == (20.0, 23.0)
    assert state.decision.action == ACTION_HEAT
    assert _calls(climate_calls, "set_temperature") == []  # held by the same-mode gate

    hass.states.async_set(TEMP_ENTITY, "18.6", {})
    await _settle(hass, freezer, timedelta(minutes=8))
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] == 20.0


async def test_an_override_stands_peak_saving_aside_until_it_ends(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_ACTIVE

    await coordinator.async_start_override(low=22.0, high=23.5, hours=1)
    await hass.async_block_till_done()
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_OVERRIDE
    assert (state.effective_low, state.effective_high) == (22.0, 23.5)
    assert (state.peak_low_adjustment, state.peak_high_adjustment) == (0.0, 0.0)
    assert state.decision.action == ACTION_HEAT  # 21 is below 22 - 0.3

    await _settle(hass, freezer, timedelta(hours=1, seconds=2))
    state = coordinator.data
    assert state.override_active is False
    assert state.peak_state == PEAK_STATE_ACTIVE
    # The override's band stays the manual band once it ends (no schedule),
    # and the peak relaxes that.
    assert (state.effective_low, state.effective_high) == (21.0, 25.0)


@pytest.mark.parametrize("peak_state", [STATE_UNAVAILABLE, STATE_UNKNOWN, "", None])
async def test_a_dead_signal_reads_as_no_peak(
    hass: HomeAssistant, hass_storage: dict[str, Any], peak_state: str | None
) -> None:
    coordinator = await _zone(hass)
    if peak_state is not None:
        _peak(hass, peak_state)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_NO_SIGNAL
    assert (state.effective_low, state.effective_high) == (20.0, 23.0)


# ----- before a peak -----


async def test_pre_heating_raises_the_floor_by_the_heat_offset(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await _recently_heated(coordinator)
    _climate(hass)
    start = dt_util.utcnow() + timedelta(minutes=30)
    _peak(hass, "off", next_event=start)
    hass.states.async_set(TEMP_ENTITY, "20.5", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_PRECONDITIONING
    assert state.peak_next_start == start
    assert state.peak_ends is None
    assert state.peak_precondition == ACTION_HEAT
    assert (state.effective_low, state.effective_high) == (21.0, 23.0)
    assert state.peak_low_adjustment == 1.0
    assert state.decision.action == ACTION_HEAT  # 20.5 is below 21 - 0.3
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] == 21.0
    # No schedule, so the lookahead is built from the band: shaped at every
    # step, since the lead time has begun.
    assert spy.call_args.kwargs["bands_per_step"] == [(21.0, 23.0)] * 60


async def test_pre_cooling_lowers_the_ceiling_by_the_cool_offset(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await _recently_heated(coordinator, action=ACTION_COOL)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=30))
    hass.states.async_set(TEMP_ENTITY, "22.5", {})
    await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_PRECONDITIONING
    assert state.peak_precondition == ACTION_COOL
    assert (state.effective_low, state.effective_high) == (20.0, 21.5)
    assert state.peak_high_adjustment == -1.5


async def test_mpc_sees_the_step_coming(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Ninety minutes out with an hour's lead: the band is as it was now, and
    MPC's lookahead shows the step from minute 30 -- onto each step's own
    scheduled band, through a transition at minute 45."""
    freezer.move_to(START)
    schedule: list[StoredTransition] = [
        {"at": "00:00", "low": 20.0, "high": 23.0},
        {"at": "16:45", "low": 20.5, "high": 23.5},
    ]
    coordinator = await _zone(hass, schedule=schedule)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=90))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_WAITING
    assert (state.effective_low, state.effective_high) == (20.0, 23.0)
    assert state.peak_precondition is None
    assert spy.call_args.kwargs["bands_per_step"] == (
        [(20.0, 23.0)] * 30 + [(21.0, 23.0)] * 15 + [(21.5, 23.5)] * 15
    )
    assert coordinator._unsub_peak_timer is not None


@pytest.mark.parametrize(
    "next_event",
    [
        "garbage",
        "2026-06-15T15:59:00+00:00",  # already passed
    ],
)
async def test_no_usable_start_shapes_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    next_event: str,
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=next_event)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.peak_state == PEAK_STATE_WAITING
    assert state.peak_next_start is None
    assert (state.effective_low, state.effective_high) == (20.0, 23.0)
    assert coordinator._unsub_peak_timer is None


async def test_a_zone_that_has_not_run_for_a_day_does_not_pre_condition(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await coordinator._store.async_update_zone(
        "office",
        last_action=ACTION_IDLE,
        previous_action=ACTION_HEAT,
        # Re-stamped by an idle re-commit a minute ago, as every one is.
        last_action_at=dt_util.utcnow().isoformat(),
        last_active_at=(dt_util.utcnow() - timedelta(hours=25)).isoformat(),
    )
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=30))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    assert coordinator.data.effective_low == 20.0


async def test_a_room_drifting_the_other_way_is_not_pre_conditioned(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """The zone heated an hour ago, but the room is warming on its own now."""
    freezer.move_to(START)
    coordinator = await _zone(
        hass,
        persisted_idle_slope=0.01,
        persisted_idle_slope_at=dt_util.utcnow().isoformat(),
    )
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=30))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.thermal_slopes.idle == 0.01
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    assert coordinator.data.effective_low == 20.0


async def test_the_timer_wakes_a_quiet_zone_at_the_lead_start(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Three hours out with an hour's lead: the timer is capped at an hour,
    re-arms, and the zone steps at the lead start with no sensor report."""
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(hours=3))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    assert coordinator._unsub_peak_timer is not None

    await _settle(hass, freezer, timedelta(hours=1))
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    await _settle(hass, freezer, timedelta(minutes=59))
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    await _settle(hass, freezer, timedelta(minutes=1))
    assert coordinator.data.peak_state == PEAK_STATE_PRECONDITIONING
    assert coordinator.data.effective_low == 21.0
    # Inside the lead time nothing more is armed: the band holds its step
    # until the announced start, and the entity turning on is its own event.
    assert coordinator._unsub_peak_timer is None

    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(hours=3))
    await _settle(hass, freezer, QUIET)
    assert coordinator._unsub_peak_timer is not None
    await coordinator.async_unload()
    assert coordinator._unsub_peak_timer is None


async def test_only_state_and_next_event_changes_refresh(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    coordinator = await _zone(hass)
    later = dt_util.utcnow() + timedelta(hours=2)
    _peak(hass, "off", next_event=later, tariff="shoulder")
    await hass.async_block_till_done()
    with patch.object(coordinator, "_schedule_debounced_refresh") as refresh:
        _peak(hass, "off", next_event=later, tariff="other")
        await hass.async_block_till_done()
        assert refresh.call_count == 0
        _peak(hass, "off", next_event=later + timedelta(days=1), tariff="other")
        await hass.async_block_till_done()
        assert refresh.call_count == 1
        _peak(hass, "on", next_event=later + timedelta(days=1), tariff="other")
        await hass.async_block_till_done()
        assert refresh.call_count == 2


# ----- what the zone records -----


@pytest.mark.parametrize(
    ("action", "rooms"),
    [
        (ACTION_HEAT, ("19.0", "19.2", "20.2", "20.4")),
        (ACTION_COOL, ("24.0", "23.8", "22.8", "22.6")),
    ],
    ids=["heat", "cool"],
)
async def test_last_active_at_marks_heat_and_cool_commits_only(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    action: str,
    rooms: tuple[str, str, str, str],
) -> None:
    """Stamped by a heat or cool commit and by its re-commit once the minimum
    cycle has run; not by the release, nor by an idle re-commit. Rooms: start
    the cycle, keep it running past the minimum cycle, release it, idle on."""
    start, running, released, idling = rooms
    freezer.move_to(START)
    coordinator = await _zone(hass, peak_saving_enabled=False)
    _climate(hass)
    hass.states.async_set(TEMP_ENTITY, start, {})
    await _refresh(hass, coordinator)
    started_at = dt_util.utcnow().isoformat()
    assert coordinator.get_zone_data()["last_action"] == action
    assert coordinator.get_zone_data()["last_active_at"] == started_at

    hass.states.async_set(TEMP_ENTITY, running, {})
    await _settle(hass, freezer, timedelta(minutes=10))
    zone = coordinator.get_zone_data()
    assert zone["last_action"] == action
    recommitted_at = zone["last_action_at"]
    assert zone["last_active_at"] == recommitted_at != started_at

    hass.states.async_set(TEMP_ENTITY, released, {})
    await _settle(hass, freezer, timedelta(minutes=2))
    zone = coordinator.get_zone_data()
    assert zone["last_action"] == ACTION_IDLE
    assert zone["last_action_at"] != recommitted_at  # the release
    assert zone["last_active_at"] == recommitted_at

    hass.states.async_set(TEMP_ENTITY, idling, {})
    await _settle(hass, freezer, timedelta(minutes=10))
    zone = coordinator.get_zone_data()
    assert zone["last_action"] == ACTION_IDLE
    assert zone["last_active_at"] == recommitted_at  # nor an idle re-commit


async def test_releasing_a_stale_cycle_does_not_make_it_recent(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A heat last committed three days ago -- the zone in shadow since, say
    -- released now: the release says nothing about when the zone last heated,
    so the zone doesn't pre-heat on the strength of it."""
    freezer.move_to(START)
    coordinator = await _zone(hass)
    days_ago = (dt_util.utcnow() - timedelta(days=3)).isoformat()
    await coordinator._store.async_update_zone(
        "office",
        last_action=ACTION_HEAT,
        previous_action=ACTION_IDLE,
        last_action_at=days_ago,
        last_active_at=days_ago,
    )
    _climate(hass)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=30))
    hass.states.async_set(TEMP_ENTITY, "21.5", {})
    await _refresh(hass, coordinator)
    assert coordinator.get_zone_data()["last_action"] == ACTION_IDLE
    assert coordinator.get_zone_data()["last_active_at"] == days_ago
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    assert coordinator.data.effective_low == 20.0


# ----- with the zone's other features -----


async def test_a_stand_in_reading_in_a_peak(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """On the climate's own reading the deadbands widen by 0.5 as ever, on
    top of the relaxed band, and nothing is sampled."""
    freezer.move_to(START)
    coordinator = await _zone(hass)
    _peak(hass, "on")
    _climate(hass, current_temperature=18.4)
    hass.states.async_set(TEMP_ENTITY, STATE_UNAVAILABLE, {})
    await _refresh(hass, coordinator)
    await _settle(hass, freezer, timedelta(seconds=FALLBACK_GRACE_S + 2))
    state = coordinator.data
    assert state.room_source == ROOM_SOURCE_CLIMATE
    assert state.peak_state == PEAK_STATE_ACTIVE
    assert state.decision.action == ACTION_IDLE  # 18.4 is above 19 - 0.3 - 0.5

    _climate(hass, current_temperature=18.1)
    await _settle(hass, freezer, QUIET)
    assert coordinator.data.decision.action == ACTION_HEAT
    assert coordinator._samples_cache == []


async def test_shadow_mode_reports_the_peak_and_commands_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    coordinator = await _zone(hass, enabled=False)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "18.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_ACTIVE
    assert coordinator.data.decision.action == ACTION_HEAT
    assert climate_calls == []


@pytest.mark.parametrize(
    ("room", "action"), [("17.0", ACTION_HEAT), ("26.0", ACTION_COOL)], ids=["heat", "cool"]
)
@pytest.mark.parametrize("peak_on", [False, True])
async def test_a_cycle_in_a_peak_gets_no_fan_boost(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    peak_on: bool,
    room: str,
    action: str,
) -> None:
    coordinator = await _zone(
        hass, fan_control_enabled=True, active_fan_mode="high", idle_fan_mode="low"
    )
    _climate(hass, fan_modes=["low", "high"], fan_mode="low")
    _peak(hass, "on" if peak_on else "off")
    hass.states.async_set(TEMP_ENTITY, room, {})
    await _refresh(hass, coordinator)
    assert coordinator.data.decision.action == action
    fans = [c["fan_mode"] for c in _calls(climate_calls, "set_fan_mode")]
    assert fans == ([] if peak_on else ["high"])

    # The idle side is still commanded in a peak.
    _climate(hass, HVAC_MODE_HEAT, fan_modes=["low", "high"], fan_mode="high")
    climate_calls.clear()
    await coordinator._maybe_apply_action(
        idle_decision(),
        True,
        decision_room=21.0,
        fan_boost=not peak_on,
    )
    assert [c["fan_mode"] for c in _calls(climate_calls, "set_fan_mode")] == ["low"]


# ----- wiring: options flow -> coordinator -> entities -----


def _zone_entry() -> MockConfigEntry:
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
    )


async def test_the_options_flow_sets_and_clears_the_peak_entity_without_a_flush(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN].zone_coordinators[entry.entry_id].peak_entity_id is None
    store = hass.data[DOMAIN].store
    sample = {"t": dt_util.utcnow().isoformat(), "temp": 21.0, "action": ACTION_IDLE}
    await store.async_update_zone("office", samples=[sample])

    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert init_result["type"] == FlowResultType.FORM
    assert CONF_PEAK_ENTITY in {str(key) for key in init_result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        init_result["flow_id"], {CONF_TEMP_SENSOR: TEMP_ENTITY, CONF_PEAK_ENTITY: PEAK_ENTITY}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PEAK_ENTITY] == PEAK_ENTITY
    assert hass.data[DOMAIN].zone_coordinators[entry.entry_id].peak_entity_id == PEAK_ENTITY
    assert len(store.get_zone("office")["samples"]) == 1

    # Reopened, the form offers the chosen entity, so saving it unchanged
    # keeps it.
    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    peak_key = next(
        key for key in init_result["data_schema"].schema if str(key) == CONF_PEAK_ENTITY
    )
    assert peak_key.description == {"suggested_value": PEAK_ENTITY}
    await hass.config_entries.options.async_configure(
        init_result["flow_id"], {CONF_TEMP_SENSOR: TEMP_ENTITY, CONF_PEAK_ENTITY: PEAK_ENTITY}
    )
    await hass.async_block_till_done()

    # Emptying the selector clears it.
    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        init_result["flow_id"], {CONF_TEMP_SENSOR: TEMP_ENTITY}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert CONF_PEAK_ENTITY in entry.options
    assert entry.options[CONF_PEAK_ENTITY] is None
    assert hass.data[DOMAIN].zone_coordinators[entry.entry_id].peak_entity_id is None
    assert len(store.get_zone("office")["samples"]) >= 1


async def test_the_entities_switch_tune_and_report_peak_saving(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    await hass.config.async_set_time_zone("UTC")
    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_PEAK_ENTITY: PEAK_ENTITY})
    ends = dt_util.utcnow() + timedelta(hours=3)
    _peak(hass, "on", next_event=ends)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    sensor = hass.states.get("sensor.office_peak_saving")
    assert sensor is not None
    assert sensor.state == PEAK_STATE_OFF
    assert sensor.attributes["options"] == list(PEAK_STATES)
    assert sensor.attributes["peak_entity"] == PEAK_ENTITY
    assert hass.states.get("switch.office_peak_saving_enabled").state == "off"
    assert hass.states.get("number.office_peak_heat_offset").state == "1.0"
    assert hass.states.get("number.office_peak_cool_offset").state == "1.0"
    assert hass.states.get("number.office_peak_precondition_minutes").state == "60"
    for entity_id, bounds in (
        ("number.office_peak_heat_offset", (0.0, 5.0, 0.5)),
        ("number.office_peak_cool_offset", (0.0, 5.0, 0.5)),
        ("number.office_peak_precondition_minutes", (0, 180, 15)),
    ):
        attrs = hass.states.get(entity_id).attributes
        assert (attrs["min"], attrs["max"], attrs["step"]) == bounds, entity_id

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.office_peak_cool_offset", "value": 2.0},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.office_peak_saving_enabled"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("number.office_peak_heat_offset").state == "1.0"
    assert hass.states.get("number.office_peak_cool_offset").state == "2.0"
    sensor = hass.states.get("sensor.office_peak_saving")
    assert sensor.state == PEAK_STATE_ACTIVE
    assert sensor.attributes["ends"] == ends.isoformat()
    assert sensor.attributes["next_start"] is None
    assert sensor.attributes["low_adjustment"] == -1.0
    assert sensor.attributes["high_adjustment"] == 2.0
    assert sensor.attributes["precondition"] is None
    assert hass.states.get("sensor.office_effective_high").state == "24.5"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.office_peak_saving_enabled"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.office_peak_saving").state == PEAK_STATE_OFF
    assert hass.states.get("sensor.office_effective_high").state == "22.5"


async def test_mpc_in_shadow_plans_on_the_relaxed_band_at_every_step(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """The end of a peak is not known ahead, so every step of the shadow plan
    is relaxed, each from its own scheduled band."""
    freezer.move_to(START)
    schedule: list[StoredTransition] = [
        {"at": "00:00", "low": 20.0, "high": 23.0},
        {"at": "16:30", "low": 21.0, "high": 24.0},
    ]
    coordinator = await _zone(hass, schedule=schedule)
    _climate(hass, min_temp=19.5, max_temp=25.0)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_ACTIVE
    # Bounded by the unit's range at every step, as the band in force is.
    assert spy.call_args.kwargs["bands_per_step"] == [(19.5, 24.5)] * 30 + [(20.0, 25.0)] * 30


async def test_mpc_sees_a_pre_cooling_step_coming(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """The cooling side of `test_mpc_sees_the_step_coming`: from the lead start
    the lookahead lowers the ceiling by the cool offset, and leaves the floor."""
    freezer.move_to(START)
    coordinator = await _zone(hass, schedule=[{"at": "00:00", "low": 20.0, "high": 23.0}])
    await _recently_heated(coordinator, action=ACTION_COOL)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=90))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == PEAK_STATE_WAITING
    assert spy.call_args.kwargs["bands_per_step"] == [(20.0, 23.0)] * 30 + [(20.0, 21.5)] * 30


@pytest.mark.parametrize("peak_on", [False, True])
async def test_a_peak_bypasses_the_predictor_too(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    peak_on: bool,
) -> None:
    """A zone on the predictor, MPC off, in a room cooling at 1.8 °C/h: the
    predictor heats ahead of the deadband edge it projects the room crossing
    within its 15-minute lookahead. It does at 20.0 in a 20-23 band, and would
    at 19.0 in the peak's 19-24.5 -- where hysteresis, in charge, waits for
    18.7."""
    freezer.move_to(START)
    now = dt_util.utcnow()
    coordinator = await _zone(hass, learning_enabled=True, lookahead_minutes=15)
    base = now - timedelta(minutes=60)
    coordinator._samples_cache = [
        Sample(t=base + timedelta(minutes=2 * i), temp=21.8 - 0.06 * i, action=ACTION_IDLE)
        for i in range(30)
    ]
    _climate(hass)
    _peak(hass, "on" if peak_on else "off")
    hass.states.async_set(TEMP_ENTITY, "19.0" if peak_on else "20.0", {})
    await _refresh(hass, coordinator)
    state = coordinator.data
    assert state.thermal_slopes.idle == pytest.approx(-0.03)
    assert state.predicted_decision.action == ACTION_HEAT
    if not peak_on:
        assert state.decision.action == ACTION_HEAT
        return
    assert state.peak_state == PEAK_STATE_ACTIVE
    assert state.decision.action == ACTION_IDLE
    assert all(c["hvac_mode"] != HVAC_MODE_HEAT for c in _calls(climate_calls, "set_hvac_mode"))


@pytest.mark.parametrize("peak_saving", [False, True])
@pytest.mark.parametrize(
    "next_event",
    ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59"],
)
async def test_a_next_event_with_no_utc_equivalent_leaves_the_zone_controlling(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    next_event: str,
    peak_saving: bool,
) -> None:
    """A template's "never" sentinel at the end of the datetime range: the
    refresh must still succeed, and simply see no announced start."""
    coordinator = await _zone(hass, peak_saving_enabled=peak_saving)
    await hass.config.async_set_time_zone("America/New_York")
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=next_event)
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.last_update_success is True
    assert coordinator.data.peak_state == (PEAK_STATE_WAITING if peak_saving else PEAK_STATE_OFF)
    assert coordinator.data.peak_next_start is None


async def test_a_switched_off_zone_takes_nothing_from_the_peak_entity(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Neither reads it on a refresh nor refreshes on its changes -- until it
    is switched on."""
    coordinator = await _zone(hass, peak_saving_enabled=False)
    _peak(hass, "off")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await hass.async_block_till_done()
    with (
        patch.object(coordinator, "_schedule_debounced_refresh") as refresh,
        patch.object(coordinator, "_read_peak_signal", wraps=coordinator._read_peak_signal) as read,
    ):
        _peak(hass, "on")
        await hass.async_block_till_done()
        await _refresh(hass, coordinator)
        assert refresh.call_count == 0
        assert read.call_count == 0

        await coordinator.async_set_peak_saving_enabled(True)
        await hass.async_block_till_done()
        assert read.call_count == 1
        assert coordinator.data.peak_state == PEAK_STATE_ACTIVE
        _peak(hass, "off")
        await hass.async_block_till_done()
        assert refresh.call_count == 1


async def test_a_peak_entity_appearing_or_going_refreshes_the_zone(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    coordinator = await _zone(hass)
    with patch.object(coordinator, "_schedule_debounced_refresh") as refresh:
        _peak(hass, "off")
        await hass.async_block_till_done()
        assert refresh.call_count == 1
        hass.states.async_remove(PEAK_ENTITY)
        await hass.async_block_till_done()
        assert refresh.call_count == 2


async def test_unload_stops_watching_the_peak_entity(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    coordinator = await _zone(hass)
    await coordinator.async_unload()
    assert coordinator._unsub_peak is None
    with patch.object(coordinator, "_schedule_debounced_refresh") as refresh:
        _peak(hass, "on")
        await hass.async_block_till_done()
        assert refresh.call_count == 0


async def test_the_peak_timer_is_cancelled_before_it_is_re_armed(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """One pending wake at a time, however many refreshes re-arm it."""
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(hours=3))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    first = coordinator._unsub_peak_timer
    assert first is not None
    with patch(
        "custom_components.comfort_band.coordinator.async_call_later", return_value=MagicMock()
    ) as arm:
        cancelled = MagicMock(wraps=first)
        coordinator._unsub_peak_timer = cancelled
        await _refresh(hass, coordinator)
        cancelled.assert_called_once_with()
        peak_arms = [c for c in arm.call_args_list if c.args[2] == coordinator._on_peak_timer_fire]
        assert len(peak_arms) == 1
        assert peak_arms[0].args[1] == 3600
    coordinator._unsub_peak_timer = None


async def test_a_naive_next_event_is_read_in_local_time(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await hass.config.async_set_time_zone("Europe/Berlin")
    _peak(hass, "off", next_event="2026-06-15T19:30:00")  # 17:30 UTC in summer
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_next_start == datetime(2026, 6, 15, 17, 30, tzinfo=UTC)


async def test_a_running_cycle_sets_the_direction(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """While a heat cycle runs, the cycle before it -- here a cool -- does not
    decide the direction; the one running does."""
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await coordinator._store.async_update_zone(
        "office",
        last_action=ACTION_HEAT,
        previous_action=ACTION_COOL,
        last_action_at=dt_util.utcnow().isoformat(),
        last_active_at=dt_util.utcnow().isoformat(),
    )
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=30))
    hass.states.async_set(TEMP_ENTITY, "21.5", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_precondition == ACTION_HEAT
    assert coordinator.data.effective_low == 21.0


async def test_the_adjustments_are_rounded(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A floor clamped to the unit's minimum moves by a difference the floats
    can't hold exactly; the sensor reports it to a thousandth."""
    coordinator = await _zone(hass, manual_low=20.1, peak_heat_offset=3.0)
    _climate(hass, min_temp=18.5)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.effective_low == 18.5
    assert coordinator.data.peak_low_adjustment == -1.6


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "warm", None])
async def test_an_unreadable_unit_range_bounds_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any], bad: object
) -> None:
    coordinator = await _zone(hass)
    _climate(hass, min_temp=bad, max_temp=bad)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert (coordinator.data.effective_low, coordinator.data.effective_high) == (19.0, 24.5)


@pytest.mark.parametrize(
    ("min_temp", "max_temp", "step", "band"),
    [
        (16.2, 30.0, None, (16.5, 23.5)),  # the default 0.5 step
        (16.2, 23.4, 0.5, (16.5, 23.0)),
        (16.0, 30.0, 0.5, (16.0, 23.5)),  # already on the step: unchanged
        (16.2, 30.0, 1.0, (17.0, 23.5)),
    ],
)
async def test_the_units_range_is_moved_in_to_a_whole_step(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    min_temp: float,
    max_temp: float,
    step: float | None,
    band: tuple[float, float],
) -> None:
    """The setpoint is rounded to the unit's step before it is sent, so a
    limit off the step would let rounding put it outside the range again."""
    coordinator = await _zone(
        hass, manual_low=17.0, manual_high=22.0, peak_heat_offset=2.0, peak_cool_offset=1.5
    )
    attrs: dict[str, Any] = {"min_temp": min_temp, "max_temp": max_temp}
    if step is not None:
        attrs["target_temp_step"] = step
    _climate(hass, **attrs)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "15.0", {})
    await _refresh(hass, coordinator)
    assert (coordinator.data.effective_low, coordinator.data.effective_high) == band
    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] == band[0]


@pytest.mark.parametrize("peak_saving", [False, True])
async def test_a_unit_range_too_large_for_a_float_leaves_the_zone_controlling(
    hass: HomeAssistant, hass_storage: dict[str, Any], peak_saving: bool
) -> None:
    """Read only while peak saving is on, and harmless when it is."""
    coordinator = await _zone(hass, peak_saving_enabled=peak_saving)
    _climate(hass, min_temp=10**400, max_temp=-(10**400))
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    with patch.object(
        coordinator, "_climate_temp_limits", wraps=coordinator._climate_temp_limits
    ) as limits:
        await _refresh(hass, coordinator)
    assert coordinator.last_update_success is True
    assert limits.call_count == (1 if peak_saving else 0)
    expected = (19.0, 24.5) if peak_saving else (20.0, 23.0)
    assert (coordinator.data.effective_low, coordinator.data.effective_high) == expected


async def test_switching_off_or_withdrawing_the_start_cancels_the_wake(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(hours=3))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator._unsub_peak_timer is not None
    _peak(hass, "off")  # the announcement withdrawn
    await _refresh(hass, coordinator)
    assert coordinator._unsub_peak_timer is None

    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(hours=3))
    await _refresh(hass, coordinator)
    assert coordinator._unsub_peak_timer is not None
    await coordinator.async_set_peak_saving_enabled(False)
    await hass.async_block_till_done()
    assert coordinator._unsub_peak_timer is None


async def test_with_no_schedule_the_lookahead_matches_the_horizon(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass, mpc_horizon_minutes=30)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=70))
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    with _spy_plan() as spy:
        await _refresh(hass, coordinator)
    assert spy.call_args.kwargs["bands_per_step"] == [(20.0, 23.0)] * 10 + [(21.0, 23.0)] * 20


async def test_the_sensor_reports_pre_conditioning_and_feedback_records_the_state(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    await hass.config.async_set_time_zone("UTC")
    entry = _zone_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_PEAK_ENTITY: PEAK_ENTITY})
    start = dt_util.utcnow() + timedelta(minutes=30)
    _peak(hass, "off", next_event=start)
    hass.states.async_set(TEMP_ENTITY, "21.5", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = hass.data[DOMAIN].store
    await store.async_update_zone(
        "office",
        last_action=ACTION_IDLE,
        previous_action=ACTION_HEAT,
        last_active_at=dt_util.utcnow().isoformat(),
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.office_peak_saving_enabled"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.office_peak_saving_enabled").state == "on"
    sensor = hass.states.get("sensor.office_peak_saving")
    assert sensor.state == PEAK_STATE_PRECONDITIONING
    assert sensor.attributes["next_start"] == start.isoformat()
    assert sensor.attributes["precondition"] == ACTION_HEAT
    assert sensor.attributes["ends"] is None
    assert sensor.attributes["low_adjustment"] == 1.0

    await hass.services.async_call(
        DOMAIN, "record_feedback", {"zone": "office", "label": "too_hot"}, blocking=True
    )
    (entry_logged,) = hass.data[DOMAIN].feedback_store.get_entries("office")
    assert entry_logged["peak_state"] == PEAK_STATE_PRECONDITIONING


@pytest.mark.parametrize(
    ("lead", "minutes_out", "expected"),
    [
        (30, 45, PEAK_STATE_WAITING),
        (30, 25, PEAK_STATE_PRECONDITIONING),
        (120, 90, PEAK_STATE_PRECONDITIONING),
        (120, 125, PEAK_STATE_WAITING),
    ],
)
async def test_the_lead_time_is_the_zones_own(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    lead: int,
    minutes_out: int,
    expected: str,
) -> None:
    freezer.move_to(START)
    coordinator = await _zone(hass, peak_precondition_minutes=lead)
    await _recently_heated(coordinator)
    _peak(hass, "off", next_event=dt_util.utcnow() + timedelta(minutes=minutes_out))
    hass.states.async_set(TEMP_ENTITY, "21.5", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.peak_state == expected


@pytest.mark.parametrize(
    ("max_temp", "step", "high"),
    [
        # 302 * 0.1 is 30.200000000000003, which Home Assistant refuses against
        # a maximum of 30.2: the ceiling steps in to the next setpoint down.
        (30.2, 0.1, 30.1),
        (29.9, 0.1, 29.8),
        (29.4, 0.2, 29.2),
        (30.0, 0.5, 30.0),
    ],
)
async def test_the_relaxed_ceiling_is_a_setpoint_the_unit_takes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    max_temp: float,
    step: float,
    high: float,
) -> None:
    coordinator = await _zone(hass, manual_high=26.0, peak_cool_offset=5.0)
    _climate(hass, min_temp=16.0, max_temp=max_temp, target_temp_step=step)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "32.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.effective_high == high
    assert coordinator.data.decision.action == ACTION_COOL
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] <= max_temp


@pytest.mark.parametrize(
    ("min_temp", "low"),
    [
        # 16.8 / 0.3 is 56.00000000000001: taken at face value, the floor would
        # move up a whole step, to 17.1, though 16.8 is itself a setpoint.
        (16.8, 16.8),
        # 51 * 0.3 is 15.299999999999999, which Home Assistant refuses against
        # a minimum of 15.3: the floor steps in to the next setpoint up.
        (15.3, 15.6),
    ],
)
async def test_the_relaxed_floor_is_the_lowest_setpoint_the_unit_takes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    climate_calls: list[tuple[str, dict[str, Any]]],
    min_temp: float,
    low: float,
) -> None:
    coordinator = await _zone(hass, manual_low=18.0, peak_heat_offset=3.0)
    _climate(hass, min_temp=min_temp, max_temp=30.0, target_temp_step=0.3)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "14.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.data.effective_low == low
    assert coordinator.data.decision.action == ACTION_HEAT
    assert _calls(climate_calls, "set_temperature")[-1]["temperature"] >= min_temp


@pytest.mark.parametrize("step", [10**400, 1e-310, 0, -0.5])
async def test_an_unusable_step_leaves_the_range_as_published(
    hass: HomeAssistant, hass_storage: dict[str, Any], step: object
) -> None:
    coordinator = await _zone(hass, manual_low=17.0, peak_heat_offset=2.0)
    _climate(hass, min_temp=16.2, max_temp=30.0, target_temp_step=step)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.0", {})
    await _refresh(hass, coordinator)
    assert coordinator.last_update_success is True
    assert coordinator.data.effective_low == 16.2


async def test_the_peak_entity_can_be_any_on_off_entity(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The README's setups need each of these: a Threshold helper or template
    binary sensor, an input_boolean or switch an automation flips, and a
    schedule helper."""
    from custom_components.comfort_band.config_flow import _PEAK_SELECTOR

    assert sorted(_PEAK_SELECTOR.config["domain"]) == [
        "binary_sensor",
        "input_boolean",
        "schedule",
        "switch",
    ]


@pytest.mark.parametrize(
    ("attrs", "band"),
    [
        # Limits so large that dividing by the step overflows: left as
        # published, and since relaxation never narrows the band, that side
        # simply doesn't relax.
        ({"min_temp": 1e308, "max_temp": -1e308}, (20.0, 23.0)),
        ({"min_temp": 16.0, "max_temp": 30.0, "target_temp_step": 1e-320}, (19.0, 24.5)),
    ],
)
async def test_unit_limits_that_overflow_the_alignment_leave_the_zone_controlling(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    attrs: dict[str, Any],
    band: tuple[float, float],
) -> None:
    coordinator = await _zone(hass)
    _climate(hass, **attrs)
    _peak(hass, "on")
    hass.states.async_set(TEMP_ENTITY, "21.5", {})
    await _refresh(hass, coordinator)
    assert coordinator.last_update_success is True
    assert (coordinator.data.effective_low, coordinator.data.effective_high) == band
