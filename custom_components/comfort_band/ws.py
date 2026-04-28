"""Websocket commands for the Comfort Band frontend card.

The integration's services in `services.py` are write-only; the card needs
a read API to render the schedule editor. This module owns the read API.
Add live-update subscriptions here when the card grows in v0.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import websocket_command
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

if TYPE_CHECKING:
    from . import ComfortBandData


@callback
def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register every websocket command in this module."""
    async_register_command(hass, ws_get_schedule)


@websocket_command(
    {
        vol.Required("type"): "comfort_band/get_schedule",
        vol.Required("zone"): str,
        vol.Required("profile"): str,
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
        connection.send_error(
            msg["id"], "zone_not_found", f"Zone {zone!r} does not exist"
        )
        return
    connection.send_result(msg["id"], schedule)
