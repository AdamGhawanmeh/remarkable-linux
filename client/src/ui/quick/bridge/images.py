"""Thumbnails for the official PageThumbnail.qml.

PageThumbnail.qml is an Image whose source comes from a C++ ThumbnailProvider element:
    ThumbnailProvider { id: provider; document: ...; page: ...; monochrome: ... }
    source: provider.source
Here the element produces image://rmthumb/<document id>/<page>?v=<index hash>, and the
image provider renders that page with the same pipeline (and on-disk cache) as Module 4's
thumbnails, on its own threads (AsyncImageProvider).
"""

from __future__ import annotations

import logging
from pathlib import Path

import shiboken6

from PySide6.QtCore import Property, QObject, QRect, QRectF, QSize, Qt, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPixmap
from PySide6.QtQml import QQmlEngine
from PySide6.QtQuick import QQuickImageProvider

from ...library import LibrarySource
from ...pages import DocumentPages
from ...workers import EXPECTED_ERRORS, ThumbnailService, _Job
from . import native

log = logging.getLogger("rm.ui.quick.images")


class ThumbnailProvider(QObject):
    """ThumbnailProvider: the thumbnail URL for one page of a document (`source`)."""

    pageChanged = Signal()
    documentChanged = Signal()
    cacheChanged = Signal()
    sourceChanged = Signal()
    autoRotateChanged = Signal()
    monochromeChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._document: QObject | None = None
        self._page = 0
        self._monochrome = False

    def _set_document(self, document: QObject | None) -> None:
        if document is self._document:
            return
        if self._document is not None and shiboken6.isValid(self._document):
            self._document.thumbnailChanged.disconnect(self.sourceChanged)
        self._document = document
        if document is not None:
            document.thumbnailChanged.connect(self.sourceChanged)
        self.documentChanged.emit()
        self.sourceChanged.emit()

    def _set_page(self, page: int) -> None:
        if int(page) != self._page:
            self._page = int(page)
            self.pageChanged.emit()
            self.sourceChanged.emit()

    def _set_monochrome(self, value: bool) -> None:
        if bool(value) != self._monochrome:
            self._monochrome = bool(value)
            self.monochromeChanged.emit()

    document = Property(QObject, lambda self: self._document, _set_document, notify=documentChanged)
    page = Property(int, lambda self: self._page, _set_page, notify=pageChanged)
    monochrome = Property(bool, lambda self: self._monochrome, _set_monochrome, notify=monochromeChanged)

    def _source(self) -> QUrl:
        node = getattr(self._document, "node", None) if self._document is not None else None
        if node is None or node.is_folder:
            return QUrl()
        return QUrl(f"image://rmthumb/{node.id}/{self._page}?v={node.hash[:16]}")

    source = Property(QUrl, _source, notify=sourceChanged)


def _deliver(serial: int, image: QImage) -> None:
    native.library().rm_deliver_image(serial, shiboken6.getCppPointer(image)[0])


class AsyncImageProvider(QObject):
    """An asynchronous image provider (image://<name>/) whose Python render(ident,
    requested) runs on its own threads; install(engine, name) adds it.

    No Python may run on Qt Quick's image reader thread: Qt calls the provider and the
    response there while holding the reader's lock, so a Python override would wait
    for the GIL with that lock held, while the GUI thread, holding the GIL, can wait
    for the lock (QQuickPixmap::load): a deadlock. The provider and response Qt sees are
    C++ (native/arkcontrols/imageprovider.cpp); they queue request() to this object on
    the GUI thread, and the image goes back through rm_deliver_image. After close(),
    requests are answered at once with no image (the engine can outlive the provider's
    threads at exit)."""

    def __init__(self, threads: int = 2) -> None:
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(threads)
        self.closed = False

    def install(self, engine: QQmlEngine, name: str) -> None:
        if not native.library().rm_add_image_provider(shiboken6.getCppPointer(engine)[0], name.encode(),
                                                      shiboken6.getCppPointer(self)[0]):
            raise RuntimeError(f"cannot add image provider {name!r}")

    @Slot(int, str, QSize)
    def request(self, serial: int, ident: str, requested: QSize) -> None:
        if self.closed or not shiboken6.isValid(self.pool):
            _deliver(serial, QImage())
            return
        requested = QSize(requested)

        def job() -> None:
            try:
                image = self.render(ident, requested)
            except Exception:                       # never leave the engine waiting
                log.exception("image %s failed", ident)
                image = QImage()
            _deliver(serial, image)

        self.pool.start(_Job(job))

    def close(self) -> None:
        self.closed = True
        self.pool.waitForDone(30000)

    def render(self, ident: str, requested: QSize) -> QImage:
        raise NotImplementedError


class ThumbnailImageProvider(AsyncImageProvider):
    """image://rmthumb/<document id>/<page>?v=<hash>: a page thumbnail, rendered with
    the same pipeline and on-disk cache as Module 4's."""

    def __init__(self, source: LibrarySource, library, cache_dir: Path) -> None:
        super().__init__()
        self.source = source
        self.library = library
        self.dir = cache_dir / "thumbnails"

    def render(self, ident: str, requested: QSize) -> QImage:
        path, _, _ = ident.partition("?")
        doc_id, _, page = path.partition("/")
        doc = self.library.tree.get(doc_id)
        width = requested.width() if requested.width() > 0 else 328
        image = QImage()
        if doc is not None and not doc.is_folder:
            cached = self.dir / f"{doc.hash}-{width}-p{page or 0}-v{ThumbnailService.VERSION}.png"
            if cached.exists():
                image = QImage(str(cached))
            if image.isNull():
                try:
                    pages = DocumentPages(self.source, doc)
                    try:
                        index = min(max(int(page or 0), 0), max(len(pages.pages) - 1, 0))
                        if pages.pages:
                            frame = pages.content(index).frame()
                            image = pages.render(index, width / frame.width(), crop="clip")
                    finally:
                        pages.close()
                    if not image.isNull():
                        self.dir.mkdir(parents=True, exist_ok=True)
                        image.save(str(cached))
                except EXPECTED_ERRORS as err:
                    log.info("no thumbnail for %s: %s", doc_id, err)
        return image


class DragIconImageProvider(QQuickImageProvider):
    """image://dragicon/<count>[/<width>/<height>]: the picture under the pointer while
    dragging library entries (DropManager.qml), drawn as the app's DragIconImageProvider
    draws it: with a size, one card (two when several entries) and a round count badge;
    without, a 64 px badge at device pixel ratio 2."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, ident: str, size: QSize, requested: QSize) -> QPixmap:
        parts = ident.split("/")
        count = parts[0]
        if len(parts) > 2:
            pixmap = self._cards(count, int(parts[1] or 0), int(parts[2] or 0))
        else:
            pixmap = self._badge(count)
        size.setWidth(pixmap.width())
        size.setHeight(pixmap.height())
        return pixmap

    @staticmethod
    def _badge(count: str) -> QPixmap:
        pixmap = QPixmap(64, 64)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(Qt.GlobalColor.black))
        painter.drawEllipse(1, 1, 30, 30)
        painter.setPen(QColor(Qt.GlobalColor.white))
        painter.drawText(QRect(0, 0, 32, 32), int(Qt.AlignmentFlag.AlignCenter.value), count)
        painter.end()
        return pixmap

    @staticmethod
    def _cards(count: str, width: int, height: int) -> QPixmap:
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        dark = QColor.fromRgbF(0.13, 0.13, 0.13, 0.8)
        painter.setPen(dark)
        painter.setBrush(QBrush(QColor.fromRgbF(1.0, 1.0, 1.0, 0.8)))
        painter.drawRects([QRectF(1.0, 15.0, width - 30, height - 30)])
        if int(count or 0) > 1:
            painter.drawRects([QRectF(5.0, 20.0, width - 30, height - 30)])
            painter.setBrush(QBrush(dark))
            badge = QRectF((width - 25) - 15.0, 5.0, 30.0, 30.0)
            painter.drawRoundedRect(badge, 30.0, 30.0)
            painter.setPen(QColor(Qt.GlobalColor.white))
            painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter.value), count)
        painter.end()
        return pixmap
