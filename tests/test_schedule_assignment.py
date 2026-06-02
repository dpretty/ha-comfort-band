"""End-to-end tests for the v0.14.0 named-shared-schedule assignment.

Spins up two real zones via `make_zone_entry`, creates a shared schedule and
assigns rooms to it through HA's normal service / select paths, then proves the
headline guarantee: editing the shared schedule once moves *every* assigned
room's effective band together. Also covers the assignment select's attributes
and its dangling-id read-side coercion. Mirrors `test_fan_entities.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.comfort_band.const import DOMAIN, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED

OFFICE_TEMP = "sensor.office_temp"
BEDROOM_TEMP = "sensor.bedroom2_temp"


@pytest.fixture
async def two_zones(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> AsyncIterator[None]:
    """Two set-up zones (office + bedroom2) with independent temp sensors."""
    office = make_zone_entry(zone_name="office", temp_sensor=OFFICE_TEMP)
    bedroom = make_zone_entry(
        zone_name="bedroom2",
        climate_entity="climate.bedroom2_hvac",
        temp_sensor=BEDROOM_TEMP,
    )
    office.add_to_hass(hass)
    bedroom.add_to_hass(hass)
    hass.states.async_set(OFFICE_TEMP, "21.0", {})
    hass.states.async_set(BEDROOM_TEMP, "21.0", {})
    # Setting up one entry loads every pending entry of the domain (both zones).
    assert await hass.config_entries.async_setup(office.entry_id)
    await hass.async_block_till_done()
    yield None


async def _create_shared(hass: HomeAssistant, name: str) -> str:
    await hass.services.async_call(DOMAIN, "create_shared_schedule", {"name": name}, blocking=True)
    await hass.async_block_till_done()
    sid = hass.data[DOMAIN].shared_schedule_registry.id_for(name)
    assert sid is not None
    return sid


async def _assign(hass: HomeAssistant, zone: str, shared_id: str | None) -> None:
    data: dict[str, Any] = {"zone": zone}
    if shared_id is not None:
        data["shared_id"] = shared_id
    await hass.services.async_call(DOMAIN, "assign_schedule", data, blocking=True)
    await hass.async_block_till_done()


async def test_editing_shared_schedule_moves_every_assigned_room(
    hass: HomeAssistant, hass_storage: dict[str, Any], two_zones: None
) -> None:
    """The headline guarantee: assign two rooms to one shared schedule, edit it
    once, and BOTH rooms' effective bands follow in the same refresh — so
    open-door rooms stop fighting (one heating while the other cools)."""
    sid = await _create_shared(hass, "Bedrooms")
    await _assign(hass, "office", sid)
    await _assign(hass, "bedroom2", sid)

    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "shared_id": sid,
            "profile": "home",
            "transitions": [{"at": "00:00", "low": 21.0, "high": 24.0}],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    for zone in ("office", "bedroom2"):
        assert float(hass.states.get(f"sensor.{zone}_effective_low").state) == 21.0, zone
        assert float(hass.states.get(f"sensor.{zone}_effective_high").state) == 24.0, zone


async def test_assignment_select_state_and_attributes(
    hass: HomeAssistant, hass_storage: dict[str, Any], two_zones: None
) -> None:
    """Selecting a shared schedule sets the select state to its name and exposes
    schedule_id + a shared_schedules catalogue (with members) for the card."""
    sid = await _create_shared(hass, "Bedrooms")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.office_schedule_assignment", "option": "Bedrooms"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("select.office_schedule_assignment")
    assert state is not None
    assert state.state == "Bedrooms"
    assert state.attributes["schedule_id"] == sid
    assert state.attributes["options"] == ["Own schedule", "Bedrooms"]
    summaries = {s["id"]: s for s in state.attributes["shared_schedules"]}
    assert summaries[sid]["name"] == "Bedrooms"
    assert summaries[sid]["members"] == ["office"]
    # Persisted to the store.
    assert hass.data[DOMAIN].store.get_zone("office")["schedule_id"] == sid


async def test_assignment_select_defaults_to_own_schedule(
    hass: HomeAssistant, hass_storage: dict[str, Any], two_zones: None
) -> None:
    """An unassigned zone shows the 'Own schedule' sentinel and a null id."""
    state = hass.states.get("select.office_schedule_assignment")
    assert state is not None
    assert state.state == "Own schedule"
    assert state.attributes["schedule_id"] is None
    assert state.attributes["options"] == ["Own schedule"]


async def test_assignment_select_coerces_dangling_id_to_own(
    hass: HomeAssistant, hass_storage: dict[str, Any], two_zones: None
) -> None:
    """If the assigned shared schedule vanishes from under the zone (store
    corruption / out-of-band delete), the select shows 'Own schedule' and a null
    schedule_id rather than a stale name — read-side coercion."""
    sid = await _create_shared(hass, "Bedrooms")
    await _assign(hass, "office", sid)
    assert hass.states.get("select.office_schedule_assignment").state == "Bedrooms"

    # Forcibly drop the shared schedule WITHOUT unassigning the zone (the normal
    # delete refuses while referenced) to fabricate a genuine dangling id, then
    # nudge the select to re-render via the list signal.
    store = hass.data[DOMAIN].store
    store._data["shared_schedules"].pop(sid)
    async_dispatcher_send(hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED)
    await hass.async_block_till_done()

    state = hass.states.get("select.office_schedule_assignment")
    assert state.state == "Own schedule"  # coerced, not the stale "Bedrooms"
    assert state.attributes["schedule_id"] is None
    # The zone pointer is still dangling; band resolution (see test_coordinator)
    # falls back own->manual, so the room keeps working.
    assert store.get_zone("office")["schedule_id"] == sid
