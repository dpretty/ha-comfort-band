"""Pure peak-period shaping for Comfort Band (v0.24.0).

A peak period is a stretch of time in which running the HVAC costs far more
than usual -- a demand-charge window, a time-of-use peak, a price spike, a
demand-response event. The zone learns of one from an entity whose `on` state
means a peak is in progress, and, while that entity is off, from its
`next_event` attribute, which says when the next one starts (a schedule helper
publishes it; a template binary sensor can too).

Two things change, both on top of whatever band the schedule or override
resolved:

- **During a peak** the band relaxes: the floor drops by the heating offset
  and the ceiling rises by the cooling offset (`relax`), and the coordinator
  runs plain hysteresis on it, so a cycle starts only once the room is past a
  relaxed edge by the deadband, and the setpoint it sends is that edge.
- **Before a peak** whose start is known, the band steps toward the side the
  room will drift away from during the peak (`precondition_band`), for the
  lead time before the start. The floor rises by the heating offset, or the
  ceiling falls by the cooling offset, never closer than
  PEAK_PRECONDITION_GAP_C to the far edge: the room banks no more than the
  peak will borrow, it never leaves its normal band, and the far edge -- the
  one MPC heats or cools towards -- never moves.

An active override suspends both: it is someone asking for a band now.

Like `schedule.py`, nothing here touches Home Assistant: the coordinator
reads the entities and the store and hands the values in.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    PEAK_PRECONDITION_GAP_C,
    PEAK_RECENT_ACTIVE_HOURS,
    PEAK_STATE_ACTIVE,
    PEAK_STATE_NO_SIGNAL,
    PEAK_STATE_OFF,
    PEAK_STATE_OVERRIDE,
    PEAK_STATE_PRECONDITIONING,
    PEAK_STATE_WAITING,
    SLOPE_EPSILON_PER_HOUR,
)

Band = tuple[float, float]


@dataclass(frozen=True)
class PeakSignal:
    """What the peak entity says this refresh.

    `available` is False when the entity is missing or its state is
    unavailable or unknown; `active` is then False too, so a dead signal fails
    toward the normal band. `next_event` is the entity's `next_event`
    attribute, parsed (`parse_next_event`): while the entity is off, when the
    next peak starts; while it is on, its next boundary, which for a schedule
    helper is normally the end of the current peak. None when absent,
    unparseable, or not in the future.
    """

    available: bool
    active: bool
    next_event: datetime | None = None


@dataclass(frozen=True)
class PeakShaping:
    """The peak policy for one refresh: the state to report and the band to use.

    `low` / `high` are the band in force now, after shaping. `relaxing` is
    True exactly when a peak is relaxing the band now -- the coordinator then
    runs plain hysteresis. `precondition` is the direction (`ACTION_HEAT` /
    `ACTION_COOL`) the band is shaped toward from `lead_start` on, whether or
    not that moment has come yet: MPC's lookahead shapes the steps from then
    on, and the coordinator wakes the zone at it. Both are None when no
    pre-conditioning is planned. `next_start` is the announced start of the
    next peak while none is on; `ends` is the signal's next boundary while one
    is.
    """

    state: str
    low: float
    high: float
    relaxing: bool = False
    precondition: str | None = None
    lead_start: datetime | None = None
    next_start: datetime | None = None
    ends: datetime | None = None

    @property
    def preconditioning_now(self) -> bool:
        return self.state == PEAK_STATE_PRECONDITIONING


def parse_next_event(raw: object, *, now: datetime, local_tz: tzinfo) -> datetime | None:
    """Parse a `next_event` attribute into an aware datetime in the future.

    Accepts a datetime (what a schedule helper publishes) or an ISO 8601
    string (what a template sensor, or a state restored from JSON, carries).
    A naive value is taken as local time; the result is in UTC. Anything else
    -- including a value with no UTC equivalent -- and any value at or before
    ``now`` is None: an announced start that has already passed says nothing
    about the next peak, and holding the pre-conditioned band on the strength
    of one would pin it there for as long as the attribute stays stale. The
    comparison is between instants, so it is unaffected by the offsets the
    two values carry.
    """
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    else:
        return None
    # In UTC from here on: arithmetic on an aware datetime is wall-clock
    # arithmetic in its own zone, so a lead time subtracted across a DST
    # change from a local value would land an hour out. A value at either end
    # of the datetime range -- a template's "never" sentinel -- has no UTC
    # equivalent, and neither has one whose tzinfo gives an offset out of
    # range; the error must not reach the refresh.
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=local_tz)
        value = value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    if value <= now:
        return None
    return value


def relax(
    low: float,
    high: float,
    heat_offset: float,
    cool_offset: float,
    *,
    min_temp: float | None = None,
    max_temp: float | None = None,
) -> Band:
    """The band during a peak: the floor down by `heat_offset`, the ceiling up
    by `cool_offset`.

    Bounded by the climate entity's `min_temp` / `max_temp` when it publishes
    them. During a peak the zone's heat setpoint is the relaxed floor (and its
    cool setpoint the relaxed ceiling), and Home Assistant refuses a setpoint
    outside the entity's range -- the unit would then go on heating to
    whatever setpoint it had, which before the peak was usually the top of the
    band. Never narrower than the band it relaxes, whatever the bounds say.
    """
    relaxed_low = low - heat_offset
    if min_temp is not None:
        relaxed_low = max(relaxed_low, min_temp)
    relaxed_high = high + cool_offset
    if max_temp is not None:
        relaxed_high = min(relaxed_high, max_temp)
    return min(low, relaxed_low), max(high, relaxed_high)


def precondition_band(
    low: float,
    high: float,
    direction: str | None,
    *,
    heat_offset: float,
    cool_offset: float,
) -> Band:
    """The band before a peak: the near edge moved toward the far one.

    Pre-heating raises the floor by `heat_offset`; pre-cooling lowers the
    ceiling by `cool_offset`. Either stops PEAK_PRECONDITION_GAP_C short of
    the far edge, and a band already narrower than that is left alone. The far
    edge never moves. `direction` None returns the band unchanged.
    """
    if direction == ACTION_HEAT:
        return max(low, min(low + heat_offset, high - PEAK_PRECONDITION_GAP_C)), high
    if direction == ACTION_COOL:
        return low, min(high, max(high - cool_offset, low + PEAK_PRECONDITION_GAP_C))
    return low, high


def precondition_direction(
    last_action: str | None,
    previous_action: str | None,
    last_active_at: datetime | None,
    now: datetime,
    idle_slope: float | None,
) -> str | None:
    """Which way to pre-condition, or None for not at all.

    The way the zone last ran: `last_action` while a heat or cool cycle is
    running, otherwise `previous_action`, the cycle the current idle stretch
    followed. Only if the zone was heating or cooling within
    PEAK_RECENT_ACTIVE_HOURS (`last_active_at`), so a zone that has needed
    neither for a day is not started just to pre-condition.

    Vetoed when the room is clearly drifting the other way right now -- the
    idle slope (°C/min) beyond the predictor's flatness threshold, rising for
    a pre-heat or falling for a pre-cool. A room warming on its own on a
    sunny afternoon is being pre-heated already, and raising its floor ahead
    of a hot afternoon peak because the zone heated that morning would be the
    wrong way round. An unknown or flat slope vetoes nothing.
    """
    if last_action in (ACTION_HEAT, ACTION_COOL):
        recent = last_action
    elif previous_action in (ACTION_HEAT, ACTION_COOL):
        recent = previous_action
    else:
        return None
    # A naive stamp is only reachable through a hand-edited store, and
    # subtracting it from an aware `now` would raise and fail the refresh.
    if last_active_at is None or last_active_at.tzinfo is None:
        return None
    if now - last_active_at > timedelta(hours=PEAK_RECENT_ACTIVE_HOURS):
        return None
    if idle_slope is not None:
        epsilon = SLOPE_EPSILON_PER_HOUR / 60.0
        if recent == ACTION_HEAT and idle_slope > epsilon:
            return None
        if recent == ACTION_COOL and idle_slope < -epsilon:
            return None
    return recent


def resolve(
    *,
    configured: bool,
    enabled: bool,
    signal: PeakSignal | None,
    override_active: bool,
    low: float,
    high: float,
    heat_offset: float,
    cool_offset: float,
    lead_minutes: int,
    direction: str | None,
    now: datetime,
    min_temp: float | None = None,
    max_temp: float | None = None,
) -> PeakShaping:
    """Decide the peak state and the band for this refresh.

    `low` / `high` are the band before shaping (override or schedule).
    Precedence: switched off or no entity -> `off`; the entity missing or
    unavailable -> `no_signal`; a peak on -> `active`, or `override` while an
    override is in force; a known start within the lead time -> `preconditioning`
    when there is a direction, `override` under an override; otherwise
    `waiting`. Only `active` and `preconditioning` change the band.
    """
    if not configured or not enabled:
        return PeakShaping(state=PEAK_STATE_OFF, low=low, high=high)
    if signal is None or not signal.available:
        return PeakShaping(state=PEAK_STATE_NO_SIGNAL, low=low, high=high)
    if signal.active:
        if override_active:
            return PeakShaping(
                state=PEAK_STATE_OVERRIDE, low=low, high=high, ends=signal.next_event
            )
        relaxed_low, relaxed_high = relax(
            low, high, heat_offset, cool_offset, min_temp=min_temp, max_temp=max_temp
        )
        return PeakShaping(
            state=PEAK_STATE_ACTIVE,
            low=relaxed_low,
            high=relaxed_high,
            relaxing=True,
            ends=signal.next_event,
        )
    next_start = signal.next_event
    if next_start is None or lead_minutes <= 0:
        return PeakShaping(state=PEAK_STATE_WAITING, low=low, high=high, next_start=next_start)
    lead_start = next_start - timedelta(minutes=lead_minutes)
    in_window = lead_start <= now < next_start
    if override_active:
        state = PEAK_STATE_OVERRIDE if in_window else PEAK_STATE_WAITING
        return PeakShaping(state=state, low=low, high=high, next_start=next_start)
    if direction is None:
        return PeakShaping(state=PEAK_STATE_WAITING, low=low, high=high, next_start=next_start)
    if not in_window:
        return PeakShaping(
            state=PEAK_STATE_WAITING,
            low=low,
            high=high,
            precondition=direction,
            lead_start=lead_start,
            next_start=next_start,
        )
    shaped_low, shaped_high = precondition_band(
        low, high, direction, heat_offset=heat_offset, cool_offset=cool_offset
    )
    return PeakShaping(
        state=PEAK_STATE_PRECONDITIONING,
        low=shaped_low,
        high=shaped_high,
        precondition=direction,
        lead_start=lead_start,
        next_start=next_start,
    )


def shape_steps(
    bands: Sequence[Band],
    *,
    now: datetime,
    step_minutes: float,
    shape: Callable[[float, float], Band],
    from_time: datetime | None = None,
) -> list[Band]:
    """Apply `shape` to every per-step band from `from_time` on (all of them
    when it is None).

    Step `i` is taken to fall at ``now + i * step_minutes``, as in
    `schedule.upcoming_bands`, which builds the list MPC plans with. Steps
    past the announced start of a peak keep the pre-conditioned band, so MPC
    plans to keep the room up through the start rather than letting it go in
    the last minutes before it.
    """
    out: list[Band] = []
    for i, (low, high) in enumerate(bands):
        at = now + timedelta(minutes=i * step_minutes)
        out.append(shape(low, high) if from_time is None or at >= from_time else (low, high))
    return out
