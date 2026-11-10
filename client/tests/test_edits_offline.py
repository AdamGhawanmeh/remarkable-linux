"""Offline tests for editing: an image put on a page is kept here as a pending edit
(core.edits), drawn over the cached page (ui.library), prepared as the app stores one
(ui.editing), and uploaded only by SyncClient.push_edits, against the in-memory cloud.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fakecloud import FakeCloud, paired_auth, sha  # noqa: E402
from rmfixtures import line_item, points_v2  # noqa: E402

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QImageIOHandler, QImageWriter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import push_edits  # noqa: E402
from core.edits import (EditError, ImageDelete, ImageInsert, ImageRestore, ImageTransform, PendingEdits,  # noqa: E402
                        document_files, page_images, page_with_edits)
from core.models import json_bytes  # noqa: E402
from core.sync import SyncClient, SyncError, json_changes, parse_index  # noqa: E402
from ui import editing  # noqa: E402
from ui.library import LibrarySource, document_pages  # noqa: E402
from viewer import rm_writer  # noqa: E402
from viewer.rm_parser import RmParseError, parse  # noqa: E402

THROWAWAY = "11111111-aaaa-4aaa-8aaa-111111111111"
OTHER = "22222222-bbbb-4bbb-8bbb-222222222222"
PDF = "33333333-cccc-4ccc-8ccc-333333333333"
PAGE1 = "aaaaaaaa-0000-4000-8000-000000000001"
PAGE2 = "aaaaaaaa-0000-4000-8000-000000000002"     # has no .rm yet
TABLET = bytes(range(16))
THEN, NOW = 1_730_000_000_000, 1_760_000_000_000
LAYER = (0, 11)


def drawn_page() -> bytes:
    """A page the tablet drew one stroke on."""
    header, blocks = rm_writer.split(rm_writer.blank_page(TABLET))
    return rm_writer.join(header, blocks) + line_item(LAYER, (1, 15), tool=17, color=0,
                                                      points=points_v2([(0, 0, 0, 0, 2, 255), (50, 50, 0, 0, 2, 255)]))


def notebook_content(*pages: str) -> dict:
    return {"fileType": "notebook", "formatVersion": 2, "orientation": "portrait", "pageCount": len(pages),
            "extraMetadata": {}, "tags": [],
            "cPages": {"pages": [{"id": p, "idx": {"timestamp": "1:2", "value": "b" + "abc"[n]},
                                  "template": {"timestamp": "1:1", "value": "Blank"}} for n, p in enumerate(pages)]}}


def meta(name: str, **extra) -> dict:
    return {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": str(THEN),
            "createdTime": str(THEN), "pinned": False, **extra}


def build_cloud() -> FakeCloud:
    cloud = FakeCloud()
    cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE1, PAGE2),
                   {f"{THROWAWAY}/{PAGE1}.rm": drawn_page()})
    cloud.put_item(OTHER, meta("Diary"), notebook_content(PAGE1), {f"{OTHER}/{PAGE1}.rm": drawn_page()})
    cloud.put_item(PDF, meta("Paper"), {"fileType": "pdf", "pageCount": 1}, {f"{PDF}.pdf": b"%PDF-1.4\n"})
    return cloud


def png(width: int = 40, height: int = 30, alpha: bool = True, color: str = "#3050c0") -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    if alpha:
        image.setPixelColor(0, 0, QColor(0, 0, 0, 0))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def box(x0=-100.0, y0=200.0, x1=100.0, y1=350.0):
    return editing.corners((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)


class Base(unittest.TestCase):
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
        self.tree = self.source.refresh()
        self.doc = self.tree.get(THROWAWAY)
        self.image = png()

    def insert(self, page: str = PAGE1, data: bytes | None = None, when: int = NOW, doc=None) -> ImageInsert:
        return self.source.insert_image(doc or self.doc, page, data or self.image, "png", box(), when)

    def writes(self) -> list[str]:
        return [headers.get("rm-filename", path) for path, headers in self.cloud.puts()]


class JsonTests(unittest.TestCase):
    def test_json_is_written_as_qt_writes_it(self):
        text = json_bytes({"b": [], "a": {}, "c": [1, 2.0, 0.5, {"y": True, "x": None}], "d": "é\n\"q\""}).decode()
        self.assertEqual(text, '{\n    "a": {\n    },\n    "b": [\n    ],\n    "c": [\n        1,\n        2,\n'
                               '        0.5,\n        {\n            "x": null,\n            "y": true\n        }\n'
                               '    ],\n    "d": "é\\n\\"q\\""\n}\n')
        self.assertEqual(json_bytes({}), b"{\n}\n")

    def test_json_changes_names_the_fields(self):
        old = {"a": 1, "pages": [{"id": "p1", "t": "1"}, {"id": "p2"}], "gone": True}
        new = {"a": 1, "pages": [{"id": "p1", "t": "2"}, {"id": "p2", "m": "5"}], "new": []}
        self.assertEqual(json_changes(old, new), [("gone", True, None), ("new", None, []),
                                                  ("pages[id=p1].t", "1", "2"), ("pages[id=p2].m", None, "5")])
        self.assertEqual(json_changes(1, 1.0), [("", 1, 1.0)])
        self.assertEqual(json_changes([1, 2], [1, 2, 3]), [("", [1, 2], [1, 2, 3])])


class PendingEditsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "pending"
        self.data = b"\x89PNG image"
        self.edit = ImageInsert.new(THROWAWAY, PAGE1, "png", box(), sha(self.data), len(self.data), NOW)

    def test_new_edit_gets_a_file_name_and_key_like_the_apps(self):
        other = ImageInsert.new(THROWAWAY, PAGE1, "jpg", box(), sha(self.data), len(self.data), NOW)
        self.assertRegex(self.edit.file_name, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.png$")
        self.assertTrue(other.file_name.endswith(".jpg"))
        self.assertNotEqual(other.key, self.edit.key)
        self.assertEqual(self.edit.path, f"{THROWAWAY}/{PAGE1}/{self.edit.file_name}")
        key = bytes.fromhex(self.edit.key)[::-1]                # stored last byte first, as the tablet's
        self.assertEqual((len(key), key[6] >> 4, key[8] >> 6), (16, 4, 2))
        self.assertEqual(self.edit.vertices, tuple(box()))
        for bad in ({"extension": "gif"}, {"vertices": box()[:3]}):
            with self.assertRaises(EditError):
                ImageInsert.new(**{"doc_id": THROWAWAY, "page_id": PAGE1, "extension": "png", "vertices": box(),
                                   "sha256": sha(self.data), "size": 1, "created_ms": NOW, **bad})

    def test_edits_and_their_files_outlive_the_process(self):
        edits = PendingEdits(self.root)
        edits.add(self.edit, self.data)
        again = PendingEdits(self.root)
        self.assertEqual(again.all(), [self.edit])
        self.assertEqual((again.of(THROWAWAY), again.of(OTHER), again.documents()), ([self.edit], [], [THROWAWAY]))
        self.assertEqual(again.data(self.edit), self.data)
        self.assertEqual((self.root / "edits.json").stat().st_mode & 0o077, 0)

    def test_the_file_must_be_the_one_described(self):
        with self.assertRaises(EditError):
            PendingEdits(self.root).add(self.edit, self.data + b"!")
        self.assertEqual(PendingEdits(self.root).all(), [])

    def test_discard_forgets_an_edit_and_the_file_nothing_else_uses(self):
        edits = PendingEdits(self.root)
        twin = ImageInsert.new(THROWAWAY, PAGE2, "png", box(), sha(self.data), len(self.data), NOW)
        edits.add(self.edit, self.data)
        edits.add(twin, self.data)
        edits.discard([self.edit])
        self.assertEqual((edits.all(), edits.data(twin)), ([twin], self.data))
        edits.discard([twin])
        self.assertEqual(PendingEdits(self.root).all(), [])
        with self.assertRaises(EditError):
            edits.data(twin)

    def test_an_unreadable_record_is_no_edits(self):
        self.root.mkdir(parents=True)
        (self.root / "edits.json").write_text('{"edits": [{"kind": "image", "doc_id": 1}]}')
        with self.assertLogs("rm.edits", "ERROR"):
            self.assertEqual(PendingEdits(self.root).all(), [])


class DocumentFilesTests(Base):
    def files(self, edits, doc=None, author=None):
        doc = doc or self.doc
        return document_files(doc, edits, lambda name: self.source.blob(doc, name), lambda e: self.image,
                              author or self.sync.author())

    def edit(self, page=PAGE1, when=NOW, doc=THROWAWAY) -> ImageInsert:
        return ImageInsert.new(doc, page, "png", box(), sha(self.image), len(self.image), when)

    def test_an_image_changes_four_files(self):
        edit = self.edit()
        files = self.files([edit])
        rm, content, metadata = f"{THROWAWAY}/{PAGE1}.rm", f"{THROWAWAY}.content", f"{THROWAWAY}.metadata"
        self.assertEqual(sorted(files), sorted([rm, content, metadata, edit.path]))
        self.assertEqual(files[edit.path], self.image)
        [image] = parse(files[rm]).placed_images
        self.assertEqual((image.file_name, image.image_ref.hex(), image.vertices), (edit.file_name, edit.key, box()))
        self.assertEqual(len(parse(files[rm]).strokes), 1)
        # The page as it was, plus the image: the old blocks are all still there, byte for byte.
        old = [b.encode() for b in rm_writer.split(drawn_page())[1]][1:]
        new = [b.encode() for b in rm_writer.split(files[rm])[1]]
        self.assertTrue(set(old) <= set(new))
        self.assertEqual(len(new), len(old) + 3)                    # authors (ours added), table, item
        self.assertEqual(rm_writer.decode_authors(rm_writer.split(files[rm])[1][0].payload),
                         [(TABLET, 1), (self.sync.author(), 2)])

    def test_only_the_time_fields_change_in_content_and_metadata(self):
        files = self.files([self.edit()])
        content = {"old": json.loads(self.source.blob(self.doc, f"{THROWAWAY}.content")),
                   "new": json.loads(files[f"{THROWAWAY}.content"])}
        self.assertEqual(json_changes(content["old"], content["new"]),
                         [(f"cPages.pages[id={PAGE1}].modifed", None, str(NOW))])
        metadata = json.loads(files[f"{THROWAWAY}.metadata"])
        self.assertEqual(json_changes(self.doc.metadata.raw, metadata), [("lastModified", str(THEN), str(NOW))])
        self.assertEqual(files[f"{THROWAWAY}.metadata"], json_bytes(metadata))

    def test_times_never_go_back(self):
        files = self.files([self.edit(when=THEN - 5000)])
        self.assertEqual(json.loads(files[f"{THROWAWAY}.metadata"])["lastModified"], str(THEN))
        page = json.loads(files[f"{THROWAWAY}.content"])["cPages"]["pages"][0]
        self.assertEqual(page["modifed"], str(THEN - 5000))        # it had none: the time of the edit

    def test_a_page_without_strokes_gets_its_file(self):
        edit = self.edit(PAGE2)
        files = self.files([edit])
        self.assertNotIn(f"{THROWAWAY}/{PAGE2}.rm", self.doc.files)
        scene = parse(files[f"{THROWAWAY}/{PAGE2}.rm"])
        self.assertEqual(([layer.name for layer in scene.layers], len(scene.placed_images)), (["Layer 1"], 1))
        self.assertEqual(rm_writer.decode_authors(rm_writer.split(files[f"{THROWAWAY}/{PAGE2}.rm"])[1][0].payload),
                         [(self.sync.author(), 1)])

    def test_two_images_on_a_page_stack_in_order(self):
        first, second = self.edit(), self.edit(when=NOW + 10)
        files = self.files([first, second])
        self.assertEqual([i.file_name for i in parse(files[f"{THROWAWAY}/{PAGE1}.rm"]).placed_images],
                         [first.file_name, second.file_name])
        self.assertEqual(json.loads(files[f"{THROWAWAY}.metadata"])["lastModified"], str(NOW + 10))

    def test_edits_the_page_already_has_change_nothing(self):
        edit = self.edit()
        page = self.files([edit])[f"{THROWAWAY}/{PAGE1}.rm"]
        self.assertEqual(page_images(page), {bytes.fromhex(edit.key): edit.file_name})
        self.assertEqual(page_with_edits(page, [edit], self.sync.author()), page)
        self.assertEqual((page_images(None), page_images(drawn_page())), ({}, {}))

    def test_edits_that_no_longer_fit_are_refused(self):
        with self.assertRaises(EditError):                          # not a page of the document
            self.files([self.edit("aaaaaaaa-0000-4000-8000-00000000dead")])
        with self.assertRaises(EditError):                          # another document's edit
            self.files([self.edit(doc=OTHER)])
        self.assertEqual(self.files([]), {})


class LibraryTests(Base):
    def test_only_the_named_notebook_may_be_edited(self):
        self.assertEqual(self.source.edit_refusal(self.doc), "")
        self.assertTrue(self.source.can_edit(self.doc))
        for doc_id, why in ((OTHER, "only turned on for the notebook named 'throwaway'"),
                            ("nope", "not in the library")):
            self.assertIn(why, self.source.edit_refusal(self.tree.get(doc_id)))
        self.assertIn("not turned on", LibrarySource(self.sync).edit_refusal(self.doc))

    def test_a_pdf_or_a_trashed_or_an_ambiguous_name_is_refused(self):
        self.cloud.put_item(PDF, meta("throwaway"), {"fileType": "pdf", "pageCount": 1}, {f"{PDF}.pdf": b"%PDF-1.4\n"})
        tree = self.source.refresh()
        self.assertIn("Only notebooks", self.source.edit_refusal(tree.get(PDF)))
        self.assertEqual(self.source.edit_refusal(tree.get(THROWAWAY)), "")     # the PDF does not count
        self.cloud.put_item(OTHER, meta("throwaway"), notebook_content(PAGE1), {f"{OTHER}/{PAGE1}.rm": drawn_page()})
        tree = self.source.refresh()
        for doc_id in (THROWAWAY, OTHER):
            self.assertIn("2 notebooks are named", self.source.edit_refusal(tree.get(doc_id)))
        self.cloud.put_item(OTHER, meta("throwaway", parent="trash"), notebook_content(PAGE1))
        tree = self.source.refresh()
        self.assertIn("trash", self.source.edit_refusal(tree.get(OTHER)))
        self.assertEqual(self.source.edit_refusal(tree.get(THROWAWAY)), "")

    def test_an_inserted_image_is_kept_here_and_drawn(self):
        page = document_pages(self.doc)[0]
        before = self.source.page_scene(self.doc, page)
        self.assertEqual(before.placed_images, [])
        edit = self.insert()
        self.assertEqual(self.source.pending_edits(self.doc), [edit])
        self.assertEqual(self.source.pending_edits(self.doc, PAGE2), [])
        [image] = self.source.page_scene(self.doc, page).placed_images
        self.assertEqual((image.file_name, image.data, image.vertices), (edit.file_name, self.image, box()))
        self.assertEqual(self.writes(), [])                                     # nothing went to the cloud
        self.assertEqual(self.tree.get(THROWAWAY).files, self.doc.files)
        # ...and it is still there when the app starts again.
        again = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                              editable=("throwaway",), cached_only=True)
        doc = again.load_cached().get(THROWAWAY)
        self.assertEqual(len(again.page_scene(doc, document_pages(doc)[0]).placed_images), 1)

    def test_a_blank_page_shows_its_pending_image(self):
        page = document_pages(self.doc)[1]
        self.assertIsNone(page.rm_name)
        self.assertIsNone(self.source.page_scene(self.doc, page))
        self.insert(PAGE2)
        self.assertEqual(len(self.source.page_scene(self.doc, page).placed_images), 1)

    def test_other_documents_and_unknown_pages_are_refused_and_nothing_is_kept(self):
        with self.assertRaises(EditError):
            self.insert(doc=self.tree.get(OTHER))
        with self.assertRaises(EditError):
            self.insert("aaaaaaaa-0000-4000-8000-00000000dead")
        with self.assertRaises(EditError):
            self.source.insert_image(self.doc, PAGE1, self.image, "gif", box(), NOW)
        self.assertEqual(self.sync.edits.all(), [])

    def test_a_page_the_writer_does_not_understand_is_refused(self):
        odd = drawn_page() + rm_writer.Block(0x7E, 1, 1, b"").encode()
        self.cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE1), {f"{THROWAWAY}/{PAGE1}.rm": odd})
        doc = self.source.refresh().get(THROWAWAY)
        with self.assertRaises(EditError) as raised:
            self.insert(doc=doc)
        self.assertIn("not understood", str(raised.exception))
        self.assertEqual(self.sync.edits.all(), [])

    def test_logging_out_forgets_pending_edits(self):
        self.insert()
        self.source.forget()
        self.assertEqual(self.sync.edits.all(), [])
        self.assertEqual(list((self.dir / "cache" / "pending" / "files").iterdir()), [])


class PushTests(Base):
    def rows(self, doc_id: str = THROWAWAY) -> dict[str, str]:
        return {name: sha(data) for name, data in self.cloud.item(doc_id).items()}

    def test_the_plan_is_worked_out_without_the_network(self):
        edit = self.insert()
        requests = len(self.cloud.calls)
        plan = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache").push_plan(THROWAWAY)
        self.assertEqual(len(self.cloud.calls), requests)
        self.assertEqual([f.name for f in plan.files],
                         [f"{THROWAWAY}.content", f"{THROWAWAY}.metadata", f"{THROWAWAY}/{PAGE1}.rm", edit.path])
        self.assertEqual([f.old_sha256 is None for f in plan.files], [False, False, False, True])
        self.assertEqual(plan.fields, {f"{THROWAWAY}.content": [(f"cPages.pages[id={PAGE1}].modifed", None, str(NOW))],
                                       f"{THROWAWAY}.metadata": [("lastModified", str(THEN), str(NOW))]})
        self.assertEqual((plan.generation, plan.root_hash, plan.old_index_hash),
                         (self.cloud.generation, self.cloud.root_hash, self.doc.hash))
        rows = {e.id: e for e in parse_index(plan.index_data).entries}
        self.assertEqual(set(rows), set(self.doc.files) | {edit.path})
        self.assertEqual(rows[edit.path].hash, sha(self.image))
        text = plan.describe()
        for part in (THROWAWAY, PAGE1, edit.file_name, edit.key, "lastModified", "modifed", "PUT /sync/v3/root"):
            self.assertIn(part, text)

    def test_push_uploads_the_changed_files_and_only_those(self):
        edit = self.insert()
        before, others = self.rows(), {d: self.rows(d) for d in (OTHER, PDF)}
        plan = self.sync.push_plan(THROWAWAY)
        tree = self.sync.push_edits(THROWAWAY)
        after = self.rows()
        self.assertEqual(sorted(self.writes()), sorted([f.name for f in plan.files]
                                                       + [f"{THROWAWAY}.docSchema", "root.docSchema", "/sync/v3/root"]))
        self.assertEqual(after, {**before, **{f.name: f.sha256 for f in plan.files}})   # exactly what the plan said
        self.assertEqual({d: self.rows(d) for d in (OTHER, PDF)}, others)
        self.assertEqual(self.cloud.item(THROWAWAY)[edit.path], self.image)
        self.assertEqual(self.sync.edits.all(), [])
        self.assertEqual(tree.get(THROWAWAY).files[edit.path], sha(self.image))
        # The synced page now has the image itself; nothing is pending or drawn twice.
        doc = tree.get(THROWAWAY)
        [image] = self.source.page_scene(doc, document_pages(doc)[0]).placed_images
        self.assertEqual(image.data, self.image)
        self.assertEqual(self.sync.push_edits(THROWAWAY), tree)                   # nothing left: no requests
        self.assertEqual(len([w for w in self.writes() if w == "/sync/v3/root"]), 1)

    def test_another_device_syncing_meanwhile_keeps_both_changes(self):
        edit = self.insert()
        theirs = drawn_page() + line_item(LAYER, (1, 16), tool=17, color=0, points=points_v2([(9, 9, 0, 0, 2, 255)]))

        def tablet_draws():
            self.cloud.before_commit = None
            content = notebook_content(PAGE1, PAGE2)
            content["cPages"]["pages"][0]["modifed"] = str(NOW + 60_000)
            self.cloud.put_item(THROWAWAY, meta("throwaway", lastModified=str(NOW + 60_000)), content,
                                {f"{THROWAWAY}/{PAGE1}.rm": theirs})
        self.cloud.before_commit = tablet_draws
        tree = self.sync.push_edits(THROWAWAY)
        scene = parse(self.cloud.item(THROWAWAY)[f"{THROWAWAY}/{PAGE1}.rm"])
        self.assertEqual((len(scene.strokes), [i.file_name for i in scene.placed_images]), (2, [edit.file_name]))
        self.assertEqual(self.cloud.metadata(THROWAWAY)["lastModified"], str(NOW + 60_000))   # theirs is later
        content = json.loads(self.cloud.item(THROWAWAY)[f"{THROWAWAY}.content"])
        self.assertEqual(content["cPages"]["pages"][0]["modifed"], str(NOW + 60_000))
        self.assertEqual(tree.get(THROWAWAY).files[edit.path], sha(self.image))
        self.assertEqual(self.sync.edits.all(), [])

    def test_a_document_that_is_gone_or_trashed_keeps_its_edits_pending(self):
        edit = self.insert()
        self.cloud.put_item(THROWAWAY, meta("throwaway", parent="trash"), notebook_content(PAGE1, PAGE2),
                            {f"{THROWAWAY}/{PAGE1}.rm": drawn_page()})
        with self.assertRaises(SyncError):
            self.sync.push_edits(THROWAWAY)
        self.cloud.remove_item(THROWAWAY)
        with self.assertRaises(SyncError):
            self.sync.push_edits(THROWAWAY)
        self.assertEqual(self.sync.edits.all(), [edit])
        self.assertNotIn("/sync/v3/root", self.writes())

    def test_a_page_removed_meanwhile_is_not_overwritten(self):
        edit = self.insert()
        self.cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE2))
        with self.assertRaises(SyncError):
            self.sync.push_edits(THROWAWAY)
        self.assertEqual((self.sync.edits.all(), self.writes()), ([edit], []))

    def test_a_failed_upload_leaves_the_edit_pending(self):
        edit = self.insert()
        self.cloud.fail_next = [500] * 12
        self.sync._sleep = lambda seconds: None
        with self.assertRaises(SyncError):
            self.sync.push_edits(THROWAWAY)
        self.assertEqual(self.sync.edits.all(), [edit])
        self.cloud.fail_next = []
        self.sync.push_edits(THROWAWAY)
        self.assertEqual(self.sync.edits.all(), [])

    def run_tool(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        with contextlib.redirect_stdout(out):
            status = push_edits.main(list(argv), sync)
        return status, out.getvalue()

    def test_the_tool_shows_the_plan_and_sends_nothing(self):
        self.assertEqual(self.run_tool(), (0, "No pending edits.\n"))
        edit = self.insert()
        requests = len(self.cloud.calls)
        status, text = self.run_tool()
        self.assertEqual((status, len(self.cloud.calls)), (0, requests))
        for part in ("'throwaway'", THROWAWAY, PAGE1, edit.file_name, "Nothing was sent", "--confirm"):
            self.assertIn(part, text)

    def test_the_tool_uploads_only_when_told_which_document(self):
        self.insert()
        status, text = self.run_tool("--confirm", OTHER)
        self.assertEqual((status, self.writes()), (1, []))
        status, text = self.run_tool("--confirm", THROWAWAY)
        self.assertEqual(status, 0, text)
        self.assertIn("/sync/v3/root", self.writes())
        self.assertEqual(PendingEdits(self.dir / "cache" / "pending").all(), [])

    def test_the_tool_discards(self):
        self.insert()
        status, text = self.run_tool("--discard", THROWAWAY)
        self.assertEqual((status, self.writes(), PendingEdits(self.dir / "cache" / "pending").all()), (0, [], []))


def tablet_image_page() -> bytes:
    """A page with an image the tablet put on it and later moved (as on a real
    layout): table entry 1:15, the first item 1:16 deleted (two ids), the moved one 1:18
    with moveId 1:16."""
    from rmfixtures import image_entries, image_item, image_value, images_table
    header, blocks = rm_writer.split(rm_writer.blank_page(TABLET))
    return (rm_writer.join(header, blocks[:4]) + images_table(image_entries({TABLET_KEY: TABLET_FILE}))
            + b"".join(b.encode() for b in blocks[4:]) + image_item(LAYER, (1, 16), deleted=2)
            + image_item(LAYER, (1, 18), image_value(TABLET_KEY, box(), move_id=(1, 16)), left=(1, 17)))


TABLET_KEY = bytes.fromhex("5a0c9e3b7d214f68a1e2c3b4d5f60718")
TABLET_FILE = "3f9d2c41-8b7e-4a15-9c06-7e2d1b5a4c83.png"
ME = bytes.fromhex("ffeeddccbbaa99887766554433221100")


def items(page: bytes) -> list:
    return [rm_writer.decode_item(b.payload) for b in rm_writer.split(page)[1] if b.type == rm_writer.BLOCK_IMAGE_ITEM]


class TransformWriterTests(unittest.TestCase):
    """rm_writer.transform_image / delete_image, as reMarkable.exe commits a selection
    transform and a delete."""

    def test_a_move_deletes_the_item_and_adds_a_copy_with_a_moveid(self):
        page = rm_writer.append_image(drawn_page(), author=TABLET, key=TABLET_KEY, file_name=TABLET_FILE,
                                      vertices=box())
        [old] = items(page)
        before = rm_writer.decode_image_value(old.value)
        new = rm_writer.transform_image(page, author=ME, key=TABLET_KEY, vertices=editing.moved(box(), 50, -20))
        deleted, copy = items(new)
        # The old item: same place, neighbours and block type, no value, one deleted id.
        self.assertEqual((deleted.item, deleted.left, deleted.right, deleted.deleted_length, deleted.value),
                         (old.item, old.left, old.right, 1, None))
        # The copy: a new id of this author above everything, on top of the layer, after the old one.
        value = rm_writer.decode_image_value(copy.value)
        self.assertEqual((copy.parent, copy.item, copy.left, copy.right), (LAYER, (2, old.item[1] + 1), old.item, (0, 0)))
        self.assertEqual(value.move_id, old.item)                                # names the item it replaced
        self.assertEqual((value.ref, value.ref_timestamp, value.bounds_timestamp, value.indices),
                         (before.ref, before.ref_timestamp, before.bounds_timestamp, before.indices))
        self.assertEqual(value.vertices, editing.moved(box(), 50, -20))
        self.assertEqual(rm_writer.decode_authors(rm_writer.split(new)[1][0].payload), [(TABLET, 1), (ME, 2)])
        # Everything else is the page as it was, byte for byte.
        old_blocks = [b.encode() for b in rm_writer.split(page)[1][1:] if b.type != rm_writer.BLOCK_IMAGE_ITEM]
        new_blocks = [b.encode() for b in rm_writer.split(new)[1][1:] if b.type != rm_writer.BLOCK_IMAGE_ITEM]
        self.assertEqual(new_blocks, old_blocks)
        [image] = parse(new).placed_images
        self.assertEqual((image.item_id, image.vertices), (copy.item, editing.moved(box(), 50, -20)))

    def test_a_moved_image_keeps_naming_the_first_item(self):
        page = tablet_image_page()
        once = rm_writer.transform_image(page, author=ME, key=TABLET_KEY, vertices=editing.moved(box(), 10, 0))
        twice = rm_writer.transform_image(once, author=ME, key=TABLET_KEY, vertices=editing.moved(box(), 20, 0))
        live = [i for i in items(twice) if i.value is not None]
        self.assertEqual(len(live), 1)
        self.assertEqual(rm_writer.decode_image_value(live[0].value).move_id, (1, 16))    # the tablet's original
        self.assertEqual([(i.item, i.deleted_length) for i in items(twice)],
                         [((1, 16), 2), ((1, 18), 1), ((2, 19), 1), ((2, 20), 0)])
        self.assertEqual([i.left for i in items(twice)][2:], [(1, 18), (2, 19)])

    def test_scaling_and_turning_keep_a_rectangle_the_app_accepts(self):
        page = tablet_image_page()
        for corners in (editing.scaled(box(), -100, 200, 2.5, 2.5), editing.scaled(box(), 100, 350, -1, 1),
                        editing.rotated(box(), 0, 275, 30), editing.rotated(box(), 0, 275, -135)):
            new = rm_writer.transform_image(page, author=ME, key=TABLET_KEY, vertices=corners)
            [image] = parse(new).placed_images
            for got, want in zip(image.vertices, corners):
                for a, b in zip(got, want):
                    self.assertAlmostEqual(a, b, places=3)
        for bad in (editing.scaled(box(), 0, 0, 0.001, 0.001),
                    [(0, 0, 0, 0), (10, 0, 1, 0), (12, 10, 1, 1), (0, 10, 0, 1)],           # not a rectangle
                    [(0, 0, 0, 0), (10, 0, 1, 0), (10, 10, 0, 1), (0, 10, 1, 1)]):          # texture swapped
            with self.assertRaises(ValueError):
                rm_writer.transform_image(page, author=ME, key=TABLET_KEY, vertices=bad)

    def test_no_change_and_a_gone_image_change_nothing(self):
        page = tablet_image_page()
        self.assertIs(rm_writer.transform_image(page, author=ME, key=TABLET_KEY, vertices=box()), page)
        self.assertIsNone(rm_writer.transform_image(page, author=ME, key=bytes(16), vertices=box()))
        self.assertEqual(rm_writer.delete_image(page, author=ME, key=bytes(16)), (page, None))

    def test_delete_leaves_the_item_deleted_and_prunes_the_table(self):
        page = tablet_image_page()
        new, gone = rm_writer.delete_image(page, author=ME, key=TABLET_KEY)
        self.assertEqual(gone, TABLET_FILE)
        self.assertEqual([(i.item, i.deleted_length, i.value) for i in items(new)], [((1, 16), 2, None), ((1, 18), 1, None)])
        [table] = [b for b in rm_writer.split(new)[1] if b.type == rm_writer.BLOCK_IMAGES_TABLE]
        self.assertEqual(table.payload, bytes.fromhex("1c0100000000"))           # kept, empty, as in real pages
        self.assertEqual(parse(new).placed_images, [])
        self.assertEqual(rm_writer.split(rm_writer.join(*rm_writer.split(new)))[1][-1].payload,
                         rm_writer.split(new)[1][-1].payload)

    def test_a_page_where_two_items_show_one_image_is_refused(self):
        from rmfixtures import image_item, image_value
        page = tablet_image_page() + image_item(LAYER, (1, 30), image_value(TABLET_KEY, box()), left=(1, 18))
        with self.assertRaises(RmParseError):
            rm_writer.transform_image(page, author=ME, key=TABLET_KEY, vertices=editing.moved(box(), 1, 1))


class SelectionGeometryTests(unittest.TestCase):
    def scene(self):
        from rmfixtures import image_entries, image_item, image_value, images_table
        second = bytes(range(16, 32))
        page = rm_writer.append_image(tablet_image_page(), author=ME, key=second, file_name="b.png",
                                      vertices=box(0, 300, 200, 400))
        return parse(page), second

    def test_the_topmost_image_of_the_layer_under_the_point_is_found(self):
        scene, second = self.scene()
        self.assertEqual(editing.image_at(scene, 0, -50, 250).image_ref, TABLET_KEY)
        self.assertEqual(editing.image_at(scene, 0, 50, 320).image_ref, second)      # where both are: the upper
        self.assertIsNone(editing.image_at(scene, 0, 500, 900))
        self.assertIsNone(editing.image_at(scene, 1, -50, 250))                      # no such layer
        self.assertIsNone(editing.image_at(None, 0, 0, 0))

    def test_a_turned_image_is_hit_inside_its_turned_shape_only(self):
        corners = editing.rotated(box(-100, 0, 100, 200), 0, 100, 45)
        page = rm_writer.append_image(drawn_page(), author=ME, key=TABLET_KEY, file_name="a.png", vertices=corners)
        scene = parse(page)
        self.assertIsNotNone(editing.image_at(scene, 0, 0, 100))
        self.assertIsNotNone(editing.image_at(scene, 0, 0, -30))                    # a tip of the diamond
        self.assertIsNone(editing.image_at(scene, 0, -95, 5))                       # a corner of the old box

    def test_find_and_bounds(self):
        scene, second = self.scene()
        layer, image = editing.find_image(scene, second)
        self.assertEqual((layer, editing.corner_bounds(image.vertices)), (0, (0, 300, 200, 100)))
        self.assertIsNone(editing.find_image(scene, bytes(16)))

    def test_transforms(self):
        square = editing.corners(0, 0, 10, 10)
        self.assertEqual(editing.moved(square, 3, 4)[0], (-2, -1, 0, 0))
        self.assertEqual(editing.scaled(square, -5, -5, 2, 2)[2], (15, 15, 1, 1))
        turned = editing.rotated(square, 0, 0, 90)                                  # clockwise on screen
        self.assertAlmostEqual(turned[0][0], 5)
        self.assertAlmostEqual(turned[0][1], -5)
        self.assertAlmostEqual(turned[1][0], 5)
        self.assertAlmostEqual(turned[1][1], 5)


class TransformEditTests(Base):
    """Moves and deletes kept as pending edits, replayed and uploaded."""

    def setUp(self):
        super().setUp()
        content = notebook_content(PAGE1, PAGE2)
        self.cloud.put_item(THROWAWAY, meta("throwaway"), content,
                            {f"{THROWAWAY}/{PAGE1}.rm": tablet_image_page(),
                             f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}": self.image})
        self.tree = self.source.refresh()
        self.doc = self.tree.get(THROWAWAY)
        self.page = document_pages(self.doc)[0]

    def shown(self):
        return self.source.page_scene(self.doc, self.page).placed_images

    def test_edits_are_records_that_survive_a_restart(self):
        moved = self.source.transform_image(self.doc, PAGE1, TABLET_KEY, editing.moved(box(), 5, 5), NOW)
        gone = self.source.delete_image(self.doc, PAGE1, TABLET_KEY, NOW + 1)
        self.assertEqual(PendingEdits(self.dir / "cache" / "pending").all(), [moved, gone])
        self.assertEqual((moved.key, gone.key), (TABLET_KEY.hex(), TABLET_KEY.hex()))
        with self.assertRaises(EditError):
            ImageTransform.new(THROWAWAY, PAGE1, "zz", box(), NOW)
        with self.assertRaises(EditError):
            ImageTransform.new(THROWAWAY, PAGE1, TABLET_KEY, box()[:3], NOW)

    def test_a_tablet_image_moved_here_is_drawn_moved(self):
        [image] = self.shown()
        self.assertEqual(image.data, self.image)
        self.source.transform_image(self.doc, PAGE1, TABLET_KEY, editing.moved(box(), 40, 60), NOW)
        [image] = self.shown()
        self.assertEqual((image.vertices, image.data), (editing.moved(box(), 40, 60), self.image))
        self.assertEqual(self.writes(), [])

    def test_moving_changes_the_page_content_and_metadata_only(self):
        self.source.transform_image(self.doc, PAGE1, TABLET_KEY, editing.scaled(box(), -100, 200, 2, 2), NOW)
        plan = self.sync.push_plan(THROWAWAY)
        self.assertEqual([f.name for f in plan.files], [f"{THROWAWAY}.content", f"{THROWAWAY}.metadata",
                                                         f"{THROWAWAY}/{PAGE1}.rm"])
        self.assertEqual(plan.removed, [])
        self.assertIn("move/resize image", plan.describe())
        self.sync.push_edits(THROWAWAY)
        page = self.cloud.item(THROWAWAY)[f"{THROWAWAY}/{PAGE1}.rm"]
        [image] = parse(page).placed_images
        self.assertEqual(image.vertices, editing.scaled(box(), -100, 200, 2, 2))
        self.assertEqual(rm_writer.decode_image_value(items(page)[-1].value).move_id, (1, 16))
        self.assertIn(f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}", self.cloud.item(THROWAWAY))

    def test_deleting_takes_the_file_out_of_the_document(self):
        self.source.delete_image(self.doc, PAGE1, TABLET_KEY, NOW)
        self.assertEqual(self.shown(), [])
        plan = self.sync.push_plan(THROWAWAY)
        self.assertEqual([f.name for f in plan.removed], [f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}"])
        self.assertIn("removed from the document's index", plan.describe())
        rows = {e.id for e in parse_index(plan.index_data).entries}
        self.assertNotIn(f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}", rows)
        self.sync.push_edits(THROWAWAY)
        files = self.cloud.item(THROWAWAY)
        self.assertNotIn(f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}", files)
        self.assertEqual(parse(files[f"{THROWAWAY}/{PAGE1}.rm"]).placed_images, [])
        self.assertEqual(self.sync.edits.all(), [])

    def test_an_image_inserted_then_deleted_here_never_uploads_its_file(self):
        edit = self.insert(PAGE2)
        self.source.delete_image(self.doc, PAGE2, bytes.fromhex(edit.key), NOW + 1)
        files = self.sync.staged_files(self.sync.tree.get(THROWAWAY))
        self.assertNotIn(edit.path, files)
        self.assertEqual(parse(files[f"{THROWAWAY}/{PAGE2}.rm"]).placed_images, [])
        self.assertEqual([(i.deleted_length, i.value) for i in items(files[f"{THROWAWAY}/{PAGE2}.rm"])], [(1, None)])

    def test_an_image_inserted_then_moved_here_is_uploaded_moved(self):
        edit = self.insert(PAGE2)
        self.source.transform_image(self.doc, PAGE2, bytes.fromhex(edit.key), editing.moved(box(), 0, 500), NOW + 1)
        self.sync.push_edits(THROWAWAY)
        files = self.cloud.item(THROWAWAY)
        self.assertEqual(files[edit.path], self.image)
        [image] = parse(files[f"{THROWAWAY}/{PAGE2}.rm"]).placed_images
        self.assertEqual(image.vertices, editing.moved(box(), 0, 500))

    def test_a_move_of_an_image_the_tablet_deleted_meanwhile_is_dropped(self):
        self.source.transform_image(self.doc, PAGE1, TABLET_KEY, editing.moved(box(), 5, 5), NOW)
        deleted, _ = rm_writer.delete_image(tablet_image_page(), author=TABLET, key=TABLET_KEY)
        self.cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE1, PAGE2),
                            {f"{THROWAWAY}/{PAGE1}.rm": deleted})
        before = dict(self.cloud.item(THROWAWAY))
        self.sync.push_edits(THROWAWAY)
        self.assertEqual(self.cloud.item(THROWAWAY), before)                    # nothing left to upload
        self.assertEqual(self.sync.edits.all(), [])

    def test_other_documents_cannot_be_changed(self):
        with self.assertRaises(EditError):
            self.source.delete_image(self.tree.get(OTHER), PAGE1, TABLET_KEY, NOW)
        with self.assertRaises(EditError):
            self.source.transform_image(self.doc, PAGE1, TABLET_KEY, box()[:2] + box()[:2], NOW)
        self.assertEqual(self.sync.edits.all(), [])


class ClipboardTests(Base):
    """Copy, cut and paste of images, as the app's cloneSelectedItems /
    getSelectionPastePos / cloneAddAndSelectItems and its system clipboard do them."""

    def setUp(self):
        super().setUp()
        self.cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE1, PAGE2),
                            {f"{THROWAWAY}/{PAGE1}.rm": tablet_image_page(),
                             f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}": self.image})
        self.tree = self.source.refresh()
        self.doc = self.tree.get(THROWAWAY)
        self.tablet_image = self.source.page_scene(self.doc, document_pages(self.doc)[0]).placed_images[0]

    def clip(self, scale=0.4):
        return editing.clip_image(THROWAWAY, PAGE1, self.tablet_image, scale)

    # -- the clipboard record --
    def test_a_copy_is_a_plain_record_of_the_image(self):
        item = self.clip()
        self.assertEqual(json.loads(json.dumps(item)), item)                 # survives QML and back
        self.assertEqual((item["type"], item["documentId"], item["pageId"], item["fileName"]),
                         ("image", THROWAWAY, PAGE1, TABLET_FILE))
        value = rm_writer.decode_image_value(self.tablet_image.value)
        self.assertEqual((tuple(item["boundsTimestamp"]), tuple(item["refTimestamp"])),
                         (value.bounds_timestamp, value.ref_timestamp))
        self.assertNotIn("moveId", item)                                     # a clone has none
        self.assertEqual(item["vertices"], [[x * 0.4, y * 0.4, u, v] for x, y, u, v in box()])

    def test_records_handed_back_are_checked(self):
        good = self.clip()
        [back] = editing.clip_images([good, {"type": "line"}, {"type": "image", "vertices": [[1, 2]]}, None, 7])
        self.assertEqual((back["timestamps"], back["vertices"][0]), (((1, 17), (1, 18)), tuple(good["vertices"][0])))
        self.assertEqual(editing.clip_images(None), [])
        self.assertEqual(editing.items_bounds([good]), editing.corner_bounds(back["vertices"]))
        self.assertIsNone(editing.items_bounds([]))

    # -- where a paste goes --
    def test_paste_position_is_the_point_kept_inside_the_page(self):
        items = [editing.clip_image(THROWAWAY, PAGE1, self.tablet_image, 1.0)]      # 200 x 150
        page = (-702.0, 0.0, 1404.0, 1872.0)
        self.assertEqual(editing.paste_position(items, page, (0, 500), None), (0, 500))
        self.assertEqual(editing.paste_position(items, page, (-680, 500), None), (-602, 500))   # left edge
        self.assertEqual(editing.paste_position(items, page, (690, 500), None), (602, 500))     # right edge
        self.assertEqual(editing.paste_position(items, page, (0, 10), None), (0, 75))           # top
        self.assertEqual(editing.paste_position(items, page, (0, 1860), None), (0, 1860))       # the page grows down
        self.assertEqual(editing.paste_position(items, page, (0, 500), (0, 500)), (100, 575))   # at the last corner
        self.assertEqual(editing.paste_position([], page, (5, 6), None), (5, 6))

    def test_pasted_corners_are_scaled_then_centred(self):
        item = editing.clip_images([self.clip(0.4)])[0]
        corners = editing.pasted_corners(item, 1 / 0.4, (300, 900))
        x, y, w, h = editing.corner_bounds(corners)
        for got, want in zip((x + w / 2, y + h / 2, w, h), (300, 900, 200, 150)):
            self.assertAlmostEqual(got, want, places=6)
        same = editing.pasted_corners(editing.clip_images([self.clip(1.0)])[0], 1.0 + 1e-15, (0, 0))
        self.assertEqual(editing.corner_bounds(same)[2:], (200, 150))

    # -- what a paste writes --
    def test_a_paste_gets_a_new_file_key_and_ids_and_keeps_the_timestamps(self):
        item = editing.clip_images([self.clip(1.0)])[0]
        corners = editing.pasted_corners(item, 1.0, (100, 1000))
        data = self.source.image_file(THROWAWAY, PAGE1, TABLET_FILE)
        self.assertEqual(data, self.image)
        edit = self.source.insert_image(self.doc, PAGE1, data, "png", corners, NOW, item["timestamps"])
        self.assertNotEqual((edit.key, edit.file_name), (TABLET_KEY.hex(), TABLET_FILE))
        self.assertEqual(PendingEdits(self.dir / "cache" / "pending").all(), [edit])     # timestamps kept on disk
        page = self.sync.staged_files(self.sync.tree.get(THROWAWAY))[f"{THROWAWAY}/{PAGE1}.rm"]
        old, new = (items(tablet_image_page())[-1], items(page)[-1])
        value = rm_writer.decode_image_value(new.value)
        me = rm_writer.decode_authors(rm_writer.split(page)[1][0].payload)[-1][1]
        self.assertEqual(new.item, (me, 20))                         # name 19, then the item: clock 18 + 2
        self.assertEqual((new.left, new.right), (old.item, (0, 0)))
        self.assertEqual((value.bounds_timestamp, value.ref_timestamp, value.move_id), ((1, 17), (1, 18), None))
        [entry] = [e for e in rm_writer.decode_images_table(
            [b for b in rm_writer.split(page)[1] if b.type == rm_writer.BLOCK_IMAGES_TABLE][0].payload)
            if e.key == bytes.fromhex(edit.key)]
        self.assertEqual((entry.file_name, entry.timestamp), (edit.file_name, (me, 19)))
        self.assertEqual(len(parse(page).placed_images), 2)          # the original stays

    def test_a_copy_of_an_image_put_here_and_not_uploaded_can_be_pasted(self):
        first = self.insert(PAGE2)
        self.assertEqual(self.source.image_file(THROWAWAY, PAGE2, first.file_name), self.image)
        with self.assertRaises(EditError):
            self.source.image_file(THROWAWAY, PAGE2, "gone.png")
        with self.assertRaises(EditError):
            self.source.image_file("nope", PAGE2, first.file_name)

    def test_cut_and_paste_uploads_a_new_file_and_drops_the_old(self):
        item = editing.clip_images([self.clip(1.0)])[0]
        self.source.delete_image(self.doc, PAGE1, TABLET_KEY, NOW)                    # cut: copy, then delete
        data = self.source.image_file(THROWAWAY, PAGE1, TABLET_FILE)                 # still loadable
        edit = self.source.insert_image(self.doc, PAGE1, data, "png", editing.pasted_corners(item, 1.0, (0, 600)),
                                        NOW + 1, item["timestamps"])
        self.sync.push_edits(THROWAWAY)
        files = self.cloud.item(THROWAWAY)
        self.assertNotIn(f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}", files)
        self.assertEqual(files[edit.path], data)
        [image] = parse(files[f"{THROWAWAY}/{PAGE1}.rm"]).placed_images
        self.assertEqual(image.image_ref.hex(), edit.key)

    def test_timestamps_must_be_pairs(self):
        with self.assertRaises(EditError):
            ImageInsert.new(THROWAWAY, PAGE1, "png", box(), sha(self.image), len(self.image), NOW, [(1, 2)])
        with self.assertRaises(EditError):
            ImageInsert.new(THROWAWAY, PAGE1, "png", box(), sha(self.image), len(self.image), NOW, [(300, 2), (1, 1)])


class SystemClipboardTests(unittest.TestCase):
    """What the app takes from the system clipboard (transferDataFromSystemClipboard)."""

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def take(self, mime):
        from PySide6.QtCore import QObject, Signal
        from ui.quick.bridge.platform import Clipboard

        class System(QObject):
            dataChanged = Signal()

            def ownsClipboard(self):
                return False

            def mimeData(self):
                return mime

            def image(self):
                return mime.imageData() if mime.hasImage() else QImage()

            def setText(self, text):
                self.text = text
        clipboard = Clipboard(system=System())
        return clipboard, clipboard.property("text")

    def mime(self, **kinds):
        from PySide6.QtCore import QMimeData
        mime = QMimeData()
        for kind, value in kinds.items():
            {"text": mime.setText, "html": mime.setHtml, "image": mime.setImageData, "urls": mime.setUrls}[kind](value)
        return mime

    def picture(self):
        image = QImage(30, 20, QImage.Format.Format_RGB32)
        image.fill(QColor("#008000"))
        return image

    def test_a_copied_picture_is_preferred_to_nothing(self):
        clipboard, content = self.take(self.mime(image=self.picture()))
        self.assertEqual((content["text"], content["hasImage"], clipboard.property("hasText")), ("", True, True))
        self.assertEqual(clipboard.image_of(content).size().width(), 30)
        self.assertTrue(clipboard.image_of({"imageSerial": 99}).isNull())

    def test_text_wins_over_a_picture_unless_the_picture_is_meant(self):
        _, content = self.take(self.mime(text="hello", image=self.picture()))
        self.assertEqual((content["text"], content["hasImage"]), ("hello", True))      # text is pasted
        _, content = self.take(self.mime(html="<b>x</b>", image=self.picture()))      # picture + HTML, no text
        self.assertEqual((content["text"], content["html"], content["hasImage"]), ("", "", True))
        _, content = self.take(self.mime(text="https://example.com/a.png",
                                         html='<img src="https://example.com/a.png">', image=self.picture()))
        self.assertEqual((content["text"], content["hasImage"]), ("", True))           # a browser's "copy image"

    def test_a_copied_image_file_and_an_inline_html_picture(self):
        import base64
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QUrl
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            self.picture().save(str(path))
            _, content = self.take(self.mime(urls=[QUrl.fromLocalFile(str(path))], text=str(path)))
            self.assertEqual((content["text"], content["hasImage"]), ("", True))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.picture().save(buffer, "PNG")
        html = f'<p>x</p><img src="data:image/png;base64,{base64.b64encode(bytes(data)).decode()}">'
        clipboard, content = self.take(self.mime(html=html, text="x"))
        self.assertEqual((content["text"], content["hasImage"]), ("", True))
        self.assertEqual(clipboard.image_of(content).width(), 30)

    def test_plain_text_and_nothing(self):
        clipboard, content = self.take(self.mime(text="just words"))
        self.assertEqual((content["text"], content["hasImage"]), ("just words", False))
        clipboard, content = self.take(self.mime())
        self.assertEqual((content, clipboard.property("hasText")), ({}, False))

    def test_items_and_text_set_what_was_copied_last(self):
        clipboard, _ = self.take(self.mime())
        clipboard.setProperty("items", [{"type": "image"}])
        self.assertEqual((clipboard.property("hasItems"), clipboard.property("lastCopiedType")), (True, 1))
        clipboard.setTextFromString("copied")
        self.assertEqual((clipboard.property("lastCopiedType"), clipboard.textString()), (0, "copied"))
        self.assertEqual(clipboard._system.text, "copied")                  # text also goes to the system


class ImageEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def image(self, width, height, fmt=QImage.Format.Format_ARGB32, color="#804020") -> QImage:
        image = QImage(width, height, fmt)
        image.fill(QColor(color))
        return image

    def test_a_picture_with_alpha_is_stored_as_png(self):
        source = self.image(64, 48)
        source.setPixelColor(3, 3, QColor(0, 0, 0, 0))
        data, extension, size = editing.encode_image(source)
        self.assertEqual((extension, data[:8], (size.width(), size.height())), ("png", b"\x89PNG\r\n\x1a\n", (64, 48)))
        self.assertEqual(QImage.fromData(data).pixelColor(3, 3).alpha(), 0)

    def test_a_picture_without_alpha_is_stored_as_jpeg(self):
        data, extension, size = editing.encode_image(self.image(64, 48, QImage.Format.Format_RGB32))
        self.assertEqual((extension, data[:3], (size.width(), size.height())), ("jpg", b"\xff\xd8\xff", (64, 48)))

    def test_a_large_picture_is_scaled_to_fit_3200(self):
        _, _, size = editing.encode_image(self.image(6400, 1600, QImage.Format.Format_RGB32))
        self.assertEqual((size.width(), size.height()), (3200, 800))
        _, _, size = editing.encode_image(self.image(100, 4000, QImage.Format.Format_RGB32))
        self.assertEqual((size.width(), size.height()), (80, 3200))

    def test_a_png_over_the_limit_becomes_a_jpeg_on_white(self):
        source = self.image(64, 48, color="#00000000")              # fully transparent
        limit, editing.MAX_BYTES = editing.MAX_BYTES, 0
        try:
            with self.assertRaises(EditError):                      # nothing fits in 0 bytes
                editing.encode_image(source)
            editing.MAX_BYTES = len(editing._written(source, b"PNG")) - 1
            # Too small for the PNG; the flat JPEG of this picture is larger still.
            with self.assertRaises(EditError):
                editing.encode_image(source)
        finally:
            editing.MAX_BYTES = limit
        flat = QImage(source.size(), QImage.Format.Format_RGB32)
        flat.fill(Qt.GlobalColor.white)
        self.assertEqual(QImage.fromData(editing._written(flat, b"JPEG", 90)).pixelColor(5, 5).lightness(), 255)

    def test_quality_drops_until_the_file_fits(self):
        noise = QImage(256, 256, QImage.Format.Format_RGB32)
        seed = 1
        for y in range(256):
            for x in range(256):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                noise.setPixel(x, y, 0xFF000000 | (seed & 0xFFFFFF))
        sizes = [len(editing._written(noise, b"JPEG", q)) for q in editing.JPEG_QUALITIES]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        limit, editing.MAX_BYTES = editing.MAX_BYTES, sizes[2]
        try:
            data, extension, _ = editing.encode_image(noise)
        finally:
            editing.MAX_BYTES = limit
        self.assertEqual((extension, len(data)), ("jpg", sizes[2]))

    def test_a_file_is_read_turned_as_its_exif_says(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            writer = QImageWriter(str(path), b"JPEG")
            writer.setTransformation(QImageIOHandler.Transformation.TransformationRotate90)
            self.assertTrue(writer.write(self.image(80, 40, QImage.Format.Format_RGB32)))
            image = editing.read_image(path)
            self.assertEqual((image.width(), image.height()), (40, 80))
            data, extension, vertices = editing.prepare_image(path, 0, 500)
            self.assertEqual((extension, vertices), ("jpg", editing.corners(0, 500, 40, 80)))
            (Path(tmp) / "broken.png").write_bytes(b"not an image")
            with self.assertRaises(EditError):
                editing.read_image(Path(tmp) / "broken.png")

    def test_placement(self):
        self.assertEqual(editing.placed_size(400, 300), (400, 300))             # one scene unit a pixel
        self.assertEqual(editing.placed_size(2808, 1872), (2808, 1872))         # larger than the page too, as the app
        self.assertEqual(editing.corners(0, 100, 40, 20),
                         [(-20, 90, 0, 0), (20, 90, 1, 0), (20, 110, 1, 1), (-20, 110, 0, 1)])


OTHER_KEY = bytes.fromhex("0123456789abcdef0123456789abcdef")
OTHER_FILE = "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d.png"


def staged_page(sync, page_id=PAGE1) -> bytes:
    return sync.staged_files(sync.tree.get(THROWAWAY))[f"{THROWAWAY}/{page_id}.rm"]


class RestoreWriterTests(unittest.TestCase):
    """rm_writer.restore_image: an item put back as reMarkable.exe's undo and redo put
    one back (0x1408cbd60): a new item under a new id, right after the deleted one."""

    def two_images(self) -> bytes:
        """The tablet's image (live item 1:18) with another image put on top of it."""
        return rm_writer.append_image(tablet_image_page(), author=ME, key=OTHER_KEY, file_name=OTHER_FILE,
                                      vertices=box(0, 600, 100, 700))

    def test_a_new_item_right_after_the_deleted_one(self):
        page = self.two_images()
        self.assertEqual([(i.item, i.left) for i in items(page)][-1], ((2, 22), (1, 18)))
        [_, value] = rm_writer.image_item(page, TABLET_KEY)
        gone, name = rm_writer.delete_image(page, author=ME, key=TABLET_KEY, prune=False)
        self.assertIsNone(name)                                                 # the entry is kept for now
        back = rm_writer.restore_image(gone, author=ME, item=(1, 18), value=value)
        restored = items(back)[-1]
        # A new id (the clock ticks once), between the deleted item and the one after it.
        self.assertEqual((restored.parent, restored.item, restored.left, restored.right, restored.deleted_length),
                         (LAYER, (2, 23), (1, 18), (2, 22), 0))
        again = rm_writer.decode_image_value(restored.value)
        self.assertEqual(again, value)                                          # moveId 1:16 kept: it had one
        # The deleted item stays deleted; every other block is as it was, byte for byte.
        self.assertEqual([b.encode() for b in rm_writer.split(back)[1] if b.type != rm_writer.BLOCK_IMAGE_ITEM
                          or decode(b).item != (2, 23)], [b.encode() for b in rm_writer.split(gone)[1]])
        # Back where it was among the layer's items: under the other image.
        self.assertEqual([i.image_ref for i in stacked(parse(back))], [TABLET_KEY, OTHER_KEY])
        self.assertEqual([i.image_ref for i in stacked(parse(page))], [TABLET_KEY, OTHER_KEY])
        self.assertEqual([i for i, _ in rm_writer.sequence(rm_writer.split(back)[1], LAYER)],
                         [(1, 16), (1, 17), (1, 18), (2, 23), (2, 22)])
        self.assertEqual(rm_writer.join(*rm_writer.split(back)), back)        # re-encodes to the same bytes

    def test_the_moveid_names_the_deleted_item_when_it_had_none(self):
        page = rm_writer.append_image(drawn_page(), author=ME, key=OTHER_KEY, file_name=OTHER_FILE, vertices=box())
        [item_id, value] = rm_writer.image_item(page, OTHER_KEY)
        self.assertEqual((item_id, value.move_id), ((2, 19), None))
        gone, _ = rm_writer.delete_image(page, author=ME, key=OTHER_KEY, prune=False)
        back = rm_writer.restore_image(gone, author=ME, item=item_id, value=value)
        restored = items(back)[-1]
        self.assertEqual((restored.item, restored.left, restored.right), ((2, 20), (2, 19), (0, 0)))
        self.assertEqual(rm_writer.decode_image_value(restored.value).move_id, (2, 19))

    def test_what_cannot_be_put_back_is_refused(self):
        page = self.two_images()
        [_, value] = rm_writer.image_item(page, TABLET_KEY)
        with self.assertRaises(ValueError):                                     # still shown
            rm_writer.restore_image(page, author=ME, item=(1, 18), value=value)
        gone, _ = rm_writer.delete_image(page, author=ME, key=TABLET_KEY, prune=False)
        with self.assertRaises(ValueError):                                     # not a deleted item
            rm_writer.restore_image(gone, author=ME, item=(2, 22), value=value)
        with self.assertRaises(ValueError):                                     # no such item
            rm_writer.restore_image(gone, author=ME, item=(3, 40), value=value)
        pruned, _ = rm_writer.prune_images(gone, {TABLET_KEY})
        with self.assertRaises(ValueError):                                     # its entry is gone
            rm_writer.restore_image(pruned, author=ME, item=(1, 18), value=value)

    def test_pruning_drops_only_unused_entries_of_the_keys_named(self):
        page = self.two_images()
        gone, _ = rm_writer.delete_image(page, author=ME, key=TABLET_KEY, prune=False)
        self.assertEqual(rm_writer.prune_images(gone, {OTHER_KEY}), (gone, []))     # still shown
        pruned, names = rm_writer.prune_images(gone, {TABLET_KEY, OTHER_KEY})
        self.assertEqual(names, [TABLET_FILE])
        self.assertEqual(pruned, rm_writer.delete_image(page, author=ME, key=TABLET_KEY)[0])


def decode(block):
    return rm_writer.decode_item(block.payload)


def stacked(scene) -> list:
    """The images of a page bottom to top, as they are drawn (Layer.items)."""
    return [i for layer in scene.layers for i in layer.items() if hasattr(i, "image_ref") and i.vertices]


class UndoTests(Base):
    """Undo and redo of image edits, as reMarkable.exe's SceneActionStack does them: an
    undo or redo is new edits (a delete, an item put back under a new id), kept and
    uploaded like any other edit."""

    def setUp(self):
        super().setUp()
        self.cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE1, PAGE2),
                            {f"{THROWAWAY}/{PAGE1}.rm": tablet_image_page(),
                             f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}": self.image})
        self.tree = self.source.refresh()
        self.doc = self.tree.get(THROWAWAY)
        self.page = document_pages(self.doc)[0]

    def state(self, page=PAGE1):
        return self.source.history_state(self.doc, page)

    def undo(self, page=PAGE1, when=NOW + 100):
        return self.source.undo(self.doc, page, when)

    def redo(self, page=PAGE1, when=NOW + 200):
        return self.source.redo(self.doc, page, when)

    def shown(self, page=None):
        page = page or self.page
        return [(i.image_ref, i.vertices) for i in stacked(self.source.page_scene(self.doc, page))]

    def kinds(self):
        return [type(e).__name__ for e in self.sync.edits.all()]

    def test_nothing_to_undo_or_redo_at_first(self):
        self.assertEqual(self.state(), (False, False))
        self.assertFalse(self.undo())
        self.assertFalse(self.redo())
        self.assertEqual(self.sync.edits.all(), [])

    def test_undoing_an_insert_deletes_it_and_redoing_puts_it_back(self):
        before = self.shown()
        edit = self.insert()
        inserted = self.shown()
        self.assertEqual(self.state(), (True, False))
        self.assertTrue(self.undo())
        self.assertEqual(self.shown(), before)
        self.assertEqual(self.state(), (False, True))
        self.assertTrue(self.redo())
        self.assertEqual(self.shown(), inserted)
        self.assertEqual(self.state(), (True, False))
        self.assertEqual(self.kinds(), ["ImageInsert", "ImageDelete", "ImageRestore"])
        # Redo did not bring the deleted item back: a new item after it, naming it as moveId.
        page = staged_page(self.sync)
        first, again = [i for i in items(page) if i.item[0] != 1]
        self.assertEqual((first.deleted_length, first.value), (1, None))
        self.assertEqual((again.left, again.item[1]), (first.item, first.item[1] + 1))
        self.assertEqual(rm_writer.decode_image_value(again.value).move_id, first.item)
        self.assertEqual(rm_writer.decode_image_value(again.value).ref, bytes.fromhex(edit.key))
        # The file is uploaded once, as for any image put on the page.
        self.assertIn(edit.path, self.sync.staged_files(self.sync.tree.get(THROWAWAY)))

    def test_undoing_a_move_puts_the_image_back_in_its_place_among_the_layers_items(self):
        other = self.insert()                                       # on top of the tablet's image
        self.assertEqual([k for k, _ in self.shown()], [TABLET_KEY, bytes.fromhex(other.key)])
        self.source.transform_image(self.doc, PAGE1, TABLET_KEY, editing.moved(box(), 300, 0), NOW + 1)
        self.assertEqual(self.shown()[-1], (TABLET_KEY, editing.moved(box(), 300, 0)))    # a move puts it on top
        self.assertTrue(self.undo())
        self.assertEqual(self.shown(), [(TABLET_KEY, box()), (bytes.fromhex(other.key), box())])
        page = staged_page(self.sync)
        restored = next(i for i in items(page) if i.value is not None
                        and rm_writer.decode_image_value(i.value).ref == TABLET_KEY)
        self.assertEqual(restored.left, (1, 18))                    # right after the tablet's own item
        self.assertEqual(rm_writer.decode_image_value(restored.value).move_id, (1, 16))   # the first, as before
        self.assertTrue(self.redo())
        self.assertEqual(self.shown()[-1], (TABLET_KEY, editing.moved(box(), 300, 0)))    # on top again
        self.assertEqual(self.kinds(), ["ImageInsert", "ImageTransform", "ImageDelete", "ImageRestore",
                                        "ImageDelete", "ImageRestore"])

    def test_undoing_a_delete_keeps_the_file_in_the_document(self):
        self.source.delete_image(self.doc, PAGE1, TABLET_KEY, NOW)
        self.assertEqual(self.shown(), [])
        self.assertTrue(self.undo())
        self.assertEqual(self.shown(), [(TABLET_KEY, box())])
        plan = self.sync.push_plan(THROWAWAY)
        self.assertEqual(plan.removed, [])
        self.assertIn("put back image", plan.describe())
        self.sync.push_edits(THROWAWAY)
        files = self.cloud.item(THROWAWAY)
        self.assertEqual(files[f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}"], self.image)
        page = files[f"{THROWAWAY}/{PAGE1}.rm"]
        [image] = parse(page).placed_images
        self.assertEqual((image.image_ref, image.vertices), (TABLET_KEY, box()))
        # The table keeps the tablet's entry as it was, byte for byte.
        table = lambda data: next(b.payload for b in rm_writer.split(data)[1] if b.type == rm_writer.BLOCK_IMAGES_TABLE)
        self.assertEqual(table(page), table(tablet_image_page()))

    def test_a_new_edit_empties_the_redo_stack(self):
        self.insert()
        self.assertTrue(self.undo())
        self.assertEqual(self.state(), (False, True))
        self.source.transform_image(self.doc, PAGE1, TABLET_KEY, editing.moved(box(), 5, 5), NOW + 300)
        self.assertEqual(self.state(), (True, False))
        self.assertFalse(self.redo())
        self.assertTrue(self.undo())                                # the move
        self.assertFalse(self.undo())                               # the insert went with the redo stack
        self.assertEqual(self.shown(), [(TABLET_KEY, box())])
        self.assertEqual(self.state(), (False, True))

    def test_undo_and_redo_many_times_mint_new_ids_and_agree_with_a_replay(self):
        self.source.delete_image(self.doc, PAGE1, TABLET_KEY, NOW)
        for n in range(3):
            self.assertTrue(self.undo(when=NOW + 10 * n))
            self.assertEqual(self.shown(), [(TABLET_KEY, box())])
            self.assertTrue(self.redo(when=NOW + 10 * n + 5))
            self.assertEqual(self.shown(), [])
        page = staged_page(self.sync)
        ids = [i.item for i in items(page)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([i.item[1] for i in items(page)][2:], list(range(19, 22)))     # one tick each put-back
        self.assertTrue(all(i.value is None for i in items(page)))
        self.assertEqual(rm_writer.join(*rm_writer.split(page)), page)
        self.assertEqual(page_with_edits(tablet_image_page(), self.sync.edits.all(), self.sync.author()), page)
        # Every put-back names the tablet's first item, as every move does.
        self.assertEqual(self.kinds(), ["ImageDelete"] + ["ImageRestore", "ImageDelete"] * 3)

    def test_undo_made_here_applies_to_a_newer_page(self):
        other = self.insert()
        self.source.transform_image(self.doc, PAGE1, bytes.fromhex(other.key), editing.moved(box(), 0, 400), NOW + 1)
        self.assertTrue(self.undo())
        # Meanwhile the tablet draws a stroke on the page.
        newer = tablet_image_page() + line_item(LAYER, (1, 30), tool=17, color=0,
                                                points=points_v2([(0, 0, 0, 0, 2, 255), (9, 9, 0, 0, 2, 255)]))
        self.cloud.put_item(THROWAWAY, meta("throwaway"), notebook_content(PAGE1, PAGE2),
                            {f"{THROWAWAY}/{PAGE1}.rm": newer, f"{THROWAWAY}/{PAGE1}/{TABLET_FILE}": self.image})
        self.sync.push_edits(THROWAWAY)
        page = self.cloud.item(THROWAWAY)[f"{THROWAWAY}/{PAGE1}.rm"]
        self.assertEqual([(i.image_ref, i.vertices) for i in stacked(parse(page))],
                         [(TABLET_KEY, box()), (bytes.fromhex(other.key), box())])
        self.assertEqual([s.item_id for s in parse(page).strokes], [(1, 30)])     # the tablet's stroke kept
        self.assertTrue(all(i.item[1] > 30 for i in items(page) if i.item[0] != 1))     # ids above the newer clock

    def test_the_stacks_go_when_the_page_changes_elsewhere_or_the_document_closes(self):
        self.insert()
        self.source.forget_history(THROWAWAY)
        self.assertEqual(self.state(), (False, False))
        self.insert()
        self.sync.edits.clear()                                     # discarded (push_edits --discard)
        self.assertEqual(self.state(), (False, False))
        self.insert()
        self.sync.push_edits(THROWAWAY)                             # uploaded: the page is new
        self.source.refresh()
        self.assertEqual(self.state(), (False, False))
        self.assertFalse(self.undo())

    def test_pages_have_their_own_stacks(self):
        self.insert(PAGE2)
        self.assertEqual((self.state(PAGE1), self.state(PAGE2)), ((False, False), (True, False)))
        self.assertTrue(self.undo(PAGE2))
        self.assertEqual(self.shown(document_pages(self.doc)[1]), [])

    def test_several_images_pasted_at_once_are_undone_at_once(self):
        with self.source.edit_action(self.doc, PAGE1) as action:
            for n in range(2):
                self.source.insert_image(self.doc, PAGE1, self.image, "png", box(n * 100, 600, n * 100 + 50, 650),
                                         NOW, None, action)
        self.assertEqual(len(self.shown()), 3)
        self.assertTrue(self.undo())
        self.assertEqual(self.shown(), [(TABLET_KEY, box())])
        self.assertEqual(self.state(), (False, True))
        self.assertTrue(self.redo())
        self.assertEqual(len(self.shown()), 3)

    def test_an_edit_that_changes_nothing_is_not_an_action(self):
        self.source.transform_image(self.doc, PAGE1, TABLET_KEY, box(), NOW)       # where it is already
        self.assertEqual(self.state(), (False, False))

    def test_undo_edits_are_records_that_survive_a_restart(self):
        self.source.delete_image(self.doc, PAGE1, TABLET_KEY, NOW)
        self.undo()
        self.assertEqual(PendingEdits(self.dir / "cache" / "pending").all(), self.sync.edits.all())
        restore = self.sync.edits.all()[-1]
        self.assertEqual(restore.item, ("page", 1, 18))
        with self.assertRaises(EditError):
            ImageRestore.new(THROWAWAY, PAGE1, TABLET_KEY, ("page", 1, 0), NOW)
        with self.assertRaises(EditError):
            ImageRestore.new(THROWAWAY, PAGE1, TABLET_KEY, ("edit", ""), NOW)
        # A new process has the edits but not the stacks, as the app keeps them in memory.
        fresh = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                              editable=("throwaway",))
        fresh.refresh()
        self.assertEqual(fresh.history_state(fresh.sync.tree.get(THROWAWAY), PAGE1), (False, False))
        self.assertEqual(len(fresh.sync.edits.all()), 2)


if __name__ == "__main__":
    unittest.main()
