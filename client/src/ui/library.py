"""Library queries for the navigator, and page/PDF access for the viewer.

Qt-free so the navigation rules can be tested on their own. The views and their
order mirror the official sidebar (qml/client/Sidebar.qml):

    My files | Notebooks, PDFs, Ebooks, Favorites, Tags | Import, Trash, Settings
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from core.models import (ROOT_ID, TRASH_ID, Document, DocumentTree, FileType, Folder, Node,
                         Page)
from core.sync import SyncClient
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
    """

    def __init__(self, sync: SyncClient, *, cached_only: bool = False, scene_cache: int = 64) -> None:
        self.sync = sync
        self.cached_only = cached_only
        self._scenes: OrderedDict[str, PageScene | None] = OrderedDict()
        self._scene_cache = scene_cache
        self._lock = threading.Lock()

    @property
    def writable(self) -> bool:
        """Changes (imports, folders, moves, trash) go to the cloud: not when offline."""
        return not self.cached_only

    def load_cached(self) -> DocumentTree:
        return self.sync.load_cached()

    def refresh(self) -> DocumentTree:
        if self.cached_only:
            return self.sync.load_cached()
        return self.sync.refresh()

    def forget(self) -> None:
        """Drop the cached library (logging out)."""
        self.sync.forget()
        with self._lock:
            self._scenes.clear()

    def blob(self, doc: Document, name: str) -> bytes:
        return self.sync.get_blob(doc.files[name], name, cached_only=self.cached_only)

    def page_scene(self, doc: Document, page: PageRef) -> PageScene | None:
        """Parsed strokes/text for a page; None if the page has no .rm file."""
        if page.rm_name is None:
            return None
        key = doc.files[page.rm_name]
        with self._lock:
            if key in self._scenes:
                self._scenes.move_to_end(key)
                return self._scenes[key]
        try:
            scene = parse(self.blob(doc, page.rm_name))
        except RmParseError as err:
            log.error("cannot parse %s: %s", page.rm_name, err)
            scene = None
        with self._lock:
            self._scenes[key] = scene
            while len(self._scenes) > self._scene_cache:
                self._scenes.popitem(last=False)
        return scene

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
