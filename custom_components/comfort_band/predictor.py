"""Predictive controller for Comfort Band.

Per-zone rolling-window thermal-slope estimator paired with anticipatory
startup, shutoff, and passive drift acceptance. Composes with hysteresis:
`decide()` returns the same `HysteresisDecision` shape, so downstream apply
logic (min-cycle gates, climate calls) does not need to know prediction is
happening.

The decider is a pure function: given the rolling sample buffer, the current
`HysteresisInputs` snapshot, the lookahead horizon, the per-zone comfort
tolerance, and the hysteresis decision that would otherwise be issued, it
returns either:
  - the same hysteresis decision (no anticipation triggered), OR
  - an earlier action (anticipated heat / cool, or anticipated idle release).

Module structure mirrors `hysteresis.py`: stateless logic, all state passed
in/out via dataclasses; `now` is injected so unit tests stay pure.

Slope segmentation: three slopes per zone (idle, recovery_heat, recovery_cool),
each computed over the most recent contiguous run of like-actioned samples in
the buffer -- contiguous in time as well, with no gap longer than
SAMPLE_MAX_GAP_MINUTES inside it (v0.20.0; see `_latest_run_of`) -- and for
idle, less any samples in the first IDLE_SETTLE_MINUTES of the idle stretch it
belongs to (v0.19.0; see `_settled`). Each may be None when its segment has
fewer than SLOPE_MIN_SAMPLES samples or the WLS denominator is near-singular.
MPC plans with the same slopes but for two substitutions the coordinator
makes: a cached idle slope while there is no live one (v0.12.0), and, since
v0.21.0, the previous cycle's recovery slope for a heat or cool cycle too
young for its own (see `carry_over_recovery_slopes`; since v0.22.0 a run
still running is young until it has been watched for
CARRY_OVER_MIN_SPAN_MINUTES as well, and MPC holds a cycle while it is).

Three projection thresholds, summarised:
- Anticipatory **shutoff** projects at the band edge (`low`/`high`): fires
  the same idle-release hysteresis would issue once the band is crossed,
  just earlier so the room peaks at the edge instead of overshooting.
- Anticipatory **startup** projects at the deadband edge (`low - db_below` /
  `high + db_above`): fires the same heat / cool entry hysteresis would
  issue once the deadband is crossed, just earlier.
- Passive **drift acceptance** projects at the band edge too: suppresses
  the heat / cool hysteresis wants to fire when the slope says natural
  recovery will return us within the lookahead window. Bounded by
  `passive_tolerance` (per-zone comfort floor) and a forecast-movement
  jitter guard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    CARRY_OVER_MIN_SPAN_MINUTES,
    IDLE_SETTLE_MINUTES,
    PASSIVE_FORECAST_MOVEMENT_MIN_C,
    SAMPLE_MAX_COUNT,
    SAMPLE_MAX_GAP_MINUTES,
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
    """In-memory thermal sample. The coordinator holds these between refreshes.

    `fan_mode` is the climate entity's `fan_mode` attribute at sample time;
    None when the entity does not expose one or when the attribute is unset.
    v0.8 records but does not consume it — slope estimation still partitions
    samples by `action` only. v0.9 will partition by `(action, fan_mode)` so
    MPC's action space can include per-fan-mode variants without needing a
    warm-up window after release (the buffer already contains the data).

    Defaults to None so callers (legacy tests, future hand-constructed
    samples) don't have to thread the field when they don't care about it;
    the coordinator's `_append_sample` always passes the actually-observed
    value.
    """

    t: datetime
    temp: float
    action: str  # one of ACTION_*
    fan_mode: str | None = None


@dataclass(frozen=True)
class ThermalSlopes:
    """Per-action slope estimates and buffer bookkeeping.

    Slopes are °C/minute internally; the sensor multiplies by 60 for display
    in °C/h. Each slope may be None when its segment has fewer than
    SLOPE_MIN_SAMPLES samples or the WLS denominator is singular.

    v0.9.1 adds per-segment diagnostic fields so users can spot when an
    estimate is unreliable due to resolution-limited sensors:

      - ``sample_count_*``: number of samples in each per-action segment.
        Existing ``sample_count`` is the aggregate across all actions —
        the new per-segment counts let a user check that idle has, say,
        10 samples behind its slope estimate (not just inheriting from
        the heat/cool totals).
      - ``std_dev_*``: population standard deviation of sample
        temperatures in each segment (°C). When ``std_dev_idle ≈ 0``
        over many samples, the sensor reported the same quantized value
        throughout the window — the slope estimate is unreliable
        regardless of sample count. Distinguishes "stable room" from
        "stable-looking sensor that's masking real drift".
      - ``method_*``: which slope-estimator produced each value —
        "wls" (used as-is), "none" (too few samples / singular fit), or
        (recovery slopes only) "rejected" when the WLS fit had a
        physically-impossible sign and was discarded (see
        ``_reject_wrong_sign``). Reserved string field so future
        fallback methods can be added without changing the shape.

    Defaults on the new fields preserve backward compatibility with any
    tests that construct ThermalSlopes positionally (additions at the end
    don't shift existing arg positions).

    v0.19.0: the idle diagnostics (``sample_count_idle``, ``std_dev_idle``)
    describe the settled part of the idle run -- the samples actually behind
    ``idle`` -- so they read 0 for the first IDLE_SETTLE_MINUTES of every idle
    stretch. ``idle_measured_at`` is when the newest of those samples was
    taken (None whenever ``idle`` is): the coordinator stamps the persisted
    idle slope with it, so that stamp says when the drift was observed rather
    than when a refresh last happened to recompute it.

    v0.21.0: in the slopes MPC plans with, a recovery ``method_*`` of
    ``"previous"`` marks a value carried over from the previous cycle (see
    `carry_over_recovery_slopes`). The counts and spreads beside it still
    describe the latest run, as they do beside a cached idle slope -- and
    since v0.22.0 that run may have a fit of its own already, set aside until
    it has been watched for long enough.
    """

    idle: float | None
    recovery_heat: float | None
    recovery_cool: float | None
    sample_count: int
    window_minutes: float
    last_updated: datetime | None
    sample_count_idle: int = 0
    sample_count_recovery_heat: int = 0
    sample_count_recovery_cool: int = 0
    std_dev_idle: float = 0.0
    std_dev_recovery_heat: float = 0.0
    std_dev_recovery_cool: float = 0.0
    method_idle: str = "none"
    method_recovery_heat: str = "none"
    method_recovery_cool: str = "none"
    idle_measured_at: datetime | None = None

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

    def carried_over(self, action: str | None) -> bool:
        """Whether the recovery slope for `action` is the previous cycle's,
        carried over to a cycle too young for its own (v0.21.0). Always False
        for idle and unknown, which have no recovery slope."""
        if action == ACTION_HEAT:
            return self.method_recovery_heat == "previous"
        if action == ACTION_COOL:
            return self.method_recovery_cool == "previous"
        return False


# Slope-flatness threshold expressed in slope's native unit (°C/minute), so
# `decide()` doesn't redo the division on every refresh.
_SLOPE_EPSILON_PER_MINUTE: Final = SLOPE_EPSILON_PER_HOUR / 60.0


def sample_to_dict(s: Sample) -> SerializedSample:
    """Serialize a sample for the storage layer."""
    return {
        "t": s.t.isoformat(),
        "temp": s.temp,
        "action": s.action,
        "fan_mode": s.fan_mode,
    }


def sample_from_dict(d: SerializedSample) -> Sample | None:
    """Deserialize a sample. Returns None on any corrupt or missing field.

    Strict on every field (including missing keys) so a hand-edited or
    partially-written store can't crash the predictor and propagate the
    failure up to `async_setup`, which would mark the whole config entry
    failed. The bad entry is silently dropped and the rest of the buffer
    is preserved.

    `fan_mode` is the v0.8 addition. Missing key → None (the legacy v0.7
    case); explicit None → None; non-string-non-None → reject. The asymmetry
    with `action` (which rejects missing) reflects intent: action has been
    in the schema since v0.6 and any v0.6+ payload should have it, while
    fan_mode is `NotRequired` and v0.7 payloads legitimately lack it.
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
    # fan_mode: missing key or explicit None → None (v0.7 compat); else must
    # be a string. Same type-widening pattern as `action_raw` above.
    fan_mode_raw: object = d.get("fan_mode")
    if fan_mode_raw is not None and not isinstance(fan_mode_raw, str):
        return None
    return Sample(t=parsed, temp=temp, action=action_raw, fan_mode=fan_mode_raw)


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
    fan_mode: str | None = None,
) -> tuple[list[Sample], bool]:
    """Append a new sample, applying rate-limit + age cap.

    Returns `(new_samples, appended)`. When `appended=True` the returned list
    is a fresh copy; the caller should replace its cached reference. When
    `appended=False` the original list is returned unchanged (same reference)
    -- the caller can skip persisting.

    Rate-limit: skip the append if the previous sample was within
    SAMPLE_MIN_INTERVAL_S AND its action matches the incoming action.
    Action transitions are always recorded (the segmenter relies on them).
    Fan-mode changes are NOT treated as transitions in v0.8 — slope
    estimation ignores fan_mode, so a fan-mode change inside the rate-limit
    window would only inflate the buffer without affecting any v0.8 control.
    v0.9 (when slopes partition by fan_mode) will revisit this.

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
    pruned.append(Sample(t=now, temp=temp, action=action, fan_mode=fan_mode))
    if len(pruned) > SAMPLE_MAX_COUNT:
        pruned = pruned[-SAMPLE_MAX_COUNT:]
    return pruned, True


def _latest_run_of(
    samples: list[Sample], action: str, *, across_gaps: bool = False
) -> list[Sample]:
    """Most recent contiguous run of samples with `s.action == action`.

    Returns [] if no such run exists. Walks backward to find the latest
    matching sample, then expands the run boundary while predecessors
    match. The trailing-run constraint means each action's slope reflects
    the most recent cycle of that action — passive drift before a heat
    cycle does not bleed into the slope used for the next heat cycle.

    Contiguous in time as well as in label (v0.20.0): the run stops at the
    first gap of more than SAMPLE_MAX_GAP_MINUTES between consecutive
    samples. Nothing is sampled while the room sensor is dark, while the
    climate entity is unreachable (a dropped command appends nothing;
    only shadow mode and the min-cycle gates sample on regardless), or
    while Home Assistant is down, so the samples either side of such a gap
    were otherwise joined into one run spanning time nobody observed -- and
    the fit across it is set by whatever happened in the gap. Its two
    clusters sit far apart on the time axis, which gives the older one the
    leverage to tilt the line however little weight it carries: a room that
    stepped down during the gap and has held level since reads as still
    falling, for as long as samples from before the gap are among those
    fitted.

    With `across_gaps` the run is joined by label alone, as it was before
    v0.20.0: the whole stretch of the action the buffer can see, which is
    what the idle settle window is measured from (see `_settled`).
    """
    bounds = _run_bounds(samples, action, len(samples), across_gaps=across_gaps)
    if bounds is None:
        return []
    start, end = bounds
    return samples[start:end]


def _run_bounds(
    samples: list[Sample], action: str, stop: int, *, across_gaps: bool = False
) -> tuple[int, int] | None:
    """Where the most recent run of `action` before index `stop` lies.

    `(start, end)` such that `samples[start:end]` is the run, or None when no
    sample before `stop` has that action. The run is the one `_latest_run_of`
    describes, among `samples[:stop]` only: passing a run's `start` as `stop`
    finds the run of the same action before it.
    """
    end: int | None = None
    for i in range(stop - 1, -1, -1):
        if samples[i].action == action:
            end = i + 1
            break
    if end is None:
        return None
    max_gap = timedelta(minutes=SAMPLE_MAX_GAP_MINUTES)
    start = end - 1
    while (
        start > 0
        and samples[start - 1].action == action
        and (across_gaps or samples[start].t - samples[start - 1].t <= max_gap)
    ):
        start -= 1
    return start, end


def _settled(run: list[Sample], *, since: datetime) -> list[Sample]:
    """The part of an idle run that is passive drift rather than aftermath.

    Drops every sample in the first IDLE_SETTLE_MINUTES after `since`, when
    the idle stretch the run belongs to began. An idle run nearly
    always starts at the release of a heat or cool cycle, and until the room
    has settled it moves back against that cycle -- after cooling, fast enough
    to read as warming at a couple of degrees an hour. That is a fact about
    the cycle, not about the room: projected over MPC's horizon (and,
    persisted, for a day after) it makes a cold night look warm enough to
    cool against, and the next cycle's aftermath then confirms it.

    Measured from the stretch's first sample in the buffer whatever precedes
    it, rather than only when a heat or cool sample is visibly in front. The
    buffer cannot say what came before a stretch at its start: the release
    may just have aged out -- and trimming only visible releases was measured
    letting exactly those runs back in, the transient intact, once the cycle
    in front of them was pruned -- or a flush may have emptied it. The price
    is paid by a stretch that has idled for longer than the window: it loses
    its oldest half hour, under 4 % of the fit's weight but about a third of
    its leverage, so its slope is somewhat noisier (replayed over ten days of
    two zones, such slopes moved by a median 0.03 °C/h).

    A gap in sampling inside the stretch (v0.20.0) splits the run that is
    fitted, but not the window, which stays where the stretch began. The unit
    was left idle on both sides of the gap, and a cycle a stand-in commands in
    between empties the buffer instead (`_flush_samples_for_stand_in`), so
    there is normally no new release to wait out: the samples after the gap
    are fitted as soon as there are enough of them. Not always: when
    `set_hvac_mode` raises but the unit took the command anyway (a cloud
    timeout), nothing is recorded while the unit runs, and the aftermath of
    the cycle it ran is fitted as drift, as it was before v0.20.0. Restarting
    the window at every gap would wait that out, and was measured: over ten
    days of five zones it withheld the live slope three times as long, and the
    slope MPC planned with after an outage -- the cached one, meanwhile --
    was further from what the room went on to do than the joined fit had
    been. Once the samples from before a long gap have left the window,
    though, the stretch's first sample is the first one after the gap, and
    the window starts again there.
    """
    settled_from = since + timedelta(minutes=IDLE_SETTLE_MINUTES)
    return [s for s in run if s.t >= settled_from]


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


def _segment_std_dev(segment: list[Sample]) -> float:
    """Population standard deviation of sample temperatures (°C).

    Surfaced as a sensor attribute so users can spot resolution-limited
    data: a `std_dev_idle` near 0 over many samples means the sensor
    reported the same value throughout the window — the slope estimate
    is therefore unreliable (a 0.5 °C-resolution sensor on a slowly
    drifting room can read the same value for hours, masking the
    drift). Distinguishes "slope = 0 because the room is genuinely
    stable" from "slope = 0 because the sensor can't see the drift".
    Returns 0.0 for <2 samples (no variance to measure).
    """
    if len(segment) < 2:
        return 0.0
    mean = sum(s.temp for s in segment) / len(segment)
    return math.sqrt(sum((s.temp - mean) ** 2 for s in segment) / len(segment))


def _segment_slope(segment: list[Sample], *, now: datetime) -> tuple[float | None, str]:
    """Per-segment slope estimate with method bookkeeping.

    Returns ``(slope_per_minute, method)`` where ``method`` is one of:

      - ``"wls"``: WLS produced a slope; used as-is.
      - ``"none"``: no slope could be computed (too few samples or
        singular WLS denominator).

    The v0.9.1 diagnostic was originally going to include a
    baseline-fallback method for resolution-limited sensors, but
    investigation showed the fallback didn't help the realistic
    scenarios: when the window has no quantization crossings (the
    user's overnight gym case), both WLS and a first-to-last baseline
    return 0; when the window has a crossing, WLS detects it (with
    biased magnitude but correct direction). The honest answer is
    that fixing this requires a wider sample retention than the
    current 90 min, deferred to v1.0. The ``method`` field stays as
    a forward-compatible string so future fallback methods can be
    added without changing the attribute shape.
    """
    wls = _wls_slope(segment, now=now)
    if wls is None:
        return None, "none"
    return wls, "wls"


def _reject_wrong_sign(
    slope: float | None, method: str, *, want_positive: bool
) -> tuple[float | None, str]:
    """Discard a recovery slope whose sign is physically impossible.

    A *heating* segment cannot have a non-positive slope and a *cooling*
    segment cannot have a non-negative one — over a real segment, running the
    HVAC moves the room in the commanded direction. A wrong-sign estimate is
    sensor/segment noise (typically a handful of samples scraped from a few
    sub-minute action blips). Left in place it is actively harmful: MPC's
    forward simulation would believe "heating cools the room" and pick *idle*
    while the room sits below band, stranding it (and starving the estimator of
    the clean heat samples that would fix the slope — a self-reinforcing loop).

    Discarding it (→ ``None``, method ``"rejected"``) routes the decision to the
    reactive predictor / MPC bail-out, which heats when below band. Idle drift is
    legitimately ±, so this guard applies only to the recovery slopes.
    """
    if slope is None:
        return None, method
    if want_positive and slope <= 0:
        return None, "rejected"
    if not want_positive and slope >= 0:
        return None, "rejected"
    return slope, method


def estimate_slopes(samples: list[Sample], *, now: datetime) -> ThermalSlopes:
    """Compute per-action slopes over the most recent contiguous run of each
    action class, plus buffer bookkeeping for the sensor's attributes.

    The idle run is fitted without any samples in the first
    IDLE_SETTLE_MINUTES of its idle stretch (`_settled`); the recovery runs
    are fitted whole, since the cycle is what they measure.
    """
    idle_stretch = _latest_run_of(samples, ACTION_IDLE, across_gaps=True)
    idle_run = (
        _settled(_latest_run_of(samples, ACTION_IDLE), since=idle_stretch[0].t)
        if idle_stretch
        else []
    )
    heat_run = _latest_run_of(samples, ACTION_HEAT)
    cool_run = _latest_run_of(samples, ACTION_COOL)

    window_min = 0.0
    last_updated: datetime | None = None
    if samples:
        last_updated = samples[-1].t
        window_min = (samples[-1].t - samples[0].t).total_seconds() / 60.0

    idle_slope, idle_method = _segment_slope(idle_run, now=now)
    # Recovery slopes are sign-guarded: a heating segment that fits a
    # non-positive slope (or a cooling segment a non-negative one) is noise, and
    # using it makes MPC believe the HVAC pushes the room the wrong way -> it
    # idles below band. Idle drift keeps both signs.
    heat_slope, heat_method = _reject_wrong_sign(
        *_segment_slope(heat_run, now=now), want_positive=True
    )
    cool_slope, cool_method = _reject_wrong_sign(
        *_segment_slope(cool_run, now=now), want_positive=False
    )

    return ThermalSlopes(
        idle=idle_slope,
        recovery_heat=heat_slope,
        recovery_cool=cool_slope,
        sample_count=len(samples),
        window_minutes=round(window_min, 1),
        last_updated=last_updated,
        sample_count_idle=len(idle_run),
        sample_count_recovery_heat=len(heat_run),
        sample_count_recovery_cool=len(cool_run),
        std_dev_idle=round(_segment_std_dev(idle_run), 4),
        std_dev_recovery_heat=round(_segment_std_dev(heat_run), 4),
        std_dev_recovery_cool=round(_segment_std_dev(cool_run), 4),
        method_idle=idle_method,
        method_recovery_heat=heat_method,
        method_recovery_cool=cool_method,
        idle_measured_at=idle_run[-1].t if idle_slope is not None else None,
    )


def carry_over_recovery_slopes(
    slopes: ThermalSlopes, samples: list[Sample], *, now: datetime
) -> ThermalSlopes:
    """The slopes MPC plans with: `slopes`, except that a heat or cool cycle
    too young for a recovery slope of its own plans with the previous cycle's.

    Every cycle starts as a run of one sample, and a slope comes from the
    latest run alone, so the refresh after MPC starts a cycle finds no
    recovery slope for it. The earlier run MPC planned the start with is still
    in the buffer; it has just stopped being the latest. With no other
    recovery slope MPC is no longer ready and the reactive predictor takes
    over; with one, the action just started drops out of MPC's candidates.
    Either way a room inside its band is released at once. In production a
    cool cycle MPC started was released 58 seconds later. Ten days of five
    zones' recorded history, replayed through the decision stack, lost the
    recovery slope on the refresh after every one of the 97 cycles MPC
    started, and 59 of those cycles were released there. MPC then sat out a
    median of 68 minutes, because the short run went on hiding the earlier one
    until a reactive cycle long enough to fit replaced it.

    So until the new run is old enough for a fit of its own -- four samples,
    watched for CARRY_OVER_MIN_SPAN_MINUTES (since v0.22.0; see below) -- it
    borrows the slope of the run of that action before it, the run MPC started
    the cycle with, provided that run is long enough to fit. The borrowed fit
    goes through the v0.15.0 sign guard like any other, so a previous cycle
    whose fit was discarded lends nothing; and a new run old enough always
    keeps its own slope, or the guard's verdict on it. Only a new cycle
    borrows. A young run whose sample before it is of its own action -- split
    from it only by a gap in sampling -- is a cycle resumed after an outage,
    and v0.20.0 decided against planning its resumption with the slope from
    before the outage, since nothing says the unit kept doing what it was
    doing. A gap anywhere else, in the idle before the new cycle or straight
    before its first sample, still leaves a new cycle; and the previous
    cycle's slope stood across that gap anyway, since v0.20.0 leaves a cycle
    that ended before an outage its slope.

    The previous cycle only, not the last one long enough to fit further back.
    v0.21.0 chose that to bound MPC's chatter: with no switching hysteresis,
    MPC could end a cycle a sample or two in and start the next within
    minutes, and the short run in between lent nothing, so the next cycle went
    to the reactive path. Walking further back removed the bound -- simulating
    three hours on from each of the 97 starts at a one-minute cadence, it took
    the cycles of two and a half minutes or less from 98 to 182, where the
    previous cycle only took them to 65. Since v0.22.0 MPC does not end a
    cycle it is planning on a borrowed slope (`mpc.plan`), so a cycle it
    starts runs long enough to lend unless something else ends it first, and a
    run too short to lend comes from the band edge, a reversal to the other
    action (not held), MPC deferring, the reactive path's own cycles, or a
    hand edit. With the hold in place the walk back was measured again: it
    gained at most half a point of time in band (no lag, at a sample a minute)
    and 0.1-0.2 points on the rebound plant at five minutes, and nothing
    elsewhere, so the simpler rule stays.

    The carry-over ends when the young run has four samples and, while it is
    still running, has been watched for CARRY_OVER_MIN_SPAN_MINUTES. Four
    samples alone, as in v0.21.0, come three minutes in at about a sample a
    minute -- the fastest a run grows -- and a unit that had not moved the
    room by then left them drifting the old way: the fit came out the wrong
    way round, the sign guard discarded it, nothing is carried over a rejected
    fit, and the reactive path took the cycle at its fourth minute. Until the
    run has been watched for the span, its own fit -- the right way round or
    not -- is set aside for the borrowed one. The guard's verdict still stands
    on the fit MPC uses: the borrowed one passed it when the previous cycle
    was fitted, and the run's own is judged from the refresh it takes over.
    Once the run has ended its own fit stands, as before (see `_too_young`).
    At a five-minute cadence four samples normally span about fifteen minutes,
    so this rarely matters there: in ten days of five zones' history, extra
    samples gave a running cycle four inside ten minutes four times, and two
    of those changed a decision. A unit still not moving the room once the
    span is up loses the cycle then, as it did at the fourth minute before. It
    also ends, as a gap in sampling would end it, when a run still running has
    had no sample for longer than SAMPLE_MAX_GAP_MINUTES: the next sample
    would start a resumed run, which borrows nothing, and until then the run
    cannot grow out of its youth (see `_previous_cycle_slope`).

    Nothing outlives the buffer: the previous cycle is one the buffer still
    holds, which is pruned to the 90-minute window as samples are appended.
    In the replay that ran out once in the 97 starts, when the run MPC had
    started with lost its oldest sample to the window between one refresh
    and the next.

    For MPC only, like the cached idle slope: the reactive predictor goes on
    using the live slopes, so its anticipatory shutoff is untouched and a zone
    not on MPC controls exactly as before -- though its MPC shadow and the
    thermal_slope sensor do show the carried value, marked ``method_* ==
    "previous"``. That mark is also what MPC's hold keys on
    (`ThermalSlopes.carried_over`).
    """
    heat = _previous_cycle_slope(samples, ACTION_HEAT, now=now)
    cool = _previous_cycle_slope(samples, ACTION_COOL, now=now)
    if heat is not None:
        slopes = replace(slopes, recovery_heat=heat, method_recovery_heat="previous")
    if cool is not None:
        slopes = replace(slopes, recovery_cool=cool, method_recovery_cool="previous")
    return slopes


def _previous_cycle_slope(samples: list[Sample], action: str, *, now: datetime) -> float | None:
    """The recovery slope a new `action` cycle borrows until it has its own.

    None unless the latest run of `action` is too young for its own fit to
    stand, began at a change of action and -- if still running -- has had a
    sample within SAMPLE_MAX_GAP_MINUTES of `now`, and the run of `action`
    before it can be fitted and its fit survives the sign guard; see
    `carry_over_recovery_slopes` for why.
    """
    latest = _run_bounds(samples, action, len(samples))
    if latest is None or not _too_young(samples, *latest):
        return None
    start = latest[0]
    # The first run in the buffer has nothing before it to borrow from. The
    # same action straight before it makes it a cycle resumed after a gap in
    # sampling, which waits for a slope of its own.
    if start == 0 or samples[start - 1].action == action:
        return None
    # Nor does a run still running that has had no sample for longer than a
    # gap (v0.22.0). Whatever is appended next will start a resumed run, which
    # borrows nothing, so this stops borrowing now rather than then. Meanwhile
    # nothing is being appended -- a quiet sensor, or every command raising,
    # which for a cloud unit can still reach it -- so the run cannot grow out
    # of its youth, and without this it would hold MPC's cycle for as long as
    # the run it borrows from stayed fittable.
    if latest[1] == len(samples) and now - samples[-1].t > timedelta(
        minutes=SAMPLE_MAX_GAP_MINUTES
    ):
        return None
    previous = _run_bounds(samples, action, start)
    if previous is None or previous[1] - previous[0] < SLOPE_MIN_SAMPLES:
        return None
    slope, _ = _reject_wrong_sign(
        *_segment_slope(samples[previous[0] : previous[1]], now=now),
        want_positive=action == ACTION_HEAT,
    )
    return slope


def _too_young(samples: list[Sample], start: int, end: int) -> bool:
    """Whether the run `samples[start:end]` is too young for its own fit to
    replace a borrowed one: fewer samples than a fit needs, or still running
    and watched for less than CARRY_OVER_MIN_SPAN_MINUTES.

    The span is for a cycle still in progress, whose early samples may only
    show the unit coming up. Once the run has ended -- another action's sample
    after it -- its fit is all there is to say about that cycle, and it stands
    as it did before v0.22.0: the next cycle borrows it anyway. Left young, a
    short run that ended on a fit the guard discarded went on lending the
    cycle before it through the idle after it, and MPC started cycles on that
    slope which the run then had nothing to lend to."""
    if end - start < SLOPE_MIN_SAMPLES:
        return True
    if end < len(samples):
        return False
    span = samples[end - 1].t - samples[start].t
    return span < timedelta(minutes=CARRY_OVER_MIN_SPAN_MINUTES)


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
    passive_tolerance: float,
    hysteresis_decision: HysteresisDecision,
) -> HysteresisDecision:
    """Anticipate startup, shutoff, or passive recovery; otherwise defer to
    hysteresis.

    Takes pre-computed slopes (caller is expected to need them anyway for
    the thermal_slope sensor) -- avoids a redundant WLS pass per refresh.

    Three predictor behaviours, branched on `current_action`:

    - **Shutoff** (current heat/cool): projects forward at the recovery
      slope, releases to idle when projection reaches the hysteresis release
      threshold (`low` for heat, `high` for cool). Hysteresis itself
      releases at the edge, but thermal momentum then carries the room past;
      anticipating the crossing lets the room peak *at* the edge instead.

    - **Startup** (current idle/unknown, hysteresis says idle): projects
      forward at the idle slope, fires heat / cool when projection crosses
      the deadband entry edge (`low - deadband_below` / `high + deadband_above`).
      Same entry condition hysteresis would use later, just earlier.

    - **Passive drift acceptance** (current idle/unknown, hysteresis says
      heat/cool): hysteresis wants to act because the room is already past
      the deadband, but the slope is reversing -- natural recovery will
      return us to band within `lookahead_minutes`. Stay idle. Two guards
      apply: the forecast must move the room by at least
      `PASSIVE_FORECAST_MOVEMENT_MIN_C` toward the band (defends against
      false-positive suppression on sensor jitter), and the room must be
      within `passive_tolerance` °C of the band edge (per-zone comfort
      floor; 0 disables).

    Shutoff takes priority over startup -- stopping early matters more than
    starting early because overshoot is harder to recover from than late
    starts. The branches are mutually exclusive on `current_action` so at
    most one return fires per call.
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
    # ACTION_UNKNOWN routes into the idle branch because passive drift is
    # the only behavior the predictor can model when the previous action
    # is unknown (room sensor just came back, fresh boot, etc.). The idle
    # slope is computed from action==idle samples, so this means "if the
    # buffer has any idle history, use it."
    elif inputs.current_action in (ACTION_IDLE, ACTION_UNKNOWN) and slopes.idle is not None:
        projected = inputs.room + slopes.idle * lookahead_minutes

        # Anticipatory startup: room in band, slope predicts crossing out.
        if slopes.idle < -_SLOPE_EPSILON_PER_MINUTE and projected < (
            inputs.low - inputs.deadband_below
        ):
            return heat_decision(inputs.low)
        if slopes.idle > _SLOPE_EPSILON_PER_MINUTE and projected > (
            inputs.high + inputs.deadband_above
        ):
            return cool_decision(inputs.high)

        # Passive drift acceptance: hysteresis wants to act because room
        # is outside the band, but natural recovery will return us within
        # the lookahead window. The slope-sign predicates below cleanly
        # partition with the startup branches above -- startup heat needs
        # a cooling slope, passive heat needs a warming slope, so they
        # never both apply to the same input.
        if hysteresis_decision.action == ACTION_HEAT and (
            slopes.idle > _SLOPE_EPSILON_PER_MINUTE
            and projected >= inputs.low
            and projected - inputs.room >= PASSIVE_FORECAST_MOVEMENT_MIN_C
            and inputs.room >= inputs.low - passive_tolerance
        ):
            return idle_decision()
        if hysteresis_decision.action == ACTION_COOL and (
            slopes.idle < -_SLOPE_EPSILON_PER_MINUTE
            and projected <= inputs.high
            and inputs.room - projected >= PASSIVE_FORECAST_MOVEMENT_MIN_C
            and inputs.room <= inputs.high + passive_tolerance
        ):
            return idle_decision()

    return hysteresis_decision
