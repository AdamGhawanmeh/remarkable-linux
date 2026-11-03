"""Main window: navigator and viewer in a stack, background sync and cloud changes,
Screen Share, status bar."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QStackedWidget

from core.models import Document, DocumentTree

from .library import LibrarySource
from .liveview import ACCEPT_TEXT, LiveViewController, LiveViewWindow, disconnect_message
from .navigator import Navigator
from .theme import STYLESHEET, Fonts, font
from .viewer import DocumentViewer
from .workers import Mutator, Syncer, ThumbnailService


class MainWindow(QMainWindow):
    def __init__(self, source: LibrarySource, cache_dir: Path, *, mode_note: str = "",
                 liveview: LiveViewController | None = None) -> None:
        super().__init__()
        self.setWindowTitle("reMarkable")
        self.resize(1280, 860)
        self.setMinimumSize(720, 520)
        self.setStyleSheet(STYLESHEET)
        self.source = source
        self.mode_note = mode_note

        self.thumbnails = ThumbnailService(source, cache_dir, parent=self)
        self.navigator = Navigator(self.thumbnails)
        self.viewer = DocumentViewer(source)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.navigator)
        self.stack.addWidget(self.viewer)
        self.setCentralWidget(self.stack)

        self.status = QLabel()
        self.status.setFont(font(Fonts.small))
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().setSizeGripEnabled(False)

        self.syncer = Syncer(source, self)
        self.syncer.started.connect(self._sync_started)
        self.syncer.finished.connect(self._sync_finished)
        self.syncer.failed.connect(self._sync_failed)
        self.navigator.syncRequested.connect(self.sync)
        self.navigator.openDocument.connect(self.open_document)
        self.navigator.set_writable(source.writable)

        self.mutator = Mutator(source, self)
        self.navigator.changeRequested.connect(self.mutator.run)
        self.mutator.started.connect(lambda what: self._set_status(f"{what}…"))
        self.mutator.progress.connect(self._change_progress)
        self.mutator.finished.connect(self._change_finished)
        self.mutator.failed.connect(self._set_status)

        self.liveview = liveview
        self.liveview_window: LiveViewWindow | None = None
        self.navigator.actions.set_screen_share(liveview is not None)
        if liveview is not None:
            self.navigator.actions.screenShareClicked.connect(self.join_screen_share)
            liveview.roomOffered.connect(self._room_offered)
            liveview.connecting.connect(self._screen_share_connecting)
            liveview.connected.connect(lambda: self._set_status("Screen Share connected"))
            liveview.streamEvent.connect(self._screen_share_event)
            liveview.noActiveSession.connect(self._no_active_session)
            liveview.disconnected.connect(self._screen_share_ended)
        self.viewer.closed.connect(lambda: self.stack.setCurrentWidget(self.navigator))
        self.viewer.exportStatus.connect(self._set_status)

    # -- library -------------------------------------------------------------------

    def start(self, sync: bool = True) -> None:
        """Show the cached library immediately, then (optionally) sync in the background."""
        tree = self.source.load_cached()
        self.navigator.set_tree(tree)
        self.navigator.view.setFocus()
        self._set_status(f"{self._summary(tree)} from cache" if len(tree) else "No cached library yet")
        if sync:
            self.sync()

    def sync(self) -> None:
        self.syncer.refresh()

    def _sync_started(self) -> None:
        self.navigator.actions.set_syncing(True)
        self.navigator.set_loading(True)
        self._set_status("Syncing…")

    def _sync_finished(self, tree: DocumentTree) -> None:
        self.navigator.actions.set_syncing(False)
        self.navigator.loading = False
        self.navigator.set_tree(tree)
        self._set_status(f"{self._summary(tree)} · synced {datetime.now():%H:%M}")

    def _sync_failed(self, message: str) -> None:
        self.navigator.actions.set_syncing(False)
        self.navigator.set_loading(False)
        self._set_status(f"Sync failed ({message}); showing the cached library")

    def _change_progress(self, what: str, sent: int, total: int) -> None:
        if total:
            self._set_status(f"{what}… {100 * sent // total}%")

    def _change_finished(self, tree: DocumentTree, message: str) -> None:
        self.navigator.set_tree(tree)
        self._set_status(message)

    @staticmethod
    def _summary(tree: DocumentTree) -> str:
        return f"{len(tree.documents())} documents, {len(tree.folders())} folders"

    def _set_status(self, text: str) -> None:
        self.status.setText(f"{self.mode_note} · {text}" if self.mode_note else text)

    # -- documents -----------------------------------------------------------------

    def open_document(self, doc: Document) -> None:
        self.stack.setCurrentWidget(self.viewer)
        self.viewer.open(doc)

    # -- Screen Share ------------------------------------------------------------------

    def ask_accept_screen_share(self) -> bool:
        """ScreenShareAcceptDialog.qml (separate so tests can answer it)."""
        box = QMessageBox(QMessageBox.Icon.Question, "Screen Share", ACCEPT_TEXT, parent=self)
        box.addButton("Decline", QMessageBox.ButtonRole.RejectRole)
        accept = box.addButton("Accept", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(accept)
        box.exec()
        return box.clickedButton() is accept

    def show_screen_share_ended(self, text: str) -> None:
        """ScreenShareDisconnectDialog.qml, without blocking."""
        box = QMessageBox(QMessageBox.Icon.Information, "Screen Share is no longer active", text, parent=self)
        box.addButton("Close", QMessageBox.ButtonRole.AcceptRole)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def join_screen_share(self) -> None:
        self.liveview.join_active()

    def _room_offered(self, room_id: str) -> None:
        if self.liveview_window is None and self.ask_accept_screen_share():
            self.liveview.accept(room_id)

    def _screen_share_connecting(self) -> None:
        self.navigator.actions.set_screen_share(True, active=True)
        self._set_status("Screen Share is connecting")
        if self.liveview_window is None:
            self.liveview_window = LiveViewWindow()
            self.liveview_window.closed.connect(self._screen_share_window_closed)
            self.liveview_window.show()

    def _screen_share_event(self, event: object) -> None:
        if self.liveview_window is not None:
            self.liveview_window.on_event(event)

    def _screen_share_window_closed(self) -> None:
        self.liveview_window = None
        self.liveview.disconnect()

    def _close_screen_share_window(self) -> None:
        self.navigator.actions.set_screen_share(True, active=False)
        window, self.liveview_window = self.liveview_window, None
        if window is not None:
            window.closed.disconnect(self._screen_share_window_closed)
            window.close()
            window.deleteLater()

    def _no_active_session(self) -> None:
        self._close_screen_share_window()
        self._set_status("No active Screen Share sessions found")

    def _screen_share_ended(self, event) -> None:
        had_window = self.liveview_window is not None
        self._close_screen_share_window()
        message = disconnect_message(event)
        self._set_status(message)
        if had_window and event.reason != "UserInitiated":
            self.show_screen_share_ended(message)

    def closeEvent(self, event) -> None:
        if self.liveview is not None:
            self._close_screen_share_window()
            self.liveview.stop()
        self.mutator.wait(120000)         # never abandon an upload half-committed
        self.syncer.wait(5000)
        self.thumbnails.wait(5000)
        self.viewer.renderer.wait(5000)
        self.viewer.exporter.wait(60000)
        super().closeEvent(event)
