"""The QML client: the official main.qml over the Python bridges.

API parity: each bridge class exposes what the app's own C++ class exposes to QML
(src/ui/quick/bridge/qml_api.json, read from reMarkable.exe's moc metadata): every property,
signal, slot and invokable method (by name and argument count) and every enum value.

The rest loads qrc:/qml/main.qml (offscreen) over the demo library and drives it.
"""

from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from PySide6.QtCore import (Q_ARG, QEventLoop, QMetaObject, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer,
                            QUrl, Signal, qVersion)
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtNetwork import QNetworkInformation
from PySide6.QtWidgets import QApplication

from ui.quick.app import configure, prepare

prepare()
app = QApplication.instance() or QApplication([])
configure(app)

from core.models import ROOT_ID, TRASH_ID
from core.sync import SyncClient
from demo_library import build_demo_cloud, demo_id, make_pdf, ms
from fakecloud import paired_auth
from ui.library import LibrarySource
from ui.quick import bridge
from ui.quick import app as quick_app
from ui.quick.app import QuickApp
from ui.quick.bridge import images, items, library, platform, scene

API = json.loads((HERE.parent / "src" / "ui" / "quick" / "bridge" / "qml_api.json").read_text())

# Bridge class -> the app classes it stands for (their own members, then their bases').
# "Entry:enums": QML uses the C++ Entry and Document classes only for their enums; the
# objects it holds are QmlEntryWrapper/QmlDocumentWrapper.
CLASSES = {
    library.LibraryView: ["LibraryView"],
    library.LibraryNavigator: ["LibraryNavigator", "LibraryView"],
    library.LibrarySearch: ["LibrarySearch", "LibraryView"],
    library.EntryWrapper: ["QmlEntryWrapper", "Entry:enums"],
    library.DocumentWrapper: ["QmlDocumentWrapper", "QmlEntryWrapper", "Document:enums"],
    library.DocumentIOEnums: ["DocumentIO"],
    library.Library: ["xofm::libs::library::qml::LibraryFacade"],
    library.LibraryController: ["xofm::libs::library::LibraryController"],
    library.DocumentSelection: ["xofm::libs::library::DocumentSelection"],
    library.PageSelection: ["PageSelection"],
    library.DocumentSync: ["xofm::libs::syncinterface::DocumentSyncInterface"],
    items.DocumentCover: ["DocumentCover"],
    items.TriangleItem: ["TriangleItem"],
    items.FontExtender: ["ark::controls::FontExtender"],
    items.ShareNative: ["ShareNative"],
    images.ThumbnailProvider: ["ThumbnailProvider"],
    platform.Settings: ["Settings", "SettingsBase"],
    platform.ArkTokensSettings: ["ark::tokens::Settings"],
    platform.PlatformHelpers: ["PlatformHelpers"],
    platform.PlatformApp: ["PlatformApp", "XoApp"],
    platform.Authentication: ["Authentication"],
    platform.UserInfo: ["xofm::libs::user::UserInfo"],
    platform.NetworkClient: ["NetworkClient", "AbstractNetworkClient"],
    platform.DocumentController: ["xofm::libs::library::DocumentController", "TaskTracker"],
    platform.Clipboard: ["Clipboard"],
    platform.LanguageSettings: ["xofm::libs::localization::LanguageSettings"],
    platform.LinkProvider: ["LinkProviderWrapper"],
    platform.DeviceRecovery: ["xofm::libs::recovery::DeviceRecovery"],
    bridge.DocumentImporter: ["xofm::libs::library::DocumentImporter"],
    bridge.ScreenShareClient: ["screenshare::ClientFacade"],
    bridge.ScreenSharePainter: ["screenshare::Painter"],
    platform.HandwritingLanguageModel: ["HandwritingLanguageModel"],
    scene.SceneView: ["SceneView"],
    scene.SceneController: ["SceneController"],
    scene.SceneTileManager: ["SceneTileManager"],
    scene.SceneKeyHandler: ["SceneKeyHandler"],
    scene.SceneKeyHandlerAction: ["SceneKeyHandlerAction"],
    scene.SceneParticipant: ["SceneParticipant"],
    scene.SceneRenderContext: ["SceneRenderContext"],
    scene.ParagraphStyle: ["scene::ParagraphStyle"],
    scene.Page: ["Page"],
    scene.DocumentLockManager: ["DocumentLockManager"],
    scene.SceneLinkHandler: ["SceneLinkHandler"],
    scene.PageSearchResultOverlayItem: ["PageSearchResultOverlayItem"],
    scene.DocumentSearchController: ["DocumentSearchController"],
    scene.PagePickerModel: ["PagePickerModel"],
    scene.Recognizer: ["Recognizer"],
    scene.RecognitionJob: ["RecognitionJob"],
    scene.SelectionImage: ["SelectionImage"],
    scene.SceneLayersModel: ["xofm::libs::sceneview::SceneLayersModel"],
}

# Members a QML engine cannot call, so not exposed: C++-only types in their signatures.
NOT_QML = {
    "TaskTracker": {"execute", "onFinished", "onFailed"},
}


def real_warnings(warnings: list[str]) -> list[str]:
    """QML warnings that are binding errors. Not: the platform's StandardKey notices (Qt
    warns when a key has several bindings; the shipped Qt 6.10.3 has the same check), and
    on Qt 6.11 that two of the app's components shadow a base member (RMIconButton.enabled,
    Background.data), a check the app's Qt does not have."""
    notices = ["Only binding to one of multiple key bindings"]
    if tuple(int(x) for x in qVersion().split(".")[:2]) >= (6, 11):
        notices.append("overrides a member of the base object")
    return [w for w in warnings if not any(n in w for n in notices)]


def wait(ms: int) -> None:
    """Spin the event loop (unlike QTest.qWait this releases the GIL, so Qt Quick's
    image-loading thread can run Python providers)."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class ApiParityTests(unittest.TestCase):
    def check(self, cls: type, names: list[str]) -> None:
        meta = cls.staticMetaObject
        properties = {meta.property(i).name() for i in range(meta.propertyCount())}
        methods: dict[str, set[int]] = {}
        for i in range(meta.methodCount()):
            m = meta.method(i)
            methods.setdefault(bytes(m.name()).decode(), set()).add(m.parameterCount())
        enums = {}
        for i in range(meta.enumeratorCount()):
            e = meta.enumerator(i)
            enums[e.name()] = {e.key(k): e.value(k) for k in range(e.keyCount())}
        for name in names:
            enums_only = name.endswith(":enums")
            spec = dict(API["classes"][name.removesuffix(":enums")])
            if enums_only:
                spec["properties"], spec["methods"] = [], []
            for prop in spec["properties"]:
                with self.subTest(cls=cls.__name__, property=prop["name"]):
                    self.assertIn(prop["name"], properties)
            for m in spec["methods"]:
                if m["name"] in NOT_QML.get(name, ()):
                    continue
                if m["kind"] in ("slot", "method") and not m["args"] and m["name"] in properties:
                    continue    # a property's C++ getter, read through the property
                with self.subTest(cls=cls.__name__, method=m["name"], args=len(m["args"])):
                    self.assertIn(len(m["args"]), methods.get(m["name"], set()))
            for enum, info in spec["enums"].items():
                with self.subTest(cls=cls.__name__, enum=enum):
                    if enum in enums:
                        self.assertEqual(enums[enum], info["keys"])
                    else:   # a flags alias (Filters for Filter) shares its keys
                        self.assertIn(info["keys"], list(enums.values()))

    def test_each_bridge_matches_the_app(self):
        for cls, names in CLASSES.items():
            self.check(cls, names)

    def test_every_registered_type_is_checked(self):
        registered = ({cls for _, _, cls in bridge.UNCREATABLE} | {cls for _, _, cls in bridge.CREATABLE}
                      | {cls for _, _, _, cls in bridge.SINGLETONS})
        unchecked = registered - set(CLASSES)
        self.assertEqual(unchecked, set())

    def test_registered_names_are_the_apps(self):
        spec = {(t["module"], t["name"]) for t in API["types"]}
        ours = ({(m, n) for m, n, _ in bridge.UNCREATABLE} | {(m, n) for m, n, _ in bridge.CREATABLE}
                | {(m, n) for _, m, n, _ in bridge.SINGLETONS})
        self.assertLessEqual(ours, spec)


class LastOpenedPageTests(unittest.TestCase):
    def test_a_notebook_never_opened_on_a_page_opens_at_the_first(self):
        """The tablet writes lastOpenedPage -1 for a new notebook; DocumentViewport.goToPage
        refuses it, which left the document loading for ever."""
        from core.models import Content, DocumentTree, Metadata, make_node
        pages = {"pages": [{"id": f"00000000-0000-4000-8000-00000000000{i}",
                            "idx": {"timestamp": "1:2", "value": f"b{chr(97 + i)}"}} for i in range(3)]}
        lib = library.Library()
        for doc_id, page in (("11111111-1111-4111-8111-111111111111", -1),
                             ("22222222-2222-4222-8222-222222222222", 7),
                             ("33333333-3333-4333-8333-333333333333", 1)):
            node = make_node(doc_id, Metadata.parse({"visibleName": "n", "type": "DocumentType", "parent": "",
                                                     "lastOpenedPage": page}),
                             Content.parse({"fileType": "notebook", "formatVersion": 2, "cPages": pages}))
            lib.set_tree(DocumentTree([*lib.tree.documents(), node]))
        self.assertEqual([lib.entryForId(d.id).property("lastOpenedPage") for d in
                          sorted(lib.tree.documents(), key=lambda d: d.id)], [0, 2, 1])


class RelativeTimeTests(unittest.TestCase):
    """StringUtils' relative time (xofm/libs/library/src/shared.cpp), in the app's
    English translation."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication, QTranslator
        from ui.quick import assets
        from ui.quick.app import OVERRIDES, TRANSLATION
        assets.register_resources(overrides=OVERRIDES)
        cls.translator = QTranslator()
        assert cls.translator.load(TRANSLATION.format("en"))
        QCoreApplication.installTranslator(cls.translator)

    @classmethod
    def tearDownClass(cls):
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.removeTranslator(cls.translator)

    def test_thresholds_and_rounding(self):
        minute, hour, day = 60_000, 3_600_000, 86_400_000
        cases = [(59_999, "just now"), (minute, "1 minute ago"), (90 * 1000, "2 minutes ago"),
                 (hour, "1 hour ago"), (36 * hour, "2 days ago"), (day * 29, "29 days ago"),
                 (day * 30, "1 month ago"), (day * 365, "1 year ago"), (-2 * day, "in 2 days")]
        for delta, text in cases:
            with self.subTest(delta=delta):
                self.assertEqual(library.relative_time(delta), text)


class PaintedItemTests(unittest.TestCase):
    """The painted items, drawn as the app's paint() functions draw them."""

    def render(self, item, w=120, h=160) -> QImage:
        item.setWidth(w)
        item.setHeight(h)
        image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        item.paint(painter)
        painter.end()
        return image

    def test_triangle_corners(self):
        t = items.TriangleItem()
        t.setProperty("color", QColor("black"))
        t.setProperty("edge", int((Qt.Edge.BottomEdge | Qt.Edge.LeftEdge).value))
        image = self.render(t, 40, 40)
        self.assertEqual(image.pixelColor(5, 35).alpha(), 255)     # bottom-left half filled
        self.assertEqual(image.pixelColor(35, 5).alpha(), 0)       # top-right half empty

    def test_notebook_cover_has_spine_and_rounded_corners(self):
        cover = items.DocumentCover()
        cover.setProperty("documentType", 1)
        cover.setProperty("color", QColor("#808080"))
        cover.setProperty("cached", False)
        image = self.render(cover)
        self.assertEqual(image.pixelColor(6, 80).name(), "#808080")     # spine, decorationScale * 12 wide
        self.assertEqual(image.pixelColor(119, 0).name(), "#ffffff")    # outside the rounded corner: background
        self.assertEqual(image.pixelColor(60, 80).alpha(), 0)           # transparent inside the outline

    def test_pdf_cover_folds_the_corner(self):
        cover = items.DocumentCover()
        cover.setProperty("documentType", 2)
        cover.setProperty("backgroundColor", QColor("#ff0000"))
        cover.setProperty("cached", False)
        image = self.render(cover)
        self.assertEqual(image.pixelColor(118, 2).name(), "#ff0000")    # cut corner shows the background
        self.assertEqual(image.pixelColor(100, 30).name(), "#ffffff")   # the folded flap

    def test_cached_cover_draws_the_same(self):
        a, b = items.DocumentCover(), items.DocumentCover()
        for c in (a, b):
            c.setProperty("documentType", 3)
        b.setProperty("cached", False)
        self.assertEqual(self.render(a), self.render(b))

    def test_font_extender_switches_font_antialiasing(self):
        no_antialias = QFont.StyleStrategy.NoAntialias.value
        f = items.FontExtender()
        f.setProperty("antialiasing", False)
        self.assertTrue(f.property("font").styleStrategy().value & no_antialias)
        f.setProperty("antialiasing", True)
        self.assertFalse(f.property("font").styleStrategy().value & no_antialias)

    def test_drag_icon_badge(self):
        provider = images.DragIconImageProvider()
        size = QSize()
        pixmap = provider.requestPixmap("3", size, QSize())
        self.assertEqual((pixmap.width(), pixmap.height(), pixmap.devicePixelRatio()), (64, 64, 2.0))
        self.assertEqual(pixmap.toImage().pixelColor(16, 3).name(), "#000000")


class TileManagerTests(unittest.TestCase):
    def manager(self, width=800, height=600):
        tiles = scene.SceneTileManager()
        tiles.setProperty("viewportSize", QSize(width, height))
        tiles.setProperty("sceneBoundingRect", QRectF(-702, 0, 1404, 1872))
        return tiles

    def test_maps_view_and_scene(self):
        tiles = self.manager()
        tiles.setFocalPoint(QPointF(0, 1000), 0.5)
        corner = tiles.sceneToView(QPointF(-702, 1000))
        self.assertEqual((corner.x(), corner.y()), (49.0, 300.0))
        back = tiles.viewToScene(corner)
        self.assertAlmostEqual(back.x(), -702)
        self.assertAlmostEqual(back.y(), 1000)

    def test_clamps_to_the_paper(self):
        tiles = self.manager()
        tiles.setFocalPoint(QPointF(500, -400), 0.5)     # page narrower than the view: centred
        self.assertEqual(tiles.property("center"), QPointF(0, 600))     # and the top edge stays in view
        tiles.moveViewBy(QPointF(0, -10_000))            # scrolled far down: stops at the bottom
        self.assertEqual(tiles.property("center").y(), 1872 - 600)

    def test_zoom_keeps_the_point_under_the_cursor(self):
        tiles = self.manager()
        tiles.setFocalPoint(QPointF(0, 936), 1.0)
        before = tiles.viewToScene(QPointF(300, 200))
        tiles.zoomOnPoint(QPointF(300, 200), 1.5)
        after = tiles.viewToScene(QPointF(300, 200))
        self.assertAlmostEqual(before.x(), after.x())
        self.assertAlmostEqual(before.y(), after.y())
        self.assertAlmostEqual(tiles.property("scale"), 1.5)


class PdfLinkTests(unittest.TestCase):
    def test_links_in_scene_coordinates(self):
        from pypdf import PdfWriter
        from pypdf.annotations import Link
        from ui.pages import PdfFile, pdf_target_rect
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "links.pdf"
            writer = PdfWriter()
            writer.add_blank_page(612, 792)
            writer.add_blank_page(612, 792)
            writer.add_annotation(0, Link(rect=(72, 692, 172, 720), url="https://remarkable.com"))
            writer.add_annotation(0, Link(rect=(72, 72, 172, 100), target_page_index=1))
            with path.open("wb") as f:
                writer.write(f)
            pdf = PdfFile(path)
            frame = pdf_target_rect(pdf.page_size(0))
            links = scene.pdf_links(pdf, 0, frame, {0: 0, 1: 1})
            pdf.close()
        by_url = {link["url"]: link for link in links}
        self.assertIn("https://remarkable.com", by_url)
        top = by_url["https://remarkable.com"]["rect"]
        px = 226 / 72
        self.assertAlmostEqual(top.left(), frame.x() + 72 * px - 702, delta=1)
        self.assertAlmostEqual(top.top(), (792 - 720) * px, delta=1)    # PDF y grows upwards
        self.assertEqual(by_url[""]["page"], 1)
        controller = scene.SceneController()
        controller.set_page(frame, frame, [], links)
        self.assertTrue(controller.linkAt(top.center())["valid"])
        self.assertFalse(controller.linkAt(QPointF(-702, 0))["valid"])


class QuickAppTests(unittest.TestCase):
    """main.qml from the app's resources, over the demo library."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_demo_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
        cls.window = cls.app.window

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        # Free the Python wrappers of QML objects before the engine deletes the objects.
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def names(self, ids) -> list[str]:
        return [self.app.library.tree.get(i).name for i in ids]

    def entry_model(self):
        # main.qml's _entryModel: the navigator whose filter the sidebar sets.
        return next(n for n in self.window.findChildren(library.LibraryNavigator)
                    if not n.property("filter") & library.LibraryView.Filter["Collections"]
                    and n.property("library") is self.app.library and not n.property("filterTags"))

    def test_loads_without_binding_errors(self):
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_my_files_lists_the_root(self):
        tree = self.app.library.tree
        model = self.entry_model()
        model.setProperty("filter", int(library.LibraryView.Filter["Documents"]))
        model.setProperty("currentDirectoryId", "")
        model.updateNow()
        expected = {n.id for n in tree.children(ROOT_ID) if not n.is_folder}
        self.assertEqual(set(model.property("entryIds")), expected)

    def test_trash_and_notebooks_filters(self):
        model = self.entry_model()
        model.setProperty("currentDirectoryId", TRASH_ID)
        model.setProperty("filter", int(library.LibraryView.Filter["Documents"]))
        model.updateNow()
        self.assertEqual(self.names(model.property("entryIds")), ["Old draft"])
        self.assertEqual(model.property("rootDirectoryId"), TRASH_ID)
        model.setProperty("currentDirectoryId", "")
        model.setProperty("filter", int(library.LibraryView.Filter["NativeDocuments"]))
        model.setProperty("ignoreParent", True)
        model.updateNow()
        notebooks = {d.name for d in self.app.library.tree.documents()
                     if d.file_type.name == "NOTEBOOK" and not self.app.library.tree.is_trashed(d.id)}
        self.assertEqual(set(self.names(model.property("entryIds"))), notebooks)
        model.setProperty("ignoreParent", False)
        model.setProperty("filter", int(library.LibraryView.Filter["Documents"]))

    def test_entries_read_like_the_app(self):
        tree = self.app.library.tree
        sketch = next(d for d in tree.documents() if d.name == "Sketchbook")
        entry = self.app.library.entryForId(sketch.id)
        self.assertIsInstance(entry, library.DocumentWrapper)
        self.assertEqual(entry.property("subtext"), "Page 1 of 1")
        self.assertEqual(entry.property("type"), 1)
        story = next(d for d in tree.documents() if d.name == "A Short Story")
        self.assertEqual(self.app.library.entryForId(story.id).property("subtext"), "0% read")

    def scene_views(self):
        views, todo = [], [self.window.contentItem()]
        while todo:
            item = todo.pop()
            if isinstance(item, scene.SceneView) and item.isVisible():
                views.append(item)
            todo.extend(item.childItems())
        return views

    def wait_for(self, condition, ms=6000):
        for _ in range(ms // 100):
            if condition():
                return True
            wait(100)
        return condition()

    def test_document_view_draws_the_page_and_closes(self):
        import shiboken6
        from PySide6.QtTest import QTest
        self.window.resize(1270, 875)
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(90, 68))
        wait(500)
        QTest.mouseDClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(295, 370))
        self.assertTrue(self.wait_for(lambda: any(v.property("status") == 2 for v in self.scene_views())))
        view = next(v for v in self.scene_views() if v.property("status") == 2)
        self.assertEqual(view.property("pageStatus"), scene.PAGE_READY)
        controller, tiles = view._get("controller"), view._get("tileManager")
        self.assertGreaterEqual(controller.property("boundingRect").height(), 1872)
        self.assertTrue(self.wait_for(lambda: view._image is not None))
        # The page is drawn: its rectangle in the window shows more than paper.
        page = tiles.sceneToView(controller.property("primaryContentRect")).toRect().intersected(
            QRect(0, 0, round(view.width()), round(view.height())))
        image = self.window.grabWindow()
        origin = view.mapToScene(QPointF(0, 0)).toPoint()
        colors = {image.pixel(origin.x() + x, origin.y() + y)
                  for x in range(page.left(), page.right(), 7) for y in range(page.top(), page.bottom(), 7)}
        self.assertGreater(len(colors), 5)
        # Back to the library: the view goes, the window stays (it used to be destroyed).
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.wait_for(lambda: not self.scene_views()))
        wait(300)
        self.assertTrue(shiboken6.isValid(self.window))

    def test_screen_share_through_main_qml(self):
        from liveview.rfb import FramebufferUpdate, Rect, ServerHello
        from liveview.session import Disconnected, RoomOffered
        client = self.app.singletons.screen_share

        def dialog(name):
            return next(o for o in self.window.findChildren(QObject) if o.metaObject().className().startswith(name))

        client._handle(RoomOffered("room", "tablet"))
        self.assertTrue(self.wait_for(lambda: dialog("ScreenShareAcceptDialog").property("visible")))
        client.acceptConnection()
        QMetaObject.invokeMethod(dialog("ScreenShareAcceptDialog"), "hide")
        client._handle(ServerHello(2, 4, 2))
        self.assertTrue(client.property("active"))
        self.assertTrue(self.wait_for(lambda: self.window.property("screenShareWindowObject") is not None))
        red = (0xF800).to_bytes(2, "little")
        client._handle(FramebufferUpdate(((Rect(0, 0, 4, 2), red * 8),)))
        self.assertEqual(client.image.pixelColor(1, 1).red(), 255)
        client._handle(Disconnected("Timeout"))
        self.assertFalse(client.property("active"))
        self.assertTrue(self.wait_for(lambda: dialog("ScreenShareDisconnectDialog").property("visible")))
        QMetaObject.invokeMethod(dialog("ScreenShareDisconnectDialog"), "hide")
        share = self.window.property("screenShareWindowObject")
        if share is not None:
            share.close()
        self.assertTrue(self.wait_for(lambda: not self.window.property("screenShareOn")))

    def test_sidebar_click_switches_view(self):
        self.window.resize(1270, 875)
        wait(200)
        from PySide6.QtTest import QTest
        model = self.entry_model()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(90, 244))
        wait(400)
        self.assertEqual(model.property("filter"), int(library.LibraryView.Filter["PDFs"]))
        self.assertTrue(set(self.names(model.property("entryIds"))) >= {"Research paper"})
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(90, 68))
        wait(400)
        self.assertEqual(model.property("filter"), int(library.LibraryView.Filter["Documents"]))


class WorkflowTests(unittest.TestCase):
    """What a user does, through the bridges the app's QML calls, over the fake cloud:
    documents imported from this computer open at once, pages turn, reading positions are
    kept, documents export (PlatformHelpers.exportFile), and library changes and tags."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = build_demo_cloud()
        # Uploaded by another client with no page list and no page count (a real
        # library has two such PDFs): the viewer counts the pages itself.
        cls.bare = demo_id("bare upload")
        cls.cloud.put_item(cls.bare, {"visibleName": "Bare upload", "parent": "", "type": "DocumentType",
                                      "lastModified": ms(3)}, {"fileType": "pdf"},
                           {f"{cls.bare}.pdf": make_pdf("Bare", [["one"], ["two"], ["three"]])})
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.singletons.platform_helpers.errorMessage.connect(cls.notices.append)
        cls.app.library.errorMessage.connect(cls.notices.append)
        cls.out = tmp / "exports"
        cls.out.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def tearDown(self):
        self.close_document()

    def wait_for(self, condition, ms=8000):
        for _ in range(ms // 100):
            if condition():
                return True
            wait(100)
        return condition()

    def qml(self, prefix: str) -> QObject:
        return next(o for o in self.window.findChildren(QObject) if o.metaObject().className().startswith(prefix))

    def scene_views(self) -> list:
        views, todo = [], [self.window.contentItem()]
        while todo:
            item = todo.pop()
            if isinstance(item, scene.SceneView) and item.isVisible():
                views.append(item)
            todo.extend(item.childItems())
        return views

    def import_file(self, path: Path) -> str:
        importer, done = self.app.singletons.importer, []
        importer.finished.connect(done.append)
        importer.failed.connect(done.append)
        try:
            importer.importFromUrls([QUrl.fromLocalFile(str(path))], "")
            self.assertTrue(self.wait_for(lambda: done))
        finally:
            importer.finished.disconnect(done.append)
            importer.failed.disconnect(done.append)
        return next(d.id for d in self.app.library.tree.documents() if d.name == path.stem)

    def open_document(self, doc_id: str, page_id: str | None = None):
        """DocumentView.open, as main.qml's onOpenEntry calls it: the page's SceneView once
        it has loaded (or failed to)."""
        entry = self.app.library.entryForId(doc_id)
        QMetaObject.invokeMethod(self.qml("DocumentView_"), "open", Q_ARG("QVariant", entry))

        def loaded():
            return next((v for v in self.scene_views() if v.property("status") == 2
                         and (page_id is None or v.property("pageId") == page_id)), None)
        self.assertTrue(self.wait_for(lambda: loaded() is not None),
                        [(v.property("pageId"), v.property("status")) for v in self.scene_views()])
        return loaded()

    def close_document(self):
        """Back to the library, as ActionManager does (documentView.hide())."""
        if self.qml("DocumentView_").property("visible"):
            QMetaObject.invokeMethod(self.qml("DocumentView_"), "hide")
            self.assertTrue(self.wait_for(lambda: not self.scene_views()))

    def export(self, target: Path, doc_id: str, kind: str = "ExportPdf", pages=None) -> bool:
        helpers = self.app.singletons.platform_helpers
        export_type = int(platform.PlatformHelpers.ExportType[kind])
        url = QUrl.fromLocalFile(str(target)).toString()
        if pages is None:
            return helpers.exportFile(url, doc_id, export_type)
        return helpers.exportFile(url, doc_id, export_type, "", False, pages)

    def wait_exported(self, *doc_ids: str) -> None:
        entries = [self.app.library.entryForId(i) for i in doc_ids]
        self.assertTrue(self.wait_for(lambda: not any(e.property("isExporting") for e in entries), 30000))

    def test_imported_pdf_opens_at_once(self):
        path = self.dir / "Field notes.pdf"
        path.write_bytes(make_pdf("Field notes", [["alpha " * 8] * 30, ["beta"] * 2, ["gamma"]]))
        doc_id = self.import_file(path)
        node = self.app.library.tree.get(doc_id)
        self.assertEqual(node.content.pages, [])                 # the tablet adds the page list later
        self.assertEqual(node.page_count, 3)                     # counted when it was imported
        self.assertTrue(self.source.sync.blobs.has(node.files[f"{doc_id}.pdf"]))   # no download to open it
        entry = self.app.library.entryForId(doc_id)
        self.assertEqual(entry.property("pageCount"), 3)
        self.assertEqual([entry.idForPage(i) for i in range(3)], ["pdf-page-0", "pdf-page-1", "pdf-page-2"])
        self.assertEqual(entry.pageForId("pdf-page-2"), 2)
        self.assertEqual(entry.property("redirectionPageMap"), [0, 1, 2])
        self.assertFalse(entry.isNotepage(0))

        view = self.open_document(doc_id, "pdf-page-0")
        self.assertEqual(view.property("pageStatus"), scene.PAGE_READY)
        self.assertFalse(self.qml("DocumentViewport").property("isLoading"))
        self.assertTrue(self.wait_for(lambda: view._image is not None))
        image = view._image
        dark = sum(QColor(image.pixel(x, y)).lightness() < 128
                   for x in range(0, image.width(), 3) for y in range(0, image.height(), 3))
        self.assertGreater(dark, 20)                             # the PDF's text, not blank paper
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "setCurrentPage", Q_ARG("QVariant", 2))
        view = next(v for v in self.scene_views() if v.property("pageId") == "pdf-page-2")
        self.assertTrue(self.wait_for(lambda: view.property("pageStatus") == scene.PAGE_READY))
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_pdf_without_page_count_takes_it_from_the_pdf(self):
        entry = self.app.library.entryForId(self.bare)
        self.assertEqual(entry.property("pageCount"), 0)
        self.open_document(self.bare)
        self.assertTrue(self.wait_for(lambda: entry.property("pageCount") == 3))
        # The viewport reopens the page through the id it has now.
        view = self.open_document(self.bare, "pdf-page-0")
        self.assertEqual(view.property("pageStatus"), scene.PAGE_READY)
        self.assertEqual(entry.property("subtext"), "Page 1 of 3")
        self.assertEqual(entry.idForPage(2), "pdf-page-2")

    def epub(self) -> str:
        """An EPUB imported from this computer: no PDF rendition until a tablet makes one."""
        import zipfile
        path = self.dir / "Pocket book.epub"
        if not path.exists():
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr("META-INF/container.xml", "<container/>")
            return self.import_file(path)
        return next(d.id for d in self.app.library.tree.documents() if d.name == "Pocket book")

    def test_epub_without_rendition_says_why_instead_of_loading(self):
        view = self.open_document(self.epub())
        self.assertEqual(view.property("pageStatus"), scene.PAGE_LOAD_ERROR)
        self.assertIn("no PDF rendition yet", view.property("pageStatusDetails"))
        self.assertFalse(self.qml("DocumentViewport").property("isLoading"))

    def test_export_imported_and_synced_documents(self):
        from pypdf import PdfReader
        path = self.dir / "Trip plan.pdf"
        path.write_bytes(make_pdf("Trip plan", [["day one"], ["day two"]]))
        imported, paper = self.import_file(path), demo_id("Research paper")
        entry = self.app.library.entryForId(imported)

        # One document: the file ExportDialog's save dialog chose (here without .pdf).
        self.assertTrue(self.export(self.out / "mine", imported))
        self.assertTrue(entry.property("isExporting"))
        self.assertEqual(entry.property("status"), int(library.EntryWrapper.Status.Exporting))
        self.wait_exported(imported)
        self.assertEqual(entry.property("status"), int(library.EntryWrapper.Status.Ok))
        self.assertEqual(len(PdfReader(self.out / "mine.pdf").pages), 2)
        self.assertIn(f"Exported to {self.out / 'mine.pdf'}", self.notices)

        # Several documents: the folder its folder dialog chose, one file each.
        folder = self.out / "batch"
        folder.mkdir()
        self.assertTrue(self.export(folder, imported))
        self.assertTrue(self.export(folder, paper))
        self.wait_exported(imported, paper)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Research paper.pdf", "Trip plan.pdf"])
        exported = PdfReader(folder / "Research paper.pdf")
        self.assertEqual(len(exported.pages), 3)                  # 2 PDF pages + the inserted note page
        self.assertIn("/RMLayer", exported.pages[0]["/Resources"]["/XObject"])   # its ink, over the PDF page
        self.assertTrue(self.export(folder, paper))               # again: next to the first, not over it
        self.wait_exported(paper)
        self.assertTrue((folder / "Research paper 2.pdf").exists())

        # Pages picked in the page overview, in page order.
        self.assertTrue(self.export(self.out / "picked.pdf", paper, pages=[2, 0]))
        self.wait_exported(paper)
        picked = PdfReader(self.out / "picked.pdf")
        self.assertEqual([round(float(p.mediabox.height)) for p in picked.pages],
                         [round(float(exported.pages[i].mediabox.height)) for i in (0, 2)])

        # Export as rM file: a .rmdoc in the chosen folder.
        import zipfile
        self.assertTrue(self.export(self.out, imported, "ExportRmBundle"))
        self.wait_exported(imported)
        with zipfile.ZipFile(self.out / "Trip plan.rmdoc") as zf:
            self.assertIn(f"{imported}.pdf", zf.namelist())

    def test_export_problems_are_reported(self):
        notices = len(self.notices)
        self.assertFalse(self.export(self.out / "no-such-folder" / "x.pdf", demo_id("Sketchbook")))
        self.assertFalse(self.export(self.out, demo_id("Sketchbook"), "ExportRmHtml"))
        self.assertIn("no such folder", self.notices[notices])
        self.assertEqual(self.notices[notices + 1], "This export format is not available on Linux")
        epub = self.epub()                                         # nothing to draw yet: fails in the worker
        self.assertTrue(self.export(self.out / "epub.pdf", epub))
        self.wait_exported(epub)
        self.assertIn('Unable to export "Pocket book": ValueError: document has no pages', self.notices)
        self.assertEqual(self.app.library.entryForId(epub).property("status"), int(library.EntryWrapper.Status.Ok))
        self.assertFalse((self.out / "epub.pdf").exists())

    def scale(self) -> float:
        view = next(v for v in self.scene_views() if v.property("status") == 2)
        return round(view._get("tileManager")._get("scale"), 4)

    def turn_to(self, page: int, page_id: str):
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "setCurrentPage", Q_ARG("QVariant", page))
        self.assertTrue(self.wait_for(lambda: any(v.property("pageId") == page_id and v.property("status") == 2
                                                  for v in self.scene_views())))
        wait(200)

    def test_turning_pages_keeps_them_fitted(self):
        # Notebook pages all have the same paper, so DocumentViewport never re-fits the
        # zoom: page 2 used to open at 100 % (scale 1), much larger than the window.
        notes = self.app.library.entryForId(demo_id("Meeting notes"))
        self.open_document(notes.property("id"))
        first = self.scale()
        self.assertLess(first, 0.9)
        self.turn_to(1, notes.idForPage(1))
        self.assertEqual(self.scale(), first)
        self.close_document()
        # An inserted note page in a PDF fits like a notebook page; the PDF pages around it keep theirs.
        paper = self.app.library.entryForId(demo_id("Research paper"))
        self.open_document(paper.property("id"))
        pdf_scale = self.scale()
        self.turn_to(1, paper.idForPage(1))
        self.assertEqual(self.scale(), first)
        self.turn_to(2, paper.idForPage(2))
        self.assertEqual(self.scale(), pdf_scale)

    def test_sync_does_not_reload_the_open_page(self):
        self.open_document(demo_id("Sketchbook"))
        view = self.scene_views()[0]
        self.app.syncer.refresh()
        self.assertTrue(self.wait_for(lambda: not self.app.syncer.running))
        wait(300)
        self.assertEqual(self.scene_views(), [view])

    def cloud_metadata(self, doc_id: str) -> dict:
        """The document's metadata as a fresh client reads it from the (fake) cloud."""
        fresh = SyncClient(paired_auth(self.dir / f"auth-{len(self.notices)}", self.cloud),
                           Path(tempfile.mkdtemp(dir=self.dir)))
        return fresh.refresh().get(doc_id).metadata.raw

    def test_reading_position_is_kept_and_saved(self):
        doc_id = demo_id("Research paper")
        entry = self.app.library.entryForId(doc_id)
        before = self.cloud_metadata(doc_id)
        self.open_document(doc_id)
        self.turn_to(2, entry.idForPage(2))
        self.close_document()
        # At once: the entry and the next open start where the reader left.
        self.assertEqual(entry.property("lastOpenedPage"), 2)
        self.assertEqual(entry.property("subtext"), "Page 3 of 3")
        view = self.open_document(doc_id, entry.idForPage(2))
        self.assertEqual(self.qml("DocumentViewport").property("currentPage"), 2)
        self.close_document()
        # Then in the cloud, without touching lastModified (reading is not editing).
        self.assertTrue(self.wait_for(lambda: self.cloud_metadata(doc_id)["lastOpenedPage"] == 2, 10000))
        # The commit's result reaches the library on the GUI thread a moment after the cloud has it.
        self.assertTrue(self.wait_for(lambda: self.app.library.reading_position(doc_id) == {}))
        after = self.cloud_metadata(doc_id)
        self.assertEqual(after["lastModified"], before["lastModified"])
        self.assertGreater(int(after["lastOpened"]), int(before["lastOpened"]))
        self.assertEqual(entry.property("lastOpenedPage"), 2)
        del view

    def test_tags_view_opens_a_tagged_page(self):
        # TagsViewSelectionBehavior: setLastOpenedPage(document, page), then open it.
        doc_id = demo_id("Meeting notes")
        entry = self.app.library.entryForId(doc_id)
        self.app.singletons.controller.setLastOpenedPage(doc_id, 1)
        self.open_document(doc_id, entry.idForPage(1))
        self.assertEqual(self.qml("DocumentViewport").property("currentPage"), 1)

    def test_background_saves_fail_quietly(self):
        from ui.workers import Mutator
        offline = Mutator(LibrarySource(SyncClient(paired_auth(self.dir / "quiet", self.cloud), self.dir / "q"),
                                        cached_only=True))
        failures, done = [], []
        offline.failed.connect(failures.append)
        offline.run("Saving the reading position", lambda sync, _: None, "", done.append, quiet=True)
        self.assertEqual((failures, done), ([], [False]))
        offline.run("Renaming", lambda sync, _: None, "", done.append)
        self.assertEqual(failures, ["Renaming: not possible offline"])

    def test_library_changes_and_tags(self):
        lib, controller = self.app.library, self.app.singletons.controller
        # Tags set on the tablet: listed, and the library filters by them.
        self.assertTrue({"work", "reading"} <= set(lib.tagLabels(True)))
        view = library.LibraryNavigator()
        view.setLibrary(lib)
        view.setFilter(int(library.LibraryView.Filter["Documents"]))
        view.setIgnoreParent(True)
        view.setFilterTags(["work"])
        view.updateNow()
        self.assertEqual(sorted(lib.tree.get(i).name for i in view.property("entryIds")),
                         ["Meeting notes", "Research paper"])
        view.setFilterTags(["work", "reading"])                   # documents with all of them
        view.updateNow()
        self.assertEqual([lib.tree.get(i).name for i in view.property("entryIds")], ["Research paper"])
        # LibraryController's changes are cloud commits that come back into the library.
        sketch, personal = demo_id("Sketchbook"), demo_id("Personal")
        node = lambda: lib.tree.get(sketch)                       # noqa: E731
        for change, done in ((lambda: controller.setVisibleName(sketch, "Sketches"), lambda: node().name == "Sketches"),
                             (lambda: controller.togglePinned([sketch]), lambda: not node().pinned),   # it was a favorite
                             (lambda: controller.moveEntries([sketch], personal), lambda: node().parent_id == personal),
                             (lambda: controller.moveEntriesToTrash([sketch]), lambda: lib.tree.is_trashed(sketch)),
                             (lambda: controller.restoreEntriesFromTrash([sketch]), lambda: node().parent_id == ROOT_ID)):
            change()
            self.assertTrue(self.wait_for(done))
        cloud = self.cloud_metadata(sketch)
        self.assertEqual((cloud["visibleName"], cloud["pinned"], cloud["parent"]), ("Sketches", False, ROOT_ID))

    def test_unavailable_changes_say_so(self):
        controller, notices = self.app.singletons.controller, len(self.notices)
        controller.cloneEntry(demo_id("Personal"), "")                 # Copy, then Paste of a folder
        self.assertEqual(self.notices[notices:], ["Only documents can be copied"])

    def test_paste_from_qml_copies_the_document(self):
        # The exact call NavigatorSelection.paste() makes after Copy: two arguments, from QML.
        from PySide6.QtQml import QQmlComponent
        lib, notices = self.app.library, len(self.notices)
        source = lib.tree.get(demo_id("Sketchbook"))
        before = set(lib.tree.nodes)
        component = QQmlComponent(self.app.engine)
        component.setData(b"""
            import QtQuick
            import xofm.libs.library
            QtObject {
                property string source
                property string target
                function paste() { LibraryController.cloneEntry(source, target); }
            }""", QUrl())
        selection = component.create()
        self.assertIsNotNone(selection, component.errorString())
        selection.setProperty("source", source.id)
        selection.setProperty("target", "")
        QMetaObject.invokeMethod(selection, "paste")
        self.assertTrue(self.wait_for(lambda: set(lib.tree.nodes) - before), self.notices[notices:])
        [copy_id] = set(lib.tree.nodes) - before
        self.assertTrue(lib.tree.get(copy_id).name.startswith(f"{source.name} copy"))
        self.assertEqual(self.notices[notices:], [])
        selection.deleteLater()

    def test_a_copy_of_a_copy_from_qml(self):
        # A -> B, then B (a copy, never opened) -> C, each through NavigatorSelection.paste()'s call.
        from PySide6.QtQml import QQmlComponent
        lib, notices = self.app.library, len(self.notices)
        component = QQmlComponent(self.app.engine)
        component.setData(b"""
            import QtQuick
            import xofm.libs.library
            QtObject {
                property string source
                function paste() { LibraryController.cloneEntry(source, ""); }
            }""", QUrl())
        selection = component.create()
        self.assertIsNotNone(selection, component.errorString())

        def paste(source_id):
            before = set(lib.tree.nodes)
            selection.setProperty("source", source_id)
            QMetaObject.invokeMethod(selection, "paste")
            self.assertTrue(self.wait_for(lambda: set(lib.tree.nodes) - before), self.notices[notices:])
            [new] = set(lib.tree.nodes) - before
            return lib.tree.get(new)

        a = lib.tree.get(demo_id("Sketchbook"))
        b = paste(a.id)
        c = paste(b.id)
        self.assertTrue(b.name.startswith(f"{a.name} copy"))
        self.assertTrue(c.name.startswith(f"{b.name} copy"))
        self.assertEqual(c.page_count, a.page_count)
        self.assertEqual(self.notices[notices:], [])
        selection.deleteLater()

    def test_the_tag_dialog_tags_documents_and_pages(self):
        # TagSelectionPopup.onAccepted -> LibraryController.setEntryTags / setPageTags.
        controller, lib = self.app.singletons.controller, self.app.library
        doc_id = demo_id("Sketchbook")
        entry = lib.entryForId(doc_id)
        controller.setEntryTags(doc_id, ["work", "ideas"], [])
        self.assertTrue(self.wait_for(lambda: sorted(entry.property("tags")) == ["ideas", "work"]))
        self.assertIn("work", lib.tagLabels(True))
        controller.setPageTags(doc_id, [0], ["todo"], [])
        self.assertTrue(self.wait_for(lambda: entry.tagsForPage(0) == ["todo"]))
        self.assertIn(doc_id, [r["documentId"] for r in lib.taggedPages()])
        controller.setEntryTags(doc_id, ["ideas"], [])                 # "work" unticked
        self.assertTrue(self.wait_for(lambda: entry.property("tags") == ["ideas"]))
        fresh = SyncClient(paired_auth(self.dir / "auth-tags", self.cloud), Path(tempfile.mkdtemp(dir=self.dir)))
        content = fresh.refresh().get(doc_id).content
        self.assertEqual((content.tags, [t["name"] for t in content.raw["pageTags"]]), (["ideas"], ["todo"]))
        # The same calls from QML, with JS arrays as TagSelectionPopup passes them.
        from PySide6.QtQml import QQmlComponent
        component = QQmlComponent(self.app.engine)
        component.setData(b"""
            import QtQuick
            import xofm.libs.library
            QtObject {
                property string doc
                function clear() {
                    LibraryController.setEntryTags(doc, [], ["ideas"]);
                    LibraryController.setPageTags(doc, [0], [], []);
                }
            }""", QUrl())
        dialog = component.create()
        self.assertIsNotNone(dialog, component.errorString())
        dialog.setProperty("doc", doc_id)
        QMetaObject.invokeMethod(dialog, "clear")
        self.assertTrue(self.wait_for(lambda: entry.property("tags") == ["ideas"] and not entry.tagsForPage(0)))
        dialog.deleteLater()

    def test_copy_then_paste_makes_a_named_copy(self):
        # NavigatorSelection.paste after Copy: LibraryController.cloneEntry(id, folder).
        controller, lib = self.app.singletons.controller, self.app.library
        source = lib.tree.get(demo_id("Sketchbook"))
        added = []
        lib.entryAdded.connect(added.append)
        from core.models import unique_name
        taken = lambda: {n.name for n in lib.tree.nodes.values()}                  # noqa: E731
        first = unique_name(f"{source.name} copy", taken())                       # other tests may have copied it
        copy_id = controller.cloneEntry(source.id, demo_id("Personal"))
        self.assertTrue(self.wait_for(lambda: lib.tree.get(copy_id) is not None))
        copy = lib.tree.get(copy_id)
        self.assertEqual((copy.name, copy.parent_id), (first, demo_id("Personal")))
        self.assertEqual(copy.page_count, source.page_count)
        self.assertEqual(added, [copy_id])
        self.assertEqual(self.cloud_metadata(copy_id)["visibleName"], first)
        # Pasted again: the name is made unique, as uniqueVisibleName does.
        second = unique_name(f"{source.name} copy", taken())
        self.assertNotEqual(second, first)
        again = controller.cloneEntry(source.id, demo_id("Personal"))
        self.assertTrue(self.wait_for(lambda: lib.tree.get(again) is not None))
        self.assertEqual(lib.tree.get(again).name, second)

    def test_export_png(self):
        paper = demo_id("Research paper")
        folder = self.out / "images"
        folder.mkdir()
        self.assertTrue(self.export(folder, paper, "ExportPng"))
        self.wait_exported(paper)
        pngs = sorted((folder / "Research paper").iterdir())
        self.assertEqual([p.name for p in pngs], [f"Research paper - {n}.png" for n in (1, 2, 3)])
        first, note = QImage(str(pngs[0])), QImage(str(pngs[1]))
        self.assertEqual(first.width(), round(595 * 226 / 72))          # the A4 PDF page at 226 DPI
        self.assertEqual((note.width(), note.height()), (1404, 1872))  # the inserted note page
        self.assertTrue(self.export(folder, paper, "ExportPng", pages=[2]))   # picked pages; a new folder
        self.wait_exported(paper)
        self.assertEqual([p.name for p in (folder / "Research paper 2").iterdir()], ["Research paper - 3.png"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Research paper", "Research paper 2"])

    def test_export_svg(self):
        import xml.etree.ElementTree as ET
        paper, notes = demo_id("Research paper"), demo_id("Meeting notes")
        folder = self.out / "vector"
        folder.mkdir()
        entry = self.app.library.entryForId(paper)
        self.assertTrue(self.export(folder, paper, "ExportSvg"))
        self.assertTrue(entry.property("isExporting"))
        self.wait_exported(paper)
        self.assertEqual(entry.property("status"), int(library.EntryWrapper.Status.Ok))
        self.assertIn(f"Exported to {folder / 'Research paper'}", self.notices)
        files = sorted((folder / "Research paper").iterdir())
        self.assertEqual([f.name for f in files], [f"Research paper - {n}.svg" for n in (1, 2, 3)])
        svg = "{http://www.w3.org/2000/svg}"
        roots = [ET.parse(f).getroot() for f in files]
        self.assertEqual([r.find(f"{svg}image") is not None for r in roots], [True, False, True])   # PDF pages
        self.assertEqual(roots[1].get("viewBox"), "0 0 1404 1872")                                   # the note page
        # A notebook, picked pages: vector only, one file per page.
        self.assertTrue(self.export(folder, notes, "ExportSvg", pages=[0]))
        self.wait_exported(notes)
        only = list((folder / "Meeting notes").iterdir())
        self.assertEqual([f.name for f in only], ["Meeting notes - 1.svg"])
        self.assertLess(only[0].stat().st_size, 100_000)

    def test_abort_export(self):
        import threading
        from ui.workers import _Job
        exporter = self.app.exporter
        sketch, notes = demo_id("Sketchbook"), demo_id("Meeting notes")
        gate = threading.Event()
        exporter.worker.pool.start(_Job(lambda: gate.wait(10)))   # hold the export thread
        try:
            self.assertTrue(self.export(self.out / "running.pdf", sketch))
            self.assertTrue(self.export(self.out / "queued.pdf", notes))
            entries = [self.app.library.entryForId(i) for i in (sketch, notes)]
            self.assertTrue(all(e.property("isExporting") for e in entries))
            entries[1].abortExport()                              # queued: dropped
            self.assertFalse(entries[1].property("isExporting"))
            entries[0].abortExport()                              # running: stops at its next step
        finally:
            gate.set()
        self.wait_exported(sketch, notes)
        wait(200)
        self.assertEqual(list(self.out.glob("running.pdf*")) + list(self.out.glob("queued.pdf*")), [])
        self.assertEqual(entries[0].property("status"), int(library.EntryWrapper.Status.Ok))
        self.assertTrue(self.export(self.out / "after.pdf", sketch))   # the exporter still works
        self.wait_exported(sketch)
        self.assertTrue((self.out / "after.pdf").exists())


class EditingTests(unittest.TestCase):
    """Editing, through the app's own QML: its editing interface is on only in the notebook
    the library lets this client edit, and an image file dropped there (DocumentViewport's
    drop area -> SceneController.insertImageFileAsSceneItem) is put on the page here,
    without anything being uploaded."""

    wait_for, qml, scene_views = WorkflowTests.wait_for, WorkflowTests.qml, WorkflowTests.scene_views
    open_document, close_document = WorkflowTests.open_document, WorkflowTests.close_document

    @classmethod
    def setUpClass(cls):
        from test_edits_offline import OTHER, PAGE1, THROWAWAY, build_cloud, png
        cls.THROWAWAY, cls.OTHER, cls.PAGE1 = THROWAWAY, OTHER, PAGE1
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway",))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)
        cls.picture = tmp / "picture.png"
        cls.picture.write_bytes(png(200, 100, color="#d02020"))

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
        self.close_document()
        self.source.sync.edits.clear()

    def editing(self) -> bool:
        return self.app.singletons.user_info.property("featureToggles")["clientEdit"]

    def drop_area(self) -> QObject:
        return next(o for o in self.window.findChildren(QObject)
                    if o.metaObject().indexOfMethod("dropSupportedImages(QVariant,QVariant)") >= 0)

    def pending(self) -> list:
        return self.source.sync.edits.all()

    def test_the_editing_interface_is_on_only_in_the_notebook_that_may_be_edited(self):
        self.assertFalse(self.editing())
        self.open_document(self.OTHER)
        self.assertFalse(self.editing())
        self.assertFalse(self.drop_area().property("enabled"))
        self.close_document()
        self.open_document(self.THROWAWAY)
        self.assertTrue(self.editing())
        self.assertTrue(self.wait_for(lambda: self.drop_area().property("enabled")))
        wait(300)
        self.close_document()
        self.assertFalse(self.editing())
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_a_copy_of_the_editable_notebook_may_be_edited_too(self):
        controller = self.app.singletons.controller
        copy_id = controller.cloneEntry(self.THROWAWAY, "")
        self.assertTrue(self.wait_for(lambda: self.source.sync.tree.get(copy_id) is not None), self.notices)
        copy = self.source.sync.tree.get(copy_id)
        self.assertEqual(copy.name, "throwaway copy")
        self.assertTrue(self.source.can_edit(copy))
        self.open_document(copy_id)
        self.assertTrue(self.wait_for(self.editing))                    # the bridge's edit interface is on
        self.close_document()
        # A copy of the copy inherits it; a copy of another notebook does not.
        again = controller.cloneEntry(copy_id, "")
        other = controller.cloneEntry(self.OTHER, "")
        self.assertTrue(self.wait_for(lambda: self.source.sync.tree.get(again) is not None
                                      and self.source.sync.tree.get(other) is not None), self.notices)
        self.assertTrue(self.source.can_edit(self.source.sync.tree.get(again)))
        self.assertFalse(self.source.can_edit(self.source.sync.tree.get(other)))
        # Still so after a restart: kept with the cache.
        restarted = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                  editable=("throwaway",))
        restarted.refresh()
        self.assertTrue(restarted.can_edit(restarted.sync.tree.get(copy_id)))
        self.assertTrue(restarted.can_edit(restarted.sync.tree.get(again)))
        self.assertFalse(restarted.can_edit(restarted.sync.tree.get(other)))
        # Editing turned off: no copy may be edited either.
        off = LibrarySource(restarted.sync, editable=())
        self.assertEqual(off.edit_refusal(restarted.sync.tree.get(copy_id)), "Editing is not turned on")
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_an_image_dropped_on_the_page_is_put_there_and_stays_on_this_computer(self):
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        self.assertTrue(self.wait_for(lambda: view._image is not None))
        files = dict(self.cloud.item(self.THROWAWAY))
        point = QPointF(view.width() / 2 + 40, 300)
        target = view._tiles.viewToScene(point)
        QMetaObject.invokeMethod(self.drop_area(), "dropSupportedImages",
                                 Q_ARG("QVariant", [QUrl.fromLocalFile(str(self.picture))]), Q_ARG("QVariant", point))
        self.assertTrue(self.wait_for(self.pending), self.notices)
        [edit] = self.pending()
        self.assertEqual((edit.doc_id, edit.page_id, edit.file_name[-4:]), (self.THROWAWAY, self.PAGE1, ".png"))
        xs, ys = [v[0] for v in edit.vertices], [v[1] for v in edit.vertices]
        self.assertAlmostEqual((min(xs) + max(xs)) / 2, target.x(), places=3)   # centred where it was dropped
        self.assertAlmostEqual((min(ys) + max(ys)) / 2, target.y(), places=3)
        self.assertEqual((max(xs) - min(xs), max(ys) - min(ys)), (200, 100))    # a scene unit a pixel
        # The page is drawn again with the picture on it.
        self.assertTrue(self.wait_for(lambda: view._content is not None and view._content.scene is not None
                                      and len(view._content.scene.placed_images) == 1))
        # It is selected (drawn in the selection box): once the selection ends, the page has it.
        self.assertTrue(self.wait_for(lambda: self.handler().property("visible")))
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "endItemSelection")
        self.assertTrue(self.wait_for(lambda: view._selected is None))

        def red_where_dropped() -> bool:
            image, spot = view._image, view._tiles.sceneToView(target)
            box = view._tiles.sceneToView(view._bounds)
            x = round((spot.x() - box.x()) * image.width() / box.width())
            y = round((spot.y() - box.y()) * image.height() / box.height())
            color = QColor(image.pixel(x, y))
            return color.red() > 180 and color.green() < 80
        self.assertTrue(self.wait_for(red_where_dropped))
        self.assertEqual(view.property("status"), 2)
        self.assertEqual(self.cloud.item(self.THROWAWAY), files)                # the cloud's copy is untouched
        self.assertEqual(self.notices, [])
        self.assertEqual(real_warnings(self.app.warnings), [])

    def handler(self) -> QObject:
        return next(o for o in self.window.findChildren(QObject)
                    if o.metaObject().indexOfMethod("moveSelectedItems(QVariant,QVariant)") >= 0
                    and o.metaObject().indexOfProperty("sceneSelectionRect") >= 0)

    def test_a_dropped_image_is_selected_then_moves_resizes_and_deletes(self):
        from core.edits import ImageDelete, ImageInsert, ImageTransform
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        viewport = self.qml("DocumentViewport")
        point = QPointF(view.width() / 2, 300)
        QMetaObject.invokeMethod(self.drop_area(), "dropSupportedImages",
                                 Q_ARG("QVariant", [QUrl.fromLocalFile(str(self.picture))]), Q_ARG("QVariant", point))
        handler = self.handler()
        # Dropped: selected at once, the app's selection box on it.
        self.assertTrue(self.wait_for(lambda: handler.property("visible")), self.notices)
        [insert] = self.pending()
        rect = handler.property("sceneSelectionRect")
        xs = [v[0] for v in insert.vertices]
        self.assertAlmostEqual(rect.x(), min(xs), places=2)
        self.assertEqual((rect.width(), rect.height()), (200, 100))
        self.assertTrue(view._controller.property("selectionContainsImage"))
        self.assertTrue(self.wait_for(lambda: view.selected_image() is not None))
        # Dragging the box moves the image (SceneSelectionHandler.moveSelectedItems).
        QMetaObject.invokeMethod(handler, "moveSelectedItems", Q_ARG("QVariant", 30), Q_ARG("QVariant", 40))
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 2))
        move = self.pending()[1]
        self.assertIsInstance(move, ImageTransform)
        self.assertEqual(move.key, insert.key)
        self.assertEqual(move.vertices[0][:2], (insert.vertices[0][0] + 30, insert.vertices[0][1] + 40))
        self.assertTrue(self.wait_for(lambda: handler.property("sceneSelectionRect").x() == rect.x() + 30))
        # A corner handle scales about the opposite corner.
        view._controller.scaleSelectedItems(view._controller.property("currentLayer"),
                                            QPointF(min(xs) + 30, rect.y() + 40), 2.0, 2.0)
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 3))
        self.assertTrue(self.wait_for(lambda: handler.property("sceneSelectionRect").width() == 400))
        # Clicking elsewhere ends the selection; clicking the image selects it again.
        QMetaObject.invokeMethod(viewport, "endItemSelection")
        self.assertTrue(self.wait_for(lambda: not handler.property("visible")))
        self.assertIsNone(view._selected)
        inside = QPointF(min(xs) + 30 + 50, rect.y() + 40 + 50)
        self.assertTrue(view._controller.selectImageAtPoint(inside, view._controller.property("currentLayer")))
        viewport.setProperty("selectionMode", True)
        self.assertTrue(self.wait_for(lambda: handler.property("visible")))
        self.assertFalse(view._controller.selectImageAtPoint(QPointF(600, 1800), 0))
        # Backspace deletes it.
        view.processKey(int(Qt.Key.Key_Backspace.value), 0, False, "")
        self.assertTrue(self.wait_for(lambda: isinstance(self.pending()[-1], ImageDelete)))
        self.assertTrue(self.wait_for(lambda: not handler.property("visible")))
        self.assertTrue(self.wait_for(lambda: view._content.scene is not None
                                      and len(view._content.scene.placed_images) == 0))
        self.assertEqual([type(e) for e in self.pending()], [ImageInsert, ImageTransform, ImageTransform, ImageDelete])
        self.assertEqual(self.notices, [])
        self.assertEqual(real_warnings(self.app.warnings), [])

    def action(self, text: str) -> QObject:
        """One of the app's ActionManager actions, by its text (Ctrl+Z is "Undo", Ctrl+Shift+Z "Redo")."""
        return next(o for o in self.qml("ActionManager").findChildren(QObject)
                    if o.metaObject().className().startswith("ProxyAction") and o.property("text") == text)

    def test_undo_and_redo_through_the_apps_actions(self):
        from core.edits import ImageDelete, ImageInsert, ImageRestore
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        controller = view._controller
        self.assertTrue(self.wait_for(lambda: view.property("status") == 2))
        self.assertEqual((controller.property("undoAvailable"), controller.property("redoAvailable")), (False, False))
        undo, redo = self.action("Undo"), self.action("Redo")
        # Actions go to the item with the focus (main.qml actionHandler): the viewport, as after a click.
        self.window.requestActivate()
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "forceActiveFocus")
        self.assertTrue(self.wait_for(lambda: undo.property("enabled") is False))
        self.drop_and_select(view)
        self.assertTrue(self.wait_for(lambda: controller.property("undoAvailable")))
        self.assertTrue(self.wait_for(lambda: undo.property("enabled")))
        # Undo (the viewport ends the selection first, then SceneController.undo).
        QMetaObject.invokeMethod(undo, "trigger")
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 2), self.notices)
        self.assertTrue(self.wait_for(lambda: view._content.scene is not None
                                      and len(view._content.scene.placed_images) == 0))
        self.assertFalse(self.handler().property("visible"))
        self.assertTrue(self.wait_for(lambda: controller.property("redoAvailable")
                                      and not controller.property("undoAvailable")))
        self.assertTrue(self.wait_for(lambda: redo.property("enabled")))
        QMetaObject.invokeMethod(redo, "trigger")
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 3), self.notices)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.placed_images) == 1))
        self.assertEqual([type(e) for e in self.pending()], [ImageInsert, ImageDelete, ImageRestore])
        self.assertTrue(self.wait_for(lambda: controller.property("undoAvailable")
                                      and not controller.property("redoAvailable")))
        # Closing the document drops the stacks, as the app's go with its pages.
        self.close_document()
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        self.assertTrue(self.wait_for(lambda: view.property("status") == 2))
        self.assertFalse(view._controller.property("undoAvailable"))
        self.assertEqual(self.notices, [])
        self.assertEqual(real_warnings(self.app.warnings), [])

    def drop_and_select(self, view):
        QMetaObject.invokeMethod(self.drop_area(), "dropSupportedImages",
                                 Q_ARG("QVariant", [QUrl.fromLocalFile(str(self.picture))]),
                                 Q_ARG("QVariant", QPointF(view.width() / 2, 300)))
        self.assertTrue(self.wait_for(lambda: self.handler().property("visible")), self.notices)

    def copy(self, view):
        """DocumentViewport's copy action: Clipboard.items = cloneSelectedItems(...)."""
        clipboard = self.app.singletons.clipboard
        viewport = self.qml("DocumentViewport")
        clipboard.setProperty("items", view._controller.cloneSelectedItems(
            view._controller.property("currentLayer"), viewport.property("defaultScale")))
        return clipboard

    def test_copy_then_paste_adds_a_new_image_selected(self):
        from core.edits import ImageInsert
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        self.drop_and_select(view)
        [original] = self.pending()
        clipboard = self.copy(view)
        self.assertTrue(clipboard.property("hasItems"))
        self.assertEqual(clipboard.property("lastCopiedType"), 1)                # Items
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "pasteLines")
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 2), self.notices)
        pasted = self.pending()[1]
        self.assertIsInstance(pasted, ImageInsert)
        self.assertNotEqual((pasted.file_name, pasted.key), (original.file_name, original.key))
        self.assertEqual(pasted.sha256, original.sha256)          # the same picture, stored again
        self.assertIsNotNone(pasted.timestamps)                   # the copied item's two timestamps
        size = lambda e: (max(v[0] for v in e.vertices) - min(v[0] for v in e.vertices),
                          max(v[1] for v in e.vertices) - min(v[1] for v in e.vertices))
        for got, want in zip(size(pasted), size(original)):
            self.assertAlmostEqual(got, want, places=6)
        self.assertTrue(self.wait_for(lambda: view._selected == bytes.fromhex(pasted.key)))
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.placed_images) == 2))
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_cut_deletes_and_paste_brings_it_back(self):
        from core.edits import ImageDelete, ImageInsert
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        self.drop_and_select(view)
        self.copy(view)
        view._controller.deleteSelectedItems(view._controller.property("currentLayer"))
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "endItemSelection")
        self.assertTrue(self.wait_for(lambda: isinstance(self.pending()[-1], ImageDelete)))
        self.assertTrue(self.wait_for(lambda: view._content.scene is not None
                                      and not view._content.scene.placed_images))
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "pasteLines")
        self.assertTrue(self.wait_for(lambda: isinstance(self.pending()[-1], ImageInsert)
                                      and len(self.pending()) == 3), self.notices)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.placed_images) == 1))

    def test_a_picture_copied_in_another_program_pastes_as_a_floating_image(self):
        from PySide6.QtCore import QMimeData
        from core.edits import ImageInsert

        class System(QObject):
            dataChanged = Signal()

            def __init__(self, mime):
                super().__init__()
                self.mime = mime

            def ownsClipboard(self):
                return False

            def mimeData(self):
                return self.mime

            def image(self):
                return self.mime.imageData()

        picture = QImage(120, 80, QImage.Format.Format_RGB32)
        picture.fill(QColor("#1060d0"))
        mime = QMimeData()
        mime.setImageData(picture)
        clipboard = self.app.singletons.clipboard
        system, clipboard._system = clipboard._system, System(mime)
        try:
            clipboard.systemClipboardDataChanged()
        finally:
            clipboard._system = system
        content = clipboard.property("text")
        self.assertTrue(clipboard.property("hasText"))
        self.assertEqual((content["text"], content["hasImage"]), ("", True))
        self.assertEqual(clipboard.property("lastCopiedType"), 0)                # Text
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        self.assertFalse(view._controller.shouldPasteClipboardContentInTextMode(content))
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "pasteClipboardData", Q_ARG("QVariant", 0))
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 1), self.notices)
        [pasted] = self.pending()
        self.assertIsInstance(pasted, ImageInsert)
        self.assertEqual((pasted.file_name[-4:], pasted.timestamps), (".jpg", None))     # opaque: JPEG
        xs = [v[0] for v in pasted.vertices]
        self.assertEqual(max(xs) - min(xs), 120)
        self.assertTrue(self.wait_for(lambda: self.handler().property("visible")))
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_page_actions_reach_the_document_controller_from_qml(self):
        # The calls PageActions.qml / DocumentViewport.qml make, from QML: an arrow
        # function as the callback, JS arrays of page numbers.
        from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject
        uploads = len(self.cloud.puts())
        from PySide6.QtQml import QQmlComponent
        self.open_document(self.THROWAWAY)
        component = QQmlComponent(self.app.engine)
        component.setData(b"""
            import QtQuick
            import com.remarkable
            QtObject {
                property int calls: 0
                function add(id) { return DocumentController.addPageWithTemplateAndPaperSizeFromPage(id, 2, "Blank", 1, () => { calls += 1; }); }
                function addHere(id) { return DocumentController.addPageWithTemplateAndPageSize(id, 1, "Blank", Qt.size(1404, 1872), () => { calls += 1; }); }
                function duplicate(id) { return DocumentController.copyPages(id, [0], id, 1); }
                function move(id) { return DocumentController.movePages(id, [3], id, 0, () => { calls += 1; }); }
                function remove(id) { return DocumentController.deletePages(id, [0]); }
            }""", QUrl())
        actions = component.create()
        self.assertIsNotNone(actions, component.errorString())
        document = self.app.library.entryForId(self.THROWAWAY)

        def call(name):
            return QMetaObject.invokeMethod(actions, name, Q_RETURN_ARG("QVariant"), Q_ARG("QVariant", self.THROWAWAY))

        self.assertTrue(call("add"))
        self.assertTrue(call("addHere"))
        self.assertTrue(call("duplicate"))
        self.assertEqual(document.property("pageCount"), 5)
        added = document.idForPage(3)
        self.assertTrue(call("move"))
        self.assertEqual(document.idForPage(0), added)
        self.assertTrue(call("remove"))
        self.assertEqual(document.property("pageCount"), 4)
        self.assertTrue(self.wait_for(lambda: actions.property("calls") == 3))
        self.assertEqual([type(e).__name__ for e in self.pending()],
                         ["PageAdd", "PageAdd", "PageCopy", "PageMove", "PageDelete"])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.cloud.puts()[uploads:], [])                       # nothing uploaded
        # The page shown is still there and still loads.
        self.assertTrue(self.wait_for(lambda: any(v.property("status") == 2 for v in self.scene_views())))
        actions.deleteLater()

    def test_page_copy_and_paste_with_the_keyboard_duplicates_the_page(self):
        # Page overview: click a page, Ctrl+C (PageActions.copyPageAction), Ctrl+V
        # (moveTo -> DocumentController.copyPages(doc, pages, doc, index)): the same document.
        from PySide6.QtTest import QTest
        self.open_document(self.THROWAWAY)
        click = lambda x, y: QTest.mouseClick(self.window, Qt.MouseButton.LeftButton,      # noqa: E731
                                              Qt.KeyboardModifier.NoModifier, QPoint(x, y))
        if self.app.singletons.user_info.property("featureToggles")["clientEdit"]:
            click(842, 143)                              # skip the editing introduction, if shown
            wait(500)
        self.window.requestActivate()
        click(160, 500)                                  # focus the document
        wait(300)
        click(1082, 65)                                  # page overview
        wait(1500)
        click(100, 350)                                  # the first page
        wait(500)
        QTest.keyClick(self.window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        wait(300)
        QTest.keyClick(self.window, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: bool(self.pending())), self.notices)
        [copy] = self.pending()
        self.assertEqual((type(copy).__name__, copy.doc_id, copy.sources, copy.index),
                         ("PageCopy", self.THROWAWAY, (self.PAGE1,), 1))
        entry = self.app.library.entryForId(self.THROWAWAY)
        self.assertTrue(self.wait_for(lambda: entry.property("pageCount") == 3))
        self.assertEqual(self.notices, [])

    def test_another_notebook_is_never_edited(self):
        view = self.open_document(self.OTHER)
        view._controller.insertImageFileAsSceneItem(QUrl.fromLocalFile(str(self.picture)), QPointF(0, 300))
        wait(300)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("only turned on for the notebook named 'throwaway'", self.notices[0])

    def test_a_file_that_is_not_an_image_changes_nothing(self):
        broken = self.dir / "broken.png"
        broken.write_bytes(b"not an image")
        view = self.open_document(self.THROWAWAY)
        view._controller.insertImageFileAsSceneItem(QUrl.fromLocalFile(str(broken)), QPointF(0, 300))
        self.assertTrue(self.wait_for(lambda: self.notices))
        self.assertIn("Could not read the image broken.png", self.notices[0])
        view._controller.insertImageFileAsSceneItem(QUrl("https://example.com/a.png"), QPointF(0, 300))
        self.assertTrue(self.wait_for(lambda: len(self.notices) == 2))
        self.assertIn("on this computer", self.notices[1])
        self.assertEqual(self.pending(), [])
        self.assertEqual(view.property("status"), 2)



class TypingTests(unittest.TestCase):
    """Typed text through the app's own QML: the Text button turns text mode on, the
    keys go to the page view (SceneView, as the app's keyHandler maps them) and back
    through SceneController.keyAction, and what is typed is shown at once and kept as
    pending edits of the notebook that may be edited, nothing uploaded."""

    setUpClass = classmethod(EditingTests.setUpClass.__func__)
    tearDownClass = classmethod(EditingTests.tearDownClass.__func__)
    setUp, tearDown, pending = EditingTests.setUp, EditingTests.tearDown, EditingTests.pending
    wait_for, qml, scene_views = EditingTests.wait_for, EditingTests.qml, EditingTests.scene_views
    open_document, close_document = EditingTests.open_document, EditingTests.close_document

    def document_view(self) -> QObject:
        return self.qml("DocumentView_")

    def text_mode(self, on: bool) -> None:
        self.document_view().setProperty("textMode", on)

    def keys(self, text: str = "", key=None, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        """Key presses, as the system hands them to the window (shortcuts first)."""
        from PySide6.QtTest import QTest
        for c in text:
            k = Qt.Key(ord(c.upper())) if c.isalnum() else {" ": Qt.Key.Key_Space, "!": Qt.Key.Key_Exclam}[c]
            QTest.keyClick(self.window, k, Qt.KeyboardModifier.ShiftModifier if c.isupper() else
                           Qt.KeyboardModifier.NoModifier)
        if key is not None:
            QTest.keyClick(self.window, key, modifiers)
        wait(20)

    def typing(self, view):
        """The page view in text mode, with the keyboard."""
        self.text_mode(True)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None), self.notices)
        self.window.requestActivate()
        view.forceActiveFocus()
        self.assertTrue(self.wait_for(view.hasActiveFocus), "the page view has no keyboard")
        return view.text_session

    def paragraphs(self, session) -> list:
        from viewer.rm_parser import text_block
        block = text_block(session.text.root_text(), session.authors)
        return [(p.style.name, [(s.text, s.bold, s.italic) for s in p.spans]) for p in block.paragraphs]

    def test_typing_enter_backspace_styles_and_formatting(self):
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        controller = view._controller
        session = self.typing(view)
        # A page with no text gets a box whose first paragraph is a title (createRootDocument).
        self.assertTrue(controller.property("hasRootDocument"), "no text box")
        self.assertEqual(controller.property("textDocumentId"), "0:0")
        self.keys("meeting notex")
        self.keys(key=Qt.Key.Key_Backspace)
        self.keys("s")
        self.keys(key=Qt.Key.Key_Return)
        self.keys("first point")
        self.assertEqual(session.text.text, "meeting notes\nfirst point")
        self.assertEqual(controller.property("textCursorIndex"), 25)
        # Ctrl+5 bullet, Ctrl+7 checkbox, Ctrl+3 subheading 2, Ctrl+2 subheading, Ctrl+4 body.
        for key, style in ((Qt.Key.Key_5, "BULLETPOINT"), (Qt.Key.Key_7, "CHECKBOX_UNCHECKED"),
                           (Qt.Key.Key_3, "SUBHEADING_2"), (Qt.Key.Key_2, "SUBHEADING"), (Qt.Key.Key_4, "PARAGRAPH"),
                           (Qt.Key.Key_5, "BULLETPOINT")):
            self.keys(key=key, modifiers=Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.paragraphs(session)[1][0], style)
        self.assertTrue(controller.property("textParagraphStyle").matchesButtonStyle(3))       # Bulletpoint
        self.assertFalse(controller.property("textParagraphStyle").matchesButtonStyle(0))
        # Enter in a list item continues the list; Enter on the empty item ends it.
        self.keys(key=Qt.Key.Key_Return)
        self.assertEqual(self.paragraphs(session)[2][0], "BULLETPOINT")
        self.keys(key=Qt.Key.Key_Return)
        self.assertEqual([p[0] for p in self.paragraphs(session)], ["TITLE", "BULLETPOINT", "PARAGRAPH"])
        # Shift+Home selects back to the line start; Ctrl+B makes it bold, Ctrl+I italic.
        self.keys("plain bold")
        for _ in range(4):
            self.keys(key=Qt.Key.Key_Left, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(controller.property("hasTextSelection"))
        self.keys(key=Qt.Key.Key_B, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(controller.property("textSelectionStyles"), 1)
        self.keys(key=Qt.Key.Key_End)
        self.keys(key=Qt.Key.Key_I, modifiers=Qt.KeyboardModifier.ControlModifier)     # for what is typed next
        self.keys("!")
        self.assertEqual(self.paragraphs(session)[2][1],
                         [("plain ", False, False), ("bold", True, False), ("!", False, True)])
        # Shown at once: the cursor is drawn where the text ends.
        self.assertGreater(controller.property("textCursorPosition").y(), 234)
        # Kept as pending edits after a pause, nothing uploaded.
        files = dict(self.cloud.item(self.THROWAWAY))
        self.assertTrue(self.wait_for(lambda: self.pending() and not session.edits), self.notices)
        self.assertEqual({type(e).__name__ for e in self.pending()}, {"TextEdit"})
        self.assertEqual(dict(self.cloud.item(self.THROWAWAY)), files)
        self.text_mode(False)
        self.assertTrue(self.wait_for(lambda: view.text_session is None))
        doc = self.source.sync.tree.get(self.THROWAWAY)
        from ui.library import document_pages
        scene_ = self.source.page_scene(doc, document_pages(doc)[0])
        self.assertEqual(scene_.text.text, "meeting notes\nfirst point\nplain bold!")
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_undo_and_redo_of_typing(self):
        view = self.open_document(self.THROWAWAY, self.PAGE1)
        controller = view._controller
        self.typing(view)
        self.keys("one")
        self.keys(key=Qt.Key.Key_Left)                  # the cursor moved: "one" is an action of its own
        self.keys(key=Qt.Key.Key_End)
        self.keys(" two")
        self.assertTrue(controller.property("undoAvailable"))
        self.keys(key=Qt.Key.Key_Z, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None and view.text_session.text.text == "one"),
                        self.notices)
        self.keys(key=Qt.Key.Key_Z, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None and view.text_session.text.text == ""))
        self.keys(key=Qt.Key.Key_Y, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None and view.text_session.text.text == "one"))
        self.assertTrue(controller.property("redoAvailable"))
        self.keys("!")                                  # typing after a redo: on the text as it is
        self.assertEqual(view.text_session.text.text, "one!")
        self.text_mode(False)
        self.assertTrue(self.wait_for(lambda: view.text_session is None and not self.notices))
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_no_typing_where_editing_is_not_turned_on(self):
        view = self.open_document(self.OTHER)
        self.text_mode(True)
        self.assertTrue(self.wait_for(lambda: self.notices))
        self.assertIsNone(view.text_session)
        self.assertEqual(self.pending(), [])
        self.text_mode(False)


class FakeNetwork(QObject):
    """Stands in for QNetworkInformation (LinkProvider's `information`)."""

    reachabilityChanged = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._reachability = QNetworkInformation.Reachability.Online

    def reachability(self):
        return self._reachability

    def set(self, reachability) -> None:
        self._reachability = reachability
        self.reachabilityChanged.emit(reachability)


class SyncCheckTests(unittest.TestCase):
    """Settings > Cloud > "Check sync" (the app's CheckSyncDialog) over the fake cloud, as
    reMarkable.exe's Checker does it: the network, the log-in, the internet check, the
    sync it asks for, and what the dialog then says."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_demo_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"))
        cls.source.refresh()
        cls.network = FakeNetwork()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp, network_information=cls.network)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.checker = cls.app.singletons.document_sync.syncChecker
        cls.settings, cls.dialog = (next(o for o in cls.window.findChildren(QObject)
                                         if o.metaObject().className().startswith(name))
                                    for name in ("GeneralSettings", "CheckSyncDialog"))

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window, cls.settings, cls.dialog
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def setUp(self):
        self.texts: list[str] = []
        self.checker.changed.connect(self._record)

    def tearDown(self):
        self.checker.changed.disconnect(self._record)
        for dialog in (self.dialog, self.settings):
            if dialog.property("visible"):
                QMetaObject.invokeMethod(dialog, "hide")
        wait(300)
        self.assertTrue(self.wait_for(lambda: not self.checker.running and not self.app.syncer.running))

    def _record(self):
        text = self.checker.property("status")["text"]
        if text and (not self.texts or self.texts[-1] != text):
            self.texts.append(text)

    wait_for = WorkflowTests.wait_for

    def find_visible(self, prop, value):
        todo = [self.window.contentItem()]
        while todo:
            item = todo.pop()
            todo.extend(item.childItems())
            if item.property(prop) != value or item.width() <= 0:
                continue
            shown, up = True, item
            while up is not None and shown:
                shown, up = up.isVisible() and up.opacity() > 0, up.parentItem()
            if shown:
                return item
        return None

    def click(self, item):
        from PySide6.QtTest import QTest
        centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)

    def click_check_sync(self):
        """General settings > Cloud ("Check sync"), then the dialog's "Check sync" button."""
        if not self.dialog.property("visible"):
            QMetaObject.invokeMethod(self.settings, "show")
            self.assertTrue(self.wait_for(lambda: self.find_visible("text", "Cloud") is not None))
            self.click(self.find_visible("text", "Cloud"))
            self.assertTrue(self.wait_for(lambda: self.dialog.property("visible")))
        self.assertTrue(self.wait_for(lambda: self.find_visible("text", "Check sync") is not None))
        self.click(self.find_visible("text", "Check sync"))

    def status(self):
        return self.checker.property("status")

    def check_texts(self) -> list[str]:
        """What the dialog said from the start of the check on."""
        return self.texts[self.texts.index("Checking internet connection..."):]

    def add_document(self, name: str) -> None:
        doc = demo_id(name)
        self.cloud.put_item(doc, {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": ms(0)},
                            {"fileType": "pdf"}, {f"{doc}.pdf": make_pdf(name, [["one"]])})

    def test_check_sync_checks_the_connection_then_syncs(self):
        requests = len(self.cloud.requests)
        statuses, counts = [], []
        document_sync = self.app.singletons.document_sync
        document_sync.syncStatusChanged.connect(lambda status, detail: statuses.append(status))
        document_sync.progressCountsChanged.connect(counts.append)
        self.add_document("Arrived from the tablet")
        self.click_check_sync()
        self.assertTrue(self.checker.running)
        self.assertTrue(self.wait_for(lambda: self.checker.state == library.SyncChecker.Success))
        self.assertEqual(self.check_texts(), ["Checking internet connection...", "Waiting for sync", "Syncing...",
                                              "Syncing (0 of 1)", "Syncing (1 of 1)", "Sync completed successfully."])
        self.assertEqual(self.status(), {"text": "Sync completed successfully.", "detail": "",
                                         "icon": "qrc:/ark/icons/checkmark"})
        self.assertFalse(self.checker.property("error"))
        # The internet check first, then a full sync from the root.
        made = self.cloud.requests[requests:]
        self.assertEqual(made[0], "/discovery/v1/endpoints")
        self.assertTrue(any(path.startswith("/sync/") for path in made[1:]))
        self.assertEqual(statuses, [library.SyncStatus.Syncing, library.SyncStatus.Completed])
        # ProgressCounts as the app's SyncNotification reads them ("Syncing (1 of 1)").
        self.assertEqual(counts[-2], {"transferred": 1, "scheduled": 1, "progress": 1.0, "state": "Syncing"})
        self.assertEqual(counts[-1]["state"], "")
        # The dialog shows it, and the button is back.
        self.assertIsNotNone(self.find_visible("message", "Sync completed successfully."))
        self.assertIsNotNone(self.find_visible("text", "Check sync"))
        self.assertEqual(real_warnings(self.app.warnings), [])

    def test_no_network(self):
        requests = len(self.cloud.requests)
        self.network.set(QNetworkInformation.Reachability.Disconnected)
        try:
            self.assertFalse(self.app.singletons.link.property("isOnline"))
            self.assertTrue(self.wait_for(lambda: self.find_visible("message", "No cloud connection")))  # main.qml
            self.click_check_sync()
            self.assertTrue(self.wait_for(lambda: self.checker.property("error")))
            self.assertEqual(self.status(), {"text": "You're not connected to Wi-Fi", "detail": "",
                                             "icon": "qrc:/ark/icons/wifi_x"})
            self.assertEqual(self.cloud.requests[requests:], [])
        finally:
            self.network.set(QNetworkInformation.Reachability.Online)
        self.assertTrue(self.wait_for(lambda: self.find_visible("message", "No cloud connection") is None))

    def test_no_internet(self):
        import socket
        import urllib.error
        transport = self.app.auth.transport

        def offline(method, url, headers, body, timeout):
            raise urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
        self.app.auth.transport = offline
        try:
            self.click_check_sync()
            self.assertTrue(self.wait_for(lambda: self.checker.property("error")))
        finally:
            self.app.auth.transport = transport
        text = 'Unable to connect to the Internet. \nCheck your network settings and try again.("Host not found error")'
        self.assertEqual(self.status(), {"text": text, "detail": "", "icon": "qrc:/ark/icons/wifi_x"})
        self.assertFalse(self.app.syncer.running)
        self.assertIsNotNone(self.find_visible(
            "text", "There was an issue with your last sync. Click 'Check sync' to try again."))

    def test_failed_sync(self):
        statuses = []
        self.app.singletons.document_sync.syncStatusChanged.connect(lambda status, detail: statuses.append(status))
        self.cloud.fail_next = [400]
        self.click_check_sync()
        self.assertTrue(self.wait_for(lambda: self.checker.property("error")))
        self.assertEqual(self.status()["text"], "Failure during sync. Check your network and try again.")
        self.assertEqual(self.status()["icon"], "qrc:/ark/icons/cloud_x")
        self.assertIn("HTTP 400", self.status()["detail"])
        # A failure (status > 2) is what the app's own "Unable to sync" notification waits for.
        self.assertEqual(statuses, [library.SyncStatus.Syncing, library.SyncStatus.Failed])
        # Checking again once the cloud answers clears it.
        self.click_check_sync()
        self.assertTrue(self.wait_for(lambda: self.checker.state == library.SyncChecker.Success))

    def test_a_sync_running_when_the_check_asks_for_one(self):
        """While a sync runs the dialog hides its button (the checker follows that sync), so
        one can only be running when a sync starts during the internet check: the check's
        own full sync then runs after it, as the app queues it."""
        import threading
        internet, sync, transport = threading.Event(), threading.Event(), self.app.auth.transport

        def slow(method, url, headers, body, timeout):
            (sync if "/sync/" in url else internet).wait(10)
            return transport(method, url, headers, body, timeout)
        self.app.auth.transport = slow
        try:
            self.click_check_sync()
            self.assertTrue(self.wait_for(lambda: self.checker.state == library.SyncChecker.CheckInternetConnection))
            requests = len(self.cloud.requests)
            self.app.syncer.refresh()
            self.assertEqual(self.checker.state, library.SyncChecker.Syncing)
            internet.set()
            self.assertTrue(self.wait_for(lambda: self.checker.state == library.SyncChecker.WaitingForSync))
            sync.set()
            self.assertTrue(self.wait_for(lambda: self.checker.state == library.SyncChecker.Success
                                          and not self.app.syncer.running))
        finally:
            self.app.auth.transport = transport
        roots = [p for p in self.cloud.requests[requests:] if p in ("/sync/v3/root", "/sync/v4/root")]
        self.assertEqual(len(roots), 2)          # the running sync, then the one the check asked for
        self.assertEqual(self.check_texts(), ["Checking internet connection...", "Syncing...", "Waiting for sync",
                                              "Sync completed successfully.", "Syncing...",
                                              "Sync completed successfully."])

    def test_the_sync_must_start_in_time(self):
        request_sync = self.checker.request_sync
        self.checker.request_sync = lambda: None
        self.checker._timer.setInterval(300)
        try:
            self.click_check_sync()
            self.assertTrue(self.wait_for(lambda: self.checker.state == library.SyncChecker.WaitingForSync))
            self.assertEqual(self.status()["text"], "Waiting for sync")
            self.assertTrue(self.wait_for(lambda: self.checker.property("error")))
        finally:
            self.checker.request_sync = request_sync
            self.checker._timer.setInterval(library.SyncChecker.TIMEOUT_MS)
        self.assertEqual(self.status(), {"text": "Unable to sync. \nTry restarting if the problem persists",
                                         "detail": "", "icon": "qrc:/ark/icons/cloud_x"})

    def test_offline_the_app_is_not_connected_to_the_cloud(self):
        cloud = build_demo_cloud()
        tmp = Path(self.tmp.name) / "offline"
        source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cloud), tmp / "cache"), cached_only=True)
        app = QuickApp(source, tmp / "cache", network_information=FakeNetwork())
        app.start()
        self.assertFalse(app.syncer.running)              # nothing to sync offline
        checker = app.singletons.document_sync.syncChecker
        checker.setProperty("onDevice", False)              # as CheckSyncDialog sets it
        checker.startCheck()
        self.assertTrue(checker.property("error"))
        self.assertEqual(checker.property("status")["text"],
                         "You're not connected to the cloud. Check if your app is paired to a reMarkable account.")
        self.assertEqual(cloud.requests, [])
        app.shutdown()

    def test_internet_errors_as_the_app_names_them(self):
        import http.client
        import socket
        import ssl
        import urllib.error
        from core.auth import http_error_code

        def cert(verify_code):
            err = ssl.SSLCertVerificationError(1, "certificate verify failed")
            err.verify_code = verify_code
            return urllib.error.URLError(err)
        codes = {urllib.error.URLError(socket.gaierror(-2, "Name or service not known")): 2,
                 http.client.IncompleteRead(b"partial"): 5,
                 cert(10): 6, cert(9): 6, cert(20): 7,
                 urllib.error.URLError(ssl.SSLError(1, "handshake failure")): 7,
                 TimeoutError("timed out"): 10, ConnectionRefusedError(111, "Connection refused"): 10}
        self.assertEqual({type(getattr(e, "reason", e)).__name__ + str(c): http_error_code(e) for e, c in codes.items()},
                         {type(getattr(e, "reason", e)).__name__ + str(c): c for e, c in codes.items()})
        status = library.internet_error_status
        self.assertTrue(status(6, False)["text"].startswith("There might be an issue with the date/time information"))
        self.assertTrue(status(7, False)["text"].startswith("Unable to establish a secure connection"))
        self.assertTrue(status(10, False)["text"].endswith('try again.("Unspecified network error")'))
        # An answer with an HTTP error status has no error code: the text alone.
        self.assertEqual(status(None, True)["text"],
                         "You're connected to Wi-Fi, but not the internet.\nCheck your Wi-Fi settings and try again.")

    def test_link_follows_reachability_as_the_app_maps_it(self):
        R = QNetworkInformation.Reachability
        network = FakeNetwork()
        link = platform.LinkProvider(information=network)
        changes = []
        link.availabilityChanged.connect(changes.append)
        for reachability, online in ((R.Local, True), (R.Site, True), (R.Online, True),
                                     (R.Unknown, False), (R.Disconnected, False)):
            network.set(reachability)
            self.assertEqual(link.property("isOnline"), online, reachability)
            self.assertEqual(changes[-1], 2 if online else 0)


class LoginTests(unittest.TestCase):
    """Logging in through the app's own LoginWizard (main.qml) with no auth.json, over the
    fake cloud, then out again: no demo library, no restart."""

    # LogoutConfirmDialog's onAccepted (main.qml) also hides the never-shown downgrade
    # dialog, so every log-out prints this; the shipped app does the same.
    LOGOUT_NOTICE = "Trying to close a hidden SheetDialog"

    @classmethod
    def setUpClass(cls):
        import time
        from core.auth import AuthClient
        from core.config import AuthStore, Settings
        from fakecloud import AUTH_HOST
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_demo_cloud()
        cls.cloud.codes = {"abcdefgh", "ijklmnop"}

        def network(method, url, headers, body, timeout):
            if AUTH_HOST in url:
                time.sleep(0.3)        # as long as a real round trip, so the wizard's sheets animate
            return cls.cloud(method, url, headers, body, timeout)
        cls.auth = AuthClient(Settings(), AuthStore(tmp / "config"), network)
        cls.app = QuickApp(LibrarySource(SyncClient(cls.auth, tmp / "cache")), tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=True)
        wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.wizard = next(o for o in cls.window.findChildren(QObject)
                          if o.metaObject().className().startswith("LoginWizard"))

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window, cls.wizard
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def wait_for(self, condition, ms=8000):
        for _ in range(ms // 100):
            if condition():
                return True
            wait(100)
        return condition()

    def visible_item(self, prop, value):
        todo = [self.window.contentItem()]
        while todo:
            item = todo.pop()
            todo.extend(item.childItems())
            if item.property(prop) != value or item.width() <= 0:
                continue
            shown, up = True, item
            while up is not None and shown:
                shown, up = up.isVisible() and up.opacity() > 0, up.parentItem()
            if shown:
                return item
        self.fail(f"no visible item with {prop} = {value!r}")

    def click(self, item):
        from PySide6.QtTest import QTest
        centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        wait(500)

    def enter_code(self, code):
        from PySide6.QtTest import QTest
        field = self.visible_item("placeholderText", "Verification code")
        field.setProperty("text", "")
        self.click(field)
        for ch in code:
            QTest.keyClick(self.window, getattr(Qt.Key, f"Key_{ch.upper()}"))
        self.click(self.visible_item("text", "Pair the app"))

    def state(self):
        return self.wizard.property("state")

    def test_log_in_from_the_wizard_then_out(self):
        from core.config import AuthStore
        authn = self.app.singletons.authentication
        store = AuthStore(Path(self.tmp.name) / "config")
        # Not paired: the wizard covers an empty window, and nothing has gone to the network.
        self.assertFalse(authn.property("loggedIn"))
        self.assertTrue(self.wizard.property("visible"))
        self.assertEqual(self.state(), "intro")
        self.assertEqual(self.app.library.tree.documents(), [])
        self.assertEqual(self.cloud.requests, [])

        opened = []
        platform.QDesktopServices, saved = type("Desktop", (), {"openUrl": staticmethod(
            lambda url: opened.append(url.toString()))}), platform.QDesktopServices
        try:
            self.click(self.visible_item("text", "Log in"))
        finally:
            platform.QDesktopServices = saved
        self.assertEqual(opened, ["https://my.remarkable.com/device/desktop/connect#desktop"])
        self.assertEqual(self.state(), "login")
        # Settings.Prod resolves (com.remarkable's Settings singleton): no environment line.
        environment = [i for i in self.window.findChildren(QObject)
                       if str(i.property("text") or "").startswith("<b>Service environment")]
        self.assertTrue(environment and not any(i.isVisible() for i in environment))

        self.enter_code("wrongcod")
        self.assertTrue(self.wait_for(lambda: self.state() == "help"))      # "Unable to verify account"
        self.assertEqual(authn.property("loginResult"), platform.LOGIN_RESULTS["InvalidOneTimeCode"])
        self.assertFalse(store.load().is_paired)
        self.click(self.visible_item("text", "Back"))

        self.enter_code("abcdefgh")
        self.assertEqual(self.state(), "waiting")                             # "Pairing your device"
        self.assertTrue(self.wait_for(lambda: authn.property("loggedIn")))
        self.assertTrue(self.wait_for(lambda: len(self.app.library.tree.documents()) > 5))
        self.assertTrue(self.wait_for(lambda: not self.wizard.property("visible")))
        saved_state = store.load()
        self.assertTrue(saved_state.device_token and saved_state.user_token)
        warnings = len(self.app.warnings)

        # Log out through the app's dialog: device deleted, tokens and library gone, wizard back.
        dialog = next(o for o in self.window.findChildren(QObject)
                      if o.metaObject().className().startswith("LogoutConfirmDialog"))
        QMetaObject.invokeMethod(dialog, "show")
        wait(500)
        self.click(self.visible_item("text", "Log out"))
        self.assertTrue(self.wait_for(lambda: not authn.property("loggedIn")))
        self.assertTrue(self.wait_for(lambda: self.wizard.property("visible")))
        self.assertEqual(self.state(), "intro")
        self.assertEqual(self.app.library.tree.documents(), [])
        self.assertFalse(store.load().is_paired)
        self.assertEqual(self.cloud.device_tokens, {"device"})
        self.assertFalse((Path(self.tmp.name) / "cache" / "blobs").exists())
        self.assertEqual(real_warnings(self.app.warnings[warnings:]), [self.LOGOUT_NOTICE])

        # Log in again; then the account drops this device: back to the wizard.
        self.wizard.setProperty("state", "login")       # as "Log in" does, without the browser
        wait(500)
        self.enter_code("ijklmnop")
        self.assertTrue(self.wait_for(lambda: authn.property("loggedIn")))
        self.assertTrue(self.wait_for(lambda: len(self.app.library.tree.documents()) > 5))
        self.cloud.revoked = True
        self.app.syncer.refresh()
        self.assertTrue(self.wait_for(lambda: not authn.property("loggedIn")))
        self.assertTrue(self.wait_for(lambda: self.wizard.property("visible")))
        self.assertEqual(self.state(), "login")                                # code entry, with a notice
        self.assertEqual(authn.property("loginErrorMessage"), quick_app.LOGGED_OUT_BY_CLOUD)
        self.assertGreater(len(self.app.library.tree.documents()), 5)         # kept until the next log-in
        self.assertEqual([w for w in real_warnings(self.app.warnings) if w != self.LOGOUT_NOTICE], [])

    def test_login_results_the_wizard_understands(self):
        import ssl
        import urllib.error
        from core.auth import AuthError, AuthNetworkError, DeviceRevokedError, PairingError
        results = platform.LOGIN_RESULTS

        def network(reason):
            try:
                raise AuthNetworkError("could not reach any auth host") from urllib.error.URLError(reason)
            except AuthNetworkError as err:
                return err
        expired = ssl.SSLCertVerificationError(1, "certificate has expired")
        expired.verify_code = 10
        for error, result in (
                (PairingError("rejected (HTTP 400)", 400), "InvalidOneTimeCode"),
                (PairingError("rejected (HTTP 502)", 502), "GenericError"),
                (network(expired), "SslCertificateTimeError"),
                (network(ssl.SSLError(1, "handshake failure")), "SslHandshakeError"),
                (network(OSError("Network is unreachable")), "NetworkError"),
                (DeviceRevokedError("device token rejected"), "GenericError"),
                (AuthError("You must connect a rM device"), "GenericError")):
            with self.subTest(error=str(error)):
                code, message = platform.login_result(error)
                self.assertEqual(code, results[result])
                self.assertTrue(message)
        # LoginWizard replaces the cloud's "no tablet" message by matching its text.
        self.assertIn("You must connect a rM device", platform.login_result(AuthError("You must connect a rM device"))[1])

    def test_offline_never_logs_in(self):
        from core.auth import AuthClient
        from core.config import AuthStore, Settings
        tmp = Path(self.tmp.name) / "offline"
        auth = AuthClient(Settings(), AuthStore(tmp), self.cloud)
        app = QuickApp(LibrarySource(SyncClient(auth, tmp / "cache"), cached_only=True), tmp / "cache")
        authn = app.singletons.authentication
        self.assertTrue(authn.property("loggedIn"))     # the cached library, no log-in screen
        authn.legacyLogin("abcdefgh")
        self.assertFalse(authn.property("loggedIn"))
        self.assertEqual(authn.property("loginResult"), platform.LOGIN_RESULTS["NetworkError"])
        self.assertEqual(authn.property("loginErrorMessage"), quick_app.OFFLINE_LOGIN)
        self.assertFalse(auth.is_paired)
        app.shutdown()

    def test_run_py_opens_the_demo_only_when_asked(self):
        import importlib.util
        import os
        from unittest import mock
        spec = importlib.util.spec_from_file_location("run_client", HERE.parent / "run.py")
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        home = Path(self.tmp.name) / "home"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "config"),
                                          "XDG_CACHE_HOME": str(home / "cache")}):
            quick, sync = run.make_app(run.parse_args([]), home / "demo")
            self.assertFalse(quick.auth.is_paired)                         # no auth.json
            self.assertFalse(quick.singletons.authentication.property("loggedIn"))   # -> log-in screen
            self.assertEqual(quick.source.load_cached().documents(), [])   # not the demo library
            self.assertTrue(sync)
            quick.shutdown()
            demo, _ = run.make_app(run.parse_args(["--demo"]), home / "demo")
            self.assertTrue(demo.singletons.authentication.property("loggedIn"))
            self.assertGreater(len(demo.source.refresh().documents()), 5)
            demo.shutdown()
        self.assertFalse((home / "config" / "remarkable-linux" / "auth.json").exists())

    def test_standalone_build_gives_launched_programs_the_system_libraries(self):
        """GitHub issue #2: the browser did not open from the standalone build."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_client", HERE.parent / "run.py")
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        bundle = "/opt/remarkable-linux/_internal"
        # What PyInstaller's bootloader leaves when LD_LIBRARY_PATH was unset...
        env = {"LD_LIBRARY_PATH": bundle, "PATH": "/usr/bin"}
        run.restore_system_environment(env, frozen=True)
        self.assertEqual(env, {"PATH": "/usr/bin"})
        # ...and when the user had one: theirs comes back.
        env = {"LD_LIBRARY_PATH": f"{bundle}:/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}
        run.restore_system_environment(env, frozen=True)
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/opt/lib"})
        # Running from source changes nothing.
        env = {"LD_LIBRARY_PATH": "/opt/lib"}
        run.restore_system_environment(env, frozen=False)
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/opt/lib"})
        # Once Qt has read them, the bundle's Qt paths and the bootloader's markers go too
        # (what a frozen build's xdg-open child got); anyone else's Qt paths stay.
        internal = "/opt/remarkable-linux/_internal"
        env = {"QT_PLUGIN_PATH": f"{internal}/PySide6/Qt/plugins", "QML2_IMPORT_PATH": f"{internal}/PySide6/Qt/qml",
               "_PYI_ARCHIVE_FILE": "/opt/remarkable-linux/remarkable-linux", "_PYI_PARENT_PROCESS_LEVEL": "1",
               "_PYI_LINUX_PROCESS_NAME": "remarkable-linu", "PATH": "/usr/bin"}
        run.release_bundle_environment(env, internal)
        self.assertEqual(env, {"PATH": "/usr/bin"})
        env = {"QT_PLUGIN_PATH": "/usr/lib64/qt6/plugins"}
        run.release_bundle_environment(env, internal)
        self.assertEqual(env, {"QT_PLUGIN_PATH": "/usr/lib64/qt6/plugins"})
        env = {"_PYI_PARENT_PROCESS_LEVEL": "1"}
        run.release_bundle_environment(env, None)            # not frozen: no bundle, nothing changes
        self.assertEqual(env, {"_PYI_PARENT_PROCESS_LEVEL": "1"})


if __name__ == "__main__":
    unittest.main()
