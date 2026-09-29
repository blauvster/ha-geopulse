"""Tests for the GeoPulse API client (custom_components/geopulse/api.py).

Pure aiohttp-client tests, no Home Assistant fixtures needed. A real
aiohttp.web test server stands in for GeoPulse rather than aioresponses,
which monkeypatches aiohttp internals that have since changed shape
(aioresponses 0.7.9 doesn't work against aiohttp >=3.10's ClientResponse
signature) - hitting a real, if tiny, HTTP server sidesteps that version
coupling entirely.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer

from custom_components.geopulse import api
from custom_components.geopulse.api import (
    GeoPulseApiError,
    GeoPulseAuthError,
    GeoPulseClient,
)

TOKEN = "test-token"

# pytest-homeassistant-custom-component blocks sockets by default; opt in
# the same way HA core's own hass_client fixture does. Connections are still
# restricted to 127.0.0.1 by the plugin's socket_allow_hosts.
pytestmark = pytest.mark.usefixtures("socket_enabled")


class FakeGeoPulseServer:
    """Minimal aiohttp.web app that stands in for a GeoPulse instance."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.last_request_json: dict | None = None
        self._routes: dict[tuple[str, str], object] = {}

    def route(self, method: str, path: str, handler) -> None:
        self.app.router.add_route(method, path, handler)


@pytest.fixture
async def geopulse_server():
    server = FakeGeoPulseServer()
    yield server


async def make_client(server: FakeGeoPulseServer):
    test_server = TestServer(server.app)
    test_client = TestClient(test_server)
    await test_client.start_server()
    base_url = str(test_client.make_url("")).rstrip("/")
    client = GeoPulseClient(test_client.session, base_url, TOKEN)
    return client, test_client


async def test_get_source_configs(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return web.json_response(
            [
                {"id": "abc-123", "type": "HOME_ASSISTANT", "deviceId": None, "active": True},
                {"id": "def-456", "type": "OWNTRACKS", "deviceId": "tom_phone", "active": True},
            ]
        )

    geopulse_server.route("GET", "/api/gps/source/", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        configs = await client.async_get_source_configs()
    finally:
        await test_client.close()

    assert len(configs) == 2
    assert configs[0].type == "HOME_ASSISTANT"
    assert configs[1].device_id == "tom_phone"


async def test_get_last_known_position(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "success",
                "message": None,
                "data": {
                    "id": 1,
                    "timestamp": "2026-09-21T12:00:00Z",
                    "coordinates": {"lat": 51.5, "lng": -0.1},
                    "accuracy": 5.0,
                    "battery": 80.0,
                    "velocity": 0.0,
                    "altitude": 10.0,
                    "sourceType": "OWNTRACKS",
                },
            }
        )

    geopulse_server.route("GET", "/api/gps/last-known-position", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        point = await client.async_get_last_known_position()
    finally:
        await test_client.close()

    assert point is not None
    assert point.latitude == 51.5
    assert point.longitude == -0.1
    assert point.timestamp == datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


async def test_get_last_known_position_none(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "success", "message": None, "data": None})

    geopulse_server.route("GET", "/api/gps/last-known-position", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        point = await client.async_get_last_known_position()
    finally:
        await test_client.close()

    assert point is None


async def test_get_latest_point_for_source_type_unwraps_page_envelope(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        assert request.query["sourceTypes"] == "OWNTRACKS"
        assert request.query["limit"] == "1"
        return web.json_response(
            {
                "status": "success",
                "message": None,
                "data": {
                    "data": [
                        {
                            "id": 2,
                            "timestamp": "2026-09-21T12:05:00Z",
                            "coordinates": {"lat": 1.0, "lng": 2.0},
                            "accuracy": 3.0,
                            "battery": 50.0,
                            "velocity": 1.0,
                            "altitude": 0.0,
                            "sourceType": "OWNTRACKS",
                        }
                    ],
                    "pagination": {"page": 1, "limit": 1, "total": 1},
                },
            }
        )

    geopulse_server.route("GET", "/api/gps", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        point = await client.async_get_latest_point_for_source_type("OWNTRACKS")
    finally:
        await test_client.close()

    assert point is not None
    assert point.id == 2


async def test_get_friends(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "success",
                "message": None,
                "data": [
                    {
                        "userId": "u1",
                        "friendId": "f1",
                        "fullName": "Alex",
                        "lastLatitude": 10.0,
                        "lastLongitude": 20.0,
                        "lastBattery": 90.0,
                        "lastSeen": "2026-09-21T12:00:00Z",
                        "friendSharesLiveLocation": True,
                    }
                ],
            }
        )

    geopulse_server.route("GET", "/api/friends", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        friends = await client.async_get_friends()
    finally:
        await test_client.close()

    assert len(friends) == 1
    assert friends[0].shares_live_location is True


async def test_get_friend_location(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        assert request.match_info["friendId"] == "f1"
        return web.json_response(
            {
                "status": "success",
                "message": None,
                "data": {
                    "id": 5,
                    "userId": "f1",
                    "timestamp": "2026-09-21T12:10:00Z",
                    "latitude": 3.0,
                    "longitude": 4.0,
                    "accuracy": 8.0,
                    "altitude": 12.0,
                    "velocity": 2.0,
                    "sourceType": "OWNTRACKS",
                },
            }
        )

    geopulse_server.route("GET", "/api/friends/{friendId}/location", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        point = await client.async_get_friend_location("f1")
    finally:
        await test_client.close()

    assert point is not None
    assert point.latitude == 3.0
    assert point.accuracy == 8.0


async def test_get_friends_location_trails(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        assert request.query["minutes"] == "60"
        return web.json_response(
            {
                "status": "success",
                "message": None,
                "data": {
                    "f1": [
                        {
                            "id": 6,
                            "userId": "f1",
                            "timestamp": "2026-09-21T12:00:00Z",
                            "latitude": 1.0,
                            "longitude": 2.0,
                            "accuracy": None,
                            "altitude": None,
                            "velocity": None,
                            "sourceType": "OWNTRACKS",
                        },
                        {
                            "id": 7,
                            "userId": "f1",
                            "timestamp": "2026-09-21T12:05:00Z",
                            "latitude": 1.1,
                            "longitude": 2.1,
                            "accuracy": None,
                            "altitude": None,
                            "velocity": None,
                            "sourceType": "OWNTRACKS",
                        },
                    ]
                },
            }
        )

    geopulse_server.route("GET", "/api/friends/location/trails", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        trails = await client.async_get_friends_location_trails(minutes=60)
    finally:
        await test_client.close()

    assert list(trails.keys()) == ["f1"]
    assert len(trails["f1"]) == 2
    assert trails["f1"][1].latitude == 1.1


async def test_post_homeassistant_location_payload_shape(geopulse_server):
    captured = {}

    async def handler(request: web.Request) -> web.Response:
        captured["body"] = await request.json()
        captured["auth"] = request.headers.get("Authorization")
        return web.json_response({})

    geopulse_server.route("POST", "/api/homeassistant", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        await client.async_post_homeassistant_location(
            device_id="tom_phone",
            timestamp=datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc),
            latitude=51.5,
            longitude=-0.1,
            accuracy=5.0,
            altitude=10.0,
            speed=0.0,
            battery_level=80.0,
        )
    finally:
        await test_client.close()

    assert captured["auth"] == f"Bearer {TOKEN}"
    assert captured["body"]["device_id"] == "tom_phone"
    assert captured["body"]["location"]["latitude"] == 51.5
    assert captured["body"]["battery"]["level"] == 80
    assert captured["body"]["timestamp"] == "2026-09-21T12:00:00.000Z"


async def test_post_homeassistant_location_fills_required_fields(geopulse_server):
    """Mirrors the live server: empty 200 with no Content-Type, and a 500 if
    altitude or battery is missing."""
    captured = {}

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        captured["body"] = body
        if body["location"].get("altitude") is None or body.get("battery") is None:
            return web.json_response({"message": "NPE"}, status=500)
        return web.Response(status=200)

    geopulse_server.route("POST", "/api/homeassistant", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        await client.async_post_homeassistant_location(
            device_id="router_tracker",
            timestamp=datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc),
            latitude=51.5,
            longitude=-0.1,
        )
    finally:
        await test_client.close()

    assert captured["body"]["location"]["altitude"] == 0.0
    assert captured["body"]["battery"] == {"level": 0}


async def test_401_raises_auth_error(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=401)

    geopulse_server.route("GET", "/api/gps/source/", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        with pytest.raises(GeoPulseAuthError):
            await client.async_get_source_configs()
    finally:
        await test_client.close()


async def test_500_raises_api_error(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    geopulse_server.route("GET", "/api/gps/source/", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        with pytest.raises(GeoPulseApiError):
            await client.async_get_source_configs()
    finally:
        await test_client.close()


async def test_timeout_raises_api_error(geopulse_server, monkeypatch):
    async def handler(request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.json_response([])

    monkeypatch.setattr(api, "REQUEST_TIMEOUT", ClientTimeout(total=0.05))
    geopulse_server.route("GET", "/api/gps/source/", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        with pytest.raises(GeoPulseApiError):
            await client.async_get_source_configs()
    finally:
        await test_client.close()


async def test_invalid_json_raises_api_error(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="{not json", content_type="application/json")

    geopulse_server.route("GET", "/api/gps/source/", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        with pytest.raises(GeoPulseApiError):
            await client.async_get_source_configs()
    finally:
        await test_client.close()


async def test_unexpected_shape_raises_api_error(geopulse_server):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "success", "data": []})

    geopulse_server.route("GET", "/api/gps/source/", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        with pytest.raises(GeoPulseApiError):
            await client.async_get_source_configs()
    finally:
        await test_client.close()


async def test_latest_point_ignores_other_source_type(geopulse_server):
    """An unrecognised sourceTypes filter is dropped server-side; don't trust it."""

    async def handler(request: web.Request) -> web.Response:
        point = {
            "id": 3,
            "timestamp": "2026-09-21T12:05:00Z",
            "coordinates": {"lat": 1.0, "lng": 2.0},
            "sourceType": "OWNTRACKS",
        }
        return web.json_response(
            {"status": "success", "message": None, "data": {"data": [point], "pagination": {}}}
        )

    geopulse_server.route("GET", "/api/gps", handler)
    client, test_client = await make_client(geopulse_server)
    try:
        point = await client.async_get_latest_point_for_source_type("TRACCAR")
    finally:
        await test_client.close()

    assert point is None
