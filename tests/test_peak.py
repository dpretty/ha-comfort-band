"""Pure tests for `peak.py` (v0.24.0 peak saving)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from custom_components.comfort_band import peak
from custom_components.comfort_band.const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    PEAK_STATE_ACTIVE,
    PEAK_STATE_NO_SIGNAL,
    PEAK_STATE_OFF,
    PEAK_STATE_OVERRIDE,
    PEAK_STATE_PRECONDITIONING,
    PEAK_STATE_WAITING,
)

NOW = datetime(2026, 6, 15, 7, 0, tzinfo=UTC)
LOCAL = ZoneInfo("Europe/Berlin")
# 0.05 °C/h, the predictor's flatness threshold, in °C/min.
EPSILON = 0.05 / 60.0


# ----- relax -----


def test_relax_moves_each_edge_by_its_own_offset() -> None:
    assert peak.relax(20.0, 23.0, 1.5, 2.0) == (18.5, 25.0)


def test_relax_with_zero_offsets_is_the_band() -> None:
    assert peak.relax(20.0, 23.0, 0.0, 0.0) == (20.0, 23.0)


def test_relax_stops_at_the_units_range() -> None:
    # A unit that takes 16-30: the floor stops at 16 rather than asking for 15.
    assert peak.relax(17.0, 29.0, 2.0, 2.0, min_temp=16.0, max_temp=30.0) == (16.0, 30.0)


def test_relax_never_narrows_the_band_whatever_the_units_range_says() -> None:
    # A band already outside the unit's range stays as it is.
    assert peak.relax(15.0, 31.0, 1.0, 1.0, min_temp=16.0, max_temp=30.0) == (15.0, 31.0)


# ----- precondition_band -----


def test_preheat_raises_the_floor_by_the_heat_offset() -> None:
    assert peak.precondition_band(20.0, 23.0, ACTION_HEAT, heat_offset=1.0, cool_offset=2.0) == (
        21.0,
        23.0,
    )


def test_precool_lowers_the_ceiling_by_the_cool_offset() -> None:
    assert peak.precondition_band(20.0, 23.0, ACTION_COOL, heat_offset=2.0, cool_offset=1.0) == (
        20.0,
        22.0,
    )


def test_preconditioning_stops_half_a_degree_short_of_the_far_edge() -> None:
    assert peak.precondition_band(20.0, 23.0, ACTION_HEAT, heat_offset=5.0, cool_offset=0) == (
        22.5,
        23.0,
    )
    assert peak.precondition_band(20.0, 23.0, ACTION_COOL, heat_offset=0, cool_offset=5.0) == (
        20.0,
        20.5,
    )


def test_a_wide_band_pre_conditions_only_by_the_offset() -> None:
    # An away band of 16-26: the floor rises 1 degree, not to 25.5.
    assert peak.precondition_band(16.0, 26.0, ACTION_HEAT, heat_offset=1.0, cool_offset=1.0) == (
        17.0,
        26.0,
    )


def test_a_band_narrower_than_the_gap_is_left_alone() -> None:
    assert peak.precondition_band(21.0, 21.4, ACTION_HEAT, heat_offset=1.0, cool_offset=1.0) == (
        21.0,
        21.4,
    )
    assert peak.precondition_band(21.0, 21.4, ACTION_COOL, heat_offset=1.0, cool_offset=1.0) == (
        21.0,
        21.4,
    )


@pytest.mark.parametrize("direction", [None, ACTION_IDLE])
def test_no_direction_leaves_the_band_alone(direction: str | None) -> None:
    assert peak.precondition_band(20.0, 23.0, direction, heat_offset=1.0, cool_offset=1.0) == (
        20.0,
        23.0,
    )


# ----- precondition_direction -----


def test_direction_follows_a_running_cycle() -> None:
    assert peak.precondition_direction(ACTION_HEAT, ACTION_COOL, NOW, NOW, None) == ACTION_HEAT
    assert peak.precondition_direction(ACTION_COOL, ACTION_HEAT, NOW, NOW, None) == ACTION_COOL


def test_direction_while_idle_follows_the_cycle_before() -> None:
    recent = NOW - timedelta(hours=2)
    assert peak.precondition_direction(ACTION_IDLE, ACTION_HEAT, recent, NOW, None) == ACTION_HEAT
    assert peak.precondition_direction(ACTION_IDLE, ACTION_COOL, recent, NOW, None) == ACTION_COOL


@pytest.mark.parametrize(
    ("last", "previous"),
    [(None, None), (ACTION_IDLE, None), (ACTION_IDLE, ACTION_IDLE)],
)
def test_no_direction_without_a_cycle(last: str | None, previous: str | None) -> None:
    assert peak.precondition_direction(last, previous, NOW, NOW, None) is None


def test_no_direction_once_the_last_cycle_is_a_day_old() -> None:
    day = timedelta(hours=24)
    assert (
        peak.precondition_direction(ACTION_IDLE, ACTION_HEAT, NOW - day, NOW, None) == ACTION_HEAT
    )
    assert (
        peak.precondition_direction(
            ACTION_IDLE, ACTION_HEAT, NOW - day - timedelta(seconds=1), NOW, None
        )
        is None
    )


@pytest.mark.parametrize("stamp", [None, datetime(2026, 6, 15, 6, 0)])
def test_no_direction_without_a_usable_stamp(stamp: datetime | None) -> None:
    # Unknown (an upgraded zone), or naive (a hand-edited store).
    assert peak.precondition_direction(ACTION_IDLE, ACTION_HEAT, stamp, NOW, None) is None


def test_an_opposing_drift_vetoes_the_direction() -> None:
    warming = 2 * EPSILON
    assert peak.precondition_direction(ACTION_IDLE, ACTION_HEAT, NOW, NOW, warming) is None
    assert peak.precondition_direction(ACTION_IDLE, ACTION_COOL, NOW, NOW, -warming) is None


def test_a_drift_the_same_way_or_flat_vetoes_nothing() -> None:
    assert (
        peak.precondition_direction(ACTION_IDLE, ACTION_HEAT, NOW, NOW, -2 * EPSILON) == ACTION_HEAT
    )
    assert peak.precondition_direction(ACTION_IDLE, ACTION_HEAT, NOW, NOW, EPSILON) == ACTION_HEAT
    assert peak.precondition_direction(ACTION_IDLE, ACTION_COOL, NOW, NOW, -EPSILON) == ACTION_COOL
    assert peak.precondition_direction(ACTION_IDLE, ACTION_COOL, NOW, NOW, 0.0) == ACTION_COOL


# ----- parse_next_event -----


def test_parse_takes_an_aware_datetime_and_returns_utc() -> None:
    raw = datetime(2026, 6, 15, 12, 0, tzinfo=LOCAL)
    parsed = peak.parse_next_event(raw, now=NOW, local_tz=LOCAL)
    assert parsed == raw
    assert parsed is not None and parsed.utcoffset() == timedelta(0)


def test_parse_takes_an_iso_string_with_an_offset() -> None:
    parsed = peak.parse_next_event("2026-06-15T12:00:00+03:00", now=NOW, local_tz=LOCAL)
    assert parsed == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


def test_parse_reads_a_naive_value_as_local_time() -> None:
    parsed = peak.parse_next_event("2026-06-15T12:00:00", now=NOW, local_tz=LOCAL)
    assert parsed == datetime(2026, 6, 15, 12, 0, tzinfo=LOCAL)
    naive = peak.parse_next_event(datetime(2026, 6, 15, 12, 0), now=NOW, local_tz=LOCAL)
    assert naive == datetime(2026, 6, 15, 12, 0, tzinfo=LOCAL)


@pytest.mark.parametrize("raw", [None, "", "soon", 1781020800, ["2026-06-15T12:00:00"]])
def test_parse_rejects_what_is_not_a_time(raw: object) -> None:
    assert peak.parse_next_event(raw, now=NOW, local_tz=LOCAL) is None


@pytest.mark.parametrize(
    ("raw", "local_tz"),
    [
        # A "never" sentinel at either end of the range has no UTC equivalent:
        # converting it raises, and that must not reach the refresh.
        ("0001-01-01T00:00:00", timezone(timedelta(hours=3))),
        ("9999-12-31T23:59:59", timezone(timedelta(hours=-3))),
        ("9999-12-31T23:59:59-05:00", UTC),
        (datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))), UTC),
    ],
)
def test_parse_rejects_a_value_with_no_utc_equivalent(raw: object, local_tz: timezone) -> None:
    assert peak.parse_next_event(raw, now=NOW, local_tz=local_tz) is None


def test_parse_rejects_a_start_that_has_passed() -> None:
    assert peak.parse_next_event(NOW, now=NOW, local_tz=LOCAL) is None
    assert peak.parse_next_event(NOW - timedelta(minutes=1), now=NOW, local_tz=LOCAL) is None
    later = NOW + timedelta(seconds=1)
    assert peak.parse_next_event(later, now=NOW, local_tz=LOCAL) == later


def test_a_lead_time_across_a_clock_change_is_measured_in_real_time() -> None:
    # Clocks go forward at 02:00 local on 2026-03-29 in this zone. A peak at
    # 03:30 local is 01:30 UTC; an hour's lead starts at 00:30 UTC, which is
    # 01:30 local -- wall-clock arithmetic in the local zone would say 02:30.
    start = peak.parse_next_event(
        datetime(2026, 3, 29, 3, 30, tzinfo=LOCAL),
        now=datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
        local_tz=LOCAL,
    )
    assert start is not None
    assert start - timedelta(hours=1) == datetime(2026, 3, 29, 0, 30, tzinfo=UTC)


# ----- resolve -----


def _resolve(**overrides: object) -> peak.PeakShaping:
    kwargs: dict[str, object] = {
        "configured": True,
        "enabled": True,
        "signal": peak.PeakSignal(available=True, active=False),
        "override_active": False,
        "low": 20.0,
        "high": 23.0,
        "heat_offset": 1.0,
        "cool_offset": 1.5,
        "lead_minutes": 60,
        "direction": ACTION_HEAT,
        "now": NOW,
    }
    kwargs.update(overrides)
    return peak.resolve(**kwargs)  # type: ignore[arg-type]


def test_off_unless_configured_and_enabled() -> None:
    on = peak.PeakSignal(available=True, active=True)
    for shaping in (_resolve(configured=False, signal=on), _resolve(enabled=False, signal=on)):
        assert shaping.state == PEAK_STATE_OFF
        assert (shaping.low, shaping.high) == (20.0, 23.0)
        assert not shaping.relaxing


@pytest.mark.parametrize("signal", [None, peak.PeakSignal(available=False, active=False)])
def test_no_signal_keeps_the_band(signal: peak.PeakSignal | None) -> None:
    shaping = _resolve(signal=signal)
    assert shaping.state == PEAK_STATE_NO_SIGNAL
    assert (shaping.low, shaping.high) == (20.0, 23.0)
    assert not shaping.relaxing


def test_an_active_peak_relaxes_the_band() -> None:
    ends = NOW + timedelta(hours=3)
    shaping = _resolve(signal=peak.PeakSignal(available=True, active=True, next_event=ends))
    assert shaping.state == PEAK_STATE_ACTIVE
    assert (shaping.low, shaping.high) == (19.0, 24.5)
    assert shaping.relaxing
    assert shaping.ends == ends
    assert shaping.next_start is None
    assert shaping.precondition is None


def test_an_active_peak_respects_the_units_range() -> None:
    shaping = _resolve(
        signal=peak.PeakSignal(available=True, active=True), min_temp=19.5, max_temp=24.0
    )
    assert (shaping.low, shaping.high) == (19.5, 24.0)


def test_an_override_stands_peak_saving_aside() -> None:
    ends = NOW + timedelta(hours=2)
    shaping = _resolve(
        signal=peak.PeakSignal(available=True, active=True, next_event=ends), override_active=True
    )
    assert shaping.state == PEAK_STATE_OVERRIDE
    assert (shaping.low, shaping.high) == (20.0, 23.0)
    assert not shaping.relaxing
    assert shaping.ends == ends


def test_waiting_without_an_announced_start() -> None:
    shaping = _resolve()
    assert shaping.state == PEAK_STATE_WAITING
    assert (shaping.low, shaping.high) == (20.0, 23.0)
    assert shaping.lead_start is None


def test_waiting_before_the_lead_time_plans_the_step() -> None:
    start = NOW + timedelta(minutes=90)
    shaping = _resolve(signal=peak.PeakSignal(available=True, active=False, next_event=start))
    assert shaping.state == PEAK_STATE_WAITING
    assert (shaping.low, shaping.high) == (20.0, 23.0)
    assert shaping.next_start == start
    assert shaping.lead_start == start - timedelta(minutes=60)
    assert shaping.precondition == ACTION_HEAT
    assert not shaping.preconditioning_now


@pytest.mark.parametrize("minutes_to_start", [60, 30, 1])
def test_preconditioning_inside_the_lead_time(minutes_to_start: int) -> None:
    start = NOW + timedelta(minutes=minutes_to_start)
    shaping = _resolve(signal=peak.PeakSignal(available=True, active=False, next_event=start))
    assert shaping.state == PEAK_STATE_PRECONDITIONING
    assert (shaping.low, shaping.high) == (21.0, 23.0)
    assert shaping.preconditioning_now
    assert not shaping.relaxing


def test_precooling_inside_the_lead_time() -> None:
    start = NOW + timedelta(minutes=30)
    shaping = _resolve(
        signal=peak.PeakSignal(available=True, active=False, next_event=start),
        direction=ACTION_COOL,
    )
    assert shaping.state == PEAK_STATE_PRECONDITIONING
    assert (shaping.low, shaping.high) == (20.0, 21.5)


def test_one_minute_before_the_lead_time_is_still_waiting() -> None:
    start = NOW + timedelta(minutes=61)
    shaping = _resolve(signal=peak.PeakSignal(available=True, active=False, next_event=start))
    assert shaping.state == PEAK_STATE_WAITING


def test_no_preconditioning_with_no_lead_time_or_no_direction() -> None:
    start = NOW + timedelta(minutes=30)
    signal = peak.PeakSignal(available=True, active=False, next_event=start)
    for shaping in (
        _resolve(signal=signal, lead_minutes=0),
        _resolve(signal=signal, direction=None),
    ):
        assert shaping.state == PEAK_STATE_WAITING
        assert (shaping.low, shaping.high) == (20.0, 23.0)
        assert shaping.lead_start is None
        assert shaping.next_start == start


def test_an_override_inside_the_lead_time_reads_override() -> None:
    signal = peak.PeakSignal(available=True, active=False, next_event=NOW + timedelta(minutes=30))
    shaping = _resolve(signal=signal, override_active=True)
    assert shaping.state == PEAK_STATE_OVERRIDE
    assert (shaping.low, shaping.high) == (20.0, 23.0)
    assert shaping.lead_start is None
    assert shaping.next_start == signal.next_event
    early = peak.PeakSignal(available=True, active=False, next_event=NOW + timedelta(hours=2))
    assert _resolve(signal=early, override_active=True).state == PEAK_STATE_WAITING


# ----- shape_steps -----


def _plus_one(low: float, high: float) -> tuple[float, float]:
    return low + 1.0, high


def test_shape_steps_from_a_time_shapes_that_step_and_every_later_one() -> None:
    bands = [(20.0, 23.0)] * 5
    shaped = peak.shape_steps(
        bands,
        now=NOW,
        step_minutes=1.0,
        shape=_plus_one,
        from_time=NOW + timedelta(minutes=2),
    )
    assert shaped == [(20.0, 23.0), (20.0, 23.0), (21.0, 23.0), (21.0, 23.0), (21.0, 23.0)]


def test_shape_steps_without_a_time_shapes_every_step() -> None:
    bands = [(20.0, 23.0), (19.0, 22.0)]
    shaped = peak.shape_steps(bands, now=NOW, step_minutes=1.0, shape=_plus_one)
    assert shaped == [(21.0, 23.0), (20.0, 22.0)]


def test_shape_steps_leaves_its_input_alone() -> None:
    bands = [(20.0, 23.0)]
    peak.shape_steps(bands, now=NOW, step_minutes=1.0, shape=_plus_one)
    assert bands == [(20.0, 23.0)]


def test_parse_compares_instants_across_offsets() -> None:
    # 09:59 at +03:00 is 06:59 UTC, a minute before NOW (07:00 UTC), though its
    # wall-clock reading is hours later than NOW's.
    plus_three = timezone(timedelta(hours=3))
    assert (
        peak.parse_next_event(
            datetime(2026, 6, 15, 9, 59, tzinfo=plus_three), now=NOW, local_tz=LOCAL
        )
        is None
    )


class _OutOfRangeOffset(tzinfo):
    """A tzinfo whose offset Python refuses: a day or more."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=25)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "bad"


def test_parse_rejects_a_tzinfo_with_an_offset_out_of_range() -> None:
    raw = datetime(2026, 6, 15, 12, 0, tzinfo=_OutOfRangeOffset())
    assert peak.parse_next_event(raw, now=NOW, local_tz=LOCAL) is None
