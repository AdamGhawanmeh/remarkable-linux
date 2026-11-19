"""Library queries for the navigator, and page/PDF access for the viewer.

Qt-free so the navigation rules can be tested on their own. The views and their
order mirror the official sidebar (qml/client/Sidebar.qml):

    My files | Notebooks, PDFs, Ebooks, Favorites, Tags | Import, Trash, Settings
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from core.models import (ROOT_ID, TRASH_ID, Content, Document, DocumentTree, FileType, Folder, Node,
                         Page)
from core.edits import (PAGE_EDITS, Change, Edit, EditError, ImageDelete, ImageInsert, ImageTransform, ItemsTransform,
                        LayerAdd, LayerAttachChange, LayerDelete, LayerMergeChange, LayerMergeDown, LayerMove,
                        LayerMoveChange, LayerRename, LayerRenameChange, LayerVisibility, LayerVisibilityChange,
                        PageAdd, PageCopy, PageDelete, PageHistory, PageMove, PageTemplate, SceneFlagChange, SceneFlags,
                        Staged, StrokeAdd, StrokeChange, StrokeDelete, StrokeErase, StrokePart, StrokePaste,
                        ToolSettings, document_files, page_images, swapped)
from core.sync import SyncClient, SyncError, build_index, file_entry, sha256_hex
from core.config import atomic_write_json, read_json
from viewer import rm_layer, rm_select, rm_stroke, rm_writer
from viewer.rm_parser import PageScene, RmParseError, parse
from viewer.templates import parse_pagedata

EDITABLE_COPIES = "editable-copies.json"    # in the cache: copies of editable notebooks (their ids)
log = logging.getLogger("rm.ui.library")


class ViewFilter(Enum):
    MY_FILES = "my_files"
    NOTEBOOKS = "notebooks"
    PDFS = "pdfs"
    EBOOKS = "ebooks"
    FAVORITES = "favorites"
    TAGS = "tags"
    TRASH = "trash"


@dataclass(frozen=True)
class SidebarItem:
    label: str
    icon: str
    view: ViewFilter | None      # None: an action, or not implemented in this client yet
    group: int                   # dividers separate groups
    action: str = ""             # e.g. "import": runs instead of showing a view


# Labels and icons from Sidebar.qml (qsTr strings and qrc:/ark/icons/* names).
SIDEBAR: tuple[SidebarItem, ...] = (
    SidebarItem("My files", "my_files", ViewFilter.MY_FILES, 0),
    SidebarItem("Notebooks", "notebook", ViewFilter.NOTEBOOKS, 1),
    SidebarItem("PDFs", "document", ViewFilter.PDFS, 1),
    SidebarItem("Ebooks", "ebook", ViewFilter.EBOOKS, 1),
    SidebarItem("Favorites", "star", ViewFilter.FAVORITES, 1),
    SidebarItem("Tags", "tag", ViewFilter.TAGS, 1),
    SidebarItem("Import", "import", None, 2, action="import"),
    SidebarItem("Trash", "trashcan", ViewFilter.TRASH, 2),
    SidebarItem("Settings", "cog", None, 2),
)

VIEW_TITLES = {item.view: item.label for item in SIDEBAR if item.view}


class SortOrder(Enum):
    # Labels from SortOrderButton.qml sortOrderLabel(); "File size" is not offered
    # (the size the app shows is not in the synced metadata).
    LAST_MODIFIED = "Last modified"
    RECENTLY_OPENED = "Last opened"
    DATE_CREATED = "Date created"
    ALPHABETICAL = "Alphabetical (A-Z)"
    PAGE_COUNT = "Page count"

    def label(self, in_trash: bool = False) -> str:
        # In the trash the modification time is when the item was deleted.
        return "Date removed" if in_trash and self is SortOrder.LAST_MODIFIED else self.value


@dataclass(frozen=True)
class Location:
    view: ViewFilter = ViewFilter.MY_FILES
    folder_id: str = ROOT_ID       # inside My files or Trash
    tag: str | None = None         # inside Tags
    search: str = ""               # non-empty: library-wide name search


@dataclass(frozen=True)
class Crumb:
    label: str
    location: Location


@dataclass(frozen=True)
class Entry:
    kind: str                      # "folder" | "tag" | "document"
    title: str
    subtext: str
    node: Node | None = None       # folder/document
    tag: str | None = None
    pinned: bool = False
    count: int = 0                 # items in a folder / documents with a tag


_FILTER_TYPES = {
    ViewFilter.NOTEBOOKS: {FileType.NOTEBOOK},
    ViewFilter.PDFS: {FileType.PDF},
    ViewFilter.EBOOKS: {FileType.EPUB},
}

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def relative_time(when: datetime | None, now: datetime | None = None) -> str:
    """Short relative date for subtexts. The app's own `entry.subtext` is computed in
    C++; this is an approximation."""
    if when is None:
        return ""
    now = now or datetime.now(timezone.utc)
    days = (now.date() - when.astimezone(now.tzinfo).date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if when.year == now.year:
        return when.strftime("%b %-d")
    return when.strftime("%b %-d, %Y")


def document_tags(doc: Document) -> set[str]:
    tags = set(doc.content.tags)
    for tag in doc.content.raw.get("pageTags") or []:
        name = tag.get("name") if isinstance(tag, dict) else None
        if name:
            tags.add(name)
    return tags


class LibraryQuery:
    """Navigator queries over a DocumentTree."""

    def __init__(self, tree: DocumentTree, now: datetime | None = None) -> None:
        self.tree = tree
        self.now = now

    # -- entries ---------------------------------------------------------------

    def entries(self, loc: Location, sort: SortOrder = SortOrder.LAST_MODIFIED) -> list[Entry]:
        """Folders/tags first (as in the grid's folder header), then documents."""
        if loc.search.strip():
            nodes = [n for n in self.tree.search(loc.search.strip())]
            return self._to_entries(nodes, sort)
        if loc.view in (ViewFilter.MY_FILES, ViewFilter.TRASH):
            folder = loc.folder_id or (TRASH_ID if loc.view == ViewFilter.TRASH else ROOT_ID)
            return self._to_entries(self.tree.children(folder), sort)
        live = [n for n in self.tree.nodes.values() if not self.tree.is_trashed(n.id)]
        if loc.view in _FILTER_TYPES:
            types = _FILTER_TYPES[loc.view]
            return self._to_entries([n for n in live if isinstance(n, Document)
                                     and n.file_type in types], sort)
        if loc.view == ViewFilter.FAVORITES:
            return self._to_entries([n for n in live if n.pinned], sort)
        if loc.view == ViewFilter.TAGS:
            if loc.tag is None:
                counts: dict[str, int] = {}
                for doc in live:
                    if isinstance(doc, Document):
                        for tag in document_tags(doc):
                            counts[tag] = counts.get(tag, 0) + 1
                return [Entry("tag", tag, _items(n), tag=tag, count=n)
                        for tag, n in sorted(counts.items(), key=lambda kv: kv[0].casefold())]
            return self._to_entries([n for n in live if isinstance(n, Document)
                                     and loc.tag in document_tags(n)], sort)
        raise ValueError(f"unknown view {loc.view}")

    def _to_entries(self, nodes: list[Node], sort: SortOrder) -> list[Entry]:
        folders = [n for n in nodes if isinstance(n, Folder)]
        documents = [n for n in nodes if isinstance(n, Document)]
        return ([self.entry(n) for n in self._sorted(folders, sort)]
                + [self.entry(n) for n in self._sorted(documents, sort)])

    def _sorted(self, nodes: list[Node], sort: SortOrder) -> list[Node]:
        by_name = sorted(nodes, key=lambda n: (n.name.casefold(), n.id))
        if sort == SortOrder.ALPHABETICAL:
            return by_name
        key = {
            SortOrder.LAST_MODIFIED: lambda n: n.last_modified or _EPOCH,
            SortOrder.RECENTLY_OPENED: lambda n: n.metadata.last_opened or _EPOCH,
            SortOrder.DATE_CREATED: lambda n: n.metadata.created_time or _EPOCH,
            SortOrder.PAGE_COUNT: lambda n: n.page_count if isinstance(n, Document) else 0,
        }[sort]
        return sorted(by_name, key=key, reverse=True)   # stable: ties stay alphabetical

    def entry(self, node: Node) -> Entry:
        if isinstance(node, Folder):
            n = len(self.tree.children(node.id))
            return Entry("folder", node.name, _items(n), node=node, pinned=node.pinned, count=n)
        return Entry("document", node.name, relative_time(node.last_modified, self.now),
                     node=node, pinned=node.pinned)

    # -- breadcrumbs -------------------------------------------------------------

    def breadcrumbs(self, loc: Location) -> list[Crumb]:
        if loc.search.strip():
            return [Crumb(f'Search results for "{loc.search.strip()}"', loc)]
        root = Crumb(VIEW_TITLES[loc.view], Location(loc.view))
        crumbs = [root]
        if loc.view in (ViewFilter.MY_FILES, ViewFilter.TRASH) and loc.folder_id not in (ROOT_ID, TRASH_ID):
            for node in self.tree.breadcrumbs(loc.folder_id):
                crumbs.append(Crumb(node.name, Location(loc.view, folder_id=node.id)))
        elif loc.view == ViewFilter.TAGS and loc.tag is not None:
            crumbs.append(Crumb(loc.tag, Location(ViewFilter.TAGS, tag=loc.tag)))
        return crumbs

    def open_location(self, loc: Location, entry: Entry) -> Location:
        """Where activating a folder/tag entry leads."""
        if entry.kind == "tag":
            return Location(ViewFilter.TAGS, tag=entry.tag)
        view = ViewFilter.TRASH if self.tree.is_trashed(entry.node.id) else ViewFilter.MY_FILES
        return Location(view, folder_id=entry.node.id)

    def is_valid(self, loc: Location) -> bool:
        if loc.folder_id in (ROOT_ID, TRASH_ID):
            return True
        return isinstance(self.tree.get(loc.folder_id), Folder)


def _items(n: int) -> str:
    # ContentEntriesBase.qml: qsTr("1 item") / qsTr("%1 items")
    return "1 item" if n == 1 else f"{n} items"


# ------------------------------------------------------------------- page access

@dataclass(frozen=True)
class PageRef:
    index: int
    page_id: str
    rm_name: str | None          # "<doc>/<page>.rm" if the page has strokes
    pdf_page: int | None         # page in the PDF/EPUB rendition, if any
    template: str = ""           # template name ("P Lines small"); "" / "Blank" = plain paper


def unlisted_page_id(pdf_page: int) -> str:
    """The id this client gives page `pdf_page` of a document without a page list.

    A PDF or EPUB uploaded without one (this client's imports, and other uploaders') gets
    its page list, with CRDT page ids, from the tablet the first time it is opened there.
    Until then the viewer still needs an id per page (DocumentViewport opens pages by id),
    so pages are named after their PDF page. These ids are never written to the cloud.
    """
    return f"pdf-page-{pdf_page}"


def has_page_list(doc: Document) -> bool:
    return any(not p.deleted for p in doc.content.pages)


def document_pages(doc: Document, pdf_page_count: int | None = None,
                   pagedata: list[str] | None = None) -> list[PageRef]:
    """Live pages in reading order with their stroke file, PDF page and template.

    Documents without a page list (never opened on a tablet) fall back to one page
    per PDF page (ids from unlisted_page_id), or per .rm file for notebooks. Templates
    come from .content; older (formatVersion 1) documents list them in .pagedata, one
    line per page.
    """
    live: list[Page] = [p for p in doc.content.pages if not p.deleted]
    if not live:
        if pdf_page_count:
            live = [Page(id=unlisted_page_id(i), redirect=i) for i in range(pdf_page_count)]
        else:
            ids = sorted(name[len(doc.id) + 1:-3] for name in doc.files
                         if name.startswith(f"{doc.id}/") and name.endswith(".rm"))
            live = [Page(id=pid) for pid in ids]
    refs = []
    for i, page in enumerate(live):
        name = f"{doc.id}/{page.id}.rm"
        template = page.template or (pagedata[i] if pagedata and i < len(pagedata) else "")
        refs.append(PageRef(i, page.id, name if name in doc.files else None, page.redirect, template))
    return refs


def pagedata_name(doc: Document) -> str | None:
    name = f"{doc.id}.pagedata"
    return name if name in doc.files else None


def pdf_name(doc: Document) -> str | None:
    name = f"{doc.id}.pdf"
    return name if name in doc.files else None


def start_page(doc: Document, page_count: int) -> int:
    """Page to open at: the last opened page, as the app does (PageOverview.qml)."""
    if page_count <= 0:
        return 0
    return min(max(doc.metadata.last_opened_page, 0), page_count - 1)


def _stroke_values(page: bytes | None) -> dict[tuple[int, int], str]:
    """The live strokes of a page: id -> value (hex), as undo keeps them ({} for a page
    the stroke engine cannot read whole: no undo step is kept then)."""
    try:
        return {s.item: s.line.encode().hex() for s in rm_stroke.strokes(page)}
    except (RmParseError, ValueError) as err:
        log.warning("strokes of a page not readable for undo: %s", err)
        return {}


def _node_anchors(page: bytes, offsets) -> dict[tuple[int, int], tuple[float, float]]:
    """The anchor position of each group of `page` anchored to typed text, from where
    the page draws its strokes (`offsets`: stroke id -> position): the group a stroke
    is in, or the nearest anchored group above it."""
    if not offsets:
        return {}
    blocks = rm_writer.split(page)[1]
    anchored = rm_stroke.anchored_nodes(blocks)
    if not anchored:
        return {}
    parent_of = {}
    for layer in rm_writer.layers(blocks):
        parent_of.update(rm_stroke.group_nodes(blocks, layer))
    out = {}
    for stroke in rm_stroke.strokes(page):
        where = offsets.get(stroke.item)
        node, seen = stroke.parent, set()
        while where is not None and node not in anchored and node in parent_of and node not in seen:
            seen.add(node)
            node = parent_of[node]
        if where is not None and node in anchored:
            out.setdefault(node, (float(where[0]), float(where[1])))
    return out


def _merged(first: tuple, second: list) -> tuple:
    """A paste's undo step and the move of what it pasted as one step (0x1408f3920):
    what the move put back of the pasted items is not put back, and what the paste added
    and the move did not take is still taken; the pasted images taken by the move are
    simply the moved ones."""
    pasted = {p.ref: p for c in first if isinstance(c, StrokeChange) for p in c.remove}
    first_images = {c.key: c for c in first if isinstance(c, Change)}
    moved_refs = {p.ref for c in second if isinstance(c, StrokeChange) for p in c.readd}
    out: list = []
    remove: list = [p for ref, p in pasted.items() if ref not in moved_refs]
    readd: list = []
    images: dict[str, Change] = {k: c for k, c in first_images.items()}
    for change in second:
        if isinstance(change, StrokeChange):
            remove += change.remove
            readd += [p for p in change.readd if p.ref not in pasted]
        elif isinstance(change, Change) and change.key in images and images[change.key].readd is None:
            images[change.key] = Change(change.key, None, change.remove)
        elif isinstance(change, Change):
            images[change.key] = change
    out += [c for c in first if not isinstance(c, (StrokeChange, Change))]
    if remove or readd:
        out.append(StrokeChange(tuple(remove), tuple(readd)))
    return tuple(out + list(images.values()))


# The kinds of documents whose pages may be edited (LibrarySource.edit_refusal).
EDITABLE_TYPES = (FileType.NOTEBOOK, FileType.PDF, FileType.EPUB)


class LibrarySource:
    """The library as the UI sees it: a tree plus page/PDF data, via SyncClient.

    `cached_only` keeps every read local (offline mode). Parsed scenes are kept in a
    small LRU so flipping pages back and forth does not re-parse.

    `editable` names the notebooks whose pages may be edited here (none by default):
    editing is being built feature by feature, and is tried on notebooks named for it.
    A copy made here of a notebook that may be edited may be edited too, and so on down
    (inherit_editing): their ids are kept with the cache (EDITABLE_COPIES), so they stay
    so after a restart.
    Edits are kept locally (SyncClient.edits) and drawn over the cached pages; uploading
    them is SyncClient.push_edits.
    """

    def __init__(self, sync: SyncClient, *, cached_only: bool = False, scene_cache: int = 64,
                 editable: tuple[str, ...] = ()) -> None:
        self.sync = sync
        self.cached_only = cached_only
        self.editable = tuple(editable)
        self._copies_file = Path(sync.root_dir) / EDITABLE_COPIES
        self._copies: set[str] = self._read_copies()
        self._scenes: OrderedDict[str, PageScene | None] = OrderedDict()
        self._scene_cache = scene_cache
        self._lock = threading.Lock()
        # Undo and redo, per (document id, page id), while the document is open.
        self._histories: dict[tuple[str, str], PageHistory] = {}
        # Documents worked out with their pending edits, by id: (key, Staged).
        self._staged: dict[str, tuple[tuple, Staged]] = {}
        self._history_lock = threading.RLock()
        # Documents made here (createDocument, Quick sheets) until the cloud has them, by
        # id: their node, files and folder, and whether their upload has started.
        self._created: dict[str, dict] = {}
        # Changes of .content/.metadata fields sent to the cloud and shown before the
        # cloud's copy has them, by document id: (hash of the copy they apply to,
        # content fields, metadata fields). Dropped once that copy is replaced.
        self._shown: dict[str, tuple[str, dict, dict]] = {}

    @property
    def writable(self) -> bool:
        """Changes (imports, folders, moves, trash) go to the cloud: not when offline."""
        return not self.cached_only

    def load_cached(self) -> DocumentTree:
        return self.sync.load_cached()

    def refresh(self, progress: Callable[[int, int, bool], None] | None = None) -> DocumentTree:
        """Sync the tree (`progress` as SyncClient.refresh). Offline: the cache."""
        if self.cached_only:
            return self.sync.load_cached()
        return self.sync.refresh(progress)

    def forget(self) -> None:
        """Drop the cached library (logging out)."""
        self.sync.forget()
        with self._lock:
            self._scenes.clear()

    def blob(self, doc: Document, name: str) -> bytes:
        created = self._created.get(doc.id)
        if created is not None and name in created["files"]:
            return created["files"][name]
        return self.sync.get_blob(doc.files[name], name, cached_only=self.cached_only)

    # -- documents made here (core.create) ------------------------------------------

    def synced(self, doc_id: str) -> Node | None:
        """The item as last synced, or a document made here that the cloud does not have
        yet (it is shown, opened and edited at once, as the app's are)."""
        node = self.sync.tree.get(doc_id)
        if node is not None:
            return node
        created = self._created.get(doc_id)
        return created["node"] if created is not None else None

    def created(self, doc_id: str) -> Document | None:
        """A document made here and not in the cloud yet, or None."""
        created = self._created.get(doc_id)
        return created["node"] if created is not None and doc_id not in self.sync.tree else None

    def _created_node(self, doc_id: str, files: dict[str, bytes]) -> Document:
        from core.models import Metadata, make_node
        schema = self.sync.root.schema_version if self.sync.root is not None else 3
        entries = [file_entry(n, d) for n, d in files.items()]
        doc_hash, _ = build_index(schema, entries, doc_id)
        for entry in entries:
            self.sync.blobs.put(entry.hash, files[entry.id])
        node = make_node(doc_id, Metadata.parse(files[f"{doc_id}.metadata"]),
                         Content.parse(files[f"{doc_id}.content"]), hash=doc_hash,
                         files={e.id: e.hash for e in entries})
        return node

    def creation_refusal(self, parent: str) -> str:
        """Why a document cannot be made here now ("": it can): createDocument's own
        checks (free disk space, the folder), and this client's: editing turned on, and
        online, since the cloud is where the document is kept."""
        from core.create import MIN_FREE_BYTES
        import shutil
        if not self.editable:
            return "Editing is not turned on"
        if not self.writable:
            return "Not possible offline"
        try:
            if shutil.disk_usage(self.sync.root_dir).free <= MIN_FREE_BYTES:
                return "Not enough free disk space"
        except OSError:
            pass
        tree = self.sync.tree
        folder = tree.get(parent) if parent else None
        if parent and parent != ROOT_ID and (folder is None or not folder.is_folder or tree.is_trashed(parent)):
            return "The folder is no longer in the library"
        return ""

    def _add_created(self, doc_id: str, files: dict[str, bytes], parent: str) -> Document:
        if self.synced(doc_id) is not None:
            raise EditError(f"refusing to create document with id {doc_id} that already exists")
        node = self._created_node(doc_id, files)
        with self._lock:
            self._created[doc_id] = {"node": node, "files": dict(files), "parent": parent, "uploading": False}
            self._copies = self._read_copies() | {doc_id}
            atomic_write_json(self._copies_file, {"version": 1, "ids": sorted(self._copies)})
        log.info("made document %s here; it may be edited", doc_id)
        return node

    def create_notebook(self, name: str, parent: str, now_ms: int, doc_id: str | None = None,
                        page_id: str | None = None) -> Document:
        """A new notebook (LibraryController::createDocument, core.create), shown at once
        and uploaded by upload_created. EditError when it cannot be made."""
        from core.create import given_uuid, notebook_content, notebook_files, notebook_metadata
        refusal = self.creation_refusal(parent)
        if refusal:
            raise EditError(refusal)
        doc_id = given_uuid(doc_id) or str(uuid.uuid4())
        page_id = given_uuid(page_id) or str(uuid.uuid4())
        content = notebook_content(self.sync.author(), page_id)
        metadata = notebook_metadata(name, parent or ROOT_ID, now_ms)
        return self._add_created(doc_id, notebook_files(doc_id, content, metadata), parent or ROOT_ID)

    def create_from_pages(self, name: str, parent: str, source: Document, indices, now_ms: int,
                          doc_id: str | None = None) -> tuple[Document, PageDelete]:
        """A new notebook made of pages moved out of `source` (createDocumentFromExisting):
        each page with its edits, files and template becomes a page of the new notebook
        (new page ids), and the pages are deleted from `source` as a pending page edit.
        Note pages only: a page showing a PDF page cannot leave its PDF."""
        from core.create import notebook_files, notebook_from_pages, notebook_metadata
        from core.edits import page_files
        refusal = self.creation_refusal(parent)
        if refusal:
            raise EditError(refusal)
        source = self._editable(source)
        staged = self.staged(source)
        pages = self._pages_at(staged.ids, indices)
        content = staged.content if staged.changes_page_list else None
        if content is None:
            from core.edits import current_content
            content = current_content(source, lambda n: self.blob(source, n), self.sync.author())
        from core.pagemap import PageMap
        infos = [PageMap(content, self.sync.author()).info(p) for p in pages]
        if any(i is None for i in infos):
            raise EditError("a page to move is not in the document")
        if any(i.redirect is not None for i in infos):
            raise EditError("Pages of a PDF or ebook cannot be moved into a notebook")
        doc_id = str(uuid.uuid4()) if doc_id is None else doc_id
        moved = [(p, str(uuid.uuid4()), i.template) for p, i in zip(pages, infos)]
        files = notebook_files(doc_id, notebook_from_pages(self.sync.author(), [(n, t) for _, n, t in moved]),
                               notebook_metadata(name, parent or ROOT_ID, now_ms, opened=False))
        for old, new, _ in moved:
            page = staged.page(old)
            if page is not None:
                files[f"{doc_id}/{new}.rm"] = page
            for file_name in sorted(set(page_images(page).values())):
                found = staged.image_source(old, file_name)
                if found is not None:
                    files[f"{doc_id}/{new}/{file_name}"] = (self.blob(source, found[1]) if found[0] == "doc"
                                                           else self.sync.edits.data(found[1]))
            if old in staged.origin:                     # added or copied here: its source's files
                extra = staged.copied_files(old)
            else:
                extra = [(n, n) for n in page_files(source, old)
                         if not n.endswith(".rm") and not n.startswith(f"{source.id}/{old}/")]
            for from_name, as_name in extra:             # -metadata.json, highlights, thumbnail
                files[doc_id + as_name[len(source.id):].replace(old, new)] = self.blob(source, from_name)
        delete = PageDelete.new(source.id, pages, now_ms)
        node = self._add_created(doc_id, files, parent or ROOT_ID)
        self._add_edit(source, delete)
        return node, delete

    def change_created(self, doc_id: str, change: Callable[[dict, dict], bool]) -> bool:
        """Change a document made here before its upload has started: `change(content,
        metadata)` on its .content and .metadata (False: nothing changed). False when it
        is not such a document (it is changed in the cloud then)."""
        from core.create import notebook_files
        with self._lock:
            created = self._created.get(doc_id)
            if created is None or created["uploading"] or doc_id in self.sync.tree:
                return False
            files = created["files"]
            content = json.loads(files[f"{doc_id}.content"])
            metadata = json.loads(files[f"{doc_id}.metadata"])
        if change(content, metadata):
            files = {**files, **notebook_files(doc_id, content, metadata)}
            node = self._created_node(doc_id, files)
            with self._lock:
                created.update(files=files, node=node)
                self._staged.pop(doc_id, None)
                self._scenes.clear()
        return True

    def upload_created(self, doc_id: str) -> tuple[dict[str, bytes], str] | None:
        """The files and folder of a document made here, now being uploaded (no more
        local changes to it); None when there is nothing to upload."""
        with self._lock:
            created = self._created.get(doc_id)
            if created is None or created["uploading"]:
                return None
            created["uploading"] = True
            return dict(created["files"]), created["parent"]

    def created_failed(self, doc_id: str) -> None:
        """Its upload failed: the document goes, with the edits made to it here."""
        with self._lock:
            created = self._created.pop(doc_id, None)
            self._staged.pop(doc_id, None)
        if created is not None and doc_id not in self.sync.tree:
            self.sync.edits.discard(self.sync.edits.of(doc_id))

    # -- editing ------------------------------------------------------------------

    def _read_copies(self) -> set[str]:
        try:
            raw = read_json(self._copies_file) or {}
            return {str(i) for i in raw.get("ids") or [] if isinstance(i, str) and i}
        except (OSError, ValueError, AttributeError) as err:
            log.error("ignoring unreadable %s: %s", self._copies_file, err)
            return set()

    def inherit_editing(self, source_id: str, copy_id: str) -> bool:
        """A copy of document `source_id` was made (LibraryController.cloneEntry): when
        the source may be edited here, so may the copy, from now on (kept with the
        cache). Whether it may."""
        source = self.synced(source_id)
        if not self.can_edit(source) or not copy_id:
            return False
        with self._lock:
            self._copies = self._read_copies() | {str(copy_id)}
            atomic_write_json(self._copies_file, {"version": 1, "ids": sorted(self._copies)})
        log.info("copy %s of editable document %s may be edited too", copy_id, source_id)
        return True

    def edit_refusal(self, doc: Document | None) -> str:
        """Why `doc` may not be edited here ("": it may): only a notebook, PDF or ebook,
        not in the trash, whose name is one of `editable` and the only document of that
        name, or a copy made here of one that may be edited (by its id), so an edit can
        never land on a document it was not meant for. A PDF or ebook needs its page
        list (the tablet makes it the first time it opens one: until then this client
        shows pages it cannot name to the cloud, unlisted_page_id)."""
        if not self.editable:
            return "Editing is not turned on"
        tree = self.sync.tree
        if not isinstance(doc, Document) or self.synced(doc.id) is None:
            return "This document is not in the library"
        if doc.id in self._created:                     # made here, not uploaded yet: a notebook
            return ""
        if doc.id not in self._copies:
            if doc.name not in self.editable:
                return f"Editing is only turned on for the document named {' or '.join(map(repr, self.editable))}"
            same = [n for n in tree.nodes.values() if isinstance(n, Document) and n.name == doc.name
                    and n.file_type in EDITABLE_TYPES and not tree.is_trashed(n.id)]
            if doc.file_type in EDITABLE_TYPES and not tree.is_trashed(doc.id) and len(same) != 1:
                return (f"{len(same)} documents are named {doc.name!r}: editing needs the name to be "
                        "of one document only")
        if doc.file_type not in EDITABLE_TYPES:
            return "Only notebooks, PDFs and ebooks can be edited"
        if tree.is_trashed(doc.id):
            return "A document in the trash cannot be edited"
        if doc.file_type is not FileType.NOTEBOOK and not has_page_list(tree.get(doc.id)):
            return "This document has no page list yet: open it once on your reMarkable, then sync"
        return ""

    def can_edit(self, doc: Document | None) -> bool:
        return not self.edit_refusal(doc)

    def pending_edits(self, doc: Document, page_id: str | None = None) -> list[Edit]:
        return [e for e in self.sync.edits.of(doc.id) if page_id is None or e.page_id == page_id]

    def staged(self, doc: Document) -> Staged:
        """The document as last synced with its pending edits (core.edits.Staged)."""
        synced = self.synced(doc.id)
        base = synced if isinstance(synced, Document) else doc
        edits = self.sync.edits.of(doc.id)
        key = (base.hash, tuple(edits))
        with self._lock:
            known = self._staged.get(doc.id)
            if known is not None and known[0] == key:
                return known[1]
        staged = Staged(base, edits, lambda name: self.blob(base, name), self.sync.author() if edits else b"")
        staged.key = key
        with self._lock:
            self._staged[doc.id] = (key, staged)
        return staged

    def show_fields(self, doc_id: str, content: dict | None = None, metadata: dict | None = None) -> None:
        """Show these .content / .metadata fields of an item at once, while the change
        that sets them is on its way to the cloud (the app shows its own changes before
        they are uploaded)."""
        node = self.sync.tree.get(doc_id)
        if node is None:
            return
        with self._lock:
            old = self._shown.get(doc_id)
            fields = (dict(old[1]), dict(old[2])) if old is not None and old[0] == node.hash else ({}, {})
            fields[0].update(content or {})
            fields[1].update(metadata or {})
            self._shown[doc_id] = (node.hash, fields[0], fields[1])

    def _with_shown(self, doc: Node, synced: Node | None) -> Node:
        shown = self._shown.get(doc.id)
        if shown is None:
            return doc
        if synced is None or synced.hash != shown[0]:
            with self._lock:
                if self._shown.get(doc.id) is shown:
                    del self._shown[doc.id]             # the cloud's copy changed: it is what counts
            return doc
        from core.models import Metadata
        metadata = Metadata.parse({**doc.metadata.raw, **shown[2]})
        marked = hashlib.sha256(f"{doc.hash}:{json.dumps([shown[1], shown[2]], sort_keys=True)}".encode()).hexdigest()
        if isinstance(doc, Document):
            return Document(id=doc.id, metadata=metadata, hash=marked, files=dict(doc.files),
                            content=Content.parse({**(doc.content.raw if doc.content else {}), **shown[1]}))
        return Folder(id=doc.id, metadata=metadata, hash=marked, files=dict(doc.files), content=doc.content)

    def view(self, doc: Node) -> Node:
        return self._with_shown(self._view(doc), self.sync.tree.get(doc.id))

    def _view(self, doc: Node) -> Node:
        """`doc` as the app shows it: with the page list its pending page edits make
        (added, copied, moved and deleted pages). Its hash is made from the synced one
        and the edits, so that views and thumbnails of the old page list are not reused;
        it is never sent to the cloud (writes go by id through SyncClient)."""
        if not isinstance(doc, Document) or not any(isinstance(e, PAGE_EDITS) for e in self.sync.edits.of(doc.id)):
            return doc
        try:
            staged = self.staged(doc)
        except (EditError, SyncError) as err:
            log.error("cannot show the pending page edits of %s: %s", doc.id, err)
            return doc
        if not staged.changes_page_list:
            return doc
        # A hash of its own from the first character on: thumbnail URLs carry only the
        # start of it (image://rmthumb/<id>/<page>?v=<16 chars>), and a page index now
        # showing another page must not get the old picture from Qt's image cache.
        marked = hashlib.sha256(f"{doc.hash}:{staged.key[1]!r}".encode()).hexdigest()
        return Document(id=doc.id, metadata=doc.metadata, hash=marked, files=dict(doc.files),
                        content=Content.parse(staged.content))

    def view_tree(self, tree: DocumentTree) -> DocumentTree:
        """`tree` with each document that has pending page edits as view() shows it, and
        the documents made here that the cloud does not have yet."""
        with self._lock:
            for doc_id in [i for i in self._created if i in self.sync.tree]:
                del self._created[doc_id]           # uploaded: the cloud's copy from now on
            made = [c["node"] for i, c in self._created.items() if i not in tree]
        documents = set(self.sync.edits.documents()) | set(self._shown)
        if not documents & (set(tree.nodes) | {n.id for n in made}) and not made:
            return tree
        nodes = list(tree.nodes.values()) + made
        return DocumentTree([self.view(node) if node.id in documents else node for node in nodes])

    # -- pages: add, duplicate, move, delete (the app's DocumentController) --

    def page_ids(self, doc: Document) -> list[str]:
        """The document's pages as shown here (with its pending page edits)."""
        return list(self.staged(self._editable(doc)).ids)

    def _place(self, ids: list[str], index: int) -> tuple[int, str]:
        """An index of the page list as given to the app (clamped), and the page before it."""
        index = min(max(int(index), 0), len(ids))
        return index, ids[index - 1] if index > 0 else ""

    def _pages_at(self, ids: list[str], indices) -> list[str]:
        """The ids of the pages at `indices` (in list order), EditError if one is out of range."""
        try:
            wanted = sorted({int(i) for i in indices})
        except (TypeError, ValueError) as err:
            raise EditError(f"not a list of pages: {indices!r}") from err
        if not wanted or wanted[0] < 0 or wanted[-1] >= len(ids):
            raise EditError(f"no such pages: {sorted(wanted)} of {len(ids)}")
        return [ids[i] for i in wanted]

    def page_template(self, doc: Document, index: int) -> tuple[str, str]:
        """(page id, its template as shown here: "" when it has none) of the page at
        `index`; EditError when there is no such page."""
        from core.edits import current_content
        from core.pagemap import PageMap
        staged = self.staged(self.synced(doc.id) or doc)
        if not 0 <= int(index) < len(staged.ids):
            raise EditError(f"no page {index} of {len(staged.ids)}")
        page_id = staged.ids[int(index)]
        base = self.synced(doc.id) or doc
        content = staged.content if staged.changes_page_list else current_content(
            base, lambda n: self.blob(base, n), self.sync.author())
        info = PageMap(json.loads(json.dumps(content)), self.sync.author()).info(page_id)
        return page_id, info.template if info is not None else ""

    def set_page_template(self, doc: Document, index: int, template: str, now_ms: int) -> bool:
        """DocumentController::setTemplateForPage: the page at `index` gets `template` when
        it has another (an empty name changes nothing), and lastModified moves. On a
        document made here and not uploaded yet it is written into its .content at once;
        otherwise it is a pending page edit (PageTemplate). Whether anything changed."""
        from core.create import set_page_template
        if not template:
            return False
        doc = self._editable(doc)
        page_id, current = self.page_template(doc, index)
        if current == template:
            return False

        def change(content: dict, metadata: dict) -> bool:
            if not set_page_template(content, page_id, template, self.sync.author()):
                return False
            metadata["lastModified"] = str(max(int(metadata.get("lastModified") or 0), int(now_ms)))
            return True
        if self.change_created(doc.id, change):
            return True
        self._add_edit(doc, PageTemplate.new(doc.id, page_id, template, now_ms))
        return True

    def add_page(self, doc: Document, index: int, template: str, now_ms: int, page_uuid: str = "") -> PageAdd:
        """Add an empty page at `index` (past the end or negative: at the end, as
        doAddPage clamps it), here only until pushed. The app passes the template; the
        desktop's "Add page" passes "Blank" (Strings.defaultTemplateName). `page_uuid`
        names the page when it is a UUID no page of the document has (else a new one)."""
        doc = self._editable(doc)
        staged = self.staged(doc)
        ids = staged.ids
        index = len(ids) if index < 0 else index
        index, after = self._place(ids, index)
        new_id = None
        try:
            if page_uuid and str(uuid.UUID(page_uuid)) not in set(ids) | staged.deleted \
                    and not any(p.id == str(uuid.UUID(page_uuid)) for p in doc.content.pages):
                new_id = str(uuid.UUID(page_uuid))
        except ValueError:
            new_id = None
        edit = PageAdd.new(doc.id, index, after, template, now_ms, new_id)
        self._add_edit(doc, edit)
        return edit

    def copy_pages(self, doc: Document, indices, insert_index: int, now_ms: int) -> PageCopy:
        """Duplicate the pages at `indices`, the copies in a row at `insert_index`."""
        doc = self._editable(doc)
        ids = self.staged(doc).ids
        sources = self._pages_at(ids, indices)
        index, after = self._place(ids, insert_index)
        edit = PageCopy.new(doc.id, sources, index, after, now_ms)
        self._add_edit(doc, edit)
        return edit

    def move_pages(self, doc: Document, indices, insert_index: int, now_ms: int) -> PageMove:
        """Move the pages at `indices` to before the page now at `insert_index`."""
        doc = self._editable(doc)
        ids = self.staged(doc).ids
        pages = self._pages_at(ids, indices)
        index, after = self._place(ids, insert_index)
        edit = PageMove.new(doc.id, pages, index, after, now_ms)
        self._add_edit(doc, edit)
        return edit

    def delete_pages(self, doc: Document, indices, now_ms: int) -> PageDelete:
        """Delete the pages at `indices` (a document left empty gets a "Blank" page)."""
        doc = self._editable(doc)
        pages = self._pages_at(self.staged(doc).ids, indices)
        edit = PageDelete.new(doc.id, pages, now_ms)
        self._add_edit(doc, edit)
        return edit

    def _layer_ref(self, doc: Document, page_id: str, layer_index: int | None):
        """(ItemRef, (author, counter)) of live layer `layer_index` on `page_id`, or
        (None, None) when `layer_index` is None (meaning the page's current layer)."""
        if layer_index is None:
            return None, None
        done = self.staged(doc).replay(page_id)
        infos = rm_layer.layers_info(done.page)
        idx = int(layer_index)
        if not 0 <= idx < len(infos):
            raise EditError(f"no layer {idx} of {len(infos)}")
        node_id = infos[idx].node_id
        return done.ref(node_id), node_id

    def insert_image(self, doc: Document, page_id: str, data: bytes, extension: str, vertices,
                     now_ms: int, timestamps=None, action: list[Change] | None = None,
                     layer_index: int | None = None) -> ImageInsert:
        """Put an image (`data`: a PNG or JPEG file as the app stores one, see
        ui.editing.encode_image) on a page, here only: the edit is kept with the cache
        and drawn over the page until SyncClient.push_edits uploads it. EditError when
        `doc` may not be edited, or the page cannot take the image safely. It can be
        undone: on its own, or as part of `action` (see edit_action)."""
        doc = self._editable(doc, page_id)
        layer_ref, _ = self._layer_ref(doc, page_id, layer_index)
        edit = ImageInsert.new(doc.id, page_id, extension, vertices, sha256_hex(data), len(data), now_ms,
                               timestamps, layer=layer_ref)
        with self.edit_action(doc, page_id, action) as changes:
            self._add_edit(doc, edit, data)
            changes.append(Change(edit.key, None, ("edit", edit.uid)))
        return edit

    def image_file(self, doc_id: str, page_id: str, file_name: str) -> bytes:
        """The file of an image of a page, as stored with the document
        ("<doc>/<page>/<file name>"), or put on the page here and not uploaded yet: what
        the app's paste loads to store a copy (0x1409a8240). EditError when it cannot be
        had (the document is gone, or the file is not cached offline)."""
        doc = self.synced(doc_id)
        name = f"{doc_id}/{page_id}/{file_name}"
        if isinstance(doc, Document) and name in doc.files:
            try:
                return self.blob(doc, name)
            except (SyncError, OSError) as err:
                raise EditError(f"the copied image cannot be loaded: {err}") from err
        for edit in self.sync.edits.of(doc_id):
            if isinstance(edit, ImageInsert) and edit.page_id == page_id and edit.file_name == file_name:
                return self.sync.edits.data(edit)
        raise EditError("the copied image is no longer there")

    def transform_image(self, doc: Document, page_id: str, key: bytes, vertices, now_ms: int,
                        action: list[Change] | None = None) -> ImageTransform:
        """Move, scale or turn an image of a page to new corners, here only (as
        insert_image). EditError when not allowed or not possible safely."""
        doc = self._editable(doc, page_id)
        edit = ImageTransform.new(doc.id, page_id, key, vertices, now_ms)
        with self.edit_action(doc, page_id, action) as changes:
            before = self._live_item(doc, page_id, key)
            self._add_edit(doc, edit)
            after = self._live_item(doc, page_id, key)
            if before is not None and after is not None and after[1] != before[1]:     # not left as it was
                changes.append(Change(edit.key, before[0], ("edit", edit.uid)))
        return edit

    def delete_image(self, doc: Document, page_id: str, key: bytes, now_ms: int,
                     action: list[Change] | None = None) -> ImageDelete:
        """Delete an image of a page, here only (as insert_image)."""
        doc = self._editable(doc, page_id)
        edit = ImageDelete.new(doc.id, page_id, key, now_ms)
        with self.edit_action(doc, page_id, action) as changes:
            before = self._live_item(doc, page_id, key)
            self._add_edit(doc, edit)
            if before is not None:
                changes.append(Change(edit.key, before[0], None))
        return edit

    # -- undo and redo --

    def _seen(self, doc_id: str, page_id: str):
        synced = self.synced(doc_id)
        files = synced.files if isinstance(synced, Document) else {}
        return files.get(f"{doc_id}/{page_id}.rm"), tuple(e for e in self.sync.edits.of(doc_id) if e.page_id == page_id)

    def _history(self, doc_id: str, page_id: str) -> PageHistory:
        """The page's undo and redo stacks, empty when the page changed since they were
        last used (synced anew, or its pending edits uploaded or discarded)."""
        seen = self._seen(doc_id, page_id)
        history = self._histories.get((doc_id, page_id))
        if history is None or history.seen != seen:
            history = self._histories[(doc_id, page_id)] = PageHistory(seen)
        return history

    @contextmanager
    def edit_action(self, doc: Document, page_id: str, action: list[Change] | None = None):
        """Edits made in this block are one action for undo (several images pasted at
        once, as the app's add-items action). With `action` (a list from an outer
        edit_action), they are part of that one instead."""
        if action is not None:
            yield action
            return
        with self._history_lock:
            history = self._history(doc.id, page_id)
            changes: list[Change] = []
            try:
                yield changes
            finally:
                if changes:
                    history.push(changes)
                    history.seen = self._seen(doc.id, page_id)

    def _live_item(self, doc: Document, page_id: str, key: bytes):
        """(how an edit names it, its id) of the item showing the image `key` on the
        page as it is shown here, or None."""
        try:
            done = self.staged(doc).replay(page_id)
            live = rm_writer.image_item(done.page, key)
        except (RmParseError, ValueError) as err:
            raise EditError(f"the page cannot be edited: {err}") from err
        return (done.ref(live[0]), live[0]) if live is not None else None

    def history_state(self, doc: Document, page_id: str) -> tuple[bool, bool]:
        """(undo available, redo available) for a page."""
        with self._history_lock:
            history = self._history(doc.id, page_id)
            return bool(history.undo), bool(history.redo)

    def undo(self, doc: Document, page_id: str, now_ms: int) -> bool:
        """Undo the last edit of a page, with new edits (core.edits.swapped). False when
        there is nothing to undo; EditError when it cannot be done (the stacks are then
        left as they were)."""
        return self._step(doc, page_id, now_ms, redo=False)

    def redo(self, doc: Document, page_id: str, now_ms: int) -> bool:
        """Redo the last edit undone on a page (as undo)."""
        return self._step(doc, page_id, now_ms, redo=True)

    def _step(self, doc: Document, page_id: str, now_ms: int, redo: bool) -> bool:
        with self._history_lock:
            history = self._history(doc.id, page_id)
            source, target = (history.redo, history.undo) if redo else (history.undo, history.redo)
            if not source:
                return False
            doc = self._editable(doc, page_id)
            edits, back = swapped(source[-1], doc.id, page_id, now_ms, history.aliases)
            self._add_edits(doc, [(e, None) for e in edits])
            source.pop()
            target.append(back)
            history.seen = self._seen(doc.id, page_id)
            return True

    # -- typed text --

    def text_page(self, doc: Document, page_id: str):
        """(the page's .rm with its pending edits, None when it has none; what those
        edits made, to name items in new ones: core.edits.Replay) for typing on it.
        EditError when the page may not be edited here."""
        doc = self._editable(doc, page_id)
        return self.staged(doc).replay(page_id)

    def add_text_edits(self, doc: Document, page_id: str, edits: list, steps: list) -> None:
        """Keep text edits typed on a page (ui.textedit.TextSession.take), and their
        undo steps, each one action for undo, as the app's text actions are."""
        if not edits:
            return
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            self._add_edits(doc, [(e, None) for e in edits])
            for step in steps:
                history.push(step)
            history.seen = self._seen(doc.id, page_id)

    def add_stroke(self, doc: Document, page_id: str, line, now_ms: int,
                   layer_index: int | None = None) -> StrokeAdd:
        """Keep a stroke drawn on a page: `line` as the app would add it
        (viewer.rm_stroke.draw), one action for undo (the app's AddItem). Refused, with
        nothing kept, on a document that may not be edited. On a page with typed text
        it goes on the layer, unanchored (viewer.rm_stroke.has_text)."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            layer_ref, _ = self._layer_ref(doc, page_id, layer_index)
            edit = StrokeAdd.new(doc.id, page_id, line, now_ms, layer=layer_ref)
            self._add_edit(doc, edit)
            done = self.staged(doc).replay(page_id)
            item = done.made.get(edit.uid)
            value = _stroke_values(done.page).get(item) if item is not None else None
            if value is not None:
                history.push([StrokeChange(remove=(StrokePart(("edit", edit.uid), value),))])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def erase_strokes(self, doc: Document, page_id: str, eraser, now_ms: int, offsets=None,
                      layer_index: int | None = None) -> StrokeErase | None:
        """Keep an eraser taken over a page: `eraser`, the eraser's own line
        (viewer.rm_stroke.eraser_line), one action for undo (the app's eraser action:
        the strokes it deleted put back, the pieces it left taken away). None, with
        nothing kept, when it touches no stroke. Refused like add_stroke.

        `offsets`: where the page draws the strokes of groups anchored to typed text
        (stroke id -> anchor position, as renderer.stroke_offsets gives them): the
        eraser reaches those strokes where they are drawn, and their pieces stay in
        their group (rm_stroke.erase)."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            if before.page is None:
                return None
            layer_ref, layer_node = self._layer_ref(doc, page_id, layer_index)
            try:
                anchors = _node_anchors(before.page, offsets)
                erased = rm_stroke.erase(before.page, author=self.sync.author(), eraser=eraser,
                                         layer=layer_node, anchors=anchors)
            except (RmParseError, ValueError) as err:
                raise EditError(f"the page cannot be erased on: {err}") from err
            if not erased.deleted:
                return None
            edit = StrokeErase.new(doc.id, page_id, eraser, now_ms,
                                   anchors=[(a, c, x, y) for (a, c), (x, y) in anchors.items()],
                                   layer=layer_ref)
            self._add_edit(doc, edit)
            old = _stroke_values(before.page)
            after = self.staged(doc).replay(page_id)
            new = _stroke_values(after.page)
            readd = tuple(StrokePart(before.ref(item), old[item]) for item in erased.deleted if item in old)
            pieces = []
            for n in range(len(erased.made)):
                item = after.made.get(f"{edit.uid}/{n}")
                if item in new:
                    pieces.append(StrokePart(("edit", f"{edit.uid}/{n}"), new[item]))
            history.push([StrokeChange(remove=tuple(pieces), readd=readd)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def delete_strokes(self, doc: Document, page_id: str, items, now_ms: int) -> StrokeDelete:
        """Keep a delete of whole strokes of a page, named as edits name items:
        ("page", author, counter) or ("edit", uid)."""
        doc = self._editable(doc, page_id)
        edit = StrokeDelete.new(doc.id, page_id, items, now_ms)
        self._add_edit(doc, edit)
        return edit

    # -- the selection: moving, deleting, pasting strokes and images --

    def undo_top(self, doc: Document, page_id: str):
        """The step on top of a page's undo stack (to merge a selection's move into the
        paste that made it, as the app's stack does, 0x1408f3920), or None."""
        with self._history_lock:
            history = self._history(doc.id, page_id)
            return history.undo[-1] if history.undo else None

    def _items(self, doc: Document, page_id: str):
        try:
            done = self.staged(doc).replay(page_id)
            return done, {i.item: i for i in rm_select.page_items(done.page)}
        except (RmParseError, ValueError) as err:
            raise EditError(f"the page cannot be edited: {err}") from err

    def item_values(self, doc: Document, page_id: str, items) -> dict:
        """The values (rm_stroke.LineValue, rm_writer.ImageValue) of live items of a page
        as shown here, by id: what copying clones."""
        _, live = self._items(self.synced(doc.id) or doc, page_id)
        return {tuple(i): (live[tuple(i)].line if live[tuple(i)].is_line else live[tuple(i)].image)
                for i in items if tuple(i) in live}

    def transform_items(self, doc: Document, page_id: str, items, matrix, now_ms: int, offsets=None,
                        merge=None):
        """Commit a selection's move, scale and turn (`matrix`, rm_select's order) of the
        page's live items `items` (ids), one ItemsTransform, one action for undo (the app's
        SceneDeleteAppendItems: undo puts the originals back, redo the transformed). When
        `merge` is the step on top of the undo stack (the paste that made the selection),
        the two become one step. `offsets`: where the page draws items of groups anchored
        to text (item id -> anchor position), whose node's transform is conjugated with
        it. (edit, {old id: new id}); (None, {}) for the identity or nothing left to move."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before, live = self._items(doc, page_id)
            chosen = [tuple(i) for i in items if tuple(i) in live]
            if not chosen or rm_select.is_identity(rm_select.matrix(matrix)):
                return None, {}
            anchors = {live[i].parent: tuple(offsets[i]) for i in chosen if offsets and i in offsets}
            edit = ItemsTransform.new(doc.id, page_id, [before.ref(i) for i in chosen], matrix, now_ms,
                                      [(a, c, x, y) for (a, c), (x, y) in anchors.items()])
            self._add_edit(doc, edit)
            after, now = self._items(doc, page_id)
            removed, readded, images, moved = [], [], [], {}
            for n, item in enumerate(chosen):
                new = after.made.get(f"{edit.uid}/{n}")
                if new is None or new not in now:
                    continue
                moved[item] = new
                ref = ("edit", f"{edit.uid}/{n}")
                if live[item].is_line:
                    removed.append(StrokePart(ref, now[new].line.encode().hex()))
                    readded.append(StrokePart(before.ref(item), live[item].line.encode().hex()))
                else:
                    images.append(Change(live[item].image.ref.hex(), before.ref(item), ref))
            changes = ([StrokeChange(tuple(removed), tuple(readded))] if removed else []) + images
            if merge is not None and history.undo and history.undo[-1] is merge:
                history.undo[-1] = _merged(merge, changes)
                history.redo.clear()
            else:
                history.push(changes)
            history.seen = self._seen(doc.id, page_id)
            return edit, moved

    def paste_strokes(self, doc: Document, page_id: str, lines, now_ms: int, action: list | None = None,
                      layer_index: int | None = None):
        """Paste line values (placed already: rm_select.pasted) on top of the target
        layer (default: current layer), one StrokePaste, part of `action` or one action
        for undo (the app's add-items). (edit, the ids they took)."""
        doc = self._editable(doc, page_id)
        layer_ref, _ = self._layer_ref(doc, page_id, layer_index)
        edit = StrokePaste.new(doc.id, page_id, lines, now_ms, layer=layer_ref)
        with self.edit_action(doc, page_id, action) as changes:
            self._add_edit(doc, edit)
            after, now = self._items(doc, page_id)
            ids = [after.made.get(f"{edit.uid}/{n}") for n in range(len(edit.values))]
            changes.append(StrokeChange(remove=tuple(StrokePart(("edit", f"{edit.uid}/{n}"), now[i].line.encode().hex())
                                                     for n, i in enumerate(ids) if i in now)))
        return edit, [i for i in ids if i is not None]

    # -- layers & SceneInfo --

    def _current_layer_node(self, page: bytes | None) -> tuple[int, int]:
        if page is None:
            return rm_writer.LAYER_1
        return rm_writer.current_layer(rm_writer.split(page)[1])

    def add_layer(self, doc: Document, page_id: str, now_ms: int) -> LayerAdd:
        """Add a new layer on top of `page_id` (max 5 live layers), making it active, as
        one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            if len(infos) >= rm_layer.MAX_LAYERS:
                raise EditError(f"Limit of {rm_layer.MAX_LAYERS} layers reached")
            old_cur = before.ref(self._current_layer_node(before.page))
            name = (rm_layer.next_layer_name(rm_writer.split(before.page)[1])
                    if before.page is not None else f"Layer {len(infos) + 1}")
            edit = LayerAdd.new(doc.id, page_id, name, now_ms)
            self._add_edit(doc, edit)
            new_ref = ("edit", edit.uid)
            history.push([LayerAttachChange(new_ref, new_ref, old_cur, attached=True)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def delete_layer(self, doc: Document, page_id: str, layer_index: int, now_ms: int) -> LayerDelete:
        """Delete (detach) layer `layer_index` on `page_id` (refusing the last layer),
        as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            idx = int(layer_index)
            if not 0 <= idx < len(infos):
                raise EditError(f"no layer {idx} of {len(infos)}")
            if len(infos) <= 1:
                raise EditError("Cannot delete the last layer")
            target_node = infos[idx].node_id
            old_cur_node = self._current_layer_node(before.page)
            old_cur_idx = next((n for n, info in enumerate(infos) if info.node_id == old_cur_node), len(infos) - 1)
            remaining = [info.node_id for info in infos if info.node_id != target_node]
            if idx == old_cur_idx:
                new_cur_node = remaining[old_cur_idx - 1] if old_cur_idx == len(remaining) else remaining[old_cur_idx]
            else:
                new_cur_node = old_cur_node
            target_ref = before.ref(target_node)
            old_cur_ref = before.ref(old_cur_node)
            new_cur_ref = before.ref(new_cur_node)
            edit = LayerDelete.new(doc.id, page_id, target_ref, now_ms, new_current=new_cur_ref)
            self._add_edit(doc, edit)
            history.push([LayerAttachChange(target_ref, old_cur_ref, new_cur_ref, attached=False)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def rename_layer(self, doc: Document, page_id: str, layer_index: int, name: str, now_ms: int) -> LayerRename | None:
        """Rename layer `layer_index` on `page_id`, as one undo step. None when unchanged."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            idx = int(layer_index)
            if not 0 <= idx < len(infos):
                raise EditError(f"no layer {idx} of {len(infos)}")
            old_name = infos[idx].name
            new_name = str(name)
            if old_name == new_name:
                return None
            target_ref = before.ref(infos[idx].node_id)
            edit = LayerRename.new(doc.id, page_id, target_ref, new_name, now_ms)
            self._add_edit(doc, edit)
            history.push([LayerRenameChange(target_ref, old_name, new_name)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def set_layer_visible(self, doc: Document, page_id: str, layer_index: int, visible: bool,
                          now_ms: int) -> LayerVisibility | None:
        """Toggle visibility of layer `layer_index` on `page_id`, as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            idx = int(layer_index)
            if not 0 <= idx < len(infos):
                raise EditError(f"no layer {idx} of {len(infos)}")
            old_visible = infos[idx].visible
            new_visible = bool(visible)
            if old_visible == new_visible:
                return None
            target_ref = before.ref(infos[idx].node_id)
            edit = LayerVisibility.new(doc.id, page_id, target_ref, new_visible, now_ms)
            self._add_edit(doc, edit)
            history.push([LayerVisibilityChange(target_ref, old_visible, new_visible)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def move_layer(self, doc: Document, page_id: str, from_index: int, to_index: int,
                   now_ms: int) -> LayerMove | None:
        """Reorder layer `from_index` to `to_index` (0 = bottom) on `page_id`, as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            src, dst = int(from_index), int(to_index)
            if not 0 <= src < len(infos) or not 0 <= dst < len(infos):
                raise EditError(f"invalid layer move {src} -> {dst} of {len(infos)}")
            if src == dst:
                return None
            target_ref = before.ref(infos[src].node_id)
            edit = LayerMove.new(doc.id, page_id, target_ref, dst, now_ms)
            self._add_edit(doc, edit)
            history.push([LayerMoveChange(target_ref, src, dst)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def merge_layer_down(self, doc: Document, page_id: str, layer_index: int, now_ms: int) -> LayerMergeDown:
        """Merge layer `layer_index` (>= 1) down into `layer_index - 1`, as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            idx = int(layer_index)
            if not 1 <= idx < len(infos) or before.page is None:
                raise EditError(f"cannot merge layer {idx} down")
            src_node = infos[idx].node_id
            dst_node = infos[idx - 1].node_id
            try:
                merged = rm_layer.merge_layer_down(before.page, author=self.sync.author(), layer=src_node)
            except (RmParseError, ValueError) as err:
                raise EditError(f"cannot merge layer: {err}") from err
            src_ref = before.ref(src_node)
            dst_ref = before.ref(dst_node)
            edit = LayerMergeDown.new(doc.id, page_id, src_ref, now_ms)
            self._add_edit(doc, edit)
            src_items = tuple((before.ref(old_id), kind, raw.hex(), None)
                              for old_id, kind, raw, _ in merged.transferred)
            dst_items = tuple(("edit", f"{edit.uid}/{n}") for n in range(len(src_items)))
            groups = tuple(before.ref(g) for g in merged.moved_groups)
            history.push([LayerMergeChange(src_ref, dst_ref, groups, src_items, dst_items,
                                           before.ref(merged.old_current), merged=True)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def clear_lines_in_layer(self, doc: Document, page_id: str, layer_index: int,
                             now_ms: int) -> StrokeDelete | None:
        """Delete all strokes in layer `layer_index` (and groups below it), as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            if before.page is None:
                return None
            infos = rm_layer.layers_info(before.page)
            idx = int(layer_index)
            if not 0 <= idx < len(infos):
                raise EditError(f"no layer {idx} of {len(infos)}")
            _, deleted = rm_layer.clear_lines_in_layer(before.page, author=self.sync.author(),
                                                       layer=infos[idx].node_id)
            if not deleted:
                return None
            edit = StrokeDelete.new(doc.id, page_id, [before.ref(i) for i, _ in deleted], now_ms)
            self._add_edit(doc, edit)
            history.push([StrokeChange(readd=tuple(StrokePart(before.ref(i), raw.hex()) for i, raw in deleted))])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def set_current_layer(self, doc: Document, page_id: str, layer_index: int,
                          now_ms: int) -> SceneFlags | None:
        """Persist active layer `layer_index` to SceneInfo field 1 without disturbing the
        undo stack. None when unchanged (or default Layer 1 on an empty page)."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            infos = rm_layer.layers_info(before.page)
            idx = int(layer_index)
            if not 0 <= idx < len(infos):
                raise EditError(f"no layer {idx} of {len(infos)}")
            target_node = infos[idx].node_id
            if before.page is None and target_node == rm_writer.LAYER_1:
                return None
            cur_node, _, _ = rm_layer.scene_info(before.page)
            if cur_node == target_node:
                return None
            edit = SceneFlags.new(doc.id, page_id, now_ms, current_layer=before.ref(target_node))
            self._add_edit(doc, edit)
            history.seen = self._seen(doc.id, page_id)
        return edit

    def set_background_visible(self, doc: Document, page_id: str, visible: bool,
                               now_ms: int) -> SceneFlags | None:
        """Persist SceneInfo field 2 (backgroundVisible / Hide template) as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            _, old_bg, _ = rm_layer.scene_info(before.page)
            new_bg = bool(visible)
            if old_bg == new_bg:
                return None
            edit = SceneFlags.new(doc.id, page_id, now_ms, background_visible=new_bg)
            self._add_edit(doc, edit)
            history.push([SceneFlagChange("background_visible", old_bg, new_bg)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def set_root_document_visible(self, doc: Document, page_id: str, visible: bool,
                                  now_ms: int) -> SceneFlags | None:
        """Persist SceneInfo field 3 (rootDocumentVisible / Text row) as one undo step."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            history = self._history(doc.id, page_id)
            before = self.staged(doc).replay(page_id)
            _, _, old_rd = rm_layer.scene_info(before.page)
            new_rd = bool(visible)
            if old_rd == new_rd:
                return None
            edit = SceneFlags.new(doc.id, page_id, now_ms, root_document_visible=new_rd)
            self._add_edit(doc, edit)
            history.push([SceneFlagChange("root_document_visible", old_rd, new_rd)])
            history.seen = self._seen(doc.id, page_id)
        return edit

    def delete_items(self, doc: Document, page_id: str, items, now_ms: int) -> bool:
        """Delete selected strokes and images of a page (the app's deleteSelectedItems),
        one action for undo. False when none of them is on the page any more."""
        with self._history_lock:
            doc = self._editable(doc, page_id)
            before, live = self._items(doc, page_id)
            chosen = [tuple(i) for i in items if tuple(i) in live]
            if not chosen:
                return False
            lines = [i for i in chosen if live[i].is_line]
            edits: list[Edit] = []
            changes: list = []
            if lines:
                edits.append(StrokeDelete.new(doc.id, page_id, [before.ref(i) for i in lines], now_ms))
                changes.append(StrokeChange(readd=tuple(StrokePart(before.ref(i), live[i].line.encode().hex())
                                                        for i in lines)))
            for i in chosen:
                if not live[i].is_line:
                    key = live[i].image.ref
                    edits.append(ImageDelete.new(doc.id, page_id, key, now_ms))
                    changes.append(Change(key.hex(), before.ref(i), None))
            history = self._history(doc.id, page_id)
            self._add_edits(doc, [(e, None) for e in edits])
            history.push(changes)
            history.seen = self._seen(doc.id, page_id)
            return True

    # -- pen settings --

    def tool_settings(self, doc: Document) -> dict[str, str]:
        """The pen settings of a document (its .content "extraMetadata", with the
        pending ToolSettings over it): {} when it has none or they cannot be read."""
        synced = self.synced(doc.id)
        extra: dict = {}
        if isinstance(synced, Document) and f"{doc.id}.content" in synced.files:
            try:
                content = json.loads(self.blob(synced, f"{doc.id}.content"))
                extra = content.get("extraMetadata") if isinstance(content, dict) else {}
            except (SyncError, OSError, ValueError) as err:
                log.warning("cannot read the pen settings of %s: %s", doc.id, err)
        out = {str(k): str(v) for k, v in (extra if isinstance(extra, dict) else {}).items()}
        for edit in self.sync.edits.of(doc.id):
            if isinstance(edit, ToolSettings):
                out.update(edit.values)
        return out

    def set_tool_settings(self, doc: Document, values: dict[str, str], now_ms: int) -> ToolSettings | None:
        """Keep new pen settings for a document (`values`: extraMetadata keys), merged
        into its one pending ToolSettings. None when they change nothing."""
        doc = self._editable(doc)
        current = self.tool_settings(doc)
        values = {str(k): str(v) for k, v in values.items()}
        if all(current.get(k) == v for k, v in values.items()):
            return None
        old = [e for e in self.sync.edits.of(doc.id) if isinstance(e, ToolSettings)]
        merged: dict[str, str] = {}
        for edit in old:
            merged.update(edit.values)
        merged.update(values)
        edit = ToolSettings.new(doc.id, merged, now_ms)
        self.sync.edits.replace(old, edit)
        with self._lock:
            self._staged.clear()
        return edit

    def forget_history(self, doc_id: str) -> None:
        """The document was closed: its undo and redo go, as the app's go with its pages."""
        with self._history_lock:
            for key in [k for k in self._histories if k[0] == doc_id]:
                del self._histories[key]

    def _editable(self, doc: Document, page_id: str | None = None) -> Document:
        """The document as last synced, when it may be edited (and has the page
        `page_id`, with the pending edits: a page added here counts)."""
        refusal = self.edit_refusal(doc)
        if refusal:
            raise EditError(refusal)
        doc = self.synced(doc.id)                # the document as last synced
        if page_id is not None and page_id not in self.staged(doc).ids:
            raise EditError("this page is not in the document's page list")
        return doc

    def _add_edit(self, doc: Document, edit: Edit, data: bytes | None = None) -> None:
        self._add_edits(doc, [(edit, data)])

    def _add_edits(self, doc: Document, edits: list[tuple[Edit, bytes | None]]) -> None:
        """Work the document out with the edits (and the files of inserted images) before
        keeping them: edits the writer refuses leave nothing behind."""
        new = {id(e): data for e, data in edits}
        document_files(doc, self.sync.edits.of(doc.id) + [e for e, _ in edits], lambda name: self.blob(doc, name),
                       lambda e: new[id(e)] if id(e) in new else self.sync.edits.data(e), self.sync.author())
        for edit, data in edits:
            self.sync.edits.add(edit, data)
        with self._lock:
            self._scenes.clear()
            self._staged.clear()

    def _page_data(self, doc: Document, page: PageRef) -> bytes | None:
        """A page's .rm as it is to be shown: the cloud's file (None: the page has
        none) with the pending edits made to it, or a page added or copied here."""
        staged = self.staged(doc)
        if not staged.page_edits.get(page.page_id) and page.page_id not in staged.origin:
            return self.blob(doc, page.rm_name) if page.rm_name else None
        try:
            return staged.page(page.page_id)
        except EditError as err:
            log.error("cannot show the pending edits of page %s: %s", page.page_id, err)
            return self.blob(doc, page.rm_name) if page.rm_name else None

    def page_scene(self, doc: Document, page: PageRef) -> PageScene | None:
        """Parsed strokes/text for a page, with the edits made here that are not
        uploaded yet; None if the page has no .rm file and no such edit."""
        staged = self.staged(doc)
        if page.rm_name is None and not staged.has_file(page.page_id):
            return None
        key = (doc.id, page.page_id, doc.files.get(page.rm_name or "") or "", staged.key)
        with self._lock:
            if key in self._scenes:
                self._scenes.move_to_end(key)
                return self._scenes[key]
        try:
            data = self._page_data(doc, page)
            scene = parse(data) if data is not None else None
        except RmParseError as err:
            log.error("cannot parse page %s: %s", page.page_id, err)
            scene = None
        if scene is not None:
            self._attach_images(doc, page, scene)
        with self._lock:
            self._scenes[key] = scene
            while len(self._scenes) > self._scene_cache:
                self._scenes.popitem(last=False)
        return scene

    def _attach_images(self, doc: Document, page: PageRef, scene: PageScene) -> None:
        """Give each image item its file, stored with the document as
        "<doc>/<page>/<file name>" (or not uploaded yet). One that is missing or not
        cached stays undrawn."""
        staged = self.staged(doc)
        folder = f"{doc.id}/{page.page_id}"
        for image in scene.placed_images:
            source = staged.image_source(page.page_id, image.file_name) if image.file_name else None
            if source is None:
                log.warning("%s: image %s has no file %r", folder, image.item_id, image.file_name)
                continue
            try:
                image.data = self.blob(doc, source[1]) if source[0] == "doc" else self.sync.edits.data(source[1])
            except (SyncError, EditError, OSError) as err:
                log.warning("%s: image %s: %s", folder, image.file_name, err)

    def pagedata(self, doc: Document) -> list[str] | None:
        """Legacy per-page template names, when the document has a .pagedata file."""
        name = pagedata_name(doc)
        return parse_pagedata(self.blob(doc, name)) if name else None

    def export_rmdoc(self, doc: Document, path):
        """Write the document as a .rmdoc archive (SyncClient.export_rmdoc)."""
        return self.sync.export_rmdoc(doc.id, path, cached_only=self.cached_only)

    def pdf_path(self, doc: Document):
        """Local file of the document's PDF rendition (fetched into the blob cache
        if needed), or None if the document has no PDF."""
        name = pdf_name(doc)
        if name is None:
            return None
        self.blob(doc, name)
        return self.sync.blobs.path(doc.files[name])
