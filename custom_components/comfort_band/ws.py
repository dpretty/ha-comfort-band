"""Websocket commands for the Comfort Band frontend card.

The integration's services in `services.py` are write-only; the card needs
a read API to render the schedule editor. This module owns the read API
plus a push subscription so multiple card instances stay in sync without
polling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import async_response, websocket_command
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_ZONE_SCHEDULE_CHANGED

if TYPE_CHECKING:
    from . import ComfortBandData
    from .storage import StoredProfileSchedule


_NAME_FIELD = vol.All(str, vol.Length(min=1, max=255))


@callback
def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register every websocket command in this module."""
    async_register_command(hass, ws_get_schedule)
    async_register_command(hass, ws_subscribe_schedule)


@websocket_command(
    {
        vol.Required("type"): "comfort_band/get_schedule",
        vol.Required("zone"): _NAME_FIELD,
        vol.Required("profile"): _NAME_FIELD,
    }
)
@callback
def ws_get_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return `{baseline, current}` for the (zone, profile), or `null` if unset."""
    data: ComfortBandData = hass.data[DOMAIN]
    zone = msg["zone"]
    profile = msg["profile"]
    try:
        schedule = data.store.get_zone_schedule(zone, profile)
    except KeyError:
        connection.send_error(msg["id"], "zone_not_found", f"Zone {zone!r} does not exist")
        return
    connection.send_result(msg["id"], schedule)


@websocket_command(
    {
        vol.Required("type"): "comfort_band/subscribe_schedule",
        vol.Required("zone"): _NAME_FIELD,
        vol.Required("profile"): _NAME_FIELD,
    }
)
@async_response
async def ws_subscribe_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to (zone, profile) schedule changes.

    Sends one initial `{schedule}` event with the current value, then one
    event per `SIGNAL_ZONE_SCHEDULE_CHANGED` dispatch matching the
    requested (zone, profile). Cleanup is automatic on WS disconnect.
    """
    data: ComfortBandData = hass.data[DOMAIN]
    zone = msg["zone"]
    profile = msg["profile"]

    if not data.store.has_zone(zone):
        connection.send_error(msg["id"], "zone_not_found", f"Zone {zone!r} does not exist")
        return
    # An unknown profile is a typo, not an empty schedule: get_zone_schedule
    # would return None for both, which would leave the client on a
    # subscription that never fires. Reject up front.
    if profile not in data.store.list_profiles():
        connection.send_error(msg["id"], "profile_not_found", f"Profile {profile!r} does not exist")
        return
    # No awaits between this snapshot and the dispatcher_connect below —
    # a future refactor that introduces one would risk missing an update
    # written in the gap.
    initial = data.store.get_zone_schedule(zone, profile)

    @callback
    def _forward(
        changed_zone: str,
        changed_profile: str,
        schedule: StoredProfileSchedule,
    ) -> None:
        if changed_zone != zone or changed_profile != profile:
            return
        connection.send_event(msg["id"], {"schedule": schedule})

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_ZONE_SCHEDULE_CHANGED, _forward
    )
    connection.send_result(msg["id"])
    connection.send_event(msg["id"], {"schedule": initial})
