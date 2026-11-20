"""A table of contents panel for PDFs and EPUBs (client-only).

reMarkable.exe 3.29's desktop QML has no table of contents panel: nothing in it calls
`LibraryController.loadTableOfContents`, though the app answers the call (its
DocumentWorker LoadTableOfContents job -> `QmlDocumentWrapper.tableOfContentsUpdated`
with `tocRunning` around it; ARCHITECTURE.md §5.7). This panel is injected beside the
official DocumentView without changing its QML, as the pen toolbar (bridge/pen.py) and
Settings > Editing (ui/outgoing.py) are: a "Contents" button at the top right of the
document's view container, shown when the open document has a table of contents, and a
panel along the right edge listing its entries (indented by level, the chapter being
read marked); a click on an entry turns to its page as the app turns pages
(`DocumentViewport.setCurrentPage`). Ctrl+Shift+T or the button opens and closes it,
Escape closes it. Entries the backend could not place on a page (page -1) are listed
but not clickable.

The list is asked for once per document opened (and again when the document's page
list changes: inserted or moved pages move the entries), on the backend's worker."""

from __future__ import annotations

import logging

import shiboken6
from PySide6.QtCore import Property, QByteArray, QMetaObject, QObject, Q_ARG, QUrl, Signal, Slot
from PySide6.QtQml import QQmlComponent, QQmlContext, qmlEngine
from PySide6.QtQuick import QQuickItem

log = logging.getLogger(__name__)

_controller: QObject | None = None          # the app's LibraryController (QuickApp sets it)


def set_controller(controller: QObject | None) -> None:
    global _controller
    _controller = controller


class TocModel(QObject):
    """What the panel shows: the entries ({name, page, level, path}), whether they are
    being loaded, the page being read (bound from DocumentViewport.currentPage) and the
    entry holding it, and whether the panel is open. It follows one document at a time
    (show): its tableOfContentsUpdated, tocRunning and page list."""

    chaptersChanged = Signal()
    loadingChanged = Signal()
    currentChanged = Signal()
    pageChanged = Signal()
    openChanged = Signal()
    jumped = Signal(int)                    # the page turned to

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._chapters: list[dict] = []
        self._loading = False
        self._page = 0
        self._open = False
        self.viewport: QQuickItem | None = None
        self.document = None                # the DocumentWrapper followed
        self.doc_id: str | None = None
        self.layout: tuple = ()

    # -- properties ---------------------------------------------------------------
    def _get_chapters(self) -> list:
        return list(self._chapters)

    chapters = Property("QVariantList", _get_chapters, notify=chaptersChanged)
    available = Property(bool, lambda self: bool(self._chapters), notify=chaptersChanged)
    loading = Property(bool, lambda self: self._loading, notify=loadingChanged)

    def _get_open(self) -> bool:
        return self._open

    def _set_open(self, value: bool) -> None:
        value = bool(value) and bool(self._chapters)
        if value != self._open:
            self._open = value
            self.openChanged.emit()

    open = Property(bool, _get_open, _set_open, notify=openChanged)

    def _get_page(self) -> int:
        return self._page

    def _set_page(self, page) -> None:
        page = int(page) if isinstance(page, (int, float)) else 0
        if page != self._page:
            self._page = page
            self.pageChanged.emit()
            self.currentChanged.emit()

    page = Property(int, _get_page, _set_page, notify=pageChanged)

    def _current(self) -> int:
        """The entry holding the page being read: the last one placed at or before it
        (of several on that page, the last listed)."""
        best = -1
        for row, chapter in enumerate(self._chapters):
            page = int(chapter.get("page", -1))
            if 0 <= page <= self._page and (best < 0 or page >= int(self._chapters[best]["page"])):
                best = row
        return best

    current = Property(int, _current, notify=currentChanged)

    # -- the backend --------------------------------------------------------------
    @Slot("QVariantList")
    def set_chapters(self, chapters) -> None:
        self._chapters = [dict(c) for c in (chapters or []) if hasattr(c, "keys")]
        self.chaptersChanged.emit()
        self.currentChanged.emit()
        if not self._chapters:
            self._set_open(False)

    @Slot()
    def running_changed(self) -> None:
        document = self.document
        loading = bool(document.property("tocRunning")) if document is not None and shiboken6.isValid(document) else False
        if loading != self._loading:
            self._loading = loading
            self.loadingChanged.emit()

    @Slot()
    def pages_changed(self) -> None:
        """The document's page list changed (pages inserted, moved, deleted): its
        entries are placed again."""
        if self.document is not None and shiboken6.isValid(self.document):
            self.show(self.document)

    def show(self, document) -> None:
        """Follow `document` (a DocumentWrapper): its table of contents is asked for when
        it is not the one followed, or when its page list changed; a notebook has none."""
        from core.models import FileType
        from .library import DocumentWrapper
        if not isinstance(document, DocumentWrapper):
            return
        node = document.node
        has_toc = node.file_type in (FileType.PDF, FileType.EPUB)
        layout = tuple(p.pdf_page for p in document._pages()) if has_toc else ()
        if document is self.document and node.id == self.doc_id and layout == self.layout:
            return
        old = self.document
        if old is not document:
            if old is not None and shiboken6.isValid(old):
                for signal, slot in ((old.tableOfContentsUpdated, self.set_chapters),
                                     (old.tocRunningChanged, self.running_changed),
                                     (old.pageMapChanged, self.pages_changed)):
                    try:
                        signal.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass
            document.tableOfContentsUpdated.connect(self.set_chapters)
            document.tocRunningChanged.connect(self.running_changed)
            document.pageMapChanged.connect(self.pages_changed)
            self.document = document
        new_document = node.id != self.doc_id
        self.doc_id, self.layout = node.id, layout
        if new_document:
            self.set_chapters([])
        if has_toc and _controller is not None and shiboken6.isValid(_controller):
            _controller.loadTableOfContents(node.id)

    @Slot()
    def toggle(self) -> None:
        self._set_open(not self._open)

    @Slot(int)
    def jump(self, row: int) -> None:
        """Turn to entry `row`'s page, as the app turns pages (DocumentViewport.setCurrentPage)."""
        if not 0 <= row < len(self._chapters):
            return
        page = int(self._chapters[row].get("page", -1))
        viewport = self.viewport
        if page < 0 or viewport is None or not shiboken6.isValid(viewport):
            return
        QMetaObject.invokeMethod(viewport, "setCurrentPage", Q_ARG("QVariant", page))
        self._set_page(page)
        self.jumped.emit(page)


PANEL_QML = b"""
import QtQuick
import QtQuick.Controls
import ark.controls as ArkControls

Item {
    id: toc
    objectName: "tocSidebar"
    anchors.fill: parent
    z: 55
    visible: tocModel.available

    readonly property color frame: "#d9d4cb"
    readonly property color picked: "#e6e1d8"
    readonly property color hover: "#f2efe9"

    // The page being read, from the official viewport (its currentPage property).
    Binding {
        target: tocModel
        property: "page"
        value: viewport ? viewport.currentPage : 0
    }

    Shortcut {
        sequence: "Ctrl+Shift+T"
        enabled: tocModel.available
        onActivated: tocModel.toggle()
    }
    Shortcut {
        sequence: "Escape"
        enabled: tocModel.open
        onActivated: tocModel.open = false
    }

    Rectangle {
        id: button
        objectName: "tocButton"
        x: toc.width - width - 16 - (tocModel.open ? panel.width : 0)
        y: 16
        width: 40; height: 40; radius: 6
        color: tocModel.open ? toc.picked : (buttonHover.hovered ? toc.hover : "#ffffff")
        border.color: toc.frame
        ArkControls.Icon {
            anchors.centerIn: parent
            source: "qrc:/ark/icons/list"
            size: 24
            color: "#000000"
        }
        TapHandler {
            gesturePolicy: TapHandler.ReleaseWithinBounds
            onTapped: tocModel.toggle()
        }
        HoverHandler { id: buttonHover }
        ToolTip.visible: buttonHover.hovered
        ToolTip.delay: 600
        ToolTip.text: qsTr("Contents")
    }

    Rectangle {
        id: panel
        objectName: "tocPanel"
        visible: tocModel.open
        width: Math.min(360, toc.width * 0.45)
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        color: "#ffffff"
        border.color: toc.frame

        // Clicks and wheel turns on the panel stay on it (not on the page below).
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.AllButtons
            onWheel: (wheel) => { wheel.accepted = true; }
        }

        Text {
            id: title
            text: qsTr("Contents")
            font.pixelSize: 20
            font.weight: Font.Medium
            x: 16; y: 20
            color: "#000000"
        }

        ListView {
            id: list
            objectName: "tocList"
            anchors.top: title.bottom
            anchors.topMargin: 12
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: 1
            clip: true
            model: tocModel.chapters
            currentIndex: tocModel.current
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                objectName: "tocEntry_" + index
                readonly property bool placed: modelData.page >= 0
                width: ListView.view.width
                height: 40
                color: index === tocModel.current ? toc.picked : (entryHover.hovered && placed ? toc.hover : "transparent")
                Text {
                    objectName: "tocName"
                    anchors.verticalCenter: parent.verticalCenter
                    x: 16 + 20 * Math.max(0, modelData.level || 0)
                    width: parent.width - x - pageText.width - 24
                    text: modelData.name
                    elide: Text.ElideRight
                    font.pixelSize: 16
                    font.weight: (modelData.level || 0) === 0 ? Font.Medium : Font.Normal
                    color: placed ? "#000000" : "#8a8a8a"
                }
                Text {
                    id: pageText
                    objectName: "tocPage"
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.right: parent.right
                    anchors.rightMargin: 16
                    text: placed ? String(modelData.page + 1) : ""
                    font.pixelSize: 14
                    color: "#5c5c5c"
                }
                TapHandler {
                    enabled: placed
                    gesturePolicy: TapHandler.ReleaseWithinBounds
                    onTapped: tocModel.jump(index)
                }
                HoverHandler { id: entryHover }
            }
        }
    }
}
"""


class _Toc(QObject):
    """The panel of one DocumentViewport: its model, QML context and item (children of
    the viewport's QObject, so they go with it)."""

    def __init__(self, viewport: QQuickItem) -> None:
        super().__init__(viewport)
        self.model = TocModel(self)
        self.model.viewport = viewport
        self.context: QQmlContext | None = None
        self.panel: QQuickItem | None = None


def toc_of(viewport: QQuickItem | None) -> _Toc | None:
    return viewport.findChild(_Toc) if viewport is not None and shiboken6.isValid(viewport) else None


def attach(view: QQuickItem) -> _Toc | None:
    """The contents panel for the DocumentViewport that `view` (a SceneView) shows a page
    in, made the first time; then pointed at the view's document."""
    viewport = view.parentItem()
    if viewport is None or viewport.metaObject().indexOfProperty("cursorActive") < 0:
        return None
    found = toc_of(viewport)
    if found is None:
        found = _Toc(viewport)
        engine = qmlEngine(view)
        container = viewport.parentItem()
        if engine is not None and container is not None:
            found.context = QQmlContext(engine.rootContext(), found)
            found.context.setContextProperty("tocModel", found.model)
            found.context.setContextProperty("viewport", viewport)
            component = QQmlComponent(engine, found)
            component.setData(QByteArray(PANEL_QML), QUrl("qrc:/linux/TocSidebar.qml"))
            panel = component.create(found.context)
            if panel is None:
                log.error("contents panel: %s", "; ".join(e.toString() for e in component.errors()))
            else:
                panel.setParent(found)
                panel.setParentItem(container)
                found.panel = panel
    found.model.show(view._get("document"))
    return found
