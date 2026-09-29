"""Tests for the import coordinator and device_tracker entities."""

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.geopulse.api import GeoPulseApiError, GeoPulseAuthError
from custom_components.geopulse.const import (
    CONF_BASE_URL,
    CONF_EXPORT_ENTITIES,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_POLL_INTERVAL,
    CONF_READ_TOKEN,
    CONF_RECORDER_EXCLUDE,
    DOMAIN,
)

from .conftest import USER_ID, friend, gps_point

ACCOUNT = "device_tracker.geopulse_account"
OWNTRACKS = "device_tracker.geopulse_owntracks"
ALEX = "device_tracker.geopulse_alex"
# The coordinator adds up to 1 s of jitter to each refresh; tick past it.
POLL = timedelta(seconds=46)


async def setup_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_BASE_URL: "http://geopulse.local", CONF_READ_TOKEN: "read"},
        options={
            CONF_IMPORT_AGGREGATE_ACCOUNT: True,
            CONF_IMPORT_SOURCE_TYPES: ["OWNTRACKS"],
            CONF_IMPORT_FRIEND_IDS: ["f1"],
            CONF_EXPORT_ENTITIES: {},
            CONF_POLL_INTERVAL: 45,
            CONF_RECORDER_EXCLUDE: True,
            **options,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta = POLL) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    # Interval refreshes run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_entities_created(hass: HomeAssistant, mock_api: MagicMock) -> None:
    await setup_entry(hass)

    account = hass.states.get(ACCOUNT)
    assert account.state == STATE_NOT_HOME
    assert account.attributes["latitude"] == 51.5
    assert account.attributes["longitude"] == -0.1
    assert account.attributes["gps_accuracy"] == 5.0
    assert account.attributes["source_type"] == "gps"
    assert account.attributes["altitude"] == 10.0
    assert account.attributes["speed"] == pytest.approx(1.5 / 3.6)  # km/h -> m/s
    assert account.attributes["battery"] == 80.0
    assert account.attributes["last_seen"] == "2026-09-29T12:00:00+00:00"
    assert account.attributes["geopulse_source"] == "COLOTA"
    assert account.attributes["friendly_name"] == "GeoPulse account"

    owntracks = hass.states.get(OWNTRACKS)
    assert owntracks.attributes["latitude"] == 40.0
    assert owntracks.attributes["friendly_name"] == "GeoPulse OwnTracks"

    alex = hass.states.get(ALEX)
    assert alex.attributes["latitude"] == 1.0
    assert alex.attributes["battery"] == 50.0
    assert alex.attributes["last_seen"] == "2026-09-29T11:00:00+00:00"
    # Friend summary carries no accuracy/altitude/speed (docs/DEVELOPMENT.md, Import).
    assert "altitude" not in alex.attributes


async def test_only_current_position_endpoints_polled(
    hass: HomeAssistant, mock_api: MagicMock
) -> None:
    await setup_entry(hass)
    mock_api.async_get_last_known_position.assert_called_once()
    mock_api.async_get_latest_point_for_source_type.assert_called_once_with("OWNTRACKS")
    mock_api.async_get_friends.assert_called_once()
    # History stays in GeoPulse: never the trail/per-friend-location calls.
    mock_api.async_get_friends_location_trails.assert_not_called()
    mock_api.async_get_friend_location.assert_not_called()


async def test_unselected_targets_not_fetched(hass: HomeAssistant, mock_api: MagicMock) -> None:
    await setup_entry(
        hass,
        **{CONF_IMPORT_AGGREGATE_ACCOUNT: False, CONF_IMPORT_FRIEND_IDS: []},
    )
    mock_api.async_get_last_known_position.assert_not_called()
    mock_api.async_get_friends.assert_not_called()
    assert hass.states.get(ACCOUNT) is None
    assert hass.states.get(OWNTRACKS) is not None


async def test_home_zone(hass: HomeAssistant, mock_api: MagicMock) -> None:
    mock_api.async_get_last_known_position.return_value = gps_point(
        lat=hass.config.latitude, lng=hass.config.longitude
    )
    await setup_entry(hass)
    assert hass.states.get(ACCOUNT).state == STATE_HOME


async def test_no_position_is_unknown(hass: HomeAssistant, mock_api: MagicMock) -> None:
    mock_api.async_get_last_known_position.return_value = None
    mock_api.async_get_friends.return_value = [friend("f1", "Alex", shares=False)]
    await setup_entry(hass)
    assert hass.states.get(ACCOUNT).state == STATE_UNKNOWN
    assert "latitude" not in hass.states.get(ACCOUNT).attributes
    # Friend stopped sharing since setup: no position, not a stale one.
    assert hass.states.get(ALEX).state == STATE_UNKNOWN


async def test_position_updates_on_poll(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass)
    mock_api.async_get_last_known_position.return_value = gps_point(lat=52.0, lng=0.5)
    await poll(hass, freezer)
    assert hass.states.get(ACCOUNT).attributes["latitude"] == 52.0


async def test_poll_interval_option(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass, **{CONF_POLL_INTERVAL: 300})
    await poll(hass, freezer, timedelta(seconds=60))
    assert mock_api.async_get_last_known_position.call_count == 1
    await poll(hass, freezer, timedelta(seconds=242))
    assert mock_api.async_get_last_known_position.call_count == 2


async def test_outage_keeps_position_then_goes_unavailable(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass)
    mock_api.async_get_last_known_position.side_effect = GeoPulseApiError("down")

    # Grace is max(5 min, 3 polls) = 5 min at a 45 s interval.
    await poll(hass, freezer)
    state = hass.states.get(ACCOUNT)
    assert state.state == STATE_NOT_HOME
    assert state.attributes["latitude"] == 51.5

    for _ in range(6):
        await poll(hass, freezer)
    assert hass.states.get(ACCOUNT).state == STATE_UNAVAILABLE

    mock_api.async_get_last_known_position.side_effect = None
    await poll(hass, freezer)
    assert hass.states.get(ACCOUNT).state == STATE_NOT_HOME


async def test_auth_error_during_poll_starts_reauth(
    hass: HomeAssistant, mock_api: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup_entry(hass)
    mock_api.async_get_friends.side_effect = GeoPulseAuthError("revoked")
    await poll(hass, freezer)

    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == entry.entry_id


@pytest.mark.parametrize(
    ("side_effect", "reauth"),
    [(GeoPulseApiError("down"), False), (GeoPulseAuthError("bad"), True)],
)
async def test_geopulse_down_at_startup(
    hass: HomeAssistant,
    mock_api: MagicMock,
    freezer: FrozenDateTimeFactory,
    side_effect: Exception,
    reauth: bool,
) -> None:
    """Setup still succeeds (so export keeps running); trackers recover."""
    mock_api.async_get_last_known_position.side_effect = side_effect
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ACCOUNT).state == STATE_UNAVAILABLE
    assert bool(hass.config_entries.flow.async_progress()) is reauth

    mock_api.async_get_last_known_position.side_effect = None
    await poll(hass, freezer)
    assert hass.states.get(ACCOUNT).state == STATE_NOT_HOME


async def test_friend_name_fallback_without_data(
    hass: HomeAssistant, mock_api: MagicMock
) -> None:
    mock_api.async_get_friends.side_effect = GeoPulseApiError("down")
    await setup_entry(hass)
    assert hass.states.get("device_tracker.geopulse_friend_f1") is not None


async def test_deselected_entity_removed(
    hass: HomeAssistant, mock_api: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    entry = await setup_entry(hass)
    assert entity_registry.async_get(OWNTRACKS) is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_IMPORT_SOURCE_TYPES: []}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(OWNTRACKS) is None
    assert entity_registry.async_get(ACCOUNT) is not None


async def test_unique_ids_stable(
    hass: HomeAssistant, mock_api: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    entry = await setup_entry(hass)
    assert {
        e.unique_id for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    } == {
        f"{entry.entry_id}_account",
        f"{entry.entry_id}_source_owntracks",
        f"{entry.entry_id}_friend_f1",
    }
