"""ScreenShareClient (screenshare::ClientFacade) and ScreenSharePainter
(screenshare::Painter) for the official main.qml and ScreenShare.qml, over the Module 7
LiveView service.

main.qml shows ScreenShareAcceptDialog on serverAvailableChanged (a tablet offers a
session), opens the Screen Share window when `active` turns true, and shows
ScreenShareDisconnectDialog on disconnected. ScreenShare.qml feeds framebufferUpdated /
cursorUpdated / rotationChanged to ScreenSharePainter, the QQuickPaintedItem it sizes and
rotates, which draws the framebuffer and the 15 px hover cursor.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Property, QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtQuick import QQuickPaintedItem

from liveview.rfb import CursorMoved, Framebuffer, FramebufferUpdate, Rotated, ServerHello
from liveview.session import (Connected, Connecting, Disconnected, LiveViewService, NoActiveSession,
                              RoomOffered)

from ...workers import emit_from_worker


class ScreenShareClient(QObject):
    activeChanged = Signal()
    retryingChanged = Signal()
    framebufferUpdated = Signal(QRect)
    framebufferSizeChanged = Signal()
    cursorUpdated = Signal(QPoint)
    serverAvailableChanged = Signal()
    rotationChanged = Signal(int)
    scopesChanged = Signal()
    connected = Signal(int)
    disconnected = Signal(int)
    connectionFailed = Signal()
    roomNotFound = Signal()
    _event = Signal(object)

    def __init__(self, service_factory: Callable[[Callable[[object], None]], LiveViewService] | None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._event.connect(self._handle)
        self.service = service_factory(lambda e: emit_from_worker(self, lambda: self._event.emit(e))) \
            if service_factory else None
        self._room: str | None = None
        self._available = False
        self._initialized = False
        self._active = False
        self._rotation = 0
        self.image: QImage | None = None
        self._framebuffer: Framebuffer | None = None
        self.last_reason = ""

    # -- properties --
    @property
    def server_available(self) -> bool:
        return self._available

    def _set_rotation(self, degrees: int) -> None:
        if int(degrees) != self._rotation:
            self._rotation = int(degrees)
            self.rotationChanged.emit(self._rotation)

    active = Property(bool, lambda self: self._active, notify=activeChanged)
    initialized = Property(bool, lambda self: self._initialized, notify=activeChanged)
    retrying = Property(bool, lambda self: False, notify=retryingChanged)
    framebufferWidth = Property(float, lambda self: float(self.image.width() if self.image else 1404),
                                notify=framebufferSizeChanged)
    framebufferHeight = Property(float, lambda self: float(self.image.height() if self.image else 1872),
                                 notify=framebufferSizeChanged)
    rotation = Property(int, lambda self: self._rotation, _set_rotation, notify=rotationChanged)

    # -- QML calls --
    @Slot()
    def acceptConnection(self) -> None:
        if self.service is not None and self._room:
            self.service.accept(self._room)
        self._set_available(False)

    @Slot()
    def declineConnection(self) -> None:
        self._room = None
        self._set_available(False)

    @Slot()
    def joinActiveRoom(self) -> None:
        if self.service is not None:
            self.service.join_active()

    @Slot()
    def stop(self) -> None:
        if self.service is not None:
            self.service.disconnect()

    def start(self) -> None:
        if self.service is not None:
            self.service.start()

    def shutdown(self) -> None:
        if self.service is not None:
            self.service.stop()

    # -- service events (GUI thread) --
    def _set_available(self, available: bool) -> None:
        if available != self._available:
            self._available = available
            self.serverAvailableChanged.emit()

    def _set_state(self, initialized: bool, active: bool) -> None:
        if (initialized, active) != (self._initialized, self._active):
            self._initialized, self._active = initialized, active
            self.activeChanged.emit()

    def _handle(self, event: object) -> None:
        if isinstance(event, RoomOffered):
            if not self._active:
                self._room = event.room_id
                self._set_available(True)
        elif isinstance(event, NoActiveSession):
            self.roomNotFound.emit()
        elif isinstance(event, ServerHello):
            self.image = QImage(event.width, event.height, QImage.Format.Format_RGB16)
            self.image.fill(QColor("white"))
            self._framebuffer = Framebuffer(event.width, event.height, self.image.bits(), self.image.bytesPerLine())
            self.framebufferSizeChanged.emit()
            self._set_state(True, True)
        elif isinstance(event, FramebufferUpdate) and self._framebuffer is not None:
            changed = self._framebuffer.apply(event)
            if changed is not None:
                self.framebufferUpdated.emit(QRect(changed.x, changed.y, changed.w, changed.h))
        elif isinstance(event, CursorMoved):
            self.cursorUpdated.emit(QPoint(event.x, event.y))
        elif isinstance(event, Rotated):
            self._set_rotation(event.degrees)
        elif isinstance(event, Disconnected):
            self.last_reason = event.reason
            self._set_state(False, False)
            if event.reason != "UserInitiated":
                self.disconnected.emit(0)
        elif isinstance(event, Connected):
            self.connected.emit(0)
        elif isinstance(event, Connecting):
            pass


class ScreenSharePainter(QQuickPaintedItem):
    screenShareChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._client: ScreenShareClient | None = None
        self._cursor_color = QColor("#CCF41515")
        self._cursor_size = 15
        self._buffer_height = 1872
        self._cursor: QPoint | None = None
        self.setAntialiasing(True)

    def _set_client(self, client) -> None:
        self._client = client
        self.screenShareChanged.emit()
        self.update()

    screenShare = Property(QObject, lambda self: self._client, _set_client, notify=screenShareChanged)
    cursorColor = Property(QColor, lambda self: self._cursor_color,
                           lambda self, c: (setattr(self, "_cursor_color", QColor(c)), self.update()))
    cursorSize = Property(int, lambda self: self._cursor_size,
                          lambda self, s: (setattr(self, "_cursor_size", int(s)), self.update()))
    bufferHeight = Property(int, lambda self: self._buffer_height,
                            lambda self, h: setattr(self, "_buffer_height", int(h)))

    @Slot(QRect)
    def updateFramebuffer(self, rect: QRect) -> None:
        self.update()

    @Slot(QPoint)
    def moveCursor(self, point: QPoint) -> None:
        self._cursor = point
        self.update()

    def paint(self, painter: QPainter) -> None:
        image = self._client.image if self._client is not None else None
        if image is None or image.isNull():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = QRectF(0, 0, self.width(), self.height())
        painter.drawImage(target, image)
        if self._cursor is not None and 0 <= self._cursor.x() < image.width() and 0 <= self._cursor.y() < image.height():
            sx, sy = self.width() / image.width(), self.height() / image.height()
            centre = QPointF(self._cursor.x() * sx, self._cursor.y() * sy)
            r = self._cursor_size / 2
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._cursor_color)
            painter.drawEllipse(QRectF(centre.x() - r, centre.y() - r, self._cursor_size, self._cursor_size))
