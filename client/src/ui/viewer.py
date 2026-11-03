"""Document viewer: one page at a time, fit to width, scrolling through its full height.

Layout follows qml/client/desktop/document: a header with Back and the document
title, the page on the light background, and a page-change pill
(chevron_left · "n / N" · chevron_right) below it. Opens at the last opened page.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QKeySequence, QPainter, QPaintEvent, QPen, QShortcut
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from core.models import Document

from .export import PDF_FILTER, RMDOC_FILTER, export_file_name
from .library import LibrarySource, start_page
from .navigator import tool_button
from .pages import DocumentPages
from .theme import Colors, Fonts, Metrics, font
from .workers import EXPECTED_ERRORS, Exporter, PageRenderer, _Job, emit_from_worker

log = logging.getLogger("rm.ui.viewer")

ZOOM_LEVELS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)
PAGE_MARGIN = 24


def text_button(text: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tooltip)
    button.setFont(font(Fonts.title, QFont.Weight.Medium))
    button.setFixedSize(Metrics.icon_medium + 12, Metrics.icon_medium + 12)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class PageCanvas(QWidget):
    """Shows the rendered page (white sheet with a gray3 outline) or a message."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image: QImage | None = None
        self.message = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_image(self, image: QImage | None, message: str = "") -> None:
        self.image, self.message = image, message
        self.updateGeometry()
        self.adjustSize()
        self.update()

    def page_size(self) -> QSize:
        if self.image is None:
            return QSize(0, 0)
        ratio = self.image.devicePixelRatio()
        return QSize(round(self.image.width() / ratio), round(self.image.height() / ratio))

    def sizeHint(self) -> QSize:
        size = self.page_size()
        return QSize(size.width() + 2 * PAGE_MARGIN, size.height() + 2 * PAGE_MARGIN)

    def page_rect(self) -> QRect:
        size = self.page_size()
        x = max(PAGE_MARGIN, (self.width() - size.width()) // 2)
        return QRect(x, PAGE_MARGIN, size.width(), size.height())

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(Colors.light))
        if self.image is not None:
            rect = self.page_rect()
            p.drawImage(rect, self.image)
            p.setPen(QPen(QColor(Colors.gray3), 1))
            p.drawRect(rect.adjusted(-1, -1, 0, 0))
        if self.message:
            p.setPen(QColor(Colors.text_secondary))
            p.setFont(font(Fonts.large))
            p.drawText(self.rect().adjusted(0, 0, 0, -self.rect().height() // 3),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)


class DocumentViewer(QWidget):
    closed = Signal()
    pageChanged = Signal(int)
    exportStatus = Signal(str)
    _opened = Signal(int, object, str)     # token, DocumentPages | None, error

    def __init__(self, source: LibrarySource, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.doc: Document | None = None
        self.pages: DocumentPages | None = None
        self.index = 0
        self.zoom = 1.0
        self._token = 0
        self._open_pool = QThreadPool(self)
        self._open_pool.setMaxThreadCount(1)
        self.renderer = PageRenderer(self)
        self.renderer.rendered.connect(self._on_rendered)
        self.renderer.failed.connect(self._on_failed)
        self._opened.connect(self._on_opened)
        self.exporter = Exporter(source, self)
        self.exporter.progress.connect(
            lambda done, total: self.exportStatus.emit(f"Exporting page {done} of {total}…"))
        self.exporter.finished.connect(self._export_finished)
        self.exporter.failed.connect(self._export_failed)
        self.export_dir = Path.home()
        self._resize_timer = QTimer(self, singleShot=True, interval=150)
        self._resize_timer.timeout.connect(self._render)

        # header (PageActionHeader.qml: Back, title)
        self.back = tool_button("chevron_left", "Back to library (Esc)", "Back")
        self.back.clicked.connect(self.close_document)
        self.title = QLabel()
        self.title.setFont(font(Fonts.large, QFont.Weight.Medium))
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The Ark icon set has no minus icon, so both zoom steps are text buttons.
        self.zoom_out = text_button("−", "Zoom out (Ctrl+-)")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setFont(font(Fonts.medium))
        self.zoom_in = text_button("+", "Zoom in (Ctrl++)")
        self.fit = tool_button("fit_to_width", "Fit to width (Ctrl+0)")
        self.zoom_out.clicked.connect(lambda: self.step_zoom(-1))
        self.zoom_in.clicked.connect(lambda: self.step_zoom(1))
        self.fit.clicked.connect(lambda: self.set_zoom(1.0))
        # PageActionHeader.qml: Export (icon "export")
        self.export_button = tool_button("export", "Export as PDF or reMarkable archive", "Export")
        self.export_button.clicked.connect(self.export_document)
        header = QHBoxLayout()
        header.setContentsMargins(Metrics.content_margin, 0, Metrics.content_margin, 0)
        header.addWidget(self.back)
        header.addWidget(self.title, 1)
        for w in (self.export_button, self.zoom_out, self.zoom_label, self.zoom_in, self.fit):
            header.addWidget(w)
        header_widget = QWidget()
        header_widget.setFixedHeight(Metrics.header_height)
        header_widget.setLayout(header)
        # A separate 1 px divider: a CSS border on the header would be painted over by
        # its children's backgrounds.
        divider = QWidget()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {Colors.divider};")

        # page
        self.canvas = PageCanvas()
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        # page-change pill (DocumentView.qml: light fill, gray3 border, chevrons)
        self.prev = tool_button("chevron_left", "Previous page (←)")
        self.next = tool_button("chevron_right", "Next page (→)")
        self.page_label = QLabel()
        self.page_label.setFont(font(Fonts.medium, QFont.Weight.Medium))
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setMinimumWidth(70)
        self.prev.clicked.connect(lambda: self.go_to(self.index - 1))
        self.next.clicked.connect(lambda: self.go_to(self.index + 1))
        pill = QWidget()
        pill.setObjectName("pill")
        pill.setStyleSheet(f"#pill {{ background: {Colors.light}; border: 1px solid {Colors.gray3}; "
                           f"border-radius: {Metrics.radius * 2}px; }}")
        pill_row = QHBoxLayout(pill)
        pill_row.setContentsMargins(4, 2, 4, 2)
        for w in (self.prev, self.page_label, self.next):
            pill_row.addWidget(w)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, Metrics.base_margin, 0, Metrics.base_margin)
        footer.addStretch(1)
        footer.addWidget(pill)
        footer.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header_widget)
        layout.addWidget(divider)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(footer)

        for seq, fn in ((QKeySequence.StandardKey.ZoomIn, lambda: self.step_zoom(1)),
                        (QKeySequence.StandardKey.ZoomOut, lambda: self.step_zoom(-1)),
                        (QKeySequence("Ctrl+0"), lambda: self.set_zoom(1.0))):
            QShortcut(seq, self, fn)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._update_controls()

    # -- opening -------------------------------------------------------------------

    def open(self, doc: Document) -> None:
        self._token += 1
        token = self._token
        self._close_pages()
        self.doc = doc
        self.title.setText(doc.name)
        self.index = 0
        self.canvas.set_image(None, "Opening…")
        self._update_controls()
        source = self.source

        def job():
            try:
                pages = DocumentPages(source, doc)
            except EXPECTED_ERRORS as err:
                emit_from_worker(self, lambda: self._opened.emit(token, None, f"{type(err).__name__}: {err}"))
                return
            emit_from_worker(self, lambda: self._opened.emit(token, pages, ""))
        self._open_pool.start(_Job(job))
        self.setFocus()

    def _on_opened(self, token: int, pages: DocumentPages | None, error: str) -> None:
        if token != self._token:
            if pages is not None:
                pages.close()
            return
        if pages is None:
            self.canvas.set_image(None, f"Could not open this document.\n{error}")
            return
        self.pages = pages
        if not pages.pages:
            self.canvas.set_image(None, "This document has no pages.")
            self._update_controls()
            return
        self.go_to(start_page(self.doc, len(pages.pages)))

    def close_document(self) -> None:
        self._token += 1
        self._close_pages()
        self.doc = None
        self.closed.emit()

    def _close_pages(self) -> None:
        if self.pages is not None:
            self.renderer.wait()
            self.pages.close()
            self.pages = None

    # -- pages ---------------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self.pages.pages) if self.pages else 0

    def go_to(self, index: int) -> None:
        if not self.pages or not 0 <= index < self.page_count:
            return
        self.index = index
        self._update_controls()
        self.scroll.verticalScrollBar().setValue(0)
        self._render()
        self.pageChanged.emit(index)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = zoom
        self._update_controls()
        self._render()

    def step_zoom(self, direction: int) -> None:
        levels = ZOOM_LEVELS
        current = min(range(len(levels)), key=lambda i: abs(levels[i] - self.zoom))
        self.set_zoom(levels[max(0, min(len(levels) - 1, current + direction))])

    def _fit_width(self) -> float:
        return max(100, self.scroll.viewport().width() - 2 * PAGE_MARGIN)

    def _render(self) -> None:
        if not self.pages or not self.page_count:
            return
        self._token += 1
        ratio = self.devicePixelRatioF() or 1.0
        self._render_ratio = ratio
        self.renderer.request(self._token, self.pages, self.index, self._fit_width() * self.zoom * ratio)

    def _on_rendered(self, token: int, image: QImage) -> None:
        if token != self._token:
            return
        image.setDevicePixelRatio(self._render_ratio)
        self.canvas.set_image(image)

    def _on_failed(self, token: int, error: str) -> None:
        if token == self._token:
            self.canvas.set_image(None, f"Could not render this page.\n{error}")

    def _update_controls(self) -> None:
        n = self.page_count
        self.page_label.setText(f"{self.index + 1} / {n}" if n else "–")
        self.prev.setEnabled(n > 0 and self.index > 0)
        self.next.setEnabled(n > 0 and self.index < n - 1)
        self.zoom_label.setText(f"{round(self.zoom * 100)}%")
        self.zoom_out.setEnabled(self.zoom > ZOOM_LEVELS[0])
        self.zoom_in.setEnabled(self.zoom < ZOOM_LEVELS[-1])

    # -- export --------------------------------------------------------------------

    def ask_export_path(self, doc: Document) -> tuple[str, str] | None:
        """Native save dialog; returns (path, "pdf" | "rmdoc") or None if cancelled."""
        start = str(self.export_dir / export_file_name(doc.name, ".pdf"))
        path, selected = QFileDialog.getSaveFileName(self, "Export", start, f"{PDF_FILTER};;{RMDOC_FILTER}")
        if not path:
            return None
        kind = "rmdoc" if selected == RMDOC_FILTER or path.lower().endswith(".rmdoc") else "pdf"
        suffix = f".{kind}"
        if not path.lower().endswith(suffix):
            path += suffix
        return path, kind

    def export_document(self) -> None:
        if self.doc is None or self.exporter.running:
            return
        choice = self.ask_export_path(self.doc)
        if not choice:
            return
        path, kind = choice
        self.export_dir = Path(path).parent
        self.export_button.setEnabled(False)
        self.exportStatus.emit(f"Exporting “{self.doc.name}”…")
        self.exporter.export(kind, self.doc, path)

    def _export_finished(self, path: str) -> None:
        self.export_button.setEnabled(True)
        self.exportStatus.emit(f"Exported to {path}")

    def _export_failed(self, error: str) -> None:
        self.export_button.setEnabled(True)
        self.exportStatus.emit(f"Export failed ({error})")

    # -- events --------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.pages:
            self._resize_timer.start()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self.go_to(self.index + 1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.go_to(self.index - 1)
        elif key == Qt.Key.Key_Home:
            self.go_to(0)
        elif key == Qt.Key.Key_End:
            self.go_to(self.page_count - 1)
        elif key == Qt.Key.Key_Escape:
            self.close_document()
        else:
            super().keyPressEvent(event)
