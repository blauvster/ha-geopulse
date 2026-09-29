"""Tests for the export direction (HA -> GeoPulse)."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.geopulse.api import GeoPulseApiError, GeoPulseAuthError
from custom_components.geopulse.const import (
    CONF_BASE_URL,
    CONF_EXPORT_ENTITIES,
    CONF_EXPORT_RETRY_QUEUE,
    CONF_EXPORT_TOKEN,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_READ_TOKEN,
    DOMAIN,
)
from custom_components.geopulse.export import build_payload, storage_key

from .conftest import USER_ID

PHONE = "device_tracker.phone"
TABLET = "device_tracker.tablet"


def location(lat: float, lng: float = 2.0, **extra: Any) -> dict[str, Any]:
    return {"latitude": lat, "longitude": lng, "gps_accuracy": 10, **extra}


async def setup_entry(
    hass: HomeAssistant, *, retry: bool = True, token: str | None = "export", **options: Any
) -> MockConfigEntry:
    data = {CONF_BASE_URL: "http://geopulse.local", CONF_READ_TOKEN: "read"}
    if token:
        data[CONF_EXPORT_TOKEN] = token
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data=data,
        options={
            CONF_IMPORT_AGGREGATE_ACCOUNT: True,
            CONF_IMPORT_SOURCE_TYPES: [],
            CONF_IMPORT_FRIEND_IDS: [],
            CONF_EXPORT_ENTITIES: {PHONE: "phone", TABLET: "tablet"},
            CONF_EXPORT_RETRY_QUEUE: retry,
            **options,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await settle(hass)


def sent(mock_api: MagicMock) -> list[tuple[str, float]]:
    return [
        (c.kwargs["device_id"], c.kwargs["latitude"])
        for c in mock_api.async_post_homeassistant_location.call_args_list
    ]


# --- build_payload -------------------------------------------------------


def test_payload_fields() -> None:
    state = State(PHONE, "not_home", location(1.0, battery_level=55, altitude=30, speed=2.5))
    payload = build_payload("phone", state, None)
    assert payload == {
        "device_id": "phone",
        "timestamp": state.last_updated.isoformat(),
        "latitude": 1.0,
        "longitude": 2.0,
        "accuracy": 10,
        "altitude": 30,
        "speed": 2.5,
        "battery_level": 55,
    }


def test_payload_battery_attribute_fallback() -> None:
    state = State(PHONE, "not_home", location(1.0, battery=40))
    assert build_payload("phone", state, None)["battery_level"] == 40


@pytest.mark.parametrize(
    "new_state",
    [
        None,
        State(PHONE, "unavailable", location(1.0)),
        State(PHONE, "unknown", location(1.0)),
        State(PHONE, "home", {"source_type": "router"}),  # no coordinates
    ],
)
def test_payload_skipped(new_state: State | None) -> None:
    assert build_payload("phone", new_state, None) is None


def test_payload_skipped_when_not_moved() -> None:
    old = State(PHONE, "not_home", location(1.0, battery_level=50))
    new = State(PHONE, "not_home", location(1.0, battery_level=49))
    assert build_payload("phone", new, old) is None


# --- exporter ------------------------------------------------------------


async def test_state_change_exported(hass: HomeAssistant, mock_api: MagicMock) -> None:
    await setup_entry(hass)
    hass.states.async_set(PHONE, "not_home", location(1.0, battery_level=55))
    await settle(hass)

    post = mock_api.async_post_homeassistant_location
    post.assert_called_once()
    kwargs = post.call_args.kwargs
    assert kwargs["device_id"] == "phone"
    assert kwargs["latitude"] == 1.0
    assert kwargs["battery_level"] == 55
    assert kwargs["timestamp"].tzinfo is not None


async def test_only_selected_entities_exported(hass: HomeAssistant, mock_api: MagicMock) -> None:
    await setup_entry(hass)
    hass.states.async_set("device_tracker.other", "not_home", location(1.0))
    await settle(hass)
    mock_api.async_post_homeassistant_location.assert_not_called()


async def test_export_client_uses_export_token(hass: HomeAssistant, mock_api: MagicMock) -> None:
    with patch("custom_components.geopulse.GeoPulseClient") as client_cls:
        client_cls.return_value = mock_api
        await setup_entry(hass)
    tokens = [c.args[2] for c in client_cls.call_args_list]
    assert tokens == ["export", "read"]


async def test_no_exporter_without_token(hass: HomeAssistant, mock_api: MagicMock) -> None:
    entry = await setup_entry(hass, token=None)
    assert entry.runtime_data.exporter is None


async def test_export_works_while_import_down(hass: HomeAssistant, mock_api: MagicMock) -> None:
    mock_api.async_get_last_known_position.side_effect = GeoPulseApiError("read side down")
    await setup_entry(hass)
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)
    assert sent(mock_api) == [("phone", 1.0)]


async def test_retry_queue_preserves_order(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup_entry(hass)
    post = mock_api.async_post_homeassistant_location
    post.side_effect = GeoPulseApiError("down")

    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)
    hass.states.async_set(TABLET, "not_home", location(2.0))
    hass.states.async_set(PHONE, "not_home", location(3.0))
    await settle(hass)
    # One failed attempt; later points wait behind it instead of hammering.
    assert post.call_count == 1
    assert entry.runtime_data.exporter.queue_length == 3

    post.side_effect = None
    await tick(hass, freezer, 31)
    assert sent(mock_api)[1:] == [("phone", 1.0), ("tablet", 2.0), ("phone", 3.0)]
    assert entry.runtime_data.exporter.queue_length == 0


async def test_retry_backoff_grows(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass)
    post = mock_api.async_post_homeassistant_location
    post.side_effect = GeoPulseApiError("down")
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)

    await tick(hass, freezer, 31)  # 30 s -> attempt 2
    assert post.call_count == 2
    await tick(hass, freezer, 31)  # next wait is 60 s: no attempt yet
    assert post.call_count == 2
    await tick(hass, freezer, 30)
    assert post.call_count == 3


async def test_retry_disabled_drops(hass: HomeAssistant, mock_api: MagicMock) -> None:
    entry = await setup_entry(hass, retry=False)
    post = mock_api.async_post_homeassistant_location
    post.side_effect = [GeoPulseApiError("down"), None]

    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)
    hass.states.async_set(PHONE, "not_home", location(2.0))
    await settle(hass)
    assert sent(mock_api) == [("phone", 1.0), ("phone", 2.0)]
    assert entry.runtime_data.exporter.queue_length == 0


async def test_queue_persisted_across_restart(
    hass: HomeAssistant,
    mock_api: MagicMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    entry = await setup_entry(hass)
    mock_api.async_post_homeassistant_location.side_effect = GeoPulseApiError("down")
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[storage_key(entry.entry_id)]["data"]
    assert [p["latitude"] for p in stored] == [1.0]

    mock_api.async_post_homeassistant_location.reset_mock(side_effect=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert sent(mock_api) == [("phone", 1.0)]


async def test_queue_saved_while_running(
    hass: HomeAssistant,
    mock_api: MagicMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A crash (no clean unload) still leaves the queue on disk."""
    entry = await setup_entry(hass)
    mock_api.async_post_homeassistant_location.side_effect = GeoPulseApiError("down")
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)
    await tick(hass, freezer, 6)
    assert len(hass_storage[storage_key(entry.entry_id)]["data"]) == 1


async def test_queue_cap(hass: HomeAssistant, mock_api: MagicMock) -> None:
    with patch("custom_components.geopulse.export.MAX_QUEUE", 2):
        entry = await setup_entry(hass)
        mock_api.async_post_homeassistant_location.side_effect = GeoPulseApiError("down")
        for lat in (1.0, 2.0, 3.0):
            hass.states.async_set(PHONE, "not_home", location(lat))
            await settle(hass)
    exporter = entry.runtime_data.exporter
    assert [p["latitude"] for p in exporter._queue] == [2.0, 3.0]


async def test_auth_error_raises_and_clears_issue(
    hass: HomeAssistant,
    mock_api: MagicMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry = await setup_entry(hass)
    post = mock_api.async_post_homeassistant_location
    post.side_effect = GeoPulseAuthError("bad export token")
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)

    issue_id = f"export_auth_failed_{entry.entry_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None
    # Import side is unaffected: no reauth for the read token.
    assert not hass.config_entries.flow.async_progress()

    post.side_effect = None
    await tick(hass, freezer, 31)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_unload_stops_export(hass: HomeAssistant, mock_api: MagicMock) -> None:
    entry = await setup_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)
    mock_api.async_post_homeassistant_location.assert_not_called()


async def test_remove_entry_deletes_queue(
    hass: HomeAssistant, mock_api: MagicMock, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert storage_key(entry.entry_id) in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert storage_key(entry.entry_id) not in hass_storage


async def test_timestamp_round_trips_through_queue(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass)
    when = datetime(2026, 9, 29, 12, 0, 0, 123000, tzinfo=timezone.utc)
    freezer.move_to(when)
    hass.states.async_set(PHONE, "not_home", location(1.0))
    await settle(hass)
    assert mock_api.async_post_homeassistant_location.call_args.kwargs["timestamp"] == when
