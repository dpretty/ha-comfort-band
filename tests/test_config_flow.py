"""Config flow tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comfort_band.const import (
    CONF_CLIMATE_ENTITY,
    CONF_KIND,
    CONF_TEMP_SENSOR,
    CONF_ZONE_NAME,
    DOMAIN,
    ENTRY_KIND_PROFILE_MANAGER,
    ENTRY_KIND_ZONE,
)


async def _start_user(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


# ----- menu -----


async def test_menu_shows_both_options_initially(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    result = await _start_user(hass)
    assert result["type"] == FlowResultType.MENU
    assert "profile_manager" in result["menu_options"]
    assert "zone" in result["menu_options"]


async def test_menu_omits_profile_manager_when_already_configured(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    profile_manager_entry.add_to_hass(hass)
    result = await _start_user(hass)
    assert result["type"] == FlowResultType.MENU
    assert "profile_manager" not in result["menu_options"]
    assert "zone" in result["menu_options"]


# ----- profile manager -----


async def test_profile_manager_flow_creates_entry(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    menu = await _start_user(hass)
    confirm = await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": "profile_manager"}
    )
    assert confirm["type"] == FlowResultType.FORM
    final = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})
    assert final["type"] == FlowResultType.CREATE_ENTRY
    assert final["title"] == "Comfort Band Profiles"
    assert final["data"] == {CONF_KIND: ENTRY_KIND_PROFILE_MANAGER}


# Singleton enforcement is verified at the menu level by
# test_menu_omits_profile_manager_when_already_configured; the unique_id
# guard inside async_step_profile_manager is belt-and-braces against
# alternative entry sources we don't currently expose.


# ----- zone -----


_VALID_ZONE_INPUT = {
    CONF_ZONE_NAME: "office",
    CONF_CLIMATE_ENTITY: "climate.office_hvac",
    CONF_TEMP_SENSOR: "sensor.office_room_temperature",
}


async def test_zone_flow_creates_entry(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    menu = await _start_user(hass)
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "zone"})
    assert form["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(form["flow_id"], _VALID_ZONE_INPUT)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Comfort Band: office"
    assert result["data"] == {
        CONF_KIND: ENTRY_KIND_ZONE,
        CONF_ZONE_NAME: "office",
        CONF_CLIMATE_ENTITY: "climate.office_hvac",
        CONF_TEMP_SENSOR: "sensor.office_room_temperature",
    }


async def test_zone_flow_normalises_slug(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    menu = await _start_user(hass)
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "zone"})
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {**_VALID_ZONE_INPUT, CONF_ZONE_NAME: "  Office  "}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ZONE_NAME] == "office"


async def test_zone_flow_rejects_duplicate_slug(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    make_zone_entry().add_to_hass(hass)
    menu = await _start_user(hass)
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "zone"})
    result = await hass.config_entries.flow.async_configure(form["flow_id"], _VALID_ZONE_INPUT)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zone_flow_rejects_invalid_slug(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    menu = await _start_user(hass)
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "zone"})
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {**_VALID_ZONE_INPUT, CONF_ZONE_NAME: "9bad-slug"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_ZONE_NAME: "invalid_slug"}


# ----- setup_entry plumbing (zone) -----


async def test_setup_zone_entry_creates_coordinator(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(_VALID_ZONE_INPUT[CONF_TEMP_SENSOR], "21.5", {})

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = hass.data[DOMAIN]
    assert entry.entry_id in data.zone_coordinators
    coordinator = data.zone_coordinators[entry.entry_id]
    assert coordinator.zone_name == "office"
    assert coordinator.data is not None
    assert coordinator.data.room == 21.5
    assert data.zone_slug_to_entry_id["office"] == entry.entry_id


async def test_setup_profile_manager_entry_runs(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    profile_manager_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(profile_manager_entry.entry_id)
    await hass.async_block_till_done()
    data = hass.data[DOMAIN]
    assert data.profile_registry.active == "home"


async def test_unload_zone_entry_releases_coordinator(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    data = hass.data[DOMAIN]
    assert entry.entry_id not in data.zone_coordinators
    assert "office" not in data.zone_slug_to_entry_id


async def test_remove_zone_entry_drops_zone_from_store(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = hass.data[DOMAIN]
    assert data.store.has_zone("office")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not data.store.has_zone("office")


async def test_session_a_placeholder_is_removed_at_setup(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    placeholder = MockConfigEntry(domain=DOMAIN, data={}, title="Comfort Band")
    placeholder.add_to_hass(hass)
    # Initial setup attempt: __init__.async_setup is called when this entry tries
    # to set up. It schedules removal; verify the entry is gone after the loop.
    with patch("custom_components.comfort_band.LOGGER"):
        result = await hass.config_entries.async_setup(placeholder.entry_id)
    assert result is False
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(placeholder.entry_id) is None
