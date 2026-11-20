"""Pen strokes and the eraser on .rm v6 pages, made as reMarkable.exe 3.29 makes them.

A stroke is one CrdtLineItem block (0x05, always min_version 2, version 2): the item
header every sequence item has (rm_writer.ItemHeader: parent node, id, left and right
neighbours, deletedLength) and, while it is live, a value:

    u8 3
    1  tool            u32   Line::Tool
    2  color           u32   Line::Color
    3  thicknessScale  f64   the pen's size setting (1 thin, 2 medium, 3 thick)
    4  startingLength  f32   0, or for a piece an eraser left: where along the
                             original stroke it starts
    5  points          n x 14 bytes: f32 x, f32 y, u16 speed*4, u16 width*4,
                       u8 direction*255/2pi, u8 pressure*255 (x 0 = centre of the page)
    6  timestamp       id    0:1 unless the stroke was transformed
    7  moveId          id    only when not 0:0 (the item it replaced)
    8  argb            u32   only when color is 9 (ArgbCode)
    9  sourceId        id    only when not 0:0 (shader pieces: the stroke they come from)
    then whatever the app did not know, kept as it is

(writer 0x140a24bf0, reader 0x140a20dd0, point packing 0x14045dae0). LineValue holds all
of it and gives back the bytes it was read from.

Drawing. The desktop app's own QML has no pen, but the pen code the tablet uses is in
the binary, mouse path included, and this follows it:

    PenInputSurface (0x140ad8e90, 0x140ad8d80, 0x140ad9060): a mouse gives samples
        (x, y, pressure 0.7, no tilt); the press and the release give one each, every
        move one, and one more every 25 ms the pressed mouse stays where it is.
    PenInputLineHandler::addPoint (0x140ac9360) -> 0x140b40df0 / 0x140b41680: each
        sample becomes one stored point, its width, speed, direction and pressure worked
        out by the tool (StrokeBuilder).
    SceneController (0x1404473a0 -> 0x140b3f850 -> 0x140b3ede0): the finished line is
        thinned out (simplify) and only then added to the page.
    addLine (0x1409578c0) -> layer append (0x1408c2bc0): the item gets the page's next
        id and goes on top of the layer: left = the layer's last item, right = 0:0
        (append_stroke).

Erasing. Deleting an item (0x1408ce450) leaves its block where it is, without its
value and with deletedLength 1, then joins it to a deleted neighbour when both are one
unbroken run of ids (delete_strokes, can_join). The eraser (0x140994f30) works on the outline of
its own stroke: what of a stroke lies inside is cut out, the stroke is deleted, and the
pieces left over are put right after it as new strokes (erase).

Arithmetic is done in single precision, operation by operation, as the binary does it,
so that the stored bytes come out the same.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Iterable, NamedTuple

from . import rm_writer as w
from .rm_parser import (TAG_BYTE4, TAG_BYTE8, TAG_ID, TAG_LENGTH4, RmParseError, _Values)

ZERO_ID = w.ZERO_ID
DEFAULT_TIMESTAMP = (0, 1)          # what a new SceneLineItem has (0x1405b59d0)
POINT = struct.Struct("<ffHHBB")
MAX_STORED_POINTS = 30000           # the writer stores no more (0x140a24bf0)
MAX_LINE_POINTS = 29990             # a line must stay below this to be added (0x1404473a0)

# Line::Tool (moc metadata of reMarkable.exe)
PAINTBRUSH, PENCIL, BALLPOINT, MARKER, FINELINER, HIGHLIGHTER, ERASER, SHARP_PENCIL = range(8)
ERASE_SECTION, CLEAR_PAGE, ZOOM_TOOL, SELECTION_TOOL, PAINTBRUSH_V2, SHARP_PENCIL_V2 = range(8, 14)
PENCIL_V2, BALLPOINT_V2, MARKER_V2, FINELINER_V2, HIGHLIGHTER_V2, SOLID_PEN = range(14, 20)
RESERVED_PEN, CALLIGRAPHY, MASKED_ERASER, SHADING_MARKER = range(20, 24)
COLOR_ARGB = 9
DEFAULT_ARGB = 0xFF000000

# What a layer does not take as a stroke: addLine refuses the erasers, the selection
# tool and ClearPage (0x1409578c0), the layer the rest (0x1408c2bc0; 0x140b3e440).
NOT_DRAWN = frozenset({ERASER, ERASE_SECTION, CLEAR_PAGE, ZOOM_TOOL, SELECTION_TOOL, MASKED_ERASER})
# The tools the toolbar offers: Ballpoint pen, Fineliner, Marker, Pencil, Mechanical
# pencil, Paintbrush, Highlighter, Calligraphy pen, Shader.
PENS = (BALLPOINT_V2, FINELINER_V2, MARKER_V2, PENCIL_V2, SHARP_PENCIL_V2, PAINTBRUSH_V2, HIGHLIGHTER_V2,
        CALLIGRAPHY, SHADING_MARKER)
THIN, MEDIUM, THICK = 1.0, 2.0, 3.0     # Last<Tool>Size in a document's .content
MOUSE_PRESSURE = 0.7                    # 0x3f333333, what a mouse sample carries


class StrokeError(ValueError):
    """A stroke or an erasure the app would not make, or this code will not yet."""


# ------------------------------------------------------------------ single precision

_F32 = struct.Struct("<f")
_INF = float("inf")


def f32(value: float) -> float:
    """`value` rounded to single precision."""
    try:
        return _F32.unpack(_F32.pack(value))[0]
    except OverflowError:
        return math.copysign(_INF, value)


def _roundf(value: float) -> int:
    """C roundf, then the truncation to an integer that follows it everywhere."""
    if value != value or abs(value) == _INF:
        return 0
    return int(math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5))


def _sqrt(value: float) -> float:
    return f32(math.sqrt(value)) if value >= 0 else float("nan")


def _pow(base: float, exponent: float) -> float:
    try:
        return f32(math.pow(base, exponent))
    except (ValueError, OverflowError):
        return float("nan")


_PI = f32(math.pi)
_TWO_PI = f32(2 * math.pi)
_255 = 255.0


def _angle(dy: float, dx: float) -> float:
    """atan2f brought into 0..2 pi (0x140b41f10)."""
    a = f32(math.atan2(dy, dx))
    if a < 0:
        return f32(f32(math.fmod(a, _TWO_PI)) + _TWO_PI)
    if a > _TWO_PI:
        return f32(math.fmod(a, _TWO_PI))
    return a


# ------------------------------------------------------------------ points

class Point(NamedTuple):
    """A stored point: where it is, and the four numbers as the file holds them."""

    x: float
    y: float
    speed: int          # u16: speed * 4
    width: int          # u16: width * 4
    direction: int      # u8: radians * 255 / 2 pi
    pressure: int       # u8: 0..1 * 255


def unpack_points(data: bytes) -> list[Point]:
    if len(data) % POINT.size:
        raise RmParseError(f"point data length {len(data)} is not a multiple of {POINT.size}")
    return [Point(*p) for p in POINT.iter_unpack(data)]


def pack_points(points: Iterable[Point]) -> bytes:
    return b"".join(POINT.pack(p.x, p.y, p.speed & 0xFFFF, p.width & 0xFFFF, p.direction & 0xFF, p.pressure & 0xFF)
                    for p in points)


def make_point(x: float, y: float, speed: float, width: float, direction: float, pressure: float) -> Point:
    """A point from its values in their own units (speed, width in scene units,
    direction in radians, pressure 0..1), rounded as the app rounds them (0x14045dae0)."""
    return Point(f32(x), f32(y), _roundf(f32(f32(speed) * 4.0)) & 0xFFFF, _roundf(f32(f32(width) * 4.0)) & 0xFFFF,
                 _roundf(f32(f32(f32(direction) * _255) / _TWO_PI)) & 0xFF, _roundf(f32(f32(pressure) * _255)) & 0xFF)


# ------------------------------------------------------------------ the value of a line item

@dataclass(frozen=True)
class LineValue:
    """The value of a CrdtLineItem, whole: encode() of a decoded value is the bytes it
    was read from."""

    tool: int
    color: int
    thickness_scale: float
    starting_length: float
    points: bytes                               # 14 bytes each
    timestamp: tuple[int, int] | None = DEFAULT_TIMESTAMP
    move_id: tuple[int, int] | None = None
    argb: int | None = None
    source_id: tuple[int, int] | None = None
    tail: bytes = b""                           # what follows the last field the app knows

    @property
    def count(self) -> int:
        return len(self.points) // POINT.size

    def unpacked(self) -> list[Point]:
        return unpack_points(self.points)

    def encode(self) -> bytes:
        out = (b"\x03" + w.f_u32(1, self.tool) + w.f_u32(2, self.color) + w.f_f64(3, self.thickness_scale)
               + w.f_f32(4, self.starting_length) + w.f_sub(5, self.points))
        if self.timestamp is not None:
            out += w.f_id(6, self.timestamp)
        if self.move_id is not None:
            out += w.f_id(7, self.move_id)
        if self.argb is not None:
            out += w.f_u32(8, self.argb)
        if self.source_id is not None:
            out += w.f_id(9, self.source_id)
        return out + self.tail


def decode_line_value(value: bytes) -> LineValue:
    """The value sub-block of a line item of a version 2 block, read as the app reads
    it (0x140a20dd0): fields 1 to 5, then 6, 7, 8 and 9 where they are there, the rest
    kept. RmParseError when it does not come out as the same bytes again."""
    if not value or value[0] != 3:
        raise RmParseError("not the value of a line item")
    r = _Values(value[1:], "line")
    tool, color = r.need(1, TAG_BYTE4), r.need(2, TAG_BYTE4)
    thickness = r.need(3, TAG_BYTE8)
    starting = struct.unpack("<f", struct.pack("<I", r.need(4, TAG_BYTE4)))[0]
    points = r.need(5, TAG_LENGTH4)
    line = LineValue(tool, color, thickness, starting, points, r.get(6, TAG_ID), r.get(7, TAG_ID),
                     r.get(8, TAG_BYTE4), r.get(9, TAG_ID), r.rest())
    if len(points) % POINT.size or line.encode() != value:
        raise RmParseError("the line item's value is not understood")
    return line


def new_line(tool: int, color: int, thickness_scale: float, points: Iterable[Point] | bytes, *,
             argb: int | None = None, starting_length: float = 0.0) -> LineValue:
    """The value a newly drawn line gets: timestamp 0:1, no moveId, no sourceId; its
    ARGB colour is stored only with the colour ArgbCode (opaque black if none is given)."""
    data = points if isinstance(points, bytes) else pack_points(points)
    return LineValue(int(tool), int(color), float(thickness_scale), f32(starting_length), data, DEFAULT_TIMESTAMP,
                     None, (DEFAULT_ARGB if argb is None else argb & 0xFFFFFFFF) if color == COLOR_ARGB else None)


# ------------------------------------------------------------------ from pen samples to points

_V2_SYNTH = frozenset({SHARP_PENCIL_V2, PENCIL_V2, BALLPOINT_V2, MARKER_V2, SOLID_PEN, RESERVED_PEN, CALLIGRAPHY,
                       SHADING_MARKER})                         # mask 0xb9e000 (0x140b40e40)
_SPEED_FADES_PRESSURE = frozenset({PAINTBRUSH, PENCIL, MARKER, PAINTBRUSH_V2, BALLPOINT_V2, MARKER_V2,
                                   SHADING_MARKER})             # 0x140b3ff40
_SPEED_FACTOR = 1.0                                             # 0x14121d6b0


def rotation_of(m11: float, m12: float, m21: float, m22: float) -> float | None:
    """The rotation (degrees) PenInputLineHandler::setTransform (0x140aca100) reads off
    the transform it is given, for the calligraphy nib: a matrix with no shear part
    (m12 = m21 = 0) is 0 when both scales are positive, else 180; one with no scale
    part (m11 = m22 = 0) is 90 when m12 > 0 and m21 < 0, else 270; anything else is
    "unknown rotation" (None: the handler keeps the rotation it had)."""
    if m12 == 0 and m21 == 0:
        return 0.0 if m11 > 0 and m22 > 0 else 180.0
    if m11 == 0 and m22 == 0:
        return 90.0 if m12 > 0 and m21 < 0 else 270.0
    return None


class StrokeBuilder:
    """A line being drawn: every pen sample added becomes one point, as
    PenInputLineHandler makes it (state 0x140b40c30, addPoint 0x140b40df0).

    `thickness` is the pen's size (THIN, MEDIUM, THICK) and becomes the line's
    thicknessScale; `scale` is how many screen pixels a scene unit covers (the handler
    takes it from its transform, 0x140b0d080), which only speed depends on;
    `right_handed` and `rotation` (degrees) turn the calligraphy nib."""

    def __init__(self, tool: int, color: int = 0, thickness: float = MEDIUM, argb: int | None = None, *,
                 scale: float = 1.0, right_handed: bool = True, rotation: float = 0.0) -> None:
        if tool in (PENCIL, BALLPOINT, MARKER) or not 0 <= tool <= SHADING_MARKER or tool == CLEAR_PAGE:
            raise StrokeError(f"tool {tool} makes no points here")
        self.tool, self.color, self.argb = int(tool), int(color), argb
        self.thickness = float(thickness)
        self.scale, self.right_handed, self.rotation = f32(scale), bool(right_handed), f32(rotation)
        self.points: list[Point] = []
        self._last: tuple | None = None         # the sample before: x, y, pressure, tilt x, tilt y
        self._before: tuple | None = None       # and the one before that
        self._v2_brush = tool in (PAINTBRUSH_V2, PENCIL_V2)     # 0x140b41f50

    def add(self, x: float, y: float, pressure: float = MOUSE_PRESSURE, tilt_x: float = 0.0, tilt_y: float = 0.0) -> None:
        sample = (f32(x), f32(y), f32(pressure), f32(tilt_x), f32(tilt_y))
        if self._last is None:
            self._last = self._before = sample
        point = self._point_v2(sample) if self.tool in _V2_SYNTH else self._point(sample)
        if point is not None:
            self._append(point)
        self._before, self._last = self._last, sample

    def _append(self, point: Point) -> None:
        """0x140b3cf40: a stroke whose first point came out with no width takes the
        third point's numbers for its first two."""
        self.points.append(point)
        if len(self.points) > 2 and self.points[0].width == 0:
            third = self.points[2]
            for n in (0, 1):
                self.points[n] = third._replace(x=self.points[n].x, y=self.points[n].y)

    def _thinned(self) -> float:
        """The pen's size as ballpoint and mechanical pencil use it."""
        t = f32(self.thickness)
        return f32(t / f32(1.35)) if t > 1.0 else t

    def _store(self, sample: tuple, speed: float, width: float, direction: float, pressure: float) -> Point:
        return Point(sample[0], sample[1], _roundf(f32(speed * 4.0)) & 0xFFFF, _roundf(f32(width * 4.0)) & 0xFFFF,
                     _roundf(f32(f32(direction * _255) / _TWO_PI)) & 0xFF, _roundf(f32(pressure * _255)) & 0xFF)

    def _point_v2(self, sample: tuple) -> Point:
        """0x140b41680: speed and direction from the sample before, width by the tool."""
        x, y, p, tilt_x, tilt_y = sample
        tan_x, tan_y = f32(math.tan(tilt_x)), f32(math.tan(tilt_y))
        tilt = f32(math.atan(_sqrt(f32(f32(tan_y * tan_y) + f32(tan_x * tan_x)))))
        dx, dy = f32(x - self._last[0]), f32(y - self._last[1])
        speed = f32(f32(_sqrt(f32(f32(dy * dy) + f32(dx * dx))) * self.scale) * _SPEED_FACTOR)
        direction = _angle(dy, dx)
        lean = 1.0 if 1.0 < tilt else tilt
        size = f32(self.thickness)
        tool = self.tool
        if tool == BALLPOINT_V2:
            width = f32(max(2.0, f32(p * 3.0)) * self._thinned())
        elif tool == SHARP_PENCIL_V2:
            width = f32(self._thinned() * 4.0)
            if p < 0:
                p = 0.0
            elif p > 1.0:
                p = 1.0
            else:
                p = f32(f32(3.0 - f32(p + p)) * f32(p * p))
            p = f32(f32(p * f32(0.3)) + f32(0.7))
        elif tool == PENCIL_V2:
            low, high, span = f32(0.5235988), f32(0.9599311), f32(0.4363323)       # 30, 55, 25 degrees
            a = 0.0 if low > lean else (1.0 if lean > high else f32(f32(lean - low) / span))
            a = _pow(a, 4.0 if self._v2_brush else 2.0)
            if self.points:
                p = f32(f32(f32(self.points[-1].pressure / _255) * f32(0.8)) + f32(p * f32(0.19999999)))
            width = f32(f32(f32(a * 15.0) + f32(p + 2.0)) * size)
        elif tool == MARKER_V2:
            width = f32(f32(f32(f32(f32(lean * lean) * f32(f32(p * 0.5) + 0.5)) * 10.0) + 2.5) * size)
        elif tool in (SOLID_PEN, RESERVED_PEN):
            width = f32(size + size)
        elif tool == CALLIGRAPHY:
            nib = f32(f32(f32((40.0 if self.right_handed else 170.0) + self.rotation) * _PI) / 180.0)
            lean_to = f32(math.atan2(f32(math.sin(tilt_y)), f32(math.sin(tilt_x))))
            if lean_to < 0:
                lean_to = f32(lean_to + _TWO_PI)
            off = f32(direction - lean_to)
            if off < 0:
                off = f32(off + _TWO_PI)
            off = f32(off - f32(f32(1.5707964) - nib))
            if off < 0:
                off = f32(off + _TWO_PI)
            if off > _PI:
                off = f32(off + f32(-6.2831855))
            third = f32(1.0471976)
            broad = max(f32(f32(third - abs(off)) / third), 0.0)
            width = max(f32(f32(f32(f32(broad * p) * 4.5) + 1.0) * size), 1.5)
            if self.points:
                width = f32(f32(f32(self.points[-1].width * 0.25) * f32(1.0 - 0.25)) + f32(0.25 * width))
        else:                                   # SHADING_MARKER
            low = f32(0.1)
            t = 0.0 if low > p else (1.0 if p > f32(0.3) else f32(f32(p - low) / f32(0.20000002)))
            width = f32(_reach(tool, self.thickness) * f32(f32(f32(1.0 - t) * f32(0.33333334)) + t))
            if self.points:
                speed = f32(f32(f32(self.points[-1].speed * 0.25) * f32(0.9)) + f32(speed * low))
        return self._store(sample, speed, width, direction, p)

    def _point(self, sample: tuple) -> Point | None:
        """0x140b40df0 itself: pressure, speed and direction are averages over this
        sample and the one before; fineliner, highlighter, paintbrush and the erasers."""
        x, y, p, tilt_x, tilt_y = sample
        last, before = self._last, self._before
        p = f32(f32(p + last[2]) * 0.5)
        if p > 1.0:
            p = 1.0
        size = f32(self.thickness)
        dy, dx = f32(y - last[1]), f32(x - last[0])
        was_dy, was_dx = f32(last[1] - before[1]), f32(last[0] - before[0])
        direction = f32(_angle(dy, dx) + _angle(was_dy, was_dx))
        gone = _sqrt(f32(f32(was_dy * was_dy) + f32(was_dx * was_dx)))
        going = _sqrt(f32(f32(dy * dy) + f32(dx * dx)))
        speed = f32(f32(f32(f32(gone + going) * self.scale) * 0.5) * _SPEED_FACTOR)
        tool = self.tool
        if tool in _SPEED_FADES_PRESSURE:
            fade = f32(1.0 - f32(f32(speed / self.scale) / 100.0))
            p = f32(p * (0.5 if 0.5 > fade else (1.0 if fade > 1.0 else fade)))
        whole = False                           # the old pens count their width in whole units
        if tool in (PAINTBRUSH, PAINTBRUSH_V2):
            p = max(f32(0.25), p)
            by_pressure, by_tilt = (4.5, 4.5) if self._v2_brush else (10.0, 5.0)
            lean = _sqrt(f32(_pow(tilt_x, 2.0) + _pow(tilt_y, 2.0)))
            base = max(2.0, f32(f32(_pow(p, 4.5) * by_pressure) + f32(_pow(lean, 4.0) * by_tilt)))
        elif tool in (FINELINER, FINELINER_V2):
            base, whole = 2.0, tool == FINELINER
        elif tool in (HIGHLIGHTER, HIGHLIGHTER_V2):
            base = 30.0
        elif tool in (ERASER, MASKED_ERASER):
            base = 10.0
        elif tool == SHARP_PENCIL:
            size, base, whole = self._thinned(), 3.0, True
        elif tool in (ERASE_SECTION, ZOOM_TOOL, SELECTION_TOOL):
            size = base = 2.0
        else:
            return None
        if whole:
            base = float(int(base))
        return self._store(sample, speed, max(f32(base * size), 2.0), f32(direction * 0.5), p)

    def line(self) -> LineValue:
        """The line as drawn, before it is thinned out."""
        return new_line(self.tool, self.color, self.thickness, self.points, argb=self.argb)


# ------------------------------------------------------------------ thinning a finished line

_SIMPLIFY_SPEED = _SPEED_FADES_PRESSURE                                           # 0x140b3ede0, first switch
_SIMPLIFY_PRESSURE = frozenset({PAINTBRUSH, PENCIL, BALLPOINT, MARKER, PAINTBRUSH_V2, SHARP_PENCIL_V2, PENCIL_V2,
                                BALLPOINT_V2, MARKER_V2, CALLIGRAPHY, SHADING_MARKER})   # table 0x140b3f828


def _fuzzy_equal(a: float, b: float) -> bool:
    return f32(abs(f32(a - b)) * 100000.0) <= min(abs(a), abs(b))


def simplify_points(tool: int, points: list[Point], distance: float = 0.2, pressure: float | None = 0.1,
                    width: float | None = 2 / 3, speed: float | None = 2.0) -> list[Point]:
    """`points` without those the line does not need (0x140b3ede0): between two kept
    points the one farthest from the straight line through them is kept as well when
    it lies more than `distance` off it, or when some point between them differs from
    the first of the two by more than `width` in width (or `pressure` in pressure, or
    `speed` in speed, for the tools that vary in them); and so on for both halves."""
    count = len(points)
    if count < 2:
        return list(points)
    by_speed = tool in _SIMPLIFY_SPEED and speed is not None
    by_pressure = tool in _SIMPLIFY_PRESSURE and pressure is not None
    distance = f32(distance)
    width = None if width is None else f32(width)
    pressure, speed = f32(pressure or 0.0), f32(speed or 0.0)
    keep = bytearray(count)
    keep[0] = keep[-1] = 1
    todo = [(0, count - 1)]
    while todo:
        first, last = todo.pop()
        a, b = points[first], points[last]
        far, at = -1.0, 0
        d_pressure = d_width = d_speed = 0.0
        ax, ay = f32(a.x - b.x), f32(a.y - b.y)
        same = _fuzzy_equal(a.x, b.x) and _fuzzy_equal(a.y, b.y)
        length = 0.0 if same else math.sqrt(ax * ax + ay * ay)
        a_pressure, a_width, a_speed = f32(a.pressure / _255), f32(a.width * 0.25), f32(a.speed * 0.25)
        for n in range(first + 1, last):
            q = points[n]
            qx, qy = f32(q.x - b.x), f32(q.y - b.y)
            if same:
                off = f32(math.sqrt(qy * qy + qx * qx))
            elif length > 0:
                along = (ay / length) * qy + (ax / length) * qx
                square = (qy * qy + qx * qx) - along * along
                off = f32(math.sqrt(square)) if square >= 0 else 0.0
            else:
                off = 0.0
            if off > far:
                far, at = off, n
            d_pressure = max(d_pressure, abs(f32(a_pressure - f32(q.pressure / _255))))
            d_width = max(d_width, abs(f32(a_width - f32(q.width * 0.25))))
            d_speed = max(d_speed, abs(f32(a_speed - f32(q.speed * 0.25))))
        if (distance < far or (width is not None and width < d_width) or (by_pressure and pressure < d_pressure)
                or (by_speed and speed < d_speed)):
            keep[at] = 1
            todo += [(first, at), (at, last)]
    return [p for p, kept in zip(points, keep) if kept]


def simplify(line: LineValue) -> LineValue | None:
    """A drawn line as the scene takes it (0x140b3f850): thinned out, once more and
    harder when it still has 29990 points or more; None when even that leaves too many
    (the app then adds nothing)."""
    points = simplify_points(line.tool, line.unpacked())
    if len(points) >= MAX_LINE_POINTS:
        points = simplify_points(line.tool, points, 1.0, 0.1, 0.5, 1.0)
        if len(points) >= MAX_LINE_POINTS:
            return None
    return replace(line, points=pack_points(points))


# ------------------------------------------------------------------ hold to straighten

def _crt_round(value: float) -> float:
    """CRT round: half away from zero."""
    return float(math.floor(value + 0.5)) if value >= 0 else float(math.ceil(value - 0.5))


def straighten(line: LineValue, end: tuple[float, float], scale: float = 1.0) -> LineValue | None:
    """The drawn `line` made a straight line to `end` (scene coordinates), as
    sceneview::ShapesOverlay makes a Line (type 1, 0x140461c10 -> 0x14046be20): the
    same points, each keeping its speed, width, direction and pressure bytes; only x
    and y change. A = the first point, B = `end`, tol = min(|AB| / 3, 10) / scale
    (`scale`: screen pixels per scene unit). A and B are rounded to whole scene units;
    the line is made horizontal when |B.y - A.y| < tol, vertical when |B.x - A.x| < tol,
    and otherwise keeps its slope (no angle steps). Point i goes to
    A + (B - A) * s_i / s_total, s the length drawn up to it, in double precision,
    stored as float32. None for fewer than 3 points ("Insufficient points in line")."""
    points = line.unpacked()
    if len(points) < 3:
        return None
    ax, ay = float(points[0].x), float(points[0].y)
    bx, by = float(end[0]), float(end[1])
    tol = min(math.hypot(bx - ax, by - ay) / 3.0, 10.0) / (scale if scale > 0 else 1.0)
    ax, ay, bx, by = _crt_round(ax), _crt_round(ay), _crt_round(bx), _crt_round(by)
    if abs(by - ay) < tol:
        by = ay
    if abs(bx - ax) < tol:
        bx = ax
    along = [0.0]
    for p, q in zip(points, points[1:]):
        along.append(along[-1] + math.hypot(float(q.x) - float(p.x), float(q.y) - float(p.y)))
    total = along[-1]
    out = []
    for n, p in enumerate(points):
        t = along[n] / total if total > 0 else (n / (len(points) - 1))
        out.append(p._replace(x=f32(ax + (bx - ax) * t), y=f32(ay + (by - ay) * t)))
    return replace(line, points=pack_points(out))


def draw(samples: Iterable[tuple], tool: int, color: int = 0, thickness: float = MEDIUM, argb: int | None = None,
         **how) -> LineValue | None:
    """The line the app would put on a page for pen `samples`, each (x, y) from a
    mouse, or (x, y, pressure, tilt x, tilt y) from a pen, in scene coordinates. None
    for no samples, or too many."""
    pen = StrokeBuilder(tool, color, thickness, argb, **how)
    for sample in samples:
        pen.add(*sample)
    return simplify(pen.line()) if pen.points else None


# ------------------------------------------------------------------ the strokes of a page

@dataclass(frozen=True)
class StrokeItem:
    """A live line item of a page."""

    item: tuple[int, int]
    parent: tuple[int, int]
    left: tuple[int, int]
    right: tuple[int, int]
    line: LineValue


def _line_item(block: w.Block) -> tuple[w.ItemHeader, LineValue | None]:
    item = w.decode_item(block.payload)
    if item.encode() != block.payload:
        raise RmParseError("the line item is not understood")
    if item.value is None:
        return item, None
    if block.version < 2:
        raise StrokeError("the stroke is stored with version 1 points")
    return item, decode_line_value(item.value)


def strokes(page: bytes | None, parent: tuple[int, int] | None = None) -> list[StrokeItem]:
    """The live strokes of a page (of the node `parent` only, when given), in file order."""
    out = []
    for block in w.split(page)[1] if page is not None else []:
        if block.type == w.BLOCK_LINE_ITEM:
            item, line = _line_item(block)
            if line is not None and parent in (None, item.parent):
                out.append(StrokeItem(item.item, item.parent, item.left, item.right, line))
    return out


def has_text(blocks: list[w.Block]) -> bool:
    """Whether the page has a text box (RootText, 0x07). Strokes are drawn and erased on
    such a page as on any other: a new one goes on the current layer itself, unanchored;
    the eraser takes the layer's line items and those of the groups below it, those
    anchored to the text where the page draws them (erase, `anchors`), and leaves the
    text as it is. (The app puts strokes drawn on a page with text into anchored
    groups, which follow the text when it reflows: 472 of 472 strokes on the cached
    pages with real text, ARCHITECTURE.md 5.1. New ones are not made here.)"""
    return any(b.type == w.BLOCK_ROOT_TEXT for b in blocks)


def _layer(blocks: list[w.Block], layer: tuple[int, int] | None) -> tuple[int, int]:
    layer = w.current_layer(blocks) if layer is None else tuple(layer)
    if layer not in w.layers(blocks):
        raise StrokeError(f"{layer} is not a layer of the page")
    return layer


def _drawable(line: LineValue) -> LineValue:
    if not line.points:
        raise StrokeError("an empty line")                      # "addLine: empty line"
    if line.tool in NOT_DRAWN:
        raise StrokeError(f"tool {line.tool} does not draw")    # "addLine: invalid tool"
    if line.count >= MAX_LINE_POINTS:
        raise StrokeError("the line has too many points")
    if line.color == COLOR_ARGB and line.argb is None:
        return replace(line, argb=DEFAULT_ARGB)
    if line.color != COLOR_ARGB and line.argb is not None:
        return replace(line, argb=None)
    return line


def append_strokes(page: bytes | None, *, author: bytes, lines: list[LineValue],
                   layer: tuple[int, int] | None = None) -> tuple[bytes, list[tuple[int, int]]]:
    """(`page` with `lines` drawn on top of `layer` one after the other, the ids they
    took): append_stroke for several."""
    lines = [_drawable(line) for line in lines]
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    before, original = list(blocks), [b.payload for b in blocks]
    me = w._author_index(blocks, author)
    layer = _layer(blocks, layer)
    order = w.sequence(blocks, layer)
    left, clock, new, ids = order[-1][0] if order else ZERO_ID, w.highest_counter(blocks), [], []
    for line in lines:
        clock += 1
        new.append(w.new_block(w.BLOCK_LINE_ITEM, w.ItemHeader(layer, (me, clock), left, ZERO_ID, 0, line.encode()).encode()))
        left = (me, clock)
        ids.append(left)
    blocks = _insert_items(blocks, new)
    w._check_untouched(before, original, blocks, {0})
    return w.join(header, blocks), ids


def append_stroke(page: bytes | None, *, author: bytes, line: LineValue,
                  layer: tuple[int, int] | None = None) -> bytes:
    """`page` (None: no .rm yet) with `line` drawn on top of `layer` (default: the
    current layer), as addLine (0x1409578c0) and the layer (0x1408c2bc0) add it: the
    item takes the page's next id, its left neighbour is the layer's last item, its
    right 0:0. Every other block stays as it is, byte for byte."""
    return append_strokes(page, author=author, lines=[line], layer=layer)[0]


def find_strokes(page: bytes | None, line: LineValue) -> list[tuple[int, int]]:
    """The ids of the live strokes of the page with exactly this value, in file order."""
    if page is None:
        return []
    value, found = w.f_sub(6, _drawable(line).encode()), []
    for block in w.split(page)[1]:
        if block.type == w.BLOCK_LINE_ITEM and block.payload.endswith(value):
            item = w.decode_item(block.payload)
            if item.value is not None and not item.deleted_length:
                found.append(item.item)
    return found


# ------------------------------------------------------------------ deleting items

def _valid(item: w.ItemHeader) -> bool:
    """0x1408d2f40: an id is newer than both neighbours it was put between."""
    return item.item[1] > item.left[1] and item.item[1] > item.right[1]


def _last_id(item: w.ItemHeader) -> tuple[int, int]:
    return item.item[0], item.item[1] + max(item.deleted_length, 1) - 1


def can_join(a: w.ItemHeader, b: w.ItemHeader) -> bool:
    """Whether two deleted items next to each other become one run of deleted ids
    (0x1408c3e60): `b` was put right after the last id of `a`, by the same author,
    under the very next id, before the same right neighbour."""
    return (a.deleted_length > 0 and b.deleted_length > 0 and a.value is None and b.value is None
            and _last_id(a) == b.left and a.item[0] == b.item[0] and _last_id(a)[1] + 1 == b.item[1]
            and a.right == b.right and _valid(a) and _valid(b)
            and a.deleted_length + b.deleted_length < 1 << 32)


class _Link:
    """An item of a node: the item, the block it was read from (None: a new one), the
    type and versions it is written under, and its neighbours in the node's order."""

    __slots__ = ("item", "block", "kind", "prev", "next", "changed", "gone")

    def __init__(self, item: w.ItemHeader, block: w.Block | None, kind: tuple[int, int, int]) -> None:
        self.item, self.block, self.kind = item, block, kind
        self.prev: _Link | None = None
        self.next: _Link | None = None
        self.changed, self.gone = block is None, False

    def ids(self):
        author, counter = self.item.item
        return ((author, counter + n) for n in range(max(self.item.deleted_length, 1)))


class _Chain:
    """The items of one node in their order (rm_writer.sequence), to delete items of
    and put new ones into as the scene's sequence does, and then written back: an
    item that changed under its block's own type and versions (a deleted line stays a
    line block, a tombstone a tombstone: 0x140a094e0), the others as they were."""

    def __init__(self, blocks: list[w.Block], parent: tuple[int, int]) -> None:
        self.parent = parent
        self.at: dict[tuple[int, int], _Link] = {}
        self.of: dict[int, _Link] = {}                  # by the block read
        self.new: list[_Link] = []
        last: _Link | None = None
        for item_id, block in w.sequence(blocks, parent):
            link = self.of.get(id(block))
            if link is None:
                item = w.decode_item(block.payload)
                if item.encode() != block.payload:
                    raise RmParseError("an item of the page is not understood")
                link = self.of[id(block)] = _Link(item, block, (block.type, block.min_version, block.version))
                link.prev = last
                if last is not None:
                    last.next = link
                last = link
            elif link is not last:
                raise RmParseError("a run of deleted ids of the page is not in one piece")
            self.at[item_id] = link

    def _join(self, a: _Link, b: _Link) -> None:
        """`b` becomes part of `a`, the earlier of the two."""
        for item_id in b.ids():
            self.at[item_id] = a
        a.item = replace(a.item, deleted_length=a.item.deleted_length + b.item.deleted_length)
        a.changed, b.gone = True, True
        a.next = b.next
        if b.next is not None:
            b.next.prev = a

    def delete(self, item_id: tuple[int, int]) -> bool:
        """Delete the live item `item_id` as the sequence does (0x1408ce450): it loses
        its value and counts one deleted id; then it is joined to the deleted item
        after it, and to the one before it, where can_join allows. False when the
        node has no such live item."""
        link = self.at.get(item_id)
        if link is None or link.item.value is None or link.item.deleted_length:
            return False
        link.item = replace(link.item, deleted_length=1, value=None)
        link.changed = True
        if link.next is not None and can_join(link.item, link.next.item):
            self._join(link, link.next)
        if link.prev is not None and can_join(link.prev.item, link.item):
            self._join(link.prev, link)
        return True

    def _after(self, link: _Link, new: _Link) -> None:
        new.prev, new.next = link, link.next
        if link.next is not None:
            link.next.prev = new
        link.next = new
        self.new.append(new)
        for item_id in new.ids():
            self.at[item_id] = new

    def insert_after(self, left: tuple[int, int], item_id: tuple[int, int], value: bytes) -> None:
        """Put a new line item right after the item `left` (0x1408c8920): its right
        neighbour is what followed `left`. When `left` is an id inside a run of
        deleted ids the run is cut there, the second part keeping its ids, with the
        id before it as its left and the run's right (as real pages have it: the
        piece an eraser left always follows the last id of a run)."""
        link = self.at.get(left)
        if link is None:
            raise RmParseError(f"the page has no item {left} to put a stroke after")
        cut = left[1] - link.item.item[1] + 1
        if link.item.deleted_length > cut:
            rest = w.ItemHeader(self.parent, (left[0], left[1] + 1), left, link.item.right,
                                link.item.deleted_length - cut, None)
            link.item = replace(link.item, deleted_length=cut)
            link.changed = True
            self._after(link, _Link(rest, None, link.kind))
        right = link.next.item.item if link.next is not None else ZERO_ID
        self._after(link, _Link(w.ItemHeader(self.parent, item_id, left, right, 0, value), None,
                                (w.BLOCK_LINE_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_LINE_ITEM])))

    def write(self, blocks: list[w.Block]) -> list[w.Block]:
        """`blocks` with what was done to this node's items."""
        out = []
        for block in blocks:
            link = self.of.get(id(block))
            if link is None or not (link.changed or link.gone):
                out.append(block)
            elif not link.gone:
                out.append(w.Block(*link.kind, link.item.encode()))
        return _insert_items(out, [w.Block(*link.kind, link.item.encode()) for link in self.new if not link.gone])


def _insert_items(blocks: list[w.Block], new: list[w.Block]) -> list[w.Block]:
    """`blocks` with the item blocks `new` where the app stores them: after the other
    blocks, the items sorted by parent then id (rm_writer.insert_items)."""
    return w.insert_items(blocks, new)


def delete_strokes(page: bytes, items: Iterable[tuple[int, int]]) -> bytes:
    """`page` with the strokes `items` (ids) deleted, each as the app deletes an item
    (_Chain.delete): the block stays a line block where it is, without its value and
    with deletedLength 1, and is joined to deleted neighbours. Ids that are not live
    strokes of the page (deleted meanwhile) are passed over."""
    header, blocks = w.split(page)
    parents: dict[tuple[int, int], tuple[int, int]] = {}
    for block in blocks:
        if block.type == w.BLOCK_LINE_ITEM:
            item = w.decode_item(block.payload)
            if item.value is not None and not item.deleted_length:
                parents[item.item] = item.parent
    chains: dict[tuple[int, int], _Chain] = {}
    changed = False
    for item in items:
        parent = parents.get(tuple(item))
        if parent is None:
            continue
        if parent not in chains:
            chains[parent] = _Chain(blocks, parent)
        changed |= chains[parent].delete(tuple(item))
    if not changed:
        return page
    for chain in chains.values():
        blocks = chain.write(blocks)
    return w.join(header, blocks)


# ------------------------------------------------------------------ the eraser

_EPSILON = f32(1e-05)       # 0x140d6dd34


@dataclass(frozen=True)
class Outline:
    """What an eraser covers: a closed outline (corner points in order, the last
    joined to the first by its own last point), filled where the winding is not 0."""

    points: tuple[tuple[float, float], ...]

    @cached_property
    def edges(self) -> tuple:
        return tuple(zip(self.points, self.points[1:]))

    @cached_property
    def box(self) -> tuple[float, float, float, float]:
        """The least and greatest x and y of the corner points: left, top, right, bottom."""
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    def bounds(self) -> tuple[float, float, float, float]:
        """left, top, right, bottom: at least 1e-5 wide and high (0x140a5f380)."""
        left, top, right, bottom = self.box
        tiny = 9.999999747378752e-06
        return left, top, left + max(f32(right - left), tiny), top + max(f32(bottom - top), tiny)

    def contains(self, x: float, y: float) -> bool:
        """0x140a63420: the winding of the outline around (x, y), counted along a ray
        from the point to the left of everything."""
        left, top, right, bottom = self.bounds()
        if not (top <= y < bottom and left <= x < right):
            return False
        winding, out = 0, f32(f32(left) - _EPSILON)
        for (ax, ay), (bx, by) in self.edges:
            if (min(ay, by) <= y < max(ay, by) and min(ax, bx) < x and _EPSILON <= abs(f32(ay - by))
                    and _crossing((x, y), (out, y), (ax, ay), (bx, by)) is not None):
                winding += 1 if ay < by else -1
        return winding != 0


def _crossing(p1, p2, p3, p4) -> float | None:
    """Where the stretch p1-p2 crosses p3-p4, as a part of p1-p2 (0 at p1, 1 at p2);
    None when they do not cross or run alongside (0x140a63300)."""
    run = f32(p2[0] - p1[0])
    ex, ey = f32(p3[0] - p4[0]), f32(p3[1] - p4[1])
    rise = f32(p2[1] - p1[1])
    den = f32(f32(ex * rise) - f32(ey * run))
    if not _EPSILON <= abs(den):
        return None
    ox, oy = f32(p1[0] - p3[0]), f32(p1[1] - p3[1])
    s = f32(f32(f32(ox * ey) - f32(oy * ex)) / den)
    if not 0.0 <= s <= 1.0:
        return None
    u = f32(f32(f32(oy * run) - f32(ox * rise)) / den)
    return s if 0.0 <= u <= 1.0 else None


def eraser_outline(eraser: LineValue) -> Outline:
    """What the eraser line `eraser` covers (0x140a5f380). The Eraser's line is given
    the width of its first point, with round ends and corners, and the outline of that
    is taken; any other tool's line (EraseSection: a lasso) is itself the outline.

    The app has Qt do both (QPainterPathStroker::createStroke and
    QPainterPath::toFillPolygon), so this does too, with the same Qt."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath, QPainterPathStroker, QTransform

    points = eraser.unpacked()
    if not points:
        return Outline(())
    path = QPainterPath()
    path.moveTo(QPointF(points[0].x, points[0].y))
    for p in points[1:]:
        path.lineTo(QPointF(p.x, p.y))
    if eraser.tool == ERASER:
        stroker = QPainterPathStroker()
        stroker.setWidth(f32(points[0].width * 0.25))
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        path = stroker.createStroke(path)
    polygon = path.toFillPolygon(QTransform())
    return Outline(tuple((f32(polygon.at(n).x()), f32(polygon.at(n).y())) for n in range(polygon.size())))


def _between(a: Point, b: Point, part: float) -> Point:
    """The point `part` of the way from `a` to `b`, every number of it taken that far
    between theirs and rounded again (0x140a612c0)."""
    rest = f32(1.0 - part)

    def mix(low: float, high: float) -> float:
        return f32(f32(rest * low) + f32(high * part))

    turn_a = f32(f32(f32(a.direction + a.direction) * _PI) / _255)
    turn_b = f32(f32(f32(b.direction + b.direction) * _PI) / _255)
    return Point(mix(a.x, b.x), mix(a.y, b.y),
                 _roundf(f32(mix(f32(a.speed * 0.25), f32(b.speed * 0.25)) * 4.0)) & 0xFFFF,
                 _roundf(f32(mix(f32(a.width * 0.25), f32(b.width * 0.25)) * 4.0)) & 0xFFFF,
                 _roundf(f32(f32(mix(turn_a, turn_b) * _255) / _TWO_PI)) & 0xFF,
                 _roundf(f32(mix(f32(a.pressure / _255), f32(b.pressure / _255)) * _255)) & 0xFF)


# How far a tool's ink can reach from its points, for a size of 1 (0x140b3e5f0).
_REACH = {PAINTBRUSH: 50.0, PENCIL: 50.0, BALLPOINT: 2.0, BALLPOINT_V2: 2.0, SOLID_PEN: 2.0, RESERVED_PEN: 2.0,
          MARKER: 20.0, MARKER_V2: 20.0, FINELINER: 3.0, SHARP_PENCIL: 3.0, ERASE_SECTION: 3.0, SELECTION_TOOL: 3.0,
          FINELINER_V2: 3.0, HIGHLIGHTER: 30.0, HIGHLIGHTER_V2: 30.0, ZOOM_TOOL: 10.0, SHADING_MARKER: 10.0,
          PAINTBRUSH_V2: 15.0, SHARP_PENCIL_V2: 4.0, PENCIL_V2: 18.0, CALLIGRAPHY: 6.0}


def _reach(tool: int, thickness: float) -> float:
    size = f32(thickness)
    factor = f32(thickness * 10.0) if tool in (ERASER, MASKED_ERASER) else _REACH.get(tool, 1.0)
    return f32(f32(f32(size * factor) * 1.0) + 2.0)


def line_bounds(line: LineValue, points: list[Point] | None = None) -> tuple[float, float, float, float] | None:
    """left, top, right, bottom of a line (Line::boundingRect, 0x140b3d760): the box
    around its points, widened on every side by half of what its tool can reach."""
    points = line.unpacked() if points is None else points
    if not points:
        return None
    xs, ys = [p.x for p in points], [p.y for p in points]
    left, top = min(xs), min(ys)
    pad = _reach(line.tool, line.thickness_scale) * 0.5
    return left - pad, top - pad, left + f32(max(xs) - left) + pad, top + f32(max(ys) - top) + pad


def _overlap(a: tuple, b: tuple) -> bool:
    """QRectF::intersects: the two share some area."""
    return a[0] < a[2] and a[1] < a[3] and b[0] < b[2] and b[1] < b[3] and a[0] < b[2] and b[0] < a[2] \
        and a[1] < b[3] and b[1] < a[3]


def _whole(t: float) -> bool:
    return not abs(f32(t - _roundf(t))) > _EPSILON


def erase_line(outline: Outline, line: LineValue, dx: float = 0.0, dy: float = 0.0) -> list[LineValue] | None:
    """What an eraser leaves of `line` (drawn at an offset of dx, dy): None when it
    does not touch it, else the pieces left over, in order, possibly none
    (0x140a63920). A piece is the line's own points between two cuts, with a point
    worked out for each cut end; it starts where the line's length up to there says
    (startingLength); a piece of fewer than two points is dropped.

    The cuts are where the line crosses the outline. What lies between two cuts is
    erased when its middle is inside the outline. What is kept is shortened at each
    cut by a margin that grows with the line's width there and with how squarely the
    line meets the outline, so that the eraser takes the line's full width."""
    points = line.unpacked()
    count = len(points)
    edges = outline.edges
    if count < 1 or not edges:
        return None
    far_left, far_top, far_right, far_bottom = outline.box
    highlighter = line.tool in (HIGHLIGHTER, HIGHLIGHTER_V2)
    slim, wide = (0.0, 1.5) if highlighter else (f32(0.7), 2.0)
    dx, dy = f32(dx), f32(dy)
    box = line_bounds(line, points)
    if not _overlap((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy), outline.bounds()):
        return None
    length = f32(line.starting_length)
    cuts = [[0.0, length, 0.0, True]]           # where (in points), length so far, margin, erased after
    lengths: list[float] = []
    for n in range(1, count):                   # 0x140a62500
        a, b = points[n - 1], points[n]
        ax, ay, bx, by = f32(dx + a.x), f32(dy + a.y), f32(dx + b.x), f32(dy + b.y)
        run, rise = f32(bx - ax), f32(by - ay)
        step = _sqrt(f32(f32(rise * rise) + f32(run * run)))
        lengths.append(step)
        low_x, high_x, low_y, high_y = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        near = far_top <= high_y and low_y < far_bottom and far_left <= high_x and low_x <= far_right
        for (ex, ey), (fx, fy) in edges if near else ():
            if not (min(ey, fy) <= high_y and low_y < max(ey, fy) and min(ex, fx) <= high_x and low_x <= max(ex, fx)):
                continue
            part = _crossing((ax, ay), (bx, by), (ex, ey), (fx, fy))
            if part is None:
                continue
            thick = f32(f32(f32(1.0 - part) * f32(a.width * 0.25)) + f32(f32(b.width * 0.25) * part))
            edge_x, edge_y = f32(fx - ex), f32(fy - ey)
            edge = _sqrt(f32(f32(edge_y * edge_y) + f32(edge_x * edge_x)))
            square = abs(f32(f32(f32(edge_y / edge) * f32(rise / step)) + f32(f32(edge_x / edge) * f32(run / step))))
            margin = f32(f32(f32(f32(f32(1.0 - square) * slim) + f32(square * wide)) * thick) * 0.5)
            cuts.append([f32(float(n - 1) + part), f32(f32(part * step) + length), margin, True])
        length = f32(length + step)
    cuts.append([float(count - 1), length, 0.0, True])
    cuts.sort(key=lambda cut: cut[0])

    for k in range(len(cuts) - 1):              # 0x140a62f60: which stretches are erased
        start, end = cuts[k][0], cuts[k + 1][0]
        if not _EPSILON <= f32(end - start):
            continue
        up = int(math.ceil(start))
        if up == int(math.ceil(end)):
            down = int(math.floor(start))
            part = f32(f32(f32(end + start) * 0.5) - down)
            rest = f32(1.0 - part)
            x = f32(f32(rest * points[down].x) + f32(part * points[up].x))
            y = f32(f32(rest * points[down].y) + f32(part * points[up].y))
        else:
            x, y = points[up].x, points[up].y
        cuts[k][3] = outline.contains(f32(x + dx), f32(y + dy))
    k = 0
    while k < len(cuts) - 1:                    # two kept stretches in a row: a cut of no length between
        if not cuts[k][3] and not cuts[k + 1][3]:
            cuts.insert(k + 1, cuts[k + 1][:3] + [True])
            k += 1
        k += 1

    for k in range(len(cuts) - 1):              # 0x140a64210: the margins
        if cuts[k][3]:
            continue
        head, tail = cuts[k][2], cuts[k + 1][2]
        if not f32(tail + head) <= f32(cuts[k + 1][1] - cuts[k][1]):
            cuts[k][3] = True
            continue
        start, end, left = cuts[k][0], cuts[k + 1][0], head
        while left > 0.0 and end > start:
            floor = float(math.floor(start))
            step = lengths[int(floor)]
            if f32(start - floor) < _EPSILON:
                if left < step:
                    start = f32(f32(left / step) + floor)
                    break
                start, used = f32(start + 1.0), step
            else:
                used = f32(f32(1.0 - f32(start - floor)) * step)
                if left < used:
                    start = f32(start + f32(left / step))
                    break
                start = float(math.ceil(start))
            left = f32(left - used)
        left = tail
        while left > 0.0 and end > start:
            floor = float(math.floor(end))
            if f32(end - floor) < _EPSILON:
                floor = f32(end - 1.0)
                step = used = lengths[int(floor)]
                if left < step:
                    end = f32(f32(1.0 - f32(left / step)) + floor)
                    break
            else:
                step = lengths[int(floor)]
                used = f32(f32(end - floor) * step)
                if left < used:
                    end = f32(end - f32(left / step))
                    break
            left, end = f32(left - used), floor
        if start < end:
            cuts[k][0], cuts[k + 1][0], cuts[k][1] = start, end, f32(head + cuts[k][1])
        else:
            cuts[k][3] = True

    pieces = []
    for k in range(len(cuts) - 1):              # 0x140a612c0: the pieces
        if cuts[k][3]:
            continue
        start, end = cuts[k][0], cuts[k + 1][0]
        first, last = int(math.ceil(start)), int(math.floor(end))
        made: list[Point] = []
        if not _whole(start):
            down = int(math.floor(start))
            made.append(_between(points[down], points[first], f32(start - down)))
        elif _whole(end) and len(cuts) == 2:
            return None                         # one stretch, the whole line: untouched
        if last >= first:
            made += points[first:last + 1]
        if not _whole(end):
            made.append(_between(points[last], points[int(math.ceil(end))], f32(end - last)))
        if len(made) >= 2:
            pieces.append(replace(line, points=pack_points(made), starting_length=cuts[k][1],
                                  timestamp=DEFAULT_TIMESTAMP, move_id=None, source_id=None, tail=b""))
    return pieces


@dataclass(frozen=True)
class Erased:
    """A page after an eraser went over it: the strokes it deleted, and for each the
    ids of the pieces put in its place."""

    page: bytes
    deleted: tuple[tuple[int, int], ...]
    pieces: tuple[tuple[tuple[int, int], ...], ...]

    @property
    def made(self) -> tuple[tuple[int, int], ...]:
        return tuple(i for ids in self.pieces for i in ids)


def group_nodes(blocks: list[w.Block], node: tuple[int, int]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """(group node, its parent node) for the groups below `node`, at any depth, through
    live group items whose node is still attached to that parent (SceneTreeMove): each
    node before the ones below it, in the order its parent holds them (0x1408d1670)."""
    parents = w.tree_parents(blocks)
    out: list[tuple[tuple[int, int], tuple[int, int]]] = []
    seen = {tuple(node)}

    def visit(parent: tuple[int, int]) -> None:
        done: set[int] = set()
        for _, block in w.sequence(blocks, parent):
            if block.type != w.BLOCK_GROUP_ITEM or id(block) in done:
                continue
            done.add(id(block))
            item = w.decode_item(block.payload)
            if item.value is None:
                continue
            child = next((v for i, k, v in w.read_values(item.value[1:]) if (i, k) == (2, w.TAG_ID)), None)
            if child is None or child in seen or parents.get(child, parent) != parent:
                continue
            seen.add(child)
            out.append((child, parent))
            visit(child)

    visit(tuple(node))
    return out


def anchored_nodes(blocks: list[w.Block]) -> set[tuple[int, int]]:
    """The tree nodes of a page that carry a text anchor (SceneTreeNode field 7)."""
    out = set()
    for block in blocks:
        if block.type == w.BLOCK_TREE_NODE:
            fields = {i: (k, v) for i, k, v in w.read_values(block.payload)}
            if 7 in fields and 1 in fields and fields[1][0] == w.TAG_ID:
                out.add(fields[1][1])
    return out


def erase(page: bytes, *, author: bytes, eraser: LineValue, layer: tuple[int, int] | None = None,
          anchors: dict | None = None) -> Erased:
    """`page` with the eraser line `eraser` (tool Eraser, or EraseSection for a lasso)
    taken over `layer` (default: the current layer), as the app erases (0x140996ae0 ->
    0x140994f30): the layer's own strokes, then those of every group below it at any
    depth (group_nodes), in the order each node holds them. Every stroke it touches is
    deleted (as delete_strokes does); then what is left of each is put right after it,
    in its own node, as new strokes with the page's next ids (0x140993ba0 ->
    0x1408c8920). A piece has the stroke's tool, colour and size, timestamp 0:1 and no
    moveId; a shader's piece names the stroke it comes from (sourceId).

    A group anchored to typed text stores its strokes relative to its anchor position:
    `anchors` gives that position (scene = stored + position) by node, and it is added
    to the stroke's points for the cut test only (0x140a63920 takes it as dx, dy), so
    the pieces keep the group's own coordinates. A node below an anchored group takes
    the nearest anchored group's position. An anchored group whose position is not
    given is left alone (it cannot be told where it is drawn). The group's anchor is
    not touched; the app's clustering and re-anchoring afterwards (0x140979810) is not
    done. Untouched when the eraser meets nothing."""
    if eraser.tool not in (ERASER, ERASE_SECTION):
        raise StrokeError(f"tool {eraser.tool} does not erase")
    if eraser.count >= MAX_STORED_POINTS:
        raise StrokeError("the eraser line is too large")       # 0x140997e60
    header, blocks = w.split(page)
    layer = _layer(blocks, layer)
    outline = eraser_outline(eraser)
    if not outline.points:
        return Erased(page, (), ())
    anchors = {tuple(k): (float(v[0]), float(v[1])) for k, v in (anchors or {}).items()}
    anchored = anchored_nodes(blocks)
    # Where each node's strokes are drawn: the nearest anchored node's position.
    offset: dict[tuple[int, int], tuple[float, float] | None] = {layer: (0.0, 0.0)}
    nodes = [layer]
    for node, parent in group_nodes(blocks, layer):
        nodes.append(node)
        offset[node] = anchors.get(node) if node in anchored else offset.get(parent)
    hit: list[tuple[w.ItemHeader, LineValue, list[LineValue]]] = []
    seen: set[int] = set()
    for node in nodes:
        where = offset.get(node)
        if where is None:
            continue
        for _, block in w.sequence(blocks, node):
            if block.type != w.BLOCK_LINE_ITEM or id(block) in seen:
                continue
            seen.add(id(block))
            item = w.decode_item(block.payload)
            if item.value is None or block.version < 2:
                continue
            line = decode_line_value(item.value)
            pieces = erase_line(outline, line, *where)
            if pieces is not None:
                hit.append((item, line, pieces))
    if not hit:
        return Erased(page, (), ())
    me = w._author_index(blocks, author) if any(pieces for _, _, pieces in hit) else None
    chains: dict[tuple[int, int], _Chain] = {}
    for item, _, _ in hit:
        if item.parent not in chains:
            chains[item.parent] = _Chain(blocks, item.parent)
        chains[item.parent].delete(item.item)
    clock = w.highest_counter(blocks)
    made = []
    for item, line, pieces in hit:
        ids, left = [], item.item
        for piece in pieces:
            if line.tool == SHADING_MARKER:
                piece = replace(piece, source_id=line.source_id if line.source_id not in (None, ZERO_ID) else item.item)
            clock += 1
            chains[item.parent].insert_after(left, (me, clock), piece.encode())
            left = (me, clock)
            ids.append(left)
        made.append(tuple(ids))
    for chain in chains.values():
        blocks = chain.write(blocks)
    return Erased(w.join(header, blocks), tuple(item.item for item, _, _ in hit), tuple(made))


@dataclass(frozen=True)
class Cleared:
    """A layer cleared (ClearPage): the page after it, and what it deleted as
    (node, item id, line value bytes), in the order the app deletes them: the layer's
    own items, then each group below it, each node's in its sequence order."""

    page: bytes
    deleted: tuple[tuple[tuple[int, int], tuple[int, int], bytes], ...]

    def undo_order(self) -> tuple[tuple[tuple[int, int], bytes], ...]:
        """(item, value) as the app's undo puts them back, each taking the next id: the
        macro's node actions last first (0x1408f52b0), each node's items in order."""
        nodes: list[tuple[int, int]] = []
        for node, _, _ in self.deleted:
            if node not in nodes:
                nodes.append(node)
        return tuple((item, value) for node in reversed(nodes)
                     for n, item, value in self.deleted if n == node)


def clear_layer(page: bytes, *, layer: tuple[int, int] | None = None) -> Cleared:
    """`page` with the strokes of `layer` (default: the current layer) deleted, as the
    app's ClearPage does it (SceneController::clearLinesInLayer 0x140439c50 -> 0x140995ad0
    -> scene::eraser::createEraseItemsActions 0x1409931c0): the layer and every group
    node below it at any depth (group_nodes), each node's live items that are neither
    images nor groups (the filter 0x140433d20: value type 7 or 2 stays) deleted as
    delete_strokes deletes them, one SceneDeleteItemsActionCrdt per node in one
    SceneMacroAction. Nothing is cut and no id is taken. Every item a real page has of
    that kind is a line; glyph ranges (none in any cached page) and strokes stored with
    version 1 points (which this client cannot put back) are left. The app also detaches
    a group node that is left empty (SceneDeleteGroupActionCrdt, a SceneTreeMove to 0:0)
    and drops its anchor; the client leaves such a group in place, empty, as its eraser
    does. Untouched when the layer holds no stroke."""
    header, blocks = w.split(page)
    layer = _layer(blocks, layer)
    nodes = [layer] + [node for node, _ in group_nodes(blocks, layer)]
    found: list[tuple[tuple[int, int], tuple[int, int], bytes]] = []
    for node in nodes:
        seen: set[int] = set()
        for item_id, block in w.sequence(blocks, node):
            if block.type != w.BLOCK_LINE_ITEM or id(block) in seen or block.version < 2:
                continue
            seen.add(id(block))
            item = w.decode_item(block.payload)
            if item.value is not None and not item.deleted_length:
                found.append((node, item.item, item.value))
    if not found:
        return Cleared(page, ())
    chains: dict[tuple[int, int], _Chain] = {}
    for node, item, _ in found:
        if node not in chains:
            chains[node] = _Chain(blocks, node)
        chains[node].delete(item)
    for chain in chains.values():
        blocks = chain.write(blocks)
    return Cleared(w.join(header, blocks), tuple(found))


def restore_stroke(page: bytes, *, author: bytes, item: tuple[int, int], line: LineValue,
                   move_id: tuple[int, int] | None = None) -> tuple[bytes, tuple[int, int]]:
    """(`page` with a deleted stroke put back, its new id), as the app's undo and redo
    put an item back (SceneActionStack -> 0x14095e690 / 0x14095f520 -> 0x1408cbd60): the
    deleted item stays deleted, and a new line item with the page's next id goes right
    after it (`item`: the deleted id; inside a run of deleted ids the run is cut there,
    as _Chain.insert_after does), with `line` as its value and its moveId set to
    `move_id` (default: `item`) unless the value has one already. StrokeError when the
    page has no deleted item `item`."""
    header, blocks = w.split(page)
    item = tuple(item)
    owner = None
    for block in blocks:
        if block.type in w.ITEM_BLOCKS:
            found = w.decode_item(block.payload)
            if found.item[0] == item[0] and 0 <= item[1] - found.item[1] < max(found.deleted_length, 1):
                owner = found
                break
    if owner is None or owner.value is not None or owner.deleted_length < 1:
        raise StrokeError(f"the page has no deleted item {item}")
    line = _drawable(line) if line.tool not in NOT_DRAWN else line
    if line.move_id in (None, ZERO_ID):
        line = replace(line, move_id=tuple(move_id) if move_id is not None else item)
    me = w._author_index(blocks, author)
    new_id = (me, w.highest_counter(blocks) + 1)
    chain = _Chain(blocks, owner.parent)
    chain.insert_after(item, new_id, line.encode())
    return w.join(header, chain.write(blocks)), new_id


def eraser_line(samples: Iterable[tuple], thickness: float = MEDIUM, tool: int = ERASER, **how) -> LineValue:
    """The line an eraser draws over pen `samples` (as draw takes them): what erase
    takes. Its points are 10 scene units wide for each unit of `thickness`."""
    pen = StrokeBuilder(tool, 2, thickness, **how)
    for sample in samples:
        pen.add(*sample)
    return pen.line()


__all__ = ["BALLPOINT_V2", "CALLIGRAPHY", "CLEAR_PAGE", "COLOR_ARGB", "Cleared", "DEFAULT_TIMESTAMP", "ERASER", "ERASE_SECTION", "Erased",
           "FINELINER_V2", "HIGHLIGHTER_V2", "LineValue", "anchored_nodes", "group_nodes", "MARKER_V2", "MEDIUM", "MOUSE_PRESSURE", "NOT_DRAWN",
           "Outline", "PAINTBRUSH_V2", "PENCIL_V2", "PENS", "Point", "SHADING_MARKER", "SHARP_PENCIL_V2",
           "StrokeBuilder", "StrokeError", "StrokeItem", "THICK", "THIN", "append_stroke", "append_strokes", "can_join",
           "clear_layer", "decode_line_value", "delete_strokes", "draw", "erase", "erase_line", "eraser_line", "eraser_outline",
           "find_strokes", "has_text", "restore_stroke",
           "line_bounds", "make_point", "new_line", "pack_points", "rotation_of", "simplify", "simplify_points",
           "straighten", "strokes",
           "unpack_points"]
