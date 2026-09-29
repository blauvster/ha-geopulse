"""Tests for the Lovelace card backend (websocket proxy + card registration)."""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geopulse.api import GeoPulseApiError, GeoPulseAuthError
from custom_components.geopulse.const import (
    CONF_BASE_URL,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_READ_TOKEN,
    DOMAIN,
)
from custom_components.geopulse.timeline import (
    CARD_FILE,
    FRONTEND_URL,
    async_register_card,
    slim_timeline,
)

from .conftest import USER_ID

pytestmark = pytest.mark.usefixtures("socket_enabled")

FRIEND_ID = "friend-1"


def multi_user_response() -> dict[str, Any]:
    """Shape of GeoPulse's MultiUserTimelineDTO, as seen on a live server."""
    return {
        "requestingUserId": USER_ID,
        "timelines": [
            {
                "userId": FRIEND_ID,
                "fullName": "Alex",
                "email": "alex@example.com",
                "assignedColor": "#10B981",
                "timeline": {
                    "stays": [],
                    "trips": [],
                    "dataGaps": [
                        {"id": 1, "startTime": "2026-09-29T01:00:00Z",
                         "endTime": "2026-09-29T03:00:00Z", "durationSeconds": 7200}
                    ],
                },
                "pathSegments": [[], [{"latitude": 1.0, "longitude": 2.0}]],
                "stats": {"totalDistanceMeters": 0},
            },
            {
                "userId": USER_ID,
                "fullName": None,
                "email": "me@example.com",
                "assignedColor": "#F59E0B",
                "timeline": {
                    "stays": [
                        {"id": 7, "timestamp": "2026-09-29T08:00:00Z", "stayDuration": 5400,
                         "latitude": 51.123456789, "longitude": -0.987654321,
                         "locationName": "<b>Home</b>", "city": None, "country": "UK"}
                    ],
                    "trips": [
                        {"id": 8, "timestamp": "2026-09-29T09:30:00Z", "tripDuration": 1500,
                         "distanceMeters": 12300, "movementType": "CAR",
                         "latitude": 51.1, "longitude": -0.9,
                         "endLatitude": 51.2, "endLongitude": -0.8}
                    ],
                    "dataGaps": [],
                },
                "pathSegments": [
                    [{"latitude": 51.1, "longitude": -0.9, "accuracy": 5, "timestamp": "x"},
                     {"latitude": 51.2, "longitude": -0.8}]
                ],
                "stats": {"totalDistanceMeters": 12300},
            },
        ],
    }


def test_slim_timeline() -> None:
    data = slim_timeline(multi_user_response())
    me, alex = data["people"]
    # Account owner first, whatever GeoPulse's order.
    assert me["is_self"] and not alex["is_self"]
    assert me["name"] == "me@example.com"  # no full name -> email
    assert me["color"] == "#F59E0B"
    assert me["stays"] == [
        {"start": "2026-09-29T08:00:00Z", "duration": 5400,
         "location": [51.123457, -0.987654], "name": "<b>Home</b>"}
    ]  # names passed through verbatim; the card escapes them
    assert me["trips"] == [
        {"start": "2026-09-29T09:30:00Z", "duration": 1500, "distance": 12300,
         "movement": "CAR", "from": [51.1, -0.9], "to": [51.2, -0.8]}
    ]
    assert me["path"] == [[[51.1, -0.9], [51.2, -0.8]]]  # only lat/lng survive
    assert me["distance"] == 12300
    assert alex["path"] == [[[1.0, 2.0]]]  # empty segment dropped
    assert alex["gaps"] == [{"start": "2026-09-29T01:00:00Z", "end": "2026-09-29T03:00:00Z"}]


def test_slim_timeline_empty() -> None:
    assert slim_timeline({}) == {"people": []}


async def setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_BASE_URL: "http://geopulse.local", CONF_READ_TOKEN: "read"},
        options={CONF_IMPORT_AGGREGATE_ACCOUNT: True},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def call(ws_client: Any, **msg: Any) -> dict[str, Any]:
    await ws_client.send_json_auto_id({"type": "geopulse/timeline", **msg})
    return await ws_client.receive_json()


async def test_ws_timeline(
    hass: HomeAssistant, mock_api: MagicMock, hass_ws_client: Any, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    mock_api.async_get_multi_user_timeline = AsyncMock(return_value=multi_user_response())
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    # After authenticating: the test token must not look issued in the future.
    freezer.move_to("2026-09-29T14:00:00+01:00")

    reply = await call(client, start_date="2026-09-28", end_date="2026-09-29")
    assert reply["success"], reply
    assert [p["name"] for p in reply["result"]["people"]] == ["me@example.com", "Alex"]

    # Whole days in HA's time zone, and never past "now".
    start, end = mock_api.async_get_multi_user_timeline.call_args.args
    london = ZoneInfo("Europe/London")
    assert start == datetime(2026, 9, 28, 0, 0, tzinfo=london)
    assert end == datetime(2026, 9, 29, 14, 0, tzinfo=london)
    assert reply["result"]["start"] == "2026-09-28T00:00:00+01:00"


async def test_ws_timeline_past_day_ends_at_midnight(
    hass: HomeAssistant, mock_api: MagicMock, hass_ws_client: Any, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    mock_api.async_get_multi_user_timeline = AsyncMock(return_value=multi_user_response())
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    # After authenticating: the test token must not look issued in the future.
    freezer.move_to("2026-09-29T14:00:00+01:00")
    await call(client, start_date="2026-09-20", end_date="2026-09-20")
    start, end = mock_api.async_get_multi_user_timeline.call_args.args
    assert (end - start).total_seconds() == 24 * 3600


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [("2026-09-29", "2026-09-28"), ("2026-08-01", "2026-09-29")],
)
async def test_ws_timeline_invalid_range(
    hass: HomeAssistant, mock_api: MagicMock, hass_ws_client: Any, start_date: str, end_date: str
) -> None:
    mock_api.async_get_multi_user_timeline = AsyncMock()
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    reply = await call(client, start_date=start_date, end_date=end_date)
    assert reply["error"]["code"] == "invalid_range"
    mock_api.async_get_multi_user_timeline.assert_not_called()


async def test_ws_timeline_requires_admin(
    hass: HomeAssistant,
    mock_api: MagicMock,
    hass_ws_client: Any,
    hass_read_only_access_token: str,
) -> None:
    """The proxy's token grants full GeoPulse account access."""
    mock_api.async_get_multi_user_timeline = AsyncMock()
    await setup_entry(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    reply = await call(client, start_date="2026-09-29", end_date="2026-09-29")
    assert reply["error"]["code"] == "unauthorized"
    mock_api.async_get_multi_user_timeline.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "code"),
    [(GeoPulseAuthError("x"), "unauthorized"), (GeoPulseApiError("down"), "geopulse_unavailable")],
)
async def test_ws_timeline_geopulse_errors(
    hass: HomeAssistant, mock_api: MagicMock, hass_ws_client: Any, side_effect: Exception, code: str
) -> None:
    mock_api.async_get_multi_user_timeline = AsyncMock(side_effect=side_effect)
    await setup_entry(hass)
    client = await hass_ws_client(hass)
    reply = await call(client, start_date="2026-09-29", end_date="2026-09-29")
    assert reply["error"]["code"] == code


async def test_ws_timeline_entry_selection(
    hass: HomeAssistant, mock_api: MagicMock, hass_ws_client: Any
) -> None:
    mock_api.async_get_multi_user_timeline = AsyncMock(return_value={})
    entry = await setup_entry(hass)
    client = await hass_ws_client(hass)
    ok = await call(client, entry_id=entry.entry_id, start_date="2026-09-29", end_date="2026-09-29")
    assert ok["success"]
    missing = await call(client, entry_id="nope", start_date="2026-09-29", end_date="2026-09-29")
    assert missing["error"]["code"] == "not_found"

    await hass.config_entries.async_unload(entry.entry_id)
    unloaded = await call(client, start_date="2026-09-29", end_date="2026-09-29")
    assert unloaded["error"]["code"] == "not_found"


async def test_register_card(hass: HomeAssistant) -> None:
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await async_register_card(hass)

    (paths,), _ = hass.http.async_register_static_paths.call_args
    assert paths[0].url_path == FRONTEND_URL
    assert paths[0].path.endswith("custom_components/geopulse/frontend")
    url = add_js.call_args.args[1]
    assert url.startswith(f"{FRONTEND_URL}/{CARD_FILE}?v=")  # cache-busted
