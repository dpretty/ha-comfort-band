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

Strict cold-start gate: requires all three slopes (idle, recovery_heat,
recovery_cool) to be present. Falls back to the v0.7 predictor decision
silently when any slope is None — the caller (coordinator) exposes the gate
state via the `mpc_ready` binary sensor so users see why MPC isn't firing.

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
from .predictor import ThermalSlopes


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


def _slope_for(action: Action, slopes: ThermalSlopes) -> float | None:
    """Pick the slope this action would produce. None when MPC isn't ready
    (caller is expected to gate on `is_ready` first, but be defensive).
    """
    if action.kind == ACTION_HEAT:
        return slopes.recovery_heat
    if action.kind == ACTION_COOL:
        return slopes.recovery_cool
    return slopes.idle


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

    `inputs.room` is asserted non-None by the caller (`plan` returns
    UNKNOWN_DECISION before reaching here when room is None).
    """
    assert inputs.room is not None, "simulate called with room=None — caller bug"

    slope = _slope_for(action, slopes)
    if slope is None:
        # Caller is expected to gate on is_ready before reaching here; this
        # branch keeps simulate robust if the action space ever expands to
        # candidates whose slope can be selectively unavailable.
        return ActionScore(
            action=action,
            time_in_band_minutes=0.0,
            end_temp=inputs.room,
            midpoint_distance=abs(inputs.room - (inputs.low + inputs.high) / 2.0),
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
        next_temp = temp + slope * step_min
        a_in = inputs.low <= temp <= inputs.high
        b_in = inputs.low <= next_temp <= inputs.high
        if a_in and b_in:
            time_in_band += step_min
        elif a_in or b_in:
            time_in_band += step_min * 0.5
        temp = next_temp

    midpoint = (inputs.low + inputs.high) / 2.0
    return ActionScore(
        action=action,
        time_in_band_minutes=time_in_band,
        end_temp=temp,
        midpoint_distance=abs(temp - midpoint),
    )


def is_ready(slopes: ThermalSlopes) -> bool:
    """True when all three slopes are available; gates MPC activation.

    Stricter than the v0.7 predictor (which falls through per-branch on
    missing slopes) — MPC enumerates the full action space, so partial data
    would lead to non-comparable scores. A zone that has only ever heated
    will have `recovery_cool=None` indefinitely (correct, not a bug); the
    coordinator silently uses the v0.7 predictor for those zones until the
    first cool segment accumulates SLOPE_MIN_SAMPLES samples.
    """
    return (
        slopes.idle is not None
        and slopes.recovery_heat is not None
        and slopes.recovery_cool is not None
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

    scores = [
        simulate(a, slopes, inputs, horizon_minutes=horizon_minutes)
        for a in enumerate_actions(inputs)
    ]
    # Larger time_in_band wins; on ties, smaller midpoint_distance wins
    # (closer to band centre).
    best = max(
        scores,
        key=lambda s: (s.time_in_band_minutes, -s.midpoint_distance),
    )
    if best.action.kind == ACTION_IDLE:
        return idle_decision()
    if best.action.kind == ACTION_HEAT:
        # target_temp is non-None for heat / cool actions (enumerate_actions
        # constructs them with inputs.high / inputs.low). Assert keeps mypy
        # honest and serves as a tripwire if enumerate_actions changes.
        assert best.action.target_temp is not None
        return heat_decision(best.action.target_temp)
    # best.action.kind == ACTION_COOL
    assert best.action.target_temp is not None
    return cool_decision(best.action.target_temp)
