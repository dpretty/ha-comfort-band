"""Tests for ComfortBandStore (persistence)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.storage import ComfortBandStore

# ----- defaults -----


async def test_first_load_returns_default_skeleton(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    assert store.list_zones() == []
    assert store.list_profiles() == ["away", "home", "sleep"]
    assert store.active_profile == "home"


async def test_default_zone_has_sane_initial_values(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    zone = await store.async_add_zone("office")
    assert zone["enabled"] is False  # shadow-mode default
    assert zone["override_until"] is None
    assert zone["last_action"] is None
    assert zone["deadband_below"] == 0.3
    assert zone["deadband_above"] == 0.5
    assert zone["min_cycle_minutes"] == 8
    assert zone["override_hours"] == 3
    assert zone["manual_low"] < zone["manual_high"]
    assert zone["schedules"] == {}


# ----- round-trip persistence -----


async def test_zone_round_trips_across_store_instances(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    first = ComfortBandStore(hass)
    await first.async_load()
    await first.async_add_zone("office")
    await first.async_update_zone("office", enabled=True, manual_low=21.5)

    second = ComfortBandStore(hass)
    await second.async_load()
    zone = second.get_zone("office")
    assert zone["enabled"] is True
    assert zone["manual_low"] == 21.5


async def test_active_profile_persists(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    first = ComfortBandStore(hass)
    await first.async_load()
    await first.async_set_active_profile("away")

    second = ComfortBandStore(hass)
    await second.async_load()
    assert second.active_profile == "away"


# ----- deep-copy isolation -----


async def test_get_zone_returns_independent_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")

    zone = store.get_zone("office")
    zone["manual_low"] = 99.0
    zone["schedules"]["home"] = {"baseline": [], "current": []}

    fresh = store.get_zone("office")
    assert fresh["manual_low"] != 99.0
    assert fresh["schedules"] == {}


async def test_data_property_is_deep_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")

    snapshot = store.data
    snapshot["zones"]["office"]["enabled"] = True
    snapshot["active_profile"] = "away"

    assert store.get_zone("office")["enabled"] is False
    assert store.active_profile == "home"


# ----- mutator validation -----


async def test_add_zone_rejects_duplicate(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    with pytest.raises(ValueError, match="already exists"):
        await store.async_add_zone("office")


async def test_update_zone_rejects_unknown_field(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    with pytest.raises(KeyError, match="Unknown zone field: bogus"):
        await store.async_update_zone("office", bogus=42)


async def test_set_active_profile_rejects_unknown(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="does not exist"):
        await store.async_set_active_profile("vacation")


async def test_remove_default_profile_refused(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="Cannot delete the default profile"):
        await store.async_remove_profile("home")


async def test_remove_active_profile_falls_back_to_home(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_set_active_profile("away")
    assert store.active_profile == "away"
    await store.async_remove_profile("away")
    assert store.active_profile == "home"


# ----- schedules -----


async def test_set_zone_schedule_round_trip(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    baseline = [{"at": "06:00", "low": 20.0, "high": 23.0}]
    await store.async_set_zone_schedule("office", "home", baseline)
    schedule = store.get_zone_schedule("office", "home")
    assert schedule is not None
    assert schedule["baseline"] == baseline
    assert schedule["current"] == baseline


async def test_set_zone_schedule_unknown_profile_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    with pytest.raises(ValueError, match="Profile 'vacation' does not exist"):
        await store.async_set_zone_schedule("office", "vacation", [])


async def test_set_zone_schedule_independent_lists(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    baseline = [{"at": "06:00", "low": 20.0, "high": 23.0}]
    await store.async_set_zone_schedule("office", "home", baseline)
    # Mutate the source list after persisting; persisted copy must be unaffected.
    baseline[0]["low"] = 99.0
    schedule = store.get_zone_schedule("office", "home")
    assert schedule is not None
    assert schedule["baseline"][0]["low"] == 20.0
