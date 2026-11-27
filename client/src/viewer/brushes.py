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
colour, the others nothing. These tiles are the legacy pencils' (tools 1, 7).

Version 2 pencils (13, 14) and the Shader (23) go through 3.29's strokev2 pipelines
(ARCHITECTURE.md 5.2, "strokev2 Shader and v2 pencil"), which shade per pixel; the second
half of this module ports them:

- the Shader: a coverage mask of the segment quads (Qt's anti-aliased fill; 3.29 fills
  32 x 32 tiles of 8-bit coverage), a per-point opacity factor from the speed, and the
  32-bit blenders 0x140b053f0 / 0x140b059a0 (additive for a white tint, else the
  luminance-weighted multiply composite) on integers;
- the v2 pencils: FillPencil 0x140b13610, which sets the stroke colour on the pixels
  whose value noise (permutation table 0x140e8e1d0) is at most the pressure's density.
"""

from __future__ import annotations

import math
import os
import re
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


# ------------------------------------------------------- strokev2 Shader and v2 pencils

# The noise permutation table, 256 bytes stored twice (0x140e8e1d0), so that perm[a + b]
# with a, b < 256 needs no wrap.
_PERM_HEX = ("177da13467754625f765cba97c7e2c7b"
             "98ee912dab72fd0ac088049df91e2348"
             "af3f4d5ab510606f85684ba25d3842f0"
             "083254e531d2adef8d01571202c68f39"
             "e1a03ad9a8cef5ccc706493c14e6d3e9"
             "5ec858094a9b210fdb82e2ca53ec2aac"
             "a5da37de2e6b629a6d43c4b27f9e0df3"
             "414fa6f819e073504433b880e8d0977a"
             "1ad4692bb3d5eb9492590ec31c4e704c"
             "fa2f18fb8c6cbabee4aab78b27bcf4f6"
             "84307790b48a86c152b6787956dcd103"
             "5bf19555cd9671d81f6429a4b1d699e7"
             "2647b9ae61c91d5f075c36febf7622dd"
             "830ba363ea51e3939cb0118e450c6e3e"
             "1bff00c23b74f2fc1315bb35cf814087"
             "3d28a7ed66df6a9fc5bdd78924201605")
PERM = tuple(bytes.fromhex(_PERM_HEX)) * 2          # 512 entries (table at 0x140e8e1d0)

NOISE_SCALE = 1.0       # [I] the rasteriser's S (0x140b19420 param+0x20/0x24) was not read
SHADING_NORMAL_BLEND = "SHADING_NORMAL_BLEND"       # environment variable (any non-empty value)
SHADER_OPACITY_SPAN = (2.0, 10.0)                   # k = speed / 4 below / above which s = 0 / 1
PENCIL_DENSITY = (35.0, 165.0)                      # d = 35 + 165 x pressure / 255


def div255(x: int) -> int:
    """The composite's /255: (x + 0x101 >> 8) + x >> 8 (exact for 0 .. 65025)."""
    return (((x + 0x101) >> 8) + x) >> 8


def shader_opacity(k: float) -> float:
    """The Shader's per-point opacity factor (0x140b334d0) for k = speed / 4 (the parser's
    Point.speed already is the u16 point field x 0.25): s = 0 below 2, 1 above 10, else
    smoothstep((k - 2) / 8); factor = 0.8 + 0.2 (1 - s)."""
    lo, hi = SHADER_OPACITY_SPAN
    t = min(1.0, max(0.0, (k - lo) / (hi - lo)))
    return 0.8 + 0.2 * (1.0 - (3.0 - 2.0 * t) * t * t)


def normal_blend_set() -> bool:
    """SHADING_NORMAL_BLEND: any non-empty value turns the luminance weighting off."""
    return bool(os.environ.get(SHADING_NORMAL_BLEND))


def dither(c: int, x: int, y: int) -> int:
    """The coverage c dithered with the noise table (the 16-bit and flagged variants,
    0x140b051a0 / 0x140b055e0): c &= perm[perm[y & 255] + (x & 255)] | 0xF0. [I] The
    32-bit path the desktop takes is assumed not to (target flag +0x71 off): unused."""
    return c & (PERM[PERM[y & 255] + (x & 255)] | 0xF0)


def luminance_weighted(c: int, a: int, r: int, g: int, b: int) -> int:
    """c x (76 R + 150 G + 29 B) / A_D / 255 of the premultiplied destination: a dark
    destination takes less. A transparent destination (A_D = 0) leaves c as it is."""
    if a == 0:
        return c
    return c * min(255, (76 * r + 150 * g + 29 * b) // a) // 255


def shade_pixel(dst: int, color: tuple[int, int, int, int], c: int, normal_blend: bool | None = None) -> int:
    """One pixel of the 32-bit Shader blenders (0x140b053f0 / 0x140b059a0): `dst` is a
    premultiplied ARGB32 value, `color` the premultiplied (A, R, G, B) of the stroke at
    this point, `c` the 8-bit coverage. Returns the new premultiplied ARGB32 value."""
    if not c:
        return dst
    if normal_blend is None:
        normal_blend = normal_blend_set()
    da, dr, dg, db = dst >> 24, (dst >> 16) & 255, (dst >> 8) & 255, dst & 255
    ca, cr, cg, cb = color
    if cr == cg == cb == ca:                   # a white tint: additive
        return ((min(255, da + ca * c // 255) << 24) | (min(255, dr + cr * c // 255) << 16)
                | (min(255, dg + cg * c // 255) << 8) | min(255, db + cb * c // 255))
    if not normal_blend:
        c = luminance_weighted(c, da, dr, dg, db)
        if not c:
            return dst
    sa = div255(ca * c)
    inv = 255 - sa
    room = 255 - da

    def channel(d: int, s: int) -> int:
        return min(255, div255(d * inv + div255(s * c) * (room + d)))

    out_a = min(255, sa + da - div255(sa * da))
    return (out_a << 24) | (channel(dr, cr) << 16) | (channel(dg, cg) << 8) | channel(db, cb)


def scale_color(color: tuple[int, int, int, int], factor_byte: int) -> tuple[int, int, int, int]:
    """The premultiplied colour with the per-point opacity (factor_byte / 255) multiplied
    into its alpha (and, kept premultiplied, its channels)."""
    return tuple((v * factor_byte + 127) // 255 for v in color)


_RUN = re.compile(rb"[^\x00]+")


def shade_region(image, coverage: bytes, factors: bytes, color: tuple[int, int, int, int],
                 origin: tuple[int, int] = (0, 0), dithered: bool = False,
                 normal_blend: bool | None = None) -> None:
    """Blend the stroke into `image` (QImage, ARGB32_Premultiplied) per pixel. `coverage`
    and `factors` are w x h bytes (row-major) of the region the image holds; `origin` is
    the region's pixel position in the target (the dither's x, y)."""
    w, h = image.width(), image.height()
    if normal_blend is None:
        normal_blend = normal_blend_set()
    pixels = memoryview(image.bits()).cast("B").cast("I")
    stride = image.bytesPerLine() // 4
    ox, oy = origin
    colors: dict[int, tuple[int, int, int, int]] = {}
    for y in range(h):
        row = coverage[y * w:(y + 1) * w]
        frow = factors[y * w:(y + 1) * w]
        base = y * stride
        for run in _RUN.finditer(row):
            for x in range(run.start(), run.end()):
                c = row[x]
                if dithered:
                    c = dither(c, x + ox, y + oy)
                    if not c:
                        continue
                f = frow[x]
                tint = colors.get(f)
                if tint is None:
                    tint = colors[f] = scale_color(color, f)
                pixels[base + x] = shade_pixel(pixels[base + x], tint, c, normal_blend)


def premultiplied(rgba: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """(R, G, B, A8) -> the premultiplied (A, R, G, B)."""
    r, g, b, a = rgba
    return a, (r * a + 127) // 255, (g * a + 127) // 255, (b * a + 127) // 255


def _normal(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(dx, dy)
    return None if length == 0 else (-dy / length, dx / length)


def shader_rect(points, pad: int = 1) -> tuple[int, int, int, int] | None:
    """The integer (x, y, w, h) that holds every quad of (x, y, half_width, ...) points."""
    if not any(_normal(p, q) for p, q in zip(points, points[1:])):
        return None
    x0 = math.floor(min(p[0] - p[2] for p in points)) - pad
    y0 = math.floor(min(p[1] - p[2] for p in points)) - pad
    x1 = math.ceil(max(p[0] + p[2] for p in points)) + pad
    y1 = math.ceil(max(p[1] + p[2] for p in points)) + pad
    return x0, y0, x1 - x0, y1 - y0


def _gray_bytes(image) -> bytes:
    w, h, step = image.width(), image.height(), image.bytesPerLine()
    data = bytes(image.constBits())
    if step == w:
        return data[:w * h]
    return b"".join(data[y * step:y * step + w] for y in range(h))


def shader_masks(points, rect: tuple[int, int, int, int]) -> tuple[bytes, bytes]:
    """The 8-bit coverage of the stroke's segment quads (Qt's anti-aliased fill; each
    quad is the segment +- half-width along its normal, 0x140b326a0, with a bevel at every
    joint [I: the join shape, 0x140b30590, was not read]) and the per-point opacity
    factor (a byte, 255 = 1.0) interpolated along each segment, over `rect` (pixels of the
    space `points` = (x, y, half_width, factor) are in)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPolygonF

    rx, ry, rw, rh = rect
    coverage = QImage(rw, rh, QImage.Format.Format_Grayscale8)
    coverage.fill(0)
    factors = QImage(rw, rh, QImage.Format.Format_Grayscale8)
    factors.fill(255)
    cp, fp = QPainter(coverage), QPainter(factors)
    try:
        for painter in (cp, fp):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.translate(-rx, -ry)
        cp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cp.setBrush(QColor(255, 255, 255))
        fp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)

        def gray(f: float) -> QColor:
            g = round(255 * f)
            return QColor(g, g, g)

        def draw(corners, brush) -> None:
            polygon = QPolygonF([QPointF(*c) for c in corners])
            cp.drawPolygon(polygon)
            fp.setBrush(brush)
            fp.drawPolygon(polygon)

        previous = None
        for p, q in zip(points, points[1:]):
            n = _normal(p, q)
            if n is None:
                continue
            nx, ny = n
            gradient = QLinearGradient(QPointF(p[0], p[1]), QPointF(q[0], q[1]))
            gradient.setColorAt(0.0, gray(p[3]))
            gradient.setColorAt(1.0, gray(q[3]))
            draw([(p[0] + nx * p[2], p[1] + ny * p[2]), (q[0] + nx * q[2], q[1] + ny * q[2]),
                  (q[0] - nx * q[2], q[1] - ny * q[2]), (p[0] - nx * p[2], p[1] - ny * p[2])],
                 QBrush(gradient))
            if previous is not None:             # the bevel on both sides of the joint
                (mx, my), pn = previous
                for sign in (1, -1):
                    draw([(p[0], p[1]), (p[0] + sign * mx * p[2], p[1] + sign * my * p[2]),
                          (p[0] + sign * nx * p[2], p[1] + sign * ny * p[2])], QBrush(gray(p[3])))
            previous = (n, p)
    finally:
        cp.end()
        fp.end()
    return _gray_bytes(coverage), _gray_bytes(factors)


def shader_points(points) -> list[tuple[float, float, float, float]]:
    """(x, y, half_width, k = speed / 4) -> (x, y, half_width, opacity factor)."""
    return [(x, y, half, shader_opacity(k)) for x, y, half, k in points]


def shader_image(points, color: tuple[int, int, int, int], normal_blend: bool | None = None):
    """The Shader stroke blended into a transparent image (QImage ARGB32_Premultiplied),
    as (image, x, y): the multiply composite over nothing is the colour at the coverage,
    so it carries the stroke into any painter or SVG that multiplies it. `points` are
    (x, y, half_width, opacity factor); `color` the premultiplied (A, R, G, B)."""
    from PySide6.QtGui import QImage

    rect = shader_rect(points)
    if rect is None:
        return None
    coverage, factors = shader_masks(points, rect)
    image = QImage(rect[2], rect[3], QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    shade_region(image, coverage, factors, color, normal_blend=normal_blend)
    return image, rect[0], rect[1]


def shade_into(image, points, color: tuple[int, int, int, int], dithered: bool = False,
               normal_blend: bool | None = None, origin: tuple[int, int] = (0, 0)):
    """Blend a Shader stroke into the pixels of `image` (ARGB32_Premultiplied, `points` in
    its pixels) over the stroke's bounds only; returns the (x, y, w, h) it touched, or
    None. `origin` is where the image sits in the real target (the dither's x, y)."""
    from PySide6.QtCore import QRect

    rect = shader_rect(points)
    if rect is None:
        return None
    bounds = QRect(*rect).intersected(image.rect())
    if bounds.isEmpty():
        return None
    rect = (bounds.x(), bounds.y(), bounds.width(), bounds.height())
    coverage, factors = shader_masks(points, rect)
    # shade_region takes the whole image, so a clipped rect shades the image's rows: work
    # on the sub-image when the stroke does not cover all of it.
    if rect[:2] == (0, 0) and rect[2:] == (image.width(), image.height()):
        shade_region(image, coverage, factors, color, origin=origin, dithered=dithered, normal_blend=normal_blend)
        return rect
    part = image.copy(bounds)
    shade_region(part, coverage, factors, color, origin=(origin[0] + rect[0], origin[1] + rect[1]),
                 dithered=dithered, normal_blend=normal_blend)
    from PySide6.QtGui import QPainter

    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    painter.drawImage(bounds.topLeft(), part)
    painter.end()
    return rect


# ----- v2 pencils (FillPencil, 0x140b13610)

def value_noise(a: float, b: float) -> int:
    """N(a, b): value noise, bilinear over perm[perm[b & 255] + (a & 255)] at the four
    lattice corners with 8-bit weights (255 - w) / w for the fractions."""
    ia, ib = math.floor(a), math.floor(b)
    wx, wy = int((a - ia) * 256), int((b - ib) * 256)

    def corner(i: int, j: int) -> int:
        return PERM[PERM[j & 255] + (i & 255)]

    v00 = corner(ia, ib)
    if wx == 0 and wy == 0:
        return v00
    top = (v00 * (255 - wx) + corner(ia + 1, ib) * wx) // 255
    bottom = (corner(ia, ib + 1) * (255 - wx) + corner(ia + 1, ib + 1) * wx) // 255
    return (top * (255 - wy) + bottom * wy) // 255


def pencil_noise(u: float, v: float, x: float, y: float) -> int:
    """n = N(u, v) >> 3 + ((N(7x, 7y) + 3 N(3x, 3y) + 2 N(x, y)) / 6 x 7 >> 3), with (u, v)
    the stroke attributes (arc length, signed distance) and (x, y) the pixel relative to
    the rasteriser's origin, both scaled by 1 / S (NOISE_SCALE)."""
    x, y = x / NOISE_SCALE, y / NOISE_SCALE
    u, v = u / NOISE_SCALE, v / NOISE_SCALE
    fine = (value_noise(7 * x, 7 * y) + 3 * value_noise(3 * x, 3 * y) + 2 * value_noise(x, y)) // 6
    return (value_noise(u, v) >> 3) + ((fine * 7) >> 3)


def pencil_density(d: float, h: float, v: float) -> int:
    """d': d for a stroke whose half-width h is at most 2; else d x 1 for |v| / h < 0.5,
    x (1 - smoothstep(2 (|v| / h - 0.5))) up to 1, and 0 beyond."""
    if h <= 2:
        return int(d)
    r = abs(v) / h
    if r < 0.5:
        return int(d)
    if r >= 1:
        return 0
    t = 2 * (r - 0.5)
    return int(d * (1.0 - (3.0 - 2.0 * t) * t * t))


def pencil_hit(u: float, v: float, x: float, y: float, d: float, h: float) -> bool:
    """FillPencil's per-pixel test: the pixel takes the stroke colour when n <= d'."""
    return pencil_noise(u, v, x, y) <= pencil_density(d, h, v)


def pencil_image(points, color: tuple[int, int, int, int], origin: tuple[int, int] | None = None):
    """A v2 pencil stroke as (image, x, y): a transparent ARGB32_Premultiplied image of
    the stroke's bounds in which the pixels FillPencil sets hold `color` ((R, G, B, A8),
    not anti-aliased). `points` are (x, y, half_width, pressure 0..1) in page pixels.
    Each segment is a quad whose pixels take u = arc length, v = signed distance from the
    centre line, d = 35 + 165 x pressure and h = half-width, interpolated along it
    [I] (butt ends; the interior joints are covered by extending each segment by the
    half-width). `origin` is the rasteriser origin the noise is relative to: [I] the
    stroke's first point, rounded (0x140b19420 param+0x28 was not read)."""
    from PySide6.QtGui import QImage

    if not points:
        return None
    if origin is None:
        origin = (math.floor(points[0][0] + 0.5), math.floor(points[0][1] + 0.5))
    rect = shader_rect(points, pad=1)
    if rect is None:
        # A dot (one point, or all coincident): [I] a segment as long as it is wide.
        x, y, half, pressure = points[0]
        if half <= 0:
            return None
        points = [(x - half, y, half, pressure), (x + half, y, half, pressure)]
        rect = shader_rect(points, pad=1)
    x0, y0, w, h = rect
    ox, oy = origin
    pixel = (lambda r, g, b, a: (a << 24) | (r << 16) | (g << 8) | b)(*premultiplied(color)[1:], color[3])
    buffer = bytearray(w * h * 4)
    pixels = memoryview(buffer).cast("I")
    d0, d1 = PENCIL_DENSITY
    arc = 0.0
    last = len(points) - 2
    for i, (p, q) in enumerate(zip(points, points[1:])):
        dx, dy = q[0] - p[0], q[1] - p[1]
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        tx, ty = dx / length, dy / length
        nx, ny = -ty, tx
        before = p[2] if i > 0 else 0.0           # interior joints: extend by the half-width
        after = q[2] if i < last else 0.0
        pad = 2 * max(p[2], q[2])
        for py in range(max(0, math.floor(min(p[1], q[1]) - pad) - y0),
                        min(h, math.ceil(max(p[1], q[1]) + pad) - y0)):
            for px in range(max(0, math.floor(min(p[0], q[0]) - pad) - x0),
                            min(w, math.ceil(max(p[0], q[0]) + pad) - x0)):
                if pixels[py * w + px]:
                    continue
                ex, ey = x0 + px + 0.5 - p[0], y0 + py + 0.5 - p[1]
                s = ex * tx + ey * ty
                if s < -before or s > length + after:
                    continue
                v = ex * nx + ey * ny
                t = min(1.0, max(0.0, s / length))
                half = p[2] + (q[2] - p[2]) * t
                if abs(v) > half:
                    continue
                density = d0 + d1 * (p[3] + (q[3] - p[3]) * t)
                if pencil_hit(arc + s, v, x0 + px - ox, y0 + py - oy, density, half):
                    pixels[py * w + px] = pixel
        arc += length
    data = bytes(buffer)
    image = QImage(data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    return image, x0, y0
