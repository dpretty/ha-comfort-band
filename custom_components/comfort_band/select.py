"""Global select entity for the active profile.

One per HA install (singleton). Reads `ProfileRegistry.names` for options
and `ProfileRegistry.active` for the current value. Selecting an option
flips the active profile, which fires the dispatcher signal that every
zone coordinator listens on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import ComfortBandProfileEntity

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

    async def async_select_option(self, option: str) -> None:
        await self._registry().async_set_active(option)

    def _registry(self) -> ProfileRegistry:
        return self.hass.data[DOMAIN].profile_registry  # type: ignore[no-any-return]


async def async_setup_entry(
    _hass: HomeAssistant,
    _entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ActiveProfileSelect()])
