"""Profile registry — thin wrapper around the store that fires a dispatcher
signal whenever the active profile changes, so every zone coordinator can
react in lockstep without polling.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DEFAULT_PROFILE, SIGNAL_ACTIVE_PROFILE_CHANGED
from .storage import ComfortBandStore


class ProfileRegistry:
    """Reads and mutates the profile slice of the store."""

    def __init__(self, hass: HomeAssistant, store: ComfortBandStore) -> None:
        self._hass = hass
        self._store = store

    @property
    def names(self) -> list[str]:
        return self._store.list_profiles()

    @property
    def active(self) -> str:
        return self._store.active_profile

    async def async_set_active(self, name: str) -> None:
        if name == self.active:
            return
        await self._store.async_set_active_profile(name)
        async_dispatcher_send(self._hass, SIGNAL_ACTIVE_PROFILE_CHANGED, name)

    async def async_create(self, name: str, description: str = "") -> None:
        await self._store.async_add_profile(name, description)

    async def async_delete(self, name: str) -> None:
        if name == DEFAULT_PROFILE:
            raise ValueError(f"Cannot delete the default profile {DEFAULT_PROFILE!r}")
        was_active = self.active == name
        await self._store.async_remove_profile(name)
        if was_active:
            async_dispatcher_send(self._hass, SIGNAL_ACTIVE_PROFILE_CHANGED, DEFAULT_PROFILE)
