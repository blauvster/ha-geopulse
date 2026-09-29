"""Async client for the GeoPulse REST API.

Endpoints and response shapes are confirmed against the GeoPulse backend
source (tess1o/geopulse@main) rather than its docs site, whose published
docs and generated OpenAPI spec are incomplete for several endpoints used
here. See Plan.md §12 for the endpoint-by-endpoint references.

This module has no Home Assistant imports so it can be unit tested in
isolation and reused by both the coordinator and the export flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    API_PATH_FRIEND_LOCATION,
    API_PATH_FRIENDS,
    API_PATH_FRIENDS_LOCATION_TRAILS,
    API_PATH_GPS_LAST_KNOWN_POSITION,
    API_PATH_GPS_POINTS,
    API_PATH_GPS_SOURCE,
    API_PATH_HOMEASSISTANT_INGEST,
)

REQUEST_TIMEOUT = ClientTimeout(total=15)


class GeoPulseError(Exception):
    """Base error for GeoPulse API failures."""


class GeoPulseAuthError(GeoPulseError):
    """Raised on a 401 response - the token is missing/invalid/expired."""


class GeoPulseApiError(GeoPulseError):
    """Raised on any other non-2xx response or unexpected payload shape."""


@dataclass
class GpsSourceConfig:
    """A GPS source configuration on the account (backend GpsSourceConfigDTO)."""

    id: str
    type: str
    device_id: str | None
    active: bool
    user_id: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GpsSourceConfig":
        return cls(
            id=data["id"],
            type=data["type"],
            device_id=data.get("deviceId"),
            active=data.get("active", False),
            user_id=data.get("userId"),
        )


@dataclass
class GpsPoint:
    """A single GPS point (backend GpsPointDTO)."""

    id: int
    timestamp: datetime
    latitude: float
    longitude: float
    accuracy: float | None
    battery: float | None
    velocity: float | None
    altitude: float | None
    source_type: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GpsPoint":
        coordinates = data["coordinates"]
        return cls(
            id=data["id"],
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            latitude=coordinates["lat"],
            longitude=coordinates["lng"],
            accuracy=data.get("accuracy"),
            battery=data.get("battery"),
            velocity=data.get("velocity"),
            altitude=data.get("altitude"),
            source_type=data.get("sourceType"),
        )


@dataclass
class Friend:
    """A friend with an existing accepted friendship (backend FriendInfoDTO)."""

    friend_id: str
    user_id: str | None
    full_name: str | None
    email: str | None
    last_latitude: float | None
    last_longitude: float | None
    last_battery: float | None
    last_seen: str | None
    shares_live_location: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Friend":
        return cls(
            friend_id=data["friendId"],
            user_id=data.get("userId"),
            full_name=data.get("fullName"),
            email=data.get("email"),
            last_latitude=data.get("lastLatitude"),
            last_longitude=data.get("lastLongitude"),
            last_battery=data.get("lastBattery"),
            last_seen=data.get("lastSeen"),
            shares_live_location=bool(data.get("friendSharesLiveLocation")),
        )


@dataclass
class FriendPoint:
    """A friend's GPS point (backend GpsPointPathPointDTO).

    Richer than the summary embedded in `Friend` (accuracy/altitude/velocity
    instead of just battery), but - unlike `GpsPoint` - carries no battery
    field at all; the mapper GeoPulse uses for this endpoint just doesn't
    include it. Combine with `Friend.last_battery` if both are wanted.
    """

    id: int
    user_id: str | None
    timestamp: datetime
    latitude: float
    longitude: float
    accuracy: float | None
    altitude: float | None
    velocity: float | None
    source_type: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FriendPoint":
        return cls(
            id=data["id"],
            user_id=data.get("userId"),
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            latitude=data["latitude"],
            longitude=data["longitude"],
            accuracy=data.get("accuracy"),
            altitude=data.get("altitude"),
            velocity=data.get("velocity"),
            source_type=data.get("sourceType"),
        )


class GeoPulseClient:
    """Thin async wrapper around the GeoPulse REST API."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401:
                    raise GeoPulseAuthError(f"GeoPulse rejected the token for {path}")
                if response.status >= 400:
                    body = await response.text()
                    raise GeoPulseApiError(
                        f"GeoPulse returned {response.status} for {path}: {body}"
                    )
                # Not response.json(): /api/homeassistant answers 200 with an
                # empty body and no Content-Type, which aiohttp's json()
                # rejects before looking at the body.
                body = await response.read()
                if not body.strip():
                    return None
                return json.loads(body)
        except (ClientError, TimeoutError) as err:
            # aiohttp's total-request timeout raises a bare TimeoutError, not a
            # ClientError subclass, so it has to be caught separately.
            raise GeoPulseApiError(f"Error communicating with GeoPulse: {err!r}") from err
        except ValueError as err:
            # JSON content type but an undecodable body.
            raise GeoPulseApiError(f"Invalid JSON from GeoPulse for {path}") from err

    async def _request_enveloped(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        payload = await self._request(method, path, **kwargs)
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise GeoPulseApiError(f"Unexpected response shape for {path}")
        if payload.get("status") != "success":
            raise GeoPulseApiError(payload.get("message") or f"GeoPulse error for {path}")
        return payload.get("data")

    async def async_get_source_configs(self) -> list[GpsSourceConfig]:
        """List the account's GPS source configs. Raw list, no envelope."""
        data = await self._request("GET", API_PATH_GPS_SOURCE)
        if not isinstance(data, list):
            raise GeoPulseApiError(f"Unexpected response shape for {API_PATH_GPS_SOURCE}")
        return [GpsSourceConfig.from_dict(item) for item in data]

    async def async_get_last_known_position(self) -> GpsPoint | None:
        """Latest point across all of the account's source types."""
        data = await self._request_enveloped("GET", API_PATH_GPS_LAST_KNOWN_POSITION)
        return GpsPoint.from_dict(data) if data else None

    async def async_get_latest_point_for_source_type(
        self, source_type: str
    ) -> GpsPoint | None:
        """Latest point restricted to one source type.

        GeoPulse has no per-device-id endpoint, only per-source-type filtering,
        so two devices sharing a source type are indistinguishable here.
        """
        data = await self._request_enveloped(
            "GET",
            API_PATH_GPS_POINTS,
            params={
                "sourceTypes": source_type,
                "limit": 1,
                "sortBy": "timestamp",
                "sortOrder": "desc",
            },
        )
        # /api/gps/points wraps its own pagination envelope (GpsPointPageDTO:
        # {"data": [...GpsPointDTO], "pagination": {...}}) inside the outer
        # ApiResponse envelope already unwrapped by _request_enveloped above.
        points = data.get("data") if isinstance(data, dict) else data
        if not points:
            return None
        point = GpsPoint.from_dict(points[0])
        # The server drops a sourceTypes value it doesn't recognise (logs a
        # warning, filters nothing), which would hand back another source's
        # latest point. Never attribute that to this source type.
        if point.source_type != source_type:
            return None
        return point

    async def async_get_friends(self) -> list[Friend]:
        """List friends, each carrying an embedded last-known position."""
        data = await self._request_enveloped("GET", API_PATH_FRIENDS)
        return [Friend.from_dict(item) for item in (data or [])]

    async def async_get_friend_location(self, friend_id: str) -> FriendPoint | None:
        """One friend's current location, with accuracy/altitude/velocity.

        Card use only (Plan.md Development Plan Phase 5) - the import
        coordinator (Phase 3) deliberately never calls this or
        `async_get_friends_location_trails`. History stays in GeoPulse;
        HA only ever polls current position via `async_get_friends`.
        """
        data = await self._request_enveloped(
            "GET", API_PATH_FRIEND_LOCATION.format(friend_id=friend_id)
        )
        return FriendPoint.from_dict(data) if data else None

    async def async_get_friends_location_trails(
        self, minutes: int = 60, end_time: datetime | None = None
    ) -> dict[str, list[FriendPoint]]:
        """Recent location history for every friend sharing live location.

        Card use only (Plan.md Development Plan Phase 5) - not called by
        the import coordinator (Phase 3), which polls current position
        only so that GeoPulse remains the sole source of truth for
        history and HA's recorder never mirrors it.

        Server caps `minutes` at 1440 (24h) looking back from `end_time`
        (defaults to now).
        """
        params: dict[str, Any] = {"minutes": minutes}
        if end_time is not None:
            params["endTime"] = end_time.isoformat()
        data = await self._request_enveloped(
            "GET", API_PATH_FRIENDS_LOCATION_TRAILS, params=params
        )
        return {
            friend_id: [FriendPoint.from_dict(point) for point in points]
            for friend_id, points in (data or {}).items()
        }

    async def async_post_homeassistant_location(
        self,
        *,
        device_id: str,
        timestamp: datetime,
        latitude: float,
        longitude: float,
        accuracy: float | None = None,
        altitude: float | None = None,
        speed: float | None = None,
        battery_level: float | None = None,
    ) -> None:
        """Push one location point to GeoPulse's Home Assistant ingest endpoint.

        This endpoint is public (no JWT/API-token auth) - identity comes solely
        from the Bearer token matching a HOME_ASSISTANT-type source config, so
        it must be called with the export/location-source token, not the read
        token used by the rest of this client.

        `speed` is m/s (GeoPulse converts to km/h on ingest). Altitude and
        battery are sent even when unknown: GeoPulse's mapper unboxes both
        unconditionally and answers 500 if either is missing (confirmed
        against a live server), so unknown altitude becomes 0.0 and unknown
        battery 0. `battery.level` is an int server-side.
        """
        body: dict[str, Any] = {
            "device_id": device_id,
            "timestamp": timestamp.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "location": {
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": accuracy,
                "altitude": altitude if altitude is not None else 0.0,
                "speed": speed,
            },
            "battery": {"level": round(battery_level) if battery_level is not None else 0},
        }
        await self._request("POST", API_PATH_HOMEASSISTANT_INGEST, json_body=body)
