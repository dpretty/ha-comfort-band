"""Tests for the pure asymmetric-deadband decider."""

from __future__ import annotations

import pytest

from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
)
from custom_components.comfort_band.hysteresis import (
    HysteresisDecision,
    HysteresisInputs,
    decide,
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


# ----- room=None: unknown, no climate write -----


def test_unknown_when_room_is_none() -> None:
    decision = decide(_inputs(None))
    assert decision == HysteresisDecision(action=ACTION_UNKNOWN, target_mode=None, target_temp=None)


def test_unknown_even_when_already_heating() -> None:
    decision = decide(_inputs(None, current=ACTION_HEAT))
    assert decision.action == ACTION_UNKNOWN
    assert decision.target_mode is None


# ----- entry from idle -----


def test_idle_to_heat_when_well_below_low() -> None:
    decision = decide(_inputs(19.6))  # 0.4 below low=20, exceeds db_below=0.3
    assert decision == HysteresisDecision(
        action=ACTION_HEAT, target_mode=HVAC_MODE_HEAT, target_temp=20.0
    )


def test_idle_to_cool_when_well_above_high() -> None:
    decision = decide(_inputs(23.6))  # 0.6 above high=23, exceeds db_above=0.5
    assert decision == HysteresisDecision(
        action=ACTION_COOL, target_mode=HVAC_MODE_COOL, target_temp=23.0
    )


def test_idle_stays_idle_inside_band() -> None:
    decision = decide(_inputs(21.5))
    assert decision == HysteresisDecision(
        action=ACTION_IDLE, target_mode=HVAC_MODE_FAN_ONLY, target_temp=None
    )


def test_idle_stays_idle_within_deadband_below_low() -> None:
    # 0.2 below low=20 -> still inside the deadband (0.3), so no heat.
    decision = decide(_inputs(19.8))
    assert decision.action == ACTION_IDLE


def test_idle_stays_idle_within_deadband_above_high() -> None:
    # 0.4 above high=23 -> still inside the deadband (0.5), so no cool.
    decision = decide(_inputs(23.4))
    assert decision.action == ACTION_IDLE


def test_idle_to_heat_at_exact_threshold_below() -> None:
    # room < low - db_below is strict, so room = low - db_below stays idle.
    just_inside = decide(_inputs(20.0 - 0.3))
    assert just_inside.action == ACTION_IDLE
    just_outside = decide(_inputs(20.0 - 0.3 - 0.001))
    assert just_outside.action == ACTION_HEAT


def test_idle_to_cool_at_exact_threshold_above() -> None:
    just_inside = decide(_inputs(23.0 + 0.5))
    assert just_inside.action == ACTION_IDLE
    just_outside = decide(_inputs(23.0 + 0.5 + 0.001))
    assert just_outside.action == ACTION_COOL


# ----- holding mode (no premature release) -----


def test_heat_holds_while_below_low() -> None:
    decision = decide(_inputs(19.5, current=ACTION_HEAT))
    assert decision.action == ACTION_HEAT
    assert decision.target_temp == 20.0


def test_cool_holds_while_above_high() -> None:
    decision = decide(_inputs(23.5, current=ACTION_COOL))
    assert decision.action == ACTION_COOL
    assert decision.target_temp == 23.0


def test_heat_holds_within_deadband_below_low() -> None:
    # Already heating; even though room=19.8 is inside the entry deadband,
    # we keep heating until we cross the band edge (low=20).
    decision = decide(_inputs(19.8, current=ACTION_HEAT))
    assert decision.action == ACTION_HEAT


# ----- release back to idle -----


def test_heat_releases_when_room_reaches_low() -> None:
    decision = decide(_inputs(20.0, current=ACTION_HEAT))
    assert decision.action == ACTION_IDLE
    assert decision.target_mode == HVAC_MODE_FAN_ONLY


def test_heat_releases_above_low() -> None:
    decision = decide(_inputs(20.5, current=ACTION_HEAT))
    assert decision.action == ACTION_IDLE


def test_cool_releases_when_room_reaches_high() -> None:
    decision = decide(_inputs(23.0, current=ACTION_COOL))
    assert decision.action == ACTION_IDLE
    assert decision.target_mode == HVAC_MODE_FAN_ONLY


def test_cool_releases_below_high() -> None:
    decision = decide(_inputs(22.0, current=ACTION_COOL))
    assert decision.action == ACTION_IDLE


# ----- asymmetric deadband -----


@pytest.mark.parametrize(
    ("room", "expected_action"),
    [
        # Symmetric distance from band edges; asymmetric deadbands
        # (below=0.3, above=0.5) make heat fire on a smaller excursion.
        (19.65, ACTION_HEAT),  # 0.35 below low (>0.3) -> heat
        (23.35, ACTION_IDLE),  # 0.35 above high (<0.5) -> still idle
        (23.55, ACTION_COOL),  # 0.55 above high (>0.5) -> cool
        (19.45, ACTION_HEAT),  # 0.55 below low (>0.3) -> heat
    ],
)
def test_asymmetric_deadband_thresholds(room: float, expected_action: str) -> None:
    assert decide(_inputs(room)).action == expected_action
