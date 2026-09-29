"""Diagnostics for the GeoPulse integration.

Location data is sensitive, so this never includes tokens, the server URL
or coordinates - only what's needed to debug: timestamps, counts, queue
lengths and whether each import target currently has a position.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_BASE_URL, CONF_EXPORT_ENTITY_TOKENS, CONF_READ_TOKEN

if TYPE_CHECKING:
    from . import GeoPulseConfigEntry

TO_REDACT = {CONF_BASE_URL, CONF_READ_TOKEN, CONF_EXPORT_ENTITY_TOKENS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GeoPulseConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data
    last_success = coordinator.last_update_success_time

    def point_age(point: Any) -> str | None:
        return point.timestamp.isoformat() if point is not None else None

    return {
        "entry": {
            "title": "**REDACTED**",  # defaults to the server's host name
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "import": {
            "last_update_success": coordinator.last_update_success,
            "last_update_success_time": last_success.isoformat() if last_success else None,
            "update_interval_seconds": coordinator.update_interval.total_seconds(),
            "account_last_point": point_age(data.account) if data else None,
            "source_types_last_point": (
                {t: point_age(p) for t, p in data.source_types.items()} if data else {}
            ),
            "friends": (
                {
                    friend_id: {
                        "shares_live_location": friend.shares_live_location,
                        "has_position": friend.last_latitude is not None,
                        "last_seen": friend.last_seen,
                    }
                    for friend_id, friend in data.friends.items()
                }
                if data
                else {}
            ),
        },
        "export": runtime.exporter.diagnostics() if runtime.exporter else None,
    }
