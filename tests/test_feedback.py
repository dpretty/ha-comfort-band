"""Tests for FeedbackStore (comfort-feedback log persistence)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.feedback import FeedbackEntry, FeedbackStore


def _entry(
    zone: str,
    timestamp: str,
    label: str = "just_right",
    *,
    room_temp: float | None = 21.0,
    low: float = 19.5,
    high: float = 22.5,
    action: str = "idle",
) -> FeedbackEntry:
    return {
        "zone": zone,
        "timestamp": timestamp,
        "label": label,
        "room_temp": room_temp,
        "low": low,
        "high": high,
        "action": action,
    }


async def test_append_then_get_returns_entry(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = FeedbackStore(hass)
    await store.async_load()
    await store.async_append(_entry("office", "2026-05-01T10:00:00+00:00", "too_hot"))

    entries = store.get_entries("office")
    assert len(entries) == 1
    assert entries[0]["label"] == "too_hot"
    assert entries[0]["room_temp"] == 21.0


async def test_get_entries_is_copy_on_read(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = FeedbackStore(hass)
    await store.async_load()
    await store.async_append(_entry("office", "2026-05-01T10:00:00+00:00"))
    entries = store.get_entries("office")
    entries[0]["label"] = "MUTATED"
    # A second read is unaffected by the caller mutating the first result.
    assert store.get_entries("office")[0]["label"] == "just_right"


async def test_get_entries_filters_by_zone(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = FeedbackStore(hass)
    await store.async_load()
    await store.async_append(_entry("office", "2026-05-01T10:00:00+00:00"))
    await store.async_append(_entry("gym", "2026-05-01T11:00:00+00:00"))
    await store.async_append(_entry("office", "2026-05-01T12:00:00+00:00"))

    office = store.get_entries("office")
    assert len(office) == 2
    assert all(e["zone"] == "office" for e in office)
    assert len(store.get_entries("gym")) == 1
    assert store.get_entries("unknown") == []


async def test_get_entries_since_filter(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = FeedbackStore(hass)
    await store.async_load()
    await store.async_append(_entry("office", "2026-05-01T10:00:00+00:00", "too_cold"))
    await store.async_append(_entry("office", "2026-05-02T10:00:00+00:00", "just_right"))
    await store.async_append(_entry("office", "2026-05-03T10:00:00+00:00", "too_hot"))

    recent = store.get_entries("office", since="2026-05-02T00:00:00+00:00")
    assert [e["label"] for e in recent] == ["just_right", "too_hot"]
    # Boundary is inclusive (>= since).
    on_boundary = store.get_entries("office", since="2026-05-02T10:00:00+00:00")
    assert [e["label"] for e in on_boundary] == ["just_right", "too_hot"]


async def test_get_entries_unparseable_since_returns_all(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = FeedbackStore(hass)
    await store.async_load()
    await store.async_append(_entry("office", "2026-05-01T10:00:00+00:00"))
    await store.async_append(_entry("office", "2026-05-02T10:00:00+00:00"))
    # A garbage `since` is ignored (forgiving), not an error.
    assert len(store.get_entries("office", since="not-a-date")) == 2


async def test_append_trims_to_cap(
    hass: HomeAssistant, hass_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patch the module-level cap small so the test stays fast; `async_append`
    # reads the global at call time, so the patched value takes effect.
    monkeypatch.setattr("custom_components.comfort_band.feedback.FEEDBACK_MAX_ENTRIES", 3)
    store = FeedbackStore(hass)
    await store.async_load()
    for i in range(5):
        await store.async_append(_entry("office", f"2026-05-0{i + 1}T10:00:00+00:00", str(i)))

    entries = store.get_entries("office")
    assert len(entries) == 3
    # Oldest two dropped; the most-recent three survive in order.
    assert [e["label"] for e in entries] == ["2", "3", "4"]


async def test_load_is_idempotent_and_persists(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = FeedbackStore(hass)
    await store.async_load()
    await store.async_load()  # second load is a no-op (no crash, no reset)
    await store.async_append(_entry("office", "2026-05-01T10:00:00+00:00"))

    # A fresh store instance reads the persisted entry back from disk.
    reopened = FeedbackStore(hass)
    await reopened.async_load()
    assert len(reopened.get_entries("office")) == 1
