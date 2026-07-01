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

try:
    from . import ux_protocol
except ImportError:  # pragma: no cover - loaded standalone (unit tests via
    # importlib.util.spec_from_file_location bypass the package __init__, so
    # there's no parent package for a relative import to resolve against).
    import importlib.util as _importlib_util

    _UX_PROTOCOL_SPEC = _importlib_util.spec_from_file_location(
        "coolledx_ux_protocol", Path(__file__).resolve().parent / "ux_protocol.py"
    )
    ux_protocol = _importlib_util.module_from_spec(_UX_PROTOCOL_SPEC)
    _UX_PROTOCOL_SPEC.loader.exec_module(ux_protocol)

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
# CoolLEDUX display modes  (from the official app's CoolleduxModeChooseDialog)
# ---------------------------------------------------------------------------
# On UX devices these integers are written into the program body's mode field
# (see CoolLEDXDevice.set_mode); they differ from the classic CoolLEDX mode
# numbering.  Shared by the light (effect) and select (display-mode) entities
# via the coordinator's stored effect value, so the mapping lives here once.

UX_MODE_MAP: dict[str, int] = {
    "Scroll Left": 2,
    "Up": 4,
    "Down": 5,
    "Accumulate": 6,
    "Picture": 7,
    "Shining": 8,
    "Left Panning": 9,
    "Right Panning": 10,
    "Cover": 11,
    "Left-Right": 13,
}

# UX modes that scroll horizontally.  For these, short text is padded with
# blank columns (see :func:`_pad_image_columns`) so it slides fully across the
# panel — the firmware only scrolls content wider than the panel, so an
# unpadded word that fits would otherwise sit static.
UX_SCROLL_MODES: frozenset[int] = frozenset({2, 9, 10, 13})

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


def _image_to_argb_pixels(
    image: Image.Image,
    bg_color: tuple[int, int, int] = (0, 0, 0),
) -> list[int]:
    """Flatten a PIL image onto *bg_color* into a row-major ARGB pixel list.

    Used by the CoolLEDUX send path: any alpha channel is composited onto
    *bg_color* first, then every pixel is packed as ``0xAARRGGBB`` (alpha
    forced to ``0xFF``) in row-major order (``index = row * width + col``),
    matching the input format expected by
    ``ux_protocol.build_image_program``.

    Args:
        image:    Source image (any mode; converted to RGBA internally).
        bg_color: Background colour alpha is composited onto.

    Returns:
        Row-major list of ``0xAARRGGBB`` pixel ints, length
        ``image.width * image.height``.
    """
    img = image.convert("RGBA")
    bg = Image.new("RGBA", img.size, (*bg_color, 255))
    img = Image.alpha_composite(bg, img).convert("RGB")
    width, height = img.size

    pixels: list[int] = []
    for y in range(height):
        for x in range(width):
            r, g, b = img.getpixel((x, y))
            pixels.append((0xFF << 24) | (r << 16) | (g << 8) | b)
    return pixels


def _render_text_image(
    text: str,
    sign_height: int,
    sign_width: int,
    color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: str | None = None,
    font_size: int | None = None,
) -> Image.Image:
    """Rasterise *text* to an RGBA image, cropped to its bounding box.

    This is the shared Pillow drawing/font-loading step behind both
    :func:`render_text_payload` (CoolLEDX 1-bit bitfield payload) and the
    CoolLEDUX ARGB-pixel send path in :class:`CoolLEDXDevice` — the actual
    rasterisation logic lives here exactly once so both dialects render
    text identically.

    Args:
        text:       Text string to display.
        sign_height:Sign height in pixels.
        sign_width: Sign width in pixels (used as minimum canvas width).
        color:      RGB text colour.
        bg_color:   RGB background colour.
        font_path:  Optional path to a TrueType font (.ttf/.otf).
        font_size:  Font size in points; defaults to ``sign_height - 2``.

    Returns:
        Cropped RGBA image, ``(text_width, sign_height)`` in size.
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
    return img.crop((0, 0, text_width, sign_height))


def _render_text_image_fill_height(
    text: str,
    sign_height: int,
    color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: str | None = None,
) -> Image.Image:
    """Rasterise *text* so it fills the full sign height.

    Renders at a supersampled font size, crops to the actual inked bounding
    box, then scales to exactly ``sign_height`` pixels tall (width scaled
    proportionally).  This makes text occupy the full vertical extent of the
    sign regardless of the font's own metrics — unlike
    :func:`_render_text_image`, which leaves glyphs at their natural (often
    sub-height, vertically off-centre) size and is kept only for the legacy
    CoolLEDX 1-bit payload path.

    Args:
        text:       Text string to display.
        sign_height:Target height in pixels (the returned image is exactly
                    this tall).
        color:      RGB text colour.
        bg_color:   RGB background colour.
        font_path:  Optional path to a TrueType font (.ttf/.otf).

    Returns:
        RGB image ``(scaled_width, sign_height)`` in size.
    """
    supersample = max(sign_height * 4, 32)
    canvas_w = max(4096, len(text) * supersample)
    img = Image.new("RGB", (canvas_w, supersample * 2), bg_color)
    draw = ImageDraw.Draw(img)

    try:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        if font_path:
            font = ImageFont.truetype(font_path, supersample)
        else:
            font = ImageFont.load_default(supersample)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()

    draw.text((supersample // 4, supersample // 4), text, color, font=font)
    del draw

    ink = img.getbbox()  # bounding box of non-background (non-black) pixels
    if ink is None:
        return Image.new("RGB", (1, sign_height), bg_color)

    cropped = img.crop(ink)
    w, h = cropped.size
    new_w = max(1, round(w * sign_height / h))
    return cropped.resize((new_w, sign_height), Image.LANCZOS)


def _pad_image_columns(
    image: Image.Image,
    pad: int,
    bg_color: tuple[int, int, int] = (0, 0, 0),
) -> Image.Image:
    """Return a copy of *image* with *pad* blank columns added on each side.

    Used by the CoolLEDUX scrolling-text path: the sign's firmware only
    scrolls program content that is wider than the physical panel (a word
    that fits is shown static), so padding a short text image with blank
    columns forces it to slide fully across the screen — entering from the
    right edge and exiting the left.

    Args:
        image:    Source image.
        pad:      Blank columns to add on the left and on the right.  ``<= 0``
                  returns *image* unchanged.
        bg_color: Fill colour for the added columns.

    Returns:
        The padded RGB image (``width + 2*pad`` wide), or *image* if
        ``pad <= 0``.
    """
    if pad <= 0:
        return image
    src = image.convert("RGB")
    w, h = src.size
    padded = Image.new("RGB", (w + 2 * pad, h), bg_color)
    padded.paste(src, (pad, 0))
    return padded


def _rotate_180(image: Image.Image) -> Image.Image:
    """Return *image* rotated 180° (lossless) for upside-down mounting."""
    return image.transpose(Image.Transpose.ROTATE_180)


def render_text_payload(
    text: str,
    sign_height: int,
    sign_width: int,
    color: tuple[int, int, int] = (255, 255, 255),
    bg_color: tuple[int, int, int] = (0, 0, 0),
    font_path: str | None = None,
    font_size: int | None = None,
    invert: bool = False,
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
        invert:     If ``True``, rotate the rendered image 180° before
                    packing (for upside-down mounting).

    Returns:
        Raw payload bytes.
    """
    img = _render_text_image(
        text, sign_height, sign_width, color, bg_color, font_path, font_size
    )
    if invert:
        img = _rotate_180(img)
    output_width, output_height = img.size

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
    invert: bool = False,
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
        invert:     If ``True``, rotate the rendered image 180° before
                    packing (for upside-down mounting).

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

    if invert:
        img = _rotate_180(img)

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

        # CoolLEDUX ("Rayhome Devil Eyes" full-colour) devices speak the
        # two-phase compressed program-upload protocol in ux_protocol.py
        # instead of the classic CoolLEDX chunked-payload protocol; the
        # classic protocol's DATA_ERROR rejection on these devices is what
        # necessitates this separate send path (see ux_protocol.py docs).
        self._is_ux = self._color_mode == COLOR_MODE_FULL or (
            self._name or ""
        ).startswith("CoolLEDUX")

        self._client: BleakClientWithServiceCache | None = None
        self._write_char = None  # cached BleakGATTCharacteristic
        self._lock: asyncio.Lock = asyncio.Lock()

        # Pending ACK future for the UX send path — resolved by
        # _handle_notification with the (command, status) tuple decoded via
        # ux_protocol.parse_notification.  ``_ack_expected_cmd`` is the response
        # command byte we are waiting for; the sign echoes the command of the
        # frame it is ACKing, so a notification whose command differs (e.g. a
        # stale turn_on/brightness echo still in flight from an earlier
        # _send_simple) must NOT resolve the future.  ``None`` accepts any.
        self._ack_future: asyncio.Future[tuple[int | None, int | None]] | None = None
        self._ack_expected_cmd: int | None = None

        # State for set_color (re-render current text with new colour)
        self._current_text: str | None = None
        self._current_color: tuple[int, int, int] = (255, 255, 255)

        # 180° rotation for upside-down mounting, applied to the final
        # rendered image on every send path (see render_text_payload /
        # render_image_payload / set_text / send_image).
        self.invert: bool = False

        # CoolLEDUX display-field state.  On UX devices the classic
        # set_mode/set_speed commands (0x06/0x07) do not affect an already
        # uploaded program — mode, speed and stay-time are fields baked into
        # the program body — so they are stored here and applied by
        # re-uploading the last content (see _send_program_upload /
        # _reupload_last_ux).  Defaults mirror ux_protocol.build_image_program.
        self._ux_mode: int = 2       # scroll-left
        self._ux_speed: int = 255
        self._ux_stay_time: int = 3
        self._ux_last_pixels: list[int] | None = None
        self._ux_last_size: tuple[int, int] = (0, 0)

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

        An active subscription is required for the device to process writes
        at all (see :meth:`connect`). On CoolLEDUX devices, notifications are
        also ACKs for the two-phase program-upload send path: each one is
        parsed via :func:`ux_protocol.parse_notification` and used to resolve
        the currently-pending :attr:`_ack_future` (if any), unblocking
        :meth:`_send_program_upload`.
        """
        _LOGGER.debug("Notification from %s: %s", self._name, data.hex())
        future = self._ack_future
        if future is None or future.done():
            return
        cmd, status = ux_protocol.parse_notification(bytes(data))
        if self._ack_expected_cmd is not None and cmd != self._ack_expected_cmd:
            # Stale/echo notification for a different command — ignore it so it
            # can't be mistaken for the ACK of the frame we're currently
            # awaiting (see _ack_expected_cmd).
            _LOGGER.debug(
                "Ignoring notification cmd=%s (awaiting cmd=%s) from %s",
                cmd,
                self._ack_expected_cmd,
                self._name,
            )
            return
        future.set_result((cmd, status))

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
    # CoolLEDUX two-phase program-upload send path
    # ------------------------------------------------------------------

    async def _ux_write_and_await_ack(
        self, frame: bytes, expected_cmd: int | None = None, timeout: float = 5.0
    ) -> tuple[int | None, int | None]:
        """Write one already-framed UX-protocol frame and await its ACK.

        Must be called while already holding :attr:`_lock` — this writes
        directly via ``write_gatt_char`` rather than going through
        :meth:`_write_chunks` (which takes the lock itself), so that the
        whole begin+content handshake in :meth:`_send_program_upload` can be
        serialised as a single critical section.

        Args:
            frame:   Fully-framed bytes to write (e.g. from
                     ``ux_protocol.build_program_upload``).
            timeout: Seconds to wait for the device's ACK notification.

        Returns:
            The ``(response_command, status_code)`` tuple decoded from the
            device's ACK notification via
            :func:`ux_protocol.parse_notification`.

        Raises:
            asyncio.TimeoutError: If no ACK notification arrives within
                                   *timeout* seconds.
        """
        # A framed UX packet can exceed the BLE write-without-response MTU
        # (a single ~600-byte write fails with org.bluez "Failed to initiate
        # write").  Split it into MTU-sized BLE writes; the sign reassembles
        # the 0x01..0x03 framed byte stream and sends exactly one ACK per
        # complete frame (the official app does the same via fastble's
        # split-write).  The ACK future is armed before the first chunk and
        # resolves only once the final chunk completes the frame.
        max_write = 0
        if self._write_char is not None:
            max_write = (
                getattr(self._write_char, "max_write_without_response_size", 0) or 0
            )
        if max_write <= 0:
            max_write = 20  # BLE default ATT payload; safe lower bound

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[int | None, int | None]] = loop.create_future()
        self._ack_future = future
        self._ack_expected_cmd = expected_cmd
        try:
            for offset in range(0, len(frame), max_write):
                await self._client.write_gatt_char(
                    WRITE_CHAR_UUID,
                    bytearray(frame[offset : offset + max_write]),
                    response=False,
                )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            if self._ack_future is future:
                self._ack_future = None
                self._ack_expected_cmd = None

    async def _send_program_upload(
        self,
        pixels_argb: list[int],
        width: int,
        height: int,
        **kw,
    ) -> None:
        """Upload a single-image program to a CoolLEDUX device.

        Builds the "begin program" + LZSS-compressed content frames via
        :func:`ux_protocol.build_program_upload` and sends them in order:
        the begin frame first (its ACK status must be 0, or this raises
        immediately — the device won't accept content packets otherwise),
        then each content frame, retried up to 3 times on ACK timeout or a
        non-zero status (matching the official app's behaviour).

        Args:
            pixels_argb: Row-major list of 0xAARRGGBB pixel values, length
                         ``width * height``.
            width:       Image width in pixels.
            height:      Image height in pixels.
            **kw:        Extra keyword arguments forwarded to
                         ``ux_protocol.build_program_upload`` (e.g.
                         ``mode``, ``speed``, ``stayTime``).

        Raises:
            RuntimeError: If not connected, the begin-program ACK reports a
                          non-zero status, or a content frame still fails
                          after 3 attempts.
        """
        # mode/speed/stayTime are program-body fields; default them from the
        # device's stored UX display-field state unless the caller overrode
        # them, so a later set_mode/set_speed re-upload picks up the new value.
        kw.setdefault("mode", self._ux_mode)
        kw.setdefault("speed", self._ux_speed)
        kw.setdefault("stayTime", self._ux_stay_time)

        # Remember the last content so set_mode/set_speed can re-upload it
        # with the new field values (the hardware has no "change mode of the
        # current program" command).
        self._ux_last_pixels = pixels_argb
        self._ux_last_size = (width, height)

        begin, content = ux_protocol.build_program_upload(
            pixels_argb, width, height, **kw
        )

        async with self._lock:
            if not self.is_connected or self._client is None:
                raise RuntimeError(f"Not connected to {self._name}")

            _cmd, status = await self._ux_write_and_await_ack(
                begin, expected_cmd=0x02
            )
            # Begin-program ACK status is a directive, not pass/fail:
            #   0 -> device lacks this program, send the content packets
            #   1 -> device already has this exact program (CRC match); the
            #        content is redundant, so we're done
            #   other -> a real error (see ux_protocol ErrorCode values)
            if status == 1:
                return
            if status != 0:
                raise RuntimeError(
                    f"{self._name}: begin-program ACK failed (status={status})"
                )

            for idx, frame in enumerate(content):
                last_error: Exception | None = None
                for _attempt in range(3):
                    try:
                        _cmd, status = await self._ux_write_and_await_ack(
                            frame, expected_cmd=0x03
                        )
                    except asyncio.TimeoutError as exc:
                        last_error = exc
                        continue
                    if status == 0:
                        break
                    last_error = RuntimeError(
                        f"{self._name}: content chunk {idx} ACK failed "
                        f"(status={status})"
                    )
                else:
                    raise last_error or RuntimeError(
                        f"{self._name}: content chunk {idx} failed after retries"
                    )

    async def _reupload_last_ux(self) -> bool:
        """Re-upload the last UX program with the current display-field state.

        Used by :meth:`set_mode` / :meth:`set_speed` on CoolLEDUX devices,
        where mode/speed are baked into the program body: applying a new value
        means re-sending the stored content (:attr:`_ux_last_pixels`) so
        :meth:`_send_program_upload` rebuilds it with the updated
        :attr:`_ux_mode` / :attr:`_ux_speed` / :attr:`_ux_stay_time`.

        Returns:
            ``True`` if content was re-uploaded, ``False`` if nothing has been
            uploaded yet (the new field value is stored for the next upload).
        """
        if self._ux_last_pixels is None:
            return False
        width, height = self._ux_last_size
        await self._send_program_upload(self._ux_last_pixels, width, height)
        return True

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

        On CoolLEDUX devices (:attr:`_is_ux`) speed is a program-body field:
        the value is stored and the last content is re-uploaded so it takes
        effect (the classic ``CMD_SPEED`` command does not alter an uploaded
        UX program).  On classic devices the ``CMD_SPEED`` command is sent.

        Args:
            value: Speed level 0–255.
        """
        value = max(0, min(255, int(value)))
        if self._is_ux:
            self._ux_speed = value
            await self._reupload_last_ux()
            return
        await self._send_simple(bytes([CMD_SPEED, value]))

    async def set_mode(self, mode: int) -> None:
        """Set display animation mode.

        On CoolLEDUX devices (:attr:`_is_ux`) mode is a program-body field:
        the value is stored and the last content is re-uploaded so it takes
        effect (the classic ``CMD_MODE`` command does not alter an uploaded
        UX program).  On classic devices the ``CMD_MODE`` command is sent.

        Args:
            mode: Mode byte.  UX modes: 2=scroll-left, 4=up, 5=down,
                  6=accumulate, 7=picture, 8=shining, 9/10=panning,
                  11/12=cover, 13=left-right.  Classic: 0x02=scroll-left, etc.
        """
        if self._is_ux:
            self._ux_mode = mode & 0xFF
            await self._reupload_last_ux()
            return
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
        *,
        mode: int | None = None,
        speed: int | None = None,
    ) -> None:
        """Render *text* and send to sign via the text command (``CMD_TEXT``).

        The rendered image is chunked and sent in 128-byte pieces.

        On CoolLEDUX devices (:attr:`_is_ux`), the classic chunked-text
        command is rejected with DATA_ERROR; text is instead rasterised
        with the exact same Pillow rendering (see :func:`_render_text_image`
        — text drawn on a black background, cropped to its bounding box x
        ``height`` pixels tall) and sent via the two-phase program-upload
        path (:meth:`_send_program_upload`).

        Args:
            text:      Text string to display.
            color:     RGB colour tuple for the text.
            font_path: Optional path to a TrueType font file.
            font_size: Font height in points; defaults to ``height - 2``.
            mode:      Optional CoolLEDUX display mode to set before
                       rendering (ignored on classic devices). See
                       :meth:`set_mode`.
            speed:     Optional CoolLEDUX scroll speed (0-255) to set before
                       rendering (ignored on classic devices). See
                       :meth:`set_speed`.
        """
        self._current_text = text
        self._current_color = color

        if self._is_ux:
            # mode/speed are program-body fields (see _ux_mode/_ux_speed);
            # apply them before the scroll-padding decision below, which
            # depends on _ux_mode.
            if mode is not None:
                self._ux_mode = mode & 0xFF
            if speed is not None:
                self._ux_speed = max(0, min(255, int(speed)))

            img = _render_text_image_fill_height(
                text=text,
                sign_height=self._height,
                color=color,
                bg_color=(0, 0, 0),
                font_path=font_path,
            )
            # For horizontal-scroll modes, pad with blank columns so even a
            # short word (narrower than the panel) slides fully across —
            # the firmware only scrolls content wider than the panel.
            if self._ux_mode in UX_SCROLL_MODES:
                img = _pad_image_columns(img, self._width, bg_color=(0, 0, 0))
            if self.invert:
                img = _rotate_180(img)
            pixels = _image_to_argb_pixels(img, bg_color=(0, 0, 0))
            width, height = img.size
            await self._send_program_upload(pixels, width, height)
            return

        payload = render_text_payload(
            text=text,
            sign_height=self._height,
            sign_width=self._width,
            color=color,
            font_path=font_path,
            font_size=font_size,
            invert=self.invert,
        )
        await self._write_chunks(build_chunks(bytes(payload), CMD_TEXT))

    async def send_image(
        self,
        image: Union["Image.Image", bytes, str, "Path"],
        bg_color: tuple[int, int, int] = (0, 0, 0),
    ) -> None:
        """Send a static image to the sign via the image command (``CMD_IMAGE``).

        The image is scaled to sign height and chunked.

        On CoolLEDUX devices (:attr:`_is_ux`), the classic chunked-image
        command is rejected with DATA_ERROR; the image is instead resized to
        exactly ``(width, height)`` and sent via the two-phase
        program-upload path (:meth:`_send_program_upload`).

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

        if self._is_ux:
            ux_image = pil_image.convert("RGBA").resize(
                (self._width, self._height), Image.LANCZOS
            )
            if self.invert:
                ux_image = _rotate_180(ux_image)
            pixels = _image_to_argb_pixels(ux_image, bg_color=bg_color)
            await self._send_program_upload(pixels, self._width, self._height)
            return

        payload = render_image_payload(
            image=pil_image,
            sign_height=self._height,
            sign_width=self._width,
            bg_color=bg_color,
            invert=self.invert,
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
