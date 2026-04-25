"""Legacy YAML schedule importer.

The legacy band-control system stored each zone's schedule as 48 numeric
helper entities -- `input_number.{zone}_hour_HH_low` and `_high` for HH
00..23. The `comfort_band.import_legacy` service reads those helpers and
collapses them into a transition list (via `schedule.import_legacy_hourly`),
which is then written into the zone's `home` profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .schedule import Transition, import_legacy_hourly

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_UNAVAILABLE = ("unavailable", "unknown", "", None)


def read_legacy_hourly_schedule(hass: HomeAssistant, source_zone_name: str) -> list[Transition]:
    """Read 48 input_numbers and collapse to a transition list.

    Raises ValueError on any missing/unavailable/non-numeric helper. The
    caller is expected to surface that to the user via the service result.
    """
    values: dict[int, tuple[float, float]] = {}
    for hour in range(24):
        low = _read_number(hass, f"input_number.{source_zone_name}_hour_{hour:02d}_low")
        high = _read_number(hass, f"input_number.{source_zone_name}_hour_{hour:02d}_high")
        values[hour] = (low, high)
    return import_legacy_hourly(values)


def _read_number(hass: HomeAssistant, entity_id: str) -> float:
    state = hass.states.get(entity_id)
    if state is None:
        raise ValueError(f"Legacy entity not found: {entity_id}")
    if state.state in _UNAVAILABLE:
        raise ValueError(f"Legacy entity {entity_id} is {state.state!r}; cannot import")
    try:
        return float(state.state)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"Legacy entity {entity_id} state is not numeric: {state.state!r}"
        ) from err
