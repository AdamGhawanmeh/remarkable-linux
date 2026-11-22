"""The audit's safety fixes (ARCHITECTURE.md 5.4, 5.13): pages counted and ordered as
3.29 does, documents and pages newer than this client refused (never opened or written),
a sync protocol it does not speak refused, and legacy eraser strokes that show what is
under the ink again instead of painting white. Synthetic ids only."""

from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from PySide6.QtCore import QEventLoop, QObject, QRectF, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from ui.quick.app import configure, prepare

prepare()
app = QApplication.instance() or QApplication([])
configure(app)

from core import models
from core.edits import EditError, document_files
from core.models import Content, Document, LoadErrorType, Metadata, UnsupportedFormatError, check_format
from core.sync import RootInfo, SyncClient, SyncError, UnsupportedProtocolError, protocol_refusal
from demo_library import build_demo_cloud, demo_id, ms
from fakecloud import paired_auth
from rmfixtures import (ROOT, block, f_id, f_u8, group_item, line_item, points_v2, scene_tree, tree_node,
                        v6_file)
from ui.library import LibrarySource, document_pages
from viewer import renderer, rm_parser, rm_writer

LAYER = (0, 11)


def wait(ms_: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms_, loop.quit)
    loop.exec()


def cpage(page_id: str, key: str, template: str = "Blank", deleted: bool = False) -> dict:
    entry = {"id": page_id, "idx": {"timestamp": "1:2", "value": key},
             "template": {"timestamp": "1:1", "value": template}}
    if deleted:
        entry["deleted"] = {"timestamp": "1:3", "value": 1}
    return entry


def notebook_content(pages: list[dict], **extra) -> dict:
    return {"fileType": "notebook", "formatVersion": 2, "orientation": "portrait",
            "cPages": {"pages": pages, "uuids": [{"first": "00000000-0000-4000-8000-00000000aaaa", "second": 1}],
                       "original": {"timestamp": "0:0", "value": -1},
                       "lastOpened": {"timestamp": "0:0", "value": ""}}, **extra}


def node_of(doc_id: str, content: dict, files: dict | None = None) -> Document:
    return Document(id=doc_id, metadata=Metadata.parse({"visibleName": "x", "type": "DocumentType"}),
                    content=Content.parse(content), files=files or {})


class PageCountAndOrderTests(unittest.TestCase):
    def test_a_formatversion_2_document_counts_its_live_pages(self):
        # 3.29's loader (0x1406669f0) sets pageCount to the page list's size: a stale
        # cached pageCount (a real PDF says 10 with 15 live pages) is not used.
        pages = [cpage(f"p{n}", f"b{chr(97 + n)}") for n in range(5)] + [cpage("gone", "bz", deleted=True)]
        content = Content.parse(notebook_content(pages, pageCount=2))
        self.assertEqual(content.page_count, 5)
        self.assertEqual(len(content.page_ids), 5)

    def test_a_formatversion_1_document_keeps_its_stored_count(self):
        raw = {"fileType": "pdf", "formatVersion": 1, "pageCount": 4, "pages": ["a", "b", "c"],
               "redirectionPageMap": [0, 1, 2], "originalPageCount": 3}
        self.assertEqual(Content.parse(raw).page_count, 4)
        self.assertEqual(Content.parse({**raw, "pageCount": 0}).page_count, 3)

    def test_pages_with_one_key_are_ordered_by_id_as_the_page_map_orders_them(self):
        from core.pagemap import PageMap
        raw = notebook_content([cpage("zz-second", "ba"), cpage("aa-first", "ba"), cpage("mm-last", "bb")])
        self.assertEqual(Content.parse(raw).page_ids, ["aa-first", "zz-second", "mm-last"])
        self.assertEqual(PageMap(json.loads(json.dumps(raw)), "00000000-0000-4000-8000-00000000aaaa").ids(),
                         Content.parse(raw).page_ids)

    def test_pagedata_is_read_for_formatversion_1_only(self):
        # "Skipping template/pagedata file loading for version 2" (0x140661df0).
        v2 = node_of(demo_id("pd2"), notebook_content([{"id": "p0", "idx": {"timestamp": "1:2", "value": "ba"}}]))
        self.assertEqual([r.template for r in document_pages(v2, None, ["P Lines small"])], [""])
        v1 = node_of(demo_id("pd1"), {"fileType": "pdf", "formatVersion": 1, "pages": ["p0"],
                                      "redirectionPageMap": [0], "originalPageCount": 1})
        self.assertEqual([r.template for r in document_pages(v1, 1, ["P Lines small"])], ["P Lines small"])

    def test_format_version_is_read_as_the_app_reads_it(self):
        # 0x1406cefc0: QJsonValue::toInt(0), at least 1; 1 when missing.
        self.assertEqual([models.format_version_of(r) for r in
                          ({}, {"formatVersion": 2}, {"formatVersion": 2.0}, {"formatVersion": "3"},
                           {"formatVersion": 0}, {"formatVersion": True}, {"formatVersion": 7})],
                         [1, 2, 2, 1, 1, 1, 7])


class FormatGuardTests(unittest.TestCase):
    def test_a_newer_formatversion_is_unsupported_and_never_written(self):
        content = notebook_content([cpage("p0", "ba")])
        content["formatVersion"] = 3
        node = node_of(demo_id("future"), content, {f"{demo_id('future')}.content": "0" * 64})
        self.assertTrue(node.content.unsupported)
        with self.assertRaises(UnsupportedFormatError):
            check_format(content)
        check_format({**content, "formatVersion": 2})
        with self.assertRaisesRegex(EditError, "formatVersion 3"):
            document_files(node, [object()], lambda name: b"{}", lambda e: b"", b"\0" * 16)
        self.assertEqual(models.load_error_text(LoadErrorType.UNSUPPORTED_FORMAT_VERSION),
                         "There's a mismatch in format versions. Try updating your software.")
        self.assertEqual(models.load_error_text(LoadErrorType.CLOUD_ARCHIVE), "Document is archived.")
        self.assertEqual(models.load_error_text(0), "")

    def page(self, *blocks: bytes) -> bytes:
        return v6_file(tree_node(LAYER, "Layer 1"), scene_tree(LAYER), group_item(ROOT, (1, 30), LAYER), *blocks)

    def test_a_block_newer_than_its_reader_refuses_the_page(self):
        # 3.29's block reader (0x140a02fb0) with its versions (0x140a079c0): a line
        # block (read up to version 2) of version 3 that needs a reader of version 3.
        stroke = line_item(LAYER, (1, 40), tool=15, color=0, points=points_v2([(0, 100, 0, 0, 2, 255)]))
        newer = bytearray(stroke)
        newer[5:7] = bytes([3, 3])                      # min_version, version
        data = self.page(bytes(newer))
        with self.assertRaisesRegex(rm_parser.RmParseError, "newer reMarkable"):
            rm_parser.parse(data)
        with self.assertRaisesRegex(rm_parser.RmParseError, "newer reMarkable"):
            rm_writer.split(data)
        # A newer version that an old reader may still read (min_version 2) is fine,
        # and so is every real page shape (the versions all cached pages have).
        newer[5:7] = bytes([2, 3])
        self.assertEqual(len(rm_parser.parse(self.page(bytes(newer))).strokes), 1)
        for t, (low, high) in rm_writer.BLOCK_VERSIONS.items():
            self.assertFalse(rm_parser.block_unreadable(t, low, high), hex(t))
        self.assertTrue(rm_parser.block_unreadable(0x0E, 4, 4))
        self.assertFalse(rm_parser.block_unreadable(0x0E, 0, 9))      # min_version 0: kept as it is
        self.assertFalse(rm_parser.block_unreadable(0x7E, 4, 9))      # unknown type: kept byte for byte

    def test_unknown_blocks_are_still_kept(self):
        data = self.page(block(0x7E, b"\x01\x02", version=9, min_version=4))
        header, blocks = rm_writer.split(data)
        self.assertIn(0x7E, [b.type for b in blocks])
        self.assertEqual(rm_writer.join(header, blocks), data)


class ProtocolTests(unittest.TestCase):
    def test_gentree_or_an_unknown_schema_is_refused(self):
        for reply in ({"hash": "a" * 64, "generation": 1, "schemaVersion": 5},
                      {"hash": "a" * 64, "generation": 1, "schemaVersion": 3, "protocol": "gentree"},
                      {"hash": "a" * 64, "generation": 1, "syncProtocol": {"name": "GenTree"}}):
            with self.assertRaises(UnsupportedProtocolError):
                RootInfo.parse(json.dumps(reply).encode())
            self.assertIn("newer sync protocol", protocol_refusal(reply))
        for schema in (3, 4):
            self.assertEqual(RootInfo.parse(json.dumps({"hash": "a" * 64, "generation": 2,
                                                        "schemaVersion": schema}).encode()).schema_version, schema)
        self.assertTrue(issubclass(UnsupportedProtocolError, SyncError))

    def test_a_discovery_reply_naming_gentree_stops_the_sync_before_any_root_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            cloud = build_demo_cloud()
            sync = SyncClient(paired_auth(Path(tmp) / "auth", cloud), Path(tmp) / "cache")
            sync.refresh()                                   # schema 3: fine
            sync.auth.resolver._discovered = {"sync": {"protocol": "gentree", "host": "example.invalid"}}
            with self.assertRaisesRegex(UnsupportedProtocolError, "generation-tree"):
                sync.refresh()


class EraserTests(unittest.TestCase):
    """A legacy Eraser (6) stroke removes the ink drawn before it and shows what is under
    the ink (3.29: 0x140b37da0 copies the background image), never white."""

    BACKGROUND = QColor("#3060c0")          # stands for a PDF page or template

    def scene(self) -> rm_parser.PageScene:
        line = lambda item, y: line_item(LAYER, item, tool=15, color=0, points=points_v2(          # noqa: E731
            [(-300 + 20 * n, y, 0, 0, 12, 255) for n in range(31)]))
        eraser = line_item(LAYER, (1, 42), tool=6, color=2, points=points_v2(
            [(0, 380 + 10 * n, 0, 0, 60, 255) for n in range(25)]))
        data = v6_file(tree_node(LAYER, "Layer 1"), scene_tree(LAYER), group_item(ROOT, (1, 30), LAYER),
                       line((1, 40), 500), eraser, line((1, 43), 560))
        return rm_parser.parse(data)

    def render(self, scene) -> QImage:
        image = QImage(1404 // 2, 1872 // 2, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(self.BACKGROUND)
        painter = QPainter(image)
        painter.scale(0.5, 0.5)
        renderer.paint(painter, scene, background="#ffffff")
        painter.end()
        return image

    def test_erased_ink_shows_the_background_and_later_ink_stays(self):
        image = self.render(self.scene())
        centre = 702 / 2                            # scene x 0 at the page's centre line
        under = image.pixelColor(round(centre), 500 // 2)
        self.assertEqual(under.name(), self.BACKGROUND.name())          # not white, not ink
        self.assertEqual(image.pixelColor(round(centre - 100), 500 // 2).name(), "#000000")   # ink outside it
        self.assertEqual(image.pixelColor(round(centre), 560 // 2).name(), "#000000")         # drawn after it
        self.assertFalse(any(image.pixelColor(x, y).name() == "#ffffff"
                             for x in range(300, 400) for y in range(180, 320, 4)))

    def test_svg_masks_what_came_before_and_has_no_white_stroke(self):
        out = "\n".join(renderer.svg_scene(self.scene()))
        self.assertIn('<mask id="erase-0"', out)
        masked = out.index('mask="url(#erase-0)"')
        first, second = (i for i in range(len(out)) if out.startswith('<g data-tool="ballpoint_v2"', i))
        self.assertEqual(out.count('mask="url(#erase-0)"'), 1)    # the first layer: no finished layer yet
        self.assertLess(masked, first)                              # the ink before the eraser is masked
        between = out[out.rindex("<g", 0, masked):second]    # the masked group is closed by then:
        self.assertEqual(between.count("<g"), between.count("</g>"))  # the ink after the eraser is not masked
        self.assertNotIn('stroke="#ffffff"', out)

    def test_the_eraser_area_is_its_runs_stroked_round(self):
        scene = self.scene()
        eraser = next(s for s in scene.strokes if renderer.is_eraser(s))
        box = renderer.eraser_outline(scene, eraser).boundingRect()
        self.assertAlmostEqual(box.top(), 380 - 30, delta=1.5)
        self.assertAlmostEqual(box.bottom(), 620 + 30, delta=1.5)
        self.assertAlmostEqual(box.width(), 60, delta=1.5)


class LoadErrorDialogTests(unittest.TestCase):
    """main.qml's requestOpenDocument shows SoftwareUpdateDialog for a document whose
    format is newer, and UnableToViewDialog (with loadErrorDescription) for an archived
    one, instead of opening them."""

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_demo_cloud()
        meta = lambda name: {"visibleName": name, "parent": "", "type": "DocumentType",    # noqa: E731
                             "lastModified": ms(1)}
        cls.future, cls.archived, cls.stale = demo_id("future doc"), demo_id("archived doc"), demo_id("stale count")
        newer = notebook_content([cpage(demo_id("future page"), "ba")])
        newer["formatVersion"] = 3
        cls.cloud.put_item(cls.future, meta("Future"), newer)
        cls.cloud.put_item(cls.archived, meta("Archived"), notebook_content([cpage(demo_id("arch page"), "ba")]),
                           {f"{cls.archived}.cloudarchive": b""})
        cls.cloud.put_item(cls.stale, meta("Stale"), notebook_content(
            [cpage(demo_id(f"stale {n}"), f"b{chr(97 + n)}") for n in range(3)], pageCount=1))
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
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def dialog(self, prefix: str) -> QObject:
        return next(o for o in self.window.findChildren(QObject) if o.metaObject().className().startswith(prefix))

    def request_open(self, doc_id: str) -> None:
        from PySide6.QtCore import Q_ARG, QMetaObject
        entry = self.app.library.entryForId(doc_id)
        QMetaObject.invokeMethod(self.window, "requestOpenDocument", Q_ARG("QVariant", entry),
                                 Q_ARG("QVariant", None))
        wait(100)

    def test_the_document_properties(self):
        future, archived, stale = (self.app.library.entryForId(i) for i in (self.future, self.archived, self.stale))
        self.assertEqual((future.property("loadError"), future.property("loadErrorType")),
                         (True, int(LoadErrorType.UNSUPPORTED_FORMAT_VERSION)))
        self.assertEqual((archived.property("loadError"), archived.property("loadErrorType"),
                          archived.property("loadErrorDescription")),
                         (True, int(LoadErrorType.CLOUD_ARCHIVE), "Document is archived."))
        self.assertEqual((stale.property("loadError"), stale.property("pageCount")), (False, 3))

    def test_a_newer_document_shows_the_software_update_dialog(self):
        dialog = self.dialog("SoftwareUpdateDialog")
        self.request_open(self.future)
        self.assertTrue(dialog.property("visible"))
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(dialog, "hide")
        wait(50)
        self.source.edit_everything = True              # Settings > Editing on
        try:
            refusal = self.source.edit_refusal(self.source.sync.tree.get(self.future))
        finally:
            self.source.edit_everything = False
        self.assertIn("mismatch in format versions", refusal)

    def test_an_archived_document_shows_unable_to_view(self):
        dialog = self.dialog("UnableToViewDialog")
        self.request_open(self.archived)
        self.assertTrue(dialog.property("visible"))
        self.assertEqual(dialog.property("textLineOne"), "Document is archived.")
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(dialog, "hide")
        wait(50)

    def test_tags_and_copies_of_a_newer_document_are_refused(self):
        with self.assertRaisesRegex(SyncError, "formatVersion 3"):
            self.source.sync.set_tags([self.future], ["x"])
        with self.assertRaisesRegex(SyncError, "formatVersion 3"):
            self.source.sync.clone_document(self.future, "", "copy")


if __name__ == "__main__":
    unittest.main()
