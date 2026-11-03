"""The app's scene types for its document view (DocumentView.qml, DocumentViewport.qml
and what they use), so those run unmodified.

In the app these wrap its native page engine: tiles rendered by a DocumentWorker, text
editing, handwriting conversion. Here:
    SceneView          draws the page with this client's renderer (ui/pages.py: PDF/EPUB
                       background, template, strokes, typed text) through the tile
                       manager's transform, re-rendering when the zoom settles
    SceneTileManager   the view <-> scene mapping, zoom and scrolling the QML drives
    SceneController    the page's paper and content geometry, and its layers
    PagePickerModel    the pages for the page overview grid
    SceneLayersModel   the page's layers for the layers foldout
The other members (editing, selection, conversion) exist so the QML binds, and do
nothing: this client does not edit pages (generated.py).

Scene coordinates are the app's: tablet pixels at 226 DPI, x = 0 on the page's vertical
centre line, y = 0 at its top (ui/pages.py uses x = 0 at the left edge). The tile
manager's view coordinates are the SceneView item's.
"""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import Callable

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, QPointF, QRect, QRectF, QSize, QSizeF, Qt,
                            QThreadPool, QTimer, Signal, Slot)
from PySide6.QtGui import QImage, QPainter, QTransform
from PySide6.QtQuick import QQuickItem, QQuickPaintedItem

from core.models import FileType
from viewer.rm_parser import PAGE_HEIGHT, PAGE_WIDTH

from ...library import pdf_name
from ...pages import DocumentPages, PageContent, render_content
from ...workers import EXPECTED_ERRORS, _Job, emit_from_worker
from .generated import qml_class

log = logging.getLogger("rm.ui.quick.scene")

HALF_WIDTH = PAGE_WIDTH / 2
PORTRAIT = QRectF(-HALF_WIDTH, 0, PAGE_WIDTH, PAGE_HEIGHT)
# Largest page image kept per zoom (pixels); deeper zooms upscale it.
MAX_PIXELS = 32_000_000

_source: Callable[[], object] = lambda: None


def set_page_source(provider: Callable[[], object]) -> None:
    """Where SceneView and the page models load documents from (the app's LibrarySource)."""
    global _source
    _source = provider


def to_scene(rect: QRectF) -> QRectF:
    """ui/pages.py page coordinates -> scene coordinates."""
    return rect.translated(-HALF_WIDTH, 0)


def no_pages_reason(node) -> str:
    """Why a document has nothing to show (PageErrorOverlay's diagnostic line)."""
    if node.file_type is FileType.EPUB and pdf_name(node) is None:
        # An EPUB uploaded from a computer: the tablet adds the PDF rendition this
        # client draws once it has opened the ebook.
        return "This ebook has no PDF rendition yet: open it once on your reMarkable, then sync"
    return "This document has no pages"


# -- enums and helpers the QML only names -------------------------------------------

Page = qml_class("Page")
ParagraphStyle = qml_class("scene::ParagraphStyle", name="ParagraphStyle")
SceneKeyHandlerAction = qml_class("SceneKeyHandlerAction")
SceneParticipant = qml_class("SceneParticipant")
SceneRenderContext = qml_class("SceneRenderContext")
SceneKeyHandler = qml_class("SceneKeyHandler", register=("com.remarkable", "SceneKeyHandler", "uncreatable"))
RecognitionJob = qml_class("RecognitionJob")
Recognizer = qml_class("Recognizer")
DocumentLockManager = qml_class("DocumentLockManager")


class _LinkHandler:
    """Follows a link found by SceneController.linkAt: a URL, or a page of the
    document (its target PDF page, mapped to the document's page)."""

    @Slot("QVariantMap")
    def activate(self, link: dict) -> None:
        if not link or not link.get("valid"):
            return
        if link.get("url"):
            self.openUrl.emit(link["url"])
        elif link.get("page", -1) >= 0:
            self.goToPage.emit(int(link["page"]))


SceneLinkHandler = qml_class("SceneLinkHandler", behaviour=_LinkHandler)


def pdf_links(pdf, pdf_page: int, frame: QRectF, page_of_pdf: dict[int, int]) -> list[dict]:
    """The links on a PDF page, in scene coordinates (QtPdf's link model)."""
    from PySide6.QtPdf import QPdfLinkModel
    model = QPdfLinkModel()
    model.setDocument(pdf.doc)
    model.setPage(pdf_page)
    roles = {bytes(v).decode(): k for k, v in model.roleNames().items()}
    scale = frame.width() / pdf.page_size(pdf_page).width()
    links = []
    for row in range(model.rowCount(QModelIndex())):
        index = model.index(row, 0)
        rect: QRectF = model.data(index, roles["rectangle"])
        url = model.data(index, roles["url"])
        target = model.data(index, roles["page"])
        area = QRectF(frame.x() + rect.x() * scale, frame.y() + rect.y() * scale, rect.width() * scale,
                      rect.height() * scale)
        links.append({"valid": True, "index": row, "rect": to_scene(area),
                      "url": url.toString() if url is not None and not url.isEmpty() else "",
                      "page": page_of_pdf.get(int(target), -1) if target is not None and target >= 0 else -1})
    return links
PAGE_NONE, PAGE_READY, PAGE_LOAD_ERROR = (Page.StatusCode[k].value for k in ("None", "Ready", "LoadError"))


class _PaintedNothing:
    def paint(self, painter: QPainter) -> None:
        pass


# Search highlights and selection previews: nothing to show without search/selection.
PageSearchResultOverlayItem = qml_class("PageSearchResultOverlayItem", QQuickPaintedItem, _PaintedNothing)
SelectionImage = qml_class("SelectionImage", QQuickPaintedItem, _PaintedNothing)
DocumentSearchController = qml_class("DocumentSearchController")


# -- tile manager ---------------------------------------------------------------------

class _TileManager:
    _DEFAULTS = {"scale": 1.0, "tileSize": QSize(1024, 1024), "preservingTiles": False}

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)

    def _view_half(self) -> QPointF:
        size = self._get("viewportSize")
        return QPointF(size.width() / 2, size.height() / 2)

    def _apply(self, center: QPointF, scale: float) -> None:
        """Set the transform, keeping the view inside sceneBoundingRect (the app limits
        scrolling to the paper): along an axis where the bounds are smaller than the
        view, the bounds are centred."""
        scale = max(scale, 1e-6)
        bounds: QRectF = self._get("sceneBoundingRect")
        half = self._view_half()
        cx, cy = center.x(), center.y()
        if bounds.isValid():
            hw, hh = half.x() / scale, half.y() / scale
            cx = bounds.center().x() if bounds.width() <= 2 * hw else min(max(cx, bounds.left() + hw),
                                                                           bounds.right() - hw)
            cy = bounds.center().y() if bounds.height() <= 2 * hh else min(max(cy, bounds.top() + hh),
                                                                            bounds.bottom() - hh)
        center = QPointF(cx, cy)
        if center == self._get("center") and scale == self._get("scale"):
            return
        self._values["center"], self._values["scale"] = center, scale
        to_view = QTransform().translate(half.x(), half.y()).scale(scale, scale).translate(-cx, -cy)
        self._values["sceneToViewTransform"] = to_view
        self._values["viewToSceneTransform"] = to_view.inverted()[0]
        self.transformChanged.emit()
        self.changed.emit(QRect(), 0)

    def _property_changed(self, name: str) -> None:
        if name in ("sceneBoundingRect", "viewportSize"):
            self._apply(self._get("center"), self._get("scale"))

    def to_view(self, p: QPointF) -> QPointF:
        c, s, half = self._get("center"), self._get("scale"), self._view_half()
        return QPointF((p.x() - c.x()) * s + half.x(), (p.y() - c.y()) * s + half.y())

    def to_scene(self, p: QPointF) -> QPointF:
        c, s, half = self._get("center"), self._get("scale"), self._view_half()
        return QPointF((p.x() - half.x()) / s + c.x(), (p.y() - half.y()) / s + c.y())

    @Slot(QPointF, float)
    def setFocalPoint(self, center: QPointF, scale: float) -> None:
        self._apply(QPointF(center), float(scale))

    @Slot(QPointF, float)
    def zoomOnPoint(self, point: QPointF, factor: float) -> None:
        """Zoom by `factor` keeping the scene point under `point` (view) in place."""
        anchor = self.to_scene(QPointF(point))
        scale = self._get("scale") * float(factor)
        half = self._view_half()
        self._apply(QPointF(anchor.x() - (point.x() - half.x()) / scale,
                            anchor.y() - (point.y() - half.y()) / scale), scale)

    @Slot(float)
    def zoom(self, factor: float) -> None:
        self.zoomOnPoint(self._view_half(), factor)

    @Slot(QPointF)
    def moveViewBy(self, offset: QPointF) -> None:
        """Move the content by `offset` view pixels (positive y: content moves down)."""
        c, s = self._get("center"), self._get("scale")
        self._apply(QPointF(c.x() - offset.x() / s, c.y() - offset.y() / s), s)

    @Slot()
    def reset(self) -> None:
        self._apply(self._get("center"), self._get("scale"))

    @Slot()
    def reload(self) -> None:
        self.changed.emit(QRect(), 0)

    @Slot()
    def requestMissingTiles(self) -> None:
        self.changed.emit(QRect(), 0)

    @Slot(QPointF, result=QPointF)
    @Slot(QRectF, result=QRectF)
    def viewToScene(self, value):
        if isinstance(value, QRectF):
            return QRectF(self.to_scene(value.topLeft()), self.to_scene(value.bottomRight()))
        return self.to_scene(QPointF(value))

    @Slot(QPointF, result=QPointF)
    @Slot(QRectF, result=QRectF)
    def sceneToView(self, value):
        if isinstance(value, QRectF):
            return QRectF(self.to_view(value.topLeft()), self.to_view(value.bottomRight()))
        return self.to_view(QPointF(value))

    @Slot(result=QRectF)
    def viewportRect(self) -> QRectF:
        size = self._get("viewportSize")
        return self.viewToScene(QRectF(0, 0, size.width(), size.height()))

    @Slot(QPointF)
    @Slot(QPointF, int)
    @Slot(QPointF, int, int)
    @Slot(QPointF, int, int, int)
    @Slot(QPointF, int, int, int, int)
    def scrollToMakeScenePositionVisible(self, pos: QPointF, left: int = 0, top: int = 0, right: int = 0,
                                         bottom: int = 0) -> None:
        self.scrollToMakeSceneRectangleVisible(QRectF(QPointF(pos), QSizeF(0, 0)), left, top, right, bottom)

    @Slot(QRectF)
    @Slot(QRectF, int)
    @Slot(QRectF, int, int)
    @Slot(QRectF, int, int, int)
    @Slot(QRectF, int, int, int, int)
    def scrollToMakeSceneRectangleVisible(self, rect: QRectF, left: int = 0, top: int = 0, right: int = 0,
                                          bottom: int = 0) -> None:
        view = self.sceneToView(QRectF(rect))
        size = self._get("viewportSize")
        dx = dy = 0.0
        if view.left() < left:
            dx = left - view.left()
        elif view.right() > size.width() - right:
            dx = size.width() - right - view.right()
        if view.top() < top:
            dy = top - view.top()
        elif view.bottom() > size.height() - bottom:
            dy = size.height() - bottom - view.bottom()
        if dx or dy:
            self.moveViewBy(QPointF(dx, dy))


SceneTileManager = qml_class("SceneTileManager", QObject, _TileManager,
                             register=("com.remarkable", "SceneTileManager", "uncreatable"))


# -- controller -----------------------------------------------------------------------

class _Controller:
    _DEFAULTS = {"defaultPortraitBounds": PORTRAIT, "paperPortraitBounds": PORTRAIT, "paperNoteBounds": PORTRAIT,
                 "boundingRect": PORTRAIT, "primaryContentRect": PORTRAIT,
                 "explicitPaperSize": QSize(PAGE_WIDTH, PAGE_HEIGHT)}

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        self.layers: list = []
        self.links: list[dict] = []

    def set_page(self, frame: QRectF, bounds: QRectF, layers: list, links: list[dict]) -> None:
        """The loaded page: its frame (paper), content bounds and links, in scene
        coordinates."""
        self.layers = layers
        self.links = links
        self._set("explicitPaperSize", QSize(round(frame.width()), round(frame.height())))
        self._set("primaryContentRect", frame)
        self._set("boundingRect", bounds)

    @Slot(QPointF, result="QVariantMap")
    def linkAt(self, pos: QPointF) -> dict:
        """The link under a scene position (DocumentViewport reads .valid and .index)."""
        for link in self.links:
            if link["rect"].contains(QPointF(pos)):
                return {k: v for k, v in link.items() if k != "rect"}
        return {"valid": False, "index": -1}

    @Slot(int, result=bool)
    def isLayerVisible(self, index: int) -> bool:
        return 0 <= index < len(self.layers) and self.layers[index].visible


SceneController = qml_class("SceneController", QObject, _Controller,
                            register=("com.remarkable", "SceneController", "uncreatable"))


# -- the page view --------------------------------------------------------------------

class _SceneView:
    """A page, drawn through the tile manager's transform."""

    # DocumentViewport sets tileImageFormat: SceneView.Format_RGB32 (QImage's formats).
    Format = IntEnum("Format", {f.name: f.value for f in QImage.Format if f.name.startswith("Format_")})

    _DEFAULTS = {"status": 0, "pageStatus": PAGE_NONE, "defaultScale": 1.0, "penScale": 1.0,
                 "limitScrollingToPaper": True, "tileAntialiasing": True}
    _loaded = Signal(object)
    _rendered = Signal(object)

    def __init__(self, parent: QQuickItem | None = None) -> None:
        QQuickPaintedItem.__init__(self, parent)
        self.setAntialiasing(True)
        self.__dict__.setdefault("_values", {})
        self._controller = SceneController(self)
        self._tiles = SceneTileManager(self)
        self._values["controller"] = self._controller
        self._values["tileManager"] = self._tiles
        self._values["viewport"] = self
        self._values["keyHandler"] = SceneKeyHandler(self)
        self._pages: DocumentPages | None = None
        self._content: PageContent | None = None
        self._bounds = QRectF()             # content bounds, scene coordinates
        self._image: QImage | None = None   # the page rendered at self._image_scale
        self._image_scale = 0.0
        self._rendering = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._refresh = QTimer(self, singleShot=True)
        self._refresh.timeout.connect(self._render)
        self._loaded.connect(self._on_loaded)
        self._rendered.connect(self._on_rendered)
        self._tiles.transformChanged.connect(self._transform_changed)
        self._device_ratio = 1.0
        self.windowChanged.connect(self._window_changed)

    # -- loading --
    def componentComplete(self) -> None:
        QQuickPaintedItem.componentComplete(self)
        self._load()

    def _property_changed(self, name: str) -> None:
        if name in ("pageId", "document") and self.isComponentComplete():
            self._load()

    def _load(self) -> None:
        """Load the page `pageId` (the first page if the id is empty or unknown: the
        document's page count may only be known once its PDF is open, see
        DocumentWrapper.learn_pdf_page_count). Always ends Active or with a page error,
        never left Updating: DocumentViewport shows its loading dots until then."""
        document, page_id, source = self._get("document"), self._get("pageId"), _source()
        node = getattr(document, "node", None)
        if node is None or source is None:
            return
        self._set("status", 1)  # Updating

        def job():
            pages = None
            try:
                pages = DocumentPages(source, node)
                if not pages.pages:
                    raise ValueError(no_pages_reason(node))
                ids = [p.page_id for p in pages.pages]
                content = pages.content(ids.index(page_id) if page_id in ids else 0)
                frame = content.frame()
                bounds = QRectF(*content.bounds("page"))
                links = []
                if content.has_pdf_page:
                    page_of_pdf = {p.pdf_page: i for i, p in enumerate(pages.pages) if p.pdf_page is not None}
                    links = pdf_links(content.pdf, content.pdf_page, frame, page_of_pdf)
                result = (pages, content, to_scene(frame), to_scene(bounds), links, None)
            except Exception as err:      # anything left unreported would keep the loading dots up
                if not isinstance(err, EXPECTED_ERRORS):
                    log.exception("loading page %r of %s", page_id, node.id)
                    err = RuntimeError(f"{type(err).__name__}: {err}")
                if pages is not None:
                    pages.close()
                result = (None, None, None, None, [], err)
            emit_from_worker(self, lambda: self._loaded.emit(result))
        self._pool.start(_Job(job))

    def _on_loaded(self, result) -> None:
        pages, content, frame, bounds, links, error = result
        if error is not None:
            log.warning("cannot load page %s: %s", self._get("pageId"), error)
            self._set("pageStatus", PAGE_LOAD_ERROR)
            self._set("pageStatusDetails", str(error))
            self._set("status", 2)
            return
        if self._pages is not None:
            self._pages.close()
        self._pages, self._content, self._bounds = pages, content, bounds
        layers = content.scene.layers if content.scene is not None else []
        self._controller.set_page(frame, bounds, layers, links)
        self._tiles._set("sceneBoundingRect", bounds)
        self._update_viewport()
        self._set("defaultCenter", QPointF(0, frame.height() / 2))
        self._set("sceneExteriorBoundary", bounds)
        self._set("pageStatus", PAGE_READY)
        self._set("awaitingInitialTileSet", False)
        self._set("status", 2)  # Active
        self._render()
        # Last: when this gives the document its page count, the viewport reopens the
        # page through its new id, replacing this view.
        learn = getattr(self._get("document"), "learn_pdf_page_count", None)
        if learn is not None and pages.pdf is not None:
            learn(pages.pdf.page_count)

    # -- geometry --
    def geometryChange(self, new: QRectF, old: QRectF) -> None:
        QQuickPaintedItem.geometryChange(self, new, old)
        self._update_viewport()

    def _update_viewport(self) -> None:
        size = QSize(max(1, round(self.width())), max(1, round(self.height())))
        self._tiles._set("viewportSize", size)
        self._set("defaultScale", size.width() / PAGE_WIDTH)

    def _transform_changed(self) -> None:
        self.update()
        wanted = self._tiles._get("scale") * self._dpr()
        if self._image is None or abs(self._image_scale - wanted) / wanted > 0.01:
            self._refresh.start(max(0, int(self._get("tileRefreshInterval") or 0)) or 120)

    def _window_changed(self, window) -> None:
        # Recorded here rather than read with self.window(): calling window() on a
        # SceneView that DocumentViewport is destroying (nukeView) took the app's
        # window down with it (PySide 6.10).
        self._device_ratio = window.effectiveDevicePixelRatio() if window is not None else 1.0

    def _dpr(self) -> float:
        return self._device_ratio

    # -- rendering --
    def _render(self) -> None:
        if self._content is None or self._rendering:
            return
        scale = self._tiles._get("scale") * self._dpr()
        area = self._bounds.width() * self._bounds.height() * scale * scale
        if area > MAX_PIXELS:
            scale *= (MAX_PIXELS / area) ** 0.5
        content = self._content
        self._rendering = True

        def job():
            try:
                image = render_content(content, scale, "page")
            except EXPECTED_ERRORS as err:
                log.warning("cannot render page: %s", err)
                image = None
            emit_from_worker(self, lambda: self._rendered.emit((image, scale)))
        self._pool.start(_Job(job))

    def _on_rendered(self, result) -> None:
        image, scale = result
        self._rendering = False
        if image is not None:
            self._image, self._image_scale = image, scale
            self.update()
        wanted = self._tiles._get("scale") * self._dpr()
        capped = self._bounds.width() * self._bounds.height() * wanted * wanted > MAX_PIXELS
        if not capped and abs(self._image_scale - wanted) / wanted > 0.01:
            self._render()

    def paint(self, painter: QPainter) -> None:
        if self._image is None:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(self._tiles.sceneToView(self._bounds), self._image)

    def releaseResources(self) -> None:
        self._pool.waitForDone(5000)
        if self._pages is not None:
            self._pages.close()
            self._pages = None

    @Slot(QPointF, float)
    def setFocalPoint(self, center: QPointF, scale: float) -> None:
        self._tiles.setFocalPoint(center, scale)

    @Slot()
    def setFocalPointToContent(self) -> None:
        self._tiles.setFocalPoint(self._bounds.center(), self._tiles._get("scale"))

    @Slot()
    def recenter(self) -> None:
        self.setFocalPointToContent()

    @Slot(int, int, bool, str, result="QVariantMap")
    def processKey(self, key: int, modifiers: int, auto_repeat: bool, text: str) -> dict:
        """The SceneKeyHandlerAction for a key: page up/down scroll (the viewport does
        that); without text editing, other keys do nothing."""
        actions = SceneKeyHandlerAction.Type
        kind = {int(Qt.Key.Key_PageUp.value): actions["PageUp"], int(Qt.Key.Key_PageDown.value): actions["PageDown"]}
        return {"type": int(kind.get(int(key), actions["None"]))}

    @Slot(result=bool)
    def isNotePage(self) -> bool:
        return self._content is not None and not self._content.has_pdf_page


SceneView = qml_class("SceneView", QQuickPaintedItem, _SceneView)


# -- models ---------------------------------------------------------------------------

class _PagePicker:
    """The document's pages, one row per page number (PageGridView's modelData)."""

    def __init__(self, parent: QObject | None = None) -> None:
        QAbstractListModel.__init__(self, parent)
        self._count = 0

    def _property_changed(self, name: str) -> None:
        if name in ("documentId", "library"):
            self.resetModel()

    @Slot()
    def resetModel(self) -> None:
        library, doc_id = self._get("library"), self._get("documentId")
        entry = library.entryForId(doc_id) if library is not None and doc_id else None
        self.beginResetModel()
        # The document's pageCount: also right for documents without a page list.
        self._count = int(entry.property("pageCount") or 0) if entry is not None and not entry.node.is_folder else 0
        self.endResetModel()
        self.modelDataChanged.emit()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self._count

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            return index.row()
        return None

    def roleNames(self) -> dict:
        return {Qt.ItemDataRole.DisplayRole: b"display"}


PagePickerModel = qml_class("PagePickerModel", QAbstractListModel, _PagePicker)


class _Layers:
    """The current page's layers (LayersFoldout's Repeater)."""

    ROLES = (b"layerIndex", b"layerName", b"layerVisible", b"layerSelected", b"layerMoveUpEnabled",
             b"layerMoveDownEnabled", b"layerMergeEnabled", b"layerTrashEnabled")

    def __init__(self, parent: QObject | None = None) -> None:
        QAbstractListModel.__init__(self, parent)

    def _property_changed(self, name: str) -> None:
        if name == "controller":
            controller = self._get("controller")
            if controller is not None:
                getattr(controller, controller._NOTIFY["boundingRect"]).connect(self._reset)
            self._reset()

    def _layers(self) -> list:
        controller = self._get("controller")
        return controller.layers if controller is not None else []

    def _reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()
        self._set("count", len(self._layers()))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._layers())

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        layers = self._layers()
        if not index.isValid() or not 0 <= index.row() < len(layers):
            return None
        layer, row = layers[index.row()], index.row()
        values = (row, layer.name, layer.visible, row == len(layers) - 1, False, False, False, False)
        offset = int(role) - Qt.ItemDataRole.UserRole.value
        return values[offset] if 0 <= offset < len(values) else None

    def roleNames(self) -> dict:
        return {Qt.ItemDataRole.UserRole.value + i: name for i, name in enumerate(self.ROLES)}


SceneLayersModel = qml_class("xofm::libs::sceneview::SceneLayersModel", QAbstractListModel, _Layers,
                             name="SceneLayersModel")
