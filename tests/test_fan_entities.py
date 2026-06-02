"""End-to-end tests for the v0.13.0 fan-boost entities.

Spins up a real zone via `make_zone_entry` (with a climate entity that exposes
`fan_modes`), then reads / writes the `fan_control_enabled` switch and the two
fan-mode selects through HA's normal service-call paths. Mirrors
`test_apparent_temp_entities.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.const import DOMAIN

CLIMATE_ENTITY = "climate.office_hvac"
ZONE_TEMP_ENTITY = "sensor.office_temp"  # non-colliding with comfort_band's mirror
FAN_MODES = ["low", "mid", "high"]


@pytest.fixture
async def fan_zone(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> AsyncIterator[Any]:
    """A set-up zone whose climate exposes `fan_modes`. Yields the entry."""
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"fan_modes": FAN_MODES, "fan_mode": "low"})
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry


async def test_fan_control_switch_defaults_off_and_toggles(
    hass: HomeAssistant, hass_storage: dict[str, Any], fan_zone: Any
) -> None:
    state = hass.states.get("switch.office_fan_control_enabled")
    assert state is not None
    assert state.state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.office_fan_control_enabled"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.office_fan_control_enabled").state == "on"
    assert hass.data[DOMAIN].store.get_zone("office")["fan_control_enabled"] is True


async def test_fan_mode_select_options_match_climate_fan_modes(
    hass: HomeAssistant, hass_storage: dict[str, Any], fan_zone: Any
) -> None:
    for entity_id in ("select.office_active_fan_mode", "select.office_idle_fan_mode"):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        # Options are the unit's fan_modes plus the leading "(none)" sentinel.
        assert state.attributes["options"] == ["(none)", *FAN_MODES]
        # Nothing picked yet -> shows the "(none)" sentinel.
        assert state.state == "(none)"


async def test_fan_mode_select_persists_choice(
    hass: HomeAssistant, hass_storage: dict[str, Any], fan_zone: Any
) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.office_active_fan_mode", "option": "high"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get("select.office_active_fan_mode").state == "high"
    assert hass.data[DOMAIN].store.get_zone("office")["active_fan_mode"] == "high"


async def test_fan_mode_select_unavailable_when_climate_has_no_fan_modes(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """A fanless climate (no `fan_modes`) -> the selects render unavailable."""
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(CLIMATE_ENTITY, "off", {})  # no fan_modes attribute
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("select.office_active_fan_mode")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_fan_mode_select_blanks_stale_stored_value(
    hass: HomeAssistant, hass_storage: dict[str, Any], fan_zone: Any
) -> None:
    """A stored mode no longer in the climate's `fan_modes` renders blank (not
    the dead string, and not an error) — the user just re-picks."""
    store = hass.data[DOMAIN].store
    await store.async_update_zone("office", active_fan_mode="turbo")  # not in FAN_MODES
    coordinator = hass.data[DOMAIN].zone_coordinators[fan_zone.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("select.office_active_fan_mode")
    assert state is not None
    # Blank (current_option coerced to None), still available (fan_modes present).
    assert state.state == STATE_UNKNOWN
    assert state.attributes["options"] == ["(none)", *FAN_MODES]


async def test_fan_mode_select_none_option_clears_the_mode(
    hass: HomeAssistant, hass_storage: dict[str, Any], fan_zone: Any
) -> None:
    """Picking the '(none)' sentinel writes None — lets a user drop one side
    (e.g. keep idle, stop boosting active) without disabling the whole switch."""
    store = hass.data[DOMAIN].store
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.office_active_fan_mode", "option": "high"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert store.get_zone("office")["active_fan_mode"] == "high"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.office_active_fan_mode", "option": "(none)"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert store.get_zone("office")["active_fan_mode"] is None
    assert hass.states.get("select.office_active_fan_mode").state == "(none)"


async def test_fan_mode_select_current_option_coercion(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Unit-level guard for `FanModeSelect.current_option`. HA's `@final`
    `SelectEntity.state` *also* coerces a not-in-options value to None, so an
    integration-level state assertion alone is false-confidence — assert the
    property directly so the code's own coercion is genuinely pinned:
    stored None -> "(none)" sentinel; stored valid -> the value; stored stale
    (not in the unit's fan_modes) -> None."""
    from custom_components.comfort_band.coordinator import ZoneCoordinator
    from custom_components.comfort_band.select import FanModeSelect
    from custom_components.comfort_band.storage import ComfortBandStore

    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    coordinator = ZoneCoordinator(hass, store, "office", CLIMATE_ENTITY, ZONE_TEMP_ENTITY)
    hass.states.async_set(CLIMATE_ENTITY, "off", {"fan_modes": FAN_MODES, "fan_mode": "low"})
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    await coordinator.async_refresh()
    sel = FanModeSelect(coordinator, "active_fan_mode", coordinator.async_set_active_fan_mode)

    # Stored None -> the "(none)" sentinel (which is in options, so no warning).
    assert sel.current_option == "(none)"
    # Stored valid mode -> that mode.
    await store.async_update_zone("office", active_fan_mode="high")
    await coordinator.async_refresh()
    assert sel.current_option == "high"
    # Stored mode no longer offered by the unit -> blank (None), not the dead
    # string. This is the branch HA's state property would also coerce, so it's
    # asserted here on the property directly.
    await store.async_update_zone("office", active_fan_mode="turbo")
    await coordinator.async_refresh()
    assert sel.current_option is None
    await coordinator.async_unload()
