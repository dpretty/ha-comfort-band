"""Profile registry — thin wrapper around the store that fires dispatcher
signals on profile mutations so every zone coordinator and the singleton
select entity can react without polling.

Two signals:
- `SIGNAL_ACTIVE_PROFILE_CHANGED(name)` — fires when the active profile
  changes (including indirectly, e.g. when the active is deleted or renamed).
- `SIGNAL_PROFILE_LIST_CHANGED` — fires on every list mutation (create,
  clone, rename, delete) so the select entity re-pushes its `options`.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_ACTIVE_PROFILE_CHANGED, SIGNAL_PROFILE_LIST_CHANGED
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

    @property
    def default(self) -> str:
        return self._store.default_profile

    def description(self, name: str) -> str:
        return self._store.get_profile(name)["description"]

    async def async_set_active(self, name: str) -> None:
        if name == self.active:
            return
        await self._store.async_set_active_profile(name)
        async_dispatcher_send(self._hass, SIGNAL_ACTIVE_PROFILE_CHANGED, name)

    async def async_create(self, name: str, description: str = "") -> None:
        await self._store.async_add_profile(name, description)
        async_dispatcher_send(self._hass, SIGNAL_PROFILE_LIST_CHANGED)

    async def async_clone(self, source: str, target: str, description: str = "") -> None:
        await self._store.async_clone_profile(source, target, description)
        async_dispatcher_send(self._hass, SIGNAL_PROFILE_LIST_CHANGED)

    async def async_rename(self, old: str, new: str) -> None:
        if old == new:
            return
        was_active = self.active == old
        await self._store.async_rename_profile(old, new)
        async_dispatcher_send(self._hass, SIGNAL_PROFILE_LIST_CHANGED)
        if was_active:
            async_dispatcher_send(self._hass, SIGNAL_ACTIVE_PROFILE_CHANGED, new)

    async def async_delete(self, name: str) -> None:
        if name == self.default:
            raise ValueError(f"Cannot delete the default profile {name!r}")
        was_active = self.active == name
        await self._store.async_remove_profile(name)
        async_dispatcher_send(self._hass, SIGNAL_PROFILE_LIST_CHANGED)
        if was_active:
            async_dispatcher_send(self._hass, SIGNAL_ACTIVE_PROFILE_CHANGED, self.default)
