"""Constants for the CoolLEDX integration."""

DOMAIN = "coolledx"

# BLE GATT
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# Advertised name prefixes for this device family
NAME_PREFIXES = ("CoolLEDX", "CoolLEDM", "CoolLEDU")

# Config
CONF_ADDRESS = "address"

# Color modes reported in advertisement manufacturer data
COLOR_MODE_MONO = 0
COLOR_MODE_SEVEN = 1
COLOR_MODE_RGB = 2
