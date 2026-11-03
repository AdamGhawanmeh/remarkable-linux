"""DocumentImporter (xofm::libs::library::DocumentImporter): files dropped on the window
or picked in FileImportDialog become cloud documents (Module 6's SyncClient.import_file),
one upload at a time, in order."""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QUrl, Signal, Slot

from core.models import ROOT_ID
from core.sync import IMPORT_TYPES

log = logging.getLogger("rm.ui.quick.importer")


class DocumentImporter(QObject):
    imported = Signal(QObject, QObject, QUrl)
    failed = Signal(QUrl)
    started = Signal(str, QUrl)
    finished = Signal(QUrl)

    def __init__(self, submit: Callable[[str, Callable, str], None] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.submit = submit
        self._queue: deque[tuple[QUrl, str]] = deque()
        self._busy = False

    @Slot(QUrl, result=bool)
    def isSupportedFormat(self, url: QUrl) -> bool:
        path = QUrl(url).toLocalFile() or QUrl(url).toString()
        return Path(path).suffix.lower() in IMPORT_TYPES

    @Slot(QObject, result=bool)
    def containsValidDocument(self, drag_event: QObject) -> bool:
        urls = drag_event.property("urls") if drag_event is not None else None
        return any(self.isSupportedFormat(QUrl(u)) for u in urls or [])

    @Slot("QList<QUrl>", str)
    def importFromUrls(self, urls: list, parent_id: str) -> None:
        for url in urls:
            url = QUrl(url)
            if self.isSupportedFormat(url):
                self._queue.append((url, parent_id or ROOT_ID))
            else:
                self.failed.emit(url)
        self.checkQueue()

    @Slot()
    def checkQueue(self) -> None:
        if self._busy or not self._queue or self.submit is None:
            return
        url, parent_id = self._queue.popleft()
        self.doImport(url, "", parent_id)

    @Slot(QUrl, str)
    @Slot(QUrl, str, str)
    def doImport(self, url: QUrl, visual_name: str, parent_id: str = ROOT_ID) -> None:
        path = url.toLocalFile()
        name = visual_name or Path(path).name
        self._busy = True
        self.started.emit(name, url)

        self.submit(f"Uploading {name}",
                    lambda sync, progress: sync.import_file(path, parent_id or ROOT_ID, progress=progress),
                    f"Imported {name}", lambda ok: self._done(url, ok))

    def _done(self, url: QUrl, ok: bool) -> None:
        self._busy = False
        (self.finished if ok else self.failed).emit(url)
        self.checkQueue()
