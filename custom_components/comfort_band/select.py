"""Select entities.

Two kinds, dispatched on `entry.data["kind"]`:
  - `profile_manager` (singleton): `select.active_profile` — reads
    `ProfileRegistry.names` for options and `.active` for the value; selecting
    flips the active profile and fires the dispatcher signal every zone
    coordinator listens on.
  - `zone` (per-zone, v0.13.0): `select.{zone}_active_fan_mode` /
    `_idle_fan_mode` — options come live from the zone climate's `fan_modes`;
    the stored string is the value; selecting persists it for the fan-boost.
  - `zone` (per-zone, v0.14.0): `select.{zone}_schedule_assignment` — pick a
    shared schedule (or "Own schedule") to point this zone's band at.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_KIND,
    DOMAIN,
    ENTRY_KIND_PROFILE_MANAGER,
    ENTRY_KIND_ZONE,
    OWN_SCHEDULE_LABEL,
    SIGNAL_SHARED_SCHEDULE_LIST_CHANGED,
)
from .coordinator import ZoneCoordinator
from .entity import ComfortBandProfileEntity, ComfortBandZoneEntity

if TYPE_CHECKING:
    from .profiles import ProfileRegistry
    from .shared_schedules import SharedScheduleRegistry

# Sentinel option on the fan-mode selects meaning "don't command this side"
# (stored as None). Lets a user stop boosting one side from the UI — e.g. keep
# the quiet idle fan but drop the active one — without disabling the whole
# switch. Collision with a real fan mode named "(none)" is implausible.
_FAN_MODE_NONE = "(none)"

# Sentinel option on the schedule-assignment select meaning "use this zone's
# own schedule" (stored `schedule_id` = None). Reserved as a name (see const).
_OWN_SCHEDULE = OWN_SCHEDULE_LABEL


class ActiveProfileSelect(ComfortBandProfileEntity, SelectEntity):
    def __init__(self) -> None:
        super().__init__("active_profile")

    @property
    def options(self) -> list[str]:
        return self._registry().names

    @property
    def current_option(self) -> str | None:
        return self._registry().active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Surface the rename-aware default and per-profile descriptions so the
        # card can render them without a second WS round-trip. Pushed
        # alongside `options` and `state` every time the entity writes state.
        registry = self._registry()
        return {
            "default_profile": registry.default,
            "descriptions": {name: registry.description(name) for name in registry.names},
        }

    async def async_select_option(self, option: str) -> None:
        await self._registry().async_set_active(option)

    def _registry(self) -> ProfileRegistry:
        return self.hass.data[DOMAIN].profile_registry  # type: ignore[no-any-return]


class FanModeSelect(ComfortBandZoneEntity, SelectEntity):
    """Per-zone fan-mode picker for the v0.13.0 deterministic fan-boost.

    `options` are read live from the climate's `fan_modes` attribute, so the
    dropdown always reflects what the unit actually supports (no hard-coded
    list). The selected string is stored verbatim and later matched against the
    live `fan_modes` before being commanded — robust to a unit re-ordering or
    re-labelling its modes.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ZoneCoordinator,
        key: str,
        setter: Callable[[str | None], Awaitable[None]],
    ) -> None:
        super().__init__(coordinator, key)
        self._field = key
        self._setter = setter

    @property
    def options(self) -> list[str]:
        # Prepend the "(none)" sentinel so the user can clear this side from the
        # UI. Only when the unit actually has fan modes — an empty list keeps
        # the select `available=False` (see below) rather than offering a lone
        # "(none)" on a fanless climate.
        modes = self.coordinator._climate_fan_modes()
        return [_FAN_MODE_NONE, *modes] if modes else []

    @property
    def available(self) -> bool:
        # Unavailable (greyed out) when the coordinator is failing OR the
        # climate exposes no fan modes (offline / fanless unit) — honest UI.
        # Gate on the raw fan modes, not `self.options` (which always carries
        # the sentinel once there's at least one real mode).
        return super().available and bool(self.coordinator._climate_fan_modes())

    @property
    def current_option(self) -> str | None:
        # `self._field` is a runtime str, so the StoredZone TypedDict can't be
        # indexed by it directly (mypy literal-key rule) — cast as the storage
        # module does for the same reason.
        zone = cast("dict[str, Any]", self.coordinator.data.zone)
        stored = zone[self._field]
        if stored is None:
            # Unset -> show the sentinel (which is in `options`, so HA doesn't
            # warn about current_option not being a valid option).
            return _FAN_MODE_NONE
        # A stored mode the unit no longer offers (re-labelled) renders blank
        # rather than tripping HA's "current_option not in options" warning;
        # storage is left intact (read-side coercion only) and the coordinator's
        # own membership guard independently refuses to command a dead string.
        return stored if stored in self.coordinator._climate_fan_modes() else None

    async def async_select_option(self, option: str) -> None:
        # HA only ever passes a value currently in `options` (incl. the
        # sentinel, which maps back to None = don't command this side).
        await self._setter(None if option == _FAN_MODE_NONE else option)


class ScheduleAssignmentSelect(ComfortBandZoneEntity, SelectEntity):
    """Per-zone picker for the v0.14.0 shared-schedule assignment.

    `options` = the leading "Own schedule" sentinel + every shared schedule's
    name. Selecting a shared name points this zone's `schedule_id` at it (so it
    resolves the shared band); "Own schedule" clears it back to None. Exposes
    `schedule_id` + a `shared_schedules` catalogue (`[{id, name, members}]`) as
    attributes so the card can read everything from `hass.states`.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ZoneCoordinator) -> None:
        super().__init__(coordinator, "schedule_assignment")

    def _registry(self) -> SharedScheduleRegistry:
        return self.hass.data[DOMAIN].shared_schedule_registry  # type: ignore[no-any-return]

    @property
    def options(self) -> list[str]:
        return [_OWN_SCHEDULE, *self._registry().names]

    @property
    def current_option(self) -> str | None:
        sid = self.coordinator.data.zone["schedule_id"]
        if sid is None:
            return _OWN_SCHEDULE
        # A dangling id (shared schedule deleted) resolves to the own schedule,
        # so show "Own schedule" rather than a stale name. Read-side coercion.
        name = self._registry().name_for(sid)
        return name if name is not None else _OWN_SCHEDULE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sid = self.coordinator.data.zone["schedule_id"]
        registry = self._registry()
        return {
            # None on "Own schedule" or a dangling id — lets the card build a
            # stable id-keyed schedule ref without name->id guessing.
            "schedule_id": sid if (sid is not None and registry.has(sid)) else None,
            "shared_schedules": registry.summaries(),
        }

    async def async_select_option(self, option: str) -> None:
        if option == _OWN_SCHEDULE:
            await self.coordinator.async_set_schedule_assignment(None)
            return
        shared_id = self._registry().id_for(option)
        # HA only passes an in-`options` value, but the schedule could have been
        # renamed/deleted between render and click — no-op rather than raise.
        if shared_id is not None:
            await self.coordinator.async_set_schedule_assignment(shared_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Re-render options/attributes on shared-schedule create/rename/delete
        # (a coordinator refresh only covers this zone's own assignment, not the
        # global list).
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED, self._handle_list_changed
            )
        )

    @callback
    def _handle_list_changed(self) -> None:
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    kind = entry.data.get(CONF_KIND)
    if kind == ENTRY_KIND_PROFILE_MANAGER:
        async_add_entities([ActiveProfileSelect()])
        return
    if kind == ENTRY_KIND_ZONE:
        coordinator: ZoneCoordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
        async_add_entities(
            [
                FanModeSelect(
                    coordinator, "active_fan_mode", coordinator.async_set_active_fan_mode
                ),
                FanModeSelect(coordinator, "idle_fan_mode", coordinator.async_set_idle_fan_mode),
                ScheduleAssignmentSelect(coordinator),
            ]
        )
