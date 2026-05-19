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

from dataclasses import dataclass
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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import apparent_temp, hysteresis, predictor, schedule
from .const import (
    ACTION_COOL,
    ACTION_HEAT,
    ACTION_UNKNOWN,
    CLIMATE_ECHO_WINDOW_S,
    LOGGER,
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
    thermal_slopes: ThermalSlopes

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

    # ----- mutators (called from entities + services) -----

    def get_zone_data(self) -> StoredZone:
        """Snapshot of the persisted zone (deep copy). Cheap; KB-sized."""
        return self._store.get_zone(self.zone_name)

    async def async_set_param(self, field: str, value: Any) -> None:
        """Update a tunable (deadband_*, override_hours, min_cycle_minutes,
        cross_mode_min_minutes, lookahead_minutes) without triggering an
        override.

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

    async def async_set_use_apparent_temperature(self, use_apparent_temperature: bool) -> None:
        await self._store.async_update_zone(
            self.zone_name, use_apparent_temperature=use_apparent_temperature
        )
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
            schedule_data, fallback=(zone["manual_low"], zone["manual_high"])
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
        predicted_decision = predictor.decide(
            thermal_slopes,
            hyst_inputs,
            lookahead_minutes=zone["lookahead_minutes"],
            hysteresis_decision=hyst_decision,
        )
        final_decision = predicted_decision if zone["learning_enabled"] else hyst_decision

        # Reschedule next-transition + override-expiry timers.
        self._schedule_next_transition(schedule_data)
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
            thermal_slopes=thermal_slopes,
        )

        # Apply in a follow-up task so this refresh returns immediately --
        # entities can render the new state without waiting on climate calls.
        self.hass.async_create_task(
            self._maybe_apply_action(final_decision, zone["enabled"], decision_room=decision_room)
        )

        return state

    # ----- helpers -----

    def _read_numeric_sensor(self, entity_id: str | None) -> float | None:
        """Shared read path for any external numeric sensor: returns the
        float value, or None when the entity is missing, unavailable, or
        non-numeric. Used by both the room-temp and humidity readers (and
        any future sensor input — predictive control / IAQ / etc.)."""
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

    def _resolve_schedule(
        self,
        schedule_data: StoredProfileSchedule | None,
        fallback: tuple[float, float],
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
        return schedule.resolve(transitions, dt_util.now().time())

    def _schedule_next_transition(self, schedule_data: StoredProfileSchedule | None) -> None:
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

        # About to issue climate commands: snapshot our intent so the
        # climate-state listener can recognise the resulting echoes and
        # avoid mistaking them for manual edits.
        self._last_command_state = {
            "hvac_mode": decision.target_mode,
            "target_temp": decision.target_temp,
        }
        self._last_command_at = now_utc

        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.climate_entity_id, "hvac_mode": decision.target_mode},
            blocking=True,
        )
        if decision.target_temp is not None:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.climate_entity_id,
                    "temperature": decision.target_temp,
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
        new_samples, appended = predictor.append_sample(
            self._samples_cache, now=now_utc, temp=decision_room, action=action
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
        self.hass.async_create_task(self._store.async_update_zone(self.zone_name, samples=[]))


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
