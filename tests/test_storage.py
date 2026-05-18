"""Tests for ComfortBandStore (persistence)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.comfort_band.const import SIGNAL_ZONE_SCHEDULE_CHANGED
from custom_components.comfort_band.storage import ComfortBandStore

# ----- defaults -----


async def test_first_load_returns_default_skeleton(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    assert store.list_zones() == []
    assert store.list_profiles() == ["away", "home"]
    assert store.active_profile == "home"
    assert store.default_profile == "home"


async def test_default_zone_has_sane_initial_values(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    zone = await store.async_add_zone("office")
    assert zone["enabled"] is False  # shadow-mode default
    assert zone["override_until"] is None
    assert zone["last_action"] is None
    assert zone["previous_action"] is None
    assert zone["deadband_below"] == 0.3
    assert zone["deadband_above"] == 0.5
    assert zone["min_cycle_minutes"] == 8
    assert zone["cross_mode_min_minutes"] == 8
    assert zone["override_hours"] == 3
    assert zone["manual_low"] < zone["manual_high"]
    assert zone["schedules"] == {}


# ----- round-trip persistence -----


async def test_zone_round_trips_across_store_instances(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    first = ComfortBandStore(hass)
    await first.async_load()
    await first.async_add_zone("office")
    await first.async_update_zone("office", enabled=True, manual_low=21.5)

    second = ComfortBandStore(hass)
    await second.async_load()
    zone = second.get_zone("office")
    assert zone["enabled"] is True
    assert zone["manual_low"] == 21.5


async def test_active_profile_persists(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    first = ComfortBandStore(hass)
    await first.async_load()
    await first.async_set_active_profile("away")

    second = ComfortBandStore(hass)
    await second.async_load()
    assert second.active_profile == "away"


# ----- deep-copy isolation -----


async def test_get_zone_returns_independent_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")

    zone = store.get_zone("office")
    zone["manual_low"] = 99.0
    zone["schedules"]["home"] = {"baseline": [], "current": []}

    fresh = store.get_zone("office")
    assert fresh["manual_low"] != 99.0
    assert fresh["schedules"] == {}


async def test_data_property_is_deep_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")

    snapshot = store.data
    snapshot["zones"]["office"]["enabled"] = True
    snapshot["active_profile"] = "away"

    assert store.get_zone("office")["enabled"] is False
    assert store.active_profile == "home"


# ----- mutator validation -----


async def test_add_zone_rejects_duplicate(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    with pytest.raises(ValueError, match="already exists"):
        await store.async_add_zone("office")


async def test_update_zone_rejects_unknown_field(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    with pytest.raises(KeyError, match="Unknown zone field: bogus"):
        await store.async_update_zone("office", bogus=42)


async def test_set_active_profile_rejects_unknown(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="does not exist"):
        await store.async_set_active_profile("vacation")


async def test_remove_default_profile_refused(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="Cannot delete the default profile"):
        await store.async_remove_profile("home")


async def test_remove_active_profile_falls_back_to_home(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_set_active_profile("away")
    assert store.active_profile == "away"
    await store.async_remove_profile("away")
    assert store.active_profile == "home"


# ----- schedules -----


async def test_set_zone_schedule_round_trip(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    baseline = [{"at": "06:00", "low": 20.0, "high": 23.0}]
    await store.async_set_zone_schedule("office", "home", baseline)
    schedule = store.get_zone_schedule("office", "home")
    assert schedule is not None
    assert schedule["baseline"] == baseline
    assert schedule["current"] == baseline


async def test_set_zone_schedule_unknown_profile_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    with pytest.raises(ValueError, match="Profile 'vacation' does not exist"):
        await store.async_set_zone_schedule("office", "vacation", [])


async def test_set_zone_schedule_independent_lists(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    baseline = [{"at": "06:00", "low": 20.0, "high": 23.0}]
    await store.async_set_zone_schedule("office", "home", baseline)
    # Mutate the source list after persisting; persisted copy must be unaffected.
    baseline[0]["low"] = 99.0
    schedule = store.get_zone_schedule("office", "home")
    assert schedule is not None
    assert schedule["baseline"][0]["low"] == 20.0


# ----- profile CRUD: clone / rename / migration -----


async def test_get_profile_returns_deep_copy(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    profile = store.get_profile("home")
    profile["description"] = "tampered"
    assert store.get_profile("home")["description"] != "tampered"


async def test_get_profile_unknown_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(KeyError):
        store.get_profile("vacation")


async def test_clone_profile_copies_per_zone_schedules(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    baseline = [{"at": "06:00", "low": 20.0, "high": 23.0}]
    await store.async_set_zone_schedule("office", "home", baseline)

    await store.async_clone_profile("home", "weekend", "Saturdays + Sundays")

    assert "weekend" in store.list_profiles()
    assert store.get_profile("weekend")["description"] == "Saturdays + Sundays"
    cloned = store.get_zone_schedule("office", "weekend")
    assert cloned is not None
    assert cloned["baseline"] == baseline
    # Mutate the source schedule; cloned must be independent.
    await store.async_set_zone_schedule(
        "office", "home", [{"at": "07:00", "low": 21.0, "high": 22.0}]
    )
    cloned_after = store.get_zone_schedule("office", "weekend")
    assert cloned_after is not None
    assert cloned_after["baseline"] == baseline


async def test_clone_to_existing_target_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="already exists"):
        await store.async_clone_profile("home", "away")


async def test_clone_from_unknown_source_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(KeyError):
        await store.async_clone_profile("ghost", "new")


async def test_rename_profile_renames_in_profiles_and_zones(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_set_zone_schedule(
        "office", "away", [{"at": "06:00", "low": 18.0, "high": 21.0}]
    )

    await store.async_rename_profile("away", "trip")

    assert "trip" in store.list_profiles()
    assert "away" not in store.list_profiles()
    assert store.get_profile("trip")["name"] == "trip"
    # Schedule key followed the rename.
    assert store.get_zone_schedule("office", "trip") is not None
    assert store.get_zone_schedule("office", "away") is None


async def test_rename_active_profile_updates_active_pointer(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_set_active_profile("away")
    await store.async_rename_profile("away", "trip")
    assert store.active_profile == "trip"


async def test_rename_default_profile_updates_default_pointer(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_rename_profile("home", "weekday")
    assert store.default_profile == "weekday"
    # And deletion of the new default name is now refused.
    with pytest.raises(ValueError, match="Cannot delete the default profile"):
        await store.async_remove_profile("weekday")


async def test_rename_to_existing_name_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(ValueError, match="already exists"):
        await store.async_rename_profile("away", "home")


async def test_rename_unknown_raises(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    with pytest.raises(KeyError):
        await store.async_rename_profile("ghost", "new")


async def test_rename_noop_returns_quickly(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_rename_profile("home", "home")  # no-op
    assert store.list_profiles() == ["away", "home"]


async def test_remove_profile_strips_zone_schedules(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    await store.async_set_zone_schedule(
        "office", "away", [{"at": "06:00", "low": 18.0, "high": 21.0}]
    )
    assert store.get_zone_schedule("office", "away") is not None
    await store.async_remove_profile("away")
    # Orphan schedule cleaned up.
    assert store.get_zone_schedule("office", "away") is None


async def test_remove_profile_after_default_rename_uses_new_name(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Rename of `home` to e.g. `weekday` should leave `weekday` undeletable
    (because it's still the default). The literal string "home" is no longer
    privileged after a rename."""
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_rename_profile("home", "weekday")
    # Re-create a profile named "home" — should now be deletable, since it
    # is no longer the default.
    await store.async_add_profile("home", "user-recreated")
    await store.async_remove_profile("home")
    assert "home" not in store.list_profiles()


async def test_load_legacy_data_without_default_profile_migrates(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A v0.1 payload (no `default_profile` key) should migrate to `home`."""
    hass_storage["comfort_band.data"] = {
        "version": 1,
        "data": {
            "zones": {},
            "profiles": {
                "home": {"name": "home", "description": ""},
                "away": {"name": "away", "description": ""},
                "sleep": {"name": "sleep", "description": "Overnight schedule."},
            },
            "active_profile": "home",
            # NB: no `default_profile` key.
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()
    assert store.default_profile == "home"
    # The legacy "sleep" profile survives as a normal user profile.
    assert "sleep" in store.list_profiles()
    # And is deletable now (no longer a built-in, not the default).
    await store.async_remove_profile("sleep")
    assert "sleep" not in store.list_profiles()


async def test_load_legacy_data_without_home_uses_first_alphabetical(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """If `home` doesn't exist on legacy load, fall back to the first
    alphabetical profile as the new default."""
    hass_storage["comfort_band.data"] = {
        "version": 1,
        "data": {
            "zones": {},
            "profiles": {
                "zulu": {"name": "zulu", "description": ""},
                "alpha": {"name": "alpha", "description": ""},
            },
            "active_profile": "zulu",
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()
    assert store.default_profile == "alpha"


async def test_load_legacy_data_with_empty_profiles_reseeds_builtins(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """If the legacy payload has an empty profiles dict (corruption case),
    reseed the built-ins and set default_profile to home."""
    hass_storage["comfort_band.data"] = {
        "version": 1,
        "data": {
            "zones": {},
            "profiles": {},
            "active_profile": "home",
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()
    assert store.default_profile == "home"
    assert set(store.list_profiles()) == {"home", "away"}


async def test_load_legacy_v0_3_zone_backfills_new_fields(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A v0.3 zone payload (no `learning_enabled` / `use_apparent_temperature`
    yet) must backfill both fields with `False` on load — otherwise any
    later `async_update_zone(..., learning_enabled=...)` would KeyError.
    """
    hass_storage["comfort_band.data"] = {
        "version": 1,
        "data": {
            "zones": {
                "office": {
                    "zone_name": "office",
                    "schedules": {},
                    "manual_low": 19.5,
                    "manual_high": 22.5,
                    "override_hours": 3,
                    "override_until": None,
                    "deadband_below": 0.3,
                    "deadband_above": 0.5,
                    "min_cycle_minutes": 8,
                    "enabled": False,
                    # NB: no learning_enabled / use_apparent_temperature.
                    "last_action_at": None,
                    "last_action": None,
                }
            },
            "profiles": {
                "home": {"name": "home", "description": ""},
                "away": {"name": "away", "description": ""},
            },
            "active_profile": "home",
            "default_profile": "home",
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()
    zone = store.get_zone("office")
    assert zone["learning_enabled"] is False
    assert zone["use_apparent_temperature"] is False
    # The v0.4 → v0.5 backfill ran on the same load and defaulted
    # cross_mode_min_minutes from the existing min_cycle_minutes (8).
    # `previous_action` is added at the same time and starts unset.
    assert zone["cross_mode_min_minutes"] == 8
    assert zone["previous_action"] is None
    # And update_zone now works on the new field without KeyError.
    await store.async_update_zone("office", learning_enabled=True)
    assert store.get_zone("office")["learning_enabled"] is True


async def test_load_legacy_v0_4_zone_backfills_cross_mode_from_min_cycle(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A v0.4 zone payload (has learning_enabled / use_apparent_temperature
    but no `cross_mode_min_minutes`) must backfill the new field from the
    zone's own min_cycle_minutes — preserving any custom tuning the user
    had applied to same-mode dwell."""
    hass_storage["comfort_band.data"] = {
        "version": 1,
        "data": {
            "zones": {
                "office": {
                    "zone_name": "office",
                    "schedules": {},
                    "manual_low": 19.5,
                    "manual_high": 22.5,
                    "override_hours": 3,
                    "override_until": None,
                    "deadband_below": 0.3,
                    "deadband_above": 0.5,
                    # User had tuned same-mode down to 4 min; cross-mode
                    # should inherit that, not the system default of 8.
                    "min_cycle_minutes": 4,
                    "enabled": False,
                    "learning_enabled": False,
                    "use_apparent_temperature": False,
                    # NB: no cross_mode_min_minutes.
                    "last_action_at": None,
                    "last_action": None,
                }
            },
            "profiles": {
                "home": {"name": "home", "description": ""},
                "away": {"name": "away", "description": ""},
            },
            "active_profile": "home",
            "default_profile": "home",
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()
    zone = store.get_zone("office")
    assert zone["cross_mode_min_minutes"] == 4  # inherited from min_cycle_minutes
    assert zone["previous_action"] is None  # also backfilled in the same pass
    # And update_zone now works on the new field without KeyError.
    await store.async_update_zone("office", cross_mode_min_minutes=15)
    assert store.get_zone("office")["cross_mode_min_minutes"] == 15


async def test_clone_profile_without_source_schedule_creates_empty_target(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Cloning a profile when the source has no schedule for a zone leaves
    the zone with no schedule for the target either — not an error."""
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")
    # No schedule seeded on "home" for office.
    await store.async_clone_profile("home", "weekend")
    assert "weekend" in store.list_profiles()
    assert store.get_zone_schedule("office", "weekend") is None


async def test_add_profile_count_cap(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Cap on total profile count guards against unbounded growth of the
    .storage file from a misbehaving caller."""
    from custom_components.comfort_band.const import MAX_PROFILES

    store = ComfortBandStore(hass)
    await store.async_load()
    # Built-ins already use 2 slots; fill up to MAX_PROFILES.
    for i in range(MAX_PROFILES - 2):
        await store.async_add_profile(f"p{i}")
    assert len(store.list_profiles()) == MAX_PROFILES
    with pytest.raises(ValueError, match=f"more than {MAX_PROFILES}"):
        await store.async_add_profile("one_too_many")


async def test_clone_profile_count_cap(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    from custom_components.comfort_band.const import MAX_PROFILES

    store = ComfortBandStore(hass)
    await store.async_load()
    for i in range(MAX_PROFILES - 2):
        await store.async_add_profile(f"p{i}")
    with pytest.raises(ValueError, match=f"more than {MAX_PROFILES}"):
        await store.async_clone_profile("home", "extra")


async def test_set_zone_schedule_fires_signal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Every persisted schedule write must dispatch SIGNAL_ZONE_SCHEDULE_CHANGED."""
    store = ComfortBandStore(hass)
    await store.async_load()
    await store.async_add_zone("office")

    received: list[tuple[str, str, Any]] = []
    unsub = async_dispatcher_connect(
        hass,
        SIGNAL_ZONE_SCHEDULE_CHANGED,
        lambda zone, profile, schedule: received.append((zone, profile, schedule)),
    )
    try:
        baseline = [{"at": "06:00", "low": 20.0, "high": 23.0}]
        await store.async_set_zone_schedule("office", "home", baseline)
        await hass.async_block_till_done()
    finally:
        unsub()

    assert len(received) == 1
    zone, profile, schedule = received[0]
    assert zone == "office"
    assert profile == "home"
    assert schedule is not None
    assert schedule["baseline"] == baseline
    assert schedule["current"] == baseline
