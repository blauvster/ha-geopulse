"""Tests for the GeoPulse config, options and reauth flows."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geopulse.api import GeoPulseApiError, GeoPulseAuthError
from custom_components.geopulse.const import (
    CONF_BASE_URL,
    CONF_EXPORT_ENTITIES,
    CONF_EXPORT_ENTITY_TOKENS,
    CONF_EXPORT_RETRY_QUEUE,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_POLL_INTERVAL,
    CONF_READ_TOKEN,
    CONF_RECORDER_EXCLUDE,
    CONF_TIMELINE_USERS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
)

from .conftest import USER_ID, source_config

BASE_URL = "https://geopulse.example.com"

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


def schema_field(result: dict[str, Any], name: str) -> Any:
    """Return the selector for one field of a shown form."""
    for key, value in result["data_schema"].schema.items():
        if str(key) == name:
            return value
    raise KeyError(name)


def device(device_id: str, token: str | None = None) -> dict[str, str]:
    """Input for one tracker's export_device_ids screen."""
    return {"device_id": device_id} | ({"token": token} if token else {})


def suggested_device_id(result: dict[str, Any]) -> str:
    key = next(k for k in result["data_schema"].schema if str(k) == "device_id")
    return key.description["suggested_value"]


def token_required(result: dict[str, Any]) -> bool:
    key = next(k for k in result["data_schema"].schema if str(k) == "token")
    return isinstance(key, vol.Required)


async def submit_devices(
    configure: Any, result: dict[str, Any], *inputs: dict[str, str]
) -> dict[str, Any]:
    """Fill the export_device_ids screen once per tracker, in order."""
    for values in inputs:
        assert result["step_id"] == "export_device_ids", result
        result = await configure(result["flow_id"], values)
        if result.get("errors"):
            return result
    return result


def select_values(result: dict[str, Any], name: str) -> list[str]:
    return [opt["value"] for opt in schema_field(result, name).config["options"]]


async def start_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Run the connection step successfully; returns the import step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: f"{BASE_URL}/", CONF_READ_TOKEN: " read "}
    )


def make_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={CONF_BASE_URL: BASE_URL, CONF_READ_TOKEN: "read"},
        options={
            CONF_IMPORT_AGGREGATE_ACCOUNT: True,
            CONF_IMPORT_SOURCE_TYPES: [],
            CONF_IMPORT_FRIEND_IDS: [],
            CONF_EXPORT_ENTITIES: {},
            CONF_EXPORT_RETRY_QUEUE: True,
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS,
            CONF_RECORDER_EXCLUDE: True,
            **options,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_full_flow_import_and_export(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "import_selection"
    # HOME_ASSISTANT filtered out (would re-import our own exports), the two
    # OWNTRACKS configs collapsed, and the non-sharing friend hidden.
    assert select_values(result, CONF_IMPORT_SOURCE_TYPES) == ["GPSLOGGER", "OWNTRACKS"]
    assert select_values(result, CONF_IMPORT_FRIEND_IDS) == ["f1"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_IMPORT_AGGREGATE_ACCOUNT: False,
            CONF_IMPORT_SOURCE_TYPES: ["OWNTRACKS"],
            CONF_IMPORT_FRIEND_IDS: ["f1"],
        },
    )
    assert result["step_id"] == "export_selection"
    assert [str(k) for k in result["data_schema"].schema] == [
        CONF_EXPORT_ENTITIES, CONF_EXPORT_RETRY_QUEUE
    ]  # no entry-wide token

    hass.states.async_set("device_tracker.phone", "home", {"friendly_name": "Phone"})
    hass.states.async_set("device_tracker.tablet", "home")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EXPORT_ENTITIES: ["device_tracker.phone", "device_tracker.tablet"],
            CONF_EXPORT_RETRY_QUEUE: True,
        },
    )
    # One screen per tracker, with a real label and the entity id.
    assert result["step_id"] == "export_device_ids"
    assert result["description_placeholders"] == {
        "entity": "Phone (device_tracker.phone)", "index": "1", "total": "2"
    }
    assert suggested_device_id(result) == "device_tracker.phone"
    assert token_required(result)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], device(" my_phone ", "tok-me")
    )
    assert result["description_placeholders"]["index"] == "2"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], device("device_tracker.tablet", "tok-alex")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "geopulse.example.com"
    assert result["result"].unique_id == USER_ID
    assert result["data"] == {
        CONF_BASE_URL: BASE_URL,
        CONF_READ_TOKEN: "read",
        CONF_EXPORT_ENTITY_TOKENS: {
            "device_tracker.phone": "tok-me",
            "device_tracker.tablet": "tok-alex",
        },
    }
    assert result["options"] == {
        CONF_IMPORT_AGGREGATE_ACCOUNT: False,
        CONF_IMPORT_SOURCE_TYPES: ["OWNTRACKS"],
        CONF_IMPORT_FRIEND_IDS: ["f1"],
        CONF_EXPORT_ENTITIES: {
            "device_tracker.phone": "my_phone",
            "device_tracker.tablet": "device_tracker.tablet",
        },
        CONF_EXPORT_RETRY_QUEUE: True,
        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS,
        CONF_RECORDER_EXCLUDE: True,
    }


async def test_import_only_skips_device_ids(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EXPORT_RETRY_QUEUE: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_EXPORT_ENTITY_TOKENS not in result["data"]
    assert result["options"][CONF_EXPORT_ENTITIES] == {}


async def test_import_step_hides_empty_pickers(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.async_get_source_configs.return_value = [source_config("HOME_ASSISTANT", "c1")]
    mock_client.async_get_friends.return_value = []
    result = await start_flow(hass)
    fields = [str(key) for key in result["data_schema"].schema]
    assert fields == [CONF_IMPORT_AGGREGATE_ACCOUNT]


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (GeoPulseAuthError("nope"), "invalid_auth"),
        (GeoPulseApiError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_connection_errors_recover(
    hass: HomeAssistant, mock_client: MagicMock, side_effect: Exception, error: str
) -> None:
    configs = mock_client.async_get_source_configs.return_value
    mock_client.async_get_source_configs.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: BASE_URL, CONF_READ_TOKEN: "read"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.async_get_source_configs.side_effect = None
    mock_client.async_get_source_configs.return_value = configs
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: BASE_URL, CONF_READ_TOKEN: "read"}
    )
    assert result["step_id"] == "import_selection"


@pytest.mark.parametrize("url", ["geopulse.local", "ftp://geopulse.local", "http://"])
async def test_invalid_url(hass: HomeAssistant, mock_client: MagicMock, url: str) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: url, CONF_READ_TOKEN: "read"}
    )
    assert result["errors"] == {CONF_BASE_URL: "invalid_url"}
    mock_client.async_get_source_configs.assert_not_called()


async def test_already_configured(hass: HomeAssistant, mock_client: MagicMock) -> None:
    make_entry(hass)
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_unique_id_falls_back_to_url(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.async_get_source_configs.return_value = []
    mock_client.async_get_friends.return_value = []
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EXPORT_RETRY_QUEUE: True}
    )
    assert result["result"].unique_id == BASE_URL


async def test_export_requires_token(hass: HomeAssistant, mock_client: MagicMock) -> None:
    hass.states.async_set("device_tracker.phone", "home")
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXPORT_ENTITIES: ["device_tracker.phone"], CONF_EXPORT_RETRY_QUEUE: True},
    )
    # Blank token (the frontend would block it; the backend must too).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": "phone", "token": "  "}
    )
    assert result["errors"] == {"token": "export_token_required"}
    assert suggested_device_id(result) == "phone"


async def test_per_tracker_tokens(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Different people -> different accounts, where labels may repeat; one
    person's trackers may share a token."""
    for eid in ("phone", "tablet", "watch"):
        hass.states.async_set(f"device_tracker.{eid}", "home")
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EXPORT_ENTITIES: [
                "device_tracker.phone", "device_tracker.tablet", "device_tracker.watch"
            ],
            CONF_EXPORT_RETRY_QUEUE: True,
        },
    )
    result = await submit_devices(
        hass.config_entries.flow.async_configure, result,
        device("phone", "tok-me"), device("phone", "tok-alex"), device("watch", "tok-me"),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EXPORT_ENTITY_TOKENS] == {
        "device_tracker.phone": "tok-me",
        "device_tracker.tablet": "tok-alex",
        "device_tracker.watch": "tok-me",
    }
    assert result["options"][CONF_EXPORT_ENTITIES] == {
        "device_tracker.phone": "phone",
        "device_tracker.tablet": "phone",
        "device_tracker.watch": "watch",
    }


async def test_nothing_selected(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EXPORT_RETRY_QUEUE: True}
    )
    assert result["errors"] == {"base": "nothing_selected"}


@pytest.mark.parametrize(
    ("inputs", "error"),
    [
        # Same account: labels must differ.
        ([device("same", "tok"), device("same", "tok")], {"device_id": "duplicate_device_id"}),
        ([device("  ", "tok")], {"device_id": "device_id_required"}),
    ],
)
async def test_device_id_validation(
    hass: HomeAssistant,
    mock_client: MagicMock,
    inputs: list[dict[str, str]],
    error: dict[str, str],
) -> None:
    hass.states.async_set("device_tracker.phone", "home")
    hass.states.async_set("device_tracker.tablet", "home")
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EXPORT_ENTITIES: ["device_tracker.phone", "device_tracker.tablet"],
            CONF_EXPORT_RETRY_QUEUE: True,
        },
    )
    result = await submit_devices(hass.config_entries.flow.async_configure, result, *inputs)
    assert result["errors"] == error
    # The rejected value is kept for correction; the token never is.
    assert suggested_device_id(result) == inputs[-1]["device_id"]


async def test_export_picker_excludes_own_entities(
    hass: HomeAssistant, mock_client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    entity_registry.async_get_or_create("device_tracker", DOMAIN, "imported", suggested_object_id="gp")
    entity_registry.async_get_or_create("device_tracker", "mobile_app", "x", suggested_object_id="phone")
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: True}
    )
    config = schema_field(result, CONF_EXPORT_ENTITIES).config
    assert config["exclude_entities"] == ["device_tracker.gp"]

    # And the schema itself rejects one if submitted anyway.
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EXPORT_ENTITIES: ["device_tracker.gp"],
                CONF_EXPORT_RETRY_QUEUE: True,
            },
        )


async def test_reauth(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = make_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_READ_TOKEN: "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_READ_TOKEN] == "new-token"


async def test_reauth_invalid_token(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = make_entry(hass)
    mock_client.async_get_source_configs.side_effect = GeoPulseAuthError("nope")
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_READ_TOKEN: "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_READ_TOKEN] == "read"


async def test_reauth_wrong_account(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = make_entry(hass)
    mock_client.async_get_source_configs.return_value = [
        source_config("OWNTRACKS", "c9", user_id="someone-else")
    ]
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_READ_TOKEN: "other"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_READ_TOKEN] == "read"


async def test_reauth_adopts_user_id_for_url_keyed_entry(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, unique_id=BASE_URL)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_READ_TOKEN: "new-token"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == USER_ID


async def test_options_settings(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 120, CONF_RECORDER_EXCLUDE: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_POLL_INTERVAL] == 120
    assert entry.options[CONF_RECORDER_EXCLUDE] is False
    assert entry.options[CONF_IMPORT_AGGREGATE_ACCOUNT] is True


async def test_options_timeline_viewers(hass: HomeAssistant, mock_client: MagicMock) -> None:
    alex = await hass.auth.async_create_user("Alex")
    await hass.auth.async_create_system_user("Some add-on")
    entry = make_entry(hass, **{CONF_TIMELINE_USERS: ["deleted-user-id"]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    options = schema_field(result, CONF_TIMELINE_USERS).config["options"]
    labels = {o["value"]: o["label"] for o in options}
    assert labels[alex.id] == "Alex"
    assert "Some add-on" not in labels.values()  # system users can't log in
    assert labels["deleted-user-id"] == "deleted-user-id (removed user)"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_POLL_INTERVAL: 45, CONF_RECORDER_EXCLUDE: True, CONF_TIMELINE_USERS: [alex.id]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TIMELINE_USERS] == [alex.id]


async def test_options_import_keeps_stale_selection(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    # TRACCAR and friend f9 are no longer on the account but were selected.
    entry = make_entry(hass, **{CONF_IMPORT_SOURCE_TYPES: ["TRACCAR"], CONF_IMPORT_FRIEND_IDS: ["f9"]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_selection"}
    )
    assert select_values(result, CONF_IMPORT_SOURCE_TYPES) == ["GPSLOGGER", "OWNTRACKS", "TRACCAR"]
    assert select_values(result, CONF_IMPORT_FRIEND_IDS) == ["f1", "f9"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_IMPORT_AGGREGATE_ACCOUNT: False, CONF_IMPORT_SOURCE_TYPES: ["GPSLOGGER"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_IMPORT_SOURCE_TYPES] == ["GPSLOGGER"]
    assert entry.options[CONF_IMPORT_FRIEND_IDS] == []


async def test_options_import_nothing_selected(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_selection"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_AGGREGATE_ACCOUNT: False}
    )
    assert result["errors"] == {"base": "nothing_selected"}


async def test_options_import_cannot_connect(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = make_entry(hass)
    mock_client.async_get_source_configs.side_effect = GeoPulseApiError("down")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_selection"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_options_export_adds_token_and_entities(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    entry = make_entry(hass)
    hass.states.async_set("device_tracker.phone", "home")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "export_selection"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_EXPORT_ENTITIES: ["device_tracker.phone"], CONF_EXPORT_RETRY_QUEUE: False},
    )
    assert token_required(result)  # nothing saved for this tracker yet
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_id": "phone", "token": ""}
    )
    assert result["errors"] == {"token": "export_token_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], device("phone", "tok-me")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_EXPORT_ENTITY_TOKENS] == {"device_tracker.phone": "tok-me"}
    assert entry.options[CONF_EXPORT_ENTITIES] == {"device_tracker.phone": "phone"}
    assert entry.options[CONF_EXPORT_RETRY_QUEUE] is False


async def test_options_export_blank_token_keeps_existing(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    entry = make_entry(
        hass,
        **{CONF_EXPORT_ENTITIES: {"device_tracker.phone": "phone", "device_tracker.tablet": "t"}},
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_EXPORT_ENTITY_TOKENS: {"device_tracker.phone": "tok-alex", "device_tracker.tablet": "x"},
        },
    )
    hass.states.async_set("device_tracker.phone", "home")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "export_selection"}
    )
    # Tablet deselected: its token must go with it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_EXPORT_ENTITIES: ["device_tracker.phone"], CONF_EXPORT_RETRY_QUEUE: True},
    )
    assert suggested_device_id(result) == "phone"
    assert not token_required(result)  # blank keeps the saved one
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], device("phone")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_EXPORT_ENTITY_TOKENS] == {"device_tracker.phone": "tok-alex"}


async def test_options_token_only_change_reloads(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    entry = make_entry(hass, **{CONF_EXPORT_ENTITIES: {"device_tracker.phone": "phone"}})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EXPORT_ENTITY_TOKENS: {"device_tracker.phone": "old"}}
    )
    hass.states.async_set("device_tracker.phone", "home")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reload = AsyncMock(return_value=True)
    hass.config_entries.async_reload = reload
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "export_selection"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_EXPORT_ENTITIES: ["device_tracker.phone"], CONF_EXPORT_RETRY_QUEUE: True},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], device("phone", "new")
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_EXPORT_ENTITY_TOKENS] == {"device_tracker.phone": "new"}
    reload.assert_called_once_with(entry.entry_id)


async def test_flow_over_http_api(
    hass: HomeAssistant, mock_client: MagicMock, hass_client
) -> None:
    """Drive the flow the way the frontend does, so every form is serialized."""
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("device_tracker.phone", "home")
    client = await hass_client()

    async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await client.post(path, json=body)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    flows = "/api/config/config_entries/flow"
    result = await post(flows, {"handler": DOMAIN})
    flow = f"{flows}/{result['flow_id']}"
    result = await post(flow, {CONF_BASE_URL: BASE_URL, CONF_READ_TOKEN: "read"})
    assert result["step_id"] == "import_selection"
    result = await post(flow, {CONF_IMPORT_AGGREGATE_ACCOUNT: True})
    assert result["step_id"] == "export_selection"
    result = await post(
        flow,
        {
            CONF_EXPORT_ENTITIES: ["device_tracker.phone"],
            CONF_EXPORT_RETRY_QUEUE: True,
        },
    )
    assert result["step_id"] == "export_device_ids"
    result = await post(flow, device("phone", "tok-alex"))
    assert result["type"] == "create_entry"
