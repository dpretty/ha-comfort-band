"""Tests for the pure schedule resolver and legacy importer."""

from __future__ import annotations

from datetime import time

import pytest

from custom_components.comfort_band.schedule import (
    Transition,
    import_legacy_hourly,
    normalize_schedule,
    resolve,
    schedule_from_dict,
    schedule_to_dict,
)


def _t(h: int, m: int, low: float, high: float) -> Transition:
    return Transition(at=time(h, m), low=low, high=high)


# ----- normalize_schedule -----


def test_normalize_sorts_by_time() -> None:
    out = normalize_schedule([_t(22, 0, 19, 22), _t(6, 0, 20, 23), _t(9, 30, 21, 24)])
    assert [t.at for t in out] == [time(6, 0), time(9, 30), time(22, 0)]


def test_normalize_rejects_duplicate_at() -> None:
    with pytest.raises(ValueError, match="Duplicate transition at 06:00"):
        normalize_schedule([_t(6, 0, 20, 22), _t(6, 0, 19, 23)])


def test_normalize_rejects_low_ge_high() -> None:
    with pytest.raises(ValueError, match=r"low.*must be < high"):
        normalize_schedule([_t(6, 0, 23, 23)])
    with pytest.raises(ValueError, match=r"low.*must be < high"):
        normalize_schedule([_t(6, 0, 24, 22)])


def test_normalize_returns_fresh_list() -> None:
    src = [_t(6, 0, 20, 22)]
    out = normalize_schedule(src)
    assert out is not src


# ----- resolve -----


def test_resolve_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty schedule"):
        resolve([], time(12, 0))


def test_resolve_single_transition_covers_whole_day() -> None:
    schedule = normalize_schedule([_t(0, 0, 20, 22)])
    assert resolve(schedule, time(0, 0)) == (20, 22)
    assert resolve(schedule, time(12, 0)) == (20, 22)
    assert resolve(schedule, time(23, 59)) == (20, 22)


def test_resolve_picks_most_recent_transition() -> None:
    schedule = normalize_schedule([_t(6, 0, 20, 23), _t(9, 30, 21, 24), _t(22, 0, 18, 21)])
    assert resolve(schedule, time(8, 0)) == (20, 23)
    assert resolve(schedule, time(10, 0)) == (21, 24)
    assert resolve(schedule, time(22, 30)) == (18, 21)


def test_resolve_wraps_around_midnight() -> None:
    # No transition at/before 03:00 -> picks the last one of the day.
    schedule = normalize_schedule([_t(6, 0, 20, 23), _t(22, 0, 18, 21)])
    assert resolve(schedule, time(3, 0)) == (18, 21)
    assert resolve(schedule, time(0, 0)) == (18, 21)


def test_resolve_includes_exact_boundary() -> None:
    # Querying exactly at a transition's `at` returns that transition.
    schedule = normalize_schedule([_t(6, 0, 20, 23), _t(9, 30, 21, 24)])
    assert resolve(schedule, time(6, 0)) == (20, 23)
    assert resolve(schedule, time(9, 30)) == (21, 24)


# ----- schedule_to_dict / schedule_from_dict -----


def test_schedule_round_trip() -> None:
    original = normalize_schedule(
        [_t(6, 0, 20.5, 22.5), _t(9, 30, 21.0, 24.0), _t(22, 0, 18.5, 21.5)]
    )
    serialized = schedule_to_dict(original)
    assert serialized == [
        {"at": "06:00", "low": 20.5, "high": 22.5},
        {"at": "09:30", "low": 21.0, "high": 24.0},
        {"at": "22:00", "low": 18.5, "high": 21.5},
    ]
    restored = schedule_from_dict(serialized)
    assert restored == original


def test_schedule_from_dict_rejects_non_string_at() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        schedule_from_dict([{"at": 600, "low": 20, "high": 22}])


# ----- import_legacy_hourly -----


def test_import_legacy_collapses_24_equal_hours() -> None:
    values = dict.fromkeys(range(24), (20.0, 23.0))
    result = import_legacy_hourly(values)
    assert result == [Transition(at=time(0, 0), low=20.0, high=23.0)]


def test_import_legacy_emits_transitions_at_each_change() -> None:
    # Cool nights, warmer days: 00..05 = (18, 22), 06..21 = (20, 24), 22..23 = (18, 22).
    values: dict[int, tuple[float, float]] = {}
    for h in range(0, 6):
        values[h] = (18.0, 22.0)
    for h in range(6, 22):
        values[h] = (20.0, 24.0)
    for h in range(22, 24):
        values[h] = (18.0, 22.0)
    result = import_legacy_hourly(values)
    assert result == [
        Transition(at=time(0, 0), low=18.0, high=22.0),
        Transition(at=time(6, 0), low=20.0, high=24.0),
        Transition(at=time(22, 0), low=18.0, high=22.0),
    ]


def test_import_legacy_rejects_missing_hour() -> None:
    values = {h: (20.0, 23.0) for h in range(24) if h != 13}
    with pytest.raises(ValueError, match=r"Missing hours.*\[13\]"):
        import_legacy_hourly(values)


def test_import_legacy_rejects_low_ge_high() -> None:
    values = dict.fromkeys(range(24), (20.0, 23.0))
    values[5] = (24.0, 22.0)
    with pytest.raises(ValueError, match=r"Hour 05.*low.*must be < high"):
        import_legacy_hourly(values)
