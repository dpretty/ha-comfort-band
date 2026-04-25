"""Pure schedule resolver and legacy importer for Comfort Band.

A schedule is a list of `Transition`s sorted by `at`. The band active at any
moment is the most recent transition whose `at` <= now_local — wrapping past
midnight, so a single transition at 22:00 covers the whole day.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class Transition:
    """A single point on the daily schedule."""

    at: time
    low: float
    high: float


def normalize_schedule(transitions: Iterable[Transition]) -> list[Transition]:
    """Return transitions sorted by `at`, validated for uniqueness and low<high."""
    ordered = sorted(transitions, key=lambda t: t.at)
    seen: set[time] = set()
    for t in ordered:
        if t.at in seen:
            raise ValueError(f"Duplicate transition at {t.at.isoformat()}")
        seen.add(t.at)
        if t.low >= t.high:
            raise ValueError(
                f"Transition at {t.at.isoformat()}: low ({t.low}) must be < high ({t.high})"
            )
    return ordered


def resolve(transitions: Sequence[Transition], now_local: time) -> tuple[float, float]:
    """Return (low, high) for the band active at `now_local`.

    `transitions` must be normalized (sorted, validated). Empty list raises.
    If no transition's `at` is <= `now_local`, wraps to the last transition
    of the day (which started "yesterday" in calendar terms).
    """
    if not transitions:
        raise ValueError("Cannot resolve an empty schedule")
    times = [t.at for t in transitions]
    idx = bisect_right(times, now_local)
    chosen = transitions[idx - 1] if idx else transitions[-1]
    return chosen.low, chosen.high


def schedule_to_dict(transitions: Sequence[Transition]) -> list[dict[str, object]]:
    """Serialize a schedule to a list of dicts suitable for JSON storage."""
    return [{"at": t.at.strftime("%H:%M"), "low": t.low, "high": t.high} for t in transitions]


def schedule_from_dict(data: Iterable[Mapping[str, object]]) -> list[Transition]:
    """Deserialize a schedule from stored dicts. Does not normalize — caller may."""
    out: list[Transition] = []
    for d in data:
        at_raw = d["at"]
        if not isinstance(at_raw, str):
            raise TypeError(f"Transition `at` must be a string, got {type(at_raw).__name__}")
        out.append(
            Transition(
                at=time.fromisoformat(at_raw),
                low=float(d["low"]),  # type: ignore[arg-type]
                high=float(d["high"]),  # type: ignore[arg-type]
            )
        )
    return out


def import_legacy_hourly(values: Mapping[int, tuple[float, float]]) -> list[Transition]:
    """Convert legacy hourly slots (hour 0..23 -> (low, high)) to a transition list.

    Adjacent identical hours collapse into a single transition (so a flat
    24-hour band yields one transition at 00:00). Hours must cover all of
    0..23 — partial coverage raises ValueError.
    """
    missing = set(range(24)) - set(values)
    if missing:
        raise ValueError(f"Missing hours in legacy schedule: {sorted(missing)}")
    transitions: list[Transition] = []
    last: tuple[float, float] | None = None
    for h in range(24):
        low, high = values[h]
        if low >= high:
            raise ValueError(f"Hour {h:02d}: low ({low}) must be < high ({high})")
        if (low, high) != last:
            transitions.append(Transition(at=time(hour=h), low=low, high=high))
            last = (low, high)
    return transitions
