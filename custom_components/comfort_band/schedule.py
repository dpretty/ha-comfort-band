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


def resolve(
    transitions: Sequence[Transition],
    now_local: time,
    ramp_minutes: float = 0,
) -> tuple[float, float]:
    """Return (low, high) for the band active at `now_local`.

    `transitions` must be normalized (sorted, validated). Empty list raises.
    If no transition's `at` is <= `now_local`, wraps to the last transition
    of the day (which started "yesterday" in calendar terms).

    v0.10.0: optional ``ramp_minutes`` smooths transitions by linearly
    interpolating band edges within ±ramp_minutes/2 of each transition's
    time. With ``ramp_minutes=0`` (default) behaviour is the v0.9.x
    instant step. Used in pair with :func:`upcoming_bands` so MPC's
    schedule lookahead sees the smoothed curve too.

    A 4 °C overnight setback rising to morning at 06:00 with
    ``ramp_minutes=30`` becomes a 15-min shoulder either side: at 05:45
    the band starts to lift; at 06:00 it's halfway; at 06:15 it's fully
    at the morning value. Eases HVAC into the new setpoint instead of
    chasing an instant deficit.
    """
    if not transitions:
        raise ValueError("Cannot resolve an empty schedule")
    if ramp_minutes > 0 and len(transitions) >= 2:
        smoothed = _ramp_interpolate(transitions, now_local, ramp_minutes)
        if smoothed is not None:
            return smoothed
    times = [t.at for t in transitions]
    idx = bisect_right(times, now_local)
    chosen = transitions[idx - 1] if idx else transitions[-1]
    return chosen.low, chosen.high


def _ramp_interpolate(
    transitions: Sequence[Transition],
    now_local: time,
    ramp_minutes: float,
) -> tuple[float, float] | None:
    """If ``now_local`` falls within the ramp window of some transition,
    return the linearly-interpolated (low, high). Otherwise return None
    so the caller falls back to the standard step resolution.

    For each transition T with effective time ``t_T``, the nominal ramp
    window is ``[t_T - ramp/2, t_T + ramp/2]``. At ``t_T - ramp/2`` the
    band is the PREVIOUS transition's band; at ``t_T`` it's the midpoint;
    at ``t_T + ramp/2`` it's fully T's band.

    **Per-transition clamp for close neighbours**: the effective half-ramp
    used at transition T is

        ``half_T = min(ramp/2, gap_to_prev/2, gap_to_next/2)``

    so that adjacent ramp windows touch at the midpoint between two
    transitions but never overlap. This keeps the interpolated output
    monotonic even when the user configures a schedule whose neighbours
    sit closer than ``ramp_minutes`` apart (e.g. a short morning prep
    band followed by a main band 10 min later, ramp=30): each ramp
    shortens to fit the available gap, and the function returns the
    band defined by whichever transition the moment is nearer to.

    Midnight wrap: we test each transition at three offsets (yesterday,
    today, tomorrow) so a ramp window that straddles 00:00 still
    matches.
    """
    n = len(transitions)
    half_ramp_nominal = ramp_minutes / 2.0
    now_min = now_local.hour * 60 + now_local.minute + now_local.second / 60.0
    day = 24 * 60

    # Pre-compute each transition's local half-ramp, clamped against
    # neighbouring transitions' gaps so adjacent ramps never overlap.
    # Gap to a neighbour at the *same* time would be 0 (impossible after
    # ``normalize_schedule`` rejects duplicates); for a single-transition
    # schedule we never call this helper (gated in ``resolve``).
    half_ramps: list[float] = []
    for i, t in enumerate(transitions):
        t_min = t.at.hour * 60 + t.at.minute + t.at.second / 60.0
        prev_t = transitions[(i - 1) % n]
        next_t = transitions[(i + 1) % n]
        prev_min = prev_t.at.hour * 60 + prev_t.at.minute + prev_t.at.second / 60.0
        next_min = next_t.at.hour * 60 + next_t.at.minute + next_t.at.second / 60.0
        gap_prev = (t_min - prev_min) % day or day
        gap_next = (next_min - t_min) % day or day
        half_ramps.append(min(half_ramp_nominal, gap_prev / 2.0, gap_next / 2.0))

    for i, t in enumerate(transitions):
        t_min_base = t.at.hour * 60 + t.at.minute + t.at.second / 60.0
        half_ramp = half_ramps[i]
        # half_ramp == 0 only if a duplicate slipped past normalization;
        # treat as "no ramp here" (the step resolution at exact equality
        # picks this transition correctly).
        if half_ramp <= 0:
            continue
        # Check yesterday's, today's, and tomorrow's instance of this
        # transition so a ramp window crossing midnight matches.
        for offset in (-day, 0, day):
            t_min = t_min_base + offset
            dist = t_min - now_min
            if abs(dist) <= half_ramp:
                # In the ramp window. The "previous" band is the one
                # immediately preceding transition i in schedule order;
                # for i == 0 it wraps to the last transition.
                prev_idx = (i - 1) % n
                prev = transitions[prev_idx]
                # Progress: 0 at dist=+half_ramp (start of ramp, fully
                # at prev band), 0.5 at dist=0 (transition time, midpoint),
                # 1 at dist=-half_ramp (end of ramp, fully at this band).
                progress = (half_ramp - dist) / (2.0 * half_ramp)
                progress = max(0.0, min(1.0, progress))
                low = prev.low + (t.low - prev.low) * progress
                high = prev.high + (t.high - prev.high) * progress
                return low, high
    return None


def upcoming_bands(
    transitions: Sequence[Transition],
    start_local: time,
    horizon_minutes: int,
    step_minutes: float,
    ramp_minutes: float = 0,
) -> list[tuple[float, float]]:
    """Return per-step ``(low, high)`` for ``horizon_minutes`` ahead.

    Output length is ``round(horizon_minutes / step_minutes)``. Each entry
    is the band active at ``start_local + i * step_minutes``, wrapping
    past midnight via the same logic as ``resolve`` (the wall clock loops
    back to 00:00 after 24:00, so a schedule with transitions only in the
    morning still defines a band for evening steps that overflow).

    Used by ``mpc.plan`` to feed the cost-function band check per step,
    so the MPC can anticipate upcoming schedule transitions (e.g.
    pre-heating before the morning band rises) rather than seeing the
    current band frozen across the whole horizon — a v0.8.1 limitation
    the user observed when the morning room temp ramped up only AFTER
    the band rose, not before.

    Falls back to repeated ``resolve`` calls (one per step) rather than a
    pointer walk through ``transitions``: typical MPC horizon is 60 steps
    x O(log transitions=24) ~ 270 comparisons per refresh — negligible
    on any HA host. The simpler implementation reads more clearly and
    reuses the same midnight-wrap behaviour as the single-time resolver.
    """
    if not transitions:
        raise ValueError("Cannot resolve an empty schedule")
    if step_minutes <= 0:
        raise ValueError(f"step_minutes must be positive, got {step_minutes}")
    steps = round(horizon_minutes / step_minutes)
    start_min = start_local.hour * 60 + start_local.minute
    out: list[tuple[float, float]] = []
    for i in range(steps):
        offset = (start_min + i * step_minutes) % (24 * 60)
        # `time` only carries integer hour/minute, so we truncate. For
        # non-negative `offset` (always, after the mod above) the form
        # below is equivalent to `int(offset) // 60` / `int(offset) % 60`
        # — Python's `int()` on a positive float floors. The `//` and
        # `%` first preserves the intent at the source level (mod within
        # 60-minute hour first, then convert to int), which reads more
        # cleanly if a future caller chooses fractional `step_minutes`
        # and someone wonders whether the truncation order matters.
        h = int(offset // 60)
        m = int(offset % 60)
        out.append(resolve(transitions, time(hour=h, minute=m), ramp_minutes=ramp_minutes))
    return out


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
