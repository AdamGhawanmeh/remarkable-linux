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
Typed text: in text mode (SceneView.textModeEnabled) the page's text is a
ui.textedit.TextSession held by the view: keys become the app's actions (SceneView's
keyHandler; QML hands each back through SceneController.keyAction), the page is drawn
again with the text as it is now, and the view draws the cursor and the selection, as
the app's view does. What is typed is kept as pending edits of the library in batches
(LibrarySource.add_text_edits): after a pause, before an undo or redo, and when text
mode ends or the page changes.

Editing so far is images on notebook pages, where the library allows editing
(LibrarySource.editable): an image file dropped on a page
(SceneController.insertImageFileAsSceneItem), and an image selected
(selectImageAtPoint) then moved, resized, turned or deleted through the app's own
SceneSelectionHandler (moveSelectedItems, scaleSelectedItems, rotateSelectedItems,
deleteSelectedItems), with SelectionImage drawing the selected image while it is
dragged. The other editing members (pens, text, conversion, copying) exist so the QML
binds, and do nothing yet (generated.py).

Scene coordinates are the app's: tablet pixels at 226 DPI, x = 0 on the page's vertical
centre line, y = 0 at its top (ui/pages.py uses x = 0 at the left edge). The tile
manager's view coordinates are the SceneView item's.
"""

from __future__ import annotations

import logging
import time
from enum import IntEnum
from typing import Callable

import shiboken6

from PySide6.QtCore import (QAbstractListModel, QCoreApplication, QModelIndex, QObject, QPointF, QRect, QRectF, QSize, QSizeF, Qt,
                            QThreadPool, QTimer, QUrl, Signal, Slot)
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtQuick import QQuickItem, QQuickPaintedItem

from core.edits import EditError
from core.models import FileType
from viewer.rm_parser import PAGE_HEIGHT, PAGE_WIDTH
from viewer.rm_parser import ParagraphStyle as StyleType
from viewer import rm_text
from viewer.renderer import qt_measure

from ... import editing
from ... import textedit
from ...editing import prepare_image
from ...library import pdf_name
from ...pages import DocumentPages, PageContent, render_content
from ...workers import EXPECTED_ERRORS, _Job, emit_from_worker
from .generated import qml_class

log = logging.getLogger("rm.ui.quick.scene")

HALF_WIDTH = PAGE_WIDTH / 2
TEXT_KEEP_MS = 1000         # what was typed is kept as pending edits after this long a pause
CARET_WIDTH = 3.0           # scene pixels


def _now_ms() -> int:
    return int(time.time() * 1000)


def _changed_end(before: str, after: str, cursor: int) -> int:
    """Where the cursor goes after an undo or redo: after what it put back, or where
    what it took away was (the cursor where it was when the text is the same)."""
    if before == after:
        return min(cursor, rm_text.units(after))
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while end < min(len(before), len(after)) - start and before[-1 - end] == after[-1 - end]:
        end += 1
    return rm_text.units(after[:len(after) - end])
PORTRAIT = QRectF(-HALF_WIDTH, 0, PAGE_WIDTH, PAGE_HEIGHT)
# Largest page image kept per zoom (pixels); deeper zooms upscale it.
MAX_PIXELS = 32_000_000

_source: Callable[[], object] = lambda: None
# Tells the user why an edit was not made (the app shows it as a notification).
_notify: Callable[[str], None] = lambda message: log.warning("%s", message)

# The transform (center, scale) each DocumentViewport last showed a page at, by viewport
# item. DocumentViewport turns a page by changing currentPage, which re-fits viewportScale
# onto the outgoing SceneView, and then creating a new one; it applies the zoom to the new
# view only when viewportScale changes again, which it never does between two notebook
# pages (same paper size). So a page's new view starts at its predecessor's transform.
_viewport_transforms: dict[int, tuple[QPointF, float]] = {}


def _viewport_key(item: QQuickItem) -> int | None:
    parent = item.parentItem()
    return shiboken6.getCppPointer(parent)[0] if parent is not None and shiboken6.isValid(parent) else None


# The worker pools of SceneViews that are gone, kept until their last job has finished.
# A view's pool is not its child: destroying a QThreadPool waits for its jobs, and a view
# destroyed while Python holds the GIL (from a Python call into Qt, e.g. a test's key
# click) would wait for a job that needs the GIL to finish: a deadlock. The jobs of a
# gone view emit nothing (emit_from_worker).
_retired_pools: list[QThreadPool] = []
_retire_timer: QTimer | None = None


def _view_pool() -> QThreadPool:
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    return pool


def _retire_pool(pool: QThreadPool) -> None:
    global _retire_timer
    _retired_pools.append(pool)
    if QCoreApplication.instance() is None:         # shutting down: they go with the process
        return
    if _retire_timer is None or not shiboken6.isValid(_retire_timer):
        _retire_timer = QTimer(singleShot=False, interval=250)
        _retire_timer.timeout.connect(_drop_idle_pools)
    if not _retire_timer.isActive():
        _retire_timer.start()


def _drop_idle_pools() -> None:
    _retired_pools[:] = [p for p in _retired_pools if p.activeThreadCount() > 0]
    if not _retired_pools and _retire_timer is not None:
        _retire_timer.stop()


def set_page_source(provider: Callable[[], object]) -> None:
    """Where SceneView and the page models load documents from (the app's LibrarySource)."""
    global _source
    _source = provider


_clipboard = None                       # the app's Clipboard singleton (pictures pasted from other programs)


def set_clipboard(clipboard) -> None:
    global _clipboard
    _clipboard = clipboard


def _js(value):
    """A value QML handed over (QJSValue or plain) as Python data."""
    to_variant = getattr(value, "toVariant", None)
    return to_variant() if callable(to_variant) else value


def set_edit_notifier(notify: Callable[[str], None]) -> None:
    global _notify
    _notify = notify


def forget_history(doc_id: str) -> None:
    """A document was closed: its pages' undo and redo go with it."""
    source = _source()
    if source is not None and hasattr(source, "forget_history"):
        source.forget_history(doc_id)


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
class _ParagraphStyle:
    """A paragraph's style as QML gets it (SceneController.textParagraphStyle): the
    app's scene::ParagraphStyle, here over the style as stored."""

    stored: tuple | None = None

    @classmethod
    def of(cls_, stored: tuple | None, parent: QObject) -> QObject:
        value = ParagraphStyle(parent)
        value.stored = stored
        return value

    def _held(self) -> tuple[int, int]:
        return textedit.held(self.stored)

    @Slot(int, result=bool)
    def matchesButtonStyle(self, button: int) -> bool:
        """TextFormattingUtils: a list button matches its list kind at any indent; a
        text style button, only that style."""
        mine = textedit.held(self.stored)
        theirs = textedit.held(rm_text.stored_style(StyleType(int(button))))
        if textedit.is_list(*theirs):
            return textedit.is_list(*mine) and mine[1] >> 4 & 0xF == theirs[1] >> 4 & 0xF
        return textedit.type_of(self.stored) == StyleType(int(button))

    @Slot(result=bool)
    def isListItem(self) -> bool:
        return textedit.is_list(*self._held())

    @Slot(result=bool)
    def isCheckbox(self) -> bool:
        base, properties = self._held()
        return textedit.is_list(base, properties) and properties >> 4 & 0xF == 1

    @Slot(result=bool)
    def isBulletpoint(self) -> bool:
        base, properties = self._held()
        return textedit.is_list(base, properties) and properties >> 4 & 0xF == 0

    @Slot(result=bool)
    def isNumberedList(self) -> bool:
        base, properties = self._held()
        return textedit.is_list(base, properties) and properties >> 4 & 0xF == 2

    @Slot(result=bool)
    def isText(self) -> bool:
        return self._held()[0] == 2

    @Slot(result=int)
    def indentLevel(self) -> int:
        base, properties = self._held()
        return properties & 0xF if textedit.is_list(base, properties) else 0

    @Slot(result=bool)
    def isChecked(self) -> bool:
        return self.isCheckbox() and self._held()[1] >> 8 & 7 == 1


ParagraphStyle = qml_class("scene::ParagraphStyle", name="ParagraphStyle", behaviour=_ParagraphStyle)
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


# Search highlights: nothing to show without search.
PageSearchResultOverlayItem = qml_class("PageSearchResultOverlayItem", QQuickPaintedItem, _PaintedNothing)


class _SelectionImage:
    """What SceneSelectionHandler's SelectionPreview shows inside the selection box: the
    selected image, fitted to the box, so it follows the box while it is dragged or
    resized (the page is drawn without it meanwhile)."""

    def __init__(self, parent: QQuickItem | None = None) -> None:
        QQuickPaintedItem.__init__(self, parent)
        self._picture: QImage | None = None
        self._matrix = None                 # image pixels -> scene
        self._rect = QRectF()               # the scene area the box stands for

    @Slot(QObject, int, int, QRectF, QTransform)
    def updateImage(self, worker, page: int, layer: int, scene_rect: QRectF, preview: QTransform) -> None:
        self._picture, self._matrix, self._rect = None, None, QRectF(scene_rect)
        selected = worker.selected_image() if hasattr(worker, "selected_image") else None
        if selected is not None:
            self._picture, self._matrix = selected
        self.update()

    @Slot()
    def reset(self) -> None:
        self._picture, self._matrix = None, None
        self.update()

    @Slot(result=QImage)
    def image(self) -> QImage:
        return self._picture if self._picture is not None else QImage()

    def paint(self, painter: QPainter) -> None:
        if self._picture is None or self._matrix is None or self._rect.width() <= 0 or self._rect.height() <= 0:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(self.width() / self._rect.width(), self.height() / self._rect.height())
        painter.translate(-self._rect.x(), -self._rect.y())
        painter.setTransform(QTransform(*self._matrix), True)
        painter.drawImage(QPointF(0, 0), self._picture)


SelectionImage = qml_class("SelectionImage", QQuickPaintedItem, _SelectionImage)
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
        # Set by the SceneView showing the page: (file url, scene position) -> None.
        self.insert_image: Callable[[QUrl, QPointF], None] | None = None
        self.view = None                    # that SceneView: selecting and changing images
        self.last_area: tuple[float, float] | None = None   # corner of the area last selected

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

    @Slot(QUrl, QPointF)
    def insertImageFileAsSceneItem(self, url: QUrl, pos: QPointF) -> None:
        """An image file dropped on the page (DocumentViewport's drop area), centred on
        the scene position `pos`."""
        if self.insert_image is not None:
            self.insert_image(QUrl(url), QPointF(pos))

    # -- selection (DocumentViewport, SceneSelectionHandler) --
    @Slot(QPointF, int, result=bool)
    def selectImageAtPoint(self, pos: QPointF, layer: int) -> bool:
        """Select the topmost image of the layer at a scene position: the selection box
        opens on it (areaSelected). False where there is none, or the page may not be
        edited here."""
        return self.view is not None and self.view.select_image_at(QPointF(pos), int(layer))

    @Slot()
    def clearSelectedItems(self) -> None:
        if self.view is not None:
            self.view.clear_selection()

    @Slot(int, QPointF)
    def moveSelectedItems(self, layer: int, offset: QPointF) -> None:
        if self.view is not None:
            self.view.transform_selection(int(layer), lambda v: editing.moved(v, offset.x(), offset.y()))

    @Slot(int, QPointF, float, float)
    def scaleSelectedItems(self, layer: int, origin: QPointF, x_scale: float, y_scale: float) -> None:
        if self.view is not None:
            self.view.transform_selection(int(layer), lambda v: editing.scaled(v, origin.x(), origin.y(), x_scale, y_scale))

    @Slot(int, QPointF, float)
    def rotateSelectedItems(self, layer: int, origin: QPointF, rotation: float) -> None:
        if self.view is not None:
            self.view.transform_selection(int(layer), lambda v: editing.rotated(v, origin.x(), origin.y(), rotation))

    @Slot(int)
    def deleteSelectedItems(self, layer: int) -> None:
        if self.view is not None:
            self.view.delete_selection(int(layer))

    # -- the clipboard (DocumentViewport's copy, cut and paste actions) --
    @Slot(int, float, result="QVariantList")
    def cloneSelectedItems(self, layer: int, scale_factor: float) -> list:
        """Clones of the selected items for Clipboard.items, scaled by `scale_factor`
        (0x14043a320). Cut is this, then deleteSelectedItems."""
        return self.view.copy_selection(int(layer), float(scale_factor)) if self.view is not None else []

    @Slot("QVariant", result=QRectF)
    def getItemBoundingRect(self, items) -> QRectF:
        bounds = editing.items_bounds(_js(items))
        return QRectF(*bounds) if bounds else QRectF()

    @Slot("QVariant", QRectF, QPointF, result=QPointF)
    def getSelectionPastePos(self, items, scene_bounds: QRectF, pos: QPointF) -> QPointF:
        b = QRectF(scene_bounds)
        x, y = editing.paste_position(_js(items), (b.x(), b.y(), b.width(), b.height()), (pos.x(), pos.y()),
                                      self.last_area)
        return QPointF(x, y)

    @Slot(int, "QVariant", float, QPointF)
    def cloneAddAndSelectItems(self, layer: int, items, scale_factor: float, pos: QPointF) -> None:
        if self.view is not None:
            self.view.paste_items(int(layer), _js(items), float(scale_factor), QPointF(pos))

    @Slot("QVariant", result=bool)
    def shouldPasteClipboardContentInTextMode(self, content) -> bool:
        """Text (or HTML) is pasted as text; a picture copied in another program, which
        then has neither, floats on the page."""
        content = _js(content) or {}
        return bool(content.get("text") or content.get("html")) if hasattr(content, "get") else False

    @Slot("QVariant", QPointF)
    def pasteClipboardContentAsFloatingSceneImage(self, content, pos: QPointF) -> None:
        if self.view is not None and _clipboard is not None:
            self.view.paste_picture(_clipboard.image_of(_js(content)), QPointF(pos))

    @Slot("QVariant")
    @Slot("QVariant", int)
    @Slot("QVariant", int, int)
    def pasteClipboardContent(self, content, mode: int = 0, merge: int = 0) -> None:
        """Text pasted in text mode, typed at the cursor (as plain text)."""
        content = _js(content) or {}
        text = content.get("text") if hasattr(content, "get") else None
        if not text or self.view is None or self.view.text_session is None:
            _notify("Only plain text can be pasted into typed text on Linux yet")
            return
        self.view.text_do(lambda t: t.paste(str(text)))

    @Slot(result="QVariantList")
    @Slot(QRect, result="QVariantList")
    def getLineBoundingRectsToBeSelected(self, area: QRect | None = None) -> list:
        """The outlines DocumentViewport draws around each selected stroke: none, as
        strokes cannot be selected yet (an image's outline is the selection box)."""
        return []

    # -- typed text (DocumentViewport in text mode) --
    @Slot()
    @Slot("QVariant")
    @Slot("QVariant", float)
    def createRootDocument(self, style=None, width: float | None = None) -> None:
        """Text mode starts on a page: its text box is made when it has none (its first
        paragraph `style`, ParagraphStyle.Type), and the view starts typing on it."""
        if self.view is not None:
            style = _js(style)
            self.view.start_text(StyleType(int(style)) if isinstance(style, (int, float)) else StyleType.TITLE)

    @Slot()
    @Slot(QPointF)
    def focusRootDocument(self, pos: QPointF | None = None) -> None:
        if self.view is not None:
            self.view.start_text(StyleType.TITLE, None if pos is None else QPointF(pos))

    @Slot("QVariant")
    @Slot("QVariant", int)
    def keyAction(self, action, flags: int = 0) -> None:
        if self.view is not None:
            self.view.text_key(_js(action) or {})

    @Slot(bool, bool)
    def setCursorVisible(self, visible: bool, update: bool) -> None:
        if self._set("isTextCursorVisible", bool(visible)) and self.view is not None:
            self.view.update()

    @Slot(QPointF)
    @Slot(QPointF, int)
    @Slot(QPointF, int, int)
    def setCursorPosition(self, pos: QPointF, mode: int = 0, overscroll: int = 0) -> None:
        if self.view is not None:
            self.view.text_place(QPointF(pos), int(mode))

    @Slot(int)
    @Slot(int, int)
    def setCursorIndex(self, index: int, mode: int = 0) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.place(int(index), int(mode) == textedit.KEEP_ANCHOR), edits=False)

    @Slot(int, int)
    def moveCursor(self, operation: int, mode: int) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.move(int(operation), int(mode)), edits=False)

    @Slot("QVariant")
    def cycleParagraphStyle(self, style) -> None:
        style = _js(style)
        stored = getattr(style, "stored", None)
        kind = textedit.type_of(stored) if hasattr(style, "stored") else StyleType(int(style))
        if self.view is not None:
            self.view.text_do(lambda t: t.cycle_style(kind))

    @Slot("QVariant")
    @Slot("QVariant", int)
    def setParagraphStyle(self, style, indent: int = 0) -> None:
        style = _js(style)
        kind = textedit.type_of(style.stored) if hasattr(style, "stored") else StyleType(int(style))
        if self.view is not None:
            self.view.text_do(lambda t: t.set_style(kind))

    @Slot(int)
    def setTextStyle(self, style: int) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.toggle_style(int(style)))

    @Slot()
    def indentParagraph(self) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.indent(1))

    @Slot()
    def unindentParagraph(self) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.indent(-1))

    @Slot(int)
    def selectText(self, mode: int) -> None:
        """SceneParticipant::SelectionMode: 1 word, 2 token, 3 line, 4 block, 5 all."""
        actions = {1: "select_word", 2: "select_word", 3: "select_paragraph", 4: "select_paragraph", 5: "select_all"}
        if self.view is not None and int(mode) in actions:
            self.view.text_do(lambda t: getattr(t, actions[int(mode)])(), edits=False)

    @Slot(int, int)
    def selectTextRange(self, start: int, end: int) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.select_range(int(start), int(end)), edits=False)

    @Slot()
    def clearSelectedText(self) -> None:
        if self.view is not None:
            self.view.text_do(lambda t: t.place(t.cursor), edits=False)

    @Slot()
    def copySelectedText(self) -> None:
        text = self.view.text_session.selected_text() if self.view is not None and self.view.text_session else ""
        if text:
            self.textCopied.emit(text)

    @Slot()
    def cutSelectedText(self) -> None:
        if self.view is None or self.view.text_session is None:
            return
        cut = []
        self.view.text_do(lambda t: cut.append(t.cut_selection()))
        if cut and cut[0]:
            self.textCopied.emit(cut[0])

    @Slot()
    def markKeyboardUsedDuringSession(self) -> None:
        pass

    # -- undo and redo (DocumentViewport's undo and redo actions) --
    @Slot()
    def undo(self) -> None:
        if self.view is not None:
            self.view.history_step(redo=False)

    @Slot()
    def redo(self) -> None:
        if self.view is not None:
            self.view.history_step(redo=True)

    @Slot(int)
    def applyPendingEdit(self, layer: int) -> None:
        """Each move is saved when it ends: nothing is left pending."""

    @Slot()
    def cancelPendingEdit(self) -> None:
        pass

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
    _edited = Signal(object)
    _text_opened = Signal(object)
    _text_kept = Signal(object)

    def __init__(self, parent: QQuickItem | None = None) -> None:
        QQuickPaintedItem.__init__(self, parent)
        self.setAntialiasing(True)
        self.__dict__.setdefault("_values", {})
        self._controller = SceneController(self)
        self._controller.insert_image = self._insert_image
        self._controller.view = self
        self._values["worker"] = self       # SelectionImage asks it for the selected image
        self._selected: bytes | None = None     # images-table key of the selected image
        self._reselect = False              # open the selection box again once the page reloads
        self._tiles = SceneTileManager(self)
        self._values["controller"] = self._controller
        self._values["tileManager"] = self._tiles
        self._values["viewport"] = self
        self._values["keyHandler"] = SceneKeyHandler(self)
        self._pages: DocumentPages | None = None
        self._content: PageContent | None = None
        self._page_id = ""                  # of the page shown (its id in the document)
        self._bounds = QRectF()             # content bounds, scene coordinates
        self._image: QImage | None = None   # the page rendered at self._image_scale
        self._image_scale = 0.0
        self._rendering = False
        self._render_again = False
        self._pool = _view_pool()
        self.destroyed.connect(lambda *_, pool=self._pool: _retire_pool(pool))
        self._refresh = QTimer(self, singleShot=True)
        self._refresh.timeout.connect(self._render)
        self._loaded.connect(self._on_loaded)
        self._rendered.connect(self._on_rendered)
        self._edited.connect(self._on_edited)
        self._tiles.transformChanged.connect(self._transform_changed)
        self._device_ratio = 1.0
        self.windowChanged.connect(self._window_changed)
        # Typing (textModeEnabled): the page's text while it is typed on, what to do
        # once it is open, and the timer that keeps what was typed after a pause.
        self.text_session: textedit.TextSession | None = None
        self._text_opening = False
        self._text_then: list = []
        self._text_keep = QTimer(self, singleShot=True, interval=TEXT_KEEP_MS)
        self._text_keep.timeout.connect(self._keep_text)
        self._text_opened.connect(self._on_text_opened)
        self._text_kept.connect(self._on_text_kept)
        self.setFlag(QQuickItem.ItemAcceptsInputMethod, False)

    # -- loading --
    def componentComplete(self) -> None:
        QQuickPaintedItem.componentComplete(self)
        inherited = _viewport_transforms.get(_viewport_key(self))
        if inherited is not None:
            self._tiles.setFocalPoint(*inherited)
        self._load()

    def _property_changed(self, name: str) -> None:
        if name in ("pageId", "document") and self.isComponentComplete():
            self._set_selected(None)
            self.stop_text()
            self._load()
        elif name == "textModeEnabled" and not self._get("textModeEnabled"):
            self.stop_text()

    def _load(self, quiet: bool = False) -> None:
        """Load the page `pageId` (the first page if the id is empty or unknown: the
        document's page count may only be known once its PDF is open, see
        DocumentWrapper.learn_pdf_page_count). Always ends Active or with a page error,
        never left Updating: DocumentViewport shows its loading dots until then.
        `quiet` reloads a page already shown (after an edit) without those dots."""
        document, page_id, source = self._get("document"), self._get("pageId"), _source()
        node = getattr(document, "node", None)
        if node is None or source is None:
            return
        if not quiet:
            self._set("status", 1)  # Updating

        def job():
            pages = None
            try:
                pages = DocumentPages(source, node)
                if not pages.pages:
                    raise ValueError(no_pages_reason(node))
                ids = [p.page_id for p in pages.pages]
                index = ids.index(page_id) if page_id in ids else 0
                content = pages.content(index)
                frame = content.frame()
                bounds = QRectF(*content.bounds("page"))
                links = []
                if content.has_pdf_page:
                    page_of_pdf = {p.pdf_page: i for i, p in enumerate(pages.pages) if p.pdf_page is not None}
                    links = pdf_links(content.pdf, content.pdf_page, frame, page_of_pdf)
                result = (pages, content, to_scene(frame), to_scene(bounds), links, None, ids[index])
            except Exception as err:      # anything left unreported would keep the loading dots up
                if not isinstance(err, EXPECTED_ERRORS):
                    log.exception("loading page %r of %s", page_id, node.id)
                    err = RuntimeError(f"{type(err).__name__}: {err}")
                if pages is not None:
                    pages.close()
                result = (None, None, None, None, [], err, "")
            emit_from_worker(self, lambda: self._loaded.emit(result))
        self._pool.start(_Job(job))

    def _on_loaded(self, result) -> None:
        pages, content, frame, bounds, links, error, shown = result
        if error is not None:
            log.warning("cannot load page %s: %s", self._get("pageId"), error)
            self._set("pageStatus", PAGE_LOAD_ERROR)
            self._set("pageStatusDetails", str(error))
            self._set("status", 2)
            return
        if self._pages is not None:
            self._pages.close()
        self._pages, self._content, self._bounds, self._page_id = pages, content, bounds, shown
        layers = content.scene.layers if content.scene is not None else []
        self._controller.set_page(frame, bounds, layers, links)
        # The layer edits go to: Layer 1, the one the app uses until another is chosen.
        node_ids = [layer.node_id for layer in layers]
        self._controller._set("currentLayer", node_ids.index((0, 11)) if (0, 11) in node_ids else max(len(layers) - 1, 0))
        self._tiles._set("sceneBoundingRect", bounds)
        self._update_viewport()
        self._set("defaultCenter", QPointF(0, frame.height() / 2))
        self._set("sceneExteriorBoundary", bounds)
        self._update_history()
        self._set("pageStatus", PAGE_READY)
        self._set("awaitingInitialTileSet", False)
        self._set("status", 2)  # Active
        if self._reselect:
            self._reselect = False
            self._reopen_selection()
        self._render()
        # Last: when this gives the document its page count, the viewport reopens the
        # page through its new id, replacing this view.
        learn = getattr(self._get("document"), "learn_pdf_page_count", None)
        if learn is not None and pages.pdf is not None:
            learn(pages.pdf.page_count)

    # -- editing --
    def _insertion_target(self, what: str):
        """(source, document, page id) where an image may be put now, or None after
        telling the user why not."""
        source, node = _source(), getattr(self._get("document"), "node", None)
        if source is None or node is None or self._content is None or not self._page_id:
            return None
        refusal = source.edit_refusal(node)
        if not refusal and self._content.has_pdf_page:
            refusal = f"{what} can only be put on notebook pages"
        if refusal:
            _notify(refusal)
            return None
        return source, node, self._page_id

    def _run_insertion(self, page_id: str, insert) -> None:
        """Run `insert()` (making the pending edits; it returns the key of the image to
        select, or None) on the view's worker, then draw the page again with that image
        selected, as the app selects what it adds (addAndSelect, 0x1409af310)."""
        def job():
            error = select = None
            try:
                select = insert()
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:      # an edit never takes the page view down with it
                log.exception("putting an image on page %s", page_id)
                error = f"The image could not be inserted: {type(err).__name__}"
            emit_from_worker(self, lambda: self._edited.emit((error, select)))
        self._pool.start(_Job(job))

    def _insert_image(self, url: QUrl, pos: QPointF) -> None:
        """Put an image file on the page shown, centred on a scene position
        (insertImageFileAsSceneItem): prepared as the app stores one (ui/editing.py) and
        kept as a pending edit of the library (LibrarySource.insert_image)."""
        target = self._insertion_target("Images")
        if target is None:
            return
        if not url.isLocalFile():
            _notify("Only image files on this computer can be inserted")
            return
        source, node, page_id = target
        path, x, y, now_ms = url.toLocalFile(), pos.x(), pos.y(), int(time.time() * 1000)

        def insert():
            data, extension, vertices = prepare_image(path, x, y)
            return bytes.fromhex(source.insert_image(node, page_id, data, extension, vertices, now_ms).key)
        self._run_insertion(page_id, insert)

    def paste_picture(self, picture: QImage, pos: QPointF) -> None:
        """A picture from the system clipboard, centred on a scene position
        (pasteClipboardContentAsFloatingSceneImage, 0x1404346b0): stored and placed as a
        dropped file is (createPersistedSceneImageItem, 0x14043c3c0)."""
        target = self._insertion_target("Images")
        if target is None or picture.isNull():
            return
        source, node, page_id = target
        x, y, now_ms = pos.x(), pos.y(), int(time.time() * 1000)

        def insert():
            data, extension, size = editing.encode_image(picture)
            corners = editing.corners(x, y, *editing.placed_size(size.width(), size.height()))
            return bytes.fromhex(source.insert_image(node, page_id, data, extension, corners, now_ms).key)
        self._run_insertion(page_id, insert)

    def paste_items(self, layer: int, items, scale: float, pos: QPointF) -> None:
        """Copied items pasted, centred on a scene position (cloneAddAndSelectItems,
        0x14043a060): each image is loaded from where it was copied and stored again as
        a new file under a new key (0x1409b1a90 -> 0x14094c420), and its copy keeps the
        copied item's timestamps; one whose image cannot be loaded is dropped, as the
        app drops it."""
        images = editing.clip_images(items)
        target = self._insertion_target("Images") if images else None
        if target is None:
            return
        source, node, page_id = target
        center, now_ms = (pos.x(), pos.y()), int(time.time() * 1000)

        def insert():
            select = None
            with source.edit_action(node, page_id) as action:      # undone at once, as the app's add-items
                for item in images:
                    select = paste_one(item, action) or select
            return select

        def paste_one(item, action) -> bytes | None:
            try:
                picture = QImage.fromData(source.image_file(item["documentId"], item["pageId"], item["fileName"]))
            except EditError as err:
                log.warning("dropping pasted image item, failed to load source image ref: %s", err)
                return None
            if picture.isNull():
                log.warning("dropping pasted image item, failed to load source image ref")
                return None
            data, extension, _ = editing.encode_image(picture)
            corners = editing.pasted_corners(item, scale, center)
            edit = source.insert_image(node, page_id, data, extension, corners, now_ms, item["timestamps"], action)
            return bytes.fromhex(edit.key)
        self._run_insertion(page_id, insert)

    def _on_edited(self, result) -> None:
        """An edit was made (or refused): draw the page again, with the image it
        concerns selected (`select`: its key; the image just dropped, as the app selects
        it), or without a selection."""
        error, select = result
        if error is not None:
            _notify(str(error))
        if select is not None:
            self._selected, self._reselect = select, True
        self._load(quiet=True)

    # -- the selection --
    def _editable(self) -> bool:
        source, node = _source(), getattr(self._get("document"), "node", None)
        return (source is not None and node is not None and self._content is not None
                and not self._content.has_pdf_page and source.can_edit(node))

    def _set_selected(self, key: bytes | None) -> None:
        changed = key != self._selected
        self._selected = key
        self._controller._set("selectionContainsImage", key is not None)
        self._controller._set("selectionItemCount", 1 if key is not None else 0)
        if changed and self._content is not None:
            self._render()

    def select_image_at(self, pos: QPointF, layer: int) -> bool:
        if not self._editable():
            return False
        image = editing.image_at(self._content.scene, layer, pos.x(), pos.y())
        if image is None:
            return False
        self._set_selected(image.image_ref)
        # After the QML that asked has switched to selecting (it does so once this
        # returns), as the app answers from its worker.
        QTimer.singleShot(0, self._reopen_selection)
        return True

    def _reopen_selection(self) -> None:
        """Open the selection box on the selected image where it is now, or close it
        when the image is gone."""
        found = editing.find_image(self._content.scene if self._content else None, self._selected) \
            if self._selected is not None else None
        if found is None:
            if self._selected is not None:
                self._set_selected(None)
                self._controller.selectionCleared.emit()
            return
        layer, image = found
        self._set_selected(image.image_ref)
        x, y, w, h = editing.corner_bounds(image.vertices)
        self._controller.last_area = (x, y)
        self._controller.areaSelected.emit(layer, QRectF(x, y, w, h))

    def clear_selection(self) -> None:
        self._set_selected(None)

    def selected_image(self):
        """(picture, image pixels -> scene matrix) of the selected image, for SelectionImage."""
        from viewer.renderer import image_matrix
        found = editing.find_image(self._content.scene if self._content else None, self._selected) \
            if self._selected is not None else None
        if found is None or not found[1].data:
            return None
        image = found[1]
        picture = getattr(image, "_qimage", None) or QImage.fromData(image.data)
        image._qimage = picture
        matrix = image_matrix(self._content.scene, image, picture.width(), picture.height())
        if picture.isNull() or matrix is None:
            return None
        a, b, c, d, e, f = matrix                   # to page coordinates (x 0 at the left edge)...
        return picture, (a, b, c, d, e - self._content.scene.x_origin, f)       # ...to scene coordinates

    def _selection_change(self, layer: int):
        """(source, document, page id, the selected image) for an edit of the selection,
        or None (nothing selected, another layer, or not editable)."""
        if self._selected is None or not self._editable():
            return None
        found = editing.find_image(self._content.scene, self._selected)
        if found is None or found[0] != layer:
            return None
        return _source(), self._get("document").node, self._page_id, found[1]

    def transform_selection(self, layer: int, change) -> None:
        """Move, scale or turn the selected image (`change`: its corners -> new corners),
        saved as a pending edit of the library, then the page is drawn again with the
        selection box on the image's new place."""
        target = self._selection_change(layer)
        if target is None:
            return
        source, node, page_id, image = target
        key, corners, now_ms = image.image_ref, change(list(image.vertices)), int(time.time() * 1000)

        def job():
            error = None
            try:
                source.transform_image(node, page_id, key, corners, now_ms)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("changing an image on page %s", page_id)
                error = f"The image could not be changed: {type(err).__name__}"
            emit_from_worker(self, lambda: self._edited.emit((error, key)))
        self._pool.start(_Job(job))

    def copy_selection(self, layer: int, scale: float) -> list:
        """The selected image as a clipboard item (cloneSelectedItems)."""
        if self._selected is None or self._content is None:
            return []
        found = editing.find_image(self._content.scene, self._selected)
        if found is None or found[0] != layer:
            return []
        return [editing.clip_image(self._get("document").node.id, self._page_id, found[1], scale)]

    def delete_selection(self, layer: int) -> None:
        target = self._selection_change(layer)
        if target is None:
            return
        source, node, page_id, image = target
        key, now_ms = image.image_ref, int(time.time() * 1000)
        self._set_selected(None)
        self._controller.selectionCleared.emit()

        def job():
            error = None
            try:
                source.delete_image(node, page_id, key, now_ms)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("deleting an image on page %s", page_id)
                error = f"The image could not be deleted: {type(err).__name__}"
            emit_from_worker(self, lambda: self._edited.emit((error, None)))
        self._pool.start(_Job(job))

    # -- undo and redo --
    def history_step(self, redo: bool) -> None:
        """Undo or redo the last edit of the page shown (SceneController::undo/redo,
        which run the page's SceneActionStack on the scene's worker): made as new
        pending edits (LibrarySource.undo/redo), then the page is drawn again with
        nothing selected (DocumentViewport ends the selection before it undoes)."""
        source, node = _source(), getattr(self._get("document"), "node", None)
        if source is None or node is None or not self._page_id or not hasattr(source, "undo"):
            return
        if self.text_session is not None:
            self._keep_text(then=lambda: self._text_history(redo))
            return
        (self._controller.aboutToRedo if redo else self._controller.aboutToUndo).emit()
        if self._selected is not None:
            self._set_selected(None)
            self._controller.selectionCleared.emit()
        page_id, now_ms = self._page_id, int(time.time() * 1000)

        def job():
            error = None
            try:
                (source.redo if redo else source.undo)(node, page_id, now_ms)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("%s on page %s", "redo" if redo else "undo", page_id)
                error = f"Could not {'redo' if redo else 'undo'}: {type(err).__name__}"
            emit_from_worker(self, lambda: self._edited.emit((error, None)))
        self._pool.start(_Job(job))

    def _update_history(self) -> None:
        """undoAvailable / redoAvailable of the page shown."""
        source, node = _source(), getattr(self._get("document"), "node", None)
        state = (False, False)
        if source is not None and node is not None and self._page_id and hasattr(source, "history_state"):
            state = source.history_state(node, self._page_id)
        if self.text_session is not None and self.text_session.steps:
            state = (True, False)
        self._controller._set("undoAvailable", state[0])
        self._controller._set("redoAvailable", state[1])

    # -- typed text --
    def _text_target(self):
        """(source, document node, page id) when text may be typed on the page shown;
        else None, after telling the user why not."""
        source, node = _source(), getattr(self._get("document"), "node", None)
        if source is None or node is None or self._content is None or not self._page_id:
            return None
        refusal = source.edit_refusal(node)
        if not refusal and self._content.has_pdf_page:
            refusal = "Text can only be typed on notebook pages"
        if refusal:
            _notify(refusal)
            return None
        return source, node, self._page_id

    def start_text(self, style: StyleType, pos: QPointF | None = None) -> None:
        """Text mode on the page shown (createRootDocument, focusRootDocument): its text
        is read with the pending edits, on the view's worker, then typed on here."""
        if pos is not None:
            self._text_then.append(lambda t: t.place(t.index_at(pos.x(), pos.y())))
        if self.text_session is not None or self._text_opening:
            return
        target = self._text_target()
        if target is None:
            return
        source, node, page_id = target
        scene, now = self._content.scene if self._content is not None else None, _now_ms
        self._text_opening = True
        self._controller._set("keyActionRunning", True)

        def job():
            try:
                done = source.text_page(node, page_id)
                session = textedit.TextSession.open(node.id, page_id, done.page, done, source.sync.author(), scene,
                                                    qt_measure, now, style)
                result = (session, None)
            except (EditError, *EXPECTED_ERRORS) as err:
                result = (None, str(err))
            except Exception as err:      # typing never takes the page view down with it
                log.exception("opening the text of page %s", page_id)
                result = (None, f"The text cannot be typed on: {type(err).__name__}")
            emit_from_worker(self, lambda: self._text_opened.emit(result))
        self._pool.start(_Job(job))

    def _on_text_opened(self, result) -> None:
        session, error = result
        self._text_opening = False
        self._controller._set("keyActionRunning", False)
        if error is not None:
            _notify(error)
            return
        if not self._get("textModeEnabled") and not self._text_then:
            # Text mode ended while the page's text was read: still keep the box made.
            self.text_session = session
            self.stop_text()
            return
        self.text_session = session
        then, self._text_then = self._text_then, []
        for step in then:
            step(session)
        if session.edits:
            self._text_keep.start()
        self._text_changed()

    def text_do(self, change, edits: bool = True) -> None:
        """Do `change(session)` on the text being typed, then show it."""
        session = self.text_session
        if session is None:
            if self._text_opening:
                self._text_then.append(change)
            return
        try:
            change(session)
        except rm_text.TextError as err:
            log.warning("text change not made: %s", err)
        if edits and session.edits:
            self._text_keep.start()
        self._text_changed()

    def text_key(self, action: dict) -> None:
        """A key action QML hands back (SceneController::keyAction)."""
        kind = int(action.get("type", 0))
        if kind == textedit.UNDO:
            self.history_step(redo=False)
        elif kind == textedit.REDO:
            self.history_step(redo=True)
        elif kind == textedit.COPY:
            self._controller.copySelectedText()
        elif kind == textedit.CUT:
            self._controller.cutSelectedText()
        elif kind in (textedit.PASTE, textedit.PASTE_AND_MATCH_STYLE):
            if _clipboard is not None:
                self._controller.pasteClipboardContent({"text": _clipboard.textString()})
        elif kind != textedit.NONE:
            self.text_do(lambda t: t.key(action))

    def text_place(self, pos: QPointF, mode: int) -> None:
        self.text_do(lambda t: t.place(t.index_at(pos.x(), pos.y()), mode == textedit.KEEP_ANCHOR), edits=False)

    def _text_changed(self) -> None:
        """The controller's text properties from the session, and the page drawn again."""
        c, session = self._controller, self.text_session
        if session is None:
            for name, value in (("textDocumentId", None), ("hasTextSelection", False), ("textCursorIndex", 0),
                                ("textCursorAnchorIndex", 0), ("isTextCursorVisible", False)):
                c._set(name, value)
            self.update()
            return
        caret, anchor = session.caret(), session.caret(session.anchor)
        c._set("hasRootDocument", True)
        c._set("textDocumentId", "0:0")
        c._set("textCursorIndex", session.cursor)
        c._set("textCursorAnchorIndex", session.anchor)
        c._set("textCursorPosition", QPointF(caret.x, caret.top))
        c._set("textCursorAnchorPosition", QPointF(anchor.x, anchor.top))
        c._set("textLineHeight", round(caret.height))
        c._set("textAnchorLineHeight", round(anchor.height))
        c._set("isTextCursorNewline", caret.empty_line)
        c._set("hasTextSelection", session.has_selection())
        c._set("textStyles", session.char_styles())
        c._set("textSelectionStyles", session.selection_styles())
        c._set("rootDocumentLength", session.text.length)
        stored = session.stored_style()
        old = c._get("textParagraphStyle")
        if getattr(old, "stored", object()) != stored:
            c._set("textParagraphStyle", ParagraphStyle.of(stored, c))
        c._set("textParagraphBounds", QRectF(*session.paragraph_bounds()))
        rects = session.selection_rects()
        if rects:
            xs, ys = [r[0] for r in rects] + [r[0] + r[2] for r in rects], [r[1] for r in rects] + [r[1] + r[3] for r in rects]
            c._set("textSelectionBounds", QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
        self._update_history()
        self.update()
        self._render()

    def _keep_text(self, then=None) -> None:
        """Hand what was typed to the library (LibrarySource.add_text_edits) on the
        view's worker, then `then()`."""
        self._text_keep.stop()
        session = self.text_session
        edits, steps = session.take() if session is not None else ([], [])
        source, node = _source(), getattr(self._get("document"), "node", None)
        if not edits or source is None or node is None:
            if then is not None:
                then()
            return
        page_id = session.page_id

        def job():
            error = None
            try:
                source.add_text_edits(node, page_id, edits, steps)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("keeping the text typed on page %s", page_id)
                error = f"The text could not be kept: {type(err).__name__}"
            emit_from_worker(self, lambda: self._text_kept.emit((error, then)))
        self._pool.start(_Job(job))

    def _on_text_kept(self, result) -> None:
        error, then = result
        if error is not None:
            # What was typed is not kept: show the page as the library has it.
            _notify(error)
            if self.text_session is not None:
                self._reopen_text()
            return
        self._update_history()
        if then is not None:
            then()

    def _text_history(self, redo: bool) -> None:
        """Undo or redo with the text kept, then type on the page as it is after it."""
        source, node = _source(), getattr(self._get("document"), "node", None)
        if source is None or node is None or self.text_session is None:
            return
        (self._controller.aboutToRedo if redo else self._controller.aboutToUndo).emit()
        page_id, now_ms = self.text_session.page_id, _now_ms()
        self._controller._set("keyActionRunning", True)

        def job():
            error = None
            try:
                (source.redo if redo else source.undo)(node, page_id, now_ms)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("%s on page %s", "redo" if redo else "undo", page_id)
                error = f"Could not {'redo' if redo else 'undo'}: {type(err).__name__}"
            emit_from_worker(self, lambda: self._text_kept.emit((error, self._reopen_text)))
        self._pool.start(_Job(job))

    def _reopen_text(self) -> None:
        """Read the page's text again (after an undo, or an edit not kept), the cursor
        where it was."""
        old = self.text_session
        self.text_session = None
        self._controller._set("keyActionRunning", False)
        if old is not None:
            at, before = old.cursor, old.plain()
            self._text_then.append(lambda t: t.place(_changed_end(before, t.plain(), at)))
        self.start_text(StyleType.TITLE)

    def stop_text(self) -> None:
        """Text mode ends (or the page changes): what was typed is kept, and the page
        is shown as the library has it."""
        if self.text_session is None:
            return
        self._keep_text(then=lambda: self._load(quiet=True))
        self.text_session = None
        self._text_then = []
        self._text_changed()

    # -- geometry --
    def geometryChange(self, new: QRectF, old: QRectF) -> None:
        QQuickPaintedItem.geometryChange(self, new, old)
        self._update_viewport()

    def _update_viewport(self) -> None:
        size = QSize(max(1, round(self.width())), max(1, round(self.height())))
        self._tiles._set("viewportSize", size)
        self._set("defaultScale", size.width() / PAGE_WIDTH)

    def _transform_changed(self) -> None:
        key = _viewport_key(self)
        if key is not None:
            _viewport_transforms[key] = (QPointF(self._tiles._get("center")), float(self._tiles._get("scale")))
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
        if self._content is None:
            return
        if self._rendering:
            # The render under way may be of the page as it was (reloaded after an
            # edit): render once more when it is done.
            self._render_again = True
            return
        scale = self._tiles._get("scale") * self._dpr()
        area = self._bounds.width() * self._bounds.height() * scale * scale
        if area > MAX_PIXELS:
            scale *= (MAX_PIXELS / area) ** 0.5
        content = self._content
        if self.text_session is not None:
            from dataclasses import replace
            content = replace(content, scene=self.text_session.shown_scene())
        if self._selected is not None and content.scene is not None:
            content = _without_image(content, self._selected)    # SelectionImage shows it
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
        if self._render_again:
            self._render_again = False
            self._render()
            return
        wanted = self._tiles._get("scale") * self._dpr()
        capped = self._bounds.width() * self._bounds.height() * wanted * wanted > MAX_PIXELS
        if not capped and abs(self._image_scale - wanted) / wanted > 0.01:
            self._render()

    def paint(self, painter: QPainter) -> None:
        if self._image is None:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(self._tiles.sceneToView(self._bounds), self._image)
        session = self.text_session
        if session is None or not self._get("textModeEnabled"):
            return
        for x, y, w, h in session.selection_rects():            # the selection, as the app's view shades it
            painter.fillRect(self._tiles.sceneToView(QRectF(x, y, w, h)), QColor(0, 0, 0, 56))
        if self._controller._get("isTextCursorVisible") and not session.has_selection():
            caret = session.caret()
            rect = self._tiles.sceneToView(QRectF(caret.x - CARET_WIDTH / 2, caret.top + caret.height * 0.12,
                                                  CARET_WIDTH, caret.height * 0.76))
            painter.fillRect(rect, QColor(0, 0, 0))

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
        """The SceneKeyHandlerAction for a key (SceneKeyHandler::process). Out of text
        mode only page up/down do something (the viewport scrolls), and Backspace
        deletes a selected image."""
        if self._get("textModeEnabled"):
            return textedit.key_action(int(key), int(modifiers), bool(auto_repeat), str(text or ""))
        actions = SceneKeyHandlerAction.Type
        kind = {int(Qt.Key.Key_PageUp.value): actions["PageUp"], int(Qt.Key.Key_PageDown.value): actions["PageDown"]}
        if int(key) in (int(Qt.Key.Key_Backspace.value), int(Qt.Key.Key_Delete.value)) and self._selected is not None:
            # Delete comes as the selection handler's shortcut; Backspace reaches here.
            self.delete_selection(self._controller._get("currentLayer"))
        return {"type": int(kind.get(int(key), actions["None"]))}

    def keyPressEvent(self, event) -> None:
        """In text mode the view has the keyboard, as the app's does: each key becomes
        an action (sceneKeyActionReceived), which QML hands back to keyAction."""
        if not self._get("textModeEnabled"):
            event.ignore()
            return
        action = textedit.key_action(int(event.key()), int(event.modifiers().value), event.isAutoRepeat(), event.text())
        if action["type"] == textedit.NONE:
            event.ignore()
            return
        event.accept()
        self.sceneKeyActionReceived.emit(action)

    @Slot(result=bool)
    def isNotePage(self) -> bool:
        return self._content is not None and not self._content.has_pdf_page


SceneView = qml_class("SceneView", QQuickPaintedItem, _SceneView)


def _without_image(content: PageContent, key: bytes) -> PageContent:
    """The page with the image `key` left out (it is being moved)."""
    from dataclasses import replace
    scene = content.scene
    layers = [replace(layer, images=[i for i in layer.images if i.image_ref != key]) for layer in scene.layers]
    return replace(content, scene=replace(scene, layers=layers))


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
