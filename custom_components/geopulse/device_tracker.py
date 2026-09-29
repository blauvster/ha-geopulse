"""Imported GeoPulse positions as device_tracker entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import Friend, GpsPoint
from .const import (
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_RECORDER_EXCLUDE,
    SOURCE_TYPE_LABELS,
)
from .coordinator import GeoPulseCoordinator, GeoPulseData

if TYPE_CHECKING:
    from . import GeoPulseConfigEntry

ATTR_ALTITUDE = "altitude"
ATTR_SPEED = "speed"
ATTR_BATTERY = "battery"
ATTR_LAST_SEEN = "last_seen"
ATTR_GEOPULSE_SOURCE = "geopulse_source"


@dataclass(frozen=True)
class Position:
    """A current position, normalized across own-account and friend data."""

    latitude: float
    longitude: float
    accuracy: float | None = None
    altitude: float | None = None
    speed: float | None = None
    battery: float | None = None
    last_seen: datetime | None = None
    source_type: str | None = None

    @classmethod
    def from_point(cls, point: GpsPoint | None) -> Position | None:
        if point is None:
            return None
        return cls(
            latitude=point.latitude,
            longitude=point.longitude,
            accuracy=point.accuracy,
            altitude=point.altitude,
            # GeoPulse stores km/h for every source; HA trackers use m/s.
            speed=point.velocity / 3.6 if point.velocity is not None else None,
            battery=point.battery,
            last_seen=point.timestamp,
            source_type=point.source_type,
        )

    @classmethod
    def from_friend(cls, friend: Friend | None) -> Position | None:
        # A friend who stopped sharing, or has never reported, has no position.
        if (
            friend is None
            or not friend.shares_live_location
            or friend.last_latitude is None
            or friend.last_longitude is None
        ):
            return None
        return cls(
            latitude=friend.last_latitude,
            longitude=friend.last_longitude,
            battery=friend.last_battery,
            last_seen=dt_util.parse_datetime(friend.last_seen) if friend.last_seen else None,
        )


PositionGetter = Callable[[GeoPulseData], Position | None]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoPulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    options = entry.options
    tracker_cls = (
        GeoPulseUnrecordedTracker
        if options.get(CONF_RECORDER_EXCLUDE, True)
        else GeoPulseTracker
    )

    entities: list[GeoPulseTracker] = []
    if options.get(CONF_IMPORT_AGGREGATE_ACCOUNT):
        entities.append(
            tracker_cls(
                coordinator, "account", "GeoPulse account",
                lambda data: Position.from_point(data.account),
            )
        )
    for source_type in options.get(CONF_IMPORT_SOURCE_TYPES, []):
        entities.append(
            tracker_cls(
                coordinator,
                f"source_{source_type.lower()}",
                f"GeoPulse {SOURCE_TYPE_LABELS.get(source_type, source_type)}",
                lambda data, t=source_type: Position.from_point(data.source_types.get(t)),
            )
        )
    for friend_id in options.get(CONF_IMPORT_FRIEND_IDS, []):
        # No data if GeoPulse was down at startup; the name only matters for
        # the entity_id on first registration, which follows a live flow.
        friend = coordinator.data.friends.get(friend_id) if coordinator.data else None
        label = (friend and (friend.full_name or friend.email)) or f"friend {friend_id[:8]}"
        entities.append(
            tracker_cls(
                coordinator,
                f"friend_{friend_id}",
                f"GeoPulse {label}",
                lambda data, f=friend_id: Position.from_friend(data.friends.get(f)),
            )
        )

    # Deselected targets would otherwise linger as orphaned registry entries.
    registry = er.async_get(hass)
    wanted = {entity.unique_id for entity in entities}
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "device_tracker" and reg_entry.unique_id not in wanted:
            registry.async_remove(reg_entry.entity_id)

    async_add_entities(entities)


class GeoPulseTracker(CoordinatorEntity[GeoPulseCoordinator], TrackerEntity):
    """One imported position: whole account, one source type, or one friend."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GeoPulseCoordinator,
        key: str,
        name: str,
        get_position: PositionGetter,
    ) -> None:
        super().__init__(coordinator)
        self._get_position = get_position
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_name = name
        self._unsub_grace: CALLBACK_TYPE | None = None
        self._update_from_data()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_grace_timer)

    @property
    def available(self) -> bool:
        """Keep the last position through short GeoPulse outages."""
        if self.coordinator.last_update_success:
            return True
        last_success = self.coordinator.last_update_success_time
        return (
            last_success is not None
            and dt_util.utcnow() - last_success < self.coordinator.unavailable_grace
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_data()
        self._cancel_grace_timer()
        # The coordinator only notifies on the *first* failure of a run, so
        # without this nothing would re-check `available` once the grace
        # period lapses and the stale position would be shown indefinitely.
        last_success = self.coordinator.last_update_success_time
        if not self.coordinator.last_update_success and last_success is not None:
            remaining = last_success + self.coordinator.unavailable_grace - dt_util.utcnow()
            if remaining.total_seconds() > 0:
                self._unsub_grace = async_call_later(
                    self.hass, remaining, self._grace_expired
                )
        super()._handle_coordinator_update()

    @callback
    def _grace_expired(self, _now: datetime) -> None:
        self._unsub_grace = None
        self.async_write_ha_state()

    @callback
    def _cancel_grace_timer(self) -> None:
        if self._unsub_grace is not None:
            self._unsub_grace()
            self._unsub_grace = None

    @callback
    def _update_from_data(self) -> None:
        position = self._get_position(self.coordinator.data) if self.coordinator.data else None
        if position is None:
            # No point yet: state becomes unknown rather than a fake location.
            self._attr_latitude = None
            self._attr_longitude = None
            self._attr_location_accuracy = 0
            self._attr_extra_state_attributes = {}
            return
        self._attr_latitude = position.latitude
        self._attr_longitude = position.longitude
        self._attr_location_accuracy = position.accuracy or 0
        attrs: dict[str, Any] = {
            ATTR_ALTITUDE: position.altitude,
            ATTR_SPEED: position.speed,
            ATTR_BATTERY: position.battery,
            ATTR_LAST_SEEN: position.last_seen.isoformat() if position.last_seen else None,
            ATTR_GEOPULSE_SOURCE: position.source_type,
        }
        self._attr_extra_state_attributes = {k: v for k, v in attrs.items() if v is not None}


class GeoPulseUnrecordedTracker(GeoPulseTracker):
    """Tracker whose location attributes never reach HA's recorder.

    This is how history stays GeoPulse-only (Plan.md §6). HA core has no
    per-entity recorder-exclude registry option - the recorder only honours
    its YAML filter - so per-entity-class `_unrecorded_attributes` is the
    supported mechanism. The zone state (home/not_home/zone name) is still
    recorded; coordinates, accuracy, altitude, speed and timing are not.
    """

    _unrecorded_attributes = frozenset(
        {
            ATTR_LATITUDE,
            ATTR_LONGITUDE,
            ATTR_GPS_ACCURACY,
            ATTR_ALTITUDE,
            ATTR_SPEED,
            ATTR_LAST_SEEN,
        }
    )
