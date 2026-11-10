"""Document library model: documents, folders and the tree that links them.

Each library item is identified by a UUID and described by two JSON files
(ARCHITECTURE.md §5):

    <uuid>.metadata  visibleName, type (DocumentType|CollectionType), parent,
                     pinned, lastModified (ms epoch as string), deleted, ...
    <uuid>.content   fileType (notebook|pdf|epub), formatVersion, pageCount,
                     cPages (formatVersion 2) or pages (formatVersion 1), tags, ...

Parent is "" for the library root and "trash" for trashed items. Unknown JSON keys
are preserved in `raw` so nothing is lost when files are written back later.

New files (imports, folders) follow what the official apps write, as seen in a real
synced library: folders get `{"tags": []}` as .content, never-opened documents have
lastOpened "0", and every file is JSON with 4-space indentation and sorted keys (Qt's
QJsonDocument::Indented output).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator

log = logging.getLogger("rm.models")

ROOT_ID = ""
TRASH_ID = "trash"

DOCUMENT_TYPE = "DocumentType"
COLLECTION_TYPE = "CollectionType"


class FileType(str, Enum):
    NOTEBOOK = "notebook"
    PDF = "pdf"
    EPUB = "epub"
    UNKNOWN = ""

    @classmethod
    def parse(cls, value: Any) -> "FileType":
        try:
            return cls(value or "")
        except ValueError:
            return cls.UNKNOWN


def parse_uuid(value: str) -> str:
    """Return the canonical lower-case form of a UUID string, or raise ValueError."""
    return str(uuid.UUID(str(value).strip()))


def is_uuid(value: str) -> bool:
    try:
        parse_uuid(value)
    except ValueError:
        return False
    return True


def parse_timestamp(value: Any) -> datetime | None:
    """Parse reMarkable timestamps: ms epoch as str/int, or ISO 8601."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    log.debug("unparseable timestamp %r", value)
    return None


def ms_timestamp(when: float) -> str:
    """Seconds since the epoch → the files' millisecond timestamp string."""
    return str(int(when * 1000))


def _json_text(value: Any, depth: int) -> str:
    pad = "    " * depth
    if isinstance(value, dict):
        rows = [f"{pad}    {json.dumps(str(k), ensure_ascii=False)}: {_json_text(v, depth + 1)}"
                for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))]
        return "{\n" + "".join(row + ",\n" for row in rows)[:-2] + ("\n" if rows else "") + pad + "}"
    if isinstance(value, (list, tuple)):
        rows = [f"{pad}    {_json_text(v, depth + 1)}" for v in value]
        return "[\n" + "".join(row + ",\n" for row in rows)[:-2] + ("\n" if rows else "") + pad + "]"
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return str(int(value))                  # Qt writes a whole number without ".0"
    return json.dumps(value, ensure_ascii=False)


def json_bytes(raw: dict[str, Any]) -> bytes:
    """Serialise a .metadata/.content file the way the tablet and the apps write them
    (Qt's QJsonDocument::toJson): keys sorted, four spaces a level, and an empty object
    or array on two lines ("{", then "}" at its own indent). Parsing a file the tablet
    wrote and serialising it again gives the same bytes, so changing one field of a
    file changes only that field."""
    return (_json_text(raw, 0) + "\n").encode("utf-8")


def new_metadata(name: str, parent: str, item_type: str, now_ms: str) -> dict[str, Any]:
    raw: dict[str, Any] = {"createdTime": now_ms, "lastModified": now_ms, "parent": parent,
                           "pinned": False, "type": item_type, "visibleName": name}
    if item_type == DOCUMENT_TYPE:
        raw.update(lastOpened="0", lastOpenedPage=0)
    return raw


def new_folder_content() -> dict[str, Any]:
    return {"tags": []}


def new_document_content(file_type: "FileType", page_count: int, size: int) -> dict[str, Any]:
    """.content for an imported PDF/EPUB. No page list: the tablet builds one (with
    its CRDT ids) the first time the document is opened, as for other uploads."""
    return {
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": file_type.value,
        "fontName": "",
        "formatVersion": 1,
        "lineHeight": -1,
        "margins": 125,
        "orientation": "portrait",
        "pageCount": page_count,
        "pageTags": [],
        "sizeInBytes": str(size),
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }


def unique_name(base: str, taken: set[str]) -> str:
    """`base`, or "base 2", "base 3", ... if that name is already used."""
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load(data: bytes | str | dict | None) -> dict[str, Any]:
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


@dataclass
class Metadata:
    visible_name: str = ""
    type: str = DOCUMENT_TYPE
    parent: str = ROOT_ID
    pinned: bool = False
    deleted: bool = False
    last_modified: datetime | None = None
    last_opened: datetime | None = None
    created_time: datetime | None = None
    last_opened_page: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: bytes | str | dict) -> "Metadata":
        raw = _load(data)
        return cls(
            visible_name=str(raw.get("visibleName", "")),
            type=raw.get("type", DOCUMENT_TYPE),
            parent=str(raw.get("parent") or ROOT_ID),
            # Older files use "bookmarked" for what is now "pinned".
            pinned=bool(raw.get("pinned", raw.get("bookmarked", False))),
            deleted=bool(raw.get("deleted", False)),
            last_modified=parse_timestamp(raw.get("lastModified")),
            last_opened=parse_timestamp(raw.get("lastOpened")),
            created_time=parse_timestamp(raw.get("createdTime")),
            last_opened_page=_int(raw.get("lastOpenedPage")),
            raw=raw,
        )

    @property
    def is_collection(self) -> bool:
        return self.type == COLLECTION_TYPE


@dataclass
class Page:
    id: str
    template: str = ""
    deleted: bool = False
    # Index of the page in the document's PDF/EPUB rendition; None for pages
    # inserted on the tablet (no background).
    redirect: int | None = None


@dataclass
class Content:
    file_type: FileType = FileType.UNKNOWN
    format_version: int = 1
    page_count: int = 0
    pages: list[Page] = field(default_factory=list)
    orientation: str = ""
    tags: list[str] = field(default_factory=list)
    cover_page_number: int = 0
    size_in_bytes: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: bytes | str | dict | None) -> "Content":
        raw = _load(data)
        fmt = int(raw.get("formatVersion") or 1)
        pages: list[Page] = []
        c_pages = raw.get("cPages")
        if isinstance(c_pages, dict) and isinstance(c_pages.get("pages"), list):
            # formatVersion 2: CRDT page list ordered by idx.value.
            entries = sorted(c_pages["pages"],
                             key=lambda p: str((p.get("idx") or {}).get("value", "")))
            for p in entries:
                redir = (p.get("redir") or {}).get("value")
                pages.append(Page(
                    id=str(p.get("id", "")),
                    template=str((p.get("template") or {}).get("value", "")),
                    # A register: {"value": 1} deleted, {"value": 0} deleted and put back
                    # (undeleted on another device: the page is live and counted).
                    deleted=bool((p.get("deleted") or {}).get("value")) if isinstance(p.get("deleted"), dict)
                    else bool(p.get("deleted")),
                    redirect=int(redir) if isinstance(redir, (int, float)) else None,
                ))
        elif isinstance(raw.get("pages"), list):
            # formatVersion 1: ordered page UUIDs; redirectionPageMap holds the
            # PDF page per page, -1 for inserted pages.
            redirects = raw.get("redirectionPageMap") or []
            for i, pid in enumerate(raw["pages"]):
                r = redirects[i] if i < len(redirects) else None
                pages.append(Page(id=str(pid),
                                  redirect=r if isinstance(r, int) and r >= 0 else None))
        tags = []
        for tag in raw.get("tags") or []:
            tags.append(tag.get("name", "") if isinstance(tag, dict) else str(tag))
        live = [p for p in pages if not p.deleted]
        return cls(
            file_type=FileType.parse(raw.get("fileType")),
            format_version=fmt,
            page_count=int(raw.get("pageCount") or len(live)),
            pages=pages,
            orientation=str(raw.get("orientation", "")),
            tags=[t for t in tags if t],
            cover_page_number=int(raw.get("coverPageNumber") or 0),
            size_in_bytes=int(raw.get("sizeInBytes") or 0),
            raw=raw,
        )

    @property
    def page_ids(self) -> list[str]:
        return [p.id for p in self.pages if not p.deleted]


@dataclass
class Node:
    """Common base for documents and folders."""

    id: str
    metadata: Metadata
    # Hash of this entry's index in the cloud tree; identifies the remote version.
    hash: str = ""
    # Every file in the entry's index: name -> blob hash.
    files: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.metadata.visible_name

    @property
    def parent_id(self) -> str:
        return self.metadata.parent

    @property
    def pinned(self) -> bool:
        return self.metadata.pinned

    @property
    def last_modified(self) -> datetime | None:
        return self.metadata.last_modified

    @property
    def is_folder(self) -> bool:
        return False

    @property
    def in_trash(self) -> bool:
        return self.metadata.parent == TRASH_ID


@dataclass
class Folder(Node):
    # A folder's .content holds only its tags ({"tags": [...]}).
    content: Content = field(default_factory=Content)

    @property
    def is_folder(self) -> bool:
        return True


@dataclass
class Document(Node):
    content: Content = field(default_factory=Content)

    @property
    def file_type(self) -> FileType:
        return self.content.file_type

    @property
    def page_count(self) -> int:
        return self.content.page_count


def make_node(node_id: str, metadata: Metadata, content: Content | None = None,
              hash: str = "", files: dict[str, str] | None = None) -> Node:
    if metadata.is_collection:
        return Folder(id=node_id, metadata=metadata, hash=hash, files=dict(files or {}),
                      content=content or Content())
    return Document(id=node_id, metadata=metadata, hash=hash, files=dict(files or {}),
                    content=content or Content())


class DocumentTree:
    """In-memory library tree with traversal helpers.

    The root ("") and trash ("trash") are virtual: they have no files of their own.
    Items whose parent is unknown (e.g. not synced yet) are listed as orphans and
    shown under the root so they stay reachable.
    """

    def __init__(self, nodes: list[Node] | None = None) -> None:
        self.nodes: dict[str, Node] = {}
        self._children: dict[str, list[str]] = {}
        for node in nodes or []:
            self.nodes[node.id] = node
        self._index()

    def _index(self) -> None:
        self._children = {ROOT_ID: [], TRASH_ID: []}
        self.orphans: list[str] = []
        for node in self.nodes.values():
            parent = node.parent_id
            if parent not in (ROOT_ID, TRASH_ID) and parent not in self.nodes:
                self.orphans.append(node.id)
                parent = ROOT_ID
            self._children.setdefault(parent, []).append(node.id)
        for ids in self._children.values():
            ids.sort(key=lambda i: (not self.nodes[i].is_folder, self.nodes[i].name.lower(), i))

    def add(self, node: Node) -> None:
        self.nodes[node.id] = node
        self._index()

    def remove(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self._index()

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.nodes

    def get(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def children(self, folder_id: str = ROOT_ID) -> list[Node]:
        """Direct children, folders first then by name."""
        return [self.nodes[i] for i in self._children.get(folder_id, [])]

    def walk(self, folder_id: str = ROOT_ID) -> Iterator[tuple[Node, int]]:
        """Depth-first (node, depth) traversal below `folder_id`, cycle-safe."""
        seen: set[str] = set()
        stack = [(n, 0) for n in reversed(self.children(folder_id))]
        while stack:
            node, depth = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            yield node, depth
            if node.is_folder:
                stack.extend((c, depth + 1) for c in reversed(self.children(node.id)))

    def breadcrumbs(self, node_id: str) -> list[Node]:
        """Ancestors from the top-level folder down to the node itself.

        Stops (rather than looping forever) on a parent cycle or missing parent.
        """
        crumbs: list[Node] = []
        seen: set[str] = set()
        current = self.nodes.get(node_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            crumbs.append(current)
            current = self.nodes.get(current.parent_id)
        return list(reversed(crumbs))

    def path(self, node_id: str, sep: str = "/") -> str:
        crumbs = self.breadcrumbs(node_id)
        prefix = "trash:" if crumbs and crumbs[0].in_trash else ""
        return prefix + sep + sep.join(n.name for n in crumbs)

    def search(self, query: str, *, include_trash: bool = False,
               folders: bool = True, documents: bool = True) -> list[Node]:
        """Case-insensitive substring search on visible names."""
        q = query.casefold()
        results = []
        for node in self.nodes.values():
            if not include_trash and self.is_trashed(node.id):
                continue
            if node.is_folder and not folders or not node.is_folder and not documents:
                continue
            if q in node.name.casefold():
                results.append(node)
        return sorted(results, key=lambda n: (n.name.casefold(), n.id))

    def descendants(self, node_id: str) -> list[Node]:
        """Everything below a folder (cycle-safe)."""
        return [node for node, _ in self.walk(node_id)]

    def is_within(self, node_id: str, ancestor_id: str) -> bool:
        """True if `node_id` is `ancestor_id` or lies below it."""
        return any(n.id == ancestor_id for n in self.breadcrumbs(node_id))

    def is_trashed(self, node_id: str) -> bool:
        crumbs = self.breadcrumbs(node_id)
        return bool(crumbs) and crumbs[0].in_trash

    def documents(self) -> list[Document]:
        return [n for n in self.nodes.values() if isinstance(n, Document)]

    def folders(self) -> list[Folder]:
        return [n for n in self.nodes.values() if isinstance(n, Folder)]
