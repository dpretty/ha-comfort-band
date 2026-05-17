"""Apparent temperature — Steadman 1994, simplified for indoor (no wind).

Why this formula: NWS heat-index and humexes are degenerate below ~27 °C
(they return the input unchanged), making them useless for typical European
indoor temperatures of 15-22 °C. The Steadman 1994 "apparent temperature"
form works across the full residential indoor range and is simple enough
to keep here without a numerical-stability surprise.

    AT = T + 0.33 · e - 4.00
    e  = (RH / 100) · 6.105 · exp(17.27 · T / (237.7 + T))

`e` is the water-vapor partial pressure derived from the August-Roche-Magnus
saturation-vapor approximation, scaled by relative humidity. The constants
match the Australian BoM documentation; AT is in °C.
"""

from __future__ import annotations

import math


def compute(temp_c: float, humidity_pct: float | None) -> float:
    """Return apparent temperature in °C.

    If humidity is None or out of [0, 100], returns `temp_c` unchanged —
    so callers can pass whatever the sensor reports and get a safe value
    when humidity isn't available. The whole point of the toggle is to
    let users keep `use_apparent_temperature=True` without worrying that
    a flaky humidity sensor will silently break decisions.
    """
    if humidity_pct is None or not 0.0 <= humidity_pct <= 100.0:
        return temp_c
    vapor_pressure = (humidity_pct / 100.0) * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
    return temp_c + 0.33 * vapor_pressure - 4.00
