"""Composite page rendering: PDF/EPUB background + strokes + typed text.

Safe to call from worker threads: everything renders into a QImage, and each job
opens its own QPdfDocument (QtPdf serialises PDFium access internally).

PDF placement: the PDF page is drawn at physical size on the 226 DPI screen grid
(226/72 page pixels per PDF point), centred on the page's vertical centre line and
top-aligned. An A4 page is therefore 1868 px wide, wider than the 1404 px screen.
Verified against real annotated A4 and US Letter PDFs (handwriting lands in the form
boxes); fit-to-page and fit-width placements visibly do not (ARCHITECTURE.md §5.2).

Page frame: the area a page "is": the PDF page for PDF-backed pages, else the
1404 × 1872 screen page. Thumbnails clip to it; the viewer shows the frame plus any
ink or text beyond it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import shiboken6
from PySide6.QtCore import QPoint, QRectF, QSize, QSizeF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPdf import QPdfDocument

from core.models import Document
from core.sync import SyncError
from viewer.rm_parser import PAGE_HEIGHT, PAGE_WIDTH, PageScene
from viewer.renderer import paint, qt_measure, render_bounds
from viewer.templates import Template, default_library, paint_template

from .library import LibrarySource, PageRef, document_pages

log = logging.getLogger("rm.ui.pages")

PAPER = "#FFFFFF"
PDF_PX_PER_PT = 226 / 72


class PdfFile:
    """A PDF opened from a local file."""

    def __init__(self, path: str | Path) -> None:
        self.doc = QPdfDocument()
        error = self.doc.load(str(path))
        if error != QPdfDocument.Error.None_ or self.doc.status() != QPdfDocument.Status.Ready:
            raise ValueError(f"cannot open PDF {path}: {error}")

    @property
    def page_count(self) -> int:
        return self.doc.pageCount()

    def page_size(self, index: int) -> QSizeF:
        return self.doc.pagePointSize(index)

    def render(self, index: int, size: QSize) -> QImage:
        """The page on opaque white paper (RGB32).

        QtPdf leaves whatever a PDF doesn't paint transparent (most real PDFs have no
        page background), so it is flattened here once: every consumer then draws, and
        Darken-blends highlighters, against solid white rather than transparency.
        """
        page = self.doc.render(index, size)
        image = QImage(size, QImage.Format.Format_RGB32)
        image.fill(QColor(PAPER))
        if not page.isNull():
            painter = QPainter(image)
            painter.drawImage(0, 0, page)
            painter.end()
        return image

    def close(self) -> None:
        if shiboken6.isValid(self.doc):     # at exit Qt may have deleted it first
            self.doc.close()


def pdf_target_rect(page_size: QSizeF) -> QRectF:
    """Where a PDF page of `page_size` points lands, in page pixels."""
    if page_size.width() <= 0 or page_size.height() <= 0:
        return QRectF(0, 0, PAGE_WIDTH, PAGE_HEIGHT)
    w, h = page_size.width() * PDF_PX_PER_PT, page_size.height() * PDF_PX_PER_PT
    return QRectF(PAGE_WIDTH / 2 - w / 2, 0, w, h)


@dataclass
class PageContent:
    scene: PageScene | None
    pdf: PdfFile | None
    pdf_page: int | None
    template: Template | None = None     # drawn only on pages without a PDF background

    @property
    def has_pdf_page(self) -> bool:
        return (self.pdf is not None and self.pdf_page is not None
                and 0 <= self.pdf_page < self.pdf.page_count)

    def frame(self) -> QRectF:
        if self.has_pdf_page:
            return pdf_target_rect(self.pdf.page_size(self.pdf_page))
        return QRectF(0, 0, PAGE_WIDTH, PAGE_HEIGHT)

    def bounds(self, crop: str) -> tuple[float, float, float, float]:
        """"clip": the page frame; "page": the frame plus any ink/text beyond it."""
        frame = self.frame()
        if crop == "page" and self.scene is not None:
            x, y, w, h = render_bounds(self.scene, "page", measure=qt_measure)
            frame = frame.united(QRectF(x, y, w, h)) if self.has_pdf_page else QRectF(x, y, w, h)
        elif crop != "clip" and crop != "page":
            raise ValueError(f"unknown crop mode {crop!r}")
        return frame.x(), frame.y(), frame.width(), frame.height()


def _draw_pdf_page(painter: QPainter, content: PageContent) -> None:
    """Draw the PDF page rendered at exactly its device-pixel rectangle. Drawing a
    render into a fractional target resamples it nearest-neighbour, which drops or
    doubles a pixel column and shifts everything past it."""
    device = painter.worldTransform().mapRect(content.frame())
    left, top = round(device.left()), round(device.top())
    size = QSize(max(1, round(device.right()) - left), max(1, round(device.bottom()) - top))
    painter.save()
    painter.resetTransform()
    painter.drawImage(QPoint(left, top), content.pdf.render(content.pdf_page, size))
    painter.restore()


def render_content(content: PageContent, scale: float, crop: str = "page") -> QImage:
    """Render one page to an image at `scale` (page pixels → image pixels).

    crop "page" grows with the content (faithful, scrollable); "clip" is exactly the
    page frame (thumbnails).
    """
    x, y, w, h = content.bounds(crop)
    image = QImage(max(1, round(w * scale)), max(1, round(h * scale)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(PAPER))
    painter = QPainter(image)
    try:
        painter.scale(scale, scale)
        painter.translate(-x, -y)
        if content.template is not None and not content.has_pdf_page:
            paint_template(painter, content.template, (x, y, w, h))
        if content.has_pdf_page:
            _draw_pdf_page(painter, content)
        if content.scene is not None:
            paint(painter, content.scene, background=PAPER)
    finally:
        painter.end()
    return image


class DocumentPages:
    """A document's pages and its PDF, loaded from a LibrarySource (worker-thread safe)."""

    def __init__(self, source: LibrarySource, doc: Document) -> None:
        self.source = source
        self.doc = doc
        self.pdf_path: Path | None = source.pdf_path(doc)
        self.pdf = PdfFile(self.pdf_path) if self.pdf_path else None
        pagedata = None
        if any(not p.template for p in doc.content.pages if not p.deleted):
            try:
                pagedata = source.pagedata(doc)
            except SyncError as err:          # e.g. offline and not cached: plain paper
                log.info("no .pagedata for %s: %s", doc.id, err)
        self.pages: list[PageRef] = document_pages(doc, self.pdf.page_count if self.pdf else None, pagedata)
        self.templates = default_library()

    def content(self, index: int) -> PageContent:
        page = self.pages[index]
        return PageContent(self.source.page_scene(self.doc, page), self.pdf, page.pdf_page,
                           self.templates.get(page.template))

    def render(self, index: int, scale: float, crop: str = "page") -> QImage:
        return render_content(self.content(index), scale, crop)

    def close(self) -> None:
        if self.pdf is not None:
            self.pdf.close()


def cover_index(doc: Document, page_count: int) -> int:
    """Thumbnail page: coverPageNumber -1 means "last opened page", else that page."""
    cover = doc.content.cover_page_number
    if cover < 0:
        cover = doc.metadata.last_opened_page
    return min(max(cover, 0), max(page_count - 1, 0))
