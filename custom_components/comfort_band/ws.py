"""Websocket commands for the Comfort Band frontend card.

The integration's services in `services.py` are write-only; the card needs
read APIs to render the schedule editor. This module owns the
request/response `get_schedule`, the push `subscribe_schedule` that keeps
multiple card instances in sync without polling, and `get_feedback` (the
read side of the comfort-feedback log written by `record_feedback`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import async_response, websocket_command
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_SHARED_SCHEDULE_CHANGED, SIGNAL_ZONE_SCHEDULE_CHANGED

if TYPE_CHECKING:
    from . import ComfortBandData
    from .storage import StoredProfileSchedule


_NAME_FIELD = vol.All(str, vol.Length(min=1, max=255))


@callback
def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register every websocket command in this module."""
    async_register_command(hass, ws_get_schedule)
    async_register_command(hass, ws_subscribe_schedule)
    async_register_command(hass, ws_get_feedback)


def _schedule_ref_error(connection: ActiveConnection, msg: dict[str, Any]) -> bool:
    """Validate exactly-one-of (zone | shared_id). Sends an error + returns True
    if invalid; otherwise returns False. v0.14.0."""
    if (msg.get("zone") is None) == (msg.get("shared_id") is None):
        connection.send_error(
            msg["id"], "invalid_format", "Provide exactly one of 'zone' or 'shared_id'"
        )
        return True
    return False


@websocket_command(
    {
        vol.Required("type"): "comfort_band/get_schedule",
        vol.Optional("zone"): _NAME_FIELD,
        vol.Optional("shared_id"): _NAME_FIELD,
        vol.Required("profile"): _NAME_FIELD,
    }
)
@callback
def ws_get_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return `{baseline, current}` for a (zone | shared_id) + profile, or `null`
    if that profile has no schedule yet.

    The target is a zone's own schedule (`zone`) or a shared schedule
    (`shared_id`) — exactly one. `null` can't distinguish "no schedule yet"
    from "profile does not exist"; the push command `subscribe_schedule` errors
    on those so the card doesn't sit on a subscription that will never fire.
    """
    data: ComfortBandData = hass.data[DOMAIN]
    if _schedule_ref_error(connection, msg):
        return
    profile = msg["profile"]
    zone = msg.get("zone")
    if zone is not None:
        if not data.store.has_zone(zone):
            connection.send_error(msg["id"], "zone_not_found", f"Zone {zone!r} does not exist")
            return
        connection.send_result(msg["id"], data.store.get_zone_schedule(zone, profile))
        return
    shared_id = msg["shared_id"]
    if not data.store.has_shared_schedule(shared_id):
        connection.send_error(
            msg["id"], "shared_schedule_not_found", f"Shared schedule {shared_id!r} does not exist"
        )
        return
    connection.send_result(msg["id"], data.store.get_shared_schedule_slot(shared_id, profile))


@websocket_command(
    {
        vol.Required("type"): "comfort_band/subscribe_schedule",
        vol.Optional("zone"): _NAME_FIELD,
        vol.Optional("shared_id"): _NAME_FIELD,
        vol.Required("profile"): _NAME_FIELD,
    }
)
@async_response
async def ws_subscribe_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to a (zone | shared_id) + profile schedule.

    Sends one initial `{schedule}` event, then one per matching change signal —
    `SIGNAL_ZONE_SCHEDULE_CHANGED` for a zone, `SIGNAL_SHARED_SCHEDULE_CHANGED`
    for a shared schedule (so every card editing the same shared id stays in
    sync). Both signals carry `(id, profile, schedule)`. Cleanup is automatic
    on WS disconnect.
    """
    data: ComfortBandData = hass.data[DOMAIN]
    if _schedule_ref_error(connection, msg):
        return
    profile = msg["profile"]
    # An unknown profile is a typo, not an empty schedule: the lookups below
    # return None for both, which would leave the client on a subscription that
    # never fires. Reject up front.
    if profile not in data.store.list_profiles():
        connection.send_error(msg["id"], "profile_not_found", f"Profile {profile!r} does not exist")
        return

    zone = msg.get("zone")
    if zone is not None:
        if not data.store.has_zone(zone):
            connection.send_error(msg["id"], "zone_not_found", f"Zone {zone!r} does not exist")
            return
        ref_id = zone
        signal = SIGNAL_ZONE_SCHEDULE_CHANGED
        initial = data.store.get_zone_schedule(zone, profile)
    else:
        shared_id = msg["shared_id"]
        if not data.store.has_shared_schedule(shared_id):
            connection.send_error(
                msg["id"],
                "shared_schedule_not_found",
                f"Shared schedule {shared_id!r} does not exist",
            )
            return
        ref_id = shared_id
        signal = SIGNAL_SHARED_SCHEDULE_CHANGED
        initial = data.store.get_shared_schedule_slot(shared_id, profile)
    # No awaits between this snapshot and the dispatcher_connect below — a
    # future refactor that introduces one would risk missing an update written
    # in the gap.

    @callback
    def _forward(
        changed_id: str,
        changed_profile: str,
        schedule: StoredProfileSchedule,
    ) -> None:
        if changed_id != ref_id or changed_profile != profile:
            return
        connection.send_event(msg["id"], {"schedule": schedule})

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, signal, _forward)
    # `send_result` must precede `send_event`: the HA WS protocol expects the
    # subscription ack before any events, and the JS client only resolves its
    # `subscribeMessage` promise on the result frame.
    connection.send_result(msg["id"])
    connection.send_event(msg["id"], {"schedule": initial})


@websocket_command(
    {
        vol.Required("type"): "comfort_band/get_feedback",
        vol.Required("zone"): _NAME_FIELD,
        vol.Optional("since"): str,
    }
)
@callback
def ws_get_feedback(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return `{entries: [...]}` of recorded comfort feedback for a zone.

    Read path for the v3 auto-learning loop (the `record_feedback` service
    is the write path). Optional `since` (ISO-8601) filters to entries at or
    after that time. Unknown zone → `zone_not_found`, matching
    `get_schedule`. Entries are returned oldest-first.
    """
    data: ComfortBandData = hass.data[DOMAIN]
    zone = msg["zone"]
    if not data.store.has_zone(zone):
        connection.send_error(msg["id"], "zone_not_found", f"Zone {zone!r} does not exist")
        return
    entries = data.feedback_store.get_entries(zone, msg.get("since"))
    connection.send_result(msg["id"], {"entries": entries})
