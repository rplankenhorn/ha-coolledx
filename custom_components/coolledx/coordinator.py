"""Coordinator for the CoolLEDX integration."""

from __future__ import annotations

import logging

from bleak.backends.device import BLEDevice

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .device import CoolLEDXDevice

_LOGGER = logging.getLogger(__name__)

type CoolLEDXConfigEntry = ConfigEntry["CoolLEDXCoordinator"]


def parse_geometry(
    service_info: BluetoothServiceInfoBleak,
) -> tuple[int | None, int | None, int | None]:
    """Parse height, width, and color_mode from BLE manufacturer data.

    The CoolLEDX manufacturer-data value bytes encode device geometry:
      index [6]   = height in pixels
      index [7:9] = width in pixels (big-endian uint16)
      index [9]   = color_mode  (0=mono, 1=seven-colour, 2=RGB,
                    3=full-colour as reported by CoolLEDUX hardware)

    Iterates over all manufacturer IDs present in the advertisement because
    the ID itself is not standardised across device revisions.

    Returns:
        ``(height, width, color_mode)`` on success, or
        ``(None, None, None)`` if the data are absent or malformed.
    """
    for _mfr_id, data in service_info.manufacturer_data.items():
        try:
            if len(data) < 10:
                continue
            height: int = data[6]
            width: int = int.from_bytes(data[7:9], "big")
            color_mode: int = data[9]
            # Sanity-check values before accepting them.  CoolLEDUX signs
            # report color_mode 3 (full colour); the renderer always emits RGB
            # bitfields, so any 0..3 mode is safe to accept here.
            if height > 0 and width > 0 and 0 <= color_mode <= 3:
                return height, width, color_mode
        except (IndexError, ValueError, TypeError):
            _LOGGER.debug(
                "Failed to parse manufacturer data from %s for geometry",
                service_info.address,
                exc_info=True,
            )
    return None, None, None


class CoolLEDXCoordinator:
    """Manage BLE connectivity and optimistic state for a CoolLEDX sign.

    Intentionally not a ``DataUpdateCoordinator`` because the device is
    largely write-only and does not push state back.  Entities call
    ``async_ensure_connected`` before writing commands, then update the
    optimistic attributes on this coordinator so they can be read back.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        height: int,
        width: int,
        color_mode: int,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass:       The Home Assistant instance.
            address:    BLE MAC address of the device (upper-case).
            name:       Human-readable name used for logging and as
                        the ``name`` argument to ``CoolLEDXDevice``.
            height:     Sign pixel height parsed from the advertisement
                        (or the default of 16).
            width:      Sign pixel width parsed from the advertisement
                        (or the default of 96).
            color_mode: Color mode constant parsed from the advertisement
                        (or the default ``COLOR_MODE_RGB``).
        """
        self.hass = hass
        self.address = address
        self.name = name
        self.height = height
        self.width = width
        self.color_mode = color_mode

        self._device: CoolLEDXDevice | None = None

        # ------------------------------------------------------------------
        # Optimistic state — the hardware is write-only so entities track
        # the last value they sent and report it back via these attributes.
        # ------------------------------------------------------------------
        self.is_on: bool = False
        self.brightness: int = 255
        self.rgb_color: tuple[int, int, int] = (255, 255, 255)
        self.effect: int | None = None
        self.speed: int = 128
        self.text: str = ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_ble_device(self) -> BLEDevice | None:
        """Return the freshest ``BLEDevice`` from the HA Bluetooth stack.

        Using the freshest device ensures writes route through the best
        available proxy even if the Bluetooth topology has changed since
        the coordinator was constructed.
        """
        return async_ble_device_from_address(
            self.hass, self.address.upper(), connectable=True
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Verify the device is reachable and create the ``CoolLEDXDevice``.

        Called once from ``async_setup_entry`` in ``__init__.py``.

        Raises:
            ConfigEntryNotReady: If the device cannot be found via the
                Bluetooth stack (no adapter or proxy within range).
        """
        ble_device = self._get_ble_device()
        if ble_device is None:
            raise ConfigEntryNotReady(
                f"CoolLEDX device {self.address} is not reachable; "
                "ensure a Bluetooth adapter or ESPHome proxy can see it"
            )
        self._device = CoolLEDXDevice(
            ble_device=ble_device,
            name=self.name,
            height=self.height,
            width=self.width,
            color_mode=self.color_mode,
        )

    async def async_ensure_connected(self) -> CoolLEDXDevice:
        """Return a connected ``CoolLEDXDevice``, connecting if necessary.

        Refreshes the underlying ``BLEDevice`` from the Bluetooth stack on
        every call so proxy-routing stays optimal.  The device's own
        ``asyncio.Lock`` serialises concurrent writes.

        Returns:
            The ready-to-use ``CoolLEDXDevice``.

        Raises:
            ConfigEntryNotReady: If the device is not reachable.
        """
        ble_device = self._get_ble_device()
        if ble_device is None:
            raise ConfigEntryNotReady(
                f"CoolLEDX device {self.address} is not reachable"
            )

        if self._device is None:
            self._device = CoolLEDXDevice(
                ble_device=ble_device,
                name=self.name,
                height=self.height,
                width=self.width,
                color_mode=self.color_mode,
            )
        else:
            # Update to the freshest BLEDevice — proxy may have changed.
            self._device._ble_device = ble_device

        if not self._device.is_connected:
            await self._device.connect()

        return self._device

    async def async_disconnect(self) -> None:
        """Disconnect from the device; safe to call when already disconnected."""
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Error during disconnect from %s", self.address, exc_info=True
                )
