"""Tests for the v0.8 model-predictive controller.

Each test pins one behaviour of `enumerate_actions`, `simulate`, `is_ready`,
or `plan` — review-feedback discipline from v0.7.1: prefer many small tests
that each fail one guard over a few large tests that confound failure modes.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
)
from custom_components.comfort_band.hysteresis import (
    UNKNOWN_DECISION,
    HysteresisInputs,
    heat_decision,
    idle_decision,
)
from custom_components.comfort_band.mpc import (
    Action,
    enumerate_actions,
    is_ready,
    plan,
    simulate,
)
from custom_components.comfort_band.predictor import ThermalSlopes

_T0 = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _slopes(
    *,
    idle: float | None = 0.0,
    recovery_heat: float | None = 0.05,
    recovery_cool: float | None = -0.05,
    sample_count: int = 30,
) -> ThermalSlopes:
    """Build a ThermalSlopes with all three slopes present by default. Slopes
    are °C/minute (matching the estimator's native unit).
    """
    return ThermalSlopes(
        idle=idle,
        recovery_heat=recovery_heat,
        recovery_cool=recovery_cool,
        sample_count=sample_count,
        window_minutes=30.0,
        last_updated=_T0,
    )


def _inputs(
    room: float | None,
    *,
    low: float = 20.0,
    high: float = 23.0,
    db_below: float = 0.3,
    db_above: float = 0.5,
    current: str = ACTION_IDLE,
) -> HysteresisInputs:
    return HysteresisInputs(
        room=room,
        low=low,
        high=high,
        deadband_below=db_below,
        deadband_above=db_above,
        current_action=current,
    )


# ----- Action dataclass -----


def test_action_dataclass_is_frozen() -> None:
    a = Action(ACTION_HEAT, 23.0)
    with pytest.raises(FrozenInstanceError):
        a.kind = ACTION_COOL  # type: ignore[misc]


def test_action_dataclass_equality() -> None:
    assert Action(ACTION_IDLE, None) == Action(ACTION_IDLE, None)
    assert Action(ACTION_HEAT, 23.0) != Action(ACTION_HEAT, 22.0)


# ----- enumerate_actions -----


def test_enumerate_actions_returns_three_candidates() -> None:
    actions = enumerate_actions(_inputs(21.5))
    assert len(actions) == 3
    kinds = [a.kind for a in actions]
    assert ACTION_IDLE in kinds
    assert ACTION_HEAT in kinds
    assert ACTION_COOL in kinds


def test_enumerate_actions_heat_targets_band_high_edge() -> None:
    """v0.8 design: heat targets `inputs.high`, not `inputs.low`. Drives the
    climate to fill the band; cost function decides release timing."""
    actions = enumerate_actions(_inputs(21.5, low=20.0, high=23.0))
    heat = next(a for a in actions if a.kind == ACTION_HEAT)
    assert heat.target_temp == 23.0


def test_enumerate_actions_cool_targets_band_low_edge() -> None:
    actions = enumerate_actions(_inputs(21.5, low=20.0, high=23.0))
    cool = next(a for a in actions if a.kind == ACTION_COOL)
    assert cool.target_temp == 20.0


def test_enumerate_actions_idle_has_no_target_temp() -> None:
    actions = enumerate_actions(_inputs(21.5))
    idle = next(a for a in actions if a.kind == ACTION_IDLE)
    assert idle.target_temp is None


# ----- simulate -----


def test_simulate_idle_flat_slope_at_midpoint_full_horizon_in_band() -> None:
    """Room sits at the band midpoint with a zero slope. Every step stays
    in band, so time_in_band equals the full horizon."""
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.0),
        _inputs(21.5),
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes == pytest.approx(20.0)


def test_simulate_idle_drift_down_partial_in_band() -> None:
    """Room at 20.45 drifting down at -0.1 °C/min crosses low=20 mid-step
    during iteration 4. Iters 0-3 both in band (4 x 1.0 = 4.0); iter 4
    straddles (0.5); iters 5-19 both out. Total = 4.5.

    Crossing happens mid-step (room=20.45 means iter 4 spans 20.05 → 19.95)
    so the test result is robust against float imprecision near the edge.
    """
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=-0.1),
        _inputs(20.45, low=20.0, high=23.0),
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes == pytest.approx(4.5)


def test_simulate_heat_full_horizon_when_recovery_keeps_room_in_band() -> None:
    """Room at 20.0 (low edge) heated at +0.05 °C/min reaches 21.0 by min 20 —
    still inside band [20, 23]. All 20 minutes count."""
    score = simulate(
        Action(ACTION_HEAT, 23.0),
        _slopes(recovery_heat=0.05),
        _inputs(20.0, low=20.0, high=23.0),
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes == pytest.approx(20.0)


def test_simulate_heat_with_negative_recovery_scores_below_horizon() -> None:
    """Pathological case: room sits at midpoint but the recovery_heat slope
    is negative (broken HVAC, sun load overpowering AC heat). The heat action
    drives the room out of band — the score should be less than the full
    horizon. Pins that simulate doesn't blindly score "heat" as in-band."""
    score = simulate(
        Action(ACTION_HEAT, 23.0),
        _slopes(recovery_heat=-0.2),
        _inputs(20.5, low=20.0, high=23.0),
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes < 20.0


def test_simulate_starting_outside_band_counts_time_after_return() -> None:
    """Room at 19.55 (below low=20) with idle slope +0.1 °C/min crosses low
    mid-step during iteration 4 (spans 19.95 → 20.05). Iters 0-3 both out;
    iter 4 straddles (0.5); iters 5-19 both in (15 x 1.0 = 15). Total = 15.5.

    Crossing happens mid-step (room=19.55) so the assertion is robust
    against float imprecision near the band edge.
    """
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.1),
        _inputs(19.55, low=20.0, high=23.0),
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes == pytest.approx(15.5)


def test_simulate_end_temp_reflects_projection() -> None:
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.05),
        _inputs(21.0),
        horizon_minutes=20,
    )
    # 21.0 + 0.05 * 20 = 22.0
    assert score.end_temp == pytest.approx(22.0)


def test_simulate_midpoint_distance_uses_band_midpoint() -> None:
    """Band [20, 23] midpoint is 21.5. End-temp 22.0 → distance 0.5."""
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.05),
        _inputs(21.0, low=20.0, high=23.0),
        horizon_minutes=20,
    )
    assert score.midpoint_distance == pytest.approx(0.5)


def test_simulate_returns_zero_score_when_action_slope_unavailable() -> None:
    """Defensive branch: if a per-action slope is None when simulate is
    called directly (bypassing `plan`'s `is_ready` gate), the score is zero
    and end_temp falls back to the input room. Future-proofs against an
    expanded action space (e.g., v0.9 fan-mode candidates) where some
    candidates' slopes may legitimately be unavailable while others aren't.
    """
    score = simulate(
        Action(ACTION_HEAT, 23.0),
        _slopes(recovery_heat=None),
        _inputs(21.5, low=20.0, high=23.0),
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes == 0.0
    assert score.end_temp == 21.5  # falls back to input room
    assert score.midpoint_distance == pytest.approx(0.0)  # 21.5 == midpoint


def test_simulate_returns_zero_score_when_room_is_none() -> None:
    """Symmetric defensive branch: `plan` already gates on `room is not None`
    before calling simulate, but simulate must not raise when called
    directly with room=None — preserves the "controller never crashes"
    contract."""
    inputs = HysteresisInputs(
        room=None,
        low=20.0,
        high=23.0,
        deadband_below=0.3,
        deadband_above=0.5,
        current_action=ACTION_IDLE,
    )
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.0),
        inputs,
        horizon_minutes=20,
    )
    assert score.time_in_band_minutes == 0.0
    assert score.end_temp == 21.5  # falls back to midpoint
    assert score.midpoint_distance == 0.0


# ----- is_ready -----


def test_is_ready_true_when_all_slopes_present() -> None:
    assert is_ready(_slopes(idle=0.0, recovery_heat=0.05, recovery_cool=-0.05)) is True


def test_is_ready_false_when_idle_slope_missing() -> None:
    assert is_ready(_slopes(idle=None, recovery_heat=0.05, recovery_cool=-0.05)) is False


def test_is_ready_false_when_recovery_heat_missing() -> None:
    assert is_ready(_slopes(idle=0.0, recovery_heat=None, recovery_cool=-0.05)) is False


def test_is_ready_false_when_recovery_cool_missing() -> None:
    assert is_ready(_slopes(idle=0.0, recovery_heat=0.05, recovery_cool=None)) is False


# ----- plan -----


def test_plan_returns_unknown_when_room_is_none() -> None:
    result = plan(
        _slopes(),
        _inputs(None),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result == UNKNOWN_DECISION


def test_plan_falls_back_to_predictor_when_idle_slope_missing() -> None:
    """Cold start: idle slope hasn't accumulated SLOPE_MIN_SAMPLES yet. MPC
    silently defers to the predictor's decision (the caller is expected to
    look at `mpc_ready` to know why)."""
    predictor_decision = heat_decision(20.0)
    result = plan(
        _slopes(idle=None),
        _inputs(21.5),
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_falls_back_to_predictor_when_recovery_heat_missing() -> None:
    predictor_decision = idle_decision()
    result = plan(
        _slopes(recovery_heat=None),
        _inputs(21.5),
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_falls_back_to_predictor_when_recovery_cool_missing() -> None:
    predictor_decision = idle_decision()
    result = plan(
        _slopes(recovery_cool=None),
        _inputs(21.5),
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_picks_idle_when_idle_stays_in_band_longer_than_heat_or_cool() -> None:
    """Synthetic scenario: room at midpoint, idle slope flat (full horizon in
    band), heat slope drives out the top, cool slope drives out the bottom.
    MPC should pick idle.
    """
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.2, recovery_cool=-0.2),
        _inputs(21.5, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=heat_decision(20.0),  # would be wrong if MPC isn't running
    )
    assert result == idle_decision()


def test_plan_picks_heat_when_heat_stays_in_band_longest() -> None:
    """Room close to the low edge with a falling idle slope (drift out the
    bottom soon). Heat slope is gentle (stays in band). MPC picks heat.
    """
    result = plan(
        # idle drift will cross low=20 within the horizon; heat stays in.
        _slopes(idle=-0.1, recovery_heat=0.02, recovery_cool=-0.05),
        _inputs(20.5, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_HEAT
    # v0.8 design: heat targets the band's high edge.
    assert result.target_temp == 23.0
    assert result.target_mode == HVAC_MODE_HEAT


def test_plan_picks_cool_when_cool_stays_in_band_longest() -> None:
    """Symmetric to the heat case: room near high edge, rising idle drift,
    gentle cool slope."""
    result = plan(
        _slopes(idle=0.1, recovery_heat=0.05, recovery_cool=-0.02),
        _inputs(22.5, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_COOL
    assert result.target_temp == 20.0
    assert result.target_mode == HVAC_MODE_COOL


def test_plan_tie_broken_by_midpoint_distance() -> None:
    """Two candidates tie on time_in_band (both full horizon). The one whose
    end-of-horizon temp is closer to the band midpoint wins.

    Room at 21.0; idle (slope=0) ends at 21.0 → distance 0.5 from midpoint
    21.5. Heat (slope=+0.025) ends at 21.5 → distance 0.0. Heat wins despite
    both scoring 20.0 min in band.
    """
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.025, recovery_cool=-0.025),
        _inputs(21.0, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_HEAT


def test_plan_tie_broken_by_midpoint_distance_cool_side() -> None:
    """Symmetric to the heat tie-break: room at 22.0 (above midpoint 21.5);
    idle (slope=0) ends at 22.0 → distance 0.5. Cool (slope=-0.025) ends at
    21.5 → distance 0.0. Cool wins despite both scoring 20.0 min in band.

    The `max` key sort is symmetric on `midpoint_distance`, but having both
    sides explicitly tested protects against a future regression that picks
    only one direction (e.g., a sign-flip in the key).
    """
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.025, recovery_cool=-0.025),
        _inputs(22.0, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_COOL


def test_plan_returns_idle_decision_with_fan_only_mode() -> None:
    """When MPC picks idle, returns the canonical idle_decision() shape —
    target_mode=fan_only, target_temp=None. Confirms MPC uses the shared
    hysteresis decision constructors, not a private one."""
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.2, recovery_cool=-0.2),
        _inputs(21.5),
        horizon_minutes=20,
        predictor_decision=heat_decision(20.0),
    )
    assert result.target_mode == HVAC_MODE_FAN_ONLY
    assert result.target_temp is None
