"""Edits made in this client that the cloud does not have yet.

The app edits a document's files on disk and its sync uploads them later. Here a
document is the cloud's files, cached by hash, so an edit is kept as what was done (an
image put on a page), not as files: PendingEdits stores it under <cache>/pending/, and
document_files() works out the files it changes from the document as it is now. The
viewer draws from that (LibrarySource), and SyncClient.push_edits uploads it; when
another device changed the document in between, the same edit is applied to the newer
version.

Four kinds: an image put on a page (ImageInsert), an image moved, scaled or turned
(ImageTransform), an image deleted (ImageDelete), and an image put back where it was
(ImageRestore: what undo and redo write). They name the image by its key in the page's
images table, which stays the same however often it is moved, so they apply as well to
an image of the tablet's as to one put on the page here.

Typed text (TextEdit): text typed or deleted, a stretch made bold or italic or not,
a paragraph given a style (viewer.rm_text, as the app's text document does each). An
edit names characters by id, not by position, so that it lands on the same text when
it is made again on a newer version of the page: ("page", author, counter), a
character the page had, or ("edit", uid, offset), one typed by an earlier edit. Text
goes after the character named (none: at the start); a range runs from its first
character to its last. A page with no text box gets one (its first paragraph a title,
as the app makes one), and changes its .rm, .content and .metadata like an image does.

Pen strokes (viewer.rm_stroke): a stroke drawn on a page (StrokeAdd: the finished
line, as the app would add it), an eraser taken over a page (StrokeErase: the eraser's
own line; what it deletes and what pieces it leaves is worked out on the page as it is
when the edit is made again, as the app works it out when the eraser is lifted), and
strokes deleted whole (StrokeDelete: by item, as ImageRestore names one). They change
the page's .rm, .content and .metadata like an image does, and no other file.

Undo and redo (PageHistory) work as in reMarkable.exe (SceneActionStack): they do not
take edits back but make new ones, so they upload like any other edit. A stroke put
back by one is a new item right after its deleted self (StrokeRestore).

A document's pen settings (ToolSettings: LastTool, Last<Tool>Size, ... in .content
"extraMetadata") are kept as an edit too, only the newest one per document.

Four more change the page list itself (core.pagemap, as the app's DocumentWorker):
a page added (PageAdd), pages duplicated (PageCopy), pages moved (PageMove) and pages
deleted (PageDelete). They name pages by id and their place by the page before it, so
they too apply to a newer version of the document. Staged works a document out with
all its pending edits: its page list, and each page's file.

What an inserted image changes, as in the app (reMarkable.exe 3.29):
    <doc>/<page>/<uuid>.png|.jpg   the image file, added
    <doc>/<page>.rm                the page, with the image item (viewer.rm_writer)
    <doc>.content                  that page's "modifed", the time of the edit
    <doc>.metadata                 "lastModified", the time of the edit
Times are milliseconds as decimal strings, and never move backwards. A moved image
changes the page, .content and .metadata; a deleted one also takes its file out of the
document once no item on the page shows it.

What the page edits change (DocumentWorker doAddPage/doCopyPages/doMovePages/
doDeletePages, 0x140558ea0, 0x14055a1f0, 0x14055e4f0, 0x14055aaf0):
    <doc>.content                  cPages (core.pagemap) and pageCount
    <doc>.metadata                 "lastModified"
    a copied page's files          its .rm (with the edits made to it before the copy),
                                   images, <page>-metadata.json, .highlights/<page>.json
                                   and .thumbnails/<page>.png, under the new page's id
                                   (pagecopyhelper.cpp copyPageFiles: the same bytes)
    a deleted page's files         out of the document
An added page gets no .rm: the app keeps a new page's scene in memory until something
is drawn on it, and a page without one shows as empty paper (as PDF pages do).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import struct
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable

from viewer import rm_layer
from viewer import rm_select
from viewer import rm_stroke
from viewer import rm_writer
from viewer import rm_text
from viewer.rm_parser import ParagraphStyle, RmParseError

from .config import atomic_write_json, ensure_private_dir, read_json
from .models import Document, json_bytes
from .pagemap import PageMap, PageMapError, is_legacy, upgrade

log = logging.getLogger("rm.edits")

EDITS_FILE = "edits.json"
IMAGE_EXTENSIONS = ("png", "jpg")


class EditError(Exception):
    """An edit that cannot be made, or no longer fits its document."""


@dataclass(frozen=True)
class ImageInsert:
    """An image put on a page: its file (named as the app names one, a UUID), the key
    its page's images table gets, and its four corners (x y u v, clockwise from the top
    left, in scene coordinates: x 0 at the centre of the page)."""

    doc_id: str
    page_id: str
    file_name: str
    key: str                        # 16 bytes, hex
    vertices: tuple[tuple[float, float, float, float], ...]
    sha256: str                     # of the image file
    size: int
    created_ms: int
    # A pasted copy keeps the (boundsTimestamp, imageRef timestamp) of the item copied.
    timestamps: tuple[tuple[int, int], tuple[int, int]] | None = None
    uid: str = ""                   # names the item it makes, for undo (ItemRef)
    layer: tuple | None = None      # target layer (ItemRef), or None for the page's current layer

    @property
    def path(self) -> str:
        """The file's name in the document."""
        return f"{self.doc_id}/{self.page_id}/{self.file_name}"

    @classmethod
    def new(cls, doc_id: str, page_id: str, extension: str, vertices, sha256: str, size: int,
            created_ms: int, timestamps=None, layer=None) -> "ImageInsert":
        """With a new file name and key: two random UUIDs, as the app makes them. The key
        is stored as the tablet's is, the UUID's bytes last to first."""
        if extension not in IMAGE_EXTENSIONS:
            raise EditError(f"images are stored as {' or '.join(IMAGE_EXTENSIONS)}, not {extension!r}")
        corners = _corners(vertices)
        return cls(doc_id, page_id, f"{uuid.uuid4()}.{extension}", uuid.uuid4().bytes[::-1].hex(), corners,
                   sha256, int(size), int(created_ms), _timestamps(timestamps), uuid.uuid4().hex, _opt_ref(layer))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageInsert":
        return cls(str(raw["doc_id"]), str(raw["page_id"]), str(raw["file_name"]), str(raw["key"]),
                   _corners(raw["vertices"]), str(raw["sha256"]), int(raw["size"]), int(raw["created_ms"]),
                   _timestamps(raw.get("timestamps")), str(raw.get("uid") or ""), _opt_ref(raw.get("layer")))


def _timestamps(value):
    """(boundsTimestamp, imageRef timestamp) as two (author, counter) pairs, or None."""
    if value is None:
        return None
    pair = tuple(tuple(int(n) for n in stamp) for stamp in value)
    if len(pair) != 2 or any(len(stamp) != 2 or not 0 <= stamp[0] <= 0xFF or stamp[1] < 0 for stamp in pair):
        raise EditError(f"not a pair of item timestamps: {value!r}")
    return pair


def _corners(vertices) -> tuple[tuple[float, float, float, float], ...]:
    corners = tuple(tuple(float(c) for c in vertex) for vertex in vertices)
    try:
        rm_writer.check_corners(corners)
    except ValueError as err:
        raise EditError(str(err)) from err
    return corners


def _key(key: str | bytes) -> str:
    key = key.hex() if isinstance(key, bytes) else str(key)
    if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
        raise EditError(f"not an image key: {key!r}")
    return key


@dataclass(frozen=True)
class ImageTransform:
    """An image of a page moved, scaled or turned: its new four corners."""

    doc_id: str
    page_id: str
    key: str                        # the image's key in the page's images table, hex
    vertices: tuple[tuple[float, float, float, float], ...]
    created_ms: int
    uid: str = ""                   # names the item it makes, for undo (ItemRef)

    @classmethod
    def new(cls, doc_id: str, page_id: str, key, vertices, created_ms: int, uid: str | None = None) -> "ImageTransform":
        return cls(doc_id, page_id, _key(key), _corners(vertices), int(created_ms),
                   uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageTransform":
        return cls.new(raw["doc_id"], raw["page_id"], raw["key"], raw["vertices"], raw["created_ms"],
                       raw.get("uid") or "")


@dataclass(frozen=True)
class ImageDelete:
    """An image of a page deleted."""

    doc_id: str
    page_id: str
    key: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, page_id: str, key, created_ms: int) -> "ImageDelete":
        return cls(str(doc_id), str(page_id), _key(key), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageDelete":
        return cls.new(raw["doc_id"], raw["page_id"], raw["key"], raw["created_ms"])


# An image item, named so that it can be found again whatever ids the page gives the
# edits when they are made again on a newer version of it: ("edit", uid), the item an
# edit put on the page, or ("page", author, counter), an item the page had already.
ItemRef = tuple


def _ref(value) -> ItemRef:
    ref = tuple(value)
    if len(ref) == 2 and ref[0] == "edit" and isinstance(ref[1], str) and ref[1]:
        return ref
    if (len(ref) == 3 and ref[0] == "page" and all(isinstance(n, int) and not isinstance(n, bool) for n in ref[1:])
            and 0 <= ref[1] <= 0xFF and ref[2] > 0):
        return ref
    raise EditError(f"not an item reference: {value!r}")


def _opt_ref(value) -> ItemRef | None:
    return None if value is None else _ref(value)


@dataclass(frozen=True)
class ImageRestore:
    """An image put back on its page as it was before an edit took it off: the item
    `item` (deleted, or replaced by a move) comes back as a new item right after it, as
    reMarkable.exe's undo and redo put an item back (rm_writer.restore_image). Nothing
    happens when the page shows the image already, or no longer has that item."""

    doc_id: str
    page_id: str
    key: str
    item: ItemRef
    created_ms: int
    uid: str = ""

    @classmethod
    def new(cls, doc_id: str, page_id: str, key, item, created_ms: int, uid: str | None = None) -> "ImageRestore":
        return cls(str(doc_id), str(page_id), _key(key), _ref(item), int(created_ms),
                   uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageRestore":
        return cls.new(raw["doc_id"], raw["page_id"], raw["key"], raw["item"], raw["created_ms"], raw.get("uid") or "")


# ------------------------------------------------------------------ pen strokes

def _points(value) -> str:
    """A line's points as an edit keeps them: the 14 bytes of each, in hex."""
    data = bytes(value) if isinstance(value, (bytes, bytearray)) else bytes.fromhex(str(value))
    if not data or len(data) % rm_stroke.POINT.size:
        raise EditError("not the points of a line")
    return data.hex()


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise EditError(f"not a {what}: {value!r}")
    return float(value)


@dataclass(frozen=True)
class StrokeAdd:
    """A stroke drawn on a page: the line as the app adds it once the pen is lifted
    (rm_stroke.draw: its points made from the pen's samples, then thinned out), which
    goes on top of the page's current layer under the page's next id
    (rm_stroke.append_stroke). `thickness` is the pen's size, the line's
    thicknessScale; `argb` its colour when `color` is 9 (ArgbCode)."""

    doc_id: str
    page_id: str
    tool: int
    color: int
    thickness: float
    points: str                     # 14 bytes a point, hex
    created_ms: int
    argb: int | None = None
    uid: str = ""                   # names the item it makes (ItemRef)
    layer: ItemRef | None = None    # target layer (ItemRef), or None for the page's current layer

    def line(self) -> rm_stroke.LineValue:
        return rm_stroke.new_line(self.tool, self.color, self.thickness, bytes.fromhex(self.points), argb=self.argb)

    @classmethod
    def new(cls, doc_id: str, page_id: str, line: rm_stroke.LineValue, created_ms: int, uid: str | None = None,
            layer=None) -> "StrokeAdd":
        if line.tool in rm_stroke.NOT_DRAWN:
            raise EditError(f"tool {line.tool} does not draw")
        if line.count >= rm_stroke.MAX_LINE_POINTS:
            raise EditError("the line has too many points")
        return cls(str(doc_id), str(page_id), int(line.tool), int(line.color), _number(line.thickness_scale, "pen size"),
                   _points(line.points), int(created_ms),
                   (rm_stroke.DEFAULT_ARGB if line.argb is None else int(line.argb)) if line.color == rm_stroke.COLOR_ARGB
                   else None, uuid.uuid4().hex if uid is None else str(uid), _opt_ref(layer))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "StrokeAdd":
        argb = raw.get("argb")
        return cls(str(raw["doc_id"]), str(raw["page_id"]), int(raw["tool"]), int(raw["color"]),
                   _number(raw["thickness"], "pen size"), _points(raw["points"]), int(raw["created_ms"]),
                   None if argb is None else int(argb), str(raw.get("uid") or ""), _opt_ref(raw.get("layer")))


@dataclass(frozen=True)
class StrokeErase:
    """An eraser taken over a page's current layer: the eraser's own line (`tool` 6,
    the Eraser, whose line is as wide as its first point; or 8, EraseSection, whose
    line is the outline of what goes). Every stroke it touches is deleted, and what is
    left of each put in its place as new strokes (rm_stroke.erase). The pieces it
    makes are named ("edit", "<uid>/<n>"), n counting them in the order made.

    Groups below the layer are erased too; `anchors` gives the anchor position of each
    group anchored to typed text as the page drew it when the eraser went over it
    ((node author, node counter, x, y)), so that the same strokes are hit when the
    edit is made again. A group anchored to text with no position here is left alone
    (edits kept before this was recorded erase the layer and its plain groups)."""

    doc_id: str
    page_id: str
    tool: int
    thickness: float
    points: str
    created_ms: int
    uid: str = ""
    anchors: tuple[tuple[int, int, float, float], ...] = ()
    layer: ItemRef | None = None

    def line(self) -> rm_stroke.LineValue:
        return rm_stroke.new_line(self.tool, 2, self.thickness, bytes.fromhex(self.points))

    def anchor_map(self) -> dict[tuple[int, int], tuple[float, float]]:
        return {(a, c): (x, y) for a, c, x, y in self.anchors}

    @classmethod
    def new(cls, doc_id: str, page_id: str, eraser: rm_stroke.LineValue, created_ms: int,
            uid: str | None = None, anchors=(), layer=None) -> "StrokeErase":
        if eraser.tool not in (rm_stroke.ERASER, rm_stroke.ERASE_SECTION):
            raise EditError(f"tool {eraser.tool} does not erase")
        if eraser.count >= rm_stroke.MAX_STORED_POINTS:
            raise EditError("the eraser line is too large")
        nodes = tuple(sorted((int(a), int(c), _number(x, "anchor position"), _number(y, "anchor position"))
                             for a, c, x, y in anchors))
        return cls(str(doc_id), str(page_id), int(eraser.tool), _number(eraser.thickness_scale, "eraser size"),
                   _points(eraser.points), int(created_ms), uuid.uuid4().hex if uid is None else str(uid), nodes,
                   _opt_ref(layer))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "StrokeErase":
        nodes = tuple((int(a), int(c), _number(x, "anchor position"), _number(y, "anchor position"))
                      for a, c, x, y in raw.get("anchors") or ())
        return cls(str(raw["doc_id"]), str(raw["page_id"]), int(raw["tool"]), _number(raw["thickness"], "eraser size"),
                   _points(raw["points"]), int(raw["created_ms"]), str(raw.get("uid") or ""), nodes,
                   _opt_ref(raw.get("layer")))


@dataclass(frozen=True)
class StrokeDelete:
    """Strokes deleted whole, each as the app deletes an item (rm_stroke.delete_strokes):
    its block stays where it is, without its value and counting one deleted id, joined
    to deleted neighbours. A stroke gone meanwhile is passed over."""

    doc_id: str
    page_id: str
    items: tuple[ItemRef, ...]
    created_ms: int
    uid: str = ""

    @classmethod
    def new(cls, doc_id: str, page_id: str, items, created_ms: int, uid: str | None = None) -> "StrokeDelete":
        refs = tuple(_ref(item) for item in items)
        if not refs:
            raise EditError("a delete of no strokes")
        return cls(str(doc_id), str(page_id), refs, int(created_ms), uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "StrokeDelete":
        return cls.new(raw["doc_id"], raw["page_id"], raw["items"], raw["created_ms"], raw.get("uid") or "")


@dataclass(frozen=True)
class StrokeRestore:
    """A deleted stroke put back, as the app's undo and redo put an item back
    (rm_stroke.restore_stroke): a new item right after the deleted one (`item`), with
    the value it had (`value`: the line value's bytes, hex) and, unless that value
    names one, moveId = the id of `origin` (default `item`): the item it first was."""

    doc_id: str
    page_id: str
    item: ItemRef
    value: str
    created_ms: int
    origin: ItemRef | None = None
    uid: str = ""

    def line(self) -> rm_stroke.LineValue:
        return rm_stroke.decode_line_value(bytes.fromhex(self.value))

    @property
    def is_glyph(self) -> bool:
        """A PDF/EPUB text highlight (glyph range value, u8 1 ...), not a stroke."""
        return self.value[:2] == "01"

    @classmethod
    def new(cls, doc_id: str, page_id: str, item, value, created_ms: int, origin=None,
            uid: str | None = None) -> "StrokeRestore":
        data = bytes(value) if isinstance(value, (bytes, bytearray)) else bytes.fromhex(str(value))
        try:
            if data[:1] == b"\x01":                 # a glyph range (PDF/EPUB text highlight)
                from viewer.rm_parser import decode_glyph_value
                decode_glyph_value(data)
            else:
                rm_stroke.decode_line_value(data)
        except (RmParseError, ValueError, IndexError, struct.error, UnicodeError) as err:
            raise EditError(f"not the value of a stroke: {err}") from err
        return cls(str(doc_id), str(page_id), _ref(item), data.hex(), int(created_ms),
                   None if origin is None else _ref(origin), uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "StrokeRestore":
        return cls.new(raw["doc_id"], raw["page_id"], raw["item"], raw["value"], raw["created_ms"],
                       raw.get("origin"), raw.get("uid") or "")


@dataclass(frozen=True)
class ItemsTransform:
    """Selected strokes and images moved, scaled or turned, as the app commits a
    selection when it closes (rm_select.transform_items: per node, the items deleted,
    their clones added and deleted again, the transformed clones added, each named
    after the item it first was by moveId). `matrix` is the product of every move,
    scale and turn of the selection (m11, m12, m21, m22, dx, dy, QTransform's order);
    `anchors` the anchor position of each text-anchored group holding selected items
    ((node author, node counter, x, y)), with which the transform is conjugated there.
    The item that takes the place of items[n] is named ("edit", "<uid>/<n>"); an item
    gone meanwhile is passed over."""

    doc_id: str
    page_id: str
    items: tuple[ItemRef, ...]
    matrix: tuple[float, ...]
    created_ms: int
    anchors: tuple[tuple[int, int, float, float], ...] = ()
    uid: str = ""

    @classmethod
    def new(cls, doc_id: str, page_id: str, items, matrix, created_ms: int, anchors=(),
            uid: str | None = None) -> "ItemsTransform":
        refs = tuple(_ref(item) for item in items)
        if not refs:
            raise EditError("a transform of no items")
        try:
            m = rm_select.matrix(matrix)
        except rm_select.SelectError as err:
            raise EditError(str(err)) from err
        nodes = tuple((int(a), int(c), _number(x, "anchor position"), _number(y, "anchor position"))
                      for a, c, x, y in anchors)
        return cls(str(doc_id), str(page_id), refs, m, int(created_ms), nodes,
                   uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ItemsTransform":
        return cls.new(raw["doc_id"], raw["page_id"], raw["items"], raw["matrix"], raw["created_ms"],
                       raw.get("anchors") or (), raw.get("uid") or "")


@dataclass(frozen=True)
class StrokePaste:
    """Copied strokes pasted on a page (rm_select.append_items): their values (hex), as
    the paste places them (scaled, centred where pasted, moveId 0:0, timestamp kept),
    appended to the page's current layer in this order, each named ("edit", "<uid>/<n>")."""

    doc_id: str
    page_id: str
    values: tuple[str, ...]
    created_ms: int
    uid: str = ""
    layer: ItemRef | None = None

    def lines(self) -> list[rm_stroke.LineValue]:
        return [rm_stroke.decode_line_value(bytes.fromhex(v)) for v in self.values]

    @classmethod
    def new(cls, doc_id: str, page_id: str, values, created_ms: int, uid: str | None = None,
            layer=None) -> "StrokePaste":
        out = []
        for value in values:
            data = value.encode() if isinstance(value, rm_stroke.LineValue) else (
                bytes(value) if isinstance(value, (bytes, bytearray)) else bytes.fromhex(str(value)))
            try:
                line = rm_stroke.decode_line_value(data)
            except (RmParseError, ValueError) as err:
                raise EditError(f"not the value of a stroke: {err}") from err
            if line.tool in rm_select.NOT_ADDED or not line.points:
                raise EditError(f"a stroke of tool {line.tool} cannot be pasted")
            out.append(data.hex())
        if not out:
            raise EditError("a paste of no strokes")
        return cls(str(doc_id), str(page_id), tuple(out), int(created_ms), uuid.uuid4().hex if uid is None else str(uid),
                   _opt_ref(layer))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "StrokePaste":
        return cls.new(raw["doc_id"], raw["page_id"], raw["values"], raw["created_ms"], raw.get("uid") or "",
                       raw.get("layer"))


# ------------------------------------------------------------------ layers & SceneInfo

@dataclass(frozen=True)
class LayerAdd:
    """A new layer added on top of a page (rm_layer.add_layer); named ("edit", uid)."""

    doc_id: str
    page_id: str
    name: str
    created_ms: int
    uid: str = ""

    @classmethod
    def new(cls, doc_id: str, page_id: str, name: str, created_ms: int, uid: str | None = None) -> "LayerAdd":
        return cls(str(doc_id), str(page_id), str(name or ""), int(created_ms),
                   uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerAdd":
        return cls.new(raw["doc_id"], raw["page_id"], raw.get("name") or "", raw["created_ms"], raw.get("uid") or "")


@dataclass(frozen=True)
class LayerDelete:
    """A layer detached via SceneTreeMove to (0, 0) (rm_layer.detach_layer)."""

    doc_id: str
    page_id: str
    layer: ItemRef
    created_ms: int
    new_current: ItemRef | None = None

    @classmethod
    def new(cls, doc_id: str, page_id: str, layer, created_ms: int, new_current=None) -> "LayerDelete":
        return cls(str(doc_id), str(page_id), _ref(layer), int(created_ms), _opt_ref(new_current))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerDelete":
        return cls.new(raw["doc_id"], raw["page_id"], raw["layer"], raw["created_ms"], raw.get("new_current"))


@dataclass(frozen=True)
class LayerReattach:
    """A detached layer re-attached under ROOT_NODE (rm_layer.reattach_layer), back at
    `index` (0 = bottom) when known."""

    doc_id: str
    page_id: str
    layer: ItemRef
    created_ms: int
    set_current: ItemRef | None = None
    index: int | None = None

    @classmethod
    def new(cls, doc_id: str, page_id: str, layer, created_ms: int, set_current=None,
            index: int | None = None) -> "LayerReattach":
        return cls(str(doc_id), str(page_id), _ref(layer), int(created_ms), _opt_ref(set_current),
                   None if index is None else int(index))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerReattach":
        return cls.new(raw["doc_id"], raw["page_id"], raw["layer"], raw["created_ms"], raw.get("set_current"),
                       raw.get("index"))


@dataclass(frozen=True)
class LayerRename:
    """A layer renamed (SceneTreeNode field 2 LWW string)."""

    doc_id: str
    page_id: str
    layer: ItemRef
    name: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, page_id: str, layer, name: str, created_ms: int) -> "LayerRename":
        return cls(str(doc_id), str(page_id), _ref(layer), str(name), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerRename":
        return cls.new(raw["doc_id"], raw["page_id"], raw["layer"], raw["name"], raw["created_ms"])


@dataclass(frozen=True)
class LayerVisibility:
    """A layer's visibility changed (SceneTreeNode field 3 LWW bool)."""

    doc_id: str
    page_id: str
    layer: ItemRef
    visible: bool
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, page_id: str, layer, visible: bool, created_ms: int) -> "LayerVisibility":
        return cls(str(doc_id), str(page_id), _ref(layer), bool(visible), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerVisibility":
        return cls.new(raw["doc_id"], raw["page_id"], raw["layer"], bool(raw["visible"]), raw["created_ms"])


@dataclass(frozen=True)
class LayerMove:
    """A layer moved to `new_index` (0 = bottom) in ROOT_NODE's CRDT sequence."""

    doc_id: str
    page_id: str
    layer: ItemRef
    new_index: int
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, page_id: str, layer, new_index: int, created_ms: int) -> "LayerMove":
        if int(new_index) < 0:
            raise EditError(f"invalid layer index {new_index}")
        return cls(str(doc_id), str(page_id), _ref(layer), int(new_index), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerMove":
        return cls.new(raw["doc_id"], raw["page_id"], raw["layer"], raw["new_index"], raw["created_ms"])


@dataclass(frozen=True)
class LayerMergeDown:
    """A layer merged down into the layer beneath it (rm_layer.merge_layer_down). Cloned
    items added to dst are named ("edit", "<uid>/<n>")."""

    doc_id: str
    page_id: str
    layer: ItemRef
    created_ms: int
    uid: str = ""

    @classmethod
    def new(cls, doc_id: str, page_id: str, layer, created_ms: int, uid: str | None = None) -> "LayerMergeDown":
        return cls(str(doc_id), str(page_id), _ref(layer), int(created_ms),
                   uuid.uuid4().hex if uid is None else str(uid))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerMergeDown":
        return cls.new(raw["doc_id"], raw["page_id"], raw["layer"], raw["created_ms"], raw.get("uid") or "")


@dataclass(frozen=True)
class LayerUnmerge:
    """Undo of LayerMergeDown (rm_layer.unmerge_layer): deletes cloned items from dst,
    re-attaches src, restores src's deleted items (named ("edit", "<uid>/<n>")), moves
    child groups back to src, and restores currentLayer."""

    doc_id: str
    page_id: str
    src: ItemRef
    dst: ItemRef
    moved_groups: tuple[ItemRef, ...]
    # (deleted item ref in src, block_type, raw value hex, origin ref or None)
    restore_items: tuple[tuple[ItemRef, int, str, ItemRef | None], ...]
    remove_from_dst: tuple[ItemRef, ...]
    old_current: ItemRef | None
    created_ms: int
    uid: str = ""
    src_index: int | None = None    # where src stood before the merge (0 = bottom)

    @classmethod
    def new(cls, doc_id: str, page_id: str, src, dst, moved_groups, restore_items, remove_from_dst,
            old_current, created_ms: int, uid: str | None = None, src_index: int | None = None) -> "LayerUnmerge":
        restored = tuple((_ref(r), int(k), bytes.fromhex(str(v)).hex(), _opt_ref(o)) for r, k, v, o in restore_items)
        return cls(str(doc_id), str(page_id), _ref(src), _ref(dst),
                   tuple(_ref(g) for g in moved_groups), restored, tuple(_ref(r) for r in remove_from_dst),
                   _opt_ref(old_current), int(created_ms), uuid.uuid4().hex if uid is None else str(uid),
                   None if src_index is None else int(src_index))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "LayerUnmerge":
        return cls.new(raw["doc_id"], raw["page_id"], raw["src"], raw["dst"], raw.get("moved_groups") or (),
                       raw.get("restore_items") or (), raw.get("remove_from_dst") or (),
                       raw.get("old_current"), raw["created_ms"], raw.get("uid") or "", raw.get("src_index"))


@dataclass(frozen=True)
class SceneFlags:
    """SceneInfo (0x0D) LWW fields updated: currentLayer (1), backgroundVisible (2),
    rootDocumentVisible (3), and/or the paper size (field 8, with fields 5 and 7 that
    the writer derives from it: Scene::setPaperSize, ARCHITECTURE.md 5.6)."""

    doc_id: str
    page_id: str
    created_ms: int
    current_layer: ItemRef | None = None
    background_visible: bool | None = None
    root_document_visible: bool | None = None
    paper_size: tuple[float, float] | None = None

    @classmethod
    def new(cls, doc_id: str, page_id: str, created_ms: int, *, current_layer=None,
            background_visible: bool | None = None, root_document_visible: bool | None = None,
            paper_size=None) -> "SceneFlags":
        if current_layer is None and background_visible is None and root_document_visible is None \
                and paper_size is None:
            raise EditError("a SceneFlags edit must change at least one field")
        paper = None
        if paper_size is not None:
            paper = (float(paper_size[0]), float(paper_size[1]))
            if not (paper[0] > 0 and paper[1] > 0):
                raise EditError(f"invalid paper size {paper_size!r}")
        return cls(str(doc_id), str(page_id), int(created_ms), _opt_ref(current_layer),
                   None if background_visible is None else bool(background_visible),
                   None if root_document_visible is None else bool(root_document_visible), paper)

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "SceneFlags":
        bg = raw.get("background_visible")
        rd = raw.get("root_document_visible")
        return cls.new(raw["doc_id"], raw["page_id"], raw["created_ms"],
                       current_layer=raw.get("current_layer"),
                       background_visible=None if bg is None else bool(bg),
                       root_document_visible=None if rd is None else bool(rd),
                       paper_size=raw.get("paper_size"))


LAYER_EDITS = (LayerAdd, LayerDelete, LayerReattach, LayerRename, LayerVisibility,
               LayerMove, LayerMergeDown, LayerUnmerge, SceneFlags)


STROKE_EDITS = (StrokeAdd, StrokeErase, StrokeDelete, StrokeRestore, StrokePaste, ItemsTransform)


# ------------------------------------------------------------------ the pen settings of a document

TOOL_SETTING_PREFIXES = ("Last", "Secondary")


@dataclass(frozen=True)
class ToolSettings:
    """The pen settings a document keeps in its .content "extraMetadata" (the app's
    DocumentToolSettings, 0x14060e1e0): LastTool, LastPen, Last<Tool>Size,
    Last<Tool>Color, ..., all strings. `values` are the keys set, each with its new
    value; made again, they are set on the newer .content and nothing else is."""

    doc_id: str
    values: tuple[tuple[str, str], ...]
    created_ms: int

    @property
    def page_id(self) -> str:
        return ""

    @classmethod
    def new(cls, doc_id: str, values, created_ms: int) -> "ToolSettings":
        pairs = tuple(sorted((str(k), str(v)) for k, v in dict(values).items()))
        if not pairs or any(not k.startswith(TOOL_SETTING_PREFIXES) for k, _ in pairs):
            raise EditError(f"not pen settings: {values!r}")
        return cls(str(doc_id), pairs, int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ToolSettings":
        return cls.new(raw["doc_id"], dict(raw["values"]), raw["created_ms"])


# ------------------------------------------------------------------ page edits
#
# Places are given as the app gives them, an index in the list of pages as shown, and
# also by `after`, the id of the page then before that place ("" at the front): made
# again on a newer version of the page list, an edit goes after that page, or to the
# index when it is gone.

def _ids(values) -> tuple[str, ...]:
    ids = tuple(str(v) for v in values)
    if not ids or any(not v for v in ids):
        raise EditError(f"not a list of page ids: {values!r}")
    return ids


def _new_id() -> str:
    return str(uuid.uuid4())


class _PageEdit:
    """A change of the page list: it belongs to no page of its own (page_id "")."""

    @property
    def page_id(self) -> str:
        return ""


@dataclass(frozen=True)
class PageAdd(_PageEdit):
    """A new page (doAddPage): an empty page with template `template` at `index`."""

    doc_id: str
    new_id: str
    index: int
    after: str
    template: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, index: int, after: str, template: str, created_ms: int,
            new_id: str | None = None) -> "PageAdd":
        return cls(str(doc_id), str(new_id or _new_id()), int(index), str(after or ""), str(template or ""),
                   int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PageAdd":
        return cls.new(raw["doc_id"], raw["index"], raw["after"], raw["template"], raw["created_ms"], raw["new_id"])


@dataclass(frozen=True)
class PageCopy(_PageEdit):
    """Pages duplicated (doCopyPages): a copy of each of `sources`, named `new_ids`,
    put in a row at `index`."""

    doc_id: str
    sources: tuple[str, ...]
    new_ids: tuple[str, ...]
    index: int
    after: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, sources, index: int, after: str, created_ms: int, new_ids=None) -> "PageCopy":
        sources = _ids(sources)
        new_ids = _ids(new_ids) if new_ids is not None else tuple(_new_id() for _ in sources)
        if len(new_ids) != len(sources):
            raise EditError("a copied page needs one new id per page")
        return cls(str(doc_id), sources, new_ids, int(index), str(after or ""), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PageCopy":
        return cls.new(raw["doc_id"], raw["sources"], raw["index"], raw["after"], raw["created_ms"], raw["new_ids"])


IMPORT_FILES = ("{doc}/{page}.rm", "{doc}/{page}-metadata.json", "{doc}.highlights/{page}.json",
                "{doc}.thumbnails/{page}.png")     # and "{doc}/{page}/<file>": the page's resources


def import_path(pattern: str, doc_id: str, page_id: str) -> str:
    """The target name of an imported page file (`pattern` as PageImport keeps it)."""
    if pattern in IMPORT_FILES:
        return pattern.format(doc=doc_id, page=page_id)
    prefix = "{doc}/{page}/"
    if not pattern.startswith(prefix) or "/" in pattern[len(prefix):] or not pattern[len(prefix):] \
            or pattern[len(prefix):] in (".", ".."):
        raise EditError(f"not a page file: {pattern!r}")
    return f"{doc_id}/{page_id}/{pattern[len(prefix):]}"


@dataclass(frozen=True)
class PageImport(_PageEdit):
    """Pages copied in from another document (DocumentController::copyPages/movePages
    with another target: CopyExternalPagesTask -> DocumentWorker::doCopyExternalPages,
    0x140559b20). Each source page becomes a page `new_ids[i]` of this document, put in
    a row at `index`, with the source page's template (raw: none when it had none) and
    NO PDF page (insertPage with redirection -1: a page of a PDF arrives as its ink on
    plain paper). Its files are copied as they are (pagecopyhelper): kept here as they
    were when copied, by hash in the pending files (`files[i]`: (pattern, sha256, size)
    per file, pattern as in IMPORT_FILES or "{doc}/{page}/<name>")."""

    doc_id: str
    source_doc: str
    sources: tuple[str, ...]
    new_ids: tuple[str, ...]
    templates: tuple[str, ...]
    files: tuple[tuple[tuple[str, str, int], ...], ...]
    index: int
    after: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, source_doc: str, sources, templates, files, index: int, after: str,
            created_ms: int, new_ids=None) -> "PageImport":
        sources = _ids(sources)
        new_ids = _ids(new_ids) if new_ids is not None else tuple(_new_id() for _ in sources)
        templates = tuple(str(t or "") for t in templates)
        files = tuple(tuple((str(p), str(s), int(n)) for p, s, n in page) for page in files)
        if not (len(new_ids) == len(sources) == len(templates) == len(files)):
            raise EditError("an imported page needs one new id, template and file list per page")
        if str(source_doc) == str(doc_id):
            raise EditError("pages copied within a document are a PageCopy")
        for page in files:
            for pattern, sha, _ in page:
                import_path(pattern, "d", "p")
                if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                    raise EditError(f"not a sha256 hex digest: {sha!r}")
        return cls(str(doc_id), str(source_doc), sources, new_ids, templates, files, int(index),
                   str(after or ""), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PageImport":
        return cls.new(raw["doc_id"], raw["source_doc"], raw["sources"], raw["templates"], raw["files"],
                       raw["index"], raw["after"], raw["created_ms"], raw["new_ids"])

    def hashes(self) -> set[str]:
        return {sha for page in self.files for _, sha, _ in page}


@dataclass(frozen=True)
class PageMove(_PageEdit):
    """Pages moved (doMovePages) to before the page at `index` of the list as it was."""

    doc_id: str
    pages: tuple[str, ...]
    index: int
    after: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, pages, index: int, after: str, created_ms: int) -> "PageMove":
        return cls(str(doc_id), _ids(pages), int(index), str(after or ""), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PageMove":
        return cls.new(raw["doc_id"], raw["pages"], raw["index"], raw["after"], raw["created_ms"])


@dataclass(frozen=True)
class PageDelete(_PageEdit):
    """Pages deleted (doDeletePages). A document left without pages gets a new "Blank"
    one, as the app adds one then; `blank_id` is its id."""

    doc_id: str
    pages: tuple[str, ...]
    blank_id: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, pages, created_ms: int, blank_id: str | None = None) -> "PageDelete":
        return cls(str(doc_id), _ids(pages), str(blank_id or _new_id()), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PageDelete":
        return cls.new(raw["doc_id"], raw["pages"], raw["created_ms"], raw["blank_id"])


@dataclass(frozen=True)
class PageTemplate(_PageEdit):
    """A page's template set (DocumentController.setTemplateForPage): the page map is
    compared with the pages' templates after the task (0x14065aff0) and only a template
    that differs is written, as a register write; lastModified then moves."""

    doc_id: str
    page: str
    template: str
    created_ms: int

    @classmethod
    def new(cls, doc_id: str, page: str, template: str, created_ms: int) -> "PageTemplate":
        if not page or not template:
            raise EditError("a template needs a page and a template name")
        return cls(str(doc_id), str(page), str(template), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PageTemplate":
        return cls.new(raw["doc_id"], raw["page"], raw["template"], raw["created_ms"])


# A character of a page's text, named so that it can be found again whatever ids the
# page gives the edits when they are made again: ("page", author, counter), one the page
# had, or ("edit", uid, offset), the offset-th UTF-16 unit typed by the TextEdit `uid`.
CharRef = tuple

TEXT_OPS = ("insert", "delete", "format", "unformat", "style", "create", "reinsert", "remove")
TEXT_STYLES = ("bold", "italic")


def _char_ref(value) -> CharRef | None:
    if value is None:
        return None
    ref = tuple(value)
    if (len(ref) == 3 and ref[0] == "edit" and isinstance(ref[1], str) and ref[1]
            and isinstance(ref[2], int) and not isinstance(ref[2], bool) and ref[2] >= 0):
        return ref
    if (len(ref) == 3 and ref[0] == "page" and all(isinstance(n, int) and not isinstance(n, bool) for n in ref[1:])
            and 0 <= ref[1] <= 0xFF and ref[2] > 0):
        return ref
    raise EditError(f"not a character reference: {value!r}")


@dataclass(frozen=True)
class TextEdit:
    """A change of a page's typed text (viewer.rm_text):

        insert    `text` after the character `first` (None: at the start); `bold` and
                  `italic` (None: as it falls) say on which side of a styled stretch's
                  markers it goes when it is typed right at one's end
        delete    the characters `first` to `last`
        format    `style` ("bold"/"italic") given to the characters `first` to `last`
        unformat  `style` taken off them
        style     the paragraph after the newline `first` (None: the first paragraph)
                  given `paragraph` (a viewer.rm_parser.ParagraphStyle value), or
                  `stored` (style_legacy, base_style, style_properties) when given
        create    a text box on a page that has none, its first paragraph `paragraph`
                  (SceneController::createRootDocument); on a page with one, nothing
        reinsert  `text` (or a marker, `code`) put back right after the item `first`
                  (None: at the start), whatever that item is now: what undo and
                  redo put back
        remove    the items `first` to `last` deleted, in the sequence's order (text
                  and markers alike): what undo and redo take away
    Every item an edit makes is named ("edit", uid, n), n counting them in the order
    made (the UTF-16 units of typed text; the markers of a format).
    """

    doc_id: str
    page_id: str
    op: str
    created_ms: int
    first: CharRef | None = None
    last: CharRef | None = None
    text: str = ""
    style: str = ""
    paragraph: int = 0
    bold: bool | None = None
    italic: bool | None = None
    uid: str = ""
    code: int = 0
    stored: tuple[int | None, int | None, int | None] | None = None

    def __post_init__(self) -> None:
        if self.op not in TEXT_OPS:
            raise EditError(f"not a text edit: {self.op!r}")
        if self.op == "insert" and not self.text:
            raise EditError("an insert without text")
        if self.op == "reinsert" and bool(self.text) == bool(self.code):
            raise EditError("a reinsert puts back text or a marker")
        if self.op in ("delete", "format", "unformat", "remove") and (self.first is None or self.last is None):
            raise EditError(f"a {self.op} without its range")
        if self.op in ("format", "unformat") and self.style not in TEXT_STYLES:
            raise EditError(f"not a text style: {self.style!r}")

    @classmethod
    def new(cls, doc_id: str, page_id: str, op: str, created_ms: int, *, first=None, last=None, text: str = "",
            style: str = "", paragraph: int = 0, bold: bool | None = None, italic: bool | None = None,
            code: int = 0, stored=None, uid: str | None = None) -> "TextEdit":
        return cls(str(doc_id), str(page_id), op, int(created_ms), _char_ref(first), _char_ref(last), str(text),
                   str(style), int(paragraph), bold, italic, uid or uuid.uuid4().hex, int(code), _stored(stored))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "TextEdit":
        flag = lambda v: None if v is None else bool(v)       # noqa: E731
        return cls(str(raw["doc_id"]), str(raw["page_id"]), str(raw["op"]), int(raw["created_ms"]),
                   _char_ref(raw.get("first")), _char_ref(raw.get("last")), str(raw.get("text") or ""),
                   str(raw.get("style") or ""), int(raw.get("paragraph") or 0), flag(raw.get("bold")),
                   flag(raw.get("italic")), str(raw.get("uid") or ""), int(raw.get("code") or 0),
                   _stored(raw.get("stored")))


def _stored(value) -> tuple | None:
    """A paragraph style as stored: (style_legacy, base_style, style_properties)."""
    if value is None:
        return None
    stored = tuple(value)
    if len(stored) != 3 or any(v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0)
                               for v in stored):
        raise EditError(f"not a stored paragraph style: {value!r}")
    return stored


PAGE_EDITS = (PageAdd, PageCopy, PageImport, PageMove, PageDelete, PageTemplate)
DEFAULT_TEMPLATE = "Blank"      # the template of the page a document left empty gets (0x140817f50)

Edit = (ImageInsert | ImageTransform | ImageDelete | ImageRestore | PageAdd | PageCopy | PageMove | PageDelete
        | PageTemplate | TextEdit | StrokeAdd | StrokeErase | StrokeDelete | StrokeRestore | StrokePaste | ItemsTransform
        | ToolSettings | LayerAdd | LayerDelete | LayerReattach | LayerRename | LayerVisibility | LayerMove
        | LayerMergeDown | LayerUnmerge | SceneFlags)
KINDS: dict[str, type] = {"image": ImageInsert, "image-transform": ImageTransform, "image-delete": ImageDelete,
                          "image-restore": ImageRestore, "page-add": PageAdd, "page-copy": PageCopy, "page-import": PageImport,
                          "page-move": PageMove, "page-delete": PageDelete, "page-template": PageTemplate,
                          "text": TextEdit,
                          "stroke": StrokeAdd, "stroke-erase": StrokeErase, "stroke-delete": StrokeDelete,
                          "stroke-restore": StrokeRestore, "stroke-paste": StrokePaste,
                          "items-transform": ItemsTransform, "tool-settings": ToolSettings,
                          "layer-add": LayerAdd, "layer-delete": LayerDelete, "layer-reattach": LayerReattach,
                          "layer-rename": LayerRename, "layer-visible": LayerVisibility, "layer-move": LayerMove,
                          "layer-merge": LayerMergeDown, "layer-unmerge": LayerUnmerge, "scene-flags": SceneFlags}


class PendingEdits:
    """The edits not uploaded yet, oldest first, kept in <root>/edits.json with the image
    files beside it (by hash). Safe to use from several threads and several processes
    (the app and tools/push_edits.py): the file is the record, read again whenever
    another process changed it, and every change is a read-modify-write of it under a
    lock (<root>/.lock), so a process never writes back edits another one has uploaded
    and discarded meanwhile."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._edits: list[Edit] = []
        self._seen: tuple | None = None               # (mtime_ns, size, inode) of the file read
        # Called (from whichever thread made it) after every change made here: the app's
        # outgoing sync (ui.outgoing) learns of new edits so.
        self.listeners: list[Callable[[], None]] = []
        with self._lock:
            self._refresh()

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:           # a listener never stops an edit from being kept
                log.exception("pending edits listener")

    def _stamp(self) -> tuple | None:
        try:
            st = (self.root / EDITS_FILE).stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size, st.st_ino

    def _refresh(self) -> None:
        """Read the file again if it is not the one last read (or written) here."""
        stamp = self._stamp()
        if stamp == self._seen and self._seen is not None:
            return
        self._seen = stamp
        if stamp is None:
            self._edits = []
            return
        try:
            raw = read_json(self.root / EDITS_FILE) or {}
            self._edits = [KINDS[e["kind"]].load(e) for e in raw.get("edits") or []]
        except (ValueError, KeyError, TypeError, OSError, EditError) as err:
            # Edits that cannot be read are not uploaded as something else: start empty.
            log.error("ignoring unreadable %s: %s", self.root / EDITS_FILE, err)
            self._edits = []

    @contextmanager
    def _changing(self):
        """The edits as the file has them now, to change; written back at the end."""
        with self._lock:
            ensure_private_dir(self.root)
            with open(self.root / ".lock", "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    self._seen = None                   # always read what is on disk now
                    self._refresh()
                    yield
                    self._save()
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        self._notify()

    def _file(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise EditError(f"not a sha256 hex digest: {sha256!r}")
        return self.root / "files" / sha256

    def _save(self) -> None:
        ensure_private_dir(self.root)
        atomic_write_json(self.root / EDITS_FILE,
                          {"version": 1, "edits": [{"kind": _kind(e), **asdict(e)} for e in self._edits]})
        self._seen = self._stamp()

    def all(self) -> list[Edit]:
        with self._lock:
            self._refresh()
            return list(self._edits)

    def of(self, doc_id: str) -> list[Edit]:
        with self._lock:
            self._refresh()
            return [e for e in self._edits if e.doc_id == doc_id]

    def documents(self) -> list[str]:
        return list(dict.fromkeys(e.doc_id for e in self.all()))

    def add(self, edit: Edit, data=None) -> None:
        """Keep `edit` (and its files first, so an edit on record always has them): an
        inserted image's file (`data`: its bytes), or the files of pages imported from
        another document (`data`: sha256 -> bytes)."""
        files: dict[str, bytes] = {}
        if isinstance(edit, ImageInsert):
            if data is None or hashlib.sha256(data).hexdigest() != edit.sha256 or len(data) != edit.size:
                raise EditError("the image is not the one the edit describes")
            files[edit.sha256] = data
        elif isinstance(edit, PageImport):
            data = data or {}
            for page in edit.files:
                for _, sha, size in page:
                    blob = data.get(sha)
                    if blob is None or hashlib.sha256(blob).hexdigest() != sha or len(blob) != size:
                        raise EditError("an imported page file is not the one the edit describes")
                    files[sha] = blob
        with self._changing():
            for sha, blob in files.items():
                path = self._file(sha)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_bytes(blob)
                tmp.replace(path)
            self._edits.append(edit)

    def replace(self, old: list[Edit], edit: Edit) -> None:
        """Forget `old` and keep `edit` in one change of the file (a document's pen
        settings: only the newest are kept)."""
        gone = set(old)
        with self._changing():
            self._edits = [e for e in self._edits if e not in gone] + [edit]

    def data(self, edit: ImageInsert) -> bytes:
        try:
            return self._file(edit.sha256).read_bytes()
        except OSError as err:
            raise EditError(f"the image of an edit is missing: {err}") from err

    def file(self, sha256: str) -> bytes:
        """A file kept for a pending edit, by its hash (an imported page's)."""
        try:
            return self._file(sha256).read_bytes()
        except OSError as err:
            raise EditError(f"a file of a pending edit is missing: {err}") from err

    def discard(self, edits: list[Edit]) -> None:
        """Forget edits (uploaded, or given up), and image files no edit uses any more."""
        gone = set(edits)
        with self._changing():
            self._edits = [e for e in self._edits if e not in gone]
            for sha in _hashes(gone) - _hashes(self._edits):
                self._file(sha).unlink(missing_ok=True)

    def clear(self) -> None:
        self.discard(self.all())

    def settle(self, uploaded: list[Edit], doc_id: str,
               rewrite: Callable[[Edit], Edit] | None = None) -> tuple[list[Edit], list[Edit]]:
        """Forget `uploaded` (the cloud has them now) and pass every other edit of
        `doc_id` through `rewrite` (rebase_refs: edits made while they were uploaded name
        the items those made by the edits' uids, which mean nothing once they are gone),
        in one change of the file. (the document's other edits before, after)."""
        gone = set(uploaded)
        with self._changing():
            before = [e for e in self._edits if e.doc_id == doc_id and e not in gone]
            kept = []
            for edit in self._edits:
                if edit in gone:
                    continue
                kept.append(rewrite(edit) if rewrite is not None and edit.doc_id == doc_id else edit)
            self._edits = kept
            after = [e for e in kept if e.doc_id == doc_id]
            for sha in _hashes(gone) - _hashes(self._edits):
                self._file(sha).unlink(missing_ok=True)
        return before, after


# -- after an upload: edits made meanwhile name what the uploaded ones made --------------
#
# An edit names an item another pending edit made by that edit's uid: ("edit", uid) for
# an image, stroke or layer, ("edit", uid, n) for the n-th character typed by a text edit.
# Once that edit is uploaded and forgotten, the name means nothing; the item is now one
# the page has, with the id the upload gave it. rebase_refs renames such references, in
# edits and in the undo history (core.edits.Change and friends) alike.

def uploaded_names(staged: "Staged") -> tuple[dict[str, tuple[int, int]], dict[str, list[tuple[int, int]]]]:
    """(made, typed) of every page of `staged` (the document with the edits that were
    uploaded, on the version they were uploaded onto): the id each uploaded edit's item
    got, by uid, and the ids of each text edit's characters. A copied page keeps its
    source page's ids (the file is copied byte for byte), so the names are the same."""
    made: dict[str, tuple[int, int]] = {}
    typed: dict[str, list[tuple[int, int]]] = {}
    for page_id in staged.page_edits:
        if page_id in staged.deleted:
            continue
        done = staged.replay(page_id)
        made.update(done.made)
        typed.update(done.typed)
    return made, typed


def rebase_refs(obj, made: dict, typed: dict):
    """`obj` (an edit, an undo history entry, a tuple, list or dict of them) with each
    reference to an item an uploaded edit made, ("edit", uid) or ("edit", uid, n), named
    ("page", author, counter) as the page has it now. Anything else is left as it is,
    and so is a reference whose item the upload did not leave on the page (it means
    "gone" either way). Unchanged parts are returned as they were (the same object)."""
    if not made and not typed:
        return obj
    if isinstance(obj, tuple):
        if len(obj) in (2, 3) and obj[0] == "edit" and isinstance(obj[1], str):
            if len(obj) == 2 and obj[1] in made:
                return ("page", *made[obj[1]])
            if (len(obj) == 3 and isinstance(obj[2], int) and obj[1] in typed
                    and 0 <= obj[2] < len(typed[obj[1]])):
                return ("page", *typed[obj[1]][obj[2]])
            return obj
        if obj and not isinstance(obj[0], (tuple, list, dict, str)) and not is_dataclass(obj[0]):
            return obj                  # numbers (points, vertices, ids): no references in there
        new = tuple(rebase_refs(x, made, typed) for x in obj)
        return obj if all(a is b for a, b in zip(new, obj)) else new
    if isinstance(obj, list):
        new = [rebase_refs(x, made, typed) for x in obj]
        return obj if all(a is b for a, b in zip(new, obj)) else new
    if isinstance(obj, dict):
        new = {rebase_refs(k, made, typed): rebase_refs(v, made, typed) for k, v in obj.items()}
        same = len(new) == len(obj) and all(a is b for a, b in zip(new, obj)) and \
            all(new[k] is v for k, v in zip(new, obj.values()))
        return obj if same else new
    if is_dataclass(obj) and not isinstance(obj, type):
        changes = {}
        for f in fields(obj):
            if not f.init:
                continue
            value = getattr(obj, f.name)
            moved = rebase_refs(value, made, typed)
            if moved is not value:
                changes[f.name] = moved
        return replace(obj, **changes) if changes else obj
    return obj


def _hashes(edits) -> set[str]:
    """The pending files `edits` use (inserted images, imported pages)."""
    out: set[str] = set()
    for e in edits:
        if isinstance(e, ImageInsert):
            out.add(e.sha256)
        elif isinstance(e, PageImport):
            out |= e.hashes()
    return out


def _kind(edit: Edit) -> str:
    return next(name for name, kind in KINDS.items() if isinstance(edit, kind))


def page_images(page: bytes | None) -> dict[bytes, str]:
    """The images a page's images table lists: key -> file name."""
    if page is None:
        return {}
    return {entry.key: entry.file_name for block in rm_writer.split(page)[1]
            if block.type == rm_writer.BLOCK_IMAGES_TABLE for entry in rm_writer.decode_images_table(block.payload)}


@dataclass
class Replay:
    """A page with edits made to it, and what they did: `made`, the id of the item each
    edit put on the page (by the edit's uid); `removed`, the value each item taken off
    the page had (by its id)."""

    page: bytes | None
    made: dict[str, tuple[int, int]]
    removed: dict[tuple[int, int], rm_writer.ImageValue]
    typed: dict[str, list[tuple[int, int]]] = field(default_factory=dict)   # uid -> the ids its items took

    def char(self, ref: CharRef | None) -> tuple[int, int] | None:
        """The id a character reference names now (None: none, or not known)."""
        if ref is None:
            return None
        if ref[0] == "page":
            return ref[1], ref[2]
        made = self.typed.get(ref[1])
        if made is None or ref[2] >= len(made):
            return None
        return made[ref[2]]

    def char_ref(self, char_id: tuple[int, int]) -> CharRef:
        """How an edit names the item `char_id` of the page."""
        for uid, made in reversed(list(self.typed.items())):
            if made and made[0][0] == char_id[0] and made[0][1] <= char_id[1] <= made[-1][1]:
                if char_id in made:
                    return ("edit", uid, made.index(char_id))
        return ("page", *char_id)

    def resolve(self, ref: ItemRef) -> tuple[int, int] | None:
        return self.made.get(ref[1]) if ref[0] == "edit" else (ref[1], ref[2])

    def ref(self, item: tuple[int, int]) -> ItemRef:
        """How an edit names the item `item` of the page."""
        uids = [uid for uid, made in self.made.items() if made == item]
        return ("edit", uids[-1]) if uids else ("page", *item)


def replay(page: bytes | None, edits: list[Edit], author: bytes) -> Replay:
    """`page` (None: it has no .rm yet) with the edits made to it, in order. An edit the
    page has already (an upload that went through without being noticed) changes
    nothing; so does a move or delete of an image the page no longer shows (deleted
    meanwhile, on the tablet or here: the delete wins). Images-table entries no item
    shows any more are dropped at the end, as the app drops them when it saves
    (pruneImageTable): an image deleted then put back keeps its entry where it was."""
    base = page
    made: dict[str, tuple[int, int]] = {}
    removed: dict[tuple[int, int], rm_writer.ImageValue] = {}
    deleted: set[bytes] = set()
    typed: dict[str, list[tuple[int, int]]] = {}

    def note(uid: str, key: bytes) -> None:
        live = rm_writer.image_item(page, key)
        if uid and live is not None:
            made[uid] = live[0]

    edits, at = list(edits), 0
    while at < len(edits):
        edit = edits[at]
        at += 1
        if isinstance(edit, TextEdit):
            if edit.op == "create" and not any(isinstance(e, TextEdit) and e.op != "create" for e in edits[at:]):
                # A text box nothing was ever typed in (kept so by earlier versions of
                # this client when text mode was only opened): no box.
                continue
            page = _text_edit(page, edit, author, Replay(page, made, removed, typed))
            continue
        if isinstance(edit, LAYER_EDITS):
            done = Replay(page, made, removed, typed)
            try:
                if isinstance(edit, LayerAdd):
                    page, node_id = rm_layer.add_layer(page, author=author, name=edit.name or None)
                    if edit.uid:
                        made[edit.uid] = node_id
                elif isinstance(edit, LayerDelete):
                    target = done.resolve(edit.layer)
                    new_cur = done.resolve(edit.new_current) if edit.new_current is not None else None
                    if page is not None and target is not None and target in rm_writer.layers(rm_writer.split(page)[1]):
                        page, _ = rm_layer.detach_layer(page, author=author, layer=target, new_current=new_cur)
                elif isinstance(edit, LayerReattach):
                    target = done.resolve(edit.layer)
                    set_cur = done.resolve(edit.set_current) if edit.set_current is not None else None
                    if page is not None and target is not None:
                        page = rm_layer.reattach_layer(page, author=author, layer=target, set_current=set_cur,
                                                       index=edit.index)
                elif isinstance(edit, LayerRename):
                    target = done.resolve(edit.layer)
                    if target is not None:
                        page = rm_layer.set_layer_name(page, author=author, layer=target, name=edit.name)
                elif isinstance(edit, LayerVisibility):
                    target = done.resolve(edit.layer)
                    if target is not None:
                        page = rm_layer.set_layer_visible(page, author=author, layer=target, visible=edit.visible)
                elif isinstance(edit, LayerMove):
                    target = done.resolve(edit.layer)
                    if page is not None and target is not None and target in rm_writer.layers(rm_writer.split(page)[1]):
                        page = rm_layer.move_layer(page, author=author, layer=target, new_index=edit.new_index)
                elif isinstance(edit, LayerMergeDown):
                    target = done.resolve(edit.layer)
                    if page is not None and target is not None and target in rm_writer.layers(rm_writer.split(page)[1]):
                        merged = rm_layer.merge_layer_down(page, author=author, layer=target)
                        page = merged.page
                        for n, (old_id, kind, raw, new_id) in enumerate(merged.transferred):
                            if edit.uid:
                                made[f"{edit.uid}/{n}"] = new_id
                            if kind == rm_writer.BLOCK_IMAGE_ITEM:
                                removed[old_id] = rm_writer.decode_image_value(raw)
                elif isinstance(edit, LayerUnmerge):
                    src = done.resolve(edit.src)
                    dst = done.resolve(edit.dst)
                    if page is not None and src is not None and dst is not None:
                        groups = tuple(g for g in (done.resolve(r) for r in edit.moved_groups) if g is not None)
                        rest_items = []
                        rest_indices = []
                        for n, (ref, kind, raw_hex, orig_ref) in enumerate(edit.restore_items):
                            item_id = done.resolve(ref)
                            if item_id is not None:
                                orig = done.resolve(orig_ref) if orig_ref is not None else None
                                rest_items.append((item_id, kind, bytes.fromhex(raw_hex), orig))
                                rest_indices.append((n, item_id))
                        rem_dst = tuple(i for i in (done.resolve(r) for r in edit.remove_from_dst) if i is not None)
                        old_cur = done.resolve(edit.old_current) if edit.old_current is not None else None
                        page, restored = rm_layer.unmerge_layer(
                            page, author=author, src=src, dst=dst, moved_groups=groups,
                            restore_items=tuple(rest_items), remove_from_dst=rem_dst, old_current=old_cur,
                            src_index=edit.src_index)
                        for n, item_id in rest_indices:
                            if edit.uid and item_id in restored:
                                made[f"{edit.uid}/{n}"] = restored[item_id]
                elif isinstance(edit, SceneFlags):
                    cur = done.resolve(edit.current_layer) if edit.current_layer is not None else None
                    if edit.current_layer is not None and cur is None:
                        continue
                    page = rm_layer.set_scene_info(
                        page, author=author, current_layer=cur,
                        background_visible=edit.background_visible,
                        root_document_visible=edit.root_document_visible,
                        paper=edit.paper_size)
            except (rm_layer.LayerError, RmParseError, ValueError) as err:
                raise EditError(f"the layer edit does not fit the page: {err}") from err
            continue
        if isinstance(edit, StrokeAdd):
            # Strokes drawn one after the other on the same layer are put on the page together.
            run = [edit]
            while at < len(edits) and isinstance(edits[at], StrokeAdd) and edits[at].layer == edit.layer:
                run.append(edits[at])
                at += 1
            done = Replay(page, made, removed, typed)
            target_layer = done.resolve(edit.layer) if edit.layer is not None else None
            page = _draw(page, run, author, made, target_layer)
            continue
        if isinstance(edit, StrokeErase):
            if page is not None:
                done = Replay(page, made, removed, typed)
                target_layer = done.resolve(edit.layer) if edit.layer is not None else None
                erased = rm_stroke.erase(page, author=author, eraser=edit.line(), layer=target_layer,
                                         anchors=edit.anchor_map())
                page = erased.page
                for n, piece in enumerate(erased.made):
                    made[f"{edit.uid}/{n}"] = piece
            continue
        if isinstance(edit, StrokeDelete):
            if page is not None:
                done = Replay(page, made, removed, typed)
                page = rm_stroke.delete_strokes(page, [i for i in map(done.resolve, edit.items) if i is not None])
            continue
        if isinstance(edit, StrokeRestore):
            done = Replay(page, made, removed, typed)
            item = done.resolve(edit.item)
            origin = done.resolve(edit.origin) if edit.origin is not None else None
            if page is None or item is None:
                log.warning("not putting back a stroke on page %s: the item it was is not known", edit.page_id)
                continue
            try:
                page, new_id = rm_stroke.restore_item(page, author=author, item=item,
                                                      value=bytes.fromhex(edit.value), move_id=origin)
            except rm_stroke.StrokeError as err:
                log.warning("not putting back a stroke on page %s: %s", edit.page_id, err)
                continue
            if edit.uid:
                made[edit.uid] = new_id
            continue
        if isinstance(edit, StrokePaste):
            done = Replay(page, made, removed, typed)
            target_layer = done.resolve(edit.layer) if edit.layer is not None else None
            try:
                page, ids = rm_select.append_items(page, author=author, values=edit.lines(), layer=target_layer)
            except (rm_select.SelectError, rm_stroke.StrokeError) as err:
                raise EditError(f"the paste does not fit the page: {err}") from err
            made.update((f"{edit.uid}/{n}", item) for n, item in enumerate(ids))
            continue
        if isinstance(edit, ItemsTransform):
            if page is None:
                continue
            done = Replay(page, made, removed, typed)
            items = [done.resolve(ref) for ref in edit.items]
            anchors = {(a, c): (x, y) for a, c, x, y in edit.anchors}
            try:
                page, moved, old = rm_select.transform_items(page, author=author, items=[i for i in items if i],
                                                             m=edit.matrix, anchors=anchors)
            except (rm_select.SelectError, rm_stroke.StrokeError) as err:
                raise EditError(f"the transform does not fit the page: {err}") from err
            for n, item in enumerate(items):
                if item in moved:
                    made[f"{edit.uid}/{n}"] = moved[item]
                    if isinstance(old.get(item), rm_writer.ImageValue):
                        removed[item] = old[item]
            continue
        if isinstance(edit, ToolSettings):
            continue
        key = bytes.fromhex(edit.key)
        if isinstance(edit, ImageInsert):
            if key not in page_images(page):
                done = Replay(page, made, removed, typed)
                target_layer = done.resolve(edit.layer) if edit.layer is not None else None
                page = rm_writer.append_image(page, author=author, key=key, file_name=edit.file_name,
                                              vertices=list(edit.vertices), layer=target_layer,
                                              timestamps=edit.timestamps)
                note(edit.uid, key)
            continue
        if not isinstance(edit, (ImageTransform, ImageDelete, ImageRestore)):
            raise EditError(f"unknown edit {type(edit).__name__}")
        if page is None:
            continue
        live = rm_writer.image_item(page, key)
        if isinstance(edit, ImageTransform):
            if live is None:
                continue
            new = rm_writer.transform_image(page, author=author, key=key, vertices=list(edit.vertices))
            if new is not None and new is not page:
                removed[live[0]] = live[1]
                page = new
            note(edit.uid, key)
        elif isinstance(edit, ImageDelete):
            if live is not None:
                page, _ = rm_writer.delete_image(page, author=author, key=key, prune=False)
                removed[live[0]] = live[1]
                deleted.add(key)
        elif live is None:
            item = Replay(page, made, removed).resolve(edit.item)
            value = removed.get(item) if item is not None else None
            if value is None or value.ref != key:
                log.warning("not putting back image %s on page %s: the item it was is not known", edit.key, edit.page_id)
                continue
            page = rm_writer.restore_image(page, author=author, item=item, value=value)
            note(edit.uid, key)
    if deleted and page is not None:
        page, _ = rm_writer.prune_images(page, deleted)
    if page is not None and base is not None:
        # One save of the page's editing session: PageStats loads + 1 over its file as
        # loaded, however many edits the session made (the app's writer, 0x140a094e0).
        page = rm_writer.saved(page, base)
    return Replay(page, made, removed, typed)


def _draw(page: bytes | None, edits: list[StrokeAdd], author: bytes, made: dict[str, tuple[int, int]],
          layer: tuple[int, int] | None = None) -> bytes | None:
    """`page` with the strokes of `edits` drawn on it, in order. One the page has
    already (the very same line on the same layer, not yet taken for an edit before this
    one: an upload that went through without being noticed) is not drawn again."""
    lines, uids = [], []
    taken = set(made.values())
    by_parent = None
    if page is not None and layer is not None:
        by_parent = {s.item: s.parent for s in rm_stroke.strokes(page)}
    for edit in edits:
        line = edit.line()
        there = next((item for item in rm_stroke.find_strokes(page, line)
                      if item not in taken and (by_parent is None or by_parent.get(item) == layer)), None)
        if there is not None:
            taken.add(there)
            if edit.uid:
                made[edit.uid] = there
        else:
            lines.append(line)
            uids.append(edit.uid)
    if lines:
        page, ids = rm_stroke.append_strokes(page, author=author, lines=lines, layer=layer)
        made.update((uid, item) for uid, item in zip(uids, ids) if uid)
    return page


def apply_text_edit(doc: rm_text.TextDocument, clock: rm_text.Clock, edit: TextEdit, done: Replay) -> bool:
    """Make a text edit on a page's text (`done`: what the edits before it made, which
    this one's are added to). False when it names an item the text does not have
    (another device took it out of a newer version, or it never had it): nothing is
    done, as a move of an image deleted meanwhile does nothing."""

    def where(ref: CharRef | None, after: bool) -> int | None:
        """The text position of a character: before it, or after it (a deleted one:
        where it was)."""
        if ref is None:
            return 0
        char = done.char(ref)
        found = doc.position_of(char) if char is not None else None
        if found is None:
            return None
        return found[0] + (1 if after and found[1] else 0)

    def made(ids) -> None:
        if edit.uid and ids:
            done.typed[edit.uid] = list(ids)

    if edit.op == "create":
        return True
    if edit.op == "insert":
        at = where(edit.first, True)
        if at is None:
            return False
        first = doc.insert(at, edit.text, clock, bold=edit.bold, italic=edit.italic)
        if first is not None:
            made([(first[0], first[1] + k) for k in range(rm_text.units(edit.text))])
        return True
    if edit.op == "reinsert":
        after = done.char(edit.first) if edit.first is not None else None
        if edit.first is not None and after is None:
            return False
        ids = doc.reinsert(after, clock, edit.text, edit.code)
        if ids is None:
            return False
        made(ids)
        return True
    if edit.op == "remove":
        first, last = done.char(edit.first), done.char(edit.last)
        if first is None or last is None:
            return False
        doc.remove(first, last)
        return True
    if edit.op == "style":
        key = rm_text.ZERO if edit.first is None else done.char(edit.first)
        if key is None or key not in doc.paragraph_keys():
            return False
        doc.set_style(key, edit.stored if edit.stored is not None else ParagraphStyle(edit.paragraph), clock)
        return True
    start, end = where(edit.first, False), where(edit.last, True)
    if start is None or end is None:
        return False
    if end <= start:
        return True
    if edit.op == "delete":
        doc.delete(start, end - start)
    elif edit.op == "format":
        made(doc.format(start, end - start, edit.style, clock))
    else:
        result = doc.unformat(start, end - start, edit.style, clock)
        if result is not None:
            made(result[1])
    return True


def _text_edit(page: bytes | None, edit: TextEdit, author: bytes, done: Replay) -> bytes | None:
    """`page` with a text edit made to it (apply_text_edit)."""

    def change(doc: rm_text.TextDocument, clock: rm_text.Clock) -> None:
        if not apply_text_edit(doc, clock, edit, done):
            log.warning("not making text edit %s on page %s: its text is gone", edit.op, edit.page_id)

    first = ParagraphStyle(edit.paragraph) if edit.op == "create" else ParagraphStyle.TITLE
    try:
        return rm_text.edit_text(page, author=author, change=change, style=first)
    except ValueError as err:
        raise EditError(f"the text edit does not fit the page: {err}") from err


def text_of(page: bytes | None, edits: list[Edit], author: bytes) -> tuple[rm_text.TextDocument | None, Replay]:
    """The text of `page` with `edits` made to it, and what they made: to name a
    position of it in a new TextEdit (Replay.char_ref of TextDocument.id_at)."""
    done = replay(page, edits, author)
    return rm_text.page_text(done.page), done


def page_with_edits(page: bytes | None, edits: list[Edit], author: bytes) -> bytes | None:
    """`page` with the edits made to it (replay)."""
    return replay(page, edits, author).page


def live_ref(page: bytes | None, edits: list[Edit], author: bytes, key: bytes) -> ItemRef | None:
    """How an edit names the item showing the image `key` on `page` with `edits` made
    to it; None when no item shows it."""
    done = replay(page, edits, author)
    live = rm_writer.image_item(done.page, key)
    return done.ref(live[0]) if live is not None else None


# ------------------------------------------------------------------ undo and redo

@dataclass(frozen=True)
class Change:
    """What an edit did to one image, as the app's item actions record it (two halves
    swapped by every undo and redo, 0x14095e690 / 0x14095f520): `remove`, the item that
    shows the image now (None: it is not shown), and `readd`, the item to put back (None:
    there was none)."""

    key: str
    readd: ItemRef | None
    remove: ItemRef | None


@dataclass(frozen=True)
class TextPart:
    """A stretch of a page's text an undo or redo takes away or puts back: its text (or
    a marker, `code`), the item it comes right after (`after`; None: the start), and,
    while it is on the page, its first and last items."""

    after: CharRef | None
    text: str = ""
    code: int = 0
    first: CharRef | None = None
    last: CharRef | None = None
    items: tuple = ()                   # each of its items, named, while on the page or when it was


def _alias(aliases: dict | None, ref):
    """What an item is named now: put back by an undo or redo with a new id, it goes
    by that (the app translates ids so too, 0x140a4b080 "translated")."""
    seen = set()
    while aliases and ref in aliases and ref not in seen:
        seen.add(ref)
        ref = aliases[ref]
    return ref


@dataclass(frozen=True)
class TextChange:
    """What a text action did, as the app's text actions record it (TextAction:
    `remove`, the stretches it added, still on the page; `readd`, those it took away,
    to put back), swapped by every undo and redo like an image's Change."""

    remove: tuple[TextPart, ...] = ()
    readd: tuple[TextPart, ...] = ()


@dataclass(frozen=True)
class StyleChange:
    """A paragraph's style changed: the paragraph (the newline before it; None: the
    first), the style it had (`readd`) and has now (`remove`), as stored."""

    paragraph: CharRef | None
    readd: tuple | None
    remove: tuple | None


@dataclass(frozen=True)
class StrokePart:
    """A stroke an undo or redo takes away or puts back: its name (while on the page, or
    the deleted item it was), its value (hex), and the item it first was (its moveId
    when it is put back; None: itself)."""

    ref: ItemRef
    value: str
    origin: ItemRef | None = None


@dataclass(frozen=True)
class StrokeChange:
    """What a pen or eraser action did, as the app's item actions record it (AddItem,
    InsertItemsAfter: `remove`, the strokes it put on the page; `readd`, those it took
    off), swapped by every undo and redo: the strokes to remove are deleted, those to
    put back come back as new items right after their deleted ones."""

    remove: tuple[StrokePart, ...] = ()
    readd: tuple[StrokePart, ...] = ()


@dataclass(frozen=True)
class LayerAttachChange:
    """A layer added (attached=True) or deleted (attached=False), swapped by undo/redo
    between LayerDelete and LayerReattach."""

    layer: ItemRef
    current_when_attached: ItemRef | None
    current_when_detached: ItemRef | None
    attached: bool
    index: int | None = None        # where the layer stands while attached (0 = bottom)


@dataclass(frozen=True)
class LayerRenameChange:
    """A layer renamed: swapped by undo/redo between old_name and new_name."""

    layer: ItemRef
    old_name: str
    new_name: str


@dataclass(frozen=True)
class LayerVisibilityChange:
    """A layer's visibility changed: swapped by undo/redo between old_visible and new_visible."""

    layer: ItemRef
    old_visible: bool
    new_visible: bool


@dataclass(frozen=True)
class LayerMoveChange:
    """A layer reordered: swapped by undo/redo between old_index and new_index."""

    layer: ItemRef
    old_index: int
    new_index: int


@dataclass(frozen=True)
class LayerMergeChange:
    """A layer merged down into dst (merged=True) or unmerged (merged=False), swapped by
    undo/redo between LayerUnmerge and LayerMergeDown."""

    src: ItemRef
    dst: ItemRef
    moved_groups: tuple[ItemRef, ...]
    # (item ref in src, block_type, raw value hex, origin ref or None)
    src_items: tuple[tuple[ItemRef, int, str, ItemRef | None], ...]
    dst_items: tuple[ItemRef, ...]
    old_current: ItemRef | None
    merged: bool = True
    src_index: int | None = None    # where src stood before the merge (0 = bottom)


@dataclass(frozen=True)
class SceneFlagChange:
    """SceneInfo bool flag changed ("background_visible" or "root_document_visible"),
    swapped by undo/redo between old_value and new_value."""

    field: str
    old_value: bool
    new_value: bool


BODY_TEXT = (1, None, None)         # how a paragraph without a style of its own is stored


def swapped(changes: tuple, doc_id: str, page_id: str, now_ms: int,
            aliases: dict | None = None) -> tuple[list[Edit], tuple]:
    """(the edits that undo, or redo, `changes`; the changes that take them back again).
    For each image, in order: the item showing it is deleted, and the item to put back
    comes back as a new item (ImageRestore), as the app's undo and redo remove one half
    of an action and add the other (0x1408c5910, then 0x1408cbd60). Text is taken
    away by id and put back where it was with new ids (TextEdit remove, reinsert), a
    paragraph style set back; the changes of an action are swapped last to first (the
    changes that take them back again are listed so, last first). An
    item put back gets a new id: `aliases` (kept with the page's history) records the
    new name of each, and every name is looked up there first."""
    name = lambda ref: _alias(aliases, ref)         # noqa: E731
    edits: list[Edit] = []
    back: list = []
    for change in reversed(changes):
        if isinstance(change, LayerAttachChange):
            layer = name(change.layer)
            cur_att = name(change.current_when_attached) if change.current_when_attached is not None else None
            cur_det = name(change.current_when_detached) if change.current_when_detached is not None else None
            if change.attached:
                edits.append(LayerDelete.new(doc_id, page_id, layer, now_ms, new_current=cur_det))
            else:
                edits.append(LayerReattach.new(doc_id, page_id, layer, now_ms, set_current=cur_att,
                                               index=change.index))
            back.append(LayerAttachChange(layer, cur_att, cur_det, not change.attached, change.index))
            continue
        if isinstance(change, LayerRenameChange):
            layer = name(change.layer)
            edits.append(LayerRename.new(doc_id, page_id, layer, change.old_name, now_ms))
            back.append(LayerRenameChange(layer, change.new_name, change.old_name))
            continue
        if isinstance(change, LayerVisibilityChange):
            layer = name(change.layer)
            edits.append(LayerVisibility.new(doc_id, page_id, layer, change.old_visible, now_ms))
            back.append(LayerVisibilityChange(layer, change.new_visible, change.old_visible))
            continue
        if isinstance(change, LayerMoveChange):
            layer = name(change.layer)
            edits.append(LayerMove.new(doc_id, page_id, layer, change.old_index, now_ms))
            back.append(LayerMoveChange(layer, change.new_index, change.old_index))
            continue
        if isinstance(change, LayerMergeChange):
            src, dst = name(change.src), name(change.dst)
            groups = tuple(name(g) for g in change.moved_groups)
            old_cur = name(change.old_current) if change.old_current is not None else None
            if change.merged:
                src_items = tuple((name(r), k, v, name(o) if o is not None else None)
                                  for r, k, v, o in change.src_items)
                dst_items = tuple(name(r) for r in change.dst_items)
                unmerge = LayerUnmerge.new(doc_id, page_id, src, dst, groups, src_items, dst_items, old_cur, now_ms,
                                           src_index=change.src_index)
                edits.append(unmerge)
                restored_src = []
                for n, (ref, kind, raw_hex, orig) in enumerate(src_items):
                    made_ref = ("edit", f"{unmerge.uid}/{n}")
                    if aliases is not None:
                        aliases[ref] = made_ref
                    restored_src.append((made_ref, kind, raw_hex, orig or ref))
                # dst_items: the clones' names, so a redo can point later steps at the new clones.
                back.append(LayerMergeChange(src, dst, groups, tuple(restored_src), dst_items, old_cur, merged=False,
                                             src_index=change.src_index))
            else:
                merge = LayerMergeDown.new(doc_id, page_id, src, now_ms)
                edits.append(merge)
                src_items = tuple((name(r), k, v, name(o) if o is not None else None)
                                  for r, k, v, o in change.src_items)
                new_dst = tuple(("edit", f"{merge.uid}/{n}") for n in range(len(src_items)))
                if aliases is not None:
                    # Steps above this one (an erase, a move...) name the clones the
                    # merge undone made; the redo's clones take their place.
                    for old, new in zip(change.dst_items, new_dst):
                        aliases[old] = new
                back.append(LayerMergeChange(src, dst, groups, src_items, new_dst, old_cur, merged=True,
                                             src_index=change.src_index))
            continue
        if isinstance(change, SceneFlagChange):
            edits.append(SceneFlags.new(doc_id, page_id, now_ms, **{change.field: change.old_value}))
            back.append(SceneFlagChange(change.field, change.new_value, change.old_value))
            continue
        if isinstance(change, StrokeChange):
            gone = tuple(StrokePart(name(p.ref), p.value, p.origin) for p in change.remove)
            if gone:
                edits.append(StrokeDelete.new(doc_id, page_id, [p.ref for p in gone], now_ms))
            readded = []
            for part in change.readd:
                at = name(part.ref)
                restore = StrokeRestore.new(doc_id, page_id, at, part.value, now_ms, part.origin or at)
                edits.append(restore)
                made = ("edit", restore.uid)
                if aliases is not None:
                    aliases[at] = made
                readded.append(StrokePart(made, part.value, part.origin or at))
            back.append(StrokeChange(tuple(readded), gone))
            continue
        if isinstance(change, StyleChange):
            paragraph = name(change.paragraph)
            edits.append(TextEdit.new(doc_id, page_id, "style", now_ms, first=paragraph,
                                      stored=change.readd or BODY_TEXT))
            back.append(StyleChange(paragraph, change.remove, change.readd or BODY_TEXT))
            continue
        if isinstance(change, TextChange):
            gone = []
            for part in change.remove:
                items = tuple(name(r) for r in part.items) or (name(part.first), name(part.last))
                if items[0] is not None and items[-1] is not None:
                    edits.append(TextEdit.new(doc_id, page_id, "remove", now_ms, first=items[0], last=items[-1]))
                gone.append(TextPart(name(part.after), part.text, part.code, items=items if part.items else ()))
            readded = []
            for part in change.readd:
                edit = TextEdit.new(doc_id, page_id, "reinsert", now_ms, first=name(part.after), text=part.text,
                                    code=part.code)
                edits.append(edit)
                count = rm_text.units(part.text) if part.text else 1
                new = tuple(("edit", edit.uid, k) for k in range(count))
                if aliases is not None:
                    for old, made in zip(part.items, new):
                        aliases[old] = made
                readded.append(TextPart(name(part.after), part.text, part.code, new[0], new[-1], new))
            back.append(TextChange(tuple(readded), tuple(gone)))
            continue
        if change.remove is not None:
            edits.append(ImageDelete.new(doc_id, page_id, change.key, now_ms))
        made = None
        if change.readd is not None:
            restore = ImageRestore.new(doc_id, page_id, change.key, change.readd, now_ms)
            edits.append(restore)
            made = ("edit", restore.uid)
        back.append(Change(change.key, change.remove, made))
    return edits, tuple(back)              # last to first again when it is swapped back


class PageHistory:
    """The undo and redo stacks of a page, as reMarkable.exe keeps them for each page's
    scene (SceneActionStack, Scene+0x368): an edit made goes on the undo stack and
    empties the redo stack (push, 0x1408f3c70); undo moves the top of the undo stack to
    the redo stack, redo the other way (0x1408f50f0, 0x1408f46e0). There is no limit.
    The stacks are kept in memory only and are emptied when the page changes in another
    way (the app empties them when it loads the page again, 0x140a7a4d0): `seen` is
    what the page was (its synced file, its pending edits) when they were last used."""

    def __init__(self, seen: Any) -> None:
        self.seen = seen
        self.undo: list[tuple] = []
        self.redo: list[tuple] = []
        self.aliases: dict = {}             # text items put back, old name -> new (swapped)
        # Applied to what is pushed: names of items uploaded meanwhile become the page's
        # own (LibrarySource sets it; rebase_refs).
        self.rename: Callable[[tuple], tuple] = lambda changes: changes

    def push(self, changes: list | tuple) -> None:
        if changes:
            self.undo.append(tuple(self.rename(tuple(changes))))
            self.redo.clear()

    def rebase(self, rename: Callable) -> None:
        """Rename the references in both stacks (an upload of this page's edits)."""
        self.undo = [rename(entry) for entry in self.undo]
        self.redo = [rename(entry) for entry in self.redo]
        self.aliases = rename(self.aliases)


def _later(old: Any, new_ms: int) -> str:
    """A millisecond timestamp string moved forward to `new_ms`, never back."""
    try:
        return str(max(int(old), new_ms))
    except (TypeError, ValueError):
        return str(new_ms)


# ------------------------------------------------------------------ a document with its edits

def current_content(doc: Document, read: Callable[[str], bytes], author: bytes) -> dict:
    """The document's .content as the app holds it once loaded: a formatVersion 1 one
    (`pages` + `redirectionPageMap`, 63 cached PDFs and EPUBs) upgraded to cPages as the
    app upgrades it on load (core.pagemap.upgrade), with the .pagedata lines as its
    templates. The app writes it so the first time it saves the document (a page's
    `modifed`, a page added): the cloud copy stays formatVersion 1 until then."""
    content = json.loads(read(f"{doc.id}.content"))
    if not isinstance(content, dict):
        raise ValueError("the .content is not a JSON object")
    if not is_legacy(content):
        return content
    name, templates = f"{doc.id}.pagedata", None
    if name in doc.files:
        templates = [line.strip() for line in read(name).decode("utf-8", errors="replace").splitlines()]
    return upgrade(content, author, templates)


def page_files(doc: Document, page_id: str) -> list[str]:
    """The files of `doc` that belong to page `page_id` (the app's page paths:
    resourceDirectoryPath, pageLinesPath, pageMetadataPath, pageHighlightsPath,
    pageThumbnailPath)."""
    own = {f"{doc.id}/{page_id}.rm", f"{doc.id}/{page_id}-metadata.json", f"{doc.id}.highlights/{page_id}.json",
           f"{doc.id}.thumbnails/{page_id}.png"}
    folder = f"{doc.id}/{page_id}/"
    return sorted(name for name in doc.files if name in own or name.startswith(folder))


class Staged:
    """`doc` (as the cloud has it) with `edits` made to it, in order: the page list
    (`ids`, and `content`, the .content with it when the edits change it) and, for each
    page, the edits made to it and where it comes from: the document's own page, a new
    empty page, or a copy of another page as it was when copied. `read(name)` gives a
    file of `doc`. Pages are worked out when asked for (page, replay)."""

    def __init__(self, doc: Document, edits: list[Edit], read: Callable[[str], bytes], author: bytes,
                 files: Callable[[str], bytes] | None = None) -> None:
        self.doc, self.read, self.author = doc, read, author
        self._files = files                             # sha256 -> a pending file (imported pages)
        self.page_edits: dict[str, list[Edit]] = {}
        # page id -> ("new",) | ("copy", source page id, how many of its edits the copy has)
        #          | ("import", the PageImport, which of its pages)
        self.origin: dict[str, tuple] = {}
        self.deleted: set[str] = set()                  # pages the edits deleted
        self.page_list_edits: list[Edit] = []
        self.content: dict | None = None                # .content with the new page list
        self._pages: dict[str, bytes | None] = {}
        self.tool_settings: dict[str, str] = {}         # extraMetadata keys set, newest last
        live = [p.id for p in doc.content.pages if not p.deleted]
        pagemap: PageMap | None = None
        for edit in edits:
            if edit.doc_id != doc.id:
                raise EditError("an edit of another document")
            if isinstance(edit, ToolSettings):
                self.tool_settings.update(edit.values)
                continue
            if not isinstance(edit, PAGE_EDITS):
                if edit.page_id not in self.deleted:   # edits of a page deleted after them are moot
                    self.page_edits.setdefault(edit.page_id, []).append(edit)
                continue
            if pagemap is None:
                try:
                    pagemap = PageMap(current_content(doc, read, author), author)
                except (ValueError, PageMapError) as err:
                    raise EditError(f"the page list cannot be changed: {err}") from err
            # A template the page has already changes nothing (the app compares the page
            # map before writing, 0x14065aff0): no page map write, no lastModified.
            if self._apply(pagemap, edit) is not False:
                self.page_list_edits.append(edit)
        if pagemap is not None and self.page_list_edits:
            self.content = pagemap.save()
            live = pagemap.ids()
        self.ids: list[str] = live

    # -- the page list --

    @staticmethod
    def _place(pagemap: PageMap, index: int, after: str) -> int:
        if not after:
            return 0
        at = pagemap.index(after)
        return at + 1 if at >= 0 else min(max(index, 0), len(pagemap.live))

    def _apply(self, pagemap: PageMap, edit: Edit) -> bool | None:
        """Make a page edit on the page list. One the list has already (uploaded without
        it being noticed), or whose pages are gone, changes nothing."""
        try:
            if isinstance(edit, PageAdd):
                if not pagemap.known(edit.new_id):
                    pagemap.insert(self._place(pagemap, edit.index, edit.after), edit.new_id, edit.template)
                    self.origin[edit.new_id] = ("new",)
            elif isinstance(edit, PageCopy):
                pairs = [(pagemap.index(s), s, n) for s, n in zip(edit.sources, edit.new_ids)
                         if pagemap.index(s) >= 0 and not pagemap.known(n)]
                at = self._place(pagemap, edit.index, edit.after)
                infos = [(pagemap.info(s), s, n) for _, s, n in sorted(pairs)]      # read before inserting
                for i, (info, source, new_id) in enumerate(infos):
                    pagemap.insert(at + i, new_id, info.template, info.redirect)
                    self.origin[new_id] = ("copy", source, len(self.page_edits.get(source, [])))
            elif isinstance(edit, PageImport):
                at = self._place(pagemap, edit.index, edit.after)
                for i, (new_id, template) in enumerate(zip(edit.new_ids, edit.templates)):
                    if pagemap.known(new_id):
                        continue
                    # doCopyExternalPages: insertPage(index, id, template, -1, mode 0)
                    pagemap.insert(at, new_id, template, None)
                    self.origin[new_id] = ("import", edit, i)
                    at += 1
            elif isinstance(edit, PageMove):
                indices = [pagemap.index(p) for p in edit.pages if pagemap.index(p) >= 0]
                if indices:
                    pagemap.move(indices, self._place(pagemap, edit.index, edit.after))
            elif isinstance(edit, PageDelete):
                for page_id in edit.pages:
                    at = pagemap.index(page_id)
                    if at >= 0:
                        pagemap.delete(at)
                        self.deleted.add(page_id)
                        self.page_edits.pop(page_id, None)
                if not pagemap.live and not pagemap.known(edit.blank_id):
                    pagemap.insert(0, edit.blank_id, DEFAULT_TEMPLATE)
                    self.origin[edit.blank_id] = ("new",)
            elif isinstance(edit, PageTemplate):
                return pagemap.set_template(edit.page, edit.template)
        except PageMapError as err:
            raise EditError(f"the page list cannot be changed: {err}") from err

    @property
    def changes_page_list(self) -> bool:
        return self.content is not None

    def removed(self) -> list[str]:
        """Pages of `doc` the edits deleted."""
        return sorted(p for p in self.deleted if p not in self.origin)

    def missing(self) -> list[str]:
        """Pages with edits that are not in the page list: deleted elsewhere."""
        return sorted(set(self.page_edits) - set(self.ids))

    # -- pages --

    def base(self, page_id: str, upto: int | None = None) -> bytes | None:
        """A page before its own edits (None: no file): the document's .rm, nothing for
        a new page, the source page for a copy."""
        origin = self.origin.get(page_id)
        if origin is None:
            name = f"{self.doc.id}/{page_id}.rm"
            return self.read(name) if name in self.doc.files else None
        if origin[0] == "new":
            return None
        if origin[0] == "import":
            sha = self._imported(origin, "{doc}/{page}.rm")
            return self.file(sha) if sha is not None else None
        return self._page(origin[1], origin[2])

    @staticmethod
    def _imported(origin: tuple, pattern: str) -> str | None:
        """The hash of an imported page's file kept as `pattern`, or None."""
        return next((sha for p, sha, _ in origin[1].files[origin[2]] if p == pattern), None)

    def file(self, sha256: str) -> bytes:
        """A pending file by hash (an imported page's)."""
        if self._files is None:
            raise EditError("the files of imported pages are not at hand")
        return self._files(sha256)

    def _page(self, page_id: str, count: int | None = None) -> bytes | None:
        edits = self.page_edits.get(page_id, [])
        edits = edits if count is None else edits[:count]
        base = self.base(page_id)
        if not edits:
            return base
        try:
            return page_with_edits(base, edits, self.author)
        except (RmParseError, ValueError) as err:
            raise EditError(f"the page cannot be edited: {err}") from err

    def page(self, page_id: str) -> bytes | None:
        """The page's .rm with all its edits (None: it has none)."""
        if page_id not in self._pages:
            self._pages[page_id] = self._page(page_id)
        return self._pages[page_id]

    def replay(self, page_id: str) -> Replay:
        try:
            return replay(self.base(page_id), self.page_edits.get(page_id, []), self.author)
        except (RmParseError, ValueError) as err:
            raise EditError(f"the page cannot be edited: {err}") from err

    def has_file(self, page_id: str) -> bool:
        """Whether the page has a .rm, from the document or made by its edits."""
        origin = self.origin.get(page_id)
        if self.page_edits.get(page_id):
            return True
        if origin is None:
            return f"{self.doc.id}/{page_id}.rm" in self.doc.files
        if origin[0] == "import":
            return self._imported(origin, "{doc}/{page}.rm") is not None
        return origin[0] == "copy" and (bool(origin[2]) or self.has_file(origin[1]))

    def image_source(self, page_id: str, file_name: str):
        """Where the page's image file `file_name` is: ("doc", name of a file of `doc`),
        ("edit", the ImageInsert that brought it), ("pending", sha256 of an imported
        page's file), or None."""
        for edit in self.page_edits.get(page_id, []):
            if isinstance(edit, ImageInsert) and edit.file_name == file_name:
                return "edit", edit
        origin = self.origin.get(page_id)
        if origin is None:
            name = f"{self.doc.id}/{page_id}/{file_name}"
            return ("doc", name) if name in self.doc.files else None
        if origin[0] == "import":
            sha = self._imported(origin, "{doc}/{page}/" + file_name)
            return ("pending", sha) if sha is not None else None
        return self.image_source(origin[1], file_name) if origin[0] == "copy" else None

    def image_data(self, source: tuple, read_edit: Callable[[ImageInsert], bytes]) -> bytes:
        """The bytes of an image_source() result."""
        if source[0] == "doc":
            return self.read(source[1])
        if source[0] == "pending":
            return self.file(source[1])
        return read_edit(source[1])

    def page_extras(self, page_id: str) -> list[tuple[str, Callable[[], bytes]]]:
        """(name in this document, its bytes) for the page files other than the .rm a
        copied or imported page takes with it: -metadata.json, highlights, thumbnail
        (and an imported page's whole folder), as the app's pagecopyhelper copies them."""
        origin, page = self.origin.get(page_id), page_id
        while origin is not None and origin[0] == "copy":
            page, origin = origin[1], self.origin.get(origin[1])
        if origin is not None and origin[0] == "import":
            return [(import_path(pattern, self.doc.id, page_id), lambda sha=sha: self.file(sha))
                    for pattern, sha, _ in origin[1].files[origin[2]]
                    if pattern != "{doc}/{page}.rm"]       # the page's folder too, as copied
        return [(new, lambda name=name: self.read(name)) for name, new in self.copied_files(page_id)]

    def copied_files(self, page_id: str) -> list[tuple[str, str]]:
        """(file of `doc`, its name for the copy) for the page files other than the .rm
        and images that a copy takes from the document's page it comes from."""
        origin, page = self.origin.get(page_id), page_id
        while origin is not None and origin[0] == "copy":
            page, origin = origin[1], self.origin.get(origin[1])
        if origin is not None or page == page_id:
            return []
        out = []
        for name in page_files(self.doc, page):
            if name.endswith(".rm") or name.startswith(f"{self.doc.id}/{page}/"):
                continue
            out.append((name, name.replace(page, page_id)))
        return out


def document_files(doc: Document, edits: list[Edit], read: Callable[[str], bytes],
                   image: Callable[[ImageInsert], bytes], author: bytes,
                   files: Callable[[str], bytes] | None = None) -> dict[str, bytes | None]:
    """The files of `doc` that `edits` add, change (name -> new bytes) or remove (name ->
    None); empty when nothing is left to do. `read(name)` gives a file `doc` has,
    `image(edit)` an inserted image's file, `files(sha256)` a kept file of an imported
    page. EditError if an edit no longer fits: its page was removed, or cannot be
    edited safely."""
    staged = Staged(doc, edits, read, author, files)
    missing = staged.missing()
    if missing:
        raise EditError("the page of an edit is no longer in the document")
    out: dict[str, bytes | None] = {}
    changed: dict[str, int] = {}                        # page id -> time of its newest edit
    for page_id in staged.ids:
        origin, page_edits = staged.origin.get(page_id), staged.page_edits.get(page_id, [])
        if origin is None and not page_edits:
            continue
        name = f"{doc.id}/{page_id}.rm"
        old = read(name) if origin is None and name in doc.files else None
        new = staged.page(page_id)
        if page_edits and new != staged.base(page_id):
            changed[page_id] = max(e.created_ms for e in page_edits)
        if new == old:
            continue
        if new is not None:
            out[name] = new
        used = set(page_images(new).values())
        for file_name in sorted(used):                  # an image file is written once
            path = f"{doc.id}/{page_id}/{file_name}"
            if path in doc.files:
                continue
            source = staged.image_source(page_id, file_name)
            if source is None:
                log.warning("page %s shows image %s, which has no file", page_id, file_name)
            else:
                out[path] = staged.image_data(source, image)
        if origin is None:
            for file_name in set(page_images(old).values()) - used:
                path = f"{doc.id}/{page_id}/{file_name}"
                if path in doc.files:                   # the page no longer uses it
                    out[path] = None
        else:
            for copy, data in staged.page_extras(page_id):
                out[copy] = data()
    for page_id in staged.removed():
        for name in page_files(doc, page_id):
            out[name] = None

    content_name, metadata_name = f"{doc.id}.content", f"{doc.id}.metadata"
    if not changed and not staged.changes_page_list and not staged.tool_settings:
        return out if out else {}
    original = json.loads(read(content_name))
    try:
        content = staged.content if staged.changes_page_list else current_content(doc, read, author)
    except (ValueError, PageMapError) as err:
        raise EditError(f"the page list cannot be written: {err}") from err
    listed = (content.get("cPages") or {}).get("pages") if isinstance(content.get("cPages"), dict) else None
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict) and entry.get("id") in changed:
            entry["modifed"] = _later(entry.get("modifed"), changed[entry["id"]])
    if staged.tool_settings:
        extra = content.get("extraMetadata")
        content["extraMetadata"] = {**(extra if isinstance(extra, dict) else {}), **staged.tool_settings}
    if content != original:
        out[content_name] = json_bytes(content)
    if not out:
        return {}
    if not changed and not staged.page_list_edits:
        # Pen settings alone change no page: lastModified is left as it is (whether the
        # app bumps it for a settings-only save has not been traced).
        return out
    newest = max([*changed.values(), *(e.created_ms for e in staged.page_list_edits)])
    metadata = json.loads(read(metadata_name))
    stamp = _later(metadata.get("lastModified"), newest)
    if stamp != metadata.get("lastModified"):           # times never go back: it may stay as it is
        metadata["lastModified"] = stamp
        out[metadata_name] = json_bytes(metadata)
    return out
