"""
Unit tests for the CoolLEDUX ("ux_protocol") pure-Python protocol port.

Every test is driven by golden-vector fixtures under tests/fixtures/ that were
generated from the official app's decompiled Java (CoolledUXUtils / TextEmoji
ManagerCoolLEDUX / DeviceManager.CoolleduxGraffitiProgramContent). See each
fixture's "format_notes" field for its exact schema.

This module (custom_components/coolledx/ux_protocol.py) must stay free of
homeassistant/bleak imports so it is independently unit-testable; it is loaded
here via the `ux_module` fixture (importlib, bypassing the package __init__).
"""

from conftest import load_fixture


# ===========================================================================
# rgb444_transfer / pixel_to_rgb444
# ===========================================================================


class TestRgb444Transfer:
    """rgb444Transfer(channel): 0-255 -> 4-bit nibble 0-15."""

    def test_all_channel_cases(self, ux_module):
        fixture = load_fixture("ux_rgb444.json")
        for case in fixture["rgb444_transfer_cases"]:
            result = ux_module.rgb444_transfer(case["channel"])
            assert result == case["value"], f"channel={case['channel']}"

    def test_boundary_at_238_is_15(self, ux_module):
        assert ux_module.rgb444_transfer(238) == 15

    def test_boundary_at_47_is_0(self, ux_module):
        assert ux_module.rgb444_transfer(47) == 0

    def test_boundary_at_48_is_1(self, ux_module):
        assert ux_module.rgb444_transfer(48) == 1


class TestPixelToRgb444:
    """pixel_to_rgb444(argb) -> 2 bytes [0x0R, 0xGB]."""

    def test_all_pixel_cases(self, ux_module):
        fixture = load_fixture("ux_rgb444.json")
        for case in fixture["rgb444_pixel_cases"]:
            argb = int(case["argb_hex"], 16)
            result = ux_module.pixel_to_rgb444(argb)
            expected = bytes.fromhex(case["output_hex"])
            assert result == expected, f"argb_hex={case['argb_hex']}"

    def test_returns_two_bytes(self, ux_module):
        result = ux_module.pixel_to_rgb444(0xFFFFFFFF)
        assert isinstance(result, bytes)
        assert len(result) == 2


# ===========================================================================
# crc32_mpeg
# ===========================================================================


class TestCrc32Mpeg:
    """CRC32 (poly 0x04C11DB7, init 0xFFFFFFFF, MSB-first, no reflection/xor)."""

    def test_all_crc_cases_int(self, ux_module):
        fixture = load_fixture("ux_crc.json")
        for case in fixture["cases"]:
            data = bytes.fromhex(case["input_hex"])
            result = ux_module.crc32_mpeg(data)
            expected = int(case["output_hex"], 16)
            assert result == expected, case["name"]

    def test_all_crc_cases_bytes_helper(self, ux_module):
        fixture = load_fixture("ux_crc.json")
        for case in fixture["cases"]:
            data = bytes.fromhex(case["input_hex"])
            result = ux_module.crc32_mpeg_bytes(data)
            expected = bytes.fromhex(case["output_hex"])
            assert result == expected, case["name"]
            assert len(result) == 4

    def test_empty_input_is_all_ones(self, ux_module):
        assert ux_module.crc32_mpeg(b"") == 0xFFFFFFFF


# ===========================================================================
# lzss_compress
# ===========================================================================


class TestLzssCompress:
    """Okumura tree-LZSS port: N=512, F=18, THRESHOLD=2."""

    def test_all_lzss_cases(self, ux_module):
        fixture = load_fixture("ux_lzss.json")
        for case in fixture["cases"]:
            data = bytes.fromhex(case["input_hex"])
            result = ux_module.lzss_compress(data)
            if case["output_hex"] is None:
                assert result is None, case["name"]
            else:
                expected = bytes.fromhex(case["output_hex"])
                assert result == expected, case["name"]

    def test_empty_input_returns_none_not_empty_bytes(self, ux_module):
        # Java returned null for zero-length input; the Python port must
        # replicate this faithfully (None, not b"").
        result = ux_module.lzss_compress(b"")
        assert result is None


# ===========================================================================
# escape / convertData framing quirk
# ===========================================================================


class TestEscape:
    """convertData/escape: 1/2/3 -> 02,(b^4); trailing control byte at the
    very end of the input array is double-processed: 02,06,(b^4)."""

    def test_escape_basic_control_bytes_not_at_end(self, ux_module):
        # A trailing byte always triggers the double-escape quirk (see
        # test_escape_trailing_control_byte_quirk below), so to exercise the
        # *normal* 2-byte substitution the control byte must be followed by
        # something else.
        assert ux_module.escape(bytes([0x02, 0x00])) == bytes.fromhex("020600")
        assert ux_module.escape(bytes([0x01, 0x00])) == bytes.fromhex("020500")
        assert ux_module.escape(bytes([0x03, 0x00])) == bytes.fromhex("020700")

    def test_escape_null_byte_not_escaped(self, ux_module):
        assert ux_module.escape(bytes([0x00])) == bytes([0x00])

    def test_escape_normal_bytes_pass_through(self, ux_module):
        assert ux_module.escape(bytes([0x04, 0xFF, 0x10])) == bytes([0x04, 0xFF, 0x10])

    def test_escape_trailing_control_byte_quirk(self, ux_module):
        # Last byte of input is 0x01: must double-process to 02 06 05,
        # NOT the plain 2-byte escape (02 05) used everywhere else.
        result = ux_module.escape(bytes([0x04, 0x01]))
        assert result == bytes.fromhex("040206 05".replace(" ", ""))

    def test_escape_non_trailing_control_byte_is_normal(self, ux_module):
        # Same byte (0x01), but NOT last -> normal 2-byte escape.
        result = ux_module.escape(bytes([0x01, 0x04]))
        assert result == bytes.fromhex("020504")

    def test_escape_trailing_quirk_for_02_and_03(self, ux_module):
        # A single-byte array trivially makes that byte "last" -> quirk
        # applies even for a lone control byte.
        # Trailing 0x02 -> marker(02) + escaped-marker(06) + escaped value(06)
        assert ux_module.escape(bytes([0x02])) == bytes.fromhex("020606")
        # Trailing 0x03 -> 02 06 07
        assert ux_module.escape(bytes([0x03])) == bytes.fromhex("020607")
        # Trailing 0x01 -> 02 06 05
        assert ux_module.escape(bytes([0x01])) == bytes.fromhex("020605")


# ===========================================================================
# build_ux_packet
# ===========================================================================


class TestBuildUxPacket:
    """getDataPacket(payload, cmd, pkgSize): chunk + frame."""

    def test_all_packet_cases(self, ux_module):
        fixture = load_fixture("ux_packet.json")
        for case in fixture["cases"]:
            cmd = int(case["cmd"], 16)
            pkg_size = case["pkg_size"]
            data = bytes.fromhex(case["input_hex"])
            result = ux_module.build_ux_packet(data, cmd, pkg_size)
            expected = [bytes.fromhex(h) for h in case["output_hex_chunks"]]
            assert len(result) == len(expected), case["name"]
            for idx, (got, exp) in enumerate(zip(result, expected)):
                assert got == exp, f"{case['name']} chunk {idx}"

    def test_small_payload_single_chunk(self, ux_module):
        fixture = load_fixture("ux_packet.json")
        case = next(c for c in fixture["cases"] if c["name"] == "small_10_byte_payload")
        data = bytes.fromhex(case["input_hex"])
        result = ux_module.build_ux_packet(data, int(case["cmd"], 16), case["pkg_size"])
        assert len(result) == 1
        assert result[0][0] == 0x01
        assert result[0][-1] == 0x03

    def test_large_payload_three_chunks(self, ux_module):
        fixture = load_fixture("ux_packet.json")
        case = next(
            c for c in fixture["cases"] if c["name"] == "large_2500_byte_payload_3_chunks"
        )
        data = bytes.fromhex(case["input_hex"])
        result = ux_module.build_ux_packet(data, int(case["cmd"], 16), case["pkg_size"])
        assert len(result) == 3


# ===========================================================================
# build_begin
# ===========================================================================


class TestBuildBegin:
    """getStartDataForProgram(progData, i, i2, i3)."""

    def test_all_begin_cases(self, ux_module):
        fixture = load_fixture("ux_begin.json")
        for case in fixture["cases"]:
            prog_data = bytes.fromhex(case["prog_data_hex"])
            result = ux_module.build_begin(prog_data, case["i"], case["i2"], case["i3"])
            expected = bytes.fromhex(case["output_hex"])
            assert result == expected, case["name"]

    def test_begin_is_framed(self, ux_module):
        fixture = load_fixture("ux_begin.json")
        case = fixture["cases"][0]
        prog_data = bytes.fromhex(case["prog_data_hex"])
        result = ux_module.build_begin(prog_data, case["i"], case["i2"], case["i3"])
        assert result[0] == 0x01
        assert result[-1] == 0x03


# ===========================================================================
# build_image_program
# ===========================================================================


class TestBuildImageProgram:
    """getDataWithGraffitiCombineProgram: unframed graffiti body, column-major
    pixel order."""

    def test_solid_red_16x96_matches_fixture(self, ux_module):
        fixture = load_fixture("ux_image_program.json")
        params = fixture["params"]
        width = params["showWidth"]
        height = params["showHeight"]
        argb = int(params["argb_color"], 16)
        pixels = [argb] * (width * height)

        result = ux_module.build_image_program(
            pixels,
            width,
            height,
            layerType=params["layerType"],
            mode=params["mode"],
            speed=params["speed"],
            stayTime=params["stayTime"],
            startColumn=params["startColumn"],
            startRow=params["startRow"],
        )
        expected = bytes.fromhex(fixture["output_hex"])
        assert result == expected

    def test_uses_default_params(self, ux_module):
        # Defaults per the fixture's note: layerType=1, mode=2, speed=255,
        # stayTime=3, startColumn=0, startRow=0.
        fixture = load_fixture("ux_image_program.json")
        params = fixture["params"]
        width = params["showWidth"]
        height = params["showHeight"]
        argb = int(params["argb_color"], 16)
        pixels = [argb] * (width * height)

        result = ux_module.build_image_program(pixels, width, height)
        expected = bytes.fromhex(fixture["output_hex"])
        assert result == expected

    def test_body_is_unframed(self, ux_module):
        # Unlike build_ux_packet/build_begin, this body is NOT framed with
        # 0x01/0x03 -- it starts with the body length prefix.
        fixture = load_fixture("ux_image_program.json")
        params = fixture["params"]
        width = params["showWidth"]
        height = params["showHeight"]
        argb = int(params["argb_color"], 16)
        pixels = [argb] * (width * height)
        result = ux_module.build_image_program(pixels, width, height)
        assert result[0] != 0x01  # not frame-wrapped
        expected_len_prefix = (len(result)).to_bytes(4, "big")
        assert result[0:4] == expected_len_prefix
