"""Offline tests for new notebooks, Quick sheets and the document settings the library
sets (core.create, ARCHITECTURE.md 5.6): the files reMarkable.exe 3.29 writes, made here
and uploaded to the in-memory cloud, and the app's own QML driving them (Ctrl+N, Ctrl+O,
the new-notebook dialog). Synthetic ids only.
Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fakecloud import FakeCloud, paired_auth  # noqa: E402
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QSizeF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import create  # noqa: E402
from core.edits import EditError, PageAdd, PageDelete, PageTemplate, document_files  # noqa: E402
from core.models import DocumentTree, Metadata, Content, json_bytes, make_node  # noqa: E402
from core.pagemap import PageMap  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from test_edits_offline import (OTHER, PAGE1, PAGE2, THEN, THROWAWAY, build_cloud, drawn_page, meta,  # noqa: E402
                                notebook_content)
from ui.library import LibrarySource  # noqa: E402
from viewer.rm_writer import author_uuid  # noqa: E402

DEVICE = "00000000-0000-4000-8000-000000000000"          # paired_auth's device
AUTHOR = str(uuid.UUID(bytes=author_uuid(DEVICE)))
NOW = 1_800_000_000_000
NEW_DOC = "44444444-dddd-4ddd-8ddd-444444444444"
NEW_PAGE = "55555555-eeee-4eee-8eee-555555555555"
FOLDER = "66666666-ffff-4fff-8fff-666666666666"
QS_LOW, QS_HIGH = "0aaaaaaa-0000-4000-8000-00000000000a", "0bbbbbbb-0000-4000-8000-00000000000b"


def expected_content(page: str | None = NEW_PAGE, template: str | None = None) -> dict:
    """A new notebook's .content, written out key by key as 3.29 writes it."""
    entry = {"id": page, "idx": {"timestamp": "1:2", "value": "ba"}}
    if template:
        entry["template"] = {"timestamp": "1:1", "value": template}
    return {
        "cPages": {"lastOpened": {"timestamp": "0:0", "value": ""}, "original": {"timestamp": "0:0", "value": -1},
                   "pages": [entry] if page else [], "uuids": [{"first": AUTHOR, "second": 1}]},
        "coverPageNumber": -1, "customZoomCenterX": 0, "customZoomCenterY": 936,
        "customZoomOrientation": "portrait", "customZoomPageHeight": 1872, "customZoomPageWidth": 1404,
        "customZoomScale": 1, "documentMetadata": {}, "extraMetadata": {}, "fileType": "notebook",
        "fontName": "", "formatVersion": 2, "lineHeight": 100, "orientation": "portrait",
        "pageCount": 1 if page else 0, "pageTags": [], "sizeInBytes": "0", "tags": [],
        "textAlignment": "justify", "textScale": 1, "zoomMode": "bestFit",
    }


def diff(before: dict, after: dict) -> dict:
    """{key: (old, new)} of the top-level keys that differ."""
    return {k: (before.get(k), after.get(k)) for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)}


class SchemaTests(unittest.TestCase):
    def test_a_new_notebook_is_written_as_the_app_writes_it(self):
        content = create.notebook_content(author_uuid(DEVICE), NEW_PAGE)
        self.assertEqual(content, expected_content())
        metadata = create.notebook_metadata("Notebook", "", NOW)
        self.assertEqual(metadata, {"createdTime": str(NOW), "lastModified": str(NOW), "lastOpened": str(NOW),
                                    "lastOpenedPage": -1, "new": False, "parent": "", "pinned": False,
                                    "source": "", "type": "DocumentType", "visibleName": "Notebook"})
        files = create.notebook_files(NEW_DOC, content, metadata)
        self.assertEqual(sorted(files), [f"{NEW_DOC}.content", f"{NEW_DOC}.metadata"])     # no .rm, no .pagedata
        # Qt's toJson layout: sorted keys, empty objects/arrays over two lines.
        text = files[f"{NEW_DOC}.content"].decode()
        self.assertIn('    "documentMetadata": {\n    },\n', text)
        self.assertIn('    "pageTags": [\n    ],\n', text)
        self.assertNotIn("margins", text)
        # It reads back as one live, empty page (no template register yet).
        parsed = Content.parse(files[f"{NEW_DOC}.content"])
        self.assertEqual((parsed.page_ids, parsed.pages[0].template, parsed.cover_page_number), ([NEW_PAGE], "", -1))

    def test_given_ids_are_used_when_they_are_uuids(self):
        self.assertEqual(create.given_uuid(NEW_DOC.upper()), NEW_DOC)
        self.assertEqual(create.given_uuid("{" + NEW_DOC + "}"), NEW_DOC)
        for bad in ("", None, "not-a-uuid", NEW_DOC[:-1]):
            self.assertIsNone(create.given_uuid(bad))

    def test_orientation_reads_and_writes_as_the_app_maps_it(self):
        self.assertEqual(create.orientation_name(Qt.Orientation.Horizontal.value), "landscape")
        self.assertEqual(create.orientation_name(Qt.Orientation.Vertical.value), "portrait")
        self.assertEqual(create.orientation_name(0), "portrait")
        for name, value in (("landscape", 1), ("portrait", 2), (None, 2), ("sideways", 2)):
            self.assertEqual(create.orientation_value(name), value)
        content = {"orientation": "portrait"}
        self.assertFalse(create.set_orientation(content, 2))
        self.assertTrue(create.set_orientation(content, 1))
        self.assertEqual(content, {"orientation": "landscape"})
        self.assertFalse(create.set_orientation({}, 2))         # missing reads portrait: nothing to write

    def test_cover_page_and_template_change_only_when_they_differ(self):
        content = expected_content()
        self.assertFalse(create.set_cover_page(content, -1))
        self.assertTrue(create.set_cover_page(content, 0))
        self.assertEqual(content["coverPageNumber"], 0)
        self.assertTrue(create.set_page_template(content, NEW_PAGE, "Blank", author_uuid(DEVICE)))
        self.assertEqual(content["cPages"]["pages"][0]["template"], {"timestamp": "1:1", "value": "Blank"})
        self.assertFalse(create.set_page_template(content, NEW_PAGE, "Blank", author_uuid(DEVICE)))
        self.assertFalse(create.set_page_template(content, NEW_PAGE, "", author_uuid(DEVICE)))
        self.assertTrue(create.set_page_template(content, NEW_PAGE, "P Lines small", author_uuid(DEVICE)))
        self.assertEqual(content["cPages"]["pages"][0]["template"], {"timestamp": "1:2", "value": "P Lines small"})

    def test_the_quick_sheets_are_found_by_name_type_and_trash(self):
        def doc(doc_id, name, parent="", file_type="notebook"):
            return make_node(doc_id, Metadata.parse(meta(name, parent=parent)),
                             Content.parse({"fileType": file_type}))
        tree = DocumentTree([doc(QS_HIGH, "Quick sheets"), doc(QS_LOW, "Quick sheets", parent="trash"),
                             doc(NEW_DOC, "Quick sheets", file_type="pdf"), doc(OTHER, "Quick Sheets")])
        self.assertEqual(create.quick_sheets_id(tree), QS_HIGH)
        self.assertFalse(create.is_quick_sheet(tree.get(QS_LOW), tree))         # in the trash
        self.assertFalse(create.is_quick_sheet(tree.get(NEW_DOC), tree))        # a PDF
        self.assertFalse(create.is_quick_sheet(tree.get(OTHER), tree))          # the name differs
        tree.add(doc(QS_LOW, "Quick sheets"))
        self.assertEqual(create.quick_sheets_id(tree), QS_LOW)                  # the smallest id
        self.assertEqual(create.quick_sheets_id(DocumentTree([])), "")

    def test_a_page_template_edit_is_written_only_when_it_differs(self):
        content = notebook_content(PAGE1, PAGE2)
        doc = make_node(THROWAWAY, Metadata.parse(meta("throwaway")), Content.parse(content),
                        files={f"{THROWAWAY}.content": "c", f"{THROWAWAY}.metadata": "m"})
        files = {f"{THROWAWAY}.content": json.dumps(content).encode(),
                 f"{THROWAWAY}.metadata": json.dumps(meta("throwaway")).encode()}
        same = document_files(doc, [PageTemplate.new(THROWAWAY, PAGE2, "Blank", NOW)], files.__getitem__,
                              lambda e: b"", author_uuid(DEVICE))
        self.assertEqual(same, {})                                      # Blank already: nothing to write
        out = document_files(doc, [PageTemplate.new(THROWAWAY, PAGE2, "P Grid small", NOW)], files.__getitem__,
                             lambda e: b"", author_uuid(DEVICE))
        new = json.loads(out[f"{THROWAWAY}.content"])
        page2 = next(p for p in new["cPages"]["pages"] if p["id"] == PAGE2)
        self.assertEqual(page2["template"]["value"], "P Grid small")
        self.assertEqual(page2["template"]["timestamp"], "2:2")         # a new author's write after 1:1
        self.assertEqual(json.loads(out[f"{THROWAWAY}.metadata"])["lastModified"], str(NOW))


class LibraryCreateTests(unittest.TestCase):
    """LibrarySource and SyncClient: made here, shown at once, uploaded as is."""

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = build_cloud()
        self.cloud.put_item(FOLDER, {"visibleName": "Folder", "parent": "", "type": "CollectionType",
                                     "lastModified": str(THEN)}, {"tags": []})
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway",))
        self.source.refresh()

    def upload(self, doc_id: str) -> None:
        files, parent = self.source.upload_created(doc_id)
        self.sync.create_document(doc_id, files, parent)

    def cloud_json(self, doc_id: str, kind: str) -> dict:
        return json.loads(self.cloud.item(doc_id)[f"{doc_id}.{kind}"])

    def test_a_new_notebook_is_shown_at_once_and_uploaded_as_made(self):
        node = self.source.create_notebook("Notebook", FOLDER, NOW, NEW_DOC, NEW_PAGE)
        self.assertEqual(node.id, NEW_DOC)
        shown = self.source.view_tree(self.sync.tree)
        self.assertEqual((shown.get(NEW_DOC).name, shown.get(NEW_DOC).parent_id), ("Notebook", FOLDER))
        self.assertNotIn(NEW_DOC, self.cloud.docs)                          # not uploaded yet
        self.assertTrue(self.source.can_edit(shown.get(NEW_DOC)))
        self.assertEqual(self.source.page_ids(shown.get(NEW_DOC)), [NEW_PAGE])
        # The dialog's template, before the upload: into the files themselves.
        self.assertTrue(self.source.set_page_template(shown.get(NEW_DOC), 0, "Blank", NOW + 5))
        self.assertEqual(self.sync.edits.all(), [])
        self.upload(NEW_DOC)
        self.assertEqual(sorted(self.cloud.item(NEW_DOC)), [f"{NEW_DOC}.content", f"{NEW_DOC}.metadata"])
        self.assertEqual(self.cloud.item(NEW_DOC)[f"{NEW_DOC}.content"],
                         json_bytes(expected_content(template="Blank")))
        metadata = self.cloud_json(NEW_DOC, "metadata")
        self.assertEqual(metadata, {**create.notebook_metadata("Notebook", FOLDER, NOW), "lastModified": str(NOW + 5)})
        # Once the cloud has it, it is the cloud's copy that is shown and edited, still editable.
        shown = self.source.view_tree(self.sync.tree)
        self.assertIs(shown.get(NEW_DOC), self.sync.tree.get(NEW_DOC))
        self.assertIsNone(self.source.created(NEW_DOC))
        self.assertTrue(self.source.can_edit(self.sync.tree.get(NEW_DOC)))
        again = LibrarySource(self.sync, editable=("throwaway",))           # after a restart too
        self.assertTrue(again.can_edit(self.sync.tree.get(NEW_DOC)))
        # A template after the upload is a pending page edit.
        self.assertFalse(self.source.set_page_template(self.sync.tree.get(NEW_DOC), 0, "Blank", NOW + 9))
        self.assertTrue(self.source.set_page_template(self.sync.tree.get(NEW_DOC), 0, "P Lines small", NOW + 9))
        self.assertEqual([type(e) for e in self.sync.edits.all()], [PageTemplate])

    def test_a_new_notebook_is_refused_when_the_app_refuses_it(self):
        with self.assertRaisesRegex(EditError, "already exists"):
            self.source.create_notebook("Notebook", "", NOW, THROWAWAY)
        with self.assertRaisesRegex(EditError, "folder"):
            self.source.create_notebook("Notebook", "77777777-0000-4000-8000-000000000007", NOW)
        off = LibrarySource(self.sync)
        with self.assertRaisesRegex(EditError, "not turned on"):
            off.create_notebook("Notebook", "", NOW)
        offline = LibrarySource(self.sync, cached_only=True, editable=("throwaway",))
        with self.assertRaisesRegex(EditError, "offline"):
            offline.create_notebook("Notebook", "", NOW)
        self.assertEqual(self.source.view_tree(self.sync.tree).get(NEW_DOC), None)

    def test_a_failed_upload_takes_the_notebook_away(self):
        self.source.create_notebook("Notebook", "", NOW, NEW_DOC, NEW_PAGE)
        self.source.upload_created(NEW_DOC)
        self.source.created_failed(NEW_DOC)
        self.assertIsNone(self.source.view_tree(self.sync.tree).get(NEW_DOC))

    def test_orientation_and_cover_change_only_content(self):
        before_meta, before = self.cloud_json(THROWAWAY, "metadata"), self.cloud_json(THROWAWAY, "content")
        self.cloud.docs[THROWAWAY][f"{THROWAWAY}.thumbnails/{PAGE1}.png"] = b"thumb"
        self.cloud.publish()
        self.sync.refresh()
        self.sync.set_document_settings(THROWAWAY, lambda c: create.set_orientation(c, 1), drop_thumbnails=True)
        self.assertEqual(diff(before, self.cloud_json(THROWAWAY, "content")), {"orientation": ("portrait", "landscape")})
        self.assertEqual(self.cloud_json(THROWAWAY, "metadata"), before_meta)        # lastModified stays
        self.assertNotIn(f"{THROWAWAY}.thumbnails/{PAGE1}.png", self.cloud.item(THROWAWAY))
        self.sync.set_document_settings(THROWAWAY, lambda c: create.set_cover_page(c, 0))
        self.assertEqual(diff(before, self.cloud_json(THROWAWAY, "content")),
                         {"coverPageNumber": (None, 0), "orientation": ("portrait", "landscape")})
        self.assertEqual(self.cloud_json(THROWAWAY, "metadata"), before_meta)
        generation = self.cloud.generation
        self.sync.set_document_settings(THROWAWAY, lambda c: create.set_cover_page(c, 0))
        self.assertEqual(self.cloud.generation, generation)                         # nothing to commit

    def test_clearing_new_changes_only_that_flag(self):
        self.cloud.put_item(NEW_DOC, meta("Imported", new=True, source="com.remarkable.windows"),
                            {"fileType": "pdf", "pageCount": 1})
        self.sync.refresh()
        before = self.cloud_json(NEW_DOC, "metadata")
        self.sync.clear_new([NEW_DOC])
        self.assertEqual(diff(before, self.cloud_json(NEW_DOC, "metadata")), {"new": (True, False)})

    def test_pages_moved_into_a_new_notebook(self):
        doc = self.sync.tree.get(THROWAWAY)
        made, delete = self.source.create_from_pages("Moved", "", doc, [0], NOW, NEW_DOC)
        self.assertIsInstance(delete, PageDelete)
        self.assertEqual(delete.pages, (PAGE1,))
        self.assertEqual(self.sync.edits.all(), [delete])                   # the source loses it on push_edits
        content = Content.parse(self.source.blob(made, f"{NEW_DOC}.content"))
        (new_page,) = content.page_ids
        self.assertNotEqual(new_page, PAGE1)
        self.assertEqual(content.pages[0].template, "Blank")
        self.upload(NEW_DOC)
        files = self.cloud.item(NEW_DOC)
        self.assertEqual(files[f"{NEW_DOC}/{new_page}.rm"], drawn_page())
        metadata = self.cloud_json(NEW_DOC, "metadata")
        self.assertEqual((metadata["visibleName"], metadata["lastOpened"], metadata["lastOpenedPage"]), ("Moved", "0", -1))
        written = self.cloud_json(NEW_DOC, "content")["cPages"]["pages"]
        self.assertEqual(written, [{"id": new_page, "idx": {"timestamp": "1:2", "value": "ba"},
                                    "template": {"timestamp": "1:2", "value": "Blank"}}])
        # PDF pages cannot leave their PDF.
        from test_edits_offline import PDF
        self.assertRaises(EditError, self.source.create_from_pages, "No", "", self.sync.tree.get(PDF), [0], NOW)


# -- the app's own QML ---------------------------------------------------------------

class QuickCreateTests(unittest.TestCase):
    """Ctrl+N (the new-notebook dialog), Ctrl+O (Quick sheets) and the setters, through
    main.qml against the in-memory cloud."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway",))
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

    def keys(self, key, modifiers=Qt.KeyboardModifier.ControlModifier) -> None:
        self.tq.PenTests.keys(self, key, modifiers)

    def new_ids(self, before) -> list[str]:
        return sorted(set(self.cloud.docs) - set(before))

    def test_ctrl_n_makes_a_notebook_and_opens_it(self):
        before = set(self.cloud.docs)
        dialog = self.qml("NamingDialog_")
        self.keys(Qt.Key.Key_N)
        self.assertTrue(self.wait_for(lambda: dialog.property("visible")), self.notices)
        name = dialog.property("name")
        self.assertTrue(name.startswith("Notebook"))
        self.keys(Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        self.assertTrue(self.wait_for(lambda: len(self.new_ids(before)) == 1), self.notices)
        (doc_id,) = self.new_ids(before)
        view = self.qml("DocumentView_")
        self.assertTrue(self.wait_for(lambda: view.property("visible") and view.property("textMode")))
        # Uploaded as 3.29 makes it: one page, Blank at A:1 (the dialog's template), idx
        # "ba" at A:2, portrait, cover -1 (the dialog's defaults change nothing).
        content = json.loads(self.cloud.item(doc_id)[f"{doc_id}.content"])
        (page,) = [p["id"] for p in content["cPages"]["pages"]]
        expected = expected_content(page, template="Blank")
        self.assertEqual(content, expected)
        metadata = json.loads(self.cloud.item(doc_id)[f"{doc_id}.metadata"])
        self.assertEqual(sorted(metadata), ["createdTime", "lastModified", "lastOpened", "lastOpenedPage", "new",
                                            "parent", "pinned", "source", "type", "visibleName"])
        self.assertEqual((metadata["visibleName"], metadata["parent"], metadata["new"], metadata["source"]),
                         (name, "", False, ""))
        self.assertGreaterEqual(int(metadata["lastModified"]), int(metadata["createdTime"]))
        self.assertTrue(self.source.can_edit(self.source.sync.tree.get(doc_id)))
        self.assertEqual(sorted(self.cloud.item(doc_id)), [f"{doc_id}.content", f"{doc_id}.metadata"])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_ctrl_o_makes_the_quick_sheets_then_adds_a_page_to_it(self):
        before = set(self.cloud.docs)
        library = self.app.library
        self.keys(Qt.Key.Key_O)
        self.assertTrue(self.wait_for(lambda: len(self.new_ids(before)) == 1), self.notices)
        (doc_id,) = self.new_ids(before)
        entry = library.entryForId(doc_id)
        self.assertEqual(entry.property("visibleName"), "Quick sheets")
        self.assertTrue(entry.isQuickSheet())
        self.assertFalse(library.entryForId(THROWAWAY).isQuickSheet())
        self.assertIs(self.app.singletons.controller.quickSheets(), entry)      # found, not made again
        view = self.qml("DocumentView_")
        self.assertTrue(self.wait_for(lambda: view.property("visible")))
        content = json.loads(self.cloud.item(doc_id)[f"{doc_id}.content"])
        (page,) = [p["id"] for p in content["cPages"]["pages"]]
        self.assertEqual(content, expected_content(page, template="Blank"))
        # Its one page, at 1404 x 1872: the paper size stays, the callback runs.
        controller = self.app.singletons.document_controller
        self.assertTrue(controller.setPaperSizeForQuickSheetPage(doc_id, QSizeF(1404, 1872)))
        self.assertFalse(controller.setPaperSizeForQuickSheetPage(THROWAWAY, QSizeF(1404, 1872)))
        self.assertFalse(controller.setPaperSizeForQuickSheetPage("", QSizeF(1404, 1872)))
        view.setProperty("textMode", False)
        QMetaObject.invokeMethod(view, "hide")
        self.assertTrue(self.wait_for(lambda: not view.property("visible")))
        # Something on its last page (here: drawn on the tablet): Ctrl+O adds a page after it.
        self.cloud.docs[doc_id][f"{doc_id}/{page}.rm"] = drawn_page()
        self.cloud.publish()
        self.app.set_tree(self.source.refresh())
        self.assertTrue(self.wait_for(lambda: library.entryForId(doc_id).hasContentsOnPage(0)))
        self.keys(Qt.Key.Key_O)
        self.assertTrue(self.wait_for(lambda: view.property("visible")), self.notices)
        added = [e for e in self.source.sync.edits.all() if isinstance(e, PageAdd)]
        self.assertEqual([(e.doc_id, e.index, e.template) for e in added], [(doc_id, 1, "Blank")])
        self.assertTrue(self.wait_for(lambda: library.entryForId(doc_id).property("pageCount") == 2))
        self.assertEqual(self.new_ids(before), [doc_id])                        # no second Quick sheets
        # Two pages now: the paper size of a Quick sheets page is no longer changed.
        self.assertFalse(controller.setPaperSizeForQuickSheetPage(doc_id, QSizeF(1404, 1872)))
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_orientation_cover_and_new_show_at_once_and_upload(self):
        controller, library = self.app.singletons.controller, self.app.library
        entry = library.entryForId(OTHER)
        before_meta = json.loads(self.cloud.item(OTHER)[f"{OTHER}.metadata"])
        before = json.loads(self.cloud.item(OTHER)[f"{OTHER}.content"])
        controller.setOrientation(OTHER, Qt.Orientation.Horizontal.value)
        controller.setCoverPageNumber(OTHER, 0)
        self.assertEqual(entry.property("orientation"), Qt.Orientation.Horizontal.value)      # at once
        self.assertEqual(entry.property("coverPageNumber"), 0)
        self.assertTrue(self.wait_for(lambda: json.loads(self.cloud.item(OTHER)[f"{OTHER}.content"])
                                      .get("coverPageNumber") == 0), self.notices)
        self.assertEqual(diff(before, json.loads(self.cloud.item(OTHER)[f"{OTHER}.content"])),
                         {"coverPageNumber": (None, 0), "orientation": ("portrait", "landscape")})
        self.assertEqual(json.loads(self.cloud.item(OTHER)[f"{OTHER}.metadata"]), before_meta)
        self.assertEqual(library.entryForId(OTHER).property("orientation"), Qt.Orientation.Horizontal.value)
        generation = self.cloud.generation
        controller.setOrientation(OTHER, Qt.Orientation.Horizontal.value)      # the same: nothing is sent
        controller.clearIsNew(OTHER)                                           # not new: nothing is sent
        self.tq.wait(300)
        self.assertEqual(self.cloud.generation, generation)
        # A document the tablet marked new: selecting it clears the flag, and only it.
        self.cloud.docs[OTHER][f"{OTHER}.metadata"] = json.dumps({**before_meta, "new": True}).encode()
        self.cloud.publish()
        self.app.set_tree(self.source.refresh())
        self.assertTrue(library.entryForId(OTHER).property("isNew"))
        controller.clearIsNew(OTHER)
        self.assertFalse(library.entryForId(OTHER).property("isNew"))
        self.assertTrue(self.wait_for(lambda: json.loads(self.cloud.item(OTHER)[f"{OTHER}.metadata"])["new"] is False))
        self.assertEqual(diff({**before_meta, "new": True}, json.loads(self.cloud.item(OTHER)[f"{OTHER}.metadata"])),
                         {"new": (True, False)})
        self.assertEqual(self.notices, [])

    def test_the_paper_size_setting_is_one_the_app_knows(self):
        settings = self.app.singletons.settings
        self.assertEqual(settings.property("paperSize"), QSizeF(1404, 1872))
        settings.setProperty("paperSize", QSizeF(1620, 2160))
        self.assertEqual(settings.property("paperSize"), QSizeF(1620, 2160))
        settings.setProperty("paperSize", QSizeF(100, 100))
        self.assertEqual(settings.property("paperSize"), QSizeF(1404, 1872))
        settings.setProperty("paperSize", QSizeF(1404, 1872))


if __name__ == "__main__":
    unittest.main()
