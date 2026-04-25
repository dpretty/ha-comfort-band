"""Common fixtures for Comfort Band tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None, None, None]:
    """Enable loading of custom integrations in every test."""
    yield


@pytest.fixture
def climate_calls(hass: HomeAssistant) -> list[tuple[str, dict[str, Any]]]:
    """Record every climate.set_hvac_mode / set_temperature call."""
    recorded: list[tuple[str, dict[str, Any]]] = []

    async def _record(call: ServiceCall) -> None:
        recorded.append((call.service, dict(call.data)))

    hass.services.async_register("climate", "set_hvac_mode", _record)
    hass.services.async_register("climate", "set_temperature", _record)
    return recorded


@pytest.fixture
def make_zone_entry() -> Any:
    """Factory: build a zone-kind MockConfigEntry."""

    def _make(
        zone_name: str = "office",
        climate_entity: str = "climate.office_hvac",
        temp_sensor: str = "sensor.office_room_temperature",
    ) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            unique_id=f"zone:{zone_name}",
            title=f"Comfort Band: {zone_name}",
            data={
                CONF_KIND: ENTRY_KIND_ZONE,
                CONF_ZONE_NAME: zone_name,
                CONF_CLIMATE_ENTITY: climate_entity,
                CONF_TEMP_SENSOR: temp_sensor,
            },
        )

    return _make


@pytest.fixture
def profile_manager_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="profile_manager",
        title="Comfort Band Profiles",
        data={CONF_KIND: ENTRY_KIND_PROFILE_MANAGER},
    )
