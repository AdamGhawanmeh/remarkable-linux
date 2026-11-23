"""Composite page rendering: PDF/EPUB background + strokes + typed text.

Safe to call from worker threads: everything renders into a QImage, and each job
opens its own QPdfDocument (QtPdf serialises PDFium access internally).

PDF placement: the PDF page is drawn at physical size on the 226 DPI screen grid
(226/72 page pixels per PDF point), centred on the page's vertical centre line and
top-aligned. An A4 page is therefore 1868 px wide, wider than the 1404 px screen.
Verified against real annotated A4 and US Letter PDFs (handwriting lands in the form
boxes); fit-to-page and fit-width placements visibly do not (ARCHITECTURE.md §5.2).

Page frame: the area a page "is": the PDF page for PDF-backed pages, else the page's
paper (its SceneInfo paper size, 1404 × 1872 unless it says otherwise: 1620 × 2160,
954 × 1696 or any other the tablet set, ARCHITECTURE.md §5.6). Page coordinates put the
paper's top left at (0, 0), so the scene's x = 0, the paper's centre line, is page
x = width / 2. Thumbnails clip to the frame; the viewer shows the frame plus any ink or
text beyond it.
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
    """A PDF opened from a local file, with the password unlocked for it if it needs
    one (ui.pdflock); PdfLocked when it needs one that is not known."""

    def __init__(self, path: str | Path) -> None:
        from .pdflock import PdfLocked, password_for
        self.doc = QPdfDocument()
        password = password_for(path)
        if password:
            self.doc.setPassword(password)
        error = self.doc.load(str(path))
        if error == QPdfDocument.Error.IncorrectPassword:
            self.doc.close()
            raise PdfLocked(f"the PDF {Path(path).name} needs a password")
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


def pdf_target_rect(page_size: QSizeF, paper: tuple[float, float] = (PAGE_WIDTH, PAGE_HEIGHT)) -> QRectF:
    """Where a PDF page of `page_size` points lands, in page pixels of a page whose
    paper is `paper` (the PDF is centred on the scene's x = 0, page x = paper width / 2)."""
    if page_size.width() <= 0 or page_size.height() <= 0:
        return QRectF(0, 0, paper[0], paper[1])
    w, h = page_size.width() * PDF_PX_PER_PT, page_size.height() * PDF_PX_PER_PT
    return QRectF(paper[0] / 2 - w / 2, 0, w, h)


@dataclass
class PageContent:
    scene: PageScene | None
    pdf: PdfFile | None
    pdf_page: int | None
    template: Template | None = None     # drawn only on pages without a PDF background
    # The document's orientation is "landscape" (Qt::Horizontal). Its pages without a PDF
    # page are then the paper's note bounds, wide: see frame().
    landscape: bool = False

    @property
    def has_pdf_page(self) -> bool:
        return (self.pdf is not None and self.pdf_page is not None
                and 0 <= self.pdf_page < self.pdf.page_count)

    @property
    def paper(self) -> tuple[float, float]:
        """The page's paper size (PageScene.width x height; 1404 x 1872 without a file)."""
        if self.scene is None:
            return float(PAGE_WIDTH), float(PAGE_HEIGHT)
        return float(self.scene.width), float(self.scene.height)

    @property
    def origin(self) -> float:
        """Page x of the scene's x = 0: half the paper's width."""
        return self.paper[0] / 2

    @property
    def is_landscape(self) -> bool:
        """Shown wide: a page without a PDF page in a landscape document."""
        return self.landscape and not self.has_pdf_page

    def frame(self) -> QRectF:
        """The PDF page's rect, else the paper; on a landscape page the paper's note
        bounds QRectF(-h/2, 0, h, w) (Scene 0x1408a8df0), which is what 3.29's thumbnail
        job (0x14055d170) renders for a document whose orientation is Horizontal (or a
        PDF page wider than tall, 0x1405633f0) into a transposed (h x w) image: the scene
        is not turned, its paper simply lies across. In page coordinates (scene x = 0 at
        page x = w/2) that is (w/2 - h/2, 0, h, w)."""
        if self.has_pdf_page:
            return pdf_target_rect(self.pdf.page_size(self.pdf_page), self.paper)
        width, height = self.paper
        if self.landscape:
            return QRectF(width / 2 - height / 2, 0, height, width)
        return QRectF(0, 0, width, height)

    def bounds(self, crop: str) -> tuple[float, float, float, float]:
        """"clip": the page frame; "page": the frame plus any ink/text beyond it, with a
        margin of 20 (exports, PDF pages); "exact": the frame united with what lies
        beyond it with no margin, as 3.29's scene rect is (0x1408ab350, the union of its
        items' rects): images by their corners, text by its lines, strokes by their
        points (not padded by pen width: on 554 of 4,242 real notebook pages ink touches
        the edge that way). The viewer's note pages use it (ARCHITECTURE.md 5.17)."""
        frame = self.frame()
        if crop in ("page", "exact") and self.scene is not None:
            # A PDF page is its frame, not the notebook paper: grown only where ink lies
            # beyond it (a slide stays 16:9 once drawn on).
            paper = (frame.x(), frame.y(), frame.width(), frame.height()) \
                if self.has_pdf_page or self.is_landscape else None
            exact = crop == "exact"
            frame = QRectF(*render_bounds(self.scene, "page", margin=0.0 if exact else 20.0, measure=qt_measure,
                                          paper=paper))
        elif crop not in ("clip", "page", "exact"):
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


def render_content(content: PageContent, scale: float, crop: str = "page", background: bool = True,
                   area: tuple[float, float, float, float] | None = None) -> QImage:
    """Render one page to an image at `scale` (page pixels → image pixels).

    crop "page" grows with the content (faithful, scrollable); "clip" is exactly the
    page frame (thumbnails). `background` False leaves out the template, or the PDF
    page, as the app's "Hide template" does (SceneController.backgroundVisible): ink,
    images and typed text are drawn all the same, and the page keeps its frame.
    `area` (page coordinates) renders that rectangle instead: the viewer's whole white
    page card on a note page, which 3.29 covers with the template (its template
    painter fills whatever it is handed; ARCHITECTURE.md 5.17).
    """
    x, y, w, h = area if area is not None else content.bounds(crop)
    image = QImage(max(1, round(w * scale)), max(1, round(h * scale)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(PAPER))
    painter = QPainter(image)
    try:
        painter.scale(scale, scale)
        painter.translate(-x, -y)
        if background and content.template is not None and not content.has_pdf_page:
            paint_template(painter, content.template, (x, y, w, h), *content.paper)
        if background and content.has_pdf_page:
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
        if doc.content.format_version != 2 and any(not p.template for p in doc.content.pages if not p.deleted):
            try:
                pagedata = source.pagedata(doc)
            except SyncError as err:          # e.g. offline and not cached: plain paper
                log.info("no .pagedata for %s: %s", doc.id, err)
        self.pages: list[PageRef] = document_pages(doc, self.pdf.page_count if self.pdf else None, pagedata)
        self.templates = default_library()

    def content(self, index: int) -> PageContent:
        page = self.pages[index]
        return PageContent(self.source.page_scene(self.doc, page), self.pdf, page.pdf_page,
                           self.templates.resolve(page.template), landscape=is_landscape_document(self.doc))

    def render(self, index: int, scale: float, crop: str = "page") -> QImage:
        return render_content(self.content(index), scale, crop)

    def close(self) -> None:
        if self.pdf is not None:
            self.pdf.close()


def is_landscape_document(doc: Document) -> bool:
    """.content "orientation" "landscape" (read as 0x1406cf330: Qt::Horizontal)."""
    return getattr(doc.content, "orientation", "portrait") == "landscape"


def cover_index(doc: Document, page_count: int) -> int:
    """Thumbnail page: coverPageNumber -1 means "last opened page", else that page."""
    cover = doc.content.cover_page_number
    if cover < 0:
        cover = doc.metadata.last_opened_page
    return min(max(cover, 0), max(page_count - 1, 0))
