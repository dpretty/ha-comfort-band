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
    # `humidity_sensor` is optional in the schema; when omitted from
    # _VALID_ZONE_INPUT it lands as None in the entry data.
    assert result["data"][CONF_KIND] == ENTRY_KIND_ZONE
    assert result["data"][CONF_ZONE_NAME] == "office"
    assert result["data"][CONF_CLIMATE_ENTITY] == "climate.office_hvac"
    assert result["data"][CONF_TEMP_SENSOR] == "sensor.office_room_temperature"
    from custom_components.comfort_band.const import CONF_HUMIDITY_SENSOR

    assert result["data"][CONF_HUMIDITY_SENSOR] is None


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


# ----- OptionsFlow (humidity sensor) -----


async def test_options_flow_sets_humidity_sensor(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """Existing zones can attach a humidity sensor via OptionsFlow."""
    from custom_components.comfort_band.const import CONF_HUMIDITY_SENSOR

    entry = make_zone_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert init_result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        init_result["flow_id"],
        {CONF_HUMIDITY_SENSOR: "sensor.office_humidity"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options.get(CONF_HUMIDITY_SENSOR) == "sensor.office_humidity"


async def test_options_flow_unavailable_for_profile_manager(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    """Only zone entries have editable options; the profile manager has none."""
    profile_manager_entry.add_to_hass(hass)
    handler = config_entries.HANDLERS[DOMAIN]
    assert handler.async_supports_options_flow(profile_manager_entry) is False


async def test_options_flow_can_clear_humidity_sensor_set_in_data(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """If a humidity sensor was set at ConfigFlow time, the OptionsFlow
    must be able to clear it. The fix in async_step_init normalises the
    missing-key case to {key: None} so the resolution in __init__.py
    sees the cleared value instead of falling through to entry.data.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.comfort_band.const import (
        CONF_CLIMATE_ENTITY,
        CONF_HUMIDITY_SENSOR,
        CONF_KIND,
        CONF_TEMP_SENSOR,
        CONF_ZONE_NAME,
        ENTRY_KIND_ZONE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="zone:office",
        title="Comfort Band: office",
        data={
            CONF_KIND: ENTRY_KIND_ZONE,
            CONF_ZONE_NAME: "office",
            CONF_CLIMATE_ENTITY: "climate.office_hvac",
            CONF_TEMP_SENSOR: "sensor.office_room_temperature",
            CONF_HUMIDITY_SENSOR: "sensor.office_humidity",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Coordinator picks up the humidity_sensor from entry.data.
    coordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    assert coordinator.humidity_entity_id == "sensor.office_humidity"

    # Submit an empty OptionsFlow form (selector left blank) → should
    # clear the sensor on the next reload.
    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(init_result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Key must be present in options (with None) so the resolution code
    # path doesn't fall back to entry.data.
    assert CONF_HUMIDITY_SENSOR in entry.options
    assert entry.options[CONF_HUMIDITY_SENSOR] is None

    # The reload listener fires and re-creates the coordinator with no
    # humidity sensor.
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    assert coordinator.humidity_entity_id is None


async def test_options_flow_save_triggers_reload(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """Saving the OptionsFlow must reload the entry so the coordinator
    picks up the new humidity sensor without a HA restart."""
    from custom_components.comfort_band.const import CONF_HUMIDITY_SENSOR

    entry = make_zone_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    assert coordinator.humidity_entity_id is None  # no humidity at setup

    init_result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        init_result["flow_id"],
        {CONF_HUMIDITY_SENSOR: "sensor.office_humidity"},
    )
    await hass.async_block_till_done()

    # Reload listener fires; new coordinator picks up the new sensor.
    new_coordinator = hass.data[DOMAIN].zone_coordinators[entry.entry_id]
    assert new_coordinator.humidity_entity_id == "sensor.office_humidity"
