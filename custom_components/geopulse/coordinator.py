"""Polling coordinator for the import direction (GeoPulse -> HA).

Current position only: the coordinator never calls the friend-location or
trail endpoints, so GeoPulse stays the only store of location history
(Plan.md §4). GeoPulse has no outbound webhooks yet; if it gains them, push
handlers can feed `async_set_updated_data()` with the same `GeoPulseData`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)

from .api import Friend, GeoPulseApiError, GeoPulseAuthError, GeoPulseClient, GpsPoint
from .const import (
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import GeoPulseConfigEntry

_LOGGER = logging.getLogger(__name__)

# How long imported trackers keep showing their last position while GeoPulse
# is unreachable before going unavailable (Plan.md §9 open item). Riding out
# short outages avoids spurious zone exits (home -> unavailable -> home)
# firing automations; past this, the position is too stale to present as
# current.
MIN_UNAVAILABLE_GRACE = timedelta(minutes=5)
UNAVAILABLE_GRACE_POLLS = 3


@dataclass
class GeoPulseData:
    """One poll's worth of current positions."""

    account: GpsPoint | None = None
    source_types: dict[str, GpsPoint | None] = field(default_factory=dict)
    friends: dict[str, Friend] = field(default_factory=dict)


class GeoPulseCoordinator(TimestampDataUpdateCoordinator[GeoPulseData]):
    """Fetch every configured import target once per poll."""

    config_entry: GeoPulseConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: GeoPulseConfigEntry, client: GeoPulseClient
    ) -> None:
        interval = timedelta(
            seconds=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=interval,
        )
        self.client = client
        self.unavailable_grace = max(MIN_UNAVAILABLE_GRACE, interval * UNAVAILABLE_GRACE_POLLS)

    async def _async_update_data(self) -> GeoPulseData:
        options = self.config_entry.options
        source_types: list[str] = options.get(CONF_IMPORT_SOURCE_TYPES, [])
        want_account = options.get(CONF_IMPORT_AGGREGATE_ACCOUNT, False)
        want_friends = bool(options.get(CONF_IMPORT_FRIEND_IDS))

        async def none() -> None:
            return None

        try:
            account, friends, *points = await asyncio.gather(
                self.client.async_get_last_known_position() if want_account else none(),
                # One call covers every friend (Plan.md §4).
                self.client.async_get_friends() if want_friends else none(),
                *(
                    self.client.async_get_latest_point_for_source_type(t)
                    for t in source_types
                ),
            )
        except GeoPulseAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GeoPulseApiError as err:
            raise UpdateFailed(str(err)) from err

        return GeoPulseData(
            account=account,
            source_types=dict(zip(source_types, points)),
            friends={f.friend_id: f for f in friends or []},
        )
