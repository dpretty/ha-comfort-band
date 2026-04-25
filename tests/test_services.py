"""Tests for the eight `comfort_band.*` services."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comfort_band.const import DOMAIN

ZONE_TEMP_ENTITY = "sensor.office_temp"


@pytest.fixture
async def setup_zone(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ----- registration -----


async def test_all_services_registered(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    expected = {
        "set_schedule",
        "add_transition",
        "update_transition",
        "remove_transition",
        "start_override",
        "cancel_override",
        "set_profile",
        "import_legacy",
    }
    for service in expected:
        assert hass.services.has_service(DOMAIN, service), f"missing service: {service}"


# ----- schedule mutators -----


async def test_set_schedule_persists_and_refreshes(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "zone": "office",
            "profile": "home",
            "transitions": [
                {"at": "06:00", "low": 20.0, "high": 23.0},
                {"at": "22:00", "low": 18.0, "high": 21.0},
            ],
        },
        blocking=True,
    )
    store = hass.data[DOMAIN].store
    schedule = store.get_zone_schedule("office", "home")
    assert schedule is not None
    assert len(schedule["current"]) == 2
    assert schedule["current"][0]["at"] == "06:00"


async def test_set_schedule_unknown_zone_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="Unknown zone"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"zone": "nonexistent", "profile": "home", "transitions": []},
            blocking=True,
        )


async def test_add_then_update_then_remove_transition(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    common = {"zone": "office", "profile": "home"}
    await hass.services.async_call(
        DOMAIN, "add_transition", {**common, "at": "06:00", "low": 20, "high": 23}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "add_transition", {**common, "at": "22:00", "low": 18, "high": 21}, blocking=True
    )

    store = hass.data[DOMAIN].store
    sched = store.get_zone_schedule("office", "home")
    assert sched is not None and len(sched["current"]) == 2

    await hass.services.async_call(
        DOMAIN, "update_transition", {**common, "at": "06:00", "low": 21, "high": 24}, blocking=True
    )
    sched = store.get_zone_schedule("office", "home")
    assert sched is not None
    six_am = next(t for t in sched["current"] if t["at"] == "06:00")
    assert six_am["low"] == 21.0

    await hass.services.async_call(
        DOMAIN, "remove_transition", {**common, "at": "22:00"}, blocking=True
    )
    sched = store.get_zone_schedule("office", "home")
    assert sched is not None and len(sched["current"]) == 1


async def test_update_transition_missing_target_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "add_transition",
        {"zone": "office", "profile": "home", "at": "06:00", "low": 20, "high": 23},
        blocking=True,
    )
    with pytest.raises(ServiceValidationError, match="No transition at"):
        await hass.services.async_call(
            DOMAIN,
            "update_transition",
            {"zone": "office", "profile": "home", "at": "12:00", "low": 21, "high": 24},
            blocking=True,
        )


# ----- override services -----


async def test_start_override_then_cancel(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "start_override",
        {"zone": "office", "low": 22.0, "high": 24.0, "hours": 1},
        blocking=True,
    )
    assert hass.states.get("binary_sensor.office_override_active").state == "on"
    await hass.services.async_call(DOMAIN, "cancel_override", {"zone": "office"}, blocking=True)
    assert hass.states.get("binary_sensor.office_override_active").state == "off"


# ----- set_profile -----


async def test_set_profile_changes_active(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    profile_manager_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(profile_manager_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, "set_profile", {"profile": "away"}, blocking=True)
    assert hass.data[DOMAIN].profile_registry.active == "away"


# ----- import_legacy -----


def _seed_legacy(hass: HomeAssistant, source: str, low: float, high: float) -> None:
    for h in range(24):
        hass.states.async_set(f"input_number.{source}_hour_{h:02d}_low", str(low), {})
        hass.states.async_set(f"input_number.{source}_hour_{h:02d}_high", str(high), {})


async def test_import_legacy_happy_path(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    _seed_legacy(hass, "office", 20.0, 23.0)
    await hass.services.async_call(
        DOMAIN,
        "import_legacy",
        {"zone": "office", "source_zone_name": "office"},
        blocking=True,
    )
    store = hass.data[DOMAIN].store
    schedule = store.get_zone_schedule("office", "home")
    assert schedule is not None
    assert schedule["baseline"] == [{"at": "00:00", "low": 20.0, "high": 23.0}]
    assert schedule["current"] == [{"at": "00:00", "low": 20.0, "high": 23.0}]


async def test_import_legacy_unknown_zone_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    _seed_legacy(hass, "office", 20.0, 23.0)
    with pytest.raises(ServiceValidationError, match="Unknown zone"):
        await hass.services.async_call(
            DOMAIN,
            "import_legacy",
            {"zone": "nonexistent", "source_zone_name": "office"},
            blocking=True,
        )


async def test_import_legacy_missing_source_helpers_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    # No input_number entities created.
    with pytest.raises(ServiceValidationError, match="Legacy entity not found"):
        await hass.services.async_call(
            DOMAIN,
            "import_legacy",
            {"zone": "office", "source_zone_name": "office"},
            blocking=True,
        )
