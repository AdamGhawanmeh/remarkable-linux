"""The white page card of a note page and what is drawn on it (ARCHITECTURE.md 5.17).

DocumentView's ShadowOverlay draws the white card at the tile manager's
sceneBoundingRect, which DocumentViewport.updateSceneBoundingRect makes the paper's note
bounds on a note page (x from -h/2 to h/2, and at least 4/3 h down). 3.29 renders its
tiles over that card and paints the template into each tile, so the rules of a portrait
page run across the card's whole width and on below the paper. The client used to render
only the paper, which left blank white strips beside and below the template. Synthetic
ids only."""

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
        # The card of a portrait note page in page coordinates: x -234..1638, y 0..2496.
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

    def card(self, view) -> QRectF:
        return view._tiles._get("sceneBoundingRect")

    def test_a_portrait_page_renders_its_whole_card(self):
        view = self.open(self.portrait)
        self.assertEqual(view._bounds, QRectF(-702, 0, 1404, 1872))
        self.assertEqual(self.card(view), QRectF(-936, 0, 1872, 2496))       # note bounds, 4/3 h down
        self.assertEqual(view._image_rect, self.card(view))                  # no bare white strips
        self.assertEqual(self.qml("DocumentView_").property("pageBounds").width(),
                         view._tiles.sceneToView(self.card(view)).width())   # the card drawn is that rect
        image = view._card                                                   # what is shown
        scale = image.width() / view._image_rect.width()
        page = view._image                                                   # the paper's own part, as before
        self.assertAlmostEqual(page.width() / scale, view._bounds.width(), delta=1)
        self.assertAlmostEqual(page.height() / scale, view._bounds.height(), delta=1)
        x_left = round((-900 + 936) * scale)                                 # left of the paper (scene x < -702)
        x_right = round((900 + 936) * scale)                                 # right of it (scene x > 702)
        below = range(round(2000 * scale), image.height())                   # below the paper (y > 1872)
        rows = range(0, image.height())
        self.assertTrue(inked(image, x_left, rows))
        self.assertTrue(inked(image, x_right, rows))
        self.assertTrue(any(image.pixelColor(image.width() // 2, y).lightness() < 235 for y in below))

    def test_a_page_with_ink_below_the_paper_renders_its_grown_card(self):
        view = self.open(self.expanded)
        card = self.card(view)
        self.assertGreater(view._bounds.bottom(), 1872)
        self.assertGreaterEqual(card.bottom(), view._bounds.bottom())
        self.assertTrue(view._image_rect.contains(card) and view._image_rect.contains(view._bounds))

    def test_a_landscape_page_renders_its_card(self):
        view = self.open(self.landscape)
        self.assertEqual(view._bounds, QRectF(-936, 0, 1872, 1404))
        self.assertEqual(self.card(view), QRectF(-936, 0, 1872, 2496))
        self.assertEqual(view._image_rect, self.card(view))

    def test_a_pdf_page_is_not_widened(self):
        pdf = next(d for d in self.app.library.tree.documents()
                   if d.content.file_type.value == "pdf" and d.content.page_ids)
        view = self.open(pdf.id)
        self.assertFalse(view._content is None or not view._content.has_pdf_page)
        self.assertEqual(view._image_rect, view._bounds)


if __name__ == "__main__":
    unittest.main()
