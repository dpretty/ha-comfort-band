"""End-to-end tests for the new apparent-temp sensor + the two new switches.

Mirrors the conftest-driven setup the other entity tests use: spin up a real
zone via `make_zone_entry`, then read / write state through HA's normal
service-call paths.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.const import DOMAIN

ZONE_TEMP_ENTITY = "sensor.office_temp"
HUMIDITY_ENTITY = "sensor.office_humidity"


@pytest.fixture
async def zone_with_humidity(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.comfort_band.const import (
        CONF_CLIMATE_ENTITY,
        CONF_HUMIDITY_SENSOR,
        CONF_KIND,
        CONF_TEMP_SENSOR,
        CONF_ZONE_NAME,
        ENTRY_KIND_ZONE,
    )

    # Build a fresh MockConfigEntry so we can include humidity_sensor at
    # construction time (entry.data is immutable once set).
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="zone:office",
        title="Comfort Band: office",
        data={
            CONF_KIND: ENTRY_KIND_ZONE,
            CONF_ZONE_NAME: "office",
            CONF_CLIMATE_ENTITY: "climate.office_hvac",
            CONF_TEMP_SENSOR: ZONE_TEMP_ENTITY,
            CONF_HUMIDITY_SENSOR: HUMIDITY_ENTITY,
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    hass.states.async_set(HUMIDITY_ENTITY, "50", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_apparent_temp_sensor_reads_steadman_value(
    hass: HomeAssistant, hass_storage: dict[str, Any], zone_with_humidity: None
) -> None:
    state = hass.states.get("sensor.office_apparent_temperature")
    assert state is not None
    # 21 °C, 50 % RH → Steadman 21.16, rounded to 21.2 by the sensor.
    assert state.state != "unknown"
    assert state.state != "unavailable"
    value = float(state.state)
    assert abs(value - 21.16) < 0.1


async def test_apparent_temp_equals_room_when_humidity_absent(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "22.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    apparent = hass.states.get("sensor.office_apparent_temperature")
    room = hass.states.get("sensor.office_room_temperature")
    assert apparent is not None and room is not None
    # `compute(T, None) → T`; both sensors round identically.
    assert apparent.state == room.state


async def test_room_temperature_exposes_humidity_sensor_attribute(
    hass: HomeAssistant, hass_storage: dict[str, Any], zone_with_humidity: None
) -> None:
    state = hass.states.get("sensor.office_room_temperature")
    assert state is not None
    # The Settings tab on the card reads this attribute.
    assert state.attributes.get("humidity_sensor") == HUMIDITY_ENTITY


async def test_room_temperature_attribute_none_when_no_humidity_sensor(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.office_room_temperature")
    assert state is not None
    assert state.attributes.get("humidity_sensor") is None


async def test_learning_enabled_switch_defaults_off_and_toggles(
    hass: HomeAssistant, hass_storage: dict[str, Any], zone_with_humidity: None
) -> None:
    state = hass.states.get("switch.office_learning_enabled")
    assert state is not None
    assert state.state == "off"

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.office_learning_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.office_learning_enabled").state == "on"
    store = hass.data[DOMAIN].store
    assert store.get_zone("office")["learning_enabled"] is True


async def test_use_apparent_temperature_switch_defaults_off_and_toggles(
    hass: HomeAssistant, hass_storage: dict[str, Any], zone_with_humidity: None
) -> None:
    state = hass.states.get("switch.office_use_apparent_temperature")
    assert state is not None
    assert state.state == "off"

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.office_use_apparent_temperature"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.office_use_apparent_temperature").state == "on"
    store = hass.data[DOMAIN].store
    assert store.get_zone("office")["use_apparent_temperature"] is True
