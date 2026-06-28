"""Base entity for the CoolLEDX integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import CoolLEDXCoordinator


class CoolLEDXEntity(Entity):
    """Common base for all CoolLEDX entities.

    Holds a reference to the shared :class:`CoolLEDXCoordinator` and exposes a
    single Home Assistant device for the sign. Entities are optimistic: the
    hardware does not report state back, so each entity writes via the
    coordinator's ``CoolLEDXDevice`` and mirrors the value onto the coordinator.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: CoolLEDXCoordinator) -> None:
        """Initialise the entity with its coordinator."""
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.name,
            manufacturer="CoolLEDX",
        )

    @property
    def available(self) -> bool:
        """Return True; reachability is surfaced via write errors, not polling."""
        return True
