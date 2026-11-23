"""Images dropped on pages: where 3.29 accepts them, and the page width they give
(ARCHITECTURE.md 5.17, 5.18). Synthetic ids only.

- DocumentViewport's image DropArea is `enabled: actions.canEditDocuments && notePage`:
  files are dropped on notebook pages and on pages inserted into a PDF or EPUB
  (Document::isNotepage 0x1406609e0: redir -1), never on a PDF/EPUB page itself.
- 3.29 places a dropped picture at its own size, one scene unit a pixel, centred where it
  is dropped (0x14042ccd0 -> 0x1404377b0 -> createPersistedSceneImageItem 0x14043c3c0;
  the picture is only shrunk to fit 3200 x 3200, 0x140a4e5e0). A page's width follows its
  items exactly (its scene rect, 0x1408ab350, has no margin): an image inside the paper
  leaves it 1404 wide; one past the paper's side widens the page to the note bounds, or to
  the image itself when it reaches further.
"""

from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from PySide6.QtCore import Q_ARG, QEventLoop, QMetaObject, QObject, QPointF, QRectF, QTimer, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from ui.quick.app import configure, prepare

prepare()
app = QApplication.instance() or QApplication([])
configure(app)

import test_pdf_edit_offline as P
from core.edits import ImageInsert, ItemsTransform
from core.sync import SyncClient
from demo_library import demo_id, ms
from fakecloud import paired_auth
from ui.library import LibrarySource
from ui.pages import PageContent
from ui.quick.bridge import scene as scene_bridge
from viewer import rm_layer, rm_parser
from rmfixtures import ROOT, group_item, scene_tree, tree_node, v6_file
from rmfixtures import image_entries, image_item, image_value, images_table

LAYER = (0, 11)
NOTEBOOK = demo_id("drop notebook")
NOTE_PAGE = demo_id("drop notebook page")
EPUB_INSERTED = demo_id("drop epub inserted page")
NAMES = ("Drop notebook", *P.NAMES)


def wait(ms_: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms_, loop.quit)
    loop.exec()


def picture(path: Path, width: int, height: int) -> Path:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#d02020"))
    image.save(str(path))
    return path


def scene_with_image(x0: float, x1: float) -> rm_parser.PageScene:
    """A page holding one image from scene x0 to x1, y 600..800."""
    key = bytes(range(16))
    vertices = [(x0, 600.0, 0.0, 0.0), (x1, 600.0, 1.0, 0.0), (x1, 800.0, 1.0, 1.0), (x0, 800.0, 0.0, 1.0)]
    data = v6_file(tree_node(LAYER, "Layer 1"), scene_tree(LAYER), group_item(ROOT, (1, 30), LAYER),
                   images_table(image_entries({key: "a.png"})), image_item(LAYER, (1, 18), image_value(key, vertices)))
    return rm_parser.parse(data)


class ExactBoundsTests(unittest.TestCase):
    """PageContent.bounds("exact"): what a note page's view is built on."""

    def bounds(self, x0, x1, crop="exact"):
        return PageContent(scene_with_image(x0, x1), None, None, None).bounds(crop)

    def test_an_image_inside_the_paper_leaves_it_as_it_is(self):
        self.assertEqual(self.bounds(-150, 150), (0.0, 0.0, 1404.0, 1872.0))
        self.assertEqual(self.bounds(-702, 702), (0.0, 0.0, 1404.0, 1872.0))          # edge to edge

    def test_an_image_past_the_side_grows_the_page_by_exactly_that(self):
        self.assertEqual(self.bounds(450, 750), (0.0, 0.0, 702.0 + 750.0, 1872.0))    # page x 1452
        self.assertEqual(self.bounds(-960, 960), (-258.0, 0.0, 1920.0, 1872.0))
        # Exports and PDF pages keep their margin of 20.
        self.assertEqual(self.bounds(450, 750, "page"), (0.0, 0.0, 702.0 + 750.0 + 20.0, 1872.0))


class ImageDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = P.build_cloud()
        cls.cloud.put_item(NOTEBOOK, {"visibleName": NAMES[0], "parent": "", "type": "DocumentType",
                                      "lastModified": ms(1)},
                           {"fileType": "notebook", "formatVersion": 2, "orientation": "portrait", "pageCount": 1,
                            "cPages": {"pages": [P.page_entry(NOTE_PAGE, "ba", None, "P Lines small")],
                                       "uuids": [{"first": P.TABLET_UUID, "second": 1}],
                                       "original": {"timestamp": "0:0", "value": -1},
                                       "lastOpened": {"timestamp": "0:0", "value": ""}}})
        epub = P.pdf_content("epub", [P.page_entry(P.E0, "ba", 0), P.page_entry(EPUB_INSERTED, "bab", None, "P Lines small"),
                                      P.page_entry(P.E1, "bb", 1)], 2)
        cls.cloud.put_item(P.EPUB_DOC, P.meta(P.NAMES[2]), epub,
                           {f"{P.EPUB_DOC}.pdf": P.make_pdf(), f"{P.EPUB_DOC}.epub": b"PK\x03\x04 not a real ebook"})
        cls.small = picture(tmp / "small.png", 300, 200)
        cls.wide = picture(tmp / "wide.png", 1920, 1080)
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"), editable=NAMES)
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.singletons.settings.setRawValue("Onboarding", "ClientEdit", True)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
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
        wait(50)
        cls.tmp.cleanup()

    def setUp(self):
        self.notices.clear()

    def tearDown(self):
        QMetaObject.invokeMethod(self.qml("DocumentView_"), "hide")
        wait(200)
        self.source.sync.edits.clear()

    def qml(self, prefix: str) -> QObject:
        return next(o for o in self.window.findChildren(QObject) if o.metaObject().className().startswith(prefix))

    def drop_area(self) -> QObject:
        return next(o for o in self.window.findChildren(QObject)
                    if o.metaObject().indexOfMethod("dropSupportedImages(QVariant,QVariant)") >= 0)

    def views(self) -> list:
        out, todo = [], [self.window.contentItem()]
        while todo:
            item = todo.pop()
            if isinstance(item, scene_bridge.SceneView) and item.isVisible():
                out.append(item)
            todo.extend(item.childItems())
        return out

    def wait_for(self, condition, ms_=8000) -> bool:
        for _ in range(ms_ // 50):
            if condition():
                return True
            wait(50)
        return condition()

    def open(self, doc_id: str, page_id: str):
        entry = self.app.library.entryForId(doc_id)
        QMetaObject.invokeMethod(self.qml("DocumentView_"), "open", Q_ARG("QVariant", entry))
        self.assertTrue(self.wait_for(lambda: any(v.property("status") == 2 for v in self.views())))
        ids = [p.page_id for p in entry._pages()]
        QMetaObject.invokeMethod(self.qml("DocumentViewport_"), "setCurrentPage", Q_ARG("QVariant", ids.index(page_id)))

        def shown():
            return next((v for v in self.views() if v.property("status") == 2 and v._page_id == page_id
                         and v._card is not None), None)
        self.assertTrue(self.wait_for(lambda: shown() is not None))
        wait(100)
        return entry, ids.index(page_id), shown()

    def drop(self, view, path: Path, scene_x: float):
        before = len(self.source.sync.edits.all())
        self.window.requestActivate()
        QMetaObject.invokeMethod(self.qml("DocumentViewport_"), "forceActiveFocus")
        QMetaObject.invokeMethod(self.drop_area(), "dropSupportedImages", Q_ARG("QVariant", [QUrl.fromLocalFile(str(path))]),
                                 Q_ARG("QVariant", view._tiles.sceneToView(QPointF(scene_x, 700))))
        self.assertTrue(self.wait_for(lambda: len(self.source.sync.edits.all()) > before), self.notices)
        self.assertTrue(self.wait_for(lambda: view._sel is not None))       # selected, as the app selects it
        QMetaObject.invokeMethod(self.qml("DocumentViewport_"), "endItemSelection")
        self.assertTrue(self.wait_for(lambda: view._sel is None and view._content.scene is not None
                                      and len(view._content.scene.placed_images) >= 1))
        self.assertTrue(self.wait_for(lambda: view._image_rect.contains(view._render_rect())))
        return self.source.sync.edits.all()[before]

    def move_image(self, view, dx: float) -> None:
        before = len(self.source.sync.edits.all())
        self.qml("DocumentView_").setProperty("selectionMode", True)
        wait(100)
        image = view._content.scene.placed_images[0]
        self.assertTrue(view._controller.selectImageAtPoint(QPointF(image.bounds()[0] + 5, 700), 0))
        handler = next(o for o in self.window.findChildren(QObject)
                       if o.metaObject().className().startswith("SceneSelectionHandler"))
        self.assertTrue(self.wait_for(lambda: handler.property("visible")))
        QMetaObject.invokeMethod(handler, "moveSelectedItems", Q_ARG("QVariant", dx), Q_ARG("QVariant", 0))
        QMetaObject.invokeMethod(self.qml("DocumentView_"), "exitEditMode")
        self.assertTrue(self.wait_for(lambda: len(self.source.sync.edits.all()) > before), self.notices)
        self.assertTrue(self.wait_for(lambda: view._sel is None))
        wait(300)

    def card(self, view) -> QRectF:
        return view.page_card()

    def staged_page(self, doc_id: str, page_id: str) -> bytes:
        return self.source.staged(self.source.sync.tree.get(doc_id)).page(page_id)

    # -- where 3.29 takes dropped files --
    def test_files_are_dropped_on_note_pages_only(self):
        cases = ((NOTEBOOK, NOTE_PAGE, True), (P.PDF_DOC, P.A4, False), (P.PDF_DOC, P.BLANK, True),
                 (P.EPUB_DOC, P.E0, False), (P.EPUB_DOC, EPUB_INSERTED, True))
        for doc_id, page_id, note in cases:
            entry, index, view = self.open(doc_id, page_id)
            self.assertEqual(entry.isNotepage(index), note, page_id)
            self.assertEqual(self.drop_area().property("enabled"), note, page_id)
            QMetaObject.invokeMethod(self.qml("DocumentView_"), "hide")
            wait(200)

    def test_an_image_dropped_on_a_page_inserted_into_a_pdf_or_epub(self):
        for doc_id, page_id in ((P.PDF_DOC, P.BLANK), (P.EPUB_DOC, EPUB_INSERTED)):
            _, _, view = self.open(doc_id, page_id)
            edit = self.drop(view, self.small, 0)
            self.assertIsInstance(edit, ImageInsert)
            self.assertEqual((edit.doc_id, edit.page_id), (doc_id, page_id))
            self.assertEqual(self.card(view), QRectF(-702, 0, 1404, 1872))
            self.assertEqual(len(rm_parser.parse(self.staged_page(doc_id, page_id)).placed_images), 1)
            QMetaObject.invokeMethod(self.qml("DocumentView_"), "hide")
            wait(200)
            self.source.sync.edits.clear()
        self.assertEqual(self.notices, [])

    # -- the page width an image gives --
    def test_an_image_inside_the_page_keeps_its_width(self):
        _, _, view = self.open(NOTEBOOK, NOTE_PAGE)
        edit = self.drop(view, self.small, 0)
        xs, ys = [v[0] for v in edit.vertices], [v[1] for v in edit.vertices]
        self.assertEqual((min(xs), max(xs), min(ys), max(ys)), (-150, 150, 600, 800))   # a unit a pixel, centred
        self.assertEqual(view._bounds, QRectF(-702, 0, 1404, 1872))
        self.assertEqual(self.card(view), QRectF(-702, 0, 1404, 1872))
        self.assertEqual(view._tiles._get("sceneBoundingRect").width(), 1404)
        self.assertEqual(rm_layer.paper_size(self.staged_page(NOTEBOOK, NOTE_PAGE)), (1404, 1872))

    def test_an_image_past_the_side_widens_the_page_and_back_inside_it_does_not(self):
        _, _, view = self.open(NOTEBOOK, NOTE_PAGE)
        self.drop(view, self.small, 0)
        self.move_image(view, 600)                                  # x 450..750: past 702
        self.assertIsInstance(self.source.sync.edits.all()[-1], ItemsTransform)
        self.assertTrue(self.wait_for(lambda: self.card(view) == QRectF(-936, 0, 1872, 1872)), self.card(view))
        self.assertEqual(view._bounds.right(), 750)                 # exactly the image, no margin
        self.assertEqual(view._tiles._get("sceneBoundingRect").width(), 1872)
        self.move_image(view, -600)
        self.assertTrue(self.wait_for(lambda: self.card(view) == QRectF(-702, 0, 1404, 1872)), self.card(view))
        self.assertEqual(view._tiles._get("sceneBoundingRect").width(), 1404)

    def test_a_picture_wider_than_the_page_widens_it_to_its_own_edges(self):
        _, _, view = self.open(NOTEBOOK, NOTE_PAGE)
        edit = self.drop(view, self.wide, 0)
        xs = [v[0] for v in edit.vertices]
        self.assertEqual((min(xs), max(xs)), (-960, 960))           # 1920 px placed at 1920 units, as 3.29 places it
        self.assertTrue(self.wait_for(lambda: self.card(view) == QRectF(-960, 0, 1920, 1872)), self.card(view))
        self.assertEqual(view._image_rect, self.card(view))
        self.assertEqual(rm_layer.paper_size(self.staged_page(NOTEBOOK, NOTE_PAGE)), (1404, 1872))   # the paper stays
        # Undo takes the picture away, and the page is its paper again.
        self.assertTrue(self.wait_for(lambda: view._controller.property("undoAvailable")))
        view._controller.undo()
        self.assertTrue(self.wait_for(lambda: self.card(view) == QRectF(-702, 0, 1404, 1872)), self.card(view))


if __name__ == "__main__":
    unittest.main()
