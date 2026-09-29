"""Diagnostics must help debugging without leaking tokens or locations."""

import json
from typing import Any
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geopulse.const import (
    CONF_BASE_URL,
    CONF_EXPORT_ENTITIES,
    CONF_EXPORT_ENTITY_TOKENS,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_READ_TOKEN,
    DOMAIN,
)
from custom_components.geopulse.diagnostics import async_get_config_entry_diagnostics

from .conftest import USER_ID


async def test_diagnostics_redacts(hass: HomeAssistant, mock_api: MagicMock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        title="geopulse.secret-host.example",
        data={
            CONF_BASE_URL: "http://geopulse.secret-host.example",
            CONF_READ_TOKEN: "read-SECRET",
            CONF_EXPORT_ENTITY_TOKENS: {"device_tracker.phone": "export-SECRET"},
        },
        options={
            CONF_IMPORT_AGGREGATE_ACCOUNT: True,
            CONF_IMPORT_SOURCE_TYPES: ["OWNTRACKS"],
            CONF_IMPORT_FRIEND_IDS: ["f1"],
            CONF_EXPORT_ENTITIES: {"device_tracker.phone": "phone"},
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag: dict[str, Any] = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diag)
    for secret in ("SECRET", "secret-host", "51.5", "-0.1", "40.0", "-3.0"):
        assert secret not in dumped, secret  # tokens, server, coordinates

    assert diag["import"]["last_update_success"] is True
    assert diag["import"]["account_last_point"] == "2026-09-29T12:00:00+00:00"
    assert diag["import"]["source_types_last_point"] == {"OWNTRACKS": "2026-09-29T12:00:00+00:00"}
    assert diag["import"]["friends"]["f1"]["has_position"] is True
    assert diag["export"] == {
        "retry_queue": True,
        "accounts": [{"trackers": ["device_tracker.phone"], "queued_points": 0}],
    }
