"""Pytest configuration for the GeoPulse test suite.

Runs under pytest-homeassistant-custom-component, which needs a POSIX host
(it imports fcntl). Use the Docker test image - see docker/run-tests.sh.
"""

from collections.abc import Generator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.geopulse.api import Friend, GpsPoint, GpsSourceConfig

pytest_plugins = "pytest_homeassistant_custom_component"

USER_ID = "user-1"


def source_config(type_: str, config_id: str, user_id: str | None = USER_ID) -> GpsSourceConfig:
    return GpsSourceConfig(id=config_id, type=type_, device_id=None, active=True, user_id=user_id)


def gps_point(
    source_type: str = "OWNTRACKS", lat: float = 51.5, lng: float = -0.1, **kwargs
) -> GpsPoint:
    values = {
        "id": 1,
        "timestamp": datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        "latitude": lat,
        "longitude": lng,
        "accuracy": 5.0,
        "battery": 80.0,
        "velocity": 1.5,
        "altitude": 10.0,
        "source_type": source_type,
    }
    return GpsPoint(**(values | kwargs))


def friend(friend_id: str, name: str | None, *, shares: bool = True) -> Friend:
    return Friend(
        friend_id=friend_id,
        user_id=USER_ID,
        full_name=name,
        email=f"{friend_id}@example.com",
        last_latitude=1.0,
        last_longitude=2.0,
        last_battery=50.0,
        last_seen="2026-09-29T11:00:00Z",
        shares_live_location=shares,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA's loader see custom_components/ during tests."""


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the API client used by the config flow."""
    with patch(
        "custom_components.geopulse.config_flow.GeoPulseClient", autospec=True
    ) as client_cls:
        client = client_cls.return_value
        client.async_get_source_configs = AsyncMock(
            return_value=[
                source_config("HOME_ASSISTANT", "c1"),
                source_config("OWNTRACKS", "c2"),
                source_config("OWNTRACKS", "c3"),
                source_config("GPSLOGGER", "c4"),
            ]
        )
        client.async_get_friends = AsyncMock(
            return_value=[friend("f1", "Alex"), friend("f2", "Sam", shares=False)]
        )
        yield client


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep flow tests from running the integration's own setup."""
    with patch(
        "custom_components.geopulse.async_setup_entry", return_value=True
    ) as setup:
        yield setup


@pytest.fixture
def mock_api() -> Generator[MagicMock]:
    """Patch the API client the integration itself (coordinator) uses."""
    with patch("custom_components.geopulse.GeoPulseClient", autospec=True) as client_cls:
        client = client_cls.return_value
        client.async_get_last_known_position = AsyncMock(return_value=gps_point("COLOTA"))
        client.async_get_latest_point_for_source_type = AsyncMock(
            side_effect=lambda source_type: gps_point(source_type, lat=40.0, lng=-3.0)
        )
        client.async_get_friends = AsyncMock(
            return_value=[friend("f1", "Alex"), friend("f2", "Sam", shares=False)]
        )
        yield client
