"""Tests for ProfileRegistry — wraps the store + dispatches signals."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.comfort_band.const import (
    SIGNAL_ACTIVE_PROFILE_CHANGED,
    SIGNAL_PROFILE_LIST_CHANGED,
)
from custom_components.comfort_band.profiles import ProfileRegistry
from custom_components.comfort_band.storage import ComfortBandStore


async def _make_registry(hass: HomeAssistant) -> ProfileRegistry:
    store = ComfortBandStore(hass)
    await store.async_load()
    return ProfileRegistry(hass, store)


async def test_active_starts_at_default(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    registry = await _make_registry(hass)
    assert registry.active == "home"
    assert registry.default == "home"
    assert "home" in registry.names
    assert "away" in registry.names
    assert "sleep" not in registry.names  # dropped from built-ins in v0.3


async def test_set_active_dispatches_signal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    received: list[str] = []

    @callback
    def on_change(name: str) -> None:
        received.append(name)

    unsub = async_dispatcher_connect(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, on_change)
    try:
        await registry.async_set_active("away")
    finally:
        unsub()

    assert received == ["away"]
    assert registry.active == "away"


async def test_set_active_no_change_no_signal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    received: list[str] = []

    @callback
    def on_change(name: str) -> None:
        received.append(name)

    unsub = async_dispatcher_connect(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, on_change)
    try:
        await registry.async_set_active("home")  # already active
    finally:
        unsub()

    assert received == []


async def test_create_then_delete_user_profile(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    await registry.async_create("vacation", "Long absence")
    assert "vacation" in registry.names
    await registry.async_delete("vacation")
    assert "vacation" not in registry.names


async def test_delete_home_refused(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    registry = await _make_registry(hass)
    with pytest.raises(ValueError, match="Cannot delete the default profile"):
        await registry.async_delete("home")


async def test_delete_active_profile_falls_back_and_signals(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    await registry.async_create("vacation")
    await registry.async_set_active("vacation")

    received: list[str] = []

    @callback
    def on_change(name: str) -> None:
        received.append(name)

    unsub = async_dispatcher_connect(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, on_change)
    try:
        await registry.async_delete("vacation")
    finally:
        unsub()

    assert registry.active == "home"
    assert received == ["home"]


# ----- list-changed signal -----


async def test_create_fires_list_signal(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    registry = await _make_registry(hass)
    received: list[None] = []

    @callback
    def on_change() -> None:
        received.append(None)

    unsub = async_dispatcher_connect(hass, SIGNAL_PROFILE_LIST_CHANGED, on_change)
    try:
        await registry.async_create("weekend", "Saturday + Sunday")
    finally:
        unsub()
    assert len(received) == 1


async def test_clone_copies_schedules_and_fires_list_signal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    # Seed home with a schedule for one zone so the clone copies it.
    await registry._store.async_add_zone("office")
    await registry._store.async_set_zone_schedule(
        "office", "home", [{"at": "06:00", "low": 20.0, "high": 23.0}]
    )

    received: list[None] = []

    @callback
    def on_change() -> None:
        received.append(None)

    unsub = async_dispatcher_connect(hass, SIGNAL_PROFILE_LIST_CHANGED, on_change)
    try:
        await registry.async_clone("home", "weekend", "Saturdays + Sundays")
    finally:
        unsub()

    assert "weekend" in registry.names
    assert registry.description("weekend") == "Saturdays + Sundays"
    cloned = registry._store.get_zone_schedule("office", "weekend")
    assert cloned is not None
    assert cloned["baseline"][0]["at"] == "06:00"
    assert len(received) == 1


async def test_rename_non_active_fires_list_signal_only(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    list_signals: list[None] = []
    active_signals: list[str] = []

    @callback
    def on_list_only() -> None:
        list_signals.append(None)

    @callback
    def on_active_capture(name: str) -> None:
        active_signals.append(name)

    unsub_list = async_dispatcher_connect(hass, SIGNAL_PROFILE_LIST_CHANGED, on_list_only)
    unsub_active = async_dispatcher_connect(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, on_active_capture)
    try:
        await registry.async_rename("away", "trip")  # active is "home"
    finally:
        unsub_list()
        unsub_active()

    assert len(list_signals) == 1
    assert active_signals == []  # not the active profile


async def test_rename_active_fires_both_signals(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    await registry.async_set_active("away")
    list_signals: list[None] = []
    active_signals: list[str] = []

    @callback
    def on_list() -> None:
        list_signals.append(None)

    @callback
    def on_active(name: str) -> None:
        active_signals.append(name)

    unsub_list = async_dispatcher_connect(hass, SIGNAL_PROFILE_LIST_CHANGED, on_list)
    unsub_active = async_dispatcher_connect(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, on_active)
    try:
        await registry.async_rename("away", "trip")
    finally:
        unsub_list()
        unsub_active()

    assert len(list_signals) == 1
    assert active_signals == ["trip"]
    assert registry.active == "trip"


async def test_rename_default_moves_default_pointer(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    registry = await _make_registry(hass)
    await registry.async_rename("home", "weekday")
    assert registry.default == "weekday"
    # Renamed default still cannot be deleted.
    with pytest.raises(ValueError, match="Cannot delete the default profile"):
        await registry.async_delete("weekday")


async def test_delete_fires_list_signal(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    registry = await _make_registry(hass)
    await registry.async_create("vacation")
    received: list[None] = []

    @callback
    def on_change() -> None:
        received.append(None)

    unsub = async_dispatcher_connect(hass, SIGNAL_PROFILE_LIST_CHANGED, on_change)
    try:
        await registry.async_delete("vacation")
    finally:
        unsub()
    assert len(received) == 1


async def test_delete_active_after_default_rename_signals_new_default(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Regression test for the rename-aware default-profile fallback:
    after `home` is renamed to `weekday`, deleting whichever profile is
    active should fall back to `weekday`, NOT to the literal `home`."""
    registry = await _make_registry(hass)
    await registry.async_rename("home", "weekday")
    await registry.async_create("trip")
    await registry.async_set_active("trip")

    received: list[str] = []

    @callback
    def on_change(name: str) -> None:
        received.append(name)

    unsub = async_dispatcher_connect(hass, SIGNAL_ACTIVE_PROFILE_CHANGED, on_change)
    try:
        await registry.async_delete("trip")
    finally:
        unsub()

    assert received == ["weekday"]
    assert registry.active == "weekday"
