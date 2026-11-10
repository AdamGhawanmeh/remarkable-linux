"""Library queries for the navigator, and page/PDF access for the viewer.

Qt-free so the navigation rules can be tested on their own. The views and their
order mirror the official sidebar (qml/client/Sidebar.qml):

    My files | Notebooks, PDFs, Ebooks, Favorites, Tags | Import, Trash, Settings
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from core.models import (ROOT_ID, TRASH_ID, Content, Document, DocumentTree, FileType, Folder, Node,
                         Page)
from core.edits import (PAGE_EDITS, Change, Edit, EditError, ImageDelete, ImageInsert, ImageTransform, PageAdd,
                        PageCopy, PageDelete, PageHistory, PageMove, Staged, document_files, swapped)
from core.sync import SyncClient, SyncError, sha256_hex
from viewer import rm_writer
from viewer.rm_parser import PageScene, RmParseError, parse
from viewer.templates import parse_pagedata

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


class LibrarySource:
    """The library as the UI sees it: a tree plus page/PDF data, via SyncClient.

    `cached_only` keeps every read local (offline mode). Parsed scenes are kept in a
    small LRU so flipping pages back and forth does not re-parse.

    `editable` names the notebooks whose pages may be edited here (none by default):
    editing is being built feature by feature, and is tried on notebooks named for it.
    Edits are kept locally (SyncClient.edits) and drawn over the cached pages; uploading
    them is SyncClient.push_edits.
    """

    def __init__(self, sync: SyncClient, *, cached_only: bool = False, scene_cache: int = 64,
                 editable: tuple[str, ...] = ()) -> None:
        self.sync = sync
        self.cached_only = cached_only
        self.editable = tuple(editable)
        self._scenes: OrderedDict[str, PageScene | None] = OrderedDict()
        self._scene_cache = scene_cache
        self._lock = threading.Lock()
        # Undo and redo, per (document id, page id), while the document is open.
        self._histories: dict[tuple[str, str], PageHistory] = {}
        # Documents worked out with their pending edits, by id: (key, Staged).
        self._staged: dict[str, tuple[tuple, Staged]] = {}
        self._history_lock = threading.RLock()

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
        return self.sync.get_blob(doc.files[name], name, cached_only=self.cached_only)

    # -- editing ------------------------------------------------------------------

    def edit_refusal(self, doc: Document | None) -> str:
        """Why `doc` may not be edited here ("": it may): only a notebook, not in the
        trash, whose name is one of `editable` and the only notebook of that name, so
        an edit can never land on a document it was not meant for."""
        if not self.editable:
            return "Editing is not turned on"
        tree = self.sync.tree
        if not isinstance(doc, Document) or tree.get(doc.id) is None:
            return "This document is not in the library"
        if doc.name not in self.editable:
            return f"Editing is only turned on for the notebook named {' or '.join(map(repr, self.editable))}"
        if doc.file_type is not FileType.NOTEBOOK:
            return "Only notebooks can be edited"
        if tree.is_trashed(doc.id):
            return "A document in the trash cannot be edited"
        same = [n for n in tree.nodes.values() if isinstance(n, Document) and n.name == doc.name
                and n.file_type is FileType.NOTEBOOK and not tree.is_trashed(n.id)]
        if len(same) != 1:
            return f"{len(same)} notebooks are named {doc.name!r}: editing needs the name to be of one notebook only"
        return ""

    def can_edit(self, doc: Document | None) -> bool:
        return not self.edit_refusal(doc)

    def pending_edits(self, doc: Document, page_id: str | None = None) -> list[Edit]:
        return [e for e in self.sync.edits.of(doc.id) if page_id is None or e.page_id == page_id]

    def staged(self, doc: Document) -> Staged:
        """The document as last synced with its pending edits (core.edits.Staged)."""
        synced = self.sync.tree.get(doc.id)
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

    def view(self, doc: Node) -> Node:
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
        """`tree` with each document that has pending page edits as view() shows it."""
        documents = set(self.sync.edits.documents())
        if not documents & set(tree.nodes):
            return tree
        return DocumentTree([self.view(node) if node.id in documents else node for node in tree.nodes.values()])

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

    def insert_image(self, doc: Document, page_id: str, data: bytes, extension: str, vertices,
                     now_ms: int, timestamps=None, action: list[Change] | None = None) -> ImageInsert:
        """Put an image (`data`: a PNG or JPEG file as the app stores one, see
        ui.editing.encode_image) on a page, here only: the edit is kept with the cache
        and drawn over the page until SyncClient.push_edits uploads it. EditError when
        `doc` may not be edited, or the page cannot take the image safely. It can be
        undone: on its own, or as part of `action` (see edit_action)."""
        doc = self._editable(doc, page_id)
        edit = ImageInsert.new(doc.id, page_id, extension, vertices, sha256_hex(data), len(data), now_ms,
                               timestamps)
        with self.edit_action(doc, page_id, action) as changes:
            self._add_edit(doc, edit, data)
            changes.append(Change(edit.key, None, ("edit", edit.uid)))
        return edit

    def image_file(self, doc_id: str, page_id: str, file_name: str) -> bytes:
        """The file of an image of a page, as stored with the document
        ("<doc>/<page>/<file name>"), or put on the page here and not uploaded yet: what
        the app's paste loads to store a copy (0x1409a8240). EditError when it cannot be
        had (the document is gone, or the file is not cached offline)."""
        doc = self.sync.tree.get(doc_id)
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
        synced = self.sync.tree.get(doc_id)
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
            edits, back = swapped(source[-1], doc.id, page_id, now_ms)
            self._add_edits(doc, [(e, None) for e in edits])
            source.pop()
            target.append(back)
            history.seen = self._seen(doc.id, page_id)
            return True

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
        doc = self.sync.tree.get(doc.id)                # the document as last synced
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
