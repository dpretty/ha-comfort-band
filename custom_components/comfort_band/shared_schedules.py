"""Shared-schedule registry — thin wrapper around the store that fires
dispatcher signals on shared-schedule mutations, so the per-zone assignment
selects re-render their options and (for content edits) the websocket layer
pushes to every subscriber. Mirrors `profiles.py`.

Two signals (see `const.py`):
- `SIGNAL_SHARED_SCHEDULE_LIST_CHANGED` — fires on create / rename / delete so
  every `select.{zone}_schedule_assignment` re-pushes its `options`.
- `SIGNAL_SHARED_SCHEDULE_CHANGED(shared_id, profile, schedule)` — fired by the
  *store* on a content edit so the WS layer pushes to id-subscribers. (Not
  fired here; this registry only orchestrates the list-level mutations.)
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_SHARED_SCHEDULE_LIST_CHANGED
from .storage import ComfortBandStore


class SharedScheduleRegistry:
    """Reads and mutates the shared-schedule slice of the store."""

    def __init__(self, hass: HomeAssistant, store: ComfortBandStore) -> None:
        self._hass = hass
        self._store = store

    @property
    def ids(self) -> list[str]:
        return self._store.list_shared_schedule_ids()

    def has(self, shared_id: str) -> bool:
        return self._store.has_shared_schedule(shared_id)

    def name_for(self, shared_id: str) -> str | None:
        return self._store.get_shared_schedule_name(shared_id)

    def id_for(self, name: str) -> str | None:
        """Resolve a (case-insensitive) display name to its stable id, or None."""
        lowered = name.casefold()
        for summary in self._store.shared_schedule_summaries():
            if str(summary["name"]).casefold() == lowered:
                return str(summary["id"])
        return None

    def members(self, shared_id: str) -> list[str]:
        return self._store.zones_using_shared_schedule(shared_id)

    def summaries(self) -> list[dict[str, Any]]:
        """`[{id, name, members}]` sorted by name (for the select attributes)."""
        return self._store.shared_schedule_summaries()

    @property
    def names(self) -> list[str]:
        return [str(s["name"]) for s in self._store.shared_schedule_summaries()]

    async def async_create(self, name: str, seed_from_zone: str | None = None) -> str:
        """Create a shared schedule (optionally seeded by deep-copying a zone's
        own schedules across all profiles). Returns the new id."""
        if seed_from_zone is not None and not self._store.has_zone(seed_from_zone):
            raise ValueError(f"Unknown zone: {seed_from_zone}")
        seed = self._store.get_zone(seed_from_zone)["schedules"] if seed_from_zone else None
        shared_id = await self._store.async_add_shared_schedule(name, seed)
        async_dispatcher_send(self._hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED)
        return shared_id

    async def async_rename(self, shared_id: str, new_name: str) -> None:
        await self._store.async_rename_shared_schedule(shared_id, new_name)
        async_dispatcher_send(self._hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED)

    async def async_delete(self, shared_id: str, cascade: bool = False) -> list[str]:
        """Delete a shared schedule. Refuses (ValueError) if any zone is assigned
        to it unless `cascade` is True, in which case those zones are unassigned
        (back to "Own schedule"). Returns the affected zone names so the caller
        can refresh their coordinators."""
        if not self._store.has_shared_schedule(shared_id):
            raise KeyError(shared_id)
        users = self._store.zones_using_shared_schedule(shared_id)
        if users and not cascade:
            raise ValueError(
                f"Shared schedule is assigned to {len(users)} zone(s): "
                f"{', '.join(users)}. Pass cascade=true to unassign them."
            )
        affected = await self._store.async_remove_shared_schedule(shared_id)
        async_dispatcher_send(self._hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED)
        return affected
