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

When ready but the room is clearly outside band on a side whose recovery
slope hasn't accumulated (e.g. a heat-only zone suddenly needs cooling),
`plan` defers to the predictor for that refresh — the predictor's
hysteresis fires the correct direction reactively. Silent fallback, same
posture as the not-ready path.

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
    that point). For v0.8 the action space is fixed at three.
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
) -> ActionScore:
    """Project room temp forward at the action's slope; sum minutes in band.

    1-minute integration steps (configurable via `MPC_SIMULATION_STEP_MINUTES`)
    means the cost function tracks any nonlinearities we might introduce
    later (v0.9 fan-mode segmentation, future outdoor-temp adjustments).
    Horizon <= 60 means at most 60 steps x 3 actions = 180 iterations per
    refresh — negligible.

    Defensive return when `inputs.room is None`: zero score, midpoint
    distance 0. `plan` gates on `room is not None` before calling, so this
    branch is a tripwire for future direct callers. Mirrors the rest of the
    codebase's "return UNKNOWN / no-op rather than raise" convention.
    """
    midpoint = (inputs.low + inputs.high) / 2.0
    if inputs.room is None:
        return ActionScore(
            action=action,
            time_in_band_minutes=0.0,
            end_temp=midpoint,
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
            midpoint_distance=abs(inputs.room - midpoint),
        )

    step_min = MPC_SIMULATION_STEP_MINUTES
    steps = round(horizon_minutes / step_min)
    temp = inputs.room
    time_in_band = 0.0
    for _ in range(steps):
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
        a_in = inputs.low <= temp <= inputs.high
        b_in = inputs.low <= next_temp <= inputs.high
        if a_in and b_in:
            time_in_band += step_min
        elif a_in or b_in:
            time_in_band += step_min * 0.5
        temp = next_temp

    return ActionScore(
        action=action,
        time_in_band_minutes=time_in_band,
        end_temp=temp,
        midpoint_distance=abs(temp - midpoint),
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
) -> HysteresisDecision:
    """Pick the highest-scoring action; fall back to predictor when not ready.

    Returns `HysteresisDecision` so coordinator routing is uniform across
    hyst / predictor / MPC paths.

    Ranking is by `(time_in_band_minutes desc, midpoint_distance asc)`. The
    midpoint tie-break stabilises against the common case where e.g. idle
    with flat slope and heat with mild positive slope both score the full
    horizon — without it, dict ordering on the candidate list would decide,
    which is fragile to refactors.
    """
    if inputs.room is None:
        return UNKNOWN_DECISION
    if not is_ready(slopes):
        return predictor_decision

    # Safety bail-out: the room is clearly outside band on a side whose
    # recovery slope we don't have. MPC's "best of available" would likely
    # pick idle (the only meaningful candidate when the matching recovery
    # is missing), leaving the room out of band. The predictor's per-branch
    # fallback (and hysteresis behind it) will fire the right direction
    # reactively — defer cleanly. Silent fallback consistent with the
    # not-ready path above.
    if inputs.room < inputs.low and slopes.recovery_heat is None:
        return predictor_decision
    if inputs.room > inputs.high and slopes.recovery_cool is None:
        return predictor_decision

    # Drop candidates whose matching recovery slope is unavailable.
    # `ThermalSlopes.for_action` is the single dispatch point for "which
    # slope does this action produce" — reusing it keeps this filter and
    # `simulate`'s slope-pick consistent. Idle (slopes.idle) is guaranteed
    # available by `is_ready` above, so idle always survives the filter.
    candidates = [a for a in enumerate_actions(inputs) if slopes.for_action(a.kind) is not None]
    scores = [simulate(a, slopes, inputs, horizon_minutes=horizon_minutes) for a in candidates]
    # Larger time_in_band wins; on ties, smaller midpoint_distance wins
    # (closer to band centre).
    best = max(
        scores,
        key=lambda s: (s.time_in_band_minutes, -s.midpoint_distance),
    )
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
