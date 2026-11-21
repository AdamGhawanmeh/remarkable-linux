"""Offline tests for the v1 finish (synthetic ids and synthetic JWTs only; nothing
reaches the real cloud, ~/.cache/remarkable-linux is never read):

- landscape notebooks (ARCHITECTURE.md 5.6): a page of a document whose orientation is
  landscape is the paper's note bounds, wide; landscape ("LS") templates laid out on
  the transposed paper; thumbnails, PNG/SVG/PDF exports and the viewer's
  primaryContentRect follow; customZoom* of a new notebook stay the constructor's
  whatever the paper;
- pasting items at the text cursor: the room the text makes (ui.textedit.make_room),
  the anchored group's blocks (viewer.rm_anchor), the AnchoredGroupAdd edit kept,
  replayed and uploaded; a sentinel anchor does not move the page's clock;
- the bridge stubs wired here: SelectionImage.image, Library.countArchivedDocuments /
  purgeThumbnails, LibraryController.setScrollPosition / setZoomMode /
  setCustomZoomFit, DocumentWrapper.scrollPosition, DocumentController
  .setViewBackgroundFilter, PlatformHelpers.openFileChooser / setInhibitSleep,
  PlatformApp.shouldShowWhatsNew (and WhatsNewDialog on a first start); every no-op
  member left is in the documented skip list (tools/bridge_stub_audit.py);
- a normal start (no --edit-notebook, no REMARKABLE_EDIT_NOTEBOOK) leaves editing to
  Settings > Editing, and no launcher limits it.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_v1_finish_offline.py'
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools"), str(HERE.parent)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)
from PySide6.QtCore import QMetaObject, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

import test_edits_offline as te  # noqa: E402
from core.edits import AnchoredGroupAdd, PendingEdits, StrokePaste, TextEdit  # noqa: E402
from core.sync import SyncClient, describe_edit  # noqa: E402
from fakecloud import paired_auth  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from viewer import rm_anchor, rm_parser, rm_stroke, rm_writer as w  # noqa: E402

THROWAWAY, PAGE1, PAGE2, NOW = te.THROWAWAY, te.PAGE1, te.PAGE2, te.NOW
WIDE = "77777777-dddd-4ddd-8ddd-777777777777"           # a landscape notebook
WIDE_PAGE = "77777777-0000-4000-8000-000000000001"
LEGACY = "66666666-eeee-4eee-8eee-666666666666"          # formatVersion 1, no cPages


def landscape_content(*pages: str) -> dict:
    content = te.notebook_content(*pages)
    content.update(orientation="landscape")
    for entry in content["cPages"]["pages"]:
        entry["template"] = {"timestamp": "1:1", "value": "LS Lines medium"}
    return content


def drawn(points, tool=17) -> bytes:
    return rm_stroke.append_stroke(None, author=te.TABLET, line=rm_stroke.draw(points, tool))


# -- landscape -------------------------------------------------------------------------

class LandscapeTemplateTests(unittest.TestCase):
    """SceneTemplateRenderer (0x140a0de10): a template whose orientation is landscape (1,
    as 0x1406cf330 reads "landscape") is laid out on the transposed paper and painted from
    scene x -templateWidth / 2 (0x140a0c740), so it covers the paper's note bounds."""

    def test_the_variables_of_a_landscape_template_are_transposed(self):
        from viewer.templates import default_library, template_environment, template_left, template_size
        ls = default_library().get("LS Lines medium")
        self.assertEqual(ls.orientation, "landscape")
        self.assertEqual(template_size(ls, 1404, 1872), (1872, 1404))
        env = template_environment(ls, 1404, 1872)
        self.assertEqual({k: env[k] for k in ("templateWidth", "templateHeight", "paperOriginX", "paperOriginY",
                                              "paperWidth", "paperHeight", "parentWidth", "parentHeight")},
                         {"templateWidth": 1872, "templateHeight": 1404, "paperOriginX": 0, "paperOriginY": 0,
                          "paperWidth": 1872, "paperHeight": 1404, "parentWidth": 1872, "parentHeight": 1404})
        self.assertEqual(template_left(ls, 1404, 1872), 1404 / 2 - 1872 / 2)            # page x of scene -936
        portrait = default_library().get("P Lines medium")
        self.assertEqual(template_left(portrait, 1404, 1872), 0)
        self.assertEqual(template_environment(portrait, 1404, 1872)["templateWidth"], 1404)  # unchanged

    def test_a_landscape_template_covers_the_note_bounds(self):
        from viewer.templates import TemplatePath, default_library, template_shapes
        ls = default_library().get("LS Lines medium")
        area = (-234, 0, 1872, 1404)
        xs = [x for s in template_shapes(ls, area, 1404, 1872) if isinstance(s, TemplatePath)
              for _, pts in s.commands for x, _ in pts]
        self.assertGreaterEqual(min(xs), -234 - 1)
        self.assertLessEqual(max(xs), -234 + 1872 + 1)
        self.assertGreater(max(xs) - min(xs), 1404)            # wider than a portrait page

    def test_every_landscape_template_lays_out(self):
        from viewer.templates import default_library, template_shapes
        library = default_library()
        names = [n for n in library.names() if library.get(n).orientation == "landscape"] \
            if hasattr(library, "names") else []
        for name in names:
            shapes = list(template_shapes(library.get(name), (-234, 0, 1872, 1404), 1404, 1872))
            self.assertTrue(shapes or name.endswith("Blank"), name)


class LandscapePageTests(unittest.TestCase):
    """A page of a landscape document is QRectF(-h/2, 0, h, w) in the scene: the thumbnail
    job (0x14055d170) renders that rect into a transposed image; nothing is turned."""

    @classmethod
    def setUpClass(cls):
        pass                        # test_quick_offline made the QApplication

    def content(self, page: bytes | None, landscape=True, template="LS Lines medium"):
        from ui.pages import PageContent
        from viewer.templates import default_library
        scene = rm_parser.parse(page) if page is not None else None
        return PageContent(scene, None, None, default_library().get(template) if template else None,
                           landscape=landscape)

    def test_the_frame_is_the_note_bounds(self):
        content = self.content(None)
        self.assertEqual(content.frame(), QRectF(-234, 0, 1872, 1404))
        self.assertTrue(content.is_landscape)
        self.assertEqual(content.bounds("clip"), (-234, 0, 1872, 1404))
        self.assertEqual(self.content(None, landscape=False).frame(), QRectF(0, 0, 1404, 1872))

    def test_strokes_across_the_page_are_drawn_where_they_lie(self):
        from ui.pages import render_content
        # Scene x -900 .. 900 at y 1300: outside a portrait page, inside a landscape one.
        page = drawn([(-900, 1300), (900, 1300)])
        content = self.content(page, template=None)
        self.assertEqual(content.bounds("page"), (-234, 0, 1872, 1404))       # nothing grows it
        image = render_content(content, 0.5, "page")
        self.assertEqual((image.width(), image.height()), (936, 702))
        # Scene (0, 1300) = page (702, 1300) = image ((702 + 234) / 2, 650).
        self.assertLess(QColor(image.pixel(468, 650)).lightness(), 160)
        self.assertLess(QColor(image.pixel(30, 650)).lightness(), 160)           # scene x -880
        portrait = self.content(page, landscape=False, template=None)
        self.assertGreater(portrait.bounds("page")[2], 1404)                     # the ink grows a portrait page

    def test_thumbnails_and_exports_are_wide(self):
        from ui.export import export_pdf, page_svg
        from ui.pages import render_content
        content = self.content(drawn([(-500, 200), (500, 300)]))
        thumb = render_content(content, 0.1, "clip")
        self.assertEqual((thumb.width(), thumb.height()), (187, 140))
        svg = page_svg(content)
        self.assertIn('viewBox="-234 0 1872 1404"', svg)
        self.assertIn('width="1872" height="1404"', svg)

        class Pages:            # a DocumentPages of one page, no PDF
            pages, pdf, pdf_path = [object()], None, None
            doc = type("Doc", (), {"name": "Wide"})()

            def content(self, index):
                return content
        with tempfile.TemporaryDirectory() as tmp:
            out = export_pdf(Pages(), Path(tmp) / "wide.pdf")
            from pypdf import PdfReader
            box = PdfReader(str(out)).pages[0].mediabox
            self.assertAlmostEqual(float(box.width), 1872 * 72 / 226, places=1)
            self.assertAlmostEqual(float(box.height), 1404 * 72 / 226, places=1)

    def test_the_landscape_template_is_drawn_across_the_page(self):
        from ui.pages import render_content
        image = render_content(self.content(None), 0.25, "clip")
        self.assertEqual((image.width(), image.height()), (468, 351))
        dark = [x for x in range(image.width()) if QColor(image.pixel(x, 200)).lightness() < 250]
        row = [y for y in range(image.height()) if QColor(image.pixel(234, y)).lightness() < 250]
        self.assertTrue(row, "the lines template draws rules down the page")
        self.assertTrue(dark or row)


class LandscapeLibraryTests(unittest.TestCase):
    """DocumentPages takes the orientation from .content; new notebooks keep the
    constructor's customZoom* (Document ctor 0x14064f6xx: QRectF(-702, 0, 1404, 1872))."""

    @classmethod
    def setUpClass(cls):
        pass                        # test_quick_offline made the QApplication

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = te.build_cloud()
        self.cloud.put_item(WIDE, te.meta("wide"), landscape_content(WIDE_PAGE),
                            {f"{WIDE}/{WIDE_PAGE}.rm": drawn([(-800, 100), (800, 120)])})
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway", "wide"))
        self.source.refresh()

    def test_a_landscape_document_has_wide_pages(self):
        from ui.pages import DocumentPages
        pages = DocumentPages(self.source, self.sync.tree.get(WIDE))
        self.addCleanup(pages.close)
        content = pages.content(0)
        self.assertTrue(content.landscape)
        self.assertEqual(content.frame(), QRectF(-234, 0, 1872, 1404))
        self.assertEqual(content.template.name, "Lines medium")
        image = pages.render(0, 0.1, "clip")
        self.assertEqual((image.width(), image.height()), (187, 140))
        portrait = DocumentPages(self.source, self.sync.tree.get(THROWAWAY))
        self.addCleanup(portrait.close)
        self.assertFalse(portrait.content(0).landscape)

    def test_new_notebooks_keep_the_constructors_custom_zoom_on_any_paper(self):
        doc_id = "88888888-aaaa-4aaa-8aaa-888888888888"
        page_id = "88888888-0000-4000-8000-000000000001"
        node = self.source.create_notebook("Big", "", NOW, doc_id, page_id)
        self.assertTrue(self.source.set_page_paper(node, page_id, (1620, 2160), NOW))
        files, parent = self.source.upload_created(doc_id)
        self.sync.create_document(doc_id, files, parent)
        content = json.loads(self.cloud.item(doc_id)[f"{doc_id}.content"])
        self.assertEqual({k: content[k] for k in content if k.startswith("customZoom")},
                         {"customZoomCenterX": 0, "customZoomCenterY": 936, "customZoomOrientation": "portrait",
                          "customZoomPageHeight": 1872, "customZoomPageWidth": 1404, "customZoomScale": 1})


# -- the anchored group ------------------------------------------------------------------

class AnchoredGroupTests(unittest.TestCase):
    """viewer.rm_anchor.add_group and the AnchoredGroupAdd edit."""

    def test_the_blocks_are_the_tablets(self):
        page, node = rm_anchor.add_group(te.drawn_page(), author=te.TABLET, layer=None,
                                         char_id=rm_anchor.BEFORE_TEXT, origin_x=-464.0)
        blocks = w.split(page)[1]
        self.assertEqual(node, (1, 16))
        (nb,) = [b for b in blocks if b.type == w.BLOCK_TREE_NODE and rm_parser._read_fields(b.payload)[1].raw == node]
        # The real layout (a tablet's node 1:16, re-checked on 710 real nodes): empty label and
        # visible 1 at 0:0, anchorId stamped N+2, type N+3, threshold N+4, originX N.
        self.assertEqual(nb.payload.hex(), "1f01102c0a0000001f00002c0200000000013c050000001f000021017c0c0000001f01122f00"
                                           "feffffffffff3f8c01050000001f011321029c01080000001f011424fcfd0e42ac0108"
                                           "0000001f0110240000e8c3")
        moves = [b for b in blocks if b.type == w.BLOCK_TREE_MOVE]
        self.assertEqual(moves[-1].payload.hex(), "1f01102f000031014c030000001f000b")
        groups = [w.decode_item(b.payload) for b in blocks if b.type == w.BLOCK_GROUP_ITEM]
        self.assertEqual((groups[-1].parent, groups[-1].item), ((0, 11), (1, 17)))
        # A stroke pasted into it takes the next id after the anchor's stamps, not the
        # sentinel's counter (the page's clock does not take an anchor reference).
        from viewer import rm_select
        page2, ids = rm_select.append_items(page, author=te.TABLET, values=[rm_stroke.draw([(0, 0), (9, 9)], 17)],
                                            layer=node)
        self.assertEqual(ids, [(1, 21)])
        scene = rm_parser.parse(page2)
        (anchored,) = [s for s in scene.strokes if s.anchor is not None]
        self.assertEqual((anchored.anchor.char_id, anchored.anchor.type, anchored.anchor.origin_x),
                         (rm_anchor.BEFORE_TEXT, 2, -464.0))
        self.assertAlmostEqual(anchored.anchor.threshold, 35.748032, places=5)

    def test_a_sentinel_anchor_does_not_move_the_clock(self):
        page, _ = rm_anchor.add_group(te.drawn_page(), author=te.TABLET, layer=None,
                                      char_id=rm_anchor.AFTER_TEXT, origin_x=0.0)
        self.assertEqual(w.highest_counter(w.split(page)[1]), 20)

    def test_the_edit_is_kept_described_and_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = PendingEdits(Path(tmp))
            text = TextEdit.new(THROWAWAY, PAGE1, "insert", NOW, text="Hi\n\n")
            group = AnchoredGroupAdd.new(THROWAWAY, PAGE1, ("edit", text.uid, 3), -468.0, NOW)
            paste = StrokePaste.new(THROWAWAY, PAGE1, [rm_stroke.draw([(0, 10), (40, 20)], 17)], NOW,
                                    layer=("edit", group.uid))
            for e in (TextEdit.new(THROWAWAY, PAGE1, "create", NOW, paragraph=1), text, group, paste):
                pending.add(e)
            again = PendingEdits(Path(tmp))
            self.assertEqual(again.all()[2:], [group, paste])            # the file's "kind" is not the anchor type
            self.assertIn("anchored", describe_edit(group))
            self.assertIn("into an anchored group", describe_edit(paste))
            from core.edits import replay
            done = replay(te.drawn_page(), again.all(), w.author_uuid("00000000-0000-4000-8000-0000000000aa"))
            scene = rm_parser.parse(done.page)
            (anchored,) = [s for s in scene.strokes if s.anchor is not None]
            # Units 0..3 of "Hi\n\n": the second newline ends paragraph 1 (the empty one).
            self.assertEqual(scene.text.char_paragraph[anchored.anchor.char_id], 1)
            self.assertIn(group.uid, done.made)

    def test_a_group_whose_character_is_gone_is_not_made(self):
        from core.edits import replay
        group = AnchoredGroupAdd.new(THROWAWAY, PAGE1, ("edit", "nothing-typed", 0), 0.0, NOW)
        paste = StrokePaste.new(THROWAWAY, PAGE1, [rm_stroke.draw([(0, 10), (40, 20)], 17)], NOW,
                                layer=("edit", group.uid))
        done = replay(te.drawn_page(), [group, paste], w.author_uuid("00000000-0000-4000-8000-0000000000aa"))
        scene = rm_parser.parse(done.page)
        self.assertEqual(len(scene.strokes), 2)                       # the paste goes on the layer
        self.assertTrue(all(s.anchor is None for s in scene.strokes))


class MakeRoomTests(unittest.TestCase):
    """ui.textedit.make_room: 0x140a4ed10's newline count and styles."""

    @classmethod
    def setUpClass(cls):
        pass                        # test_quick_offline made the QApplication

    def session(self, text: str, cursor: int):
        from core.edits import Replay
        from ui import textedit
        from viewer.renderer import qt_measure
        s = textedit.TextSession.open(THROWAWAY, PAGE1, None, Replay(None, {}, {}), te.TABLET, None, qt_measure,
                                      lambda: NOW, style=rm_parser.ParagraphStyle.PARAGRAPH)
        for n, part in enumerate(text.split("\n")):
            if n:
                s.enter()
            if part:
                s.type_text(part)
        s.place(cursor)
        return s

    def test_at_the_end_of_the_text(self):
        from ui import textedit
        s = self.session("Hello", 5)
        room = textedit.make_room(s, 200.0)
        n = 3                                                       # ceil(200 / 69.546875)
        self.assertEqual(s.plain(), "Hello" + "\n" * (n + 1))        # one more at the end of the text
        self.assertEqual(room.count, n + 1)
        edits, steps = s.take()
        self.assertTrue(any(e.op == "insert" and e.text == "\n" * (n + 1) for e in edits))
        self.assertEqual(len(steps), 2)                             # the typing, then the room: one step

    def test_in_the_middle_of_a_paragraph_one_more_line_and_the_style_back(self):
        from ui import textedit
        s = self.session("abcd", 2)
        s.set_style(rm_parser.ParagraphStyle.BULLETPOINT)
        bullet = s.stored_style(0)
        textedit.make_room(s, 100.0)                                # ceil(100 / 69.55) + 1 = 3
        self.assertEqual(s.plain(), "ab\n\n\ncd")
        self.assertEqual(s.stored_style(len("ab\n\n\n")), bullet)    # "cd" keeps the bullet
        self.assertNotEqual(s.stored_style(len("ab\n")), bullet)     # the room is body text

    def test_on_an_empty_line_its_height_counts(self):
        from ui import textedit
        s = self.session("ab\n\ncd", 3)
        self.assertEqual((s.plain(), s.paragraph_range(3)), ("ab\n\ncd", (3, 3)))
        empty = textedit._block_rect(s, 3)
        room = textedit.make_room(s, 150.0)
        # n = ceil((150 - the empty line's height) / 69.546875) new lines after it.
        n = -(-(150.0 - (empty[1] - empty[0])) // textedit.APP_BODY_PITCH)
        self.assertEqual(room.count, n)
        self.assertEqual(s.plain(), "ab\n" + "\n" * (1 + int(n)) + "cd")
        lines = s.lines()
        self.assertGreater(room.y, lines[0].baseline)               # below "ab"
        self.assertLess(room.y, lines[-1].baseline)                 # above "cd"


# -- the bridge stubs -------------------------------------------------------------------

class StubTests(unittest.TestCase):
    """Members that are no-ops no more, checked without the QML."""

    @classmethod
    def setUpClass(cls):
        pass                        # test_quick_offline made the QApplication

    def test_should_show_whats_new_as_the_app(self):
        from ui.quick.bridge.platform import PlatformApp
        app = PlatformApp()
        for read, version, show in (("0", "3.29", True), ("3.28", "3.29", True), ("3.29", "3.29", False),
                                    ("3.30", "3.29", False), ("", "3.29", True), ("1.2.3", "3.29", True),
                                    ("3.9", "3.29", False), ("3.2", "3.29", True), (None, "3.29", True)):
            app.whats_new_read = lambda r=read: r
            self.assertEqual(app.shouldShowWhatsNew(version), show, (read, version))

    def test_open_file_chooser_hands_files_over_as_file_import_dialog_does(self):
        from ui.quick.bridge.platform import PlatformHelpers
        helpers = PlatformHelpers()
        got = []
        helpers.documentAccepted.connect(lambda *a: got.append(a))
        helpers.import_folder = lambda: "folder-id"
        helpers.choose_files = lambda done: done([QUrl.fromLocalFile("/tmp/a.pdf"), QUrl("https://example.invalid/x"),
                                                  QUrl.fromLocalFile("/tmp/b.epub")])
        helpers.openFileChooser()
        self.assertEqual([(u.toLocalFile(), *rest) for u, *rest in got],
                         [("/tmp/a.pdf", "", "folder-id", False, False), ("/tmp/b.epub", "", "folder-id", False, False)])

    def test_set_inhibit_sleep_uses_the_screensaver_interface(self):
        from ui.quick.bridge.platform import PlatformHelpers, ScreenSaverInhibitor
        helpers = PlatformHelpers()
        calls = []
        helpers.inhibit_sleep = lambda on, why: calls.append((on, why)) or True
        helpers.setInhibitSleep(True, "Screen Share")
        helpers.setInhibitSleep(False, "")
        self.assertEqual(calls, [(True, "Screen Share"), (False, "")])

        class Reply:
            def __init__(self, values, error=""):
                self.values, self.error = values, error

            def arguments(self):
                return self.values

            def errorName(self):
                return self.error

        class Interface:
            def __init__(self):
                self.calls = []

            def call(self, method, *args):
                self.calls.append((method, *args))
                return Reply([42] if method == "Inhibit" else [])

        fake = Interface()
        inhibitor = ScreenSaverInhibitor()
        inhibitor._interface = lambda: fake
        self.assertTrue(inhibitor.set(True, "why"))
        self.assertTrue(inhibitor.set(True, "again"))               # one inhibition at a time
        self.assertTrue(inhibitor.set(False, ""))
        self.assertTrue(inhibitor.set(False, ""))
        self.assertEqual(fake.calls, [("Inhibit", "reMarkable", "why"), ("UnInhibit", 42)])
        inhibitor._interface = lambda: None                         # no session bus
        self.assertFalse(inhibitor.set(True, "x"))

    def test_selection_image_is_the_selection_on_transparent_pixels(self):
        from PySide6.QtGui import QTransform
        from ui.quick.bridge.scene import SelectionImage
        image_item = SelectionImage()
        self.assertTrue(image_item.image().isNull())
        scene = rm_parser.parse(drawn([(10, 10), (60, 40)]))

        class Worker:
            def selection_preview(self):
                return scene, scene.x_origin
        image_item.updateImage(Worker(), 0, 0, QRectF(0.4, 0.4, 80, 60), QTransform())
        image = image_item.image()
        self.assertEqual((image.width(), image.height()), (81, 61))       # toAlignedRect of the scene rect
        self.assertEqual(image.format(), QImage.Format.Format_ARGB32_Premultiplied)
        self.assertEqual(QColor.fromRgba(image.pixel(75, 55)).alpha(), 0)
        self.assertGreater(QColor.fromRgba(image.pixel(35, 25)).alpha(), 0)   # on the stroke
        image_item.reset()
        self.assertTrue(image_item.image().isNull())

    def test_the_audit_finds_no_undocumented_no_op(self):
        import bridge_stub_audit as audit
        rows = [f"{c}.{m}" for c, m in audit.generated_stubs()] + [f"{c}.{m}" for _, c, m in audit.handwritten_noops()]
        self.assertEqual([r for r in rows if r not in audit.DOCUMENTED], [])
        for name in ("setScrollPosition", "setZoomMode", "setCustomZoomFit", "countArchivedDocuments",
                     "purgeThumbnails", "scrollPosition", "setViewBackgroundFilter", "openFileChooser",
                     "setInhibitSleep", "image", "shouldShowWhatsNew"):
            self.assertFalse(any(r.endswith("." + name) for r in rows), name)
        architecture = (HERE.parents[1] / "ARCHITECTURE.md").read_text()
        missing = [m for m in set(audit.SKIPPED) | set(audit.SKIPPED_WHOLE) if m.split(".")[-1] not in architecture]
        self.assertEqual(missing, [])


class SettingsAndLauncherTests(unittest.TestCase):
    """A normal start: nothing limits editing but Settings > Editing."""

    def test_no_option_and_no_environment_limit_nothing(self):
        import run
        self.assertEqual(run.edit_notebooks(run.parse_args([]), {}), ())
        self.assertEqual(run.edit_notebooks(run.parse_args(["--offline"]), {"HOME": "/x"}), ())
        # Still there for someone who passes it by hand.
        self.assertEqual(run.edit_notebooks(run.parse_args(["--edit-notebook", "throwaway"]), {}), ("throwaway",))
        self.assertEqual(run.edit_notebooks(argparse.Namespace(edit_notebook=[]),
                                            {"REMARKABLE_EDIT_NOTEBOOK": "throwaway"}), ("throwaway",))

    def test_the_installed_launcher_limits_nothing(self):
        installer = (HERE.parents[1] / "install.py").read_text()
        start = installer.index("def install_launcher")
        launcher = installer[start:installer.index("\ndef ", start + 1)]
        self.assertNotIn("--edit-notebook", launcher)
        self.assertNotIn("REMARKABLE_EDIT_NOTEBOOK", launcher)
        self.assertNotIn("throwaway", launcher)

    def test_settings_decide_on_a_normal_start(self):
        from ui.outgoing import EditSettings, OutgoingSync
        from ui.quick.bridge.platform import SettingsStore
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cloud = te.build_cloud()
            sync = SyncClient(paired_auth(tmp / "auth", cloud), tmp / "cache")
            import run
            for on in (False, True):
                settings = EditSettings(SettingsStore(tmp / "config" / "settings.ini"))
                settings.editing_enabled = on
                source = LibrarySource(sync, editable=run.edit_notebooks(run.parse_args([]), {}))
                source.refresh()
                out = OutgoingSync(source, settings)
                self.addCleanup(out.shutdown)
                self.assertEqual(out.property("editingLimitedTo"), "")
                for doc_id in (THROWAWAY, te.OTHER):
                    self.assertEqual(source.can_edit(sync.tree.get(doc_id)), on, (doc_id, on))


# -- through the app's own QML -------------------------------------------------------------

class QuickV1Tests(unittest.TestCase):
    """The wired members as the app's QML and singletons reach them."""

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = te.build_cloud()
        cls.cloud.put_item(WIDE, te.meta("wide"), landscape_content(WIDE_PAGE),
                           {f"{WIDE}/{WIDE_PAGE}.rm": drawn([(-800, 100), (800, 120)])})
        cls.cloud.put_item(LEGACY, te.meta("legacy"), {"fileType": "notebook", "formatVersion": 1, "pages": [PAGE1],
                                                       "pageCount": 1, "orientation": "portrait"}, {})
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway", "wide", "legacy"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.singletons.settings.setRawValue("Onboarding", "ClientEdit", True)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        tq.wait(50)
        cls.tmp.cleanup()

    def tearDown(self):
        view = tq.EditingTests.qml(self, "DocumentView_")
        if view.property("visible"):
            QMetaObject.invokeMethod(view, "hide")
            self.wait_for(lambda: not view.property("visible"))

    def wait_for(self, condition, ms=8000):
        return tq.EditingTests.wait_for(self, condition, ms)

    def qml(self, prefix: str):
        return tq.EditingTests.qml(self, prefix)

    def scene_views(self):
        return tq.EditingTests.scene_views(self)

    def content(self, doc_id: str) -> dict:
        return json.loads(self.cloud.item(doc_id)[f"{doc_id}.content"])

    def entry(self, doc_id: str):
        return self.app.library.entryForId(doc_id)

    def test_a_landscape_notebook_opens_wide(self):
        view = tq.PenTests.open_document(self, WIDE, WIDE_PAGE)
        controller = view._controller
        self.assertEqual(controller.property("primaryContentRect"), QRectF(-936, 0, 1872, 1404))
        self.assertEqual(controller.property("paperNoteBounds"), QRectF(-936, 0, 1872, 1404))
        self.assertEqual(self.entry(WIDE).property("orientation"), int(Qt.Orientation.Horizontal.value))
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def test_zoom_mode_and_custom_fit(self):
        controller = self.app.singletons.controller
        self.assertFalse(controller.setZoomMode(THROWAWAY, 4))                 # "Incorrect zoom mode set"
        self.assertTrue(controller.setZoomMode(THROWAWAY, 1))
        self.assertTrue(self.wait_for(lambda: self.content(THROWAWAY).get("zoomMode") == "fitToWidth"))
        self.assertEqual(self.entry(THROWAWAY).property("zoomMode"), 1)
        self.assertTrue(controller.setCustomZoomFit(THROWAWAY, 1.5, QPointF(10, 20), QSizeF(1404, 1872)))
        self.assertTrue(self.wait_for(lambda: self.content(THROWAWAY).get("zoomMode") == "customFit"))
        content = self.content(THROWAWAY)
        self.assertEqual((content["customZoomScale"], content["customZoomCenterX"], content["customZoomCenterY"],
                          content["customZoomPageWidth"], content["customZoomPageHeight"],
                          content["customZoomOrientation"]), (1.5, 10, 20, 1404, 1872, "portrait"))
        self.assertEqual(self.cloud.metadata(THROWAWAY)["lastModified"], str(te.THEN))    # not moved
        self.assertFalse(controller.setZoomMode("no-such-document", 0))

    def test_view_background_filter(self):
        dc = self.app.singletons.document_controller
        self.assertTrue(dc.setViewBackgroundFilter(te.OTHER, "invert"))
        self.assertTrue(self.wait_for(lambda: self.content(te.OTHER).get("viewBackgroundFilter") == "invert"))
        self.assertEqual(self.entry(te.OTHER).property("viewBackgroundFilter"), "invert")
        self.assertTrue(dc.setViewBackgroundFilter(te.OTHER, ""))
        self.assertTrue(self.wait_for(lambda: "viewBackgroundFilter" not in self.content(te.OTHER)))
        self.assertFalse(dc.setViewBackgroundFilter("no-such-document", "x"))

    def test_scroll_position_is_kept_per_page_for_eight_days(self):
        controller = self.app.singletons.controller
        now = datetime.now(timezone.utc)
        self.assertFalse(controller.setScrollPosition(THROWAWAY, 5, 10.0))      # no such page
        self.assertTrue(controller.setScrollPosition(THROWAWAY, 1, 1234.5))
        self.assertTrue(self.wait_for(lambda: "verticalScroll" in self.content(THROWAWAY)["cPages"]["pages"][1]))
        page = self.content(THROWAWAY)["cPages"]["pages"][1]
        self.assertEqual(page["verticalScroll"]["value"], 1234.5)
        self.assertEqual(page["verticalScroll"]["timestamp"], page["scrollTime"]["timestamp"])
        when = datetime.fromisoformat(page["scrollTime"]["value"].replace("Z", "+00:00"))
        self.assertLess(abs((when - now).total_seconds()), 120)
        self.assertEqual(self.entry(THROWAWAY).scrollPosition(1), 1234.5)
        self.assertIsNone(self.entry(THROWAWAY).scrollPosition(0))                 # never set
        from core.pagemap import page_scroll
        content = self.content(THROWAWAY)
        self.assertEqual(page_scroll(content, 1, now + timedelta(days=7)), 1234.5)
        self.assertIsNone(page_scroll(content, 1, now + timedelta(days=8, hours=1)))
        # A formatVersion 1 document gets its page list first, as on load.
        self.assertTrue(controller.setScrollPosition(LEGACY, 0, 50.0))
        self.assertTrue(self.wait_for(lambda: self.content(LEGACY).get("formatVersion") == 2))
        self.assertEqual(self.content(LEGACY)["cPages"]["pages"][0]["verticalScroll"]["value"], 50.0)

    def test_archived_documents_and_purged_thumbnails(self):
        library = self.app.library
        self.assertEqual(library.countArchivedDocuments(), 0)
        node = library.tree.get(te.OTHER)
        from dataclasses import replace
        library.tree.nodes[te.OTHER] = replace(node, files={**node.files, f"{te.OTHER}.cloudarchive": "0" * 64})
        try:
            self.assertEqual(library.countArchivedDocuments(), 1)
        finally:
            library.tree.nodes[te.OTHER] = node
        folder = library.thumbnail_dir
        folder.mkdir(parents=True, exist_ok=True)
        mine = folder / f"{library.tree.get(THROWAWAY).hash}-328-p0-v3.png"
        other = folder / "not-a-document-hash.png"
        mine.write_bytes(b"x")
        other.write_bytes(b"x")
        library.purgeThumbnails()
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())


class WhatsNewTests(unittest.TestCase):
    """With 3.29's own default ("0" read), a first start shows the release notes."""

    @classmethod
    def setUpClass(cls):
        from core import config
        from ui.quick.app import QuickApp
        cls.saved = config.WHATS_NEW_READ_DEFAULT
        config.WHATS_NEW_READ_DEFAULT = "0"
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = te.build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window

    @classmethod
    def tearDownClass(cls):
        from core import config
        config.WHATS_NEW_READ_DEFAULT = cls.saved
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        tq.wait(50)
        cls.tmp.cleanup()

    def test_the_dialog_shows_and_is_marked_read(self):
        dialog = tq.EditingTests.qml(self, "WhatsNewDialog_")
        self.assertTrue(tq.EditingTests.wait_for(self, lambda: dialog.property("visible")))
        QMetaObject.invokeMethod(dialog, "markWhatsNewAsRead")
        helpers = self.app.singletons.platform_helpers
        self.assertEqual(helpers.getValue(4, "0"), "3.29")
        self.assertFalse(self.app.singletons.platform_app.shouldShowWhatsNew("3.29"))


if __name__ == "__main__":
    unittest.main()
