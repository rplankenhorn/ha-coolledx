"""Config flow for the CoolLEDX integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_COLOR_MODE,
    CONF_HEIGHT,
    CONF_WIDTH,
    CONF_ADDRESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    DOMAIN,
    NAME_PREFIXES,
)
from .coordinator import parse_geometry

_LOGGER = logging.getLogger(__name__)


class CoolLEDXConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CoolLEDX."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    # ------------------------------------------------------------------
    # Bluetooth-triggered discovery path
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Bluetooth discovery.

        Called automatically by Home Assistant when an advertisement
        matching the ``bluetooth`` filter in ``manifest.json`` is seen.
        """
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm adding the discovered device."""
        assert self._discovery_info is not None
        discovery_info = self._discovery_info

        if user_input is not None:
            return self._create_entry_from_discovery(discovery_info)

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": discovery_info.name or discovery_info.address,
            },
        )

    # ------------------------------------------------------------------
    # User-initiated (manual) path
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step: show a dropdown of discovered CoolLED* devices."""
        if user_input is not None:
            address: str = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            return self._create_entry_from_discovery(discovery_info)

        # Populate the device list.
        if self._discovery_info is not None:
            # Came here from async_step_bluetooth (shouldn't happen normally,
            # but handle it gracefully).
            self._discovered_devices[self._discovery_info.address] = (
                self._discovery_info
            )
        else:
            current_addresses = self._async_current_ids(include_ignore=False)
            for discovery_info in async_discovered_service_info(self.hass):
                if discovery_info.address in current_addresses:
                    continue
                if discovery_info.address in self._discovered_devices:
                    continue
                if not any(
                    discovery_info.name.startswith(prefix)
                    for prefix in NAME_PREFIXES
                ):
                    continue
                self._discovered_devices[discovery_info.address] = discovery_info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {
                        service_info.address: (
                            f"{service_info.name} ({service_info.address})"
                        )
                        for service_info in self._discovered_devices.values()
                    }
                )
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _create_entry_from_discovery(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Build and return a config entry from a BluetoothServiceInfoBleak.

        Parses geometry from manufacturer data; falls back to defaults if the
        data are absent or cannot be decoded.
        """
        height, width, color_mode = parse_geometry(discovery_info)
        return self.async_create_entry(
            title=discovery_info.name or discovery_info.address,
            data={
                CONF_ADDRESS: discovery_info.address,
                CONF_HEIGHT: height if height is not None else DEFAULT_HEIGHT,
                CONF_WIDTH: width if width is not None else DEFAULT_WIDTH,
                CONF_COLOR_MODE: (
                    color_mode if color_mode is not None else DEFAULT_COLOR_MODE
                ),
            },
        )
