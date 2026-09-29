"""The GeoPulse integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# Populated as each phase in DEVELOPMENT_PLAN.md lands: device_tracker (Phase 3).
PLATFORMS: list[str] = []

# homeassistant is only imported inside these functions, not at module level,
# so that leaf modules with no HA dependency (api.py, const.py) can be
# imported - and unit tested - without requiring the homeassistant package
# to be installed at all. Real runtime callers (HA itself) always have it.


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Set up GeoPulse from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
