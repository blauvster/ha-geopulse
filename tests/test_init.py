"""Smoke tests: the integration loads and unloads under HA's test harness."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geopulse.const import CONF_BASE_URL, CONF_READ_TOKEN, DOMAIN


async def test_setup_and_unload_entry(hass: HomeAssistant, mock_api: MagicMock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: "http://geopulse.local", CONF_READ_TOKEN: "t"},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


def test_card_version_matches_manifest() -> None:
    """One version for the release: manifest.json is the source of truth."""
    root = Path(__file__).parents[1] / "custom_components" / "geopulse"
    manifest = json.loads((root / "manifest.json").read_text())
    card = (root / "frontend" / "geopulse-card.js").read_text()
    match = re.search(r'const VERSION = "([^"]+)";', card)
    assert match and match.group(1) == manifest["version"]
