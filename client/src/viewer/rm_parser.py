"""Parser for reMarkable `.rm` page files (lines format v3, v5 and v6).

Every file starts with a 43-byte ASCII header, space padded:

    b"reMarkable .lines file, version=6          "

v3 / v5: flat coordinate streams (little endian)
    i32 layer_count
      i32 stroke_count
        i32 tool, i32 color, i32 (unused), f32 base_width, [v5: i32 (unused)]
        i32 point_count
          f32 x, y, speed, direction, width, pressure

v6: a sequence of blocks, each framed as
        u32 payload_length, u8 0, u8 min_version, u8 version, u8 block_type
    The payload is a CRDT scene tree encoded as tagged values: varuint tag =
    (field_index << 4) | value_type, value types 0x1 u8, 0x4 32-bit, 0x8 f64,
    0xC u32-length sub-block, 0xF CrdtId (u8 author + varuint counter).

    Block types and names come from reMarkable.exe's own table (BLOCK_NAMES).
    Layers are TreeNode blocks (0x02) attached under the root node (0:1) by
    group items (0x04); strokes are line items (0x05) whose parent is the layer;
    typed text is a RootText block (0x07, see decode_root_text); inserted images
    are CrdtImageItem (0x0F) plus a SceneImagesTable (0x0E). Line points are 14 bytes in block version 2
    (f32 x, f32 y, u16 speed*4, u16 width*4, u8 direction*255/2pi, u8 pressure*255)
    and six f32 (24 bytes) in version 1.

v6 x coordinates are centred on the page (x = 0 is the vertical centre line);
v3/v5 x starts at the left edge. Point values are normalised to the v5 units
(pixels, radians, pressure 0..1) so both versions render the same way.

Tool and colour IDs come from the enum name tables in reMarkable.exe
(ARCHITECTURE.md §5).
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

log = logging.getLogger("rm.viewer.parser")

HEADER_LENGTH = 43
HEADER_PREFIX = b"reMarkable .lines file, version="

PAGE_WIDTH = 1404
PAGE_HEIGHT = 1872

ROOT_NODE = (0, 1)


class RmParseError(ValueError):
    """The data is not a readable .rm file."""


class Tool(IntEnum):
    PAINTBRUSH = 0
    PENCIL = 1
    BALLPOINT = 2
    MARKER = 3
    FINELINER = 4
    HIGHLIGHTER = 5
    ERASER = 6
    SHARP_PENCIL = 7          # "Mechanical pencil" in the UI
    ERASE_SECTION = 8
    CLEAR_PAGE = 9
    ZOOM_TOOL = 10
    SELECTION_TOOL = 11
    PAINTBRUSH_V2 = 12
    SHARP_PENCIL_V2 = 13
    PENCIL_V2 = 14
    BALLPOINT_V2 = 15
    MARKER_V2 = 16
    FINELINER_V2 = 17
    HIGHLIGHTER_V2 = 18
    SOLID_PEN = 19
    RESERVED_PEN = 20
    CALLIGRAPHY = 21
    MASKED_ERASER = 22
    SHADING_MARKER = 23
    UNKNOWN = -1

    @classmethod
    def parse(cls, value: int) -> "Tool":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def family(self) -> str:
        """Tool family regardless of v1/v2 variant, e.g. "fineliner"."""
        return _TOOL_FAMILY.get(self, "unknown")


_TOOL_FAMILY = {
    Tool.PAINTBRUSH: "paintbrush", Tool.PAINTBRUSH_V2: "paintbrush",
    Tool.PENCIL: "pencil", Tool.PENCIL_V2: "pencil",
    Tool.SHARP_PENCIL: "mechanical_pencil", Tool.SHARP_PENCIL_V2: "mechanical_pencil",
    Tool.BALLPOINT: "ballpoint", Tool.BALLPOINT_V2: "ballpoint",
    Tool.MARKER: "marker", Tool.MARKER_V2: "marker",
    Tool.FINELINER: "fineliner", Tool.FINELINER_V2: "fineliner",
    Tool.HIGHLIGHTER: "highlighter", Tool.HIGHLIGHTER_V2: "highlighter",
    Tool.SHADING_MARKER: "shader",
    Tool.CALLIGRAPHY: "calligraphy",
    Tool.SOLID_PEN: "solid", Tool.RESERVED_PEN: "solid",
    Tool.ERASER: "eraser", Tool.MASKED_ERASER: "eraser",
    Tool.ERASE_SECTION: "erase_section",
    Tool.CLEAR_PAGE: "other", Tool.ZOOM_TOOL: "other", Tool.SELECTION_TOOL: "other",
}


class Color(IntEnum):
    BLACK = 0
    GRAY = 1
    WHITE = 2
    HIGHLIGHTER_YELLOW = 3
    HIGHLIGHTER_GREEN = 4
    HIGHLIGHTER_PINK = 5
    BLUE = 6
    RED = 7
    HIGHLIGHTER_STACKED_GRAY = 8
    ARGB = 9                  # actual colour is Stroke.argb
    GREEN = 10
    CYAN = 11
    MAGENTA = 12
    YELLOW = 13
    HIGHLIGHTER_BLUE = 14
    HIGHLIGHTER_ORANGE = 15
    HIGHLIGHTER_GRAY = 16
    UNKNOWN = -1

    @classmethod
    def parse(cls, value: int) -> "Color":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    speed: float = 0.0
    direction: float = 0.0    # pen tilt direction, radians
    width: float = 0.0        # rendered width at this point, pixels
    pressure: float = 0.0     # 0..1

    @property
    def tilt(self) -> float:
        return self.direction


@dataclass(frozen=True)
class Anchor:
    """Positions a group's strokes relative to a line of typed text.

    Strokes in an anchored group store x relative to `origin_x` and y relative to
    the line holding character `char_id` of the page's RootText, so handwriting
    moves with the text it was written next to.
    """

    char_id: tuple[int, int]
    origin_x: float
    threshold: float = 0.0
    type: int = 0

    @property
    def is_special(self) -> bool:
        # (0, 2**48 - 2) / (0, 2**48 - 1): sentinels rather than a character.
        return self.char_id[0] == 0 and self.char_id[1] >= ANCHOR_SENTINEL_START


ANCHOR_SENTINEL_START = 2**48 - 2


@dataclass
class Stroke:
    tool: Tool
    color: Color
    points: list[Point]
    thickness_scale: float = 1.0   # v6 thicknessScale; v5 base brush width
    starting_length: float = 0.0
    argb: int | None = None        # 0xAARRGGBB when color is ARGB
    tool_id: int = 0               # raw values, kept for unknown enums
    color_id: int = 0
    item_id: tuple[int, int] | None = None
    anchor: Anchor | None = None   # from the nearest anchored group, if any
    seq: int = field(default=0, compare=False)    # v6 block position: stacking with images

    def bounds(self) -> tuple[float, float, float, float] | None:
        if not self.points:
            return None
        xs = [p.x for p in self.points]
        ys = [p.y for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Layer:
    name: str = ""
    visible: bool = True
    strokes: list[Stroke] = field(default_factory=list)
    node_id: tuple[int, int] | None = None
    images: list["ImageItem"] = field(default_factory=list)

    def items(self) -> list:
        """Strokes and images bottom to top, in the order the file lists them."""
        return sorted([*self.strokes, *self.images], key=lambda item: item.seq)


class ParagraphStyle(IntEnum):
    """Paragraph types as the app's text menu names them (scene::ParagraphStyle::Type;
    the value is the index into reMarkable.exe's name table). A file does not store
    these numbers: see StyleEntry."""

    TITLE = 0
    SUBHEADING_2 = 1
    PARAGRAPH = 2              # "Body text"; the default
    BULLETPOINT = 3
    BULLETPOINT_INDENTED = 4
    CHECKBOX_UNCHECKED = 5
    CHECKBOX_CHECKED = 6
    CHECKBOX_UNCHECKED_INDENTED = 7
    CHECKBOX_CHECKED_INDENTED = 8
    NUMBERED_LIST = 9
    NUMBERED_LIST_INDENTED = 10
    IMAGE_BLOCK = 11
    SUBHEADING = 12


# A paragraph's style in memory is {u8 base, u32 properties} (the first byte of the
# properties is the heading level):
#   base 1 nothing; 2 text, level 0 body, 1 title, 2 subheading 2, 3 subheading;
#        3 list, properties = indent | kind << 4 | state << 8, kind 0 bullet, 1 checkbox,
#          2 numbered, state 1 checked; 4 image.
STYLE_NONE, STYLE_TEXT, STYLE_LIST, STYLE_IMAGE = 1, 2, 3, 4


def _style_from_legacy(legacy: int) -> tuple[int, int]:
    """(base, properties) of a "style_legacy" byte (0x140a92600)."""
    if 1 <= legacy <= 3:
        return STYLE_TEXT, legacy - 1
    if 4 <= legacy <= 11:
        kind = 0 if legacy < 6 else 1 if legacy < 10 else 2
        return STYLE_LIST, (legacy in (5, 8, 9, 11)) | kind << 4 | (legacy in (7, 9)) << 8
    return (STYLE_IMAGE, 0) if legacy == 12 else (STYLE_NONE, 0)


def _style_to_legacy(base: int, properties: int) -> int:
    """The "style_legacy" byte of a style (0x140a93100)."""
    if base == STYLE_TEXT:
        return {1: 2, 2: 3, 3: 3}.get(properties & 0xFF, 1)
    if base == STYLE_LIST:
        indented, kind = bool(properties & 0xF), properties >> 4 & 0xF
        if kind == 1:
            return (7 if properties >> 8 & 7 == 1 else 6) + 2 * indented
        return (10 if kind == 2 else 4) + indented
    return 12 if base == STYLE_IMAGE else 0


# "style_legacy" -> Type (0x1408b6340); 0 (nothing) and 1 (body) are both Paragraph.
_LEGACY_TYPE = {2: ParagraphStyle.TITLE, 3: ParagraphStyle.SUBHEADING_2,
                **{legacy: ParagraphStyle(legacy - 1) for legacy in range(4, 13)}}


@dataclass(frozen=True)
class TextItem:
    """One entry of a RootText's character sequence, as the file stores it.

    An entry is a run of text typed in one go, a run of deleted characters, or one
    inline format marker. Its characters have the ids item_id, item_id + 1, ...: one
    per UTF-16 unit (the app counts in QString units), one for a marker,
    `deleted_length` for a deleted run. `left` and `right` are the ids the run was put
    between ((0, 0): the start, the end)."""

    item_id: tuple[int, int]
    left: tuple[int, int]
    right: tuple[int, int]
    deleted_length: int = 0
    data: bytes | None = None       # the string's bytes; None: the entry has no value
    utf_type: int = 1               # the byte after the string's length; the app reads only 1 (UTF-8)
    format: int = 0                 # FORMAT_*; 0: text
    value_rest: bytes = b""         # bytes of the value this parser does not know
    rest: bytes = b""               # bytes of the entry this parser does not know

    @property
    def text(self) -> str:
        if self.data is None or self.utf_type != 1:
            return ""
        return self.data.decode("utf-8", errors="replace")

    @property
    def kind(self) -> str:
        """"deleted", "format" or "text"."""
        return "deleted" if self.deleted_length or self.data is None else "format" if self.format else "text"

    @property
    def length(self) -> int:
        """How many ids the entry takes (0x1409164b0)."""
        if self.deleted_length:
            return self.deleted_length
        return 1 if self.format else len(self.text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class StyleEntry:
    """The style of one paragraph, as the file stores it: keyed by the id of the
    newline that starts the paragraph ((0, 0): the first paragraph), last write wins.

    `legacy` is the "style_legacy" byte every writer stores: 0 nothing, 1 body text,
    2 title, 3 subheading 2, 4 bullet, 5 indented bullet, 6 checkbox, 7 checked,
    8 indented checkbox, 9 indented and checked, 10 numbered, 11 indented numbered,
    12 image. `base_style` and `properties` are stored as well when the legacy byte
    cannot say it all (a "Subheading", level 3, and every indented list: 0x140a92b30),
    and then they are what the app reads."""

    char_id: tuple[int, int]
    timestamp: tuple[int, int]
    legacy: int | None = None
    base_style: int | None = None
    properties: int | None = None
    rest: bytes = b""               # bytes of the value this parser does not know

    @property
    def base(self) -> tuple[int, int]:
        """(base, properties) as the app holds the style (0x140917460)."""
        if self.base_style:
            return self.base_style, self.properties or 0
        return _style_from_legacy(self.legacy or 0)

    @property
    def style(self) -> ParagraphStyle:
        base, properties = self.base
        if base == STYLE_TEXT and properties & 0xFF == 3:
            return ParagraphStyle.SUBHEADING
        return _LEGACY_TYPE.get(_style_to_legacy(base, properties), ParagraphStyle.PARAGRAPH)


@dataclass
class RootText:
    """A RootText block (0x07) as stored: everything in it, nothing interpreted.
    `items` and `styles` are in the file's order."""

    block_id: tuple[int, int]
    items: list[TextItem]
    styles: list[StyleEntry]
    x: float
    y: float
    width_legacy: float                         # "textWidthLegacy"
    width: float | None = None                  # settings "textWidth"; newer files only
    width_timestamp: tuple[int, int] | None = None
    # Bytes this parser does not know, by where they are: "root", "text", "sequence",
    # "items", "formatting", "styles", "position", "settings", "width".
    unknown: dict[str, bytes] = field(default_factory=dict)

    @property
    def text_width(self) -> float:
        """The width in use: the newer one when it is set (0x140943e50)."""
        return self.width if self.width is not None and self.width > 0 else self.width_legacy


@dataclass
class TextSpan:
    text: str
    bold: bool = False
    italic: bool = False
    link: bool = False


@dataclass
class Paragraph:
    style: ParagraphStyle
    spans: list[TextSpan] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class TextBlock:
    """Typed text (RootText block): a box at (x, y) of the given width."""

    x: float
    y: float
    width: float
    paragraphs: list[Paragraph] = field(default_factory=list)
    # Every character id (including deleted ones and markers) -> paragraph index;
    # anchors refer to these. A newline belongs to the paragraph it ends.
    char_paragraph: dict[tuple[int, int], int] = field(default_factory=dict)
    root: RootText | None = field(default=None, repr=False, compare=False)    # as stored

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass
class ImageItem:
    """A live inserted image (CrdtImageItem, item type 7), decoded from a page the tablet
    wrote: its value is {1: imageRef, a last-write-wins 16-byte key into the page's
    SceneImagesTable}, {2: an id (boundsTimestamp)}, {3: vertices, x y u v per corner in
    scene units}, {4: triangle indices into them}, {5: moveId, the item it replaced when
    moved}. The table names the image file, stored with the document as
    "<doc>/<page>/<file_name>". `value` is the raw payload; a payload that cannot be
    decoded is kept but has no geometry and is not drawn."""

    item_id: tuple[int, int]
    parent_id: tuple[int, int]
    value: bytes = b""
    image_ref: bytes = b""
    file_name: str = ""
    vertices: list[tuple[float, float, float, float]] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    seq: int = field(default=0, compare=False)
    data: bytes | None = field(default=None, compare=False, repr=False)   # the file, attached by the loader

    def bounds(self) -> tuple[float, float, float, float] | None:
        if not self.vertices:
            return None
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class PageScene:
    version: int
    layers: list[Layer] = field(default_factory=list)
    # Added to raw x to get page coordinates (v6 is centred).
    x_origin: float = 0.0
    width: int = PAGE_WIDTH
    height: int = PAGE_HEIGHT
    text: TextBlock | None = None
    images: list[ImageItem] = field(default_factory=list)
    # Every v6 block seen, by block type.
    block_counts: dict[int, int] = field(default_factory=dict)
    # Blocks present but not interpreted (e.g. glyph highlights), by block type.
    skipped_blocks: dict[int, int] = field(default_factory=dict)

    @property
    def strokes(self) -> list[Stroke]:
        return [s for layer in self.layers for s in layer.strokes]

    @property
    def placed_images(self) -> list[ImageItem]:
        """The images drawn on the page: those in a layer, with geometry."""
        return [i for layer in self.layers for i in layer.images if i.vertices]

    def to_page(self, x: float, y: float) -> tuple[float, float]:
        return x + self.x_origin, y

    def content_bounds(self) -> tuple[float, float, float, float] | None:
        """Bounding box of all strokes and images in page coordinates."""
        boxes = [b for s in [*self.strokes, *self.placed_images] if (b := s.bounds())]
        if not boxes:
            return None
        return (min(b[0] for b in boxes) + self.x_origin, min(b[1] for b in boxes),
                max(b[2] for b in boxes) + self.x_origin, max(b[3] for b in boxes))


# ------------------------------------------------------------------ entry points

def parse_header(data: bytes) -> int:
    """Return the format version from the 43-byte header."""
    head = data[:HEADER_LENGTH]
    if len(head) < HEADER_LENGTH or not head.startswith(HEADER_PREFIX):
        raise RmParseError("not a reMarkable .lines file (bad header)")
    try:
        return int(head[len(HEADER_PREFIX):].decode("ascii").strip())
    except ValueError as err:
        raise RmParseError(f"bad version in header {head!r}") from err


def parse(data: bytes) -> PageScene:
    version = parse_header(data)
    if version in (3, 5):
        return _parse_v5(data, version)
    if version == 6:
        return _parse_v6(data)
    raise RmParseError(f"unsupported .lines version {version}")


def parse_file(path: str | Path) -> PageScene:
    return parse(Path(path).read_bytes())


# ------------------------------------------------------------------------ v3/v5

def _parse_v5(data: bytes, version: int) -> PageScene:
    r = _Reader(data, HEADER_LENGTH)
    layers = []
    for layer_index in range(r.i32()):
        layer = Layer(name=f"Layer {layer_index + 1}")
        for _ in range(r.i32()):
            tool_id, color_id, _unused, base_width = r.unpack("<iiif")
            if version == 5:
                r.i32()
            points = [Point(*r.unpack("<6f")) for _ in range(r.count(24))]
            layer.strokes.append(Stroke(
                tool=Tool.parse(tool_id), color=Color.parse(color_id), points=points,
                thickness_scale=base_width, tool_id=tool_id, color_id=color_id))
        layers.append(layer)
    if r.pos != len(data):
        raise RmParseError(f"{len(data) - r.pos} trailing bytes after v{version} data")
    return PageScene(version=version, layers=layers)


# --------------------------------------------------------------------------- v6

# Block type -> name, from the `const char*[16]` table reMarkable.exe uses to log
# blocks ("tag=%#2x(%s)"); indexed directly by block type.
BLOCK_NAMES = (
    "Conversion", "SceneTreeMove", "SceneTreeNode", "CrdtGlyphItem", "CrdtGroupItem",
    "CrdtLineItem", "CrdtTextItem", "RootText", "CrdtTombstoneItem", "Uuid2Index",
    "PageStats", "Array", "MigrationInfo", "SceneInfo", "SceneImagesTable", "CrdtImageItem",
)

BLOCK_SCENE_TREE = 0x01
BLOCK_TREE_NODE = 0x02
BLOCK_SCENE_GROUP = 0x04
BLOCK_SCENE_LINE = 0x05
BLOCK_ROOT_TEXT = 0x07
BLOCK_TOMBSTONE = 0x08
BLOCK_IMAGES_TABLE = 0x0E
BLOCK_IMAGE_ITEM = 0x0F

# Understood and not needed to draw the page: conversion/migration markers, tree
# moves (layer parents also arrive as group items), author table, page stats,
# scene info.
STRUCTURAL_BLOCKS = frozenset({0x00, 0x01, 0x09, 0x0A, 0x0C, 0x0D})


def block_name(block_type: int) -> str:
    return BLOCK_NAMES[block_type] if 0 <= block_type < len(BLOCK_NAMES) else f"Unknown{block_type}"


TAG_BYTE1, TAG_BYTE4, TAG_BYTE8, TAG_LENGTH4, TAG_ID = 0x1, 0x4, 0x8, 0xC, 0xF


@dataclass
class _Field:
    type: int
    raw: object    # int (u8 / 32-bit as u32), float (f64), bytes (sub-block), tuple (id)

    def as_int(self) -> int:
        return int(self.raw)

    def as_float32(self) -> float:
        return struct.unpack("<f", struct.pack("<I", self.raw))[0]


def _read_fields(data: bytes) -> dict[int, _Field]:
    """Decode a run of tagged values. Later duplicates of an index win."""
    r = _Reader(data)
    fields: dict[int, _Field] = {}
    while r.pos < len(data):
        tag = r.varuint()
        index, kind = tag >> 4, tag & 0xF
        if kind == TAG_BYTE1:
            value: object = r.u8()
        elif kind == TAG_BYTE4:
            value = r.unpack("<I")[0]
        elif kind == TAG_BYTE8:
            value = r.unpack("<d")[0]
        elif kind == TAG_LENGTH4:
            value = r.take(r.unpack("<I")[0])
        elif kind == TAG_ID:
            value = r.crdt_id()
        else:
            raise RmParseError(f"unknown tag type {kind:#x} at offset {r.pos}")
        fields[index] = _Field(kind, value)
    return fields


def _field(fields: dict[int, _Field], index: int, kind: int, block: str) -> _Field:
    f = fields.get(index)
    if f is None or f.type != kind:
        raise RmParseError(f"{block}: missing field {index} of type {kind:#x}")
    return f


def _lww_string(sub: bytes) -> str:
    """Last-write-wins string: {1: timestamp id, 2: {varuint len, u8 is_ascii, bytes}}."""
    inner = _field(_read_fields(sub), 2, TAG_LENGTH4, "label").raw
    r = _Reader(inner)
    length = r.varuint()
    r.u8()  # is_ascii flag
    return r.take(length).decode("utf-8", errors="replace")


def _lww_bool(sub: bytes) -> bool:
    return bool(_field(_read_fields(sub), 2, TAG_BYTE1, "visible").raw)


def _decode_points(data: bytes, block_version: int) -> list[Point]:
    points = []
    if block_version >= 2:
        if len(data) % 14:
            raise RmParseError(f"point data length {len(data)} is not a multiple of 14")
        for x, y, speed, width, direction, pressure in struct.iter_unpack("<ffHHBB", data):
            points.append(Point(x, y, speed / 4, direction * 2 * math.pi / 255,
                                width / 4, pressure / 255))
    else:
        if len(data) % 24:
            raise RmParseError(f"point data length {len(data)} is not a multiple of 24")
        points = [Point(*p) for p in struct.iter_unpack("<6f", data)]
    return points


def _decode_anchor(f: dict[int, _Field]) -> Anchor:
    """TreeNode fields 7..10, each last-write-wins {1: timestamp, 2: value}:
    7 anchor char id, 8 anchor type (u8), 9 threshold (f32), 10 origin x (f32)."""
    def lww(index: int, kind: int) -> _Field | None:
        if index not in f:
            return None
        return _field(_read_fields(f[index].raw), 2, kind, f"anchor field {index}")
    char_id = lww(7, TAG_ID).raw
    kind = lww(8, TAG_BYTE1)
    threshold = lww(9, TAG_BYTE4)
    origin = lww(10, TAG_BYTE4)
    return Anchor(char_id=char_id, origin_x=origin.as_float32() if origin else 0.0,
                  threshold=threshold.as_float32() if threshold else 0.0,
                  type=kind.as_int() if kind else 0)


def _decode_line(value: bytes, block_version: int, item_id) -> Stroke:
    """Decode a SceneLineItem value sub-block: u8 item_type then tagged fields."""
    r = _Reader(value)
    item_type = r.u8()
    if item_type != 3:
        raise RmParseError(f"line item has unexpected item type {item_type}")
    f = _read_fields(value[1:])
    tool_id = _field(f, 1, TAG_BYTE4, "line").as_int()
    color_id = _field(f, 2, TAG_BYTE4, "line").as_int()
    thickness = _field(f, 3, TAG_BYTE8, "line").raw
    starting = _field(f, 4, TAG_BYTE4, "line").as_float32()
    points = _decode_points(_field(f, 5, TAG_LENGTH4, "line").raw, block_version)
    argb = f[8].as_int() if 8 in f and f[8].type == TAG_BYTE4 else None
    return Stroke(tool=Tool.parse(tool_id), color=Color.parse(color_id), points=points,
                  thickness_scale=float(thickness), starting_length=starting, argb=argb,
                  tool_id=tool_id, color_id=color_id, item_id=item_id)


@dataclass
class _Char:
    id: tuple[int, int]
    left: tuple[int, int]
    right: tuple[int, int]
    char: str | None          # None: deleted, or a formatting marker
    fmt: int | None = None


# Inline format markers (TextItem.format): each is an entry of its own in the
# sequence, and the text from a start to its end has the style. The app makes the two
# together, the end's id one after the start's (0x140aab3c0 pairs them so).
FORMAT_BOLD_START, FORMAT_BOLD_END, FORMAT_ITALIC_START, FORMAT_ITALIC_END = 1, 2, 3, 4
FORMAT_LINK_START, FORMAT_LINK_END = 5, 6

_NO_ID = (0, 0)


def _id_key(value: tuple[int, int]) -> tuple[int, int]:
    """Ids order by counter, then author (0x140a98550)."""
    return value[1], value[0]


def _order_chars(chars: list[_Char]) -> list[_Char]:
    """Order CRDT sequence entries: each lies after `left` and before `right`.

    Topological sort over those constraints; among ready entries, prefer the one
    inserted right after the previously emitted entry, then the lowest id, which
    keeps typed runs contiguous.
    """
    by_id = {c.id: c for c in chars}
    succ: dict[tuple, list[tuple]] = {c.id: [] for c in chars}
    indegree = {c.id: 0 for c in chars}
    for c in chars:
        if c.left in by_id:
            succ[c.left].append(c.id)
            indegree[c.id] += 1
        if c.right in by_id:
            succ[c.id].append(c.right)
            indegree[c.right] += 1
    ready = sorted(i for i, d in indegree.items() if d == 0)
    out: list[_Char] = []
    last = _NO_ID
    while ready:
        pick = next((i for i in ready if by_id[i].left == last), ready[0])
        ready.remove(pick)
        out.append(by_id[pick])
        last = pick
        for nxt in succ[pick]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(out) != len(chars):
        raise RmParseError("text sequence has a cycle")
    return out


def _float32(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw))[0]


def _decode_text_item(data: bytes) -> TextItem:
    """parentId is not stored for text; 2 id, 3 left, 4 right, 5 deletedLength,
    6 value { varuint byte length, u8 utf type, the bytes [, 2: u32 format] }
    (0x1409152a0, 0x140917c70; written by 0x14091f9d0, the value only when nothing
    is deleted)."""
    r = _Values(data, "text item")
    item_id, left, right = r.need(2, TAG_ID), r.need(3, TAG_ID), r.need(4, TAG_ID)
    deleted_length = r.need(5, TAG_BYTE4)
    value = r.get(6, TAG_LENGTH4)
    if value is None:
        return TextItem(item_id, left, right, deleted_length, rest=r.rest())
    v = _Values(value, "text item value")
    length = v.varuint()
    utf_type = v.u8()
    string = v.take(length)
    return TextItem(item_id, left, right, deleted_length, string, utf_type,
                    v.get(2, TAG_BYTE4) or 0, v.rest(), r.rest())


def _decode_style_entry(r: _Values) -> StyleEntry:
    """A paragraph style: the character's id (not tagged), 1 timestamp,
    2 { 1: u8 style_legacy [, 2: u8 base_style, 3: u32 style_properties] }
    (0x1409160e0, 0x140917460; written by 0x1409103b0)."""
    char_id = r.crdt_id()
    timestamp = r.get(1, TAG_ID)
    if timestamp is None:
        raise RmParseError("paragraph format is missing its timestamp")
    value = r.get(2, TAG_LENGTH4)
    if value is None:
        raise RmParseError("paragraph format is missing its value")
    v = _Values(value, "paragraph format")
    return StyleEntry(char_id, timestamp, v.get(1, TAG_BYTE1), v.get(2, TAG_BYTE1),
                      v.get(3, TAG_BYTE4), v.rest())


def decode_root_text(payload: bytes) -> RootText:
    """The payload of a RootText block, field for field (read by 0x140916ab0, written
    by 0x14091ff90; the names are the app's own, from its JSON form of a scene):

        1: parentId, the id of the text: (0, 0)
        2: text { 1: sequence   { 1: { varuint n, n x (0: sub-block, a text item) } }
                  2: formatting { 1: styles { varuint m, m x style entry } } }
        3: position { f64 x, f64 y }
        4: textWidthLegacy, f32
        5: settings { 1: textWidth { 1: timestamp, 2: f32 } }   (newer files)

    A page has at most one. Every level may end in bytes of a newer format, which
    the app keeps and writes back; they are in RootText.unknown.
    """
    unknown: dict[str, bytes] = {}

    def done(reader: _Values, where: str) -> None:
        tail = reader.rest()
        if tail:
            unknown[where] = tail

    r = _Values(payload, "root text")
    block_id = r.need(1, TAG_ID)
    text = _Values(r.need(2, TAG_LENGTH4), "root text")
    sequence = _Values(text.need(1, TAG_LENGTH4), "root text")
    listed = _Values(sequence.need(1, TAG_LENGTH4), "root text items")
    items = []
    for _ in range(listed.varuint()):
        if listed.varuint() != (0 << 4) | TAG_LENGTH4:
            raise RmParseError("text item is not a sub-block")
        items.append(_decode_text_item(listed.take(listed.unpack("<I")[0])))
    done(listed, "items")
    done(sequence, "sequence")
    formatting = _Values(text.need(2, TAG_LENGTH4), "root text")
    listed = _Values(formatting.need(1, TAG_LENGTH4), "root text formats")
    styles = [_decode_style_entry(listed) for _ in range(listed.varuint())]
    done(listed, "styles")
    done(formatting, "formatting")
    done(text, "text")
    position = _Values(r.need(3, TAG_LENGTH4), "root text position")
    x, y = position.unpack("<dd")
    done(position, "position")
    width_legacy = _float32(r.need(4, TAG_BYTE4))
    width = width_timestamp = None
    settings = r.get(5, TAG_LENGTH4)
    if settings is not None:
        settings = _Values(settings, "root text settings")
        lww = settings.get(1, TAG_LENGTH4)
        if lww is not None:
            lww = _Values(lww, "root text width")
            width_timestamp = lww.need(1, TAG_ID)
            width = _float32(lww.need(2, TAG_BYTE4))
            done(lww, "width")
        done(settings, "settings")
    done(r, "root")
    return RootText(block_id, items, styles, x, y, width_legacy, width, width_timestamp, unknown)


def _item_chars(item: TextItem) -> list[_Char]:
    """The characters of an entry, each with its id; one that takes no id (an empty
    string) still gets its own, so that others can stand next to it."""
    out: list[_Char] = []
    author, counter = item.item_id

    def add(char: str | None, fmt: int | None = None, units: int = 1) -> None:
        nonlocal counter
        left = item.left if not out else out[-1].id
        out.append(_Char((author, counter), left, item.right, char, fmt))
        counter += units

    if item.kind == "deleted":
        for _ in range(max(item.deleted_length, 1)):
            add(None)
    elif item.kind == "format":
        add(None, item.format)
    else:
        for char in item.text:
            add(char, units=2 if ord(char) > 0xFFFF else 1)
        if not out:
            add(None)
    return out


def text_order(items: list[TextItem]) -> list[_Char]:
    """The characters of a text in their order, deleted ones and markers included.

    As the app loads them (0x1409164b0): entries that are not listed by rising id
    are in their order already, which is how it writes them. Entries listed by rising
    id are put in one by one, each after its `left`. That is the whole of it while
    every entry finds its `right` just there, as on every real page seen; where two
    devices typed at one place at once the app decides between them (0x1409350a0),
    which is not rebuilt here: such text is ordered by its left and right only.
    """
    runs = [_item_chars(item) for item in items]
    chars = [c for run in runs for c in run]
    keys = [_id_key(item.item_id) for item in items]
    if any(b < a for a, b in zip(keys, keys[1:])):
        return chars
    after: dict[tuple, tuple | None] = {}
    by_id: dict[tuple, _Char] = {}
    for c in chars:
        fits = c.id not in by_id and (c.left == _NO_ID or c.left in by_id)
        if not fits or (after.get(c.left) or _NO_ID) != c.right:
            return _order_chars(chars)
        after[c.id], after[c.left] = after.get(c.left), c.id
        by_id[c.id] = c
    out, this = [], after.get(_NO_ID)
    while this is not None:
        out.append(by_id[this])
        this = after.get(this)
    return out


def text_block(root: RootText) -> TextBlock:
    """The text as it reads: paragraphs with their styles, bold, italic and links."""
    styles: dict[tuple, StyleEntry] = {}
    for entry in root.styles:
        known = styles.get(entry.char_id)
        if known is None or _id_key(entry.timestamp) >= _id_key(known.timestamp):
            styles[entry.char_id] = entry

    def style_of(start_id) -> ParagraphStyle:
        return styles[start_id].style if start_id in styles else ParagraphStyle.PARAGRAPH

    paragraphs = [Paragraph(style_of(_NO_ID))]
    char_paragraph: dict[tuple[int, int], int] = {}
    on = {"bold": False, "italic": False, "link": False}
    markers = {FORMAT_BOLD_START: ("bold", True), FORMAT_BOLD_END: ("bold", False),
               FORMAT_ITALIC_START: ("italic", True), FORMAT_ITALIC_END: ("italic", False),
               FORMAT_LINK_START: ("link", True), FORMAT_LINK_END: ("link", False)}
    for c in text_order(root.items):
        char_paragraph[c.id] = len(paragraphs) - 1
        if c.char is not None and ord(c.char) > 0xFFFF:
            char_paragraph[(c.id[0], c.id[1] + 1)] = len(paragraphs) - 1    # its second unit
        if c.fmt is not None:
            if c.fmt in markers:
                name, value = markers[c.fmt]
                on[name] = value
        elif c.char == "\n":
            paragraphs.append(Paragraph(style_of(c.id)))
        elif c.char is not None:
            spans = paragraphs[-1].spans
            if spans and (spans[-1].bold, spans[-1].italic, spans[-1].link) == tuple(on.values()):
                spans[-1].text += c.char
            else:
                spans.append(TextSpan(c.char, **on))
    return TextBlock(x=root.x, y=root.y, width=root.text_width, paragraphs=paragraphs,
                     char_paragraph=char_paragraph, root=root)


def _decode_root_text(payload: bytes) -> TextBlock:
    return text_block(decode_root_text(payload))


def _left(f) -> tuple[int, int]:
    return f[3].raw if 3 in f and f[3].type == TAG_ID else (0, 0)


def _stacking(entries: list[tuple], group_child: dict[tuple, tuple]) -> dict[tuple, int] | None:
    """The stacking rank of every scene item id (bottom first), from the CRDT sequences
    of the nodes, a group item expanded into its node's items where it stands. None
    when a sequence does not fit together (an item after an unknown one)."""
    by_parent: dict[tuple, list[tuple]] = {}
    for parent, item, left, deleted_length in entries:
        by_parent.setdefault(parent, []).append((item, left, deleted_length))
    sequences: dict[tuple, list[tuple]] = {}
    for parent, items in by_parent.items():
        after: dict[tuple, tuple | None] = {}
        known: set[tuple] = set()
        for item, left, deleted_length in sorted(items, key=lambda x: (x[0][1], x[0][0])):
            for n in range(max(deleted_length, 1)):
                this = (item[0], item[1] + n)
                if this in known or (left != (0, 0) and left not in known):
                    return None
                after[this], after[left] = after.get(left), this
                known.add(this)
                left = this
        order, this = [], after.get((0, 0))
        while this is not None:
            order.append(this)
            this = after.get(this)
        sequences[parent] = order
    rank: dict[tuple, int] = {}

    def walk(node, seen: set) -> None:
        for item in sequences.get(node, []):
            rank.setdefault(item, len(rank))
            child = group_child.get(item)
            if child is not None and child not in seen:
                seen.add(child)
                walk(child, seen)
    for node in [ROOT_NODE, *sequences]:
        walk(node, {node})
    return rank


def _parse_v6(data: bytes) -> PageScene:
    r = _Reader(data, HEADER_LENGTH)
    names: dict[tuple, str] = {}
    visible: dict[tuple, bool] = {}
    layer_order: list[tuple] = []           # child node ids under the root, in order
    lines: list[tuple[tuple, Stroke]] = []  # (parent node, stroke) in file order
    deleted: set[tuple] = set()
    skipped: dict[int, int] = {}
    counts: dict[int, int] = {}
    text: TextBlock | None = None
    images: list[ImageItem] = []
    image_files: dict[bytes, str] = {}      # imageRef -> file name (SceneImagesTable)
    node_parent: dict[tuple, tuple] = {}    # group node -> parent node (group items)
    anchors: dict[tuple, Anchor] = {}
    sequence_items: list[tuple] = []        # (parent, id, left, deleted length) of every scene item
    group_child: dict[tuple, tuple] = {}    # live group item id -> its child node

    while r.pos < len(data):
        start = r.pos
        length, _zero, _min_version, version, block_type = r.unpack("<IBBBB")
        payload = r.take(length)
        counts[block_type] = counts.get(block_type, 0) + 1
        try:
            if block_type == BLOCK_TREE_NODE:
                f = _read_fields(payload)
                node = _field(f, 1, TAG_ID, "tree node").raw
                if 2 in f:
                    names[node] = _lww_string(f[2].raw)
                if 3 in f:
                    visible[node] = _lww_bool(f[3].raw)
                if 7 in f:
                    anchors[node] = _decode_anchor(f)
            elif block_type in (BLOCK_SCENE_GROUP, BLOCK_SCENE_LINE, BLOCK_TOMBSTONE):
                f = _read_fields(payload)
                parent = _field(f, 1, TAG_ID, "scene item").raw
                item = _field(f, 2, TAG_ID, "scene item").raw
                deleted_length = f[5].as_int() if 5 in f else 0
                value = f[6].raw if 6 in f and f[6].type == TAG_LENGTH4 else b""
                sequence_items.append((parent, item, _left(f), deleted_length))
                if block_type == BLOCK_TOMBSTONE or deleted_length or not value:
                    deleted.add(item)
                elif block_type == BLOCK_SCENE_GROUP:
                    # u8 item_type (2) then {2: child node id}
                    child = _field(_read_fields(value[1:]), 2, TAG_ID, "group item").raw
                    node_parent[child] = parent
                    group_child[item] = child
                    if parent == ROOT_NODE and child not in layer_order:
                        layer_order.append(child)
                else:
                    stroke = _decode_line(value, version, item)
                    stroke.seq = counts_total(counts)
                    lines.append((parent, stroke))
            elif block_type == BLOCK_ROOT_TEXT:
                text = _decode_root_text(payload)
            elif block_type == BLOCK_IMAGE_ITEM:
                # Same item header as other scene items; the value is kept raw.
                f = _read_fields(payload)
                item = _field(f, 2, TAG_ID, "image item").raw
                deleted_length = f[5].as_int() if 5 in f else 0
                value = f[6].raw if 6 in f and f[6].type == TAG_LENGTH4 else b""
                sequence_items.append((_field(f, 1, TAG_ID, "image item").raw, item, _left(f), deleted_length))
                if deleted_length or not value:
                    deleted.add(item)
                else:
                    image = ImageItem(item_id=item, value=value, seq=counts_total(counts),
                                      parent_id=_field(f, 1, TAG_ID, "image item").raw)
                    try:
                        image.image_ref, image.vertices, image.indices = _decode_image(value)
                    except (RmParseError, struct.error, IndexError) as err:
                        log.warning("image item %s: cannot decode (%s)", item, err)
                    images.append(image)
            elif block_type == BLOCK_IMAGES_TABLE:
                image_files.update(_decode_images_table(payload))
            elif block_type not in STRUCTURAL_BLOCKS:
                skipped[block_type] = skipped.get(block_type, 0) + 1
        except (RmParseError, struct.error, IndexError) as err:
            raise RmParseError(f"block type {block_type:#x} at offset {start}: {err}") from err

    def owner(node):
        """(layer node, nearest anchor) for a stroke's parent node."""
        anchor, seen = None, set()
        while node not in seen:
            seen.add(node)
            anchor = anchor or anchors.get(node)
            parent = node_parent.get(node)
            if parent is None or parent == ROOT_NODE:
                return node, anchor
            node = parent
        return node, anchor

    # Stacking: the order of each node's items is its CRDT sequence (each item after its
    # left neighbour, replayed in id order), a group item standing for its node's items.
    # Items are mostly stored in that order, but not one put back by undo, which goes
    # right after the item it was (rm_writer.restore_image). File order if the sequence
    # cannot be worked out.
    rank = _stacking(sequence_items, group_child)
    if rank is not None:
        for _, stroke in lines:
            stroke.seq = rank.get(stroke.item_id, stroke.seq)
        for image in images:
            image.seq = rank.get(image.item_id, image.seq)

    placed = []
    for parent, stroke in lines:
        if parent == ROOT_NODE:
            placed.append((ROOT_NODE, stroke))
            continue
        layer, stroke.anchor = owner(parent)
        placed.append((layer, stroke))
    # Layer order: root's group items, then any other top-level node owning strokes.
    for layer, _ in placed:
        if layer != ROOT_NODE and layer not in layer_order:
            layer_order.append(layer)
    layers = {node: Layer(name=names.get(node, ""), visible=visible.get(node, True),
                          node_id=node) for node in layer_order}
    for node, layer in layers.items():
        if not layer.name:
            layer.name = f"Layer {list(layers).index(node) + 1}"
    root_strokes = []
    for layer, stroke in placed:
        if stroke.item_id in deleted:
            continue
        (layers[layer].strokes if layer in layers else root_strokes).append(stroke)
    result = list(layers.values())
    if root_strokes:
        result.insert(0, Layer(name="Root", strokes=root_strokes, node_id=ROOT_NODE))
    images = [img for img in images if img.item_id not in deleted]
    for image in images:
        image.file_name = image_files.get(image.image_ref, "")
        if image.parent_id == ROOT_NODE:
            target = None
        else:
            layer, _ = owner(image.parent_id)
            target = layers.get(layer)
        if target is not None:
            target.images.append(image)
        elif result and result[0].node_id == ROOT_NODE:
            result[0].images.append(image)
        else:
            result.insert(0, Layer(name="Root", images=[image], node_id=ROOT_NODE))
    return PageScene(version=6, layers=result, x_origin=PAGE_WIDTH / 2, text=text,
                     images=images, block_counts=counts, skipped_blocks=skipped)


IMAGE_ITEM_TYPE = 7      # the value's leading byte, as 2 is a group and 3 a line


def counts_total(counts: dict[int, int]) -> int:
    """Blocks read so far: the position of the current one in the file."""
    return sum(counts.values())


def _decode_image(value: bytes) -> tuple[bytes, list[tuple[float, float, float, float]], list[int]]:
    """CrdtImageItem value -> (imageRef key, vertices, indices); see ImageItem."""
    if not value or value[0] != IMAGE_ITEM_TYPE:
        raise RmParseError(f"image item type {value[:1].hex() or 'missing'}, expected {IMAGE_ITEM_TYPE:02x}")
    f = _read_fields(value[1:])
    ref = _field(_read_fields(_field(f, 1, TAG_LENGTH4, "image ref").raw), 2, TAG_LENGTH4, "image ref").raw
    r = _Reader(_field(f, 3, TAG_LENGTH4, "image vertices").raw)
    n = r.varuint()
    if n % 4:
        raise RmParseError(f"{n} vertex values, not 4 per vertex")
    floats = r.unpack(f"<{n}f")
    vertices = [tuple(floats[i:i + 4]) for i in range(0, n, 4)]
    r = _Reader(_field(f, 4, TAG_LENGTH4, "image indices").raw)
    n = r.varuint()
    indices = list(r.unpack(f"<{n}I"))
    if any(i >= len(vertices) for i in indices):
        raise RmParseError("image index out of range")
    return ref, vertices, indices


def _decode_images_table(payload: bytes) -> dict[bytes, str]:
    """SceneImagesTable: {1: {varuint n, n x {0: 16-byte key, {1: LWW file name},
    {2: LWW flag}}}} -> {key: file name}."""
    r = _Reader(_field(_read_fields(payload), 1, TAG_LENGTH4, "images table").raw)
    files: dict[bytes, str] = {}
    for _ in range(r.varuint()):
        tag = r.varuint()
        if tag != (0 << 4 | TAG_LENGTH4):
            raise RmParseError(f"images table entry tag {tag:#x}")
        entry = r.take(r.unpack("<I")[0])
        key, fields = entry[:16], _read_fields(entry[16:])
        if 1 in fields and fields[1].type == TAG_LENGTH4:
            files[key] = _lww_string(fields[1].raw)
    return files


# ----------------------------------------------------------------------- reader

class _Reader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise RmParseError(f"unexpected end of data at offset {self.pos} (need {n} bytes)")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def unpack(self, fmt: str) -> tuple:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def u8(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        value = self.unpack("<i")[0]
        if value < 0:
            raise RmParseError(f"negative count {value} at offset {self.pos - 4}")
        return value

    def count(self, item_size: int) -> int:
        """Read an i32 count and check that many items can fit in the remaining data."""
        n = self.i32()
        if n * item_size > len(self.data) - self.pos:
            raise RmParseError(f"count {n} at offset {self.pos - 4} exceeds remaining data")
        return n

    def varuint(self) -> int:
        result = shift = 0
        while True:
            byte = self.u8()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise RmParseError(f"varuint too long at offset {self.pos}")

    def crdt_id(self) -> tuple[int, int]:
        return self.u8(), self.varuint()


class _Values(_Reader):
    """Tagged values read the way the app reads an object: its fields in their order,
    each one there or not; what follows the last one it knows is kept as it is."""

    def __init__(self, data: bytes, what: str) -> None:
        super().__init__(data)
        self.what = what

    def get(self, index: int, kind: int):
        """The value of field `index` when it is next, else None."""
        if self.pos >= len(self.data):
            return None
        start = self.pos
        tag = self.varuint()
        if tag >> 4 != index:
            self.pos = start
            return None
        if tag & 0xF != kind:
            raise RmParseError(f"{self.what}: field {index} has type {tag & 0xF:#x}, expected {kind:#x}")
        if kind == TAG_BYTE1:
            return self.u8()
        if kind == TAG_BYTE4:
            return self.unpack("<I")[0]
        if kind == TAG_BYTE8:
            return self.unpack("<d")[0]
        if kind == TAG_LENGTH4:
            return self.take(self.unpack("<I")[0])
        return self.crdt_id()

    def need(self, index: int, kind: int):
        value = self.get(index, kind)
        if value is None:
            raise RmParseError(f"{self.what}: missing field {index} of type {kind:#x}")
        return value

    def rest(self) -> bytes:
        chunk, self.pos = self.data[self.pos:], len(self.data)
        return chunk
