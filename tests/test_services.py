"""Tests for the `comfort_band.*` services."""

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
        "create_profile",
        "clone_profile",
        "rename_profile",
        "delete_profile",
        "record_feedback",
        "create_shared_schedule",
        "rename_shared_schedule",
        "delete_shared_schedule",
        "assign_schedule",
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


# ----- profile CRUD services -----


async def test_create_profile_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "create_profile",
        {"name": "weekend", "description": "Saturdays + Sundays"},
        blocking=True,
    )
    registry = hass.data[DOMAIN].profile_registry
    assert "weekend" in registry.names
    assert registry.description("weekend") == "Saturdays + Sundays"


async def test_create_profile_blank_name_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="cannot be empty"):
        await hass.services.async_call(DOMAIN, "create_profile", {"name": "   "}, blocking=True)


async def test_create_profile_duplicate_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="already exists"):
        await hass.services.async_call(DOMAIN, "create_profile", {"name": "home"}, blocking=True)


async def test_clone_profile_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    # Seed a schedule on home so the clone has something to copy.
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "zone": "office",
            "profile": "home",
            "transitions": [{"at": "06:00", "low": 20.0, "high": 23.0}],
        },
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "clone_profile",
        {"source": "home", "target": "weekend"},
        blocking=True,
    )
    store = hass.data[DOMAIN].store
    cloned = store.get_zone_schedule("office", "weekend")
    assert cloned is not None
    assert cloned["baseline"][0]["at"] == "06:00"


async def test_clone_profile_unknown_source_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "clone_profile",
            {"source": "ghost", "target": "new"},
            blocking=True,
        )


async def test_clone_profile_duplicate_target_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="already exists"):
        await hass.services.async_call(
            DOMAIN,
            "clone_profile",
            {"source": "home", "target": "away"},
            blocking=True,
        )


async def test_rename_profile_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "rename_profile",
        {"old": "away", "new": "trip"},
        blocking=True,
    )
    registry = hass.data[DOMAIN].profile_registry
    assert "trip" in registry.names
    assert "away" not in registry.names


async def test_rename_profile_blank_new_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="cannot be empty"):
        await hass.services.async_call(
            DOMAIN,
            "rename_profile",
            {"old": "away", "new": "   "},
            blocking=True,
        )


async def test_rename_profile_unknown_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "rename_profile",
            {"old": "ghost", "new": "new"},
            blocking=True,
        )


async def test_delete_profile_service(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    await hass.services.async_call(DOMAIN, "create_profile", {"name": "vacation"}, blocking=True)
    await hass.services.async_call(DOMAIN, "delete_profile", {"name": "vacation"}, blocking=True)
    registry = hass.data[DOMAIN].profile_registry
    assert "vacation" not in registry.names


async def test_delete_default_profile_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="Cannot delete the default profile"):
        await hass.services.async_call(DOMAIN, "delete_profile", {"name": "home"}, blocking=True)


async def test_delete_unknown_profile_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    # Storage raises KeyError(name) consistent with the clone/rename
    # mutators; the service catches (KeyError, ValueError) and wraps as
    # ServiceValidationError. The name is echoed in the wrapped message.
    with pytest.raises(ServiceValidationError, match="ghost"):
        await hass.services.async_call(DOMAIN, "delete_profile", {"name": "ghost"}, blocking=True)


async def test_create_profile_name_length_capped(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    # voluptuous Length validator raises MultipleInvalid before our handler
    # ever sees the value; HA wraps that into ServiceValidationError.
    from voluptuous import MultipleInvalid

    with pytest.raises((MultipleInvalid, ServiceValidationError)):
        await hass.services.async_call(DOMAIN, "create_profile", {"name": "x" * 65}, blocking=True)


async def test_create_profile_at_cap_raises_via_service_layer(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """The cap is enforced at the storage layer; verify the error
    propagates through the service handler as ServiceValidationError so
    the card surfaces it via the existing error-handling path."""
    from custom_components.comfort_band.const import MAX_PROFILES

    # Fill up to cap (2 builtins already exist).
    for i in range(MAX_PROFILES - 2):
        await hass.services.async_call(DOMAIN, "create_profile", {"name": f"p{i}"}, blocking=True)
    with pytest.raises(ServiceValidationError, match=f"more than {MAX_PROFILES}"):
        await hass.services.async_call(
            DOMAIN, "create_profile", {"name": "one_too_many"}, blocking=True
        )


async def test_clone_profile_at_cap_raises_via_service_layer(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Symmetric to test_create_profile_at_cap — clone hits the same
    storage-level cap and must surface ServiceValidationError."""
    from custom_components.comfort_band.const import MAX_PROFILES

    for i in range(MAX_PROFILES - 2):
        await hass.services.async_call(DOMAIN, "create_profile", {"name": f"p{i}"}, blocking=True)
    with pytest.raises(ServiceValidationError, match=f"more than {MAX_PROFILES}"):
        await hass.services.async_call(
            DOMAIN,
            "clone_profile",
            {"source": "home", "target": "extra"},
            blocking=True,
        )


async def test_clone_profile_empty_source_raises_clear_error(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="Source profile name cannot be empty"):
        await hass.services.async_call(
            DOMAIN,
            "clone_profile",
            {"source": "   ", "target": "weekend"},
            blocking=True,
        )


async def test_rename_profile_empty_old_raises_clear_error(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="Old profile name cannot be empty"):
        await hass.services.async_call(
            DOMAIN, "rename_profile", {"old": "  ", "new": "trip"}, blocking=True
        )


async def test_import_legacy_writes_to_default_after_home_rename(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Importer should follow the renamed `home` (default_profile), not the
    literal "home" string. Regression guard for the rename-aware fallback."""
    _seed_legacy(hass, "office", 20.0, 23.0)
    # Rename home → weekday before importing.
    await hass.services.async_call(
        DOMAIN, "rename_profile", {"old": "home", "new": "weekday"}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN,
        "import_legacy",
        {"zone": "office", "source_zone_name": "office"},
        blocking=True,
    )
    store = hass.data[DOMAIN].store
    # Schedule landed on the renamed default, not on the now-absent "home".
    assert store.get_zone_schedule("office", "weekday") is not None
    assert store.get_zone_schedule("office", "home") is None


# ----- comfort feedback (v0.11.0) -----


async def test_record_feedback_persists_enriched_entry(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """record_feedback appends one entry enriched from the live coordinator
    state (room temp, effective band, current action) plus a timestamp."""
    await hass.services.async_call(
        DOMAIN,
        "record_feedback",
        {"zone": "office", "label": "just_right"},
        blocking=True,
    )
    data = hass.data[DOMAIN]
    coordinator = data.zone_coordinators[data.zone_slug_to_entry_id["office"]]
    state = coordinator.data
    assert state is not None  # setup_zone ran a refresh

    entries = data.feedback_store.get_entries("office")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["zone"] == "office"
    assert entry["label"] == "just_right"
    assert entry["room_temp"] == state.room
    assert entry["low"] == state.effective_low
    assert entry["high"] == state.effective_high
    assert entry["action"] == state.decision.action
    assert entry["timestamp"]  # ISO timestamp present


async def test_record_feedback_unknown_zone_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="Unknown zone"):
        await hass.services.async_call(
            DOMAIN,
            "record_feedback",
            {"zone": "nope", "label": "too_hot"},
            blocking=True,
        )


async def test_record_feedback_invalid_label_rejected(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "record_feedback",
            {"zone": "office", "label": "meh"},
            blocking=True,
        )
    # A schema-rejected call must not persist anything.
    assert hass.data[DOMAIN].feedback_store.get_entries("office") == []


# ----- v0.14.0 shared schedules -----


async def test_create_assign_and_edit_shared_schedule(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    store = hass.data[DOMAIN].store
    await hass.services.async_call(
        DOMAIN, "create_shared_schedule", {"name": "Bedrooms"}, blocking=True
    )
    sid = hass.data[DOMAIN].shared_schedule_registry.id_for("Bedrooms")
    assert sid is not None

    await hass.services.async_call(
        DOMAIN, "assign_schedule", {"zone": "office", "shared_id": sid}, blocking=True
    )
    assert store.get_zone("office")["schedule_id"] == sid

    # set_schedule targeting the shared id writes the SHARED schedule, not the
    # zone's own.
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "shared_id": sid,
            "profile": "home",
            "transitions": [{"at": "06:00", "low": 21.0, "high": 24.0}],
        },
        blocking=True,
    )
    assert store.get_shared_schedule_slot(sid, "home")["current"][0]["low"] == 21.0
    assert store.get_zone_schedule("office", "home") is None  # own schedule untouched

    # Clear assignment back to own.
    await hass.services.async_call(DOMAIN, "assign_schedule", {"zone": "office"}, blocking=True)
    assert store.get_zone("office")["schedule_id"] is None


async def test_set_schedule_requires_exactly_one_target(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    # Neither zone nor shared_id.
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await hass.services.async_call(
            DOMAIN, "set_schedule", {"profile": "home", "transitions": []}, blocking=True
        )
    # Both.
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"zone": "office", "shared_id": "x", "profile": "home", "transitions": []},
            blocking=True,
        )


async def test_set_schedule_unknown_shared_id_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="Unknown shared schedule"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"shared_id": "ghost", "profile": "home", "transitions": []},
            blocking=True,
        )


async def test_delete_shared_schedule_refuses_then_cascades(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    store = hass.data[DOMAIN].store
    await hass.services.async_call(
        DOMAIN, "create_shared_schedule", {"name": "Bedrooms"}, blocking=True
    )
    sid = hass.data[DOMAIN].shared_schedule_registry.id_for("Bedrooms")
    await hass.services.async_call(
        DOMAIN, "assign_schedule", {"zone": "office", "shared_id": sid}, blocking=True
    )
    # Refuse while assigned.
    with pytest.raises(ServiceValidationError, match="assigned"):
        await hass.services.async_call(
            DOMAIN, "delete_shared_schedule", {"shared_id": sid}, blocking=True
        )
    # Cascade unassigns + deletes.
    await hass.services.async_call(
        DOMAIN, "delete_shared_schedule", {"shared_id": sid, "cascade": True}, blocking=True
    )
    assert not store.has_shared_schedule(sid)
    assert store.get_zone("office")["schedule_id"] is None


async def test_assign_schedule_unknown_shared_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    with pytest.raises(ServiceValidationError, match="does not exist"):
        await hass.services.async_call(
            DOMAIN, "assign_schedule", {"zone": "office", "shared_id": "ghost"}, blocking=True
        )
