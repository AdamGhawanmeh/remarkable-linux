"""Drawing with pens and the eraser on the pages of an editable notebook.

reMarkable.exe has the tablet's pen code (PenInputSurface, PenInputLineHandler), but its
desktop QML never puts a pen toolbar or a drawing surface on screen. This client adds
both next to the app's own document view, without changing its QML:

    PenTools     the tool in hand, its size and colour (per document, kept in its
                 .content extraMetadata as the app keeps them: ui.pen), and whether a
                 tool is in hand at all (`active`; otherwise a click on the page does
                 what the app does: text mode, selecting an image)
    PenSurface   an item over the page (a child of the DocumentViewport, above its
                 mouse areas) that takes the mouse and the stylus while a tool is in
                 hand: each sample (x, y, pressure, tilt) goes to the tool's point
                 synthesis (viewer.rm_stroke.StrokeBuilder), the line so far is drawn
                 as it grows, and on release it is kept through the page's SceneView
                 (LibrarySource.add_stroke / erase_strokes: one undo step each)
    the toolbar  TOOLBAR_QML, a column of the app's own pen icons at the left of the
                 page, with the sizes and colours of the tool in hand beside it

attach(view) sets these up for the DocumentViewport a SceneView is shown in; one set
per viewport, handed on from page to page.

A mouse gives the samples the app's PenInputSurface gives (0x140ad8e90, 0x140ad8d80,
0x140ad9060): pressure 0.7, no tilt, one sample per press, move and release and one
more every 25 ms the pressed mouse is held still. Alt, or a stylus's eraser end, erases
(PenInputSurface's "Simulating Eraser Tip"). A stylus gives its own pressure and tilt:
its press goes where Qt delivers it (what is on top there, such as the toolbar, takes
it first), and only the stroke it starts on the page reads its moves and release from
the window's QTabletEvents (TabletFilter).

The toolbar's buttons are TapHandlers, which take a mouse, a touchpad, a touch screen
and a stylus alike.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Callable

import shiboken6
from PySide6.QtCore import (Property, QByteArray, QEvent, QObject, QPointF, QTimer, QUrl, Qt, Signal, Slot)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPointingDevice
from PySide6.QtQml import QQmlComponent, QQmlContext, qmlEngine
from PySide6.QtQuick import QQuickItem, QQuickPaintedItem

from core.edits import EditError
from viewer import renderer
from viewer import rm_stroke as s
from viewer.rm_parser import Color, Layer, PageScene, Point, Stroke, Tool

from ... import pen as pens

log = logging.getLogger("rm.ui.quick.pen")

LASSO_COLOR = QColor("#3f86ff")  # the selection blue (Colors.selectionToolBlue)
# Hold to straighten: the pen held this still this long fixes the stroke as a straight
# line, whose end then follows the pen until it is lifted (ShapesOverlay's Line). The
# tablet's QML decides when a hold has happened and is not in reMarkable.exe, so these
# two values are this client's own (ARCHITECTURE.md 5.1, "Straight lines and shapes").
HOLD_MS = 500
HOLD_SLOP_PX = 4.0
STILL_MS = 25                   # PenInputSurface's timer: a held mouse adds a sample this often
TOOLBAR_Z = 50
SURFACE_Z = 1000

_source: Callable[[], object] = lambda: None
_notify: Callable[[str], None] = lambda message: log.warning("%s", message)


def set_source(provider: Callable[[], object], notify: Callable[[str], None] | None = None) -> None:
    global _source, _notify
    _source = provider
    if notify is not None:
        _notify = notify


def right_handed() -> bool:
    """Settings.rightHandMode (the app's "Right-handed" setting; True when unknown)."""
    from . import _current
    settings = getattr(_current, "settings", None)
    try:
        return bool(settings.rightHandMode) if settings is not None else True
    except RuntimeError:            # the settings object is gone (the app is closing)
        return True


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hex(argb: int) -> str:
    return f"#{argb & 0xFFFFFF:06x}"


# -- the tools ------------------------------------------------------------------------

class PenTools(QObject):
    """The pen toolbar's state for one document viewport (see the module doc)."""

    changed = Signal()
    activeChanged = Signal()
    availableChanged = Signal()
    lassoChanged = Signal()
    lassoSelected = Signal()        # a lasso took something: the toolbar turns selection mode on
    clearRequested = Signal()       # Erase all: the page's current layer is cleared (ClearPage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._available = False
        self._node = None                   # the document shown
        self._settings: dict[str, str] = {}
        self._tool = pens.DEFAULT_TOOL
        self._lasso = False                 # the lasso in hand instead of a pen

    # -- the document --
    def set_document(self, node) -> None:
        """The viewport shows `node` (a page of it): read its pen settings."""
        source = _source()
        same = node is not None and self._node is not None and node.id == self._node.id
        self._node = node
        available = bool(node is not None and source is not None and source.can_edit(node))
        if not same:
            self._settings = {}
            if available:
                try:
                    self._settings = source.tool_settings(node)
                except Exception as err:      # settings never keep the page from showing
                    log.warning("cannot read the pen settings of %s: %s", node.id, err)
            self._tool = pens.current_tool(self._settings)
            self.changed.emit()
        if available != self._available:
            self._available = available
            self.availableChanged.emit()
        if not available:
            self.deactivate()

    def pen(self) -> pens.Pen:
        return pens.pen_of(self._settings, self._tool)

    def _keep(self, values: dict[str, str]) -> None:
        """New settings: used at once, and kept with the document (a pending edit)."""
        self._settings.update(values)
        self.changed.emit()
        source = _source()
        if source is None or self._node is None:
            return
        try:
            source.set_tool_settings(self._node, values, _now_ms())
        except EditError as err:
            _notify(str(err))
        except Exception as err:
            log.exception("keeping pen settings")
            _notify(f"The pen settings could not be kept: {type(err).__name__}")

    # -- QML --
    def _get_active(self) -> bool:
        return self._active

    def _set_active(self, value: bool) -> None:
        value = bool(value) and self._available
        if value != self._active:
            self._active = value
            self.activeChanged.emit()

    def _set_lasso(self, value: bool) -> None:
        if bool(value) != self._lasso:
            self._lasso = bool(value)
            self.lassoChanged.emit()

    active = Property(bool, _get_active, _set_active, notify=activeChanged)
    lasso = Property(bool, lambda self: self._lasso, notify=lassoChanged)
    available = Property(bool, lambda self: self._available, notify=availableChanged)
    tool = Property(str, lambda self: self._tool.key, notify=changed)
    toolLabel = Property(str, lambda self: self._tool.label, notify=changed)
    size = Property(int, lambda self: self.pen().size, notify=changed)
    color = Property(str, lambda self: (self.pen().color.key if self.pen().color is not None else ""), notify=changed)
    isEraser = Property(bool, lambda self: self._tool.is_eraser, notify=changed)
    hasOptions = Property(bool, lambda self: self._tool.sizes, notify=changed)
    clearAll = Property("QVariantMap", lambda self: dict(zip(("key", "label", "icon"), pens.CLEAR_ALL)),
                        constant=True)
    tools = Property("QVariantList", lambda self: [{"key": t.key, "label": t.label, "icon": t.icon}
                                                    for t in pens.TOOLS], constant=True)
    sizes = Property("QVariantList", lambda self: [{"size": n, "label": pens.SIZE_LABELS[n],
                                                    "icon": ("stroke_thin", "stroke_medium", "stroke_thick")[n - 1]}
                                                   for n in pens.SIZES], constant=True)
    colors = Property("QVariantList", lambda self: [{"key": c.key, "label": c.label,
                                                     "hex": _hex(renderer.PALETTE_ARGB[c.color])}
                                                    for c in self._tool.colors], notify=changed)

    @Slot(str)
    def chooseTool(self, key: str) -> None:
        """A tool button: that tool in hand; the one in hand again puts it down."""
        tool = pens.BY_KEY.get(str(key))
        if tool is None or not self._available:
            return
        if tool is self._tool and self._active and not self._lasso:
            self._set_active(False)
            return
        self._set_lasso(False)
        changed = tool is not self._tool
        self._tool = tool
        self._set_active(True)
        if changed or any(self._settings.get(k) != v for k, v in pens.tool_values(tool).items()):
            self._keep(pens.tool_values(tool))

    @Slot(int)
    def setSize(self, size: int) -> None:
        if int(size) in pens.SIZES and int(size) != self.pen().size:
            self._keep(pens.size_values(self._tool, int(size)))

    @Slot(str)
    def setColor(self, key: str) -> None:
        try:
            values = pens.color_values(self._tool, str(key))
        except ValueError:
            return
        if self.pen().color is None or self.pen().color.key != key:
            self._keep(values)

    @Slot()
    def chooseLasso(self) -> None:
        """The lasso button: a freehand lasso selects what lies wholly inside it (the
        tablet's selection tool, viewer.rm_select.Lasso); in hand again puts it down."""
        if not self._available:
            return
        if self._lasso and self._active:
            self.deactivate()
            return
        self._set_lasso(True)
        self._set_active(True)

    @Slot()
    def eraseAll(self) -> None:
        """The Erase all button (the tablet's ClearPage): the strokes of the page's current
        layer are deleted at once, one undo step (SceneController::clearLinesInLayer).
        The tool in hand stays as it is."""
        if self._available:
            self.clearRequested.emit()

    @Slot()
    def deactivate(self) -> None:
        self._set_active(False)
        self._set_lasso(False)


# -- the drawing surface --------------------------------------------------------------

class _Drawing:
    """A stroke or an eraser pass under way: its samples (scene coordinates) and, for
    a pen, the points the tool makes of them so far."""

    def __init__(self, pen: pens.Pen, eraser: bool, scale: float, stylus: bool, lasso: bool = False, *,
                 right_handed: bool = True, rotation: float = 0.0) -> None:
        self.pen, self.eraser, self.scale, self.stylus, self.lasso = pen, eraser, scale, stylus, lasso
        # Erase selection in hand (not the stylus's eraser end over another tool): the
        # samples outline the area to erase.
        self.area = bool(eraser and pen.tool.is_area)
        self.size = pen.size
        self.samples: list[tuple] = []
        self.end: tuple[float, float] | None = None     # held: the straight line's end (scene)
        # The calligraphy nib turns with the hand the app is set for (Settings.rightHandMode,
        # copied into the handler when a stroke begins, 0x140ac8103) and with the page's
        # rotation on screen (setTransform, 0x140aca100).
        self.right_handed, self.rotation = bool(right_handed), float(rotation)
        self.builder = None if eraser or lasso else s.StrokeBuilder(
            pen.tool.tool, pen.line_color, pen.size, pen.argb, scale=scale, right_handed=self.right_handed,
            rotation=self.rotation)

    def add(self, sample: tuple) -> None:
        self.samples.append(sample)
        if self.builder is not None:
            self.builder.add(*sample)

    @property
    def can_straighten(self) -> bool:
        return self.builder is not None and len(self.builder.points) >= 3

    def straight(self):
        """The line as drawn, made straight to `end` (unthinned), or None."""
        if self.end is None or not self.can_straighten:
            return None
        return s.straighten(self.builder.line(), self.end, self.scale)

    def line(self):
        if self.area:
            # An EraseSection line: its points are the outline (closed back to the first),
            # as the tablet's eraseWithLine takes it; under three points it covers nothing.
            if len({(x, y) for x, y, *_ in self.samples}) < 3:
                return None
            return s.eraser_line(self.samples, s.MEDIUM, tool=s.ERASE_SECTION, scale=self.scale)
        if self.eraser:
            return s.eraser_line(self.samples, self.size, scale=self.scale)
        straight = self.straight()
        if straight is not None:
            return s.simplify(straight)         # thinned like any stroke before addLine
        return s.simplify(self.builder.line()) if self.builder.points else None


def preview_scene(tool: int, color: int, size: float, argb: int | None, points) -> PageScene:
    """A page holding just this line (stored points, scene coordinates), for the
    renderer to draw as it will draw the stroke once kept."""
    pts = [Point(p.x, p.y, p.speed / 4, p.direction * 2 * math.pi / 255, p.width / 4, p.pressure / 255)
           for p in points]
    stroke = Stroke(Tool.parse(tool), Color.parse(color), pts, size, argb=argb, tool_id=tool, color_id=color)
    return PageScene(6, [Layer(strokes=[stroke])])


class PenSurface(QQuickPaintedItem):
    """The item over the page that draws (see the module doc)."""

    def __init__(self, pen_tools: PenTools, viewport: QQuickItem) -> None:
        super().__init__(viewport)
        self.tools = pen_tools
        self.viewport = viewport
        self.view = None                    # the SceneView of the page shown
        self.setZ(SURFACE_Z)
        self.setAntialiasing(True)
        self._drawing: _Drawing | None = None
        self._previews: list[tuple[int, object]] = []    # (commit number, PageScene | eraser path points)
        self._seq = 0
        self._still = QTimer(self, interval=STILL_MS)
        self._still.timeout.connect(self._held)
        self._hold = QTimer(self, singleShot=True, interval=HOLD_MS)
        self._hold.timeout.connect(self._fix_straight)
        self._hold_at: QPointF | None = None    # where the pen last came to rest (item coordinates)
        viewport.widthChanged.connect(self._fit)
        viewport.heightChanged.connect(self._fit)
        pen_tools.activeChanged.connect(self._mode)
        pen_tools.availableChanged.connect(self._mode)
        pen_tools.clearRequested.connect(self._erase_all)
        self._fit()
        self._mode()

    def watch_window(self) -> None:
        """Take the stylus of the window the page is in (now, and when it changes)."""
        self.windowChanged.connect(self._window)
        self._window(self.window())

    def _window(self, window) -> None:
        if window is not None:
            surfaces = _filter(window).surfaces
            if self not in surfaces:
                surfaces.append(self)

    def _fit(self) -> None:
        self.setPosition(QPointF(0, 0))
        self.setSize(self.viewport.size())

    def _mode(self) -> None:
        on = self.tools._active and self.tools._available
        self.setEnabled(on)
        self.setVisible(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if on else Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(on)
        if on:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()          # the app's own cursors (I-beam, link) show through
        if not on and self._drawing is not None:
            self._end(commit=True)

    def set_view(self, view) -> None:
        if self.view is view:
            return
        if self._drawing is not None:
            self._end(commit=False)
        if self.view is not None and shiboken6.isValid(self.view):
            try:
                self.view.penShown.disconnect(self._shown)
            except (RuntimeError, TypeError):
                pass
        self.view = view
        self._previews.clear()
        view.penShown.connect(self._shown)
        self.update()

    def _view_ok(self) -> bool:
        return self.view is not None and shiboken6.isValid(self.view)

    def _erase_all(self) -> None:
        """Erase all: clear the current layer of the page shown (ClearPage)."""
        if self._drawing is not None:
            self._end(commit=True)
        if not self._view_ok():
            return
        refusal = self.view.pen_refusal()
        if refusal:
            _notify(refusal)
            return
        self.view.clear_current_layer()

    # -- samples --
    def _scene(self, pos: QPointF) -> QPointF:
        """A point of this item -> scene coordinates (through the view's tile manager)."""
        return self.view.scene_point(self.mapToItem(self.view, pos))

    def begin(self, pos: QPointF, sample_rest: tuple, eraser: bool, stylus: bool) -> bool:
        """A press at `pos` (this item's coordinates): starts drawing, or tells why not."""
        if not self._view_ok() or not self.tools._active:
            return False
        refusal = self.view.pen_refusal()
        if refusal:
            _notify(refusal)
            return True                     # taken: a pen press never starts typing
        pen = self.tools.pen()
        if self.tools._lasso:
            self._drawing = _Drawing(pen, False, self.view.pen_scale(), stylus, lasso=True)
            self.add(pos, sample_rest)
            return True
        self._drawing = _Drawing(pen, eraser or pen.tool.is_eraser, self.view.pen_scale(), stylus,
                                 right_handed=right_handed(), rotation=self.view.pen_rotation())
        if self._drawing.eraser and not pen.tool.is_eraser:
            eraser = pens.pen_of(self.tools._settings, pens.BY_KEY["eraser"])
            self._drawing.size = eraser.size
        self.add(pos, sample_rest)
        return True

    def add(self, pos: QPointF, rest: tuple = ()) -> None:
        if self._drawing is None:
            return
        p = self._scene(pos)
        if self._drawing.end is not None:
            self._drawing.end = (p.x(), p.y())      # held: the pen drags the line's end
            self.update()
            return
        self._drawing.add((p.x(), p.y(), *rest))
        self._watch_hold(pos)
        self.update()

    def _watch_hold(self, pos: QPointF) -> None:
        """Restart the hold timer when the pen has moved more than HOLD_SLOP_PX since it
        last came to rest (a pen stroke only)."""
        d = self._drawing
        if d is None or d.builder is None:
            return
        if self._hold_at is None:
            self._hold_at = QPointF(pos)            # the press: a pause before drawing is no hold
        elif math.hypot(pos.x() - self._hold_at.x(), pos.y() - self._hold_at.y()) > HOLD_SLOP_PX:
            self._hold_at = QPointF(pos)
            self._hold.start()

    def _fix_straight(self) -> None:
        """The pen was held still: the stroke becomes a straight line from its first
        point to the pen, which then moves only the line's end."""
        d = self._drawing
        if d is None or d.builder is None or not d.can_straighten or not d.samples:
            return
        self._still.stop()
        d.end = (d.samples[-1][0], d.samples[-1][1])
        self.update()

    def _held(self) -> None:
        """The pressed mouse held still: its last sample once more."""
        if self._drawing is not None and self._drawing.samples and self._drawing.end is None:
            self._drawing.add(self._drawing.samples[-1])
            self.update()

    def _end(self, commit: bool = True) -> None:
        drawing, self._drawing = self._drawing, None
        self._still.stop()
        self._hold.stop()
        self._hold_at = None
        if drawing is None:
            return
        if not commit or not self._view_ok() or not drawing.samples:
            self.update()
            return
        if drawing.lasso:
            self.update()
            points = [(sample[0], sample[1]) for sample in drawing.samples]
            if self.view.select_lasso(points):
                self.tools.lassoSelected.emit()
            return
        try:
            line = drawing.line()
        except s.StrokeError as err:
            _notify(str(err))
            line = None
        if line is None:
            if not drawing.eraser:
                _notify("The stroke has too many points to be kept")
            self.update()
            return
        self._seq += 1
        if drawing.area:
            self._previews.append((self._seq, ("area", drawing.samples)))
        elif drawing.eraser:
            self._previews.append((self._seq, ("eraser", drawing.samples, line.unpacked()[0].width / 4)))
        else:
            self._previews.append((self._seq, preview_scene(line.tool, line.color, line.thickness_scale, line.argb,
                                                            line.unpacked())))
        self.view.pen_commit("erase" if drawing.eraser else "draw", line, self._seq)
        self.update()

    def _shown(self, seq: int) -> None:
        """The page is drawn with commit `seq` (and those before it) on it."""
        self._previews = [p for p in self._previews if p[0] > seq]
        self.update()

    # -- the mouse --
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        pressed = self._stylus_press(event)
        if pressed is not None:
            # A stylus press nothing above the page took, which Qt turned into this mouse
            # press: a stylus stroke, its samples from the stylus itself (TabletFilter).
            rest, eraser = pressed
            if not self.begin(event.position(), rest, eraser or alt, stylus=True):
                event.ignore()
                return
            event.accept()
            return
        if not self.begin(event.position(), (), alt, stylus=False):
            event.ignore()
            return
        event.accept()
        if self._drawing is not None:
            self._still.start()

    def _stylus_press(self, event):
        """(pressure and tilt, eraser end) of the stylus press `event` was made from, or
        None for a mouse's own press."""
        device = event.pointingDevice()
        if device is None or device.type() not in (QPointingDevice.DeviceType.Stylus,
                                                   QPointingDevice.DeviceType.Airbrush):
            return None
        window = self.window()
        found = window.findChild(TabletFilter) if window is not None else None
        press = found.last_press if found is not None else None
        if press is None:
            return ((s.MOUSE_PRESSURE, 0.0, 0.0), False)
        return press

    def mouseMoveEvent(self, event) -> None:
        if self._drawing is not None and not self._drawing.stylus:
            self.add(event.position())
            if self._drawing is not None and self._drawing.end is None:
                self._still.start()

    def mouseReleaseEvent(self, event) -> None:
        if self._drawing is not None and not self._drawing.stylus:
            self.add(event.position())
            self._end()

    def mouseUngrabEvent(self) -> None:
        if self._drawing is not None and not self._drawing.stylus:
            self._end()

    # -- the stylus (from TabletFilter) --
    def tablet(self, event) -> bool:
        """A stylus move or release during a stylus stroke of this surface: taken (its
        pressure and tilt), so Qt makes no mouse event of it."""
        kind = event.type()
        if self._drawing is None or not self._drawing.stylus or kind == QEvent.Type.TabletPress:
            return False
        pos = self.mapFromScene(event.position())
        rest = (float(event.pressure()), math.radians(event.xTilt()), math.radians(event.yTilt()))
        self.add(pos, rest)
        if kind == QEvent.Type.TabletRelease:
            self._end()
            self.ungrabMouse()          # Qt makes no mouse release of a stylus release taken here
        return True

    # -- drawing --
    def paint(self, painter: QPainter) -> None:
        if not self._view_ok():
            return
        to_view = self.view.scene_to_view()
        offset = self.mapFromItem(self.view, QPointF(0, 0))
        # Combined with, never replacing, the transform the painter comes with: on a
        # HiDPI screen Qt paints this item into a texture of device pixels and hands
        # over a painter already scaled by the device pixel ratio. Replacing it drew the
        # preview at 1/ratio of its place: up and to the left of the pen.
        painter.translate(offset)
        painter.setTransform(to_view, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for _, preview in self._previews:
            self._paint_one(painter, preview)
        d = self._drawing
        if d is not None:
            if d.lasso:
                self._paint_lasso(painter, d.samples)
            elif d.area:
                self._paint_area(painter, d.samples)
            elif d.eraser:
                width = max(10.0 * d.size, 2.0)
                self._paint_one(painter, ("eraser", d.samples, width))
            elif d.builder.points:
                pen = d.pen
                straight = d.straight()
                points = straight.unpacked() if straight is not None else d.builder.points
                self._paint_one(painter, preview_scene(pen.tool.tool, pen.line_color, pen.size, pen.argb, points))

    @staticmethod
    def _paint_lasso(painter: QPainter, samples) -> None:
        """The lasso so far: a dashed outline, closed back to where it started."""
        if len(samples) < 2:
            return
        path = QPainterPath(QPointF(samples[0][0], samples[0][1]))
        for sample in samples[1:]:
            path.lineTo(sample[0], sample[1])
        path.closeSubpath()
        painter.save()
        painter.setBrush(QColor(LASSO_COLOR.red(), LASSO_COLOR.green(), LASSO_COLOR.blue(), 30))
        pen = QPen(LASSO_COLOR, 0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.restore()

    @staticmethod
    def _paint_area(painter: QPainter, samples) -> None:
        """The area an Erase selection takes, so far: grey, closed back to its start."""
        if len(samples) < 2:
            return
        path = QPainterPath(QPointF(samples[0][0], samples[0][1]))
        for sample in samples[1:]:
            path.lineTo(sample[0], sample[1])
        path.closeSubpath()
        painter.save()
        painter.setBrush(QColor(0, 0, 0, 40))
        pen = QPen(QColor(0, 0, 0, 120), 0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.restore()

    @classmethod
    def _paint_one(cls, painter: QPainter, preview) -> None:
        if isinstance(preview, PageScene):
            painter.save()
            if preview.layers[0].strokes[0].tool.family == "highlighter":
                painter.setOpacity(0.6)         # the page draws it Darken-blended under the ink
            renderer.paint(painter, preview, blend_modes=False)
            painter.restore()
            return
        if preview[0] == "area":
            cls._paint_area(painter, preview[1])
            return
        _, samples, width = preview
        if not samples:
            return
        path = QPainterPath(QPointF(samples[0][0], samples[0][1]))
        for sample in samples[1:]:
            path.lineTo(sample[0], sample[1])
        painter.setPen(QPen(QColor(0, 0, 0, 40), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(path)
        if len(samples) == 1:
            painter.drawPoint(QPointF(samples[0][0], samples[0][1]))


class TabletFilter(QObject):
    """The stylus for the pen surfaces of a window (Qt Quick items get no QTabletEvent of
    their own). A press is left to Qt, which gives it to whatever is on top at that
    point (the pen toolbar's buttons, the page buttons, ...) and, when nothing there
    takes it, makes a mouse press of it that reaches the pen surface: here only its
    pressure, tilt and pen end are noted (`last_press`). The moves and the release of a
    stroke the surface then started are taken here, with their pressure and tilt."""

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.surfaces: list[PenSurface] = []
        self.last_press: tuple | None = None        # ((pressure, tilt x, tilt y), eraser end)

    _TABLET = (QEvent.Type.TabletPress, QEvent.Type.TabletMove, QEvent.Type.TabletRelease)

    def eventFilter(self, obj, event) -> bool:
        """Never in the way of any other event, and never fails one: a stylus event no
        surface takes goes on as Qt would deliver it."""
        try:
            if event.type() not in self._TABLET:
                return False
            if event.type() == QEvent.Type.TabletPress:
                self.last_press = ((float(event.pressure()), math.radians(event.xTilt()), math.radians(event.yTilt())),
                                   event.pointerType() == QPointingDevice.PointerType.Eraser)
                return False
            self.surfaces = [x for x in self.surfaces if shiboken6.isValid(x)]
            for surface in self.surfaces:
                if surface.isVisible() and surface.tablet(event):
                    event.accept()
                    return True
        except RuntimeError:           # a surface or the window going away (shutting down)
            pass
        return False


# -- the toolbar ----------------------------------------------------------------------

TOOLBAR_QML = b"""
import QtQuick
import QtQuick.Controls
import ark.controls as ArkControls

Item {
    id: bar
    visible: penTools.available
    x: 16
    y: Math.max(16, (parent ? parent.height : 0) / 2 - column.height / 2)
    width: row.width
    height: Math.max(column.height, options.visible ? options.height : 0)
    z: 50

    readonly property color frame: "#d9d4cb"
    readonly property color picked: "#e6e1d8"
    readonly property color hover: "#f2efe9"

    Connections {
        target: panel
        ignoreUnknownSignals: true
        function onTextModeChanged() { if (panel.textMode) { penTools.deactivate(); clearPdfText(); } }
        function onSelectionModeChanged() { if (panel.selectionMode) { penTools.deactivate(); clearPdfText(); } }
    }
    Connections {
        target: penTools
        function onActiveChanged() {
            if (penTools.active) clearPdfText();
            if (!penTools.active || !panel) return;
            if (panel.selectionMode) {
                if (viewport && viewport.endItemSelection) viewport.endItemSelection();
                panel.selectionMode = false;
            }
            if (panel.textMode) panel.textMode = false;
        }
    }
    // A PDF / EPUB text selection goes when a tool is taken up (pen, eraser, lasso, text).
    function clearPdfText() {
        if (viewport && viewport.sceneController) viewport.sceneController.clearPdfTextSelection();
    }
    Shortcut {
        sequence: "Escape"
        enabled: penTools.active
        onActivated: penTools.deactivate()
    }
    Connections {
        target: penTools
        function onLassoSelected() { if (panel) panel.selectionMode = true; }
    }

    Row {
        id: row
        spacing: 8

        Rectangle {
            id: column
            objectName: "penToolColumn"
            width: 48
            height: toolList.height + 8
            radius: 6
            color: "#ffffff"
            border.color: bar.frame

            Column {
                id: toolList
                x: 4; y: 4
                spacing: 0
                Repeater {
                    model: penTools.tools
                    delegate: Rectangle {
                        objectName: "penTool_" + modelData.key
                        readonly property bool inHand: penTools.active && !penTools.lasso && penTools.tool === modelData.key
                        width: 40; height: 40; radius: 4
                        color: inHand ? bar.picked : (toolMouse.hovered ? bar.hover : "transparent")
                        border.color: !penTools.active && penTools.tool === modelData.key ? bar.frame : "transparent"
                        ArkControls.Icon {
                            anchors.centerIn: parent
                            source: "qrc:/ark/icons/" + modelData.icon
                            size: 24
                            color: "#000000"
                        }
                        // Pointer handlers take a mouse, a touchpad, a touch screen and a stylus alike
                        // (a MouseArea sees a stylus only through Qt's mouse synthesis).
                        TapHandler {
                            objectName: "tap"
                            gesturePolicy: TapHandler.ReleaseWithinBounds
                            onTapped: penTools.chooseTool(modelData.key)
                        }
                        HoverHandler {
                            id: toolMouse
                        }
                        ToolTip.visible: toolMouse.hovered
                        ToolTip.delay: 600
                        ToolTip.text: modelData.label
                    }
                }
                Rectangle {
                    objectName: "penTool_" + penTools.clearAll.key
                    width: 40; height: 40; radius: 4
                    color: eraseAllMouse.hovered ? bar.hover : "transparent"
                    ArkControls.Icon {
                        anchors.centerIn: parent
                        source: "qrc:/ark/icons/" + penTools.clearAll.icon
                        size: 24
                        color: "#000000"
                    }
                    TapHandler {
                        objectName: "tap"
                        gesturePolicy: TapHandler.ReleaseWithinBounds
                        onTapped: penTools.eraseAll()
                    }
                    HoverHandler {
                        id: eraseAllMouse
                    }
                    ToolTip.visible: eraseAllMouse.hovered
                    ToolTip.delay: 600
                    ToolTip.text: penTools.clearAll.label
                }
                Rectangle {
                    objectName: "penTool_lasso"
                    readonly property bool inHand: penTools.active && penTools.lasso
                    width: 40; height: 40; radius: 4
                    color: inHand ? bar.picked : (lassoMouse.hovered ? bar.hover : "transparent")
                    ArkControls.Icon {
                        anchors.centerIn: parent
                        source: "qrc:/ark/icons/selection_tool"
                        size: 24
                        color: "#000000"
                    }
                    TapHandler {
                        objectName: "tap"
                        gesturePolicy: TapHandler.ReleaseWithinBounds
                        onTapped: penTools.chooseLasso()
                    }
                    HoverHandler {
                        id: lassoMouse
                    }
                    ToolTip.visible: lassoMouse.hovered
                    ToolTip.delay: 600
                    ToolTip.text: "Lasso select"
                }
            }
        }

        Rectangle {
            id: options
            objectName: "penOptions"
            visible: penTools.active && !penTools.lasso && penTools.hasOptions
            width: 3 * 36 + 8
            height: optionColumn.height + 8
            radius: 6
            color: "#ffffff"
            border.color: bar.frame

            Column {
                id: optionColumn
                x: 4; y: 4
                spacing: 4
                Row {
                    Repeater {
                        model: penTools.sizes
                        delegate: Rectangle {
                            objectName: "penSize_" + modelData.size
                            width: 36; height: 36; radius: 4
                            color: penTools.size === modelData.size ? bar.picked
                                   : (sizeMouse.hovered ? bar.hover : "transparent")
                            ArkControls.Icon {
                                anchors.centerIn: parent
                                source: "qrc:/ark/icons/" + modelData.icon
                                size: 22
                                color: "#000000"
                            }
                            // Pointer handlers take a mouse, a touchpad, a touch screen and a stylus alike
                            // (a MouseArea sees a stylus only through Qt's mouse synthesis).
                            TapHandler {
                                objectName: "tap"
                                gesturePolicy: TapHandler.ReleaseWithinBounds
                                onTapped: penTools.setSize(modelData.size)
                            }
                            HoverHandler {
                                id: sizeMouse
                            }
                            ToolTip.visible: sizeMouse.hovered
                            ToolTip.delay: 600
                            ToolTip.text: modelData.label
                        }
                    }
                }
                Rectangle { width: 3 * 36; height: 1; color: bar.frame; visible: !penTools.isEraser }
                Grid {
                    columns: 3
                    visible: !penTools.isEraser
                    Repeater {
                        model: penTools.colors
                        delegate: Item {
                            objectName: "penColor_" + modelData.key
                            width: 36; height: 36
                            Rectangle {
                                anchors.centerIn: parent
                                width: 32; height: 32; radius: 16
                                color: "transparent"
                                border.width: 2
                                border.color: "#000000"
                                visible: penTools.color === modelData.key
                            }
                            Rectangle {
                                anchors.centerIn: parent
                                width: 24; height: 24; radius: 12
                                color: modelData.hex
                                border.width: 1
                                border.color: "#b8b2a7"
                            }
                            // Pointer handlers take a mouse, a touchpad, a touch screen and a stylus alike
                            // (a MouseArea sees a stylus only through Qt's mouse synthesis).
                            TapHandler {
                                objectName: "tap"
                                gesturePolicy: TapHandler.ReleaseWithinBounds
                                onTapped: penTools.setColor(modelData.key)
                            }
                            HoverHandler {
                                id: colorMouse
                            }
                            ToolTip.visible: colorMouse.hovered
                            ToolTip.delay: 600
                            ToolTip.text: modelData.label
                        }
                    }
                }
            }
        }
    }
}
"""


class _Set(QObject):
    """What attach() made for one viewport."""

    def __init__(self, viewport: QQuickItem) -> None:
        super().__init__(viewport)
        self.tools = PenTools(self)
        self.surface = PenSurface(self.tools, viewport)
        self.toolbar: QQuickItem | None = None
        self.context: QQmlContext | None = None


def _panel(viewport: QQuickItem) -> QObject | None:
    """The DocumentView holding the viewport (its textMode and selectionMode)."""
    item = viewport.parentItem()
    while item is not None:
        meta = item.metaObject()
        if meta.indexOfProperty("selectionMode") >= 0 and meta.indexOfProperty("textMode") >= 0:
            return item
        item = item.parentItem()
    return None


def _filter(window) -> TabletFilter:
    found = window.findChild(TabletFilter)
    if found is None:
        found = TabletFilter(window)
        window.installEventFilter(found)
    return found


_sets: list = []                    # every _Set made (weakly: dropped once gone)


def document_closed(doc_id: str) -> None:
    """The document `doc_id` was closed: the tool in hand for it is put down."""
    _sets[:] = [x for x in _sets if shiboken6.isValid(x)]
    for found in _sets:
        node = found.tools._node
        if node is not None and node.id == doc_id:
            found.tools.deactivate()
            found.tools._node = None            # opened again, its settings are read again


def tools_of(viewport: QQuickItem) -> _Set | None:
    return viewport.findChild(_Set) if viewport is not None and shiboken6.isValid(viewport) else None


def attach(view: QQuickItem) -> _Set | None:
    """The pen tools for the DocumentViewport `view` (a SceneView) shows a page in,
    made the first time; None when the view is not in one."""
    viewport = view.parentItem()
    if viewport is None or viewport.metaObject().indexOfProperty("cursorActive") < 0:
        return None
    found = tools_of(viewport)
    if found is None:
        found = _Set(viewport)
        _sets.append(found)
        engine = qmlEngine(view)
        container = viewport.parentItem()
        if engine is not None and container is not None:
            found.context = QQmlContext(engine.rootContext(), found)
            found.context.setContextProperty("penTools", found.tools)
            found.context.setContextProperty("panel", _panel(viewport))
            found.context.setContextProperty("viewport", viewport)
            component = QQmlComponent(engine)
            component.setData(QByteArray(TOOLBAR_QML), QUrl("qrc:/linux/PenToolbar.qml"))
            toolbar = component.create(found.context)
            if toolbar is None:
                log.error("pen toolbar: %s", "; ".join(e.toString() for e in component.errors()))
            else:
                toolbar.setParent(found)
                toolbar.setParentItem(container)
                found.toolbar = toolbar
        found.surface.watch_window()
    found.surface.set_view(view)
    found.tools.set_document(getattr(view._get("document"), "node", None))
    return found
