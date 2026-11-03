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

from PySide6.QtCore import QEventLoop, QMetaObject, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, qVersion
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from ui.quick.app import configure, prepare

prepare()
app = QApplication.instance() or QApplication([])
configure(app)

from core.models import ROOT_ID, TRASH_ID
from core.sync import SyncClient
from demo_library import build_demo_cloud
from fakecloud import paired_auth
from ui.library import LibrarySource
from ui.quick import bridge
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
        # Only the platform's StandardKey notices remain (Qt warns when a key has
        # several bindings; the shipped Qt 6.10.3 has the same check).
        # Qt 6.11 also notes that two of the app's components shadow a base member
        # (RMIconButton.enabled, Background.data), a check the app's Qt does not have.
        notices = ["Only binding to one of multiple key bindings"]
        if tuple(int(x) for x in qVersion().split(".")[:2]) >= (6, 11):
            notices.append("overrides a member of the base object")
        real = [w for w in self.app.warnings if not any(n in w for n in notices)]
        self.assertEqual(real, [])

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


if __name__ == "__main__":
    unittest.main()
