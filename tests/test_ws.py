"""Tests for the `comfort_band/*` websocket commands.

We invoke the handler directly with a fake `ActiveConnection` instead of
spinning up the full websocket server — that path triggers a teardown
timing flake in `pytest_homeassistant_custom_component` on macOS, and
adds nothing testable beyond what the framework already guarantees
(schema validation + dispatch). The handler itself is plain Python.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.comfort_band.ws import ws_get_schedule, ws_subscribe_schedule


class _FakeConnection:
    """Minimal stand-in for `websocket_api.ActiveConnection`.

    Captures every send_* and tracks the `subscriptions` dict used by
    HA's WS framework for cleanup on disconnect.
    """

    def __init__(self) -> None:
        self.results: list[tuple[int, Any]] = []
        self.errors: list[tuple[int, str, str]] = []
        self.messages: list[dict[str, Any]] = []
        self.subscriptions: dict[int, Callable[[], None]] = {}

    def send_result(self, msg_id: int, data: Any = None) -> None:
        self.results.append((msg_id, data))

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors.append((msg_id, code, message))

    def send_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def async_handle_exception(self, msg: dict[str, Any], err: Exception) -> None:
        # Mirrors the real ActiveConnection contract: surface bugs as errors
        # rather than swallowing them so the test sees a failure.
        self.errors.append((msg["id"], "unhandled_exception", repr(err)))


@pytest.fixture
async def setup_zone(
    hass: HomeAssistant, hass_storage: dict[str, Any], make_zone_entry: Any
) -> None:
    entry = make_zone_entry(temp_sensor="sensor.office_temp")
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temp", "21.0", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_get_schedule_returns_null_for_unset_profile(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Newly created zones have no schedules — read returns None (=> null)."""
    conn = _FakeConnection()
    ws_get_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 1, "type": "comfort_band/get_schedule", "zone": "office", "profile": "home"},
    )
    assert conn.errors == []
    assert conn.results == [(1, None)]


async def test_get_schedule_returns_baseline_and_current_after_set(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """After comfort_band.set_schedule, the WS read sees the persisted data."""
    transitions = [
        {"at": "06:00", "low": 20.0, "high": 23.0},
        {"at": "22:00", "low": 18.0, "high": 21.0},
    ]
    await hass.services.async_call(
        "comfort_band",
        "set_schedule",
        {"zone": "office", "profile": "home", "transitions": transitions},
        blocking=True,
    )

    conn = _FakeConnection()
    ws_get_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 7, "type": "comfort_band/get_schedule", "zone": "office", "profile": "home"},
    )
    assert conn.errors == []
    msg_id, schedule = conn.results[0]
    assert msg_id == 7
    assert schedule is not None
    assert schedule["baseline"] == transitions
    assert schedule["current"] == transitions


async def test_get_schedule_errors_for_unknown_zone(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """An unknown zone surfaces a typed error rather than a result."""
    conn = _FakeConnection()
    ws_get_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 3, "type": "comfort_band/get_schedule", "zone": "ghost", "profile": "home"},
    )
    assert conn.results == []
    assert conn.errors == [(3, "zone_not_found", "Zone 'ghost' does not exist")]


async def test_command_is_registered_at_setup(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Sanity: async_register_ws_commands() runs and the commands are wired."""
    handlers = hass.data.get("websocket_api", {})
    assert "comfort_band/get_schedule" in handlers
    assert "comfort_band/subscribe_schedule" in handlers


# ----- subscribe_schedule -----


def _events_from(conn: _FakeConnection) -> list[Any]:
    """Extract the `event` payload from each event_message captured."""
    return [m["event"] for m in conn.messages if m.get("type") == "event"]


async def test_subscribe_sends_initial_value_then_updates(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Subscribe pushes the current schedule, then echoes every later write."""
    initial = [{"at": "06:00", "low": 20.0, "high": 23.0}]
    await hass.services.async_call(
        "comfort_band",
        "set_schedule",
        {"zone": "office", "profile": "home", "transitions": initial},
        blocking=True,
    )

    conn = _FakeConnection()
    ws_subscribe_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 11, "type": "comfort_band/subscribe_schedule", "zone": "office", "profile": "home"},
    )
    await hass.async_block_till_done()

    assert conn.errors == []
    assert conn.results == [(11, None)]
    events = _events_from(conn)
    assert len(events) == 1
    assert events[0]["schedule"]["baseline"] == initial
    assert 11 in conn.subscriptions

    updated = [
        {"at": "06:00", "low": 20.0, "high": 23.0},
        {"at": "22:00", "low": 18.0, "high": 21.0},
    ]
    await hass.services.async_call(
        "comfort_band",
        "set_schedule",
        {"zone": "office", "profile": "home", "transitions": updated},
        blocking=True,
    )
    await hass.async_block_till_done()

    events = _events_from(conn)
    assert len(events) == 2
    assert events[1]["schedule"]["baseline"] == updated


async def test_subscribe_filters_to_matching_zone_profile(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Updates for an unrelated (zone, profile) must not reach the subscriber."""
    conn = _FakeConnection()
    ws_subscribe_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 12, "type": "comfort_band/subscribe_schedule", "zone": "office", "profile": "home"},
    )
    await hass.async_block_till_done()
    baseline_count = len(_events_from(conn))

    # Write to a different profile on the same zone — must not echo through.
    await hass.services.async_call(
        "comfort_band",
        "set_schedule",
        {
            "zone": "office",
            "profile": "away",
            "transitions": [{"at": "06:00", "low": 17.0, "high": 20.0}],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert len(_events_from(conn)) == baseline_count


async def test_subscribe_unknown_zone_errors(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """An unknown zone surfaces a typed error and does not open a subscription."""
    conn = _FakeConnection()
    ws_subscribe_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 13, "type": "comfort_band/subscribe_schedule", "zone": "ghost", "profile": "home"},
    )
    await hass.async_block_till_done()

    assert conn.results == []
    assert conn.messages == []
    assert conn.subscriptions == {}
    assert conn.errors == [(13, "zone_not_found", "Zone 'ghost' does not exist")]


async def test_subscribe_unsubscribe_stops_updates(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_zone: None
) -> None:
    """Calling the stored unsubscribe handle detaches the dispatcher listener."""
    conn = _FakeConnection()
    ws_subscribe_schedule(
        hass,
        conn,  # type: ignore[arg-type]
        {"id": 14, "type": "comfort_band/subscribe_schedule", "zone": "office", "profile": "home"},
    )
    await hass.async_block_till_done()

    unsub = conn.subscriptions[14]
    unsub()

    await hass.services.async_call(
        "comfort_band",
        "set_schedule",
        {
            "zone": "office",
            "profile": "home",
            "transitions": [{"at": "06:00", "low": 20.0, "high": 23.0}],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    # Only the initial event from subscribe — nothing after unsub.
    assert len(_events_from(conn)) == 1
