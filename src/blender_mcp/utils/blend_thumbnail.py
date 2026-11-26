"""Extract the embedded PNG thumbnail from a .blend file without running Blender.

Blender stores a TEST block near the start of every .blend file that contains
a raw RGBA pixel buffer followed immediately by a standard PNG.  The block
layout (after the 12-byte file header) is:

    code[4]   — block type, e.g. b"TEST"
    size[4]   — body length (little- or big-endian, matching file endian)
    old_ptr   — 4 or 8 bytes depending on pointer size
    sdna[4]
    count[4]
    body[size]

Inside the TEST body Blender writes:
    width[4]  height[4]  (little-endian int32)
    <width*height*4 bytes of RGBA pixel data>
    <optional PNG blob>

We skip the pixel buffer and look for the PNG magic (\\x89PNG) to get a
clean, self-contained PNG the browser can display.
"""

from __future__ import annotations

import struct
from pathlib import Path

_BLEND_MAGIC = b"BLENDER"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def extract_thumbnail(blend_path: str | Path) -> bytes | None:
    """Return the embedded PNG thumbnail bytes, or None if not present."""
    path = Path(blend_path)
    try:
        with path.open("rb") as fh:
            header = fh.read(12)
            if not header.startswith(_BLEND_MAGIC):
                return None

            # Byte 7: '_' = little-endian, '-' = big-endian
            # Byte 8: 'v' = 32-bit pointers, 'V' = 64-bit pointers
            endian = "<" if header[7:8] == b"_" else ">"
            ptr_size = 4 if header[8:9] == b"v" else 8

            # Walk blocks looking for TEST
            while True:
                block_header = fh.read(4 + 4 + ptr_size + 4 + 4)
                if len(block_header) < 4 + 4 + ptr_size + 4 + 4:
                    return None
                code = block_header[:4].rstrip(b"\x00")
                (body_size,) = struct.unpack_from(f"{endian}I", block_header, 4)

                if code == b"ENDB":
                    return None

                if code == b"TEST":
                    body = fh.read(body_size)
                    # Find the PNG magic anywhere in the body
                    idx = body.find(_PNG_MAGIC)
                    if idx != -1:
                        return body[idx:]
                    return None

                fh.seek(body_size, 1)
    except (OSError, struct.error):
        return None
