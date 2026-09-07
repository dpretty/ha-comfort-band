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
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
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
    COMMAND_WARN_INTERVAL_S,
    LOGGER,
    MPC_SIMULATION_STEP_MINUTES,
    PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES,
    SAMPLE_PERSIST_INTERVAL_S,
    SENSOR_EDGE_LOG_INTERVAL_S,
    SIGNAL_ACTIVE_PROFILE_CHANGED,
    SIGNAL_SHARED_SCHEDULE_LIST_CHANGED,
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
        # What the availability log last said, tracked apart from reality so a
        # throttled edge is re-offered on the next refresh instead of dropped.
        self._sensor_logged_available = True
        self._sensor_edge_logged_at: dict[bool, datetime | None] = {True: None, False: None}
        # Per-fault stamps for the command-path warnings: "dropped",
        # "setpoint", "fan".
        self._command_warn_logged_at: dict[str, datetime] = {}
        self._last_command_state: dict[str, Any] | None = None
        # What we last actually asked the climate for, kept apart from the
        # baseline above because the listener overwrites that with whatever the
        # entity reports. A unit is allowed to report either (see
        # `_observation_is_expected`).
        self._commanded_state: dict[str, Any] | None = None
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
        self.subscribe_and_hydrate()
        await self.async_config_entry_first_refresh()

    @callback
    def subscribe_and_hydrate(self) -> None:
        """Wire the event-driven triggers and restore cached state from the store.

        Named for both jobs because the order matters and is no longer obvious
        once this is a method rather than an inlined block: the first refresh
        must see a hydrated `_samples_cache`. `_append_sample` persists the whole
        list, and on a fresh coordinator `_last_sample_persist_at is None` so the
        first append writes immediately with no throttle -- run the refresh
        before hydration and the store is truncated to a single sample, wiping
        the learned thermal model on every restart and leaving `mpc.is_ready`
        permanently False.

        Split out of `async_setup` so tests can exercise the same wiring. This
        coordinator has `update_interval=None` -- every decision is triggered by
        a state change -- so a harness that skips this is not testing the
        production path at all: a sensor recovery reaches such a coordinator only
        through an explicit refresh, and `_on_climate_state_change` never fires,
        which silently makes every "no manual edit was detected" assertion
        vacuous. `async_setup` can't be reused directly because
        `async_config_entry_first_refresh` requires a real config entry in
        SETUP_IN_PROGRESS.

        Idempotent: a second call would otherwise orphan the first set of
        listeners, which `async_unload` could then never cancel -- leaving a
        torn-down coordinator able to command a live climate entity.
        """
        if self._unsub_state is not None:
            return
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
        self._sensor_logged_available = True
        self._sensor_edge_logged_at = {True: None, False: None}
        self._command_warn_logged_at = {}
        self._last_command_state = None
        self._commanded_state = None
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

    async def async_set_schedule_assignment(self, shared_id: str | None) -> None:
        """Assign this zone to a shared schedule (or None = "Own schedule").
        v0.14.0. Validates the id via the store, then refreshes so the band
        re-resolves from the new source immediately."""
        await self._store.async_set_zone_schedule_id(self.zone_name, shared_id)
        await self.async_refresh()
        # Membership changed: every zone's assignment select exposes a
        # `shared_schedules` summary carrying each schedule's member list, so
        # nudge them all to re-render (this zone joined/left a group).
        async_dispatcher_send(self.hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED)

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
        # Log the edge, not the state. The incident that prompted this had the
        # zone sitting *idle* when its sensor died, so nothing was commanded and
        # nothing was logged -- the room simply drifted for hours. This is the
        # line the binary sensor's docs point at.
        #
        # Rate-limited: a sensor flapping on a weak mesh crosses this boundary
        # hundreds of times an hour. The comparison is against what was last
        # *logged* rather than against reality, so a throttled edge is re-offered
        # on the next refresh instead of being dropped -- a blip that used up the
        # budget must not be able to hide the outage that follows it.
        if (
            sensor_available != self._sensor_logged_available
            # Integrations load in parallel, so during startup this zone can
            # easily refresh before the sensor's own integration has published
            # anything -- routine for MQTT, Zigbee2MQTT, Matter/Thread and
            # ESPHome, which is exactly the hardware the incident involved.
            # Warning then would be a guaranteed false positive on every
            # restart. The edge is left un-recorded rather than swallowed, so a
            # sensor that is genuinely dead is announced by the first refresh
            # after startup -- which for a zone with no schedule may not come,
            # since nothing else wakes this coordinator and a dead sensor emits
            # nothing. The binary sensor is `on` from the first refresh either
            # way.
            and self.hass.state is CoreState.running
            and self._may_log_sensor_edge(sensor_available)
        ):
            if sensor_available:
                LOGGER.info(
                    "%s: room sensor %s is reporting again", self.zone_name, self.temp_entity_id
                )
            elif zone["enabled"]:
                LOGGER.warning(
                    "%s: room sensor %s is unavailable -- this zone cannot control "
                    "until it reports again",
                    self.zone_name,
                    self.temp_entity_id,
                )
            else:
                # Shadow mode: the zone commands nothing either way, so this is
                # worth recording but not worth waking anybody for.
                LOGGER.info(
                    "%s: room sensor %s is unavailable (zone is in shadow mode)",
                    self.zone_name,
                    self.temp_entity_id,
                )
            self._sensor_logged_available = sensor_available
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
        # v0.14.0: when the zone is assigned a shared schedule, resolve its band
        # from that shared schedule (active profile, then default) instead of
        # the zone's own `schedules`. A dangling `schedule_id` (the shared
        # schedule was deleted) fails the `has_shared_schedule` guard and falls
        # back to the zone's own schedules — then to the manual band below — so
        # a refresh never raises. Everything downstream consumes `schedule_data`
        # unchanged (MPC lookahead, next-transition timer, ramp smoothing).
        sid = zone["schedule_id"]
        if sid is not None and self._store.has_shared_schedule(sid):
            schedule_data = self._store.get_shared_schedule_slot(
                sid, active_profile
            ) or self._store.get_shared_schedule_slot(sid, default_profile)
        else:
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

    def _may_log_command_warning(self, key: str) -> bool:
        """Throttle a command-path warning to one line per interval, per fault.

        All three faults repeat. The dropped command repeats hardest -- nothing
        is committed on that path, so the same-mode gate never arms and a
        room-temp-driven zone re-enters it every refresh for the length of the
        outage. The setpoint and fan faults repeat once per applied action, but
        can be permanent: a unit that will not take a plain setpoint, or a
        stored fan mode it advertises and refuses, is a property of the hardware
        rather than a passing fault.

        Each key is cleared when a call of its own kind next succeeds, so a
        fault's return is announced rather than sitting inside a stale budget.
        Note the "next succeeds": an episode that ends without one -- a fan the
        unit adopts by itself, so the call is skipped -- keeps its stamp until
        the budget expires, delaying the next announcement by up to an interval.
        """
        now = dt_util.utcnow()
        last = self._command_warn_logged_at.get(key)
        # `0 <=` because a backwards clock step -- an RTC-less Pi correcting
        # against NTP after boot -- makes the elapsed time negative, which would
        # otherwise satisfy the throttle and silence the log until the clock
        # caught up.
        if last is not None and 0 <= (now - last).total_seconds() < COMMAND_WARN_INTERVAL_S:
            return False
        self._command_warn_logged_at[key] = now
        return True

    def _may_log_sensor_edge(self, available: bool) -> bool:
        """Throttle sensor-availability logging to one line per direction.

        Per direction, not one shared budget: with a single timestamp a recovery
        line spends the following outage line's allowance, and the pending edge
        is only re-offered on the next refresh -- which for a dead sensor on a
        zone with no schedule never comes, because nothing else wakes the
        coordinator. The record then ends on the wrong word: either "reporting
        again" while the room has been dark for hours, or nothing at all for a
        real outage. Two budgets double the worst-case flapping volume -- 24
        lines an hour rather than 12, against roughly 350 unlatched.

        This narrows the problem rather than eliminating it: two *drops* still
        share a budget, so a link that blips and then dies inside one window
        leaves the record's last word as "reporting again". A throttled edge is
        re-offered on the next refresh, but a dead sensor emits no state changes
        and a zone with no schedule has no timer, so for that zone there may be
        no next refresh. The binary sensor is `on` throughout regardless, which
        is the surface this release is actually about; closing the log gap needs
        a wake-up of its own and is not worth the machinery here.
        """
        now = dt_util.utcnow()
        last = self._sensor_edge_logged_at[available]
        # `0 <=` because a backwards clock step -- an RTC-less Pi correcting
        # against NTP after boot -- makes the elapsed time negative, which would
        # otherwise satisfy the throttle and silence the log until the clock
        # caught up.
        if last is not None and 0 <= (now - last).total_seconds() < SENSOR_EDGE_LOG_INTERVAL_S:
            return False
        self._sensor_edge_logged_at[available] = now
        return True

    def _read_numeric_sensor(self, entity_id: str | None) -> float | None:
        """Shared read path for any external numeric sensor: returns the
        float value, or None when the entity is missing, unavailable,
        non-numeric, or non-finite (NaN / ±inf). Used by both the room-temp
        and humidity readers (and any future numeric sensor input --
        predictive control / IAQ / etc.).

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
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        # `float("nan")` parses happily, and every comparison against NaN is
        # False -- so hysteresis would read the room as below band and heat it
        # indefinitely, while `sensor_available` stayed True so nothing alerted.
        # A room we cannot compare is a room we cannot control, and this entity
        # exists to say so.
        return value if math.isfinite(value) else None

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
        """Read the climate entity's current `fan_mode` attribute, as a string.

        Returns None only when the entity is missing or the attribute is
        absent. A non-string value (e.g. an integer fan level) is coerced to
        str so it compares equal to the str-coerced `_climate_fan_modes()`
        entries — the v0.13.0 fan-boost redundant-command guard relies on that
        (an int current vs the stored string would otherwise never match,
        re-issuing the same fan command every cycle). It also means integer
        fan levels are captured in samples (for future `(action, fan_mode)`
        partitioning) instead of being dropped to None.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state is None:
            return None
        raw = state.attributes.get("fan_mode")
        return None if raw is None else str(raw)

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
            #
            # Throttled for the same reason as the other command-path warnings.
            # This one can be permanent: the guard above only skips the call
            # when the unit already reports the mode we want, so a stored fan
            # mode it advertises and refuses is retried on every apply, forever.
            # The wrapper in `_maybe_apply_action` shares this key -- one fault,
            # one budget, whichever of the two sites catches it.
            if self._may_log_command_warning("fan"):
                LOGGER.warning(
                    "%s: climate.set_fan_mode(%s) failed: %s", self.zone_name, desired, err
                )
        else:
            self._command_warn_logged_at.pop("fan", None)

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

        No commands are issued when `enabled=False` (shadow mode -- log only);
        it still records a sample, and drops what it had asked for so the
        manual-edit detector stops vouching for it. Min-cycle suppression
        filters re-issue of the *same* action; cross-mode-cycle suppression
        blocks heat↔cool flips within a short dwell. Idle releases (heat→idle,
        cool→idle) pass through unchecked so a heat or cool cycle can always
        stop.

        The commitment rests on `set_hvac_mode` alone -- that is the call that
        makes the unit start conditioning. The fan and setpoint calls that
        follow are best-effort: they refine a cycle that is already running, and
        a raise from either is warned about but changes nothing that was
        recorded.

        Appends a sample reflecting the action the HVAC is actually in for the
        next interval -- the newly-committed `decision.action` once
        `set_hvac_mode` has landed, or the prior `last_action` when a gate
        suppressed the re-issue. Nothing at all when the command did not reach
        the unit: an unreachable climate is not doing anything we can label.
        """
        now_utc = dt_util.utcnow()
        if not enabled:
            # Nothing is commanded here, so nothing of ours is expected either.
            # Left standing, the last command from before the zone was switched
            # to shadow would go on being accepted by the manual-edit detector
            # forever, and in shadow mode every change is somebody else's.
            self._commanded_state = None
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

        # About to issue climate commands: stamp the echo window so the
        # climate-state listener can recognise the resulting echoes and avoid
        # mistaking them for manual edits. Only hvac_mode + target_temp are
        # compared (see `_on_climate_state_change`): fan_mode is captured in
        # samples but deliberately NOT part of the manual-edit comparison
        # (v0.10.1 -- the HVAC's own per-mode / autonomous fan changes were
        # flushing the learning buffer and starving MPC of idle samples).
        #
        # Deliberately no baseline write here. It used to snapshot our intent,
        # but on the success path the tail below overwrites it from the entity
        # anyway, so the only paths it survived on were ones where the unit
        # never got what it describes -- a dropped command, `set_hvac_mode`
        # raising, and (before this change guarded them) a raising setpoint or
        # fan call. Those are exactly where our intent must not stand in for
        # what the unit is reporting. What we asked for is vouched for
        # separately, and only once it has actually been delivered.
        #
        # The cost of committing nothing on the dropped path is that
        # `last_action_at` never advances, so the same-mode gate never arms and
        # the zone re-issues the command once per refresh for the length of an
        # outage. That is bounded by the request-refresh debounce rather than by
        # any dwell here, so it scales with how fast the room sensor reports --
        # order of a hundred an hour for a 30-second sensor and several hundred
        # for a 10-second one, where a committed action holds it to one per
        # min-cycle however fast the sensor is. They are filtered
        # out at dispatch, so no device traffic leaves the machine and the
        # warning stays throttled; it self-limits on the first commit that
        # lands. A backoff would be an improvement, not a correctness fix.
        previous_command_at = self._last_command_at
        self._last_command_at = now_utc

        # HA filters on availability *at dispatch*, so that is what has to be
        # sampled -- see the delivery check below.
        before = self.hass.states.get(self.climate_entity_id)
        was_unavailable = before is not None and before.state == STATE_UNAVAILABLE
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.climate_entity_id, "hvac_mode": decision.target_mode},
                blocking=True,
            )
        except Exception as err:
            # The load-bearing call, and until now the only one still bare. A
            # raise here escaped into the fire-and-forget apply task, which
            # meant no log of our own, no commit, and -- because the same-mode
            # gate never arms without one -- the same re-entry on every refresh
            # that used to hold the echo window open across an outage. Same
            # treatment as a dropped command: say so once per interval, un-arm
            # the window, and leave the store describing what the unit was last
            # actually told.
            #
            # Not hypothetical: a cloud unit can time out, and from Home
            # Assistant 2025.4 `climate.set_hvac_mode` raises for a mode the
            # entity does not advertise, where 2024.12 only warns. This
            # integration commands `fan_only` on every idle release.
            if self._may_log_command_warning("mode"):
                LOGGER.warning(
                    "%s: could not command %s to %s (%s) -- not recording it; will retry",
                    self.zone_name,
                    self.climate_entity_id,
                    decision.target_mode,
                    err,
                )
            self._last_command_at = previous_command_at
            return
        self._command_warn_logged_at.pop("mode", None)

        # A clean return is not proof of delivery. Home Assistant answers a call
        # it cannot deliver by skipping the entity and returning normally
        # (`helpers/service.py`: `if not entity.available: continue`), so an
        # unavailable climate silently swallows the command. Recording it anyway
        # leaves the store describing an action the unit never took, which the
        # dwell gates and every later decision then trust. Leaving `last_action`
        # alone makes the next refresh simply try again.
        #
        # Narrowed to `unavailable` rather than also covering a missing entity:
        # an absent climate entity is a misconfiguration that breaks the zone
        # outright, while `unavailable` is the transient bridge or cloud blip
        # this is here for.
        # `was_unavailable` is the half that matters: HA filters at dispatch, so
        # the state *before* the call is what decides whether it was delivered.
        # Reading only afterwards cannot tell a dropped command from a delivered
        # one whose unit then blinked -- and plenty of integrations end
        # `async_set_hvac_mode` with a refresh that briefly marks a
        # just-commanded unit unreachable. Discarding the commit there is worse
        # than the bug being fixed: nothing is ever recorded, so the min-cycle
        # guard never arms and the zone re-commands on every refresh.
        #
        # The post-call half only narrows this further, to units still
        # unavailable once the call returns. Two reviewers read that differently
        # -- whether a state machine lagging a reconnect means the entity was
        # really available at the filter -- and neither could construct the race.
        # It is kept because the two failure directions are not symmetric: a
        # wrongly skipped commit re-commands every refresh and is unbounded,
        # while a wrongly kept one costs at most one min-cycle of suppression.
        # Both halves read the state machine, which can itself disagree with
        # `entity.available` during a reconnect; that residual is unfixable from
        # here and is the same on either side of this change.
        live = self.hass.states.get(self.climate_entity_id)
        if was_unavailable and live is not None and live.state == STATE_UNAVAILABLE:
            if self._may_log_command_warning("dropped"):
                LOGGER.warning(
                    "%s: commanded %s to %s but it is unavailable, so the call "
                    "was dropped -- not recording it; will retry",
                    self.zone_name,
                    self.climate_entity_id,
                    decision.target_mode,
                )
            # Deliberately no sample. Labelling one under `last_action` looks
            # right -- it is what the suppression gates do -- but measurement
            # says otherwise: with `last_action=heating` and a unit that has
            # actually stopped, an hour of perfectly good idle drift is recorded
            # as a heat run that the v0.15.0 sign guard then rejects, so the
            # idle slope reads None where it would otherwise have been learned.
            # We do not know what an unreachable unit is doing, and guessing
            # displaces data we already have.
            #
            # And un-arm the echo window: nothing was delivered, so there is no
            # echo of ours coming. Leaving it armed was measured as a real hole,
            # because this path re-enters on every refresh -- for any sensor
            # reporting faster than CLIMATE_ECHO_WINDOW_S the window never
            # closed for the length of the outage, so the reconnect carrying
            # somebody's wall edit was absorbed as an echo instead of compared.
            # At a 30-second sensor that missed the edit every time, where
            # `main` caught it every time.
            self._last_command_at = previous_command_at
            return

        # Record the commitment on the strength of `set_hvac_mode` alone: that
        # is the call that makes the unit start conditioning, so from here on the
        # store has to say so. Everything below refines a cycle that is already
        # running.
        #
        # This ordering is load-bearing. `last_action` drives the min-cycle and
        # cross-mode dwells and every later decision, so a raise from either
        # call below used to leave the unit conditioning while the store said
        # otherwise -- and one of them raises on ordinary hardware: an entity
        # advertising only TARGET_TEMPERATURE_RANGE (Ecobee, Nest, many
        # heat_cool mini-splits) makes Home Assistant itself raise for a plain
        # `temperature`, so on that whole class the zone's bookkeeping never
        # became correct at all. Committing before the setpoint lands can only
        # widen the dwell slightly, which is the safe direction to err.
        new_previous_action = (
            last_action if last_action != decision.action else zone["previous_action"]
        )
        await self._store.async_update_zone(
            self.zone_name,
            last_action=decision.action,
            last_action_at=now_utc.isoformat(),
            previous_action=new_previous_action,
        )

        self._command_warn_logged_at.pop("dropped", None)

        # v0.13.0 deterministic fan-boost. Placed right after set_hvac_mode so
        # it fires for idle (fan_only) AND heat AND cool — set_temperature below
        # is skipped for idle. Past all suppression gates + shadow-mode, so it
        # only runs when the action is genuinely applied.
        #
        # Both of the following are best-effort: neither failing changes what the
        # unit is doing, and neither may skip the bookkeeping above or the sample
        # below. `_maybe_command_fan` catches only `HomeAssistantError`, so a
        # cloud unit's timeout would otherwise escape into the fire-and-forget
        # apply task.
        try:
            await self._maybe_command_fan(zone, decision.action)
        except Exception as err:
            if self._may_log_command_warning("fan"):
                LOGGER.warning(
                    "%s: commanded %s but could not set its fan mode (%s)",
                    self.zone_name,
                    self.climate_entity_id,
                    err,
                )
        setpoint_applied: float | None = None
        if rounded_target_temp is not None:
            try:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": self.climate_entity_id,
                        "temperature": rounded_target_temp,
                    },
                    blocking=True,
                )
                setpoint_applied = rounded_target_temp
                self._command_warn_logged_at.pop("setpoint", None)
            except Exception as err:
                if self._may_log_command_warning("setpoint"):
                    LOGGER.warning(
                        "%s: commanded %s to %s but could not set the target "
                        "temperature to %s (%s) -- the unit is running against "
                        "its own setpoint",
                        self.zone_name,
                        self.climate_entity_id,
                        decision.target_mode,
                        rounded_target_temp,
                        err,
                    )
        # Snapshot the climate's actual state after our commands settle. The
        # service calls leave `decision.target_temp=None` for idle releases,
        # but the climate often keeps a stale `temperature` attribute from
        # the prior heat/cool setpoint. Without this snapshot the listener
        # would mismatch (`None` vs the stale value) on any non-echo state
        # event and spuriously flush the buffer. Reading the live state means
        # the baseline reflects reality, not our incomplete intent.
        #
        # An unreadable climate leaves `target_temp` None below, which is the
        # honest answer: it isn't emitting state-change events for the listener
        # to compare against either.
        fresh = self.hass.states.get(self.climate_entity_id)
        # Both fields come from the entity, never from our intent. Pinning what
        # we commanded here was tried on both fields and measured worse on both.
        # For the setpoint: `ClimateEntity.state_attributes` puts `temperature`
        # through `display_temp`, which rounds to the entity's own `precision` --
        # unrelated to the `target_temp_step` we rounded to, and absent from
        # `capability_attributes` when the platform publishes no step -- so a
        # whole-degree unit commanded 19.5 reports 20 forever, and a same-mode
        # re-commit changes nothing it reports, so no echo ever corrects the
        # baseline. For the mode: a unit that adopts it late keeps publishing its
        # other attributes meanwhile (`current_temperature` on its own MQTT topic
        # is the ubiquitous case), and each of those carries the old mode -- read
        # as a hand edit, six flushes an hour against `main`'s two.
        #
        # What we asked for is recorded separately below and accepted alongside
        # this, which is what covers the lag in both fields without letting our
        # intent stand in for the unit's own report.
        #
        # `unavailable` / `unknown` are not states to record, for the same
        # reason the listener refuses to compare them: a just-commanded unit
        # reading unreachable for a moment is ordinary, and `{unavailable,
        # None}` as a baseline matches nothing the unit will ever report. Leave
        # the previous baseline standing instead -- it still describes the last
        # state the unit was really in.
        if fresh is not None and fresh.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._last_command_state = {
                "hvac_mode": fresh.state,
                "target_temp": fresh.attributes.get("temperature"),
            }
        elif self._last_command_state is None:
            # Nothing to leave standing. Our intent is a poor baseline, but a
            # missing one makes the listener adopt whatever arrives first, and
            # for an entity that is merely slow to appear that is worse.
            self._last_command_state = {
                "hvac_mode": decision.target_mode,
                "target_temp": rounded_target_temp,
            }
        # Only what actually landed: a raised `set_temperature` never reached the
        # unit, so its value must not be treated as something the unit may
        # report. Absent key rather than None -- None is a setpoint a climate can
        # genuinely report.
        self._commanded_state = {"hvac_mode": decision.target_mode}
        if setpoint_applied is not None:
            self._commanded_state["target_temp"] = setpoint_applied
        # `_last_command_at` is already `now_utc` from before the calls, so the
        # echo window measures from when we started commanding rather than from
        # whenever a slow unit finished -- one timestamp per refresh, as
        # everywhere else here.
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

    def _observation_is_expected(self, observed: dict[str, Any]) -> bool:
        """True when nothing in `observed` looks like somebody else's edit.

        A field is expected if it matches the baseline (what the entity was last
        seen reporting) *or* what we last commanded for it. Both are needed, and
        neither alone will do: a slow unit reports its old value long after our
        call and only catches up outside the echo window, while a unit that
        coerces the setpoint -- rounding it to its own display precision, or
        snapping it server-side -- never reports our value at all. Picking one at
        write time therefore breaks the other, and both failures are the same
        one: a spurious "manual edit" that flushes the sample buffer and drops
        the persisted idle slope, so `mpc.is_ready` never turns true.

        This does soften detection, per field, and by a mixture rather than a
        single state: while the two disagree, a hand edit landing on our value
        for one field and the unit's for the other is accepted too. It is
        bounded on both ends. The caller moves the baseline on for every
        observation this accepts, so the two collapse to one as soon as the unit
        agrees with us; and a detected edit clears the commanded side outright,
        so a second edit is judged against the occupant's own state alone.

        What remains is a hand edit made during the lag window that lands on
        one of the two values in each field. Landing on both of ours is a change
        we would have made anyway; the mixtures are not, and turning the heating
        off at the wall while leaving our setpoint alone is the one that costs
        something -- it goes unflushed until the unit catches up or we command
        again. That is the price of not flushing on every lagging unit's own
        echo, which is the far commoner event.
        """
        baseline = self._last_command_state or {}
        commanded = self._commanded_state or {}
        return all(
            (key in baseline and value == baseline[key])
            or (key in commanded and value == commanded[key])
            for key, value in observed.items()
        )

    @callback
    def _on_climate_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Flush the sample buffer when the climate entity changes outside our path.

        Compares the observed state against two things: `_last_command_state`,
        what the entity was last seen reporting, and `_commanded_state`, what we
        last asked it for -- a field matching either is expected (see
        `_observation_is_expected`). Within the CLIMATE_ECHO_WINDOW_S window after our
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
        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # A unit dropping off the network is not somebody at the wall, and
            # `{unavailable, None}` matches nothing, so comparing it flushed the
            # learned model on every bridge blip. Ignoring it also leaves the
            # baseline describing the last state the unit was really in, which
            # is what the reconnect has to be judged against: if it comes back
            # as we left it there is nothing to flush, and if somebody changed
            # it meanwhile that is caught then.
            #
            # `unknown` for the same reason and by the same route: Home
            # Assistant writes it whenever `ClimateEntity.hvac_mode` is None,
            # which is the ordinary shape of an MQTT climate that is reachable
            # again but has not received its mode topic yet. Covering only
            # `unavailable` left that whole class flushing twice per blip --
            # once on the way out and once on the way back, because the first
            # comparison leaves the baseline holding `unknown`.
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
        if self._observation_is_expected(observed):
            # Move the baseline on, even though this matched. Without it the
            # pre-command values stay acceptable for as long as the command
            # stands: the baseline is read *before* a lagging unit catches up
            # (see `_maybe_apply_action`), so an occupant putting the thermostat
            # back to exactly what it showed beforehand -- the likeliest edit
            # there is -- would be silently swallowed. Refreshing also collapses
            # the two accepted values back to one as soon as the unit agrees,
            # confining "either answer" to the lag window it exists for.
            self._last_command_state = observed
            return
        LOGGER.info(
            "%s: manual climate edit detected (observed=%s, last_seen=%s, "
            "commanded=%s); flushing sample buffer",
            self.zone_name,
            observed,
            self._last_command_state,
            self._commanded_state,
        )
        self._samples_cache = []
        self._last_command_state = observed
        # And our last command stops vouching for anything. The unit is
        # demonstrably not doing what we asked, so leaving it standing accepts,
        # field by field, a mixture of what the occupant just set and what we
        # asked for -- a state neither of us ever chose. Concretely: they switch
        # to cool 24 (flush, correctly), then put the mode back to heat and keep
        # their 24. Mode matches our command, setpoint matches what they just
        # set, so nothing flushes and the unit heats to 24 with the model still
        # fitted to our own cycle.
        self._commanded_state = None
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
