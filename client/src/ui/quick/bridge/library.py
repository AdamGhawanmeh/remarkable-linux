"""xofm.libs.library and com.remarkable library types for the official QML.

Names, members and enum values are the app's own (its moc metadata, recorded in
bridge/qml_api.json); behaviour comes from its QML and, where the QML does not say,
from the binary (noted per member):
    Library (LibraryFacade)     entryForId(id) → QmlEntryWrapper/QmlDocumentWrapper, trashId
    LibraryView                 filter/sort flags; base of the two list models below
    LibraryNavigator            one folder or filter view: entryIds, pathIds, navigateTo()
    LibrarySearch               name search under a root
    QmlEntryWrapper             one folder or document (QmlDocumentWrapper adds pages)
    LibraryController           changes: createCollection, setVisibleName, moveEntries, ...
    DocumentSelection           the selected ids
    Entry, Document, DocumentIO enums only
They read the synced tree (LibrarySource) and send changes through the Module 6
SyncClient write API.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Callable

from PySide6.QtCore import (Property, QCoreApplication, QObject, QPointF, QSizeF, Qt, QTimer, Signal, Slot,
                            QEnum)

from core.models import (ROOT_ID, TRASH_ID, Document as DocumentNode, DocumentTree, FileType, Node,
                         unique_name)

from ...library import document_tags

log = logging.getLogger("rm.ui.quick.library")


def tr(context: str, text: str, n: int = -1) -> str:
    """The app's translations (reMarkable_en.qm, installed by QuickApp) for C++ strings."""
    return QCoreApplication.translate(context, text, None, n)


def _round(x: float) -> int:
    """C round(): halves away from zero."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


# StringUtils (xofm/libs/library/src/shared.cpp): thresholds and units in ms.
_UNITS = ((31_536_000_000, "year"), (2_592_000_000, "month"), (86_400_000, "day"), (3_600_000, "hour"),
          (60_000, "minute"))


def relative_time(delta_ms: int) -> str:
    """"just now" under a minute, else the largest unit reached, rounded; future times
    (negative delta) read "in %n ..."."""
    span = abs(delta_ms)
    if span < 60_000:
        return tr("StringUtils", "just now")
    for unit, name in _UNITS:
        if span >= unit or name == "minute":
            n = _round(span / unit)
            text = f"in %n {name}(s)" if delta_ms < 0 else f"%n {name}(s) ago"
            return tr("StringUtils", text, n)
    raise AssertionError


def _ms(when: datetime | None) -> int:
    return int(when.timestamp() * 1000) if when else 0


# -- enums --------------------------------------------------------------------------

class LibraryView(QObject):
    """Base of LibraryNavigator and LibrarySearch: which entries, in which order.

    One model lists either the children of currentDirectoryId or, with ignoreParent,
    the whole library (trashed entries only with Trashed); the filter keeps folders
    (Collections) and documents by type (NativeDocuments, PDFs, Ebooks), only pinned
    ones with Pinned, and with filterTags only documents carrying all of them.
    Updates are coalesced on a timer of updateInterval ms (0: next event loop), as the
    app does; updateNow() applies them at once."""

    Filter = IntEnum("Filter", {"None": 0, "NativeDocuments": 1, "Collections": 2, "PDFs": 4, "Ebooks": 8,
                                "Pinned": 16, "Trashed": 32, "ThirdParty": 64, "Tags": 128,
                                "ExcludeArchived": 256, "ImportedDocuments": 12, "Documents": 13,
                                "AllEntries": 15})
    SortOrder = IntEnum("SortOrder", {"LastModifiedDate": 0, "Size": 1, "Alphabetically": 2,
                                      "RecentlyOpenedDate": 3, "FileSize": 4, "DateCreated": 5})
    QEnum(Filter)
    QEnum(SortOrder)

    entryIdsChanged = Signal()
    filterChanged = Signal()
    rootDirectoryChanged = Signal()
    sortOrderChanged = Signal()
    currentDirectoryChanged = Signal()
    updateIntervalChanged = Signal()
    ignoreParentChanged = Signal()
    filterTagsChanged = Signal("QStringList")
    libraryChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library: Library | None = None
        self._filter = 0
        self._sort = 0
        self._directory = ROOT_ID
        self._root = ROOT_ID
        self._ignore_parent = False
        self._filter_tags: list[str] = []
        self._ids: list[str] = []
        self._staged: bool | None = None     # entryIds taken in a library change, not yet announced
        self._timer = QTimer(self, singleShot=True, interval=0)
        self._timer.timeout.connect(self.updateNow)

    # -- properties --
    def _set_library(self, library: QObject | None) -> None:
        if library is self._library:
            return
        if self._library is not None:
            self._library.changing.disconnect(self._stage)
            self._library.changed.disconnect(self._library_changed)
        self._library = library
        if library is not None:
            library.changing.connect(self._stage)
            library.changed.connect(self._library_changed)
        self.libraryChanged.emit()
        self.scheduleUpdateEntries()

    library = Property(QObject, lambda self: self._library, _set_library, notify=libraryChanged)

    @Slot(QObject)
    def setLibrary(self, library: QObject | None) -> None:
        self._set_library(library)

    @Slot(int)
    def setFilter(self, value: int) -> None:
        if self._filter != int(value):
            self._filter = int(value)
            self.filterChanged.emit()
            self.scheduleUpdateEntries()

    filter = Property(int, lambda self: self._filter, setFilter, notify=filterChanged)

    @Slot(int)
    def setSortOrder(self, value: int) -> None:
        if self._sort != int(value):
            self._sort = int(value)
            self.sortOrderChanged.emit()
            self.scheduleUpdateEntries()

    sortOrder = Property(int, lambda self: self._sort, setSortOrder, notify=sortOrderChanged)

    @Slot(str)
    def setCurrentDirectoryId(self, value: str) -> None:
        value = str(value or "")
        if self._directory != value:
            self._directory = value
            self.currentDirectoryChanged.emit()
            self.scheduleUpdateRootDirectoryId()
            self.scheduleUpdateEntries()

    currentDirectoryId = Property(str, lambda self: self._directory, setCurrentDirectoryId,
                                  notify=currentDirectoryChanged)
    currentDirectory = Property(QObject, lambda self: self._library.entryForId(self._directory)
                                if self._library is not None and self._directory else None,
                                notify=currentDirectoryChanged)
    rootDirectoryId = Property(str, lambda self: self._root, notify=rootDirectoryChanged)
    entryIds = Property("QStringList", lambda self: list(self._ids), notify=entryIdsChanged)

    @Slot(int)
    def setUpdateInterval(self, value: int) -> None:
        if self._timer.interval() != int(value):
            self._timer.setInterval(int(value))
            self.updateIntervalChanged.emit()

    updateInterval = Property(int, lambda self: self._timer.interval(), setUpdateInterval,
                              notify=updateIntervalChanged)

    @Slot(bool)
    def setIgnoreParent(self, value: bool) -> None:
        if self._ignore_parent != bool(value):
            self._ignore_parent = bool(value)
            self.ignoreParentChanged.emit()
            self.scheduleUpdateEntries()

    ignoreParent = Property(bool, lambda self: self._ignore_parent, setIgnoreParent, notify=ignoreParentChanged)

    @Slot("QStringList")
    def setFilterTags(self, tags: list) -> None:
        tags = [str(t) for t in tags or []]
        if self._filter_tags != tags:
            self._filter_tags = tags
            self.filterTagsChanged.emit(tags)
            self.scheduleUpdateEntries()

    filterTags = Property("QStringList", lambda self: list(self._filter_tags), setFilterTags,
                          notify=filterTagsChanged)

    # -- updates --
    @Slot()
    def scheduleUpdateEntries(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    @Slot()
    def scheduleUpdateRootDirectoryId(self) -> None:
        self._update_root()

    @Slot(result=bool)
    def isUpdateRequested(self) -> bool:
        return self._timer.isActive()

    @Slot()
    def updateNow(self) -> None:
        if self._take_ids():
            self.entryIdsChanged.emit()

    def _take_ids(self) -> bool:
        self._timer.stop()
        self._update_root()
        ids = [n.id for n in self._sorted(self._select())] if self._library is not None else []
        if ids == self._ids:
            return False
        self._ids = ids
        return True

    # A library change reaches the views in two steps, Library.changing then .changed: views
    # without a coalescing interval take their new ids in the first and announce them in the
    # second, so each one's handlers already read the other views' new ids (ContentEntries
    # joins the folder and document views' lists; one stale list names entries now gone).
    def _stage(self) -> None:
        if self._timer.interval() == 0:
            self._staged = self._take_ids()

    def _library_changed(self) -> None:
        staged, self._staged = self._staged, None
        if staged is None:
            self.scheduleUpdateEntries()
        elif staged:
            self.entryIdsChanged.emit()

    def _update_root(self) -> None:
        tree = self._library.tree if self._library is not None else None
        trashed = self._directory == TRASH_ID or (tree is not None and bool(self._directory)
                                                  and tree.is_trashed(self._directory))
        root = TRASH_ID if trashed else ROOT_ID
        if root != self._root:
            self._root = root
            self.rootDirectoryChanged.emit()

    @Slot(result="QVariantList")
    def entryIdsWithExtraInfo(self) -> list:
        """The entries as TagsView merges them with Library.taggedPages() records."""
        tree = self._library.tree
        out = []
        for i in self._ids:
            node = tree.get(i)
            if node is not None:
                out.append({"id": i, "documentId": i, "isPage": False, "timestamp": _ms(node.last_modified),
                            "tags": sorted(document_tags(node)) if isinstance(node, DocumentNode) else []})
        return out

    # -- selection and order --
    def _candidates(self) -> list[Node]:
        tree = self._library.tree
        if not self._ignore_parent:
            return tree.children(self._directory or ROOT_ID)
        trashed = bool(self._filter & LibraryView.Filter["Trashed"])
        return [n for n in tree.nodes.values() if trashed or not tree.is_trashed(n.id)]

    def _select(self) -> list[Node]:
        f, flags = self._filter, LibraryView.Filter
        kinds = {FileType.NOTEBOOK: f & flags["NativeDocuments"], FileType.PDF: f & flags["PDFs"],
                 FileType.EPUB: f & flags["Ebooks"]}
        out = []
        for node in self._candidates():
            if node.is_folder:
                if not f & flags["Collections"]:
                    continue
            elif not kinds.get(node.file_type):
                continue
            if f & flags["Pinned"] and not node.pinned:
                continue
            # Tags: only entries with entry tags; filterTags narrows to those carrying all.
            tags = set(node.content.tags)
            if f & flags["Tags"] and not tags:
                continue
            if self._filter_tags and not set(self._filter_tags) <= tags:
                continue
            out.append(node)
        return out

    def _sorted(self, nodes: list[Node]) -> list[Node]:
        by_name = sorted(nodes, key=lambda n: (n.name.casefold(), n.id))
        order = self._sort
        if order == 2:
            return by_name
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        key = {
            0: lambda n: n.last_modified or epoch,
            1: lambda n: n.page_count if isinstance(n, DocumentNode) else 0,
            3: lambda n: n.metadata.last_opened or epoch,
            4: lambda n: n.content.size_in_bytes if isinstance(n, DocumentNode) else 0,
            5: lambda n: n.metadata.created_time or epoch,
        }.get(order, lambda n: n.last_modified or epoch)
        return sorted(by_name, key=key, reverse=True)


class LibraryNavigator(LibraryView):
    """The navigator's models: the folder path for the breadcrumbs, and navigation."""

    pathChanged = Signal()
    isMovingChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._moving = False
        self.currentDirectoryChanged.connect(self.updatePath)
        self.rootDirectoryChanged.connect(self.updatePath)

    def _path(self) -> list[Node]:
        if self._library is None or not self._directory or self._directory == TRASH_ID:
            return []
        return self._library.tree.breadcrumbs(self._directory)

    pathIds = Property("QStringList", lambda self: [self._root] + [n.id for n in self._path()], notify=pathChanged)
    pathNames = Property("QStringList", lambda self: [""] + [n.name for n in self._path()], notify=pathChanged)

    @Slot(bool)
    def setIsMoving(self, value: bool) -> None:
        if self._moving != bool(value):
            self._moving = bool(value)
            self.isMovingChanged.emit()

    isMoving = Property(bool, lambda self: self._moving, setIsMoving, notify=isMovingChanged)

    @Slot(str)
    def navigateTo(self, entry_id: str) -> None:
        self.setCurrentDirectoryId(entry_id)

    @Slot()
    def navigateUp(self) -> None:
        path = self._path()
        self.setCurrentDirectoryId(path[-2].id if len(path) > 1 else self._root)

    @Slot()
    def updatePath(self) -> None:
        self.pathChanged.emit()


class LibrarySearch(LibraryView):
    """Entries under currentDirectoryId (all of My files from the root) whose name
    contains searchTerm; with an empty term, the recently opened documents if
    showRecentsIfTermEmpty, else nothing. maximumResults (0: no limit) caps the list."""

    searchTermChanged = Signal()
    showRecentsIfTermEmptyChanged = Signal(bool)
    maximumResultsChanged = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._term = ""
        self._recents = False
        self._maximum = 0

    @Slot(str)
    def setSearchTerm(self, term: str) -> None:
        if self._term != str(term):
            self._term = str(term)
            self.searchTermChanged.emit()
            self.scheduleUpdateEntries()

    searchTerm = Property(str, lambda self: self._term, setSearchTerm, notify=searchTermChanged)

    @Slot(bool)
    def setShowRecentsIfTermEmpty(self, value: bool) -> None:
        if self._recents != bool(value):
            self._recents = bool(value)
            self.showRecentsIfTermEmptyChanged.emit(self._recents)
            self.scheduleUpdateEntries()

    showRecentsIfTermEmpty = Property(bool, lambda self: self._recents, setShowRecentsIfTermEmpty,
                                      notify=showRecentsIfTermEmptyChanged)

    @Slot(int)
    def setMaximumResults(self, value: int) -> None:
        if self._maximum != int(value):
            self._maximum = int(value)
            self.maximumResultsChanged.emit(self._maximum)
            self.scheduleUpdateEntries()

    maximumResults = Property(int, lambda self: self._maximum, setMaximumResults, notify=maximumResultsChanged)

    def _candidates(self) -> list[Node]:
        tree = self._library.tree
        directory = self._directory or ROOT_ID
        if directory == ROOT_ID:
            return [n for n in tree.nodes.values() if not tree.is_trashed(n.id)]
        if directory == TRASH_ID:
            return [n for n in tree.nodes.values() if tree.is_trashed(n.id)]
        return tree.descendants(directory)

    def _select(self) -> list[Node]:
        nodes = super()._select()
        term = self._term.strip().casefold()
        if term:
            nodes = [n for n in nodes if term in n.name.casefold()]
        elif self._recents:
            nodes = [n for n in nodes if n.metadata.last_opened]
        else:
            nodes = []
        return nodes

    def _sorted(self, nodes: list[Node]) -> list[Node]:
        if not self._term.strip() and self._recents:
            epoch = datetime.min.replace(tzinfo=timezone.utc)
            nodes = sorted(nodes, key=lambda n: n.metadata.last_opened or epoch, reverse=True)
        else:
            nodes = super()._sorted(nodes)
        return nodes[:self._maximum] if self._maximum > 0 else nodes


class DocumentIOEnums(QObject):
    LoadErrorType = IntEnum("LoadErrorType", {"None": 0, "LegacyLinesConversionFailed": 1,
                                              "UnsupportedFormatVersion": 2, "MissingPdf": 3, "CloudArchive": 4,
                                              "PotentiallyUnstable": 5, "Unspecified": 15})
    QEnum(LoadErrorType)


FILE_TYPES = {FileType.NOTEBOOK: 1, FileType.PDF: 2, FileType.EPUB: 3}
_OK = 2   # Entry.Ok


# -- entries ------------------------------------------------------------------------

class EntryWrapper(QObject):
    """QmlEntryWrapper: one folder (or the base of a document) as the QML reads it. In
    QML it is the `Entry` type (`property Entry entry`), which also carries the Entry
    enums (Entry.Collection, Entry.ExcludedFromSync, ...)."""

    Type = IntEnum("Type", {"Invalid": 0, "Document": 1, "Collection": 2, "Template": 3})
    Status = IntEnum("Status", {"Unknown": 1, "Ok": 2, "Uploading": 4, "Downloading": 8, "Exporting": 16,
                                "DocumentError": 32, "SyncError": 64, "ExcludedFromSync": 128, "Archived": 256})
    ConflictHandling = IntEnum("ConflictHandling", {"NoAction": 0, "Retry": 1, "KeepLocal": 2})
    QEnum(Type)
    QEnum(Status)
    QEnum(ConflictHandling)

    parentIdChanged = Signal()
    statusChanged = Signal()
    statusAdditionalTextChanged = Signal()
    syncProgressChanged = Signal()
    exportProgressChanged = Signal()
    subtextChanged = Signal()
    progressTextChanged = Signal()
    lastModifiedTextChanged = Signal()
    thumbnailChanged = Signal()
    lastModifiedChanged = Signal()
    createdTimeChanged = Signal()
    createdTextChanged = Signal()
    visibleNameChanged = Signal()
    isPinnedChanged = Signal()
    isTrashedChanged = Signal()
    excludedFromSyncChanged = Signal()
    entryTagsChanged = Signal()
    isNewChanged = Signal(bool)
    sourceChanged = Signal(str)

    def __init__(self, node: Node, library: "Library") -> None:
        super().__init__(library)
        self._node = node
        self._library = library

    @property
    def node(self) -> Node:
        return self._node

    def update(self, node: Node) -> None:
        self._node = node
        for signal in (self.parentIdChanged, self.statusChanged, self.subtextChanged, self.progressTextChanged,
                       self.lastModifiedTextChanged, self.thumbnailChanged, self.lastModifiedChanged,
                       self.createdTimeChanged, self.createdTextChanged, self.visibleNameChanged,
                       self.isPinnedChanged, self.isTrashedChanged, self.entryTagsChanged):
            signal.emit()
        self.isNewChanged.emit(self._is_new())

    def _now(self) -> int:
        return self._library.now_ms()

    def _is_new(self) -> bool:
        return bool(self._node.metadata.raw.get("new"))

    def _last_modified_text(self) -> str:
        when = _ms(self._node.last_modified)
        return relative_time(self._now() - when) if when else ""

    def _subtext(self) -> str:
        return self._last_modified_text()

    def _progress_text(self) -> str:
        return ""

    id = Property(str, lambda self: self._node.id, constant=True)
    parentId = Property(str, lambda self: self._node.parent_id, notify=parentIdChanged)
    status = Property(int, lambda self: _OK, notify=statusChanged)
    statusAdditionalText = Property(str, lambda self: "", notify=statusAdditionalTextChanged)
    syncProgress = Property(float, lambda self: 0.0, notify=syncProgressChanged)
    exportProgress = Property(float, lambda self: 0.0, notify=exportProgressChanged)
    subtext = Property(str, lambda self: self._subtext(), notify=subtextChanged)
    progressText = Property(str, lambda self: self._progress_text(), notify=progressTextChanged)
    lastModifiedText = Property(str, lambda self: self._last_modified_text(), notify=lastModifiedTextChanged)
    createdText = Property(str, lambda self: relative_time(self._now() - _ms(self._node.metadata.created_time))
                           if self._node.metadata.created_time else "", notify=createdTextChanged)
    createdTime = Property("qlonglong", lambda self: _ms(self._node.metadata.created_time), notify=createdTimeChanged)
    isExporting = Property(bool, lambda self: False, notify=statusChanged)
    isDownloading = Property(bool, lambda self: False, notify=statusChanged)
    isExcludedFromSync = Property(bool, lambda self: False, notify=excludedFromSyncChanged)
    isNew = Property(bool, _is_new, notify=isNewChanged)
    source = Property(str, lambda self: "", notify=sourceChanged)
    isMethodsContent = Property(bool, lambda self: False, constant=True)
    eventSource = Property(str, lambda self: "", constant=True)
    lastModified = Property("qlonglong", lambda self: _ms(self._node.last_modified), notify=lastModifiedChanged)
    visibleName = Property(str, lambda self: self._node.name, notify=visibleNameChanged)
    isPinned = Property(bool, lambda self: self._node.pinned, notify=isPinnedChanged)
    isTrashed = Property(bool, lambda self: self._library.tree.is_trashed(self._node.id), notify=isTrashedChanged)
    type = Property(int, lambda self: 2 if self._node.is_folder else 1, constant=True)
    tags = Property("QStringList", lambda self: list(self._node_tags()), notify=entryTagsChanged)

    def _node_tags(self) -> list[str]:
        return list(self._node.content.tags)

    @Slot(int)
    def handleSyncError(self, handling: int) -> None:
        pass

    @Slot()
    def abortExport(self) -> None:
        pass

    @Slot(result=str)
    def fullPathName(self) -> str:
        return "/".join(n.name for n in self._library.tree.breadcrumbs(self._node.id))

    @Slot(result=str)
    def baseFilePath(self) -> str:
        return ""

    @Slot(result=str)
    def getSource(self) -> str:
        return ""


def _content(node: DocumentNode, key: str, default: Any = None) -> Any:
    value = node.content.raw.get(key)
    return default if value is None else value


_ALIGNMENT = {"left": Qt.AlignmentFlag.AlignLeft, "justify": Qt.AlignmentFlag.AlignJustify}
_ZOOM = {"bestFit": 0, "fitToWidth": 1, "fitToHeight": 2, "customFit": 3}


class DocumentWrapper(EntryWrapper):
    """QmlDocumentWrapper: a document entry; the QML `Document` type, with the Document
    enums."""

    FileType = IntEnum("FileType", {"Undefined": 0, "Notebook": 1, "Pdf": 2, "Ebook": 3})
    ZoomMode = IntEnum("ZoomMode", {"BestFit": 0, "FitToWidth": 1, "FitToHeight": 2, "CustomFit": 3})
    QEnum(FileType)
    QEnum(ZoomMode)

    pageCountChanged = Signal(int, int)
    fileTypeChanged = Signal()
    lastOpenedChanged = Signal()
    lastOpenedPageChanged = Signal(int)
    layoutPropertiesChanged = Signal()
    layoutProgressChanged = Signal()
    orientationChanged = Signal()
    zoomModeChanged = Signal()
    customZoomChanged = Signal()
    templatesChanged = Signal()
    thumbnailUpdated = Signal(int)
    tableOfContentsUpdated = Signal("QVariantList")
    tocRunningChanged = Signal()
    pageMoved = Signal(int, int)
    pageUpdated = Signal(int)
    pageAdded = Signal(int)
    pagesAdded = Signal("QVariantList")
    pagesMoved = Signal()
    pagesRemoved = Signal()
    pageMapChanged = Signal()
    coverPageNumberChanged = Signal(int)
    documentMetadataChanged = Signal()
    passwordChanged = Signal(str)
    lockedByPasswordChanged = Signal(bool)
    invalidPassword = Signal()
    loadingSucceeded = Signal()
    loadErrorChanged = Signal()
    archiveChanged = Signal()
    sizeInBytesChanged = Signal()
    redirectionPageMapChanged = Signal()
    pageTagsChanged = Signal()
    viewBackgroundFilterChanged = Signal()

    def update(self, node: Node) -> None:
        old = self._node.page_count
        super().update(node)
        self.pageCountChanged.emit(node.page_count, old)
        for signal in (self.lastOpenedChanged, self.layoutPropertiesChanged, self.orientationChanged,
                       self.zoomModeChanged, self.customZoomChanged, self.templatesChanged, self.pageMapChanged,
                       self.documentMetadataChanged, self.sizeInBytesChanged, self.redirectionPageMapChanged,
                       self.pageTagsChanged, self.viewBackgroundFilterChanged):
            signal.emit()
        self.lastOpenedPageChanged.emit(self._page())
        self.coverPageNumberChanged.emit(node.content.cover_page_number)

    def _page(self) -> int:
        return self._node.metadata.last_opened_page

    def _progress_text(self) -> str:
        """Document::progressText: "%1% read" for ebooks, else "Page %1 of %2"."""
        pages = self._node.page_count
        if FILE_TYPES.get(self._node.file_type) == 3:
            if pages <= 1:
                return "0% read"
            percent = float(_round(self._page() / (pages - 1) * 100.0))
            return tr("Document", "%1% read").replace("%1", f"{percent:g}")
        return tr("Document", "Page %1 of %2").replace("%1", str(self._page() + 1)).replace("%2", str(pages))

    def _subtext(self) -> str:
        """Document::subtext: the reading progress (downloads and password-protected
        PDFs, which this client does not have, read "Transferring" and
        "Password protected PDF")."""
        return self._progress_text()

    def _metadata(self) -> dict:
        value = self._node.content.raw.get("documentMetadata")
        return value if isinstance(value, dict) else {}

    pageCount = Property(int, lambda self: self._node.page_count, notify=pageCountChanged)
    fileType = Property(int, lambda self: FILE_TYPES.get(self._node.file_type, 0), constant=True)
    lastOpenedPage = Property(int, _page, notify=lastOpenedPageChanged)
    lastOpened = Property("qlonglong", lambda self: _ms(self._node.metadata.last_opened), notify=lastOpenedChanged)
    textScale = Property(float, lambda self: float(_content(self._node, "textScale", 1.0)),
                         notify=layoutPropertiesChanged)
    lineHeight = Property(float, lambda self: float(_content(self._node, "lineHeight", -1)),
                          notify=layoutPropertiesChanged)
    margins = Property(float, lambda self: float(_content(self._node, "margins", 0)), notify=layoutPropertiesChanged)
    fontName = Property(str, lambda self: str(_content(self._node, "fontName", "")), notify=layoutPropertiesChanged)
    textAlignment = Property(int, lambda self: int(_ALIGNMENT.get(_content(self._node, "textAlignment", "justify"),
                                                                  Qt.AlignmentFlag.AlignJustify).value),
                             notify=layoutPropertiesChanged)
    layoutProgress = Property(float, lambda self: 1.0, notify=layoutProgressChanged)
    lockedByPassword = Property(bool, lambda self: False, notify=lockedByPasswordChanged)
    coverPageNumber = Property(int, lambda self: self._node.content.cover_page_number, notify=coverPageNumberChanged)
    orientation = Property(int, lambda self: int((Qt.Orientation.Horizontal
                                                  if self._node.content.orientation == "landscape"
                                                  else Qt.Orientation.Vertical).value), notify=orientationChanged)
    zoomMode = Property(int, lambda self: _ZOOM.get(_content(self._node, "zoomMode", "bestFit"), 0),
                        notify=zoomModeChanged)
    customZoomScale = Property(float, lambda self: float(_content(self._node, "customZoomScale", 1.0)),
                               notify=customZoomChanged)
    customZoomCenter = Property(QPointF, lambda self: QPointF(float(_content(self._node, "customZoomCenterX", 0)),
                                                              float(_content(self._node, "customZoomCenterY", 0))),
                                notify=customZoomChanged)
    customZoomPageSize = Property(QSizeF, lambda self: QSizeF(float(_content(self._node, "customZoomPageWidth", 0)),
                                                              float(_content(self._node, "customZoomPageHeight", 0))),
                                  notify=customZoomChanged)
    customZoomOrientation = Property(int, lambda self: int(Qt.Orientation.Horizontal.value
                                                           if _content(self._node, "customZoomOrientation") == "landscape"
                                                           else Qt.Orientation.Vertical.value), notify=customZoomChanged)
    authors = Property("QStringList", lambda self: [str(a) for a in self._metadata().get("authors") or []],
                       notify=documentMetadataChanged)
    publisher = Property(str, lambda self: str(self._metadata().get("publisher", "")), notify=documentMetadataChanged)
    publicationDate = Property(str, lambda self: str(self._metadata().get("publicationDate", "")),
                               notify=documentMetadataChanged)
    language = Property(str, lambda self: str(self._metadata().get("language", "")), notify=documentMetadataChanged)
    loadError = Property(bool, lambda self: False, notify=loadErrorChanged)
    archived = Property(bool, lambda self: False, notify=archiveChanged)
    loadErrorType = Property(int, lambda self: 0, notify=loadErrorChanged)
    loadErrorDescription = Property(str, lambda self: "", notify=loadErrorChanged)
    sizeInBytes = Property("qlonglong", lambda self: self._node.content.size_in_bytes, notify=sizeInBytesChanged)
    redirectionPageMap = Property("QVariantList", lambda self: [p.redirect if p.redirect is not None else -1
                                                                for p in self._node.content.pages if not p.deleted],
                                  notify=redirectionPageMapChanged)
    pageTags = Property("QVariantList", lambda self: list(_content(self._node, "pageTags", [])),
                        notify=pageTagsChanged)
    authorId = Property(str, lambda self: "", constant=True)
    viewBackgroundFilter = Property(str, lambda self: str(_content(self._node, "viewBackgroundFilter", "")),
                                    notify=viewBackgroundFilterChanged)
    isImported = Property(bool, lambda self: FILE_TYPES.get(self._node.file_type) in (2, 3), constant=True)

    # -- pages --
    def _page_ids(self) -> list[str]:
        return self._node.content.page_ids

    def _page_tag_map(self) -> dict[str, list[str]]:
        tags: dict[str, list[str]] = {}
        for tag in _content(self._node, "pageTags", []):
            if isinstance(tag, dict) and tag.get("name") and tag.get("pageId"):
                tags.setdefault(str(tag["pageId"]), []).append(str(tag["name"]))
        return tags

    @Slot()
    def updateLastOpened(self) -> None:
        pass

    @Slot()
    def markOpened(self) -> None:
        pass

    @Slot(int, result=bool)
    def isNotepage(self, page: int) -> bool:
        """A page without a PDF/EPUB page behind it: every notebook page, and pages
        inserted into a PDF or EPUB."""
        if FILE_TYPES.get(self._node.file_type) not in (2, 3):
            return True
        pages = [p for p in self._node.content.pages if not p.deleted]
        return 0 <= page < len(pages) and pages[page].redirect is None

    @Slot(result="QVariantList")
    def getPageTags(self) -> list:
        ids, tags = self._page_ids(), self._page_tag_map()
        return [tags.get(pid, []) for pid in ids]

    @Slot(bool, "QVariantList", result="QStringList")
    def tagLabels(self, include_document_tags: bool, selection: list) -> list:
        ids, tags = self._page_ids(), self._page_tag_map()
        pages = [int(p) for p in selection] if selection else range(len(ids))
        labels = {t for p in pages if 0 <= p < len(ids) for t in tags.get(ids[p], [])}
        if include_document_tags:
            labels |= set(self._node.content.tags)
        return sorted(labels, key=str.casefold)

    @Slot(str, result=int)
    def pageNumberFromId(self, page_id: str) -> int:
        ids = self._page_ids()
        return ids.index(page_id) if page_id in ids else -1

    @Slot(str, result=int)
    def pageForId(self, page_id: str) -> int:
        return self.pageNumberFromId(page_id)

    @Slot(int, result=bool)
    def hasPageTags(self, page: int) -> bool:
        return bool(self.tagsForPage(page))

    @Slot(int, result="QStringList")
    def tagsForPage(self, page: int) -> list:
        ids = self._page_ids()
        return self._page_tag_map().get(ids[page], []) if 0 <= page < len(ids) else []

    @Slot(int, result=float)
    def scrollPosition(self, page: int) -> float:
        return 0.0

    @Slot(result=bool)
    def hasContentsOnAnyPage(self) -> bool:
        return any(name.endswith(".rm") for name in self._node.files)

    @Slot(int, result=str)
    def templateForPage(self, page: int) -> str:
        pages = [p for p in self._node.content.pages if not p.deleted]
        return pages[page].template if 0 <= page < len(pages) else ""

    @Slot(int, result=bool)
    def hasContentsOnPage(self, page: int) -> bool:
        ids = self._page_ids()
        return 0 <= page < len(ids) and f"{self._node.id}/{ids[page]}.rm" in self._node.files

    @Slot(int, result=str)
    def idForPage(self, page: int) -> str:
        ids = self._page_ids()
        return ids[page] if 0 <= page < len(ids) else ""

    @Slot()
    def setKeyboardUsedDuringSession(self) -> None:
        pass

    @Slot(result=bool)
    def isQuickSheet(self) -> bool:
        return False


class Library(QObject):
    """The Library singleton (xofm::libs::library::qml::LibraryFacade)."""

    downgradeDetected = Signal()
    entriesDeleted = Signal("QStringList")
    entriesRestored = Signal("QStringList")
    entriesTrashed = Signal("QStringList")
    entryAdded = Signal(str)
    entryImported = Signal(str, str)
    errorMessage = Signal(str)
    readyChanged = Signal(bool)
    changing = Signal()          # before `changed`: LibraryView takes its new ids
    changed = Signal()
    statusChanged = Signal(str)

    def __init__(self, parent: QObject | None = None, now: Callable[[], datetime] | None = None) -> None:
        super().__init__(parent)
        self._now = now
        self.tree = DocumentTree()
        self._entries: dict[str, EntryWrapper] = {}
        self._retired: list[EntryWrapper] = []
        self._ready = False

    def now_ms(self) -> int:
        return _ms(self._now() if self._now else datetime.now(timezone.utc))

    def set_tree(self, tree: DocumentTree, reset: bool = False) -> None:
        """Show `tree`. Entries that left it are reported (entriesDeleted, ...) unless
        `reset`: a different library replaces this one (logging out), nothing was deleted."""
        old = self.tree
        self.tree = tree
        for node in tree.nodes.values():
            entry = self._entries.get(node.id)
            if entry is not None and entry.node is not node:
                entry.update(node)
        gone = [i for i in self._entries if i not in tree]
        # Wrappers of removed entries are freed one change later: views drop their
        # delegates on a timer, and a delegate whose entry is freed first reads null.
        for entry in self._retired:
            entry.deleteLater()
        self._retired = [self._entries.pop(i) for i in gone]
        self.changing.emit()
        if reset:
            self.changed.emit()
            return
        added = [i for i in tree.nodes if i not in old]
        trashed = [i for i in tree.nodes if i in old and tree.is_trashed(i) and not old.is_trashed(i)]
        restored = [i for i in tree.nodes if i in old and old.is_trashed(i) and not tree.is_trashed(i)]
        if not self._ready:
            self._ready = True
            self.readyChanged.emit(True)
        elif added:
            for i in added:
                self.entryAdded.emit(i)
        if gone:
            self.entriesDeleted.emit(gone)
        if trashed:
            self.entriesTrashed.emit(trashed)
        if restored:
            self.entriesRestored.emit(restored)
        self.changed.emit()

    isReady = Property(bool, lambda self: self._ready, notify=readyChanged)
    trashId = Property(str, lambda self: TRASH_ID, constant=True)
    meetingNotesId = Property(str, lambda self: "", constant=True)

    @Slot(str, result=QObject)
    def entryForId(self, entry_id: str) -> EntryWrapper | None:
        node = self.tree.get(str(entry_id))
        if node is None:
            return None
        entry = self._entries.get(node.id)
        if entry is None:
            cls = EntryWrapper if node.is_folder else DocumentWrapper
            entry = self._entries[node.id] = cls(node, self)
        return entry

    @Slot(str, result=str)
    def parentIdForId(self, entry_id: str) -> str:
        node = self.tree.get(str(entry_id))
        return node.parent_id if node is not None else ""

    @Slot(result="QStringList")
    @Slot(bool, result="QStringList")
    def tagLabels(self, include_entry_tags: bool = False) -> list:
        labels = set()
        for doc in self.tree.documents():
            if self.tree.is_trashed(doc.id):
                continue
            for tag in _content(doc, "pageTags", []):
                if isinstance(tag, dict) and tag.get("name"):
                    labels.add(str(tag["name"]))
            if include_entry_tags:
                labels |= set(doc.content.tags)
        return sorted(labels, key=str.casefold)

    @Slot(result="QVariantList")
    def taggedPages(self) -> list:
        """One record per tagged page, as TagsView reads them."""
        out = []
        for doc in self.tree.documents():
            if self.tree.is_trashed(doc.id):
                continue
            ids = doc.content.page_ids
            by_page: dict[str, dict] = {}
            for tag in _content(doc, "pageTags", []):
                if not (isinstance(tag, dict) and tag.get("name") and tag.get("pageId") in ids):
                    continue
                record = by_page.setdefault(tag["pageId"], {
                    "id": tag["pageId"], "documentId": doc.id, "isPage": True, "page": ids.index(tag["pageId"]),
                    "tags": [], "timestamp": 0})
                record["tags"].append(str(tag["name"]))
                record["timestamp"] = max(record["timestamp"], int(tag.get("timestamp") or 0))
            out.extend(by_page.values())
        return out

    @Slot(str, result=str)
    def uniqueVisibleName(self, name: str) -> str:
        return unique_name(name, {n.name for n in self.tree.nodes.values()})

    @Slot(result=int)
    def countArchivedDocuments(self) -> int:
        return 0

    @Slot(QObject, result=str)
    def entityIdForEntry(self, entry: QObject) -> str:
        return entry.property("id") if entry is not None else ""

    @Slot()
    def purgeThumbnails(self) -> None:
        pass


# -- selection and changes ------------------------------------------------------------

_current_library: Callable[[], Library | None] = lambda: None


def set_library_source(provider: Callable[[], Library | None]) -> None:
    """DocumentSelection is created from QML without a library; the app's selection
    reads the app's library, so this tells it which one that is."""
    global _current_library
    _current_library = provider


class DocumentSelection(QObject):
    """xofm::libs::library::DocumentSelection: the selected entry ids, and one stashed id
    (push() moves the last selected id aside, pop() brings it back)."""

    selectionChanged = Signal()
    requestShareSelected = Signal("QStringList", str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ids: list[str] = []
        self._stash = ""

    def _nodes(self) -> list[Node]:
        library = _current_library()
        if library is None:
            return []
        return [n for n in (library.tree.get(i) for i in self._ids) if n is not None]

    def _set(self, ids: list[str]) -> None:
        if ids != self._ids:
            self._ids = ids
            self.selectionChanged.emit()

    ids = Property("QStringList", lambda self: list(self._ids), notify=selectionChanged)
    stash = Property(str, lambda self: self._stash, notify=selectionChanged)
    hasContent = Property(bool, lambda self: bool(self._ids), notify=selectionChanged)
    hasStash = Property(bool, lambda self: bool(self._stash), notify=selectionChanged)
    includesDownloading = Property(bool, lambda self: False, notify=selectionChanged)
    includesExporting = Property(bool, lambda self: False, notify=selectionChanged)
    includesFolders = Property(bool, lambda self: any(n.is_folder for n in self._nodes()), notify=selectionChanged)
    includesOnlyPinned = Property(bool, lambda self: bool(self._ids) and all(n.pinned for n in self._nodes()),
                                  notify=selectionChanged)
    includesDocuments = Property(bool, lambda self: any(not n.is_folder for n in self._nodes()),
                                 notify=selectionChanged)
    size = Property("qlonglong", lambda self: len(self._ids), notify=selectionChanged)

    @Slot(str, str)
    def shareSelected(self, email: str, format: str) -> None:
        self.requestShareSelected.emit(list(self._ids), email, format)

    @Slot(str)
    @Slot("QStringList")
    def add(self, ids) -> None:
        ids = [ids] if isinstance(ids, str) else [str(i) for i in ids]
        self._set(self._ids + [i for i in ids if i not in self._ids])

    @Slot("QVariantList")
    def replace(self, ids: list) -> None:
        self._set([str(i) for i in ids])

    @Slot(str)
    def remove(self, entry_id: str) -> None:
        self._set([i for i in self._ids if i != entry_id])

    @Slot(str)
    def toggle(self, entry_id: str) -> None:
        self.remove(entry_id) if entry_id in self._ids else self.add(entry_id)

    @Slot()
    def clear(self) -> None:
        self._set([])

    @Slot()
    def reset(self) -> None:
        self._stash = ""
        self._ids = []
        self.selectionChanged.emit()

    @Slot()
    def push(self) -> None:
        if self._ids:
            self._stash = self._ids[-1]
            self._ids = self._ids[:-1]
            self.selectionChanged.emit()

    @Slot()
    def pop(self) -> None:
        if self._stash:
            stash, self._stash = self._stash, ""
            self._ids = self._ids + [stash] if stash not in self._ids else self._ids
            self.selectionChanged.emit()

    @Slot()
    def updateSelection(self) -> None:
        library = _current_library()
        if library is not None:
            self._set([i for i in self._ids if i in library.tree])

    @Slot(str, result=bool)
    def isSelected(self, entry_id: str) -> bool:
        return entry_id in self._ids


def _ids(ids: Any) -> list[str]:
    return [str(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])]


class LibraryController(QObject):
    """xofm::libs::library::LibraryController: changes from the QML, run as cloud
    commits (Module 6) by `submit(what, change, done)`. Page and notebook editing is not
    part of this client: those calls are accepted and logged."""

    entryMoved = Signal(str)

    def __init__(self, library: Library, submit: Callable[[str, Callable, str], None],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.submit = submit

    def _unsupported(self, what: str) -> None:
        log.info("%s: not available in this client", what)

    @Slot(str, str, result=str)
    def createCollection(self, parent_id: str, name: str) -> str:
        self.submit(f"Creating {name}", lambda sync, _: sync.create_folder(name, parent_id or ROOT_ID),
                    f"Created folder {name}")
        return ""

    @Slot(str, str, result=str)
    @Slot(str, str, str, result=str)
    @Slot(str, str, str, str, result=str)
    def createDocument(self, directory_id: str, name: str, uuid: str = "", page_uuid: str = "") -> str:
        self._unsupported("createDocument")
        return ""

    @Slot(str, str, str, "QVariantList", result=str)
    @Slot(str, str, str, "QVariantList", "QJSValue", result=str)
    def createDocumentFromExisting(self, directory_id: str, name: str, source_id: str, pages: list,
                                   callback=None) -> str:
        self._unsupported("createDocumentFromExisting")
        return ""

    @Slot(result="QVariantList")
    def quickSheets(self) -> list:
        return []

    @Slot(str, str, result=str)
    @Slot(str, str, bool, result=str)
    def cloneEntry(self, entry_id: str, collection_id: str, clean: bool = False) -> str:
        self._unsupported("cloneEntry")
        return ""

    @Slot("QStringList", str)
    def moveEntries(self, ids: list, collection_id: str) -> None:
        ids = _ids(ids)
        target = collection_id or ROOT_ID
        self.submit("Moving", lambda sync, _: sync.move(ids, target), "Moved")
        for i in ids:
            self.entryMoved.emit(i)

    @Slot("QStringList")
    def moveEntriesToTrash(self, ids: list) -> None:
        ids = _ids(ids)
        self.submit("Moving to trash", lambda sync, _: sync.trash(ids),
                    tr("ActionHeader", "%n item(s) were moved to Trash", len(ids)))

    @Slot("QStringList")
    def restoreEntriesFromTrash(self, ids: list) -> None:
        ids = _ids(ids)
        self.submit("Restoring", lambda sync, _: sync.restore(ids),
                    tr("ActionHeader", "%n item(s) were restored to My files", len(ids)))

    @Slot("QStringList")
    def deleteEntries(self, ids: list) -> None:
        ids = _ids(ids)
        self.submit("Deleting", lambda sync, _: sync.delete(ids), f"{len(ids)} item(s) deleted")

    @Slot()
    def deleteAllEntriesFromTrash(self) -> None:
        self.submit("Emptying trash", lambda sync, _: sync.empty_trash(), "Trash emptied")

    @Slot(str)
    def loadTableOfContents(self, document_id: str) -> None:
        pass

    @Slot(str, int)
    def setLastOpenedPage(self, document_id: str, page: int) -> None:
        pass

    @Slot(str, int)
    def setOrientation(self, document_id: str, orientation: int) -> None:
        self._unsupported("setOrientation")

    @Slot(str, int)
    def setCoverPageNumber(self, document_id: str, page: int) -> None:
        self._unsupported("setCoverPageNumber")

    @Slot(str, str)
    def unlockWithPassword(self, document_id: str, password: str) -> None:
        self._unsupported("unlockWithPassword")

    @Slot(str, int, float)
    def setScrollPosition(self, document_id: str, page: int, position: float) -> None:
        pass

    @Slot(str, int)
    def setZoomMode(self, document_id: str, mode: int) -> None:
        pass

    @Slot(str, float, QPointF, QSizeF)
    def setCustomZoomFit(self, document_id: str, scale: float, center: QPointF, page_size: QSizeF) -> None:
        pass

    @Slot(str, str)
    def setVisibleName(self, entry_id: str, name: str) -> None:
        self.submit("Renaming", lambda sync, _: sync.rename(entry_id, name), f"Renamed to {name}")

    @Slot(str, bool)
    def setPinned(self, entry_id: str, pinned: bool) -> None:
        self.submit("Favorite" if pinned else "Unfavorite", lambda sync, _: sync.set_pinned([entry_id], pinned),
                    "Added to Favorites" if pinned else "Removed from Favorites")

    @Slot("QStringList")
    def togglePinned(self, ids: list) -> None:
        ids = _ids(ids)
        nodes = [self.library.tree.get(i) for i in ids]
        pin = not all(n.pinned for n in nodes if n is not None)
        self.submit("Favorite" if pin else "Unfavorite", lambda sync, _: sync.set_pinned(ids, pin),
                    "Added to Favorites" if pin else "Removed from Favorites")

    @Slot(str)
    def clearIsNew(self, entry_id: str) -> None:
        pass

    @Slot(str, "QStringList", "QStringList")
    def setEntryTags(self, entry_id: str, tags: list, keep_tags: list) -> None:
        self._unsupported("setEntryTags")

    @Slot(str, "QVariantList", "QStringList", "QStringList")
    def setPageTags(self, entry_id: str, pages: list, tags: list, keep_tags: list) -> None:
        self._unsupported("setPageTags")

    @Slot(str, int, result="QStringList")
    def tagsForPage(self, entry_id: str, page: int) -> list:
        entry = self.library.entryForId(entry_id)
        return entry.tagsForPage(page) if isinstance(entry, DocumentWrapper) else []


class ProgressCounts(dict):
    """xofm::libs::syncinterface::ProgressCounts as the QML reads it."""

    def __init__(self, transferred: int = 0, scheduled: int = 0, progress: float = 0.0, state: str = "") -> None:
        super().__init__(transferred=transferred, scheduled=scheduled, progress=progress, state=state)


class SyncChecker(QObject):
    """DocumentSync.syncChecker (CheckSyncDialog): this client checks by syncing."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._device = True

    error = Property(bool, lambda self: False, notify=changed)
    running = Property(bool, lambda self: False, notify=changed)
    status = Property("QVariantMap", lambda self: {"text": "", "detail": "", "icon": ""}, notify=changed)

    def _set_device(self, value: bool) -> None:
        self._device = bool(value)

    onDevice = Property(bool, lambda self: self._device, _set_device, notify=changed)

    @Slot()
    def startCheck(self) -> None:
        pass


class DocumentSync(QObject):
    """DocumentSync (xofm::libs::syncinterface::DocumentSyncInterface): sync state from
    this client's Syncer."""

    syncStatusChanged = Signal(int, str)
    syncProtocolChanged = Signal()
    progressCountsChanged = Signal("QVariantMap")
    subscriptionTierChanged = Signal()
    protocolFlagChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._syncing = False
        self._failure = False
        self._counts = ProgressCounts()
        self._checker = SyncChecker(self)

    def set_state(self, syncing: bool, failure: bool = False) -> None:
        self._syncing, self._failure = syncing, failure
        self._counts = ProgressCounts(state="syncing" if syncing else ("failed" if failure else "idle"),
                                      progress=0.0 if syncing else 1.0)
        self.syncStatusChanged.emit(1 if syncing else (2 if failure else 0), "")
        self.progressCountsChanged.emit(dict(self._counts))

    isSyncing = Property(bool, lambda self: self._syncing, notify=syncStatusChanged)
    hasService = Property(bool, lambda self: True, notify=syncStatusChanged)
    hasFailure = Property(bool, lambda self: self._failure, notify=syncStatusChanged)
    canArchive = Property(bool, lambda self: False, notify=subscriptionTierChanged)
    hasLimitedSync = Property(bool, lambda self: False, notify=subscriptionTierChanged)
    progressCounts = Property("QVariantMap", lambda self: dict(self._counts), notify=progressCountsChanged)
    syncChecker = Property(QObject, lambda self: self._checker, constant=True)

    @Slot(int, "qlonglong")
    def requestProtocol(self, flag: int, account_created_at: int) -> None:
        pass

    @Slot()
    def suspend(self) -> None:
        pass

    @Slot()
    def resume(self) -> None:
        pass

    @Slot(bool)
    def onDiskSpaceAvailableChangedSignificantly(self, available: bool) -> None:
        pass

    @Slot()
    def onNewClientRegistration(self) -> None:
        pass

    @Slot()
    def onConnectedNotification(self) -> None:
        pass

    @Slot()
    def onUpdateNotification(self) -> None:
        pass

    @Slot("QVariantMap")
    def onSyncProgressChanged(self, stats: dict) -> None:
        pass


class PageSelection(QObject):
    """PageSelection: selected page numbers (0-based) of one document. notePages are the
    selected pages a PDF/EPUB does not have (inserted notebook pages)."""

    selectionChanged = Signal()
    documentIdChanged = Signal()
    countChanged = Signal()
    selectionHasTextChanged = Signal()
    movePagesActionCancelled = Signal()
    libraryChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library: Library | None = None
        self._document = ""
        self._pages: list[int] = []
        self._single = False
        self._unique = False

    def _node(self) -> DocumentNode | None:
        node = self._library.tree.get(self._document) if self._library is not None else None
        return node if isinstance(node, DocumentNode) else None

    def _live_pages(self) -> list:
        node = self._node()
        return [p for p in node.content.pages if not p.deleted] if node is not None else []

    def _set_pages(self, pages: list[int]) -> None:
        pages = [int(p) for p in pages]
        if self._unique:
            pages = list(dict.fromkeys(pages))
        if self._single:
            pages = pages[-1:]
        if pages != self._pages:
            count = len(self._pages)
            self._pages = pages
            self.selectionChanged.emit()
            if len(pages) != count:
                self.countChanged.emit()

    def _set_library(self, library: QObject | None) -> None:
        if library is not self._library:
            self._library = library
            self.libraryChanged.emit()

    def _set_document(self, document_id: str) -> None:
        document_id = str(document_id or "")
        if document_id != self._document:
            self._document = document_id
            self.documentIdChanged.emit()
            self._set_pages([])

    def _set_single(self, value: bool) -> None:
        self._single = bool(value)
        self._set_pages(self._pages)

    def _set_unique(self, value: bool) -> None:
        self._unique = bool(value)
        self._set_pages(self._pages)

    def _note_pages(self) -> list[int]:
        pages = self._live_pages()
        node = self._node()
        if node is None or FILE_TYPES.get(node.file_type) not in (2, 3):
            return list(self._pages)
        return [p for p in self._pages if 0 <= p < len(pages) and pages[p].redirect is None]

    pages = Property("QVariantList", lambda self: list(self._pages), lambda self, v: self._set_pages(v),
                     notify=selectionChanged)
    notePages = Property("QVariantList", _note_pages, notify=selectionChanged)
    pageIds = Property("QStringList", lambda self: [self._live_pages()[p].id for p in self._pages
                                                    if 0 <= p < len(self._live_pages())],
                       lambda self, ids: self.setPageIds(ids), notify=selectionChanged)
    library = Property(QObject, lambda self: self._library, _set_library, notify=libraryChanged)
    documentId = Property(str, lambda self: self._document, _set_document, notify=documentIdChanged)
    containsOriginalPages = Property(bool, lambda self: len(self._note_pages()) < len(self._pages),
                                     notify=selectionChanged)
    notePageCount = Property(bool, lambda self: bool(self._note_pages()), notify=selectionChanged)
    count = Property(int, lambda self: len(self._pages), notify=countChanged)
    isEmpty = Property(bool, lambda self: not self._pages, notify=selectionChanged)
    singleSelection = Property(bool, lambda self: self._single, _set_single, notify=selectionChanged)
    removeDuplicates = Property(bool, lambda self: self._unique, _set_unique, notify=selectionChanged)
    selectionHasText = Property(bool, lambda self: False, notify=selectionHasTextChanged)

    @Slot("QVariantList")
    def setPageNumbers(self, pages: list) -> None:
        self._set_pages(pages)

    @Slot("QStringList")
    def setPageIds(self, ids: list) -> None:
        index = {p.id: i for i, p in enumerate(self._live_pages())}
        self._set_pages([index[i] for i in ids if i in index])

    @Slot(int)
    def add(self, page: int) -> None:
        if int(page) not in self._pages:
            self._set_pages(self._pages + [int(page)])

    @Slot(int)
    def remove(self, page: int) -> None:
        self._set_pages([p for p in self._pages if p != int(page)])

    @Slot(int)
    def toggle(self, page: int) -> None:
        self.remove(page) if int(page) in self._pages else self.add(page)

    @Slot()
    def clear(self) -> None:
        self._set_pages([])

    @Slot(int, int)
    def addRange(self, first: int, last: int) -> None:
        lo, hi = sorted((int(first), int(last)))
        self._set_pages(self._pages + [p for p in range(lo, hi + 1) if p not in self._pages])

    @Slot(result=str)
    def groupedSelectionString(self) -> str:
        """1-based pages, consecutive runs joined: "1-3, 5"."""
        runs: list[list[int]] = []
        for page in sorted(set(self._pages)):
            if runs and page == runs[-1][-1] + 1:
                runs[-1].append(page)
            else:
                runs.append([page])
        return ", ".join(f"{r[0] + 1}-{r[-1] + 1}" if len(r) > 1 else str(r[0] + 1) for r in runs)

    @Slot(int, result=bool)
    def isSelected(self, page: int) -> bool:
        return int(page) in self._pages

    @Slot()
    def cancelMovePagesAction(self) -> None:
        self.movePagesActionCancelled.emit()
