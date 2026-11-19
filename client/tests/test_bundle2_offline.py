"""Offline tests for canvas, pen and text polish (synthetic ids only):

- the Shader (Line::Tool ShadingMarker, 23) in the pen toolbar: drawn, stored, shown,
  erased, moved and pasted (its pieces' sourceId renamed as the app renames it);
- Hide template (SceneController.backgroundVisible): the template or the PDF page left
  out of the page drawn, nothing else, from the page's own SceneInfo value;
- the calligraphy nib turned by the right-hand setting and the page's rotation;
- the eraser reaching strokes in groups anchored to typed text, with undo and redo;
- clicking a checkbox (toggleCheckboxByIndex), with undo and redo;
- Shift+Enter typing a line break (U+2028) inside the paragraph.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import dataclasses
import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import QEvent, QObject, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

import test_edits_offline as te  # noqa: E402
from core.edits import StrokeErase, TextEdit, document_files, replay  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from rmfixtures import (group_item, paragraph_format, points_v2, root_text, scene_tree, text_item,  # noqa: E402
                        tree_node)
from ui import pen as pens  # noqa: E402
from ui import textedit  # noqa: E402
from ui.library import LibrarySource, _node_anchors  # noqa: E402
from ui.pages import PageContent, render_content  # noqa: E402
from ui.quick.bridge import pen as pen_bridge  # noqa: E402
from ui.quick.bridge import scene as scene_bridge  # noqa: E402
from viewer import renderer  # noqa: E402
from viewer import rm_select as sel  # noqa: E402
from viewer import rm_stroke as s  # noqa: E402
from viewer import rm_text  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import Color, ParagraphStyle, Tool, parse  # noqa: E402
from viewer.templates import default_library  # noqa: E402

TABLET = bytes(range(16))                    # the page's author (index 1), as test_edits_offline
DESKTOP = bytes.fromhex("ffeeddccbbaa99887766554433221100")
LAYER, ZERO = (0, 11), (0, 0)
GROUP = (1, 31)                              # a group anchored to "Hello"
ANCHORED, LOOSE = (1, 33), (1, 34)           # a stroke in the group, one on the layer
ORIGIN_X = -400.0
TEXT = "Title\nTask\nHello"                  # ids 1:15 .. 1:30; "Task" (a checkbox) starts at 6
TASK, HELLO = 6, 11
THROWAWAY, PAGE1, PAGE2 = te.THROWAWAY, te.PAGE1, te.PAGE2
NOW = te.NOW
TEMPLATE = "P Lines medium"


def _item(data: bytes, **header) -> bytes:
    _, [block] = w.split(w.V6_HEADER + data)
    item = dataclasses.replace(w.decode_item(block.payload), **header)
    return w.new_block(block.type, item.encode()).encode()


def _line(parent, item, points, left=ZERO, tool=s.FINELINER_V2, color=0) -> bytes:
    from rmfixtures import line_item
    return _item(line_item(parent, item, tool=tool, color=color, points=points_v2(points)), left=left)


def text_page(checked: bool = False) -> bytes:
    """A tablet page: a text box ("Title", a checkbox "Task", "Hello"), a group anchored
    to the "H" of "Hello" holding a level stroke 200 long (stored relative to the
    anchor: y -10), and a stroke on the layer itself far below."""
    header, blocks = w.split(w.blank_page(TABLET))
    text = root_text([text_item((1, 15), ZERO, text=TEXT)],
                     [paragraph_format(ZERO, 2), paragraph_format((1, 20), 7 if checked else 6),
                      paragraph_format((1, 25), 1)])
    level = [(x, -10.0, 0, 0, 3, 255) for x in range(0, 201, 10)]
    return (w.join(header, blocks) + scene_tree(GROUP, LAYER) + tree_node(GROUP, "", True, anchor=((1, 26), ORIGIN_X))
            + _item(group_item(LAYER, (1, 32), GROUP)) + text
            + _line(GROUP, ANCHORED, level)
            + _line(LAYER, LOOSE, [(-300.0, 1500.0, 0, 0, 3, 255), (300.0, 1500.0, 0, 0, 3, 255)], left=(1, 32)))


def offsets_of(data: bytes, measure=renderer.estimate_width) -> dict:
    """Where the page draws its anchored strokes: stroke id -> anchor position."""
    scene = parse(data)
    by_object = renderer.stroke_offsets(scene, renderer.layout_text(scene, measure))
    return {tuple(x.item_id): by_object[id(x)] for x in scene.strokes if id(x) in by_object}


def across(x: float, y: float, thickness: float = s.MEDIUM) -> s.LineValue:
    """An eraser taken straight down through (x, y)."""
    return s.eraser_line([(x, y - 40.0), (x, y), (x, y + 40.0)], thickness)


def strokes_by_parent(data: bytes) -> dict:
    out: dict = {}
    for x in s.strokes(data):
        out.setdefault(x.parent, []).append(x)
    return out


def blocks_of(data: bytes, kind: int) -> list[bytes]:
    return [b.encode() for b in w.split(data)[1] if b.type == kind]


def text_cloud(template: str = TEMPLATE) -> FakeCloud:
    content = te.notebook_content(PAGE1, PAGE2)
    for entry in content["cPages"]["pages"]:
        entry["template"]["value"] = template
    cloud = FakeCloud()
    cloud.put_item(THROWAWAY, te.meta("throwaway"), content, {f"{THROWAWAY}/{PAGE1}.rm": text_page()})
    return cloud


# ------------------------------------------------------------------ 1. the shader

class ShaderTests(unittest.TestCase):
    def test_the_toolbar_offers_the_shader(self):
        shader = pens.BY_KEY["shader"]
        self.assertEqual((shader.tool, shader.name, shader.icon, shader.label), (23, "ShadingMarker", "shader", "Shader"))
        self.assertEqual([t.key for t in pens.TOOLS][-2:], ["shader", "eraser"])
        self.assertEqual([c.key for c in shader.colors], [c.key for c in pens.PEN_COLORS])
        pen = pens.pen_of({}, shader)
        self.assertEqual((pen.size, pen.color.name, pen.line_color), (2, "Gray", int(Color.GRAY)))
        self.assertEqual(pens.tool_values(shader), {"LastTool": "ShadingMarker", "LastPen": "ShadingMarker"})
        self.assertEqual(pens.color_values(shader, "blue"), {"LastShadingMarkerColor": "Blue"})
        self.assertIs(pens.current_tool({"LastTool": "ShadingMarker"}), shader)
        settings = {"LastShadingMarkerSize": "3", "LastShadingMarkerColor": "ArgbCode",
                    "LastShadingMarkerColorCode": str(0x4020F21C)}
        self.assertEqual((pens.pen_of(settings, shader).size, pens.pen_of(settings, shader).argb), (3, 0x4020F21C))
        icon = HERE.parent / "src" / "assets" / "icons" / "icons" / "shader"
        if icon.parent.is_dir():
            self.assertTrue(icon.exists(), "the app's shader icon")

    def test_a_shader_stroke_is_made_stored_and_drawn(self):
        samples = [(-100.0 + 5 * n, 300.0, 0.05 + n * 0.02) for n in range(40)]
        line = s.draw(samples, s.SHADING_MARKER, int(Color.GRAY), s.MEDIUM)
        self.assertEqual((line.tool, line.color, line.thickness_scale, line.source_id), (23, 1, 2.0, None))
        widths = [p.width / 4 for p in line.unpacked()]
        self.assertAlmostEqual(min(widths), (10 * 2 + 2) / 3, delta=0.3)     # light pressure: a third of 10T + 2
        self.assertAlmostEqual(max(widths), 10 * 2 + 2, delta=0.3)         # from 0.3 on: all of it
        self.assertEqual(s.decode_line_value(line.encode()), line)
        page = s.append_stroke(None, author=DESKTOP, line=line)
        [stroke] = parse(page).strokes
        self.assertEqual((stroke.tool, stroke.color), (Tool.SHADING_MARKER, Color.GRAY))
        style = renderer.stroke_style(stroke, "#ffffff")
        self.assertTrue(style.blend_darken)
        image = renderer.render_image(parse(page), crop="clip")
        pixel = QColor(image.pixel(round(702 - 40), 300))
        self.assertLess(pixel.red(), 250)                                     # shaded
        self.assertGreater(pixel.red(), 150)                                  # lightly
        # The pen surface previews it with the page renderer.
        preview = pen_bridge.preview_scene(23, 1, 2.0, None, line.unpacked())
        self.assertEqual(preview.strokes[0].tool, Tool.SHADING_MARKER)

    def pieces_page(self):
        page = s.append_stroke(None, author=DESKTOP,
                               line=s.draw([(-100.0 + 7.3 * n, 100.0) for n in range(28)], s.SHADING_MARKER, 1))
        erased = s.erase(page, author=DESKTOP, eraser=across(0.25, 100.0))
        self.assertEqual({x.line.source_id for x in s.strokes(erased.page)}, {(1, 15)})
        return erased

    def test_moving_shader_pieces_renames_their_source_before_each_add(self):
        erased = self.pieces_page()
        clock = w.highest_counter(w.split(erased.page)[1])
        out, made, _ = sel.transform_items(erased.page, author=DESKTOP, items=erased.made,
                                           m=sel.translation(10.0, 0.0))
        # 0x1408cb8d0 before the clones (one id), the clones, again before the moved ones.
        self.assertEqual(sorted(made.values()), [(1, clock + 5), (1, clock + 6)])
        moved = [x.line for x in s.strokes(out) if x.item in made.values()]
        self.assertEqual({p.source_id for p in moved}, {(1, clock + 4)})
        self.assertEqual({p.move_id for p in moved}, set(erased.made))
        clones = [i for i in w.split(out)[1] if i.type == w.BLOCK_LINE_ITEM
                  and w.decode_item(i.payload).item == (1, clock + 2)]
        self.assertEqual(w.decode_item(clones[0].payload).deleted_length, 2)

    def test_pasting_shader_pieces_renames_their_source(self):
        erased = self.pieces_page()
        clock = w.highest_counter(w.split(erased.page)[1])
        values = [sel.clone(x.line) for x in s.strokes(erased.page)]
        out, ids = sel.append_items(erased.page, author=DESKTOP, values=values)
        self.assertEqual(ids, [(1, clock + 2), (1, clock + 3)])
        self.assertEqual({x.line.source_id for x in s.strokes(out) if x.item in ids}, {(1, clock + 1)})
        # A pen stroke has no sourceId: nothing to rename, no id taken.
        plain = s.draw([(0.0, 500.0), (50.0, 500.0)], s.FINELINER_V2)
        _, [only] = sel.append_items(erased.page, author=DESKTOP, values=[plain])
        self.assertEqual(only, (1, clock + 1))


# ------------------------------------------------------------------ 2. hide template

class BackgroundTests(unittest.TestCase):
    def hidden_page(self) -> bytes:
        data = w.blank_page(TABLET)
        shown = b"\x2c\x05\x00\x00\x00\x1f\x00\x00\x21\x01"        # SceneInfo field 2: {ts 0:0, true}
        self.assertEqual(data.count(shown), 1)
        return data.replace(shown, b"\x2c\x05\x00\x00\x00\x1f\x01\x0f\x21\x00")

    def test_the_page_says_whether_its_template_is_shown(self):
        self.assertTrue(parse(w.blank_page(TABLET)).background_visible)
        self.assertFalse(parse(self.hidden_page()).background_visible)
        self.assertTrue(parse(text_page()).background_visible)

    @unittest.skipUnless(default_library().get(TEMPLATE) is not None, "the app's templates are not installed")
    def test_hiding_leaves_out_the_template_only(self):
        scene = parse(text_page())
        content = PageContent(scene, None, None, default_library().get(TEMPLATE))

        def dark(image: QImage) -> int:
            return sum(1 for y in range(0, image.height(), 3) for x in range(0, image.width(), 3)
                       if QColor(image.pixel(x, y)).lightness() < 200)

        shown = render_content(content, 0.5, "page")
        hidden = render_content(content, 0.5, "page", background=False)
        bare = render_content(dataclasses.replace(content, template=None), 0.5, "page")
        self.assertEqual((shown.width(), shown.height()), (hidden.width(), hidden.height()))
        self.assertGreater(dark(shown), dark(hidden))
        self.assertEqual(hidden, bare)                 # ink and text drawn all the same
        self.assertGreater(dark(hidden), 0)


# ------------------------------------------------------------------ 3. calligraphy nib

class NibTests(unittest.TestCase):
    SAMPLES = [(100.0 + 6 * n, 100.0 + 4 * n, 0.8) for n in range(30)]

    def widths(self, **how) -> list[int]:
        return [p.width for p in s.draw(self.SAMPLES, s.CALLIGRAPHY, 0, s.MEDIUM, **how).unpacked()]

    def test_the_hand_and_the_rotation_turn_the_nib(self):
        right, left = self.widths(), self.widths(right_handed=False)
        self.assertNotEqual(right, left)
        self.assertEqual(self.widths(right_handed=True, rotation=130.0), left)       # 40 + 130 = 170
        self.assertNotEqual(self.widths(rotation=90.0), right)

    def test_rotation_read_off_the_transform_as_the_handler_reads_it(self):
        self.assertEqual(s.rotation_of(2.0, 0.0, 0.0, 2.0), 0.0)
        self.assertEqual(s.rotation_of(-1.0, 0.0, 0.0, -1.0), 180.0)
        self.assertEqual(s.rotation_of(1.0, 0.0, 0.0, -1.0), 180.0)        # mirrored: not 0
        self.assertEqual(s.rotation_of(0.0, 1.0, -1.0, 0.0), 90.0)
        self.assertEqual(s.rotation_of(0.0, -1.0, 1.0, 0.0), 270.0)
        self.assertIsNone(s.rotation_of(0.7, 0.7, -0.7, 0.7))              # "unknown rotation"

    def test_the_pen_surface_hands_both_to_the_stroke(self):
        pen = pens.pen_of({}, pens.BY_KEY["calligraphy"])
        drawing = pen_bridge._Drawing(pen, False, 1.0, False, right_handed=False, rotation=90.0)
        self.assertEqual((drawing.builder.right_handed, drawing.builder.rotation), (False, 90.0))
        self.assertIsNone(pen_bridge._Drawing(pen, True, 1.0, False).builder)          # the eraser makes no points


# ------------------------------------------------------------------ 4. anchored strokes

class AnchoredEraseTests(unittest.TestCase):
    def drawn_at(self, data: bytes) -> tuple[float, float]:
        """Where the middle of the anchored stroke is drawn (scene coordinates)."""
        dx, dy = offsets_of(data)[ANCHORED]
        return 100.25 + dx, -10.0 + dy          # off the stroke's points (an eraser edge on one keeps it)

    def test_the_eraser_cuts_anchored_strokes_where_they_are_drawn(self):
        data = text_page()
        x, y = self.drawn_at(data)
        anchors = _node_anchors(data, offsets_of(data))
        self.assertEqual(list(anchors), [GROUP])
        # Without the anchor's position the group is left alone (and so was it before).
        self.assertEqual(s.erase(data, author=DESKTOP, eraser=across(x, y)).deleted, ())
        erased = s.erase(data, author=DESKTOP, eraser=across(x, y), anchors=anchors)
        self.assertEqual(erased.deleted, (ANCHORED,))
        groups = strokes_by_parent(erased.page)
        self.assertEqual([x.item for x in groups[GROUP]], [(2, 35), (2, 36)])
        self.assertEqual([x.item for x in groups[LAYER]], [LOOSE])
        left, right = [x.line.unpacked() for x in groups[GROUP]]
        # Still in the group's own coordinates: level at y -10, cut around x 100.
        self.assertEqual({p.y for p in left + right}, {-10.0})
        self.assertLess(left[-1].x, 100.0)
        self.assertGreater(right[0].x, 100.0)
        self.assertEqual(left[0].x, 0.0)
        self.assertEqual(right[-1].x, 200.0)
        # Put right after the erased stroke in the group's sequence; the anchor, the
        # group item and the text are kept byte for byte.
        order = [i for i, _ in w.sequence(w.split(erased.page)[1], GROUP)]
        self.assertEqual(order, [ANCHORED, (2, 35), (2, 36)])
        for kind in (w.BLOCK_TREE_NODE, w.BLOCK_TREE_MOVE, w.BLOCK_ROOT_TEXT, w.BLOCK_GROUP_ITEM):
            self.assertEqual(blocks_of(erased.page, kind), blocks_of(data, kind))
        scene = parse(erased.page)
        self.assertEqual(len([x for x in scene.strokes if x.anchor is not None]), 2)

    def test_an_eraser_over_both_kinds_takes_the_layers_first(self):
        data = text_page()
        x, y = self.drawn_at(data)
        line = s.eraser_line([(x, y), (x, 1500.0), (-200.0, 1500.0)], 3.0)
        erased = s.erase(data, author=DESKTOP, eraser=line, anchors=_node_anchors(data, offsets_of(data)))
        self.assertEqual(erased.deleted, (LOOSE, ANCHORED))

    def test_a_plain_group_below_the_layer_is_erased_too(self):
        header, blocks = w.split(w.blank_page(TABLET))
        plain = (1, 40)
        data = (w.join(header, blocks) + scene_tree(plain, LAYER) + tree_node(plain, "", True)
                + _item(group_item(LAYER, (1, 41), plain))
                + _line(plain, (1, 42), [(0.0, 100.0, 0, 0, 3, 255), (100.0, 100.0, 0, 0, 3, 255)]))
        erased = s.erase(data, author=DESKTOP, eraser=across(50.0, 100.0))
        self.assertEqual(erased.deleted, ((1, 42),))
        self.assertEqual({x.parent for x in s.strokes(erased.page)}, {plain})


class AnchoredEraseEditTests(unittest.TestCase):
    """Through the library: one edit with the anchor positions, one undo step."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = text_cloud()
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway",))
        self.doc = self.source.refresh().get(THROWAWAY)

    def page(self, source=None) -> bytes:
        source = source or self.source
        return source.staged(source.sync.tree.get(THROWAWAY)).page(PAGE1)

    def live(self) -> dict:
        return {p: sorted(x.line.points for x in xs) for p, xs in strokes_by_parent(self.page()).items()}

    def test_erase_undo_redo_and_replay(self):
        data = self.page()
        before = self.live()
        offsets = offsets_of(data)
        dx, dy = offsets[ANCHORED]
        edit = self.source.erase_strokes(self.doc, PAGE1, across(100.25 + dx, -10.0 + dy), NOW, offsets)
        self.assertIsInstance(edit, StrokeErase)
        self.assertEqual(edit.anchors, ((GROUP[0], GROUP[1], dx, dy),))
        erased = self.live()
        self.assertEqual(len(erased[GROUP]), 2)
        self.assertEqual(erased[LAYER], before[LAYER])
        # Kept in the ledger with its anchors: a restart makes the same page.
        raw = json.loads(json.dumps({"kind": "stroke-erase", **dataclasses.asdict(edit)}))
        self.assertEqual(StrokeErase.load(raw), edit)
        restarted = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                  editable=("throwaway",))
        restarted.refresh()
        self.assertEqual(self.page(restarted), self.page())
        # An edit kept before anchors were recorded leaves the anchored group alone.
        old = replay(data, [dataclasses.replace(edit, anchors=())], DESKTOP).page
        self.assertEqual(strokes_by_parent(old)[GROUP][0].item, ANCHORED)
        # Undo: the stroke back in its group (a new item named after it); redo: cut again.
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 1))
        self.assertEqual(self.live(), before)
        [back] = strokes_by_parent(self.page())[GROUP]
        self.assertEqual((back.parent, back.line.move_id), (GROUP, ANCHORED))
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 2))
        self.assertEqual(self.live(), erased)
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))
        # Uploaded: the group keeps its anchor; the text is untouched.
        doc = self.source.sync.tree.get(THROWAWAY)
        files = document_files(doc, self.sync.edits.of(THROWAWAY), lambda name: self.source.blob(doc, name),
                               lambda e: b"", self.sync.author())
        out = files[f"{THROWAWAY}/{PAGE1}.rm"]
        for kind in (w.BLOCK_TREE_NODE, w.BLOCK_ROOT_TEXT):
            self.assertEqual(blocks_of(out, kind), blocks_of(data, kind))
        self.assertEqual(len(parse(out).strokes), 3)


# ------------------------------------------------------------------ 5. checkboxes

def session_of(data: bytes) -> textedit.TextSession:
    from core.edits import Replay
    return textedit.TextSession.open(THROWAWAY, PAGE1, data, Replay(data, {}, {}, {}), DESKTOP, parse(data),
                                     renderer.estimate_width, lambda: NOW)


def styles(data: bytes) -> dict:
    root = rm_text.page_text(data).root
    return {e.char_id: (e.legacy, e.base_style, e.properties) for e in root.styles}


class CheckboxTests(unittest.TestCase):
    def test_a_click_checks_and_unchecks_as_the_app_does(self):
        session = session_of(text_page())
        self.assertFalse(session.toggle_checkbox(HELLO))           # body text: no checkbox
        self.assertFalse(session.toggle_checkbox(TASK + 1))        # not where a paragraph starts
        self.assertEqual(session.steps, [])
        session.cursor = session.anchor = 3
        self.assertTrue(session.toggle_checkbox(TASK))
        self.assertEqual(session.stored_style(TASK), (7, None, None))          # as the tablet stores one checked
        self.assertEqual((session.cursor, len(session.steps)), (3, 1))
        self.assertTrue(session.toggle_checkbox(TASK))
        self.assertEqual(session.stored_style(TASK), (6, None, None))
        edits, steps = session.take()
        self.assertEqual([e.op for e in edits], ["style", "style"])
        self.assertEqual(len(steps), 2)

    def test_the_state_rule_for_every_checkbox(self):
        session = session_of(text_page())
        rule = []
        for state in range(5):
            session.text.set_style(session.paragraph_key(TASK), textedit.store(3, 0x10 | state << 8), session.clock)
            session.toggle_checkbox(TASK)
            rule.append(textedit.held(session.stored_style(TASK))[1] >> 8 & 7)
        self.assertEqual(rule, [1, 0, 1, 0, 1])                     # 0x1408b5710: 1 and 3 uncheck
        session.text.set_style(session.paragraph_key(TASK), textedit.store(3, 0x11), session.clock)   # indented
        session.toggle_checkbox(TASK)
        self.assertEqual(session.stored_style(TASK), (9, 3, 0x111))

    def test_checkboxes_are_links_keyed_by_where_their_paragraph_starts(self):
        scene = parse(text_page())
        lines = renderer.layout_text(scene)
        [(start, (x, y, width, height))] = renderer.checkbox_boxes(scene, lines)
        task = next(line for line in lines if line.paragraph == 1)
        self.assertEqual(start, TASK)
        self.assertLess(x + width, task.x + 0.01)
        self.assertTrue(y < task.baseline < y + height)

    def test_the_link_handler_hands_the_index_on(self):
        handler = scene_bridge.SceneLinkHandler()
        got = []
        handler.toggleCheckbox.connect(got.append)
        handler.openUrl.connect(lambda url: got.append(("url", url)))
        handler.activate({"valid": True, "index": 0, "url": "rmcheckbox://index=6", "page": -1})
        handler.activate({"valid": True, "index": 1, "url": "https://example.com", "page": -1})
        self.assertEqual(got, [6, ("url", "https://example.com")])

    def test_undo_and_redo_of_a_click_through_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            cloud = text_cloud()
            source = LibrarySource(SyncClient(paired_auth(Path(tmp) / "auth", cloud), Path(tmp) / "cache"),
                                   editable=("throwaway",))
            doc = source.refresh().get(THROWAWAY)
            done = source.text_page(doc, PAGE1)
            session = textedit.TextSession.open(THROWAWAY, PAGE1, done.page, done, source.sync.author(),
                                                parse(done.page), renderer.estimate_width, lambda: NOW)
            self.assertTrue(session.toggle_checkbox(TASK))
            source.add_text_edits(doc, PAGE1, *session.take())

            def task() -> tuple:
                return styles(source.staged(source.sync.tree.get(THROWAWAY)).page(PAGE1))[(1, 20)]

            self.assertEqual(task(), (7, None, None))
            self.assertTrue(source.undo(doc, PAGE1, NOW + 1))
            self.assertEqual(task()[0], 6)
            self.assertTrue(source.redo(doc, PAGE1, NOW + 2))
            self.assertEqual(task(), (7, None, None))
            self.assertTrue(all(isinstance(e, TextEdit) for e in source.sync.edits.of(THROWAWAY)))


# ------------------------------------------------------------------ 6. Shift+Enter

class LineBreakTests(unittest.TestCase):
    def test_shift_enter_types_a_line_separator(self):
        action = textedit.key_action(int(Qt.Key.Key_Return.value), textedit.SHIFT, False, "\r")
        self.assertEqual(action["type"], textedit.LINE_BREAK)
        session = session_of(text_page())
        session.place(session.text.length)
        session.key(action)
        session.type_text("World")
        self.assertEqual(session.plain(), TEXT + " World")
        self.assertEqual(session.plain().count("\n"), 2)                     # still three paragraphs
        lines = [line for line in session.lines() if line.paragraph == 2]
        self.assertEqual([[t for t, _ in line.pieces] for line in lines], [["Hello"], ["World"]])
        self.assertEqual((lines[0].start, lines[0].end, lines[1].start), (11, 17, 17))
        caret = session.caret()
        self.assertEqual(caret.line, len(session.lines()) - 1)
        session.place(16)                                                    # just before the break
        self.assertEqual(session.caret().line, len(session.lines()) - 2)
        # Stored in the paragraph's text like any character; one paragraph more there is not.
        edits, _ = session.take()
        page = replay(text_page(), edits, DESKTOP).page
        text = rm_text.page_text(page)
        self.assertEqual(text.text, TEXT + " World")
        self.assertIn(" ".encode(), b"".join(blocks_of(page, w.BLOCK_ROOT_TEXT)))
        self.assertEqual(len(parse(page).text.paragraphs), 3)
        # Backspace takes the break out again.
        again = session_of(page)
        again.place(17)
        again.backspace()
        self.assertEqual(again.plain(), TEXT + "World")


# ------------------------------------------------------------------ through the app's QML

class Bundle2QmlTests(unittest.TestCase):
    """The same through the app's own document view, on a notebook whose page has typed
    text with a checkbox, a group anchored to it, and a lined template."""

    wait_for, qml, scene_views = tq.EditingTests.wait_for, tq.EditingTests.qml, tq.EditingTests.scene_views
    close_document, pending = tq.EditingTests.close_document, tq.EditingTests.pending
    pens, toolbar_item = tq.PenTests.pens, tq.PenTests.toolbar_item
    click, window_point, drag, stylus = tq.PenTests.click, tq.PenTests.window_point, tq.PenTests.drag, tq.PenTests.stylus
    redo_key = tq.PenTests.redo_key
    document_view, text_mode, typing = tq.TypingTests.document_view, tq.TypingTests.text_mode, tq.TypingTests.typing
    THROWAWAY, PAGE1 = THROWAWAY, PAGE1

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = text_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway",))
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

    def tearDown(self):
        if self.document_view().property("textMode"):
            self.text_mode(False)
            tq.wait(100)
        self.close_document()
        self.source.sync.edits.clear()
        self.app.singletons.settings.setProperty("rightHandMode", True)

    def open_document(self, doc_id: str, page_id: str | None = None):
        """PenTests.open_document; the keyboard then on the page. (Closing the editing
        tutorial with navDone, as the tests do, leaves the focus on the hidden tutorial
        item, where the window's undo shortcut does not reach the page.)"""
        view = tq.PenTests.open_document(self, doc_id, page_id)
        view.parentItem().forceActiveFocus()
        return view

    def keys(self, key, modifiers=Qt.KeyboardModifier.ControlModifier) -> None:
        tq.PenTests.keys(self, key, modifiers)

    def page(self) -> bytes:
        return self.source.staged(self.source.sync.tree.get(THROWAWAY)).page(PAGE1)

    def anchored_now(self) -> list:
        return [x for x in s.strokes(self.page()) if x.parent == GROUP]

    def test_the_shader_draws_from_the_toolbar(self):
        view = self.open_document(THROWAWAY, PAGE1)
        tools = self.pens(view).tools
        self.click(self.toolbar_item(view, "penTool_shader"))
        self.assertEqual((tools.property("tool"), tools.property("color"), tools.property("size")), ("shader", "grey", 2))
        self.assertIn("grey", [c["key"] for c in tools.property("colors")])
        self.drag(view, [(-200.0 + 10 * n, 1000.0) for n in range(20)])
        self.assertTrue(self.wait_for(lambda: any(x.line.tool == 23 for x in s.strokes(self.page()))), self.notices)
        [shaded] = [x for x in s.strokes(self.page()) if x.line.tool == 23]
        self.assertEqual((shaded.parent, shaded.line.color, shaded.line.thickness_scale), (LAYER, 1, 2.0))
        self.assertEqual(self.source.tool_settings(self.source.sync.tree.get(THROWAWAY))["LastTool"], "ShadingMarker")
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def test_hide_template_redraws_the_page_without_it(self):
        if default_library().get(TEMPLATE) is None:
            self.skipTest("the app's templates are not installed")
        view = self.open_document(THROWAWAY, PAGE1)
        controller = view._controller
        self.assertTrue(controller.property("backgroundVisible"))
        self.assertTrue(self.wait_for(lambda: view._image is not None))

        def dark() -> int:
            image = view._image
            return sum(1 for y in range(0, image.height(), 4) for x in range(0, image.width(), 4)
                       if QColor(image.pixel(x, y)).lightness() < 200)

        with_template = dark()
        controller.setBackgroundVisible(False)
        self.assertFalse(controller.property("backgroundVisible"))
        self.assertTrue(self.wait_for(lambda: dark() < with_template), "the template is still drawn")
        without = dark()
        self.assertGreater(without, 0)                  # the text and the ink are still there
        controller.setBackgroundVisible(True)
        self.assertTrue(self.wait_for(lambda: dark() == with_template))
        self.assertTrue(self.wait_for(lambda: len(self.pending()) == 2))
        self.assertEqual([e.background_visible for e in self.pending()], [False, True])
        self.assertTrue(parse(self.page()).background_visible)
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def test_calligraphy_follows_the_right_hand_setting(self):
        view = self.open_document(THROWAWAY, PAGE1)
        made = []
        real = s.StrokeBuilder

        def builder(*args, **kw):
            made.append((kw.get("right_handed"), kw.get("rotation")))
            return real(*args, **kw)

        self.click(self.toolbar_item(view, "penTool_calligraphy"))
        with mock.patch.object(pen_bridge.s, "StrokeBuilder", side_effect=builder):
            self.drag(view, [(-200.0 + 8 * n, 1100.0 + 5 * n) for n in range(15)])
            self.app.singletons.settings.setProperty("rightHandMode", False)
            self.drag(view, [(-200.0 + 8 * n, 1200.0 + 5 * n) for n in range(15)])
        self.assertEqual(made, [(True, 0.0), (False, 0.0)])
        self.assertTrue(self.wait_for(lambda: len([x for x in s.strokes(self.page()) if x.line.tool == 21]) == 2))
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def drawn_at(self, view) -> tuple[float, float]:
        offsets = scene_bridge.anchor_offsets(view._content.scene)
        dx, dy = offsets[ANCHORED]
        return 100.25 + dx, -10.0 + dy

    def test_alt_and_the_stylus_eraser_reach_anchored_strokes(self):
        view = self.open_document(THROWAWAY, PAGE1)
        x, y = self.drawn_at(view)
        self.click(self.toolbar_item(view, "penTool_fineliner"))
        self.drag(view, [(x, y - 30.0), (x, y), (x, y + 30.0)], Qt.KeyboardModifier.AltModifier)
        self.assertTrue(self.wait_for(lambda: len(self.anchored_now()) == 2), self.notices)
        [edit] = [e for e in self.pending() if isinstance(e, StrokeErase)]
        self.assertEqual([(a, c) for a, c, _, _ in edit.anchors], [GROUP])
        self.assertEqual({x_.parent for x_ in self.anchored_now()}, {GROUP})
        # One undo step: the stroke back in its group; redo cuts it again.
        self.keys(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: len(self.anchored_now()) == 1), self.notices)
        self.assertEqual(self.anchored_now()[0].line.move_id, ANCHORED)
        self.redo_key()
        self.assertTrue(self.wait_for(lambda: len(self.anchored_now()) == 2), self.notices)
        self.keys(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: len(self.anchored_now()) == 1), self.notices)
        # The stylus's eraser end, a little to the left.
        x -= 50.0
        points = [self.window_point(view, x, y - 40.0 + 10 * n).toPointF() for n in range(9)]
        self.stylus(QEvent.Type.TabletPress, points[0], 0.5, eraser=True)
        for p in points[1:]:
            self.stylus(QEvent.Type.TabletMove, p, 0.5, eraser=True)
        self.stylus(QEvent.Type.TabletRelease, points[-1], 0.0, eraser=True)
        # Cut in its group (in two, or three where window pixels put the eraser's edge
        # right on one of the stroke's points, which the app keeps as well).
        self.assertTrue(self.wait_for(lambda: len(self.anchored_now()) >= 2), self.notices)
        self.assertEqual(len([e for e in self.pending() if isinstance(e, StrokeErase)]), 2)
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def test_clicking_a_checkbox_toggles_it(self):
        from PySide6.QtTest import QTest
        view = self.open_document(THROWAWAY, PAGE1)
        controller = view._controller
        [(start, box)] = view.checkbox_links()
        self.assertEqual(start, TASK)
        link = controller.linkAt(box.center())
        self.assertEqual((link["valid"], link["url"]), (True, "rmcheckbox://index=6"))
        self.assertFalse(controller.linkAt(QPointF(box.right() + 400, box.center().y()))["valid"])

        def task():
            return styles(self.page())[(1, 20)][0]

        at = self.window_point(view, box.center().x(), box.center().y())
        self.window.requestActivate()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None
                                      and view.text_session.stored_style(TASK) == (7, None, None)), self.notices)
        self.assertTrue(self.wait_for(lambda: task() == 7), self.notices)       # kept after a pause
        self.keys(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: task() == 6), self.notices)
        self.redo_key()
        self.assertTrue(self.wait_for(lambda: task() == 7), self.notices)
        self.assertTrue(all(isinstance(e, TextEdit) for e in self.pending()))
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def test_a_checkbox_toggled_out_of_text_mode_is_kept(self):
        view = self.open_document(THROWAWAY, PAGE1)
        self.assertFalse(self.document_view().property("textMode"))
        view._controller.toggleCheckboxByIndex(TASK)
        self.assertTrue(self.wait_for(lambda: view.text_session is None
                                      and styles(self.page())[(1, 20)][0] == 7), self.notices)

    def test_shift_enter_breaks_the_line(self):
        from PySide6.QtTest import QTest
        view = self.open_document(THROWAWAY, PAGE1)
        session = self.typing(view)
        session.place(session.text.length)
        QTest.keyClick(self.window, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        for c in "ok":
            QTest.keyClick(self.window, Qt.Key(ord(c.upper())))
        tq.wait(30)
        self.assertEqual(view.text_session.plain(), TEXT + " ok")
        self.text_mode(False)
        self.assertTrue(self.wait_for(lambda: view.text_session is None), self.notices)
        self.assertTrue(self.wait_for(lambda: rm_text.page_text(self.page()).text == TEXT + " ok"), self.notices)
        self.assertEqual(len(parse(self.page()).text.paragraphs), 3)
        self.assertEqual(tq.real_warnings(self.app.warnings), [])


if __name__ == "__main__":
    unittest.main()
