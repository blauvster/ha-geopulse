"""Export direction (HA -> GeoPulse).

Replaces GeoPulse's documented rest_command + automation recipe: listens to
the selected device_trackers and POSTs each new position to
/api/homeassistant. Every point goes through a queue persisted with HA's
Store, so with the retry queue on, points survive GeoPulse outages and HA
restarts and are delivered in order (Plan.md §5).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store

from .api import GeoPulseApiError, GeoPulseAuthError, GeoPulseClient
from .const import DOMAIN

if TYPE_CHECKING:
    from . import GeoPulseConfigEntry

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 5
# Oldest points are dropped past this; at one point a minute per tracker
# that's days of outage for a handful of trackers.
MAX_QUEUE = 5000
RETRY_INITIAL = timedelta(seconds=30)
RETRY_MAX = timedelta(minutes=15)
ISSUE_EXPORT_AUTH = "export_auth_failed"


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.export_queue"


def build_payload(
    device_id: str, new_state: State | None, old_state: State | None
) -> dict[str, Any] | None:
    """Queue item for a state change, or None if there's nothing to send.

    Skips states without coordinates (router/zone-only trackers) and changes
    that didn't move the tracker (e.g. only battery or zone name changed).
    """
    if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    attrs = new_state.attributes
    latitude, longitude = attrs.get(ATTR_LATITUDE), attrs.get(ATTR_LONGITUDE)
    if latitude is None or longitude is None:
        return None
    if old_state is not None and all(
        old_state.attributes.get(key) == attrs.get(key)
        for key in (ATTR_LATITUDE, ATTR_LONGITUDE, ATTR_GPS_ACCURACY)
    ):
        return None
    # battery_level is the (deprecated, until 2027.7) tracker attribute;
    # `battery` covers trackers like this integration's own.
    battery = attrs.get(ATTR_BATTERY_LEVEL, attrs.get("battery"))
    return {
        "device_id": device_id,
        "timestamp": new_state.last_updated.isoformat(),
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": attrs.get(ATTR_GPS_ACCURACY),
        "altitude": attrs.get("altitude"),
        "speed": attrs.get("speed"),
        "battery_level": battery,
    }


class GeoPulseExporter:
    """Push selected trackers to GeoPulse through a persisted queue."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GeoPulseConfigEntry,
        client: GeoPulseClient,
        device_ids: dict[str, str],
        retry: bool,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._device_ids = device_ids
        self._retry = retry
        self._store: Store[list[dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._queue: list[dict[str, Any]] = []
        self._backoff = RETRY_INITIAL
        self._flush_task: asyncio.Task[None] | None = None
        self._unsub_retry: CALLBACK_TYPE | None = None
        self._unsub_state: CALLBACK_TYPE | None = None

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    async def async_start(self) -> None:
        # Queued points are kept even when the retry toggle is now off: they
        # were accepted while it was on.
        self._queue = await self._store.async_load() or []
        self._unsub_state = async_track_state_change_event(
            self._hass, list(self._device_ids), self._async_state_changed
        )
        if self._queue:
            _LOGGER.info("Resuming export of %d queued point(s)", len(self._queue))
            self._start_flush()

    async def async_stop(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._cancel_retry()
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        await self._store.async_save(self._queue)

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        payload = build_payload(
            self._device_ids[entity_id], event.data["new_state"], event.data["old_state"]
        )
        if payload is None:
            return
        self._queue.append(payload)
        if len(self._queue) > MAX_QUEUE:
            dropped = len(self._queue) - MAX_QUEUE
            del self._queue[:dropped]
            _LOGGER.warning("Export queue full; dropped %d oldest point(s)", dropped)
        self._schedule_save()
        # While a retry is pending GeoPulse is known to be failing; the new
        # point waits its turn rather than jumping the queue.
        if self._unsub_retry is None:
            self._start_flush()

    @callback
    def _start_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = self._entry.async_create_background_task(
                self._hass, self._async_flush(), f"{DOMAIN} export flush"
            )

    async def _async_flush(self) -> None:
        while self._queue:
            point = self._queue[0]
            try:
                await self._client.async_post_homeassistant_location(
                    device_id=point["device_id"],
                    timestamp=datetime.fromisoformat(point["timestamp"]),
                    latitude=point["latitude"],
                    longitude=point["longitude"],
                    accuracy=point["accuracy"],
                    altitude=point["altitude"],
                    speed=point["speed"],
                    battery_level=point["battery_level"],
                )
            except GeoPulseAuthError:
                self._raise_auth_issue()
                if not self._drop_or_retry(point, "export token rejected"):
                    return
                continue
            except GeoPulseApiError as err:
                if not self._drop_or_retry(point, str(err)):
                    return
                continue
            # Only removed once delivered: at-least-once, and GeoPulse's own
            # duplicate detection absorbs a resend after a crash mid-POST.
            self._queue.pop(0)
            self._backoff = RETRY_INITIAL
            self._clear_auth_issue()
            self._schedule_save()

    def _drop_or_retry(self, point: dict[str, Any], reason: str) -> bool:
        """Handle a failed send. Returns True to keep flushing."""
        if not self._retry:
            self._queue.remove(point)
            self._schedule_save()
            _LOGGER.warning("Dropped export point for %s: %s", point["device_id"], reason)
            return True
        _LOGGER.warning(
            "Export to GeoPulse failed (%s); %d point(s) queued, retrying in %s",
            reason, len(self._queue), self._backoff,
        )
        self._unsub_retry = async_call_later(self._hass, self._backoff, self._retry_now)
        self._backoff = min(self._backoff * 2, RETRY_MAX)
        return False

    @callback
    def _retry_now(self, _now: datetime) -> None:
        self._unsub_retry = None
        self._start_flush()

    @callback
    def _cancel_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(lambda: list(self._queue), SAVE_DELAY_SECONDS)

    @callback
    def _raise_auth_issue(self) -> None:
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            f"{ISSUE_EXPORT_AUTH}_{self._entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_EXPORT_AUTH,
            translation_placeholders={"title": self._entry.title},
        )

    @callback
    def _clear_auth_issue(self) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, f"{ISSUE_EXPORT_AUTH}_{self._entry.entry_id}")
