"""Pytest configuration for the GeoPulse test suite.

Runs under pytest-homeassistant-custom-component, which needs a POSIX host
(it imports fcntl). Use the Docker test image - see docker/run-tests.sh.
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.geopulse.api import Friend, GpsSourceConfig

pytest_plugins = "pytest_homeassistant_custom_component"

USER_ID = "user-1"


def source_config(type_: str, config_id: str, user_id: str | None = USER_ID) -> GpsSourceConfig:
    return GpsSourceConfig(id=config_id, type=type_, device_id=None, active=True, user_id=user_id)


def friend(friend_id: str, name: str | None, *, shares: bool = True) -> Friend:
    return Friend(
        friend_id=friend_id,
        user_id=USER_ID,
        full_name=name,
        email=f"{friend_id}@example.com",
        last_latitude=1.0,
        last_longitude=2.0,
        last_battery=50.0,
        last_seen=None,
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
