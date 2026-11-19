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

Editing, where the library allows it (LibrarySource.editable), on notebook, PDF and
ebook pages alike (typed text: notebook pages only): an image file dropped on a page
(SceneController.insertImageFileAsSceneItem); items selected (selectImageAtPoint, a
rectangle or a lasso) then moved, resized, turned, cut, copied, pasted or deleted
through the app's own SceneSelectionHandler (moveSelectedItems, scaleSelectedItems,
rotateSelectedItems, deleteSelectedItems), with SelectionImage drawing the selection
while it is dragged; undo and redo. Pens and the eraser draw through bridge/pen.py,
which hands each finished stroke or eraser pass to the view (pen_commit). A PDF or
ebook page is the same scene as a notebook page, only wider or longer: the PDF page
fills x in [-W/2, W/2], y in [0, H] at 226/72 px per point (ui/pages.py), and nothing
is clipped to the 1404 x 1872 screen (ARCHITECTURE.md 5.4). Handwriting conversion
exists so the QML binds, and does nothing yet (generated.py).

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
from viewer import rm_select as rsel
from viewer import rm_text
from viewer.rm_stroke import rotation_of as s_rotation_of
from viewer.renderer import qt_measure

from ... import editing
from ... import selection as selecting
from ... import textedit
from ...editing import prepare_image
from ...library import pdf_name
from ...pages import DocumentPages, PageContent, render_content
from ...workers import EXPECTED_ERRORS, _Job, emit_from_worker
from .generated import qml_class
from . import pen as pen_bridge

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
    pen_bridge.set_source(provider)


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
    pen_bridge.set_source(pen_bridge._source, notify)


def forget_history(doc_id: str) -> None:
    """A document was closed: its pages' undo and redo go with it, and the pen in hand
    is put down (it is not in hand when the document is opened again)."""
    pen_bridge.document_closed(doc_id)
    source = _source()
    if source is not None and hasattr(source, "forget_history"):
        source.forget_history(doc_id)


def to_scene(rect: QRectF) -> QRectF:
    """ui/pages.py page coordinates -> scene coordinates."""
    return rect.translated(-HALF_WIDTH, 0)


def anchor_offsets(scene) -> dict:
    """Where a page draws the strokes of its groups anchored to typed text: stroke id
    -> anchor position (renderer.stroke_offsets, laid out with the app's fonts), {}
    when it has none."""
    if scene is None or not any(s.anchor is not None for s in scene.strokes):
        return {}
    from viewer.renderer import layout_text, stroke_offsets
    by_object = stroke_offsets(scene, layout_text(scene, qt_measure))
    return {tuple(s.item_id): by_object[id(s)] for s in scene.strokes
            if s.item_id is not None and id(s) in by_object}


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


CHECKBOX_URL = "rmcheckbox://index="


class _LinkHandler:
    """Follows a link found by SceneController.linkAt: a URL, or a page of the
    document (its target PDF page, mapped to the document's page)."""

    @Slot("QVariantMap")
    def activate(self, link: dict) -> None:
        if not link or not link.get("valid"):
            return
        url = str(link.get("url") or "")
        if url.startswith(CHECKBOX_URL):
            # A checkbox of the typed text (the app's link "rmcheckbox://index=<n>",
            # 0x140987930): toggled by DocumentView through toggleCheckboxByIndex(n).
            try:
                self.toggleCheckbox.emit(int(url[len(CHECKBOX_URL):]))
            except ValueError:
                pass
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
    selected strokes and images, drawn through the pending transform and fitted to the
    box, so they follow the box while it is dragged, resized or turned (the page is drawn
    without them meanwhile; the app draws them so too, 0x1409b1e40)."""

    def __init__(self, parent: QQuickItem | None = None) -> None:
        QQuickPaintedItem.__init__(self, parent)
        self._scene = None                  # (a page holding the selected items, its x origin)
        self._preview = QTransform()        # the pending transform
        self._rect = QRectF()               # the scene area the box stands for

    @Slot(QObject, int, int, QRectF, QTransform)
    def updateImage(self, worker, page: int, layer: int, scene_rect: QRectF, preview: QTransform) -> None:
        self._scene = worker.selection_preview() if hasattr(worker, "selection_preview") else None
        self._preview, self._rect = QTransform(preview), QRectF(scene_rect)
        self.update()

    @Slot()
    def reset(self) -> None:
        self._scene = None
        self.update()

    @Slot(result=QImage)
    def image(self) -> QImage:
        return QImage()

    def paint(self, painter: QPainter) -> None:
        if self._scene is None or self._rect.width() <= 0 or self._rect.height() <= 0:
            return
        from viewer.renderer import paint
        scene, x_origin = self._scene
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(self.width() / self._rect.width(), self.height() / self._rect.height())
        painter.translate(-self._rect.x(), -self._rect.y())
        painter.setTransform(self._preview, True)
        painter.translate(-x_origin, 0)         # the renderer draws in page coordinates
        paint(painter, scene)


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
                 "explicitPaperSize": QSize(PAGE_WIDTH, PAGE_HEIGHT), "backgroundVisible": True}

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
        if not self._set("boundingRect", bounds):
            # Unchanged, but the page was loaded again: DocumentViewport re-applies its
            # scroll bounds and LayersFoldout re-reads the layers on this signal.
            getattr(self, self._NOTIFY["boundingRect"]).emit()

    @Slot(bool)
    def setBackgroundVisible(self, visible: bool) -> None:
        """Hide template / Show template (and the Layers foldout's Template or
        Background row): the page is drawn again at once without, or with, its
        template or PDF page. The tablet keeps this per page in the .rm (SceneInfo
        field 2, a last-write-wins bool); here it lasts while the page is shown, from
        the value the page has (see SceneView._on_loaded)."""
        if self._set("backgroundVisible", bool(visible)) and self.view is not None:
            self.view._render()

    @Slot(QPointF, result="QVariantMap")
    def linkAt(self, pos: QPointF) -> dict:
        """The link under a scene position (DocumentViewport reads .valid and .index):
        a link of the PDF page, else the checkbox of a typed-text paragraph (the app
        looks in the same order, 0x1408a7ac0), whose url is "rmcheckbox://index=<n>",
        n the text index the paragraph starts at."""
        for link in self.links:
            if link["rect"].contains(QPointF(pos)):
                return {k: v for k, v in link.items() if k != "rect"}
        if self.view is not None:
            for n, (start, rect) in enumerate(self.view.checkbox_links()):
                if rect.contains(QPointF(pos)):
                    return {"valid": True, "index": len(self.links) + n, "url": f"{CHECKBOX_URL}{start}", "page": -1}
        return {"valid": False, "index": -1}

    @Slot(int)
    def toggleCheckboxByIndex(self, index: int) -> None:
        """Check or uncheck the checkbox of the paragraph starting at text index
        `index` (SceneLinkHandler.toggleCheckbox, when the document may be edited): a
        style edit of the page's text, one undo step, drawn at once."""
        if self.view is not None and int(index) >= 0:
            self.view.toggle_checkbox(int(index))

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

    @Slot(QRect, int)
    @Slot(QRectF, int)
    def addSelectionRect(self, rect, mode: int) -> None:
        """The selection tool's rectangle (scene coordinates, whole units), on the
        current layer (0x140437a20): InitalSelection takes what it touches,
        ToggleSelection (Shift) flips what it touches in or out."""
        if self.view is not None:
            r = rect.toRect() if isinstance(rect, QRectF) else QRect(rect)
            self.view.select_rect((r.x(), r.y(), r.width(), r.height()), int(self._get("currentLayer") or 0),
                                  int(mode) == 1)

    @Slot()
    def clearSelectedItems(self) -> None:
        if self.view is not None:
            self.view.clear_selection()

    @Slot(int, QPointF)
    def moveSelectedItems(self, layer: int, offset: QPointF) -> None:
        if self.view is not None:
            self.view.transform_selection(int(layer), rsel.translation(offset.x(), offset.y()))

    @Slot(int, QPointF, float, float)
    def scaleSelectedItems(self, layer: int, origin: QPointF, x_scale: float, y_scale: float) -> None:
        if self.view is not None:
            self.view.transform_selection(int(layer), rsel.scaling(origin.x(), origin.y(), x_scale, y_scale))

    @Slot(int, QPointF, float)
    def rotateSelectedItems(self, layer: int, origin: QPointF, rotation: float) -> None:
        if self.view is not None:
            self.view.transform_selection(int(layer), rsel.rotation(origin.x(), origin.y(), rotation))

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
    @Slot(QRectF, result="QVariantList")
    def getLineBoundingRectsToBeSelected(self, area=None) -> list:
        """The outlines DocumentViewport draws around each stroke: those the rectangle
        `area` (scene coordinates) would take while it is dragged, or, without one, the
        selected strokes where the pending transform puts them."""
        if self.view is None:
            return []
        if area is not None:
            r = area.toRect() if isinstance(area, QRectF) else QRect(area)
            area = (r.x(), r.y(), r.width(), r.height())
        return [QRect(*rect) for rect in self.view.line_rects(area)]

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
        """Commit every move, scale and turn of the selection as one edit (0x140438540:
        nothing for the identity); DocumentViewport calls it when the selection closes."""
        if self.view is not None:
            self.view.commit_selection(int(layer))

    @Slot()
    def cancelPendingEdit(self) -> None:
        """Only clears the selection (0x14094bcb0): what was pending is dropped."""
        if self.view is not None:
            self.view.clear_selection()

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
    _pen_done = Signal(object)
    penShown = Signal(int)          # the page is drawn with pen commit n (and those before it) on it

    def __init__(self, parent: QQuickItem | None = None) -> None:
        QQuickPaintedItem.__init__(self, parent)
        self.setAntialiasing(True)
        self.__dict__.setdefault("_values", {})
        self._controller = SceneController(self)
        self._controller.insert_image = self._insert_image
        self._controller.view = self
        self._values["worker"] = self       # SelectionImage asks it for the selected image
        self._sel: selecting.Selection | None = None    # what is selected, and its pending transform
        self._reselect = None               # (ids, image keys, merge step): select once the page reloads
        self._hidden: list = []             # items committed and not yet shown at their new place
        self._picked: tuple | None = None   # (content, layer, the layer's selectable items)
        self._after_load = None             # run once the page is loaded again
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
        # Pens: the last commit waiting for the page to load, and to be drawn.
        self._pen_done.connect(self._on_pen_done)
        self._pen_loading = 0
        self._pen_rendering = 0

    # -- loading --
    def componentComplete(self) -> None:
        QQuickPaintedItem.componentComplete(self)
        inherited = _viewport_transforms.get(_viewport_key(self))
        if inherited is not None:
            self._tiles.setFocalPoint(*inherited)
        self._load()
        pen_bridge.attach(self)

    def _property_changed(self, name: str) -> None:
        if name in ("pageId", "document") and self.isComponentComplete():
            self._set_selection(None)
            self.stop_text()
            self._load()
            if name == "document":
                pen_bridge.attach(self)
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
            self._pen_shown(self._pen_loading)
            return
        if self._pages is not None:
            self._pages.close()
        self._pages, self._content, self._bounds, self._page_id = pages, content, bounds, shown
        if self._pen_loading:
            self._pen_rendering, self._pen_loading = self._pen_loading, 0
        layers = content.scene.layers if content.scene is not None else []
        # The page's own bounds first: set_page then tells DocumentViewport, whose
        # updateSceneBoundingRect widens them on note pages (a margin below the paper).
        # The other way round, a reload after an edit put the bare bounds back and the
        # view jumped.
        self._tiles._set("sceneBoundingRect", bounds)
        self._controller.set_page(frame, bounds, layers, links)
        if shown != getattr(self, "_background_page", None):
            # A page newly shown starts with its template as the page has it (the
            # tablet's Hide template, SceneInfo field 2); a reload after an edit keeps
            # what was chosen here.
            self._background_page = shown
            self._controller._set("backgroundVisible",
                                  content.scene.background_visible if content.scene is not None else True)
        # The layer edits go to: Layer 1, the one the app uses until another is chosen.
        node_ids = [layer.node_id for layer in layers]
        self._controller._set("currentLayer", node_ids.index((0, 11)) if (0, 11) in node_ids else max(len(layers) - 1, 0))
        self._update_viewport()
        self._set("defaultCenter", QPointF(0, frame.height() / 2))
        self._set("sceneExteriorBoundary", bounds)
        self._update_history()
        self._set("pageStatus", PAGE_READY)
        self._set("awaitingInitialTileSet", False)
        self._set("status", 2)  # Active
        self._hidden = []
        after, self._after_load = self._after_load, None
        if after is not None:
            after()
        if self._reselect is not None:
            reselect, self._reselect = self._reselect, None
            self._reopen_selection(*reselect)
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

    def _on_edited(self, result) -> None:
        """An edit was made (or refused): draw the page again, with what it concerns
        selected (`select`: an image key, or (item ids, image keys, merge step): what
        was pasted, as the app selects what it adds), or without a selection."""
        error, select = result
        if error is not None:
            _notify(str(error))
        if isinstance(select, bytes):
            select = ([], [select], None)
        if select is not None:
            self._reselect = select
        self._load(quiet=True)

    # -- the selection --
    def _editable(self) -> bool:
        source, node = _source(), getattr(self._get("document"), "node", None)
        return source is not None and node is not None and self._content is not None and source.can_edit(node)

    def _layer_items(self, layer: int) -> list:
        """The items of a layer a selection can take (ui.selection.selectable), with the
        offsets the page draws anchored strokes at; worked out once per page shown."""
        content = self._content
        scene = content.scene if content is not None else None
        if self._picked is not None and self._picked[0] is content and self._picked[1] == layer:
            return self._picked[2]
        offsets = None
        if scene is not None and any(s.anchor is not None for s in scene.strokes):
            from viewer.renderer import layout_text, stroke_offsets
            offsets = stroke_offsets(scene, layout_text(scene, qt_measure))
        items = selecting.selectable(scene, layer, offsets)
        self._picked = (content, layer, items)
        return items

    def _set_selection(self, selection) -> None:
        changed = (selection is None) != (self._sel is None) or (
            selection is not None and self._sel is not None and selection.items != self._sel.items)
        self._sel = selection
        self._controller._set("selectionContainsImage", selection is not None and selection.has_image)
        self._controller._set("selectionContainsStroke", selection is not None and selection.has_stroke)
        self._controller._set("selectionItemCount", len(selection.items) if selection is not None else 0)
        self._controller._set("pendingEdit", QTransform(*(selection.pending if selection is not None
                                                          else rsel.IDENTITY)))
        if changed and self._content is not None:
            self._render()

    def _select(self, layer: int, ids, merge=None) -> bool:
        """Select the items `ids` of a layer (dropping those the layer does not hold) and
        open the selection box on them (after the QML that asked has switched to
        selecting, as the app answers from its worker). False when none is left."""
        items = self._layer_items(layer)
        picked = {p.item: p for p in items}
        ids = [tuple(i) for i in ids if tuple(i) in picked]
        if not ids:
            if self._sel is not None:
                self._set_selection(None)
                self._controller.selectionCleared.emit()
            return False
        self._set_selection(selecting.Selection(layer, ids, merge=merge, picked=picked))
        QTimer.singleShot(0, self._open_box)
        return True

    def _open_box(self) -> None:
        """areaSelected on the selection where the pending transform puts it."""
        sel = self._sel
        box = sel.box() if sel is not None else None
        if box is None:
            return
        rect = QRectF(box[0], box[1], box[2] - box[0], box[3] - box[1])
        self._controller.last_area = (rect.x(), rect.y())
        self._controller.areaSelected.emit(sel.layer, rect)

    def select_image_at(self, pos: QPointF, layer: int) -> bool:
        if not self._editable():
            return False
        image = editing.image_at(self._content.scene, layer, pos.x(), pos.y())
        return image is not None and self._select(layer, [image.item_id])

    def select_rect(self, rect: tuple[int, int, int, int], layer: int, toggle: bool) -> None:
        """addSelectionRect: what the rectangle takes (ui.selection.pick_rect)."""
        if not self._editable():
            return
        hit = selecting.pick_rect(self._layer_items(layer), rect)
        sel = self._sel
        if toggle and sel is not None and sel.layer == layer:
            if not rsel.is_identity(sel.pending):
                # What is selected was moved and not committed: commit, then toggle on the
                # page as it is then (the moved items under their new ids).
                self.commit_selection(layer, then=lambda moved: self._toggle_after(layer, rect, sel, moved))
                return
            ids = selecting.toggled(sel.items, hit)
        else:
            ids = hit
        if not self._select(layer, ids) and sel is None:
            self._controller.selectionCleared.emit()

    def _toggle_after(self, layer: int, rect, before, moved: dict) -> None:
        current = [moved.get(i, i) for i in before.items]
        self._select(layer, selecting.toggled(current, selecting.pick_rect(self._layer_items(layer), rect)))

    def select_lasso(self, points: list[tuple[float, float]], layer: int | None = None) -> bool:
        """A lasso drawn through scene `points` (the tablet's selection tool): what lies
        wholly inside is selected (ui.selection.pick_lasso)."""
        if not self._editable():
            return False
        layer = int(self._controller._get("currentLayer") or 0) if layer is None else layer
        if self._sel is not None:
            self.commit_selection(self._sel.layer)
        return self._select(layer, selecting.pick_lasso(self._layer_items(layer), points))

    def _reopen_selection(self, ids, keys, merge) -> None:
        """Select, once the page is loaded again, the items an edit made: `ids`, and the
        items showing the image `keys`."""
        layer = int(self._controller._get("currentLayer") or 0)
        ids = list(ids)
        for key in keys:
            found = editing.find_image(self._content.scene if self._content else None, key)
            if found is not None:
                layer = found[0]
                ids.append(found[1].item_id)
        self._select(layer, ids, merge)

    def clear_selection(self) -> None:
        self._set_selection(None)

    def selection_preview(self):
        """(a page holding the selected items, its x origin) for SelectionImage."""
        sel, content = self._sel, self._content
        if sel is None or content is None or content.scene is None:
            return None
        return selecting.preview_scene(content.scene, sel.chosen()), content.scene.x_origin

    def line_rects(self, area) -> list[tuple[int, int, int, int]]:
        if not self._editable():
            return []
        layer = int(self._controller._get("currentLayer") or 0)
        if area is not None:
            items = self._layer_items(layer)
            hit = set(selecting.pick_rect(items, area))
            return selecting.line_rects([p for p in items if p.item in hit])
        sel = self._sel
        return selecting.line_rects(sel.chosen(), sel.pending) if sel is not None else []

    def _selection_target(self, layer: int):
        """(source, document, page id, selection) for an edit of the selection, or None
        (nothing selected, another layer, or not editable)."""
        sel = self._sel
        if sel is None or sel.layer != layer or not self._editable():
            return None
        return _source(), self._get("document").node, self._page_id, sel

    def transform_selection(self, layer: int, m) -> None:
        """Move, scale or turn the selection: the pending transform is multiplied, and the
        box opens again where the selection is now; the page is not changed until the
        selection closes (commit_selection), as in the app."""
        sel = self._sel
        if sel is None or sel.layer != layer:
            return
        sel.apply(m)
        self._controller._set("pendingEdit", QTransform(*sel.pending))
        self._open_box()

    def commit_selection(self, layer: int, then=None) -> None:
        """applyPendingEdit: the selection's transform as one edit of the page
        (LibrarySource.transform_items), then the page drawn again. `then(moved)` runs
        once the page is loaded again ({old id: new id})."""
        target = self._selection_target(layer)
        if target is None or rsel.is_identity(target[3].pending):
            return
        source, node, page_id, sel = target
        items, pending, offsets, merge = list(sel.items), sel.pending, sel.offsets(), sel.merge
        sel.pending, sel.merge = rsel.IDENTITY, None
        self._hidden = items                # not drawn at the old place meanwhile
        now_ms = _now_ms()

        def job():
            error, moved = None, {}
            try:
                _, moved = source.transform_items(node, page_id, items, pending, now_ms, offsets, merge)
            except (EditError, rsel.SelectError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("moving the selection on page %s", page_id)
                error = f"The selection could not be moved: {type(err).__name__}"
            if then is not None:
                emit_from_worker(self, lambda: self._after_commit(error, moved, then))
            else:
                emit_from_worker(self, lambda: self._edited.emit((error, None)))
        self._pool.start(_Job(job))

    def _after_commit(self, error, moved, then) -> None:
        if error is not None:
            _notify(str(error))
        self._reselect = None
        self._after_load = lambda: then(moved)
        self._load(quiet=True)

    def copy_selection(self, layer: int, scale: float) -> list:
        """Clipboard records of the selected items (cloneSelectedItems, 0x14043a320):
        clones in scene coordinates, transformed by the pending transform and `scale`."""
        target = self._selection_target(layer)
        if target is None:
            return []
        source, node, page_id, sel = target
        m = rsel.multiply(sel.pending, (scale, 0.0, 0.0, scale, 0.0, 0.0)) if scale != 1 else sel.pending
        try:
            values = source.item_values(node, page_id, sel.items)
        except EditError as err:
            _notify(str(err))
            return []
        out = []
        for p in sel.chosen():
            value = values.get(p.item)
            if value is None:
                continue
            local = rsel.multiply(rsel.translation(*p.offset), m) if p.offset != (0.0, 0.0) else m
            if p.stroke is not None:
                out.append(editing.clip_line(rsel.clone(value, local)))
            else:
                out.append(editing.clip_image(node.id, page_id, p.image, 1.0,
                                              rsel.transform_vertices(p.image.vertices, m)))
        return out

    def paste_items(self, layer: int, items, scale: float, pos: QPointF) -> None:
        """Copied items pasted, the box around them centred on a scene position
        (cloneAddAndSelectItems, 0x14043a060 -> 0x1409af310): strokes appended to the
        layer (moveId 0:0, timestamp kept); each image loaded from where it was copied
        and stored again as a new file under a new key (0x1409b1a90 -> 0x14094c420),
        its copy keeping the copied item's timestamps (one whose image cannot be loaded
        is dropped, as the app drops it). One action for undo; the pasted items are then
        the selection, and moving them before it closes is part of the same undo step."""
        lines, images = editing.clip_lines(items), editing.clip_images(items)
        target = self._insertion_target("Pasted items") if (lines or images) else None
        if target is None:
            return
        source, node, page_id = target
        placed = editing.placed_items(lines, images, scale, (pos.x(), pos.y()))
        now_ms = _now_ms()

        def insert():
            ids, keys = [], []
            with source.edit_action(node, page_id) as action:      # undone at once, as the app's add-items
                strokes = [v for kind, v in placed if kind == "line"]
                if strokes:
                    ids = source.paste_strokes(node, page_id, strokes, now_ms, action)[1]
                for kind, value in placed:
                    if kind == "image":
                        key = paste_one(*value, action)
                        if key is not None:
                            keys.append(key)
            return ids, keys, source.undo_top(node, page_id)

        def paste_one(item, corners, action) -> bytes | None:
            try:
                picture = QImage.fromData(source.image_file(item["documentId"], item["pageId"], item["fileName"]))
            except EditError as err:
                log.warning("dropping pasted image item, failed to load source image ref: %s", err)
                return None
            if picture.isNull():
                log.warning("dropping pasted image item, failed to load source image ref")
                return None
            data, extension, _ = editing.encode_image(picture)
            edit = source.insert_image(node, page_id, data, extension, corners, now_ms, item["timestamps"], action)
            return bytes.fromhex(edit.key)
        self._run_insertion(page_id, insert)

    def delete_selection(self, layer: int) -> None:
        target = self._selection_target(layer)
        if target is None:
            return
        source, node, page_id, sel = target
        items, now_ms = list(sel.items), _now_ms()
        self._hidden = items
        self._set_selection(None)
        self._controller.selectionCleared.emit()

        def job():
            error = None
            try:
                source.delete_items(node, page_id, items, now_ms)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:
                log.exception("deleting the selection on page %s", page_id)
                error = f"The selection could not be deleted: {type(err).__name__}"
            emit_from_worker(self, lambda: self._edited.emit((error, None)))
        self._pool.start(_Job(job))

    # -- pens --
    def scene_point(self, pos: QPointF) -> QPointF:
        """A point of this item -> scene coordinates."""
        return self._tiles.to_scene(QPointF(pos))

    def scene_to_view(self) -> QTransform:
        c, sc, half = self._tiles._get("center"), self._tiles._get("scale"), self._tiles._view_half()
        return QTransform().translate(half.x(), half.y()).scale(sc, sc).translate(-c.x(), -c.y())

    def pen_scale(self) -> float:
        """How many screen pixels a scene unit covers (the pen handler's scale)."""
        return float(self._tiles._get("scale"))

    def pen_rotation(self) -> float:
        """The page's rotation on screen, as the pen handler reads it off its transform
        (rm_stroke.rotation_of). This view only moves and scales the page (landscape
        documents are shown as stored, as in reMarkable.exe), so it is 0."""
        m = self.scene_to_view()
        rotation = s_rotation_of(m.m11(), m.m12(), m.m21(), m.m22())
        return 0.0 if rotation is None else rotation

    def pen_refusal(self) -> str:
        """Why nothing may be drawn on or erased from the page shown ("": it may)."""
        source, node = _source(), getattr(self._get("document"), "node", None)
        if source is None or node is None:
            return "No document is open"
        refusal = source.edit_refusal(node)
        if refusal:
            return refusal
        if self._content is None or not self._page_id:
            return "The page is still loading"
        return ""

    def pen_commit(self, kind: str, line, seq: int) -> None:
        """Keep a stroke drawn (`kind` "draw") or an eraser pass ("erase") on the page
        shown, on the view's worker, then draw the page again; penShown(seq) once it is."""
        source, node, page_id = _source(), getattr(self._get("document"), "node", None), self._page_id
        if source is None or node is None or not page_id:
            self._pen_shown(seq)
            return
        if self._sel is not None:
            self._controller.selectionCleared.emit()    # DocumentViewport commits and closes it
            self._set_selection(None)
        now_ms = _now_ms()
        scene = self._content.scene if self._content is not None else None

        def job():
            error, changed = None, True
            try:
                if kind == "erase":
                    changed = source.erase_strokes(node, page_id, line, now_ms, anchor_offsets(scene)) is not None
                else:
                    source.add_stroke(node, page_id, line, now_ms)
            except (EditError, *EXPECTED_ERRORS) as err:
                error = str(err)
            except Exception as err:      # a stroke never takes the page view down with it
                log.exception("keeping a stroke on page %s", page_id)
                error = f"The stroke could not be kept: {type(err).__name__}"
            emit_from_worker(self, lambda: self._pen_done.emit((error, changed, seq)))
        self._pool.start(_Job(job))

    def _on_pen_done(self, result) -> None:
        error, changed, seq = result
        if error is not None:
            _notify(error)
        if not changed and error is None:
            self._pen_shown(seq)
            return
        self._pen_loading = max(self._pen_loading, seq)
        self._load(quiet=True)

    def _pen_shown(self, seq: int) -> None:
        if seq:
            self.penShown.emit(int(seq))

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
        if self._sel is not None:
            self._controller.selectionCleared.emit()    # committed and closed before the undo
            self._set_selection(None)
        (self._controller.aboutToRedo if redo else self._controller.aboutToUndo).emit()
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

    def checkbox_links(self) -> list[tuple[int, QRectF]]:
        """The checkboxes of the page as shown (with what is being typed): (text index
        of the paragraph, box in scene coordinates); laid out once per text shown."""
        from viewer.renderer import checkbox_boxes, layout_text
        session = self.text_session
        lines = session.lines() if session is not None else None
        scene = None if session is not None else (self._content.scene if self._content is not None else None)
        token = lines if session is not None else scene     # the session lays its text out again on a change
        cached = getattr(self, "_checkboxes", None)
        if cached is not None and cached[0] is token:
            return cached[1]
        if session is not None:
            scene = session.shown_scene()
        if scene is None or scene.text is None:
            self._checkboxes = (token, [])
            return []
        if lines is None:
            lines = layout_text(scene, qt_measure)
        boxes = [(start, to_scene(QRectF(*box))) for start, box in checkbox_boxes(scene, lines, qt_measure)]
        self._checkboxes = (token, boxes)
        return boxes

    def toggle_checkbox(self, position: int) -> None:
        """Toggle the checkbox of the paragraph starting at text index `position`, in
        the text session (opened for it when there is none, and closed again after,
        keeping the edit, when text mode is off)."""
        def toggle(t: textedit.TextSession) -> None:
            if not t.toggle_checkbox(position):
                log.info("no checkbox starts at text index %d", position)

        if self.text_session is not None or self._text_opening:
            self.text_do(toggle)
            return
        if self._text_target() is None:
            return
        self._text_then.append(toggle)
        if not self._get("textModeEnabled"):
            self._text_then.append(lambda t: QTimer.singleShot(0, self.stop_text))
        self.start_text(StyleType.TITLE)

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
        hidden = list(self._sel.items) if self._sel is not None else []
        if (hidden or self._hidden) and content.scene is not None:
            from dataclasses import replace
            content = replace(content, scene=selecting.without(content.scene, hidden + self._hidden))   # SelectionImage shows them
        self._rendering = True
        background = bool(self._controller._get("backgroundVisible"))

        def job():
            try:
                image = render_content(content, scale, "page", background)
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
        if self._pen_rendering:
            self._pen_shown(self._pen_rendering)
            self._pen_rendering = 0
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
        deletes the selection."""
        if self._get("textModeEnabled"):
            return textedit.key_action(int(key), int(modifiers), bool(auto_repeat), str(text or ""))
        actions = SceneKeyHandlerAction.Type
        kind = {int(Qt.Key.Key_PageUp.value): actions["PageUp"], int(Qt.Key.Key_PageDown.value): actions["PageDown"]}
        if int(key) in (int(Qt.Key.Key_Backspace.value), int(Qt.Key.Key_Delete.value)) and self._sel is not None:
            # Delete comes as the selection handler's shortcut; Backspace reaches here.
            self.delete_selection(self._sel.layer)
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
