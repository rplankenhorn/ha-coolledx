"""The CoolLEDX LED Sign integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    CONF_COLOR_MODE,
    CONF_HEIGHT,
    CONF_WIDTH,
    DEFAULT_COLOR_MODE,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
)
from .coordinator import CoolLEDXCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.TEXT,
    Platform.NUMBER,
    Platform.SELECT,
]

# Modern HA typed config-entry alias.  ``entry.runtime_data`` holds the
# coordinator; platform files import this type for their own type hints.
type CoolLEDXConfigEntry = ConfigEntry[CoolLEDXCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CoolLEDXConfigEntry) -> bool:
    """Set up CoolLEDX from a config entry.

    1. Read geometry from the entry data (written by config_flow from the
       advertisement, or defaults if advertisement data was absent).
    2. Build a ``CoolLEDXCoordinator`` and call ``async_setup`` to verify
       the device is reachable — raises ``ConfigEntryNotReady`` if not.
    3. Store the coordinator in ``entry.runtime_data`` (modern HA pattern).
    4. Forward setup to all platform modules.
    5. Register a disconnect callback for clean teardown.
    """
    address: str = entry.data[CONF_ADDRESS]
    height: int = entry.data.get(CONF_HEIGHT, DEFAULT_HEIGHT)
    width: int = entry.data.get(CONF_WIDTH, DEFAULT_WIDTH)
    color_mode: int = entry.data.get(CONF_COLOR_MODE, DEFAULT_COLOR_MODE)

    coordinator = CoolLEDXCoordinator(
        hass=hass,
        address=address,
        name=entry.title,
        height=height,
        width=width,
        color_mode=color_mode,
    )

    # Raises ConfigEntryNotReady if the device cannot be found.
    await coordinator.async_setup()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Disconnect when the entry is unloaded (e.g. integration removed or
    # Home Assistant stopping) so the BLE connection is released cleanly.
    entry.async_on_unload(coordinator.async_disconnect)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CoolLEDXConfigEntry) -> bool:
    """Unload a CoolLEDX config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_disconnect()
    return unload_ok
