"""Per-zone DataUpdateCoordinator.

Event-driven (`update_interval=None`); refreshes fire from:
  - room-temp state changes (debounced 2 s)
  - active-profile dispatcher signal
  - one-shot timers for override-expiry + next-transition
  - explicit `async_request_refresh()` from numbers/switches/services

Each refresh re-reads the store + sensor, runs the hysteresis decider against
the resolved effective band, and (in a follow-up task) applies the decision
via `climate.set_hvac_mode` + `set_temperature` -- but only if the per-zone
`switch.{zone}_enabled` is on. Default is OFF (shadow mode).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import apparent_temp, hysteresis, mpc, predictor, schedule
from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_IDLE,
    ACTION_UNKNOWN,
    CLIMATE_ECHO_WINDOW_S,
    LOGGER,
    MPC_SIMULATION_STEP_MINUTES,
    PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES,
    SAMPLE_PERSIST_INTERVAL_S,
    SIGNAL_ACTIVE_PROFILE_CHANGED,
)
from .hysteresis import HysteresisDecision, HysteresisInputs
from .predictor import Sample, ThermalSlopes
from .schedule import Transition, normalize_schedule, schedule_from_dict

if TYPE_CHECKING:
    from .storage import ComfortBandStore, StoredProfileSchedule, StoredZone


_DEBOUNCE_SECS = 2.0
_MAX_NEXT_TRANSITION_SECS = 3600.0  # cap re-scheduling at 1 h

# Fallback when the climate entity doesn't expose `target_temp_step`. 0.5 °C
# matches the resolution of most consumer heat pumps (Daikin, Mitsubishi,
# Fujitsu); a finer step here would mean set_temperature commands get silently
# coerced by the climate platform and our control intent diverges from what
# the HVAC actually pursues.
_DEFAULT_TEMP_STEP = 0.5


def _round_to_step(value: float, step: float) -> float:
    """Round `value` to the nearest multiple of `step`. `step <= 0` returns the
    value unchanged (defensive: a corrupt climate entity attribute might
    yield zero or negative — better to issue the precise setpoint than to
    divide-by-zero or invert the rounding).
    """
    if step <= 0:
        return value
    return round(value / step) * step


@dataclass(frozen=True)
class ZoneState:
    """Snapshot returned by `_async_update_data`. Drives every per-zone entity.

    `zone` is the full StoredZone (deep-copied) so entities can read tunables
    (manual_low/high, deadband_*, override_hours, enabled, ...) without
    poking the store directly.

    `room` is always the *raw* room reading. `apparent_temperature` is
    always the Steadman value (which equals `room` when humidity is None).
    `decision_room` is whichever of those was actually fed into hysteresis —
    surfaced for the card so users can see the value driving control.
    """

    zone: StoredZone
    room: float | None
    sensor_available: bool
    humidity: float | None
    apparent_temperature: float | None
    decision_room: float | None
    effective_low: float
    effective_high: float
    sched_low: float
    sched_high: float
    override_active: bool
    override_until: datetime | None
    decision: HysteresisDecision
    # v0.6 predictive control: predicted_decision is always populated (shadow
    # mode), regardless of learning_enabled. thermal_slopes carries the
    # current learned slopes for the thermal_slope sensor's attributes.
    predicted_decision: HysteresisDecision
    # v0.12.0: `thermal_slopes` here are the *effective* slopes — identical to
    # the live estimate except that, when the live idle slope is None but a
    # recent persisted idle slope exists, idle is substituted from storage so
    # MPC stays ready through a heating chase. These drive `mpc.is_ready` /
    # `mpc.plan` and the thermal_slope sensor only; the reactive predictor and
    # hysteresis run on the *live* slopes (the cache must not change reactive
    # control). `idle_slope_source` records which path produced the idle value
    # ("live" | "cached" | "none") and `idle_slope_cached_age_min` is the age
    # (min) of the substituted value (None unless source is "cached"). Both
    # surface on the thermal_slope sensor so users can see when MPC is running
    # on the cached value.
    thermal_slopes: ThermalSlopes
    idle_slope_source: str
    idle_slope_cached_age_min: float | None
    # v0.8 model-predictive controller. `mpc_decision` is always populated
    # (shadow mode), regardless of `mpc_enabled`. When MPC isn't ready
    # (a slope is missing), `mpc.plan` returns `predicted_decision` so the
    # shadow-comparison surface is still meaningful — users can watch
    # `mpc_action` track `predicted_action` until enough data accumulates,
    # then diverge. `mpc_ready` exposes the gate as a binary sensor.
    mpc_decision: HysteresisDecision
    mpc_ready: bool

    @property
    def enabled(self) -> bool:
        return self.zone["enabled"]

    @property
    def last_action(self) -> str | None:
        return self.zone["last_action"]


class ZoneCoordinator(DataUpdateCoordinator[ZoneState]):
    """One per zone. Owns no state of its own beyond timer subscriptions."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: ComfortBandStore,
        zone_name: str,
        climate_entity_id: str,
        temp_entity_id: str,
        humidity_entity_id: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=f"comfort_band[{zone_name}]",
            update_interval=None,
        )
        self._store = store
        self.zone_name = zone_name
        self.climate_entity_id = climate_entity_id
        self.temp_entity_id = temp_entity_id
        self.humidity_entity_id = humidity_entity_id
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_signal: CALLBACK_TYPE | None = None
        self._unsub_debounce: CALLBACK_TYPE | None = None
        self._unsub_override_timer: CALLBACK_TYPE | None = None
        self._unsub_transition_timer: CALLBACK_TYPE | None = None
        # v0.6 predictive control. Buffer is hydrated from store in
        # `async_setup`; the climate-state listener detects manual edits and
        # flushes to keep the slope estimator honest under mixed control.
        # `_last_sample_persist_at` throttles disk writes -- see _append_sample.
        self._samples_cache: list[Sample] = []
        self._last_command_state: dict[str, Any] | None = None
        self._last_command_at: datetime | None = None
        self._unsub_climate: CALLBACK_TYPE | None = None
        self._last_sample_persist_at: datetime | None = None
        # v0.12.0: throttles writes of the persisted idle slope (see
        # `_maybe_persist_idle_slope`). Mirrors `_last_sample_persist_at` --
        # ephemeral, reset on flush so the next fresh idle slope writes
        # immediately.
        self._last_idle_slope_persist_at: datetime | None = None

    # ----- setup / teardown -----

    async def async_setup(self) -> None:
        """Wire event-driven triggers and run the first refresh."""
        # Subscribe to temp + (optionally) humidity changes via the same
        # debounced path — a humidity-only change should re-evaluate when
        # `use_apparent_temperature` is on.
        watch = [self.temp_entity_id]
        if self.humidity_entity_id is not None:
            watch.append(self.humidity_entity_id)
        self._unsub_state = async_track_state_change_event(self.hass, watch, self._on_temp_change)
        self._unsub_signal = async_dispatcher_connect(
            self.hass, SIGNAL_ACTIVE_PROFILE_CHANGED, self._on_profile_change
        )
        # Hydrate the v0.6 sample buffer from store + subscribe to climate
        # state changes (manual-edit detector keeps the slope estimator honest).
        zone = self._store.get_zone(self.zone_name)
        self._samples_cache = predictor.load_samples(zone["samples"])
        self._unsub_climate = async_track_state_change_event(
            self.hass, [self.climate_entity_id], self._on_climate_state_change
        )
        await self.async_config_entry_first_refresh()

    async def async_unload(self) -> None:
        """Cancel every active subscription. Safe to call repeatedly."""
        for unsub in (
            self._unsub_state,
            self._unsub_signal,
            self._unsub_debounce,
            self._unsub_override_timer,
            self._unsub_transition_timer,
            self._unsub_climate,
        ):
            if unsub is not None:
                unsub()
        self._unsub_state = None
        self._unsub_signal = None
        self._unsub_debounce = None
        self._unsub_override_timer = None
        self._unsub_transition_timer = None
        self._unsub_climate = None
        # v0.6 predictive caches -- reset to honour the "safe to call
        # repeatedly" contract. (HA normally constructs a new coordinator
        # on reload, so this is future-proofing more than current need.)
        self._samples_cache = []
        self._last_command_state = None
        self._last_command_at = None
        self._last_sample_persist_at = None
        self._last_idle_slope_persist_at = None

    # ----- mutators (called from entities + services) -----

    def get_zone_data(self) -> StoredZone:
        """Snapshot of the persisted zone (deep copy). Cheap; KB-sized."""
        return self._store.get_zone(self.zone_name)

    async def async_set_param(self, field: str, value: Any) -> None:
        """Update a tunable (deadband_*, override_hours, min_cycle_minutes,
        cross_mode_min_minutes, lookahead_minutes, passive_tolerance,
        mpc_horizon_minutes) without triggering an override.

        Uses `async_request_refresh` (queued + deduped) rather than
        `async_refresh` because Number entities can fire rapid-fire writes
        when the user drags a slider; deduping avoids a thundering-herd
        of coordinator refreshes. The user-flip mutators below
        (`async_set_enabled`, `async_set_learning_enabled`, etc.) use the
        immediate `async_refresh` because a switch is one tap.
        """
        await self._store.async_update_zone(self.zone_name, **{field: value})
        await self.async_request_refresh()

    async def async_set_manual_low(self, value: float) -> None:
        """Set manual_low and start an override (matches legacy from-user trigger)."""
        await self._set_manual_and_override(manual_low=value)

    async def async_set_manual_high(self, value: float) -> None:
        """Set manual_high and start an override."""
        await self._set_manual_and_override(manual_high=value)

    async def async_start_override(
        self,
        *,
        low: float | None = None,
        high: float | None = None,
        hours: float | None = None,
    ) -> None:
        """Bump override_until = now + hours. Optionally update the manual band."""
        zone = self._store.get_zone(self.zone_name)
        use_hours = hours if hours is not None else zone["override_hours"]
        update: dict[str, Any] = {
            "override_until": (dt_util.utcnow() + timedelta(hours=use_hours)).isoformat()
        }
        if low is not None:
            update["manual_low"] = low
        if high is not None:
            update["manual_high"] = high
        await self._store.async_update_zone(self.zone_name, **update)
        await self.async_refresh()

    async def async_cancel_override(self) -> None:
        await self._store.async_update_zone(self.zone_name, override_until=None)
        await self.async_refresh()

    async def async_set_enabled(self, enabled: bool) -> None:
        await self._store.async_update_zone(self.zone_name, enabled=enabled)
        await self.async_refresh()

    async def async_set_learning_enabled(self, learning_enabled: bool) -> None:
        await self._store.async_update_zone(self.zone_name, learning_enabled=learning_enabled)
        await self.async_refresh()

    async def async_set_mpc_enabled(self, mpc_enabled: bool) -> None:
        """Flip the v0.8 MPC opt-in switch. Layered on top of learning_enabled —
        MPC only takes effect when both are ON *and* MPC has the data it
        needs (see `mpc.is_ready`). Mirrors `async_set_learning_enabled` so
        the switch entity wiring stays uniform across the two gates.
        """
        await self._store.async_update_zone(self.zone_name, mpc_enabled=mpc_enabled)
        await self.async_refresh()

    async def async_set_use_apparent_temperature(self, use_apparent_temperature: bool) -> None:
        await self._store.async_update_zone(
            self.zone_name, use_apparent_temperature=use_apparent_temperature
        )
        await self.async_refresh()

    async def async_set_fan_control_enabled(self, fan_control_enabled: bool) -> None:
        """Flip the v0.13.0 deterministic fan-boost opt-in."""
        await self._store.async_update_zone(self.zone_name, fan_control_enabled=fan_control_enabled)
        await self.async_refresh()

    async def async_set_active_fan_mode(self, active_fan_mode: str | None) -> None:
        """Set the fan mode commanded while heating/cooling (None = don't command)."""
        await self._store.async_update_zone(self.zone_name, active_fan_mode=active_fan_mode)
        await self.async_refresh()

    async def async_set_idle_fan_mode(self, idle_fan_mode: str | None) -> None:
        """Set the fan mode commanded while idle (None = don't command)."""
        await self._store.async_update_zone(self.zone_name, idle_fan_mode=idle_fan_mode)
        await self.async_refresh()

    async def _set_manual_and_override(self, **manual_fields: float) -> None:
        zone = self._store.get_zone(self.zone_name)
        until = (dt_util.utcnow() + timedelta(hours=zone["override_hours"])).isoformat()
        await self._store.async_update_zone(self.zone_name, override_until=until, **manual_fields)
        await self.async_refresh()

    # ----- triggers -----

    @callback
    def _on_temp_change(self, _event: Event[EventStateChangedData]) -> None:
        # Many sensors emit several updates per second; debounce so we only
        # refresh once per quiet 2 s window.
        if self._unsub_debounce is not None:
            self._unsub_debounce()
        self._unsub_debounce = async_call_later(self.hass, _DEBOUNCE_SECS, self._on_debounce_fire)

    @callback
    def _on_debounce_fire(self, _now: datetime) -> None:
        self._unsub_debounce = None
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_profile_change(self, _new_active: str) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_timer_fire(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    # ----- main update -----

    async def _async_update_data(self) -> ZoneState:
        zone = self._store.get_zone(self.zone_name)
        active_profile = self._store.active_profile

        room, sensor_available = self._read_room_temp()
        humidity = self._read_humidity()
        # `apparent_temp.compute(T, None) → T`, so when humidity is
        # unavailable the apparent value silently equals the room reading.
        # That's deliberate: it lets `use_apparent_temperature=True` stay
        # safe across humidity-sensor outages.
        apparent_temperature = apparent_temp.compute(room, humidity) if room is not None else None

        # Re-validate override.
        now_utc = dt_util.utcnow()
        override_until = _parse_iso(zone["override_until"])
        override_active = override_until is not None and now_utc < override_until
        if override_until is not None and not override_active:
            await self._store.async_update_zone(self.zone_name, override_until=None)
            zone = self._store.get_zone(self.zone_name)
            override_until = None

        # Resolve scheduled band (falls back to default profile's schedule,
        # then to manual band). `default_profile` tracks the renamed-home
        # name, so this still works after the user renames the original
        # "home" profile.
        default_profile = self._store.default_profile
        schedule_data = zone["schedules"].get(active_profile) or zone["schedules"].get(
            default_profile
        )
        sched_low, sched_high = self._resolve_schedule(
            schedule_data,
            fallback=(zone["manual_low"], zone["manual_high"]),
            ramp_minutes=zone["band_ramp_minutes"],
        )

        if override_active:
            eff_low, eff_high = zone["manual_low"], zone["manual_high"]
        else:
            eff_low, eff_high = sched_low, sched_high

        # Defensive clamp -- should never fire, since UI inputs validate
        # low < high, but a corrupt store or future profile-manager bug
        # would otherwise make hysteresis flap.
        if eff_low >= eff_high:
            LOGGER.warning(
                "%s: effective_low (%s) >= effective_high (%s); clamping",
                self.zone_name,
                eff_low,
                eff_high,
            )
            eff_low = eff_high - 0.5

        # Per-zone choice: feed apparent temperature (humidity-adjusted
        # "feels like") into the decider instead of the raw room reading.
        # Defaults to raw room. Apparent equals room when humidity is None,
        # so this is safe to leave ON even if the humidity sensor flakes.
        use_apparent = zone["use_apparent_temperature"]
        decision_room = apparent_temperature if use_apparent else room
        LOGGER.debug(
            "%s: decision room=%s (use_apparent=%s, room=%s, apparent=%s)",
            self.zone_name,
            decision_room,
            use_apparent,
            room,
            apparent_temperature,
        )

        hyst_inputs = HysteresisInputs(
            room=decision_room,
            low=eff_low,
            high=eff_high,
            deadband_below=zone["deadband_below"],
            deadband_above=zone["deadband_above"],
            current_action=zone["last_action"] or ACTION_UNKNOWN,
        )
        hyst_decision = hysteresis.decide(hyst_inputs)
        # Predictor runs every refresh (shadow mode). Slopes are computed once
        # and fed into both `decide()` (anticipation logic) and the
        # thermal_slope sensor's attributes (via ZoneState).
        thermal_slopes = predictor.estimate_slopes(self._samples_cache, now=now_utc)
        # v0.12.0: the idle (passive heat-loss) slope changes slowly, so we
        # remember the last good one beyond the 90-min sample window. When a
        # heating-dominated room chases a rising morning band, the live window
        # only yields short idle blips (idle is None) and the room would drop
        # out of MPC readiness exactly when pre-heat is needed.
        # `_resolve_idle_slope` substitutes a recent persisted idle slope in
        # that case (and refreshes the persisted value when the live one is
        # fresh), returning the *effective* slopes plus diagnostics.
        #
        # The cache is an MPC-readiness concern ONLY: `effective_slopes` feeds
        # `mpc.is_ready` / `mpc.plan` (and the thermal_slope sensor, so users
        # can see the cached value). The reactive predictor below deliberately
        # gets the *live* `thermal_slopes` — a cached idle must NOT reach the
        # v0.7 passive-drift / anticipatory-startup branches, or it would
        # silently change reactive control on a predictor-only zone (suppress a
        # heat/cool call off a stale slope). Predictor + hysteresis stay
        # byte-for-byte v0.11; only MPC gains the cache.
        (
            effective_slopes,
            idle_slope_source,
            idle_slope_cached_age_min,
        ) = await self._resolve_idle_slope(thermal_slopes, zone, now_utc)
        predicted_decision = predictor.decide(
            thermal_slopes,
            hyst_inputs,
            lookahead_minutes=zone["lookahead_minutes"],
            passive_tolerance=zone["passive_tolerance"],
            hysteresis_decision=hyst_decision,
        )
        # v0.8 MPC also runs every refresh (shadow mode). When ready, MPC plans
        # over `mpc_horizon_minutes` and returns the highest-time-in-band
        # action; when not ready it returns `predicted_decision` so the
        # shadow-comparison sensor still produces a meaningful value.
        #
        # v0.9.0+: `bands_per_step` is the per-minute (low, high) the
        # schedule resolves to over the horizon. When set, `mpc.plan`
        # feeds it to `simulate` so the cost function can anticipate
        # upcoming schedule transitions — closes the "MPC didn't
        # pre-heat before the morning band rise" report. Skipped when
        # an override is active OR when the schedule parse fails
        # (None → MPC falls back to the snapshot path).
        #
        # Override edge case: if the override expires WITHIN the
        # horizon (e.g. 30 min remaining, 60 min horizon), the snapshot
        # path treats the whole horizon as the override band — the
        # post-expiry minutes are mis-scored against the manual band
        # rather than the schedule. Acceptable for now: the
        # override-expiry timer fires at expiry and triggers a fresh
        # refresh that picks up the schedule band, bounding the
        # mis-scoring to at most one refresh cycle. A future
        # improvement could splice scheduled bands into the post-expiry
        # tail of the list.
        mpc_ready = mpc.is_ready(effective_slopes)
        bands_per_step = (
            None
            if override_active
            else self._compute_bands_per_step(
                schedule_data,
                zone["mpc_horizon_minutes"],
                ramp_minutes=zone["band_ramp_minutes"],
            )
        )
        mpc_decision = mpc.plan(
            effective_slopes,
            hyst_inputs,
            horizon_minutes=zone["mpc_horizon_minutes"],
            predictor_decision=predicted_decision,
            bands_per_step=bands_per_step,
        )
        # Three-way gate: each layer is opt-in by its own switch. learning_enabled
        # is the v0.6 predictor gate (preserves v0.7 behaviour). mpc_enabled is
        # the v0.8 MPC gate, layered on top — both must be ON, and MPC must
        # have its required slopes, for MPC's decision to be the active one.
        if zone["learning_enabled"] and zone["mpc_enabled"] and mpc_ready:
            final_decision = mpc_decision
        elif zone["learning_enabled"]:
            final_decision = predicted_decision
        else:
            final_decision = hyst_decision

        # Reschedule next-transition + override-expiry timers. Pass the
        # ramp so the timer wakes us at the ramp's leading edge instead of
        # at the bare transition, which would otherwise forfeit the leading
        # half of the smoothing in quiet rooms (no sensor activity between
        # this refresh and the next transition).
        self._schedule_next_transition(schedule_data, ramp_minutes=zone["band_ramp_minutes"])
        if override_until is not None and override_active:
            self._schedule_override_expiry(override_until - now_utc)

        state = ZoneState(
            zone=zone,
            room=room,
            sensor_available=sensor_available,
            humidity=humidity,
            apparent_temperature=apparent_temperature,
            decision_room=decision_room,
            effective_low=eff_low,
            effective_high=eff_high,
            sched_low=sched_low,
            sched_high=sched_high,
            override_active=override_active,
            override_until=override_until,
            decision=final_decision,
            predicted_decision=predicted_decision,
            thermal_slopes=effective_slopes,
            idle_slope_source=idle_slope_source,
            idle_slope_cached_age_min=idle_slope_cached_age_min,
            mpc_decision=mpc_decision,
            mpc_ready=mpc_ready,
        )

        # Apply in a follow-up task so this refresh returns immediately --
        # entities can render the new state without waiting on climate calls.
        self.hass.async_create_task(
            self._maybe_apply_action(final_decision, zone["enabled"], decision_room=decision_room)
        )

        return state

    # ----- helpers -----

    async def _resolve_idle_slope(
        self,
        slopes: ThermalSlopes,
        zone: StoredZone,
        now_utc: datetime,
    ) -> tuple[ThermalSlopes, str, float | None]:
        """Apply the persisted-idle-slope policy (v0.12.0).

        The idle (passive heat-loss) rate changes slowly, so a recent value
        stays valid well beyond the 90-min sample window. Returns
        ``(effective_slopes, source, cached_age_min)``:

        - **Live idle slope present** -> remember it (throttled write) and
          return the slopes unchanged. ``source="live"``.
        - **Live idle slope absent** but a persisted one exists within
          ``PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES`` -> substitute it via
          ``dataclasses.replace`` (tagging ``method_idle="cached"``) so MPC
          stays ready through a heating chase. ``source="cached"``, age in min.
        - **Live idle slope absent** and no usable persisted one (never
          learned, or expired) -> return unchanged. ``source="none"``. An
          expired value is cleared from storage so it can't resurface.

        Only the idle slope is persisted: recovery slopes change faster and
        are always present during a heating/cooling chase, so they don't have
        the aging-out problem idle does.
        """
        if slopes.idle is not None:
            # Persist only for learning-enabled zones: the cache is a
            # predictive-control feature — it only ever feeds mpc.is_ready /
            # mpc.plan, so a pure-hysteresis zone would never consume it. Skip
            # the storage write there to avoid needless SD-card wear. Gating on
            # learning_enabled (not mpc_enabled) keeps the cache — and thus the
            # shadow `mpc_ready` signal — warm for zones being evaluated for MPC
            # before the user flips mpc_enabled on.
            if zone["learning_enabled"]:
                await self._maybe_persist_idle_slope(slopes.idle, now_utc)
            return slopes, "live", None

        persisted = zone["persisted_idle_slope"]
        persisted_at = _parse_iso(zone["persisted_idle_slope_at"])
        if persisted is None or persisted_at is None:
            return slopes, "none", None
        if persisted_at.tzinfo is None:
            # A naive timestamp is only reachable via a hand-edited / corrupt
            # store. Drop it rather than let the aware/naive subtraction below
            # raise TypeError and fail the entire refresh (all entities would
            # go unavailable). Clearing makes the next refresh quiescent.
            await self._clear_persisted_idle_slope()
            return slopes, "none", None

        age_min = (now_utc - persisted_at).total_seconds() / 60.0
        if age_min > PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES:
            # Stale -- drop it so a long-dead value can't keep MPC "ready"
            # against a thermal model that no longer holds.
            await self._clear_persisted_idle_slope()
            return slopes, "none", None

        effective = replace(slopes, idle=persisted, method_idle="cached")
        # Clamp the reported age at 0 to absorb minor clock skew (a timestamp
        # written by a slightly-ahead clock would otherwise read negative).
        return effective, "cached", round(max(0.0, age_min), 1)

    async def _maybe_persist_idle_slope(self, slope: float, now_utc: datetime) -> None:
        """Persist a fresh live idle slope, throttled to bound flash wear.

        Writes at most once per ``SAMPLE_PERSIST_INTERVAL_S`` (mirroring the
        sample-buffer cadence), refreshing both the value and the timestamp.
        That keeps ``persisted_idle_slope_at`` tracking "this slope is current"
        to within ~5 min, so the cached value's age at the start of a heating
        chase reflects time-since-idle (when the chase began), not
        time-since-first-observed. The first call after setup / a buffer flush
        (``_last_idle_slope_persist_at is None``) writes immediately. The idle
        rate is slow-changing, so a value up to 5 min stale is fine.
        """
        due = (
            self._last_idle_slope_persist_at is None
            or (now_utc - self._last_idle_slope_persist_at).total_seconds()
            >= SAMPLE_PERSIST_INTERVAL_S
        )
        if not due:
            return
        await self._store.async_update_zone(
            self.zone_name,
            persisted_idle_slope=slope,
            persisted_idle_slope_at=now_utc.isoformat(),
        )
        # Advance the throttle only after the write lands (mirrors the
        # sample-persist path) so a failed write doesn't push the next attempt
        # out by a full interval.
        self._last_idle_slope_persist_at = now_utc

    async def _clear_persisted_idle_slope(self) -> None:
        """Null the persisted idle slope (stale-expiry path).

        Buffer-flush sites clear it inline in their own store write to keep
        the flush atomic; this is the standalone expiry path.
        """
        self._last_idle_slope_persist_at = None
        await self._store.async_update_zone(
            self.zone_name,
            persisted_idle_slope=None,
            persisted_idle_slope_at=None,
        )

    def _read_numeric_sensor(self, entity_id: str | None) -> float | None:
        """Shared read path for any external numeric sensor: returns the
        float value, or None when the entity is missing, unavailable, or
        non-numeric. Used by both the room-temp and humidity readers (and
        any future numeric sensor input — predictive control / IAQ / etc.).

        Reads `state.state` only — not entity attributes. The v0.8 climate-
        attribute readers (`_target_temp_step`, `_current_climate_fan_mode`)
        intentionally don't reuse this path because their fallback semantics
        (typed default vs None) and value-vs-attribute access differ.
        """
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_room_temp(self) -> tuple[float | None, bool]:
        # `sensor_available` is True iff a numeric reading was produced.
        value = self._read_numeric_sensor(self.temp_entity_id)
        return value, value is not None

    def _read_humidity(self) -> float | None:
        """The apparent-temp formula treats None as "no adjustment", so a
        missing / offline humidity sensor degrades gracefully."""
        return self._read_numeric_sensor(self.humidity_entity_id)

    def _target_temp_step(self) -> float:
        """Read the climate entity's `target_temp_step` attribute.

        Falls back to `_DEFAULT_TEMP_STEP` (0.5 °C) when the entity is
        missing, the attribute is absent, non-numeric, or non-finite (NaN /
        +-inf). The non-finite guard matters because `float("nan")` succeeds
        — passing NaN into `_round_to_step` would raise on `int(round(x/nan))`
        and crash the apply path on every refresh for a misbehaving climate
        platform.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state is None:
            return _DEFAULT_TEMP_STEP
        raw = state.attributes.get("target_temp_step")
        if raw is None:
            return _DEFAULT_TEMP_STEP
        try:
            step = float(raw)
        except (TypeError, ValueError):
            return _DEFAULT_TEMP_STEP
        if not math.isfinite(step):
            return _DEFAULT_TEMP_STEP
        return step

    def _current_climate_fan_mode(self) -> str | None:
        """Read the climate entity's `fan_mode` attribute for sample capture.

        Returns None when the entity is missing, the attribute is absent, or
        the attribute is non-string. Stored alongside each sample so v0.9
        can partition slope estimates by fan_mode without waiting on a
        warm-up window.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state is None:
            return None
        raw = state.attributes.get("fan_mode")
        if isinstance(raw, str):
            return raw
        return None

    def _climate_fan_modes(self) -> list[str]:
        """The climate entity's supported `fan_modes`, as strings.

        Returns [] when the entity is missing/unavailable, exposes no
        `fan_modes`, or the attribute isn't a list — so the fan-boost command
        and the fan-mode selects fail closed (no command / unavailable select)
        on a climate that doesn't support fan control. v0.13.0.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state is None:
            return []
        raw = state.attributes.get("fan_modes")
        if not isinstance(raw, list):
            return []
        return [str(mode) for mode in raw]

    async def _maybe_command_fan(self, zone: StoredZone, action: str) -> None:
        """v0.13.0 deterministic fan-boost: command the climate's fan mode by
        action — `active_fan_mode` while heating/cooling, `idle_fan_mode` while
        idle. Opt-in via `fan_control_enabled`.

        Only ever changes the climate's `fan_mode` attribute, which the
        manual-edit detector deliberately ignores (v0.10.1), so this can never
        flush the learning buffer. Skips silently when: fan control is off; the
        target side is None (user hasn't picked, or an idle-only config); the
        mode isn't in the climate's live `fan_modes` (unavailable / fanless /
        stale stored value); or it already equals the current `fan_mode` (no
        redundant command — important for cloud/mesh-routed units).
        """
        if not zone["fan_control_enabled"]:
            return
        if action in (ACTION_HEAT, ACTION_COOL):
            desired = zone["active_fan_mode"]
        elif action == ACTION_IDLE:
            desired = zone["idle_fan_mode"]
        else:
            return
        if desired is None or desired not in self._climate_fan_modes():
            return
        if desired == self._current_climate_fan_mode():
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {"entity_id": self.climate_entity_id, "fan_mode": desired},
                blocking=True,
            )
        except HomeAssistantError as err:
            # A unit that rejects set_fan_mode (e.g. mid-transition to fan_only)
            # must not abort the rest of _maybe_apply_action -- the sample
            # append still needs to run.
            LOGGER.warning("%s: climate.set_fan_mode(%s) failed: %s", self.zone_name, desired, err)

    def _resolve_schedule(
        self,
        schedule_data: StoredProfileSchedule | None,
        fallback: tuple[float, float],
        *,
        ramp_minutes: float = 0,
    ) -> tuple[float, float]:
        if schedule_data is None or not schedule_data.get("current"):
            return fallback
        try:
            transitions = normalize_schedule(schedule_from_dict(schedule_data["current"]))
        except (KeyError, TypeError, ValueError) as err:
            LOGGER.warning(
                "%s: corrupt schedule (%s); falling back to manual band", self.zone_name, err
            )
            return fallback
        return schedule.resolve(transitions, dt_util.now().time(), ramp_minutes=ramp_minutes)

    def _compute_bands_per_step(
        self,
        schedule_data: StoredProfileSchedule | None,
        horizon_minutes: int,
        *,
        ramp_minutes: float = 0,
    ) -> list[tuple[float, float]] | None:
        """Build per-step ``(low, high)`` over the MPC horizon for lookahead.

        Returns ``None`` when the schedule is missing, empty, or fails to
        parse — MPC falls back to its snapshot path in that case (uses
        ``inputs.low / inputs.high`` for every step, the v0.8.x
        behaviour). Doesn't log on parse failure: ``_resolve_schedule``
        already warned for the same input in the same refresh cycle, so
        a second warning would just duplicate noise.

        Parses the schedule a third time per refresh (after
        ``_resolve_schedule`` and ``_schedule_next_transition``). The
        re-parse cost is microseconds and the alternative — threading
        a normalized list through five call sites — is more change
        surface than it's worth for the integration's current scale.
        """
        if schedule_data is None or not schedule_data.get("current"):
            return None
        try:
            transitions = normalize_schedule(schedule_from_dict(schedule_data["current"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not transitions:
            return None
        return schedule.upcoming_bands(
            transitions,
            dt_util.now().time(),
            horizon_minutes,
            MPC_SIMULATION_STEP_MINUTES,
            ramp_minutes=ramp_minutes,
        )

    def _schedule_next_transition(
        self,
        schedule_data: StoredProfileSchedule | None,
        *,
        ramp_minutes: float = 0,
    ) -> None:
        if self._unsub_transition_timer is not None:
            self._unsub_transition_timer()
            self._unsub_transition_timer = None
        if schedule_data is None or not schedule_data.get("current"):
            return
        try:
            transitions = normalize_schedule(schedule_from_dict(schedule_data["current"]))
        except (KeyError, TypeError, ValueError):
            return
        if not transitions:
            return
        secs = _seconds_until_next_transition(transitions, dt_util.now())
        if secs is None:
            return
        # v0.10.0: when ramp smoothing is enabled, wake at the ramp's
        # leading edge `t - R/2` instead of the bare transition `t`. This
        # ensures the smoothing actually starts on time in quiet rooms
        # where no sensor activity would otherwise trigger a refresh in
        # the leading half of the window. Only adjust when the leading
        # edge is still in the future — if `secs <= half_ramp_secs` we
        # are already inside the ramp window, and the existing wake-up
        # at the bare transition is the correct next-significant moment
        # (subtracting again would cause an immediate re-fire loop).
        if ramp_minutes > 0:
            half_ramp_secs = ramp_minutes * 60.0 / 2.0
            if secs > half_ramp_secs:
                secs = secs - half_ramp_secs
        capped = min(secs, _MAX_NEXT_TRANSITION_SECS)
        self._unsub_transition_timer = async_call_later(self.hass, capped, self._on_timer_fire)

    def _schedule_override_expiry(self, delta: timedelta) -> None:
        if self._unsub_override_timer is not None:
            self._unsub_override_timer()
        seconds = max(delta.total_seconds(), 1.0)
        self._unsub_override_timer = async_call_later(self.hass, seconds, self._on_timer_fire)

    async def _maybe_apply_action(
        self,
        decision: HysteresisDecision,
        enabled: bool,
        *,
        decision_room: float | None,
    ) -> None:
        """Translate the decision into climate.set_hvac_mode + set_temperature.

        Skipped entirely when `enabled=False` (shadow mode -- log only).
        Min-cycle suppression filters re-issue of the *same* action;
        cross-mode-cycle suppression blocks heat↔cool flips within a short
        dwell. Idle releases (heat→idle, cool→idle) pass through unchecked
        so a heat or cool cycle can always stop.

        After applying (or holding), appends a sample to the v0.6 predictor
        buffer reflecting the action the HVAC is actually in for the next
        interval -- either the newly-committed `decision.action` (when
        climate calls succeeded) or the prior `last_action` (when a gate
        suppressed).
        """
        now_utc = dt_util.utcnow()
        if not enabled:
            LOGGER.debug(
                "%s: shadow mode -- would %s (target_mode=%s, target_temp=%s)",
                self.zone_name,
                decision.action,
                decision.target_mode,
                decision.target_temp,
            )
            # In shadow mode the HVAC may be user-controlled. Record samples
            # under the decider's intent so the predictor learns idle drift;
            # the climate-state listener flushes if the user actually moves
            # the climate. There's an inherent window between a manual change
            # and the listener firing during which sample labels can be wrong
            # (e.g., labelled `idle` while the user is heating manually) --
            # the flush corrects this after-the-fact and the README documents
            # the limitation.
            #
            # Skip the append when there's no usable room reading: predictor
            # decisions with target_mode=None pair with decision_room=None,
            # and a sample with no temperature isn't a useful data point.
            if decision_room is None or decision.target_mode is None:
                return
            await self._append_sample(decision_room, decision.action, now_utc)
            return
        if decision.target_mode is None:
            # UNKNOWN_DECISION (room unavailable -- both hysteresis.decide and
            # predictor.decide return UNKNOWN when inputs.room is None).
            # decision_room is None at this point too, so no sample to append.
            return

        zone = self._store.get_zone(self.zone_name)
        last_action = zone["last_action"]
        last_action_at = _parse_iso(zone["last_action_at"])
        # `elapsed_s` is None for a fresh-from-restart zone (no action has
        # ever been committed). Both gates below short-circuit on `None`
        # via their `elapsed_s is not None` guards, so neither suppresses
        # the first heat or cool — correct: there's no committed action
        # to dwell after.
        elapsed_s = (
            (now_utc - last_action_at).total_seconds() if last_action_at is not None else None
        )

        # Same-mode min-cycle: don't re-issue the same action too quickly.
        if (
            last_action == decision.action
            and elapsed_s is not None
            and elapsed_s < zone["min_cycle_minutes"] * 60
        ):
            # Gate suppresses re-issue but the HVAC keeps doing `last_action`,
            # which equals `decision.action` here -- record the sample so the
            # predictor still learns the in-progress recovery slope.
            await self._append_sample(decision_room, last_action or ACTION_UNKNOWN, now_utc)
            return

        # Cross-mode min-cycle: don't flip between heat and cool too quickly.
        # The hysteresis decider never returns heat → cool directly — it
        # always releases through idle first — so on the normal path
        # `last_action` is `idle` and we look back at the action before
        # idle via `previous_action`. `last_action_at` is the time the
        # current (idle) action was committed, which equals the time the
        # prior heat/cool ended — exactly the timestamp the dwell should
        # be measured against. Idle/unknown prior actions don't trigger
        # the gate (no prior commitment to dwell after).
        #
        # The `last_action in (HEAT, COOL)` branch is defensive: it
        # cannot fire if the always-through-idle invariant in
        # hysteresis.py holds, but it ensures the gate still triggers if
        # that invariant is ever violated rather than silently allowing
        # a direct flip.
        prior_active_action = (
            last_action if last_action in (ACTION_HEAT, ACTION_COOL) else zone["previous_action"]
        )
        is_cross_mode_flip = (
            prior_active_action in (ACTION_HEAT, ACTION_COOL)
            and decision.action in (ACTION_HEAT, ACTION_COOL)
            and prior_active_action != decision.action
        )
        if (
            is_cross_mode_flip
            and elapsed_s is not None
            and elapsed_s < zone["cross_mode_min_minutes"] * 60
        ):
            LOGGER.debug(
                "%s: cross-mode min-cycle suppressed %s → %s "
                "(via=%s elapsed=%.0fs, threshold=%dmin)",
                self.zone_name,
                prior_active_action,
                decision.action,
                last_action,
                elapsed_s,
                zone["cross_mode_min_minutes"],
            )
            # The HVAC keeps doing `last_action` (typically idle, since the
            # decider releases through idle before the flip). Record under
            # that action so the predictor's idle_slope reflects what's
            # actually happening during the dwell.
            await self._append_sample(decision_room, last_action or ACTION_UNKNOWN, now_utc)
            return

        # Round the decision's target_temp to the climate's step before
        # issuing the service call: v0.8 MPC uses the band's upper edge as
        # the heat target, and that value may not align to the climate's
        # native resolution (e.g., 0.1 vs 0.5). The climate platform would
        # silently coerce on receive, but pre-rounding here means our
        # `_last_command_state` snapshot matches what the climate will end
        # up at — keeping the manual-edit listener's comparison honest.
        rounded_target_temp: float | None = None
        if decision.target_temp is not None:
            step = self._target_temp_step()
            rounded_target_temp = _round_to_step(decision.target_temp, step)

        # About to issue climate commands: snapshot our intent so the
        # climate-state listener can recognise the resulting echoes and
        # avoid mistaking them for manual edits. Only hvac_mode + target_temp
        # are compared (see `_on_climate_state_change`): fan_mode is captured
        # in samples but deliberately NOT part of the manual-edit comparison
        # (v0.10.1 -- the HVAC's own per-mode / autonomous fan changes were
        # flushing the learning buffer and starving MPC of idle samples).
        self._last_command_state = {
            "hvac_mode": decision.target_mode,
            "target_temp": rounded_target_temp,
        }
        self._last_command_at = now_utc

        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.climate_entity_id, "hvac_mode": decision.target_mode},
            blocking=True,
        )
        # v0.13.0 deterministic fan-boost. Placed right after set_hvac_mode so
        # it fires for idle (fan_only) AND heat AND cool — set_temperature below
        # is skipped for idle. Past all suppression gates + shadow-mode, so it
        # only runs when the action is genuinely applied.
        await self._maybe_command_fan(zone, decision.action)
        if rounded_target_temp is not None:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.climate_entity_id,
                    "temperature": rounded_target_temp,
                },
                blocking=True,
            )
        # Snapshot the climate's actual state after our commands settle. The
        # service calls leave `decision.target_temp=None` for idle releases,
        # but the climate often keeps a stale `temperature` attribute from
        # the prior heat/cool setpoint. Without this snapshot the listener
        # would mismatch (`None` vs the stale value) on any non-echo state
        # event and spuriously flush the buffer. Reading the live state means
        # the baseline reflects reality, not our incomplete intent.
        #
        # If the climate state isn't readable we KEEP the pre-call baseline
        # (set just before the service calls above) -- an unreachable climate
        # won't be emitting state-change events anyway, so there's nothing
        # for the listener to compare against. Resetting to None here would
        # silently disable manual-edit detection on the next event.
        fresh = self.hass.states.get(self.climate_entity_id)
        if fresh is not None:
            self._last_command_state = {
                "hvac_mode": fresh.state,
                "target_temp": fresh.attributes.get("temperature"),
            }
            # Reuse the pre-call `now_utc` rather than calling utcnow() again:
            # the small delta from slow climate calls would just slightly
            # narrow the echo window without changing correctness, but staying
            # in lockstep with `now_utc` matches the surrounding code's pattern
            # of "one timestamp per refresh."
            self._last_command_at = now_utc
        # Roll `previous_action` forward on real transitions; leave it alone
        # on same-mode re-commits (after the min-cycle window expires the
        # coordinator re-issues the same hvac_mode — that's a refresh, not
        # a transition, and the prior non-idle action shouldn't be
        # overwritten by a same-action self-reference).
        new_previous_action = (
            last_action if last_action != decision.action else zone["previous_action"]
        )
        await self._store.async_update_zone(
            self.zone_name,
            last_action=decision.action,
            last_action_at=now_utc.isoformat(),
            previous_action=new_previous_action,
        )
        # Record a sample under the newly-committed action — the predictor's
        # next refresh will see this sample in the trailing run for
        # decision.action and compute the slope from it.
        await self._append_sample(decision_room, decision.action, now_utc)

    async def _append_sample(
        self, decision_room: float | None, action: str, now_utc: datetime
    ) -> None:
        """Append a sample to the rolling buffer and (sometimes) persist it.

        Skipped when `decision_room is None` (sensor unavailable -- no data
        point worth recording). Rate-limit + age-cap logic lives in
        `predictor.append_sample`; this method just wires it to the store
        and updates the in-memory cache.

        v0.8 also captures the climate entity's current `fan_mode` attribute
        and persists it with the sample. v0.8 doesn't *use* fan_mode (slope
        estimation still partitions by action only), but v0.9's MPC
        extension partitions by `(action, fan_mode)` — recording it now
        means v0.9 ships with data already in the buffer.

        Disk persistence is throttled: every action transition writes
        immediately (the slope segmenter needs the boundary on cold start),
        but consecutive same-action samples persist at most once every
        SAMPLE_PERSIST_INTERVAL_S. Without this, the integration would write
        the full samples list to .storage every ~60 s and meaningfully
        accelerate flash wear on SD-card-backed installs. Worst-case data
        loss on crash is one persist interval of in-memory samples; the
        predictor recovers in well under the WLS window.
        """
        if decision_room is None:
            return
        # `prior_action` is captured BEFORE the append so it reflects the
        # buffer's last action, not the freshly-decided one. Used only to
        # decide whether to persist immediately (transition) or throttle.
        prior_action = self._samples_cache[-1].action if self._samples_cache else None
        fan_mode = self._current_climate_fan_mode()
        new_samples, appended = predictor.append_sample(
            self._samples_cache,
            now=now_utc,
            temp=decision_room,
            action=action,
            fan_mode=fan_mode,
        )
        if not appended:
            return
        self._samples_cache = new_samples

        # Always persist on action transitions (the slope segmenter relies on
        # the recorded boundary at cold start) and on the very first persist
        # after install/restart/flush (_last_sample_persist_at is None).
        # Otherwise rate-limit to SAMPLE_PERSIST_INTERVAL_S.
        is_transition = prior_action is not None and prior_action != action
        recently_persisted = (
            self._last_sample_persist_at is not None
            and (now_utc - self._last_sample_persist_at).total_seconds() < SAMPLE_PERSIST_INTERVAL_S
        )
        if not is_transition and recently_persisted:
            return

        await self._store.async_update_zone(
            self.zone_name,
            samples=[predictor.sample_to_dict(s) for s in new_samples],
        )
        self._last_sample_persist_at = now_utc

    @callback
    def _on_climate_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Flush the sample buffer when the climate entity changes outside our path.

        Compares observed state to `_last_command_state` (what we last asked
        the climate to be). Within the CLIMATE_ECHO_WINDOW_S window after our
        own command the state may transition through intermediate values
        (`set_hvac_mode` + `set_temperature` fire two state-change events
        sequentially, and a slow climate can take many seconds to acknowledge
        the second one) -- ignore those. Otherwise, a mismatch indicates a
        manual edit; flush samples so the slope estimator doesn't fit stale
        dynamics.

        v0.8 also compared `fan_mode` here, on the theory that a fan change
        alters the room's thermal dynamics. v0.10.1 removes it: in practice
        many HVACs report a different fan_mode in `fan_only` (idle) vs
        `heat`/`cool`, and modulate fan speed autonomously while running.
        Those device-driven changes are not "manual edits", but they tripped
        the comparison and flushed the buffer on every idle<->active
        transition — so the buffer never held idle AND recovery samples at
        once, `mpc.is_ready` never turned True, and MPC silently fell back to
        the reactive predictor (no schedule-lookahead pre-heat). Comparing
        only `hvac_mode` + `target_temp` still catches genuine manual setpoint
        / mode edits (incl. physical-remote edits with no HA context). fan_mode
        is still captured per-sample for future `(action, fan_mode)`
        partitioning; it just no longer forces a flush.
        """
        # `EventStateChangedData` declares both keys as required (`State | None`),
        # so subscript access is the type-honest read; .get() would silently
        # mask a future schema rename.
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if old_state is None or new_state is None:
            # Initial state-added or entity-removed -- not a manual edit.
            return
        observed = {
            "hvac_mode": new_state.state,
            "target_temp": new_state.attributes.get("temperature"),
        }
        now = dt_util.utcnow()
        # `0 <= elapsed < window` so a backwards NTP step (now < last_command_at)
        # doesn't trip the negative `< window` branch and suppress legitimate
        # manual-edit detection indefinitely.
        elapsed_s = (
            (now - self._last_command_at).total_seconds()
            if self._last_command_at is not None
            else None
        )
        is_echo = elapsed_s is not None and 0 <= elapsed_s < CLIMATE_ECHO_WINDOW_S
        if is_echo:
            # Climate may emit one event per attribute change. Update the
            # baseline to whatever the climate ended up at so the next
            # non-echo change is compared against the latest stable state.
            self._last_command_state = observed
            return
        if self._last_command_state is None:
            # No baseline yet -- first observed state becomes the baseline
            # without triggering a flush.
            self._last_command_state = observed
            return
        if observed == self._last_command_state:
            return
        LOGGER.info(
            "%s: manual climate edit detected (observed=%s, last_command=%s); "
            "flushing sample buffer",
            self.zone_name,
            observed,
            self._last_command_state,
        )
        self._samples_cache = []
        self._last_command_state = observed
        # Reset the persist throttle so the first sample after the flush
        # writes immediately, matching the "transitions always persist"
        # contract (a flush is functionally a forced segment boundary).
        self._last_sample_persist_at = None
        # v0.12.0: a manual edit invalidates the learned thermal model, so the
        # persisted idle slope is dropped too -- the new control regime may
        # have a different passive heat-loss rate. Cleared in the same write as
        # samples=[] to keep the flush atomic.
        self._last_idle_slope_persist_at = None
        self.hass.async_create_task(
            self._store.async_update_zone(
                self.zone_name,
                samples=[],
                persisted_idle_slope=None,
                persisted_idle_slope_at=None,
            )
        )


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed


def _seconds_until_next_transition(transitions: list[Transition], now: datetime) -> float | None:
    """Wall-clock seconds from `now` until the next `at` time (today or tomorrow).

    Returns None if `transitions` is empty or every candidate is in the past
    *and* none can be scheduled into tomorrow (shouldn't happen with sorted
    inputs, but keep the type honest).
    """
    if not transitions:
        return None
    today = now.date()
    times = [t.at for t in transitions]
    next_today = next((t for t in times if t > now.time()), None)
    if next_today is not None:
        candidate = datetime.combine(today, next_today, tzinfo=now.tzinfo)
    else:
        candidate = datetime.combine(today + timedelta(days=1), times[0], tzinfo=now.tzinfo)
    delta = (candidate - now).total_seconds()
    return delta if delta > 0 else None
