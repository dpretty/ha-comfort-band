"""Predictive controller for Comfort Band.

Per-zone rolling-window thermal-slope estimator paired with anticipatory
startup/shutoff. Composes with hysteresis: `decide()` returns the same
`HysteresisDecision` shape, so downstream apply logic (min-cycle gates,
climate calls) does not need to know prediction is happening.

The decider is a pure function: given the rolling sample buffer, the current
`HysteresisInputs` snapshot, the lookahead horizon, and the hysteresis decision
that would otherwise be issued, it returns either:
  - the same hysteresis decision (no anticipation triggered), OR
  - an earlier action (anticipated heat/cool, or anticipated idle release).

Module structure mirrors `hysteresis.py`: stateless logic, all state passed
in/out via dataclasses; `now` is injected so unit tests stay pure.

Slope segmentation: three slopes per zone (idle, recovery_heat, recovery_cool),
each computed over the most recent contiguous run of like-actioned samples in
the buffer. Each may be None when its segment has fewer than
SLOPE_MIN_SAMPLES samples or the WLS denominator is near-singular.

Anticipatory shutoff projects at the *band* edge (low/high) — it triggers
the same idle-release that hysteresis would issue once the band is crossed,
just earlier so the room peaks at the edge instead of overshooting.
Anticipatory startup projects at the *deadband* edge — it triggers the
same heat/cool entry that hysteresis would issue once the deadband is
crossed, just earlier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    SAMPLE_MAX_COUNT,
    SAMPLE_MIN_INTERVAL_S,
    SAMPLE_WINDOW_MINUTES,
    SLOPE_EPSILON_PER_HOUR,
    SLOPE_MIN_SAMPLES,
    SLOPE_WEIGHT_TAU_MINUTES,
)
from .hysteresis import (
    UNKNOWN_DECISION,
    HysteresisDecision,
    HysteresisInputs,
    cool_decision,
    heat_decision,
    idle_decision,
)

if TYPE_CHECKING:
    from .storage import SerializedSample


@dataclass(frozen=True)
class Sample:
    """In-memory thermal sample. The coordinator holds these between refreshes."""

    t: datetime
    temp: float
    action: str  # one of ACTION_*


@dataclass(frozen=True)
class ThermalSlopes:
    """Per-action slope estimates and buffer bookkeeping.

    Slopes are °C/minute internally; the sensor multiplies by 60 for display
    in °C/h. Each slope may be None when its segment has fewer than
    SLOPE_MIN_SAMPLES samples or the WLS denominator is singular.
    """

    idle: float | None
    recovery_heat: float | None
    recovery_cool: float | None
    sample_count: int
    window_minutes: float
    last_updated: datetime | None

    def for_action(self, action: str | None) -> float | None:
        """Return the slope matching `action`, falling back to idle.

        Used by the thermal_slope sensor to pick the "current" slope based on
        what the HVAC is doing right now. Lives on the dataclass (not the
        sensor) so other consumers (services, future WS endpoints) don't
        have to re-derive the selection logic.
        """
        if action == ACTION_HEAT:
            return self.recovery_heat
        if action == ACTION_COOL:
            return self.recovery_cool
        return self.idle


# Slope-flatness threshold expressed in slope's native unit (°C/minute), so
# `decide()` doesn't redo the division on every refresh.
_SLOPE_EPSILON_PER_MINUTE: Final = SLOPE_EPSILON_PER_HOUR / 60.0


def sample_to_dict(s: Sample) -> SerializedSample:
    """Serialize a sample for the storage layer."""
    return {"t": s.t.isoformat(), "temp": s.temp, "action": s.action}


def sample_from_dict(d: SerializedSample) -> Sample | None:
    """Deserialize a sample. Returns None on any corrupt or missing field.

    Strict on every field (including missing keys) so a hand-edited or
    partially-written store can't crash the predictor and propagate the
    failure up to `async_setup`, which would mark the whole config entry
    failed. The bad entry is silently dropped and the rest of the buffer
    is preserved.
    """
    try:
        parsed = datetime.fromisoformat(d["t"])
    except (KeyError, TypeError, ValueError):
        return None
    # `fromisoformat` accepts naive ISO strings -- we always write UTC-aware,
    # but a hand-edited store could inject a naive timestamp which would
    # later TypeError in `_wls_slope`'s `(now - sample.t)` arithmetic (aware
    # minus naive). Reject up-front to keep the predictor crashproof.
    if parsed.tzinfo is None:
        return None
    try:
        temp = float(d["temp"])
    except (KeyError, TypeError, ValueError):
        return None
    # `float()` accepts "nan" / "inf" strings as well as float-NaN, but those
    # produce NaN-propagating slopes (epsilon comparisons silently return
    # False, so decide() falls through quietly -- but the sensor would
    # display "nan" instead of HA's idiomatic "unknown"). Reject up-front.
    if not math.isfinite(temp):
        return None
    # mypy sees `action` as `str` (per the TypedDict), but a hand-edited
    # store may have any value. Type-widen via the `object` annotation; the
    # runtime isinstance check is then type-honest. A missing key resolves
    # to None via `.get()`, which fails isinstance(_, str).
    action_raw: object = d.get("action")
    if not isinstance(action_raw, str):
        return None
    return Sample(t=parsed, temp=temp, action=action_raw)


def load_samples(serialized: list[SerializedSample]) -> list[Sample]:
    """Best-effort deserializer used by the coordinator on hydration.

    Drops samples with unparseable timestamps rather than raising — a
    corrupt entry shouldn't disable the predictor for the whole zone.
    """
    result: list[Sample] = []
    for d in serialized:
        s = sample_from_dict(d)
        if s is not None:
            result.append(s)
    return result


def append_sample(
    samples: list[Sample],
    *,
    now: datetime,
    temp: float,
    action: str,
) -> tuple[list[Sample], bool]:
    """Append a new sample, applying rate-limit + age cap.

    Returns `(new_samples, appended)`. When `appended=True` the returned list
    is a fresh copy; the caller should replace its cached reference. When
    `appended=False` the original list is returned unchanged (same reference)
    -- the caller can skip persisting.

    Rate-limit: skip the append if the previous sample was within
    SAMPLE_MIN_INTERVAL_S AND its action matches the incoming action.
    Action transitions are always recorded (the segmenter relies on them).

    Age cap: drop samples older than SAMPLE_WINDOW_MINUTES on every append.
    Count cap (SAMPLE_MAX_COUNT) defends against clock skew (e.g., a future-
    dated sample preventing the age cap from firing).
    """
    if samples:
        last = samples[-1]
        interval_s = (now - last.t).total_seconds()
        # Same-action samples within the rate-limit window are dropped.
        # `interval_s < window` correctly rate-limits backwards-clock skew
        # too (negative is "less than window" by definition), avoiding
        # double-recording when NTP steps backward.
        if interval_s < SAMPLE_MIN_INTERVAL_S and last.action == action:
            return samples, False

    cutoff = now - timedelta(minutes=SAMPLE_WINDOW_MINUTES)
    pruned = [s for s in samples if s.t >= cutoff]
    pruned.append(Sample(t=now, temp=temp, action=action))
    if len(pruned) > SAMPLE_MAX_COUNT:
        pruned = pruned[-SAMPLE_MAX_COUNT:]
    return pruned, True


def _latest_run_of(samples: list[Sample], action: str) -> list[Sample]:
    """Most recent contiguous run of samples with `s.action == action`.

    Returns [] if no such run exists. Walks backward to find the latest
    matching sample, then expands the run boundary while predecessors
    match. The trailing-run constraint means each action's slope reflects
    the most recent cycle of that action — passive drift before a heat
    cycle does not bleed into the slope used for the next heat cycle.
    """
    end: int | None = None
    for i in range(len(samples) - 1, -1, -1):
        if samples[i].action == action:
            end = i + 1
            break
    if end is None:
        return []
    start = end - 1
    while start > 0 and samples[start - 1].action == action:
        start -= 1
    return samples[start:end]


def _wls_slope(segment: list[Sample], *, now: datetime) -> float | None:
    """Weighted-least-squares slope (°C/minute) with exponential recency weights.

    Returns None when the segment has fewer than SLOPE_MIN_SAMPLES samples
    or the WLS denominator is near-singular (clustered timestamps).

    `age_min` is clamped at zero so a future-dated sample (NTP step backwards,
    clock skew) gets weight 1.0 instead of `exp(positive)` -- otherwise a
    minor skew could inflate a single sample's influence by orders of
    magnitude.
    """
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


def estimate_slopes(samples: list[Sample], *, now: datetime) -> ThermalSlopes:
    """Compute per-action slopes over the most recent contiguous run of each
    action class, plus buffer bookkeeping for the sensor's attributes.
    """
    idle_run = _latest_run_of(samples, ACTION_IDLE)
    heat_run = _latest_run_of(samples, ACTION_HEAT)
    cool_run = _latest_run_of(samples, ACTION_COOL)

    window_min = 0.0
    last_updated: datetime | None = None
    if samples:
        last_updated = samples[-1].t
        window_min = (samples[-1].t - samples[0].t).total_seconds() / 60.0

    return ThermalSlopes(
        idle=_wls_slope(idle_run, now=now),
        recovery_heat=_wls_slope(heat_run, now=now),
        recovery_cool=_wls_slope(cool_run, now=now),
        sample_count=len(samples),
        window_minutes=round(window_min, 1),
        last_updated=last_updated,
    )


def project(temp: float, slope_per_minute: float | None, minutes: float) -> float | None:
    """Project `temp` forward by `minutes` at `slope_per_minute`. None-safe."""
    if slope_per_minute is None:
        return None
    return temp + slope_per_minute * minutes


def decide(
    slopes: ThermalSlopes,
    inputs: HysteresisInputs,
    *,
    lookahead_minutes: int,
    hysteresis_decision: HysteresisDecision,
) -> HysteresisDecision:
    """Anticipate startup or shutoff; otherwise return the hysteresis decision.

    Takes pre-computed slopes (caller is expected to need them anyway for
    the thermal_slope sensor) -- avoids a redundant WLS pass per refresh.

    Shutoff anticipation (current action == heat/cool) takes priority over
    startup — stopping early matters more than starting early because
    overshoot is harder to recover from than late starts. The branches are
    mutually exclusive on `current_action` so at most one fires per call.

    Shutoff projects at the hysteresis *release* threshold (`low` for heat,
    `high` for cool): hysteresis releases when room reaches that edge, but
    by then thermal momentum carries it past. Anticipating the crossing
    lets the room peak *at* the edge instead of overshooting it.

    Startup projects at the deadband entry edge (`low - deadband_below` /
    `high + deadband_above`): we trigger the same entry condition hysteresis
    would use later, just earlier.
    """
    if inputs.room is None:
        return UNKNOWN_DECISION

    if inputs.current_action == ACTION_HEAT and slopes.recovery_heat is not None:
        if slopes.recovery_heat > _SLOPE_EPSILON_PER_MINUTE:
            projected = inputs.room + slopes.recovery_heat * lookahead_minutes
            if projected >= inputs.low:
                return idle_decision()
    elif inputs.current_action == ACTION_COOL and slopes.recovery_cool is not None:
        if slopes.recovery_cool < -_SLOPE_EPSILON_PER_MINUTE:
            projected = inputs.room + slopes.recovery_cool * lookahead_minutes
            if projected <= inputs.high:
                return idle_decision()
    # ACTION_UNKNOWN routes into the idle/startup branch because passive
    # drift is the only behavior the predictor can model when the previous
    # action is unknown (room sensor just came back, fresh boot, etc.).
    # The idle slope is computed from action==idle samples, so this means
    # "if the buffer has any idle history, use it for startup."
    elif inputs.current_action in (ACTION_IDLE, ACTION_UNKNOWN) and slopes.idle is not None:
        if slopes.idle < -_SLOPE_EPSILON_PER_MINUTE:
            projected = inputs.room + slopes.idle * lookahead_minutes
            if projected < (inputs.low - inputs.deadband_below):
                return heat_decision(inputs.low)
        elif slopes.idle > _SLOPE_EPSILON_PER_MINUTE:
            projected = inputs.room + slopes.idle * lookahead_minutes
            if projected > (inputs.high + inputs.deadband_above):
                return cool_decision(inputs.high)

    return hysteresis_decision
