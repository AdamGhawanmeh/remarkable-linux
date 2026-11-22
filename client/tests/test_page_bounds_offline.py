"""The white page card of a note page, its scroll range and what is drawn on it
(ARCHITECTURE.md 5.17).

3.29's QML (DocumentViewport.updateSceneBoundingRect) asks for the paper's note bounds,
-h/2..h/2, as a note page's scroll range, and DocumentView draws the white card at that
range, twice as tall. 3.29's C++ SceneView then puts its own range back
(updateTileManagerScrollBoundary): its exterior boundary, which is the portrait paper
unless ink or text reaches past its sides. The client did only the QML part, so an
unexpanded portrait page showed a 1872-wide card. Now the card is the page (the
paper, grown where ink or text goes past it); the template is drawn over exactly that.
Synthetic ids only."""

from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from PySide6.QtCore import Q_ARG, QEventLoop, QMetaObject, QObject, QRectF, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from ui.quick.app import configure, prepare

prepare()
app = QApplication.instance() or QApplication([])
configure(app)

from core.sync import SyncClient
from demo_library import build_demo_cloud, demo_id, ms
from fakecloud import paired_auth
from rmfixtures import ROOT, group_item, line_item, points_v2, scene_tree, tree_node, v6_file
from ui.library import LibrarySource
from ui.pages import PageContent, render_content
from ui.quick.bridge import scene as scene_bridge
from viewer import rm_parser
from viewer.templates import default_library

LAYER = (0, 11)
LINES = default_library().resolve("P Lines small")
LS_LINES = default_library().resolve("LS Lines small")
TEMPLATES = LINES is not None and LS_LINES is not None


def wait(ms_: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms_, loop.quit)
    loop.exec()


def page(strokes: list[list[tuple[float, float]]]) -> bytes:
    lines = [line_item(LAYER, (1, 40 + i), tool=15, color=0,
                       points=points_v2([(x, y, 1, 0, 3, 200) for x, y in pts])) for i, pts in enumerate(strokes)]
    return v6_file(tree_node(LAYER, "Layer 1"), scene_tree(LAYER), group_item(ROOT, (1, 30), LAYER), *lines)


def notebook(cloud, name: str, template: str, orientation: str, strokes) -> str:
    doc, pid = demo_id(name), demo_id(name + " page")
    content = {"fileType": "notebook", "formatVersion": 2, "orientation": orientation, "pageCount": 1,
               "cPages": {"pages": [{"id": pid, "idx": {"timestamp": "1:2", "value": "ba"},
                                     "template": {"timestamp": "1:1", "value": template}}],
                          "uuids": [{"first": "00000000-0000-4000-8000-00000000aaaa", "second": 1}],
                          "original": {"timestamp": "0:0", "value": -1},
                          "lastOpened": {"timestamp": "0:0", "value": ""}}}
    cloud.put_item(doc, {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": ms(1)},
                   content, {f"{doc}/{pid}.rm": page(strokes)})
    return doc


def inked(image: QImage, x: int, ys: range) -> bool:
    return any(image.pixelColor(x, y).lightness() < 235 for y in ys)


@unittest.skipUnless(TEMPLATES, "the app's page templates are not installed (install.py)")
class RenderAreaTests(unittest.TestCase):
    def content(self, template, landscape=False) -> PageContent:
        scene = rm_parser.parse(page([[(-100, 300), (100, 300)]]))
        return PageContent(scene, None, None, template, landscape=landscape)

    def test_the_template_covers_the_area_asked_for(self):
        content = self.content(LINES)
        # Without an area: the paper only, 1404 x 1872.
        plain = render_content(content, 0.25)
        self.assertEqual((plain.width(), plain.height()), (351, 468))
        # An area wider and taller than the paper (page coordinates): the template fills it all.
        card = render_content(content, 0.25, area=(-234.0, 0.0, 1872.0, 2496.0))
        self.assertEqual((card.width(), card.height()), (468, 624))
        rows = range(0, 624)
        self.assertTrue(inked(card, 5, rows))                 # rules left of the paper (page x < 0)
        self.assertTrue(inked(card, 462, rows))               # and right of it (page x > 1404)
        below = range(round(1900 * 0.25), 624)
        self.assertTrue(any(card.pixelColor(234, y).lightness() < 235 for y in below))   # and below 1872

    def test_a_landscape_page_fills_its_card_too(self):
        content = self.content(LS_LINES, landscape=True)
        card = render_content(content, 0.25, area=(-234.0, 0.0, 1872.0, 2496.0))
        self.assertTrue(any(card.pixelColor(234, y).lightness() < 235 for y in range(round(1500 * 0.25), 624)))


@unittest.skipUnless(TEMPLATES, "the app's page templates are not installed (install.py)")
class PageCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_demo_cloud()
        cls.portrait = notebook(cls.cloud, "Bounds portrait", "P Lines small", "portrait",
                                [[(-500 + 50 * i, 300) for i in range(20)]])
        cls.expanded = notebook(cls.cloud, "Bounds expanded", "P Lines small", "portrait",
                                [[(-500 + 60 * i, 1500 + 40 * i) for i in range(28)]])
        cls.landscape = notebook(cls.cloud, "Bounds landscape", "LS Lines small", "landscape",
                                 [[(-700 + 50 * i, 300) for i in range(28)]])
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def qml(self, prefix: str) -> QObject:
        return next(o for o in self.window.findChildren(QObject) if o.metaObject().className().startswith(prefix))

    def views(self) -> list:
        out, todo = [], [self.window.contentItem()]
        while todo:
            item = todo.pop()
            if isinstance(item, scene_bridge.SceneView) and item.isVisible():
                out.append(item)
            todo.extend(item.childItems())
        return out

    def open(self, doc_id: str):
        QMetaObject.invokeMethod(self.qml("DocumentView_"), "open",
                                 Q_ARG("QVariant", self.app.library.entryForId(doc_id)))
        for _ in range(160):
            wait(50)
            shown = [v for v in self.views() if v.property("status") == 2]
            if shown and shown[0]._card is not None and shown[0]._image_rect.contains(shown[0]._render_rect()):
                wait(100)
                return shown[0]
        self.fail("the page did not render")

    def tearDown(self):
        QMetaObject.invokeMethod(self.qml("DocumentView_"), "hide")
        wait(200)

    def scroll(self, view) -> QRectF:
        return view._tiles._get("sceneBoundingRect")

    def shown_card(self) -> QRectF:
        return self.qml("DocumentView_").property("pageBounds")

    def assert_card(self, view, card: QRectF) -> None:
        self.assertEqual(view.page_card(), card)
        self.assertEqual(view._image_rect, card)                              # the template over the card only
        shown = view._tiles.sceneToView(card)
        bounds = self.shown_card()
        for a, b in zip(bounds.getRect(), shown.getRect()):                  # the white card and its shadow
            self.assertAlmostEqual(a, b, delta=0.5)

    def test_an_unexpanded_portrait_page_is_its_paper(self):
        view = self.open(self.portrait)
        self.assertEqual(view._bounds, QRectF(-702, 0, 1404, 1872))
        self.assertEqual(view.exterior_boundary(), QRectF(-702, 0, 1404, 1872))   # 3.29's, ExcludeMargin
        self.assert_card(view, QRectF(-702, 0, 1404, 1872))
        scroll = self.scroll(view)
        self.assertEqual((scroll.left(), scroll.width(), scroll.top()), (-702, 1404, 0))
        self.assertGreaterEqual(scroll.bottom(), 1872)
        self.assertLess(scroll.bottom(), 1872 + 400)                         # room for the page indicator only
        image = view._card
        scale = image.width() / view._image_rect.width()
        self.assertEqual(image.height(), round(1872 * scale))                # nothing below the paper
        self.assertTrue(inked(image, image.width() - 2, range(0, image.height())))   # rules reach the right edge

    def test_the_qml_note_bounds_do_not_widen_a_portrait_page(self):
        view = self.open(self.portrait)
        view._tiles.setProperty("sceneBoundingRect", QRectF(-936, 0, 1872, 2496))   # updateSceneBoundingRect's
        wait(50)
        self.assertEqual(self.scroll(view).width(), 1404)
        self.assert_card(view, QRectF(-702, 0, 1404, 1872))
        grown = QRectF(-702, -40, 1404, self.scroll(view).height() + 80)    # onAreaSelected's extra room stays
        view._tiles.setProperty("sceneBoundingRect", grown)
        wait(50)
        self.assertEqual(self.scroll(view), grown)

    def test_a_page_with_ink_past_its_sides_and_bottom_grows_its_card(self):
        view = self.open(self.expanded)
        bounds = view._bounds
        self.assertGreater(bounds.bottom(), 1872)
        self.assertGreater(bounds.right(), 936)
        card = view.page_card()
        # Past the portrait paper's side: 3.29 widens to the note bounds, and to the ink.
        self.assertEqual((card.left(), card.right()), (-936, bounds.right()))
        self.assertEqual((card.top(), card.bottom()), (0, bounds.bottom()))
        self.assert_card(view, card)
        self.assertGreaterEqual(self.scroll(view).bottom(), bounds.bottom())

    def test_a_landscape_page_is_its_paper(self):
        view = self.open(self.landscape)
        self.assertEqual(view._bounds, QRectF(-936, 0, 1872, 1404))
        self.assert_card(view, QRectF(-936, 0, 1872, 1404))
        self.assertEqual(self.scroll(view).width(), 1872)

    def test_a_pdf_page_is_not_widened(self):
        pdf = next(d for d in self.app.library.tree.documents()
                   if d.content.file_type.value == "pdf" and d.content.page_ids)
        view = self.open(pdf.id)
        self.assertFalse(view._content is None or not view._content.has_pdf_page)
        self.assertEqual(view._image_rect, view._bounds)
        self.assertEqual(view.page_card(), view._bounds)


if __name__ == "__main__":
    unittest.main()
