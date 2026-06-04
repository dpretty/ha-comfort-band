"""Tests for the predictive controller (slope estimator + anticipatory startup/shutoff)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    SAMPLE_MAX_COUNT,
    SAMPLE_MIN_INTERVAL_S,
    SAMPLE_WINDOW_MINUTES,
    SLOPE_MIN_SAMPLES,
)
from custom_components.comfort_band.hysteresis import (
    HysteresisDecision,
    HysteresisInputs,
    cool_decision,
    heat_decision,
    idle_decision,
)
from custom_components.comfort_band.predictor import (
    Sample,
    ThermalSlopes,
    append_sample,
    decide,
    estimate_slopes,
    load_samples,
    project,
    sample_from_dict,
    sample_to_dict,
)

_T0 = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _samples_at(
    interval_s: float, count: int, *, action: str, start_temp: float, slope_per_h: float
) -> list[Sample]:
    """Build a synthetic segment: `count` samples at `interval_s` apart with
    a linear temperature trend, starting from `_T0`. `slope_per_h` is °C/h.
    """
    slope_per_minute = slope_per_h / 60.0
    out: list[Sample] = []
    for i in range(count):
        t = _T0 + timedelta(seconds=interval_s * i)
        temp = start_temp + slope_per_minute * (interval_s * i / 60.0)
        out.append(Sample(t=t, temp=temp, action=action))
    return out


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


def _hyst_idle() -> HysteresisDecision:
    return idle_decision()


def _hyst_heat(low: float = 20.0) -> HysteresisDecision:
    return heat_decision(low)


def _hyst_cool(high: float = 23.0) -> HysteresisDecision:
    return cool_decision(high)


def _decide_from_samples(
    samples: list[Sample],
    inputs: HysteresisInputs,
    *,
    lookahead_minutes: int,
    hysteresis_decision: HysteresisDecision,
    now: datetime,
    passive_tolerance: float = 0.5,
) -> HysteresisDecision:
    """Test convenience: estimate slopes from samples, then call decide().
    Mirrors what the coordinator does (compute slopes once, feed in).
    `passive_tolerance` defaults to the production default (0.5 °C); tests
    that exercise the passive-acceptance branch override it explicitly.
    """
    slopes = estimate_slopes(samples, now=now)
    return decide(
        slopes,
        inputs,
        lookahead_minutes=lookahead_minutes,
        passive_tolerance=passive_tolerance,
        hysteresis_decision=hysteresis_decision,
    )


# ----- estimate_slopes -----


def test_flat_line_yields_zero_slope() -> None:
    """Truly flat data produces slope = 0 with method = "wls".

    v0.9.1 adds the diagnostic fields (`method_*`, `sample_count_*`,
    `std_dev_*`) so the sensor can surface what's behind the estimate.
    For genuinely constant samples, WLS produces 0 and tags "wls" —
    honest report that we used WLS and it found no slope, vs a "none"
    tag which would mean we had no slope to compute at all.
    """
    samples = _samples_at(120, 10, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.0)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.idle is not None
    assert abs(slopes.idle) < 1e-6
    assert slopes.recovery_heat is None
    assert slopes.recovery_cool is None
    # v0.9.1 diagnostic fields populated.
    assert slopes.method_idle == "wls"
    assert slopes.sample_count_idle == 10
    assert slopes.method_recovery_heat == "none"
    assert slopes.method_recovery_cool == "none"
    assert slopes.sample_count_recovery_heat == 0
    assert slopes.sample_count_recovery_cool == 0


def test_monotone_idle_drift_recovered() -> None:
    # -0.5 °C/h over 30 min (16 samples at 120s) — should recover to within 0.01 °C/h.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=-0.5)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.idle is not None
    slope_per_hour = slopes.idle * 60.0
    assert slope_per_hour == pytest.approx(-0.5, abs=0.01)


def test_recovery_heat_slope_distinct_from_idle() -> None:
    # +2.5 °C/h while heating — distinct from passive drift.
    samples = _samples_at(120, 10, action=ACTION_HEAT, start_temp=19.5, slope_per_h=2.5)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.recovery_heat is not None
    assert slopes.recovery_heat * 60.0 == pytest.approx(2.5, abs=0.01)
    assert slopes.idle is None
    assert slopes.recovery_cool is None
    assert slopes.method_recovery_heat == "wls"  # valid positive slope kept


# ----- v0.15.0: recovery-slope sign guard -----


def test_negative_heat_slope_is_rejected() -> None:
    """A heating segment that fits a NEGATIVE slope (sensor/segment noise from a
    few sub-minute blips) is discarded: the model must never believe heating
    cools the room (which made MPC idle while below band — the gym bug)."""
    samples = _samples_at(120, 10, action=ACTION_HEAT, start_temp=21.0, slope_per_h=-1.0)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.recovery_heat is None
    assert slopes.method_recovery_heat == "rejected"
    # The segment is still reported in the diagnostics (it existed; only the
    # slope value was discarded) so the rejection is visible, not silent.
    assert slopes.sample_count_recovery_heat == 10


def test_positive_cool_slope_is_rejected() -> None:
    """Symmetric guard: a cooling segment that fits a NON-negative slope is
    discarded (cooling cannot warm the room)."""
    samples = _samples_at(120, 10, action=ACTION_COOL, start_temp=25.0, slope_per_h=1.0)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.recovery_cool is None
    assert slopes.method_recovery_cool == "rejected"
    assert slopes.sample_count_recovery_cool == 10


def test_valid_cool_slope_kept() -> None:
    """A correctly-signed cooling slope (negative) is untouched."""
    samples = _samples_at(120, 10, action=ACTION_COOL, start_temp=25.0, slope_per_h=-2.0)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.recovery_cool is not None
    assert slopes.recovery_cool * 60.0 == pytest.approx(-2.0, abs=0.01)
    assert slopes.method_recovery_cool == "wls"


def test_idle_slope_keeps_both_signs() -> None:
    """The guard applies only to recovery slopes; idle drift is legitimately ±
    (a room can passively warm or cool), so a negative idle slope is kept."""
    samples = _samples_at(120, 10, action=ACTION_IDLE, start_temp=22.0, slope_per_h=-1.5)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.idle is not None
    assert slopes.idle * 60.0 == pytest.approx(-1.5, abs=0.01)
    assert slopes.method_idle == "wls"


def test_segment_below_min_samples_yields_none() -> None:
    samples = _samples_at(
        120, SLOPE_MIN_SAMPLES - 1, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.5
    )
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.idle is None


def test_recency_weights_actually_weight() -> None:
    # Build a clean linear drift, then perturb the OLDEST sample by +1 °C.
    # Recency weighting (τ=20min) should make that bend less than 10% vs.
    # perturbing the NEWEST sample.
    base = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=-0.5)
    base_slope = estimate_slopes(base, now=base[-1].t).idle
    assert base_slope is not None

    # Perturb oldest
    perturb_old = list(base)
    perturb_old[0] = Sample(t=base[0].t, temp=base[0].temp + 1.0, action=ACTION_IDLE)
    slope_old = estimate_slopes(perturb_old, now=base[-1].t).idle
    assert slope_old is not None

    # Perturb newest
    perturb_new = list(base)
    perturb_new[-1] = Sample(t=base[-1].t, temp=base[-1].temp + 1.0, action=ACTION_IDLE)
    slope_new = estimate_slopes(perturb_new, now=base[-1].t).idle
    assert slope_new is not None

    bend_old = abs(slope_old - base_slope)
    bend_new = abs(slope_new - base_slope)
    # Old sample's effect should be substantially smaller than new sample's. The
    # exact ratio depends on weight-by-leverage interaction in the WLS denominator
    # (purely-uniform weights would give ~1.0 by endpoint symmetry); 0.5 catches
    # a regression where recency weighting was disabled.
    assert bend_old < 0.5 * bend_new


def test_segmenting_isolates_trailing_run() -> None:
    # idle samples (drift -0.5 °C/h), then a heat run (+3 °C/h), then idle again.
    idle_before = _samples_at(120, 6, action=ACTION_IDLE, start_temp=21.0, slope_per_h=-0.5)
    heat_start_temp = idle_before[-1].temp
    heat_t0 = idle_before[-1].t + timedelta(seconds=120)
    heat_run = []
    for i in range(6):
        t = heat_t0 + timedelta(seconds=120 * i)
        heat_run.append(Sample(t=t, temp=heat_start_temp + 0.05 * i, action=ACTION_HEAT))
    idle_after_t0 = heat_run[-1].t + timedelta(seconds=120)
    idle_after = []
    for i in range(6):
        t = idle_after_t0 + timedelta(seconds=120 * i)
        idle_after.append(Sample(t=t, temp=heat_run[-1].temp - 0.02 * i, action=ACTION_IDLE))
    samples = idle_before + heat_run + idle_after

    slopes = estimate_slopes(samples, now=idle_after[-1].t)
    # Trailing idle run is `idle_after`, NOT `idle_before` — different slope.
    assert slopes.idle is not None
    # idle_after is -0.02 °C / 2min = -0.6 °C/h
    assert slopes.idle * 60.0 == pytest.approx(-0.6, abs=0.05)
    # Heat run captured separately.
    assert slopes.recovery_heat is not None
    assert slopes.recovery_heat > 0


def test_singular_system_returns_none() -> None:
    # All samples at the same timestamp — denominator collapses to 0.
    samples = [Sample(t=_T0, temp=21.0 + 0.1 * i, action=ACTION_IDLE) for i in range(6)]
    slopes = estimate_slopes(samples, now=_T0)
    assert slopes.idle is None


def test_last_updated_reflects_most_recent_sample() -> None:
    samples = _samples_at(120, 5, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.0)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.last_updated == samples[-1].t
    assert slopes.sample_count == 5


def test_window_minutes_spans_buffer() -> None:
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.0)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    # 15 intervals of 120s = 1800s = 30min
    assert slopes.window_minutes == 30.0


# ----- project -----


def test_project_linear() -> None:
    assert project(20.0, 0.05, 5) == pytest.approx(20.25)


def test_project_none_slope_returns_none() -> None:
    assert project(20.0, None, 5) is None


# ----- ThermalSlopes.for_action -----


def test_for_action_dispatch() -> None:
    s = ThermalSlopes(
        idle=0.001,
        recovery_heat=0.05,
        recovery_cool=-0.05,
        sample_count=16,
        window_minutes=30.0,
        last_updated=_T0,
    )
    assert s.for_action(ACTION_HEAT) == 0.05
    assert s.for_action(ACTION_COOL) == -0.05
    assert s.for_action(ACTION_IDLE) == 0.001
    # Fallback to idle for any unrecognised / None action -- the sensor calls
    # this with zone["last_action"] which is None on fresh zones.
    assert s.for_action(None) == 0.001
    assert s.for_action(ACTION_UNKNOWN) == 0.001


# ----- decide: startup -----


def test_startup_heat_when_steep_idle_drift_down() -> None:
    # Room at 20.4 (inside band), idle slope -3 °C/h, lookahead 5 min.
    # Projection: 20.4 + (-3/60)*5 = 20.4 - 0.25 = 20.15 < (low - db_below = 19.7).
    # Wait: 20.15 > 19.7. Need steeper drift.
    # -6 °C/h → 20.4 - 0.5 = 19.9 > 19.7. Still no.
    # -10 °C/h → 20.4 - 0.833 = 19.57 < 19.7. Triggers.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=21.0, slope_per_h=-10.0)
    # Build inputs with the *current* room being just slightly below the start_temp so it's
    # inside the band but the projection drops below the deadband edge.
    inputs = _inputs(20.4, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_HEAT
    assert decision.target_mode == HVAC_MODE_HEAT
    assert decision.target_temp == 20.0


def test_startup_cool_when_steep_idle_drift_up() -> None:
    # Hysteresis would say idle (22.8 < 23.5 deadband entry); predictor fires
    # cool because projection 22.8 + 10/60*5 = 23.63 crosses the deadband.
    from custom_components.comfort_band.hysteresis import decide as hysteresis_decide

    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=10.0)
    inputs = _inputs(22.8, current=ACTION_IDLE)
    assert hysteresis_decide(inputs).action == ACTION_IDLE  # predictor must fire earlier
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_COOL
    assert decision.target_mode == HVAC_MODE_COOL
    assert decision.target_temp == 23.0


def test_startup_falls_through_when_slope_flat() -> None:
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.0)
    inputs = _inputs(21.0, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision == _hyst_idle()


def test_startup_falls_through_when_projection_inside_deadband() -> None:
    # Drift -3 °C/h, room at 21.5, projection: 21.5 - 0.25 = 21.25 > 19.7. No trigger.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=-3.0)
    inputs = _inputs(21.5, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision == _hyst_idle()


def test_startup_works_when_current_action_unknown() -> None:
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=21.0, slope_per_h=-10.0)
    inputs = _inputs(20.4, current=ACTION_UNKNOWN)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_HEAT


def test_startup_cool_works_when_current_action_unknown() -> None:
    # Symmetric to the heat case: ACTION_UNKNOWN routes through the same
    # idle/unknown branch, so cool startup must fire too.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=10.0)
    inputs = _inputs(22.8, current=ACTION_UNKNOWN)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_COOL


# ----- decide: shutoff -----


def test_shutoff_release_idle_when_heat_overshoot_predicted() -> None:
    # Heating, room below low. Hysteresis would say "keep heating" (room < low).
    # Predictor anticipates: room=19.0, slope +15 °C/h, lookahead 5min ->
    # projected = 19.0 + 15/60*5 = 20.25 >= low (20.0). Release idle now so
    # the room peaks at low instead of overshooting past it. This is the
    # whole point of anticipatory shutoff: it must fire *before* hysteresis
    # would release.
    samples = _samples_at(120, 10, action=ACTION_HEAT, start_temp=18.0, slope_per_h=15.0)
    inputs = _inputs(19.0, current=ACTION_HEAT)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE
    assert decision.target_mode == HVAC_MODE_FAN_ONLY
    assert decision.target_temp is None


def test_shutoff_release_idle_when_cool_undershoot_predicted() -> None:
    # Symmetric to the heat test: cooling, room still above high. Hysteresis
    # would keep cooling (room > high). Predictor: 24.0 + (-15/60)*5 = 22.75
    # <= high (23.0). Release now so the room peaks at high, not below it.
    from custom_components.comfort_band.hysteresis import decide as hysteresis_decide

    samples = _samples_at(120, 10, action=ACTION_COOL, start_temp=25.0, slope_per_h=-15.0)
    inputs = _inputs(24.0, current=ACTION_COOL)
    assert hysteresis_decide(inputs).action == ACTION_COOL  # predictor must fire earlier
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_cool(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE


def test_shutoff_falls_through_when_slope_shallow() -> None:
    # Heating with shallow slope -- projection won't reach low in lookahead.
    # room=18.0, slope 0.5 °C/h, lookahead 5min -> projected = 18.04 << 20.
    samples = _samples_at(120, 10, action=ACTION_HEAT, start_temp=18.0, slope_per_h=0.5)
    inputs = _inputs(18.0, current=ACTION_HEAT)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision == _hyst_heat()


def test_shutoff_falls_through_when_heat_slope_negative() -> None:
    # Pathological: action is heat but temp is dropping (e.g., HVAC not
    # responding). Invariant: a wrong-sign heat segment must never trigger
    # shutoff anticipation. v0.15.0 enforces this upstream — estimate_slopes
    # sign-rejects the negative fit (recovery_heat -> None), so shutoff can't
    # fire and the decision falls through to hysteresis. (decide() also defends
    # the `recovery_heat > epsilon` predicate directly; the shallow-positive
    # path is covered by test_shutoff_falls_through_when_heat_slope_shallow.)
    samples = _samples_at(120, 10, action=ACTION_HEAT, start_temp=22.0, slope_per_h=-1.0)
    inputs = _inputs(19.5, current=ACTION_HEAT)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision == _hyst_heat()


def test_shutoff_falls_through_when_cool_slope_positive() -> None:
    # Symmetric: cool action but temp is rising. v0.15.0 sign-rejects the
    # wrong-sign cool slope (recovery_cool -> None), so shutoff can't fire and
    # the decision falls through to hysteresis.
    samples = _samples_at(120, 10, action=ACTION_COOL, start_temp=21.0, slope_per_h=+1.0)
    inputs = _inputs(24.0, current=ACTION_COOL)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_cool(),
        now=samples[-1].t,
    )
    assert decision == _hyst_cool()


def test_shutoff_releases_before_hysteresis_would() -> None:
    # The crucial behavioural claim: predictor fires the release BEFORE
    # hysteresis would (i.e., when room is still below `low`). Without this
    # test we could ship a "shutoff" branch that only ever agrees with
    # hysteresis's own release decision, which is not anticipation at all.
    samples = _samples_at(120, 10, action=ACTION_HEAT, start_temp=18.0, slope_per_h=20.0)
    inputs = _inputs(19.5, current=ACTION_HEAT)  # still below low=20 -> hysteresis would heat
    # Hysteresis on these inputs would return heat (room < low).
    from custom_components.comfort_band.hysteresis import decide as hysteresis_decide

    hyst = hysteresis_decide(inputs)
    assert hyst.action == ACTION_HEAT
    # Predictor: projection = 19.5 + 20/60*5 = 21.17 >= 20 -> idle.
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=hyst,
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE


# ----- decide: passive drift acceptance -----


def test_passive_heat_suppressed_when_idle_slope_recovers() -> None:
    # Room is below the deadband entry (hysteresis would fire heat), but the
    # idle slope is positive and projection lands comfortably inside the
    # band within lookahead. Predictor should override hysteresis to idle.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=18.5, slope_per_h=8.0)
    inputs = _inputs(19.5, current=ACTION_IDLE)  # 0.5 below low=20 (within tolerance 0.5)
    # Projection: 19.5 + 8/60*5 = 20.17 > low (20.0). Movement 0.67 >= 0.1.
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE


def test_passive_cool_suppressed_when_idle_slope_recovers() -> None:
    # Symmetric: room above deadband (hyst would cool), but slope is
    # negative and projection lands comfortably inside the band.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=24.0, slope_per_h=-8.0)
    inputs = _inputs(23.5, current=ACTION_IDLE)  # 0.5 above high=23
    # Projection: 23.5 - 8/60*5 = 22.83 < high. Movement 0.67 >= 0.1.
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_cool(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE


def test_passive_falls_through_when_slope_wrong_sign() -> None:
    # Hyst says heat but slope is negative (room cooling further). Passive
    # branch requires a recovering slope -- fall through to hysteresis.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=20.0, slope_per_h=-2.0)
    inputs = _inputs(19.5, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision == _hyst_heat()


def test_passive_falls_through_when_projection_does_not_reach_band() -> None:
    # Isolates the projection guard. Room is INSIDE the comfort tolerance
    # (19.65 vs floor 19.5) and movement clears the jitter guard, but the
    # slope is too shallow for the projection to actually reach the band
    # within lookahead. Predictor must defer to hysteresis.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=19.3, slope_per_h=2.0)
    inputs = _inputs(19.65, current=ACTION_IDLE)  # within tolerance 0.5
    # Projection: 19.65 + 2/60*5 = 19.817 < low (20). Movement 0.167 > 0.1.
    # Comfort floor: 19.65 >= low - 0.5 = 19.5. Only projection fails.
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision == _hyst_heat()


def test_passive_falls_through_when_deviation_exceeds_tolerance() -> None:
    # Room is deeper below band than passive_tolerance allows -- comfort
    # floor wins, predictor doesn't suppress even with a strong slope.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=18.0, slope_per_h=20.0)
    inputs = _inputs(19.0, current=ACTION_IDLE)  # 1.0 below low, tolerance 0.5
    # Projection: 19.0 + 20/60*5 = 20.67 >= low. But room < (low - tolerance).
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
        passive_tolerance=0.5,
    )
    assert decision == _hyst_heat()


def test_passive_falls_through_when_forecast_movement_below_min() -> None:
    # Slope is just above epsilon (so passes the slope-sign guard) but the
    # forecast moves the room by < PASSIVE_FORECAST_MOVEMENT_MIN_C (0.1 °C).
    # Without this jitter guard a sensor-noise slope could spuriously
    # suppress a hysteresis-correct heat call.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=19.4, slope_per_h=0.6)
    inputs = _inputs(19.6, current=ACTION_IDLE)
    # Projection: 19.6 + 0.6/60*5 = 19.65. Even though >= low would fail,
    # the key assertion is movement = 0.05 < 0.1, so suppression must NOT
    # fire even if projection happened to clear low.
    inputs_at_boundary = _inputs(20.0 - 0.05, current=ACTION_IDLE, low=20.0)  # crafted edge
    decision = _decide_from_samples(
        samples,
        inputs_at_boundary,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    # Movement = 0.05 °C, below the 0.1 °C jitter guard -- expect hyst.
    assert decision == _hyst_heat()
    # The original inputs (without the boundary craft) should also fall
    # through, demonstrating the same guard via the projection-< -low path.
    decision2 = _decide_from_samples(
        samples, inputs, lookahead_minutes=5, hysteresis_decision=_hyst_heat(), now=samples[-1].t
    )
    assert decision2 == _hyst_heat()


def test_passive_falls_through_when_idle_slope_none() -> None:
    # Fewer than SLOPE_MIN_SAMPLES idle samples -> idle_slope is None ->
    # predictor cannot evaluate passive branch -> hysteresis fires heat.
    samples = _samples_at(
        120, SLOPE_MIN_SAMPLES - 1, action=ACTION_IDLE, start_temp=18.5, slope_per_h=6.0
    )
    inputs = _inputs(19.5, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision == _hyst_heat()


def test_passive_works_when_current_action_unknown() -> None:
    # ACTION_UNKNOWN routes into the same elif arm as ACTION_IDLE, so passive
    # suppression must apply when the previous action was unknown (boot from
    # storage with last_action=None, sensor outage recovery, etc.). Room at
    # 19.6 sits visibly *inside* the comfort tolerance (low - 0.5 = 19.5) so
    # the test isn't sensitive to inclusive-vs-exclusive interpretation of
    # the floor predicate.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=18.5, slope_per_h=8.0)
    inputs = _inputs(19.6, current=ACTION_UNKNOWN)
    # Projection: 19.6 + 8/60*5 = 20.27 >= low (20.0); movement 0.67 >= 0.1.
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE


def test_passive_cool_works_when_current_action_unknown() -> None:
    # Symmetric to the heat-side test above: hot room recovering on its own
    # while last_action is unknown. Room 23.3 sits above the band edge
    # (high=23.0) but inside the comfort tolerance (high + 0.5 = 23.5).
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=24.5, slope_per_h=-8.0)
    inputs = _inputs(23.3, current=ACTION_UNKNOWN)
    # Projection: 23.3 - 8/60*5 = 22.63 <= high (23.0); movement 0.67 >= 0.1.
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_cool(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_IDLE


def test_passive_tolerance_zero_disables_suppression() -> None:
    # passive_tolerance=0 means even a room right at low - 0.001 won't
    # be tolerated. Provides users an "always defer to hysteresis on
    # band exits" knob for restoring pre-v0.7 behaviour.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=18.5, slope_per_h=6.0)
    inputs = _inputs(19.5, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_heat(),
        now=samples[-1].t,
        passive_tolerance=0.0,
    )
    assert decision == _hyst_heat()


# ----- decide: fall-through -----


def test_falls_through_when_buffer_empty() -> None:
    inputs = _inputs(21.0, current=ACTION_IDLE)
    decision = _decide_from_samples(
        [],
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=_T0,
    )
    assert decision == _hyst_idle()


def test_falls_through_when_segment_too_short() -> None:
    samples = _samples_at(
        120, SLOPE_MIN_SAMPLES - 1, action=ACTION_IDLE, start_temp=21.0, slope_per_h=-10.0
    )
    inputs = _inputs(20.4, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision == _hyst_idle()


def test_unknown_decision_when_room_is_none() -> None:
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.0)
    decision = _decide_from_samples(
        samples,
        _inputs(None, current=ACTION_IDLE),
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    assert decision.action == ACTION_UNKNOWN
    assert decision.target_mode is None


# ----- append_sample -----


def test_append_sample_first_always_appends() -> None:
    new, appended = append_sample([], now=_T0, temp=21.0, action=ACTION_IDLE)
    assert appended is True
    assert len(new) == 1
    assert new[0] == Sample(t=_T0, temp=21.0, action=ACTION_IDLE)


def test_append_sample_rate_limited_same_action() -> None:
    samples = [Sample(t=_T0, temp=21.0, action=ACTION_IDLE)]
    next_t = _T0 + timedelta(seconds=SAMPLE_MIN_INTERVAL_S - 1)
    new, appended = append_sample(samples, now=next_t, temp=21.1, action=ACTION_IDLE)
    assert appended is False
    assert new is samples  # same reference -- not even copied


def test_append_sample_accepts_action_transition_within_rate_limit() -> None:
    samples = [Sample(t=_T0, temp=21.0, action=ACTION_IDLE)]
    next_t = _T0 + timedelta(seconds=10)
    new, appended = append_sample(samples, now=next_t, temp=20.0, action=ACTION_HEAT)
    assert appended is True
    assert new[-1].action == ACTION_HEAT


def test_append_sample_prunes_old_samples() -> None:
    old = Sample(t=_T0, temp=20.0, action=ACTION_IDLE)
    recent_t = _T0 + timedelta(minutes=SAMPLE_WINDOW_MINUTES + 5)
    new, appended = append_sample([old], now=recent_t, temp=21.0, action=ACTION_IDLE)
    assert appended is True
    assert old not in new
    assert len(new) == 1


def test_append_sample_count_cap() -> None:
    # Build a buffer beyond the cap, then append — expect cap enforcement.
    samples = [
        Sample(t=_T0 + timedelta(seconds=i), temp=21.0, action=ACTION_IDLE)
        for i in range(SAMPLE_MAX_COUNT + 5)
    ]
    next_t = _T0 + timedelta(seconds=SAMPLE_MAX_COUNT + 5 + SAMPLE_MIN_INTERVAL_S)
    new, appended = append_sample(samples, now=next_t, temp=21.0, action=ACTION_HEAT)
    assert appended is True
    assert len(new) == SAMPLE_MAX_COUNT


def test_append_sample_records_fan_mode() -> None:
    """v0.8: the coordinator passes the climate's current fan_mode; the
    appended sample must carry it through. v0.9's per-fan-mode slope
    segmentation depends on this being recorded faithfully."""
    new, appended = append_sample([], now=_T0, temp=21.0, action=ACTION_IDLE, fan_mode="med")
    assert appended is True
    assert new[0].fan_mode == "med"


def test_append_sample_records_none_fan_mode() -> None:
    """Climate entities that don't expose `fan_mode` (or transient missing
    attribute) yield fan_mode=None. The sample still appends."""
    new, appended = append_sample([], now=_T0, temp=21.0, action=ACTION_IDLE, fan_mode=None)
    assert appended is True
    assert new[0].fan_mode is None


def test_append_sample_fan_mode_change_does_not_force_append() -> None:
    """v0.8 doesn't treat fan_mode transitions as segment boundaries — slope
    estimation ignores fan_mode here, so a fan-mode change inside the
    rate-limit window would only inflate the buffer without affecting
    control. (v0.9 may revisit when slopes partition by fan_mode.)
    """
    samples = [Sample(t=_T0, temp=21.0, action=ACTION_IDLE, fan_mode="low")]
    next_t = _T0 + timedelta(seconds=SAMPLE_MIN_INTERVAL_S - 1)
    _new, appended = append_sample(
        samples, now=next_t, temp=21.1, action=ACTION_IDLE, fan_mode="high"
    )
    assert appended is False


def test_estimate_slopes_ignores_fan_mode_in_v0_8() -> None:
    """Mixed fan-mode samples within a single action segment still produce
    one slope per action. v0.9 partitions by (action, fan_mode); v0.8 must
    not pre-partition or the v0.7 estimator behaviour regresses.
    """
    samples = []
    for i in range(8):
        t = _T0 + timedelta(seconds=120 * i)
        # Alternate fan_mode every other sample — would split into 4 segments
        # if estimate_slopes did naive segmentation by (action, fan_mode).
        fan = "low" if i % 2 == 0 else "high"
        samples.append(Sample(t=t, temp=21.0 + 0.05 * i, action=ACTION_IDLE, fan_mode=fan))
    slopes = estimate_slopes(samples, now=samples[-1].t)
    # 0.05 °C / 120 s = 0.025 °C/min = 1.5 °C/h. One slope, all 8 samples used.
    assert slopes.idle is not None
    assert slopes.sample_count == 8


# ----- serialization -----


def test_sample_roundtrip() -> None:
    s = Sample(t=_T0, temp=21.5, action=ACTION_HEAT)
    restored = sample_from_dict(sample_to_dict(s))
    assert restored == s


def test_sample_roundtrip_preserves_fan_mode() -> None:
    """v0.8 added fan_mode to Sample. Persisted samples must round-trip the
    value so the v0.9 slope-by-fan-mode segmentation works on existing data.
    """
    s = Sample(t=_T0, temp=21.5, action=ACTION_HEAT, fan_mode="high")
    restored = sample_from_dict(sample_to_dict(s))
    assert restored == s
    assert restored is not None
    assert restored.fan_mode == "high"


def test_sample_from_dict_accepts_missing_fan_mode_key() -> None:
    """v0.7 payloads don't have `fan_mode` in the SerializedSample. After
    upgrade, those samples must continue to load (with fan_mode=None) rather
    than being dropped. `NotRequired` + `.get()` makes this work.
    """
    legacy = {"t": _T0.isoformat(), "temp": 21.0, "action": ACTION_IDLE}
    restored = sample_from_dict(legacy)  # type: ignore[arg-type]
    assert restored is not None
    assert restored.fan_mode is None
    assert restored.t == _T0
    assert restored.temp == 21.0
    assert restored.action == ACTION_IDLE


def test_sample_from_dict_accepts_explicit_none_fan_mode() -> None:
    """The v0.8 write path stores `fan_mode: None` when the climate entity
    doesn't expose one. Distinct from the missing-key case above — both must
    round-trip to a Sample with fan_mode=None."""
    payload = {"t": _T0.isoformat(), "temp": 21.0, "action": ACTION_IDLE, "fan_mode": None}
    restored = sample_from_dict(payload)  # type: ignore[arg-type]
    assert restored is not None
    assert restored.fan_mode is None


def test_sample_from_dict_rejects_non_string_fan_mode() -> None:
    """Hand-edited `.storage` could put a non-string under `fan_mode`. Same
    strictness as the `action` field — drop the sample rather than crash the
    consumer downstream.
    """
    bad = {"t": _T0.isoformat(), "temp": 21.0, "action": ACTION_IDLE, "fan_mode": 42}
    assert sample_from_dict(bad) is None  # type: ignore[arg-type]


def test_sample_from_dict_returns_none_on_corrupt_timestamp() -> None:
    bad = {"t": "not a real timestamp", "temp": 21.0, "action": ACTION_IDLE}
    assert sample_from_dict(bad) is None  # type: ignore[arg-type]


def test_sample_from_dict_returns_none_on_naive_timestamp() -> None:
    # `datetime.fromisoformat` parses naive timestamps (no tzinfo) as valid,
    # but later WLS arithmetic `(now - sample.t)` would TypeError on
    # naive-minus-aware. The explicit tzinfo guard prevents that.
    bad = {"t": "2026-05-19T12:00:00", "temp": 21.0, "action": ACTION_IDLE}
    assert sample_from_dict(bad) is None  # type: ignore[arg-type]


def test_sample_from_dict_returns_none_on_non_numeric_temp() -> None:
    # Hand-edited or corrupt `.storage` could yield a string/null `temp`;
    # the predictor would crash on the first arithmetic op without this guard.
    bad_string = {"t": _T0.isoformat(), "temp": "warm", "action": ACTION_IDLE}
    bad_none = {"t": _T0.isoformat(), "temp": None, "action": ACTION_IDLE}
    assert sample_from_dict(bad_string) is None  # type: ignore[arg-type]
    assert sample_from_dict(bad_none) is None  # type: ignore[arg-type]


def test_sample_from_dict_returns_none_on_nan_or_inf_temp() -> None:
    # `float()` accepts "nan"/"inf" strings as well as float-NaN, which would
    # otherwise poison the slope (NaN-propagation through WLS, sensor renders
    # "nan" instead of "unknown"). All forms must be rejected at the boundary.
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
        d = {"t": _T0.isoformat(), "temp": bad, "action": ACTION_IDLE}
        assert sample_from_dict(d) is None, f"should reject temp={bad!r}"  # type: ignore[arg-type]


def test_sample_from_dict_returns_none_on_missing_keys() -> None:
    # A partially-written `.storage` entry can be missing any field. Without
    # the KeyError guard, the resulting crash propagates up to async_setup
    # and fails the whole config entry on what may be a single bad row.
    assert sample_from_dict({"temp": 21.0, "action": ACTION_IDLE}) is None  # type: ignore[typeddict-item]
    assert sample_from_dict({"t": _T0.isoformat(), "action": ACTION_IDLE}) is None  # type: ignore[typeddict-item]
    assert sample_from_dict({"t": _T0.isoformat(), "temp": 21.0}) is None  # type: ignore[typeddict-item]
    # Action present but wrong type (a future schema bug, or a hand-edit).
    bad_action = {"t": _T0.isoformat(), "temp": 21.0, "action": 42}
    assert sample_from_dict(bad_action) is None  # type: ignore[typeddict-item]


def test_load_samples_drops_corrupt_entries() -> None:
    good = sample_to_dict(Sample(t=_T0, temp=21.0, action=ACTION_IDLE))
    bad = {"t": "bogus", "temp": 22.0, "action": ACTION_HEAT}
    out = load_samples([good, bad, good])  # type: ignore[list-item]
    assert len(out) == 2
    assert all(s.action == ACTION_IDLE for s in out)


# ----- epsilon boundary -----


def test_startup_slope_at_epsilon_treated_as_flat() -> None:
    # Build samples that yield a slope just at +epsilon (0.05 °C/h).
    # The predicate is strict `> epsilon`, so we should fall through.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.5, slope_per_h=0.05)
    inputs = _inputs(22.5, current=ACTION_IDLE)
    decision = _decide_from_samples(
        samples,
        inputs,
        lookahead_minutes=5,
        hysteresis_decision=_hyst_idle(),
        now=samples[-1].t,
    )
    # Floating-point: the recovered slope may be slightly above or below 0.05.
    # Either way, with such a flat slope the projection (22.5 + 0.05/60*5 = 22.504)
    # is nowhere near (high + deadband_above = 23.5), so the predicate fails.
    assert decision == _hyst_idle()


# ----- v0.9.1: per-segment diagnostics (sample counts, std_dev, method) -----


def _quantized_samples(
    plateaus: list[tuple[float, int]], *, action: str, interval_s: float = 600
) -> list[Sample]:
    """Build samples that simulate a low-resolution sensor sitting at the
    same quantized value for stretches. `plateaus = [(value, count), ...]`
    means `count` samples at `value`, all `interval_s` apart, then the
    next plateau. Mimics the real-world overnight gym scenario where a
    0.5 °C sensor reads 21.0 for 2 hours, then 20.5 for 2 hours, etc.
    """
    out: list[Sample] = []
    t = _T0
    for value, count in plateaus:
        for _ in range(count):
            out.append(Sample(t=t, temp=value, action=action))
            t = t + timedelta(seconds=interval_s)
    return out


def test_per_segment_sample_counts_populated() -> None:
    """Per-segment counts surface separately from the aggregate
    `sample_count`. Construct a buffer with one each of idle / heat /
    cool runs and assert each per-segment count matches its run length
    while `sample_count` reflects the union.
    """
    idle_run = _samples_at(120, 6, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.0)
    # Splice in a heat run after the idle stretch (shift timestamps so
    # they don't overlap with idle).
    heat_start_t = idle_run[-1].t + timedelta(seconds=120)
    heat_run: list[Sample] = []
    for i in range(5):
        heat_run.append(
            Sample(
                t=heat_start_t + timedelta(seconds=120 * i),
                temp=20.0 + i * 0.5,
                action=ACTION_HEAT,
            )
        )
    cool_start_t = heat_run[-1].t + timedelta(seconds=120)
    cool_run: list[Sample] = []
    for i in range(4):
        cool_run.append(
            Sample(
                t=cool_start_t + timedelta(seconds=120 * i),
                temp=23.0 - i * 0.5,
                action=ACTION_COOL,
            )
        )
    all_samples = idle_run + heat_run + cool_run

    slopes = estimate_slopes(all_samples, now=all_samples[-1].t)
    assert slopes.sample_count == 15
    assert slopes.sample_count_idle == 6
    assert slopes.sample_count_recovery_heat == 5
    assert slopes.sample_count_recovery_cool == 4


def test_std_dev_near_zero_for_quantized_plateau() -> None:
    """The signature diagnostic: samples plateau at one quantized value
    → std_dev ≈ 0. A user looking at the sensor attributes can see
    `std_dev_idle = 0.0` over many samples and recognise that the
    slope (also 0) reflects a sensor that's reporting one value
    throughout the window, NOT a genuinely stable room. The slope
    can't be trusted in this regime.
    """
    samples = _quantized_samples([(20.5, 10)], action=ACTION_IDLE, interval_s=600)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.std_dev_idle == pytest.approx(0.0)
    assert slopes.sample_count_idle == 10


def test_std_dev_positive_for_clean_drift() -> None:
    """Counterpart: a sample series with real variance produces a
    non-zero std_dev. Distinguishes "stable room" from "stable sensor"
    in the diagnostic — when std_dev is positive, the slope estimate
    is informed by real temperature variance, not just one quantized
    plateau.
    """
    # -0.5 °C/h over 30 min (16 samples, 120s apart) → temp ranges over
    # ~0.25 °C. std_dev should be a sizable fraction of that range.
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=-0.5)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.std_dev_idle > 0.05


def test_method_none_when_segment_has_too_few_samples() -> None:
    """Method tag distinguishes "no slope because no data" (none) from
    "slope = 0 because data is flat" (wls). Important for diagnostics:
    a "none" method should not be misread as "the room is stable".
    """
    samples = _samples_at(
        120, SLOPE_MIN_SAMPLES - 1, action=ACTION_IDLE, start_temp=21.0, slope_per_h=0.5
    )
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.idle is None
    assert slopes.method_idle == "none"
    # Per-segment count still reports the actual run length even when
    # the slope itself couldn't be computed.
    assert slopes.sample_count_idle == SLOPE_MIN_SAMPLES - 1


def test_method_wls_when_slope_computed() -> None:
    """When WLS produces a slope, method is tagged "wls". Reserved
    string field — v0.9.1 only emits "wls" or "none", but the schema
    is in place for future fallback methods.
    """
    samples = _samples_at(120, 16, action=ACTION_IDLE, start_temp=22.0, slope_per_h=-0.5)
    slopes = estimate_slopes(samples, now=samples[-1].t)
    assert slopes.method_idle == "wls"


def test_thermal_slopes_back_compat_positional_constructor() -> None:
    """v0.9.1 added new fields to ThermalSlopes with defaults. Existing
    test/code that constructed instances positionally with only the
    original fields should still work — defaults preserve the contract.
    """
    s = ThermalSlopes(
        idle=0.01,
        recovery_heat=None,
        recovery_cool=None,
        sample_count=5,
        window_minutes=10.0,
        last_updated=_T0,
    )
    assert s.sample_count_idle == 0
    assert s.sample_count_recovery_heat == 0
    assert s.sample_count_recovery_cool == 0
    assert s.std_dev_idle == 0.0
    assert s.std_dev_recovery_heat == 0.0
    assert s.std_dev_recovery_cool == 0.0
    assert s.method_idle == "none"
    assert s.method_recovery_heat == "none"
    assert s.method_recovery_cool == "none"
