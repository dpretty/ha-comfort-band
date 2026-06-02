"""End-to-end tests: every entity a zone or profile-manager entry should
register actually appears in `hass.states` after setup completes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

ZONE_TEMP_ENTITY = "sensor.office_temp"  # external; non-colliding with the mirror

_EXPECTED_ZONE_ENTITIES = (
    "number.office_manual_low",
    "number.office_manual_high",
    "number.office_override_hours",
    "number.office_deadband_below",
    "number.office_deadband_above",
    "number.office_minimum_cycle_minutes",
    "number.office_cross_mode_minimum_minutes",
    "number.office_lookahead_minutes",
    "number.office_passive_tolerance",
    "number.office_mpc_horizon_minutes",  # v0.8
    "sensor.office_effective_low",
    "sensor.office_effective_high",
    "sensor.office_room_temperature",  # comfort_band's diagnostic mirror
    "sensor.office_apparent_temperature",
    "sensor.office_override_ends",
    "sensor.office_current_action",
    "sensor.office_thermal_slope",
    "sensor.office_predicted_action",
    "sensor.office_mpc_action",  # v0.8
    "binary_sensor.office_override_active",
    "binary_sensor.office_mpc_ready",  # v0.8
    "button.office_cancel_override",
    "switch.office_enabled",
    "switch.office_learning_enabled",
    "switch.office_mpc_enabled",  # v0.8
    "switch.office_use_apparent_temperature",
    "switch.office_fan_control_enabled",  # v0.13
    "select.office_active_fan_mode",  # v0.13 (unavailable until climate has fan_modes)
    "select.office_idle_fan_mode",  # v0.13
)


async def test_zone_entry_creates_all_entities(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.5", {})

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in _EXPECTED_ZONE_ENTITIES:
        state = hass.states.get(entity_id)
        assert state is not None, f"missing entity: {entity_id}"


async def test_shared_data_carries_loaded_feedback_store(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """v0.11.0: ComfortBandData exposes a loaded FeedbackStore wired in
    `_ensure_shared_data` (the record_feedback service depends on it)."""
    from custom_components.comfort_band.const import DOMAIN
    from custom_components.comfort_band.feedback import FeedbackStore

    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.5", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    feedback_store = hass.data[DOMAIN].feedback_store
    assert isinstance(feedback_store, FeedbackStore)
    # Loaded + usable: an empty log returns no entries rather than raising.
    assert feedback_store.get_entries("office") == []


async def test_profile_manager_entry_creates_select(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    profile_manager_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(profile_manager_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("select.comfort_band_profiles_active_profile")
    assert state is not None
    assert state.state == "home"
    assert sorted(state.attributes["options"]) == ["away", "home"]
    assert state.attributes["default_profile"] == "home"
    # Per-profile descriptions exposed alongside options for the card.
    descriptions = state.attributes["descriptions"]
    assert set(descriptions.keys()) == {"away", "home"}
    assert "occupied" in descriptions["home"].lower()


async def test_select_entity_attributes_track_rename(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    """End-to-end: renaming the default profile updates the select entity's
    `default_profile` and `descriptions` attributes (via the new
    SIGNAL_PROFILE_LIST_CHANGED → async_write_ha_state path)."""
    profile_manager_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(profile_manager_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "comfort_band",
        "rename_profile",
        {"old": "home", "new": "weekday"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("select.comfort_band_profiles_active_profile")
    assert state is not None
    assert state.state == "weekday"  # active also followed the rename
    assert sorted(state.attributes["options"]) == ["away", "weekday"]
    assert state.attributes["default_profile"] == "weekday"
    assert set(state.attributes["descriptions"].keys()) == {"away", "weekday"}


async def test_zone_room_temp_sensor_mirrors_external(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "22.3", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mirror = hass.states.get("sensor.office_room_temperature")
    assert mirror is not None
    assert float(mirror.state) == 22.3


async def test_zone_current_action_sensor_starts_idle_inside_band(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})  # in default band [19.5, 22.5]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    action = hass.states.get("sensor.office_current_action")
    assert action is not None
    assert action.state == "idle"


async def test_setting_manual_low_starts_override_via_number_entity(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Pre-condition: override is off.
    assert hass.states.get("binary_sensor.office_override_active").state == "off"

    # Write to the number entity via the public service -- the same path the UI uses.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.office_manual_low", "value": 22.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Override should now be active and ending in ~3 hours (default).
    assert hass.states.get("binary_sensor.office_override_active").state == "on"
    ends = hass.states.get("sensor.office_override_ends")
    assert ends is not None
    assert ends.state not in ("unknown", "unavailable", "")


async def test_cancel_override_button_clears_override(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.office_manual_high", "value": 23.0},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_override_active").state == "on"

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.office_cancel_override"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_override_active").state == "off"


async def test_select_changes_active_profile(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    profile_manager_entry: MockConfigEntry,
) -> None:
    profile_manager_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(profile_manager_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": "select.comfort_band_profiles_active_profile",
            "option": "away",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("select.comfort_band_profiles_active_profile")
    assert state is not None
    assert state.state == "away"
