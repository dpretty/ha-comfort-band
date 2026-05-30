"""Comfort-feedback log (v0.11.0).

A lightweight, append-only store for user comfort feedback — the foundation
for the v3 auto-learning loop. Each `record_feedback` service call appends one
entry enriched with the live room temperature, effective band, and current
action, so a later aggregator can correlate "too_hot at 22:00 while idle in
band (20, 24)" patterns without re-deriving context.

Kept in a SEPARATE Store (`comfort_band.feedback`) from the main zone data so
this unbounded-by-nature history never bloats or risks corrupting the core
config. Capped to the most-recent `FEEDBACK_MAX_ENTRIES` entries. Accessors
return deep copies for copy-on-read isolation, matching `ComfortBandStore`.
"""

from __future__ import annotations

import copy
from typing import TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    FEEDBACK_MAX_ENTRIES,
    FEEDBACK_STORAGE_KEY,
    FEEDBACK_STORAGE_VERSION,
)


class FeedbackEntry(TypedDict):
    """One recorded comfort-feedback data point."""

    zone: str
    timestamp: str  # ISO-8601, UTC (dt_util.utcnow().isoformat())
    label: str  # one of FEEDBACK_LABELS
    room_temp: float | None  # raw room reading at record time (None if sensor unavailable)
    low: float  # effective band low at record time
    high: float  # effective band high at record time
    action: str  # ACTION_* in effect at record time


class FeedbackData(TypedDict):
    """Serialized shape of the feedback Store."""

    entries: list[FeedbackEntry]


def _default_data() -> FeedbackData:
    return {"entries": []}


class FeedbackStore:
    """Append-only comfort-feedback log with copy-on-read isolation."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[FeedbackData] = Store(
            hass, FEEDBACK_STORAGE_VERSION, FEEDBACK_STORAGE_KEY
        )
        self._data: FeedbackData = _default_data()
        self._loaded = False

    async def async_load(self) -> None:
        """Read from disk; default to an empty log on first run. Idempotent."""
        if self._loaded:
            return
        loaded = await self._store.async_load()
        if loaded is not None:
            self._data = loaded
        self._loaded = True

    async def async_append(self, entry: FeedbackEntry) -> None:
        """Append an entry and persist, trimming to the most-recent cap.

        Entries accumulate in chronological order (callers append "now"), so
        keeping the tail keeps the newest `FEEDBACK_MAX_ENTRIES`.
        """
        entries = self._data["entries"]
        entries.append(copy.deepcopy(entry))
        # Guard `> 0`: `del entries[:-0]` is `del entries[:]` (Python has no
        # negative-zero slice), so a cap of 0 would wipe the whole list. Treat
        # 0 (or negative) as "unbounded" instead — keeps a future "disable the
        # cap" edit from silently nuking every append.
        if FEEDBACK_MAX_ENTRIES > 0 and len(entries) > FEEDBACK_MAX_ENTRIES:
            # Delete in place so `self._data["entries"]` stays the same list
            # object the rest of the method (and tests) reference.
            del entries[:-FEEDBACK_MAX_ENTRIES]
        await self._store.async_save(self._data)

    def get_entries(self, zone: str, since: str | None = None) -> list[FeedbackEntry]:
        """Return this zone's entries (deep-copied), oldest-first.

        `since` is an optional ISO-8601 timestamp; when given, only entries
        with `timestamp >= since` are returned. An unparseable `since` is
        ignored (returns all of the zone's entries) rather than raising — the
        WS layer validates shape, this method is forgiving on value.
        """
        since_dt = dt_util.parse_datetime(since) if since else None
        out: list[FeedbackEntry] = []
        for entry in self._data["entries"]:
            if entry["zone"] != zone:
                continue
            if since_dt is not None:
                entry_dt = dt_util.parse_datetime(entry["timestamp"])
                if entry_dt is not None and entry_dt < since_dt:
                    continue
            out.append(copy.deepcopy(entry))
        return out
