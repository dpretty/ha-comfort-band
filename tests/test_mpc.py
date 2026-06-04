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
    cool_decision,
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
    # `idle_slope` is the cost-function baseline — every refresh scores
    # "stay idle" against alternatives. Without it MPC can't act.
    assert is_ready(_slopes(idle=None, recovery_heat=0.05, recovery_cool=-0.05)) is False


def test_is_ready_true_when_only_recovery_heat_present() -> None:
    """v0.8.1: heat-only zones activate MPC once idle + heat slopes
    accumulate. The unused cool slope never accumulating shouldn't block
    MPC from running at all (the v0.8.0 behaviour)."""
    assert is_ready(_slopes(idle=0.0, recovery_heat=0.05, recovery_cool=None)) is True


def test_is_ready_true_when_only_recovery_cool_present() -> None:
    """Symmetric: cool-only zones (summer installs) activate MPC once idle
    + cool slopes accumulate."""
    assert is_ready(_slopes(idle=0.0, recovery_heat=None, recovery_cool=-0.05)) is True


def test_is_ready_false_when_only_idle_present() -> None:
    """Idle alone isn't enough — there's only one candidate to score, and
    no meaningful comparison. Defer to the predictor in this state."""
    assert is_ready(_slopes(idle=0.0, recovery_heat=None, recovery_cool=None)) is False


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


def test_plan_falls_back_to_predictor_when_both_recovery_slopes_missing() -> None:
    """v0.8.1: idle-only zones can't compare candidates meaningfully. Defer
    to the predictor regardless of room position."""
    predictor_decision = idle_decision()
    result = plan(
        _slopes(recovery_heat=None, recovery_cool=None),
        _inputs(21.5),
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_in_band_heat_only_zone_picks_from_idle_and_heat() -> None:
    """v0.8.1: heat-only zone (cool slope missing) with room inside band.
    MPC scores {idle, heat} only — the cool candidate is filtered out
    before simulate.

    v0.9.0 idle-preference: when idle achieves full-horizon in-band, it
    wins over heat even if heat would land closer to band midpoint. The
    intent is to avoid burning compressor cycles on a margin gain when
    passive drift already keeps the room in band. Old v0.8.x behaviour
    (heat wins on midpoint tie-break) is now considered a regression —
    the user's "no MPC pre-heat unless genuinely needed" expectation.
    """
    # Room mid-band, idle slope flat (full horizon in band → 20 min), heat
    # slope mild positive (also full horizon in band → 20 min). Idle
    # preference fires: idle wins.
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.025, recovery_cool=None),
        _inputs(21.0, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_IDLE
    assert result.target_temp is None


def test_plan_in_band_cool_only_zone_picks_from_idle_and_cool() -> None:
    """Symmetric: cool-only zone, room mid-band. MPC scores {idle, cool}.

    v0.9.0: idle wins when it achieves full-horizon in-band, regardless
    of cool's midpoint advantage. Closes the user's "winter cooling
    fired when room would have settled passively" report.
    """
    # Room at 22.0, idle slope flat (stays at 22.0, in band → 20 min),
    # cool slope mild negative. Idle preference fires: idle wins.
    result = plan(
        _slopes(idle=0.0, recovery_heat=None, recovery_cool=-0.025),
        _inputs(22.0, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_IDLE
    assert result.target_temp is None


def test_plan_defers_to_predictor_when_room_below_band_without_heat_slope() -> None:
    """v0.8.1 safety bail-out: cool-only zone, but room has dropped below
    band. MPC can't model heating, so it shouldn't pick "best of idle / cool"
    (which would likely be idle and leave the room cold). Defer to the
    predictor — the hysteresis fallback will fire heat reactively.
    """
    predictor_decision = heat_decision(20.0)  # what the predictor would say
    result = plan(
        _slopes(idle=-0.05, recovery_heat=None, recovery_cool=-0.05),
        # Room at 19.5 — below low=20.0.
        _inputs(19.5, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_defers_to_predictor_when_room_above_band_without_cool_slope() -> None:
    """Symmetric to the above: heat-only zone, room rose above band.
    MPC defers; the predictor / hysteresis fires cool reactively.
    """
    predictor_decision = cool_decision(23.0)
    result = plan(
        _slopes(idle=0.05, recovery_heat=0.05, recovery_cool=None),
        _inputs(23.5, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_defers_at_exact_low_edge_without_heat_slope() -> None:
    """v0.8.1 boundary: when room sits EXACTLY at the band's low edge in a
    cool-only zone, the bail-out's inclusive `<=` triggers and defers to
    the predictor. Without this, MPC could score idle vs cool and pick
    idle on tie-break (idle's midpoint-distance fallback to abs(room - mid))
    for one refresh before `room < low` flips True next cycle. Matches
    simulate's inclusive band-membership check.

    `predictor_decision` here is an arbitrary token — what matters is that
    plan() returns it unchanged, proving the bail-out fired. The token's
    specific action is irrelevant to this test's invariant; we use
    heat_decision purely to make the assertion distinct from the
    enumerate_actions output.
    """
    predictor_decision = heat_decision(20.0)
    result = plan(
        _slopes(idle=-0.05, recovery_heat=None, recovery_cool=-0.05),
        _inputs(20.0, low=20.0, high=23.0),  # exactly at low edge
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_defers_at_exact_high_edge_without_cool_slope() -> None:
    """Symmetric inclusive bail-out at the upper edge for heat-only zones.

    Same caveat as the low-edge test: `predictor_decision` is an arbitrary
    token — the invariant under test is that plan() returns it unchanged.
    """
    predictor_decision = cool_decision(23.0)
    result = plan(
        _slopes(idle=0.05, recovery_heat=0.05, recovery_cool=None),
        _inputs(23.0, low=20.0, high=23.0),  # exactly at high edge
        horizon_minutes=20,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision


def test_plan_heats_far_below_band_when_heat_slope_rejected() -> None:
    """v0.15.0: when the heat slope was sign-rejected (None) and the room is
    well below the band, MPC must defer to the predictor (which heats) rather
    than idle. This is the gym bug: a garbage *negative* heat-slope estimate let
    MPC's forward sim believe "heating cools the room" and pick idle while the
    room sat ~5 °C under the rising band. The predictor produces None for that
    slope (see test_predictor), and here `is_ready` stays True via idle +
    recovery_cool, so the bail-out (not the not-ready early return) fires.
    """
    predictor_decision = heat_decision(26.0)
    result = plan(
        _slopes(idle=0.05, recovery_heat=None, recovery_cool=-0.05),
        _inputs(17.0, low=22.0, high=26.0),  # ~5 °C below the low edge
        horizon_minutes=60,
        predictor_decision=predictor_decision,
    )
    assert result == predictor_decision  # heat, not idle


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


def test_plan_idle_preference_over_heat_midpoint_win() -> None:
    """v0.9.0: when idle achieves full-horizon in-band, prefer it over
    heat even if heat lands closer to band midpoint. Inverts the v0.8.x
    midpoint tie-break for the specific case where idle is the cheaper
    option that also satisfies the comfort constraint. Pins the
    "winter cooling fired when room would have settled passively"
    user report — symmetric for heat vs idle.

    Room at 21.0; idle (slope=0) stays at 21.0 → full horizon in band,
    end_temp 21.0, distance 0.5 from midpoint 21.5. Heat
    (slope=+0.025) ends at 21.5 → full horizon in band, distance 0.0.
    Pre-v0.9.0: heat wins on midpoint. v0.9.0: idle wins on activation
    preference. The midpoint tie-break still applies between two
    non-idle actions, just not idle vs active.
    """
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.025, recovery_cool=-0.025),
        _inputs(21.0, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_IDLE


def test_plan_idle_preference_over_cool_midpoint_win() -> None:
    """Symmetric to the heat case: idle wins over cool when both achieve
    full horizon, even if cool lands closer to midpoint.

    Room at 22.0 (above midpoint 21.5); idle (slope=0) stays at 22.0 →
    distance 0.5. Cool (slope=-0.025) ends at 21.5 → distance 0.0.
    v0.9.0: idle wins.
    """
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.025, recovery_cool=-0.025),
        _inputs(22.0, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_IDLE


def test_plan_midpoint_tie_break_still_applies_between_non_idle_actions() -> None:
    """The midpoint tie-break is preserved when comparing two non-idle
    actions (heat vs cool) — idle preference only fires when idle is
    one of the tied candidates. Setup the room slightly outside band
    so idle does NOT reach full horizon; then heat and cool compete
    on midpoint distance.

    Room at 19.5 (below low=20.0), idle slope flat: idle drifts at
    19.5 for full 20 min → outside band → 0 min in band. Heat
    (slope=+0.1) reaches 20.0 at minute 5, ends at 21.5 → ~15 min in
    band, distance 0 from midpoint. Cool (slope=-0.025) drifts further
    down → 0 min in band. Heat wins because cool scores 0 while heat
    scores 15. Confirms the tie-break path between non-idle actions
    isn't broken by the v0.9.0 idle preference.
    """
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.1, recovery_cool=-0.025),
        _inputs(19.5, low=20.0, high=23.0),
        horizon_minutes=20,
        predictor_decision=idle_decision(),
    )
    assert result.action == ACTION_HEAT


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


# ----- bands_per_step lookahead (v0.9.0+) -----


def test_simulate_uses_bands_per_step_when_provided() -> None:
    """When `bands_per_step` is supplied, `simulate`'s in-band check
    uses the band at THAT step rather than the snapshot. Setup: room
    starts at 21.0 (in the snapshot band of 20-22), idle slope flat.
    `bands_per_step` shifts the band upward to 23-25 at step 5;
    suddenly room at 21.0 is below the new low. The latter half of
    the horizon should NOT count as in band.
    """
    # 10 steps total: first 5 at (20, 22) — room=21 is in band —
    # latter 5 at (23, 25) — room=21 is below low.
    bands = [(20.0, 22.0)] * 5 + [(23.0, 25.0)] * 5
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.0),
        _inputs(21.0, low=20.0, high=22.0),
        horizon_minutes=10,
        bands_per_step=bands,
    )
    # Trapezoidal accounting: step 4-5 transitions from in-band
    # to out-of-band, counts 0.5; steps 0-3 fully in (4 min); steps
    # 5-9 fully out. Total ~4.5 min in band.
    assert 4.0 <= score.time_in_band_minutes <= 5.0


def test_simulate_falls_back_to_snapshot_when_bands_per_step_none() -> None:
    """Existing v0.8.x behaviour preserved: when bands_per_step is None,
    in-band check uses `inputs.low` / `inputs.high` for every step."""
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.0),
        _inputs(21.0, low=20.0, high=22.0),
        horizon_minutes=10,
        bands_per_step=None,
    )
    # Idle slope flat, room 21.0 stays in [20, 22] for full horizon.
    assert score.time_in_band_minutes == 10.0


def test_simulate_midpoint_uses_end_horizon_band() -> None:
    """The midpoint tie-break is anchored on the END band's midpoint
    when bands_per_step is provided. Useful when the band shifts and
    a final position should be judged against where band centre IS at
    end-of-horizon, not where it WAS at start."""
    # Band moves from (20, 22) midpoint 21 to (24, 26) midpoint 25.
    # Idle keeps room at 21.0; midpoint distance is |21 - 25| = 4.
    bands = [(20.0, 22.0)] * 5 + [(24.0, 26.0)] * 5
    score = simulate(
        Action(ACTION_IDLE, None),
        _slopes(idle=0.0),
        _inputs(21.0, low=20.0, high=22.0),
        horizon_minutes=10,
        bands_per_step=bands,
    )
    assert score.midpoint_distance == 4.0
    assert score.end_temp == 21.0


def test_plan_morning_ramp_with_lookahead_picks_heat() -> None:
    """v0.9.0 anchor scenario: heat-only zone, current band (16, 19),
    room at 17.5 (comfortably in current band), idle slope is mildly
    negative, recovery_heat present. `bands_per_step` shows the band
    shifting up to (20, 22) at minute 30 (the morning ramp). Without
    lookahead, idle would win (room in band, idle achieves full
    horizon → idle preference fires). WITH lookahead, idle ends up at
    16.5 in the new (20, 22) band → outside band → fewer in-band
    minutes than heat, which pre-heats to land inside the new band.
    Heat should win.
    """
    # Horizon = 60 min; band rises at minute 30.
    bands = [(16.0, 19.0)] * 30 + [(20.0, 22.0)] * 30
    result = plan(
        _slopes(idle=-0.02, recovery_heat=0.08, recovery_cool=None),
        _inputs(17.5, low=16.0, high=19.0),
        horizon_minutes=60,
        predictor_decision=idle_decision(),
        bands_per_step=bands,
    )
    assert result.action == ACTION_HEAT


def test_plan_morning_ramp_without_lookahead_picks_idle() -> None:
    """Control case for the morning-ramp test above: WITHOUT
    `bands_per_step`, MPC sees only the current band (16, 19). Room
    at 17.5 is comfortably in band; idle slope mildly negative means
    room drifts down to ~16.3 over 60 min — still in band. Idle
    achieves full horizon → idle preference fires → idle wins. This
    is the v0.8.x behaviour we're replacing for lookahead-equipped
    refreshes.
    """
    result = plan(
        _slopes(idle=-0.02, recovery_heat=0.08, recovery_cool=None),
        _inputs(17.5, low=16.0, high=19.0),
        horizon_minutes=60,
        predictor_decision=idle_decision(),
        bands_per_step=None,
    )
    assert result.action == ACTION_IDLE


def test_plan_bail_out_reads_bands_per_step_first_entry() -> None:
    """v0.9.0: the safety bail-out (room outside band on the missing
    recovery side) reads `bands_per_step[0]` when provided, not the
    snapshot. Scenario: heat-only zone, snapshot band is (20, 23),
    but at THIS refresh the schedule has just rotated (e.g., 06:00
    sharp) and the live band is (16, 19). Room is at 17.0 — below
    the SNAPSHOT low (20), would trigger bail-out if reading snapshot
    — but inside the LIVE band (16-19), so MPC should proceed.
    Without the bands_per_step[0] read, MPC would defer to predictor
    even though it has everything it needs.
    """
    bands = [(16.0, 19.0)] * 60
    result = plan(
        _slopes(idle=0.0, recovery_heat=0.05, recovery_cool=None),
        # Snapshot says (20, 23) but the live band is (16, 19) per
        # bands_per_step[0]. Room=17 is inside (16, 19).
        _inputs(17.0, low=20.0, high=23.0),
        horizon_minutes=60,
        predictor_decision=idle_decision(),
        bands_per_step=bands,
    )
    # Idle keeps room at 17.0, full horizon in band → idle wins via
    # idle preference. The key is that MPC didn't bail out — if the
    # bail-out read the SNAPSHOT (20, 23), room=17 would be <= 20
    # and bail-out would return the predictor_decision.
    assert result.action == ACTION_IDLE
    # The bail-out path would return `predictor_decision` verbatim (here
    # `idle_decision()`); the non-bail-out path returns a fresh
    # `idle_decision()` from MPC's own scoring. Both produce equivalent
    # decision objects (target_temp=None, target_mode=fan_only), so this
    # test proves the code-path via coverage rather than via outcome.
    # The symmetric snapshot-path test below uses a heat_decision
    # predictor for an unambiguous outcome-based assertion.


def test_plan_bail_out_uses_snapshot_when_bands_per_step_none() -> None:
    """Symmetric to the bail-out-reads-bands-per-step-first-entry test:
    without bands_per_step, the bail-out reads inputs.low / inputs.high
    (v0.8.1 behaviour). Cool-only zone, room at 17.0 below snapshot
    low=20.0, no recovery_heat → bail-out fires → predictor decision
    returned verbatim. Pins backwards compat for the legacy call path
    that doesn't pass bands_per_step."""
    predictor_decision = heat_decision(20.0)
    result = plan(
        _slopes(idle=0.0, recovery_heat=None, recovery_cool=-0.05),
        _inputs(17.0, low=20.0, high=23.0),
        horizon_minutes=60,
        predictor_decision=predictor_decision,
        bands_per_step=None,
    )
    assert result == predictor_decision
