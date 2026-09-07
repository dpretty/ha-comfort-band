"""End-to-end tests for the new apparent-temp sensor + the two new switches.

Mirrors the conftest-driven setup the other entity tests use: spin up a real
zone via `make_zone_entry`, then read / write state through HA's normal
service-call paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.const import DOMAIN

ZONE_TEMP_ENTITY = "sensor.office_temp"
HUMIDITY_ENTITY = "sensor.office_humidity"


@pytest.fixture
async def zone_with_humidity(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> AsyncIterator[None]:
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
    # Yield so future teardown hooks (entry unload, etc.) can run if needed.
    # Matches the pattern HA core uses for fixtures that own a ConfigEntry.
    yield


async def test_apparent_temp_sensor_reads_steadman_value(
    hass: HomeAssistant, hass_storage: dict[str, Any], zone_with_humidity: None
) -> None:
    state = hass.states.get("sensor.office_apparent_temperature")
    assert state is not None
    # 21 °C, 50 % RH → Steadman 21.09, rounded to 21.1 by the sensor.
    assert state.state != "unknown"
    assert state.state != "unavailable"
    value = float(state.state)
    assert abs(value - 21.09) < 0.1


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


async def test_room_sensor_unavailable_binary_sensor(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """v0.16.0: a zone that has stopped controlling must say so somewhere alertable.

    The incident this came from went unnoticed for hours while a bedroom sat
    below its band, because a dead sensor produced no user-visible signal at all.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.office_room_sensor_unavailable")
    assert state is not None
    assert state.state == "off"
    # `problem` so it reads as an alertable fault, not a diagnostic curiosity.
    assert state.attributes.get("device_class") == "problem"
    # Deliberately first-class: a diagnostic entity is hidden by default, and
    # this is the one signal saying the room is no longer being controlled.
    # Checked on the registry entry -- `entity_category` is a registry property,
    # not a state attribute, so asserting on `state.attributes` proves nothing.
    from homeassistant.helpers import entity_registry as er

    reg_entry = er.async_get(hass).async_get("binary_sensor.office_room_sensor_unavailable")
    assert reg_entry is not None and reg_entry.entity_category is None

    # Sensor drops out -> problem asserted. Room-temp changes are debounced 2 s.
    hass.states.async_set(ZONE_TEMP_ENTITY, "unavailable", {})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "on"

    # ...and clears when it comes back.
    hass.states.async_set(ZONE_TEMP_ENTITY, "20.5", {})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "off"


async def test_a_nan_reading_raises_the_alert(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """`nan` parses as a float but can't be compared, so it isn't a reading.

    Every comparison against NaN is False, so hysteresis reads the room as below
    band and heats it indefinitely -- while `sensor_available` stayed True, so
    nothing alerted. It is the one dropout shape this entity would otherwise
    miss entirely.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "off"

    hass.states.async_set(ZONE_TEMP_ENTITY, "nan", {})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "on"


async def test_alert_stays_on_when_a_refresh_fails(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """A failed refresh must not hide an alarm that is already raised.

    The entity is a `CoordinatorEntity`, so availability follows the last
    refresh succeeding -- a read-only card would turn it `unavailable` rather
    than `on`, and an automation triggering `to: "on"` would then never fire, at
    exactly the moment the zone has stopped controlling.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    import custom_components.comfort_band.coordinator as coord_mod

    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(ZONE_TEMP_ENTITY, "unavailable", {})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "on"

    real = coord_mod.predictor.estimate_slopes

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("read-only file system")

    coord_mod.predictor.estimate_slopes = _boom
    try:
        coordinator = next(iter(hass.data[DOMAIN].zone_coordinators.values()))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.last_update_success is False
        # The alarm must survive, not become `unavailable`.
        assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "on"
    finally:
        coord_mod.predictor.estimate_slopes = real


async def test_alert_does_not_claim_health_from_a_stale_snapshot(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    """A stale `off` is worse than `unavailable`.

    `data` is only replaced on a successful refresh, so if refreshes start
    failing while the sensor is still healthy, the snapshot keeps saying
    `sensor_available=True`. Reporting that as `off` would assert "no problem"
    about a room that has since gone dark.
    """
    import custom_components.comfort_band.coordinator as coord_mod

    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state == "off"

    real = coord_mod.predictor.estimate_slopes

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("read-only file system")

    coord_mod.predictor.estimate_slopes = _boom
    try:
        coordinator = next(iter(hass.data[DOMAIN].zone_coordinators.values()))
        # Refreshes break first, while the sensor still looks fine...
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        # ...and only then does the sensor actually die.
        hass.states.async_set(ZONE_TEMP_ENTITY, "unavailable", {})
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.last_update_success is False
        assert hass.states.get("binary_sensor.office_room_sensor_unavailable").state != "off"
    finally:
        coord_mod.predictor.estimate_slopes = real


async def test_setup_does_not_truncate_the_learned_history(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    make_zone_entry: Any,
    freezer: FrozenDateTimeFactory,
) -> None:
    """`async_setup` must hydrate the sample buffer before its first refresh.

    `_append_sample` persists the whole list, and a freshly built coordinator
    has no persist throttle yet, so its first append writes immediately. Refresh
    before restoring the buffer and that write truncates the stored history to
    one sample -- the learned thermal model wiped on every restart, and
    `mpc.is_ready` permanently False.

    Only reachable through a real config entry, because
    `async_config_entry_first_refresh` requires one; the coordinator-level tests
    call `subscribe_and_hydrate` directly and so cannot see the ordering at all.
    """
    from datetime import timedelta

    from homeassistant.core import ServiceCall
    from homeassistant.util import dt as dt_util

    from custom_components.comfort_band.const import DOMAIN as CB_DOMAIN
    from custom_components.comfort_band.storage import ComfortBandStore

    async def _noop(call: ServiceCall) -> None:
        return None

    for service in ("set_hvac_mode", "set_temperature", "set_fan_mode"):
        hass.services.async_register("climate", service, _noop)

    freezer.move_to("2026-07-30 12:00:00+00:00")

    # A zone with a learned history, exactly as a restart would find on disk.
    seed_store = ComfortBandStore(hass)
    await seed_store.async_load()
    await seed_store.async_add_zone("office")
    await seed_store.async_update_zone(
        "office",
        enabled=True,
        samples=[
            {
                "t": (dt_util.utcnow() - timedelta(minutes=n)).isoformat(),
                "temp": 20.0 + n * 0.1,
                "action": "idle",
                "fan_mode": None,
            }
            for n in range(8, 0, -1)
        ],
    )
    seeded = len(seed_store.get_zone("office")["samples"])
    assert seeded == 8

    entry = make_zone_entry(temp_sensor=ZONE_TEMP_ENTITY)
    entry.add_to_hass(hass)
    hass.states.async_set("climate.office_hvac", "fan_only", {})
    hass.states.async_set(ZONE_TEMP_ENTITY, "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    kept = len(hass.data[CB_DOMAIN].store.get_zone("office")["samples"])
    assert kept >= seeded, f"setup truncated the learned history: {seeded} -> {kept}"
