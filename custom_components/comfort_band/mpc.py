"""Model-predictive controller for Comfort Band.

At each refresh, enumerates a small action space (`idle`, `heat` to the band's
upper edge, `cool` to the band's lower edge), simulates each forward over
`mpc_horizon_minutes` using the learned per-action slope, and picks the
action that maximises projected time-in-band. Returns a `HysteresisDecision`
so downstream apply logic (min-cycle gates, climate.set_* calls) is unchanged.

Why heat targets the high edge (and cool targets the low edge) rather than
the band edge itself: the hysteresis decision constructors pass `target_temp`
through to `climate.set_temperature`. If we tell the climate `low` as the
heat target, its internal hysteresis will release at `low` independent of
our coordinator's release timing — meaning the room peaks at `low` and the
room/band oscillation lives across the band edge (the v0.7 problem). By
telling the climate the upper edge, the climate keeps heating until *we*
issue idle. The MPC's cost function re-evaluates idle every refresh and
picks it once projected drift-down stays inside band longer than projected
continued heating.

Cold-start gate (v0.8.1+): requires `idle_slope` (the cost-function
baseline) plus at least one recovery slope. Heat-only zones (winter / fresh
install in cold months) activate MPC once `idle` + `recovery_heat` slopes
accumulate; cool-only zones once `idle` + `recovery_cool` are available;
fully-equipped zones get the richest decision surface (all three
candidates). Falls back to the v0.7 predictor silently when not ready —
the caller (coordinator) exposes the gate state via the `mpc_ready` binary
sensor so users see why MPC isn't firing.

When ready but the room is at or outside band on a side whose recovery
slope hasn't accumulated (e.g. a heat-only zone suddenly needs cooling),
`plan` defers to the predictor for that refresh — the predictor's
hysteresis fires the correct direction reactively. The bail-out's boundary
is inclusive (`<=` / `>=`) to match `simulate`'s band-membership check
and close a single-refresh edge case where MPC could pick idle at the
exact band edge. Silent fallback, same posture as the not-ready path.

The planner is pure: state in / decision out, no IO. `simulate` does its
own 1-minute integration so the cost function tracks any nonlinearities the
caller wants to introduce later (e.g., v0.9's fan-mode-conditional slopes).
Linear projection alone would compute time-in-band in closed form, but the
explicit step loop keeps the door open without adding caller burden.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    MPC_SIMULATION_STEP_MINUTES,
)
from .hysteresis import (
    UNKNOWN_DECISION,
    HysteresisDecision,
    HysteresisInputs,
    cool_decision,
    heat_decision,
    idle_decision,
)
from .predictor import ThermalSlopes, project


@dataclass(frozen=True)
class Action:
    """A candidate action MPC considers for the current refresh.

    `kind` is one of ACTION_IDLE / ACTION_HEAT / ACTION_COOL (the same labels
    `HysteresisDecision.action` uses, so no translation is needed at the
    planner→decision boundary). `target_temp` is the value the coordinator
    will pass to `climate.set_temperature` when this action is chosen — None
    for idle (no setpoint to issue).
    """

    kind: str
    target_temp: float | None


@dataclass(frozen=True)
class ActionScore:
    """Simulation outcome for one candidate action.

    `time_in_band_minutes` is the primary cost-function value (maximised).
    `end_temp` and `midpoint_distance` support the tie-break: when two
    actions both stay in band the whole horizon, prefer the one whose
    end-of-horizon position is closer to band midpoint. This stabilises
    against indeterminacy when e.g. idle (flat slope) and heat (mild slope)
    both score the full horizon.
    """

    action: Action
    time_in_band_minutes: float
    end_temp: float
    midpoint_distance: float


def enumerate_actions(inputs: HysteresisInputs) -> list[Action]:
    """Return the v0.8 candidate set: idle, heat-to-high-edge, cool-to-low-edge.

    v0.9 will expand this to include per-fan-mode variants by reading the
    climate entity's `fan_modes` attribute (passed in via HysteresisInputs at
    that point). For v0.8 the structural action space is fixed at three.

    Note: v0.8.1+ `plan` filters this list by available recovery slopes
    before scoring — unilateral-mode zones (heat-only or cool-only) end up
    with two effective candidates at runtime. `enumerate_actions` itself
    stays fixed so the structural action space is independent of the
    current slope state.
    """
    return [
        Action(ACTION_IDLE, None),
        Action(ACTION_HEAT, inputs.high),
        Action(ACTION_COOL, inputs.low),
    ]


def simulate(
    action: Action,
    slopes: ThermalSlopes,
    inputs: HysteresisInputs,
    *,
    horizon_minutes: int,
    bands_per_step: list[tuple[float, float]] | None = None,
) -> ActionScore:
    """Project room temp forward at the action's slope; sum minutes in band.

    1-minute integration steps (configurable via `MPC_SIMULATION_STEP_MINUTES`)
    means the cost function tracks any nonlinearities we might introduce
    later (v0.9 fan-mode segmentation, future outdoor-temp adjustments).
    Horizon <= 60 means at most 60 steps x 3 actions = 180 iterations per
    refresh — negligible.

    When ``bands_per_step`` is provided (v0.9.0+), the in-band check at
    step ``i`` uses ``bands_per_step[i]`` instead of ``inputs.low /
    inputs.high``. This lets the cost function see upcoming schedule
    transitions: if the comfort band rises at minute 30, the score for
    "idle now" reflects the room being below the NEW band by then, so
    "heat now" (which would land the room inside the new band) wins
    correctly. When ``bands_per_step`` is ``None`` (legacy path /
    callers that don't care about lookahead), the band is held frozen
    at the snapshot — preserves all v0.8.x test behaviour.

    The midpoint tie-break uses the END band's midpoint (last entry in
    ``bands_per_step``) so a final position is judged against where
    band centre IS at end-of-horizon, not where it WAS at start. Bands
    are always raw temperatures (°C) — apparent-temp adjustment is
    applied upstream to ``decision_room`` in the coordinator; band
    edges remain raw by design.

    Defensive return when `inputs.room is None`: zero score, midpoint
    distance 0. `plan` gates on `room is not None` before calling, so this
    branch is a tripwire for future direct callers. Mirrors the rest of the
    codebase's "return UNKNOWN / no-op rather than raise" convention.
    """
    # End-of-horizon midpoint anchors the tie-break. With lookahead the
    # band can shift across the horizon; using the snapshot midpoint
    # would mis-score "closer to band centre" for actions whose end_temp
    # lands in the new band's range. `is not None` matches the per-step
    # loop's guard below — both should agree so an empty list (which
    # `upcoming_bands` never produces for horizon > 0, but a future caller
    # might) raises predictably from `bands_per_step[i]` rather than
    # silently falling back to the snapshot path here.
    if bands_per_step is not None:
        end_low, end_high = bands_per_step[-1]
    else:
        end_low, end_high = inputs.low, inputs.high
    end_midpoint = (end_low + end_high) / 2.0
    if inputs.room is None:
        return ActionScore(
            action=action,
            time_in_band_minutes=0.0,
            end_temp=end_midpoint,
            midpoint_distance=0.0,
        )

    # Reuse ThermalSlopes.for_action so the slope-pick logic lives in exactly
    # one place (the sibling predictor sensor also uses it). Action.kind uses
    # the same ACTION_* labels for_action expects.
    slope = slopes.for_action(action.kind)
    if slope is None:
        # Caller is expected to gate on is_ready before reaching here; this
        # branch keeps simulate robust if the action space ever expands to
        # candidates whose slope can be selectively unavailable.
        return ActionScore(
            action=action,
            time_in_band_minutes=0.0,
            end_temp=inputs.room,
            midpoint_distance=abs(inputs.room - end_midpoint),
        )

    step_min = MPC_SIMULATION_STEP_MINUTES
    steps = round(horizon_minutes / step_min)
    temp = inputs.room
    time_in_band = 0.0
    for i in range(steps):
        # Per-step band: forward-looking when bands_per_step is provided,
        # frozen snapshot otherwise. If the schedule rotates at minute 30
        # of a 60-minute horizon, the second half of this loop scores
        # against the post-rotation band.
        if bands_per_step is not None:
            low_i, high_i = bands_per_step[i]
        else:
            low_i, high_i = inputs.low, inputs.high
        # Trapezoidal rule: a step counts as "in band" by the proportion of
        # the segment whose endpoints are both in [low, high]. Simpler than
        # interpolating zero-crossings; conservative when one endpoint is
        # out of band (counts the in-band endpoint at 0.5 * step, not the
        # full step). Adequate for cost-function ranking; we're comparing
        # actions, not estimating absolute minutes-in-band exactly.
        # `project` is the shared single-step projector defined in predictor.py
        # — using it keeps the slope-extrapolation formula in one place. The
        # `is None` branch is unreachable (slope is None-checked above);
        # narrow for mypy and serve as a tripwire if simulate ever loses its
        # None-guard contract.
        next_temp = project(temp, slope, step_min)
        if next_temp is None:  # pragma: no cover — slope guaranteed non-None
            next_temp = temp
        a_in = low_i <= temp <= high_i
        b_in = low_i <= next_temp <= high_i
        if a_in and b_in:
            time_in_band += step_min
        elif a_in or b_in:
            time_in_band += step_min * 0.5
        temp = next_temp

    return ActionScore(
        action=action,
        time_in_band_minutes=time_in_band,
        end_temp=temp,
        midpoint_distance=abs(temp - end_midpoint),
    )


def is_ready(slopes: ThermalSlopes) -> bool:
    """True when MPC has the slope data to compare at least two candidates.

    Requires `idle_slope` (the cost-function baseline — every refresh scores
    "stay idle" against the alternatives) and at least one recovery slope
    (otherwise there's only one candidate and nothing to compare against).

    Heat-only zones (winter use, or fresh install in cold months) reach this
    state after the first idle and heat segments accumulate; cool-only zones
    after the first idle and cool segments. Fully-equipped zones (both
    recoveries) get the richest decision surface — `plan` will pick from
    `{idle, heat, cool}` rather than `{idle, heat}` or `{idle, cool}`.

    v0.8.0 required all three slopes; that locked unilateral-mode zones
    (heat-only / cool-only) out of MPC entirely because the unused direction's
    slope would never accumulate. v0.8.1 relaxes the gate and handles the
    rare "wrong-direction-needed" case via a per-refresh safety bail-out in
    `plan`.
    """
    return slopes.idle is not None and (
        slopes.recovery_heat is not None or slopes.recovery_cool is not None
    )


def plan(
    slopes: ThermalSlopes,
    inputs: HysteresisInputs,
    *,
    horizon_minutes: int,
    predictor_decision: HysteresisDecision,
    bands_per_step: list[tuple[float, float]] | None = None,
) -> HysteresisDecision:
    """Pick the highest-scoring action; fall back to predictor when not ready.

    Returns `HysteresisDecision` so coordinator routing is uniform across
    hyst / predictor / MPC paths.

    Ranking is by ``(time_in_band_minutes desc, midpoint_distance asc)``
    with one v0.9.0 refinement: if the best non-idle action and idle BOTH
    achieve full-horizon time-in-band (within one simulation step's
    rounding), prefer idle to avoid unnecessary HVAC activation. This
    closes the user's "cooling in winter" report — room slightly above
    high band, idle drifts it back in ~10 min, cool brings it in ~1 min;
    both produce ~30 min in-band over a 30-min horizon, so old code
    picked cool on midpoint tie-break despite idle being equally
    in-band and cheaper. The next refresh re-evaluates; if idle stops
    achieving full horizon, the active action wins normally.

    ``bands_per_step`` (v0.9.0+) carries the per-step ``(low, high)``
    over the horizon so ``simulate`` can score against an evolving
    band. The safety bail-out below reads ``bands_per_step[0]`` (the
    band active RIGHT NOW per the lookahead, which may differ from the
    snapshot if a refresh straddles a transition boundary) instead of
    ``inputs.low / inputs.high``.
    """
    if inputs.room is None:
        return UNKNOWN_DECISION
    if not is_ready(slopes):
        return predictor_decision

    # Safety bail-out: the room is at or outside band on a side whose
    # recovery slope we don't have. MPC's "best of available" would likely
    # pick idle (the only meaningful candidate when the matching recovery
    # is missing), leaving the room drifting further out of band. The
    # predictor's per-branch fallback (and hysteresis behind it) will fire
    # the right direction reactively — defer cleanly. Silent fallback
    # consistent with the not-ready path above.
    #
    # Inclusive `<=` / `>=` matches `simulate`'s band-membership check
    # (`low <= temp <= high`) — when room sits exactly at the edge, the
    # idle candidate scores indeterminately on the missing-recovery side
    # and the tie-break could pick idle for one refresh before the next
    # round catches it. Inclusive bail-out closes that 1-refresh gap.
    #
    # The bail-out reads bands_per_step[0] when lookahead is provided so
    # a refresh that lands exactly AT a schedule transition compares
    # against the new band (the band MPC will be optimizing into), not
    # the snapshot. Falls back to inputs.low / high for the legacy
    # callers / tests that don't pass bands_per_step.
    bail_low, bail_high = bands_per_step[0] if bands_per_step else (inputs.low, inputs.high)
    if inputs.room <= bail_low and slopes.recovery_heat is None:
        return predictor_decision
    if inputs.room >= bail_high and slopes.recovery_cool is None:
        return predictor_decision

    # Drop candidates whose matching recovery slope is unavailable.
    # `ThermalSlopes.for_action` is the single dispatch point for "which
    # slope does this action produce" — reusing it keeps this filter and
    # `simulate`'s slope-pick consistent. Idle (slopes.idle) is guaranteed
    # available by `is_ready` above, so idle always survives the filter.
    candidates = [a for a in enumerate_actions(inputs) if slopes.for_action(a.kind) is not None]
    scores = [
        simulate(
            a,
            slopes,
            inputs,
            horizon_minutes=horizon_minutes,
            bands_per_step=bands_per_step,
        )
        for a in candidates
    ]
    # Larger time_in_band wins; on ties, smaller midpoint_distance wins
    # (closer to band centre).
    best = max(
        scores,
        key=lambda s: (s.time_in_band_minutes, -s.midpoint_distance),
    )
    # Idle-preference tie-break (v0.9.0): when idle achieves essentially
    # full-horizon in-band coverage (within one simulation step's
    # rounding), prefer it over heat / cool to avoid unnecessary HVAC
    # cycles. The midpoint tie-break above already preserves the
    # "robust against disturbance" choice between two recovery actions;
    # this rule only fires when idle is BARELY behind on the primary
    # criterion AND inactive is the cheaper baseline. The next refresh
    # re-evaluates with fresh data — if idle loses coverage, the
    # original best wins on the next pass.
    idle_score = next((s for s in scores if s.action.kind == ACTION_IDLE), None)
    if (
        idle_score is not None
        and best.action.kind != ACTION_IDLE
        and idle_score.time_in_band_minutes >= horizon_minutes - MPC_SIMULATION_STEP_MINUTES
    ):
        best = idle_score
    if best.action.kind == ACTION_IDLE:
        return idle_decision()
    # target_temp is non-None for heat / cool actions (enumerate_actions
    # constructs them with inputs.high / inputs.low). The fallthrough below
    # defends against a future enumerate_actions that introduces a heat or
    # cool candidate without a target_temp — degrade to predictor's decision
    # rather than crash, matching the rest of the module's no-raise contract.
    if best.action.target_temp is None:
        return predictor_decision
    if best.action.kind == ACTION_HEAT:
        return heat_decision(best.action.target_temp)
    if best.action.kind == ACTION_COOL:
        return cool_decision(best.action.target_temp)
    # Explicit fallthrough: v0.9 may grow the action space (fan-mode
    # candidates), and an unrecognised `kind` here should defer to the
    # predictor rather than silently map to cool. Belt-and-suspenders.
    return predictor_decision
