"""Tests for ProfileRegistry — wraps the store + dispatches signals."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.comfort_band.const import SIGNAL_ACTIVE_PROFILE_CHANGED
from custom_components.comfort_band.profiles import ProfileRegistry
from custom_components.comfort_band.storage import ComfortBandStore


async def _make_registry(hass: HomeAssistant) -> ProfileRegistry:
    store = ComfortBandStore(hass)
    await store.async_load()
    return ProfileRegistry(hass, store)


async def test_active_starts_at_default(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    registry = await _make_registry(hass)
    assert registry.active == "home"
    assert "home" in registry.names
    assert "away" in registry.names
    assert "sleep" in registry.names


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
