"""Switch platform for the CoolLEDX integration."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CoolLEDXConfigEntry, CoolLEDXCoordinator
from .entity import CoolLEDXEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoolLEDXConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CoolLEDX invert switch entity from a config entry."""
    async_add_entities([CoolLEDXInvertSwitch(entry.runtime_data)])


class CoolLEDXInvertSwitch(CoolLEDXEntity, SwitchEntity):
    """Representation of the invert-orientation setting on a CoolLEDX sign.

    When on, rendered content is rotated 180° before being sent to the sign,
    for signs mounted upside-down.  State is optimistic: the sign does not
    report its current orientation, so this entity caches the last-sent
    value on the coordinator (persisted to the config entry).
    """

    _attr_translation_key = "invert_display"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CoolLEDXCoordinator) -> None:
        """Initialise the invert switch entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_invert"

    @property
    def is_on(self) -> bool:
        """Return True if the display is currently rendered inverted."""
        return self.coordinator.invert

    async def async_turn_on(self, **kwargs) -> None:
        """Enable invert orientation."""
        await self.coordinator.async_set_invert(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable invert orientation."""
        await self.coordinator.async_set_invert(False)
        self.async_write_ha_state()
