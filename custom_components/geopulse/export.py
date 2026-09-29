"""Export direction (HA -> GeoPulse).

Replaces GeoPulse's documented rest_command + automation recipe: listens to
the selected device_trackers and POSTs each new position to
/api/homeassistant (Plan.md §5).

GeoPulse builds one timeline per *user*; `device_id` is only a label. So
trackers for different people must go to different GeoPulse accounts, i.e.
different location-source tokens. Each token gets its own lane - queue,
retry backoff and Repairs issue - so one account's bad token or outage
doesn't hold up the others. Lanes share one persisted Store, so with the
retry queue on, points survive GeoPulse outages and HA restarts and are
delivered in order per account.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
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
# Per lane; oldest points are dropped past this. At one point a minute per
# tracker that's days of outage for a handful of trackers.
MAX_QUEUE = 5000
RETRY_INITIAL = timedelta(seconds=30)
RETRY_MAX = timedelta(minutes=15)
ISSUE_EXPORT_AUTH = "export_auth_failed"


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.export_queue"


def build_payload(
    entity_id: str, device_id: str, new_state: State | None, old_state: State | None
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
        # Routes the point to its account's lane, also after a restart. The
        # token itself is never written to the queue file.
        "entity_id": entity_id,
        "device_id": device_id,
        "timestamp": new_state.last_updated.isoformat(),
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": attrs.get(ATTR_GPS_ACCURACY),
        "altitude": attrs.get("altitude"),
        "speed": attrs.get("speed"),
        "battery_level": battery,
    }


class _Lane:
    """Queue and delivery for one location-source token (one GeoPulse user)."""

    def __init__(
        self,
        exporter: GeoPulseExporter,
        client: GeoPulseClient,
        token: str,
        entity_ids: list[str],
    ) -> None:
        self._exporter = exporter
        self._client = client
        self.entity_ids = entity_ids
        self.queue: list[dict[str, Any]] = []
        self._backoff = RETRY_INITIAL
        self._flush_task: asyncio.Task[None] | None = None
        self._unsub_retry: CALLBACK_TYPE | None = None
        # Stable per token without putting the token in the issue id.
        digest = hashlib.sha256(token.encode()).hexdigest()[:12]
        self._issue_id = f"{ISSUE_EXPORT_AUTH}_{exporter.entry.entry_id}_{digest}"

    @callback
    def add(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)
        if len(self.queue) > MAX_QUEUE:
            dropped = len(self.queue) - MAX_QUEUE
            del self.queue[:dropped]
            _LOGGER.warning(
                "Export queue for %s full; dropped %d oldest point(s)",
                ", ".join(self.entity_ids), dropped,
            )
        self._exporter.schedule_save()
        # While a retry is pending this account is known to be failing; the
        # new point waits its turn rather than jumping the queue.
        if self._unsub_retry is None:
            self.start_flush()

    @callback
    def start_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = self._exporter.entry.async_create_background_task(
                self._exporter.hass, self._async_flush(), f"{DOMAIN} export flush"
            )

    @callback
    def stop(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    async def _async_flush(self) -> None:
        while self.queue:
            point = self.queue[0]
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
                if not self._drop_or_retry(point, "location source token rejected"):
                    return
                continue
            except GeoPulseApiError as err:
                if not self._drop_or_retry(point, str(err)):
                    return
                continue
            # Only removed once delivered: at-least-once, and GeoPulse's own
            # duplicate detection absorbs a resend after a crash mid-POST.
            self.queue.pop(0)
            self._backoff = RETRY_INITIAL
            self._clear_auth_issue()
            self._exporter.schedule_save()

    def _drop_or_retry(self, point: dict[str, Any], reason: str) -> bool:
        """Handle a failed send. Returns True to keep flushing."""
        if not self._exporter.retry:
            self.queue.remove(point)
            self._exporter.schedule_save()
            _LOGGER.warning("Dropped export point for %s: %s", point["device_id"], reason)
            return True
        _LOGGER.warning(
            "Export to GeoPulse for %s failed (%s); %d point(s) queued, retrying in %s",
            ", ".join(self.entity_ids), reason, len(self.queue), self._backoff,
        )
        self._unsub_retry = async_call_later(
            self._exporter.hass, self._backoff, self._retry_now
        )
        self._backoff = min(self._backoff * 2, RETRY_MAX)
        return False

    @callback
    def _retry_now(self, _now: datetime) -> None:
        self._unsub_retry = None
        self.start_flush()

    @callback
    def _raise_auth_issue(self) -> None:
        ir.async_create_issue(
            self._exporter.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_EXPORT_AUTH,
            translation_placeholders={
                "title": self._exporter.entry.title,
                "entities": ", ".join(self.entity_ids),
            },
        )

    @callback
    def _clear_auth_issue(self) -> None:
        ir.async_delete_issue(self._exporter.hass, DOMAIN, self._issue_id)


class GeoPulseExporter:
    """Push selected trackers to GeoPulse, one lane per location-source token."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GeoPulseConfigEntry,
        client_factory: Callable[[str], GeoPulseClient],
        device_ids: dict[str, str],
        entity_tokens: dict[str, str],
        retry: bool,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.retry = retry
        self._device_ids: dict[str, str] = {}
        self._lanes: dict[str, _Lane] = {}
        self._lane_for_entity: dict[str, _Lane] = {}
        for entity_id, device_id in device_ids.items():
            if not (token := entity_tokens.get(entity_id)):
                # The config flow requires one; only reachable via a
                # hand-edited entry.
                _LOGGER.warning("No GeoPulse token for %s; not exporting it", entity_id)
                continue
            if (lane := self._lanes.get(token)) is None:
                lane = self._lanes[token] = _Lane(self, client_factory(token), token, [])
            lane.entity_ids.append(entity_id)
            self._lane_for_entity[entity_id] = lane
            self._device_ids[entity_id] = device_id
        self._store: Store[list[dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._unsub_state: CALLBACK_TYPE | None = None

    @property
    def queue_length(self) -> int:
        return sum(len(lane.queue) for lane in self._lanes.values())

    async def async_start(self) -> None:
        # Queued points are kept even when the retry toggle is now off: they
        # were accepted while it was on.
        dropped = 0
        for point in await self._store.async_load() or []:
            lane = self._lane_for_entity.get(point.get("entity_id", ""))
            if lane is None:
                dropped += 1  # tracker no longer exported
                continue
            lane.queue.append(point)
        if dropped:
            _LOGGER.info("Dropped %d queued point(s) for trackers no longer exported", dropped)
            self.schedule_save()
        self._unsub_state = async_track_state_change_event(
            self.hass, list(self._device_ids), self._async_state_changed
        )
        for lane in self._lanes.values():
            if lane.queue:
                _LOGGER.info(
                    "Resuming export of %d queued point(s) for %s",
                    len(lane.queue), ", ".join(lane.entity_ids),
                )
                lane.start_flush()

    async def async_stop(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        for lane in self._lanes.values():
            lane.stop()
        await self._store.async_save(self._snapshot())

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        payload = build_payload(
            entity_id,
            self._device_ids[entity_id],
            event.data["new_state"],
            event.data["old_state"],
        )
        if payload is not None:
            self._lane_for_entity[entity_id].add(payload)

    @callback
    def schedule_save(self) -> None:
        self._store.async_delay_save(self._snapshot, SAVE_DELAY_SECONDS)

    def _snapshot(self) -> list[dict[str, Any]]:
        return [point for lane in self._lanes.values() for point in lane.queue]
