"""
Light async tests for the CoolLEDUX two-phase program-upload send path in
device.py (``_is_ux``, ``_ack_future`` resolution, ``_send_program_upload``,
and UX routing in ``set_text``/``send_image``).

These use hand-rolled fakes for the bleak client -- no real bleak, no
hardware. Async methods are driven with ``asyncio.run`` since this project
does not depend on pytest-asyncio.
"""

import asyncio
import types

import pytest


def _standard_escape(data: bytes) -> bytes:
    """Standard (non-quirk) escape, as the *device* emits notifications:
    each byte in {1,2,3} -> 0x02,(b^4). Unlike the send-direction
    ux_protocol.escape, there is no trailing-byte double-escape quirk."""
    out = bytearray()
    for b in data:
        if b in (0x01, 0x02, 0x03):
            out += bytes([0x02, b ^ 0x04])
        else:
            out.append(b)
    return bytes(out)


def _device_notify(cmd: int, status: int) -> bytes:
    """Build a device-style ACK notification frame (standard escape)."""
    payload = bytes([cmd, status])
    return b"\x01" + _standard_escape(len(payload).to_bytes(2, "big") + payload) + b"\x03"


def _frame_command(frame: bytes) -> int:
    """Recover the command byte (first payload byte) of a frame we sent.
    The command is never the trailing byte, so a plain standard unescape
    recovers it regardless of the send-direction trailing-byte quirk."""
    inner = frame[1:-1]
    out = bytearray()
    i = 0
    while i < len(inner):
        if inner[i] == 0x02:
            out.append(inner[i + 1] ^ 0x04)
            i += 2
        else:
            out.append(inner[i])
            i += 1
    return out[2]  # skip the 2-byte length prefix


class _FakeClient:
    """Minimal stand-in for ``BleakClientWithServiceCache``.

    Records every ``write_gatt_char`` call and, unless told to drop a
    particular write, synchronously invokes the device's notification
    handler with a canned ACK frame -- so awaiting the ACK future resolves
    without needing real BLE I/O or a live event loop tick from an external
    source.
    """

    def __init__(self, ux_module, notify_handler, acks=None):
        self.is_connected = True
        self.writes: list[bytes] = []
        self._ux_module = ux_module
        self._notify_handler = notify_handler
        # `acks`: list of (cmd, status) tuples, or None entries to simulate
        # a dropped/lost ACK (no notification fires -> caller times out).
        # Consumed one per write_gatt_char call; once exhausted, further
        # writes default to a (0x03, 0x00) success ACK.
        self._acks = list(acks) if acks is not None else None
        self._buffer = bytearray()
        self._frames = 0

    async def write_gatt_char(self, _uuid, data, response=False):
        assert response is False, "FFF1 is write-without-response only"
        self.writes.append(bytes(data))
        # The real device reassembles the 0x01..0x03 framed packet from the
        # write-without-response byte stream (fastble split=true), ACKing once
        # per complete frame. Mirror that: buffer until a full frame arrives.
        if not hasattr(self, "_buffer"):
            self._buffer = bytearray()
        self._buffer += bytes(data)
        if not (self._buffer[:1] == b"\x01" and self._buffer[-1:] == b"\x03"):
            return  # partial frame; wait for more chunks
        frame = bytes(self._buffer)
        self._buffer = bytearray()
        self._frames += 1
        idx = self._frames - 1
        if self._acks is not None and idx < len(self._acks):
            ack = self._acks[idx]
        else:
            ack = 0x00  # default success status
        if ack is None:
            return  # dropped ACK: caller must time out
        # The real device echoes the command byte of the frame it ACKs; the
        # status is what varies. Accept either a bare status int or a legacy
        # (cmd, status) tuple (cmd is derived from the frame, tuple[0] ignored).
        status = ack[1] if isinstance(ack, tuple) else ack
        cmd = _frame_command(frame)
        self._notify_handler(None, bytearray(_device_notify(cmd, status)))


def _make_ux_device(device_module, ux_module, acks=None, **kwargs):
    """Build a CoolLEDXDevice already "connected" to a _FakeClient."""
    device = device_module.CoolLEDXDevice(
        ble_device=object(),
        name=kwargs.pop("name", "CoolLEDUX-Test"),
        height=kwargs.pop("height", 16),
        width=kwargs.pop("width", 96),
        color_mode=kwargs.pop("color_mode", device_module.COLOR_MODE_FULL),
    )
    client = _FakeClient(ux_module, device._handle_notification, acks=acks)
    device._client = client
    # Default to an MTU large enough that the small frames these tests use are
    # each a single BLE write (so write-count assertions equal frame counts);
    # the dedicated split test passes an explicit small max_write.
    max_write = kwargs.pop("max_write", 512)
    device._write_char = types.SimpleNamespace(
        max_write_without_response_size=max_write
    )
    return device, client


class TestIsUxDetection:
    def test_color_mode_full_sets_is_ux(self, device_module):
        device = device_module.CoolLEDXDevice(
            ble_device=object(), color_mode=device_module.COLOR_MODE_FULL
        )
        assert device._is_ux is True

    def test_name_prefix_sets_is_ux(self, device_module):
        device = device_module.CoolLEDXDevice(
            ble_device=object(),
            name="CoolLEDUX-1234",
            color_mode=device_module.COLOR_MODE_RGB,
        )
        assert device._is_ux is True

    def test_classic_device_is_not_ux(self, device_module):
        device = device_module.CoolLEDXDevice(
            ble_device=object(),
            name="CoolLED-1234",
            color_mode=device_module.COLOR_MODE_RGB,
        )
        assert device._is_ux is False


class TestHandleNotificationResolvesAckFuture:
    def test_resolves_pending_future_with_parsed_result(self, device_module, ux_module):
        device, _client = _make_ux_device(device_module, ux_module)
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            device._ack_future = future
            frame = ux_module.encode_frame(bytes([0x03, 0x00]))
            device._handle_notification(None, bytearray(frame))
            assert future.done()
            assert future.result() == (0x03, 0x00)
        finally:
            loop.close()

    def test_ignores_notification_when_no_future_pending(self, device_module, ux_module):
        device, _client = _make_ux_device(device_module, ux_module)
        device._ack_future = None
        frame = ux_module.encode_frame(bytes([0x03, 0x00]))
        # Should not raise even though there's nothing to resolve.
        device._handle_notification(None, bytearray(frame))

    def test_does_not_overwrite_an_already_done_future(self, device_module, ux_module):
        device, _client = _make_ux_device(device_module, ux_module)
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            future.set_result((0x01, 0x01))
            device._ack_future = future
            frame = ux_module.encode_frame(bytes([0x03, 0x00]))
            device._handle_notification(None, bytearray(frame))
            assert future.result() == (0x01, 0x01)
        finally:
            loop.close()


class TestHandleNotificationCommandFiltering:
    def test_stale_ack_for_other_command_is_ignored(self, device_module, ux_module):
        # Reproduces the live bug: a begin-program upload is awaiting an ACK
        # for command 0x02, but a stale turn_on ACK (command 0x09, value 0x01)
        # arrives late from an earlier _send_simple call. It must NOT resolve
        # the begin future (which would look like status=1); only the real
        # 0x02 ACK should.
        device, _client = _make_ux_device(device_module, ux_module)
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            device._ack_future = future
            device._ack_expected_cmd = 0x02
            # Device notifications use the standard (non-quirk) escape, matching
            # device_module.encode_frame; these are real observed frames.
            stale = bytes.fromhex("0100020609020503")  # turn_on ack -> (9, 1)
            device._handle_notification(None, bytearray(stale))
            assert not future.done(), "stale ACK for a different command resolved the future"
            real = bytes.fromhex("0100020602060003")  # begin ack -> (2, 0)
            device._handle_notification(None, bytearray(real))
            assert future.done()
            assert future.result() == (0x02, 0x00)
        finally:
            loop.close()

    def test_upload_survives_stale_simple_command_acks(self, device_module, ux_module):
        # End-to-end: inject stale simple-command ACKs (0x09/0x08) ahead of the
        # begin ACK in the FakeClient stream; the upload must still succeed by
        # matching on command, not on arrival order.
        device, client = _make_ux_device(
            device_module,
            ux_module,
            acks=[(0x02, 0x00)],  # begin ok; content frames default to (0x03,0x00)
        )
        width, height = 8, 8
        pixels = [0xFFFF0000] * (width * height)
        # Pre-queue a stale ACK as if turn_on's notify is still in flight
        # (real device-style frame, standard escape).
        stale = bytes.fromhex("0100020609020503")
        device._handle_notification(None, bytearray(stale))
        asyncio.run(device._send_program_upload(pixels, width, height))
        _begin, content = ux_module.build_program_upload(pixels, width, height)
        assert len(client.writes) == 1 + len(content)


class TestSendProgramUpload:
    def test_writes_begin_then_all_content_frames_on_success(
        self, device_module, ux_module
    ):
        device, client = _make_ux_device(device_module, ux_module)
        width, height = 8, 8
        pixels = [0xFFFF0000] * (width * height)

        asyncio.run(device._send_program_upload(pixels, width, height))

        _begin, content = ux_module.build_program_upload(pixels, width, height)
        assert len(client.writes) == 1 + len(content)
        assert client.writes[0][0] == 0x01 and client.writes[0][-1] == 0x03

    def test_begin_status_1_means_already_present_no_content_sent(
        self, device_module, ux_module
    ):
        # Begin ACK status 1 = "device already has this exact program (CRC
        # match), no need to send content" -> success, only the begin frame
        # is written.
        device, client = _make_ux_device(
            device_module, ux_module, acks=[(0x02, 0x01)]
        )
        width, height = 8, 8
        pixels = [0xFFFF0000] * (width * height)

        asyncio.run(device._send_program_upload(pixels, width, height))

        assert len(client.writes) == 1  # begin only, no content

    def test_raises_when_begin_ack_status_nonzero(self, device_module, ux_module):
        # First write (begin) gets a DATA_ERROR ack; nothing else should
        # be written afterward.
        device, client = _make_ux_device(
            device_module, ux_module, acks=[(0x02, 0x03)]
        )
        width, height = 8, 8
        pixels = [0xFFFF0000] * (width * height)

        with pytest.raises(RuntimeError, match="begin-program ACK failed"):
            asyncio.run(device._send_program_upload(pixels, width, height))

        assert len(client.writes) == 1

    def test_retries_a_content_frame_and_succeeds(self, device_module, ux_module):
        # begin succeeds; first content frame fails once then succeeds.
        device, client = _make_ux_device(
            device_module,
            ux_module,
            acks=[(0x02, 0x00), (0x03, 0x03), (0x03, 0x00)],
        )
        width, height = 8, 8
        pixels = [0xFFFF0000] * (width * height)

        # Only exercise this path meaningfully if there's at least one
        # content frame to retry.
        _begin, content = ux_module.build_program_upload(pixels, width, height)
        assert len(content) >= 1

        asyncio.run(device._send_program_upload(pixels, width, height))
        # begin + first content attempt (fail) + first content retry
        # (succeed) + any remaining content frames (default-acked success).
        assert len(client.writes) == 2 + len(content)

    def test_raises_after_exhausting_retries(self, device_module, ux_module):
        # begin succeeds; every content-frame attempt is DATA_ERROR.
        acks = [(0x02, 0x00)] + [(0x03, 0x03)] * 10
        device, client = _make_ux_device(device_module, ux_module, acks=acks)
        width, height = 8, 8
        pixels = [0xFFFF0000] * (width * height)

        with pytest.raises(RuntimeError, match="content chunk 0 ACK failed"):
            asyncio.run(device._send_program_upload(pixels, width, height))

        # begin (1) + 3 failed attempts at content chunk 0.
        assert len(client.writes) == 4

    def test_large_frame_is_split_into_mtu_sized_writes(
        self, device_module, ux_module
    ):
        # A UX content frame can exceed the BLE write-without-response MTU;
        # it must be split into <= max_write_without_response_size BLE writes
        # (the sign reassembles the 0x01..0x03 stream and ACKs once per frame).
        device, client = _make_ux_device(device_module, ux_module, max_write=20)
        # A frame comfortably larger than the 20-byte MTU.
        frame = ux_module.encode_frame(bytes([0x03]) + bytes(200))

        async def _run():
            return await device._ux_write_and_await_ack(
                frame, expected_cmd=0x03, timeout=1.0
            )

        cmd, status = asyncio.run(_run())
        assert (cmd, status) == (0x03, 0x00)
        # Multiple BLE writes, each within the MTU, reassembled into one frame.
        assert len(client.writes) > 1
        assert all(len(w) <= 20 for w in client.writes)
        assert b"".join(client.writes) == frame

    def test_dropped_ack_times_out(self, device_module, ux_module):
        # A write whose ACK notification never arrives must time out
        # rather than hang forever -- this is what drives the retry loop
        # in _send_program_upload. Exercised directly against the
        # low-level helper with a short timeout (the production default is
        # 5s, too slow for a unit test).
        device, client = _make_ux_device(device_module, ux_module, acks=[None])

        async def _run():
            with pytest.raises(asyncio.TimeoutError):
                await device._ux_write_and_await_ack(
                    ux_module.encode_frame(bytes([0x09, 0x00])), timeout=0.05
                )

        asyncio.run(_run())
        assert len(client.writes) == 1
        # The future must be cleared after the timeout so a later
        # unrelated notification can't be mistaken for this write's ACK.
        assert device._ack_future is None


class TestSetTextAndSendImageRouteThroughUx:
    def test_set_text_routes_to_send_program_upload_when_ux(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)
        captured = {}

        async def fake_send_program_upload(pixels, width, height, **kw):
            captured["pixels"] = pixels
            captured["width"] = width
            captured["height"] = height

        monkeypatch.setattr(device, "_send_program_upload", fake_send_program_upload)

        asyncio.run(device.set_text("HI", color=(255, 0, 0)))

        assert captured["height"] == device.height
        assert captured["width"] > 0
        assert len(captured["pixels"]) == captured["width"] * captured["height"]
        # Every pixel is a fully-opaque 0xAARRGGBB int.
        assert all((p >> 24) & 0xFF == 0xFF for p in captured["pixels"])

    def test_send_image_routes_to_send_program_upload_when_ux(
        self, device_module, ux_module, monkeypatch
    ):
        from PIL import Image

        device, _client = _make_ux_device(device_module, ux_module)
        captured = {}

        async def fake_send_program_upload(pixels, width, height, **kw):
            captured["pixels"] = pixels
            captured["width"] = width
            captured["height"] = height

        monkeypatch.setattr(device, "_send_program_upload", fake_send_program_upload)

        img = Image.new("RGB", (40, 40), (0, 255, 0))
        asyncio.run(device.send_image(img))

        assert captured["width"] == device.width
        assert captured["height"] == device.height
        assert len(captured["pixels"]) == device.width * device.height

    def test_classic_device_does_not_route_through_ux(
        self, device_module, ux_module, monkeypatch
    ):
        device = device_module.CoolLEDXDevice(
            ble_device=object(),
            name="CoolLED-Classic",
            color_mode=device_module.COLOR_MODE_RGB,
        )
        called = {"ux": False}

        async def fake_send_program_upload(*a, **kw):
            called["ux"] = True

        monkeypatch.setattr(device, "_send_program_upload", fake_send_program_upload)

        captured_chunks = {}

        async def fake_write_chunks(chunks):
            captured_chunks["chunks"] = chunks

        monkeypatch.setattr(device, "_write_chunks", fake_write_chunks)

        asyncio.run(device.set_text("HI"))

        assert called["ux"] is False
        assert "chunks" in captured_chunks


class TestUxModeSpeedReupload:
    """mode/speed on CoolLEDUX are program-body fields, not standalone
    commands: changing them re-uploads the last content with the new field
    values (see _send_program_upload / set_speed / set_mode)."""

    def _patch_ack(self, device, monkeypatch):
        """Stub the ACK round-trip so uploads complete without a fake client."""
        async def fake_ack(frame, expected_cmd=None, timeout=5.0):
            return (expected_cmd, 0)

        monkeypatch.setattr(device, "_ux_write_and_await_ack", fake_ack)

    def test_send_program_upload_forwards_instance_fields(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)
        device._ux_mode = 7
        device._ux_speed = 100
        device._ux_stay_time = 5
        captured = {}

        def fake_build(pixels, width, height, **kw):
            captured.update(kw)
            return b"BEGIN", []

        monkeypatch.setattr(
            device_module.ux_protocol, "build_program_upload", fake_build
        )
        self._patch_ack(device, monkeypatch)

        asyncio.run(device._send_program_upload([0] * 10, 5, 2))

        assert captured["mode"] == 7
        assert captured["speed"] == 100
        assert captured["stayTime"] == 5

    def test_explicit_kwargs_override_instance_fields(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)
        device._ux_mode = 2
        captured = {}

        def fake_build(pixels, width, height, **kw):
            captured.update(kw)
            return b"BEGIN", []

        monkeypatch.setattr(
            device_module.ux_protocol, "build_program_upload", fake_build
        )
        self._patch_ack(device, monkeypatch)

        asyncio.run(device._send_program_upload([0] * 10, 5, 2, mode=9))

        assert captured["mode"] == 9

    def test_send_program_upload_stores_last_content(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)

        def fake_build(pixels, width, height, **kw):
            return b"BEGIN", []

        monkeypatch.setattr(
            device_module.ux_protocol, "build_program_upload", fake_build
        )
        self._patch_ack(device, monkeypatch)

        pixels = [0xFF00FF00] * 10
        asyncio.run(device._send_program_upload(pixels, 5, 2))

        assert device._ux_last_pixels == pixels
        assert device._ux_last_size == (5, 2)

    def test_set_speed_reuploads_last_content_on_ux(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)
        calls = []

        async def fake_send(pixels, width, height, **kw):
            device._ux_last_pixels = pixels
            device._ux_last_size = (width, height)
            calls.append((width, height))

        monkeypatch.setattr(device, "_send_program_upload", fake_send)

        asyncio.run(device.set_text("HI"))
        assert len(calls) == 1

        asyncio.run(device.set_speed(42))

        assert device._ux_speed == 42
        assert len(calls) == 2  # last content re-uploaded with the new speed

    def test_set_mode_reuploads_last_content_on_ux(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)
        calls = []

        async def fake_send(pixels, width, height, **kw):
            device._ux_last_pixels = pixels
            device._ux_last_size = (width, height)
            calls.append((width, height))

        monkeypatch.setattr(device, "_send_program_upload", fake_send)

        asyncio.run(device.set_text("HI"))
        asyncio.run(device.set_mode(7))

        assert device._ux_mode == 7
        assert len(calls) == 2

    def test_set_speed_without_content_does_not_upload_on_ux(
        self, device_module, ux_module, monkeypatch
    ):
        device, _client = _make_ux_device(device_module, ux_module)
        calls = []

        async def fake_send(*a, **kw):
            calls.append(a)

        monkeypatch.setattr(device, "_send_program_upload", fake_send)

        asyncio.run(device.set_speed(42))

        assert device._ux_speed == 42
        assert calls == []  # nothing uploaded yet -> nothing to re-upload

    def test_classic_set_speed_sends_simple_command(
        self, device_module, ux_module, monkeypatch
    ):
        device = device_module.CoolLEDXDevice(
            ble_device=object(),
            name="CoolLED-Classic",
            color_mode=device_module.COLOR_MODE_RGB,
        )
        sent = []

        async def fake_simple(payload):
            sent.append(bytes(payload))

        monkeypatch.setattr(device, "_send_simple", fake_simple)

        asyncio.run(device.set_speed(200))

        assert sent == [bytes([device_module.CMD_SPEED, 200])]

    def test_classic_set_mode_sends_simple_command(
        self, device_module, ux_module, monkeypatch
    ):
        device = device_module.CoolLEDXDevice(
            ble_device=object(),
            name="CoolLED-Classic",
            color_mode=device_module.COLOR_MODE_RGB,
        )
        sent = []

        async def fake_simple(payload):
            sent.append(bytes(payload))

        monkeypatch.setattr(device, "_send_simple", fake_simple)

        asyncio.run(device.set_mode(3))

        assert sent == [bytes([device_module.CMD_MODE, 3])]
