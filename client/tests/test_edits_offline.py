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
from core.edits import (EditError, ImageDelete, ImageInsert, ImageRestore, ImageTransform, PageAdd,  # noqa: E402
                        PageCopy, PageDelete, PageMove, PendingEdits, Staged, document_files, page_images,
                        page_with_edits)
from core import pagemap  # noqa: E402
from core.pagemap import PageMap, PageMapError, jump, key_after, keys_after, keys_between, not_before  # noqa: E402
from core.models import Content, json_bytes  # noqa: E402
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


# ------------------------------------------------------------------ page management

def entry(page_id: str, key: str, author: int = 1, template: str = "Blank", **extra) -> dict:
    return {"id": page_id, "idx": {"timestamp": f"{author}:2", "value": key},
            "template": {"timestamp": f"{author}:2", "value": template}, **extra}


def content_of(*entries, uuids=None) -> dict:
    return {"fileType": "notebook", "formatVersion": 2,
            "pageCount": sum(1 for e in entries if not (e.get("deleted") or {}).get("value")),
            "cPages": {"pages": list(entries), "lastOpened": {"timestamp": "0:0", "value": ""},
                       "original": {"timestamp": "0:0", "value": -1},
                       "uuids": uuids if uuids is not None else [{"first": TABLET_UUID, "second": 1}]}}


TABLET_UUID = "0f0e0d0c-0b0a-5908-8706-050403020100"
OURS = bytes(range(100, 116))
OURS_UUID = "64656667-6869-6a6b-6c6d-6e6f70717273"
P = [f"bbbbbbbb-0000-4000-8000-{n:012d}" for n in range(10)]


class PageKeyTests(unittest.TestCase):
    """The page-order keys, as reMarkable.exe makes them (core.pagemap), checked on the
    shapes found in real libraries: every page appended on a tablet is key_after the
    one before (1829 of 1839 neighbouring pages of the library this was built from),
    and the rest are keys_between or a jump after another author's page."""

    def test_pages_added_at_the_end_count_up_from_ba(self):
        self.assertEqual(keys_after("a", 4), ["ba", "bb", "bc", "bd"])
        self.assertEqual([key_after(k) for k in ("bz", "cz", "mz")], ["ca", "da", "na"])

    def test_keys_grow_when_their_first_half_runs_high(self):
        self.assertEqual(key_after("ny"), "nza")         # "nz", all of its first half n or later: one longer
        self.assertEqual(key_after("zz"), "zzaa")        # no letter to step up: "a" added, then grown
        self.assertEqual(key_after("ona"), "onba")
        self.assertEqual(key_after("onba"), "onbb")

    def test_a_jump_leaves_room_after_another_authors_page(self):
        self.assertEqual(jump("ba"), "ona")
        self.assertEqual(key_after(jump("ba")), "onba")  # as found after a page by another device
        self.assertEqual(jump("ona"), "pnna")

    def test_keys_between_two_pages(self):
        self.assertEqual(keys_between("ba", "bb", 3), ["bab", "bac", "bad"])
        self.assertEqual(keys_between("opfx", "opfy", 1), ["opfxb"])     # as found in a real file
        self.assertEqual(keys_between("a", "ba", 1), ["aab"])            # before the first page
        self.assertEqual(keys_between("bb", "ba", 1), [])                # no room the wrong way round
        self.assertEqual(keys_between("ba", "baa", 1), [])               # "baa" counts as "ba"
        for a, b, n in (("ba", "bb", 30), ("ba", "ca", 5), ("aab", "ba", 2), ("onbz", "oobp", 4)):
            keys = keys_between(a, b, n)
            self.assertEqual(len(keys), n)
            self.assertEqual(keys, sorted(keys))
            self.assertTrue(all(a < k < b for k in keys), (a, b, keys))

    def test_not_before(self):
        self.assertTrue(not_before("bb", "ba"))
        self.assertTrue(not_before("ba", "ba"))
        self.assertTrue(not_before("ba", "baa"))         # trailing "a"s: the same place
        self.assertFalse(not_before("ba", "bab"))
        self.assertFalse(not_before("ba", "bb"))


class PageMapTests(unittest.TestCase):
    def pagemap(self, *entries, uuids=None) -> PageMap:
        return PageMap(content_of(*entries, uuids=uuids), OURS)

    def three(self) -> PageMap:
        return self.pagemap(entry(P[0], "ba"), entry(P[1], "bb"), entry(P[2], "bc"))

    def test_a_new_author_gets_the_next_index_listed_first(self):
        pm = self.three()
        self.assertEqual(pm.author, 2)
        self.assertEqual(pm.cpages["uuids"][0], {"first": OURS_UUID, "second": 2})
        self.assertEqual(PageMap(pm.content, OURS).author, 2)           # found the next time
        # Without a uuids list, the indices in the timestamps are still not reused.
        self.assertEqual(self.pagemap(entry(P[0], "ba", author=3), uuids=[]).author, 4)

    def test_insert_at_the_end_as_the_app_writes_a_new_page(self):
        pm = self.three()
        self.assertEqual(pm.insert(3, P[5], "Blank"), 3)
        new = pm.live[3]
        # Each field written once (counter 1) then counted once more by the normal insert
        # mode: the "A:2" every tablet-added page in the library has.
        self.assertEqual(new, {"id": P[5], "template": {"timestamp": "2:2", "value": "Blank"},
                               "idx": {"timestamp": "2:2", "value": key_after(jump(jump("bc")))}})
        self.assertNotIn("redir", new)
        self.assertNotIn("modifed", new)
        self.assertEqual(pm.ids(), [P[0], P[1], P[2], P[5]])

    def test_insert_after_our_own_page_does_not_jump(self):
        pm = self.pagemap(entry(P[0], "ba", author=2), uuids=[{"first": OURS_UUID, "second": 2}])
        pm.insert(5, P[5])                                               # clamped to the end
        self.assertEqual(pm.live[1]["idx"], {"timestamp": "2:2", "value": "bb"})
        self.assertNotIn("template", pm.live[1])

    def test_insert_at_the_front_and_in_the_middle(self):
        pm = self.three()
        pm.insert(0, P[5])
        pm.insert(2, P[6], "P Lines small", redirect=4)
        self.assertEqual(pm.ids(), [P[5], P[0], P[6], P[1], P[2]])
        self.assertEqual(pm.live[0]["idx"]["value"], "aab")
        self.assertEqual(pm.live[2]["idx"]["value"], "bab")
        self.assertEqual(pm.live[2]["redir"], {"timestamp": "2:2", "value": 4})
        keys = [e["idx"]["value"] for e in pm.live]
        self.assertEqual(keys, sorted(keys))
        with self.assertRaises(PageMapError):
            pm.insert(0, P[5])                                           # already a page of it

    def test_pages_left_without_room_are_keyed_again(self):
        # Two devices put pages at the same key: inserting after the first keys the
        # crowded one again, with the writer's counter one more.
        pm = self.pagemap(entry(P[0], "ba"), entry(P[1], "ba"), entry(P[2], "bb"))
        pm.insert(1, P[5])
        self.assertEqual(pm.ids()[:2], [P[0], P[5]])
        crowded = next(e for e in pm.live if e["id"] == P[1])
        self.assertEqual(crowded["idx"]["timestamp"], "2:3")
        keys = [e["idx"]["value"] for e in pm.live]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), 4)

    def test_move_keys_the_moved_pages_between_their_new_neighbours(self):
        pm = self.pagemap(*(entry(P[n], k) for n, k in enumerate(["ba", "bb", "bc", "bd", "be"])))
        self.assertTrue(pm.move([0, 1], 4))                              # before the last page
        self.assertEqual(pm.ids(), [P[2], P[3], P[0], P[1], P[4]])
        moved = {e["id"]: e["idx"] for e in pm.live}
        self.assertEqual([moved[P[0]]["value"], moved[P[1]]["value"]], ["bdb", "bdc"])
        self.assertEqual(moved[P[0]]["timestamp"], "2:3")               # counter one more, our index
        self.assertEqual(moved[P[2]]["timestamp"], "1:2")               # pages not moved are not written
        self.assertTrue(pm.move([4], 0))
        self.assertEqual(pm.ids()[0], P[4])
        self.assertTrue(pm.move([0], 5))                                 # to the end: keys after the last
        self.assertEqual(pm.ids()[-1], P[4])
        self.assertFalse(pm.move([], 1))
        self.assertFalse(pm.move([9], 1))

    def test_delete_marks_the_page_and_lists_it_after_the_live_ones(self):
        pm = self.three()
        self.assertEqual(pm.delete(1), P[1])
        content = pm.save()
        pages = content["cPages"]["pages"]
        self.assertEqual([p["id"] for p in pages], [P[0], P[2], P[1]])
        self.assertEqual(pages[2]["deleted"], {"timestamp": "2:1", "value": 1})   # as the tablet's "1:1"
        self.assertEqual(pages[2]["idx"], {"timestamp": "1:2", "value": "bb"})     # keeps its place
        self.assertEqual(content["pageCount"], 2)
        self.assertTrue(pm.known(P[1]))
        with self.assertRaises(PageMapError):
            pm.delete(5)

    def test_saved_layout_is_the_one_real_files_have(self):
        # Live pages by key, then deleted pages by id: the layout of every file of the library.
        pm = self.pagemap(entry(P[3], "bb"), {**entry(P[9], "ba"), "deleted": {"timestamp": "1:1", "value": 1}},
                          entry(P[1], "ba"), {**entry(P[0], "bc"), "deleted": {"timestamp": "1:1", "value": 1}})
        pm.insert(2, P[5])
        pages = pm.save()["cPages"]["pages"]
        self.assertEqual([p["id"] for p in pages], [P[1], P[3], P[5], P[0], P[9]])
        reparsed = json.loads(json_bytes(pm.content))
        self.assertEqual(reparsed["cPages"]["pages"], pages)

    def test_a_page_deleted_and_put_back_is_live(self):
        # Found in a real library: deleted on one device ("1"), put back on another ("0").
        back = {**entry(P[1], "bb"), "deleted": {"timestamp": "2:2", "value": 0}}
        gone = {**entry(P[2], "ba"), "deleted": {"timestamp": "1:1", "value": 1}}
        pm = self.pagemap(entry(P[0], "ba"), back, gone)
        self.assertEqual(pm.ids(), [P[0], P[1]])
        content = Content.parse(content_of(entry(P[0], "ba"), back, gone))
        self.assertEqual((content.page_ids, content.page_count), ([P[0], P[1]], 2))

    def test_the_unknown_fields_of_a_page_are_kept(self):
        pm = self.pagemap(entry(P[0], "ba", modifed="123", scrollTime={"timestamp": "1:1", "value": "x"}),
                          entry(P[1], "bb"))
        pm.move([0], 2)
        moved = pm.live[1]
        self.assertEqual((moved["id"], moved["modifed"], moved["scrollTime"]), (P[0], "123", {"timestamp": "1:1", "value": "x"}))


def image_page(key: bytes, file_name: str) -> bytes:
    return rm_writer.append_image(drawn_page(), author=TABLET, key=key, file_name=file_name, vertices=box())


class ImagePageBase(Base):
    """The throwaway notebook with an image on its first page, an image file and a page
    metadata file, as tablet pages have them."""

    KEY = bytes(range(32, 48))
    FILE = "cccccccc-0000-4000-8000-000000000001.png"

    def setUp(self):
        super().setUp()
        content = notebook_content(PAGE1, PAGE2)
        content["cPages"]["uuids"] = [{"first": TABLET_UUID, "second": 1}]
        self.cloud.put_item(THROWAWAY, meta("throwaway"), content,
                            {f"{THROWAWAY}/{PAGE1}.rm": image_page(self.KEY, self.FILE),
                             f"{THROWAWAY}/{PAGE1}/{self.FILE}": self.image,
                             f"{THROWAWAY}/{PAGE1}-metadata.json": b'{"layers": [{"name": "Layer 1"}]}\n'})
        self.tree = self.source.refresh()
        self.doc = self.tree.get(THROWAWAY)



class PageEditTests(ImagePageBase):
    """Pages added, duplicated, moved and deleted: kept as pending edits, shown at once
    (LibrarySource.view) and uploaded by push_edits, as reMarkable.exe's DocumentWorker
    changes a document."""

    def staged(self) -> Staged:
        return self.source.staged(self.doc)

    def files(self) -> dict:
        return self.sync.staged_files(self.doc)

    def content(self, files=None) -> dict:
        files = self.files() if files is None else files
        return json.loads(files[f"{THROWAWAY}.content"])

    def live(self, content: dict) -> list[str]:
        return [p["id"] for p in sorted(content["cPages"]["pages"], key=lambda p: p["idx"]["value"])
                if "deleted" not in p]

    # -- add --

    def test_an_added_page_changes_only_content_and_metadata(self):
        edit = self.source.add_page(self.doc, 1, "Blank", NOW)
        files = self.files()
        self.assertEqual(sorted(files), [f"{THROWAWAY}.content", f"{THROWAWAY}.metadata"])     # no .rm
        content = self.content(files)
        self.assertEqual(self.live(content), [PAGE1, edit.new_id, PAGE2])
        self.assertEqual(content["pageCount"], 3)
        self.assertEqual(content["cPages"]["uuids"][0]["second"], 2)
        old = json.loads(self.source.blob(self.doc, f"{THROWAWAY}.content"))
        changes = json_changes(old, content)
        self.assertIn((f"cPages.pages[id={edit.new_id}]", None,
                       {"id": edit.new_id, "template": {"timestamp": "2:2", "value": "Blank"},
                        "idx": {"timestamp": "2:2", "value": "bab"}}), changes)
        self.assertIn(("pageCount", 2, 3), changes)
        self.assertEqual(json.loads(files[f"{THROWAWAY}.metadata"])["lastModified"], str(NOW))
        self.assertEqual((edit.index, edit.after, edit.template), (1, PAGE1, "Blank"))

    def test_an_added_page_is_shown_and_can_take_an_image(self):
        edit = self.source.add_page(self.doc, -1, "P Lines small", NOW)                 # -1: at the end
        view = self.source.view(self.doc)
        self.assertEqual(view.content.page_ids, [PAGE1, PAGE2, edit.new_id])
        self.assertEqual((view.page_count, view.content.pages[2].template), (3, "P Lines small"))
        # Its thumbnails are not the old ones: QML's thumbnail URLs carry the first 16 characters.
        self.assertNotEqual(view.hash[:16], self.doc.hash[:16])
        new_page = document_pages(view)[2]
        self.assertIsNone(self.source.page_scene(view, new_page))      # empty paper: no .rm
        image = self.insert(page=edit.new_id, doc=view)
        files = self.files()
        self.assertEqual(page_images(files[f"{THROWAWAY}/{edit.new_id}.rm"]), {bytes.fromhex(image.key): image.file_name})
        self.assertEqual(files[image.path], self.image)
        self.assertEqual(self.content(files)["cPages"]["pages"][2]["modifed"], str(NOW))
        [placed] = self.source.page_scene(view, new_page).placed_images
        self.assertEqual(placed.data, self.image)

    def test_add_page_takes_a_given_uuid_once(self):
        mine = "dddddddd-0000-4000-8000-000000000001"
        self.assertEqual(self.source.add_page(self.doc, 0, "Blank", NOW, mine).new_id, mine)
        self.assertNotEqual(self.source.add_page(self.doc, 0, "Blank", NOW, mine).new_id, mine)
        self.assertNotEqual(self.source.add_page(self.doc, 0, "Blank", NOW, PAGE1).new_id, PAGE1)

    # -- duplicate --

    def test_a_duplicate_copies_the_page_files_under_a_new_id(self):
        edit = self.source.copy_pages(self.doc, [0], 1, NOW)
        [copy] = edit.new_ids
        files = self.files()
        self.assertEqual(files[f"{THROWAWAY}/{copy}.rm"], self.source.blob(self.doc, f"{THROWAWAY}/{PAGE1}.rm"))
        self.assertEqual(files[f"{THROWAWAY}/{copy}/{self.FILE}"], self.image)
        self.assertEqual(files[f"{THROWAWAY}/{copy}-metadata.json"],
                         self.source.blob(self.doc, f"{THROWAWAY}/{PAGE1}-metadata.json"))
        self.assertNotIn(f"{THROWAWAY}/{PAGE1}.rm", files)              # the original is untouched
        content = self.content(files)
        self.assertEqual(self.live(content), [PAGE1, copy, PAGE2])
        entry_ = next(p for p in content["cPages"]["pages"] if p["id"] == copy)
        self.assertEqual(entry_["template"], {"timestamp": "2:2", "value": "Blank"})  # the source's template
        self.assertNotIn("modifed", entry_)

    def test_a_duplicate_has_the_edits_made_before_it_only(self):
        before = self.insert(PAGE1)
        [copy] = self.source.copy_pages(self.doc, [0], 2, NOW + 1).new_ids
        after = self.insert(PAGE1, when=NOW + 2)
        files = self.files()
        self.assertEqual(set(page_images(files[f"{THROWAWAY}/{copy}.rm"]).values()), {self.FILE, before.file_name})
        self.assertEqual(set(page_images(files[f"{THROWAWAY}/{PAGE1}.rm"]).values()),
                         {self.FILE, before.file_name, after.file_name})
        self.assertEqual(files[f"{THROWAWAY}/{copy}/{before.file_name}"], self.image)
        self.assertEqual(self.live(self.content(files)), [PAGE1, PAGE2, copy])
        # Shown: the copy with its images.
        view = self.source.view(self.doc)
        scene = self.source.page_scene(view, document_pages(view)[2])
        self.assertEqual(sorted(i.file_name for i in scene.placed_images), sorted([self.FILE, before.file_name]))
        self.assertTrue(all(i.data == self.image for i in scene.placed_images))

    def test_duplicating_several_pages_keeps_their_order(self):
        edit = self.source.copy_pages(self.doc, [1, 0], 2, NOW)
        self.assertEqual(self.live(self.content()), [PAGE1, PAGE2, *edit.new_ids])
        self.assertEqual(edit.sources, (PAGE1, PAGE2))

    # -- move --

    def test_a_move_changes_only_the_page_order(self):
        self.source.move_pages(self.doc, [0], 2, NOW)
        files = self.files()
        self.assertEqual(sorted(files), [f"{THROWAWAY}.content", f"{THROWAWAY}.metadata"])
        content = self.content(files)
        self.assertEqual(self.live(content), [PAGE2, PAGE1])
        old = json.loads(self.source.blob(self.doc, f"{THROWAWAY}.content"))
        fields = {f for f, _, _ in json_changes(old, content)}
        self.assertEqual(fields, {f"cPages.pages[id={PAGE1}].idx.timestamp", f"cPages.pages[id={PAGE1}].idx.value",
                                  "cPages.uuids", "cPages.pages (order of entries)"})
        self.assertEqual(self.source.view(self.doc).content.page_ids, [PAGE2, PAGE1])

    # -- delete --

    def test_a_deleted_page_stays_listed_and_its_files_go(self):
        self.source.delete_pages(self.doc, [0], NOW)
        files = self.files()
        for name in (f"{THROWAWAY}/{PAGE1}.rm", f"{THROWAWAY}/{PAGE1}/{self.FILE}", f"{THROWAWAY}/{PAGE1}-metadata.json"):
            self.assertIsNone(files[name], name)
        content = self.content(files)
        self.assertEqual((self.live(content), content["pageCount"]), ([PAGE2], 1))
        gone = content["cPages"]["pages"][-1]
        self.assertEqual((gone["id"], gone["deleted"]), (PAGE1, {"timestamp": "2:1", "value": 1}))

    def test_the_edits_of_a_deleted_page_are_dropped(self):
        self.insert(PAGE1)
        self.source.delete_pages(self.doc, [0], NOW + 1)
        files = self.files()
        self.assertEqual({n for n, d in files.items() if d is not None}, {f"{THROWAWAY}.content", f"{THROWAWAY}.metadata"})

    def test_deleting_every_page_leaves_a_blank_one(self):
        edit = self.source.delete_pages(self.doc, [0, 1], NOW)
        content = self.content()
        self.assertEqual(self.live(content), [edit.blank_id])
        blank = next(p for p in content["cPages"]["pages"] if p["id"] == edit.blank_id)
        self.assertEqual(blank["template"]["value"], "Blank")
        self.assertEqual(content["pageCount"], 1)

    # -- staging, refusing, uploading --

    def test_page_edits_are_kept_across_restarts(self):
        self.source.add_page(self.doc, 0, "Blank", NOW)
        self.source.copy_pages(self.doc, [1], 0, NOW)
        self.source.move_pages(self.doc, [2], 0, NOW)
        self.source.delete_pages(self.doc, [3], NOW)
        kept = PendingEdits(self.dir / "cache" / "pending").all()
        self.assertEqual(kept, self.sync.edits.all())
        self.assertEqual([type(e) for e in kept], [PageAdd, PageCopy, PageMove, PageDelete])

    def test_page_edits_of_a_document_not_editable_are_refused(self):
        other = self.tree.get(OTHER)
        for make in (lambda: self.source.add_page(other, 0, "Blank", NOW),
                     lambda: self.source.copy_pages(other, [0], 1, NOW),
                     lambda: self.source.move_pages(other, [0], 1, NOW),
                     lambda: self.source.delete_pages(other, [0], NOW)):
            with self.assertRaises(EditError):
                make()
        with self.assertRaises(EditError):
            self.source.move_pages(self.doc, [5], 0, NOW)              # no such page
        self.assertEqual(self.sync.edits.all(), [])

    def test_page_edits_go_after_their_page_on_a_newer_version(self):
        edit = self.source.add_page(self.doc, 1, "Blank", NOW)          # after PAGE1
        newer = notebook_content(PAGE2, PAGE1)                          # the tablet moved PAGE1 to the end
        newer["cPages"]["pages"][1]["idx"]["value"] = "bc"
        newer["cPages"]["uuids"] = [{"first": TABLET_UUID, "second": 1}]
        self.cloud.put_item(THROWAWAY, meta("throwaway"), newer, {f"{THROWAWAY}/{PAGE1}.rm": drawn_page()})
        self.sync.push_edits(THROWAWAY)
        content = json.loads(self.cloud.item(THROWAWAY)[f"{THROWAWAY}.content"])
        self.assertEqual(self.live(content), [PAGE2, PAGE1, edit.new_id])

    def test_push_uploads_the_page_edits_and_only_once(self):
        added = self.source.add_page(self.doc, 2, "Blank", NOW)
        [copy] = self.source.copy_pages(self.doc, [0], 0, NOW).new_ids
        plan = self.sync.push_plan(THROWAWAY)
        text = plan.describe()
        for part in ("add page " + added.new_id, "duplicate pages", copy, "pageCount"):
            self.assertIn(part, text)
        tree = self.sync.push_edits(THROWAWAY)
        item = self.cloud.item(THROWAWAY)
        content = json.loads(item[f"{THROWAWAY}.content"])
        self.assertEqual(self.live(content), [copy, PAGE1, PAGE2, added.new_id])
        self.assertEqual(item[f"{THROWAWAY}/{copy}.rm"], item[f"{THROWAWAY}/{PAGE1}.rm"])
        self.assertEqual(item[f"{THROWAWAY}/{copy}/{self.FILE}"], self.image)
        self.assertEqual(self.sync.edits.all(), [])
        self.assertEqual(tree.get(THROWAWAY).content.page_ids, [copy, PAGE1, PAGE2, added.new_id])
        # The same edits again change nothing: what the cloud has is not added twice.
        self.assertEqual(document_files(tree.get(THROWAWAY), [added, PageCopy.new(THROWAWAY, [PAGE1], 0, "", NOW, [copy])],
                                        lambda n: self.sync.get_blob(tree.get(THROWAWAY).files[n], n), lambda e: b"",
                                        self.sync.author()), {})


class DocumentControllerTests(Base):
    """The QML side: PageActions.qml calls DocumentController, whose slots make the
    pending edits and show the document again with its new page list."""

    def setUp(self):
        super().setUp()
        from ui.quick.bridge.library import Library
        from ui.quick.bridge.platform import DocumentController
        self.library = Library()
        self.library.set_tree(self.source.view_tree(self.tree))
        self.controller = DocumentController(self.library)
        self.messages: list[str] = []
        self.controller.source = lambda: self.source
        self.controller.refresh = lambda: self.library.set_tree(self.source.view_tree(self.sync.tree))
        self.controller.notify = self.messages.append
        self.entry = self.library.entryForId(THROWAWAY)

    def test_add_duplicate_move_delete_from_the_page_overview(self):
        added = []
        self.entry.pagesAdded.connect(added.append)
        self.assertTrue(self.controller.addPageWithTemplateAndPaperSizeFromPage(THROWAWAY, 2, "Blank", 1))
        self.assertEqual((self.entry.pageCount, added), (3, [[2]]))
        new_id = self.entry.idForPage(2)
        self.assertEqual(self.entry.templateForPage(2), "Blank")
        self.assertTrue(self.controller.copyPages(THROWAWAY, [0], THROWAWAY, 1))
        self.assertEqual(self.entry.pageCount, 4)
        self.assertTrue(self.controller.movePages(THROWAWAY, [3], THROWAWAY, 0))
        self.assertEqual(self.entry.idForPage(0), new_id)
        self.assertTrue(self.controller.deletePages(THROWAWAY, [0]))
        self.assertEqual(self.entry.pageCount, 3)
        self.assertEqual(self.entry.pageForId(new_id), -1)
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.sync.edits.all()), 4)

    def test_add_page_without_a_template_takes_the_one_before(self):
        self.assertTrue(self.controller.addPageWithTemplateAndPageSize(THROWAWAY))
        self.assertEqual(self.entry.templateForPage(2), "Blank")

    def test_what_cannot_be_done_is_said(self):
        self.assertFalse(self.controller.copyPages(THROWAWAY, [0], OTHER, 0))
        self.assertFalse(self.controller.deletePages(OTHER, [0]))
        self.assertFalse(self.controller.movePages(THROWAWAY, [7], THROWAWAY, 0))
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.sync.edits.all(), [])


# ------------------------------------------------------------------ document copies

class CloneTests(ImagePageBase):
    """Copy, then Paste, of a document (core.clone, SyncClient.clone_document): a new
    document with new page ids, its files the source's bytes, as the tablet's copies in
    a real library are."""

    def clone(self, name="throwaway copy", parent="", now=NOW, **kw):
        from core.clone import clone_files
        doc = kw.pop("doc", self.doc)
        ids = iter(f"eeeeeeee-0000-4000-8000-{n:012d}" for n in range(1, 99))
        return clone_files(doc.id, doc.files, lambda n: self.source.blob(doc, n), copy_id=COPY, name=name,
                           parent=parent, now_ms=now, author=self.sync.author(), new_page_id=lambda: next(ids))

    def test_the_copy_gets_new_ids_and_the_same_page_bytes(self):
        copied, written = self.clone()
        new1, new2 = "eeeeeeee-0000-4000-8000-000000000001", "eeeeeeee-0000-4000-8000-000000000002"
        self.assertEqual(copied, {f"{THROWAWAY}/{PAGE1}.rm": f"{COPY}/{new1}.rm",
                                  f"{THROWAWAY}/{PAGE1}/{self.FILE}": f"{COPY}/{new1}/{self.FILE}",
                                  f"{THROWAWAY}/{PAGE1}-metadata.json": f"{COPY}/{new1}-metadata.json"})
        self.assertEqual(sorted(written), [f"{COPY}.content", f"{COPY}.metadata"])
        content = json.loads(written[f"{COPY}.content"])
        cpages = content["cPages"]
        self.assertEqual([(p["id"], p["idx"], p["template"]) for p in cpages["pages"]],
                         [(new1, {"timestamp": "2:2", "value": "ba"}, {"timestamp": "2:2", "value": "Blank"}),
                          (new2, {"timestamp": "2:2", "value": "bb"}, {"timestamp": "2:2", "value": "Blank"})])
        self.assertEqual(cpages["lastOpened"], {"timestamp": "2:1", "value": new1})
        self.assertEqual(cpages["original"], {"timestamp": "2:1", "value": -1})
        self.assertEqual(cpages["uuids"][1:], [{"first": TABLET_UUID, "second": 1}])     # the source's authors
        self.assertEqual(content["pageCount"], 2)
        source = json.loads(self.source.blob(self.doc, f"{THROWAWAY}.content"))
        self.assertEqual({k: v for k, v in content.items() if k not in ("cPages",)},
                         {k: v for k, v in source.items() if k not in ("cPages",)})

    def test_the_copy_is_named_placed_and_new(self):
        _, written = self.clone(name="Mine", parent=OTHER)
        metadata = json.loads(written[f"{COPY}.metadata"])
        self.assertEqual({k: metadata[k] for k in ("visibleName", "parent", "createdTime", "lastModified",
                                                    "lastOpened", "lastOpenedPage", "pinned", "type")},
                         {"visibleName": "Mine", "parent": OTHER, "createdTime": str(NOW), "lastModified": str(NOW),
                          "lastOpened": "0", "lastOpenedPage": 0, "pinned": False, "type": "DocumentType"})
        from core.clone import copy_name
        self.assertEqual(copy_name("throwaway"), "throwaway copy")
        self.assertEqual(copy_name("Notes", "Copie %1"), "Copie Notes")        # as the French catalogue has it

    def test_deleted_pages_and_their_tags_stay_behind(self):
        self.source.delete_pages(self.doc, [1], NOW)
        self.sync.push_edits(THROWAWAY)
        doc = self.sync.tree.get(THROWAWAY)
        content = json.loads(self.source.blob(doc, f"{THROWAWAY}.content"))
        content["pageTags"] = [{"name": "a", "pageId": PAGE1, "timestamp": 1}, {"name": "b", "pageId": PAGE2, "timestamp": 1}]
        self.cloud.put_item(THROWAWAY, meta("throwaway"), content,
                            {n: data for n, data in self.cloud.item(THROWAWAY).items()
                             if not n.endswith((".content", ".metadata"))})
        doc = self.source.refresh().get(THROWAWAY)
        _, written = self.clone(doc=doc)
        copy = json.loads(written[f"{COPY}.content"])
        self.assertEqual(len(copy["cPages"]["pages"]), 1)
        self.assertEqual(copy["pageTags"], [{"name": "a", "pageId": "eeeeeeee-0000-4000-8000-000000000001", "timestamp": 1}])

    def test_paste_uploads_only_the_new_files(self):
        copy_id = self.sync.clone_document(THROWAWAY, "", "throwaway copy", COPY)
        self.assertEqual(copy_id, COPY)
        uploaded = [w for w in self.writes() if not w.endswith(".docSchema") and w != "/sync/v3/root"]
        self.assertEqual(sorted(uploaded), [f"{COPY}.content", f"{COPY}.metadata"])     # the rest by hash
        item = self.cloud.item(COPY)
        source = self.cloud.item(THROWAWAY)
        page = next(n for n in item if n.endswith(".rm"))
        self.assertEqual(item[page], source[f"{THROWAWAY}/{PAGE1}.rm"])
        self.assertEqual(item[f"{page[:-3]}/{self.FILE}"], self.image)
        tree = self.source.refresh()
        copy = tree.get(COPY)
        self.assertEqual((copy.name, copy.parent_id, copy.page_count), ("throwaway copy", "", 2))
        self.assertFalse(set(copy.content.page_ids) & set(tree.get(THROWAWAY).content.page_ids))
        self.assertEqual(tree.get(THROWAWAY).hash, self.doc.hash)                          # the source is untouched

    def test_a_copy_has_the_pending_edits_of_its_source(self):
        image = self.insert(PAGE1)
        added = self.source.add_page(self.doc, 2, "Blank", NOW)
        self.sync.clone_document(THROWAWAY, "", "throwaway copy", COPY)
        item = self.cloud.item(COPY)
        content = json.loads(item[f"{COPY}.content"])
        self.assertEqual(content["pageCount"], 3)
        first = content["cPages"]["pages"][0]["id"]
        self.assertEqual(set(page_images(item[f"{COPY}/{first}.rm"]).values()), {self.FILE, image.file_name})
        self.assertEqual(item[f"{COPY}/{first}/{image.file_name}"], self.image)
        self.assertEqual(len(self.sync.edits.of(THROWAWAY)), 2)              # still pending on the source
        self.assertNotEqual(content["cPages"]["pages"][2]["id"], added.new_id)   # a copy's pages are its own

    def test_a_copy_cannot_go_into_a_folder_that_is_gone(self):
        with self.assertRaises(SyncError):
            self.sync.clone_document(THROWAWAY, "ffffffff-0000-4000-8000-000000000000", "x", COPY)
        self.assertIsNone(self.source.refresh().get(COPY))


COPY = "cccccccc-1111-4111-8111-111111111111"


if __name__ == "__main__":
    unittest.main()
