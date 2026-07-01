"""Text platform for the CoolLEDX integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CoolLEDXConfigEntry, CoolLEDXCoordinator
from .device import UX_MODE_MAP
from .entity import CoolLEDXEntity

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_IMAGE = "send_image"
SERVICE_SEND_ANIMATION = "send_animation"
SERVICE_DISPLAY_TEXT = "display_text"


def _read_bytes(path: str) -> bytes:
    """Read a file's bytes (runs in the executor, off the event loop)."""
    with open(path, "rb") as fh:
        return fh.read()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoolLEDXConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CoolLEDX text entity from a config entry."""
    async_add_entities([CoolLEDXText(entry.runtime_data)])

    # Image/animation upload are exposed as entity services targeting the
    # message entity (the natural "content" entity for the sign).
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_IMAGE,
        {vol.Required("path"): cv.string},
        "async_send_image",
    )
    platform.async_register_entity_service(
        SERVICE_SEND_ANIMATION,
        {vol.Required("path"): cv.string},
        "async_send_animation",
    )
    platform.async_register_entity_service(
        SERVICE_DISPLAY_TEXT,
        {
            vol.Required("text"): cv.string,
            vol.Optional("color"): vol.All(
                [vol.All(vol.Coerce(int), vol.Range(min=0, max=255))],
                vol.Length(min=3, max=3),
            ),
            vol.Optional("mode"): vol.In(list(UX_MODE_MAP)),
            vol.Optional("speed"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
            vol.Optional("brightness"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=255)
            ),
            vol.Optional("invert"): cv.boolean,
        },
        "async_display_text",
    )


class CoolLEDXText(CoolLEDXEntity, TextEntity):
    """Representation of the scrolling message on a CoolLEDX sign.

    Allows the user to set the text displayed on the sign.  State is optimistic:
    the sign does not report its current text, so this entity caches the last-sent
    value on the coordinator.
    """

    _attr_translation_key = "message"
    _attr_native_max = 255
    _attr_mode = TextMode.TEXT

    def __init__(self, coordinator: CoolLEDXCoordinator) -> None:
        """Initialise the text entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_text"

    @property
    def native_value(self) -> str | None:
        """Return the current message text from optimistic coordinator state."""
        return self.coordinator.text

    async def async_set_value(self, value: str) -> None:
        """Send a new message to the sign.

        Connects to the device if needed, sends the text with the current colour,
        then updates the optimistic state on the coordinator and notifies HA.
        """
        dev = await self.coordinator.async_ensure_connected()
        await dev.set_text(value, self.coordinator.rgb_color)
        self.coordinator.text = value
        self.coordinator.is_on = True
        self.async_write_ha_state()

    async def async_send_image(self, path: str) -> None:
        """Send a still image file to the sign (``coolledx.send_image`` service)."""
        dev = await self.coordinator.async_ensure_connected()
        await dev.send_image(path)
        self.coordinator.is_on = True
        self.async_write_ha_state()

    async def async_send_animation(self, path: str) -> None:
        """Send a .jt/GIF animation file (``coolledx.send_animation`` service)."""
        dev = await self.coordinator.async_ensure_connected()
        data = await self.hass.async_add_executor_job(_read_bytes, path)
        await dev.send_animation(data)
        self.coordinator.is_on = True
        self.async_write_ha_state()

    async def async_display_text(
        self,
        text: str,
        color: list[int] | None = None,
        mode: str | None = None,
        speed: int | None = None,
        brightness: int | None = None,
        invert: bool | None = None,
    ) -> None:
        """coolledx.display_text service: set text plus optional color/mode/speed/brightness/invert in one call."""
        dev = await self.coordinator.async_ensure_connected()

        if brightness is not None:
            await dev.set_brightness(brightness)
            self.coordinator.brightness = brightness

        if invert is not None:
            # Transient per-call override; persisting the default is the
            # switch entity's job, not this service.
            dev.invert = invert
            self.coordinator.invert = invert

        rgb = tuple(color) if color is not None else self.coordinator.rgb_color
        mode_int = UX_MODE_MAP[mode] if mode is not None else None

        await dev.set_text(text, rgb, mode=mode_int, speed=speed)

        self.coordinator.text = text
        self.coordinator.rgb_color = rgb
        if mode_int is not None:
            self.coordinator.effect = mode_int
        if speed is not None:
            self.coordinator.speed = speed
        self.coordinator.is_on = True
        self.async_write_ha_state()
