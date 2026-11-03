"""Background work for the UI: thumbnails, page renders, sync refreshes and cloud
changes.

Results come back as Qt signals on objects living in the GUI thread, so slots run
in the GUI thread. Every job catches its own errors and reports them; nothing
raises across the thread boundary.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

from core.auth import AuthClient, AuthError, DeviceRevokedError, NotPairedError
from core.models import Document, DocumentTree
from core.sync import SyncClient, SyncError

from .library import LibrarySource
from .export import export_pdf, export_png
from .pages import DocumentPages, cover_index, render_content

log = logging.getLogger("rm.ui.workers")

# Failures a background job reports instead of raising.
EXPECTED_ERRORS = (SyncError, AuthError, OSError, ValueError)


def emit_from_worker(owner: QObject, emit: Callable[[], None]) -> None:
    """Emit a signal from a worker thread unless `owner` has been deleted meanwhile
    (e.g. the window was closed while a thumbnail or export was still running)."""
    if not shiboken6.isValid(owner):
        return
    try:
        emit()
    except RuntimeError:
        if shiboken6.isValid(owner):      # not a deleted-owner race: a real error
            raise


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], None]) -> None:
        super().__init__()
        self.fn = fn
        self.setAutoDelete(True)

    def run(self) -> None:
        self.fn()


class ThumbnailService(QObject):
    """Cover-page thumbnails clipped to the page frame: the 1404 × 1872 page (3:4)
    for notebooks, the PDF page for PDF-backed pages. Views letterbox them into 3:4.

    Cached in memory and on disk under <cache>/thumbnails/, keyed by the document's
    index hash, so a changed document gets a fresh thumbnail automatically.
    """

    ready = Signal(str)          # document id
    failed = Signal(str, str)    # document id, reason

    def __init__(self, source: LibrarySource, cache_dir: Path, width: int = 2 * 164,
                 threads: int = 3, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.dir = cache_dir / "thumbnails"
        self.width = width
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(threads)
        self._images: dict[str, tuple[str, QImage | None]] = {}   # id -> (hash, image|None=failed)
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    # Bump when rendering changes so cached thumbnails are redrawn (v2: page templates).
    VERSION = 2

    def _file(self, doc: Document) -> Path:
        return self.dir / f"{doc.hash}-{self.width}-v{self.VERSION}.png"

    def thumbnail(self, doc: Document) -> QImage | None:
        """The thumbnail if available; otherwise schedule it and return None."""
        with self._lock:
            known = self._images.get(doc.id)
            if known and known[0] == doc.hash:
                return known[1]
            if doc.id in self._pending:
                return None
        path = self._file(doc)
        if path.exists():
            image = QImage(str(path))
            if not image.isNull():
                with self._lock:
                    self._images[doc.id] = (doc.hash, image)
                return image
        with self._lock:
            self._pending.add(doc.id)
        self.pool.start(_Job(lambda: self._render(doc)))
        return None

    def has_failed(self, doc: Document) -> bool:
        with self._lock:
            known = self._images.get(doc.id)
            return bool(known and known[0] == doc.hash and known[1] is None)

    def _render(self, doc: Document) -> None:
        try:
            pages = DocumentPages(self.source, doc)
            try:
                if not pages.pages:
                    raise ValueError("document has no pages")
                index = cover_index(doc, len(pages.pages))
                frame = pages.content(index).frame()
                image = pages.render(index, self.width / frame.width(), crop="clip")
            finally:
                pages.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            image.save(str(self._file(doc)))
            with self._lock:
                self._images[doc.id] = (doc.hash, image)
                self._pending.discard(doc.id)
            emit_from_worker(self, lambda: self.ready.emit(doc.id))
        except EXPECTED_ERRORS as err:
            log.info("no thumbnail for %s: %s", doc.id, err)
            with self._lock:
                self._images[doc.id] = (doc.hash, None)
                self._pending.discard(doc.id)
            emit_from_worker(self, lambda: self.failed.emit(doc.id, str(err)))

    def wait(self, msecs: int = 30000) -> bool:
        return self.pool.waitForDone(msecs)


class PageRenderer(QObject):
    """Renders viewer pages off the GUI thread; stale results are dropped by token."""

    rendered = Signal(int, QImage)      # token, image
    failed = Signal(int, str)           # token, reason

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)

    def request(self, token: int, pages: DocumentPages, index: int, width_px: float) -> None:
        """Render page `index` so its frame (screen page or PDF page) is `width_px` wide;
        ink or text beyond the frame extends the image (scrollable, faithful)."""
        def job():
            try:
                content = pages.content(index)
                scale = width_px / content.frame().width()
                image = render_content(content, scale, crop="page")
            except EXPECTED_ERRORS as err:
                emit_from_worker(self, lambda: self.failed.emit(token, str(err)))
                return
            emit_from_worker(self, lambda: self.rendered.emit(token, image))
        self.pool.start(_Job(job))

    def wait(self, msecs: int = 30000) -> bool:
        return self.pool.waitForDone(msecs)


class ExportCancelled(Exception):
    """Raised inside an export job once Exporter.cancel() was called."""


class Exporter(QObject):
    """Exports a document to PDF, PNG images or .rmdoc in the background, one at a time.

    Every job ends with exactly one of finished, failed or cancelled: an error nobody
    expected (pypdf raises all kinds on unusual PDFs) is reported as a failure too,
    rather than leaving the export running forever.
    """

    progress = Signal(int, int)      # pages done, total
    finished = Signal(str)           # written path
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, source: LibrarySource, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.running = False
        self._cancel = threading.Event()

    def export(self, kind: str, doc: Document, path: str, pages: list[int] | None = None) -> bool:
        """Export `doc` (only the pages at indexes `pages`, for a PDF or PNGs, if given)."""
        if self.running:
            return False
        self.running = True
        cancel = self._cancel = threading.Event()

        def report(done: int, total: int) -> None:
            if cancel.is_set():
                raise ExportCancelled()
            emit_from_worker(self, lambda: self.progress.emit(done, total))

        def job():
            try:
                if cancel.is_set():
                    raise ExportCancelled()
                if kind in ("pdf", "png"):
                    document = DocumentPages(self.source, doc)     # own PDF handle for this thread
                    write = export_pdf if kind == "pdf" else export_png
                    try:
                        result = write(document, path, progress=report, indexes=pages)
                    finally:
                        document.close()
                elif kind == "rmdoc":
                    result = self.source.export_rmdoc(doc, path)
                else:
                    raise ValueError(f"unknown export format {kind!r}")
            except ExportCancelled:
                self.running = False
                emit_from_worker(self, self.cancelled.emit)
                return
            except Exception as err:
                if not isinstance(err, EXPECTED_ERRORS):
                    log.exception("exporting %s to %s", doc.id, path)
                self.running = False
                emit_from_worker(self, lambda: self.failed.emit(f"{type(err).__name__}: {err}"))
                return
            self.running = False
            emit_from_worker(self, lambda: self.finished.emit(str(result)))
        self.pool.start(_Job(job))
        return True

    def cancel(self) -> None:
        """Stop the running export at its next page (a PDF's pages are drawn before
        anything is written, so nothing is left behind)."""
        self._cancel.set()

    def wait(self, msecs: int = 60000) -> bool:
        return self.pool.waitForDone(msecs)


class Mutator(QObject):
    """Runs cloud changes (imports, new folders, renames, moves, favorites, trash) in
    the background, strictly one after another, so the UI never waits on the network.

    A change is a callable `change(sync, progress)` using the SyncClient's write API;
    `progress(done, total)` reports uploaded bytes.
    """

    started = Signal(str)                # what
    progress = Signal(str, int, int)     # what, bytes uploaded, total bytes
    finished = Signal(object, str)       # DocumentTree, message
    failed = Signal(str)

    def __init__(self, source: LibrarySource, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)

    def run(self, what: str, change: Callable[[SyncClient, Callable[[int, int], None]], object],
            done: str, on_done: Callable[[bool], None] | None = None, quiet: bool = False) -> bool:
        """`on_done(ok)`, if given, runs on the GUI thread after finished/failed. A `quiet`
        change (one the user did not ask for, such as saving the reading position) logs its
        failure instead of reporting it."""
        after = on_done or (lambda ok: None)
        if not self.source.writable:
            if not quiet:
                self.failed.emit(f"{what}: not possible offline")
            after(False)
            return False

        def job():
            emit_from_worker(self, lambda: self.started.emit(what))
            try:
                change(self.source.sync, lambda sent, total: emit_from_worker(
                    self, lambda: self.progress.emit(what, sent, total)))
                tree = self.source.sync.tree
            except EXPECTED_ERRORS as err:
                if quiet:
                    log.warning("%s failed: %s", what, err)
                    emit_from_worker(self, lambda: after(False))
                else:
                    emit_from_worker(self, lambda: (self.failed.emit(f"{what} failed: {err}"), after(False)))
                return
            emit_from_worker(self, lambda: (self.finished.emit(tree, done), after(True)))
        self.pool.start(_Job(job))
        return True

    def wait(self, msecs: int = 60000) -> bool:
        return self.pool.waitForDone(msecs)


class Account(QObject):
    """Logs in and out in the background, one after the other (both call the auth host).

    login(code): pair with the one-time code and fetch the first user token
    (AuthClient.login). logout(remote): delete this device on the server (unless
    `remote` is False), forget the tokens, then run `after`, e.g. to clear the library.
    """

    loggedIn = Signal()
    loginFailed = Signal(object)     # the AuthError
    loggedOut = Signal()

    def __init__(self, auth: AuthClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.auth = auth
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)

    def login(self, code: str) -> None:
        def job():
            try:
                self.auth.login(code)
            except AuthError as err:
                emit_from_worker(self, lambda: self.loginFailed.emit(err))
                return
            emit_from_worker(self, self.loggedIn.emit)
        self.pool.start(_Job(job))

    def logout(self, remote: bool = True, after: Callable[[], None] = lambda: None) -> None:
        def job():
            try:
                self.auth.unpair(remote=remote)
                after()
            except EXPECTED_ERRORS as err:      # e.g. a read-only cache: still logged out here
                log.error("logging out: %s", err)
            emit_from_worker(self, self.loggedOut.emit)
        self.pool.start(_Job(job))

    def wait(self, msecs: int = 60000) -> bool:
        return self.pool.waitForDone(msecs)


class Syncer(QObject):
    """Runs LibrarySource.refresh() in the background, one at a time. `unauthorized`
    (instead of `failed`) means the cloud no longer accepts this client: log in again."""

    finished = Signal(object)    # DocumentTree
    failed = Signal(str)
    unauthorized = Signal(str)
    started = Signal()

    def __init__(self, source: LibrarySource, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.running = False

    def refresh(self) -> bool:
        if self.running:
            return False
        self.running = True
        self.started.emit()

        def job():
            try:
                tree: DocumentTree = self.source.refresh()
            except (DeviceRevokedError, NotPairedError) as err:
                self.running = False
                emit_from_worker(self, lambda: self.unauthorized.emit(f"{type(err).__name__}: {err}"))
                return
            except EXPECTED_ERRORS as err:
                self.running = False
                emit_from_worker(self, lambda: self.failed.emit(f"{type(err).__name__}: {err}"))
                return
            self.running = False
            emit_from_worker(self, lambda: self.finished.emit(tree))
        self.pool.start(_Job(job))
        return True

    def wait(self, msecs: int = 60000) -> bool:
        return self.pool.waitForDone(msecs)
