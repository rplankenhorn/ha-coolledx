"""Emoji rendering tests for the CoolLEDUX text upload path."""

import asyncio
import types


def _make_ux_device(device_module):
    device = device_module.CoolLEDXDevice(
        ble_device=object(),
        name="CoolLEDUX-Test",
        height=16,
        width=96,
        color_mode=device_module.COLOR_MODE_FULL,
    )
    device._write_char = types.SimpleNamespace(max_write_without_response_size=512)
    return device


def _capture_uploads(device):
    uploads = []

    async def _send_program_upload(pixels, width, height):
        uploads.append({"pixels": pixels, "width": width, "height": height})

    device._send_program_upload = _send_program_upload
    return uploads


def _pixel_rgb(pixel: int) -> tuple[int, int, int]:
    return ((pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF)


def _has_colored_pixel(pixels) -> bool:
    return any((r != g or g != b) for r, g, b in (_pixel_rgb(pixel) for pixel in pixels))


def _image_has_colored_pixel(image) -> bool:
    return any((r != g or g != b) for r, g, b in image.convert("RGB").getdata())


def test_ux_literal_emoji_upload_contains_color_pixels(device_module):
    device = _make_ux_device(device_module)
    uploads = _capture_uploads(device)

    asyncio.run(device.set_text("GOAL 🔥", color=(255, 255, 255)))

    upload = uploads[-1]
    assert upload["height"] == device.height
    assert upload["width"] > 0
    assert len(upload["pixels"]) == upload["width"] * upload["height"]
    assert _has_colored_pixel(upload["pixels"])


def test_ux_shortcode_matches_literal_emoji_pixels(device_module):
    shortcode_device = _make_ux_device(device_module)
    shortcode_uploads = _capture_uploads(shortcode_device)
    literal_device = _make_ux_device(device_module)
    literal_uploads = _capture_uploads(literal_device)

    asyncio.run(shortcode_device.set_text("GOAL :fire:", color=(255, 255, 255)))
    asyncio.run(literal_device.set_text("GOAL 🔥", color=(255, 255, 255)))

    assert shortcode_uploads[-1] == literal_uploads[-1]


def test_ux_ascii_fast_path_stays_grayscale(device_module):
    device = _make_ux_device(device_module)
    uploads = _capture_uploads(device)

    asyncio.run(device.set_text("HI", color=(255, 255, 255)))

    upload = uploads[-1]
    assert upload["height"] == device.height
    assert upload["width"] > 0
    assert not _has_colored_pixel(upload["pixels"])


def test_classic_text_payload_accepts_emoji(device_module):
    payload = device_module.render_text_payload(
        text="A🔥",
        sign_height=16,
        sign_width=96,
    )

    assert isinstance(payload, bytearray)
    assert payload


def test_fill_height_renderer_outputs_color_emoji(device_module):
    image = device_module._render_text_image_fill_height(
        "A🔥",
        16,
        color=(255, 255, 255),
        bg_color=(0, 0, 0),
    )

    assert image.height == 16
    assert image.width > 0
    assert _image_has_colored_pixel(image)


def test_emoji_run_segmentation_keeps_zwj_sequence_together(device_module):
    runs = device_module._emoji_runs("A👨‍👩‍👧‍👦B")

    assert runs == [("text", "A"), ("emoji", "👨‍👩‍👧‍👦"), ("text", "B")]
