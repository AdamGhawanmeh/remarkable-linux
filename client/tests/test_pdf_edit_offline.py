"""Offline tests for editing PDF and EPUB pages (ARCHITECTURE.md 5.4): a page that has no
.rm yet gets a new v6 page; a page the tablet annotated keeps its SceneInfo, its PDF
text highlights (CrdtGlyphItem, block 0x03) and blocks this client does not know, byte
for byte; strokes are stored in the app's scene coordinates over PDF pages of any size
(226/72 px per point, x = 0 on the page's centre line), never clipped to 1404 x 1872;
formatVersion 1 documents are upgraded to cPages as the app upgrades them; ebooks and
pages inserted into a PDF are edited like any page. The last class does it all through
the app's own QML, with input given where the PDF is seen on screen.

All ids here are synthetic. Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import gc
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QPointF, QRect, QRectF, QSizeF  # noqa: E402
from PySide6.QtGui import QColor, QPageLayout, QPageSize, QPainter, QPdfWriter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakecloud import FakeCloud, paired_auth  # noqa: E402
from rmfixtures import f_id, f_sub, f_u32, line_item, points_v2, varuint  # noqa: E402

from core.edits import EditError, PageAdd, StrokeAdd, document_files  # noqa: E402
from core.models import Content  # noqa: E402
from core.pagemap import PageMapError, is_legacy, upgrade  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from ui import editing  # noqa: E402
from ui.library import LibrarySource, document_pages  # noqa: E402
from ui.pages import PDF_PX_PER_PT, DocumentPages, pdf_target_rect, render_content  # noqa: E402
from viewer import rm_select, rm_stroke, rm_writer as w  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

PDF_DOC = "44444444-dddd-4ddd-8ddd-444444444444"       # formatVersion 2 PDF
V1_DOC = "55555555-eeee-4eee-8eee-555555555555"        # formatVersion 1 PDF
EPUB_DOC = "66666666-ffff-4fff-8fff-666666666666"
UNLISTED_DOC = "77777777-aaaa-4aaa-8aaa-777777777777"  # uploaded without a page list
A4, LETTER, BLANK, SLIDE = (f"bbbbbbbb-0000-4000-8000-00000000000{n}" for n in range(1, 5))
L0, L1 = "cccccccc-0000-4000-8000-000000000001", "cccccccc-0000-4000-8000-000000000002"
E0, E1 = "dddddddd-0000-4000-8000-000000000001", "dddddddd-0000-4000-8000-000000000002"
TABLET = bytes(range(16))
TABLET_UUID = "00010203-0405-0607-0809-0a0b0c0d0e0f"
THEN, NOW = 1_730_000_000_000, 1_760_000_000_000
LAYER = (0, 11)
NAMES = ("Annotated paper", "Legacy paper", "Novel", "Unlisted paper")

# The PDF's pages (points) and a red square on each (x, y, side, in points from the top left).
SIZES = [(595.0, 842.0), (792.0, 612.0), (960.0, 540.0)]        # A4 portrait, US Letter landscape, 16:9 slide
MARKS = [(100.0, 700.0, 30.0), (650.0, 80.0, 30.0), (40.0, 470.0, 30.0)]
FIELD_6 = (-1717.0, -468.0, 3434.0, 3433.0)                     # the tablet's on a US Letter page


def make_pdf(sizes=SIZES, marks=MARKS) -> bytes:
    """A PDF with pages of `sizes` points, each with a red square at its mark."""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setResolution(72)                        # painter units = points

    def layout(size):
        page = QPageSize(QSizeF(*size), QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch)
        writer.setPageLayout(QPageLayout(page, QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
    layout(sizes[0])
    painter = QPainter(writer)
    for n, (size, (x, y, side)) in enumerate(zip(sizes, marks)):
        if n:
            layout(size)
            writer.newPage()
        painter.fillRect(QRectF(x, y, side, side), QColor("#ff0000"))
    painter.end()
    return bytes(buffer.data())


def mark_centre(n: int) -> tuple[float, float]:
    """The centre of page n's red square in scene coordinates: PDF points x 226/72, the
    page centred on x = 0 and top-aligned (ARCHITECTURE.md 5.2, 5.4)."""
    (pw, _), (x, y, side) = SIZES[n], MARKS[n]
    return (x + side / 2) * PDF_PX_PER_PT - pw * PDF_PX_PER_PT / 2, (y + side / 2) * PDF_PX_PER_PT


def glyph(item, left=(0, 0), right=(0, 0), text="Highlighted words") -> bytes:
    """A CrdtGlyphItem (0x03): a highlight on the PDF's text, laid out as the community's
    readers read one (start, length, colour, text, rectangles)."""
    raw = text.encode()
    value = (b"\x01" + f_u32(2, 120) + f_u32(3, len(text)) + f_u32(4, 3)
             + f_sub(5, varuint(len(raw)) + b"\x01" + raw)
             + f_sub(6, varuint(1) + struct.pack("<4d", 1100.0, 300.0, 400.0, 40.0)))
    header = w.ItemHeader(LAYER, item, left, right, 0, value)
    return w.Block(0x03, 1, 1, header.encode()).encode()


def stroke_block(item, left=(0, 0), points=((0, 0), (50, 50))) -> bytes:
    data = line_item(LAYER, item, tool=17, color=0, points=points_v2([(x, y, 0, 0, 2, 255) for x, y in points]))
    _, [b] = w.split(w.V6_HEADER + data)
    header = w.decode_item(b.payload)
    header.left = left
    return w.Block(b.type, b.min_version, b.version, header.encode()).encode()


def annotated_pdf_page() -> bytes:
    """A US Letter page as the tablet stores one it annotated: SceneInfo field 6 set
    (stamped 1:14), a stroke, a text highlight drawn after it, a stroke after that, and an
    Array block (0x0B) holding an id: the highest counter of the page (90)."""
    header, blocks = w.split(w.blank_page(TABLET))
    n = next(i for i, b in enumerate(blocks) if b.type == w.BLOCK_SCENE_INFO)
    values = [(i, k, w.lww((1, 14), struct.pack("<4d", *FIELD_6)) if i == 6 else v)
              for i, k, v in w.read_values(blocks[n].payload)]
    blocks[n] = w.Block(blocks[n].type, blocks[n].min_version, blocks[n].version, w.write_values(values))
    array = w.Block(0x0B, 1, 1, f_id(1, (1, 90)) + f_sub(2, b"\x00\x01\x02\x03")).encode()
    items = (stroke_block((1, 15), points=((-400, 700), (-300, 760))) + glyph((1, 16), left=(1, 15))
             + stroke_block((1, 17), left=(1, 16), points=((500, 1500), (560, 1540))))
    return w.join(header, blocks[:n + 1]) + array + b"".join(b.encode() for b in blocks[n + 1:]) + items


def page_entry(page_id: str, idx: str, redir: int | None = None, template: str = "Blank") -> dict:
    entry = {"id": page_id, "idx": {"timestamp": "1:2", "value": idx},
             "template": {"timestamp": "1:2", "value": template}}
    if redir is not None:
        entry["redir"] = {"timestamp": "1:2", "value": redir}
    return entry


def pdf_content(file_type: str, entries: list[dict], original: int) -> dict:
    return {"fileType": file_type, "formatVersion": 2, "orientation": "portrait", "pageCount": len(entries),
            "coverPageNumber": 0, "extraMetadata": {}, "tags": [],
            "cPages": {"lastOpened": {"timestamp": "1:1", "value": entries[0]["id"]},
                       "original": {"timestamp": "1:1", "value": original},
                       "pages": entries, "uuids": [{"first": TABLET_UUID, "second": 1}]}}


def v1_content() -> dict:
    return {"fileType": "pdf", "formatVersion": 1, "orientation": "portrait", "pageCount": 2,
            "coverPageNumber": 0, "extraMetadata": {}, "tags": [], "pages": [L0, L1],
            "redirectionPageMap": [0, 1], "originalPageCount": 2}


def meta(name: str, **extra) -> dict:
    return {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": str(THEN),
            "createdTime": str(THEN), "pinned": False, "lastOpened": str(THEN), "lastOpenedPage": 0, **extra}


def build_cloud() -> FakeCloud:
    pdf = make_pdf()
    cloud = FakeCloud()
    cloud.put_item(PDF_DOC, meta(NAMES[0]),
                   pdf_content("pdf", [page_entry(A4, "ba", 0), page_entry(LETTER, "bb", 1),
                                       page_entry(BLANK, "bc", None, "P Lines small"), page_entry(SLIDE, "bd", 2)], 3),
                   {f"{PDF_DOC}.pdf": pdf, f"{PDF_DOC}/{LETTER}.rm": annotated_pdf_page()})
    cloud.put_item(V1_DOC, meta(NAMES[1]), v1_content(), {f"{V1_DOC}.pdf": pdf, f"{V1_DOC}.pagedata": b"Blank\nBlank\n"})
    cloud.put_item(EPUB_DOC, meta(NAMES[2]), pdf_content("epub", [page_entry(E0, "ba", 0), page_entry(E1, "bb", 1)], 2),
                   {f"{EPUB_DOC}.pdf": pdf, f"{EPUB_DOC}.epub": b"PK\x03\x04 not a real ebook"})
    cloud.put_item(UNLISTED_DOC, meta(NAMES[3]), {"fileType": "pdf", "formatVersion": 1, "pageCount": 3, "pages": None},
                   {f"{UNLISTED_DOC}.pdf": pdf})
    return cloud


def line(points, tool=rm_stroke.FINELINER_V2):
    return rm_stroke.draw(points, tool)


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
        self.source = LibrarySource(self.sync, editable=NAMES)
        self.tree = self.source.refresh()

    def doc(self, doc_id: str):
        return self.source.sync.tree.get(doc_id)

    def files(self, doc_id: str) -> dict:
        doc = self.doc(doc_id)
        return document_files(doc, self.sync.edits.of(doc_id), lambda name: self.source.blob(doc, name),
                              lambda e: self.sync.edits.data(e), self.sync.author())

    def blob(self, doc_id: str, name: str) -> bytes:
        return self.source.blob(self.doc(doc_id), name)


class GateTests(Base):
    def test_pdfs_and_ebooks_named_for_editing_may_be_edited(self):
        for doc_id in (PDF_DOC, V1_DOC, EPUB_DOC):
            self.assertEqual(self.source.edit_refusal(self.doc(doc_id)), "", doc_id)
        self.assertIn("no page list yet", self.source.edit_refusal(self.doc(UNLISTED_DOC)))
        with self.assertRaises(EditError):
            self.source.add_stroke(self.doc(UNLISTED_DOC), "pdf-page-0", line([(0, 0), (9, 9)]), NOW)
        self.assertIn("only turned on", LibrarySource(self.sync, editable=("Something else",))
                      .edit_refusal(self.doc(PDF_DOC)))
        self.assertIn("not turned on", LibrarySource(self.sync).edit_refusal(self.doc(PDF_DOC)))
        self.assertEqual(self.sync.edits.all(), [])


class UnannotatedPageTests(Base):
    def test_a_pdf_page_without_a_file_gets_a_new_v6_page(self):
        # Ink well outside the 1404 x 1872 screen: the A4 page is 1868 px wide and 2642 high.
        points = [(-900 + 60 * k, 2500 + 5 * k) for k in range(31)]
        self.source.add_stroke(self.doc(PDF_DOC), A4, line(points), NOW)
        files = self.files(PDF_DOC)
        self.assertEqual(sorted(files), [f"{PDF_DOC}.content", f"{PDF_DOC}.metadata", f"{PDF_DOC}/{A4}.rm"])
        page = files[f"{PDF_DOC}/{A4}.rm"]
        header, blocks = w.split(page)
        self.assertEqual(header, w.V6_HEADER)
        blank = w.split(w.blank_page(self.sync.author()))[1]
        self.assertEqual([b.type for b in blocks], [b.type for b in blank] + [w.BLOCK_LINE_ITEM])
        self.assertEqual(blocks[:len(blank)], blank)                    # SceneInfo, PageStats, Layer 1 as made
        info = dict((i, v) for i, _, v in w.read_values(next(b for b in blocks if b.type == w.BLOCK_SCENE_INFO).payload))
        self.assertEqual(struct.unpack("<II", info[5]), w.PAPER)          # 1404 x 1872, also on PDF pages
        self.assertEqual(w.page_loads(page), 1)
        scene = parse(page)
        self.assertEqual([(layer.name, len(layer.strokes)) for layer in scene.layers], [("Layer 1", 1)])
        xs = [p.x for p in scene.layers[0].strokes[0].points]
        ys = [p.y for p in scene.layers[0].strokes[0].points]
        self.assertEqual((min(xs), max(xs), max(ys)), (-900.0, 900.0, 2650.0))   # not clipped
        content = json.loads(files[f"{PDF_DOC}.content"])
        before = json.loads(self.blob(PDF_DOC, f"{PDF_DOC}.content"))
        entry = next(e for e in content["cPages"]["pages"] if e["id"] == A4)
        self.assertEqual(entry.pop("modifed"), str(NOW))
        self.assertEqual(content, before)                               # nothing else changed
        self.assertEqual(json.loads(files[f"{PDF_DOC}.metadata"])["lastModified"], str(NOW))

    def test_the_page_is_drawn_over_its_pdf_page_where_it_was_drawn(self):
        cx, cy = mark_centre(0)
        self.source.add_stroke(self.doc(PDF_DOC), A4, line([(cx - 30, cy), (cx + 30, cy)]), NOW)
        pages = DocumentPages(self.source, self.doc(PDF_DOC))
        try:
            content = pages.content(0)
            self.assertTrue(content.has_pdf_page)
            frame = content.frame()
            self.assertEqual((frame.width(), frame.height()), (595 * PDF_PX_PER_PT, 842 * PDF_PX_PER_PT))
            image = render_content(content, 0.5, "clip")
            # The stroke's middle (black ink) lies inside the red square around it.
            x, y = round((cx + 702 - frame.x()) * 0.5), round((cy - frame.y()) * 0.5)
            self.assertLess(QColor(image.pixel(x, y)).lightness(), 60)
            self.assertEqual(QColor(image.pixel(x, y - 8)).name(), "#ff0000")
        finally:
            pages.close()

    def test_an_ebook_page_is_annotated_like_a_pdf_page(self):
        self.source.add_stroke(self.doc(EPUB_DOC), E1, line([(-800, 100), (800, 2400)]), NOW)
        files = self.files(EPUB_DOC)
        self.assertEqual(sorted(files), [f"{EPUB_DOC}.content", f"{EPUB_DOC}.metadata", f"{EPUB_DOC}/{E1}.rm"])
        self.assertEqual(len(parse(files[f"{EPUB_DOC}/{E1}.rm"]).layers[0].strokes), 1)
        entry = next(e for e in json.loads(files[f"{EPUB_DOC}.content"])["cPages"]["pages"] if e["id"] == E1)
        self.assertEqual((entry["redir"]["value"], entry["modifed"]), (1, str(NOW)))
        refs = document_pages(self.doc(EPUB_DOC), 3)
        self.assertEqual([(r.page_id, r.pdf_page) for r in refs], [(E0, 0), (E1, 1)])

    def test_a_page_inserted_into_a_pdf_is_annotated_like_a_notebook_page(self):
        self.source.add_stroke(self.doc(PDF_DOC), BLANK, line([(0, 0), (100, 100)]), NOW)
        files = self.files(PDF_DOC)
        self.assertIn(f"{PDF_DOC}/{BLANK}.rm", files)
        entry = next(e for e in json.loads(files[f"{PDF_DOC}.content"])["cPages"]["pages"] if e["id"] == BLANK)
        self.assertNotIn("redir", entry)                                # it still has no PDF page
        self.assertEqual(entry["template"]["value"], "P Lines small")
        self.assertEqual(entry["modifed"], str(NOW))


class AnnotatedPageTests(Base):
    """Every kind of edit on a page with a text highlight, a SceneInfo field 6 and an
    Array block: those stay byte for byte, and new ids come after the highest one."""

    def original(self) -> list[w.Block]:
        return w.split(self.blob(PDF_DOC, f"{PDF_DOC}/{LETTER}.rm"))[1]

    def page(self) -> bytes:
        return self.source.staged(self.doc(PDF_DOC)).page(LETTER)

    def check_kept(self, page: bytes) -> None:
        before = {b.type: b for b in self.original() if b.type in (w.BLOCK_SCENE_INFO, 0x0B)}
        _, blocks = w.split(page)
        for kind, block in before.items():
            self.assertEqual([b.encode() for b in blocks if b.type == kind], [block.encode()], hex(kind))
        glyphs = [b.encode() for b in blocks if b.type == 0x03]
        self.assertEqual(glyphs, [b.encode() for b in self.original() if b.type == 0x03])
        self.assertIn((1, 16), [i for i, _ in w.sequence(blocks, LAYER)])   # still in its layer's sequence
        me = dict((uuid, i) for uuid, i in w.decode_authors(blocks[0].payload))[self.sync.author()]
        self.assertEqual(me, 2)
        mine = [w.decode_item(b.payload).item for b in blocks if b.type in w.ITEM_BLOCKS
                and w.decode_item(b.payload).item[0] == me]
        self.assertTrue(mine and all(c > 90 for _, c in mine), mine)      # past the Array block's id
        parse(page)                                                       # still a page the viewer reads

    def test_strokes_erasing_images_selection_paste_and_undo_keep_what_the_tablet_wrote(self):
        doc = self.doc(PDF_DOC)
        self.source.add_stroke(doc, LETTER, line([(-900, 300), (-700, 2300)]), NOW)
        self.check_kept(self.page())
        # Erased across both of the tablet's strokes (and over the highlight, which stays).
        eraser = rm_stroke.eraser_line([(-350, 600), (-350, 900)], thickness=rm_stroke.THICK)
        self.assertIsNotNone(self.source.erase_strokes(doc, LETTER, eraser, NOW + 1))
        self.check_kept(self.page())
        # An image put on the page, moved, then the selection of everything turned.
        key = bytes.fromhex(self.source.insert_image(doc, LETTER, make_png(), "png",
                                                     editing.corners(800, 1600, 200, 100), NOW + 2).key)
        self.source.transform_image(doc, LETTER, key, editing.corners(850, 1700, 200, 100), NOW + 3)
        self.check_kept(self.page())
        live = [s.item for s in rm_stroke.strokes(self.page())]
        edit, moved = self.source.transform_items(doc, LETTER, live, (0.0, 1.0, -1.0, 0.0, 100.0, 50.0), NOW + 4)
        self.assertEqual(len(moved), len(live))
        self.check_kept(self.page())
        # Copied and pasted, then everything taken away and put back by undo and redo.
        values = self.source.item_values(doc, LETTER, list(moved.values()))
        self.source.paste_strokes(doc, LETTER, rm_select.pasted(list(values.values()), 1.0, (0.0, 2000.0)), NOW + 5)
        self.check_kept(self.page())
        items = [s.item for s in rm_stroke.strokes(self.page())]
        self.assertTrue(self.source.delete_items(doc, LETTER, items, NOW + 6))
        self.assertEqual(rm_stroke.strokes(self.page()), [])
        self.check_kept(self.page())
        self.assertTrue(self.source.undo(doc, LETTER, NOW + 7))
        self.assertEqual(len(rm_stroke.strokes(self.page())), len(items))
        self.assertTrue(self.source.redo(doc, LETTER, NOW + 8))
        self.assertEqual(rm_stroke.strokes(self.page()), [])
        self.check_kept(self.page())
        files = self.files(PDF_DOC)
        self.assertEqual(files[f"{PDF_DOC}/{LETTER}.rm"], self.page())
        self.assertEqual(w.page_loads(self.page()), 2)                     # one save session over loads 1

    def test_a_pdf_page_keeps_scene_info_when_strokes_are_erased_to_nothing(self):
        doc = self.doc(PDF_DOC)
        items = [("page", *s.item) for s in rm_stroke.strokes(self.page())]
        self.source.delete_strokes(doc, LETTER, items, NOW)
        self.assertEqual(rm_stroke.strokes(self.page()), [])
        kept = (w.BLOCK_SCENE_INFO, 0x0B, 0x03)
        self.assertEqual([b.encode() for b in w.split(self.page())[1] if b.type in kept],
                         [b.encode() for b in self.original() if b.type in kept])


class LegacyFormatTests(Base):
    """formatVersion 1 PDFs (`pages` + `redirectionPageMap`): upgraded to cPages as the app
    upgrades them when it loads one (fillContentFromDocument, 0x1406cd640)."""

    def test_the_upgrade_is_the_apps(self):
        content = upgrade(v1_content(), TABLET, ["Blank", "P Grid small"])
        self.assertFalse(is_legacy(content))
        self.assertEqual({k for k in content if k in ("pages", "redirectionPageMap", "originalPageCount")}, set())
        self.assertEqual((content["formatVersion"], content["pageCount"]), (2, 2))
        self.assertEqual(content["cPages"], {
            "lastOpened": {"timestamp": "0:0", "value": ""},
            "original": {"timestamp": "1:1", "value": 2},
            "pages": [{"id": L0, "idx": {"timestamp": "1:2", "value": "ba"}, "redir": {"timestamp": "1:2", "value": 0},
                       "template": {"timestamp": "1:2", "value": "Blank"}},
                      {"id": L1, "idx": {"timestamp": "1:2", "value": "bb"}, "redir": {"timestamp": "1:2", "value": 1},
                       "template": {"timestamp": "1:2", "value": "P Grid small"}}],
            "uuids": [{"first": TABLET_UUID, "second": 1}]})
        # The pages read as before: same ids, order and PDF pages.
        old, new = Content.parse(v1_content()), Content.parse(content)
        self.assertEqual([(p.id, p.redirect) for p in old.pages], [(p.id, p.redirect) for p in new.pages])
        # Edge cases, as the app reads them: a map of another length -> page i; a bad map -> no redir.
        odd = upgrade({**v1_content(), "redirectionPageMap": [1]}, TABLET)
        self.assertEqual([e["redir"]["value"] for e in odd["cPages"]["pages"]], [0, 1])
        bad = upgrade({**v1_content(), "redirectionPageMap": [0, 7]}, TABLET)
        self.assertEqual([("redir" in e) for e in bad["cPages"]["pages"]], [False, False])
        self.assertEqual(bad["cPages"]["original"]["value"], 2)
        with self.assertRaises(PageMapError):
            upgrade({"fileType": "pdf", "formatVersion": 1, "pages": None}, TABLET)
        self.assertIs(upgrade(content, TABLET), content)

    def test_a_stroke_on_a_legacy_pdf_upgrades_its_content_and_sets_the_page_modified(self):
        self.source.add_stroke(self.doc(V1_DOC), L1, line([(-500, 200), (600, 1900)]), NOW)
        files = self.files(V1_DOC)
        self.assertEqual(sorted(files), [f"{V1_DOC}.content", f"{V1_DOC}.metadata", f"{V1_DOC}/{L1}.rm"])
        content = json.loads(files[f"{V1_DOC}.content"])
        self.assertEqual(content["formatVersion"], 2)
        pages = content["cPages"]["pages"]
        self.assertEqual([(e["id"], e["redir"]["value"], e.get("modifed")) for e in pages],
                         [(L0, 0, None), (L1, 1, str(NOW))])
        self.assertEqual(content["cPages"]["uuids"][0]["first"].replace("-", ""), self.sync.author().hex())
        self.assertEqual(json.loads(files[f"{V1_DOC}.metadata"])["lastModified"], str(NOW))
        # Uploaded, the document reads the same, and can be edited again.
        self.sync.push_edits(V1_DOC)
        self.source.refresh()
        doc = self.doc(V1_DOC)
        self.assertEqual(doc.content.format_version, 2)
        self.assertEqual([(p.id, p.redirect) for p in doc.content.pages], [(L0, 0), (L1, 1)])
        self.source.add_stroke(doc, L0, line([(0, 0), (40, 40)]), NOW + 1)
        files = self.files(V1_DOC)
        entry = {e["id"]: e for e in json.loads(files[f"{V1_DOC}.content"])["cPages"]["pages"]}
        self.assertEqual((entry[L0]["modifed"], entry[L1]["modifed"]), (str(NOW + 1), str(NOW)))

    def test_pages_are_added_to_a_legacy_pdf(self):
        edit = self.source.add_page(self.doc(V1_DOC), 1, "Blank", NOW)
        self.assertIsInstance(edit, PageAdd)
        self.assertEqual(self.source.page_ids(self.doc(V1_DOC)), [L0, edit.new_id, L1])
        content = json.loads(self.files(V1_DOC)[f"{V1_DOC}.content"])
        self.assertEqual(content["formatVersion"], 2)
        listed = Content.parse(content).pages
        self.assertEqual([(p.id, p.redirect) for p in listed], [(L0, 0), (edit.new_id, None), (L1, 1)])
        self.source.add_stroke(self.doc(V1_DOC), edit.new_id, line([(0, 0), (40, 40)]), NOW + 1)
        self.assertIn(f"{V1_DOC}/{edit.new_id}.rm", self.files(V1_DOC))

    def test_strokes_stay_where_they_were_drawn_after_the_upgrade(self):
        self.source.add_stroke(self.doc(V1_DOC), L0, line([(-100, 100), (100, 300)]), NOW)
        self.assertIsInstance(self.sync.edits.of(V1_DOC)[0], StrokeAdd)
        page = self.files(V1_DOC)[f"{V1_DOC}/{L0}.rm"]
        points = parse(page).layers[0].strokes[0].points
        self.assertEqual((points[0].x, points[0].y, points[-1].x, points[-1].y), (-100.0, 100.0, 100.0, 300.0))


class GeometryTests(unittest.TestCase):
    """Where a PDF page of each shape lands in scene coordinates (226/72 px per point,
    centred on x = 0, top-aligned), and that the page's bounds grow with ink past it."""

    def test_page_frames(self):
        for (pw, ph) in SIZES:
            frame = pdf_target_rect(QSizeF(pw, ph)).translated(-702, 0)       # ui.pages -> scene
            self.assertAlmostEqual(frame.center().x(), 0.0, places=6)
            self.assertEqual((frame.top(), frame.width(), frame.height()),
                             (0.0, pw * PDF_PX_PER_PT, ph * PDF_PX_PER_PT))
        # Landscape and slides are wider than the screen and shorter than it.
        self.assertGreater(pdf_target_rect(QSizeF(*SIZES[2])).width(), 1404 * 2)
        self.assertLess(pdf_target_rect(QSizeF(*SIZES[2])).height(), 1872)


def make_png(width: int = 60, height: int = 30) -> bytes:
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QImage
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#3050c0"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


class QuickTests(unittest.TestCase):
    """PDF and ebook pages edited through the app's own QML (DocumentViewport, the pen
    toolbar's PenSurface, SceneSelectionHandler), with input given where the PDF is seen
    on screen: the red square of each page is found in a grab of the window, a stroke is
    drawn through it with the mouse, and it must be stored at the square's place in the
    PDF (points x 226/72, centred on x = 0), whatever the page's shape and the zoom."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq             # its harness; its tests are not collected from here
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"), editable=NAMES)
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)
        cls.picture = tmp / "picture.png"
        cls.picture.write_bytes(make_png(200, 100))

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
        del self.app.warnings[:]

    def tearDown(self):
        self.tq.EditingTests.close_document(self)
        self.source.sync.edits.clear()

    # the shared harness, bound to this class
    def wait_for(self, condition, ms=8000):
        return self.tq.WorkflowTests.wait_for(self, condition, ms)

    def qml(self, prefix):
        return self.tq.WorkflowTests.qml(self, prefix)

    def scene_views(self):
        return self.tq.WorkflowTests.scene_views(self)

    def open_document(self, doc_id, page_id=None):
        """The document opened (at its last page read), then the page `page_id` turned
        to as the app turns pages (DocumentViewport.setCurrentPage)."""
        from PySide6.QtCore import Q_ARG, QMetaObject
        view = self.tq.PenTests.open_document(self, doc_id)
        if page_id is None or view.property("pageId") == page_id:
            return view
        index = self.source.staged(self.source.sync.tree.get(doc_id)).ids.index(page_id)
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "setCurrentPage", Q_ARG("QVariant", index))

        def turned():
            return next((v for v in self.scene_views() if v.property("status") == 2
                         and v.property("pageId") == page_id), None)
        self.assertTrue(self.wait_for(lambda: turned() is not None), page_id)
        return turned()

    def window_point(self, view, x, y):
        return self.tq.PenTests.window_point(self, view, x, y)

    def pens(self, view):
        return self.tq.PenTests.pens(self, view)

    def drag(self, view, points):
        return self.tq.PenTests.drag(self, view, points)

    def handler(self):
        return self.tq.EditingTests.handler(self)

    def pending(self, kind=None) -> list:
        return [e for e in self.source.sync.edits.all() if kind is None or type(e).__name__ == kind]

    def staged_page(self, doc_id, page_id) -> bytes | None:
        return self.source.staged(self.source.sync.tree.get(doc_id)).page(page_id)

    def red_centre(self, view) -> QPointF | None:
        """The middle of the red square as the window shows it (window coordinates)."""
        image = self.window.grabWindow()
        dpr = image.devicePixelRatio() or 1.0
        box = view.mapRectToScene(QRectF(0, 0, view.width(), view.height())).toRect().intersected(
            QRect(0, 0, round(image.width() / dpr), round(image.height() / dpr)))
        xs, ys = [], []
        for y in range(box.top(), box.bottom()):
            for x in range(box.left(), box.right()):
                c = QColor(image.pixel(round(x * dpr), round(y * dpr)))
                if c.red() > 200 and c.green() < 90 and c.blue() < 90:
                    xs.append(x)
                    ys.append(y)
        if len(xs) < 9:
            return None
        return QPointF((min(xs) + max(xs) + 1) / 2, (min(ys) + max(ys) + 1) / 2)

    def test_strokes_land_where_the_pdf_is_seen_on_pages_of_any_shape_and_at_any_zoom(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        for n, page_id in ((0, A4), (1, LETTER), (2, SLIDE)):
            view = self.open_document(PDF_DOC, page_id)
            self.assertTrue(self.wait_for(lambda: view._image is not None))
            tools = self.pens(view).tools
            tools.chooseTool("fineliner")
            self.assertTrue(tools.property("active"))
            cx, cy = mark_centre(n)
            fitted = float(view._tiles._get("scale"))
            for scale in (fitted, 0.35, 1.6):
                view._tiles.setFocalPoint(QPointF(cx, cy), scale)
                self.tq.wait(500)                                  # re-rendered at this zoom
                seen = self.red_centre(view)
                self.assertIsNotNone(seen, (page_id, scale))
                # The PDF is drawn where its points say (within a pixel and a half on screen).
                expected = view.mapToScene(view._tiles.to_view(QPointF(cx, cy)))
                self.assertLess(abs(seen.x() - expected.x()), 1.5, (page_id, scale))
                self.assertLess(abs(seen.y() - expected.y()), 1.5, (page_id, scale))
                count = len(self.pending("StrokeAdd"))
                at = [QPointF(seen.x() + dx, seen.y()).toPoint() for dx in range(-12, 13, 3)]
                QTest.mousePress(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at[0])
                for p in at[1:]:
                    QTest.mouseMove(self.window, p)
                    self.tq.wait(5)
                QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at[-1])
                self.assertTrue(self.wait_for(lambda: len(self.pending("StrokeAdd")) == count + 1), self.notices)
                points = self.pending("StrokeAdd")[-1].line().unpacked()
                mid_x = (points[0].x + points[-1].x) / 2
                tolerance = 2.0 / scale + 1.0                      # two screen pixels, and rounding
                self.assertLess(abs(mid_x - cx), tolerance, (page_id, scale, mid_x, cx))
                self.assertLess(abs(points[0].y - cy), tolerance, (page_id, scale, points[0].y, cy))
                self.assertAlmostEqual(points[-1].x - points[0].x, 24 / scale, delta=2 / scale + 0.5)
            # The page keeps every stroke, unclipped, and draws them.
            page = self.staged_page(PDF_DOC, page_id)
            self.assertEqual(len(rm_stroke.strokes(page)), 3 + (2 if page_id == LETTER else 0))
            tools.deactivate()
            self.tq.EditingTests.close_document(self)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_eraser_selection_images_and_undo_on_an_annotated_pdf_page(self):
        from PySide6.QtCore import Q_ARG, QMetaObject, QUrl
        doc_id = PDF_DOC
        view = self.open_document(doc_id, LETTER)
        controller, tools = view._controller, self.pens(view).tools
        original = {b.type: b.encode() for b in w.split(self.staged_page(doc_id, LETTER))[1] if b.type in (0x03, 0x0B, 0x0D)}
        # The eraser over the tablet's first stroke (-400, 700) .. (-300, 760).
        tools.chooseTool("eraser")
        self.drag(view, [(-350, 650 + 10 * k) for k in range(15)])
        self.assertTrue(self.wait_for(lambda: self.pending("StrokeErase")), self.notices)
        self.assertTrue(self.wait_for(lambda: (1, 15) not in [s.item for s in rm_stroke.strokes(self.staged_page(doc_id, LETTER))]))
        tools.deactivate()
        # The tablet's other stroke (500, 1500) .. (560, 1540), selected with a rectangle and
        # moved past the 1404 px screen (x up to 860 on a page 2486 px wide).
        self.qml("DocumentView").setProperty("selectionMode", True)
        controller.addSelectionRect(QRect(480, 1480, 100, 80), 0)
        self.assertTrue(self.wait_for(lambda: controller.property("selectionItemCount") == 1), self.notices)
        handler = self.handler()
        QMetaObject.invokeMethod(handler, "moveSelectedItems", Q_ARG("QVariant", 300), Q_ARG("QVariant", 40))
        QMetaObject.invokeMethod(self.qml("DocumentView"), "exitEditMode")
        self.assertTrue(self.wait_for(lambda: self.pending("ItemsTransform")), self.notices)
        self.assertTrue(self.wait_for(lambda: any(
            abs(s.line.unpacked()[-1].x - 860) < 0.01 and abs(s.line.unpacked()[-1].y - 1580) < 0.01
            for s in rm_stroke.strokes(self.staged_page(doc_id, LETTER)))))
        self.qml("DocumentView").setProperty("selectionMode", False)
        # An image dropped right of the screen's edge.
        controller.insertImageFileAsSceneItem(QUrl.fromLocalFile(str(self.picture)), QPointF(1000, 300))
        self.assertTrue(self.wait_for(lambda: self.pending("ImageInsert")), self.notices)
        insert = self.pending("ImageInsert")[0]
        xs = [v[0] for v in insert.vertices]
        self.assertEqual((min(xs), max(xs)), (900.0, 1100.0))
        # Undo takes the image away, redo puts it back.
        self.assertTrue(self.wait_for(lambda: controller.property("undoAvailable")))
        QMetaObject.invokeMethod(self.qml("DocumentView"), "exitEditMode")
        controller.undo()
        self.assertTrue(self.wait_for(lambda: not w.split(self.staged_page(doc_id, LETTER))[1][-1].type == 0x0F
                                      or w.decode_item(w.split(self.staged_page(doc_id, LETTER))[1][-1].payload).value is None))
        controller.redo()
        self.tq.wait(300)
        # What the tablet wrote is still there, byte for byte, and the page still reads.
        page = self.staged_page(doc_id, LETTER)
        self.assertEqual({b.type: b.encode() for b in w.split(page)[1] if b.type in (0x03, 0x0B, 0x0D)}, original)
        self.assertEqual(len(parse(page).placed_images), 1)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_ebook_and_legacy_pdf_pages_take_strokes(self):
        # Paths inside each page but outside the 1404 x 1872 screen page: the ebook's page is
        # A4 (1868 x 2643 px), the legacy PDF's second page US Letter landscape (2486 x 1921).
        paths = {E0: [(-850 + 100 * k, 2500 - 50 * k) for k in range(18)],
                 L1: [(-1100 + 130 * k, 1500 + 10 * k) for k in range(18)]}
        for doc_id, page_id in ((EPUB_DOC, E0), (V1_DOC, L1)):
            view = self.open_document(doc_id, page_id)
            tools = self.pens(view).tools
            tools.chooseTool("ballpoint")
            path = paths[page_id]
            view._tiles.setFocalPoint(QPointF(sum(x for x, _ in path) / len(path), sum(y for _, y in path) / len(path)), 0.4)
            self.tq.wait(300)
            self.drag(view, path)
            self.assertTrue(self.wait_for(lambda: any(e.doc_id == doc_id for e in self.pending("StrokeAdd"))),
                            self.notices)
            tools.deactivate()
            doc = self.source.sync.tree.get(doc_id)
            files = document_files(doc, self.source.sync.edits.of(doc_id), lambda name: self.source.blob(doc, name),
                                   lambda e: b"", self.source.sync.author())
            content = json.loads(files[f"{doc_id}.content"])
            self.assertEqual(content["formatVersion"], 2)
            self.assertEqual(next(e for e in content["cPages"]["pages"] if e["id"] == page_id)["redir"]["value"],
                             {E0: 0, L1: 1}[page_id])
            points = parse(files[f"{doc_id}/{page_id}.rm"]).layers[0].strokes[0].points
            self.assertLess(min(p.x for p in points), -702)                 # left of the screen page
            if page_id == E0:
                self.assertGreater(max(p.y for p in points), 1872)          # below it
            else:
                self.assertGreater(max(p.x for p in points), 702)           # right of it
            self.tq.EditingTests.close_document(self)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_a_held_pen_straightens_a_line_across_a_wide_slide(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from ui.quick.bridge.pen import HOLD_MS
        view = self.open_document(PDF_DOC, SLIDE)                   # 3013 x 1695 px
        tools = self.pens(view).tools
        tools.chooseTool("fineliner")
        view._tiles.setFocalPoint(QPointF(0, 900), 0.4)
        self.tq.wait(300)
        wobbly = [(-1300.0 + 60 * n, 900.0 + (7 if n % 2 else -5)) for n in range(25)]
        at = [self.window_point(view, x, y) for x, y in wobbly]
        QTest.mousePress(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at[0])
        for p in at[1:]:
            QTest.mouseMove(self.window, p)
            self.tq.wait(5)
        self.tq.wait(HOLD_MS + 250)                                  # held still: fixed as a straight line
        end = self.window_point(view, 1300.0, 904.0)
        QTest.mouseMove(self.window, end)
        self.tq.wait(20)
        QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        self.assertTrue(self.wait_for(lambda: self.pending("StrokeAdd")), self.notices)
        points = self.pending("StrokeAdd")[-1].line().unpacked()
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].y, points[1].y)                  # snapped level
        self.assertAlmostEqual(points[0].x, -1300.0, delta=3 / 0.4)
        self.assertAlmostEqual(points[1].x, 1300.0, delta=3 / 0.4)
        tools.deactivate()
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])
