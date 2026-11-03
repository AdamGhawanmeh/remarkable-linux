"""Document export: a multi-page PDF or a .rmdoc archive.

Exported pages have each page's faithful bounds (the page frame plus any ink or typed
text beyond it) on the tablet's 226 DPI grid, and are composed from three layers:

  base    PDF-backed pages: the original PDF page itself with its content untouched,
          so it stays vector, its text selectable, its links and annotations kept.
          Notebook pages: white paper and the page template.
  darken  highlighter and shader strokes, painted with the PDF /BM /Darken blend mode:
          the viewer's QPainter::CompositionMode_Darken, so text under a highlight
          stays fully dark.
  normal  ink and typed text.

QPdfWriter draws the layers as vector pages but cannot express blend modes, so pypdf
assembles each output page: the darken and normal layer pages become Form XObjects
painted over the base. Wrapping them (rather than merging content streams) needs no
content parsing or resource renaming, so the cost does not grow with the ink.

A source PDF that pypdf cannot read (damaged, or encrypted with a password) is
rasterised into the base layer instead (BACKGROUND_SCALE × 226 DPI), as is a page
whose size pypdf and QtPdf disagree on, which would misplace the ink.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject,
                           IndirectObject, NameObject, RectangleObject)
from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QRectF, QSize, QSizeF
from PySide6.QtGui import QColor, QPageSize, QPainter, QPdfWriter

from viewer.renderer import paint
from viewer.templates import paint_template

from .pages import PAPER, DocumentPages, PageContent

log = logging.getLogger("rm.ui.export")

PAGE_DPI = 226
PT_PER_PX = 72 / PAGE_DPI
BACKGROUND_SCALE = 1.5          # rasterised source PDF pages: 339 DPI
LAYERS = ("base", "darken", "normal")
SIZE_TOLERANCE_PT = 0.5

PDF_FILTER = "PDF Document (*.pdf)"
RMDOC_FILTER = "reMarkable Archive (*.rmdoc)"



@dataclass
class _Placed:
    """One exported page, after its layers were drawn."""
    bounds: tuple[float, float, float, float]     # page pixels
    frame: QRectF                                 # page pixels
    source: PageObject | None                     # original PDF page: the vector base
    drawn: dict[str, bool]                        # layer → has content


def _open_source(path: Path) -> PdfReader | None:
    """The source PDF for vector export, or None to rasterise its pages."""
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("it needs a password")
        len(reader.pages)
        return reader
    except Exception as err:      # pypdf raises all kinds of errors on damaged files
        log.warning("rasterising %s: pypdf cannot read it (%s: %s)", path.name, type(err).__name__, err)
        return None


def _display_box(page: PageObject) -> tuple[float, float, float, float, int]:
    """(left, bottom, width, height, quarter turns) of the visible page, as PDFium
    sees it: the CropBox clipped to the MediaBox, turned by /Rotate."""
    def corners(box) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = (float(v) for v in box)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    mx0, my0, mx1, my1 = corners(page.mediabox)
    cx0, cy0, cx1, cy1 = corners(page.cropbox)
    x0, y0, x1, y1 = max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1)
    return x0, y0, x1 - x0, y1 - y0, int(page.rotation / 90) % 4


def _rotation(quarter: int, left: float, bottom: float, width: float,
              height: float) -> tuple[float, ...]:
    """Matrix from display space (origin at the displayed page's bottom-left) to the
    page's own space, for /Rotate `quarter` clockwise quarter turns of the box."""
    return {0: (1, 0, 0, 1, left, bottom),
            1: (0, 1, -1, 0, left + width, bottom),
            2: (-1, 0, 0, -1, left + width, bottom + height),
            3: (0, -1, 1, 0, left, bottom + height)}[quarter]


def _source_page(reader: PdfReader, content: PageContent) -> PageObject | None:
    """The original page to keep as the vector base, or None to rasterise it."""
    try:
        page = reader.pages[content.pdf_page]
        _, _, width, height, quarter = _display_box(page)
    except Exception as err:      # damaged page tree or boxes
        log.warning("rasterising PDF page %d: %s: %s", content.pdf_page, type(err).__name__, err)
        return None
    if quarter % 2:
        width, height = height, width
    frame = content.frame()
    if (abs(width - frame.width() * PT_PER_PX) > SIZE_TOLERANCE_PT
            or abs(height - frame.height() * PT_PER_PX) > SIZE_TOLERANCE_PT):
        log.warning("rasterising PDF page %d: pypdf sees %.1f × %.1f pt, QtPdf %.1f × %.1f pt",
                    content.pdf_page, width, height, frame.width() * PT_PER_PX, frame.height() * PT_PER_PX)
        return None
    return page


def _paint_layer(painter: QPainter, layer: str, content: PageContent,
                 bounds: tuple[float, float, float, float], vector_base: bool,
                 background_scale: float) -> bool:
    if layer != "base":
        return content.scene is not None and paint(
            painter, content.scene, background=PAPER, layers=layer, blend_modes=False) > 0
    if vector_base:
        return False                              # the original PDF page is the base
    painter.fillRect(QRectF(*bounds), QColor(PAPER))
    if content.has_pdf_page:
        frame = content.frame()
        pixels = QSize(max(1, round(frame.width() * background_scale)),
                       max(1, round(frame.height() * background_scale)))
        painter.drawImage(frame, content.pdf.render(content.pdf_page, pixels))
    elif content.template is not None:
        paint_template(painter, content.template, bounds)
    return True


def _draw_layers(pages: DocumentPages, reader: PdfReader | None, background_scale: float,
                 progress: Callable[[int, int], None] | None) -> tuple[bytes, list[_Placed]]:
    """Draw every page's layers as consecutive pages of one PDF (fonts are shared)."""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setResolution(PAGE_DPI)
    painter = QPainter()
    placed: list[_Placed] = []
    count = len(pages.pages)
    try:
        for index in range(count):
            content = pages.content(index)
            bounds = content.bounds("page")
            x, y, w, h = bounds
            source = _source_page(reader, content) if reader is not None and content.has_pdf_page else None
            size = QPageSize(QSizeF(w * PT_PER_PX, h * PT_PER_PX), QPageSize.Unit.Point, "",
                             QPageSize.SizeMatchPolicy.ExactMatch)
            drawn = {}
            for layer in LAYERS:
                writer.setPageSize(size)
                writer.setPageMargins(QMarginsF(0, 0, 0, 0))
                if painter.isActive():
                    writer.newPage()
                elif not painter.begin(writer):
                    raise OSError("cannot draw the export layers")
                painter.save()
                painter.translate(-x, -y)
                drawn[layer] = _paint_layer(painter, layer, content, bounds, source is not None,
                                            background_scale)
                painter.restore()
            placed.append(_Placed(bounds, content.frame(), source, drawn))
            if progress is not None:
                progress(index + 1, count)
    finally:
        if painter.isActive():
            painter.end()
    buffer.close()
    return bytes(buffer.data()), placed


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _stream(writer: PdfWriter, data: bytes) -> IndirectObject:
    stream = DecodedStreamObject()
    stream.set_data(data)
    return writer._add_object(stream)


def _layer_box(item: _Placed, layer: PageObject) -> tuple[float, float, float, float]:
    """A layer page's true extent. QPdfWriter rounds page sizes to whole points but
    draws from the top-left corner, so the exact page reaches past its MediaBox."""
    _, _, w, h = item.bounds
    top = float(layer.mediabox.top)
    return 0.0, top - h * PT_PER_PX, w * PT_PER_PX, top


def _form(writer: PdfWriter, layer: PageObject, box: tuple[float, ...]) -> IndirectObject:
    """A layer page as a Form XObject. It has no /Group, so a blend mode set around it
    applies to each stroke inside, exactly as the viewer blends them."""
    form = DecodedStreamObject()
    form.set_data(layer.get_contents().get_data())
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject(FloatObject(v) for v in box),
        NameObject("/Resources"): layer.raw_get("/Resources").clone(writer),
    })
    return writer._add_object(form.flate_encode())


def _copy(owner: DictionaryObject, key: str) -> DictionaryObject:
    """A shallow copy of the dictionary at owner[key] (resolved), or an empty one."""
    return DictionaryObject(owner[key]) if key in owner else DictionaryObject()


def _unique(names: DictionaryObject, base: str) -> NameObject:
    name, n = base, 0
    while name in names:
        n += 1
        name = f"{base}{n}"
    return NameObject(name)


def _place(item: _Placed, page: PageObject, top: float) -> tuple[tuple[float, ...], str]:
    """The matrix taking a layer page (its top edge at `top`) onto the original `page`,
    and the clip keeping the original's content inside its visible box. Grows the
    page's boxes when ink or text extends beyond the PDF page."""
    left, bottom, width, height, quarter = _display_box(page)
    a, b, c, d, e, f = _rotation(quarter, left, bottom, width, height)
    display_h = height if quarter % 2 == 0 else width
    x, y, w, h = item.bounds
    fx, fy = item.frame.x(), item.frame.y()
    tx, ty = (x - fx) * PT_PER_PX, display_h - top - (y - fy) * PT_PER_PX
    ctm = (a, b, c, d, a * tx + c * ty + e, b * tx + d * ty + f)
    frame = item.frame
    if max(abs(x - frame.x()), abs(y - frame.y()), abs(w - frame.width()), abs(h - frame.height())) > 0.01:
        dx0, dx1 = (x - fx) * PT_PER_PX, (x + w - fx) * PT_PER_PX
        dy0, dy1 = display_h - (y + h - fy) * PT_PER_PX, display_h - (y - fy) * PT_PER_PX
        xs = [a * dx + c * dy + e for dx in (dx0, dx1) for dy in (dy0, dy1)]
        ys = [b * dx + d * dy + f for dx in (dx0, dx1) for dy in (dy0, dy1)]
        page.mediabox = page.cropbox = RectangleObject((min(xs), min(ys), max(xs), max(ys)))
    clip = f"{_num(left)} {_num(bottom)} {_num(width)} {_num(height)} re W n\n"
    return ctm, clip


def _stamp(writer: PdfWriter, page: PageObject, layers: list[tuple[PageObject, bool]],
           box: tuple[float, ...], ctm: tuple[float, ...], clip: str) -> None:
    """Paint layer pages over `page`, isolating its own content in q … Q."""
    resources = _copy(page, "/Resources")      # this page's own: source PDFs often share one
    xobjects = _copy(resources, "/XObject")
    states = _copy(resources, "/ExtGState")
    matrix = " ".join(_num(v) for v in ctm)
    ops = []
    for layer, darken in layers:
        name = _unique(xobjects, "/RMLayer")
        xobjects[name] = _form(writer, layer, box)
        blend = ""
        if darken:
            state = _unique(states, "/RMDarken")
            states[state] = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState"),
                                              NameObject("/BM"): NameObject("/Darken")})
            blend = f"{state} gs "
        ops.append(f"q {blend}{matrix} cm {name} Do Q\n")
    if not ops and not clip:
        return
    resources[NameObject("/XObject")] = xobjects
    resources[NameObject("/ExtGState")] = states
    page[NameObject("/Resources")] = resources
    contents = page.raw_get("/Contents") if "/Contents" in page else None
    own = contents.get_object() if contents is not None else []
    refs = list(own) if isinstance(own, ArrayObject) else [contents] if contents is not None else []
    page[NameObject("/Contents")] = ArrayObject([
        _stream(writer, f"q\n{clip}".encode()),
        *(r if isinstance(r, IndirectObject) else writer._add_object(r) for r in refs),
        _stream(writer, ("\nQ\n" + "".join(ops)).encode()),
    ])


def _assemble(layers: bytes, placed: list[_Placed], title: str, out: BinaryIO) -> None:
    overlay = PdfReader(io.BytesIO(layers))
    writer = PdfWriter()
    for index, item in enumerate(placed):
        base, darken, normal = (overlay.pages[3 * index + i] for i in range(3))
        box = _layer_box(item, base)
        if item.source is not None:
            page = writer.add_page(item.source)
            ctm, clip = _place(item, page, box[3])
        else:
            page = writer.add_page(base)
            page.mediabox = page.trimbox = RectangleObject(box)
            ctm, clip = (1, 0, 0, 1, 0, 0), ""
        ink = [(layer, name == "darken") for name, layer in (("darken", darken), ("normal", normal))
               if item.drawn[name]]
        _stamp(writer, page, ink, box, ctm, clip)
    writer.add_metadata({"/Title": title, "/Creator": "reMarkable for Linux"})
    writer.write(out)


def export_pdf(pages: DocumentPages, path: str | Path, *,
               progress: Callable[[int, int], None] | None = None,
               background_scale: float = BACKGROUND_SCALE) -> Path:
    """Write every page of `pages` to a PDF at `path`; returns the path."""
    path = Path(path)
    if not pages.pages:
        raise ValueError("document has no pages")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"no such folder: {path.parent}")
    reader = _open_source(pages.pdf_path) if pages.pdf is not None else None
    layers, placed = _draw_layers(pages, reader, background_scale, progress)
    partial = path.with_name(path.name + ".part")
    try:
        with open(partial, "wb") as out:
            _assemble(layers, placed, pages.doc.name, out)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return path


def export_file_name(name: str, suffix: str) -> str:
    """A safe default file name for a document title."""
    safe = "".join("_" if c in '/\\\0:*?"<>|' else c for c in name).strip(" .") or "document"
    return f"{safe}{suffix}"
