"""The GeoPulse integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GeoPulseClient
from .const import CONF_BASE_URL, CONF_READ_TOKEN
from .coordinator import GeoPulseCoordinator

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER]


@dataclass
class GeoPulseRuntimeData:
    """Per-entry runtime objects. Phase 4 adds the exporter here."""

    coordinator: GeoPulseCoordinator


type GeoPulseConfigEntry = ConfigEntry[GeoPulseRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: GeoPulseConfigEntry) -> bool:
    """Set up GeoPulse from a config entry."""
    client = GeoPulseClient(
        async_get_clientsession(hass), entry.data[CONF_BASE_URL], entry.data[CONF_READ_TOKEN]
    )
    coordinator = GeoPulseCoordinator(hass, entry, client)
    # Raises ConfigEntryNotReady / ConfigEntryAuthFailed on failure, which HA
    # turns into setup retry / reauth.
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = GeoPulseRuntimeData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GeoPulseConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
