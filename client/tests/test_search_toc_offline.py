"""Offline tests for in-document search (DocumentSearchController, Ctrl+F) and tables of
contents (LibraryController.loadTableOfContents): ui/docsearch.py finds the text, the
bridges keep the app's state.

Search: PDF text through QtPdf (case-insensitive, white space runs alike), PDF pages
mapped through the page list (`redir`), so a blank page inserted before a PDF page, or
pages moved, put each hit on the page that shows it; typed text (.rm v6 RootText)
through the editor's own layout; the controller's stepping (forward, backward, wrapping,
not wrapping while it runs), cancel(), and a newer search replacing an older one; the
overlay's boxes where the text is drawn, at any zoom, scroll position, page size and on
a 2x screen; and the whole thing through the app's own QML (Ctrl+F, typing, Return,
Shift+Return, Escape).

Tables of contents: a PDF's nested outline, a page list with a blank page inserted
before a chapter and moved pages, an EPUB's navigation (EPUB 3 nav and EPUB 2 NCX)
placed on the pages of its PDF rendition, documents without any.

All ids, names and texts are synthetic. Run:
    QT_QPA_PLATFORM=offscreen client/.venv/bin/python -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import gc
import math
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QPointF, QRect, QRectF, QSize, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtPdf import QPdfDocument  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakecloud import FakeCloud, paired_auth  # noqa: E402
from pdffixtures import make_epub, make_text_pdf  # noqa: E402
from rmfixtures import ZERO, paragraph_format, root_text, text_item, v6_file  # noqa: E402

from core.sync import SyncClient  # noqa: E402
from ui import docsearch  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.pages import DocumentPages, render_content  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

PDF_DOC = "a1a1a1a1-0000-4000-8000-000000000001"       # A4, blank page, Letter landscape, slide
MOVED_DOC = "a1a1a1a1-0000-4000-8000-000000000002"     # the same PDF, pages moved around
NOTE_DOC = "a1a1a1a1-0000-4000-8000-000000000003"      # a notebook with typed text
EPUB_DOC = "a1a1a1a1-0000-4000-8000-000000000004"      # an ebook, nav document, blank page inserted
NCX_DOC = "a1a1a1a1-0000-4000-8000-000000000005"       # an ebook with an EPUB 2 NCX only
PLAIN_DOC = "a1a1a1a1-0000-4000-8000-000000000006"     # a PDF without an outline
BIG_DOC = "a1a1a1a1-0000-4000-8000-000000000007"       # 300 pages, for cancelling
P = [f"b2b2b2b2-0000-4000-8000-0000000000{n:02d}" for n in range(40)]
TABLET_UUID = "00010203-0405-0607-0809-0a0b0c0d0e0f"
THEN = 1_730_000_000_000
PX = 226 / 72

# (width, height, [(x, baseline, size, text)]) in points
PAGES = [
    (595, 842, [(72, 100, 24, "Needle in a haystack"), (72, 300, 12, "another needle here")]),     # A4
    (792, 612, [(50, 60, 18, "Nothing to see on this page")]),                                     # Letter landscape
    (960, 540, [(400, 500, 30, "NEEDLE at the end")]),                                            # 16:9 slide
]
OUTLINE = [("Chapter 1", 0, []), ("Chapter 2", 1, [("Section 2.1", 2, []), ("Section 2.2", 2, [])])]
TYPED = ("Alpha needle beta\nA much longer paragraph of body text that wraps over more than one line, "
         "and the needle is near its end: needle.")
BOOK = [("Prologue", 1, []), ("The Long Road", 2, [("A Detour", 3, [])]), ("Epilogue", 4, [])]
BOOK_PAGES = [(420, 595, [(40, 80, 20, "Prologue"), (40, 120, 11, "It begins.")]),
              (420, 595, [(40, 80, 20, "The Long Road"), (40, 120, 11, "Walking on.")]),
              (420, 595, [(40, 80, 11, "Still walking. A Detour comes.")]),
              (420, 595, [(40, 80, 20, "Epilogue")])]


def page_entry(page_id, idx, redir=None, template="Blank"):
    entry = {"id": page_id, "idx": {"timestamp": "1:2", "value": idx},
             "template": {"timestamp": "1:2", "value": template}}
    if redir is not None:
        entry["redir"] = {"timestamp": "1:2", "value": redir}
    return entry


def content(file_type, entries, original):
    return {"fileType": file_type, "formatVersion": 2, "orientation": "portrait", "pageCount": len(entries),
            "coverPageNumber": 0, "extraMetadata": {}, "tags": [],
            "cPages": {"lastOpened": {"timestamp": "1:1", "value": entries[0]["id"]},
                       "original": {"timestamp": "1:1", "value": original},
                       "pages": entries, "uuids": [{"first": TABLET_UUID, "second": 1}]}}


def meta(name):
    return {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": str(THEN),
            "createdTime": str(THEN), "pinned": False, "lastOpened": str(THEN), "lastOpenedPage": 0}


def typed_page(text: str) -> bytes:
    return v6_file(root_text([text_item((1, 1), ZERO, text=text)], [paragraph_format(ZERO, 1)]))


def build_cloud() -> FakeCloud:
    pdf = make_text_pdf(PAGES, OUTLINE)
    cloud = FakeCloud()
    # A blank page inserted before Chapter 2's PDF page.
    cloud.put_item(PDF_DOC, meta("Search paper"),
                   content("pdf", [page_entry(P[0], "ba", 0), page_entry(P[1], "bb", None, "P Lines small"),
                                   page_entry(P[2], "bc", 1), page_entry(P[3], "bd", 2)], 3),
                   {f"{PDF_DOC}.pdf": pdf})
    # Moved: the slide first, then the A4 page, a blank page, the Letter page.
    cloud.put_item(MOVED_DOC, meta("Moved paper"),
                   content("pdf", [page_entry(P[4], "ba", 2), page_entry(P[5], "bb", 0),
                                   page_entry(P[6], "bc", None), page_entry(P[7], "bd", 1)], 3),
                   {f"{MOVED_DOC}.pdf": pdf})
    cloud.put_item(NOTE_DOC, meta("Typed notes"),
                   content("notebook", [page_entry(P[8], "ba"), page_entry(P[9], "bb"), page_entry(P[10], "bc")], 0),
                   {f"{NOTE_DOC}/{P[8]}.rm": typed_page(TYPED), f"{NOTE_DOC}/{P[9]}.rm": typed_page("Nothing here"),
                    f"{NOTE_DOC}/{P[10]}.rm": typed_page("Last NEEDLE")})
    book_pdf = make_text_pdf(BOOK_PAGES)
    # A blank page inserted before "The Long Road".
    cloud.put_item(EPUB_DOC, meta("A synthetic novel"),
                   content("epub", [page_entry(P[11], "ba", 0), page_entry(P[12], "bb", None),
                                    page_entry(P[13], "bc", 1), page_entry(P[14], "bd", 2),
                                    page_entry(P[15], "be", 3)], 4),
                   {f"{EPUB_DOC}.pdf": book_pdf, f"{EPUB_DOC}.epub": make_epub(BOOK)})
    cloud.put_item(NCX_DOC, meta("An older novel"),
                   content("epub", [page_entry(P[16 + n], f"b{'abcd'[n]}", n) for n in range(4)], 4),
                   {f"{NCX_DOC}.pdf": book_pdf, f"{NCX_DOC}.epub": make_epub(BOOK, nav=False)})
    cloud.put_item(PLAIN_DOC, meta("No outline"),
                   content("pdf", [page_entry(P[20], "ba", 0), page_entry(P[21], "bb", 1)], 2),
                   {f"{PLAIN_DOC}.pdf": make_text_pdf(PAGES[:2])})
    many = [(300, 400, [(20, 40, 10, f"Page {n} text, needle {n}")]) for n in range(300)]
    cloud.put_item(BIG_DOC, meta("Textbook"), {"fileType": "pdf", "formatVersion": 1, "pageCount": 300, "pages": None},
                   {f"{BIG_DOC}.pdf": make_text_pdf(many)})
    return cloud


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(condition, ms: int = 8000) -> bool:
    for _ in range(ms // 20):
        if condition():
            return True
        wait(20)
    return bool(condition())


def open_pdf(data: bytes):
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    doc = QPdfDocument()
    doc.load(buffer)
    doc._buffer = buffer
    return doc


def rects(results) -> list[list[QRectF]]:
    return [[m["boundingRect"] for m in r["metrics"]] for r in results]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(cls.dir / "auth", cls.cloud), cls.dir / "cache"))
        cls.source.refresh()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def doc(self, doc_id):
        return self.source.sync.tree.get(doc_id)


class MatchingTests(unittest.TestCase):
    def test_case_and_white_space_do_not_matter(self):
        self.assertEqual(docsearch.find_all("Needle, needle\r\nNEEDLE", "needle"), [(0, 6), (8, 14), (16, 22)])
        self.assertEqual(docsearch.find_all("a long\r\n  phrase", "LONG PHRASE"), [(2, 16)])
        self.assertEqual(docsearch.find_all("aaaa", "aa"), [(0, 2), (2, 4)])          # not overlapping
        self.assertEqual(docsearch.find_all("anything", "   "), [])
        self.assertEqual(docsearch.find_all("Straße STRASSE", "strasse"), [(0, 6), (7, 14)])

    def test_indices_count_utf16_units_as_pdfium_and_typed_text_do(self):
        self.assertEqual(docsearch.find_all("\U0001F600 needle", "needle"), [(3, 9)])


class PdfSearchTests(Base):
    def setUp(self):
        self.pdf = open_pdf(make_text_pdf(PAGES, OUTLINE))

    def test_hits_lie_on_the_text_of_pages_of_any_size(self):
        # Where Helvetica puts the text: x from the given x, the box around the baseline
        # (cap height above it, descenders below), at 226/72 px per point, x = 0 on the
        # page's centre line.
        for n, (width, height, texts) in enumerate(PAGES):
            hits = docsearch.pdf_hits(self.pdf, n, "needle")
            expected = [t for t in texts if "needle" in t[3].lower()]
            self.assertEqual(len(hits), len(expected), n)
            for metrics, (x, baseline, size, text) in zip(hits, expected):
                self.assertEqual(len(metrics), 1)
                box = metrics[0]["boundingRect"]
                start = text.lower().index("needle")
                left = (x - width / 2) * PX
                self.assertGreater(box.left(), left - 1)
                self.assertLess(box.left(), left + (start * 0.8 * size + 4) * PX, (n, box))
                self.assertLess(abs(box.bottom() - baseline * PX), 0.3 * size * PX, (n, box))
                self.assertLess(abs(box.height() - 0.72 * size * PX), 0.35 * size * PX, (n, box))
                self.assertAlmostEqual(box.width(), 6 * 0.55 * size * PX, delta=1.5 * size * PX)

    def test_a_hit_over_two_lines_has_a_box_per_line(self):
        pdf = open_pdf(make_text_pdf([(595, 842, [(72, 100, 12, "the end of a long"), (72, 120, 12, "phrase starts")])]))
        [hit] = docsearch.pdf_hits(pdf, 0, "long phrase")
        self.assertEqual(len(hit), 2)
        self.assertLess(hit[0]["boundingRect"].bottom(), hit[1]["boundingRect"].top() + 5)

    def test_text_set_outside_the_page_is_not_found(self):
        # Found on the tablet's own EPUB renditions: running text above the page's top.
        pdf = open_pdf(make_text_pdf([(400, 500, [(40, -30, 12, "hidden needle"), (40, 100, 12, "seen needle")])]))
        [hit] = docsearch.pdf_hits(pdf, 0, "needle")
        self.assertGreater(hit[0]["boundingRect"].top(), 0)

    def test_flat_character_boxes_still_give_a_box(self):
        [line] = docsearch._lines([QRectF(10, 20, 9.4, 0)])
        self.assertEqual((line.width(), line.height()), (9.4, docsearch.MIN_BOX_PT))
        self.assertEqual(line.center(), QPointF(14.7, 20))
        # Beside real glyphs, flat ones (spaces, generated line ends) add nothing.
        self.assertEqual(docsearch._lines([QRectF(0, 0, 5, 10), QRectF(5, 10, 3, 0), QRectF(8, 0, 5, 10)]),
                         [QRectF(0, 0, 13, 10)])

    def test_document_pages_map_through_redir(self):
        """A blank page inserted before a PDF page, pages moved: each hit is on the page
        that shows its PDF page."""
        for doc_id, expected in ((PDF_DOC, {0: 2, 3: 1}), (MOVED_DOC, {0: 1, 1: 2})):
            pages = DocumentPages(self.source, self.doc(doc_id))
            try:
                found = {}
                docsearch.search_pages(pages, "NeEdLe", range(len(pages.pages)),
                                       lambda i, r: found.__setitem__(i, r) if r else None, threading.Event())
            finally:
                pages.close()
            self.assertEqual({k: len(v) for k, v in found.items()}, expected, doc_id)
            self.assertTrue(all(r["page"] == k for k, v in found.items() for r in v))
        # The same PDF page gives the same boxes wherever it is.
        a = DocumentPages(self.source, self.doc(PDF_DOC))
        b = DocumentPages(self.source, self.doc(MOVED_DOC))
        try:
            self.assertEqual(rects(docsearch.page_hits(a.content(3), 3, "needle")),
                             rects(docsearch.page_hits(b.content(0), 0, "needle")))
        finally:
            a.close()
            b.close()

    def test_hits_on_a_page_are_in_reading_order(self):
        hits = docsearch.page_hits(DocumentPages(self.source, self.doc(PDF_DOC)).content(0), 0, "needle")
        tops = [r["metrics"][0]["boundingRect"].top() for r in hits]
        self.assertEqual(tops, sorted(tops))

    def test_cancelled_searches_stop(self):
        pages = DocumentPages(self.source, self.doc(BIG_DOC))
        try:
            cancelled, seen = threading.Event(), []

            def on_page(i, r):
                seen.append(i)
                if i == 4:
                    cancelled.set()
            docsearch.search_pages(pages, "needle", range(len(pages.pages)), on_page, cancelled)
            self.assertEqual(seen, [0, 1, 2, 3, 4])
        finally:
            pages.close()


class TypedTextSearchTests(Base):
    def test_typed_text_hits_are_where_the_editor_lays_them_out(self):
        from ui import textedit
        from viewer.renderer import qt_measure
        scene = parse(typed_page(TYPED))
        hits = docsearch.text_hits(scene, "NEEDLE")
        self.assertEqual(len(hits), 3)
        # The editor's selection boxes over the same characters are the same boxes.
        session = textedit.TextSession.__new__(textedit.TextSession)
        session.scene, session.measure, session._lines = scene, qt_measure, None
        session.text = type("T", (), {"root_text": lambda self: scene.text.root})()
        session.authors = {}
        lines = docsearch.layout_text(scene, qt_measure)
        for (start, end), metrics in zip(docsearch.find_all(scene.text.text, "needle"), hits):
            session.cursor, session.anchor = end, start
            session._lines = lines
            expected = [QRectF(*r) for r in textedit.TextSession.selection_rects(session)]
            self.assertEqual([m["boundingRect"] for m in metrics], expected)
            for m in metrics:
                r = m["boundingRect"]
                self.assertTrue(r.top() < m["baseline"] < r.bottom())

    def test_typed_text_is_searched_on_notebook_pages(self):
        pages = DocumentPages(self.source, self.doc(NOTE_DOC))
        try:
            found = {}
            docsearch.search_pages(pages, "needle", range(3), found.__setitem__, threading.Event())
        finally:
            pages.close()
        self.assertEqual({k: len(v) for k, v in found.items()}, {0: 3, 1: 0, 2: 1})


class ControllerTests(Base):
    """DocumentSearchController as SearchResultsHeader.qml drives it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ui.quick.bridge import scene
        from ui.quick.bridge.library import Library
        cls.scene = scene
        scene.set_page_source(lambda: cls.source)
        cls.library = Library()
        cls.library.set_tree(cls.source.sync.tree)

    def make(self, doc_id):
        controller = self.scene.DocumentSearchController()
        self.addCleanup(controller.cancel)
        self.opened, self.located = [], []
        controller.openPage.connect(self.opened.append)
        controller.resultLocationChanged.connect(self.located.append)
        controller.setProperty("document", self.library.entryForId(doc_id))
        controller.setProperty("currentPageNumber", 0)
        return controller

    def run_search(self, controller, term, page=0, page_only=False):
        controller.search(term, page, page_only)
        self.assertTrue(controller.property("running"))
        self.assertTrue(wait_for(lambda: not controller.property("running")))

    def state(self, c):
        return (c.property("currentResultIndex"), c.property("totalResults"), c.property("currentPageNumber"),
                c.property("filteredResultIndex"), len(c.property("filteredResults")))

    def test_search_cycles_forward_and_backward_and_wraps(self):
        c = self.make(PDF_DOC)
        self.run_search(c, "NEEDLE")
        self.assertEqual(self.state(c), (0, 3, 0, 0, 2))
        self.assertEqual(c.property("lastSearchedPage"), 3)
        self.assertEqual(self.opened, [])                       # already on its page
        self.assertEqual(self.located[-1], c.resultLocation())
        self.assertEqual((c.property("hasNextResult"), c.property("hasPreviousResult")), (True, False))
        c.incrementCurrentResultIndex()
        self.assertEqual(self.state(c), (1, 3, 0, 1, 2))
        c.incrementCurrentResultIndex()                         # the slide, after the inserted blank page
        self.assertEqual(self.state(c), (2, 3, 3, 0, 1))
        self.assertEqual(self.opened, [3])
        self.assertEqual((c.property("hasNextResult"), c.property("hasPreviousResult")), (False, True))
        c.incrementCurrentResultIndex()                         # wraps
        self.assertEqual(self.state(c), (0, 3, 0, 0, 2))
        self.assertEqual(self.opened, [3, 0])
        c.decrementCurrentResultIndex()                         # wraps back
        self.assertEqual(self.state(c), (2, 3, 3, 0, 1))
        c.decrementCurrentResultIndex()
        self.assertEqual(self.state(c), (1, 3, 0, 1, 2))
        # The view moved to another page meanwhile: its hits, none active.
        c.setProperty("currentPageNumber", 3)
        self.assertEqual(self.state(c), (1, 3, 3, -1, 1))
        self.assertEqual(c.resultLocation(), QRectF())

    def test_the_cursor_starts_at_or_before_the_page_searched_from(self):
        c = self.make(PDF_DOC)
        self.run_search(c, "needle", 2)                         # the Letter page has none: page 0's first
        self.assertEqual(self.state(c)[:3], (0, 3, 0))
        self.run_search(c, "needle", 3)
        self.assertEqual(self.state(c)[:3], (2, 3, 3))
        self.assertEqual(self.opened[-1], 3)
        c = self.make(MOVED_DOC)                                # slide first, then A4
        self.run_search(c, "needle", 1)
        self.assertEqual(self.state(c), (1, 3, 1, 0, 2))

    def test_page_only(self):
        c = self.make(PDF_DOC)
        self.run_search(c, "needle", 3, True)
        self.assertEqual(self.state(c), (0, 1, 3, 0, 1))
        self.assertEqual(self.opened, [3])
        self.run_search(c, "needle", 1, True)                   # the blank page
        self.assertEqual(c.property("totalResults"), 0)

    def test_typed_text_in_a_notebook(self):
        c = self.make(NOTE_DOC)
        self.run_search(c, "needle")
        self.assertEqual(self.state(c), (0, 4, 0, 0, 3))
        for _ in range(3):
            c.incrementCurrentResultIndex()
        self.assertEqual(self.state(c), (3, 4, 2, 0, 1))
        box = c.resultLocation()
        self.assertTrue(box.isValid())
        self.assertEqual(self.located[-1], box)

    def test_nothing_found_and_nothing_searched(self):
        c = self.make(PDF_DOC)
        self.run_search(c, "absent words")
        self.assertEqual(self.state(c), (0, 0, 0, -1, 0))
        c.incrementCurrentResultIndex()
        c.decrementCurrentResultIndex()
        self.assertEqual(self.state(c), (0, 0, 0, -1, 0))
        for term in ("", "   "):
            c.search(term, 0, False)
            self.assertFalse(c.property("running"))

    def test_cancel_drops_everything_and_nothing_comes_after(self):
        c = self.make(BIG_DOC)
        c.search("needle", 0, False)
        self.assertTrue(wait_for(lambda: c.property("totalResults") > 0))
        c.cancel()
        self.assertEqual((c.property("running"), c.property("totalResults"), c.property("lastSearchedPage"),
                          c.property("currentPageNumber"), c.property("filteredResultIndex")), (False, 0, -1, -1, -1))
        wait(300)
        self.assertEqual(c.property("totalResults"), 0)
        self.assertEqual(c.property("filteredResults"), [])

    def test_no_wrap_while_running(self):
        c = self.make(PDF_DOC)
        hit = [docsearch.result(0, [docsearch.metric(QRectF(0, 0, 10, 10))])]
        c._generation = generation = 1000           # a search the test plays the part of
        c._set("running", True)
        c._on_page(generation, 0, hit)
        c._on_page(generation, 1, [dict(hit[0], page=1)])
        self.assertEqual(self.state(c), (0, 2, 0, 0, 1))
        c.incrementCurrentResultIndex()
        self.assertEqual(self.state(c), (1, 2, 1, 0, 1))
        c.incrementCurrentResultIndex()             # the last hit so far: no wrapping while it runs
        c._on_page(generation - 1, 2, [dict(hit[0], page=2)])      # an older search's: dropped
        self.assertEqual(self.state(c), (1, 2, 1, 0, 1))
        c._on_finished(generation)
        self.assertFalse(c.property("running"))
        c.incrementCurrentResultIndex()
        self.assertEqual(self.state(c), (0, 2, 0, 0, 1))

    def test_a_newer_search_replaces_an_older_one(self):
        c = self.make(BIG_DOC)
        c.search("needle 7", 0, False)                          # "needle 7", "needle 70".."79", ...
        c.search("needle 29", 0, False)                         # replaces it at once
        self.assertTrue(wait_for(lambda: not c.property("running"), 30000))
        self.assertEqual(c.property("totalResults"), 11)        # "needle 29", "needle 290".."299"
        found = [c._by_page[p] for p in sorted(c._by_page) if c._by_page[p]]
        self.assertEqual([r[0]["page"] for r in found], [29] + list(range(290, 300)))
        self.assertEqual(c.property("lastSearchedPage"), 299)
        self.assertEqual(self.state(c)[:3], (0, 11, 29))       # nothing at or before page 0: hit 0

    def test_another_document_cancels(self):
        c = self.make(PDF_DOC)
        self.run_search(c, "needle")
        c.setProperty("document", self.library.entryForId(NOTE_DOC))
        self.assertEqual(c.property("totalResults"), 0)

    def test_highlight_details_search_one_page(self):
        c = self.make(PDF_DOC)
        c.forwardHighlightDetails(3, {"hasDetails": True, "currentSearchText": "needle"})
        self.assertTrue(wait_for(lambda: not c.property("running") and c.property("totalResults") == 1))


class OverlayTests(Base):
    """PageSearchResultOverlayItem's boxes where the page shows the text, through the
    tile manager, at any zoom and scroll position, on pages of any shape, and on a 2x
    screen."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ui.quick.bridge import scene
        cls.scene = scene

    def overlay(self, tiles, results, active=0, size=(900, 700)):
        item = self.scene.PageSearchResultOverlayItem()
        item.setSize(QSize(*size).toSizeF())
        item.setProperty("tileManager", tiles)
        item.setProperty("visibleResults", results)
        item.setProperty("activeResultIndex", active)
        item.setProperty("highlightColor", QColor("#4034A7FF"))
        item.setProperty("activeResultColor", QColor("#8035A7FF"))
        return item

    def tiles(self, size, bounds, center, scale):
        t = self.scene.SceneTileManager()
        t.setProperty("viewportSize", QSize(*size).toSizeF())
        t._values["sceneBoundingRect"] = bounds
        t.setFocalPoint(center, scale)
        return t

    def paint(self, item, dpr=1.0):
        image = QImage(round(item.width() * dpr), round(item.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.scale(dpr, dpr)             # the scene graph hands paint() a painter scaled so
        item.paint(painter)
        painter.end()
        return image

    def test_boxes_cover_the_drawn_text_at_any_zoom_scroll_and_page_size(self):
        """The page is rendered as SceneView renders it, at the tile manager's scale and
        offset; every dark pixel of each hit's word lies inside its box, and the box hugs
        it (within a few pixels)."""
        pages = DocumentPages(self.source, self.doc(PDF_DOC))
        try:
            for index in (0, 3):
                content = pages.content(index)
                frame = self.scene.to_scene(content.frame())
                results = docsearch.page_hits(content, index, "needle")
                for scale, dx, dy in ((0.25, 0, 0), (0.5, 120, 300), (1.3, -200, 150), (2.0, 0, 0)):
                    for n, found in enumerate(results):
                        box = found["metrics"][0]["boundingRect"]
                        center = box.center() + QPointF(dx / scale, dy / scale)
                        tiles = self.tiles((900, 700), frame, center, scale)
                        item = self.overlay(tiles, results, active=n)
                        [(i, view)] = [r for r in item.view_rects() if r[0] == n]
                        self.assertEqual(view, tiles.sceneToView(box).toRect())
                        # The page as the view draws it, cropped to the viewport.
                        page = render_content(content, scale, "clip")
                        origin = tiles.sceneToView(frame.topLeft())
                        # PDFium's glyph boxes are tight to the outlines; the rendered
                        # glyphs' anti-aliasing reaches up to 0.4 pt past them.
                        spill = max(1, math.ceil(0.4 * PX * scale))
                        # Above and below the box, over its width: no ink of the word.
                        dark = self.dark_pixels(page, origin, view.adjusted(0, -12 - spill, 0, 12 + spill))
                        self.assertTrue(dark, (index, scale, n))
                        inside = [p for p in dark if view.adjusted(0, -spill, 0, spill).contains(p)]
                        self.assertEqual(len(inside), len(dark), (index, scale, n))
                        # Left and right of it (spaces there): none either.
                        for band in (QRect(view.left() - spill - 2, view.top(), 2, view.height()),
                                     QRect(view.right() + spill + 1, view.top(), 2, view.height())):
                            self.assertEqual(self.dark_pixels(page, origin, band), [], (index, scale, n))
                        hull = QRect(min(p.x() for p in dark), min(p.y() for p in dark), 1, 1)
                        hull.setRight(max(p.x() for p in dark))
                        hull.setBottom(max(p.y() for p in dark))
                        slack = max(3, round(3 * scale))
                        self.assertLessEqual(hull.left() - view.left(), slack + round(2 * scale), (index, scale))
                        self.assertLessEqual(view.right() - hull.right(), slack + round(2 * scale), (index, scale))
        finally:
            pages.close()

    @staticmethod
    def dark_pixels(page: QImage, origin: QPointF, area: QRect) -> list:
        out = []
        for y in range(area.top(), area.bottom() + 1):
            for x in range(area.left(), area.right() + 1):
                px, py = round(x - origin.x()), round(y - origin.y())
                if 0 <= px < page.width() and 0 <= py < page.height() and QColor(page.pixel(px, py)).lightness() < 110:
                    out.append(QPointF(x, y).toPoint())
        return out

    def test_painted_boxes_active_one_distinct_and_crisp_at_1x_and_2x(self):
        pages = DocumentPages(self.source, self.doc(PDF_DOC))
        try:
            content = pages.content(0)
            results = docsearch.page_hits(content, 0, "needle")
        finally:
            pages.close()
        tiles = self.tiles((900, 700), self.scene.to_scene(content.frame()), QPointF(-300, 600), 0.6)
        item = self.overlay(tiles, results, active=1)
        drawn = item.view_rects()
        self.assertEqual(len(drawn), 2)
        for dpr in (1.0, 2.0):
            image = self.paint(item, dpr)
            for index, rect in drawn:
                inner = QRect(round(rect.left() * dpr) + 1, round(rect.top() * dpr) + 1,
                              round(rect.width() * dpr) - 2, round(rect.height() * dpr) - 2)
                alpha = QColor.fromRgba(image.pixel(inner.center())).alpha()
                self.assertEqual(alpha, 0x80 if index == 1 else 0x40, (dpr, index))
                for corner in (inner.topLeft(), inner.bottomRight()):
                    self.assertGreater(QColor.fromRgba(image.pixel(corner)).alpha(), 0, (dpr, index))
                # Crisp: nothing just outside the box.
                for outside in (QPointF(round(rect.left() * dpr) - 2, inner.center().y()),
                                QPointF(inner.center().x(), round(rect.bottom() * dpr) + 3 * dpr)):
                    self.assertEqual(QColor.fromRgba(image.pixel(outside.toPoint())).alpha(), 0, (dpr, index))
        # Moving the view repaints at the new place (transformChanged -> updateIfVisible).
        before = item.view_rects()
        tiles.moveViewBy(QPointF(40, -25))
        after = item.view_rects()
        self.assertNotEqual(before, after)
        self.assertEqual([r for _, r in after],
                         [tiles.sceneToView(r["metrics"][0]["boundingRect"]).toRect() for r in results])

    def test_nothing_without_results_or_tile_manager(self):
        item = self.scene.PageSearchResultOverlayItem()
        item.setSize(QSize(100, 100).toSizeF())
        item.setProperty("visibleResults", [docsearch.result(0, [docsearch.metric(QRectF(0, 0, 10, 10))])])
        self.assertEqual(item.view_rects(), [])
        self.assertEqual(self.paint(item).pixel(5, 5), 0)


class TableOfContentsTests(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ui.quick.bridge.library import Library, LibraryController
        cls.library = Library()
        cls.library.set_tree(cls.source.sync.tree)
        cls.controller = LibraryController(cls.library, lambda *a: None)
        cls.controller.source = lambda: cls.source

    def load(self, doc_id):
        entry = self.library.entryForId(doc_id)
        got, running = [], []
        entry.tableOfContentsUpdated.connect(got.append)
        entry.tocRunningChanged.connect(lambda: running.append(entry.property("tocRunning")))
        self.controller.loadTableOfContents(doc_id)
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(running, [True, False])
        return got[0]

    def test_a_pdf_outline_nested_with_a_blank_page_inserted_before_a_chapter(self):
        self.assertEqual(self.load(PDF_DOC), [
            {"name": "Chapter 1", "path": "", "page": 0, "level": 0},
            {"name": "Chapter 2", "path": "", "page": 2, "level": 0},       # PDF page 1, after the blank page
            {"name": "Section 2.1", "path": "", "page": 3, "level": 1},
            {"name": "Section 2.2", "path": "", "page": 3, "level": 1}])

    def test_moved_pages(self):
        self.assertEqual([(c["name"], c["page"]) for c in self.load(MOVED_DOC)],
                         [("Chapter 1", 1), ("Chapter 2", 3), ("Section 2.1", 0), ("Section 2.2", 0)])

    def test_a_deleted_chapter_page_lands_on_what_follows(self):
        self.assertEqual(docsearch.page_of_pdf_page([0, None, 2, 3], 1), 2)
        self.assertEqual(docsearch.page_of_pdf_page([0, 1], 5), -1)
        self.assertEqual(docsearch.page_of_pdf_page([3, 1, 1], 1), 1)

    def test_an_ebook_navigation_placed_on_its_rendition_with_a_blank_page_inserted(self):
        self.assertEqual(self.load(EPUB_DOC), [
            {"name": "Prologue", "path": "OEBPS/text/ch1.xhtml#top", "page": 0, "level": 0},
            {"name": "The Long Road", "path": "OEBPS/text/ch2.xhtml#top", "page": 2, "level": 0},
            {"name": "A Detour", "path": "OEBPS/text/ch3.xhtml#top", "page": 3, "level": 1},
            {"name": "Epilogue", "path": "OEBPS/text/ch4.xhtml#top", "page": 4, "level": 0}])

    def test_an_ebook_with_an_ncx_only(self):
        self.assertEqual([(c["name"], c["page"], c["level"]) for c in self.load(NCX_DOC)],
                         [("Prologue", 0, 0), ("The Long Road", 1, 0), ("A Detour", 2, 1), ("Epilogue", 3, 0)])

    def test_documents_without_one(self):
        for doc_id in (PLAIN_DOC, NOTE_DOC, BIG_DOC):
            self.assertEqual(self.load(doc_id), [], doc_id)
        self.controller.loadTableOfContents("ffffffff-0000-4000-8000-00000000dead")     # not in the library
        self.assertEqual(docsearch.epub_navigation(make_epub([])), [])
        with self.assertRaises(ValueError):
            docsearch.epub_navigation(b"PK\x03\x04 not an ebook")

    def test_a_title_not_found_stays_on_the_previous_page(self):
        texts = ["Intro", "", "Part Two here", "Index"]
        chapters = [docsearch.Chapter("Intro", 0), docsearch.Chapter("Missing", 0), docsearch.Chapter("PART  two", 0)]
        self.assertEqual(docsearch.place_by_title(chapters, texts.__getitem__, 4), [0, 0, 2])


class QuickTests(unittest.TestCase):
    """Search through the app's own QML: Ctrl+F opens the search bar, the term is typed
    and Return searches; Return and Shift+Return step, the bar counts ("2 / 3") and
    the page turns; the highlight boxes are drawn on the page where the words are;
    Escape closes it all."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", build_cloud()), tmp / "cache"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        cls.tq.wait(50)
        cls.tmp.cleanup()

    def tearDown(self):
        self.tq.EditingTests.close_document(self)

    def wait_for(self, condition, ms=8000):
        return self.tq.WorkflowTests.wait_for(self, condition, ms)

    def qml(self, prefix):
        return self.tq.WorkflowTests.qml(self, prefix)

    def scene_views(self):
        return self.tq.WorkflowTests.scene_views(self)

    def key(self, key, modifiers=Qt.KeyboardModifier.NoModifier):
        from PySide6.QtTest import QTest
        self.window.requestActivate()
        QTest.keyClick(self.window, key, modifiers)
        self.tq.wait(30)

    def type_text(self, text):
        """Typed, as the system hands keys to the window (QTest.keyClicks wants a widget)."""
        from PySide6.QtTest import QTest
        self.window.requestActivate()
        for c in text:
            QTest.keyClick(self.window, Qt.Key(ord(c.upper())),
                           Qt.KeyboardModifier.ShiftModifier if c.isupper() else Qt.KeyboardModifier.NoModifier)
        self.tq.wait(30)

    def test_ctrl_f_type_return_step_and_escape(self):
        from ui.quick.bridge import scene
        view = self.tq.EditingTests.open_document(self, PDF_DOC)
        viewport = self.qml("DocumentViewport")
        header = self.qml("SearchResultsHeader")
        self.assertFalse(header.property("visible"))
        self.key(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: header.property("visible")))
        [controller] = header.findChildren(scene.DocumentSearchController)
        self.controller = controller
        self.type_text("needle")                        # the PDF has "Needle" and "NEEDLE"
        self.assertEqual(header.property("text"), "needle")
        # "Page only" starts ticked, so typing searches the page shown at once.
        self.assertTrue(self.wait_for(lambda: controller.property("totalResults") == 2))
        self.assertEqual(self.label(header), "1 / 2")
        overlays = [o for o in viewport.findChildren(scene.PageSearchResultOverlayItem)]
        self.assertEqual(len(overlays), 1)
        overlay = overlays[0]
        self.assertTrue(self.wait_for(lambda: overlay.isVisible() and len(overlay.view_rects()) == 2))
        self.assert_boxes_on_text(view, overlay)
        # The whole document: untick "Page only", search with Return.
        header.setProperty("pageOnly", False)
        self.key(Qt.Key.Key_Return)
        self.assertTrue(self.wait_for(lambda: controller.property("totalResults") == 3
                                      and not controller.property("running")))
        self.assertEqual(self.label(header), "1 / 3")
        self.key(Qt.Key.Key_Return)
        self.assertEqual(self.label(header), "2 / 3")
        self.assertEqual(overlay.property("activeResultIndex"), 1)
        self.key(Qt.Key.Key_Return)                     # on to the slide, past the inserted blank page
        self.assertEqual(self.label(header), "3 / 3")
        self.assertTrue(self.wait_for(lambda: viewport.property("currentPage") == 3))
        self.assertTrue(self.wait_for(lambda: any(v.property("status") == 2 and v.property("pageId") == P[3]
                                                  for v in self.scene_views())))
        slide = next(v for v in self.scene_views() if v.property("pageId") == P[3])
        self.assertTrue(self.wait_for(lambda: len(overlay.view_rects()) == 1 and slide._image is not None))
        self.assert_boxes_on_text(slide, overlay)
        self.key(Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.label(header), "2 / 3")
        self.assertTrue(self.wait_for(lambda: viewport.property("currentPage") == 0))
        self.key(Qt.Key.Key_Escape)
        self.assertTrue(self.wait_for(lambda: not header.property("visible")))
        self.assertEqual(controller.property("totalResults"), 0)
        self.assertFalse(overlay.isVisible())
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def label(self, header) -> str:
        controller = self.controller
        if not header.property("committedSearchInput"):
            return ""
        total = controller.property("totalResults")
        return f"{controller.property('currentResultIndex') + 1} / {total}" if total else "0 / 0"

    def assert_boxes_on_text(self, view, overlay):
        """The window as shown: inside each box the page's text shows through tinted
        blue (dark and bluish pixels), and the box holds all of the word's ink."""
        self.tq.wait(300)
        image = self.window.grabWindow()
        dpr = image.devicePixelRatio() or 1.0
        for index, rect in overlay.view_rects():
            box = overlay.mapRectToScene(QRectF(rect)).toRect()
            tinted = dark = 0
            for y in range(box.top() + 1, box.bottom()):
                for x in range(box.left() + 1, box.right()):
                    c = QColor(image.pixel(round(x * dpr), round(y * dpr)))
                    if c.blue() > c.red() + 25:
                        tinted += 1
                    if c.lightness() < 140:
                        dark += 1
            self.assertGreater(tinted, box.width() * box.height() * 0.3, (index, box))
            self.assertGreater(dark, 3, (index, box))     # the word's ink shows through
            # Just outside the box: no tint.
            ring = [QPointF(box.left() - 4, box.center().y()), QPointF(box.right() + 4, box.center().y())]
            for p in ring:
                c = QColor(image.pixel(round(p.x() * dpr), round(p.y() * dpr)))
                self.assertLessEqual(c.blue() - c.red(), 25, (index, p))


if __name__ == "__main__":
    unittest.main()
