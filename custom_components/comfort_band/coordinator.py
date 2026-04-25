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

from . import hysteresis, schedule
from .const import (
    ACTION_UNKNOWN,
    DEFAULT_PROFILE,
    LOGGER,
    SIGNAL_ACTIVE_PROFILE_CHANGED,
)
from .hysteresis import HysteresisDecision, HysteresisInputs
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
    """

    zone: StoredZone
    room: float | None
    sensor_available: bool
    effective_low: float
    effective_high: float
    sched_low: float
    sched_high: float
    override_active: bool
    override_until: datetime | None
    decision: HysteresisDecision

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
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_signal: CALLBACK_TYPE | None = None
        self._unsub_debounce: CALLBACK_TYPE | None = None
        self._unsub_override_timer: CALLBACK_TYPE | None = None
        self._unsub_transition_timer: CALLBACK_TYPE | None = None

    # ----- setup / teardown -----

    async def async_setup(self) -> None:
        """Wire event-driven triggers and run the first refresh."""
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.temp_entity_id], self._on_temp_change
        )
        self._unsub_signal = async_dispatcher_connect(
            self.hass, SIGNAL_ACTIVE_PROFILE_CHANGED, self._on_profile_change
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
        ):
            if unsub is not None:
                unsub()
        self._unsub_state = None
        self._unsub_signal = None
        self._unsub_debounce = None
        self._unsub_override_timer = None
        self._unsub_transition_timer = None

    # ----- mutators (called from entities + services) -----

    def get_zone_data(self) -> StoredZone:
        """Snapshot of the persisted zone (deep copy). Cheap; KB-sized."""
        return self._store.get_zone(self.zone_name)

    async def async_set_param(self, field: str, value: Any) -> None:
        """Update a tunable (deadband_*, override_hours, min_cycle_minutes)
        without triggering an override.
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
        await self.async_request_refresh()

    async def async_cancel_override(self) -> None:
        await self._store.async_update_zone(self.zone_name, override_until=None)
        await self.async_request_refresh()

    async def async_set_enabled(self, enabled: bool) -> None:
        await self._store.async_update_zone(self.zone_name, enabled=enabled)
        await self.async_request_refresh()

    async def _set_manual_and_override(self, **manual_fields: float) -> None:
        zone = self._store.get_zone(self.zone_name)
        until = (dt_util.utcnow() + timedelta(hours=zone["override_hours"])).isoformat()
        await self._store.async_update_zone(self.zone_name, override_until=until, **manual_fields)
        await self.async_request_refresh()

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

        # Re-validate override.
        now_utc = dt_util.utcnow()
        override_until = _parse_iso(zone["override_until"])
        override_active = override_until is not None and now_utc < override_until
        if override_until is not None and not override_active:
            await self._store.async_update_zone(self.zone_name, override_until=None)
            zone = self._store.get_zone(self.zone_name)
            override_until = None

        # Resolve scheduled band (falls back to manual band if no schedule).
        schedule_data = zone["schedules"].get(active_profile) or zone["schedules"].get(
            DEFAULT_PROFILE
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

        decision = hysteresis.decide(
            HysteresisInputs(
                room=room,
                low=eff_low,
                high=eff_high,
                deadband_below=zone["deadband_below"],
                deadband_above=zone["deadband_above"],
                current_action=zone["last_action"] or ACTION_UNKNOWN,
            )
        )

        # Reschedule next-transition + override-expiry timers.
        self._schedule_next_transition(schedule_data)
        if override_until is not None and override_active:
            self._schedule_override_expiry(override_until - now_utc)

        state = ZoneState(
            zone=zone,
            room=room,
            sensor_available=sensor_available,
            effective_low=eff_low,
            effective_high=eff_high,
            sched_low=sched_low,
            sched_high=sched_high,
            override_active=override_active,
            override_until=override_until,
            decision=decision,
        )

        # Apply in a follow-up task so this refresh returns immediately --
        # entities can render the new state without waiting on climate calls.
        self.hass.async_create_task(self._maybe_apply_action(decision, zone["enabled"]))

        return state

    # ----- helpers -----

    def _read_room_temp(self) -> tuple[float | None, bool]:
        state = self.hass.states.get(self.temp_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None):
            return None, False
        try:
            return float(state.state), True
        except (TypeError, ValueError):
            return None, False

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

    async def _maybe_apply_action(self, decision: HysteresisDecision, enabled: bool) -> None:
        """Translate the decision into climate.set_hvac_mode + set_temperature.

        Skipped entirely when `enabled=False` (shadow mode -- log only).
        Min-cycle suppression filters re-issue of the *same* action; mode
        flips fire immediately so heat+cool can't deadlock.
        """
        if not enabled:
            LOGGER.debug(
                "%s: shadow mode -- would %s (target_mode=%s, target_temp=%s)",
                self.zone_name,
                decision.action,
                decision.target_mode,
                decision.target_temp,
            )
            return
        if decision.target_mode is None:
            return

        zone = self._store.get_zone(self.zone_name)
        last_action = zone["last_action"]
        last_action_at = _parse_iso(zone["last_action_at"])
        now_utc = dt_util.utcnow()

        if (
            last_action == decision.action
            and last_action_at is not None
            and (now_utc - last_action_at).total_seconds() < zone["min_cycle_minutes"] * 60
        ):
            return

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
        await self._store.async_update_zone(
            self.zone_name,
            last_action=decision.action,
            last_action_at=now_utc.isoformat(),
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
