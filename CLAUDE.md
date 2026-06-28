# ha-coolledx — Project Guide for Claude Code

Home Assistant **custom integration** (HACS-distributed) that controls a CoolLEDX /
"Rayhome Devil Eyes" Bluetooth LED matrix sign through Home Assistant's Bluetooth stack,
including **ESPHome Bluetooth proxies**. Not a HA add-on.

## Repo layout

```
custom_components/coolledx/   # the integration
  device.py        # CoolLEDX BLE protocol: framing/escape/chunk + Pillow rendering (NO HA imports)
  coordinator.py   # connection mgmt via bleak-retry-connector
  config_flow.py   # bluetooth + manual setup
  light.py / text.py / number.py / select.py   # entities
hacs.json          # HACS metadata (root)
icons/             # brand icon
.github/workflows/validate.yml  # hassfest + HACS Action
```

## Conventions

- `device.py` stays free of `homeassistant` imports so the protocol is unit-testable.
- Connect via `bleak-retry-connector.establish_connection(...)` using the `BLEDevice`
  from `bluetooth.async_ble_device_from_address(...)` — never raw `BleakClient`, so HA
  proxies are used automatically.
- Optimistic state (device is largely write-only).
- Patterns to mirror: HA core `led_ble`, `8none1/lednetwf_ble`, `8none1/bj_led`.
- Protocol source ported from `UpDryTwist/coolledx-driver` (MIT — keep attribution).

## Dev / test

```bash
python -m pytest            # unit tests (device.py protocol vectors)
# hassfest + HACS validation run in CI (.github/workflows/validate.yml)
```

## Security — PUBLIC REPO, NO SECRETS

No API keys/tokens needed (BLE is local). Never commit: device MAC addresses, `.env`,
or captured `btsnoop`/pcap files (they leak your MAC — scrub or keep out of git). Review
`git diff` before every push.

## Issue tracking

This project uses **bd (beads)**. See `AGENTS.md` for the workflow. Use `bd ready` to find
work, `bd update <id> --claim` to start, `bd close <id>` when done.
