"""Offline tests for selecting and copying the text of PDF and EPUB pages (ui/pdftext.py,
SceneController's glyph slots, the app's own DocumentViewport / PdfTextSelection QML).
Synthetic PDFs and ids only.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_pdf_text_offline.py'
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402,F401  (QApplication and the app's QML set up first)

from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QPointF, QRectF, QSizeF, Qt  # noqa: E402
from PySide6.QtGui import QFont, QGuiApplication, QPageLayout, QPageSize, QPainter, QPdfWriter  # noqa: E402
from PySide6.QtPdf import QPdfDocument  # noqa: E402

from core.sync import SyncClient  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from ui import docsearch, pdftext  # noqa: E402
from ui.library import LibrarySource  # noqa: E402


PDF_DOC = "12121212-aaaa-4aaa-8aaa-121212121212"
EPUB_DOC = "34343434-bbbb-4bbb-8bbb-343434343434"
A4, BLANK, LETTER, SLIDE = (f"56565656-0000-4000-8000-00000000000{n}" for n in range(1, 5))
E0 = "78787878-0000-4000-8000-000000000001"
TABLET_UUID = "00010203-0405-0607-0809-0a0b0c0d0e0f"
THEN = 1_730_000_000_000
NAMES = ("Selectable paper", "Selectable novel")

SIZES = [(595.0, 842.0), (792.0, 612.0), (960.0, 540.0)]   # A4, US Letter landscape, 16:9 slide
FONT_PT = 14
# (x, baseline y) in points, text: per page.
LINES = [
    [(72, 100, "Alpha bravo, charlie delta."),
     (72, 130, "Second line of the ﬁrst page."),
     (72, 190, "A new paragraph ends here.")],
    [(80, 120, "Letter landscape words.")],
    [(100, 300, "Slide title text.")],
]
ABOVE_TOP = (72, -20, "Run-in above the page")            # an EPUB rendition's, never shown


def make_pdf() -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setResolution(72)                                # painter units = points

    def layout(size):
        page = QPageSize(QSizeF(*size), QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch)
        writer.setPageLayout(QPageLayout(page, QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
    layout(SIZES[0])
    painter = QPainter(writer)
    font = QFont("DejaVu Sans")
    font.setPixelSize(FONT_PT)
    painter.setFont(font)
    for n, size in enumerate(SIZES):
        if n:
            layout(size)
            writer.newPage()
        for x, y, text in LINES[n]:
            painter.drawText(QPointF(x, y), text)
        if n == 0:
            painter.drawText(QPointF(*ABOVE_TOP[:2]), ABOVE_TOP[2])
    painter.end()
    return bytes(buffer.data())


PDF = make_pdf()


def load(data: bytes = PDF) -> tuple[QPdfDocument, QBuffer]:
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    doc = QPdfDocument()
    doc.load(buffer)
    assert doc.status() == QPdfDocument.Status.Ready
    return doc, buffer


def plain(text: str) -> str:
    """What PDFium reads back for a line written with a ligature character (Qt's PDF
    writer maps the glyph to its letters)."""
    return text.replace("\ufb01", "fi")


def scene_point(page: int, x_pt: float, y_pt: float) -> QPointF:
    """PDF points (top left origin) -> scene: x 226/72, the page centred on x = 0."""
    w = SIZES[page][0] * pdftext.PDF_PX_PER_PT
    return QPointF(x_pt * pdftext.PDF_PX_PER_PT - w / 2, y_pt * pdftext.PDF_PX_PER_PT)


def char_point(page: int, line: int, char: int) -> QPointF:
    """Roughly the middle of character `char` of line `line` (monospace-free estimate
    good enough to land on that character: found through the page's own boxes)."""
    doc, buffer = load()
    try:
        text = doc.getAllText(page).text()
        start = text.index(plain(LINES[page][line][2]))
        box = doc.getSelectionAtIndex(page, start + char, 1).boundingRectangle()
        return scene_point(page, box.center().x(), box.center().y())
    finally:
        doc.close()


# ------------------------------------------------------------------ the engine

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.buffer = load()
        self.addCleanup(self.doc.close)
        self.page = pdftext.PageText(self.doc, 0)

    def test_lines_are_pdfiums_and_text_above_the_page_is_left_out(self):
        texts = [self.page.text[l.start:l.end] for l in self.page.lines]
        self.assertEqual(texts, [plain(t) for _, _, t in LINES[0]])
        self.assertIn(ABOVE_TOP[2], self.page.text)          # PDFium has it...
        for line in self.page.lines:                           # ...but it is not selectable
            self.assertGreaterEqual(line.box.bottom(), 0)

    def test_drag_forward_and_backward_across_lines_copies_lines_joined_by_newlines(self):
        a, b = char_point(0, 0, 6), char_point(0, 1, 5)        # "bravo..." to "Second"
        forward = self.page.select_points(a, b)
        backward = self.page.select_points(b, a)
        self.assertEqual(forward.text, "bravo, charlie delta.\nSecond")
        self.assertEqual(backward.text, forward.text)
        self.assertEqual((forward.start, forward.end), (backward.start, backward.end))
        self.assertEqual(len(forward.areas), 2)                # one area per line
        first, second = forward.areas
        self.assertLess(first.bottom(), second.bottom())
        # Handles: the first area's left and the last one's right, at half their height.
        self.assertEqual(forward.start_handle, QPointF(first.left(), first.center().y()))
        self.assertEqual(forward.end_handle, QPointF(second.right(), second.center().y()))

    def test_range_is_inclusive_and_trimmed_of_spaces(self):
        text = self.page.text
        start = text.index("bravo") - 1                          # the space before it
        sel = self.page.select(start, text.index("bravo") + 4)
        self.assertEqual(sel.text, "bravo")
        self.assertEqual((sel.start, sel.end), (text.index("bravo"), text.index("bravo") + 4))
        self.assertTrue(self.page.select(start, start).empty)      # only a space: nothing

    def test_ligatures_are_copied_as_letters_and_no_spaces_appear_between_characters(self):
        sel = self.page.select_points(char_point(0, 1, 0), char_point(0, 1, 29))
        self.assertEqual(sel.text, "Second line of the first page.")
        # A PDF whose text has the ligature characters themselves (U+FB00-FB06, as
        # PDFium reports a font's ligature glyph without a ToUnicode entry for it).
        self.assertEqual(pdftext.clean("\ufb01nal e\ufb00ort \ufb02ow\r\nnext"), "final effort flow\nnext")

    def test_double_click_selects_the_word_without_punctuation(self):
        self.assertEqual(self.page.word(char_point(0, 0, 8)).text, "bravo")      # "bravo," -> bravo
        self.assertEqual(self.page.word(char_point(0, 2, 23)).text, "here")
        # On punctuation: the glyph itself is no break, its neighbours grow the word.
        self.assertEqual(self.page.word(char_point(0, 0, 11)).text, "bravo,")
        # On whitespace / non-printable glyphs: nothing (3.29's 0x140a508f0).
        blank = self.page.text.index(" ")
        self.assertTrue(self.page.select(blank, blank).empty)

    def test_a_point_off_the_text_snaps_to_the_nearest_character(self):
        left_margin = scene_point(0, 10, 95)                      # left of line 1
        right_margin = scene_point(0, 580, 186)                   # right of line 3
        sel = self.page.select_points(left_margin, right_margin)
        self.assertEqual(sel.text, "\n".join(plain(t) for _, _, t in LINES[0]))
        below = scene_point(0, 300, 800)                          # far below everything
        self.assertEqual(self.page.hit(below), self.page.lines[-1].end - 1)

    def test_areas_cover_the_glyphs_they_select(self):
        sel = self.page.select_points(char_point(0, 0, 0), char_point(0, 0, 26))
        [area] = sel.areas
        box = self.page.box(self.page.lines[0].start, self.page.lines[0].end - self.page.lines[0].start)
        self.assertLessEqual(area.top(), box.top())
        self.assertGreaterEqual(area.bottom(), box.bottom() - 0.01)
        self.assertAlmostEqual(area.left(), box.left(), delta=0.01)
        self.assertAlmostEqual(area.right(), box.right(), delta=0.01)

    def test_other_page_shapes_map_to_their_own_frames(self):
        for n in (1, 2):
            page = pdftext.PageText(self.doc, n)
            text = LINES[n][0][2]
            sel = page.select_points(char_point(n, 0, 0), char_point(n, 0, len(text) - 1))
            self.assertEqual(sel.text, text)
            frame = page.frame
            self.assertEqual((frame.width(), frame.height()),
                             (SIZES[n][0] * pdftext.PDF_PX_PER_PT, SIZES[n][1] * pdftext.PDF_PX_PER_PT))
            for area in sel.areas:
                self.assertTrue(frame.contains(area.center()))

    def test_a_page_without_text_selects_nothing(self):
        doc, buffer = load(make_blank_pdf())
        try:
            page = pdftext.PageText(doc, 0)
            self.assertEqual(page.lines, [])
            self.assertEqual(page.hit(QPointF(0, 100)), -1)
            self.assertTrue(page.select_points(QPointF(0, 0), QPointF(100, 100)).empty)
            self.assertTrue(page.word(QPointF(0, 0)).empty)
        finally:
            doc.close()

    def test_search_still_finds_the_same_boxes_through_the_shared_helpers(self):
        hits = docsearch.pdf_hits(self.doc, 0, "charlie")
        self.assertEqual(len(hits), 1)
        sel = self.page.select(self.page.text.index("charlie"), self.page.text.index("charlie") + 6)
        box, area = hits[0][0]["boundingRect"], sel.areas[0]
        self.assertAlmostEqual(box.left(), area.left(), delta=0.01)
        self.assertAlmostEqual(box.right(), area.right(), delta=0.01)


def make_blank_pdf() -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    painter = QPainter(writer)
    painter.fillRect(QRectF(10, 10, 50, 50), Qt.GlobalColor.red)
    painter.end()
    return bytes(buffer.data())


# ------------------------------------------------------------------ through the app's QML

NOTE_DOC = "9a9a9a9a-cccc-4ccc-8ccc-9a9a9a9a9a9a"
NOTE_PAGE = "9a9a9a9a-0000-4000-8000-000000000001"
NOTE_NAME = "Paste pad"


def page_entry(page_id: str, idx: str, redir: int | None = None) -> dict:
    entry = {"id": page_id, "idx": {"timestamp": "1:2", "value": idx},
             "template": {"timestamp": "1:2", "value": "Blank"}}
    if redir is not None:
        entry["redir"] = {"timestamp": "1:2", "value": redir}
    return entry


def pdf_content(file_type: str, entries: list[dict], original: int) -> dict:
    return {"fileType": file_type, "formatVersion": 2, "orientation": "portrait", "pageCount": len(entries),
            "coverPageNumber": 0, "extraMetadata": {}, "tags": [],
            "cPages": {"lastOpened": {"timestamp": "1:1", "value": entries[0]["id"]},
                       "original": {"timestamp": "1:1", "value": original},
                       "pages": entries, "uuids": [{"first": TABLET_UUID, "second": 1}]}}


def meta(name: str) -> dict:
    return {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": str(THEN),
            "createdTime": str(THEN), "pinned": False, "lastOpened": str(THEN), "lastOpenedPage": 0}


def build_cloud() -> FakeCloud:
    import test_edits_offline as te
    cloud = FakeCloud()
    # A blank page inserted before the Letter page: the Letter page is PDF page 1 by its
    # redir, though it is the document's third page.
    cloud.put_item(PDF_DOC, meta(NAMES[0]),
                   pdf_content("pdf", [page_entry(A4, "ba", 0), page_entry(BLANK, "bb"),
                                       page_entry(LETTER, "bc", 1), page_entry(SLIDE, "bd", 2)], 3),
                   {f"{PDF_DOC}.pdf": PDF})
    cloud.put_item(EPUB_DOC, meta(NAMES[1]), pdf_content("epub", [page_entry(E0, "ba", 0)], 1),
                   {f"{EPUB_DOC}.pdf": PDF, f"{EPUB_DOC}.epub": b"PK\x03\x04 not a real ebook"})
    cloud.put_item(NOTE_DOC, meta(NOTE_NAME), te.notebook_content(NOTE_PAGE), {})
    return cloud


class QuickTests(unittest.TestCase):
    """Selecting and copying PDF / EPUB text through the app's own DocumentViewport and
    PdfTextSelection QML, with the mouse and keys as the system gives them."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq             # its harness; its tests are not collected from here
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", build_cloud()), tmp / "cache"),
                                   editable=NAMES + (NOTE_NAME,))
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

    def setUp(self):
        del self.app.warnings[:]

    def tearDown(self):
        self.tq.EditingTests.close_document(self)
        self.source.sync.edits.clear()

    # -- the shared harness
    def wait_for(self, condition, ms=8000):
        return self.tq.WorkflowTests.wait_for(self, condition, ms)

    def qml(self, prefix):
        return self.tq.WorkflowTests.qml(self, prefix)

    def scene_views(self):
        return self.tq.WorkflowTests.scene_views(self)

    def pens(self, view):
        return self.tq.PenTests.pens(self, view)

    def document_view(self):
        return self.qml("DocumentView_")

    def pending(self, kind=None) -> list:
        return [e for e in self.source.sync.edits.all() if kind is None or type(e).__name__ == kind]

    def open_document(self, doc_id, page_id=None):
        from PySide6.QtCore import Q_ARG, QMetaObject
        view = self.tq.PenTests.open_document(self, doc_id)
        if page_id is not None and view.property("pageId") != page_id:
            index = self.source.staged(self.source.sync.tree.get(doc_id)).ids.index(page_id)
            QMetaObject.invokeMethod(self.qml("DocumentViewport"), "setCurrentPage", Q_ARG("QVariant", index))

            def turned():
                return next((v for v in self.scene_views() if v.property("status") == 2
                             and v.property("pageId") == page_id), None)
            self.assertTrue(self.wait_for(lambda: turned() is not None), page_id)
            view = turned()
        self.assertTrue(self.wait_for(lambda: view.property("status") == 2 and view._image is not None))
        self.tq.wait(600)                       # the app's click counter (500 ms) at rest
        return view

    def selection(self):
        todo = [self.window.contentItem()]
        while todo:
            item = todo.pop()
            if item.objectName() == "pdfTextSelection":
                return item
            todo.extend(item.childItems())
        self.fail("no pdfTextSelection")

    def selected(self) -> str:
        sel = self.selection()
        return sel.property("selectedText") if sel.property("hasSelection") else ""

    def at(self, view, point: QPointF):
        return view.mapToScene(view._tiles.to_view(point)).toPoint()

    def drag(self, view, a: QPointF, b: QPointF, steps: int = 12) -> None:
        from PySide6.QtTest import QTest
        p0, p1 = self.at(view, a), self.at(view, b)
        QTest.mousePress(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
        for k in range(1, steps + 1):
            QTest.mouseMove(self.window, p0 + (p1 - p0) * (k / steps))
            self.tq.wait(10)
        QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        self.tq.wait(600)                       # past the click counter

    def click(self, view, point: QPointF, times: int = 1) -> None:
        from PySide6.QtTest import QTest
        p = self.at(view, point)
        for _ in range(times):
            QTest.mousePress(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
            QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
            self.tq.wait(40)
        self.tq.wait(600)

    def key(self, key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        from PySide6.QtTest import QTest
        self.window.requestActivate()
        QTest.keyClick(self.window, key, modifiers)
        self.tq.wait(60)

    def overlay_rects(self) -> list[QRectF]:
        """The highlight rectangles the app's PdfGlyphSelectionOverlay shows, in window
        coordinates."""
        overlay = self._overlay()
        rects = [r for r in overlay.childItems() if r.metaObject().className().startswith("QQuickRectangle")
                 and r.isVisible() and r.width() > 0]
        return [r.mapRectToScene(QRectF(0, 0, r.width(), r.height())) for r in rects]

    def _overlay(self):
        todo = [self.window.contentItem()]
        while todo:
            item = todo.pop()
            if item.metaObject().className().startswith("PdfGlyphSelectionOverlay"):
                return item
            todo.extend(item.childItems())
        self.fail("no PdfGlyphSelectionOverlay")

    def expected_rects(self, view) -> list[QRectF]:
        """The view's selection areas, where the page drawing puts those scene rectangles
        in the window."""
        out = []
        for area in view._glyph_selection.areas:
            tl, br = view._tiles.to_view(area.topLeft()), view._tiles.to_view(area.bottomRight())
            out.append(view.mapRectToScene(QRectF(tl, br)))
        return out

    # -- the tests
    def test_drag_forward_and_backward_and_copy_with_ctrl_c(self):
        view = self.open_document(PDF_DOC, A4)
        a, b = char_point(0, 0, 6), char_point(0, 1, 5)
        self.drag(view, a, b)
        self.assertTrue(self.wait_for(lambda: self.selected() == "bravo, charlie delta.\nSecond"), self.selected())
        self.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: QGuiApplication.clipboard().text() == "bravo, charlie delta.\nSecond"))
        self.click(view, scene_point(0, 300, 400))                  # a click elsewhere clears it
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        self.assertEqual(self.overlay_rects(), [])
        self.drag(view, b, a)                                          # backward: the same text
        self.assertTrue(self.wait_for(lambda: self.selected() == "bravo, charlie delta.\nSecond"), self.selected())
        self.assertEqual(self.pending(), [])                          # selecting edits nothing
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_double_click_selects_a_word_and_esc_clears(self):
        view = self.open_document(PDF_DOC, A4)
        self.click(view, char_point(0, 0, 14), times=2)               # "charlie"
        self.assertTrue(self.wait_for(lambda: self.selected() == "charlie"), self.selected())
        self.assertEqual(len(self.overlay_rects()), 1)
        dv = self.qml("DocumentView_")
        self.key(Qt.Key.Key_Escape)                                    # clears, the document stays open
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        self.assertEqual(self.overlay_rects(), [])
        self.assertTrue(dv.property("visible"))
        self.click(view, char_point(0, 2, 2), times=3)                # a triple click: a word too (3.29)
        self.assertTrue(self.wait_for(lambda: self.selected() == "new"), self.selected())
        self.key(Qt.Key.Key_Escape)                                    # clears...
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        self.assertTrue(dv.property("visible"))
        self.key(Qt.Key.Key_Escape)                                    # ...then closes, as 3.29 does
        self.assertTrue(self.wait_for(lambda: not dv.property("visible")))

    def test_turning_the_page_clears_and_redir_pages_select_their_own_pdf_page(self):
        view = self.open_document(PDF_DOC, A4)
        self.drag(view, char_point(0, 0, 0), char_point(0, 0, 4))
        self.assertTrue(self.wait_for(lambda: self.selected() == "Alpha"))
        blank = self.open_document(PDF_DOC, BLANK)                    # the inserted page: no PDF text
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        self.drag(blank, QPointF(-300, 300), QPointF(300, 400))
        self.assertEqual(self.selected(), "")
        letter = self.open_document(PDF_DOC, LETTER)                  # third page, PDF page 1
        text = LINES[1][0][2]
        self.drag(letter, char_point(1, 0, 0), char_point(1, 0, len(text) - 1))
        self.assertTrue(self.wait_for(lambda: self.selected() == text), self.selected())
        ebook = self.open_document(EPUB_DOC, E0)                      # an EPUB's rendition
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        self.drag(ebook, char_point(0, 2, 0), char_point(0, 2, 4))
        self.assertTrue(self.wait_for(lambda: self.selected() == "A new"), self.selected())

    def test_pens_lasso_and_search_do_not_fight_text_selection(self):
        view = self.open_document(PDF_DOC, A4)
        tools = self.pens(view).tools
        self.drag(view, char_point(0, 0, 0), char_point(0, 0, 4))
        self.assertTrue(self.wait_for(lambda: self.selected() == "Alpha"))
        tools.chooseTool("fineliner")                                 # a pen taken up: selection gone
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        self.drag(view, char_point(0, 2, 0), char_point(0, 2, 10))   # the pen draws, selects nothing
        self.assertTrue(self.wait_for(lambda: len(self.pending("StrokeAdd")) == 1))
        self.assertEqual(self.selected(), "")
        tools.deactivate()
        self.tq.wait(300)
        self.drag(view, char_point(0, 0, 0), char_point(0, 0, 4))
        self.assertTrue(self.wait_for(lambda: self.selected() == "Alpha"))
        tools.chooseLasso()                                           # the lasso: gone too
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))
        tools.deactivate()
        self.tq.wait(300)
        # Ctrl+F's search bar open: dragging still selects; Esc closes the bar and clears.
        self.key(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        header = self.qml("SearchResultsHeader")
        self.assertTrue(self.wait_for(lambda: header.property("visible")))
        self.drag(view, char_point(0, 0, 0), char_point(0, 0, 4))
        self.assertTrue(self.wait_for(lambda: self.selected() == "Alpha"))
        self.key(Qt.Key.Key_Escape)                                    # the bar's own Esc (a shortcut) first
        self.assertTrue(self.wait_for(lambda: not header.property("visible")))
        self.assertEqual(self.selected(), "Alpha")
        self.key(Qt.Key.Key_Escape)                                    # then the selection
        self.assertTrue(self.wait_for(lambda: self.selected() == ""))

    def test_highlights_sit_on_the_selected_glyphs_at_any_size_zoom_and_scroll(self):
        ratio = self.window.devicePixelRatio()
        expect = float(os.environ.get("PDFTEXT_EXPECT_DPR", ratio))
        self.assertEqual(ratio, expect)
        for page_id, n in ((A4, 0), (LETTER, 1), (SLIDE, 2)):
            view = self.open_document(PDF_DOC, page_id)
            text = LINES[n][0][2]
            fitted = float(view._tiles._get("scale"))
            first = char_point(n, 0, 0)
            for scale, focus in ((fitted, None), (fitted * 2.2, first), (fitted * 0.6, QPointF(first.x() + 150, first.y()))):
                if focus is not None:
                    view._tiles.setFocalPoint(focus, scale)
                    self.tq.wait(300)
                self.drag(view, char_point(n, 0, 0), char_point(n, 0, len(text) - 1))
                self.assertTrue(self.wait_for(lambda: self.selected() == text), (page_id, scale, self.selected()))
                shown, expected = self.overlay_rects(), self.expected_rects(view)
                self.assertEqual(len(shown), 1, (page_id, scale))
                for s, e in zip(shown, expected):
                    for edge in ("left", "top", "right", "bottom"):
                        self.assertAlmostEqual(getattr(s, edge)(), getattr(e, edge)(), delta=1.0,
                                               msg=(page_id, scale, edge, s, e))
                # The highlight covers the glyphs: every character's box lies in it.
                doc_text = view._glyph_pages
                page_text = next(iter(doc_text.values()))
                line = page_text.lines[0]
                glyphs = page_text.box(line.start, line.end - line.start)
                g0, g1 = view._tiles.to_view(glyphs.topLeft()), view._tiles.to_view(glyphs.bottomRight())
                glyph_rect = view.mapRectToScene(QRectF(g0, g1))
                self.assertTrue(shown[0].adjusted(-1, -1, 1, 1).contains(glyph_rect), (page_id, scale))
                if ratio == 1.0:
                    # In a grab: tinted blue inside, page white just above it.
                    image = self.window.grabWindow()
                    inside, above = shown[0].center().toPoint(), shown[0].topLeft().toPoint() + QPointF(4, -6).toPoint()
                    c = image.pixelColor(inside)
                    self.assertGreater(c.blue(), c.red() + 15, (page_id, scale, c.name()))
                    a = image.pixelColor(above)
                    self.assertLess(abs(a.blue() - a.red()), 15, (page_id, scale, a.name()))
                self.key(Qt.Key.Key_Escape)                                              # clear
                self.assertTrue(self.wait_for(lambda: self.selected() == ""))
                view._tiles.setFocalPoint(view._tiles._get("center"), fitted)
                self.tq.wait(200)

    @unittest.skipIf(os.environ.get("PDFTEXT_EXPECT_DPR"), "the HiDPI run itself")
    def test_highlights_line_up_at_a_device_pixel_ratio_of_2(self):
        """The same alignment test with Qt scaling the window by 2 (QT_SCALE_FACTOR=2):
        DocumentView then lays the viewport out at twice its size, scaled by 1/2, as on a
        HiDPI screen. In a process of its own (the ratio is fixed when Qt starts)."""
        import subprocess
        env = dict(os.environ, QT_SCALE_FACTOR="2", PDFTEXT_EXPECT_DPR="2", QT_QPA_PLATFORM="offscreen")
        run = subprocess.run([sys.executable, "-m", "unittest",
                              "test_pdf_text_offline.QuickTests.test_highlights_sit_on_the_selected_glyphs_at_any_size_zoom_and_scroll"],
                             cwd=HERE, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])

    def test_copied_text_pastes_into_a_notebooks_typed_text(self):
        view = self.open_document(PDF_DOC, A4)
        self.drag(view, char_point(0, 1, 0), char_point(0, 1, 29))
        self.assertTrue(self.wait_for(lambda: self.selected() == "Second line of the first page."))
        self.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: QGuiApplication.clipboard().text() == "Second line of the first page."))
        note = self.open_document(NOTE_DOC)
        note.parentItem().forceActiveFocus()           # past the first-open editing tutorial
        self.tq.TypingTests.text_mode(self, True)
        self.assertTrue(self.wait_for(lambda: note.text_session is not None))
        self.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: note.text_session.plain().strip() == "Second line of the first page."),
                        note.text_session.plain() if note.text_session else None)
        self.tq.TypingTests.text_mode(self, False)


if __name__ == "__main__":
    unittest.main()
