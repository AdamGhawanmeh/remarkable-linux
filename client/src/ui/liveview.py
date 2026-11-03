"""Screen Share UI: the controller between the session service and Qt, and the window.

Mirrors qml/client/desktop/ScreenShare.qml: the tablet's framebuffer fills the window,
keeping its aspect, rotated by -rotation; a 15 px #CCF41515 dot marks the pen hovering
over the screen; "Screen Share is connecting" shows until the first frame. The accept
and ended dialogs use ScreenShareAcceptDialog.qml / ScreenShareDisconnectDialog.qml texts.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import QWidget

from liveview.rfb import CursorMoved, Framebuffer, FramebufferUpdate, Rotated, ServerHello
from liveview.session import (Connected, Connecting, Disconnected, LiveViewService, NoActiveSession,
                              NotificationsState, RoomOffered)

from .theme import Colors, Fonts, font
from .workers import emit_from_worker

CURSOR_COLOR = "#CCF41515"
CURSOR_SIZE = 15
ACCEPT_TEXT = ("Accept the connection from your reMarkable paper tablet to start sharing, "
               "collaborating and presenting on your computer screen.")
DISCONNECT_REASONS = {
    "PingTimeout": "the tablet stopped responding",
    "ServerShutdown": "sharing was stopped on the tablet",
    "WebRtcFailure": "the connection was lost",
    "ProtocolError": "the tablet sent something unexpected",
    "NegotiationTimeout": "the tablet did not connect",
}


class LiveViewController(QObject):
    """Owns the LiveViewService; re-emits its events as Qt signals in the GUI thread."""

    roomOffered = Signal(str)            # room id
    connecting = Signal()
    connected = Signal()
    streamEvent = Signal(object)         # ServerHello | FramebufferUpdate | CursorMoved | Rotated
    disconnected = Signal(object)        # Disconnected
    noActiveSession = Signal()
    notificationsChanged = Signal(bool)

    def __init__(self, service_factory: Callable[[Callable[[object], None]], LiveViewService],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service = service_factory(lambda event: emit_from_worker(self, lambda: self._route(event)))

    def _route(self, event: object) -> None:          # on the service thread; signals are queued
        if isinstance(event, RoomOffered):
            self.roomOffered.emit(event.room_id)
        elif isinstance(event, Connecting):
            self.connecting.emit()
        elif isinstance(event, Connected):
            self.connected.emit()
        elif isinstance(event, Disconnected):
            self.disconnected.emit(event)
        elif isinstance(event, NoActiveSession):
            self.noActiveSession.emit()
        elif isinstance(event, NotificationsState):
            self.notificationsChanged.emit(event.connected)
        else:
            self.streamEvent.emit(event)

    def start(self) -> None:
        self.service.start()

    def stop(self) -> None:
        self.service.stop()

    def join_active(self) -> None:
        self.service.join_active()

    def accept(self, room_id: str) -> None:
        self.service.accept(room_id)

    def disconnect(self) -> None:
        self.service.disconnect()


class LiveViewWindow(QWidget):
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Screen Share")
        self.resize(900, 1100)
        self.image: QImage | None = None
        self.framebuffer: Framebuffer | None = None
        self.rotation = 0
        self.cursor: tuple[int, int] | None = None

    # -- stream ----------------------------------------------------------------------

    def on_event(self, event: object) -> None:
        if isinstance(event, ServerHello):
            self.image = QImage(event.width, event.height, QImage.Format.Format_RGB16)
            self.image.fill(QColor("white"))
            self.framebuffer = Framebuffer(event.width, event.height, self.image.bits(),
                                           self.image.bytesPerLine())
        elif isinstance(event, FramebufferUpdate) and self.framebuffer is not None:
            if self.framebuffer.apply(event) is None:
                return
        elif isinstance(event, CursorMoved):
            self.cursor = (event.x, event.y)
        elif isinstance(event, Rotated):
            self.rotation = event.degrees
        else:
            return
        self.update()

    # -- geometry ----------------------------------------------------------------------

    def image_transform(self) -> QTransform:
        """Image pixels → widget: fit (aspect kept), centred, rotated by -rotation."""
        w, h = self.image.width(), self.image.height()
        if self.rotation % 180:
            w, h = h, w
        scale = min(self.width() / w, self.height() / h)
        t = QTransform()
        t.translate(self.width() / 2, self.height() / 2)
        t.rotate(-self.rotation)
        t.scale(scale, scale)
        t.translate(-self.image.width() / 2, -self.image.height() / 2)
        return t

    # -- painting ----------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(Colors.dark if self.isFullScreen() else Colors.light))
        if self.image is None:
            p.setPen(QColor(Colors.text))
            p.setFont(font(Fonts.large))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Screen Share is connecting")
            return
        transform = self.image_transform()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setTransform(transform)
        p.drawImage(QPointF(0, 0), self.image)
        p.resetTransform()
        if self.cursor is not None:
            x, y = self.cursor
            if 0 <= x < self.image.width() and 0 <= y < self.image.height():
                centre = transform.map(QPointF(x, y))
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(CURSOR_COLOR))
                r = CURSOR_SIZE / 2
                p.drawEllipse(QRectF(centre.x() - r, centre.y() - r, CURSOR_SIZE, CURSOR_SIZE))

    # -- window --------------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        elif event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)


def disconnect_message(event: Disconnected) -> str:
    why = DISCONNECT_REASONS.get(event.reason)
    return f"Screen Share ended: {why}" if why else "Screen Share ended"
