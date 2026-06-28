"""Number platform for the CoolLEDX integration."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CoolLEDXConfigEntry, CoolLEDXCoordinator
from .entity import CoolLEDXEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoolLEDXConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CoolLEDX speed number entity from a config entry."""
    async_add_entities([CoolLEDXSpeed(entry.runtime_data)])


class CoolLEDXSpeed(CoolLEDXEntity, NumberEntity):
    """Representation of the scroll/animation speed on a CoolLEDX sign.

    Exposes speed as a 0–255 integer slider.  State is optimistic: the sign does
    not report its current speed, so this entity caches the last-sent value on
    the coordinator.
    """

    _attr_name = "Speed"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 255.0
    _attr_native_step = 1.0
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: CoolLEDXCoordinator) -> None:
        """Initialise the speed entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_speed"

    @property
    def native_value(self) -> float:
        """Return the current speed as a float."""
        return float(self.coordinator.speed)

    async def async_set_native_value(self, value: float) -> None:
        """Send a new speed value to the sign.

        Connects to the device if needed, sends the integer speed, then updates
        the optimistic state on the coordinator and notifies HA.
        """
        dev = await self.coordinator.async_ensure_connected()
        await dev.set_speed(int(value))
        self.coordinator.speed = int(value)
        self.async_write_ha_state()
