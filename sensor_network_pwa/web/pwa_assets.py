"""Progressive web app assets: theme colour and generated icons."""

import functools
import math
import struct
import zlib
from io import BytesIO
from pathlib import Path

from PIL import Image

PWA_THEME_COLOR = "#003b71"
PWA_ICON_SIZES = (180, 192, 512)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


@functools.cache
def build_pwa_icon_png(size: int, theme_color: str = PWA_THEME_COLOR) -> bytes:
    """Application icon: a sensor dot with two signal rings on the theme colour."""
    bg = bytes(int(theme_color[i : i + 2], 16) for i in (1, 3, 5))
    fg = b"\xff\xff\xff"
    half = size / 2
    # Everything stays inside the central 80% so the icon also works as a maskable one.
    bands = ((0.0, 0.09), (0.17, 0.23), (0.31, 0.37))
    rows = []
    for y in range(size):
        dy = (y + 0.5 - half) / size
        row = bytearray(b"\x00")
        for x in range(size):
            dist = math.hypot((x + 0.5 - half) / size, dy)
            row += fg if any(lo <= dist < hi for lo, hi in bands) else bg
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def resize_icon_png(path: Path, size: int) -> bytes:
    """Fit an uploaded image into a square transparent PNG for PWA icon routes."""
    with Image.open(path) as source:
        image = source.convert("RGBA")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        canvas.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
        stream = BytesIO()
        canvas.save(stream, format="PNG", optimize=True)
        return stream.getvalue()
