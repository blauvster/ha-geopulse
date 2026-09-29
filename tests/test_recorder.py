"""Recorder exclusion for imported trackers, against a real recorder DB."""

from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.geopulse.const import CONF_RECORDER_EXCLUDE

from .conftest import gps_point
from .test_device_tracker import ACCOUNT, ALEX, poll, setup_entry


@pytest.fixture
async def mock_recorder_before_hass(async_test_recorder: Any) -> None:
    """Start the recorder before hass (the autouse custom-integrations
    fixture would otherwise create hass first)."""


async def recorded_attributes(hass: HomeAssistant, entity_id: str) -> list[dict[str, Any]]:
    await async_wait_recording_done(hass)
    history = await hass.async_add_executor_job(
        partial(
            get_significant_states,
            hass,
            dt_util.utcnow() - timedelta(hours=1),
            entity_ids=[entity_id],
            significant_changes_only=False,
        )
    )
    return [dict(state.attributes) for state in history[entity_id]]


@pytest.mark.parametrize(("exclude", "recorded"), [(True, False), (False, True)])
async def test_recorder_exclusion(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_api: MagicMock,
    freezer: FrozenDateTimeFactory,
    exclude: bool,
    recorded: bool,
) -> None:
    """Coordinates really don't land in the recorder DB (Plan.md §6)."""
    await setup_entry(hass, **{CONF_RECORDER_EXCLUDE: exclude})
    mock_api.async_get_last_known_position.return_value = gps_point(lat=52.0, lng=0.5)
    await poll(hass, freezer)

    for entity_id in (ACCOUNT, ALEX):
        rows = await recorded_attributes(hass, entity_id)
        assert len(rows) >= 1
        for attrs in rows:
            for key in ("latitude", "longitude", "gps_accuracy", "last_seen"):
                assert (key in attrs) is recorded, (entity_id, key, attrs)
            assert "battery" in attrs
