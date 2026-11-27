"""Where a stroke goes on a page with typed text, and what the eraser does to the groups
anchored to it, as reMarkable.exe 3.29's anchor manager decides it (ARCHITECTURE.md 5.1,
"Text-anchored stroke groups": Where a new stroke goes, Proximity, Clustering, Eraser).

Pure geometry first (no Qt, no page): the items' leaf geometry in scene coordinates
(`Geo`), radius (0x140a5a1a0), tolerance (0x140a5a550) with the tool factor
(0x140b3e5f0), the densified polygon (0x140a59b90), the closed test (0x140a5a370),
`near` (0x140a5a6a0), `within` (0x140a57d70), `cluster` (0x140a58e00), `gravity`
(0x140a58a30), `index_at` (0x140974950) and `over_text` (0x1409741d0). Then the
decisions on a page (`place`, `regroup`), which return the page as the tablet's actions
would leave it: createGroup, SetAnchorIdAction, RemoveAnchorAction, SceneDeleteGroup
(a SceneTreeMove to 0:0), items deleted from a node and added to one (the next ids,
appended in order).

Inferred and kept so (the binary is silent): the line rectangle is the QTextLine's
(x, top, allotted width, height); an item's bounds are Line::boundingRect; "smaller" of
two long items is the one with fewer points and the 256-point limit counts the
densified polygon; a leaf pair is tried when the first leaf's box grown by (2d, d)
meets the second's; the (r) clusters get their groups with the kind decided as for a
new stroke; a merged group's RemoveAnchorAction stamps anchorId 0:0; an emptied group
is only detached; images and glyph ranges do not take part (strokes only).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Callable, Sequence

from . import renderer
from . import rm_anchor
from . import rm_layer
from . import rm_select
from . import rm_stroke as rs
from . import rm_writer as w
from .rm_parser import (DEFAULT_PAPER, PageScene, ParagraphStyle, RmParseError, TextBlock, _decode_authors,
                        _read_fields, decode_root_text, paper_size_of, text_block, text_order)

ZERO_ID = w.ZERO_ID
BIDIRECTIONAL, VERTICAL = rm_anchor.BIDIRECTIONAL, rm_anchor.VERTICAL
BEFORE_TEXT, AFTER_TEXT = rm_anchor.BEFORE_TEXT, rm_anchor.AFTER_TEXT
THRESHOLD = rm_anchor.THRESHOLD         # 35.748032 (manager+0x18): 4 mm at 227 dpi

LARGE = 178.74015748                    # 20 mm: an item wider and taller is anchored as mode 2 (0x14096ca70)
SMALL = 89.37007874015748               # 10 mm: below this both ways an item is small (0x140a5a1a0)
HALF_PI = math.pi / 2
STEP = 44.68503937007874                # 5 mm: segments longer than this are split (0x140a59b90)
MAX_POINTS = 256                        # more points than this: the box test (0x140a57d70)
CLOSED_LENGTH, CLOSED_GAP = 10.0, 0.5   # the closed polygon test (0x140a5a370)
NEAR_EPSILON = 1e-6                     # 0x140a5a6a0
LINE_PADDING = 10.0                     # the first line rectangle's top and the last one's bottom (0x1409741d0)
GRAVITY_REACH, GRAVITY_FLOOR = 50.0, 0.01   # 0x140a58a30: 50 beyond the text box, weight 100 / floor 0.01

Box = tuple[float, float, float, float]     # left, top, right, bottom


# ------------------------------------------------------------------ geometry

def tool_factor(tool: int, thickness: float) -> float:
    """The factor of a tool in the stroke width (0x140b3e5f0): 0/1 50, 2/15/19/20 2, 3/16 20,
    4/7/8/11/17 3, 5/18 30, 6/22 10 x thickness, 10/23 10, 12 15, 13 4, 14 18, 21 6, else 1."""
    if tool in (0, 1):
        return 50.0
    if tool in (2, 15, 19, 20):
        return 2.0
    if tool in (3, 16):
        return 20.0
    if tool in (4, 7, 8, 11, 17):
        return 3.0
    if tool in (5, 18):
        return 30.0
    if tool in (6, 22):
        return 10.0 * thickness
    if tool in (10, 23):
        return 10.0
    if tool == 12:
        return 15.0
    if tool == 13:
        return 4.0
    if tool == 14:
        return 18.0
    if tool == 21:
        return 6.0
    return 1.0


def width_factor(tool: int, thickness: float) -> float:
    """thickness x the tool's factor + 2 (0x140b3e5f0)."""
    return thickness * tool_factor(tool, thickness) + 2.0


def tolerance(tool: int, thickness: float) -> float:
    """A stroke's tolerance t (0x140a5a550): 0.25 x (thickness x tool factor + 2)."""
    return 0.25 * width_factor(tool, thickness)


def _grown(box: Box, dx: float, dy: float) -> Box:
    return box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy


def _union(boxes: Sequence[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _meets(a: Box, b: Box) -> bool:
    """QRectF::intersects: the two share some area."""
    return rs._overlap(a, b)


def _holds(box: Box, point: tuple[float, float]) -> bool:
    """QRectF::contains for a point (edges included)."""
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


@dataclass(frozen=True)
class Geo:
    """A leaf item (a stroke) in scene coordinates: its bounds (Line::boundingRect), its
    points, its tolerance t, and the item it is (None for a new one)."""

    box: Box
    points: tuple[tuple[float, float], ...]
    t: float
    item: tuple[int, int] | None = None

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def center(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    @cached_property
    def polygon(self) -> tuple[tuple[float, float], ...]:
        return tuple(densify(self.points))


def leaf_geo(line: rs.LineValue, offset: tuple[float, float] = (0.0, 0.0),
             item: tuple[int, int] | None = None) -> Geo | None:
    """The geometry of a stroke drawn at `offset` (its group's anchor position): its
    points and bounds moved by it. None for a line without points."""
    box = rs.line_bounds(line)
    if box is None:
        return None
    dx, dy = offset
    points = tuple((p.x + dx, p.y + dy) for p in line.unpacked())
    return Geo((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy), points,
               tolerance(line.tool, line.thickness_scale), item)


def is_small(g: Geo) -> bool:
    return g.width < SMALL and g.height < SMALL


def radius(g: Geo) -> float:
    """r = max(width, height) / (pi / 2) + t (0x140a5a1a0)."""
    return max(g.width, g.height) / HALF_PI + g.t


def densify(points: Sequence[tuple[float, float]], step: float = STEP) -> list[tuple[float, float]]:
    """The points with each segment longer than `step` split every `step` (0x140a59b90)."""
    out: list[tuple[float, float]] = []
    for n, p in enumerate(points):
        if n:
            q = points[n - 1]
            length = math.hypot(p[0] - q[0], p[1] - q[1])
            k = 1
            while k * step < length:
                part = k * step / length
                out.append((q[0] + (p[0] - q[0]) * part, q[1] + (p[1] - q[1]) * part))
                k += 1
        out.append(p)
    return out


def is_closed(polygon: Sequence[tuple[float, float]]) -> bool:
    """A polygon is closed when its length is at least 10 and the gap between its ends
    is under half of that length (0x140a5a370)."""
    if len(polygon) < 2:
        return False
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(polygon, polygon[1:]))
    if length < CLOSED_LENGTH:
        return False
    gap = math.hypot(polygon[-1][0] - polygon[0][0], polygon[-1][1] - polygon[0][1])
    return gap / length < CLOSED_GAP


def _inside(polygon: Sequence[tuple[float, float]], p: tuple[float, float]) -> bool:
    """Whether `p` lies in the polygon (even-odd, a ray to the right)."""
    x, y = p
    hit = False
    for (ax, ay), (bx, by) in zip(polygon, list(polygon[1:]) + [polygon[0]]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            hit = not hit
    return hit


def near(p: tuple[float, float], q: tuple[float, float], rp: float, rq: float, d: float) -> bool:
    """0x140a5a6a0: the distance is under 1e-6, or under (|dx| / distance + 1) x d + rp + rq."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    dist = math.hypot(dx, dy)
    if dist < NEAR_EPSILON:
        return True
    return dist < (abs(dx) / dist + 1.0) * d + rp + rq


def _leaf_within(a: Geo, b: Geo, d: float) -> bool:
    if is_small(a) and is_small(b) and near(a.center, b.center, radius(a), radius(b), d):
        return True
    pa, pb = a.polygon, b.polygon
    if len(pa) <= MAX_POINTS and len(pb) <= MAX_POINTS:
        for p in pa:
            for q in pb:
                if near(p, q, a.t, b.t, d):
                    return True
        for polygon, points in ((pb, pa), (pa, pb)):
            if is_closed(polygon) and 2 * sum(1 for p in points if _inside(polygon, p)) > len(points):
                return True
        return False
    small, other = (a, b) if len(a.points) <= len(b.points) else (b, a)
    box = _grown(other.box, 2 * d, d)
    return any(_holds(box, p) for p in small.points)


def _leaves(x) -> Sequence[Geo]:
    return [x] if isinstance(x, Geo) else x


def within(a, b, d: float) -> bool:
    """Whether two items are within `d` (0x140a57d70). Each is a Geo or a sequence of leaves
    (a group's): A's bounds grown by (2d, d) must meet B's, and some leaf pair, the first
    leaf's box grown by (2d, d) meeting the second's, must be near: both small and their
    centres `near`; or both of at most 256 points and some point pair `near` (with the
    leaves' tolerances), or more than half of one's points inside the other's polygon
    when that is closed; or, of more than 256 points, a point of the smaller in the
    other's grown box."""
    a, b = _leaves(a), _leaves(b)
    if not a or not b:
        return False
    if not _meets(_grown(_union([g.box for g in a]), 2 * d, d), _union([g.box for g in b])):
        return False
    for ga in a:
        reach = _grown(ga.box, 2 * d, d)
        for gb in b:
            if _meets(reach, gb.box) and _leaf_within(ga, gb, d):
                return True
    return False


def _reach(a: Geo, b: Geo, d: float) -> bool:
    """A cluster's test of two items (0x140a58e00): both small, `near` on their centres;
    else `within`."""
    if is_small(a) and is_small(b):
        return near(a.center, b.center, radius(a), radius(b), d)
    return within(a, b, d)


def cluster(items: Sequence[Geo], d: float) -> list[list[int]]:
    """The clusters of `items` (0x140a58e00), as lists of indexes into `items`: the items
    taken by the bottom of their bounds; each joins every cluster whose bounds grown by
    (2d, d) meet its own and that holds an item within reach; several such clusters merge
    into the first; none makes a new one. In creation order, each cluster's items in
    their original order."""
    clusters: list[list] = []                   # [bounds, members]
    for i in sorted(range(len(items)), key=lambda n: items[n].box[3]):
        g = items[i]
        hits = [c for c in clusters
                if _meets(_grown(c[0], 2 * d, d), g.box) and any(_reach(items[j], g, d) for j in c[1])]
        if not hits:
            clusters.append([g.box, [i]])
            continue
        first = hits[0]
        for other in hits[1:]:
            first[1].extend(other[1])
            first[0] = _union([first[0], other[0]])
            clusters[:] = [c for c in clusters if c is not other]
        first[1].append(i)
        first[0] = _union([first[0], g.box])
    return [sorted(c[1]) for c in clusters]


def gravity(items: Sequence[Geo], text_rect: Box) -> tuple[float, float]:
    """The mean of every point of `items` (scene coordinates) weighted
    w = max(0.01, (100 - h) / 100)^2, h = how far the point's x lies outside the text
    box widened by 50 each side (0 inside); the text box's centre without points
    (0x140a58a30). `text_rect`: left, top, right, bottom."""
    left, top, right, bottom = text_rect
    total = sx = sy = 0.0
    for g in items:
        for x, y in g.points:
            h = max(left - GRAVITY_REACH - x, x - (right + GRAVITY_REACH), 0.0)
            weight = max(GRAVITY_FLOOR, (100.0 - h) / 100.0) ** 2
            total += weight
            sx += x * weight
            sy += y * weight
    if total == 0.0:
        return (left + right) / 2, (top + bottom) / 2
    return sx / total, sy / total


# ------------------------------------------------------------------ the text

def default_measure() -> Callable:
    """The text measure the layout uses: the app's Qt fonts when a QGuiApplication exists,
    else the estimates (renderer.estimate_width)."""
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:
        return renderer.estimate_width
    return renderer.qt_measure if QGuiApplication.instance() is not None else renderer.estimate_width


def body_line_height(measure: Callable) -> float:
    """LH (text+0x4e8, text ctor 0x1409247b0): the body font's line height rounded up (38),
    plus the line distance 0.2 x the font's height (7.55), plus the top margin (24):
    69.55 with the app's fonts."""
    font = renderer.paragraph_font(ParagraphStyle.PARAGRAPH)
    metrics = renderer.qt_metrics if getattr(measure, "qt_document", False) else renderer.estimate_metrics
    height = metrics(font)[1]
    return math.ceil(height) + renderer.LINE_DISTANCE * height + renderer.BODY.top


class TextLayout:
    """A page's typed text laid out as 3.29 lays it out (renderer.layout_text), with what
    anchoring asks of it, all in scene coordinates."""

    def __init__(self, scene: PageScene, lines: list, measure: Callable, ids: dict[int, tuple[int, int]]) -> None:
        self.scene, self.lines, self.measure = scene, lines, measure
        self.block: TextBlock = scene.text
        self.x_origin = scene.x_origin
        self.length = renderer.text_length(self.block)
        self.ids = ids
        self.width = self.block.width or renderer.NO_WIDTH
        bottom = (lines[-1].bottom if lines else self.block.y) + renderer.DOCUMENT_MARGIN
        # The text box: left, top, right, bottom.
        self.rect: Box = (self.block.x, self.block.y, self.block.x + self.width, bottom)
        self.line_height = body_line_height(measure)

    # -- where a point is in the text

    def index_at(self, point: tuple[float, float]) -> int:
        """0x140974950: y above the text box gives -1 (the before-text sentinel), below
        it the text's length (the after-text one), else the hit-tested index (0x14093a750)
        clamped to length - 1."""
        x, y = point
        if y < self.rect[1]:
            return -1
        if y > self.rect[3]:
            return self.length
        if not self.lines:
            return min(0, self.length - 1)
        return min(renderer.index_at(self.lines, x + self.x_origin, y, self.measure), self.length - 1)

    def anchor_id(self, index: int) -> tuple[int, int]:
        """The anchorId of a text index: a sentinel for -1 and the length, else the id
        of the character there."""
        if index < 0:
            return BEFORE_TEXT
        if index >= self.length:
            return AFTER_TEXT
        while index >= 0 and index not in self.ids:
            index -= 1
        return self.ids.get(index, BEFORE_TEXT)

    def position(self, index: int) -> tuple[float, float]:
        """The anchor position of an index clamped to 0..length - 1: x of the character on
        its line, y the baseline of the visual line holding it (0x14097c4f0)."""
        if not self.lines:
            return self.block.x + renderer.DOCUMENT_MARGIN, self.block.y
        return renderer.index_point(self.scene, self.lines, max(0, min(index, self.length - 1)), self.measure)

    def group_position(self, anchor: "NodeAnchor") -> tuple[float, float]:
        """Where a group anchored so is drawn: type 2 at its stored originX, type 1 at the
        character's x; an id the text does not hold is drawn at offset (0, 0), and a type 2
        originX stamped 0:0 takes the character's x (renderer.stroke_offsets, 0x14097c4f0)."""
        point = renderer.anchor_point(self.scene, self.lines, anchor.char_id, self.measure)
        if point is None:
            return 0.0, 0.0
        if anchor.kind == BIDIRECTIONAL:
            return point
        return (anchor.origin_x if anchor.origin_set else point[0]), point[1]

    # -- over the text

    def line_rects(self, box: Box) -> list[list[float]]:
        """The line rectangles `box` is tested against (0x1409741d0): the box grown by LH/3
        up and down; from the line at its top-left index on, each line's rectangle until a
        line's top is below the grown bottom; each bottom set to the next one's top, the
        first top 10 up, the last bottom 10 down."""
        if not self.lines:
            return []
        reach = self.line_height / 3
        grown = (box[0], box[1] - reach, box[2], box[3] + reach)
        start = renderer.line_of(self.lines, max(0, self.index_at((grown[0], grown[1]))))
        right = self.block.x + self.width - renderer.DOCUMENT_MARGIN
        rects: list[list[float]] = []
        for line in self.lines[start:]:
            if line.top > grown[3]:
                break
            rects.append([line.x - self.x_origin, line.top, right, line.bottom])
        for k in range(len(rects) - 1):
            rects[k][3] = rects[k + 1][1]
        if rects:
            rects[0][1] -= LINE_PADDING
            rects[-1][3] += LINE_PADDING
        return rects

    def over_text(self, g: Geo) -> bool:
        """Whether a stroke is over text: one of its points lies in a line rectangle that
        meets its (ungrown) bounds."""
        for rect in self.line_rects(g.box):
            box = (rect[0], rect[1], rect[2], rect[3])
            if _meets(box, g.box) and any(_holds(box, p) for p in g.points):
                return True
        return False


_LAYOUTS: dict = {}


def text_layout(blocks: list[w.Block], measure: Callable | None = None) -> TextLayout | None:
    """The text layout of a page's blocks (None: no RootText block, or one that cannot be
    read). Kept by the text's bytes: the same text is laid out once."""
    root = next((b for b in blocks if b.type == w.BLOCK_ROOT_TEXT), None)
    if root is None:
        return None
    measure = measure or default_measure()
    authors = blocks[0].payload if blocks and blocks[0].type == w.BLOCK_AUTHORS else b""
    paper = DEFAULT_PAPER
    for block in blocks:
        if block.type == w.BLOCK_SCENE_INFO:
            paper = paper_size_of(_read_fields(block.payload))
            break
    key = (root.payload, authors, tuple(paper), measure)
    found = _LAYOUTS.get(key)
    if found is None:
        try:
            names = _decode_authors(authors) if authors else {}
            text = text_block(decode_root_text(root.payload), names)
            scene = PageScene(version=6, x_origin=paper[0] / 2, width=paper[0], height=paper[1], text=text)
            ids: dict[int, tuple[int, int]] = {}
            at = 0
            for c in text_order(text.root.items, names):
                if c.char is not None and c.fmt is None:
                    ids.setdefault(at, c.id)
                    at += 2 if ord(c.char) > 0xFFFF else 1
            found = TextLayout(scene, renderer.layout_text(scene, measure), measure, ids)
        except (RmParseError, ValueError, KeyError, IndexError, struct.error):
            return None
        if len(_LAYOUTS) >= 16:
            _LAYOUTS.clear()
        _LAYOUTS[key] = found
    return found


def index_at(layout: TextLayout, point: tuple[float, float]) -> int:
    """0x140974950 (TextLayout.index_at)."""
    return layout.index_at(point)


def over_text(layout: TextLayout, g: Geo) -> bool:
    """0x1409741d0 (TextLayout.over_text)."""
    return layout.over_text(g)


def decide_kind(layout: TextLayout, geos: Sequence[Geo], mode: int = 0) -> tuple[int, float]:
    """(anchor type, distance d) of new items (0x14096ca70): mode 0 becomes 2 when an
    item's bounds are wider and taller than 178.74015748; mode 2 is Vertical with
    d = 35.748032; else Bidirectional (type 1, d = 0) when some item is over text, else
    Vertical."""
    if mode == 0 and any(g.width > LARGE and g.height > LARGE for g in geos):
        mode = 2
    if mode == 2:
        return VERTICAL, THRESHOLD
    if any(layout.over_text(g) for g in geos):
        return BIDIRECTIONAL, 0.0
    return VERTICAL, THRESHOLD


# ------------------------------------------------------------------ the page's groups

@dataclass(frozen=True)
class NodeAnchor:
    """The anchor fields of a SceneTreeNode (7 anchorId, 8 type, 9 threshold, 10 originX)."""

    char_id: tuple[int, int]
    kind: int
    threshold: float
    origin_x: float
    origin_set: bool = field(default=True, compare=False)    # originX's timestamp is not 0:0

    @property
    def valid(self) -> bool:
        """A group with anchorId 0:0 or type 0 is no anchored group ("non-anchor group
        detected", 0x140971e20)."""
        return self.char_id != ZERO_ID and self.kind in (BIDIRECTIONAL, VERTICAL)


def read_anchors(blocks: list[w.Block]) -> dict[tuple[int, int], NodeAnchor]:
    """The anchor of every tree node that has one."""
    out = {}
    for block in blocks:
        if block.type != w.BLOCK_TREE_NODE:
            continue
        values = w.read_values(block.payload)
        node = next((v for i, k, v in values if i == 1 and k == w.TAG_ID), None)
        subs = {i: v for i, k, v in values if k == w.TAG_LENGTH4}
        if node is None or 7 not in subs:
            continue

        def value(index: int):
            return {j: (k, v) for j, k, v in w.read_values(subs[index])}.get(2) if index in subs else None

        char, kind, threshold, origin = value(7), value(8), value(9), value(10)
        if char is None or char[0] != w.TAG_ID:
            continue
        stamp = ({j: (k, v) for j, k, v in w.read_values(subs[10])}.get(1)) if 10 in subs else None
        out[node] = NodeAnchor(
            char[1], int(kind[1]) if kind is not None and kind[0] == w.TAG_BYTE1 else 0,
            struct.unpack("<f", threshold[1])[0] if threshold is not None and threshold[0] == w.TAG_BYTE4 else 0.0,
            struct.unpack("<f", origin[1])[0] if origin is not None and origin[0] == w.TAG_BYTE4 else 0.0,
            stamp is not None and stamp[0] == w.TAG_ID and tuple(stamp[1]) != (0, 0))
    return out


@dataclass(frozen=True)
class Leaf:
    """A live stroke of a node: its id, its node, its value, where its node draws it
    (the anchor position) and its geometry."""

    item: tuple[int, int]
    node: tuple[int, int]
    line: rs.LineValue
    offset: tuple[float, float]
    geo: Geo


@dataclass
class Group:
    """An anchored group directly on the layer: its anchor, where it is drawn, its strokes
    at any depth, and how many live items it holds."""

    node: tuple[int, int]
    char: tuple[int, int]
    kind: int
    threshold: float
    origin_x: float
    position: tuple[float, float]
    leaves: list[Leaf]
    children: int

    def geos(self) -> list[Geo]:
        return [leaf.geo for leaf in self.leaves]


@dataclass
class View:
    layer: tuple[int, int]
    groups: list[Group]
    loose: list[Leaf]


def _live_items(blocks: list[w.Block], node: tuple[int, int]) -> list[tuple[w.ItemHeader, w.Block]]:
    out, seen = [], set()
    for _, block in w.sequence(blocks, node):
        if id(block) in seen:
            continue
        seen.add(id(block))
        item = w.decode_item(block.payload)
        if item.value is not None and not item.deleted_length:
            out.append((item, block))
    return out


def read_view(blocks: list[w.Block], layer: tuple[int, int], layout: TextLayout) -> View:
    """The anchored groups directly on `layer` (live group items whose node is attached),
    with their strokes in scene coordinates, and the layer's own loose strokes."""
    anchors = read_anchors(blocks)
    nodes = rs.group_nodes(blocks, layer)
    parent = dict(nodes)
    positions = {node: layout.group_position(anchors[node]) for node, _ in nodes
                 if node in anchors and anchors[node].valid}

    def offset(node: tuple[int, int]) -> tuple[float, float]:
        seen = set()
        while node is not None and node not in seen:
            if node in positions:
                return positions[node]
            seen.add(node)
            node = parent.get(node)
        return 0.0, 0.0

    live = {node: _live_items(blocks, node) for node in [layer] + [n for n, _ in nodes]}

    def leaves(node: tuple[int, int]) -> list[Leaf]:
        out, where = [], offset(node)
        for item, block in live[node]:
            if block.type == w.BLOCK_LINE_ITEM and block.version >= 2:
                line = rs.decode_line_value(item.value)
                geo = leaf_geo(line, where, item.item)
                if geo is not None:
                    out.append(Leaf(item.item, node, line, where, geo))
        return out

    top: dict[tuple[int, int], tuple[int, int]] = {}
    groups: list[Group] = []
    for node, up in nodes:
        top[node] = node if up == layer else top.get(up, up)
        if up == layer and node in positions:
            a = anchors[node]
            groups.append(Group(node, a.char_id, a.kind, a.threshold, a.origin_x, positions[node], [],
                                len(live[node])))
    by_node = {g.node: g for g in groups}
    for node, _ in nodes:
        if top[node] in by_node:
            by_node[top[node]].leaves.extend(leaves(node))
    return View(layer, groups, leaves(layer))


# ------------------------------------------------------------------ the actions, as page writers

def stamp_anchor(page: bytes, author: bytes, node: tuple[int, int], *, char_id: tuple[int, int] | None = None,
                 origin_x: float | None = None) -> bytes:
    """`page` with the node's anchorId (field 7) and/or originX (field 10) set, each
    stamped with the page's next id (SetAnchorIdAction stamps the anchorId, then the
    originX; the other anchor fields stay as they are)."""
    header, blocks = w.split(page)
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    at = rm_layer._node_block_index(blocks, tuple(node))
    if at is None:
        raise RmParseError(f"the page has no node {node}")
    values = w.read_values(blocks[at].payload)
    if char_id is not None:
        clock += 1
        values = rm_layer._set_lww_field(values, 7, w.f_id(1, (me, clock)) + w.f_id(2, tuple(char_id)))
    if origin_x is not None:
        clock += 1
        values = rm_layer._set_lww_field(values, 10, w.f_id(1, (me, clock)) + w.f_f32(2, float(origin_x)))
    old = blocks[at]
    blocks[at] = w.Block(old.type, old.min_version, old.version, w.write_values(values))
    return w.join(header, blocks)


def remove_anchor(page: bytes, author: bytes, node: tuple[int, int]) -> bytes:
    """RemoveAnchorAction: the node's anchorId becomes 0:0 (a group that is no anchored group)."""
    return stamp_anchor(page, author, node, char_id=ZERO_ID)


def _tree_move(page: bytes, author: bytes, node: tuple[int, int], is_update: int, parent: tuple[int, int]) -> bytes:
    header, blocks = w.split(page)
    me = w._author_index(blocks, author)
    move = w.new_block(w.BLOCK_TREE_MOVE,
                       w.encode_tree_move((me, w.highest_counter(blocks) + 1), tuple(node), is_update, tuple(parent)))
    rm_layer._insert_tree_move(blocks, move)
    return w.join(header, blocks)


def detach(page: bytes, author: bytes, node: tuple[int, int]) -> bytes:
    """SceneDeleteGroupActionCrdt: a SceneTreeMove with a new id for `node`, is_update 0,
    parent 0:0 (the group item stays live)."""
    return _tree_move(page, author, node, 0, ZERO_ID)


def attach(page: bytes, author: bytes, node: tuple[int, int], parent: tuple[int, int]) -> bytes:
    """A detached node put back under `parent` (is_update 1), as undo of a group's delete does."""
    return _tree_move(page, author, node, 1, parent)


def rewrite_values(page: bytes, values: dict[tuple[int, int], bytes]) -> bytes:
    """`page` with the live items `values` (id -> value bytes) rewritten in place: same id,
    same place, new value."""
    header, blocks = w.split(page)
    out = []
    for block in blocks:
        if block.type in w.ITEM_BLOCKS:
            item = w.decode_item(block.payload)
            if item.item in values and item.value is not None and not item.deleted_length:
                block = w.Block(block.type, block.min_version, block.version,
                                replace(item, value=values[item.item]).encode())
        out.append(block)
    return w.join(header, out)


def add_items(page: bytes, author: bytes, node: tuple[int, int],
              values: Sequence[rs.LineValue]) -> tuple[bytes, list[tuple[int, int]]]:
    """(`page` with line values added to `node`, their ids): each the next id, appended in
    order (left = the node's last id, right 0:0), the moveId as it is (a clone keeps the
    original's); a shader piece's sourceId is renamed first (0x1408c2ae0)."""
    header, blocks = w.split(page)
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    chain = rm_select._Node(blocks, tuple(node))
    values, clock = rm_select._fresh_sources(list(values), me, clock)
    ids = []
    for value in values:
        clock += 1
        chain.append((me, clock), value.encode(), rm_select._kind(w.BLOCK_LINE_ITEM))
        ids.append((me, clock))
    return w.join(header, chain.write(blocks)), ids


def set_node(page: bytes, *, author: bytes, node: tuple[int, int], char_id: tuple[int, int] | None = None,
             origin_x: float | None = None, attached: bool | None = None, parent: tuple[int, int] | None = None,
             values: dict[tuple[int, int], bytes] | None = None) -> bytes:
    """`page` with an anchored node brought to a state (what undo and redo write): its
    anchorId, items rewritten in place, originX, and whether it hangs under `parent`
    (a SceneTreeMove each way). What the node has already is not written again."""
    node = tuple(node)
    anchor = read_anchors(w.split(page)[1]).get(node)
    if anchor is None:
        return page
    if char_id is not None and tuple(char_id) != anchor.char_id:
        page = stamp_anchor(page, author, node, char_id=char_id)
    if values:
        page = rewrite_values(page, values)
    if origin_x is not None and rs.f32(origin_x) != rs.f32(anchor.origin_x):
        page = stamp_anchor(page, author, node, origin_x=origin_x)
    if attached is not None:
        now = w.tree_parents(w.split(page)[1]).get(node, ZERO_ID) != ZERO_ID
        if attached and not now and parent is not None:
            page = attach(page, author, node, parent)
        elif not attached and now:
            page = detach(page, author, node)
    return page


def moved(line: rs.LineValue, dx: float, dy: float) -> rs.LineValue:
    """`line` moved by (dx, dy), as Line::transform moves it (float32 points, nothing else changed)."""
    return rm_select.transform_line(line, rm_select.translation(dx, dy))


def _clone(leaf: Leaf, shift: tuple[float, float]) -> rs.LineValue:
    """A clone of a stroke moved by `shift`, its moveId the original's, or the original's
    id when it has none (0x1409b2ab0)."""
    return rm_select._stamped(moved(leaf.line, *shift), leaf.item)


def _reanchor(page: bytes, author: bytes, group: Group, char: tuple[int, int], q: tuple[float, float],
              always: bool = False) -> tuple[bytes, tuple[float, float]]:
    """SetAnchorIdAction (vtable 0x140d6c728, redo 0x1409773b0): the anchorId stamped with
    the next id, every item of the group moved in place (same ids) by old position - Q when
    that is not zero, then originX = Q.x with the next id. The join path of anchorItems
    (0x140977bf0) makes the action unconditionally (`always`: a type 2 group gets its
    originX reset to the character's x even with the same anchorId); the eraser's regroup
    (0x140976450) skips it when the anchorId is the same. (page, where the group is drawn)."""
    if char == group.char and not always:
        return page, group.position
    page = stamp_anchor(page, author, group.node, char_id=char)
    shift = (group.position[0] - q[0], group.position[1] - q[1])
    if shift != (0.0, 0.0):
        page = rewrite_values(page, {leaf.item: moved(leaf.line, *shift).encode() for leaf in group.leaves})
    return stamp_anchor(page, author, group.node, origin_x=q[0]), q


# ------------------------------------------------------------------ a new stroke

@dataclass
class Placement:
    """What anchoring a stroke did: the page, the ids of the new items, the group nodes it
    made, and the ids of the clones it made (merged, absorbed and re-anchored items)."""

    page: bytes
    ids: list[tuple[int, int]]
    nodes: list[tuple[int, int]]
    clones: list[tuple[int, int]]


def place(page: bytes, *, author: bytes, layer: tuple[int, int], values: Sequence[rs.LineValue], mode: int = 0,
          no_merge: bool = False, measure: Callable | None = None) -> Placement | None:
    """`page` with the line values `values` (scene coordinates) anchored to its typed text,
    as anchorItems (0x14096ca70) does it; None for a page without text. In 3.29's order:
    the new group (createGroup) or the kept joined group re-anchored; the other joined
    groups merged into it and detached; the loose clusters near the new items absorbed;
    every other loose cluster into a group of its own; last the new items at -P."""
    blocks = w.split(page)[1]
    layout = text_layout(blocks, measure)
    if layout is None:
        return None
    layer = tuple(layer)
    view = read_view(blocks, layer, layout)
    news = [g for g in (leaf_geo(v) for v in values) if g is not None]
    kind, d = decide_kind(layout, news, mode)
    joined: list[Group] = []
    absorbed: list[list[Leaf]] = []
    rest: list[list[Leaf]] = []
    if not no_merge:
        for group in view.groups:
            if group.kind == kind and any(within(n, group.geos(), min(d, group.threshold)) for n in news):
                joined.append(group)
        for members in cluster([leaf.geo for leaf in view.loose], d):
            leaves = [view.loose[i] for i in members]
            near_new = any(within([leaf.geo for leaf in leaves], n, d) for n in news)
            (absorbed if near_new else rest).append(leaves)
    every = list(news) + [leaf.geo for leaves in absorbed for leaf in leaves]
    for group in joined:
        every += group.geos()
    index = layout.index_at(gravity(every, layout.rect))
    char, anchor_at = layout.anchor_id(index), layout.position(index)

    nodes: list[tuple[int, int]] = []
    clones: list[tuple[int, int]] = []
    if not joined:
        page, node = rm_anchor.add_group(page, author=author, layer=layer, char_id=char, origin_x=anchor_at[0],
                                         kind=kind, threshold=d)
        nodes.append(node)
        where = anchor_at
    else:
        kept = max(joined, key=lambda g: g.children)        # the most children, the first on ties
        node = kept.node
        page, where = _reanchor(page, author, kept, char, anchor_at, always=True)
        for other in joined:
            if other is kept:
                continue
            page = remove_anchor(page, author, other.node)
            shift = [(leaf.offset[0] - where[0], leaf.offset[1] - where[1]) for leaf in other.leaves]
            page, made = add_items(page, author, node, [_clone(leaf, s) for leaf, s in zip(other.leaves, shift)])
            clones += made
            page = detach(rs.delete_strokes(page, [leaf.item for leaf in other.leaves]), author, other.node)
    for leaves in absorbed:
        page = rs.delete_strokes(page, [leaf.item for leaf in leaves])
        page, made = add_items(page, author, node, [_clone(leaf, (-where[0], -where[1])) for leaf in leaves])
        clones += made
    for leaves in rest:
        geos = [leaf.geo for leaf in leaves]
        group_kind, group_d = decide_kind(layout, geos, 0)
        at = layout.index_at(gravity(geos, layout.rect))
        there = layout.position(at)
        page, made_node = rm_anchor.add_group(page, author=author, layer=layer, char_id=layout.anchor_id(at),
                                              origin_x=there[0], kind=group_kind, threshold=group_d)
        nodes.append(made_node)
        page = rs.delete_strokes(page, [leaf.item for leaf in leaves])
        page, made = add_items(page, author, made_node, [_clone(leaf, (-there[0], -there[1])) for leaf in leaves])
        clones += made
    page, ids = add_items(page, author, node, [moved(v, -where[0], -where[1]) for v in values])
    return Placement(page, ids, nodes, clones)


@dataclass
class Drawn:
    """Strokes drawn on a page with text: the page, the id of each stroke, and what each placed."""

    page: bytes
    ids: list[tuple[int, int]]
    placements: list[Placement]


def place_strokes(page: bytes | None, *, author: bytes, lines: Sequence[rs.LineValue],
                  layer: tuple[int, int] | None = None, measure: Callable | None = None) -> Drawn | None:
    """`page` with `lines` drawn on it one after the other, each placed as its own action
    (`place`); None when the page has no typed text (or none this client can read), which
    is then drawn on as on any page (rm_stroke.append_strokes)."""
    if page is None:
        return None
    blocks = w.split(page)[1]
    if not rs.has_text(blocks) or text_layout(blocks, measure) is None:
        return None
    layer = rs._layer(blocks, layer)
    ids, placements = [], []
    try:
        for line in lines:
            placed = place(page, author=author, layer=layer, values=[line], measure=measure)
            if placed is None:
                return None
            page = placed.page
            ids.append(placed.ids[0])
            placements.append(placed)
    except RmParseError:
        return None
    return Drawn(page, ids, placements)


# ------------------------------------------------------------------ after an erase

@dataclass
class Regrouped:
    """What the eraser's regroup did: the page, the group nodes it made, the clones it made."""

    page: bytes
    nodes: list[tuple[int, int]]
    clones: list[tuple[int, int]]


def regroup(page: bytes, *, author: bytes, layer: tuple[int, int], nodes: Sequence[tuple[int, int]],
            measure: Callable | None = None) -> Regrouped | None:
    """What 3.29 does to each anchored group an eraser touched (0x140979810): its items
    are clustered with d (type 2: 35.748032, type 1: 0); the cluster with the fewest items
    (the first on ties) stays; the items of all others are deleted in one action, then
    each is cloned into scene coordinates and anchored afresh with noMerge (a new group
    each, `place`). A group with an item left is re-anchored to the gravity of what it
    holds (0x140976450, SetAnchorIdAction when the anchorId changes); an empty one is
    detached (SceneDeleteGroupActionCrdt). None for a page without text."""
    layout = text_layout(w.split(page)[1], measure)
    if layout is None:
        return None
    layer = tuple(layer)
    made_nodes: list[tuple[int, int]] = []
    made_clones: list[tuple[int, int]] = []
    for node in nodes:
        group = next((g for g in read_view(w.split(page)[1], layer, layout).groups if g.node == tuple(node)), None)
        if group is None:
            continue
        if not group.leaves:
            if not group.children:                  # nothing live in it: it leaves the manager, detached
                page = detach(page, author, group.node)
            continue
        clusters = cluster(group.geos(), THRESHOLD if group.kind == VERTICAL else 0.0)
        if len(clusters) > 1:
            stay = min(range(len(clusters)), key=lambda k: len(clusters[k]))
            others = [members for k, members in enumerate(clusters) if k != stay]
            page = rs.delete_strokes(page, [group.leaves[i].item for members in others for i in members])
            for members in others:
                placed = place(page, author=author, layer=layer, no_merge=True, measure=measure,
                               values=[_clone(group.leaves[i], group.leaves[i].offset) for i in members])
                page = placed.page
                made_nodes += placed.nodes
                made_clones += placed.ids
            group = next((g for g in read_view(w.split(page)[1], layer, layout).groups if g.node == group.node), None)
            if group is None or not group.leaves:
                continue
        index = layout.index_at(gravity(group.geos(), layout.rect))
        page, _ = _reanchor(page, author, group, layout.anchor_id(index), layout.position(index))
    return Regrouped(page, made_nodes, made_clones)


# ------------------------------------------------------------------ what an action changed

@dataclass(frozen=True)
class NodeState:
    """An anchored node as a page has it: its anchor character, originX, and whether it
    hangs under a parent."""

    char: tuple[int, int]
    origin_x: float
    attached: bool


@dataclass
class Delta:
    """What an action did to the strokes and anchored nodes of a page: strokes added and
    deleted (id, parent, value), strokes rewritten in place (id, parent, old, new), nodes
    made or changed (node, its parent while attached, old state or None, new state)."""

    added: list[tuple[tuple[int, int], tuple[int, int], bytes]]
    deleted: list[tuple[tuple[int, int], tuple[int, int], bytes]]
    moved: list[tuple[tuple[int, int], tuple[int, int], bytes, bytes]]
    nodes: list[tuple[tuple[int, int], tuple[int, int], NodeState | None, NodeState]]


def _live_lines(blocks: list[w.Block]) -> dict[tuple[int, int], tuple[tuple[int, int], bytes]]:
    out = {}
    for block in blocks:
        if block.type == w.BLOCK_LINE_ITEM and block.version >= 2:
            item = w.decode_item(block.payload)
            if item.value is not None and not item.deleted_length:
                out[item.item] = (item.parent, item.value)
    return out


def _node_states(blocks: list[w.Block]) -> dict[tuple[int, int], tuple[NodeState, tuple[int, int]]]:
    parents = w.tree_parents(blocks)
    return {node: (NodeState(a.char_id, a.origin_x, parents.get(node, ZERO_ID) != ZERO_ID),
                   parents.get(node, ZERO_ID))
            for node, a in read_anchors(blocks).items()}


def delta(before: bytes, after: bytes) -> Delta:
    """What happened to the strokes and anchored nodes between two states of a page."""
    old, new = w.split(before)[1], w.split(after)[1]
    lines_before, lines_after = _live_lines(old), _live_lines(new)
    key = lambda item: w.id_key(item[0])        # noqa: E731
    added = sorted(((i, p, v) for i, (p, v) in lines_after.items() if i not in lines_before), key=key)
    deleted = sorted(((i, p, v) for i, (p, v) in lines_before.items() if i not in lines_after), key=key)
    moved_items = sorted(((i, lines_after[i][0], lines_before[i][1], lines_after[i][1]) for i in lines_after
                          if i in lines_before and lines_after[i][1] != lines_before[i][1]), key=key)
    states_before, states_after = _node_states(old), _node_states(new)
    nodes = []
    for node in sorted(states_after, key=w.id_key):
        state, parent = states_after[node]
        was = states_before.get(node)
        if was is None or was[0] != state:
            nodes.append((node, parent if parent != ZERO_ID else (was[1] if was else ZERO_ID),
                          was[0] if was else None, state))
    return Delta(added, deleted, moved_items, nodes)
