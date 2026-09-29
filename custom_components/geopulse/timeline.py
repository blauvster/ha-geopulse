"""Backend for the GeoPulse Lovelace card (Plan.md §7).

The card never talks to GeoPulse directly: it calls the `geopulse/timeline`
websocket command, which uses the entry's read token server-side, so the
token never reaches frontend code. The token does grant full access to the
GeoPulse account's history, so who may *view* it is configurable: by
default every logged-in HA user; with a viewer list (CONF_TIMELINE_USERS),
only those users plus administrators.

History is fetched live for the requested dates and returned trimmed to
what the card draws; nothing is cached or written to HA's database.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .api import Friend, GeoPulseApiError, GeoPulseAuthError, GeoPulseClient, GpsPoint
from .const import CONF_TIMELINE_USERS, DOMAIN

if TYPE_CHECKING:
    from . import GeoPulseConfigEntry

_LOGGER = logging.getLogger(__name__)

MAX_RANGE_DAYS = 31
FRONTEND_URL = f"/{DOMAIN}_frontend"
FRONTEND_DIR = Path(__file__).parent / "frontend"
CARD_FILE = "geopulse-card.js"


_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{3,8}")
DEFAULT_COLOR = "#3B82F6"


def _color(value: Any) -> str:
    # Interpolated into styles and marker HTML by the card: hex colours only.
    return value if isinstance(value, str) and _HEX_COLOR.fullmatch(value) else DEFAULT_COLOR


def _point(lat: Any, lng: Any) -> list[float]:
    # ~11 cm precision; keeps the payload small for long ranges.
    return [round(float(lat), 6), round(float(lng), 6)]


def slim_timeline(
    data: dict[str, Any], user_ids: list[str] | None = None
) -> dict[str, Any]:
    """Reduce GeoPulse's MultiUserTimelineDTO to what the card renders.

    `user_ids` limits the result to those people (the card's `users`
    option). Filtered here rather than via GeoPulse's `userIds` parameter:
    with that, one listed friend who has since stopped sharing their
    timeline makes GeoPulse reject the whole request (403), breaking the
    card; filtering locally just leaves that person out.
    """
    requesting = data.get("requestingUserId")
    people = []
    for person in data.get("timelines") or []:
        if user_ids and person.get("userId") not in user_ids:
            continue
        timeline = person.get("timeline") or {}
        stats = person.get("stats") or {}
        people.append(
            {
                "user_id": person.get("userId"),
                "name": person.get("fullName") or person.get("email") or "GeoPulse user",
                "color": _color(person.get("assignedColor")),
                "is_self": person.get("userId") == requesting,
                "stays": [
                    {
                        "start": stay["timestamp"],
                        "duration": stay.get("stayDuration", 0),
                        "location": _point(stay["latitude"], stay["longitude"]),
                        "name": stay.get("locationName"),
                    }
                    for stay in timeline.get("stays") or []
                ],
                "trips": [
                    {
                        "start": trip["timestamp"],
                        "duration": trip.get("tripDuration", 0),
                        "distance": trip.get("distanceMeters", 0),
                        "movement": trip.get("movementType"),
                        "from": _point(trip["latitude"], trip["longitude"]),
                        "to": _point(trip["endLatitude"], trip["endLongitude"]),
                    }
                    for trip in timeline.get("trips") or []
                ],
                "gaps": [
                    {"start": gap.get("startTime"), "end": gap.get("endTime")}
                    for gap in timeline.get("dataGaps") or []
                ],
                "path": [
                    [_point(p["latitude"], p["longitude"]) for p in segment]
                    for segment in person.get("pathSegments") or []
                    if segment
                ],
                "distance": stats.get("totalDistanceMeters", 0),
            }
        )
    # The account owner first, then friends in GeoPulse's order.
    people.sort(key=lambda p: not p["is_self"])
    return {"people": people}


async def _current_positions(client: GeoPulseClient) -> tuple[GpsPoint | None, list[Friend]]:
    """Latest position of the account and of friends sharing live location.

    Best effort: the timeline is still useful without "now" markers, so a
    failure here is logged and skipped rather than failing the request.
    """
    own, friends = await asyncio.gather(
        client.async_get_last_known_position(), client.async_get_friends(), return_exceptions=True
    )
    for result in (own, friends):
        if isinstance(result, BaseException) and not isinstance(result, GeoPulseApiError):
            raise result
        if isinstance(result, GeoPulseApiError):
            _LOGGER.debug("Current position for the card unavailable: %s", result)
    return (
        own if isinstance(own, GpsPoint) else None,
        friends if isinstance(friends, list) else [],
    )


def add_current(
    slim: dict[str, Any], own: GpsPoint | None, friends: list[Friend]
) -> dict[str, Any]:
    """Attach each person's current position, where one is available."""
    by_friend = {
        f.friend_id: f
        for f in friends
        if f.shares_live_location and f.last_latitude is not None and f.last_longitude is not None
    }
    for person in slim["people"]:
        if person["is_self"] and own is not None:
            person["current"] = {
                "location": _point(own.latitude, own.longitude),
                "time": own.timestamp.isoformat(),
            }
        elif (friend := by_friend.get(person["user_id"])) is not None:
            person["current"] = {
                "location": _point(friend.last_latitude, friend.last_longitude),
                "time": friend.last_seen,
            }
    return slim


def _find_entry(hass: HomeAssistant, entry_id: str | None) -> GeoPulseConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_id is None or entry.entry_id == entry_id:
            return entry
    return None


def _may_view(entry: GeoPulseConfigEntry, connection: websocket_api.ActiveConnection) -> bool:
    viewers = entry.options.get(CONF_TIMELINE_USERS) or []
    user = connection.user
    return not viewers or user.is_admin or user.id in viewers


def _entry_for(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> GeoPulseConfigEntry | None:
    """The requested entry if this user may view it; sends the error otherwise."""
    entry = _find_entry(hass, msg.get("entry_id"))
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No loaded GeoPulse entry")
        return None
    if not _may_view(entry, connection):
        connection.send_error(
            msg["id"],
            websocket_api.ERR_UNAUTHORIZED,
            "Not allowed to view this GeoPulse timeline",
        )
        return None
    return entry


@websocket_api.websocket_command(
    {
        vol.Required("type"): "geopulse/timeline",
        vol.Optional("entry_id"): str,
        vol.Required("start_date"): cv.date,
        vol.Required("end_date"): cv.date,
        vol.Optional("user_ids"): [str],
    }
)
@websocket_api.async_response
async def ws_timeline(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Stays, trips and path for whole days in HA's time zone."""
    start_date: date = msg["start_date"]
    end_date: date = msg["end_date"]
    if end_date < start_date:
        connection.send_error(msg["id"], "invalid_range", "end_date is before start_date")
        return
    if (end_date - start_date).days >= MAX_RANGE_DAYS:
        connection.send_error(
            msg["id"], "invalid_range", f"At most {MAX_RANGE_DAYS} days at a time"
        )
        return
    if (entry := _entry_for(hass, connection, msg)) is None:
        return

    tz = dt_util.get_default_time_zone()
    now = dt_util.now()
    start = datetime.combine(start_date, time.min, tz)
    day_after = datetime.combine(end_date + timedelta(days=1), time.min, tz)
    end = min(day_after, now)
    # "Now" markers only when the range reaches the present.
    includes_now = day_after > now
    client = entry.runtime_data.coordinator.client
    try:
        if includes_now:
            data, (own, friends) = await asyncio.gather(
                client.async_get_multi_user_timeline(start, end), _current_positions(client)
            )
        else:
            data = await client.async_get_multi_user_timeline(start, end)
    except GeoPulseAuthError:
        connection.send_error(msg["id"], "geopulse_auth_failed", "GeoPulse rejected the API token")
        return
    except GeoPulseApiError as err:
        connection.send_error(msg["id"], "geopulse_unavailable", str(err))
        return
    slim = slim_timeline(data, msg.get("user_ids"))
    if includes_now:
        slim = add_current(slim, own, friends)
    connection.send_result(
        msg["id"],
        {"start": start.isoformat(), "end": end.isoformat(), "includes_now": includes_now, **slim},
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "geopulse/users", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def ws_users(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """People the card can show: the account owner plus friends sharing
    their timeline. For the card editor's user picker."""
    if (entry := _entry_for(hass, connection, msg)) is None:
        return
    # The multi-user endpoint is the one place that lists exactly the people
    # whose timelines are visible; a one-minute window keeps it tiny.
    now = dt_util.now()
    try:
        data = await entry.runtime_data.coordinator.client.async_get_multi_user_timeline(
            now - timedelta(minutes=1), now
        )
    except GeoPulseAuthError:
        connection.send_error(msg["id"], "geopulse_auth_failed", "GeoPulse rejected the API token")
        return
    except GeoPulseApiError as err:
        connection.send_error(msg["id"], "geopulse_unavailable", str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "users": [
                {key: person[key] for key in ("user_id", "name", "color", "is_self")}
                for person in slim_timeline(data)["people"]
            ]
        },
    )


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_timeline)
    websocket_api.async_register_command(hass, ws_users)


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card (and its vendored Leaflet) and load it on every dashboard.

    HACS can't install one repository as both an integration and a
    dashboard plugin, so the integration ships the card itself; users don't
    have to add a dashboard resource by hand.
    """
    # Imported here: frontend/http aren't loaded in every test setup, and
    # the card is optional to the integration working.
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    card = await hass.async_add_executor_job((FRONTEND_DIR / CARD_FILE).read_bytes)
    version = hashlib.sha256(card).hexdigest()[:8]  # cache-bust on change
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(FRONTEND_DIR), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{FRONTEND_URL}/{CARD_FILE}?v={version}")
