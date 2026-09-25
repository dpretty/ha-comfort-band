"""Per-zone DataUpdateCoordinator.

Event-driven (`update_interval=None`); refreshes fire from:
  - room-temp state changes (debounced 2 s)
  - active-profile dispatcher signal
  - one-shot timers for override-expiry + next-transition
  - explicit `async_request_refresh()` from numbers/switches/services
  - v0.18.0, only once the room sensor has been unavailable for the grace
    period: the fallback-grace timer, and changes to the stand-in reading
    (the fallback sensor's state, or the climate entity's
    `current_temperature`)

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
    DEFAULT_FALLBACK_TO_CLIMATE,
    FALLBACK_DEADBAND_EXTRA,
    FALLBACK_GRACE_S,
    LOGGER,
    MPC_SIMULATION_STEP_MINUTES,
    PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES,
    ROOM_SOURCE_CLIMATE,
    ROOM_SOURCE_FALLBACK_SENSOR,
    ROOM_SOURCE_NONE,
    ROOM_SOURCE_PRIMARY,
    ROOM_SOURCES_STAND_IN,
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

    `room` is the *raw* reading in use: the configured room sensor while it
    reports, otherwise (v0.18.0) the stand-in named by `room_source`, or None.
    `apparent_temperature` is always the Steadman value (which equals `room`
    when humidity is None). `decision_room` is whichever of those was
    actually fed into hysteresis — surfaced for the card so users can see the
    value driving control.

    `sensor_available` is about the *configured* sensor only. It stays False
    for as long as that sensor is dark, whether or not a stand-in is driving
    control, so `binary_sensor.{zone}_room_sensor_unavailable` keeps meaning
    "the device that needs attention is not reporting".
    """

    zone: StoredZone
    room: float | None
    sensor_available: bool
    # v0.18.0: one of the ROOM_SOURCE_* constants. `fallback_active` is the
    # derived "a stand-in is driving control" flag for consumers of ZoneState
    # (only the tests today; nothing in the integration reads it); the
    # coordinator computes the same predicate locally before the state exists.
    room_source: str
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
    # (min) of the substituted value -- since v0.19.0, from the newest sample
    # behind it -- and None unless source is "cached". Both
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

    @property
    def fallback_active(self) -> bool:
        return self.room_source in ROOM_SOURCES_STAND_IN


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
        *,
        fallback_temp_entity_id: str | None = None,
        fallback_to_climate: bool = DEFAULT_FALLBACK_TO_CLIMATE,
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
        # v0.18.0 room-sensor fallback. Resolution while the configured sensor
        # is dark: the fallback sensor if one is configured and reporting, else
        # the climate entity's `current_temperature` if allowed, else nothing.
        if fallback_temp_entity_id == temp_entity_id:
            # A sensor cannot stand in for itself -- and left in place it would
            # be worse than useless: `_on_temp_change` tells the two apart by
            # entity_id, so the room sensor's own changes would be filtered as
            # fallback-sensor changes and the zone would never refresh from it.
            # The OptionsFlow refuses this; guarded here as well so that no
            # construction path can reach it.
            LOGGER.warning(
                "%s: fallback sensor %s is the room sensor itself -- a sensor cannot "
                "stand in for itself; ignoring the fallback",
                zone_name,
                fallback_temp_entity_id,
            )
            fallback_temp_entity_id = None
        self.fallback_temp_entity_id = fallback_temp_entity_id
        self.fallback_to_climate = fallback_to_climate
        # When the configured sensor was first seen dark by a refresh; None
        # while it reports. The grace period is measured from here.
        self._primary_unavailable_since: datetime | None = None
        # One-shot timer that wakes the zone when the grace period ends. A
        # dead sensor emits nothing, so without it a zone with no schedule
        # would learn that its stand-in is now allowed only on the next
        # unrelated refresh -- which may never come.
        self._unsub_fallback_timer: CALLBACK_TYPE | None = None
        # Stand-in edge log, the same shape as the availability edge below:
        # what the log last said (a stand-in in use, and which one), tracked
        # apart from reality so a throttled edge is re-offered on the next
        # refresh, plus per-direction stamps for the throttle.
        self._standin_logged_engaged: bool = False
        self._standin_logged_source: str = ROOM_SOURCE_NONE
        self._standin_edge_logged_at: dict[bool, datetime | None] = {True: None, False: None}
        # The last stand-in that engaged in the current outage, None until one
        # does. The hand-back line keys on this rather than on what was last
        # logged, because a stand-in that went dark before the room sensor
        # returned has already been logged as lost -- and the record still
        # owes the reader the end of that stand-in's story. The sample flush
        # (`_flush_samples_for_stand_in`) keys on it too, so one outage is
        # one episode: the action in force when the first stand-in engaged
        # is remembered for the whole outage, not again after a stand-in
        # goes dark and comes back, nor when the fallback sensor hands over
        # to the climate reading; and the flush lands at most once.
        self._standin_last_engaged: str | None = None
        self._standin_action_at_engage: str | None = None
        self._standin_flushed: bool = False
        # Whether this outage has been told that the grace period ended with
        # no stand-in reading at all -- the retraction of the "takes over in"
        # promise, once per outage.
        self._standin_missing_logged: bool = False
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
        # Per-fault stamps for the command-path warnings: "mode", "dropped",
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
        # The fallback sensor is watched through the same debounced path but
        # filtered in `_on_temp_change`: its changes only matter while the
        # configured sensor is dark, and a healthy zone must not gain refreshes
        # (and samples) from a sensor that is not driving it.
        if self.fallback_temp_entity_id is not None:
            watch.append(self.fallback_temp_entity_id)
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
            self._unsub_fallback_timer,
        ):
            if unsub is not None:
                unsub()
        self._unsub_fallback_timer = None
        self._primary_unavailable_since = None
        self._standin_logged_engaged = False
        self._standin_logged_source = ROOM_SOURCE_NONE
        self._standin_edge_logged_at = {True: None, False: None}
        self._standin_last_engaged = None
        self._standin_action_at_engage = None
        self._standin_flushed = False
        self._standin_missing_logged = False
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
    def _on_temp_change(self, event: Event[EventStateChangedData]) -> None:
        # A change on the fallback sensor is only a reason to refresh while it
        # might be the reading in use -- i.e. once the configured sensor has
        # been dark for the grace period. Before that it would add refreshes
        # (and, while the configured sensor reports, samples) to a zone it is
        # not driving; inside the grace window the timer's own refresh is what
        # engages the stand-in, and reads whatever it says then.
        if event.data["entity_id"] == self.fallback_temp_entity_id and not self._grace_elapsed(
            dt_util.utcnow()
        ):
            return
        self._schedule_debounced_refresh()

    @callback
    def _schedule_debounced_refresh(self) -> None:
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
        now_utc = dt_util.utcnow()

        primary_room, sensor_available = self._read_room_temp()
        # v0.18.0: while the configured sensor is dark, and once it has been
        # dark for the grace period, a stand-in reading may drive control.
        room, room_source = self._resolve_room_reading(primary_room, sensor_available, now_utc)
        fallback_active = room_source in ROOM_SOURCES_STAND_IN
        # The first refresh of this outage on which a stand-in drives control.
        # Read before the edge log below, which records the stand-in. The
        # action in force at that moment is what the stand-in's cycle is
        # measured against (see `_flush_samples_for_stand_in`).
        engaging = fallback_active and self._standin_last_engaged is None
        if engaging:
            self._standin_action_at_engage = zone["last_action"]
            self._standin_flushed = False
        # Whether this refresh's availability line already said that no
        # stand-in has a reading -- the withheld edge re-offered past the
        # boundary -- so the grace-period line below is not put directly
        # underneath it saying the same thing.
        boundary_retracted = False
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
            # after startup. Before v0.18.0 that refresh might never have come
            # for a zone with no schedule, since nothing else wakes this
            # coordinator and a dead sensor emits nothing; the fallback grace
            # timer now wakes every zone once at the grace boundary, so a
            # withheld edge is re-offered there at the latest. The binary
            # sensor is `on` from the first refresh either way.
            and self.hass.state is CoreState.running
            and self._may_log_sensor_edge(sensor_available)
        ):
            if sensor_available:
                LOGGER.info(
                    "%s: room sensor %s is reporting again", self.zone_name, self.temp_entity_id
                )
            elif zone["enabled"] and fallback_active:
                # A withheld edge -- throttled after a blip-then-die, or
                # suppressed at startup -- re-offered by the refresh that
                # engages the stand-in: for a zone with no schedule, the grace
                # timer's. The engage line that follows names the stand-in.
                LOGGER.warning(
                    "%s: room sensor %s is unavailable -- a stand-in reading is taking over",
                    self.zone_name,
                    self.temp_entity_id,
                )
            elif zone["enabled"] and self._has_fallback_source():
                # A stand-in is configured or allowed, so the outage is a
                # gap rather than a stop: say how long the gap will be.
                # Measured from the first dark refresh rather than assumed to
                # be the whole grace period, because a withheld edge can be
                # re-offered part-way through the window -- or past its end,
                # when the stand-in turned out to have no reading either.
                since = self._primary_unavailable_since
                elapsed_s = (now_utc - since).total_seconds() if since is not None else 0.0
                remaining_s = max(0.0, FALLBACK_GRACE_S - elapsed_s)
                if remaining_s > 0:
                    LOGGER.warning(
                        "%s: room sensor %s is unavailable -- no control until it reports "
                        "again or a stand-in reading takes over in %d s",
                        self.zone_name,
                        self.temp_entity_id,
                        remaining_s,
                    )
                else:
                    boundary_retracted = True
                    LOGGER.warning(
                        "%s: room sensor %s is unavailable -- no control until it reports "
                        "again or a stand-in reading becomes available",
                        self.zone_name,
                        self.temp_entity_id,
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
        # The outage line promised that a stand-in "takes over in N s". When
        # the grace period ends and none has a reading -- a unit that publishes
        # no `current_temperature`, or the fallback sensor dark too with the
        # climate reading switched off -- nothing else says so: no stand-in
        # engages, so there is no hand-over line, and the record's last word
        # would be a hand-over that never happened, followed by hours of no
        # control. Once per outage, and only for an outage in which no stand-in
        # engaged: one that engaged and then went dark is closed by the edge
        # log's "unavailable too" line instead. Withheld while Home Assistant
        # is starting for the availability edge's reason -- the climate
        # entity's own integration may simply not have published yet.
        if (
            room_source == ROOM_SOURCE_NONE
            and self._standin_last_engaged is None
            and not self._standin_missing_logged
            and not boundary_retracted
            and self._grace_elapsed(now_utc)
            and self._has_fallback_source()
            and self.hass.state is CoreState.running
        ):
            self._standin_missing_logged = True
            tried: list[str] = []
            if self.fallback_temp_entity_id is not None:
                tried.append(self.fallback_temp_entity_id)
            if self.fallback_to_climate:
                tried.append(f"{self.climate_entity_id} current_temperature")
            log = LOGGER.warning if zone["enabled"] else LOGGER.info
            log(
                "%s: grace period over -- no stand-in has a reading (%s); no control until "
                "it or room sensor %s reports%s",
                self.zone_name,
                ", ".join(tried),
                self.temp_entity_id,
                "" if zone["enabled"] else " (zone is in shadow mode)",
            )
        # Flush once the stand-in has changed the action, on the first refresh
        # that sees the change committed -- a stand-in refresh, or the hand-back
        # refresh itself, which is the last chance: the apply task this refresh
        # spawns appends the first post-hand-back sample. Evaluated ahead of the
        # edge log, which forgets the stand-in on the hand-back edge.
        if (
            (engaging or self._standin_last_engaged is not None)
            and not self._standin_flushed
            and zone["last_action"] != self._standin_action_at_engage
        ):
            await self._flush_samples_for_stand_in()
            self._standin_flushed = True
        self._log_room_source_edge(room_source, zone["enabled"], now_utc)
        humidity = self._read_humidity()
        # `apparent_temp.compute(T, None) → T`, so when humidity is
        # unavailable the apparent value silently equals the room reading.
        # That's deliberate: it lets `use_apparent_temperature=True` stay
        # safe across humidity-sensor outages.
        apparent_temperature = apparent_temp.compute(room, humidity) if room is not None else None

        # Re-validate override.
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

        # A stand-in reading gets wider deadbands: the climate entity's own
        # sensor sits at the indoor unit and usually reports whole degrees, so
        # deadbands tuned for a room sensor would short-cycle on it.
        deadband_extra = FALLBACK_DEADBAND_EXTRA if fallback_active else 0.0
        hyst_inputs = HysteresisInputs(
            room=decision_room,
            low=eff_low,
            high=eff_high,
            deadband_below=zone["deadband_below"] + deadband_extra,
            deadband_above=zone["deadband_above"] + deadband_extra,
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
        ) = await self._resolve_idle_slope(
            thermal_slopes, zone, now_utc, persist_ok=not fallback_active
        )
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
        #
        # v0.18.0: on a stand-in reading the zone runs plain hysteresis. The
        # learned slopes describe the configured sensor's placement and
        # resolution, and the predictor's anticipation and MPC's plan both
        # extrapolate from the current reading -- a reading from a sensor with
        # a different offset. The shadow sensors still show what they would do.
        if fallback_active:
            final_decision = hyst_decision
        elif zone["learning_enabled"] and zone["mpc_enabled"] and mpc_ready:
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
            room_source=room_source,
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
            self._maybe_apply_action(
                final_decision,
                zone["enabled"],
                decision_room=decision_room,
                record_samples=not fallback_active,
            )
        )

        return state

    # ----- helpers -----

    def _has_fallback_source(self) -> bool:
        """Whether any stand-in could take over: a fallback sensor is
        configured, or the climate entity's own reading is allowed."""
        return self.fallback_temp_entity_id is not None or self.fallback_to_climate

    def _grace_elapsed(self, now_utc: datetime) -> bool:
        """Whether the configured sensor has been dark for the whole grace
        period -- the point from which a stand-in reading may be in use, and
        so the point from which changes in one are worth a refresh."""
        since = self._primary_unavailable_since
        return since is not None and (now_utc - since).total_seconds() >= FALLBACK_GRACE_S

    def _resolve_room_reading(
        self, primary: float | None, sensor_available: bool, now_utc: datetime
    ) -> tuple[float | None, str]:
        """Pick the reading that drives this refresh, and name where it came from.

        The configured sensor always wins while it reports. Once it has been
        dark for `FALLBACK_GRACE_S` -- continuously, measured from the first
        refresh that saw it dark -- the stand-ins are tried in order: the
        configured fallback sensor, then the climate entity's own
        `current_temperature` if `fallback_to_climate` allows it. Inside the
        grace window, and when no stand-in has a reading either, the answer is
        `(None, ROOM_SOURCE_NONE)`: exactly what every release before v0.18.0
        did for the whole outage.

        Also owns the grace timer. A dead sensor emits nothing, so nothing else
        is guaranteed to wake this zone at the end of the window; the timer is
        armed on the first dark refresh and cancelled the moment the sensor
        reports, or once it has fired and the window is over.
        """
        if sensor_available:
            self._primary_unavailable_since = None
            self._cancel_fallback_timer()
            return primary, ROOM_SOURCE_PRIMARY
        if self._primary_unavailable_since is None:
            self._primary_unavailable_since = now_utc
        elapsed_s = (now_utc - self._primary_unavailable_since).total_seconds()
        if elapsed_s < FALLBACK_GRACE_S:
            self._schedule_fallback_timer(FALLBACK_GRACE_S - elapsed_s)
            return None, ROOM_SOURCE_NONE
        self._cancel_fallback_timer()
        value = self._read_numeric_sensor(self.fallback_temp_entity_id)
        if value is not None:
            return value, ROOM_SOURCE_FALLBACK_SENSOR
        if self.fallback_to_climate:
            value = self._read_climate_current_temperature()
            if value is not None:
                return value, ROOM_SOURCE_CLIMATE
        return None, ROOM_SOURCE_NONE

    def _read_climate_current_temperature(self) -> float | None:
        """The climate entity's own `current_temperature`, or None when the
        entity is missing, unavailable, or the attribute is absent,
        non-numeric or non-finite -- the same acceptance rule as
        `_read_numeric_sensor`, applied to an attribute rather than a state.
        An unavailable entity publishes no state attributes at all, so the
        attribute read covers that case without a separate check.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state is None:
            return None
        raw = state.attributes.get("current_temperature")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return value

    @callback
    def _schedule_fallback_timer(self, delay_s: float) -> None:
        if self._unsub_fallback_timer is not None:
            return
        # A second past the boundary, so the refresh it triggers measures the
        # elapsed time as at least the grace period rather than a hair under.
        self._unsub_fallback_timer = async_call_later(
            self.hass, max(delay_s, 0.0) + 1.0, self._on_fallback_timer_fire
        )

    @callback
    def _cancel_fallback_timer(self) -> None:
        if self._unsub_fallback_timer is not None:
            self._unsub_fallback_timer()
            self._unsub_fallback_timer = None

    @callback
    def _on_fallback_timer_fire(self, _now: datetime) -> None:
        self._unsub_fallback_timer = None
        self.hass.async_create_task(self.async_request_refresh())

    def _log_room_source_edge(self, room_source: str, enabled: bool, now_utc: datetime) -> None:
        """Announce a stand-in taking over, a stand-in going dark, and the
        configured sensor taking back over.

        The same shape as the availability edge (`_may_log_sensor_edge`): the
        comparison is against what the log last *said* rather than against
        reality, so a throttled edge is re-offered on the next refresh -- and
        unlike a dead room sensor, a change in a stand-in does trigger one.
        The bound is one line per direction per `SENSOR_EDGE_LOG_INTERVAL_S`
        within an outage. Without it a climate entity that blinks unavailable
        while the room sensor is dark would re-announce the hand-over on every
        blink, unbounded. A switch between `fallback_sensor` and `climate`
        while engaged is a hand-over too and spends that direction's budget.
        The stamps reset when the configured sensor returns, so the first
        hand-over of every outage is never throttled.
        """
        if room_source == ROOM_SOURCE_PRIMARY:
            # The one thing said about an outage in which no stand-in engaged
            # is that none had a reading; a fresh outage gets to say it again.
            self._standin_missing_logged = False
            # Nothing to close and nothing else to reset unless a stand-in
            # engaged: the stamps and the latch are only ever set past that
            # point.
            if self._standin_last_engaged is None:
                return
            LOGGER.info(
                "%s: room sensor %s is back -- stand-in %s released",
                self.zone_name,
                self.temp_entity_id,
                self._standin_last_engaged,
            )
            self._standin_last_engaged = None
            self._standin_action_at_engage = None
            self._standin_flushed = False
            self._standin_logged_engaged = False
            self._standin_edge_logged_at = {True: None, False: None}
            return
        if room_source in ROOM_SOURCES_STAND_IN:
            self._standin_last_engaged = room_source
            if self._standin_logged_engaged and self._standin_logged_source == room_source:
                return
            if not self._may_log_standin_edge(True, now_utc):
                return
            self._standin_logged_engaged = True
            self._standin_logged_source = room_source
            since = self._primary_unavailable_since
            dark_min = (now_utc - since).total_seconds() / 60 if since is not None else 0.0
            standin = (
                self.fallback_temp_entity_id
                if room_source == ROOM_SOURCE_FALLBACK_SENSOR
                else f"{self.climate_entity_id} current_temperature"
            )
            log = LOGGER.warning if enabled else LOGGER.info
            log(
                "%s: room sensor %s has been unavailable for %.0f min -- controlling from "
                "%s (%s) until it reports again%s",
                self.zone_name,
                self.temp_entity_id,
                dark_min,
                standin,
                room_source,
                "" if enabled else " (zone is in shadow mode)",
            )
            return
        # ROOM_SOURCE_NONE: inside the grace window nothing was announced, so
        # there is nothing to retract. Otherwise the stand-in itself has gone
        # dark. Without this line the record's last word about control is
        # "controlling from ... until it reports again" while nothing is
        # controlling -- the silent night this release exists to prevent, one
        # layer down.
        if not self._standin_logged_engaged or not self._may_log_standin_edge(False, now_utc):
            return
        self._standin_logged_engaged = False
        previous = self._standin_logged_source
        standin = (
            self.fallback_temp_entity_id
            if previous == ROOM_SOURCE_FALLBACK_SENSOR
            else f"{self.climate_entity_id} current_temperature"
        )
        log = LOGGER.warning if enabled else LOGGER.info
        log(
            "%s: stand-in %s (%s) is unavailable too -- no control until it or "
            "room sensor %s reports%s",
            self.zone_name,
            standin,
            previous,
            self.temp_entity_id,
            "" if enabled else " (zone is in shadow mode)",
        )

    def _may_log_standin_edge(self, engaged: bool, now_utc: datetime) -> bool:
        """Throttle the stand-in edge lines to one per direction per interval:
        `_may_log_sensor_edge`'s shape, applied to the hand-over. The stamps
        are reset by `_log_room_source_edge` when the configured sensor
        returns, so this bounds flapping *within* an outage only."""
        last = self._standin_edge_logged_at[engaged]
        # `0 <=` because a backwards clock step -- an RTC-less Pi correcting
        # against NTP after boot -- makes the elapsed time negative, which would
        # otherwise satisfy the throttle and silence the log until the clock
        # caught up.
        if last is not None and 0 <= (now_utc - last).total_seconds() < SENSOR_EDGE_LOG_INTERVAL_S:
            return False
        self._standin_edge_logged_at[engaged] = now_utc
        return True

    async def _flush_samples_for_stand_in(self) -> None:
        """Empty the sample buffer once a stand-in has changed the action.

        Nothing is sampled from a stand-in, but the cycle it commands is real:
        the buffer simply has a gap where the heat or cool run was.
        `predictor._latest_run_of` joins runs by action label alone, so on
        hand-back the idle samples from before the outage and those after it
        would form one idle run spanning that cycle, and the idle slope would
        read as strong passive warming (or cooling) -- which a learning zone
        then acts on: an anticipatory cool inside the deadband, or passive
        drift accepted in place of a heat call. Flushing makes the hand-back
        a clean segment boundary, as a sensor swap is.

        Only when the stand-in commands an action other than the one in force
        when it engaged, though. A gap in which the action never changed is
        what every outage produced before v0.18.0 and joins nothing that was
        not already one run; and on a restart where the room sensor's own
        integration takes longer than the grace period to publish (Thread or
        Matter after a power cut, a deep-sleep ESPHome node) the stand-in
        usually idles for a minute or two and hands back. Flushing there would
        discard the buffer just restored from disk -- the bug class the
        manual-edit detector's startup guards exist to prevent. The caller
        keys on the committed `last_action`, so the flush lands on the first
        refresh after the change rather than on the engage refresh, and at
        the latest on the hand-back refresh.

        Unlike the manual-edit flush this keeps the persisted idle slope and
        its stamp: the passive rate was learned from the configured sensor
        and is still valid, and nothing refreshes it while a stand-in drives
        (`_resolve_idle_slope` is told not to persist). The command-state
        bookkeeping is untouched too -- what was commanded is not in question
        here.
        """
        LOGGER.info(
            "%s: sample buffer flushed -- what a stand-in commands is not sampled, so the "
            "runs on either side of the outage must not be joined",
            self.zone_name,
        )
        self._samples_cache = []
        # The first sample after hand-back writes immediately, as after every
        # flush (a flush is a forced segment boundary).
        self._last_sample_persist_at = None
        await self._store.async_update_zone(self.zone_name, samples=[])

    async def _resolve_idle_slope(
        self,
        slopes: ThermalSlopes,
        zone: StoredZone,
        now_utc: datetime,
        *,
        persist_ok: bool = True,
    ) -> tuple[ThermalSlopes, str, float | None]:
        """Apply the persisted-idle-slope policy (v0.12.0).

        The idle (passive heat-loss) rate changes slowly, so a recent value
        stays valid well beyond the 90-min sample window. Returns
        ``(effective_slopes, source, cached_age_min)``:

        - **Live idle slope present** -> remember it (throttled write) and
          return the slopes unchanged. ``source="live"``. Not remembered
          when ``persist_ok`` is False: while a stand-in drives control
          (v0.18.0) nothing is measured -- the buffer is frozen, so a live
          slope is the pre-outage one. Since v0.19.0 the stamp is the
          measurement time, so a frozen buffer can move it at most once, to
          the newest sample it already holds when a write the throttle held
          back catches up (see `_maybe_persist_idle_slope`). A stand-in writes
          nothing at all, so the value it hands back is exactly the one it
          was handed.
        - **Live idle slope absent** but a persisted one exists within
          ``PERSISTED_IDLE_SLOPE_MAX_AGE_MINUTES`` -> substitute it via
          ``dataclasses.replace`` (tagging ``method_idle="cached"``) so MPC
          stays ready through a heating chase. ``source="cached"``, with the
          age in minutes since the newest sample behind the value.
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
            if zone["learning_enabled"] and persist_ok:
                await self._maybe_persist_idle_slope(slopes, zone, now_utc)
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

        effective = replace(
            slopes, idle=persisted, method_idle="cached", idle_measured_at=persisted_at
        )
        # Clamp the reported age at 0 to absorb minor clock skew (a timestamp
        # written by a slightly-ahead clock would otherwise read negative).
        return effective, "cached", round(max(0.0, age_min), 1)

    async def _maybe_persist_idle_slope(
        self, slopes: ThermalSlopes, zone: StoredZone, now_utc: datetime
    ) -> None:
        """Persist a fresh live idle slope, throttled to bound flash wear.

        Stamped with when the drift was observed -- the newest sample behind
        the value (``ThermalSlopes.idle_measured_at``) -- so the cached value's
        age at the start of a heating chase is the time since the room was last
        seen idling. Until v0.19.0 the stamp was this refresh's time, and a
        slope stays live for as long as its run is in the buffer, which is not
        the same thing as being current: a run still in the window after the
        zone has moved on, and above all a buffer that has stopped growing, kept
        being re-stamped as new. That is what happened when a climate entity
        went unreachable -- every command dropped, so nothing was appended and
        nothing aged out -- and an idle slope measured an hour and a half
        earlier came back as the cache at "10 min old", good for another day.

        Nothing is written while the newest sample behind the value is the one
        already stored: nothing new has been measured, which is the frozen
        buffer again. Keyed on the stamp rather than the value, because the
        value of an unchanged run still moves -- in its last digits as the
        recency weights are recomputed against a later ``now``, and by more
        when the head of a run that has stopped growing is pruned -- and
        neither is a measurement. Otherwise at most once per
        ``SAMPLE_PERSIST_INTERVAL_S`` (mirroring the sample-buffer cadence); the
        stamp can lag the newest sample by that much, which errs towards
        expiring early. The first write after setup / a manual-edit flush
        (``_last_idle_slope_persist_at is None``) is immediate. The sample
        buffer's own writes are throttled separately, so after a restart the
        stored stamp can be up to one interval newer than the newest sample
        restored with the buffer, and the first write can move it back by that
        much -- early expiry again.
        """
        slope = slopes.idle
        measured_at = slopes.idle_measured_at
        if slope is None or measured_at is None:
            return
        stamp = measured_at.isoformat()
        if zone["persisted_idle_slope_at"] == stamp:
            return
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
            persisted_idle_slope_at=stamp,
        )
        # Advance the throttle only after the write lands (mirrors the
        # sample-persist path) so a failed write doesn't push the next attempt
        # out by a full interval.
        self._last_idle_slope_persist_at = now_utc

    async def _clear_persisted_idle_slope(self) -> None:
        """Null the persisted idle slope (stale-expiry path).

        The manual-edit and sensor-swap flushes clear it inline in their own
        store write to keep the flush atomic (the stand-in flush deliberately
        keeps it -- see `_flush_samples_for_stand_in`); this is the standalone
        expiry path.
        """
        self._last_idle_slope_persist_at = None
        await self._store.async_update_zone(
            self.zone_name,
            persisted_idle_slope=None,
            persisted_idle_slope_at=None,
        )

    def _may_log_command_warning(self, key: str) -> bool:
        """Throttle a command-path warning to one line per interval, per fault.

        All four faults repeat. A dropped command and a raising `set_hvac_mode`
        repeat hardest -- neither commits, so the same-mode gate never arms and
        a room-temp-driven zone re-enters on every refresh for the length of the
        fault. The setpoint and fan faults repeat once per applied action. Three
        of the four can be permanent: an entity advertising only a temperature
        *range* raises for a plain setpoint forever, a stored fan mode a unit
        advertises but refuses is retried on every apply, and from Home
        Assistant 2025.4 a mode the entity does not advertise raises rather than
        warning. Concretely: a unit that will not take a plain setpoint, or a
        stored fan mode it advertises and refuses, is a property of the hardware
        rather than a passing fault.

        Each key is cleared when a call of its own kind next succeeds, so a
        fault's return is announced rather than sitting inside a stale budget.
        `"mode"` is the exception, and deliberately: a clean return from
        `set_hvac_mode` is not proof of delivery, so it is cleared past the
        delivery check rather than on the return (see there).
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
        leaves the record's last word as "reporting again" for a while. A
        throttled edge is re-offered on the next refresh; a dead sensor emits
        no state changes and a zone with no schedule has no timer of its own,
        but from v0.18.0 the fallback grace timer wakes every zone once at the
        grace boundary -- `FALLBACK_GRACE_S` plus a second after the drop, and
        the two intervals are equal, so the budget is back by then -- and the
        withheld edge is logged there at the latest. The binary sensor is `on`
        throughout regardless.
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

        Returns [] when the entity is missing, exposes no `fan_modes`, or the
        attribute isn't a list — so the fan-boost command and the fan-mode
        selects fail closed (no command / unavailable select) on a climate that
        doesn't support fan control. v0.13.0.

        Not [] merely because the entity is unavailable: `fan_modes` is a
        *capability* attribute, and Home Assistant keeps those published while
        dropping the state attributes (`helpers/entity.py`). An unavailable unit
        therefore still advertises its fan modes, and the command that follows
        is dropped at dispatch rather than here.
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
            # Throttled for the same reason as the other command-path warnings,
            # and carrying its traceback for the same reason they all do: a
            # class name alone names neither the integration nor the line, and
            # the throttle is what makes the frames affordable. This catch is
            # the narrow one -- anything that is not a `HomeAssistantError`
            # goes to the wrapper in `_maybe_apply_action`, which shares this
            # budget.
            # This one can be permanent: the guard above only skips the call
            # when the unit already reports the mode we want, so a stored fan
            # mode it advertises and refuses is retried on every apply, forever.
            # The wrapper in `_maybe_apply_action` shares this key -- one fault,
            # one budget, whichever of the two sites catches it.
            if self._may_log_command_warning("fan"):
                LOGGER.warning(
                    "%s: climate.set_fan_mode(%s) failed: %s",
                    self.zone_name,
                    desired,
                    err,
                    exc_info=True,
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
        record_samples: bool = True,
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
        makes the unit start conditioning. All three calls are guarded, but not
        alike: a raise from the fan or setpoint call is warned about and changes
        nothing that was recorded, because they only refine a cycle that is
        already running, while a raise from the mode call leaves no cycle we
        can say started, so it records nothing and returns. Note the shape of
        that claim: a raise does *not* establish that the unit missed the
        command -- on the Home Assistant this integration declares a floor for,
        it usually took it -- only that we cannot claim it did. See there; that
        distinction is the whole difficulty, and it is also why nothing is
        recorded at all, including no sample.

        Appends a sample reflecting the action the HVAC is actually in for the
        next interval -- the newly-committed `decision.action` once
        `set_hvac_mode` has landed, or the prior `last_action` when a gate
        suppressed the re-issue. Nothing at all when the command did not reach
        the unit: an unreachable climate is not doing anything we can label.

        v0.18.0: `record_samples=False` while a stand-in reading is driving
        control. The commands still go out, but nothing is appended -- a
        sample from a sensor with a different offset and resolution would
        bias the slope estimator, which is exactly why the OptionsFlow flushes
        the buffer on a sensor swap. Implemented by blanking the room value
        the sampler sees, so every append site below skips as it does for an
        unavailable sensor.
        """
        now_utc = dt_util.utcnow()
        sample_room = decision_room if record_samples else None
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
            if sample_room is None or decision.target_mode is None:
                return
            await self._append_sample(sample_room, decision.action, now_utc)
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
            await self._append_sample(sample_room, last_action or ACTION_UNKNOWN, now_utc)
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
            await self._append_sample(sample_room, last_action or ACTION_UNKNOWN, now_utc)
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
        # Guarded, like the two calls below it, but the bookkeeping differs
        # because this is the call the commitment rests on. A raise used to
        # escape into the fire-and-forget apply task: no log naming the zone,
        # nothing committed, and -- because the echo-window stamp written just
        # above was not rolled back while the fault re-entered on every refresh
        # -- a window that never closed, so a wall edit made during the fault
        # was absorbed as an echo instead of compared. (An edit arriving while
        # the call is still in flight is absorbed either way: the stamp is
        # written before the await and nothing here runs until the call
        # returns. That is unchanged, and a hung cloud call is exactly when
        # somebody is at the wall -- but closing it means reasoning about a
        # window that has not been handed back yet.)
        #
        # What a raise actually tells us is narrow: the call did not complete.
        # It does not say the unit never got it, and on Home Assistant 2024.12
        # -- the floor this integration declares -- it usually did.
        # `_valid_mode_or_raise` still only warns
        # for an unadvertised hvac mode until 2025.4, so what reaches this
        # `except` today is mostly a platform or cloud error -- the class where
        # the command lands and only the confirmation is lost. Not always: a
        # `ServiceNotFound` from a climate integration that failed to load
        # definitely did not land. There is no way to tell them apart here,
        # which is the whole difficulty.
        #
        # So this catches, says so, and records nothing: no commitment, no store
        # write, and -- the half the docstring above points here for -- no
        # sample, because one appended here would label the interval with an
        # action the unit was never put into, and the slope estimator would
        # learn the wrong room from it. In particular it leaves
        # the echo-window stamp written above exactly as an un-guarded raise
        # left it. That is not because it is right -- it means the window is
        # re-armed by every retry and so never closes for the length of a fault,
        # and a wall edit made during one is absorbed as an echo instead of
        # compared. It is because every alternative was measured worse. Six
        # ways of acting on the guess have been tried: assume delivered; assume
        # not; infer delivery from `State` identity across the call; vouch for
        # the attempted mode; hand the stamp back on every failing apply; hand
        # it back on every apply after the first of a run. The last two closed
        # the window while a unit that had taken the mode was still publishing
        # it, and flushed the learned model at up to 57 an hour against none
        # before the guard existed; the first two swallowed wall edits or
        # discarded the vouch of a command that had actually landed.
        #
        # The reason none of them works is that the window is the wrong
        # instrument: it answers "was anything commanded recently", and what is
        # needed here is "is this observation the unit acknowledging something
        # we asked for". `_commanded_state` is that instrument, and making it
        # hold more than one value per field -- with an expiry, and without
        # displacing what a landed command put there -- is a change to the
        # detector rather than to this `except`. It is left for one.
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.climate_entity_id, "hvac_mode": decision.target_mode},
                blocking=True,
            )
        except Exception as err:
            if self._may_log_command_warning("mode"):
                LOGGER.warning(
                    "%s: could not command %s to %s (%s: %s) -- not recording it; will retry",
                    self.zone_name,
                    self.climate_entity_id,
                    decision.target_mode,
                    type(err).__name__,
                    err,
                    exc_info=True,
                )
            return

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
            # v0.16.0 caught it every time.
            #
            # Restored rather than cleared. The window belongs to whichever
            # command last actually issued, and an apply that delivered nothing
            # does not invalidate one still open from a command that did --
            # clearing it would make *that* command's echo read as a hand edit.
            # It expires on its own schedule either way; all this stops is the
            # ratcheting.
            #
            # The mode-raise path above does neither, deliberately: see there.
            # This one can restore because a dropped command is known not to
            # have reached the unit, which is exactly what a raise does not
            # tell us.
            if self._still_the_current_apply(now_utc):
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

        # Both budgets, and only here: a clean return is not proof of delivery,
        # so clearing the mode budget on it let a command dropped at dispatch
        # end a fault the unit never heard about -- and a bridge alternating
        # between raising and unreachable then warned on every apply rather than
        # once per interval.
        self._command_warn_logged_at.pop("mode", None)
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
                    exc_info=True,
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
                        exc_info=True,
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
        # `unavailable` / `unknown` are not states to record. A just-commanded
        # unit reading unreachable for a moment is ordinary, and either as a
        # baseline matches nothing the unit will ever report -- so when it
        # recovers to the mode it was really in, with nothing commanded to cover
        # for it (after a flush, or in shadow mode), that reads as a hand edit.
        # Leave the previous baseline standing instead: it still describes the
        # last state the unit was really in. The listener declines to *compare*
        # these for the same reason, though not identically -- it has a live
        # setpoint to judge under `unknown`, and here there is a command in
        # flight that makes the whole reading momentary.
        if fresh is not None and fresh.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # A setpoint it is not reporting is carried forward here for the
            # same reason the listener carries one: it is an absence of
            # information, not a value. Storing the `None` instead would put a
            # baseline in place that the listener's own rule can no longer
            # rescue, because the next transition is judged against it -- so a
            # unit that varies `TARGET_TEMPERATURE` by mode flushed once per
            # heat/idle cycle on the way back out of `fan_only`.
            snapshot_temp = fresh.attributes.get("temperature")
            if snapshot_temp is None and self._last_command_state is not None:
                snapshot_temp = self._last_command_state["target_temp"]
            self._last_command_state = {
                "hvac_mode": fresh.state,
                "target_temp": snapshot_temp,
            }
        elif self._last_command_state is None:
            # Nothing to leave standing. Our intent is a poor baseline, but a
            # missing one makes the listener adopt whatever arrives first, and
            # for an entity that is merely slow to appear that is worse. The
            # setpoint is what actually landed, by the same rule as the
            # commanded side below: a `set_temperature` that raised never
            # reached the unit, and offering its value here would let a wall
            # edit that happens to land on it pass unnoticed.
            self._last_command_state = {
                "hvac_mode": decision.target_mode,
                "target_temp": setpoint_applied,
            }
        # Only what actually landed: a raised `set_temperature` never reached the
        # unit, so its value must not be treated as something the unit may
        # report. The key is left absent rather than set to None for the same
        # reason -- we have no opinion to offer, not an opinion that the unit
        # reports nothing. The listener never sees a None here either way, since
        # it carries an unreported setpoint forward before comparing, so the two
        # forms are indistinguishable in behaviour; absent is the honest one.
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
        await self._append_sample(sample_room, decision.action, now_utc)

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

    def _still_the_current_apply(self, mine: datetime) -> bool:
        """True while `mine`'s apply is the last one to have stamped the window.

        Applies are dispatched with `hass.async_create_task`, so two of them
        overlap whenever a climate call outlives the next refresh -- ordinary
        for a cloud unit, and nothing spaces the retries because nothing
        commits. `previous` was read before this apply's own await, so a
        superseded apply writing it back would hand away a stamp a later apply
        set after landing a real command, and that command's echo would then
        read as a hand edit.

        Only the dropped-command path calls this today, because the mode-raise
        path above deliberately touches nothing and so has nothing to own. That
        is not a claim the rest of the apply is covered: the success tail writes
        the store, `_commanded_state` and a sample with no ownership check at
        all, so a superseded apply still clobbers those three. It does that on
        `main` too, which is why it is filed as its own work rather than widened
        here on the way past.

        Identity rather than equality, because ownership is the question:
        `self._last_command_at = now_utc` stores this very object. Two applies
        entering in the same tick -- ordinary under a frozen clock, and not
        constructible in production -- hold equal stamps belonging to different
        applies, and `==` cannot tell them apart.
        """
        return self._last_command_at is mine

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
        so a second edit is judged against the occupant's own state alone. The
        first of those bounds is weaker than it sounds for a unit that coerces
        the setpoint *permanently* -- it never agrees, so its accepted pair
        stands until the next command or flush rather than for a lag window.

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
        # v0.18.0: once the configured room sensor has been dark for the grace
        # period, the climate entity's own `current_temperature` may be the
        # reading in use -- and a dark sensor emits nothing, so this is the
        # only event that tracks the room. Not before: inside the grace window
        # the zone must behave exactly as it did without a stand-in, and the
        # grace timer's refresh engages one. Ahead of the manual-edit logic,
        # which is about the setpoint and mode and does not care about this
        # attribute. Filtered to changes of that attribute only: the unit's
        # echo of our own command changes mode and setpoint, not the reading,
        # so this does not refresh on every command.
        if self.fallback_to_climate and self._grace_elapsed(dt_util.utcnow()):
            old_temp = old_state.attributes.get("current_temperature") if old_state else None
            new_temp = new_state.attributes.get("current_temperature") if new_state else None
            if old_temp != new_temp:
                self._schedule_debounced_refresh()
        if old_state is None or new_state is None:
            # Initial state-added or entity-removed -- not a manual edit.
            return
        if new_state.state == STATE_UNAVAILABLE:
            # A unit dropping off the network is not somebody at the wall, and
            # an unavailable entity publishes no *state* attributes either
            # (`helpers/entity.py` adds those only when `available`; the
            # capability ones survive), so there is no setpoint here to compare
            # -- `{unavailable, None}` matched
            # nothing and flushed the learned model on every bridge blip.
            # Ignoring it leaves the baseline describing the last state the unit
            # was really in, which is what the reconnect gets judged against: if
            # it comes back as we left it there is nothing to flush, and if
            # somebody changed it meanwhile that is caught then.
            return
        observed = {
            "hvac_mode": new_state.state,
            "target_temp": new_state.attributes.get("temperature"),
        }
        # Snapshotted before either carry below, for the flush diagnostic. Both
        # of them substitute a value the entity did not send, and naming one as
        # if it had is no help to whoever is reading the line -- an entity
        # publishing no mode is worth seeing as `unknown` there, because that is
        # the symptom to chase.
        reported = dict(observed)
        carried = self._last_command_state
        if new_state.state == STATE_UNKNOWN:
            # `unknown` is not the same thing as `unavailable`, though it is
            # easy to treat it as one. Home Assistant writes it whenever
            # `ClimateEntity.hvac_mode` is None -- an MQTT climate that is
            # reachable again but has not had its mode topic yet -- and such an
            # entity is *available*, so it goes on publishing its attributes.
            # The mode is uncomparable; the setpoint is live and is exactly
            # where a wall edit would show. So carry the last known mode
            # forward, which makes that field say nothing, and let the setpoint
            # be judged. Skipping the whole observation instead was measured
            # swallowing three consecutive dial turns.
            if carried is None:
                # Nothing to carry forward and nothing to compare against.
                # Adopting `unknown` as the baseline instead would leave it
                # holding a value the unit can never report again, so its first
                # real state -- the mode topic arriving after a restart -- would
                # read as a hand edit and flush the buffer just restored from
                # disk.
                return
            observed["hvac_mode"] = carried["hvac_mode"]
        # A setpoint the entity is not reporting is an absence of information,
        # not a value, so it says nothing rather than reading as somebody having
        # cleared the dial. A physical remote always sets a number, and no dial
        # position unsets a target.
        #
        # One service call does, and it is worth naming because the rule cannot
        # see it: `SET_TEMPERATURE_SCHEMA` is `has_at_least_one_key`, so
        # `climate.set_temperature` with only `target_temp_low`/`_high` is legal
        # on an entity advertising both features, and nulls `target_temperature`
        # while leaving the mode alone. Somebody moving such a unit from a
        # single target to a 24-28 band is accepted here, and a preset that
        # switches the entity to a range target reaches the same state from a
        # wall button rather than a service call. Every core integration I could
        # check gates that on `heat_cool`, so the mode moves too and the edit is
        # still caught; on one that does not, this is a miss. The rule accepts
        # any absent target under an expected mode -- that is the mechanism, and
        # the routes to it are only examples.
        #
        # What `None` usually means is that the platform has no target *right
        # now*:
        # `state_attributes` publishes `temperature` only while
        # `TARGET_TEMPERATURE` is in `supported_features`, and several
        # integrations vary that by mode, so a unit reaching `fan_only` -- which
        # this integration commands on every idle release -- simply stops
        # carrying one. Judging that as an edit flushed the learned model on
        # exactly the release we asked for. The mode is still compared, and a
        # regime change always shows there.
        if observed["target_temp"] is None and carried is not None:
            observed["target_temp"] = carried["target_temp"]
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
            # What the entity actually reported, both fields raw. `observed`
            # may carry a mode or a setpoint substituted for one it did not send
            # (see above), and naming a value nothing published is no help to
            # whoever is reading this.
            reported,
            # The baseline -- what the comparison was actually made against, so
            # it is the right thing to print even though its `target_temp` may
            # itself be a value carried from an earlier mode rather than one the
            # entity published alongside this one. Read it as "what we compared
            # to", not as a second observation.
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
