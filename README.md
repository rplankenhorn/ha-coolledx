# ha-coolledx

Home Assistant custom integration for **CoolLEDX** Bluetooth LED matrix signs (sold under
names like the Rayhome "Devil Eyes" programmable LED sign). Controls the sign through Home
Assistant's Bluetooth stack, including **ESPHome Bluetooth proxies** — no phone app needed.

> Status: early development (v0.1.0). Not yet feature complete.

## Features (planned for v1)

- Power on/off, brightness, color, scroll speed, and display mode as Home Assistant entities
- Scrolling **text** messages (host-rendered to the sign's pixel matrix)
- Image and animation upload via services
- Auto-discovery over Bluetooth (including ESP32 Bluetooth proxies)

## Requirements

- Home Assistant 2024.8.0 or newer
- A Bluetooth adapter on the HA host **or** an ESPHome Bluetooth proxy in range of the sign
- A CoolLEDX-family sign (advertises as `CoolLEDX` / `CoolLEDM` / `CoolLEDU`)

## Identifying your device

Before relying on this integration, confirm your sign is CoolLEDX: scan with nRF Connect
and check that it advertises a `CoolLED*` name and exposes GATT service `0xFFF0` with a
writable characteristic `0xFFF1`. Other LED signs (e.g. those advertising `LED_BLE_*`) use
a different protocol and are not supported here.

## Installation (HACS custom repository)

1. In HACS, add `https://github.com/rplankenhorn/ha-coolledx` as a custom repository
   (category: Integration).
2. Install "CoolLEDX LED Sign" and restart Home Assistant.
3. The sign should be auto-discovered when a Bluetooth adapter or proxy sees it; otherwise
   add it manually by MAC address.

## Credits

- BLE protocol ported from [UpDryTwist/coolledx-driver](https://github.com/UpDryTwist/coolledx-driver) (MIT).
- Original CoolLEDX reverse engineering by [CrimsonClyde / led-faceshields](https://git.team23.org/CrimsonClyde/led-faceshields).

## License

MIT — see [LICENSE](LICENSE).
