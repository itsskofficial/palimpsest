"""Draw the app icon: fresh writing over two faded layers.

The mark is the product's own metaphor and the same one the loading state uses — strata.
A solid vellum bar sits over two progressively fainter ones, which is what a palimpsest
*is*: the new hand written across what was there before, the old still showing through.

Rendered here rather than committed as a binary blob nobody can edit, and written with
only the standard library so it stays runnable in the offline CI job. Supersampled 4x and
box-filtered down, which is the whole of the antialiasing.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 512
SS = 4  # supersample factor
W = SIZE * SS

# The palette, straight from the UI's tokens.
GROUND_TOP = (122, 91, 65)
GROUND_BOTTOM = (86, 63, 45)
VELLUM = (247, 244, 238)

CORNER = 96 * SS

# (top, height, left, width, alpha) — newest first, fading downward.
BARS = [
    (150, 58, 84, 344, 1.00),
    (238, 58, 84, 268, 0.52),
    (326, 58, 84, 312, 0.30),
]
BAR_RADIUS = 29


def _rounded(x: float, y: float, left: float, top: float, w: float, h: float, r: float) -> bool:
    """Is (x, y) inside a rounded rectangle? Corner circles, straight edges elsewhere."""
    if not (left <= x <= left + w and top <= y <= top + h):
        return False
    cx = min(max(x, left + r), left + w - r)
    cy = min(max(y, top + r), top + h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render() -> bytes:
    """The supersampled canvas as flat RGB bytes."""
    bars = [
        (t * SS, h * SS, left * SS, w * SS, a)
        for t, h, left, w, a in BARS
    ]
    radius = BAR_RADIUS * SS
    rows = bytearray()
    for y in range(W):
        # The ground's vertical gradient, computed once per row.
        t = y / (W - 1)
        ground = tuple(
            round(GROUND_TOP[i] + (GROUND_BOTTOM[i] - GROUND_TOP[i]) * t) for i in range(3)
        )
        row = bytearray()
        for x in range(W):
            if not _rounded(x, y, 0, 0, W - 1, W - 1, CORNER):
                row += b"\x00\x00\x00"  # outside the tile: transparent-looking black
                continue
            pixel = ground
            for top, height, left, width, alpha in bars:
                if _rounded(x, y, left, top, width, height, radius):
                    pixel = tuple(
                        round(pixel[i] + (VELLUM[i] - pixel[i]) * alpha) for i in range(3)
                    )
                    break
            row += bytes(pixel)
        rows += row
    return bytes(rows)


def downsample(big: bytes) -> bytes:
    """Box-filter SSxSS blocks down to one pixel, with an alpha from the tile mask."""
    out = bytearray()
    for y in range(SIZE):
        out.append(0)  # PNG filter type 0 for this scanline
        for x in range(SIZE):
            r = g = b = 0
            covered = 0
            for dy in range(SS):
                base = ((y * SS + dy) * W + x * SS) * 3
                for dx in range(SS):
                    i = base + dx * 3
                    px = (big[i], big[i + 1], big[i + 2])
                    if px != (0, 0, 0):
                        covered += 1
                        r += px[0]
                        g += px[1]
                        b += px[2]
            n = SS * SS
            if covered == 0:
                out += b"\x00\x00\x00\x00"
            else:
                out += bytes(
                    (r // covered, g // covered, b // covered, round(255 * covered / n))
                )
    return bytes(out)


def png(raw: bytes, path: Path) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    target = Path(__file__).with_name("icon.png")
    png(downsample(render()), target)
    print(f"wrote {target} ({target.stat().st_size} bytes, {SIZE}x{SIZE})")
