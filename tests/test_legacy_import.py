"""Tests for `legacy.read_legacy_hourly_schedule`."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.legacy import read_legacy_hourly_schedule
from custom_components.comfort_band.schedule import Transition


def _seed_full_legacy(hass: HomeAssistant, name: str, low: float, high: float) -> None:
    for h in range(24):
        hass.states.async_set(f"input_number.{name}_hour_{h:02d}_low", str(low), {})
        hass.states.async_set(f"input_number.{name}_hour_{h:02d}_high", str(high), {})


async def test_happy_path_24_equal_hours_collapses_to_one_transition(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    _seed_full_legacy(hass, "office", 20.0, 23.0)
    result = read_legacy_hourly_schedule(hass, "office")
    assert result == [Transition(at=time(0, 0), low=20.0, high=23.0)]


async def test_happy_path_per_hour_band_emits_transition_at_each_change(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    # Day-night band: cooler 00..05, warmer 06..21, cooler 22..23.
    for h in range(24):
        if 6 <= h <= 21:
            low, high = 20.0, 24.0
        else:
            low, high = 18.0, 22.0
        hass.states.async_set(f"input_number.office_hour_{h:02d}_low", str(low), {})
        hass.states.async_set(f"input_number.office_hour_{h:02d}_high", str(high), {})
    result = read_legacy_hourly_schedule(hass, "office")
    assert result == [
        Transition(at=time(0, 0), low=18.0, high=22.0),
        Transition(at=time(6, 0), low=20.0, high=24.0),
        Transition(at=time(22, 0), low=18.0, high=22.0),
    ]


async def test_missing_slot_raises(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    _seed_full_legacy(hass, "office", 20.0, 23.0)
    # Drop one slot.
    hass.states.async_remove("input_number.office_hour_13_low")
    with pytest.raises(ValueError, match="Legacy entity not found"):
        read_legacy_hourly_schedule(hass, "office")


async def test_unavailable_slot_raises(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    _seed_full_legacy(hass, "office", 20.0, 23.0)
    hass.states.async_set("input_number.office_hour_05_high", "unavailable", {})
    with pytest.raises(ValueError, match="is 'unavailable'"):
        read_legacy_hourly_schedule(hass, "office")


async def test_non_numeric_slot_raises(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    _seed_full_legacy(hass, "office", 20.0, 23.0)
    hass.states.async_set("input_number.office_hour_09_low", "not-a-number", {})
    with pytest.raises(ValueError, match="not numeric"):
        read_legacy_hourly_schedule(hass, "office")
