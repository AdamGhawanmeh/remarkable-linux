"""DocumentExporter: what PlatformHelpers.exportFile starts (ExportDialog.qml's
performExport, from Export... and Export as rM file): documents written to PDF, with
their ink and typed text over the original pages (ui/export.py), to PNG or SVG pages
(one file per page, in a folder named after the document), or to a .rmdoc archive,
in the background and one after another (workers.Exporter).

ExportDialog hands over a file (one document as PDF: its save dialog) or a folder (several
documents, or a .rmdoc: its folder dialog), which gets one file per document named after
it. While a document is exported its entry reads Entry.Exporting with exportProgress, as
the app's do: the grid shows the progress and an x that calls abortExport().
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject

from core.models import Document, unique_name

from ...export import export_file_name
from ...library import LibrarySource
from ...workers import Exporter

log = logging.getLogger("rm.ui.quick.exporter")

SUFFIXES = {"pdf": ".pdf", "rmdoc": ".rmdoc", "png": "", "svg": ""}     # png, svg: a folder of pages
FOLDER_KINDS = ("png", "svg")


def export_target(target: str | Path, name: str, kind: str) -> Path:
    """The file to write for document `name`: inside `target` if it is a folder (named
    after the document, never over an existing file), else `target` itself with the
    format's suffix. PNG and SVG pages go into a new folder named after the document,
    inside the folder ExportDialog chose ("Place folder here")."""
    target, suffix = Path(target), SUFFIXES[kind]
    if kind in FOLDER_KINDS:
        if not target.is_dir():
            raise FileNotFoundError(f"no such folder: {target}")
        base = export_file_name(name, "")
        taken = {p.name for p in target.iterdir()}
        return target / unique_name(base, taken)
    if target.is_dir():
        base = Path(export_file_name(name, suffix)).stem
        taken = {p.stem for p in target.glob(f"*{suffix}")}
        return target / f"{unique_name(base, taken)}{suffix}"
    if target.suffix.lower() != suffix:
        target = target.with_name(target.name + suffix)
    if not target.parent.is_dir():
        raise FileNotFoundError(f"no such folder: {target.parent}")
    return target


@dataclass
class _Request:
    doc_id: str
    kind: str
    path: Path
    pages: list[int] | None


class DocumentExporter(QObject):
    def __init__(self, source: LibrarySource, library, notify: Callable[[str], None],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.notify = notify                  # a message for the app's notification
        self.worker = Exporter(source, self)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        self._queue: deque[_Request] = deque()
        self._current: _Request | None = None

    def _name(self, doc_id: str) -> str:
        node = self.library.tree.get(doc_id)
        return node.name if node is not None else doc_id

    def export(self, target: str, document_id: str, kind: str = "pdf", pages: list[int] | None = None) -> bool:
        """Queue an export; False (with a notification) if it cannot start."""
        node = self.library.tree.get(document_id)
        if not isinstance(node, Document):
            self.notify("Unable to export: the document is no longer in your library")
            return False
        try:
            path = export_target(target, node.name, kind)
        except (OSError, ValueError, KeyError) as err:
            self.notify(f"Unable to export \"{node.name}\": {err}")
            return False
        pages = sorted({int(p) for p in pages}) if pages and kind != "rmdoc" else None
        self._queue.append(_Request(document_id, kind, path, pages))
        log.info("exporting %s as %s to %s", document_id, kind, path)
        self.library.set_export_progress(document_id, 0.0)
        self._next()
        return True

    def abort(self, document_id: str) -> None:
        """EntryWrapper.abortExport: drop the document's queued exports and stop its
        running one."""
        self._queue = deque(r for r in self._queue if r.doc_id != document_id)
        if self._current is not None and self._current.doc_id == document_id:
            self.worker.cancel()
        else:
            self.library.set_export_progress(document_id, None, ok=False)

    def wait(self, msecs: int = 60000) -> bool:
        return self.worker.wait(msecs)

    def _next(self) -> None:
        while self._current is None and self._queue:
            request = self._queue.popleft()
            node = self.library.tree.get(request.doc_id)
            if not isinstance(node, Document):          # deleted while queued
                self._settle(request, ok=False)
                continue
            self._current = request
            if not self.worker.export(request.kind, node, str(request.path), request.pages):
                self._current = None                    # cannot happen: one job at a time
                self._settle(request, ok=False)

    def _settle(self, request: _Request, ok: bool) -> None:
        if not any(r.doc_id == request.doc_id for r in self._queue):
            self.library.set_export_progress(request.doc_id, None, ok=ok)

    def _progress(self, done: int, total: int) -> None:
        if self._current is not None and total > 0:
            self.library.set_export_progress(self._current.doc_id, done / total)

    def _end(self, ok: bool, message: str | None) -> None:
        request, self._current = self._current, None
        if request is not None:
            self._settle(request, ok)
        if message:
            self.notify(message)
        self._next()

    def _finished(self, path: str) -> None:
        log.info("exported %s", path)
        self._end(True, f"Exported to {path}")

    def _failed(self, error: str) -> None:
        name = self._name(self._current.doc_id) if self._current is not None else ""
        log.warning("export of %s failed: %s", name, error)
        self._end(False, f"Unable to export \"{name}\": {error}")

    def _cancelled(self) -> None:
        self._end(False, None)
