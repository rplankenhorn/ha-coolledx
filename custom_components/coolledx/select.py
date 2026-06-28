"""Select platform for the CoolLEDX integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CoolLEDXConfigEntry, CoolLEDXCoordinator
from .entity import CoolLEDXEntity

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode map — display-mode name (shown in the HA UI) -> device mode integer.
# The integer values must match those in CoolLEDXLight's EFFECT_MAP because
# coordinator.effect is shared between the light's effect feature and this
# select entity — selecting a mode here is equivalent to activating an effect
# on the light entity.
# ---------------------------------------------------------------------------
MODE_MAP: dict[str, int] = {
    "Static": 0,
    "Scroll Left": 1,
    "Scroll Right": 2,
    "Scroll Up": 3,
    "Scroll Down": 4,
    "Snowflake": 5,
    "Picture": 6,
    "Laser": 7,
}

MODE_MAP_REVERSE: dict[int, str] = {v: k for k, v in MODE_MAP.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoolLEDXConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CoolLEDX display-mode select entity from a config entry."""
    async_add_entities([CoolLEDXMode(entry.runtime_data)])


class CoolLEDXMode(CoolLEDXEntity, SelectEntity):
    """Representation of the display/scroll mode on a CoolLEDX sign.

    The mode integer is stored in ``coordinator.effect`` and shared with the
    light entity's effect feature — selecting a mode here and activating an
    effect via the light entity are equivalent operations on the hardware.

    State is optimistic: the sign does not report its current mode.
    """

    _attr_name = "Display mode"
    _attr_options: list[str] = list(MODE_MAP)

    def __init__(self, coordinator: CoolLEDXCoordinator) -> None:
        """Initialise the display-mode select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_mode"

    @property
    def current_option(self) -> str | None:
        """Return the currently active display-mode name, or None if unknown."""
        mode_int = self.coordinator.effect
        if mode_int is None:
            return None
        return MODE_MAP_REVERSE.get(mode_int)

    async def async_select_option(self, option: str) -> None:
        """Apply a new display mode to the sign.

        Connects to the device if needed, sends the mode integer that corresponds
        to ``option``, then updates the optimistic state on the coordinator and
        notifies HA.
        """
        dev = await self.coordinator.async_ensure_connected()
        mode_int = MODE_MAP[option]
        await dev.set_mode(mode_int)
        self.coordinator.effect = mode_int
        self.async_write_ha_state()
