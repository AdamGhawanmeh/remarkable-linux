"""The app's own painted items and helpers that the official QML instantiates.

Each is reconstructed from its implementation in reMarkable.exe (paint() and property
handling, read from the disassembly); addresses are noted so they can be re-checked.
    Triangle        (TriangleItem, com.remarkable)       paint @ 0x140601810
    DocumentCover   (com.remarkable)                     paint @ 0x140602e00
    FontExtender    (ark::controls::FontExtender)        setters @ 0x1405caab0
    ShareNative     (com.remarkable)                     platform share sheet
"""

from __future__ import annotations

import logging
import math
from enum import IntEnum

from PySide6.QtCore import Property, QObject, QPointF, QRectF, Qt, Signal, Slot, QEnum
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap, QPixmapCache
from PySide6.QtQuick import QQuickItem, QQuickPaintedItem

log = logging.getLogger("rm.ui.quick.items")

_TOP, _LEFT, _RIGHT, _BOTTOM = (int(e.value) for e in (Qt.Edge.TopEdge, Qt.Edge.LeftEdge, Qt.Edge.RightEdge,
                                                        Qt.Edge.BottomEdge))


class TriangleItem(QQuickPaintedItem):
    """A filled triangle. `edge` is a Qt.Edges mask: one edge gives the triangle from
    that edge to the centre, two adjacent edges the right triangle in that corner."""

    edgeChanged = Signal()
    colorChanged = Signal()

    def __init__(self, parent: QQuickItem | None = None) -> None:
        super().__init__(parent)
        self._edge = _TOP
        self._color = QColor(Qt.GlobalColor.black)

    def _set_color(self, color: QColor) -> None:
        color = QColor(color)
        if color != self._color:
            self._color = color
            self.colorChanged.emit()
            self.update()

    def _set_edge(self, edge: int) -> None:
        edge = int(edge)
        if edge != self._edge:
            self._edge = edge
            self.edgeChanged.emit()
            self.update()

    color = Property(QColor, lambda self: self._color, _set_color, notify=colorChanged)
    edge = Property(int, lambda self: self._edge, _set_edge, notify=edgeChanged)

    def path(self) -> QPainterPath:
        w, h = self.width(), self.height()
        points = {
            _TOP: ((w / 2, h / 2), (0, 0), (w, 0)),
            _LEFT: ((w / 2, h / 2), (0, 0), (0, h)),
            _TOP | _LEFT: ((0, 0), (w, 0), (0, h)),
            _RIGHT: ((w / 2, h / 2), (w, 0), (w, h)),
            _TOP | _RIGHT: ((0, 0), (w, 0), (w, h)),
            _BOTTOM: ((w / 2, h / 2), (0, h), (w, h)),
            _LEFT | _BOTTOM: ((0, h), (w, h), (0, 0)),
            _RIGHT | _BOTTOM: ((0, h), (w, h), (w, 0)),
        }.get(self._edge, ())
        path = QPainterPath()
        if points:
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
        return path

    def paint(self, painter: QPainter) -> None:
        path = self.path()
        painter.save()
        if not self.antialiasing():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillPath(path, QBrush(self._color, Qt.BrushStyle.SolidPattern))
        painter.restore()


class DocumentCover(QQuickPaintedItem):
    """The outline of a library thumbnail: a notebook (rounded right corners and a
    spine), a PDF (folded top-right corner), an ebook (rounded right corners), or a
    plain rectangle. In Transparent mode the area outside the shape is painted with
    backgroundColor (to hide the thumbnail behind it); Filled fills the shape with
    color. With `cached`, the drawing is kept in a QPixmapCache keyed by its inputs."""

    DocumentType = IntEnum("DocumentType", {"Undefined": 0, "Notebook": 1, "Pdf": 2, "Ebook": 3})
    FillMode = IntEnum("FillMode", {"Transparent": 0, "Filled": 1})
    QEnum(DocumentType)
    QEnum(FillMode)

    propertyChanged = Signal()

    def __init__(self, parent: QQuickItem | None = None) -> None:
        super().__init__(parent)
        self._type = 0
        self._border_width = 1.0
        self._decoration_scale = 1.0
        self._color = QColor(Qt.GlobalColor.white)
        self._border_color = QColor(Qt.GlobalColor.black)
        self._background = QColor(Qt.GlobalColor.white)
        self._fill = 0
        self._smooth = True
        self._cached = True
        self._image = QImage()
        self.propertyChanged.connect(self.handlePropertyChange)

    @Slot()
    def handlePropertyChange(self) -> None:
        self._image = QImage()
        self.update()

    def _setter(attr: str, cast):
        def set_(self, value) -> None:
            value = cast(value)
            if getattr(self, attr) != value:
                setattr(self, attr, value)
                self.propertyChanged.emit()
        return set_

    def _finite(value) -> float:
        value = float(value)
        return value if math.isfinite(value) else 0.0

    documentType = Property(int, lambda self: self._type, _setter("_type", int), notify=propertyChanged)
    borderWidth = Property(float, lambda self: self._border_width, _setter("_border_width", _finite),
                           notify=propertyChanged)
    decorationScale = Property(float, lambda self: self._decoration_scale, _setter("_decoration_scale", _finite),
                               notify=propertyChanged)
    color = Property(QColor, lambda self: self._color, _setter("_color", QColor), notify=propertyChanged)
    backgroundColor = Property(QColor, lambda self: self._background, _setter("_background", QColor),
                               notify=propertyChanged)
    borderColor = Property(QColor, lambda self: self._border_color, _setter("_border_color", QColor),
                           notify=propertyChanged)
    fillMode = Property(int, lambda self: self._fill, _setter("_fill", int), notify=propertyChanged)
    smooth = Property(bool, lambda self: self._smooth, _setter("_smooth", bool), notify=propertyChanged)
    cached = Property(bool, lambda self: self._cached, _setter("_cached", bool), notify=propertyChanged)

    # -- painting --
    def paint(self, painter: QPainter) -> None:
        painter.save()
        if not self._cached:
            self._draw(painter)
        else:
            key = (f"{self._type}:{self._border_width!r}:{self._color.name(QColor.NameFormat.HexArgb)}:"
                   f"{self._border_color.name(QColor.NameFormat.HexArgb)}:"
                   f"{self._background.name(QColor.NameFormat.HexArgb)}:{self._fill}:"
                   f"{self.width()!r}x{self.height()!r}")
            pixmap = QPixmap()
            if not QPixmapCache.find(key, pixmap):
                pixmap = QPixmap(int(math.ceil(self.width())), int(math.ceil(self.height())))
                pixmap.fill(Qt.GlobalColor.transparent)
                offscreen = QPainter(pixmap)
                self._draw(offscreen)
                offscreen.end()
                QPixmapCache.insert(key, pixmap)
            self._image = pixmap.toImage()
            if self._smooth:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(0, 0, self._image)
        painter.restore()

    def _pen(self) -> QPen:
        if self._border_width > 0:
            return QPen(QBrush(self._border_color, Qt.BrushStyle.SolidPattern), self._border_width,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.BevelJoin)
        return QPen(Qt.PenStyle.NoPen)

    def _draw(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, self.antialiasing())
        w, h = self.width(), self.height()
        if self._type == 0:
            painter.setPen(self._pen())
            painter.drawRects([QRectF(0.5, 0.5, w - 1.0, h - 1.0)])
        elif self._type == 1:
            self._draw_book(painter)
            spine = self._decoration_scale * 12.0
            painter.setBrush(self._color)
            painter.setPen(self._pen())
            painter.drawRects([QRectF(0.5, 0.5, spine - 1.0, h - 1.0)])
        elif self._type == 2:
            self._draw_pdf(painter)
        elif self._type == 3:
            self._draw_book(painter)

    @staticmethod
    def _rounded_right(path: QPainterPath, left: float, top: float, right: float, bottom: float,
                       radius: float) -> None:
        """The right side with quadratic corners, as the app builds it (the first corner
        ends at y = radius and the last segment runs to x = radius before `left`)."""
        path.lineTo(right - radius, top)
        path.quadTo(right, top, right, radius)
        path.lineTo(right, bottom - radius)
        path.quadTo(right, bottom, right - radius, bottom)
        path.lineTo(radius, bottom)
        path.lineTo(left, bottom)

    def _draw_book(self, painter: QPainter) -> None:
        radius = self._decoration_scale * 18.0
        w, h = self.width(), self.height()
        outline = QPainterPath()
        outline.moveTo(0.5, 0.5)
        self._rounded_right(outline, 0.5, 0.5, w - 0.5, h - 0.5, radius)
        outline.lineTo(0.5, 0.5)
        if self._fill == 0:
            corners = QPainterPath()
            corners.moveTo(w, 0)
            self._rounded_right(corners, w, 0, w, h, radius)
            corners.lineTo(w, 0)
            painter.fillPath(corners, QBrush(self._background, Qt.BrushStyle.SolidPattern))
        elif self._fill == 1:
            painter.fillPath(outline, QBrush(self._color, Qt.BrushStyle.SolidPattern))
        painter.setPen(self._pen())
        painter.drawPath(outline)

    def _draw_pdf(self, painter: QPainter) -> None:
        fold = self._decoration_scale * 40.0
        w, h = self.width(), self.height()
        x = w - fold - 1.0
        cut = QPainterPath()
        cut.moveTo(x, 0.5)
        cut.lineTo(w, 0.5)
        cut.lineTo(w, fold)
        cut.closeSubpath()
        painter.setBrush(self._background)
        if self._fill == 0:
            painter.setPen(self._background)
            painter.drawPath(cut)
        elif self._fill == 1:
            page = QPainterPath()
            page.moveTo(0.5, 0.5)
            page.lineTo(x - 1.0, 0.5)
            page.lineTo(w - 1.0, fold)
            page.lineTo(w - 1.0, h)
            page.lineTo(0.5, h)
            page.closeSubpath()
            painter.fillPath(page, QBrush(self._color, Qt.BrushStyle.SolidPattern))
        painter.setPen(self._pen())
        painter.setBrush(QColor(Qt.GlobalColor.white))
        dog_ear = QPainterPath()
        dog_ear.moveTo(x, 0.5)
        dog_ear.lineTo(x, fold)
        dog_ear.lineTo(fold + x, fold)
        dog_ear.closeSubpath()
        painter.drawPath(dog_ear)
        if self._border_width > 0:
            painter.drawLine(QPointF(0.5, 0.5), QPointF(x, 0.5))
            painter.drawLine(QPointF(w - 1.0, fold), QPointF(w - 1.0, h - 1.0))
            painter.drawLine(QPointF(0.5, h - 1.0), QPointF(w - 1.0, h - 1.0))
            painter.drawLine(QPointF(0.5, 0.5), QPointF(0.5, h - 1.0))


class FontExtender(QObject):
    """ark.controls FontExtender: passes a font through, turning font antialiasing off
    (QFont.NoAntialias) when `antialiasing` is false."""

    fontChanged = Signal()
    antialiasingChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._font = QFont()
        self._antialiasing = True

    def _apply(self) -> None:
        strategy = self._font.styleStrategy()
        no_aa = QFont.StyleStrategy.NoAntialias
        strategy = (strategy & ~no_aa) if self._antialiasing else (strategy | no_aa)
        self._font.setStyleStrategy(QFont.StyleStrategy(strategy))
        self.fontChanged.emit()

    def _set_font(self, font: QFont) -> None:
        if font != self._font:
            self._font = QFont(font)
            self._apply()

    def _set_antialiasing(self, value: bool) -> None:
        if bool(value) != self._antialiasing:
            self._antialiasing = bool(value)
            self._apply()

    font = Property(QFont, lambda self: QFont(self._font), _set_font, notify=fontChanged)
    antialiasing = Property(bool, lambda self: self._antialiasing, _set_antialiasing, notify=antialiasingChanged)


class ShareNative(QQuickItem):
    """The platform share sheet. Linux has none (the app's own fallback logs
    "ShareNative::shareFileNative not implemented on ..."), so sharing reports that."""

    shareFailed = Signal(str)
    docsToShareChanged = Signal()

    def __init__(self, parent: QQuickItem | None = None) -> None:
        super().__init__(parent)
        self._docs = 0

    def _set_docs(self, value: int) -> None:
        if int(value) != self._docs:
            self._docs = int(value)
            self.docsToShareChanged.emit()

    docsToShare = Property(int, lambda self: self._docs, _set_docs, notify=docsToShareChanged)

    @Slot(QObject)
    @Slot(QObject, "QVariantList")
    @Slot(QObject, "QVariantList", str)
    @Slot(QObject, "QVariantList", str, bool)
    def share(self, entry: QObject, pages: list | None = None, password: str = "", keep_password: bool = False) -> None:
        log.info("ShareNative::shareFileNative not implemented on linux")
        self.shareFailed.emit("Sharing is not available on Linux. Use Export instead.")

    @Slot(str, "QStringList")
    def exportFinished(self, document_id: str, paths: list) -> None:
        pass
