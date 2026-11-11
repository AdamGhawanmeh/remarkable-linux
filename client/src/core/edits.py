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

Undo and redo (PageHistory) work as in reMarkable.exe (SceneActionStack): they do not
take edits back but make new ones, so they upload like any other edit.

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
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from viewer import rm_writer
from viewer.rm_parser import RmParseError

from .config import atomic_write_json, ensure_private_dir, read_json
from .models import Document, json_bytes
from .pagemap import PageMap, PageMapError

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

    @property
    def path(self) -> str:
        """The file's name in the document."""
        return f"{self.doc_id}/{self.page_id}/{self.file_name}"

    @classmethod
    def new(cls, doc_id: str, page_id: str, extension: str, vertices, sha256: str, size: int,
            created_ms: int, timestamps=None) -> "ImageInsert":
        """With a new file name and key: two random UUIDs, as the app makes them. The key
        is stored as the tablet's is, the UUID's bytes last to first."""
        if extension not in IMAGE_EXTENSIONS:
            raise EditError(f"images are stored as {' or '.join(IMAGE_EXTENSIONS)}, not {extension!r}")
        corners = _corners(vertices)
        return cls(doc_id, page_id, f"{uuid.uuid4()}.{extension}", uuid.uuid4().bytes[::-1].hex(), corners,
                   sha256, int(size), int(created_ms), _timestamps(timestamps), uuid.uuid4().hex)

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageInsert":
        return cls(str(raw["doc_id"]), str(raw["page_id"]), str(raw["file_name"]), str(raw["key"]),
                   _corners(raw["vertices"]), str(raw["sha256"]), int(raw["size"]), int(raw["created_ms"]),
                   _timestamps(raw.get("timestamps")), str(raw.get("uid") or ""))


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


PAGE_EDITS = (PageAdd, PageCopy, PageMove, PageDelete)
DEFAULT_TEMPLATE = "Blank"      # the template of the page a document left empty gets (0x140817f50)

Edit = ImageInsert | ImageTransform | ImageDelete | ImageRestore | PageAdd | PageCopy | PageMove | PageDelete
KINDS: dict[str, type] = {"image": ImageInsert, "image-transform": ImageTransform, "image-delete": ImageDelete,
                          "image-restore": ImageRestore, "page-add": PageAdd, "page-copy": PageCopy,
                          "page-move": PageMove, "page-delete": PageDelete}


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
        with self._lock:
            self._refresh()

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

    def add(self, edit: Edit, data: bytes | None = None) -> None:
        """Keep `edit` (and an inserted image's file: first, so an edit on record always
        has its file)."""
        if isinstance(edit, ImageInsert) and (data is None or hashlib.sha256(data).hexdigest() != edit.sha256
                                              or len(data) != edit.size):
            raise EditError("the image is not the one the edit describes")
        with self._changing():
            if isinstance(edit, ImageInsert):
                path = self._file(edit.sha256)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            self._edits.append(edit)

    def data(self, edit: ImageInsert) -> bytes:
        try:
            return self._file(edit.sha256).read_bytes()
        except OSError as err:
            raise EditError(f"the image of an edit is missing: {err}") from err

    def discard(self, edits: list[Edit]) -> None:
        """Forget edits (uploaded, or given up), and image files no edit uses any more."""
        gone = set(edits)
        with self._changing():
            self._edits = [e for e in self._edits if e not in gone]
            used = {e.sha256 for e in self._edits if isinstance(e, ImageInsert)}
            for sha in {e.sha256 for e in gone if isinstance(e, ImageInsert)} - used:
                self._file(sha).unlink(missing_ok=True)

    def clear(self) -> None:
        self.discard(self.all())


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
    made: dict[str, tuple[int, int]] = {}
    removed: dict[tuple[int, int], rm_writer.ImageValue] = {}
    deleted: set[bytes] = set()

    def note(uid: str, key: bytes) -> None:
        live = rm_writer.image_item(page, key)
        if uid and live is not None:
            made[uid] = live[0]

    for edit in edits:
        key = bytes.fromhex(edit.key)
        if isinstance(edit, ImageInsert):
            if key not in page_images(page):
                page = rm_writer.append_image(page, author=author, key=key, file_name=edit.file_name,
                                              vertices=list(edit.vertices), timestamps=edit.timestamps)
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
    return Replay(page, made, removed)


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


def swapped(changes: tuple[Change, ...], doc_id: str, page_id: str, now_ms: int) -> tuple[list[Edit], tuple[Change, ...]]:
    """(the edits that undo, or redo, `changes`; the changes that take them back again).
    For each image, in order: the item showing it is deleted, and the item to put back
    comes back as a new item (ImageRestore), as the app's undo and redo remove one half
    of an action and add the other (0x1408c5910, then 0x1408cbd60)."""
    edits: list[Edit] = []
    back: list[Change] = []
    for change in changes:
        if change.remove is not None:
            edits.append(ImageDelete.new(doc_id, page_id, change.key, now_ms))
        made = None
        if change.readd is not None:
            restore = ImageRestore.new(doc_id, page_id, change.key, change.readd, now_ms)
            edits.append(restore)
            made = ("edit", restore.uid)
        back.append(Change(change.key, change.remove, made))
    return edits, tuple(back)


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
        self.undo: list[tuple[Change, ...]] = []
        self.redo: list[tuple[Change, ...]] = []

    def push(self, changes: list[Change] | tuple[Change, ...]) -> None:
        if changes:
            self.undo.append(tuple(changes))
            self.redo.clear()


def _later(old: Any, new_ms: int) -> str:
    """A millisecond timestamp string moved forward to `new_ms`, never back."""
    try:
        return str(max(int(old), new_ms))
    except (TypeError, ValueError):
        return str(new_ms)


# ------------------------------------------------------------------ a document with its edits

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

    def __init__(self, doc: Document, edits: list[Edit], read: Callable[[str], bytes], author: bytes) -> None:
        self.doc, self.read, self.author = doc, read, author
        self.page_edits: dict[str, list[Edit]] = {}
        # page id -> ("new",) | ("copy", source page id, how many of its edits the copy has)
        self.origin: dict[str, tuple] = {}
        self.deleted: set[str] = set()                  # pages the edits deleted
        self.page_list_edits: list[Edit] = []
        self.content: dict | None = None                # .content with the new page list
        self._pages: dict[str, bytes | None] = {}
        live = [p.id for p in doc.content.pages if not p.deleted]
        pagemap: PageMap | None = None
        for edit in edits:
            if edit.doc_id != doc.id:
                raise EditError("an edit of another document")
            if not isinstance(edit, PAGE_EDITS):
                if edit.page_id not in self.deleted:   # edits of a page deleted after them are moot
                    self.page_edits.setdefault(edit.page_id, []).append(edit)
                continue
            if pagemap is None:
                try:
                    pagemap = PageMap(json.loads(read(f"{doc.id}.content")), author)
                except (ValueError, PageMapError) as err:
                    raise EditError(f"the page list cannot be changed: {err}") from err
            self.page_list_edits.append(edit)
            self._apply(pagemap, edit)
        if pagemap is not None:
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

    def _apply(self, pagemap: PageMap, edit: Edit) -> None:
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
        return self._page(origin[1], origin[2])

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
        return origin[0] == "copy" and (bool(origin[2]) or self.has_file(origin[1]))

    def image_source(self, page_id: str, file_name: str):
        """Where the page's image file `file_name` is: ("doc", name of a file of `doc`),
        ("edit", the ImageInsert that brought it), or None."""
        for edit in self.page_edits.get(page_id, []):
            if isinstance(edit, ImageInsert) and edit.file_name == file_name:
                return "edit", edit
        origin = self.origin.get(page_id)
        if origin is None:
            name = f"{self.doc.id}/{page_id}/{file_name}"
            return ("doc", name) if name in self.doc.files else None
        return self.image_source(origin[1], file_name) if origin[0] == "copy" else None

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
                   image: Callable[[ImageInsert], bytes], author: bytes) -> dict[str, bytes | None]:
    """The files of `doc` that `edits` add, change (name -> new bytes) or remove (name ->
    None); empty when nothing is left to do. `read(name)` gives a file `doc` has,
    `image(edit)` an inserted image's file. EditError if an edit no longer fits: its page
    was removed, or cannot be edited safely."""
    staged = Staged(doc, edits, read, author)
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
                out[path] = read(source[1]) if source[0] == "doc" else image(source[1])
        if origin is None:
            for file_name in set(page_images(old).values()) - used:
                path = f"{doc.id}/{page_id}/{file_name}"
                if path in doc.files:                   # the page no longer uses it
                    out[path] = None
        else:
            for source, copy in staged.copied_files(page_id):
                out[copy] = read(source)
    for page_id in staged.removed():
        for name in page_files(doc, page_id):
            out[name] = None

    content_name, metadata_name = f"{doc.id}.content", f"{doc.id}.metadata"
    if not changed and not staged.changes_page_list:
        return out if out else {}
    original = json.loads(read(content_name))
    content = staged.content if staged.changes_page_list else json.loads(read(content_name))
    listed = (content.get("cPages") or {}).get("pages") if isinstance(content.get("cPages"), dict) else None
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict) and entry.get("id") in changed:
            entry["modifed"] = _later(entry.get("modifed"), changed[entry["id"]])
    if content != original:
        out[content_name] = json_bytes(content)
    if not out:
        return {}
    newest = max([*changed.values(), *(e.created_ms for e in staged.page_list_edits)])
    metadata = json.loads(read(metadata_name))
    stamp = _later(metadata.get("lastModified"), newest)
    if stamp != metadata.get("lastModified"):           # times never go back: it may stay as it is
        metadata["lastModified"] = stamp
        out[metadata_name] = json_bytes(metadata)
    return out
