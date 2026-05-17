"""Tests for the Steadman-1994 apparent-temperature pure module."""

from __future__ import annotations

import math

import pytest

from custom_components.comfort_band.apparent_temp import compute


def test_no_humidity_returns_temp_unchanged() -> None:
    # Caller convention: pass None when humidity is unavailable. The
    # decision pipeline must keep working off the raw room temp in that
    # case.
    assert compute(22.0, None) == 22.0
    assert compute(15.5, None) == 15.5
    assert compute(28.0, None) == 28.0


def test_negative_humidity_returns_temp_unchanged() -> None:
    # Defensive against a misconfigured / broken humidity sensor that
    # publishes a nonsensical value.
    assert compute(22.0, -5.0) == 22.0


def test_humidity_over_100_returns_temp_unchanged() -> None:
    assert compute(22.0, 150.0) == 22.0


def test_humidity_exactly_zero_produces_steadman_baseline() -> None:
    # 0% RH is physically valid (zero vapor pressure → AT = T - 4.00).
    # Documented in the compute() docstring: future tightening might
    # clamp this, but for now it stays honest to the formula. This test
    # pins the current behaviour so an accidental change is caught.
    assert math.isclose(compute(22.0, 0.0), 18.0, abs_tol=0.01)


def test_low_humidity_makes_room_feel_cooler() -> None:
    # 20 °C and 30 % RH → feels-like below 20 (the constant -4.00 in the
    # formula dominates when vapor pressure is low). Steadman reference
    # value for these inputs: 18.31 °C.
    at = compute(20.0, 30.0)
    assert at < 20.0
    assert math.isclose(at, 18.31, abs_tol=0.05)


def test_high_humidity_at_warm_temp_makes_room_feel_hotter() -> None:
    # 28 °C and 80 % RH → feels-like clearly above 28 — the canonical
    # "tropical" sticky-warm case. Steadman reference: 33.95 °C.
    at = compute(28.0, 80.0)
    assert at > 28.0
    assert math.isclose(at, 33.95, abs_tol=0.05)


def test_neutral_humidity_at_temperate_room() -> None:
    # 22 °C, 50 % RH — a UK living room in winter. Apparent temp lands
    # very close to actual; confirms the formula isn't biased. Steadman
    # reference: 22.35 °C.
    at = compute(22.0, 50.0)
    assert math.isclose(at, 22.35, abs_tol=0.05)


@pytest.mark.parametrize(
    "temp,humidity",
    [
        (15.0, 40.0),
        (18.0, 55.0),
        (22.0, 65.0),
        (25.0, 70.0),
    ],
)
def test_finite_output_across_typical_indoor_range(temp: float, humidity: float) -> None:
    # Spot-check that no input in the realistic indoor envelope produces
    # NaN / Inf — e.g. a logarithmic blow-up from a constant typo.
    at = compute(temp, humidity)
    assert math.isfinite(at)
    # Sanity: within ±5 °C of the input. Tighter than the actual formula
    # output but loose enough to catch a sign error.
    assert abs(at - temp) < 5.0
