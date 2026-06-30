"""
CoolLEDX BLE protocol layer for Home Assistant integration.

Protocol logic ported from UpDryTwist/coolledx-driver (MIT License):
  https://github.com/UpDryTwist/coolledx-driver

Protocol analysis additionally informed by CrimsonClyde/led-faceshields
  (GPL-2.0, https://git.team23.org/CrimsonClyde/led-faceshields).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from pathlib import Path
from typing import Union

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from PIL import Image, ImageDraw, ImageFont

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BLE identifiers
# ---------------------------------------------------------------------------

WRITE_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# ---------------------------------------------------------------------------
# Command bytes  (verified against upstream hardware.py)
# ---------------------------------------------------------------------------

CMD_TEXT = 0x02        # text/rendered-image (chunked)
CMD_IMAGE = 0x03       # static image (chunked)
CMD_ANIMATION = 0x04   # animation (chunked)
CMD_MODE = 0x06        # display mode
CMD_SPEED = 0x07       # scroll speed  0x00–0xFF
CMD_BRIGHTNESS = 0x08  # brightness    0x00–0xFF
CMD_SWITCH = 0x09      # on=0x01 / off=0x00
CMD_POWER_DOWN = 0x12  # power down
CMD_POWER_ON = 0x13    # power on / button-on
CMD_INIT = 0x23        # initialize (send with 0x01)

# ---------------------------------------------------------------------------
# Color-mode constants  (from advertisement manufacturer data byte [9])
# ---------------------------------------------------------------------------

COLOR_MODE_MONO = 0x00   # single-colour
COLOR_MODE_SEVEN = 0x01  # 7-colour palette
COLOR_MODE_RGB = 0x02    # full RGB
COLOR_MODE_FULL = 0x03   # full-colour reported by CoolLEDUX (rendered as RGB)

# ---------------------------------------------------------------------------
# Protocol / render constants
# ---------------------------------------------------------------------------

CHUNK_DATA_SIZE = 128   # bytes of pixel/payload data per chunk (upstream-verified)
PIXELS_PER_BYTE = 8     # 1-bit packing: 8 pixels → 1 byte
MAX_TEXT_LEN = 255      # maximum text length for single-byte length field

# Pre-compiled regexes for byte escaping.  Order matters: escape 0x02 FIRST
# so the escape-prefix byte itself is not double-escaped.
_RE_02 = re.compile(b"\x02", re.MULTILINE)
_RE_01 = re.compile(b"\x01", re.MULTILINE)
_RE_03 = re.compile(b"\x03", re.MULTILINE)


# ===========================================================================
# Pure-function protocol helpers  (unit-testable without BLE)
# ===========================================================================

def escape(data: bytes) -> bytes:
    """Escape control bytes 0x01 / 0x02 / 0x03 for wire transmission.

    Escaping rules (applied strictly in this order to avoid double-escaping):
      0x02 → 0x02 0x06
      0x01 → 0x02 0x05
      0x03 → 0x02 0x07

    Note: 0x00 is **not** escaped — verified against upstream test vectors.

    Args:
        data: Raw bytes to escape.

    Returns:
        Escaped bytes.
    """
    data = _RE_02.sub(b"\x02\x06", data)
    data = _RE_01.sub(b"\x02\x05", data)
    return _RE_03.sub(b"\x02\x07", data)


def encode_frame(payload: bytes) -> bytes:
    """Wrap *payload* in a CoolLEDX protocol frame.

    Frame format::

        0x01  +  escape( len(payload)(2 BE)  +  payload )  +  0x03

    The two-byte length prefix is escaped together with the payload so that
    any control bytes inside the length field are also escaped.  The length
    field itself records the **unescaped** payload length.

    Args:
        payload: Raw (unescaped) payload bytes.

    Returns:
        Complete framed bytes ready to write to the BLE characteristic.
    """
    length_prefix = len(payload).to_bytes(2, byteorder="big")
    return b"\x01" + escape(length_prefix + payload) + b"\x03"


def xor_checksum(data: bytes) -> int:
    """XOR all bytes in *data* and return the result (0–255)."""
    result = 0
    for byte in data:
        result ^= byte
    return result


def build_chunk_wrapper(
    command: int,
    total_payload_len: int,
    chunk_idx: int,
    chunk_data: bytes,
) -> bytes:
    """Build one chunk wrapper (the payload passed to :func:`encode_frame`).

    The structure, following UpDryTwist/coolledx-driver ``chop_up_data``::

        command   (1 byte)
        0x00      (1 byte, purpose unknown)
        totalLen  (2 bytes BE) — length of the full, un-split payload
        chunkIdx  (2 bytes BE) — zero-based chunk index
        chunkSize (1 byte)     — number of data bytes in *this* chunk
        chunk_data             — up to 128 bytes
        checksum  (1 byte)     — XOR of all bytes from 0x00 up to end of chunk_data
                                 (does **not** include the command byte)

    Args:
        command:           Command byte (e.g. ``CMD_TEXT``).
        total_payload_len: Total length of the un-chunked payload.
        chunk_idx:         Zero-based index of this chunk.
        chunk_data:        Up to 128 bytes of payload data for this chunk.

    Returns:
        Raw chunk wrapper bytes (pass to ``encode_frame``).
    """
    inner = (
        b"\x00"
        + total_payload_len.to_bytes(2, byteorder="big")
        + chunk_idx.to_bytes(2, byteorder="big")
        + len(chunk_data).to_bytes(1, byteorder="big")
        + chunk_data
    )
    # Checksum covers the inner bytes only — command byte is excluded.
    # This is the behaviour in the upstream's chop_up_data / get_xor_checksum.
    chk = xor_checksum(inner)
    return bytes([command]) + inner + bytes([chk])


def build_chunks(data: bytes, command: int) -> list[bytes]:
    """Split *data* into ≤128-byte pieces and return a list of framed chunks.

    Each chunk is framed via :func:`encode_frame` and ready to write to the
    BLE characteristic.  Follows upstream ``chop_up_data`` + ``create_command``
    exactly.

    Args:
        data:    Full raw payload bytes to chunk.
        command: Command byte that prefixes every chunk wrapper.

    Returns:
        List of framed byte strings (one per chunk).
    """
    total_len = len(data)
    # Produce at least one chunk even for empty payloads.
    raw_pieces: list[bytes] = []
    for offset in range(0, max(total_len, 1), CHUNK_DATA_SIZE):
        raw_pieces.append(data[offset : offset + CHUNK_DATA_SIZE])

    return [
        encode_frame(build_chunk_wrapper(command, total_len, idx, piece))
        for idx, piece in enumerate(raw_pieces)
    ]


# ===========================================================================
# Pillow-based pixel rendering helpers
# ===========================================================================

def _image_to_rgb_bitfields(
    image: Image.Image,
    output_width: int,
    output_height: int,
    bg_color: tuple[int, int, int] = (0, 0, 0),
    left_offset: int = 0,
    top_offset: int = 0,
) -> tuple[bytearray, bytearray, bytearray]:
    """Convert a PIL image to packed R/G/B column-major bit-fields.

    Scans left-to-right (columns), top-to-bottom within each column.
    Each group of 8 pixels becomes one byte; the topmost pixel is the MSB.
    Three separate byte arrays are returned — one per colour channel — mirroring
    the upstream ``get_separate_pixel_bytefields`` implementation.

    *output_height* must be a multiple of 8.

    Args:
        image:        Source image (will be converted to RGB internally).
        output_width: Number of pixel columns to emit.
        output_height:Number of pixel rows to emit (must be multiple of 8).
        bg_color:     Background (fill) colour for pixels outside the image.
        left_offset:  Column at which the image starts inside the output grid.
        top_offset:   Row at which the image starts inside the output grid.

    Returns:
        ``(barr_r, barr_g, barr_b)`` — packed bit-field byte arrays.
    """
    if output_height % PIXELS_PER_BYTE != 0:
        raise ValueError(
            f"output_height must be a multiple of {PIXELS_PER_BYTE}; got {output_height}"
        )

    img = image.convert("RGB")
    img_w, img_h = img.size
    bg_r, bg_g, bg_b = bg_color

    barr_r: bytearray = bytearray()
    barr_g: bytearray = bytearray()
    barr_b: bytearray = bytearray()
    tmp_r = tmp_g = tmp_b = 0

    for x in range(output_width):
        for y in range(output_height):
            if (
                y < top_offset
                or x < left_offset
                or y >= img_h + top_offset
                or x >= img_w + left_offset
            ):
                r, g, b = bg_r, bg_g, bg_b
            else:
                pixel = img.getpixel((x - left_offset, y - top_offset))
                r, g, b = pixel[0], pixel[1], pixel[2]

            tmp_r = (tmp_r << 1) | round(r / 255)
            tmp_g = (tmp_g << 1) | round(g / 255)
            tmp_b = (tmp_b << 1) | round(b / 255)

            if y % PIXELS_PER_BYTE == PIXELS_PER_BYTE - 1:
                barr_r.append(tmp_r & 0xFF)
                barr_g.append(tmp_g & 0xFF)
                barr_b.append(tmp_b & 0xFF)
                tmp_r = tmp_g = tmp_b = 0

    return barr_r, barr_g, barr_b


def render_text_payload(
    text: str,
    sign_height: int,
    sign_width: int,
    color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: str | None = None,
    font_size: int | None = None,
) -> bytearray:
    """Render *text* to the CoolLEDX text-command payload format.

    The returned bytes should be chunked with :func:`build_chunks` using
    ``CMD_TEXT``.

    Payload structure (following upstream ``create_text_payload`` /
    ``create_image_output`` with text)::

        24 × 0x00                   (unknown preamble)
        text_len  (1 byte)          (clamped to 255)
        char_metadata (80 bytes)    (0x30 for each character position)
        pixel_data_len (2 bytes BE)
        R bitfield  (output_width × output_height/8 bytes)
        G bitfield  (same length)
        B bitfield  (same length)

    Args:
        text:       Text string to display.
        sign_height:Sign height in pixels (must be a multiple of 8).
        sign_width: Sign width in pixels (used as minimum canvas width).
        color:      RGB text colour.
        bg_color:   RGB background colour.
        font_path:  Optional path to a TrueType font (.ttf/.otf).
        font_size:  Font size in points; defaults to ``sign_height - 2``.

    Returns:
        Raw payload bytes.
    """
    if font_size is None:
        font_size = max(sign_height - 2, 8)

    # Render text onto a generously-sized RGBA canvas (matches upstream).
    canvas_w = max(2048, sign_width * 4)
    img = Image.new("RGBA", (canvas_w, sign_height + 8), (*bg_color, 255))
    draw = ImageDraw.Draw(img)

    try:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        if font_path:
            font = ImageFont.truetype(font_path, font_size)
        else:
            font = ImageFont.load_default(font_size)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()

    r_c, g_c, b_c = color
    # y_offset=1 matches upstream render_text_to_image
    draw.text((0, 1), text, (r_c, g_c, b_c), font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = max(bbox[2], 1)
    del draw

    # Crop to text bounds, then ensure height == sign_height
    img = img.crop((0, 0, text_width, sign_height))

    output_width = text_width
    output_height = sign_height

    barr_r, barr_g, barr_b = _image_to_rgb_bitfields(
        img,
        output_width,
        output_height,
        bg_color=bg_color,
    )
    pixel_bits = bytes(barr_r) + bytes(barr_g) + bytes(barr_b)

    payload = bytearray()
    payload += bytearray(24)  # unknown preamble

    # Text metadata (upstream: 1-byte length + 80-byte char array)
    char_count = min(len(text), MAX_TEXT_LEN)
    payload += char_count.to_bytes(1, byteorder="big")
    char_meta = bytearray(80)
    for i in range(min(char_count, 80)):
        char_meta[i] = 0x30
    payload += char_meta

    payload += len(pixel_bits).to_bytes(2, byteorder="big")
    payload += pixel_bits

    return payload


def render_image_payload(
    image: Image.Image,
    sign_height: int,
    sign_width: int,
    bg_color: tuple[int, int, int] = (0, 0, 0),
) -> bytearray:
    """Render a PIL Image to the CoolLEDX image-command payload format.

    The returned bytes should be chunked with :func:`build_chunks` using
    ``CMD_IMAGE``.

    Payload structure (following upstream ``create_image_output`` without text)::

        24 × 0x00                   (unknown preamble)
        pixel_data_len (2 bytes BE)
        R bitfield
        G bitfield
        B bitfield

    The image is scaled proportionally so that its height equals *sign_height*.
    Width is left as-is (the sign scrolls horizontally for wide content).

    Args:
        image:      PIL Image to render.
        sign_height:Sign height in pixels (must be a multiple of 8).
        sign_width: Minimum output width (unused in current implementation —
                    preserved for API symmetry with upstream).
        bg_color:   Background colour for padding.

    Returns:
        Raw payload bytes.
    """
    img = image.convert("RGB")
    w, h = img.size

    # Scale proportionally to sign height.
    if h != sign_height:
        new_w = max(1, int(w * sign_height / h))
        img = img.resize((new_w, sign_height), Image.LANCZOS)
        w = new_w

    output_width = max(w, 1)
    output_height = sign_height

    barr_r, barr_g, barr_b = _image_to_rgb_bitfields(img, output_width, output_height, bg_color)
    pixel_bits = bytes(barr_r) + bytes(barr_g) + bytes(barr_b)

    payload = bytearray()
    payload += bytearray(24)
    payload += len(pixel_bits).to_bytes(2, byteorder="big")
    payload += pixel_bits

    return payload


def render_animation_payload_from_gif(
    anim: Image.Image,
    sign_height: int,
    sign_width: int,
    speed: int = 512,
    bg_color: tuple[int, int, int] = (0, 0, 0),
) -> bytearray:
    """Convert an animated PIL Image (GIF) to the CoolLEDX animation payload.

    The returned bytes should be chunked with :func:`build_chunks` using
    ``CMD_ANIMATION``.

    Payload structure (following upstream ``create_animation_payload``)::

        24 × 0x00        (unknown preamble)
        frame_count (1 byte)
        speed       (2 bytes BE)
        R bitfield for all frames  (concatenated)
        G bitfield for all frames
        B bitfield for all frames

    Args:
        anim:       Animated PIL Image.
        sign_height:Sign height in pixels.
        sign_width: Sign width in pixels (animations are force-fit to this).
        speed:      Animation speed value (0–65535).
        bg_color:   Background colour.

    Returns:
        Raw payload bytes.
    """
    n_frames: int = getattr(anim, "n_frames", 1)

    combined: Image.Image | None = None
    all_r = bytearray()
    all_g = bytearray()
    all_b = bytearray()

    for frame_idx in range(n_frames):
        anim.seek(frame_idx)
        if combined is None:
            combined = anim.convert("RGBA")
        else:
            combined = Image.alpha_composite(combined, anim.convert("RGBA"))

        frame_r, frame_g, frame_b = _image_to_rgb_bitfields(
            combined,
            sign_width,
            sign_height,
            bg_color=bg_color,
        )
        all_r += frame_r
        all_g += frame_g
        all_b += frame_b

    pixel_bits = bytes(all_r) + bytes(all_g) + bytes(all_b)

    payload = bytearray()
    payload += bytearray(24)
    payload += n_frames.to_bytes(1, byteorder="big")
    payload += speed.to_bytes(2, byteorder="big")
    payload += pixel_bits

    return payload


def parse_jt_animation(data: bytes, bg_color: tuple[int, int, int] = (0, 0, 0)) -> bytearray:
    """Parse a CoolLED .jt file and return an animation or image payload.

    .jt files are JSON-encoded files used by the official CoolLED app.
    This mirrors the upstream ``create_jt_payload`` implementation.

    Args:
        data:     Raw .jt file bytes.
        bg_color: Background colour (currently unused; included for future use).

    Returns:
        Raw payload bytes (suitable for :func:`build_chunks`).

    Raises:
        ValueError: If the .jt data cannot be parsed.
    """
    try:
        jt = json.loads(data)[0]
        jt_data = jt["data"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Invalid .jt file: {exc}") from exc

    render_as_image = False
    jt_rgb_data: list[int] | None = None
    frames = 1
    speed = 0

    if "aniData" in jt_data:
        jt_rgb_data = jt_data["aniData"]
        render_as_image = False
    if "graffitiData" in jt_data:
        jt_rgb_data = jt_data["graffitiData"]
        render_as_image = True

    if "frameNum" in jt_data:
        frames = jt_data["frameNum"]
    if "delays" in jt_data:
        speed = jt_data["delays"]

    payload = bytearray(24)  # unknown preamble

    if not render_as_image:
        payload += frames.to_bytes(1, byteorder="big")
        payload += speed.to_bytes(2, byteorder="big")

    if jt_rgb_data is not None:
        pixel_bits = bytearray(jt_rgb_data)
        payload += len(pixel_bits).to_bytes(2, byteorder="big")
        payload += pixel_bits

    return payload


# ===========================================================================
# CoolLEDXDevice — high-level BLE device class
# ===========================================================================

class CoolLEDXDevice:
    """CoolLEDX BLE LED sign — BLE protocol driver.

    Manages the connection (via ``bleak-retry-connector`` so HA Bluetooth
    proxies are used automatically) and exposes a high-level async API.

    This class has **no** ``homeassistant`` imports and can be imported and
    tested independently of Home Assistant.

    Example::

        device = CoolLEDXDevice(ble_device, height=16, width=96)
        await device.connect()
        await device.set_text("Hello!", color=(255, 80, 0))
        await device.disconnect()
    """

    def __init__(
        self,
        ble_device: BLEDevice,
        name: str = "CoolLEDX",
        height: int = 16,
        width: int = 96,
        color_mode: int = COLOR_MODE_RGB,
    ) -> None:
        """Initialise the device.

        Args:
            ble_device: ``BLEDevice`` from the HA Bluetooth stack.  This
                        object already encodes proxy information so that
                        :func:`establish_connection` routes writes through
                        the correct ESPHome proxy (or local adapter).
            name:       Human-readable device name used for logging and passed
                        to ``establish_connection``.
            height:     Sign pixel height (parsed from BLE advertisement by caller).
                        Must be a multiple of 8 (standard: 16, 32, …).
            width:      Sign pixel width (parsed from BLE advertisement by caller).
            color_mode: One of :data:`COLOR_MODE_MONO`, :data:`COLOR_MODE_SEVEN`,
                        or :data:`COLOR_MODE_RGB`.
        """
        self._ble_device = ble_device
        self._name = name
        self._height = height
        self._width = width
        self._color_mode = color_mode

        self._client: BleakClientWithServiceCache | None = None
        self._write_char = None  # cached BleakGATTCharacteristic
        self._lock: asyncio.Lock = asyncio.Lock()

        # State for set_color (re-render current text with new colour)
        self._current_text: str | None = None
        self._current_color: tuple[int, int, int] = (255, 255, 255)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the BLE client is currently connected."""
        return self._client is not None and self._client.is_connected

    @property
    def height(self) -> int:
        """Sign pixel height."""
        return self._height

    @property
    def width(self) -> int:
        """Sign pixel width."""
        return self._width

    @property
    def color_mode(self) -> int:
        """Active colour mode (``COLOR_MODE_*`` constant)."""
        return self._color_mode

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the BLE device via ``bleak-retry-connector``.

        Uses :func:`establish_connection` with ``BleakClientWithServiceCache``
        so that ESPHome Bluetooth proxies are routed correctly and service
        discovery is cached.

        Raises:
            BleakError: If the connection cannot be established.
        """
        _LOGGER.debug("Connecting to %s (%s)", self._name, self._ble_device.address)
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._name,
            disconnected_callback=self._handle_disconnect,
        )
        # Cache the write characteristic for fast repeated access.
        self._write_char = self._client.services.get_characteristic(WRITE_CHAR_UUID)
        if self._write_char is None:
            _LOGGER.error(
                "Write characteristic %s not found on %s — writes will fail",
                WRITE_CHAR_UUID,
                self._name,
            )
        # The sign gates command processing on an active notification
        # subscription: until a client subscribes to FFF1, every write is
        # silently dropped (no error, no display change).  Subscribe here so
        # the device accepts and ACKs subsequent commands.  The notifications
        # themselves are command echoes/ACKs we don't currently act on.
        if self._write_char is not None:
            try:
                await self._client.start_notify(
                    WRITE_CHAR_UUID, self._handle_notification
                )
            except (BleakError, EOFError) as err:
                _LOGGER.warning(
                    "Failed to subscribe to notifications on %s (%s); the sign "
                    "may ignore commands",
                    self._name,
                    err,
                )
        _LOGGER.debug("Connected to %s", self._name)

    def _handle_notification(self, _char: object, data: bytearray) -> None:
        """Handle a notification from the sign (command echo / ACK).

        We don't currently parse these, but an active subscription is required
        for the device to process writes at all (see :meth:`connect`).
        """
        _LOGGER.debug("Notification from %s: %s", self._name, data.hex())

    async def disconnect(self) -> None:
        """Disconnect from the BLE device."""
        if self._client and self._client.is_connected:
            _LOGGER.debug("Disconnecting from %s", self._name)
            await self._client.disconnect()
        self._client = None
        self._write_char = None

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Handle BLE disconnection (bleak callback)."""
        _LOGGER.debug("Disconnected from %s", self._name)
        self._client = None
        self._write_char = None

    # ------------------------------------------------------------------
    # Low-level write
    # ------------------------------------------------------------------

    async def _write_chunks(self, framed_chunks: list[bytes]) -> None:
        """Write a list of framed protocol chunks to the BLE characteristic.

        Writes are serialised with :attr:`_lock` to prevent concurrent
        BLE access.  Prefers write-without-response (lower latency);
        falls back to write-with-response when a chunk exceeds the
        characteristic's negotiated MTU.

        Args:
            framed_chunks: Ready-to-write bytes from :func:`build_chunks`.

        Raises:
            RuntimeError: If not connected.
        """
        async with self._lock:
            if not self.is_connected or self._client is None:
                raise RuntimeError(f"Not connected to {self._name}")

            char = self._write_char
            max_wwr = 0
            if char is not None:
                max_wwr = getattr(char, "max_write_without_response_size", 0) or 0

            for chunk in framed_chunks:
                _LOGGER.debug(
                    "Writing %d bytes → %s (%s)",
                    len(chunk),
                    WRITE_CHAR_UUID,
                    self._name,
                )
                if max_wwr > 0 and len(chunk) > max_wwr:
                    # Chunk exceeds MTU for write-without-response; use
                    # write-with-response (slower but avoids truncation).
                    await self._client.write_gatt_char(
                        WRITE_CHAR_UUID, bytearray(chunk), response=True
                    )
                else:
                    await self._client.write_gatt_char(
                        WRITE_CHAR_UUID, bytearray(chunk), response=False
                    )

    async def _send_simple(self, payload: bytes) -> None:
        """Frame *payload* as a single packet and write it."""
        await self._write_chunks([encode_frame(payload)])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def turn_on(self) -> None:
        """Turn the sign on.

        Sends the switch-on command (``CMD_SWITCH`` / ``0x01``).
        """
        await self._send_simple(bytes([CMD_SWITCH, 0x01]))

    async def turn_off(self) -> None:
        """Turn the sign off.

        Sends the switch-off command (``CMD_SWITCH`` / ``0x00``).
        """
        await self._send_simple(bytes([CMD_SWITCH, 0x00]))

    async def power_down(self) -> None:
        """Send the hardware power-down command (``CMD_POWER_DOWN`` / 0x12)."""
        await self._send_simple(bytes([CMD_POWER_DOWN]))

    async def power_on(self) -> None:
        """Send the hardware power-on command (``CMD_POWER_ON`` / 0x13)."""
        await self._send_simple(bytes([CMD_POWER_ON]))

    async def initialize(self) -> None:
        """Send the initialisation command (``CMD_INIT`` / 0x23 0x01)."""
        await self._send_simple(bytes([CMD_INIT, 0x01]))

    async def set_brightness(self, value: int) -> None:
        """Set display brightness.

        Args:
            value: Brightness level 0–255.
        """
        value = max(0, min(255, int(value)))
        await self._send_simple(bytes([CMD_BRIGHTNESS, value]))

    async def set_speed(self, value: int) -> None:
        """Set scroll speed.

        Args:
            value: Speed level 0–255.
        """
        value = max(0, min(255, int(value)))
        await self._send_simple(bytes([CMD_SPEED, value]))

    async def set_mode(self, mode: int) -> None:
        """Set display animation mode.

        Args:
            mode: Mode byte (e.g. ``0x02`` = scroll-left, ``0x01`` = static).
        """
        await self._send_simple(bytes([CMD_MODE, mode & 0xFF]))

    async def set_color(self, r: int, g: int, b: int) -> None:
        """Re-render the current content with a new RGB colour.

        If text has previously been set via :meth:`set_text`, it is re-rendered
        with the new colour.  Otherwise a solid colour fill is sent as a
        minimal image.

        Args:
            r: Red channel 0–255.
            g: Green channel 0–255.
            b: Blue channel 0–255.
        """
        self._current_color = (r & 0xFF, g & 0xFF, b & 0xFF)
        if self._current_text is not None:
            await self.set_text(self._current_text, color=self._current_color)
        else:
            # Solid colour fill
            img = Image.new("RGB", (self._width, self._height), self._current_color)
            await self.send_image(img)

    async def set_text(
        self,
        text: str,
        color: tuple[int, int, int] = (255, 255, 255),
        font_path: str | None = None,
        font_size: int | None = None,
    ) -> None:
        """Render *text* and send to sign via the text command (``CMD_TEXT``).

        The rendered image is chunked and sent in 128-byte pieces.

        Args:
            text:      Text string to display.
            color:     RGB colour tuple for the text.
            font_path: Optional path to a TrueType font file.
            font_size: Font height in points; defaults to ``height - 2``.
        """
        self._current_text = text
        self._current_color = color

        payload = render_text_payload(
            text=text,
            sign_height=self._height,
            sign_width=self._width,
            color=color,
            font_path=font_path,
            font_size=font_size,
        )
        await self._write_chunks(build_chunks(bytes(payload), CMD_TEXT))

    async def send_image(
        self,
        image: Union["Image.Image", bytes, str, "Path"],
        bg_color: tuple[int, int, int] = (0, 0, 0),
    ) -> None:
        """Send a static image to the sign via the image command (``CMD_IMAGE``).

        The image is scaled to sign height and chunked.

        Args:
            image:    One of:

                      * ``PIL.Image.Image`` — used directly.
                      * ``bytes`` — decoded via ``PIL.Image.open``.
                      * ``str`` or :class:`pathlib.Path` — opened as a file.

            bg_color: Background fill colour for pixels outside the image.
        """
        pil_image: Image.Image
        if isinstance(image, Image.Image):
            pil_image = image
        elif isinstance(image, (bytes, bytearray)):
            pil_image = Image.open(io.BytesIO(image))
        else:
            pil_image = Image.open(str(image))

        payload = render_image_payload(
            image=pil_image,
            sign_height=self._height,
            sign_width=self._width,
            bg_color=bg_color,
        )
        await self._write_chunks(build_chunks(bytes(payload), CMD_IMAGE))

    async def send_animation(self, data: bytes) -> None:
        """Send animation data to the sign (``CMD_ANIMATION``).

        Accepts either:

        * **Raw .jt file bytes** (JSON format from the CoolLED app) — parsed
          automatically via :func:`parse_jt_animation`.
        * **Animated GIF bytes** — decoded and rendered via Pillow.

        Args:
            data: Animation data (.jt JSON bytes or animated GIF bytes).

        Raises:
            ValueError: If *data* cannot be parsed as either format.
        """
        payload: bytearray

        # Try .jt JSON first.
        try:
            payload = parse_jt_animation(data)
            jt_parsed = True
        except (ValueError, Exception):  # noqa: BLE001
            jt_parsed = False

        if not jt_parsed:
            # Fall back to treating data as animated GIF bytes.
            try:
                anim = Image.open(io.BytesIO(data))
                payload = render_animation_payload_from_gif(
                    anim,
                    sign_height=self._height,
                    sign_width=self._width,
                )
            except Exception as exc:
                raise ValueError(
                    "send_animation: data is neither a valid .jt file nor an animated GIF"
                ) from exc

        await self._write_chunks(build_chunks(bytes(payload), CMD_ANIMATION))

    async def send_raw_animation_payload(self, payload: bytes) -> None:
        """Send a pre-built animation payload directly (``CMD_ANIMATION``).

        Use this when you have already constructed the payload bytes
        (e.g. via :func:`render_animation_payload_from_gif` or
        :func:`parse_jt_animation`) and want to send them without
        further processing.

        Args:
            payload: Pre-built animation payload bytes.
        """
        await self._write_chunks(build_chunks(payload, CMD_ANIMATION))
