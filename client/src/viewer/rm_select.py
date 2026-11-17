"""Selecting items of a page, moving, scaling and turning them, and pasting copies, made
as reMarkable.exe 3.29 makes them (ARCHITECTURE.md 5.1, "Moving, scaling and rotating a
selection").

There is no transform on nodes: a moved item is a new item. A selection's gesture
changes nothing in the page; the app multiplies a pending QTransform and commits it
once, when the selection closes (SceneController::applyPendingEdit 0x140438540 ->
0x1409b48a0). The commit, per node holding selected items (transform_items):

    1. the selected live items are deleted (0x1408c5910; the blocks stay, joined into runs);
    2. a clone of each is added (0x1408c2ae0 -> 0x1408c2bc0: each the page's next id,
       appended), its moveId = the original's moveId, or the original's id when that is 0:0;
    3. the node's transform (0x1408c3570) deletes those clones again (one run of n ids),
       transforms each (Line::transform, 0x140b3d4d0, with no new timestamp) and adds it
       once more (next id, appended).

So n strokes cost 2n ids. In a group anchored to text the transform is conjugated with
the translation by the group's anchor position (its items are stored relative to it).

Picking (0x1409b2ab0 -> 0x1408c9250, per node): the rectangle of the desktop app's
selection tool (SceneController::addSelectionRect, 0x140437a20) takes a stroke when ANY
of its points, rounded to whole units, lies in the rectangle (line item vtable +0x40,
0x1405b73a0), an image when the rectangle meets the box around its corners (0x140442e80);
with Shift (ToggleSelection) what it takes flips in or out of the selection. A lasso
(the tablet's selection tool, 0x1409b3600 -> 0x1409ae1c0) is the polygon of the lasso
line's points, as the union of four regions shifted by (+-1, +-1) (odd-even fill), and
takes a stroke only when ALL its rounded points lie in it (vtable +0x38, 0x1405b7520).
Only strokes of drawing tools and the legacy Eraser can be picked (0x1405b7530).

Copy and paste (cloneSelectedItems 0x14043a320, cloneAddAndSelectItems 0x14043a060 ->
0x1409af310): a clone has id, parent and moveId 0:0 and keeps its timestamp; a pasted
copy is scaled, moved so that the box of all pasted items is centred on the place
pasted at, and appended to the layer (n new ids, moveId 0:0).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Iterable, Sequence

from . import rm_stroke as rs
from . import rm_writer as w

ZERO_ID = w.ZERO_ID
Matrix = tuple  # (m11, m12, m21, m22, dx, dy): a QTransform without perspective
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# What a layer refuses as an item (0x1408c2bc0): EraseSection, ClearPage, ZoomTool,
# SelectionTool, MaskedEraser. Everything else may be pasted or re-added.
NOT_ADDED = frozenset({rs.ERASE_SECTION, rs.CLEAR_PAGE, rs.ZOOM_TOOL, rs.SELECTION_TOOL, rs.MASKED_ERASER})
# What a selection can pick (0x1405b7530 -> 0x140b3e0e0, or the legacy Eraser).
NOT_SELECTABLE = frozenset({rs.ERASE_SECTION, rs.CLEAR_PAGE, rs.ZOOM_TOOL, rs.SELECTION_TOOL, rs.MASKED_ERASER})


class SelectError(ValueError):
    """A selection edit that does not fit the page."""


# ------------------------------------------------------------------ transforms (QTransform, affine)

def _fuzzy_null(value: float) -> bool:
    return abs(value) <= 1e-12                  # qFuzzyIsNull(double)


def matrix(value) -> Matrix:
    m = tuple(float(v) for v in value)
    if len(m) != 6 or any(v != v or abs(v) == float("inf") for v in m):
        raise SelectError(f"not an affine transform: {value!r}")
    return m


def is_identity(m: Matrix) -> bool:
    """QTransform::isIdentity: within qFuzzyIsNull of the identity."""
    return (_fuzzy_null(m[0] - 1) and _fuzzy_null(m[1]) and _fuzzy_null(m[2]) and _fuzzy_null(m[3] - 1)
            and _fuzzy_null(m[4]) and _fuzzy_null(m[5]))


def is_translation(m: Matrix) -> bool:
    """QTransform::type() <= TxTranslate."""
    return _fuzzy_null(m[0] - 1) and _fuzzy_null(m[1]) and _fuzzy_null(m[2]) and _fuzzy_null(m[3] - 1)


def multiply(a: Matrix, b: Matrix) -> Matrix:
    """a * b as QTransform multiplies: a point goes through `a` first, then `b`."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def translation(dx: float, dy: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, float(dx), float(dy))


def scaling(ox: float, oy: float, sx: float, sy: float) -> Matrix:
    """Scaled about (ox, oy) (SceneSelectionHandler's corner handles: a negative
    scale mirrors)."""
    return multiply(multiply(translation(-ox, -oy), (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0)), translation(ox, oy))


def rotation(ox: float, oy: float, degrees: float) -> Matrix:
    """Turned about (ox, oy), clockwise on screen for a positive angle (QTransform::rotate,
    exact at quarter turns as Qt makes it)."""
    a = float(degrees) % 360.0
    if a == 90.0:
        s, c = 1.0, 0.0
    elif a == 180.0:
        s, c = 0.0, -1.0
    elif a == 270.0:
        s, c = -1.0, 0.0
    else:
        r = math.radians(float(degrees))
        s, c = math.sin(r), math.cos(r)
    return multiply(multiply(translation(-ox, -oy), (c, s, -s, c, 0.0, 0.0)), translation(ox, oy))


def map_point(m: Matrix, x: float, y: float) -> tuple[float, float]:
    """QTransform::map for an affine transform, in double precision."""
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def determinant(m: Matrix) -> float:
    return m[0] * m[3] - m[1] * m[2]


def conjugated(m: Matrix, anchor: tuple[float, float]) -> Matrix:
    """`m` for items stored relative to an anchor position (scene = stored + anchor)."""
    if not anchor[0] and not anchor[1]:
        return m
    return multiply(multiply(translation(anchor[0], anchor[1]), m), translation(-anchor[0], -anchor[1]))


# ------------------------------------------------------------------ Line::transform and images

def scale_of(m: Matrix) -> float:
    """What a transform does to sizes (0x140b3d4d0): 1 for a translation, else
    sqrt(|det|)."""
    return 1.0 if is_translation(m) else math.sqrt(abs(determinant(m)))


def transform_line(line: rs.LineValue, m: Matrix) -> rs.LineValue:
    """`line` transformed as Line::transform (0x140b3d4d0) does it: nothing for the
    identity; thicknessScale times s; each point's x and y mapped in double precision and
    stored as float32; each width roundf(width * 0.25 * s * 4) in single precision;
    speed, direction and pressure as they were (a turn does not turn the direction).
    Timestamp, moveId, sourceId and what follows them stay."""
    if is_identity(m):
        return line
    s = scale_of(m)
    s32 = rs.f32(s)
    points = []
    for p in line.unpacked():
        x, y = map_point(m, p.x, p.y)
        width = p.width
        if s != 1.0:
            width = rs._roundf(rs.f32(rs.f32(rs.f32(p.width * 0.25) * s32) * 4.0)) & 0xFFFF
        points.append(p._replace(x=rs.f32(x), y=rs.f32(y), width=width))
    return replace(line, thickness_scale=line.thickness_scale * s, points=rs.pack_points(points))


def transform_vertices(vertices, m: Matrix) -> list[tuple[float, float, float, float]]:
    """An image's corners mapped (x, y in double precision, stored as float32; u, v as
    they were)."""
    out = []
    for x, y, u, v in vertices:
        mx, my = map_point(m, x, y)
        out.append((rs.f32(mx), rs.f32(my), u, v))
    return out


def transform_image_value(value: w.ImageValue, m: Matrix) -> w.ImageValue:
    if is_identity(m):
        return value
    return w.ImageValue(value.ref, value.ref_timestamp, value.bounds_timestamp,
                        transform_vertices(value.vertices, m), value.indices, value.move_id)


# ------------------------------------------------------------------ bounds

Box = tuple  # left, top, right, bottom


def line_box(line: rs.LineValue, offset: tuple[float, float] = (0.0, 0.0)) -> Box | None:
    """Line::boundingRect (0x140b3d760) in scene coordinates."""
    b = rs.line_bounds(line)
    if b is None:
        return None
    return b[0] + offset[0], b[1] + offset[1], b[2] + offset[0], b[3] + offset[1]


def corners_box(vertices, offset: tuple[float, float] = (0.0, 0.0)) -> Box | None:
    if not vertices:
        return None
    xs, ys = [v[0] + offset[0] for v in vertices], [v[1] + offset[1] for v in vertices]
    return min(xs), min(ys), max(xs), max(ys)


def union(boxes: Iterable[Box | None]) -> Box | None:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


# ------------------------------------------------------------------ picking

_INT_MAX, _INT_MIN = 2147483647.0, -2147483648.0


def qround(value: float) -> int:
    """How the line item rounds a point to test it (0x1405b73a0): +-0.5, clamped to the
    int range, truncated."""
    value = value + 0.5 if value >= 0 else value - 0.5
    return int(min(max(value, _INT_MIN), _INT_MAX))


def rect_hits_line(xs: Sequence[float], ys: Sequence[float], rect: tuple[int, int, int, int]) -> bool:
    """Whether some point of a line, rounded, lies in the integer rectangle (x, y, w, h)
    (QRegion(QRect).contains(QPoint): right = x + w - 1)."""
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return False
    right, bottom = left + width - 1, top + height - 1
    for x, y in zip(xs, ys):
        px, py = qround(x), qround(y)
        if left <= px <= right and top <= py <= bottom:
            return True
    return False


def rect_hits_image(vertices, rect: tuple[int, int, int, int]) -> bool:
    """Whether the rectangle meets the aligned box around an image's corners
    (0x140442e80: QRegion::intersects(QRectF::toAlignedRect()))."""
    box = corners_box(vertices)
    if box is None:
        return False
    left, top = math.floor(box[0]), math.floor(box[1])
    right, bottom = math.ceil(box[2]), math.ceil(box[3])
    x, y, width, height = rect
    return width > 0 and height > 0 and left < x + width and x < right and top < y + height and y < bottom


class Lasso:
    """The region a lasso line encloses (0x1409ae1c0): the polygon of its points (QPolygonF
    -> QPolygon rounds them), shifted by (-1,-1), (-1,1), (1,-1) and (1,1), each filled
    odd-even, united. Needs PySide6."""

    def __init__(self, points: Sequence[tuple[float, float]]) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QPolygonF, QRegion
        polygon = QPolygonF([QPointF(float(x), float(y)) for x, y in points])
        region = QRegion()
        for dx, dy in ((-1.0, -1.0), (-1.0, 1.0), (1.0, -1.0), (1.0, 1.0)):
            region = region.united(QRegion(polygon.translated(dx, dy).toPolygon(), Qt.FillRule.OddEvenFill))
        self.region = region
        box = region.boundingRect()
        self.box = (box.x(), box.y(), box.width(), box.height())

    def contains(self, x: float, y: float) -> bool:
        from PySide6.QtCore import QPoint
        return self.region.contains(QPoint(qround(x), qround(y)))

    def holds_line(self, xs: Sequence[float], ys: Sequence[float], box: Box | None) -> bool:
        """Every rounded point inside, after the line's aligned box meets the region's
        bounding rectangle (0x1405b7520)."""
        if not xs or box is None:
            return False
        left, top, width, height = self.box
        bl, bt = math.floor(box[0]), math.floor(box[1])
        br, bb = math.ceil(box[2]), math.ceil(box[3])
        if not (width > 0 and height > 0 and bl < left + width and left < br and bt < top + height and top < bb):
            return False
        return all(self.contains(x, y) for x, y in zip(xs, ys))

    def holds_image(self, vertices) -> bool:
        """An image is taken when all four corners lie inside (the app tests the
        image's picture against the region, 0x140a64ee0, not traced further)."""
        return bool(vertices) and all(self.contains(v[0], v[1]) for v in vertices)


# ------------------------------------------------------------------ the items of a page

class Item:
    """A live line or image item of a page, in scene coordinates through `offset`."""

    __slots__ = ("item", "parent", "kind", "line", "image", "offset")

    def __init__(self, item, parent, kind, line=None, image=None, offset=(0.0, 0.0)) -> None:
        self.item, self.parent, self.kind = tuple(item), tuple(parent), kind
        self.line, self.image, self.offset = line, image, offset

    @property
    def is_line(self) -> bool:
        return self.kind == w.BLOCK_LINE_ITEM

    def box(self) -> Box | None:
        if self.is_line:
            return line_box(self.line, self.offset)
        return corners_box(self.image.vertices, self.offset)


def page_items(page: bytes | None) -> list[Item]:
    """The live line and image items of a page, in block order."""
    out = []
    for block in w.split(page)[1] if page is not None else []:
        if block.type not in (w.BLOCK_LINE_ITEM, w.BLOCK_IMAGE_ITEM):
            continue
        item = w.decode_item(block.payload)
        if item.value is None or item.deleted_length:
            continue
        if block.type == w.BLOCK_LINE_ITEM:
            if block.version < 2:
                continue
            out.append(Item(item.item, item.parent, block.type, line=rs.decode_line_value(item.value)))
        else:
            out.append(Item(item.item, item.parent, block.type, image=w.decode_image_value(item.value)))
    return out


# ------------------------------------------------------------------ the commit

class _Node(rs._Chain):
    """A node's items, with appending (the layer's add, 0x1408c2bc0: left = the last id
    of the sequence, deleted ones counted; right 0:0)."""

    def __init__(self, blocks, parent) -> None:
        super().__init__(blocks, parent)
        self.tail = None
        for link in self.of.values():
            if link.next is None:
                self.tail = link

    def _after(self, link, new) -> None:
        super()._after(link, new)
        if link is self.tail:
            self.tail = new

    def _join(self, a, b) -> None:
        super()._join(a, b)
        if b is self.tail:
            self.tail = a

    def append(self, item_id, value: bytes, kind) -> None:
        if self.tail is None:
            new = rs._Link(w.ItemHeader(self.parent, item_id, ZERO_ID, ZERO_ID, 0, value), None, kind)
            self.new.append(new)
            self.at[item_id] = new
            self.tail = new
            return
        last = self.tail.item
        left = (last.item[0], last.item[1] + max(last.deleted_length, 1) - 1)
        new = rs._Link(w.ItemHeader(self.parent, item_id, left, ZERO_ID, 0, value), None, kind)
        self._after(self.tail, new)


def _kind(block_type: int) -> tuple[int, int, int]:
    return (block_type, *w.BLOCK_VERSIONS[block_type])


def _stamped(value, original: tuple[int, int]):
    """A clone named after what it was a clone of: moveId = the original's moveId, or
    the original's id when it has none (0x1409b2ab0)."""
    move_id = value.move_id if value.move_id not in (None, ZERO_ID) else tuple(original)
    if isinstance(value, rs.LineValue):
        return replace(value, move_id=move_id)
    return w.ImageValue(value.ref, value.ref_timestamp, value.bounds_timestamp, list(value.vertices),
                        value.indices, move_id)


def _decode(block_type: int, value: bytes):
    return rs.decode_line_value(value) if block_type == w.BLOCK_LINE_ITEM else w.decode_image_value(value)


def transform_items(page: bytes, *, author: bytes, items: Iterable[tuple[int, int]], m: Matrix,
                    anchors: dict | None = None) -> tuple[bytes, dict, dict]:
    """(`page` with the live line and image items `items` transformed by `m` as the
    app commits a selection, {original id: new id}, {original id: its value}). Per node,
    in the node's order: the items are deleted, their clones (moveId stamped) added and
    deleted again, and the transformed clones added (2n ids). `anchors` gives a node
    anchored to text its anchor position: there `m` is conjugated with it. Items that
    are not live line or image items of the page (gone meanwhile) are passed over;
    the page is returned as it was for the identity or nothing to do."""
    m = matrix(m)
    header, blocks = w.split(page)
    wanted = {tuple(i) for i in items}
    found: dict[tuple[int, int], tuple[int, w.ItemHeader]] = {}
    for block in blocks:
        if block.type in (w.BLOCK_LINE_ITEM, w.BLOCK_IMAGE_ITEM):
            item = w.decode_item(block.payload)
            if item.item in wanted and item.value is not None and not item.deleted_length:
                if block.type == w.BLOCK_LINE_ITEM and block.version < 2:
                    raise SelectError("a selected stroke is stored with version 1 points")
                found[item.item] = (block.type, item)
    if is_identity(m) or not found:
        return page, {}, {}
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    nodes: dict[tuple[int, int], list] = {}
    for item_id, (kind, item) in found.items():
        nodes.setdefault(item.parent, []).append(item_id)
    made: dict[tuple[int, int], tuple[int, int]] = {}
    old: dict[tuple[int, int], object] = {}
    for parent in sorted(nodes, key=w.id_key):
        node = _Node(blocks, parent)
        order = {i: n for n, (i, _) in enumerate(w.sequence(blocks, parent))}
        chosen = sorted(nodes[parent], key=order.__getitem__)
        local = conjugated(m, (anchors or {}).get(parent, (0.0, 0.0)))
        values = []
        for item_id in chosen:
            kind, item = found[item_id]
            value = _decode(kind, item.value)
            old[item_id] = value
            values.append((item_id, kind, _stamped(value, item_id)))
            node.delete(item_id)
        clones = []
        for item_id, kind, value in values:
            clock += 1
            node.append((me, clock), value.encode(), _kind(kind))
            clones.append((me, clock))
        for clone in clones:
            node.delete(clone)
        for item_id, kind, value in values:
            clock += 1
            moved = transform_line(value, local) if kind == w.BLOCK_LINE_ITEM else transform_image_value(value, local)
            node.append((me, clock), moved.encode(), _kind(kind))
            made[item_id] = (me, clock)
        blocks = node.write(blocks)
    return w.join(header, blocks), made, old


def append_items(page: bytes | None, *, author: bytes, values: Sequence[rs.LineValue],
                 layer: tuple[int, int] | None = None) -> tuple[bytes, list[tuple[int, int]]]:
    """(`page` with line values pasted on top of `layer`, the ids they took): each the
    next id, appended, moveId 0:0, as the paste adds them (0x1408c2ae0). The legacy
    Eraser's white strokes may be pasted too; the tools a layer refuses may not."""
    lines = []
    for value in values:
        if value.tool in NOT_ADDED:
            raise SelectError(f"tool {value.tool} cannot be added to a layer")
        if not value.points:
            raise SelectError("an empty line")
        lines.append(replace(value, move_id=None))
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    layer = rs._layer(blocks, layer)
    if not lines:
        return w.join(header, blocks), []
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    node = _Node(blocks, layer)
    ids = []
    for line in lines:
        clock += 1
        node.append((me, clock), line.encode(), _kind(w.BLOCK_LINE_ITEM))
        ids.append((me, clock))
    return w.join(header, node.write(blocks)), ids


def clone(value, m: Matrix = IDENTITY):
    """A clipboard clone of a line or image value (0x1405b5930 then 0x14043a320): no
    moveId, timestamp kept, transformed by `m` unless that is the identity."""
    if isinstance(value, rs.LineValue):
        return transform_line(replace(value, move_id=None), m)
    out = w.ImageValue(value.ref, value.ref_timestamp, value.bounds_timestamp, list(value.vertices), value.indices,
                       None)
    return transform_image_value(out, m)


def pasted(values: Sequence, scale: float, center: tuple[float, float]) -> list:
    """Clipboard values as a paste places them (0x14043a060 -> 0x1409af310): cloned,
    scaled by `scale` about 0, 0 unless it is 1 (qFuzzyCompare), then moved so that the
    box around all of them is centred on `center`."""
    one = abs(scale - 1.0) * 1e12 <= min(abs(scale), 1.0)
    values = [clone(v, IDENTITY if one else (float(scale), 0.0, 0.0, float(scale), 0.0, 0.0)) for v in values]
    box = union(line_box(v) if isinstance(v, rs.LineValue) else corners_box(v.vertices) for v in values)
    if box is None:
        return values
    shift = translation(center[0] - (box[0] + box[2]) / 2, center[1] - (box[1] + box[3]) / 2)
    return [clone(v, shift) for v in values]


__all__ = ["IDENTITY", "Item", "Lasso", "Matrix", "NOT_ADDED", "NOT_SELECTABLE", "SelectError", "append_items",
           "clone", "conjugated", "corners_box", "determinant", "is_identity", "is_translation", "line_box",
           "map_point", "matrix", "multiply", "page_items", "pasted", "qround", "rect_hits_image", "rect_hits_line",
           "rotation", "scale_of", "scaling", "transform_image_value", "transform_items", "transform_line",
           "transform_vertices", "translation", "union"]

