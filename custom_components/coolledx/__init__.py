"""The CoolLEDX LED Sign integration.

Setup wiring (config entry -> coordinator -> platforms) is implemented in a later
phase. This stub defines the domain and the platforms that will be forwarded.
"""

from __future__ import annotations

from homeassistant.const import Platform

from .const import DOMAIN  # noqa: F401

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.TEXT,
    Platform.NUMBER,
    Platform.SELECT,
]
