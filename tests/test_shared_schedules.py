"""v0.14.0 named shared schedules: storage CRUD + SharedScheduleRegistry."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.comfort_band.const import (
    MAX_SHARED_SCHEDULES,
    SIGNAL_SHARED_SCHEDULE_CHANGED,
    SIGNAL_SHARED_SCHEDULE_LIST_CHANGED,
)
from custom_components.comfort_band.shared_schedules import SharedScheduleRegistry
from custom_components.comfort_band.storage import ComfortBandStore


def _slot(low: float = 20.0, high: float = 23.0) -> dict[str, Any]:
    t = [{"at": "06:00", "low": low, "high": high}]
    return {"baseline": list(t), "current": list(t)}


async def _loaded_store(hass: HomeAssistant) -> ComfortBandStore:
    store = ComfortBandStore(hass)
    await store.async_load()
    return store


async def test_default_data_and_zone_have_no_shared_schedule(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    assert store.list_shared_schedule_ids() == []
    zone = await store.async_add_zone("office")
    assert zone["schedule_id"] is None


async def test_create_generates_slug_id_and_refuses_name_collision(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    sid = await store.async_add_shared_schedule("Bedrooms")
    assert sid == "bedrooms"
    # Same name (case-insensitive) -> refused.
    with pytest.raises(ValueError, match="already exists"):
        await store.async_add_shared_schedule("bedrooms")
    # Distinct name that slugifies to the same base -> suffixed id, no collision.
    sid2 = await store.async_add_shared_schedule("Bedrooms!")
    assert sid2 == "bedrooms_2"


async def test_seed_from_schedules_deep_copies(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    seed = {"home": _slot(19.0, 21.0), "away": _slot(16.0, 24.0)}
    sid = await store.async_add_shared_schedule("Living", seed)
    got = store.get_shared_schedule(sid)
    assert got["name"] == "Living"
    assert got["schedules"]["home"]["current"][0]["low"] == 19.0
    # Deep copy: mutating the seed (or the returned copy) doesn't bleed in.
    seed["home"]["current"][0]["low"] = 99.0
    assert store.get_shared_schedule_slot(sid, "home")["current"][0]["low"] == 19.0


async def test_rename_is_pure_name_update_keeping_assignments(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    sid = await store.async_add_shared_schedule("Bedrooms")
    await store.async_add_zone("nate")
    await store.async_set_zone_schedule_id("nate", sid)
    await store.async_rename_shared_schedule(sid, "Kids rooms")
    # id unchanged -> nate still points at it.
    assert store.get_zone("nate")["schedule_id"] == sid
    assert store.get_shared_schedule_name(sid) == "Kids rooms"
    # Rename collision refused.
    await store.async_add_shared_schedule("Living")
    with pytest.raises(ValueError, match="already exists"):
        await store.async_rename_shared_schedule(sid, "living")


async def test_remove_unassigns_referencing_zones_and_returns_them(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    sid = await store.async_add_shared_schedule("Bedrooms")
    for z in ("nate", "zach"):
        await store.async_add_zone(z)
        await store.async_set_zone_schedule_id(z, sid)
    assert store.zones_using_shared_schedule(sid) == ["nate", "zach"]
    affected = await store.async_remove_shared_schedule(sid)
    assert affected == ["nate", "zach"]
    assert not store.has_shared_schedule(sid)
    assert store.get_zone("nate")["schedule_id"] is None
    assert store.get_zone("zach")["schedule_id"] is None


async def test_set_zone_schedule_id_validates(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    await store.async_add_zone("office")
    with pytest.raises(ValueError, match="does not exist"):
        await store.async_set_zone_schedule_id("office", "ghost")
    # None always allowed (own schedule).
    await store.async_set_zone_schedule_id("office", None)
    assert store.get_zone("office")["schedule_id"] is None


async def test_async_set_shared_schedule_fires_signal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    sid = await store.async_add_shared_schedule("Bedrooms")
    events: list[tuple[str, str, Any]] = []
    async_dispatcher_connect(
        hass,
        SIGNAL_SHARED_SCHEDULE_CHANGED,
        lambda i, p, s: events.append((i, p, s)),
    )
    await store.async_set_shared_schedule(sid, "home", [{"at": "06:00", "low": 20.0, "high": 23.0}])
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0][0] == sid
    assert events[0][1] == "home"
    assert events[0][2]["current"][0]["low"] == 20.0


async def test_summaries_carry_members(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    store = await _loaded_store(hass)
    bed = await store.async_add_shared_schedule("Bedrooms")
    await store.async_add_shared_schedule("Living")
    await store.async_add_zone("nate")
    await store.async_set_zone_schedule_id("nate", bed)
    summaries = store.shared_schedule_summaries()
    by_id = {s["id"]: s for s in summaries}
    assert by_id[bed]["name"] == "Bedrooms"
    assert by_id[bed]["members"] == ["nate"]
    # Sorted by name.
    assert [s["name"] for s in summaries] == ["Bedrooms", "Living"]


async def test_round_trips_across_store_instances(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    first = ComfortBandStore(hass)
    await first.async_load()
    sid = await first.async_add_shared_schedule("Bedrooms", {"home": _slot()})
    await first.async_add_zone("nate")
    await first.async_set_zone_schedule_id("nate", sid)

    second = ComfortBandStore(hass)
    await second.async_load()
    assert second.has_shared_schedule(sid)
    assert second.get_shared_schedule_name(sid) == "Bedrooms"
    assert second.get_zone("nate")["schedule_id"] == sid


async def test_backfill_adds_keys_to_legacy_store(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    # A v0.13 payload: a zone without `schedule_id`, top-level without
    # `shared_schedules`.
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
                    "cross_mode_min_minutes": 8,
                    "previous_action": None,
                    "samples": [],
                    "lookahead_minutes": 5,
                    "passive_tolerance": 0.5,
                    "mpc_enabled": False,
                    "mpc_horizon_minutes": 60,
                    "band_ramp_minutes": 0,
                    "enabled": False,
                    "learning_enabled": False,
                    "use_apparent_temperature": False,
                    "last_action_at": None,
                    "last_action": None,
                    "persisted_idle_slope": None,
                    "persisted_idle_slope_at": None,
                    "fan_control_enabled": False,
                    "active_fan_mode": None,
                    "idle_fan_mode": None,
                    # no schedule_id
                }
            },
            "profiles": {"home": {"name": "home", "description": ""}},
            "active_profile": "home",
            "default_profile": "home",
            # no shared_schedules
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()
    assert store.list_shared_schedule_ids() == []  # top-level backfilled to {}
    assert store.get_zone("office")["schedule_id"] is None  # per-zone backfilled


async def test_registry_create_delete_fire_list_signal_and_refuse(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    registry = SharedScheduleRegistry(hass, store)
    list_events: list[None] = []
    async_dispatcher_connect(
        hass, SIGNAL_SHARED_SCHEDULE_LIST_CHANGED, lambda: list_events.append(None)
    )

    sid = await registry.async_create("Bedrooms")
    await hass.async_block_till_done()
    assert registry.names == ["Bedrooms"]
    assert registry.id_for("bedrooms") == sid  # case-insensitive
    assert len(list_events) == 1

    await store.async_add_zone("nate")
    await store.async_set_zone_schedule_id("nate", sid)
    # Refuse delete while assigned...
    with pytest.raises(ValueError, match="assigned"):
        await registry.async_delete(sid)
    # ...cascade unassigns + returns the affected zones.
    affected = await registry.async_delete(sid, cascade=True)
    await hass.async_block_till_done()
    assert affected == ["nate"]
    assert not registry.has(sid)
    assert store.get_zone("nate")["schedule_id"] is None


async def test_registry_create_seed_from_zone_deep_copies(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """`async_create(seed_from_zone=...)` copies that zone's own schedules, fully
    isolated — later edits to either side never bleed into the other."""
    store = await _loaded_store(hass)
    registry = SharedScheduleRegistry(hass, store)
    await store.async_add_zone("nate")
    await store.async_set_zone_schedule(
        "nate", "home", [{"at": "06:00", "low": 19.0, "high": 21.0}]
    )

    sid = await registry.async_create("Bedrooms", seed_from_zone="nate")
    assert store.get_shared_schedule_slot(sid, "home")["current"][0]["low"] == 19.0

    # Editing the shared schedule must NOT mutate nate's own schedule...
    await store.async_set_shared_schedule(sid, "home", [{"at": "06:00", "low": 30.0, "high": 31.0}])
    assert store.get_zone_schedule("nate", "home")["current"][0]["low"] == 19.0
    # ...and editing nate's own schedule must NOT mutate the shared one.
    await store.async_set_zone_schedule("nate", "home", [{"at": "06:00", "low": 5.0, "high": 6.0}])
    assert store.get_shared_schedule_slot(sid, "home")["current"][0]["low"] == 30.0


async def test_registry_create_unknown_seed_zone_raises(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = await _loaded_store(hass)
    registry = SharedScheduleRegistry(hass, store)
    with pytest.raises(ValueError, match="Unknown zone"):
        await registry.async_create("Bedrooms", seed_from_zone="ghost")
    # The failed create left nothing behind.
    assert registry.names == []


async def test_id_suffix_skips_already_taken_ids(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The slug-id suffixer steps over ids already in use, even when a distinct
    name happens to slugify onto a previously-suffixed id."""
    store = await _loaded_store(hass)
    assert await store.async_add_shared_schedule("Bedrooms 2") == "bedrooms_2"
    assert await store.async_add_shared_schedule("Bedrooms") == "bedrooms"
    # "Bedrooms!" slugifies to base "bedrooms" -> bedrooms taken -> _2 taken -> _3.
    assert await store.async_add_shared_schedule("Bedrooms!") == "bedrooms_3"


async def test_cap_enforced_at_max(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Exactly MAX_SHARED_SCHEDULES are allowed; the next create is refused."""
    store = await _loaded_store(hass)
    for i in range(MAX_SHARED_SCHEDULES):
        await store.async_add_shared_schedule(f"S{i}")
    assert len(store.list_shared_schedule_ids()) == MAX_SHARED_SCHEDULES
    with pytest.raises(ValueError, match="Cannot create more than"):
        await store.async_add_shared_schedule("one too many")


async def test_backfill_handles_partial_mixed_payload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A payload that ALREADY has shared_schedules + one zone carrying a
    schedule_id and another missing it must load without loss: the populated
    shared schedule survives, the missing per-zone key is backfilled to None,
    and the existing pointer is preserved."""

    def _zone(name: str, **extra: Any) -> dict[str, Any]:
        base = {
            "zone_name": name,
            "schedules": {},
            "manual_low": 19.5,
            "manual_high": 22.5,
            "override_hours": 3,
            "override_until": None,
            "deadband_below": 0.3,
            "deadband_above": 0.5,
            "min_cycle_minutes": 8,
            "cross_mode_min_minutes": 8,
            "previous_action": None,
            "samples": [],
            "lookahead_minutes": 5,
            "passive_tolerance": 0.5,
            "mpc_enabled": False,
            "mpc_horizon_minutes": 60,
            "band_ramp_minutes": 0,
            "enabled": False,
            "learning_enabled": False,
            "use_apparent_temperature": False,
            "last_action_at": None,
            "last_action": None,
            "persisted_idle_slope": None,
            "persisted_idle_slope_at": None,
            "fan_control_enabled": False,
            "active_fan_mode": None,
            "idle_fan_mode": None,
        }
        base.update(extra)
        return base

    hass_storage["comfort_band.data"] = {
        "version": 1,
        "data": {
            "zones": {
                # Already migrated: carries an explicit schedule_id pointer.
                "nate": _zone("nate", schedule_id="bedrooms"),
                # Legacy: no schedule_id key at all.
                "office": _zone("office"),
            },
            "profiles": {"home": {"name": "home", "description": ""}},
            "active_profile": "home",
            "default_profile": "home",
            "shared_schedules": {
                "bedrooms": {
                    "name": "Bedrooms",
                    "schedules": {
                        "home": {
                            "baseline": [{"at": "06:00", "low": 20.0, "high": 23.0}],
                            "current": [{"at": "06:00", "low": 20.0, "high": 23.0}],
                        }
                    },
                }
            },
        },
    }
    store = ComfortBandStore(hass)
    await store.async_load()

    # Populated shared schedule survived intact.
    assert store.get_shared_schedule_name("bedrooms") == "Bedrooms"
    assert store.get_shared_schedule_slot("bedrooms", "home")["current"][0]["low"] == 20.0
    # Existing pointer preserved; legacy zone backfilled to None.
    assert store.get_zone("nate")["schedule_id"] == "bedrooms"
    assert store.get_zone("office")["schedule_id"] is None
    assert store.zones_using_shared_schedule("bedrooms") == ["nate"]


async def test_name_is_normalised_and_reserved_label_refused(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Names are stripped (and blank/whitespace-only refused), and the reserved
    "Own schedule" sentinel label can't be used as a real schedule name (it would
    collide with the assignment select's unassigned option)."""
    store = await _loaded_store(hass)
    # Surrounding whitespace is trimmed before storing.
    sid = await store.async_add_shared_schedule("  Bedrooms  ")
    assert store.get_shared_schedule_name(sid) == "Bedrooms"
    # Blank / whitespace-only refused.
    with pytest.raises(ValueError, match="cannot be empty"):
        await store.async_add_shared_schedule("   ")
    # The reserved sentinel (case-insensitive) refused on create and rename.
    with pytest.raises(ValueError, match="reserved"):
        await store.async_add_shared_schedule("own schedule")
    with pytest.raises(ValueError, match="reserved"):
        await store.async_rename_shared_schedule(sid, "Own Schedule")
