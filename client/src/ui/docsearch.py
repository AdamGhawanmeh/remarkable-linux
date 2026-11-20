"""In-document search and tables of contents, worked out from a document's pages.

Search (the app's DocumentSearchController, bridge/scene.py): a term is looked for in
every page of a document, in page order, and each hit is a PageSearchResult as the app
has it:

    {"page": <document page index>,
     "metrics": [{"boundingRect": QRectF, "baseline": float}, ...]}

one metric per line the hit spans, in scene coordinates (226 DPI tablet pixels, x = 0 on
the page's vertical centre line, y = 0 at its top: what SceneTileManager maps to the
view). Two kinds of text are searched on a page:
    - the PDF (or EPUB rendition) page under it: QtPdf's text and per-character boxes
      (PDFium, as the app's PdfRenderer::search), the page placed at 226/72 px per point
      centred on x = 0 (ui/pages.py); text a PDF sets outside its page is not found
      (it is never seen); the PDF page comes from the page's own `redir`
      (PageRef.pdf_page), so inserted, moved or duplicated pages find their own text and
      blank pages added to a PDF have none;
    - typed text (.rm v6 RootText): the page's paragraphs as viewer/rm_text.py and the
      renderer read them, laid out as the editor lays them out (renderer.layout_text
      with qt_measure: the app's fonts), so a box sits where the caret would.
Matching is case-insensitive (str.casefold) and treats any run of white space, line
breaks included, as one space, so a phrase broken over two lines is found. Hits on a
page are in reading order: top to bottom, then left to right.

Tables of contents (LibraryController.loadTableOfContents, bridge/library.py): a list
of the app's DocumentChapter

    {"name": str, "path": str, "page": <document page index or -1>, "level": int}

depth first, `level` 0 for top-level entries. A PDF's outline (QPdfBookmarkModel), or
for an EPUB its rendition's outline if it has one, else the book's own navigation
document (EPUB 3 nav, EPUB 2 NCX), `path` being the entry's target in the book. PDF
pages are mapped to document pages through the page list (first page showing that PDF
page, else the first one showing a later PDF page: a deleted chapter page lands on what
follows it). EPUB entries have no PDF page: each is the first page at or after the
previous entry's whose text holds the entry's title (the tablet lays the book out; this
client only has the PDF it made), else the previous entry's page.

Everything here runs off the GUI thread: each caller opens its own QPdfDocument.
"""

from __future__ import annotations

import io
import logging
import posixpath
import threading
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import unquote
from xml.etree import ElementTree

from PySide6.QtCore import QModelIndex, QRectF

from ui.pdftext import MIN_BOX_PT, pdf_frame, selection_boxes, to_scene
from ui.pdftext import solid as _solid
from viewer.renderer import BASELINE_AT, caret_x, layout_text, qt_measure
from viewer.rm_parser import PAGE_WIDTH, PageScene

log = logging.getLogger("rm.ui.docsearch")

PDF_PX_PER_PT = 226 / 72
HALF_WIDTH = PAGE_WIDTH / 2


# -- matching ---------------------------------------------------------------------------

def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def fold(text: str) -> tuple[str, list[int]]:
    """`text` casefolded with white space runs made one space, and for each character
    of the result the UTF-16 index in `text` it comes from (plus the end)."""
    out: list[str] = []
    where: list[int] = []
    at = 0
    space = False
    for ch in text:
        if ch.isspace():
            if not space:
                out.append(" ")
                where.append(at)
            space = True
        else:
            space = False
            folded = ch.casefold()
            out.extend(folded)
            where.extend([at] * len(folded))
        at += _units(ch)
    where.append(at)
    return "".join(out), where


def normalize(term: str) -> str:
    return fold(term.strip())[0]


def find_all(text: str, term: str) -> list[tuple[int, int]]:
    """Every (start, end) of `term` in `text` (UTF-16 indices), not overlapping,
    case-insensitive, white space runs alike."""
    needle = normalize(term)
    if not needle:
        return []
    hay, where = fold(text)
    out = []
    i = hay.find(needle)
    while i != -1:
        start, end = where[i], where[i + len(needle)]
        if out and start < out[-1][1]:
            i = hay.find(needle, i + 1)
            continue
        out.append((start, end))
        i = hay.find(needle, i + len(needle))
    return out


# -- hits -------------------------------------------------------------------------------

def metric(rect: QRectF, baseline: float | None = None) -> dict:
    return {"boundingRect": QRectF(rect), "baseline": float(rect.bottom() if baseline is None else baseline)}


def result(page: int, metrics: list[dict]) -> dict:
    return {"page": int(page), "metrics": metrics}


def _lines(boxes: Iterable[QRectF]) -> list[QRectF]:
    """Character boxes merged into one box per line: boxes that overlap vertically (by
    more than half the smaller one's height) and follow each other. Characters with no
    area (spaces, and what PDFium generates for line ends) are left out while others
    give the line its box; a hit of nothing else keeps them, made MIN_BOX_PT thick."""
    boxes = list(boxes)
    solid = [b for b in boxes if b.width() > 0 and b.height() > 0]
    if not solid:
        solid = [_solid(b) for b in boxes if b.width() > 0 or b.height() > 0]
    lines: list[QRectF] = []
    for box in solid:
        if lines:
            last = lines[-1]
            overlap = min(last.bottom(), box.bottom()) - max(last.top(), box.top())
            if overlap > 0.5 * min(last.height(), box.height()) and box.left() >= last.left() - 1:
                lines[-1] = last.united(box)
                continue
        lines.append(QRectF(box))
    return lines


def pdf_hits(doc, pdf_page: int, term: str, text_cache: dict | None = None) -> list[list[dict]]:
    """The hits of `term` on page `pdf_page` of the QPdfDocument `doc`: per hit, its
    metrics (one per line) in scene coordinates."""
    if text_cache is not None and pdf_page in text_cache:
        text = text_cache[pdf_page]
    else:
        text = doc.getAllText(pdf_page).text()
        if text_cache is not None:
            text_cache[pdf_page] = text
    found = find_all(text, term)
    if not found:
        return []
    size = doc.pagePointSize(pdf_page)
    if size.width() <= 0 or size.height() <= 0:
        return []
    frame = pdf_frame(size)
    scale = frame.width() / size.width()
    page = QRectF(0, 0, size.width(), size.height())
    hits = []
    for start, end in found:
        metrics = []
        for line in _lines(selection_boxes(doc, pdf_page, start, end - start)):
            if not page.intersects(line):
                continue        # text set outside the page: never shown, so not highlighted
            metrics.append(metric(to_scene(line, frame, scale)))
        if metrics:
            hits.append(metrics)
    return hits


def text_hits(scene: PageScene | None, term: str, measure=qt_measure) -> list[list[dict]]:
    """The hits of `term` in the page's typed text: per hit, one metric per laid-out
    line it is on (the caret's x at either end, the line's height), scene coordinates."""
    block = scene.text if scene is not None else None
    if block is None or block.is_blank:
        return []
    found = find_all(block.text, term)
    if not found:
        return []
    lines = layout_text(scene, measure)
    origin = HALF_WIDTH       # page -> scene, as the viewer places pages (bridge/scene.to_scene)
    hits = []
    for start, end in found:
        metrics = []
        for line in lines:
            a, b = max(start, line.start), min(end, line.end)
            if a >= b:
                continue
            top = line.baseline - line.height * BASELINE_AT
            x0 = caret_x(line, a, measure) - origin
            x1 = caret_x(line, b, measure) - origin
            if x1 <= x0:
                continue
            metrics.append(metric(QRectF(x0, top, x1 - x0, line.height), line.baseline))
        if metrics:
            hits.append(metrics)
    return hits


def _reading_order(hits: list[list[dict]]) -> list[list[dict]]:
    def key(metrics):
        r = metrics[0]["boundingRect"]
        return (round(r.top()), r.left())
    return sorted(hits, key=key)


def page_hits(content, page: int, term: str, text_cache: dict | None = None) -> list[dict]:
    """Every hit on one page (a ui.pages.PageContent): the PDF page under it and its
    typed text, as PageSearchResults of document page `page`."""
    hits: list[list[dict]] = []
    if content.has_pdf_page:
        hits += pdf_hits(content.pdf.doc, content.pdf_page, term, text_cache)
    hits += text_hits(content.scene, term)
    return [result(page, metrics) for metrics in _reading_order(hits)]


def search_pages(pages, term: str, indices: Iterable[int], on_page: Callable[[int, list[dict]], None],
                 cancelled: threading.Event) -> None:
    """Search the pages `indices` of a ui.pages.DocumentPages in turn, handing each
    page's results to on_page(index, results) (also for none); stops quietly as soon
    as `cancelled` is set."""
    cache: dict[int, str] = {}
    for index in indices:
        if cancelled.is_set():
            return
        try:
            results = page_hits(pages.content(index), index, term, cache)
        except Exception as err:        # one unreadable page does not end the search
            log.warning("search: page %d not searched: %s", index, err)
            results = []
        if cancelled.is_set():
            return
        on_page(index, results)


# -- tables of contents -------------------------------------------------------------------

@dataclass
class Chapter:
    name: str
    level: int
    pdf_page: int = -1      # page of the PDF the entry points at, if it is a PDF's
    path: str = ""          # EPUB: the entry's target in the book


def chapter(name: str, path: str, page: int, level: int) -> dict:
    return {"name": name, "path": path, "page": int(page), "level": int(level)}


def pdf_outline(doc) -> list[Chapter]:
    """A QPdfDocument's outline, depth first (QPdfBookmarkModel)."""
    from PySide6.QtPdf import QPdfBookmarkModel
    model = QPdfBookmarkModel()
    model.setDocument(doc)
    roles = {bytes(v).decode(): k for k, v in model.roleNames().items()}
    out: list[Chapter] = []

    def walk(parent: QModelIndex, level: int) -> None:
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            title = model.data(index, roles["title"]) or ""
            page = model.data(index, roles["page"])
            out.append(Chapter(" ".join(str(title).split()), level, int(page) if page is not None else -1))
            walk(index, level + 1)

    walk(QModelIndex(), 0)
    model.setDocument(None)
    return out


def page_of_pdf_page(pdf_pages: list[int | None], target: int) -> int:
    """The document page showing PDF page `target`: the first one that does, else the
    first showing the nearest later PDF page, else -1."""
    if target < 0:
        return -1
    best, best_pdf = -1, None
    for index, pdf_page in enumerate(pdf_pages):
        if pdf_page is None:
            continue
        if pdf_page == target:
            return index
        if pdf_page > target and (best_pdf is None or pdf_page < best_pdf):
            best, best_pdf = index, pdf_page
    return best


# EPUB navigation

_OPF = "{http://www.idpf.org/2007/opf}"
_NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
_XHTML = "{http://www.w3.org/1999/xhtml}"
_EPUB = "{http://www.idpf.org/2007/ops}"


def _text(element) -> str:
    return " ".join("".join(element.itertext()).split())


def epub_navigation(data: bytes) -> list[Chapter]:
    """An EPUB's table of contents: the EPUB 3 navigation document's toc nav, else the
    EPUB 2 NCX. Raises ValueError for a file that is not a readable EPUB."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
        container = ElementTree.fromstring(book.read("META-INF/container.xml"))
        rootfile = next(e for e in container.iter() if e.tag.endswith("rootfile")).get("full-path")
        opf = ElementTree.fromstring(book.read(rootfile))
    except (zipfile.BadZipFile, KeyError, StopIteration, ElementTree.ParseError) as err:
        raise ValueError(f"not a readable EPUB: {err}") from err
    base = posixpath.dirname(rootfile)
    items = {}
    for item in opf.iter(f"{_OPF}item"):
        items[item.get("id")] = item
    nav = next((i for i in items.values() if "nav" in (i.get("properties") or "").split()), None)
    if nav is not None:
        name = posixpath.normpath(posixpath.join(base, unquote(nav.get("href", ""))))
        try:
            chapters = _nav_chapters(ElementTree.fromstring(book.read(name)), posixpath.dirname(name))
        except (KeyError, ElementTree.ParseError):
            chapters = []
        if chapters:
            return chapters
    spine = next(opf.iter(f"{_OPF}spine"), None)
    ncx = items.get(spine.get("toc")) if spine is not None and spine.get("toc") else None
    if ncx is None:
        ncx = next((i for i in items.values() if i.get("media-type") == "application/x-dtbncx+xml"), None)
    if ncx is None:
        return []
    name = posixpath.normpath(posixpath.join(base, unquote(ncx.get("href", ""))))
    try:
        root = ElementTree.fromstring(book.read(name))
    except (KeyError, ElementTree.ParseError):
        return []
    out: list[Chapter] = []
    folder = posixpath.dirname(name)

    def walk(parent, level: int) -> None:
        for point in parent.findall(f"{_NCX}navPoint"):
            label = point.find(f"{_NCX}navLabel")
            content = point.find(f"{_NCX}content")
            src = content.get("src", "") if content is not None else ""
            out.append(Chapter(_text(label) if label is not None else "", level,
                               path=posixpath.normpath(posixpath.join(folder, unquote(src))) if src else ""))
            walk(point, level + 1)

    nav_map = root.find(f"{_NCX}navMap")
    if nav_map is not None:
        walk(nav_map, 0)
    return out


def _nav_chapters(doc, folder: str) -> list[Chapter]:
    navs = list(doc.iter(f"{_XHTML}nav"))
    toc = next((n for n in navs if n.get(f"{_EPUB}type") == "toc"), navs[0] if navs else None)
    if toc is None:
        return []
    out: list[Chapter] = []

    def walk(ol, level: int) -> None:
        for li in ol.findall(f"{_XHTML}li"):
            link = li.find(f"{_XHTML}a")
            if link is None:
                link = li.find(f"{_XHTML}span")
            if link is not None:
                href = link.get("href", "")
                out.append(Chapter(_text(link), level,
                                   path=posixpath.normpath(posixpath.join(folder, unquote(href))) if href else ""))
            sub = li.find(f"{_XHTML}ol")
            if sub is not None:
                walk(sub, level + 1 if link is not None else level)

    top = toc.find(f"{_XHTML}ol")
    if top is not None:
        walk(top, 0)
    return out


def place_by_title(chapters: list[Chapter], page_texts: Callable[[int], str], page_count: int) -> list[int]:
    """A page per chapter for chapters that point into a book, not at a PDF page: the
    first page at or after the previous chapter's whose text holds the title (case and
    white space aside), else the previous chapter's page (0 for the first)."""
    pages = []
    at = 0
    folded: dict[int, str] = {}
    for item in chapters:
        title = normalize(item.name)
        found = None
        if title:
            for index in range(at, page_count):
                if index not in folded:
                    folded[index] = fold(page_texts(index))[0]
                if title in folded[index]:
                    found = index
                    break
        if found is not None:
            at = found
        pages.append(at if page_count > 0 else -1)
    return pages


def table_of_contents(pdf_doc, pdf_pages: list[int | None], epub: Callable[[], bytes | None] | None = None
                      ) -> list[dict]:
    """A document's DocumentChapters: the PDF's outline mapped to document pages, or,
    when it has none and the document is an EPUB (`epub` returns the book's bytes), the
    book's navigation placed on the pages that show each title."""
    if pdf_doc is not None:
        outline = pdf_outline(pdf_doc)
        if outline:
            return [chapter(c.name, "", page_of_pdf_page(pdf_pages, c.pdf_page), c.level) for c in outline]
    if epub is None:
        return []
    data = epub()
    if not data:
        return []
    chapters = epub_navigation(data)
    if not chapters:
        return []
    if pdf_doc is None:
        return [chapter(c.name, c.path, -1, c.level) for c in chapters]
    texts: dict[int, str] = {}

    def page_text(index: int) -> str:
        pdf_page = pdf_pages[index]
        if pdf_page is None or not 0 <= pdf_page < pdf_doc.pageCount():
            return ""
        if pdf_page not in texts:
            texts[pdf_page] = pdf_doc.getAllText(pdf_page).text()
        return texts[pdf_page]

    pages = place_by_title(chapters, page_text, len(pdf_pages))
    return [chapter(c.name, c.path, p, c.level) for c, p in zip(chapters, pages)]

