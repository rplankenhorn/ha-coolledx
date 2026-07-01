"""Light platform for the CoolLEDX integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COLOR_MODE_MONO, COLOR_MODE_RGB, COLOR_MODE_SEVEN
from .coordinator import CoolLEDXConfigEntry, CoolLEDXCoordinator
from .device import UX_MODE_MAP
from .entity import CoolLEDXEntity

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Effect map — effect name (shown in the HA UI) -> device mode integer.
# Uses the CoolLEDUX mode numbering (UX_MODE_MAP in device.py).  Shares its
# integer values with the select entity's MODE_MAP (both derive from the same
# map) via the coordinator's stored effect value.
# ---------------------------------------------------------------------------
EFFECT_MAP: dict[str, int] = dict(UX_MODE_MAP)

EFFECT_MAP_REVERSE: dict[int, str] = {v: k for k, v in EFFECT_MAP.items()}

EFFECT_LIST: list[str] = list(EFFECT_MAP)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoolLEDXConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CoolLEDX light from a config entry."""
    async_add_entities([CoolLEDXLight(entry.runtime_data)])


class CoolLEDXLight(CoolLEDXEntity, LightEntity):
    """Representation of a CoolLEDX LED matrix sign as a HA light entity.

    State is optimistic: the sign does not report its current state, so this
    entity caches the last-sent values on the coordinator and reflects them
    back to Home Assistant.
    """

    _attr_name = None  # Primary feature — inherits the device name.
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = EFFECT_LIST

    def __init__(self, coordinator: CoolLEDXCoordinator) -> None:
        """Initialise the light entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_light"

        # Determine HA ColorMode from the device's advertised color capability.
        if coordinator.color_mode in (COLOR_MODE_RGB, COLOR_MODE_SEVEN):
            # COLOR_MODE_SEVEN: device snaps to nearest of 7 colours; we still
            # send full RGB so HA treats it as RGB.
            self._attr_color_mode = ColorMode.RGB
            self._attr_supported_color_modes = {ColorMode.RGB}
        else:
            # COLOR_MODE_MONO: brightness-only.
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    # ------------------------------------------------------------------
    # State properties (read from optimistic coordinator state)
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        """Return True if the sign is on."""
        return self.coordinator.is_on

    @property
    def brightness(self) -> int:
        """Return the current brightness (0-255)."""
        return self.coordinator.brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the current RGB colour, or None in brightness-only mode."""
        if self._attr_color_mode == ColorMode.RGB:
            return self.coordinator.rgb_color
        return None

    @property
    def effect(self) -> str | None:
        """Return the current effect name, or None when no effect is active."""
        mode_int = self.coordinator.effect
        if mode_int is None:
            return None
        return EFFECT_MAP_REVERSE.get(mode_int)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the sign on, optionally applying colour, brightness, or effect.

        All attribute changes are applied *before* the turn_on command so the
        sign lights up in the requested state rather than the previous one.
        """
        dev = await self.coordinator.async_ensure_connected()

        if ATTR_EFFECT in kwargs:
            effect_name: str = kwargs[ATTR_EFFECT]
            mode_int = EFFECT_MAP.get(effect_name)
            if mode_int is not None:
                await dev.set_mode(mode_int)
                self.coordinator.effect = mode_int
            else:
                _LOGGER.warning("Unknown CoolLEDX effect: %s", effect_name)

        if ATTR_RGB_COLOR in kwargs and self._attr_color_mode == ColorMode.RGB:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await dev.set_color(r, g, b)
            self.coordinator.rgb_color = (r, g, b)

        if ATTR_BRIGHTNESS in kwargs:
            brightness: int = kwargs[ATTR_BRIGHTNESS]
            await dev.set_brightness(brightness)
            self.coordinator.brightness = brightness

        await dev.turn_on()
        self.coordinator.is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the sign off."""
        dev = await self.coordinator.async_ensure_connected()
        await dev.turn_off()
        self.coordinator.is_on = False
        self.async_write_ha_state()
