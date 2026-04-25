"""Pure asymmetric-deadband decider.

`decide()` is a single-shot pure function: given the current room temperature,
the effective band, the per-side deadbands, and the action currently in progress,
it returns the desired terminal state. The coordinator handles the side effects
(min-cycle suppression, dispatching to climate.set_hvac_mode + set_temperature).

Hysteresis loop:
  - Enter heat at room < low - deadband_below; release at room >= low.
  - Enter cool at room > high + deadband_above; release at room <= high.
  - In band -> idle (fan_only).
  - room is None -> unknown (caller does not write to climate).
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
)


@dataclass(frozen=True)
class HysteresisInputs:
    """Snapshot the decider operates on. Caller assembles per refresh."""

    room: float | None
    low: float
    high: float
    deadband_below: float
    deadband_above: float
    current_action: str  # one of ACTION_*


@dataclass(frozen=True)
class HysteresisDecision:
    """Result. `target_mode is None` means the caller should NOT write climate."""

    action: str  # one of ACTION_*
    target_mode: str | None  # HVAC_MODE_* or None
    target_temp: float | None  # accompanies set_hvac_mode for heat/cool only


_UNKNOWN = HysteresisDecision(action=ACTION_UNKNOWN, target_mode=None, target_temp=None)


def _idle() -> HysteresisDecision:
    return HysteresisDecision(action=ACTION_IDLE, target_mode=HVAC_MODE_FAN_ONLY, target_temp=None)


def _heat(low: float) -> HysteresisDecision:
    return HysteresisDecision(action=ACTION_HEAT, target_mode=HVAC_MODE_HEAT, target_temp=low)


def _cool(high: float) -> HysteresisDecision:
    return HysteresisDecision(action=ACTION_COOL, target_mode=HVAC_MODE_COOL, target_temp=high)


def decide(inputs: HysteresisInputs) -> HysteresisDecision:
    """Resolve the desired action given the current snapshot."""
    if inputs.room is None:
        return _UNKNOWN

    if inputs.current_action == ACTION_HEAT:
        # Hold heat until we reach the band's lower edge.
        if inputs.room >= inputs.low:
            return _idle()
        return _heat(inputs.low)

    if inputs.current_action == ACTION_COOL:
        # Hold cool until we reach the band's upper edge.
        if inputs.room <= inputs.high:
            return _idle()
        return _cool(inputs.high)

    # Idle / unknown: apply entry thresholds with deadband.
    if inputs.room < inputs.low - inputs.deadband_below:
        return _heat(inputs.low)
    if inputs.room > inputs.high + inputs.deadband_above:
        return _cool(inputs.high)
    return _idle()
