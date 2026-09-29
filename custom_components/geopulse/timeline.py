"""Backend for the GeoPulse Lovelace card (Plan.md §7).

The card never talks to GeoPulse directly: it calls the `geopulse/timeline`
websocket command, which uses the entry's read token server-side. That
token grants full access to the GeoPulse account, so the command is
admin-only and the token never reaches frontend code.

History is fetched live for the requested dates and returned trimmed to
what the card draws; nothing is cached or written to HA's database.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .api import GeoPulseApiError, GeoPulseAuthError
from .const import DOMAIN

if TYPE_CHECKING:
    from . import GeoPulseConfigEntry

MAX_RANGE_DAYS = 31
FRONTEND_URL = f"/{DOMAIN}_frontend"
FRONTEND_DIR = Path(__file__).parent / "frontend"
CARD_FILE = "geopulse-card.js"


def _point(lat: Any, lng: Any) -> list[float]:
    # ~11 cm precision; keeps the payload small for long ranges.
    return [round(float(lat), 6), round(float(lng), 6)]


def slim_timeline(data: dict[str, Any]) -> dict[str, Any]:
    """Reduce GeoPulse's MultiUserTimelineDTO to what the card renders."""
    requesting = data.get("requestingUserId")
    people = []
    for person in data.get("timelines") or []:
        timeline = person.get("timeline") or {}
        stats = person.get("stats") or {}
        people.append(
            {
                "user_id": person.get("userId"),
                "name": person.get("fullName") or person.get("email") or "GeoPulse user",
                "color": person.get("assignedColor") or "#3B82F6",
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


def _find_entry(hass: HomeAssistant, entry_id: str | None) -> GeoPulseConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_id is None or entry.entry_id == entry_id:
            return entry
    return None


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "geopulse/timeline",
        vol.Optional("entry_id"): str,
        vol.Required("start_date"): cv.date,
        vol.Required("end_date"): cv.date,
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
    entry = _find_entry(hass, msg.get("entry_id"))
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No loaded GeoPulse entry")
        return

    tz = dt_util.get_default_time_zone()
    start = datetime.combine(start_date, time.min, tz)
    end = min(datetime.combine(end_date + timedelta(days=1), time.min, tz), dt_util.now())
    try:
        data = await entry.runtime_data.coordinator.client.async_get_multi_user_timeline(
            start, end
        )
    except GeoPulseAuthError:
        connection.send_error(msg["id"], "unauthorized", "GeoPulse rejected the API token")
        return
    except GeoPulseApiError as err:
        connection.send_error(msg["id"], "geopulse_unavailable", str(err))
        return
    connection.send_result(
        msg["id"],
        {"start": start.isoformat(), "end": end.isoformat(), **slim_timeline(data)},
    )


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_timeline)


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
