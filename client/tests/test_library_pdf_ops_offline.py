"""Offline unit and QML integration tests for pages copied and moved between documents,
clean copies, and password-protected PDFs (synthetic ids and synthetic PDFs only):

- core.edits.PageImport / LibrarySource.import_pages, move_pages_to: reMarkable.exe's
  CopyExternalPagesTask (doCopyExternalPages 0x140559b20): new page ids, the source
  template, no PDF page (redirection -1), the page files byte for byte as they were when
  copied (kept in the pending files), the target's own PDF page map untouched; a move is
  that and a delete in the source. Persistence, discard, push plan.
- core.pagemap.clean / core.clone clean copies (Document::clone with the clean option):
  a fresh page list from the PDF page count, no page files, no pending edits; notebooks
  refused.
- ui.pdflock + ui.pages.PdfFile + export: locked PDFs, unlock in memory only, wrong
  passwords, export with and without "keep password protection".
- QML: DocumentController.copyPages/movePages to another document, LibraryController.
  cloneEntry(..., true), and main.qml's own requestOpenDocument -> PasswordDialog ->
  unlockWithPassword flow (wrong password, then the right one, then the document opens).

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_library_pdf_ops_offline.py'
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import QMetaObject, QObject, Q_ARG, QUrl  # noqa: E402

import test_edits_offline as te  # noqa: E402
import test_pdf_edit_offline as tp  # noqa: E402
from core import clone  # noqa: E402
from core.edits import (EditError, PageDelete, PageImport, PendingEdits, document_files,  # noqa: E402
                        import_path)
from core.pagemap import PageMap, PageMapError, clean as clean_pages  # noqa: E402
from core.sync import SyncClient, SyncError, describe_edit  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from ui import pdflock  # noqa: E402
from ui.library import LibrarySource, document_pages  # noqa: E402
from ui.pages import DocumentPages, PdfFile  # noqa: E402
from viewer import rm_stroke, rm_writer as w  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

TABLET = bytes(range(16))
TABLET_UUID = "00010203-0405-0607-0809-0a0b0c0d0e0f"
ME = bytes.fromhex("a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
NOTE, NOTE2 = te.THROWAWAY, "88888888-1111-4111-8111-888888888888"     # editable notebooks
P1, P2, Q1 = te.PAGE1, te.PAGE2, "aaaaaaaa-0000-4000-8000-0000000000aa"
PDF_DOC, EPUB_DOC, V1_DOC = tp.PDF_DOC, tp.EPUB_DOC, tp.V1_DOC
A4, LETTER, BLANK, SLIDE = tp.A4, tp.LETTER, tp.BLANK, tp.SLIDE
SECRET_DOC = "99999999-2222-4222-8222-999999999999"
OPEN_DOC = "99999999-3333-4333-8333-999999999999"      # encrypted, empty user password
PASSWORD = "synthetic-pass-1"                          # made up for the tests
KEY = bytes(range(0x40, 0x50))
IMAGE = "12121212-3434-4565-8787-909090909090.png"
NOW = tp.NOW
EDITABLE = ("throwaway", "Second notebook", tp.NAMES[0], tp.NAMES[1])


def encrypted_pdf(user: str = PASSWORD, owner: str = "synthetic-owner") -> bytes:
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(tp.make_pdf())))
    writer.encrypt(user_password=user, owner_password=owner, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def note_page() -> bytes:
    """A notebook page with a stroke and a picture."""
    page = te.drawn_page()
    return w.append_image(page, author=TABLET, key=KEY, file_name=IMAGE,
                          vertices=[(-50.0, 100.0, 0.0, 0.0), (50.0, 100.0, 1.0, 0.0),
                                    (50.0, 175.0, 1.0, 1.0), (-50.0, 175.0, 0.0, 1.0)])


def build_cloud() -> FakeCloud:
    cloud = tp.build_cloud()
    content = te.notebook_content(P1, P2)
    content["cPages"]["pages"][1]["template"]["value"] = "P Grid medium"
    cloud.put_item(NOTE, te.meta("throwaway"), content,
                   {f"{NOTE}/{P1}.rm": note_page(), f"{NOTE}/{P1}/{IMAGE}": te.png(),
                    f"{NOTE}/{P1}-metadata.json": b'{\n    "layers": [\n    ]\n}\n',
                    f"{NOTE}.thumbnails/{P1}.png": te.png(8, 8)})
    cloud.put_item(NOTE2, te.meta("Second notebook"), te.notebook_content(Q1), {f"{NOTE2}/{Q1}.rm": te.drawn_page()})
    secret = tp.pdf_content("pdf", [tp.page_entry(f"eeeeeeee-0000-4000-8000-00000000000{n}", "b" + "abc"[n], n)
                                    for n in range(3)], 3)
    cloud.put_item(SECRET_DOC, tp.meta("Secret paper"), secret, {f"{SECRET_DOC}.pdf": encrypted_pdf()})
    cloud.put_item(OPEN_DOC, tp.meta("Owner-locked paper"), json.loads(json.dumps(secret)),
                   {f"{OPEN_DOC}.pdf": encrypted_pdf(user="")})
    return cloud


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(pdflock.forget)
        self.dir = Path(self.tmp.name)
        self.cloud = build_cloud()
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=EDITABLE)
        self.source.refresh()

    def doc(self, doc_id: str):
        return self.sync.tree.get(doc_id)

    def ids(self, doc_id: str) -> list[str]:
        return list(self.source.staged(self.doc(doc_id)).ids)

    def entries(self, doc_id: str) -> dict[str, dict]:
        staged = self.source.staged(self.doc(doc_id))
        content = staged.content if staged.changes_page_list else json.loads(
            self.source.blob(self.doc(doc_id), f"{doc_id}.content"))
        return {e["id"]: e for e in content["cPages"]["pages"]}

    def files(self, doc_id: str) -> dict:
        return self.sync.staged_files(self.doc(doc_id), cached_only=False)


# ------------------------------------------------------------------ 1. the edit and the clean page map

class EditAndPageMapTests(unittest.TestCase):
    def test_a_page_import_round_trips_and_names_only_page_files(self):
        sha = "ab" * 32
        edit = PageImport.new(NOTE, NOTE2, [Q1], ["Blank"], [[("{doc}/{page}.rm", sha, 3),
                                                             ("{doc}/{page}/pic.png", sha, 3)]], 1, P1, NOW)
        raw = json.loads(json.dumps({"kind": "page-import", **edit.__dict__}))
        self.assertEqual(PageImport.load(raw), edit)
        self.assertEqual(edit.hashes(), {sha})
        self.assertEqual(import_path("{doc}.thumbnails/{page}.png", "D", "P"), "D.thumbnails/P.png")
        self.assertEqual(import_path("{doc}/{page}/pic.png", "D", "P"), "D/P/pic.png")
        for bad in ("{doc}/{page}/../x", "{doc}/{page}/", "{doc}/../etc", "{doc}/{page}/a/b"):
            with self.assertRaises(EditError):
                import_path(bad, "D", "P")
        with self.assertRaises(EditError):
            PageImport.new(NOTE, NOTE, [Q1], [""], [[]], 0, "", NOW)          # within a document: a PageCopy
        with self.assertRaises(EditError):
            PageImport.new(NOTE, NOTE2, [Q1], [""], [[("{doc}/{page}.rm", "not-a-hash", 1)]], 0, "", NOW)

    def test_a_clean_page_map_is_made_afresh_from_the_pdf_page_count(self):
        ids = iter(f"cccccccc-0000-4000-8000-00000000000{n}" for n in range(9))
        source = tp.pdf_content("pdf", [tp.page_entry(A4, "ba", 0), tp.page_entry(BLANK, "bb", None, "P Lines small")], 3)
        source["pageTags"] = [{"name": "x", "pageId": A4, "timestamp": 1}]
        out = clean_pages(source, ME, 3, lambda: next(ids))
        cpages = out["cPages"]
        self.assertEqual(cpages["uuids"], [{"first": "a0a1a2a3-a4a5-a6a7-a8a9-aaabacadaeaf", "second": 1}])
        self.assertEqual(cpages["original"], {"timestamp": "1:1", "value": 3})
        self.assertEqual(cpages["lastOpened"], {"timestamp": "0:0", "value": ""})
        self.assertEqual([(e["idx"], e["redir"], "template" in e) for e in cpages["pages"]],
                         [({"timestamp": "1:2", "value": k}, {"timestamp": "1:3", "value": n}, False)
                          for n, k in enumerate(("ba", "bb", "bc"))])
        self.assertEqual((out["pageCount"], out["formatVersion"], "pageTags" in out), (3, 2, False))
        self.assertEqual(out["tags"], source["tags"])
        self.assertEqual(source["cPages"]["pages"][0]["id"], A4)               # the source is left alone
        legacy = clean_pages(tp.v1_content(), ME, 2, lambda: next(ids))
        self.assertFalse({"pages", "redirectionPageMap", "originalPageCount"} & set(legacy))
        with self.assertRaises(PageMapError):
            clean_pages(source, ME, 0, lambda: next(ids))


# ------------------------------------------------------------------ 2. pages between documents (LibrarySource)

class ImportPagesTests(Base):
    def test_notebook_pages_arrive_with_their_files_template_and_no_pdf_page(self):
        stroke = rm_stroke.draw([(0.0, 300.0), (100.0, 320.0)], rm_stroke.FINELINER_V2)
        self.source.add_stroke(self.doc(NOTE), P1, stroke, NOW)          # a pending edit of the source page
        shown = self.source.staged(self.doc(NOTE)).page(P1)
        edit, data = self.source.import_pages(self.doc(NOTE2), self.doc(NOTE), [1, 0], 1, NOW + 1)
        self.assertEqual(edit.sources, (P1, P2))                         # in page order
        self.assertEqual(edit.templates, ("Blank", "P Grid medium"))
        self.assertEqual(self.ids(NOTE2), [Q1, *edit.new_ids])
        new1, new2 = edit.new_ids
        files = self.files(NOTE2)
        self.assertEqual(files[f"{NOTE2}/{new1}.rm"], shown)            # with the source's pending stroke
        self.assertEqual(files[f"{NOTE2}/{new1}/{IMAGE}"], te.png())
        self.assertEqual(files[f"{NOTE2}/{new1}-metadata.json"], b'{\n    "layers": [\n    ]\n}\n')
        self.assertEqual(files[f"{NOTE2}.thumbnails/{new1}.png"], te.png(8, 8))
        self.assertNotIn(f"{NOTE2}/{new2}.rm", files)                   # P2 had no .rm
        entries = self.entries(NOTE2)
        for new_id, template in ((new1, "Blank"), (new2, "P Grid medium")):
            self.assertNotIn("redir", entries[new_id])
            self.assertEqual(entries[new_id]["template"]["value"], template)
            self.assertTrue(entries[new_id]["idx"]["timestamp"].endswith(":2"))
        self.assertEqual(json.loads(files[f"{NOTE2}.metadata"])["lastModified"], str(NOW + 1))
        # The source is read, not written; its kept files are the pending files' own.
        self.assertEqual([type(e).__name__ for e in self.sync.edits.of(NOTE)], ["StrokeAdd"])
        self.assertEqual({sha for sha in data}, edit.hashes())
        # Later edits of the source page do not reach the copy: it is the page as it was.
        self.source.add_stroke(self.doc(NOTE), P1, rm_stroke.draw([(0.0, 900.0), (90.0, 910.0)],
                                                                  rm_stroke.FINELINER_V2), NOW + 2)
        self.assertEqual(self.files(NOTE2)[f"{NOTE2}/{new1}.rm"], shown)

    def test_pdf_pages_arrive_as_their_ink_without_the_pdf(self):
        pdf = self.doc(PDF_DOC)
        edit, _ = self.source.import_pages(self.doc(NOTE), pdf, [1, 2], -1, NOW)     # -1: at the end
        self.assertEqual(self.ids(NOTE)[-2:], list(edit.new_ids))
        letter, inserted = edit.new_ids
        self.assertEqual(self.files(NOTE)[f"{NOTE}/{letter}.rm"], tp.annotated_pdf_page())   # SceneInfo field 6 kept
        entries = self.entries(NOTE)
        self.assertNotIn("redir", entries[letter])
        self.assertEqual(entries[letter]["template"]["value"], "Blank")
        self.assertEqual(entries[inserted]["template"]["value"], "P Lines small")
        pages = document_pages(self.source.view(self.doc(NOTE)))
        self.assertEqual([p.pdf_page for p in pages][-2:], [None, None])
        self.assertEqual(self.sync.edits.of(PDF_DOC), [])

    def test_notebook_pages_into_a_pdf_leave_its_pdf_page_map_alone(self):
        before = self.entries(PDF_DOC)
        edit, _ = self.source.import_pages(self.doc(PDF_DOC), self.doc(NOTE), [0], 1, NOW)
        after = self.entries(PDF_DOC)
        self.assertEqual(self.ids(PDF_DOC)[1], edit.new_ids[0])
        for page_id in (A4, LETTER, SLIDE):
            self.assertEqual(after[page_id].get("redir"), before[page_id].get("redir"))
        self.assertNotIn("redir", after[edit.new_ids[0]])
        content = json.loads(self.files(PDF_DOC)[f"{PDF_DOC}.content"])
        self.assertEqual(content["cPages"]["original"], {"timestamp": "1:1", "value": 3})
        self.assertEqual(sorted(e["redir"]["value"] for e in content["cPages"]["pages"] if "redir" in e), [0, 1, 2])
        self.assertEqual([p.pdf_page for p in document_pages(self.source.view(self.doc(PDF_DOC)), 3)],
                         [0, None, 1, None, 2])

    def test_a_formatversion_1_pdf_is_upgraded_when_pages_go_into_it(self):
        edit, _ = self.source.import_pages(self.doc(V1_DOC), self.doc(NOTE2), [0], 99, NOW)  # past the end: last
        content = json.loads(self.files(V1_DOC)[f"{V1_DOC}.content"])
        self.assertEqual(content["formatVersion"], 2)
        self.assertEqual([e["id"] for e in content["cPages"]["pages"]], [tp.L0, tp.L1, edit.new_ids[0]])

    def test_refusals_leave_nothing_behind(self):
        with self.assertRaises(EditError):                                 # the target may not be edited
            self.source.import_pages(self.doc(EPUB_DOC), self.doc(NOTE), [0], 0, NOW)
        with self.assertRaises(EditError):
            self.source.import_pages(self.doc(NOTE), self.doc(NOTE2), [5], 0, NOW)
        with self.assertRaises(EditError):
            self.source.import_pages(self.doc(NOTE), self.doc(NOTE), [0], 0, NOW)
        with self.assertRaises(EditError):                                 # a move needs the source editable too
            self.source.move_pages_to(self.doc(NOTE), self.doc(EPUB_DOC), [0], 0, NOW)
        self.assertEqual(self.sync.edits.all(), [])
        self.assertFalse((self.sync.edits.root / "files").exists() and any((self.sync.edits.root / "files").iterdir()))

    def test_a_move_copies_in_then_deletes_from_the_source(self):
        shown = self.source.staged(self.doc(NOTE)).page(P1)
        imported, delete = self.source.move_pages_to(self.doc(NOTE2), self.doc(NOTE), [0], 0, NOW)
        self.assertIsInstance(delete, PageDelete)
        self.assertEqual(self.ids(NOTE), [P2])
        self.assertEqual(self.ids(NOTE2), [imported.new_ids[0], Q1])
        self.assertEqual(self.files(NOTE2)[f"{NOTE2}/{imported.new_ids[0]}.rm"], shown)
        removed = {n for n, d in self.files(NOTE).items() if d is None}
        self.assertIn(f"{NOTE}/{P1}.rm", removed)
        self.assertIn(f"{NOTE}/{P1}/{IMAGE}", removed)
        # Every page moved out: the source gets a new "Blank" page, as doDeletePages does.
        _, last = self.source.move_pages_to(self.doc(NOTE2), self.doc(NOTE), [0], 0, NOW + 1)
        self.assertEqual(self.ids(NOTE), [last.blank_id])

    def test_imports_persist_and_their_files_go_with_them(self):
        edit, _ = self.source.import_pages(self.doc(NOTE2), self.doc(NOTE), [0], 1, NOW)
        restarted = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                  editable=EDITABLE, cached_only=True)
        doc = restarted.load_cached().get(NOTE2)
        page = document_pages(restarted.view(doc))[1]
        self.assertEqual(page.page_id, edit.new_ids[0])
        self.assertEqual(len(restarted.page_scene(doc, page).layers[0].images), 1)
        plan = "\n".join(describe_edit(e) for e in self.sync.edits.of(NOTE2))
        self.assertIn(f"pages copied in from document {NOTE}", plan)
        kept = self.sync.edits.root / "files"
        self.assertEqual({p.name for p in kept.iterdir()}, edit.hashes())
        self.sync.edits.discard([edit])
        self.assertEqual(list(kept.iterdir()), [])
        # A copy of an imported page (within the document) takes its files from the import.
        edit, _ = self.source.import_pages(self.doc(NOTE2), self.doc(NOTE), [0], 1, NOW + 1)
        copy = self.source.copy_pages(self.doc(NOTE2), [1], 2, NOW + 2)
        files = self.files(NOTE2)
        self.assertEqual(files[f"{NOTE2}/{copy.new_ids[0]}/{IMAGE}"], te.png())
        self.assertEqual(files[f"{NOTE2}.thumbnails/{copy.new_ids[0]}.png"], te.png(8, 8))
        self.assertEqual(files[f"{NOTE2}/{copy.new_ids[0]}.rm"], files[f"{NOTE2}/{edit.new_ids[0]}.rm"])

    def test_a_kept_file_that_does_not_match_its_hash_is_refused(self):
        edit, data = self.source.import_pages(self.doc(NOTE2), self.doc(NOTE), [0], 1, NOW)
        other = PendingEdits(self.dir / "other")
        bad = {sha: blob + b"x" for sha, blob in data.items()}
        with self.assertRaises(EditError):
            other.add(edit, bad)
        self.assertEqual(other.all(), [])


# ------------------------------------------------------------------ 3. clean copies

class CleanCopyTests(Base):
    COPY = "cccccccc-2222-4222-8222-222222222222"

    def test_a_clean_copy_of_an_annotated_pdf_has_no_page_files_and_a_fresh_page_list(self):
        self.source.add_stroke(self.doc(PDF_DOC), A4, rm_stroke.draw([(0.0, 300.0), (90.0, 310.0)],
                                                                    rm_stroke.FINELINER_V2), NOW)
        source = self.doc(PDF_DOC)
        copy_id = self.sync.clone_document(PDF_DOC, "", "Annotated paper copy", self.COPY, clean=True)
        copy = self.sync.tree.get(copy_id)
        self.assertEqual(sorted(copy.files), sorted([f"{copy_id}.content", f"{copy_id}.metadata", f"{copy_id}.pdf"]))
        self.assertEqual(copy.files[f"{copy_id}.pdf"], source.files[f"{PDF_DOC}.pdf"])     # not uploaded again
        content = json.loads(self.cloud.blob_of(copy_id, f"{copy_id}.content")) if hasattr(self.cloud, "blob_of") \
            else json.loads(self.source.blob(copy, f"{copy_id}.content"))
        pages = content["cPages"]["pages"]
        self.assertEqual([e["redir"]["value"] for e in pages], [0, 1, 2])
        self.assertTrue(all("template" not in e for e in pages))
        self.assertFalse({e["id"] for e in pages} & {A4, LETTER, BLANK, SLIDE})
        self.assertEqual(content["cPages"]["original"]["value"], 3)
        self.assertEqual(json.loads(self.source.blob(copy, f"{copy_id}.metadata"))["visibleName"],
                         "Annotated paper copy")
        self.assertEqual([p.pdf_page for p in document_pages(copy, 3)], [0, 1, 2])
        self.assertTrue(all(self.source.page_scene(copy, p) is None or not p.rm_name for p in document_pages(copy, 3)))
        self.assertEqual(len(self.sync.edits.of(PDF_DOC)), 1)           # the source keeps its own

    def test_a_clean_copy_of_a_formatversion_1_pdf_and_of_an_ebook(self):
        v1 = self.sync.clone_document(V1_DOC, "", "Legacy copy", clean=True)
        content = json.loads(self.source.blob(self.sync.tree.get(v1), f"{v1}.content"))
        self.assertEqual((content["formatVersion"], len(content["cPages"]["pages"])), (2, 2))
        self.assertIn(f"{v1}.pagedata", self.sync.tree.get(v1).files)                     # document files stay
        epub = self.sync.clone_document(EPUB_DOC, "", "Novel copy", clean=True)
        self.assertIn(f"{epub}.epub", self.sync.tree.get(epub).files)

    def test_a_notebook_cannot_be_cleaned(self):
        before = set(self.sync.tree.nodes)
        with self.assertRaises(SyncError):
            self.sync.clone_document(NOTE, "", "throwaway copy", clean=True)
        self.assertEqual(set(self.sync.tree.nodes), before)
        with self.assertRaises(clone.CloneError):
            clone.clone_files(NOTE, [f"{NOTE}.content"], lambda n: json.dumps(te.notebook_content(P1)).encode(),
                              copy_id=self.COPY, name="x", parent="", now_ms=NOW, author=ME, clean=True)


# ------------------------------------------------------------------ 4. password-protected PDFs

class PasswordTests(Base):
    def path(self, doc_id: str) -> Path:
        return self.source.pdf_path(self.doc(doc_id))

    def test_a_locked_pdf_opens_only_with_its_password(self):
        secret = self.path(SECRET_DOC)
        self.assertTrue(pdflock.needs_password(secret))
        self.assertTrue(pdflock.locked(secret))
        self.assertFalse(pdflock.needs_password(self.path(PDF_DOC)))
        self.assertFalse(pdflock.needs_password(self.path(OPEN_DOC)))      # owner password only
        PdfFile(self.path(OPEN_DOC)).close()
        with self.assertRaises(pdflock.PdfLocked):
            PdfFile(secret)
        with self.assertRaises(pdflock.PdfLocked):
            DocumentPages(self.source, self.doc(SECRET_DOC))
        self.assertFalse(pdflock.unlock(secret, "wrong"))
        self.assertIsNone(pdflock.password_for(secret))
        self.assertFalse(pdflock.unlock(secret, ""))
        self.assertTrue(pdflock.unlock(secret, PASSWORD))
        self.assertFalse(pdflock.locked(secret))
        pages = DocumentPages(self.source, self.doc(SECRET_DOC))
        self.assertEqual(pages.pdf.page_count, 3)
        self.assertFalse(pages.render(0, 0.1).isNull())
        pages.close()
        pdflock.forget()
        self.assertTrue(pdflock.locked(secret))

    def test_export_reads_with_the_password_and_keeps_the_protection_if_asked(self):
        from pypdf import PdfReader
        from ui.export import export_pdf
        pdflock.unlock(self.path(SECRET_DOC), PASSWORD)
        pages = DocumentPages(self.source, self.doc(SECRET_DOC))
        try:
            plain = export_pdf(pages, self.dir / "plain.pdf")
            kept = export_pdf(pages, self.dir / "kept.pdf", password=PASSWORD)
        finally:
            pages.close()
        reader = PdfReader(plain)
        self.assertFalse(reader.is_encrypted)
        self.assertEqual(len(reader.pages), 3)
        self.assertNotIn(b"/Image", plain.read_bytes())                  # vector: the source pages decrypted,
        self.assertIn(b"/MediaBox [ 0 0 792 612 ]", plain.read_bytes())  # not rasterised
        reader = PdfReader(kept)
        self.assertTrue(reader.is_encrypted)
        self.assertFalse(reader.decrypt("wrong"))
        self.assertTrue(reader.decrypt(PASSWORD))
        self.assertEqual(len(reader.pages), 3)

    def test_the_password_is_never_written_to_disk(self):
        pdflock.unlock(self.path(SECRET_DOC), PASSWORD)
        pages = DocumentPages(self.source, self.doc(SECRET_DOC))
        pages.render(0, 0.1)
        pages.close()
        needle = [PASSWORD.encode(), PASSWORD.encode("utf-16-le")]
        for root, _, names in os.walk(self.dir):
            for name in names:
                data = (Path(root) / name).read_bytes()
                self.assertFalse(any(n in data for n in needle), Path(root) / name)


# ------------------------------------------------------------------ 5. through the app's QML

class LibraryPdfOpsQmlTests(unittest.TestCase):
    wait_for, qml, scene_views = tq.EditingTests.wait_for, tq.EditingTests.qml, tq.EditingTests.scene_views
    close_document = tq.EditingTests.close_document

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"), editable=EDITABLE)
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)

    tearDownClass = classmethod(tq.EditingTests.tearDownClass.__func__)

    def setUp(self):
        self.notices.clear()
        self.source.sync.edits.clear()
        pdflock.forget()
        with self.source._lock:
            self.source._scenes.clear()
            self.source._staged.clear()
        self.app.set_tree(self.source.sync.tree)

    def component(self, qml: bytes):
        from PySide6.QtQml import QQmlComponent
        component = QQmlComponent(self.app.engine)
        component.setData(qml, QUrl())
        made = component.create()
        self.assertIsNotNone(made, component.errorString())
        self._keep = getattr(self, "_keep", []) + [component, made]     # alive until the test ends
        self.addCleanup(made.deleteLater)
        return made

    def test_pages_pasted_and_moved_into_another_document_from_qml(self):
        lib = self.app.library
        caller = self.component(b"""
            import QtQuick
            import com.remarkable
            import xofm.libs.library
            QtObject {
                property bool moved: false
                property var added: []
                property var target: null
                property Connections watch: Connections {
                    target: caller.target
                    function onPagesAdded(pages) { caller.added = pages }
                }
                id: caller
                function paste(source, pages, into, at) { return DocumentController.copyPages(source, pages, into, at); }
                function move(source, pages, into, at) {
                    return DocumentController.movePages(source, pages, into, at, () => { moved = true; });
                }
            }""")
        target = lib.entryForId(NOTE2)
        caller.setProperty("target", target)
        self.assertEqual(target.property("pageCount"), 1)
        ok = QMetaObject.invokeMethod(caller, "paste", Q_ARG("QVariant", PDF_DOC), Q_ARG("QVariant", [1]),
                                      Q_ARG("QVariant", NOTE2), Q_ARG("QVariant", 0))
        self.assertTrue(self.wait_for(lambda: lib.entryForId(NOTE2).property("pageCount") == 2), self.notices)
        self.assertEqual(list(caller.property("added")), [0])
        [edit] = self.source.sync.edits.of(NOTE2)
        self.assertEqual((edit.source_doc, edit.sources), (PDF_DOC, (LETTER,)))
        self.assertEqual(self.source.sync.edits.of(PDF_DOC), [])
        QMetaObject.invokeMethod(caller, "move", Q_ARG("QVariant", NOTE), Q_ARG("QVariant", [0]),
                                 Q_ARG("QVariant", NOTE2), Q_ARG("QVariant", 2))
        self.assertTrue(self.wait_for(lambda: caller.property("moved")), self.notices)
        self.assertEqual(lib.entryForId(NOTE2).property("pageCount"), 3)
        self.assertEqual(lib.entryForId(NOTE).property("pageCount"), 1)
        self.assertEqual(self.notices, [])
        # Into a document that may not be edited: refused, said so, nothing kept.
        QMetaObject.invokeMethod(caller, "paste", Q_ARG("QVariant", NOTE), Q_ARG("QVariant", [0]),
                                 Q_ARG("QVariant", EPUB_DOC), Q_ARG("QVariant", 0))
        self.assertTrue(self.wait_for(lambda: self.notices))
        self.assertEqual(self.source.sync.edits.of(EPUB_DOC), [])

    def test_clean_copy_from_qml_and_a_notebook_refused(self):
        lib = self.app.library
        caller = self.component(b"""
            import QtQuick
            import xofm.libs.library
            QtObject {
                function cleanCopy(id) { return LibraryController.cloneEntry(id, "", true); }
            }""")
        before = set(lib.tree.nodes)
        QMetaObject.invokeMethod(caller, "cleanCopy", Q_ARG("QVariant", PDF_DOC))
        self.assertTrue(self.wait_for(lambda: set(lib.tree.nodes) - before), self.notices)
        [copy_id] = set(lib.tree.nodes) - before
        copy = lib.entryForId(copy_id)
        self.assertEqual(copy.property("pageCount"), 3)
        node = self.source.sync.tree.get(copy_id)
        self.assertFalse(any(name.endswith(".rm") for name in node.files))
        before = set(lib.tree.nodes)
        QMetaObject.invokeMethod(caller, "cleanCopy", Q_ARG("QVariant", NOTE))
        self.assertTrue(self.wait_for(lambda: self.notices))
        tq.wait(200)
        self.assertEqual(set(lib.tree.nodes), before)

    def test_password_dialog_unlocks_and_opens_the_document(self):
        lib = self.app.library
        entry = lib.entryForId(SECRET_DOC)
        self.source.pdf_path(self.source.sync.tree.get(SECRET_DOC))     # the PDF downloaded, as when synced
        self.assertTrue(entry.property("lockedByPassword"))
        self.assertEqual(entry.property("subtext"), "Password protected PDF")
        self.assertFalse(lib.entryForId(PDF_DOC).property("lockedByPassword"))
        opener = self.component(b"""
            import QtQuick
            QtObject {
                property var win
                property bool opened: false
                function go(document) { win.requestOpenDocument(document, function() { opened = true; }); }
            }""")
        opener.setProperty("win", self.window)
        QMetaObject.invokeMethod(opener, "go", Q_ARG("QVariant", entry))
        dialog = self.qml("PasswordDialog_")
        self.assertTrue(self.wait_for(lambda: dialog.property("visible")))
        QMetaObject.invokeMethod(dialog, "passwordEntered", Q_ARG(str, "not the password"))
        self.assertTrue(self.wait_for(lambda: dialog.property("invalidPassword")))
        self.assertFalse(opener.property("opened"))
        self.assertTrue(entry.property("lockedByPassword"))
        QMetaObject.invokeMethod(dialog, "passwordEntered", Q_ARG(str, PASSWORD))
        self.assertTrue(self.wait_for(lambda: opener.property("opened")), self.notices)
        self.assertTrue(self.wait_for(lambda: not dialog.property("visible")))
        self.assertFalse(entry.property("lockedByPassword"))
        self.assertNotEqual(entry.property("subtext"), "Password protected PDF")
        view = tq.EditingTests.open_document(self, SECRET_DOC)          # the pages load, not a load error
        self.assertEqual(view.property("status"), 2)
        self.close_document()
        self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
