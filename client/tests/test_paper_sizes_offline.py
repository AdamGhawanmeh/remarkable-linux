"""Offline tests for paper sizes other than 1404 x 1872 (ARCHITECTURE.md 5.6): the
SceneInfo fields reMarkable.exe 3.29 reads and writes for a page's paper (5, 7, 8), the
template variables it binds from it, pages drawn, exported and shown at their own paper
size, and the new-notebook dialog, Quick sheets and "Add page" giving pages the size of
Settings > "Document paper width" or of the page they follow. Synthetic ids only.
Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import gc
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fakecloud import paired_auth  # noqa: E402
from PySide6.QtCore import QMetaObject, QObject, QPointF, QRectF, QSizeF, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.edits import PageAdd, PendingEdits, SceneFlags, StrokeAdd  # noqa: E402
from core.sync import SyncClient, describe_edit  # noqa: E402
from test_edits_offline import (PAGE1, PAGE2, TABLET, THEN, THROWAWAY, build_cloud, drawn_page, meta,  # noqa: E402
                                notebook_content)
from ui.library import LibrarySource  # noqa: E402
from viewer import rm_layer, rm_parser, rm_stroke, rm_writer as w  # noqa: E402
from viewer.rm_parser import _read_fields, paper_size_of  # noqa: E402

NOW = 1_800_000_000_000
NEW_DOC = "88888888-aaaa-4aaa-8aaa-888888888888"
NEW_PAGE = "88888888-0000-4000-8000-000000000001"
WIDE = "99999999-bbbb-4bbb-8bbb-999999999999"
WIDE_PAGE = "99999999-0000-4000-8000-000000000001"
SIZES = ((1404.0, 1872.0), (1620.0, 2160.0), (954.0, 1696.0))      # Settings.paperSize's table (0x14102b900)


def blocks_but(page: bytes, *types: int) -> list[tuple]:
    """The page's blocks (type, versions, payload) other than those of `types`."""
    return [(b.type, b.min_version, b.version, b.payload) for b in w.split(page)[1] if b.type not in types]


def scene_info(page: bytes) -> list[tuple[int, int, object]]:
    return next(w.read_values(b.payload) for b in w.split(page)[1] if b.type == w.BLOCK_SCENE_INFO)


def info_payload(fields: dict) -> bytes:
    """A SceneInfo payload of the given field index -> sub-block bytes, in index order."""
    return b"".join(w.f_sub(i, v) for i, v in sorted(fields.items()))


def wide_page(paper=(1620.0, 2160.0)) -> bytes:
    """A page the tablet drew one stroke on, then set to another paper size."""
    return rm_layer.set_scene_info(drawn_page(), author=TABLET, paper=paper)


class PaperFormatTests(unittest.TestCase):
    """SceneInfo fields 5, 7 and 8: reader 0x1409ef220, writer 0x1409f92f0, setter 0x1408aa520."""

    def test_the_loader_takes_field_8_then_7_then_5(self):
        stamped = w.lww((1, 20), struct.pack("<dd", 1620, 2160))
        unstamped = w.lww((0, 0), struct.pack("<dd", 1404, 1872))
        cases = [
            ({5: struct.pack("<ii", 1404, 1872), 7: struct.pack("<dd", 1404, 1872), 8: stamped}, (1620, 2160)),
            ({5: struct.pack("<ii", 954, 1696), 7: struct.pack("<dd", 1620, 2160), 8: unstamped}, (1620, 2160)),
            ({5: struct.pack("<ii", 954, 1696), 7: struct.pack("<dd", 0, 0), 8: unstamped}, (954, 1696)),
            ({5: struct.pack("<ii", 954, 1696)}, (954, 1696)),
            ({5: struct.pack("<ii", 0, 1696)}, (1404, 1872)),                 # w < 1: passed over
            ({}, (1404, 1872)),                                               # the loader's default
        ]
        for fields, expected in cases:
            self.assertEqual(paper_size_of(_read_fields(info_payload(fields))), expected, fields)

    def test_a_new_size_is_written_as_the_writer_derives_it(self):
        self.assertIsNone(rm_layer.set_scene_info(None, author=TABLET, paper=(1404, 1872)))   # nothing to write
        page = rm_layer.set_scene_info(None, author=TABLET, paper=(1620, 2160))
        values = {i: v for i, _, v in scene_info(page)}
        self.assertEqual([i for i, _, _ in scene_info(page)], [1, 2, 3, 5, 6, 7, 8, 9])
        self.assertEqual(values[5].hex(), "5406000070080000")                  # u32 1620, 2160
        self.assertEqual(values[7], struct.pack("<dd", 1620, 2160))
        # Field 8 stamped with the page's next id: the tablet's empty page ends at 0:14,
        # so this client's (author 1) first id, 1:15.
        self.assertEqual(values[8], w.lww((1, 15), struct.pack("<dd", 1620, 2160)))
        self.assertEqual(w.highest_counter(w.split(page)[1]), 15)
        blank = w.blank_page(TABLET)
        self.assertEqual(blocks_but(page, w.BLOCK_SCENE_INFO), blocks_but(blank, w.BLOCK_SCENE_INFO))
        parsed = rm_parser.parse(page)
        self.assertEqual((parsed.width, parsed.height, parsed.x_origin), (1620, 2160, 810))
        self.assertEqual(rm_layer.paper_size(page), (1620, 2160))
        self.assertEqual(rm_layer.paper_size(blank), (1404, 1872))
        # The same size again changes nothing (Scene::setPaperSize compares first).
        self.assertIs(rm_layer.set_scene_info(page, author=TABLET, paper=(1620.0, 2160.0)), page)
        self.assertIs(rm_layer.set_scene_info(page, author=TABLET, paper=(1620.0 * (1 + 1e-14), 2160.0)), page)

    def test_field_5_is_rounded_half_away_from_zero(self):
        page = rm_layer.set_scene_info(None, author=TABLET, paper=(1620.5, 2160.4))
        values = {i: v for i, _, v in scene_info(page)}
        self.assertEqual(struct.unpack("<ii", values[5]), (1621, 2160))
        self.assertEqual(struct.unpack("<dd", values[7]), (1620.5, 2160.4))
        self.assertEqual(rm_parser.parse(page).width, 1620.5)

    def test_an_existing_page_keeps_every_other_byte(self):
        before = drawn_page()
        after = wide_page()
        self.assertEqual(blocks_but(after, w.BLOCK_SCENE_INFO), blocks_but(before, w.BLOCK_SCENE_INFO))
        old_info = {i: v for i, _, v in scene_info(before)}
        new_info = {i: v for i, _, v in scene_info(after)}
        self.assertEqual({i: new_info[i] for i in (1, 2, 3, 6, 9)}, {i: old_info[i] for i in (1, 2, 3, 6, 9)})
        self.assertEqual(new_info[8], w.lww((1, 16), struct.pack("<dd", 1620, 2160)))   # after the stroke's 1:15
        # The stroke is where it was; the next stroke takes the id after the paper's.
        self.assertEqual([s.item for s in rm_stroke.strokes(after)], [(1, 15)])
        page, ids = rm_stroke.append_strokes(after, author=TABLET, lines=[rm_stroke.draw([(800, 100), (805, 120)], 17)])
        self.assertEqual(ids, [(1, 17)])
        self.assertEqual(rm_layer.paper_size(page), (1620, 2160))

    def test_a_page_written_before_fields_5_to_9_gets_them_in_order(self):
        header, blocks = w.split(drawn_page())
        n = next(i for i, b in enumerate(blocks) if b.type == w.BLOCK_SCENE_INFO)
        short = [(i, k, v) for i, k, v in w.read_values(blocks[n].payload) if i <= 3]
        blocks[n] = w.Block(blocks[n].type, blocks[n].min_version, blocks[n].version, w.write_values(short))
        old = w.join(header, blocks)
        self.assertEqual(rm_layer.paper_size(old), (1404, 1872))
        new = rm_layer.set_scene_info(old, author=TABLET, paper=(954, 1696))
        self.assertEqual([i for i, _, _ in scene_info(new)], [1, 2, 3, 5, 7, 8])
        self.assertEqual(rm_layer.paper_size(new), (954, 1696))

    def test_sizes_compare_as_qfuzzycompare(self):
        self.assertTrue(rm_layer.same_paper((1404, 1872), (1404.0000000000002, 1872)))
        self.assertFalse(rm_layer.same_paper((1404, 1872), (1404.001, 1872)))
        self.assertRaises(ValueError, rm_layer.set_scene_info, None, author=TABLET, paper=(0, 1872))


class PaperDrawingTests(unittest.TestCase):
    """Templates, pages, thumbnails and exports at a page's own paper size."""

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_template_variables_are_bound_as_the_resolver_binds_them(self):
        from viewer.templates import base_environment
        env = base_environment(1404, 1872)
        self.assertEqual({k: env[k] for k in ("templateWidth", "templateHeight", "paperOriginX", "paperOriginY",
                                              "paperWidth", "paperHeight", "physicalDpi")},
                         {"templateWidth": 1404, "templateHeight": 1872, "paperOriginX": -234, "paperOriginY": 0,
                          "paperWidth": 1872, "paperHeight": 1404, "physicalDpi": 226})
        self.assertAlmostEqual(env["pixelRatio"], 226 / 227)
        env = base_environment(1620, 2160)
        self.assertEqual((env["templateWidth"], env["paperOriginX"], env["paperWidth"], env["paperHeight"]),
                         (1620, -270, 2160, 1620))

    def test_a_lined_template_follows_the_paper(self):
        from viewer.templates import TemplatePath, default_library, template_shapes
        template = default_library().get("P Lines small")
        for width, height in SIZES:
            xs = [x for s in template_shapes(template, (0, 0, width, height), width, height)
                  if isinstance(s, TemplatePath) for _, pts in s.commands for x, _ in pts]
            # The rules span the page's "landscape" note bounds, centred on the paper.
            self.assertEqual((min(xs), max(xs)), (width / 2 - height / 2, width / 2 + height / 2))

    def content(self, page: bytes, template: str | None = "P Lines small"):
        from ui.pages import PageContent
        from viewer.templates import default_library
        return PageContent(rm_parser.parse(page), None, None, default_library().get(template) if template else None)

    def test_a_wide_page_is_drawn_on_its_own_paper(self):
        from ui.pages import render_content
        content = self.content(wide_page())
        self.assertEqual(content.frame(), QRectF(0, 0, 1620, 2160))
        self.assertEqual((content.paper, content.origin), ((1620, 2160), 810))
        self.assertEqual(content.bounds("page"), (0, 0, 1620, 2160))          # the stroke is on the paper
        image = render_content(content, 1.0, "page")
        self.assertEqual((image.width(), image.height()), (1620, 2160))
        # The tablet's stroke from scene (0, 0) to (50, 50): page x 810..860 (not 702..752).
        self.assertLess(QColor(image.pixel(835, 25)).lightness(), 128)
        self.assertGreater(QColor(image.pixel(727, 25)).lightness(), 200)
        # A thumbnail is the paper's shape.
        thumb = render_content(content, 0.1, "clip")
        self.assertEqual((thumb.width(), thumb.height()), (162, 216))
        # A page without a file is 1404 x 1872.
        from ui.pages import PageContent
        self.assertEqual(PageContent(None, None, None).frame(), QRectF(0, 0, 1404, 1872))

    def test_ink_past_the_default_width_is_on_a_wide_page_but_grows_a_default_one(self):
        def with_stroke(page):
            return rm_stroke.append_stroke(page, author=TABLET, line=rm_stroke.draw([(780, 300), (790, 320)], 17))
        self.assertEqual(self.content(with_stroke(wide_page())).bounds("page"), (0, 0, 1620, 2160))
        x, y, width, height = self.content(with_stroke(drawn_page())).bounds("page")
        self.assertGreater(width, 1404)

    def test_exports_use_the_page_paper(self):
        from ui.export import export_pdf, page_svg
        content = self.content(wide_page())
        svg = page_svg(content)
        self.assertIn('viewBox="0 0 1620 2160"', svg)
        self.assertIn('width="1620" height="2160"', svg)

        class Pages:            # a DocumentPages of one page, no PDF
            pages, pdf, pdf_path = [object()], None, None
            doc = type("Doc", (), {"name": "Wide"})()

            def content(self, index):
                return content
        with tempfile.TemporaryDirectory() as tmp:
            out = export_pdf(Pages(), Path(tmp) / "wide.pdf")
            from pypdf import PdfReader
            box = PdfReader(str(out)).pages[0].mediabox
            # 1620 x 2160 page pixels at 226 DPI, in points.
            self.assertAlmostEqual(float(box.width), 1620 * 72 / 226, places=1)
            self.assertAlmostEqual(float(box.height), 2160 * 72 / 226, places=1)

    def test_typed_text_stays_centred_on_the_paper(self):
        from viewer import rm_text
        from viewer.renderer import layout_text, qt_measure
        page = rm_text.edit_text(wide_page(), author=TABLET,
                                 change=lambda text, clock: text.insert(0, "Wide", clock))
        lines = layout_text(rm_parser.parse(page), qt_measure)
        # A new text box is 936 wide at scene x -468 whatever the paper (createRootDocument
        # 0x1408a55f0 uses a fixed 1404 x 1872): page x 810 - 468.
        self.assertAlmostEqual(lines[0].x, 810 - 468, delta=1)


class PaperLibraryTests(unittest.TestCase):
    """LibrarySource.set_page_paper: into the files of a notebook not uploaded yet, else a
    pending SceneFlags edit uploaded by push_edits."""

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = build_cloud()
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway",))
        self.source.refresh()

    def test_a_new_notebook_takes_its_paper_before_the_upload(self):
        node = self.source.create_notebook("Wide", "", NOW, NEW_DOC, NEW_PAGE)
        self.assertFalse(self.source.set_page_paper(node, NEW_PAGE, (1404, 1872), NOW))      # nothing to write
        self.assertTrue(self.source.set_page_paper(node, NEW_PAGE, (1620, 2160), NOW))
        self.assertFalse(self.source.set_page_paper(node, NEW_PAGE, (1620, 2160), NOW))
        self.assertEqual(self.sync.edits.all(), [])
        self.assertEqual(self.source.page_paper(node, NEW_PAGE), (1620, 2160))
        files, parent = self.source.upload_created(NEW_DOC)
        self.sync.create_document(NEW_DOC, files, parent)
        uploaded = self.cloud.item(NEW_DOC)
        page = uploaded[f"{NEW_DOC}/{NEW_PAGE}.rm"]
        self.assertEqual(rm_layer.paper_size(page), (1620, 2160))
        self.assertEqual({i: v for i, _, v in scene_info(page)}[8], w.lww((1, 15), struct.pack("<dd", 1620, 2160)))
        content = json.loads(uploaded[f"{NEW_DOC}.content"])
        self.assertEqual(content["customZoomPageWidth"], 1404)               # not derived from the paper [I]
        metadata = json.loads(uploaded[f"{NEW_DOC}.metadata"])
        self.assertEqual(metadata["lastModified"], str(NOW))                  # the paper does not move it

    def test_a_synced_page_takes_its_paper_as_a_pending_edit(self):
        doc = self.sync.tree.get(THROWAWAY)
        self.assertFalse(self.source.set_page_paper(doc, PAGE1, (1404, 1872), NOW))
        self.assertTrue(self.source.set_page_paper(doc, PAGE1, (954, 1696), NOW))
        self.assertTrue(self.source.set_page_paper(doc, PAGE2, (1620, 2160), NOW))           # no file yet
        edits = self.sync.edits.all()
        self.assertEqual([(type(e), e.page_id, e.paper_size) for e in edits],
                         [(SceneFlags, PAGE1, (954, 1696)), (SceneFlags, PAGE2, (1620, 2160))])
        self.assertIn("paper size 954 x 1696", describe_edit(edits[0]))
        self.assertEqual(self.source.page_paper(doc, PAGE1), (954, 1696))
        # Kept across a restart: the edits file holds the size.
        again = PendingEdits(self.sync.edits.root)
        self.assertEqual(again.all(), edits)
        self.sync.push_edits(THROWAWAY)
        uploaded = self.cloud.item(THROWAWAY)
        page1 = uploaded[f"{THROWAWAY}/{PAGE1}.rm"]
        self.assertEqual(rm_layer.paper_size(page1), (954, 1696))
        # Only SceneInfo, the authors (this client joins) and PageStats (loads + 1) differ.
        changed = (w.BLOCK_SCENE_INFO, w.BLOCK_AUTHORS, w.BLOCK_PAGE_STATS)
        self.assertEqual(blocks_but(page1, *changed), blocks_but(drawn_page(), *changed))
        self.assertEqual(rm_layer.paper_size(uploaded[f"{THROWAWAY}/{PAGE2}.rm"]), (1620, 2160))
        self.assertEqual(self.sync.edits.all(), [])


# -- the app's own QML ---------------------------------------------------------------

class QuickPaperTests(unittest.TestCase):
    """Settings > "Document paper width" through the new-notebook dialog, Quick sheets and
    "Add page", and a wide page opened, laid out and drawn on, through main.qml."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.cloud.put_item(WIDE, meta("wide"), notebook_content(WIDE_PAGE), {f"{WIDE}/{WIDE_PAGE}.rm": wide_page()})
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway", "wide"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.singletons.settings.setRawValue("Onboarding", "ClientEdit", True)
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

    def tearDown(self):
        self.app.singletons.settings.setProperty("paperSize", QSizeF(1404, 1872))
        view = self.qml("DocumentView_")
        if view.property("textMode"):
            view.setProperty("textMode", False)
            self.tq.wait(100)
        if view.property("visible"):
            QMetaObject.invokeMethod(view, "hide")
            self.wait_for(lambda: not view.property("visible"))
        self.source.sync.edits.clear()

    def wait_for(self, condition, ms=8000):
        return self.tq.EditingTests.wait_for(self, condition, ms)

    def qml(self, prefix: str) -> QObject:
        return self.tq.EditingTests.qml(self, prefix)

    def scene_views(self):
        return self.tq.EditingTests.scene_views(self)

    def keys(self, key, modifiers=Qt.KeyboardModifier.ControlModifier) -> None:
        self.tq.PenTests.keys(self, key, modifiers)

    def open_document(self, doc_id: str, page_id: str | None = None):
        return self.tq.PenTests.open_document(self, doc_id, page_id)

    def window_point(self, view, x: float, y: float):
        return self.tq.PenTests.window_point(self, view, x, y)

    def new_ids(self, before) -> list[str]:
        return sorted(set(self.cloud.docs) - set(before))

    def controller_of(self, view):
        return view._controller

    def test_ctrl_n_makes_a_notebook_at_the_paper_width_setting(self):
        self.app.singletons.settings.setProperty("paperSize", QSizeF(1620, 2160))
        before = set(self.cloud.docs)
        dialog = self.qml("NamingDialog_")
        self.keys(Qt.Key.Key_N)
        self.assertTrue(self.wait_for(lambda: dialog.property("visible")), self.notices)
        self.keys(Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        self.assertTrue(self.wait_for(lambda: len(self.new_ids(before)) == 1), self.notices)
        (doc_id,) = self.new_ids(before)
        item = self.cloud.item(doc_id)
        (page,) = [p["id"] for p in json.loads(item[f"{doc_id}.content"])["cPages"]["pages"]]
        # The dialog's setTemplateForPage(..., Settings.paperSize) gave the page its paper
        # in the files uploaded: the page's .rm, SceneInfo field 8 stamped 1:15.
        self.assertEqual(rm_layer.paper_size(item[f"{doc_id}/{page}.rm"]), (1620, 2160))
        self.assertEqual(json.loads(item[f"{doc_id}.content"])["cPages"]["pages"][0]["template"]["value"], "Blank")
        view = self.qml("DocumentView_")
        self.assertTrue(self.wait_for(lambda: view.property("visible")))
        self.assertTrue(self.wait_for(lambda: any(v.property("status") == 2 for v in self.scene_views())))
        scene_view = next(v for v in self.scene_views() if v.property("status") == 2)
        controller = self.controller_of(scene_view)
        self.assertEqual(controller.property("explicitPaperSize"), QSizeF(1620, 2160))
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_a_wide_page_opens_at_its_size_and_is_drawn_on_there(self):
        view = self.open_document(WIDE, WIDE_PAGE)
        controller = self.controller_of(view)
        self.assertEqual(controller.property("explicitPaperSize"), QSizeF(1620, 2160))
        self.assertEqual(controller.property("paperPortraitBounds"), QRectF(-810, 0, 1620, 2160))
        self.assertEqual(controller.property("paperNoteBounds"), QRectF(-1080, 0, 2160, 1620))
        self.assertEqual(controller.property("primaryContentRect"), QRectF(-810, 0, 1620, 2160))
        # DocumentViewport.updateSceneBoundingRect asks for the "landscape" note bounds
        # (+-paperNoteBounds.width / 2); 3.29's SceneView puts its own scroll range back
        # (updateTileManagerScrollBoundary 0x140459330: the exterior boundary, which for
        # an unexpanded portrait page is its paper, -w/2..w/2; ARCHITECTURE.md 5.17).
        self.assertTrue(self.wait_for(lambda: view._tiles._get("sceneBoundingRect").width() == 1620),
                        view._tiles._get("sceneBoundingRect"))
        bounds = view._tiles._get("sceneBoundingRect")
        self.assertEqual((bounds.left(), bounds.top()), (-810, 0))
        self.assertGreaterEqual(bounds.height(), 2160)
        # A stroke drawn at scene x 780, past a 1404-wide page's edge, lands there.
        pens = self.tq.PenTests.pens(self, view)
        pens.tools.chooseTool("fineliner")
        # At scale 1, window pixels are scene units: the drag lands within half a unit.
        view._tiles.setFocalPoint(QPointF(700, 400), 1.0)
        self.tq.wait(400)
        self.tq.PenTests.drag(self, view, [(770, 300), (775, 310), (780, 320), (790, 330)])
        self.assertTrue(self.wait_for(lambda: any(isinstance(e, StrokeAdd) for e in self.source.sync.edits.all())))
        stroke = next(e for e in self.source.sync.edits.all() if isinstance(e, StrokeAdd))
        page = self.source.staged(self.source.sync.tree.get(WIDE)).page(WIDE_PAGE)
        self.assertEqual(len([s for s in rm_stroke.strokes(page) if s.item != (1, 15)]), 1, stroke)
        parsed = [s for s in rm_parser.parse(page).strokes if tuple(s.item_id) != (1, 15)]
        (mine,) = parsed
        # Where it was drawn, past 702 (a 1404-wide page's edge): the pen's point
        # synthesis smooths the last mouse samples in, so the end is not exactly 790.
        self.assertAlmostEqual(min(p.x for p in mine.points), 770, delta=2)
        self.assertTrue(775 < max(p.x for p in mine.points) <= 792, [p.x for p in mine.points])
        self.assertEqual(rm_layer.paper_size(page), (1620, 2160))            # the paper stays as it was
        # Zoomed out, the view does not move when a stroke is kept and the page reloads.
        view._tiles.setFocalPoint(QPointF(-200, 1300), 0.35)
        self.tq.wait(400)
        before = (view._tiles._get("center"), view._tiles._get("scale"))
        count = len(self.source.sync.edits.all())
        self.tq.PenTests.drag(self, view, [(-300, 1200), (-250, 1220), (-200, 1240)])
        self.assertTrue(self.wait_for(lambda: len(self.source.sync.edits.all()) > count))
        self.tq.wait(600)
        self.assertEqual((view._tiles._get("center"), view._tiles._get("scale")), before)
        pens.tools.chooseTool("fineliner")                                     # put down
        # "Add page" passes the page's explicitPaperSize: the new page has that paper.
        dc = self.app.singletons.document_controller
        self.assertTrue(dc.addPageWithTemplateAndPageSize(WIDE, 1, "Blank", controller.property("explicitPaperSize")))
        added = next(e for e in self.source.sync.edits.all() if isinstance(e, PageAdd))
        paper = [e for e in self.source.sync.edits.all() if isinstance(e, SceneFlags)]
        self.assertEqual([(e.page_id, e.paper_size) for e in paper], [(added.new_id, (1620, 2160))])
        # From a page: Quick sheets' and the page menu's addPageWithTemplateAndPaperSizeFromPage.
        self.assertTrue(dc.addPageWithTemplateAndPaperSizeFromPage(WIDE, -1, "Blank", 0))
        paper = [e for e in self.source.sync.edits.all() if isinstance(e, SceneFlags)]
        self.assertEqual([e.paper_size for e in paper], [(1620, 2160), (1620, 2160)])
        # A default page added to the default notebook writes no paper at all.
        self.assertTrue(dc.addPageWithTemplateAndPageSize(THROWAWAY, 1, "Blank", QSizeF(1404, 1872)))
        self.assertEqual(len([e for e in self.source.sync.edits.all() if isinstance(e, SceneFlags)]), 2)
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_the_quick_sheets_page_takes_a_paper_size_while_it_is_alone(self):
        before = set(self.cloud.docs)
        self.keys(Qt.Key.Key_O)
        self.assertTrue(self.wait_for(lambda: len(self.new_ids(before)) == 1), self.notices)
        (doc_id,) = self.new_ids(before)
        view = self.qml("DocumentView_")
        self.assertTrue(self.wait_for(lambda: view.property("visible")))
        view.setProperty("textMode", False)
        QMetaObject.invokeMethod(view, "hide")
        self.assertTrue(self.wait_for(lambda: not view.property("visible")))
        dc = self.app.singletons.document_controller
        self.assertTrue(dc.setPaperSizeForQuickSheetPage(doc_id, QSizeF(954, 1696)))
        (page,) = self.source.page_ids(self.source.sync.tree.get(doc_id))
        self.assertEqual(self.source.page_paper(self.source.sync.tree.get(doc_id), page), (954, 1696))
        self.assertEqual([(type(e), e.paper_size) for e in self.source.sync.edits.all()], [(SceneFlags, (954, 1696))])
        self.assertFalse(dc.setPaperSizeForQuickSheetPage(doc_id, QSizeF()))              # invalid: refused
        self.assertFalse(dc.setPaperSizeForQuickSheetPage(THROWAWAY, QSizeF(954, 1696)))  # not the Quick sheets
        self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
