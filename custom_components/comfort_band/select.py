"""Select entities.

Two kinds, dispatched on `entry.data["kind"]`:
  - `profile_manager` (singleton): `select.active_profile` — reads
    `ProfileRegistry.names` for options and `.active` for the value; selecting
    flips the active profile and fires the dispatcher signal every zone
    coordinator listens on.
  - `zone` (per-zone, v0.13.0): `select.{zone}_active_fan_mode` /
    `_idle_fan_mode` — options come live from the zone climate's `fan_modes`;
    the stored string is the value; selecting persists it for the fan-boost.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_KIND, DOMAIN, ENTRY_KIND_PROFILE_MANAGER, ENTRY_KIND_ZONE
from .coordinator import ZoneCoordinator
from .entity import ComfortBandProfileEntity, ComfortBandZoneEntity

if TYPE_CHECKING:
    from .profiles import ProfileRegistry


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
        return self.coordinator._climate_fan_modes()

    @property
    def available(self) -> bool:
        # Unavailable (greyed out) when the coordinator is failing OR the
        # climate exposes no fan modes (offline / fanless unit) — honest UI,
        # and it stops HA warning about a stored value not in an empty option
        # list.
        return super().available and bool(self.options)

    @property
    def current_option(self) -> str | None:
        # Show the stored mode only when it's a currently-valid option. A stale
        # stored value (unit re-labelled its modes) renders blank rather than
        # tripping HA's "current_option not in options" warning; storage is
        # left intact (read-side coercion only) and the coordinator's own
        # membership guard independently refuses to command a dead string.
        # `self._field` is a runtime str, so the StoredZone TypedDict can't be
        # indexed by it directly (mypy literal-key rule) — cast as the storage
        # module does for the same reason.
        zone = cast("dict[str, Any]", self.coordinator.data.zone)
        stored = zone[self._field]
        return stored if stored in self.options else None

    async def async_select_option(self, option: str) -> None:
        # HA only ever passes a value currently in `options`.
        await self._setter(option)


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
            ]
        )
