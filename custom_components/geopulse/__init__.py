"""The GeoPulse integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import GeoPulseClient
from .const import (
    CONF_BASE_URL,
    CONF_EXPORT_ENTITIES,
    CONF_EXPORT_ENTITY_TOKENS,
    CONF_EXPORT_RETRY_QUEUE,
    CONF_READ_TOKEN,
    DOMAIN,
)
from .coordinator import GeoPulseCoordinator
from .export import STORAGE_VERSION, GeoPulseExporter, storage_key
from .timeline import async_register_card, async_register_websocket

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class GeoPulseRuntimeData:
    """Per-entry runtime objects."""

    coordinator: GeoPulseCoordinator
    exporter: GeoPulseExporter | None


type GeoPulseConfigEntry = ConfigEntry[GeoPulseRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the card backend once, independent of config entries."""
    async_register_websocket(hass)
    # Always true in a real HA (default_config); skipped in minimal test setups.
    if "frontend" in hass.config.components and hass.http is not None:
        await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GeoPulseConfigEntry) -> bool:
    """Set up GeoPulse from a config entry."""
    session = async_get_clientsession(hass)
    base_url = entry.data[CONF_BASE_URL]
    exporter = None
    if export_entities := entry.options.get(CONF_EXPORT_ENTITIES, {}):
        # Separate clients: the export endpoint authenticates with
        # location-source tokens, not the read token (Plan.md §2).
        exporter = GeoPulseExporter(
            hass,
            entry,
            lambda token: GeoPulseClient(session, base_url, token),
            export_entities,
            entity_tokens=entry.data.get(CONF_EXPORT_ENTITY_TOKENS, {}),
            retry=entry.options.get(CONF_EXPORT_RETRY_QUEUE, True),
        )
        await exporter.async_start()

    coordinator = GeoPulseCoordinator(
        hass, entry, GeoPulseClient(session, base_url, entry.data[CONF_READ_TOKEN])
    )
    # Deliberately not async_config_entry_first_refresh(): failing setup
    # while GeoPulse is down would also stop the exporter from capturing
    # points - exactly the outage the retry queue exists for. Trackers start
    # unavailable and recover on the next poll; an auth failure still starts
    # reauth.
    await coordinator.async_refresh()

    entry.runtime_data = GeoPulseRuntimeData(coordinator=coordinator, exporter=exporter)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GeoPulseConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and (exporter := entry.runtime_data.exporter) is not None:
        await exporter.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: GeoPulseConfigEntry) -> None:
    """Drop the persisted export queue with the entry."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
