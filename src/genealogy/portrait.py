"""A portrait is a separately authorized, metadata-free RGB derivative."""

from __future__ import annotations

import struct
import zlib

MAX_BYTES = 8 * 1024 * 1024


def safe_png(data: bytes) -> bool:
    # Accept pre-stripped, noninterlaced RGB PNG. No ancillary chunks, palettes,
    # arbitrary trailing bytes, additional frames or embedded original metadata.
    if len(data) > MAX_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    pixels = 0
    compressed = bytearray()
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 8 + length
        if end + 4 > len(data) or zlib.crc32(data[offset + 4 : end]) != int.from_bytes(
            data[end : end + 4], "big"
        ):
            return False
        chunk = data[offset + 8 : end]
        if offset == 8:
            if kind != b"IHDR" or length != 13 or chunk[8:] != bytes((8, 2, 0, 0, 0)):
                return False
            width, height = struct.unpack(">II", chunk[:8])
            if not (0 < width <= 2048 and 0 < height <= 2048):
                return False
            pixels = (3 * width + 1) * height
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND" and not chunk and end + 4 == len(data):
            try:
                decoder = zlib.decompressobj()
                rows = decoder.decompress(compressed, pixels + 1)
                return (
                    len(rows) == pixels
                    and decoder.eof
                    and not decoder.unused_data
                    and all(v <= 4 for v in rows[:: 3 * width + 1])
                )
            except zlib.error:
                return False
        else:
            return False
        offset = end + 4
    return False
