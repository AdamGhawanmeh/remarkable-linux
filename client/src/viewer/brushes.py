"""The pencil and paintbrush textures reMarkable.exe 3.29 ships, and how it picks them.

The app's resources hold four sets of four 64 x 64 greyscale tiles (install.py copies
them to assets/brushes/): `pencil<n>-light.png`, `pencil<n>.png`, `pencil<n>-hard.png`
and `brush<n>.png` (qrc :/images/; a fifth set, `marker<n>.png`, is named but not
shipped). What 3.29 does with them (ARCHITECTURE.md 5.2):

- 0x140b2d940 loads the pencil sets as QImage::Format_Grayscale8 into four tables.
- Per point, a tile is picked by a hash of the point's position (0x140b0f540 and the
  others: `(uint)(x + y + 0.0f)` multiplied by 0xD6E8FEB86659FD93, folded, again, and
  folded: the low two bits) and, for pencils, by pressure: below 0.75 the light set,
  below 0.9 the plain one, else the hard one.
- The Paintbrush (0x140b2cdf0) draws each segment with a QPen whose brush is the
  QBitmap of `brush<hash>.png`, rotated by the point's direction (2 pi * byte / 255),
  when the point's speed is above 20; slower points draw solid.

The tiles are dithered black and white: black is graphite (the light tiles are mostly
paper, the hard ones mostly graphite). Here a pencil run keeps the renderer's
pressure opacity and is filled through its tile at 0.5 + 0.5 x coverage (coverage
1 - grey / 255): the app's grain, without a light stroke vanishing where its tile is
blank (how 3.29's legacy pencil rasteriser, 0x140b08d60, mixes tile and pressure was
not traced). The paintbrush bitmap is used as 3.29 uses it: set pixels take the
colour, the others nothing. Version 2 pencils (13, 14) go
through 3.29's strokev2 FillPencil, which shades with value noise of its own rather than
these tiles: the tiles are the closest of the app's own grain this client has.
"""

from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "brushes"
TEXTURE_SIZE = 64
_MIX = 0xD6E8FEB86659FD93          # -0x2917014799a6026d as an unsigned 64-bit factor
_M64 = (1 << 64) - 1
LIGHT_BELOW, PLAIN_BELOW = 0.75, 0.9    # pressure thresholds (0x14102b6f0, 0x140d2a18c)
BRUSH_MIN_SPEED = 20.0                  # 0x140e8ee70: textured only above this speed


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def texture_variant(x: float, y: float) -> int:
    """Which of a set's four tiles a point at (x, y) (scene coordinates) uses."""
    total = _f32(_f32(_f32(x) + _f32(y)) + 0.0)
    value = int(total) & 0xFFFFFFFF                     # cvttss2si, low 32 bits
    h = (value * _MIX) & _M64
    h = ((((h >> 32) ^ h) & _M64) * _MIX) & _M64
    return (((h >> 32) ^ h) & 0xFFFFFFFF) & 3


def pencil_texture(x: float, y: float, pressure: float) -> str:
    """The pencil tile for a point: its set by pressure, its variant by position."""
    suffix = "-light" if pressure < LIGHT_BELOW else "" if pressure < PLAIN_BELOW else "-hard"
    return f"pencil{texture_variant(x, y)}{suffix}.png"


def brush_texture(x: float, y: float, speed: float) -> str | None:
    """The paintbrush tile for a point, or None where 3.29 draws it solid."""
    return f"brush{texture_variant(x, y)}.png" if speed > BRUSH_MIN_SPEED else None


@lru_cache(maxsize=None)
def _source(name: str):
    """The tile as a Grayscale8 QImage, or None when the asset is missing."""
    from PySide6.QtGui import QImage

    for path in (str(ASSET_DIR / name), f":/images/{name}"):
        image = QImage(path)
        if not image.isNull():
            return image.convertToFormat(QImage.Format.Format_Grayscale8)
    return None


def available(name: str) -> bool:
    return _source(name) is not None


@lru_cache(maxsize=256)
def texture_tile(name: str, rgb: str, alpha: float):
    """The tile `name` painted in `rgb` (#rrggbb) at `alpha`: an ARGB image whose alpha
    is the ink's coverage. Brush tiles go through QBitmap as 3.29 makes them
    (QBitmap::fromImage: set pixels take the colour, the others nothing)."""
    from PySide6.QtGui import QBitmap, QColor, QImage

    source = _source(name)
    if source is None:
        return None
    color = QColor(rgb)
    out = QImage(source.size(), QImage.Format.Format_ARGB32)
    if name.startswith("brush"):
        mask = QBitmap.fromImage(QImage(source)).toImage().convertToFormat(QImage.Format.Format_Mono)
        for y in range(source.height()):
            for x in range(source.width()):
                on = mask.pixelIndex(x, y) == 1
                out.setPixelColor(x, y, QColor(color.red(), color.green(), color.blue(),
                                               round(255 * alpha) if on else 0))
    else:
        for y in range(source.height()):
            for x in range(source.width()):
                coverage = 1.0 - source.pixelColor(x, y).red() / 255.0
                out.setPixelColor(x, y, QColor(color.red(), color.green(), color.blue(),
                                               round(255 * alpha * (0.5 + 0.5 * coverage))))
    return out


@lru_cache(maxsize=None)
def tile_runs(name: str) -> tuple[tuple[tuple[int, int, int], ...], tuple[tuple[int, int, int], ...]] | None:
    """The tile's pixels as horizontal runs (x, y, length): (graphite / set, paper / not
    set). The tiles are pure black and white, so these draw them exactly as vectors."""
    source = _source(name)
    if source is None:
        return None
    on: list[tuple[int, int, int]] = []
    off: list[tuple[int, int, int]] = []
    for y in range(source.height()):
        x = 0
        while x < source.width():
            dark = source.pixelColor(x, y).red() < 128
            start = x
            while x < source.width() and (source.pixelColor(x, y).red() < 128) == dark:
                x += 1
            (on if dark else off).append((start, y, x - start))
    return tuple(on), tuple(off)


def tile_alphas(name: str, alpha: float) -> tuple[float, float]:
    """The alpha of a tile's set and unset pixels as texture_tile paints them."""
    if name.startswith("brush"):
        return alpha, 0.0
    return alpha, alpha * 0.5
