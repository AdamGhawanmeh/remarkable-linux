"""PDF/EPUB text highlights (CrdtGlyphItem, block 0x03) erased, and typed text on PDF
and EPUB documents, as reMarkable.exe 3.29 does both (ARCHITECTURE.md §5.11).

- The value layout is 3.29's writer (0x140a1bcf0); the eraser's rule is 0x140996fb0
  (the union of the rectangles meets the outline's bounds, then more than half of the
  rectangles' centres are inside: the highlight goes whole); ClearPage's filter
  (0x140433d20) takes highlights with the strokes; deleting a layer empties it of them.
  Undo/redo put a highlight back as a new 0x03 item right after its deleted self.
- Typed text: `Document::isNotepage` (0x1406609e0) is true for a notebook, and for a
  PDF/EPUB page only when it has no PDF page behind it (redirection -1, a page inserted
  into the document); the app's QML shows the Text button and enters text mode only there.

Synthetic documents only; nothing leaves the process (the fake cloud)."""

from __future__ import annotations

import gc
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMetaObject, QPointF, Q_ARG, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakecloud import FakeCloud, paired_auth  # noqa: E402
from rmfixtures import f_id, f_sub, f_u32, line_item, points_v2, varuint  # noqa: E402

import test_pdf_edit_offline as tp  # noqa: E402

from core import outbox  # noqa: E402
from core.edits import PendingEdits, StrokeDelete, StrokeErase, StrokeRestore  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.pages import PDF_PX_PER_PT  # noqa: E402
from viewer import renderer, rm_layer, rm_stroke as s, rm_writer as w  # noqa: E402
from viewer.rm_parser import GlyphRange, decode_glyph_value, parse  # noqa: E402

TABLET = tp.TABLET
LAYER = tp.LAYER
HL_DOC = "abababab-1111-4111-8111-abababababab"     # a PDF with highlights (synthetic ids)
HL_EPUB = "cdcdcdcd-2222-4222-8222-cdcdcdcdcdcd"
P0 = "a0a0a0a0-0000-4000-8000-000000000001"         # A4 PDF page, highlighted
P1 = "a0a0a0a0-0000-4000-8000-000000000002"         # a note page inserted into the PDF
P2 = "a0a0a0a0-0000-4000-8000-000000000003"         # 16:9 slide PDF page
E0 = "e0e0e0e0-0000-4000-8000-000000000001"
NAMES = ("Highlighted paper", "Highlighted novel")
YELLOW = 0xFFFFED75


def f_u8(index: int, value: int) -> bytes:
    return varuint(index << 4 | 1) + bytes([value])


def glyph_value(rects, *, color: int = 3, text: str = "highlighted words", start: int = 10,
                argb: int = YELLOW, ids=None) -> bytes:
    """A CrdtGlyphItem value as reMarkable.exe 3.29 writes one (0x140a1bcf0)."""
    raw = text.encode()
    value = b"\x01"
    if ids is None:
        value += f_u32(2, start) + f_u32(3, len(text))
    value += f_u32(4, color) + f_sub(5, varuint(len(raw)) + b"\x01" + raw)
    value += f_sub(6, varuint(len(rects)) + b"".join(struct.pack("<4d", *r) for r in rects))
    if ids is not None:
        value += f_id(7, ids[0]) + f_id(8, ids[1]) + f_u8(9, 1)
    return value + f_u32(10, argb)


def glyph_block(item, value: bytes, left=(0, 0), right=(0, 0), versions=(1, 1)) -> bytes:
    header = w.ItemHeader(LAYER, item, left, right, 0, value)
    return w.Block(0x03, versions[0], versions[1], header.encode()).encode()


def stroke(item, points, left=(0, 0)) -> bytes:
    data = line_item(LAYER, item, tool=17, color=0, points=points_v2([(x, y, 0, 0, 2, 255) for x, y in points]))
    _, [b] = w.split(w.V6_HEADER + data)
    header = w.decode_item(b.payload)
    header.left = left
    return w.Block(b.type, b.min_version, b.version, header.encode()).encode()


def mark() -> tuple[float, float]:
    return tp.mark_centre(0)


def highlight_rects() -> list[tuple[float, float, float, float]]:
    """Two lines of a highlight just below the red square of the A4 page."""
    cx, cy = mark()
    return [(cx - 150.0, cy + 120.0, 300.0, 40.0), (cx - 150.0, cy + 170.0, 220.0, 40.0)]


def far_rects() -> list[tuple[float, float, float, float]]:
    return [(-500.0, 300.0, 200.0, 40.0), (-500.0, 350.0, 200.0, 40.0), (-500.0, 400.0, 120.0, 40.0)]


def highlighted_page() -> bytes:
    """An A4 PDF page as a tablet leaves one: a stroke, a highlight under the red
    square (1:16), a second one far from it (1:17, colour 9 with its own ARGB)."""
    header, blocks = w.split(w.blank_page(TABLET))
    items = (stroke((1, 15), [(-600.0, 1400.0), (-400.0, 1450.0)])
             + glyph_block((1, 16), glyph_value(highlight_rects()), left=(1, 15))
             + glyph_block((1, 17), glyph_value(far_rects(), color=9, argb=0xFFACFF85, ids=((1, 5), (1, 9))),
                           left=(1, 16)))
    return w.join(header, blocks) + items


def build_cloud() -> FakeCloud:
    pdf = tp.make_pdf()
    cloud = FakeCloud()
    cloud.put_item(HL_DOC, tp.meta(NAMES[0]),
                   tp.pdf_content("pdf", [tp.page_entry(P0, "ba", 0), tp.page_entry(P1, "bb", None, "Blank"),
                                          tp.page_entry(P2, "bc", 2)], 3),
                   {f"{HL_DOC}.pdf": pdf, f"{HL_DOC}/{P0}.rm": highlighted_page()})
    cloud.put_item(HL_EPUB, tp.meta(NAMES[1]), tp.pdf_content("epub", [tp.page_entry(E0, "ba", 0)], 1),
                   {f"{HL_EPUB}.pdf": pdf, f"{HL_EPUB}.epub": b"PK\x03\x04 not a real ebook",
                    f"{HL_EPUB}/{E0}.rm": highlighted_page()})
    return cloud


def live_glyphs(page: bytes | None) -> list[tuple[int, int]]:
    return [item for item, _, _ in s.glyph_ranges(page)]


def across(rects, *, steps: int = 12) -> list[tuple[float, float]]:
    """An eraser path across the middle of every rectangle, left to right then back."""
    out = []
    for x, y, wd, h in rects:
        out += [(x + wd * n / steps, y + h / 2) for n in range(steps + 1)]
    return out


# ------------------------------------------------------------------ the 0x03 value and the hit rule

class GlyphValueTests(unittest.TestCase):
    def test_the_329_layout_is_read_both_ways_and_kept_byte_for_byte(self):
        g = decode_glyph_value(glyph_value(highlight_rects(), text="ab€"))
        self.assertEqual((g.start, g.length, g.color, g.argb, g.text), (10, 3, 3, YELLOW, "ab€"))
        self.assertEqual(g.rects, highlight_rects())
        self.assertIsNone(g.first_id)
        g = decode_glyph_value(glyph_value(far_rects(), ids=((1, 5), (1, 9))))
        self.assertEqual((g.start, g.length, g.first_id, g.last_id, g.include_last), (None, None, (1, 5), (1, 9), True))
        page = highlighted_page()
        header, blocks = w.split(page)
        self.assertEqual(w.join(header, blocks), page)
        self.assertEqual(w.highest_counter(blocks), 17)

    def test_the_colour_rule_of_the_paint(self):
        def colour(c, argb=None):
            return GlyphRange((1, 1), LAYER, color=c, argb=argb).colour_argb()
        self.assertEqual(colour(0), renderer.PALETTE_ARGB[3])            # 0 -> HighlighterYellow
        self.assertEqual(colour(4), renderer.PALETTE_ARGB[4])
        self.assertEqual(colour(9, 0xFF123456), 0xFF123456)               # ArgbCode: its own
        self.assertEqual(colour(17), 0xFF000000)                          # past the table: black

    def test_the_parser_places_highlights_in_their_layer_in_stacking_order(self):
        scene = parse(highlighted_page())
        [layer] = scene.layers
        self.assertEqual([type(i).__name__ for i in layer.items()], ["Stroke", "GlyphRange", "GlyphRange"])
        self.assertEqual([g.item_id for g in layer.glyphs], [(1, 16), (1, 17)])
        self.assertEqual(scene.skipped_blocks, {})

    def test_more_than_half_of_the_centres_must_be_inside(self):
        rects = [(0.0, 0.0, 10.0, 10.0), (100.0, 0.0, 10.0, 10.0), (200.0, 0.0, 10.0, 10.0), (300.0, 0.0, 10.0, 10.0)]
        def outline(*xs):
            return s.Outline(tuple((x + dx, dy) for x in xs for dx, dy in ((-5, -5), (5, -5), (5, 20), (-5, 20))))
        lasso = lambda x0, x1: s.Outline(((x0, -20.0), (x1, -20.0), (x1, 30.0), (x0, 30.0), (x0, -20.0)))  # noqa: E731
        self.assertFalse(s.glyph_hit(lasso(-10.0, 120.0), rects))        # 2 of 4: not more than half
        self.assertTrue(s.glyph_hit(lasso(-10.0, 220.0), rects))         # 3 of 4
        self.assertFalse(s.glyph_hit(lasso(400.0, 500.0), rects))        # bounds do not meet
        # The anchor position moves the centres (float adds) only: the bounds test is
        # made on the stored rectangles (0x140996fb0 unions them as stored).
        self.assertFalse(s.glyph_hit(lasso(-10.0, 220.0), rects, 1000.0, 0.0))
        self.assertTrue(s.glyph_hit(s.Outline(((-10.0, -20.0), (1220.0, -20.0), (1220.0, 30.0), (-10.0, 30.0),
                                               (-10.0, -20.0))), rects[:1] * 3, 1000.0, 0.0))
        self.assertFalse(s.glyph_hit(lasso(990.0, 1220.0), rects, 1000.0, 0.0))     # bounds do not meet
        self.assertFalse(outline().points)


class EngineTests(unittest.TestCase):
    def test_the_eraser_takes_a_highlight_whole_and_leaves_the_others(self):
        page = highlighted_page()
        before = w.split(page)[1]
        erased = s.erase(page, author=TABLET, eraser=s.eraser_line(across(highlight_rects()), s.MEDIUM))
        self.assertEqual(erased.deleted, ((1, 16),))
        self.assertEqual(erased.made, ())
        self.assertEqual(live_glyphs(erased.page), [(1, 17)])
        blocks = w.split(erased.page)[1]
        gone = next(w.decode_item(b.payload) for b in blocks if b.type == 0x03 and w.decode_item(b.payload).item == (1, 16))
        self.assertEqual((gone.deleted_length, gone.value), (1, None))  # still a 0x03 block, no value
        self.assertEqual(len(blocks), len(before))
        self.assertEqual([b for b in blocks if b.type == 0x05], [b for b in before if b.type == 0x05])

    def test_a_pass_over_a_corner_leaves_the_highlight(self):
        x, y, wd, h = highlight_rects()[0]
        erased = s.erase(highlighted_page(), author=TABLET, eraser=s.eraser_line([(x + 2, y + 2), (x + 30, y + 2)], s.MEDIUM))
        self.assertEqual(erased.deleted, ())

    def test_the_erase_section_lasso_takes_highlights_inside_it(self):
        x, y, wd, h = highlight_rects()[0]
        lasso = [(x - 20, y - 20), (x + wd + 20, y - 20), (x + wd + 20, y + 120), (x - 20, y + 120), (x - 20, y - 20)]
        erased = s.erase(highlighted_page(), author=TABLET, eraser=s.eraser_line(lasso, tool=s.ERASE_SECTION))
        self.assertEqual(erased.deleted, ((1, 16),))

    def test_clear_page_takes_highlights_and_undo_puts_them_back_as_new_glyph_items(self):
        page = highlighted_page()
        cleared, deleted = rm_layer.clear_lines_in_layer(page, author=TABLET, layer=LAYER)
        self.assertEqual([i for i, _ in deleted], [(1, 15), (1, 16), (1, 17)])
        self.assertEqual(live_glyphs(cleared), [])
        values = dict(s.item_values(page))
        for item, raw in deleted:
            self.assertEqual(raw, values[item])
        again = cleared
        for item, raw in deleted:
            again, new_id = s.restore_item(again, author=TABLET, item=item, value=raw)
        scene = parse(again)
        self.assertEqual([type(i).__name__ for i in scene.layers[0].items()], ["Stroke", "GlyphRange", "GlyphRange"])
        self.assertEqual(sorted(g.text for g in scene.layers[0].glyphs), ["highlighted words"] * 2)
        restored = [w.decode_item(b.payload) for b in w.split(again)[1] if b.type == 0x03]
        self.assertEqual(sorted(i.value for i in restored if i.value), sorted([values[(1, 16)], values[(1, 17)]]))

    def test_a_highlight_comes_back_under_the_versions_it_was_read_with(self):
        header, blocks = w.split(w.blank_page(TABLET))
        page = w.join(header, blocks) + glyph_block((1, 16), glyph_value(highlight_rects()), versions=(1, 2))
        erased = s.erase(page, author=TABLET, eraser=s.eraser_line(across(highlight_rects()), s.MEDIUM))
        back, new_id = s.restore_item(erased.page, author=TABLET, item=(1, 16), value=glyph_value(highlight_rects()))
        block = next(b for b in w.split(back)[1] if b.type == 0x03 and w.decode_item(b.payload).item == new_id)
        self.assertEqual((block.min_version, block.version), (1, 2))
        self.assertEqual(w.decode_item(block.payload).left, (1, 16))

    def test_highlights_are_never_selected(self):
        """The item's rectangle and lasso hooks are `xor al,al; ret` (0x1404431e0, 0x140442ec0)."""
        from ui import selection
        scene = parse(highlighted_page())
        picked = selection.selectable(scene, 0)
        self.assertEqual([p.item for p in picked], [(1, 15)])
        x, y, wd, h = highlight_rects()[0]
        self.assertEqual(selection.pick_rect(picked, (int(x) - 10, int(y) - 10, int(wd) + 20, 200)), [])

    def test_the_highlight_is_drawn_and_gone_once_erased(self):
        cx, cy = mark()
        x, y, wd, h = highlight_rects()[0]
        def at(page):
            image = QImage(1404, 2800, QImage.Format.Format_ARGB32)
            image.fill(QColor("white"))
            p = QPainter(image)
            renderer.paint(p, parse(page))
            p.end()
            return QColor(image.pixel(round(x + wd / 2 + 702), round(y + h / 2)))
        self.assertEqual(at(highlighted_page()).rgb() & 0xFFFFFF, YELLOW & 0xFFFFFF)
        erased = s.erase(highlighted_page(), author=TABLET, eraser=s.eraser_line(across(highlight_rects()), s.MEDIUM))
        self.assertEqual(at(erased.page).rgb() & 0xFFFFFF, 0xFFFFFF)
        svg = "\n".join(renderer.svg_scene(parse(highlighted_page())))
        self.assertIn('data-tool="text-highlight" fill="#ffed75"', svg)
        self.assertIn("mix-blend-mode:darken", svg)


# ------------------------------------------------------------------ the library: edits, undo, restart, upload

class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = build_cloud()
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=NAMES)
        self.source.refresh()

    def doc(self, doc_id=HL_DOC):
        return self.source.sync.tree.get(doc_id)

    def page(self, doc_id=HL_DOC, page_id=P0) -> bytes | None:
        return self.source.staged(self.doc(doc_id)).page(page_id)

    def test_erase_undo_redo_restart_and_upload(self):
        for doc_id, page_id in ((HL_DOC, P0), (HL_EPUB, E0)):
            eraser = s.eraser_line(across(highlight_rects()), s.MEDIUM)
            edit = self.source.erase_strokes(self.doc(doc_id), page_id, eraser, 1000)
            self.assertIsInstance(edit, StrokeErase)
            self.assertEqual(live_glyphs(self.page(doc_id, page_id)), [(1, 17)])
            self.assertTrue(self.source.undo(self.doc(doc_id), page_id, 1001))
            self.assertEqual(len(live_glyphs(self.page(doc_id, page_id))), 2)
            self.assertIn(StrokeRestore, [type(e) for e in self.sync.edits.of(doc_id)])
            self.assertTrue(self.source.redo(self.doc(doc_id), page_id, 1002))
            self.assertEqual(live_glyphs(self.page(doc_id, page_id)), [(1, 17)])
        kinds = [type(e).__name__ for e in self.sync.edits.of(HL_DOC)]
        self.assertEqual(kinds, ["StrokeErase", "StrokeRestore", "StrokeDelete"])

        # A restart: a new process reads the same pending ledger and stages the same page.
        staged = self.page()
        sync2 = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        source2 = LibrarySource(sync2, editable=NAMES)
        source2.refresh()
        self.assertEqual(source2.staged(source2.sync.tree.get(HL_DOC)).page(P0), staged)
        self.assertEqual(len(PendingEdits(self.dir / "cache" / "pending").of(HL_DOC)), 3)

        # The upload (to the fake cloud): what the cloud holds after it is the staged page.
        result = outbox.push_document(sync2, HL_DOC, settle=source2.settle_upload)
        self.assertTrue(result)
        self.assertEqual(sync2.edits.of(HL_DOC), [])
        uploaded = self.cloud.item(HL_DOC)[f"{HL_DOC}/{P0}.rm"]
        self.assertEqual(live_glyphs(uploaded), [(1, 17)])
        self.assertEqual(len(parse(uploaded).layers[0].strokes), 1)

    def test_clear_page_takes_highlights_with_undo_and_redo(self):
        edit = self.source.clear_lines_in_layer(self.doc(), P0, None, 1000)
        self.assertIsInstance(edit, StrokeDelete)
        self.assertEqual(live_glyphs(self.page()), [])
        self.assertEqual(parse(self.page()).layers[0].strokes, [])
        self.assertTrue(self.source.undo(self.doc(), P0, 1001))
        self.assertEqual(len(live_glyphs(self.page())), 2)
        self.assertEqual(len(parse(self.page()).layers[0].strokes), 1)
        self.assertTrue(self.source.redo(self.doc(), P0, 1002))
        self.assertEqual(live_glyphs(self.page()), [])

    def test_a_deleted_layer_is_emptied_of_its_highlights_and_undo_restores_them(self):
        self.source.add_layer(self.doc(), P0, 1000)
        self.source.delete_layer(self.doc(), P0, 0, 1001)
        self.assertEqual(live_glyphs(self.page()), [])
        self.assertTrue(self.source.undo(self.doc(), P0, 1002))
        self.assertEqual(len(live_glyphs(self.page())), 2)
        self.assertEqual([len(l.glyphs) for l in parse(self.page()).layers], [2, 0])

    def test_a_pass_that_meets_nothing_keeps_nothing(self):
        far = s.eraser_line([(600.0, 2000.0), (650.0, 2000.0)], s.MEDIUM)
        self.assertIsNone(self.source.erase_strokes(self.doc(), P0, far, 1000))
        self.assertEqual(self.sync.edits.of(HL_DOC), [])


# ------------------------------------------------------------------ through the app's own QML

class QmlTests(unittest.TestCase):
    """The eraser over a PDF highlight, and typed text on a page inserted into a PDF
    whose own pages are A4 and 16:9, through the app's DocumentView."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"), editable=NAMES)
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        cls.tq.wait(50)
        cls.tmp.cleanup()

    def setUp(self):
        self.notices.clear()
        del self.app.warnings[:]

    def tearDown(self):
        dv = self.qml("DocumentView_")
        if dv is not None and dv.property("textMode"):
            dv.setProperty("textMode", False)
            self.tq.wait(50)
        self.tq.EditingTests.close_document(self)
        self.source.sync.edits.clear()
        for doc_id in (HL_DOC, HL_EPUB):
            self.source.forget_history(doc_id)

    def wait_for(self, condition, ms=8000):
        return self.tq.WorkflowTests.wait_for(self, condition, ms)

    def qml(self, prefix):
        return self.tq.WorkflowTests.qml(self, prefix)

    def scene_views(self):
        return self.tq.WorkflowTests.scene_views(self)

    def open_document(self, doc_id, page_id):
        return tp.QuickTests.open_document(self, doc_id, page_id)

    def pens(self, view):
        return self.tq.PenTests.pens(self, view)

    def drag(self, view, points):
        return self.tq.PenTests.drag(self, view, points)

    def window_point(self, view, x, y):
        return self.tq.PenTests.window_point(self, view, x, y)

    def key(self, key, modifiers=Qt.KeyboardModifier.ControlModifier):
        from PySide6.QtTest import QTest
        self.window.requestActivate()
        QTest.keyClick(self.window, key, modifiers)
        self.tq.wait(40)

    def redo_key(self):
        return self.tq.PenTests.redo_key(self)

    def keys(self, key, modifiers=Qt.KeyboardModifier.ControlModifier):
        return self.key(key, modifiers)

    def shows_yellow(self, view, x, y, r=2) -> bool:
        image = self.window.grabWindow()
        dpr = image.devicePixelRatio() or 1.0
        at = view.mapToScene(view._tiles.to_view(QPointF(x, y)))
        hits = 0
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                c = QColor(image.pixel(round((at.x() + dx) * dpr), round((at.y() + dy) * dpr)))
                hits += c.red() > 220 and c.green() > 200 and c.blue() < 170
        return hits > 2

    def test_the_eraser_takes_a_pdf_highlight_with_undo_and_redo(self):
        view = self.open_document(HL_DOC, P0)
        self.assertTrue(self.wait_for(lambda: view._image is not None))
        x, y, wd, h = highlight_rects()[0]
        centre = (x + wd / 2, y + h / 2)
        view._tiles.setFocalPoint(QPointF(*centre), 0.6)
        self.tq.wait(400)
        self.assertTrue(self.wait_for(lambda: self.shows_yellow(view, *centre)), "the highlight is not shown")
        tools = self.pens(view).tools
        tools.chooseTool("eraser")
        self.assertTrue(tools.property("active"))
        self.drag(view, across(highlight_rects(), steps=16))
        self.assertTrue(self.wait_for(lambda: not self.shows_yellow(view, *centre)), self.notices)
        page = self.source.staged(self.source.sync.tree.get(HL_DOC)).page(P0)
        self.assertEqual(live_glyphs(page), [(1, 17)])
        self.assertEqual([type(e).__name__ for e in self.source.sync.edits.of(HL_DOC)
                          if type(e).__name__ != "ToolSettings"], ["StrokeErase"])
        tools.deactivate()
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: self.shows_yellow(view, *centre)), self.notices)
        self.redo_key()
        self.assertTrue(self.wait_for(lambda: not self.shows_yellow(view, *centre)), self.notices)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def text_button(self):
        todo, found = [self.qml("DocumentView_")], []
        while todo:
            item = todo.pop()
            found.append(item)
            todo.extend(item.childItems())
        return next(i for i in found
                    if i.property("iconSource") is not None and str(i.property("iconSource")).endswith("/typing"))

    def test_typed_text_only_on_the_page_inserted_into_the_pdf_and_where_it_is_seen(self):
        from PySide6.QtTest import QTest
        dv = self.qml("DocumentView_")
        # The PDF's own pages (A4, 16:9): no Text button, and text mode does not start.
        for page_id in (P0, P2):
            view = self.open_document(HL_DOC, page_id)
            self.assertFalse(self.text_button().property("visible"), page_id)
            self.assertFalse(view.isNotePage())
        # The inserted note page: the button is there; text typed lands where the caret is seen.
        view = self.open_document(HL_DOC, P1)
        self.assertTrue(view.isNotePage())
        self.assertTrue(self.wait_for(lambda: self.text_button().property("visible")))
        dv.setProperty("textMode", True)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None), self.notices)
        self.window.requestActivate()
        view.forceActiveFocus()
        self.assertTrue(self.wait_for(view.hasActiveFocus))
        for c in "hello world":
            QTest.keyClick(self.window, Qt.Key.Key_Space if c == " " else Qt.Key(ord(c.upper())),
                           Qt.KeyboardModifier.ShiftModifier if c.isupper() else Qt.KeyboardModifier.NoModifier)
        self.tq.wait(50)
        session = view.text_session
        self.assertEqual(session.plain(), "hello world")
        # A click on the caret position of index 6 (before "world"), where the window
        # shows it, puts the caret there: view <-> scene agree on this page.
        caret = session.caret(6)
        at = self.window_point(view, caret.x, caret.top + caret.height / 2)
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None and view.text_session.cursor == 6),
                        (view.text_session.cursor if view.text_session else None))
        # Kept as a pending text edit of the inserted page.
        dv.setProperty("textMode", False)
        self.assertTrue(self.wait_for(lambda: any(type(e).__name__ == "TextEdit"
                                                  for e in self.source.sync.edits.of(HL_DOC))), self.notices)
        page = self.source.staged(self.source.sync.tree.get(HL_DOC)).page(P1)
        self.assertEqual(parse(page).text.text, "hello world")
        # At a device pixel ratio of 2 the page paints its text at twice the size, in place.
        boxes = []
        for ratio in (1.0, 2.0):
            image = QImage(round(view.width() * ratio), round(view.height() * ratio), QImage.Format.Format_ARGB32)
            image.fill(QColor("white"))
            painter = QPainter(image)
            painter.scale(ratio, ratio)
            view.paint(painter)
            painter.end()
            xs, ys = [], []
            for yy in range(image.height()):
                for xx in range(image.width()):
                    c = QColor(image.pixel(xx, yy))
                    if max(c.red(), c.green(), c.blue()) < 100:
                        xs.append(xx)
                        ys.append(yy)
            self.assertTrue(xs, ratio)
            boxes.append((min(xs) / ratio, min(ys) / ratio, max(xs) / ratio, max(ys) / ratio))
        for a, b in zip(*boxes):
            self.assertLessEqual(abs(a - b), 2.0, boxes)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_only_pages_inserted_into_a_pdf_or_ebook_are_note_pages(self):
        """isNotepage as Document::isNotepage (0x1406609e0) decides it."""
        pdf = self.app.library.entryForId(HL_DOC)
        self.assertEqual([pdf.isNotepage(i) for i in range(3)], [False, True, False])
        self.assertFalse(pdf.isNotepage(3))                     # past the page map
        self.assertFalse(pdf.isNotepage(-1))
        self.assertFalse(self.app.library.entryForId(HL_EPUB).isNotepage(0))

    def test_text_mode_is_refused_on_pdf_and_ebook_pages(self):
        for doc_id, page_id in ((HL_DOC, P2), (HL_EPUB, E0)):
            view = self.open_document(doc_id, page_id)
            view._controller.createRootDocument()
            self.tq.wait(100)
            self.assertIsNone(view.text_session)
            self.assertEqual(self.notices[-1:], ["Text can only be typed on notebook pages"])
            self.notices.clear()
            self.tq.EditingTests.close_document(self)
        self.assertEqual(self.source.sync.edits.of(HL_DOC) + self.source.sync.edits.of(HL_EPUB), [])


if __name__ == "__main__":
    unittest.main()
