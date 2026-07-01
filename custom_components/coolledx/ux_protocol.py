"""
CoolLEDUX ("Rayhome Devil Eyes" full-colour app) protocol layer.

Ported from the official CoolLEDUX Android app's decompiled Java
(CoolledUXUtils / TextEmojiManagerCoolLEDUX / DeviceManager.Coolledux
GraffitiProgramContent). This module is deliberately free of
``homeassistant``/``bleak`` imports so it can be unit-tested standalone
against the golden-vector fixtures in ``tests/fixtures/``.

This is a *separate* protocol dialect from the simpler one implemented in
``device.py`` (CoolLEDX classic). The two share superficial similarities
(0x01/0x03 framing, XOR checksums) but differ in important, fixture-verified
ways -- most notably the escape/``convertData`` routine here has a trailing-
byte double-escape quirk that ``device.py``'s regex-based ``escape`` does not
reproduce (see :func:`escape` docstring).
"""

from __future__ import annotations

# ===========================================================================
# RGB444 colour quantisation
# ===========================================================================


def rgb444_transfer(channel: int) -> int:
    """Quantise an 8-bit colour channel (0-255) to a 4-bit nibble (0-15).

    Ported from ``TextEmojiManagerCoolLEDUX.rgb444Transfer(channel)``:

    * ``channel >= 238`` -> 15
    * ``channel <= 47``  -> 0
    * otherwise          -> ``((channel - 47) // 14) + 1``

    Args:
        channel: One colour component (0-255).

    Returns:
        4-bit nibble (0-15).
    """
    if channel >= 238:
        return 15
    if channel <= 47:
        return 0
    return ((channel - 47) // 14) + 1


def pixel_to_rgb444(argb: int) -> bytes:
    """Encode one 0xAARRGGBB pixel as the 2-byte RGB444 wire format.

    Ported from ``TextEmojiManagerCoolLEDUX.getColorDataWithColorWithRGB444
    Transfer(argb)``. Alpha is ignored.

    Byte layout: ``[0x0R, 0xGB]`` where R/G/B are each the 4-bit
    :func:`rgb444_transfer` of the corresponding 8-bit channel.

    Args:
        argb: Packed 0xAARRGGBB pixel value.

    Returns:
        2 raw bytes.
    """
    r = (argb >> 16) & 0xFF
    g = (argb >> 8) & 0xFF
    b = argb & 0xFF
    rv = rgb444_transfer(r)
    gv = rgb444_transfer(g)
    bv = rgb444_transfer(b)
    return bytes([rv, (gv << 4) | bv])


# ===========================================================================
# CRC32 (CrcCode.getCrc32CheckCode2) -- NOT textbook/zlib CRC32
# ===========================================================================

_CRC32_POLY = 0x04C11DB7
_CRC32_INIT = 0xFFFFFFFF
_CRC32_MASK = 0xFFFFFFFF


def crc32_mpeg(data: bytes) -> int:
    """Bit-serial CRC32 matching ``CoolledUXUtils.CrcCode.getCrc32CheckCode2``.

    This is **not** the textbook/zlib CRC-32 (no reflection, no final XOR).
    Poly ``0x04C11DB7``, init ``0xFFFFFFFF``, MSB-first, 32 inner bit
    iterations per input byte (only the low 8 of which ever see a data bit,
    since bytes are <= 255 -- but all 32 iterations are kept since they
    affect the running CRC register state).

    Args:
        data: Raw input bytes.

    Returns:
        32-bit unsigned CRC value.
    """
    i = _CRC32_INIT
    for byte in data:
        mask = 0x80000000
        for _ in range(32):
            i = ((i << 1) ^ _CRC32_POLY) if (i & 0x80000000) else (i << 1)
            i &= _CRC32_MASK
            if byte & mask:
                i ^= _CRC32_POLY
            mask >>= 1
    return i


def crc32_mpeg_bytes(data: bytes) -> bytes:
    """:func:`crc32_mpeg` result as 4 big-endian bytes."""
    return crc32_mpeg(data).to_bytes(4, byteorder="big")


# ===========================================================================
# LZSS compression -- verbatim port of Okumura's tree-LZSS
# ===========================================================================

# Window size, max match length, and minimum match length to bother coding
# as a back-reference (anything shorter is cheaper to emit as a literal).
# These three constants (and everything derived from them below) match
# CoolledUXUtils.LzssCompress.lazssCompress(byte[]) exactly -- they differ
# from the textbook Okumura lzss.c defaults (N=4096) intentionally.
_LZSS_N = 512
_LZSS_F = 18
_LZSS_THRESHOLD = 2
_LZSS_NIL = _LZSS_N

# text_buf holds the sliding window plus an F-1 byte mirror region at the
# end (so that comparisons of up to F bytes starting near the end of the
# window never need to wrap with a modulo).
_LZSS_TEXT_BUF_SIZE = _LZSS_N + _LZSS_F - 1  # 529
# lson/dad are indexed only by window positions (0..N-1), sized N+1 (513)
# for headroom matching the reference implementation.
_LZSS_TREE_SIZE = _LZSS_N + 1  # 513
# rson is additionally indexed by the 256 root/sentinel nodes (one per
# possible first-byte value) at indices N+1 .. N+256.
_LZSS_RSON_SIZE = _LZSS_N + 257  # 769


def _m(byte: int) -> int:
    """Coerce to an unsigned byte value (0-255), mirroring Java's `b & 0xff`."""
    return byte & 0xFF


class _LzssEncoder:
    """Stateful binary-tree LZSS encoder (one-shot use per instance).

    A faithful, verbatim port of Okumura's tree-LZSS encoder (InsertNode /
    DeleteNode / Encode), parameterised with N=512, F=18, THRESHOLD=2 to
    match the CoolLEDUX Java implementation. Unlike the classic textual
    lzss.c (which pre-fills the sliding window with ASCII spaces, 0x20),
    this port pre-fills with 0x00 -- verified against the
    ``solid_red_16x96_rgb444_columnmajor_3072bytes`` fixture, whose expected
    output is only reproduced with a zero-filled window.
    """

    def __init__(self) -> None:
        self.text_buf = bytearray(_LZSS_TEXT_BUF_SIZE)
        self.lson = [0] * _LZSS_TREE_SIZE
        self.dad = [0] * _LZSS_TREE_SIZE
        self.rson = [0] * _LZSS_RSON_SIZE
        self.match_position = 0
        self.match_length = 0

    def _init_tree(self) -> None:
        for i in range(_LZSS_N + 1, _LZSS_N + 257):
            self.rson[i] = _LZSS_NIL
        for i in range(_LZSS_N):
            self.dad[i] = _LZSS_NIL

    def _insert_node(self, pos: int) -> None:
        text_buf = self.text_buf
        lson = self.lson
        rson = self.rson
        dad = self.dad
        f = _LZSS_F

        cmp = 1
        p = _LZSS_N + 1 + text_buf[pos]
        rson[pos] = _LZSS_NIL
        lson[pos] = _LZSS_NIL
        match_length = 0
        match_position = 0

        while True:
            if cmp >= 0:
                if rson[p] != _LZSS_NIL:
                    p = rson[p]
                else:
                    rson[p] = pos
                    dad[pos] = p
                    self.match_length = match_length
                    self.match_position = match_position
                    return
            else:
                if lson[p] != _LZSS_NIL:
                    p = lson[p]
                else:
                    lson[p] = pos
                    dad[pos] = p
                    self.match_length = match_length
                    self.match_position = match_position
                    return

            i = 1
            while i < f:
                cmp = text_buf[pos + i] - text_buf[p + i]
                if cmp != 0:
                    break
                i += 1

            if i > match_length:
                match_position = p
                match_length = i
                if match_length >= f:
                    break

        dad[pos] = dad[p]
        lson[pos] = lson[p]
        rson[pos] = rson[p]
        dad[lson[p]] = pos
        dad[rson[p]] = pos
        if rson[dad[p]] == p:
            rson[dad[p]] = pos
        else:
            lson[dad[p]] = pos
        dad[p] = _LZSS_NIL
        self.match_length = match_length
        self.match_position = match_position

    def _delete_node(self, p: int) -> None:
        lson = self.lson
        rson = self.rson
        dad = self.dad

        if dad[p] == _LZSS_NIL:
            return
        if rson[p] == _LZSS_NIL:
            q = lson[p]
        elif lson[p] == _LZSS_NIL:
            q = rson[p]
        else:
            q = lson[p]
            if rson[q] != _LZSS_NIL:
                while rson[q] != _LZSS_NIL:
                    q = rson[q]
                rson[dad[q]] = lson[q]
                dad[lson[q]] = dad[q]
                lson[q] = lson[p]
                dad[lson[p]] = q
            rson[q] = rson[p]
            dad[rson[p]] = q
        dad[q] = dad[p]
        if rson[dad[p]] == p:
            rson[dad[p]] = q
        else:
            lson[dad[p]] = q
        dad[p] = _LZSS_NIL

    def encode(self, data: bytes) -> bytes:
        n = _LZSS_N
        f = _LZSS_F
        threshold = _LZSS_THRESHOLD
        text_buf = self.text_buf

        out = bytearray()
        self._init_tree()

        code_buf = bytearray(17)
        code_buf[0] = 0
        code_buf_ptr = 1
        mask = 1

        data_pos = 0
        s = 0
        r = n - f
        # Pre-fill the not-yet-populated window with 0x00 (verified against
        # the solid-colour fixture -- NOT the classic lzss.c ' ' filler).
        for i in range(s, r):
            text_buf[i] = 0

        length = 0
        while length < f and data_pos < len(data):
            text_buf[r + length] = _m(data[data_pos])
            data_pos += 1
            length += 1

        if length == 0:
            return bytes(out)

        for i in range(1, f + 1):
            self._insert_node(r - i)
        self._insert_node(r)
        match_length = self.match_length
        match_position = self.match_position

        while True:
            if match_length > length:
                match_length = length
            if match_length <= threshold:
                match_length = 1
                code_buf[0] |= mask
                code_buf[code_buf_ptr] = text_buf[r]
                code_buf_ptr += 1
            else:
                code_buf[code_buf_ptr] = match_position & 0xFF
                code_buf_ptr += 1
                code_buf[code_buf_ptr] = (
                    ((match_position >> 4) & 0xF0) | (match_length - (threshold + 1))
                ) & 0xFF
                code_buf_ptr += 1

            mask = (mask << 1) & 0xFF
            if mask == 0:
                out += code_buf[:code_buf_ptr]
                code_buf[0] = 0
                code_buf_ptr = 1
                mask = 1

            last_match_length = match_length
            i = 0
            while i < last_match_length and data_pos < len(data):
                c = _m(data[data_pos])
                data_pos += 1
                self._delete_node(s)
                text_buf[s] = c
                if s < f - 1:
                    text_buf[s + n] = c
                s = (s + 1) & (n - 1)
                r = (r + 1) & (n - 1)
                self._insert_node(r)
                match_length = self.match_length
                match_position = self.match_position
                i += 1
            while i < last_match_length:
                i += 1
                self._delete_node(s)
                s = (s + 1) & (n - 1)
                r = (r + 1) & (n - 1)
                length -= 1
                if length:
                    self._insert_node(r)
                    match_length = self.match_length
                    match_position = self.match_position

            if length <= 0:
                break

        if code_buf_ptr > 1:
            out += code_buf[:code_buf_ptr]

        return bytes(out)


def lzss_compress(data: bytes) -> bytes | None:
    """Compress *data* with the CoolLEDUX tree-LZSS variant.

    Verbatim port of ``CoolledUXUtils.LzssCompress.lazssCompress(byte[])``
    (classic Okumura tree-LZSS, N=512, F=18, THRESHOLD=2).

    Args:
        data: Raw bytes to compress.

    Returns:
        Compressed bytes, or ``None`` for zero-length input (the original
        Java method returns ``null`` in this case -- this is replicated
        faithfully rather than returning ``b""``).
    """
    if len(data) == 0:
        return None
    return _LzssEncoder().encode(data)


# ===========================================================================
# Wire escaping / framing  (getSendDataWithInfo / convertData)
# ===========================================================================

_ESCAPE_MARKER = 0x02
_ESCAPE_CONTROL_BYTES = (0x01, 0x02, 0x03)


def escape(data: bytes) -> bytes:
    """Escape control bytes 0x01/0x02/0x03, per CoolLEDUX's ``convertData``.

    For each byte ``b`` in ``{1, 2, 3}``, the normal substitution is two
    bytes: a marker (``0x02``) followed by ``b ^ 4``.

    **Quirk (verified against ``ux_packet.json``'s 10-byte case, which
    triggers it via a trailing 0x01 checksum byte):** the original Java
    ``convertData`` is recursive, and when the byte needing escaping is the
    very *last* element of the input array, the inserted marker byte gets
    re-escaped too (as if convertData recursed on its own output), emitting
    **three** bytes -- ``0x02, 0x06, b ^ 4`` -- instead of the normal two.
    This does not occur for control bytes anywhere else in the array, even
    when they appear back-to-back.

    This is functionally distinct from ``device.py``'s regex-based
    ``escape()``, which does not reproduce this trailing-byte quirk and so
    cannot be reused here.

    Args:
        data: Raw (unescaped) bytes.

    Returns:
        Escaped bytes.
    """
    out = bytearray()
    n = len(data)
    for idx, b in enumerate(data):
        if b in _ESCAPE_CONTROL_BYTES:
            if idx == n - 1:
                out.append(_ESCAPE_MARKER)
                out.append(_ESCAPE_MARKER ^ 4)
                out.append(b ^ 4)
            else:
                out.append(_ESCAPE_MARKER)
                out.append(b ^ 4)
        else:
            out.append(b)
    return bytes(out)


def encode_frame(payload: bytes) -> bytes:
    """Wrap *payload* in a CoolLEDUX protocol frame.

    Frame format: ``0x01 + escape(len(payload)(2 BE) + payload) + 0x03``.
    This is ``getSendDataWithInfo`` in the decompiled Java.

    Args:
        payload: Raw (unescaped) payload bytes.

    Returns:
        Complete framed bytes.
    """
    length_prefix = len(payload).to_bytes(2, byteorder="big")
    return b"\x01" + escape(length_prefix + payload) + b"\x03"


def xor_checksum(data: bytes) -> int:
    """XOR all bytes in *data* and return the result (0-255)."""
    result = 0
    for byte in data:
        result ^= byte
    return result


# ===========================================================================
# Packet chunking  (getDataPacket)
# ===========================================================================


def build_ux_packet(data: bytes, cmd: int, pkg_size: int = 1004) -> list[bytes]:
    """Chunk *data* into <= ``pkg_size``-byte pieces and frame each one.

    Ported from ``CoolledUXUtils.getDataPacket(payload, cmd, pkgSize)``.

    Each chunk wrapper (before framing) is::

        cmd       (1 byte)
        0x00      (1 byte)
        totalLen  (4 bytes BE) -- length of the full, un-chunked payload
        chunkIdx  (2 bytes BE) -- zero-based chunk index
        chunkLen  (2 bytes BE) -- number of data bytes in this chunk
        chunk                  -- up to pkg_size bytes
        checksum  (1 byte)     -- XOR of everything from the 0x00 byte
                                   through the end of chunk data (cmd is
                                   excluded)

    Each wrapper is then framed via :func:`encode_frame`.

    Args:
        data:     Full raw payload bytes to chunk.
        cmd:      Command byte that prefixes every chunk wrapper.
        pkg_size: Maximum number of data bytes per chunk.

    Returns:
        List of fully-framed chunk byte strings, in chunk-index order.
    """
    total_len = len(data)
    pieces: list[bytes] = []
    if total_len == 0:
        pieces = [b""]
    else:
        for offset in range(0, total_len, pkg_size):
            pieces.append(data[offset : offset + pkg_size])

    frames: list[bytes] = []
    for idx, piece in enumerate(pieces):
        inner = (
            b"\x00"
            + total_len.to_bytes(4, byteorder="big")
            + idx.to_bytes(2, byteorder="big")
            + len(piece).to_bytes(2, byteorder="big")
            + piece
        )
        checksum = xor_checksum(inner)
        wrapper = bytes([cmd & 0xFF]) + inner + bytes([checksum])
        frames.append(encode_frame(wrapper))
    return frames


# ===========================================================================
# "Begin program" command  (getStartDataForProgram)
# ===========================================================================


def build_begin(prog_data: bytes, i: int, i2: int, i3: int) -> bytes:
    """Build the framed "begin program" command.

    Ported from ``CoolledUXUtils.getStartDataForProgram(progData, i, i2,
    i3)``.

    Body (before framing)::

        0x02
        crc       (4 bytes BE) -- crc32_mpeg(progData)
        len       (4 bytes BE) -- len(progData)
        i, i2, i3 (1 byte each)

    Args:
        prog_data: Program data the CRC/length are computed over.
        i:  Index byte.
        i2: Device/show-count style field (raw byte; exact device-side
            semantics don't affect wire encoding).
        i3: Same as ``i2``.

    Returns:
        Complete framed command bytes.
    """
    crc = crc32_mpeg(prog_data)
    body = (
        bytes([0x02])
        + crc.to_bytes(4, byteorder="big")
        + len(prog_data).to_bytes(4, byteorder="big")
        + bytes([i & 0xFF, i2 & 0xFF, i3 & 0xFF])
    )
    return encode_frame(body)


# ===========================================================================
# Graffiti/image program body  (getDataWithGraffitiCombineProgram)
# ===========================================================================


def build_image_program(
    pixels_argb: list[int],
    width: int,
    height: int,
    layerType: int = 1,
    mode: int = 2,
    speed: int = 255,
    stayTime: int = 3,
    startColumn: int = 0,
    startRow: int = 0,
) -> bytes:
    """Build the (unframed) graffiti/image program body.

    Ported from ``CoolledUXUtils.getDataWithGraffitiCombineProgram`` for a
    single full-frame image (this routine does not call
    ``getSendDataWithInfo`` -- the caller is responsible for any further
    framing, e.g. via :func:`build_begin` / :func:`build_ux_packet`).

    ``pixels_argb`` is row-major (index = ``row * width + col``, matching
    how callers typically build pixel buffers), but the wire format itself
    is **column-major**: for each column left-to-right, all rows top-to-
    bottom are emitted before moving to the next column.

    Body layout::

        len4BE(bodyLen + 4)
        0x02
        7 x 0x00
        layerType    (1 byte)
        startColumn  (2 bytes BE)
        startRow     (2 bytes BE)
        showWidth    (2 bytes BE)
        showHeight   (2 bytes BE)
        mode         (1 byte)
        speed        (1 byte)
        stayTime     (1 byte)
        len4BE(pixelDataLen)
        pixelData    (RGB444, 2 bytes/pixel, column-major)

    Args:
        pixels_argb: Row-major list of 0xAARRGGBB pixel values, length
                     ``width * height``.
        width:       Image width in pixels (also used as ``showWidth``).
        height:      Image height in pixels (also used as ``showHeight``).
        layerType:   Layer type byte.
        mode:        Display mode byte.
        speed:       Display speed byte.
        stayTime:    Stay-time byte.
        startColumn: Starting column offset on the sign.
        startRow:    Starting row offset on the sign.

    Returns:
        Complete unframed program body bytes.
    """
    pixel_data = bytearray()
    for x in range(width):
        for y in range(height):
            pixel_data += pixel_to_rgb444(pixels_argb[y * width + x])

    body = bytearray()
    body += bytes([0x02])
    body += bytes(7)
    body += bytes([layerType & 0xFF])
    body += startColumn.to_bytes(2, byteorder="big")
    body += startRow.to_bytes(2, byteorder="big")
    body += width.to_bytes(2, byteorder="big")
    body += height.to_bytes(2, byteorder="big")
    body += bytes([mode & 0xFF, speed & 0xFF, stayTime & 0xFF])
    body += len(pixel_data).to_bytes(4, byteorder="big")
    body += pixel_data

    out = bytearray()
    out += (len(body) + 4).to_bytes(4, byteorder="big")
    out += body
    return bytes(out)


# ===========================================================================
# Program wrapper  (getDataWithProgram)
# ===========================================================================


def wrap_program(body: bytes, content_number: int = 1) -> bytes:
    """Wrap a graffiti/image program body as full "program" data.

    Ported from ``DeviceManager.getDataWithProgram``::

        8 x 0x00
        contentNumber (1 byte) -- number of combine programs (1 for a
                                   single image/graffiti body)
        0x00
        body

    Args:
        body:           Unframed graffiti/image program body, e.g. the
                         output of :func:`build_image_program`.
        content_number: Number of combine programs bundled in *body*.

    Returns:
        Wrapped program data bytes.
    """
    return b"\x00" * 8 + bytes([content_number & 0xFF]) + b"\x00" + body


# ===========================================================================
# Full program upload  (begin + compressed content packets)
# ===========================================================================


def build_program_upload(
    pixels_argb: list[int],
    width: int,
    height: int,
    *,
    program_index: int = 0,
    program_count: int = 1,
    show_count: int = 0,
    pkg_size: int = 1004,
    **image_kwargs,
) -> tuple[bytes, list[bytes]]:
    """Build the full two-phase program upload for a single image.

    Combines :func:`build_image_program`, :func:`wrap_program`,
    :func:`build_begin`, and :func:`build_ux_packet` (with LZSS
    compression) into the ``(begin_frame, content_frames)`` pair the
    device expects: write ``begin_frame`` first and wait for its ACK,
    then write each of ``content_frames`` in order, waiting for an ACK
    after each one.

    Args:
        pixels_argb:    Row-major list of 0xAARRGGBB pixel values, length
                         ``width * height``.
        width:          Image width in pixels.
        height:         Image height in pixels.
        program_index:  Index byte passed to :func:`build_begin` as ``i``.
        program_count:  Program-count byte passed to :func:`build_begin`
                         as ``i2``.
        show_count:     Show-count byte passed to :func:`build_begin` as
                         ``i3``.
        pkg_size:       Maximum bytes per content chunk (see
                         :func:`build_ux_packet`).
        **image_kwargs: Extra keyword arguments forwarded to
                         :func:`build_image_program` (``layerType``,
                         ``mode``, ``speed``, ``stayTime``,
                         ``startColumn``, ``startRow``).

    Returns:
        ``(begin_frame, content_frames)`` -- the framed "begin program"
        command and a list of framed, LZSS-compressed content chunks.
    """
    body = build_image_program(pixels_argb, width, height, **image_kwargs)
    prog = wrap_program(body, content_number=1)
    begin = build_begin(prog, program_index, program_count, show_count)
    compressed = lzss_compress(prog)
    content = build_ux_packet(compressed, 0x03, pkg_size)
    return begin, content


# ===========================================================================
# Notification parsing  (recoverData)
# ===========================================================================


def parse_notification(frame: bytes) -> tuple[int | None, int | None]:
    """Unframe a device notification and extract ``(command, status)``.

    Ported from the decompiled ``recoverData``: strips the leading
    ``0x01``/trailing ``0x03`` frame markers, reverses the escape
    (``0x02, X`` -> ``X ^ 4``), drops the 2-byte length prefix, and
    returns the **first two** bytes of the recovered payload as
    ``(response_command, status_code)``.  The command is always the first
    payload byte; simple-command ACKs carry a 2-byte payload
    ``[cmd, echoed-value]`` while content-packet ACKs carry a longer
    payload ``[cmd, status, index/progress...]`` — so the last two bytes
    are *not* the status.

    Status ``0x00`` means success; see the module-level ``ErrorCode``
    values (0x01 TRANSMISSION_FAILED, 0x02 DEVICE_ABNORMALITY, 0x03
    DATA_ERROR, 0x04 DATA_LENGTH_ERROR, 0x05 DATA_ID_ERROR, 0x06
    DATA_CHECKSUM_ERROR) for the meaning of non-zero values.

    Args:
        frame: Raw notification bytes as received from the BLE
               characteristic (including the ``0x01``/``0x03`` framing).

    Returns:
        ``(response_command, status_code)``, or ``(None, None)`` if
        *frame* is not a well-formed frame (missing markers, or too
        short to contain a length prefix + 2-byte payload).
    """
    if len(frame) < 2 or frame[0] != 0x01 or frame[-1] != 0x03:
        return (None, None)

    middle = frame[1:-1]
    out = bytearray()
    i = 0
    n = len(middle)
    while i < n:
        b = middle[i]
        if b == _ESCAPE_MARKER and i + 1 < n:
            out.append(middle[i + 1] ^ 4)
            i += 2
        else:
            out.append(b)
            i += 1

    # First 2 bytes of `out` are the length prefix; the rest is payload.
    # The command is payload[0] and status payload[1]; content-packet ACKs
    # append index/progress bytes, so the last two bytes are NOT the status.
    payload = out[2:]
    if len(payload) < 2:
        return (None, None)

    return payload[0], payload[1]


# ===========================================================================
# Device error codes  (ErrorCode)
# ===========================================================================

ERROR_SUCCESS = 0x00
ERROR_TRANSMISSION_FAILED = 0x01
ERROR_DEVICE_ABNORMALITY = 0x02
ERROR_DATA_ERROR = 0x03
ERROR_DATA_LENGTH_ERROR = 0x04
ERROR_DATA_ID_ERROR = 0x05
ERROR_DATA_CHECKSUM_ERROR = 0x06
