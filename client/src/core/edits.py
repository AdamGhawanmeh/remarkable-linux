"""Edits made in this client that the cloud does not have yet.

The app edits a document's files on disk and its sync uploads them later. Here a
document is the cloud's files, cached by hash, so an edit is kept as what was done (an
image put on a page), not as files: PendingEdits stores it under <cache>/pending/, and
document_files() works out the files it changes from the document as it is now. The
viewer draws from that (LibrarySource), and SyncClient.push_edits uploads it; when
another device changed the document in between, the same edit is applied to the newer
version.

Three kinds: an image put on a page (ImageInsert), an image moved, scaled or turned
(ImageTransform), an image deleted (ImageDelete). The last two name the image by its key
in the page's images table, which stays the same however often it is moved, so they
apply as well to an image of the tablet's as to one put on the page here.

What an inserted image changes, as in the app (reMarkable.exe 3.29):
    <doc>/<page>/<uuid>.png|.jpg   the image file, added
    <doc>/<page>.rm                the page, with the image item (viewer.rm_writer)
    <doc>.content                  that page's "modifed", the time of the edit
    <doc>.metadata                 "lastModified", the time of the edit
Times are milliseconds as decimal strings, and never move backwards. A moved image
changes the page, .content and .metadata; a deleted one also takes its file out of the
document once no item on the page shows it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from viewer import rm_writer
from viewer.rm_parser import RmParseError

from .config import atomic_write_json, ensure_private_dir, read_json
from .models import Document, json_bytes

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
                   sha256, int(size), int(created_ms), _timestamps(timestamps))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageInsert":
        return cls(str(raw["doc_id"]), str(raw["page_id"]), str(raw["file_name"]), str(raw["key"]),
                   _corners(raw["vertices"]), str(raw["sha256"]), int(raw["size"]), int(raw["created_ms"]),
                   _timestamps(raw.get("timestamps")))


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

    @classmethod
    def new(cls, doc_id: str, page_id: str, key, vertices, created_ms: int) -> "ImageTransform":
        return cls(doc_id, page_id, _key(key), _corners(vertices), int(created_ms))

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "ImageTransform":
        return cls.new(raw["doc_id"], raw["page_id"], raw["key"], raw["vertices"], raw["created_ms"])


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


Edit = ImageInsert | ImageTransform | ImageDelete
KINDS: dict[str, type] = {"image": ImageInsert, "image-transform": ImageTransform, "image-delete": ImageDelete}


class PendingEdits:
    """The edits not uploaded yet, oldest first, kept in <root>/edits.json with the image
    files beside it (by hash). Safe to use from several threads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._edits: list[Edit] = []
        try:
            raw = read_json(root / EDITS_FILE) or {}
            self._edits = [KINDS[e["kind"]].load(e) for e in raw.get("edits") or []]
        except (ValueError, KeyError, TypeError, OSError, EditError) as err:
            # Edits that cannot be read are not uploaded as something else: start empty.
            log.error("ignoring unreadable %s: %s", root / EDITS_FILE, err)

    def _file(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise EditError(f"not a sha256 hex digest: {sha256!r}")
        return self.root / "files" / sha256

    def _save(self) -> None:
        ensure_private_dir(self.root)
        atomic_write_json(self.root / EDITS_FILE,
                          {"version": 1, "edits": [{"kind": _kind(e), **asdict(e)} for e in self._edits]})

    def all(self) -> list[Edit]:
        with self._lock:
            return list(self._edits)

    def of(self, doc_id: str) -> list[Edit]:
        with self._lock:
            return [e for e in self._edits if e.doc_id == doc_id]

    def documents(self) -> list[str]:
        return list(dict.fromkeys(e.doc_id for e in self.all()))

    def add(self, edit: Edit, data: bytes | None = None) -> None:
        """Keep `edit` (and an inserted image's file: first, so an edit on record always
        has its file)."""
        with self._lock:
            if isinstance(edit, ImageInsert):
                if data is None or hashlib.sha256(data).hexdigest() != edit.sha256 or len(data) != edit.size:
                    raise EditError("the image is not the one the edit describes")
                path = self._file(edit.sha256)
                ensure_private_dir(self.root)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            self._edits.append(edit)
            self._save()

    def data(self, edit: ImageInsert) -> bytes:
        try:
            return self._file(edit.sha256).read_bytes()
        except OSError as err:
            raise EditError(f"the image of an edit is missing: {err}") from err

    def discard(self, edits: list[Edit]) -> None:
        """Forget edits (uploaded, or given up), and image files no edit uses any more."""
        with self._lock:
            gone = set(edits)
            self._edits = [e for e in self._edits if e not in gone]
            self._save()
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


def page_with_edits(page: bytes | None, edits: list[Edit], author: bytes) -> bytes | None:
    """`page` (None: it has no .rm yet) with the edits made to it, in order. An edit the
    page has already (an upload that went through without being noticed) changes
    nothing; so does a move or delete of an image the page no longer shows (deleted
    meanwhile, on the tablet or here: the delete wins)."""
    for edit in edits:
        key = bytes.fromhex(edit.key)
        if isinstance(edit, ImageInsert):
            if key not in page_images(page):
                page = rm_writer.append_image(page, author=author, key=key, file_name=edit.file_name,
                                              vertices=list(edit.vertices), timestamps=edit.timestamps)
        elif page is None:
            continue
        elif isinstance(edit, ImageTransform):
            page = rm_writer.transform_image(page, author=author, key=key, vertices=list(edit.vertices)) or page
        elif isinstance(edit, ImageDelete):
            page, _ = rm_writer.delete_image(page, author=author, key=key)
        else:
            raise EditError(f"unknown edit {type(edit).__name__}")
    return page


def _later(old: Any, new_ms: int) -> str:
    """A millisecond timestamp string moved forward to `new_ms`, never back."""
    try:
        return str(max(int(old), new_ms))
    except (TypeError, ValueError):
        return str(new_ms)


def document_files(doc: Document, edits: list[Edit], read: Callable[[str], bytes],
                   image: Callable[[ImageInsert], bytes], author: bytes) -> dict[str, bytes | None]:
    """The files of `doc` that `edits` add, change (name -> new bytes) or remove (name ->
    None); empty when nothing is left to do. `read(name)` gives a file `doc` has,
    `image(edit)` an inserted image's file. EditError if an edit no longer fits: its page
    was removed, or cannot be edited safely."""
    out: dict[str, bytes | None] = {}
    pages: dict[str, list[Edit]] = {}
    for edit in edits:
        if edit.doc_id != doc.id:
            raise EditError("an edit of another document")
        pages.setdefault(edit.page_id, []).append(edit)
    live = {p.id for p in doc.content.pages if not p.deleted}
    changed: dict[str, int] = {}                        # page id -> time of its newest edit
    for page_id, page_edits in pages.items():
        if page_id not in live:
            raise EditError("the page of an edit is no longer in the document")
        name = f"{doc.id}/{page_id}.rm"
        old = read(name) if name in doc.files else None
        try:
            new = page_with_edits(old, page_edits, author)
        except (RmParseError, ValueError) as err:
            raise EditError(f"the page cannot be edited: {err}") from err
        if new == old:
            continue
        out[name] = new
        changed[page_id] = max(e.created_ms for e in page_edits)
        used = set(page_images(new).values())
        for edit in page_edits:                         # an image file is written once
            if isinstance(edit, ImageInsert) and edit.path not in doc.files and edit.file_name in used:
                out[edit.path] = image(edit)
        for file_name in set(page_images(old).values()) - used:
            path = f"{doc.id}/{page_id}/{file_name}"
            if path in doc.files:                       # the page no longer uses it
                out[path] = None
    if not changed:
        return {}

    content_name, metadata_name = f"{doc.id}.content", f"{doc.id}.metadata"
    content = json.loads(read(content_name))
    listed = (content.get("cPages") or {}).get("pages") if isinstance(content.get("cPages"), dict) else None
    touched = False
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict) and entry.get("id") in changed:
            stamp = _later(entry.get("modifed"), changed[entry["id"]])
            touched = touched or entry.get("modifed") != stamp
            entry["modifed"] = stamp
    if touched:
        out[content_name] = json_bytes(content)
    metadata = json.loads(read(metadata_name))
    metadata["lastModified"] = _later(metadata.get("lastModified"), max(changed.values()))
    out[metadata_name] = json_bytes(metadata)
    return out
