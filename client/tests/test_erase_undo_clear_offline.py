"""Offline unit and QML integration tests for the eraser's stacking order on undo and
the tablet's two other eraser modes (synthetic ids only):

- Stacking order: an item put back by undo or redo (rm_stroke.restore_stroke,
  rm_writer.restore_image) takes the page's next id, so its block is stored last, but
  its place in the layer's CRDT sequence is right after its own deleted self. The parser
  must list strokes and images by that sequence: read in file order, the strokes of an
  undone erase came back last in Layer.strokes, as if on top (the pre-existing
  Layers-review bug). A seeded random draw/erase/clear/undo/redo model checks it too.
- ClearPage ("Erase all"): rm_stroke.clear_layer / rm_layer.clear_lines_in_layer clear
  the active layer and the groups below it (not other layers, not images, no new ids),
  one StrokeDelete and one undo step through ui.library, persisted as pending edits.
- EraseSection ("Erase selection"): an area eraser whose line is the outline itself,
  through ui.pen, the pen bridge, ui.library (one StrokeErase, undo/redo, persisted)
  and the app's own DocumentView.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_erase_undo_clear_offline.py'
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import Qt  # noqa: E402

import test_edits_offline as te  # noqa: E402
import test_layers_offline as tl  # noqa: E402
from core.edits import StrokeDelete, StrokeErase  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from rmfixtures import group_item, scene_tree, tree_node  # noqa: E402
from test_bundle2_offline import _item, _line  # noqa: E402
from ui import pen as pens  # noqa: E402
from ui.library import LibrarySource, document_pages  # noqa: E402
from ui.quick.bridge import pen as pen_bridge  # noqa: E402
from viewer import rm_layer as ly  # noqa: E402
from viewer import rm_stroke as s  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import Color, parse  # noqa: E402

TABLET = tl.TABLET
DESKTOP = tl.DESKTOP
THROWAWAY, PAGE1 = te.THROWAWAY, te.PAGE1
NOW = te.NOW
BLUE, RED, GREEN = int(Color.BLUE), int(Color.RED), int(Color.GREEN)
stroke_line, pixel_rgb = tl.stroke_line, tl.pixel_rgb


def crossing_page() -> bytes:
    """Layer 1: a thick blue horizontal stroke, then a red vertical one crossing it at
    (0, 500) (the crossing shows red, the top one), then a green one below them."""
    page = s.append_stroke(w.blank_page(TABLET), author=TABLET, line=stroke_line(-150.0, 500.0, 150.0, 500.0, BLUE))
    page = s.append_stroke(page, author=TABLET, line=stroke_line(0.0, 350.0, 0.0, 650.0, RED))
    return s.append_stroke(page, author=TABLET, line=stroke_line(-150.0, 900.0, 150.0, 900.0, GREEN))


def colours(page: bytes, layer: int = 0) -> list[int]:
    """The colours of a layer's strokes, bottom to top, as the page draws them."""
    return [st.color for st in parse(page).layers[layer].strokes]


def stacked(page) -> list:
    """Every layer's strokes bottom to top, by colour and points: what the page (bytes, or
    a parsed scene) shows."""
    scene = parse(page) if isinstance(page, bytes) else page
    return [[(st.color, tuple((round(p.x, 2), round(p.y, 2)) for p in st.points)) for st in layer.strokes]
            for layer in scene.layers]


def square(x0: float, y0: float, x1: float, y1: float, steps: int = 4) -> list[tuple[float, float]]:
    """An area's outline, drawn round from (x0, y0) and back."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    out = [corners[0]]
    for (ax, ay), (bx, by) in zip(corners, corners[1:]):
        out += [(ax + (bx - ax) * k / steps, ay + (by - ay) * k / steps) for k in range(1, steps + 1)]
    return out


def area(points) -> s.LineValue:
    return s.eraser_line(points, s.MEDIUM, tool=s.ERASE_SECTION)


# ------------------------------------------------------------------ 1. stacking order after undo

class StackingOrderTests(unittest.TestCase):
    def test_a_stroke_put_back_is_drawn_where_it_was_not_on_top(self):
        page = crossing_page()
        before = stacked(page)
        blue = s.strokes(page)[0]
        self.assertEqual(blue.line.color, BLUE)
        page, new_id = s.restore_stroke(s.delete_strokes(page, [blue.item]), author=DESKTOP,
                                        item=blue.item, line=blue.line)
        # Stored last (it took the page's next id) ...
        self.assertEqual([x.item for x in s.strokes(page)][-1], new_id)
        # ... but drawn at the bottom, where it was: the parser follows the sequence.
        self.assertEqual(stacked(page), before)
        self.assertEqual(colours(page), [BLUE, RED, GREEN])
        r, g, b = pixel_rgb(parse(page), 0.0, 500.0)
        self.assertGreater(r, 150, "the put-back blue stroke covers the red one")

    def test_an_image_put_back_keeps_its_place_among_the_images(self):
        quad = [(-100.0, 0.0, 0.0, 0.0), (100.0, 0.0, 1.0, 0.0), (100.0, 100.0, 1.0, 1.0), (-100.0, 100.0, 0.0, 1.0)]
        page = w.append_image(w.blank_page(TABLET), author=TABLET, key=tl.KEY_A, file_name=tl.NAME_A, vertices=quad)
        page = w.append_image(page, author=TABLET, key=tl.KEY_B, file_name=tl.NAME_B, vertices=quad)
        refs = [i.image_ref for i in parse(page).layers[0].images]
        item, value = w.image_item(page, tl.KEY_A)
        page, _ = w.delete_image(page, author=DESKTOP, key=tl.KEY_A, prune=False)
        page = w.restore_image(page, author=DESKTOP, item=item, value=value)
        self.assertEqual([i.image_ref for i in parse(page).layers[0].images], refs)
        self.assertEqual([type(x).__name__ for x in parse(page).layers[0].items()], ["ImageItem", "ImageItem"])


# ------------------------------------------------------------------ 2. ClearPage in the engine

def grouped_page() -> tuple[bytes, tuple[int, int]]:
    """Layer 1: a blue stroke, a plain group (one red stroke in it), a green stroke and an
    image; Layer 2 (current): one blue stroke. Returns (page, Layer 2's id)."""
    header, blocks = w.split(w.blank_page(TABLET))
    plain = (1, 40)
    page = (w.join(header, blocks) + scene_tree(plain, w.LAYER_1) + tree_node(plain, "", True)
            + _line(w.LAYER_1, (1, 41), [(-100.0, 300.0, 0, 0, 3, 255), (100.0, 300.0, 0, 0, 3, 255)], color=BLUE)
            + _item(group_item(w.LAYER_1, (1, 42), plain), left=(1, 41))
            + _line(plain, (1, 43), [(-100.0, 400.0, 0, 0, 3, 255), (100.0, 400.0, 0, 0, 3, 255)], color=RED)
            + _line(w.LAYER_1, (1, 44), [(-100.0, 500.0, 0, 0, 3, 255), (100.0, 500.0, 0, 0, 3, 255)],
                    left=(1, 42), color=GREEN))
    page = w.append_image(page, author=TABLET, key=tl.KEY_A, file_name=tl.NAME_A,
                          vertices=[(-50.0, 600.0, 0.0, 0.0), (50.0, 600.0, 1.0, 0.0),
                                    (50.0, 700.0, 1.0, 1.0), (-50.0, 700.0, 0.0, 1.0)], layer=w.LAYER_1)
    page, l2 = ly.add_layer(page, author=TABLET)
    return s.append_stroke(page, author=TABLET, line=stroke_line(-100.0, 800.0, 100.0, 800.0, BLUE), layer=l2), l2


class ClearLayerTests(unittest.TestCase):
    def test_the_layer_and_its_groups_are_cleared_other_layers_and_images_stay(self):
        page, l2 = grouped_page()
        cleared = s.clear_layer(page, layer=w.LAYER_1)
        # The layer's own strokes in sequence order, then the group below it.
        self.assertEqual([(node, item) for node, item, _ in cleared.deleted],
                         [(w.LAYER_1, (1, 41)), (w.LAYER_1, (1, 44)), ((1, 40), (1, 43))])
        # Undo puts them back node by node, last node first (0x1408f52b0).
        self.assertEqual([item for item, _ in cleared.undo_order()], [(1, 43), (1, 41), (1, 44)])
        after = parse(cleared.page)
        self.assertEqual(after.layers[0].strokes, [])
        self.assertEqual(len(after.layers[0].images), 1)
        self.assertEqual([st.color for st in after.layers[1].strokes], [BLUE])
        self.assertEqual({x.parent for x in s.strokes(cleared.page)}, {l2})
        # Nothing is cut and no id is taken.
        self.assertEqual(w.highest_counter(w.split(cleared.page)[1]), w.highest_counter(w.split(page)[1]))

    def test_the_current_layer_is_the_default_and_an_empty_layer_is_left_untouched(self):
        page, l2 = grouped_page()
        cleared = s.clear_layer(page)
        self.assertEqual([item for _, item, _ in cleared.deleted], [s.strokes(page, l2)[0].item])
        again = s.clear_layer(cleared.page)
        self.assertEqual((again.page, again.deleted), (cleared.page, ()))
        with self.assertRaises(ly.LayerError):
            ly.clear_lines_in_layer(page, author=DESKTOP, layer=(5, 5))

    def test_undoing_a_clear_restores_the_page_as_it_was_stacked(self):
        page, _ = grouped_page()
        before = stacked(page)
        new, deleted = ly.clear_lines_in_layer(page, author=DESKTOP, layer=w.LAYER_1)
        for item, raw in deleted:
            new, _ = s.restore_stroke(new, author=DESKTOP, item=item, line=s.decode_line_value(raw))
        self.assertEqual(stacked(new), before)


# ------------------------------------------------------------------ 3. pens: the Erase selection tool

class EraseSectionToolTests(unittest.TestCase):
    def test_the_tool_is_an_area_eraser_without_sizes_or_colours(self):
        tool = pens.BY_KEY["erase_section"]
        self.assertEqual((tool.tool, tool.name, tool.icon, tool.label),
                         (s.ERASE_SECTION, "EraseSection", "erase_selection", "Erase selection"))
        self.assertTrue(tool.is_eraser and tool.is_area)
        self.assertFalse(tool.sizes or pens.BY_KEY["eraser"].is_area)
        self.assertTrue(pens.BY_KEY["eraser"].sizes)
        self.assertEqual(tool.colors, ())
        self.assertEqual(pens.tool_values(tool), {"LastTool": "EraseSection", "LastEraserTool": "EraseSection"})
        self.assertIs(pens.current_tool({"LastTool": "EraseSection"}), tool)
        self.assertEqual(pens.CLEAR_ALL, ("erase_all", "Erase all", "erase_all"))

    def test_the_drawn_line_is_the_outline_and_needs_three_points(self):
        pen = pens.pen_of({}, pens.BY_KEY["erase_section"])
        drawing = pen_bridge._Drawing(pen, True, 1.0, False)
        self.assertTrue(drawing.area)
        drawing.samples = [(0.0, 0.0), (40.0, 0.0), (0.0, 0.0)]
        self.assertIsNone(drawing.line())
        drawing.samples = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
        line = drawing.line()
        self.assertEqual(line.tool, s.ERASE_SECTION)
        outline = s.eraser_outline(line)
        self.assertTrue(outline.contains(20.0, 15.0))
        self.assertFalse(outline.contains(60.0, 15.0))
        # The stylus's eraser end over a pen is the plain eraser, never an area.
        self.assertFalse(pen_bridge._Drawing(pens.pen_of({}, pens.BY_KEY["ballpoint"]), True, 1.0, True).area)


# ------------------------------------------------------------------ 4. through the library

class EraseModesLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = tl.layer_cloud(crossing_page())
        self.source = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                    editable=("throwaway",))
        self.doc = self.source.refresh().get(THROWAWAY)

    def page(self) -> bytes:
        return self.source.staged(self.source.sync.tree.get(THROWAWAY)).page(PAGE1)

    def restarted(self):
        """The page as a new process shows it: the cached cloud page and the pending edits on disk."""
        source = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                               editable=("throwaway",), cached_only=True)
        doc = source.load_cached().get(THROWAWAY)
        return source.page_scene(doc, document_pages(doc)[0])

    def pending_kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.source.sync.edits.of(THROWAWAY)]

    def test_an_undone_eraser_pass_keeps_the_stacking_order(self):
        before = stacked(self.page())
        eraser = s.eraser_line([(-100.25, 450.0), (-100.25, 550.0)], s.MEDIUM)
        self.assertIsNotNone(self.source.erase_strokes(self.doc, PAGE1, eraser, NOW))
        self.assertEqual(colours(self.page()), [BLUE, BLUE, RED, GREEN])          # the pieces where it was
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 1))
        self.assertEqual(stacked(self.page()), before)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 2))
        self.assertEqual(colours(self.page()), [BLUE, BLUE, RED, GREEN])
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 3))
        self.assertEqual(stacked(self.page()), before)
        self.assertEqual(stacked(self.restarted()), before)

    def test_erase_all_clears_the_current_layer_only_as_one_undo_step(self):
        self.source.add_layer(self.doc, PAGE1, NOW)                     # Layer 2, now current
        self.source.add_stroke(self.doc, PAGE1, stroke_line(-100.0, 1200.0, 100.0, 1200.0, RED), NOW + 1)
        before = stacked(self.page())
        self.assertEqual([len(layer) for layer in before], [3, 1])
        edit = self.source.clear_lines_in_layer(self.doc, PAGE1, None, NOW + 2)
        self.assertIsInstance(edit, StrokeDelete)
        self.assertEqual(stacked(self.page()), [before[0], []])
        self.assertEqual(self.pending_kinds()[-1], "StrokeDelete")
        self.assertEqual(stacked(self.restarted()), [before[0], []])
        # A cleared layer again: nothing kept.
        count = len(self.pending_kinds())
        self.assertIsNone(self.source.clear_lines_in_layer(self.doc, PAGE1, None, NOW + 3))
        self.assertEqual(len(self.pending_kinds()), count)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 4))
        self.assertEqual(stacked(self.page()), before)
        self.assertEqual(stacked(self.restarted()), before)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 5))
        self.assertEqual(stacked(self.page()), [before[0], []])
        # Layer 1 by index: its three strokes, then undo puts them back in their order.
        self.source.set_current_layer(self.doc, PAGE1, 0, NOW + 6)
        self.assertIsNotNone(self.source.clear_lines_in_layer(self.doc, PAGE1, None, NOW + 7))
        self.assertEqual(stacked(self.page()), [[], []])
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 8))
        self.assertEqual(stacked(self.page()), [before[0], []])

    def test_a_random_draw_erase_clear_undo_redo_model(self):
        """Random strokes, eraser passes, erase-selection areas, clears, undos and redos:
        after every step the page shows exactly what a plain undo stack of page states
        says it should, stacking order included, and a restart shows the same."""
        rng = random.Random(20261120)
        done: list[tuple[list, list]] = []            # (before, after) of each step kept
        undone: list[tuple[list, list]] = []
        counts = dict.fromkeys(("draw", "erase", "area", "clear", "undo", "redo"), 0)
        tools = (s.MARKER_V2, s.FINELINER_V2, s.BALLPOINT_V2, s.HIGHLIGHTER_V2)
        for step in range(80):
            now = NOW + step
            op = rng.choice(("draw", "draw", "erase", "area", "clear", "undo", "undo", "redo"))
            before = stacked(self.page())
            if op == "draw":
                x, y = rng.uniform(-300.0, 300.0), rng.uniform(300.0, 1100.0)
                dx, dy = rng.choice(((250.0, 0.0), (0.0, 250.0), (180.0, 180.0)))
                self.source.add_stroke(self.doc, PAGE1, stroke_line(x - dx / 2, y - dy / 2, x + dx / 2, y + dy / 2,
                                                                   rng.choice((BLUE, RED, GREEN)), rng.choice(tools)), now)
                kept = True
            elif op == "erase":
                x, y = rng.uniform(-300.0, 300.0) + 0.25, rng.uniform(300.0, 1100.0)
                kept = self.source.erase_strokes(self.doc, PAGE1, s.eraser_line(
                    [(x, y - 150.0), (x + rng.uniform(-60.0, 60.0), y + 150.0)], s.MEDIUM), now) is not None
            elif op == "area":
                x, y = rng.uniform(-300.0, 300.0), rng.uniform(300.0, 1100.0)
                w_, h_ = rng.uniform(60.0, 300.0), rng.uniform(60.0, 300.0)
                kept = self.source.erase_strokes(self.doc, PAGE1, area(square(x, y, x + w_, y + h_)), now) is not None
            elif op == "clear":
                kept = self.source.clear_lines_in_layer(self.doc, PAGE1, None, now) is not None
            elif op == "undo":
                self.assertEqual(self.source.undo(self.doc, PAGE1, now), bool(done), step)
                if done:
                    undone.append(done.pop())
                    self.assertEqual(stacked(self.page()), undone[-1][0], f"step {step}: undo")
                counts[op] += 1
                continue
            else:
                self.assertEqual(self.source.redo(self.doc, PAGE1, now), bool(undone), step)
                if undone:
                    done.append(undone.pop())
                    self.assertEqual(stacked(self.page()), done[-1][1], f"step {step}: redo")
                counts[op] += 1
                continue
            after = stacked(self.page())
            if kept:
                done.append((before, after))
                undone.clear()
                counts[op] += 1
            else:
                self.assertEqual(after, before, f"step {step}: {op} kept nothing but changed the page")
        self.assertTrue(all(counts.values()), counts)
        self.assertEqual(stacked(self.restarted()), stacked(self.page()))

    def test_erase_selection_takes_what_is_inside_and_cuts_at_its_edge(self):
        before = stacked(self.page())
        # Round the left end of the blue stroke: its left part goes, it is cut at the edge;
        # the red and green strokes lie outside.
        edit = self.source.erase_strokes(self.doc, PAGE1, area(square(-200.0, 440.0, -60.0, 560.0)), NOW)
        self.assertIsInstance(edit, StrokeErase)
        self.assertEqual(edit.tool, s.ERASE_SECTION)
        after = parse(self.page()).layers[0].strokes
        self.assertEqual([st.color for st in after], [BLUE, RED, GREEN])
        self.assertTrue(all(p.x > -70.0 for p in after[0].points))
        self.assertGreater(max(p.x for p in after[0].points), 140.0)
        [saved] = json.loads((self.source.sync.edits.root / "edits.json").read_text())["edits"]
        self.assertEqual((saved["kind"], saved["tool"]), ("stroke-erase", s.ERASE_SECTION))
        self.assertEqual(stacked(self.restarted()), stacked(self.page()))
        # Whole strokes inside go entirely; an area that holds nothing keeps nothing.
        count = len(self.pending_kinds())
        self.assertIsNone(self.source.erase_strokes(self.doc, PAGE1, area(square(300.0, 300.0, 400.0, 400.0)), NOW + 1))
        self.assertEqual(len(self.pending_kinds()), count)
        self.assertIsNotNone(self.source.erase_strokes(self.doc, PAGE1, area(square(-200.0, 850.0, 200.0, 950.0)),
                                                       NOW + 2))
        self.assertEqual(colours(self.page()), [BLUE, RED])
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 3))
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 4))
        self.assertEqual(stacked(self.page()), before)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 5))
        self.assertEqual([st.color for st in parse(self.page()).layers[0].strokes], [BLUE, RED, GREEN])


# ------------------------------------------------------------------ 5. through the app's QML

class EraseModesQmlTests(unittest.TestCase):
    """The Erase all button and the Erase selection tool in the pen toolbar, and an
    eraser pass undone, through the app's own DocumentView: what the window shows."""

    wait_for, qml, scene_views = tq.EditingTests.wait_for, tq.EditingTests.qml, tq.EditingTests.scene_views
    close_document, pending = tq.EditingTests.close_document, tq.EditingTests.pending
    pens, toolbar_item, keys, redo_key = tq.PenTests.pens, tq.PenTests.toolbar_item, tq.PenTests.keys, tq.PenTests.redo_key
    click, window_point, drag = tq.PenTests.click, tq.PenTests.window_point, tq.PenTests.drag
    open_doc, ink, key = tl.LayersQmlTests.open_doc, tl.LayersQmlTests.ink, tl.LayersQmlTests.key
    setUp = tl.LayersQmlTests.setUp
    tearDownClass = classmethod(tq.EditingTests.tearDownClass.__func__)
    THROWAWAY, PAGE1 = THROWAWAY, PAGE1

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = tl.layer_cloud(crossing_page())
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

    def layer_strokes(self, view) -> list:
        scene = view._content.scene if view._content is not None else None
        return [st.color for st in scene.layers[0].strokes] if scene is not None else []

    def test_an_undone_eraser_pass_shows_the_strokes_as_they_were_stacked(self):
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.ink(view, 0.0, 500.0) == "red"))
        view.pen_commit("erase", s.eraser_line([(-100.25, 440.0), (-100.25, 560.0)], s.MEDIUM), 1)
        self.assertTrue(self.wait_for(lambda: self.layer_strokes(view) == [BLUE, BLUE, RED, GREEN]))
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: self.layer_strokes(view) == [BLUE, RED, GREEN]), self.notices)
        tq.wait(200)
        self.assertEqual(self.ink(view, 0.0, 500.0), "red", "the undone blue stroke came back on top")
        self.assertEqual(self.ink(view, -100.0, 500.0), "blue")
        self.close_document()

    def test_the_erase_all_button_clears_the_layer_with_undo_and_redo(self):
        view = self.open_doc(THROWAWAY)
        button = self.toolbar_item(view, "penTool_erase_all")
        self.assertTrue(self.wait_for(lambda: self.pens(view).toolbar.property("visible")))
        self.assertTrue(self.wait_for(lambda: self.ink(view, 0.0, 500.0) == "red"))
        self.click(button)
        self.assertTrue(self.wait_for(lambda: self.layer_strokes(view) == []), self.notices)
        self.assertTrue(self.wait_for(lambda: self.ink(view, 0.0, 500.0) == "paper"))
        self.assertFalse(self.pens(view).tools.property("active"), "Erase all takes no tool in hand")
        self.assertEqual([type(e).__name__ for e in self.source.sync.edits.of(THROWAWAY)], ["StrokeDelete"])
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: self.layer_strokes(view) == [BLUE, RED, GREEN]), self.notices)
        self.assertTrue(self.wait_for(lambda: self.ink(view, 0.0, 500.0) == "red"))
        self.redo_key()
        self.assertTrue(self.wait_for(lambda: self.layer_strokes(view) == []))
        self.assertEqual(self.notices, [])
        self.close_document()

    def test_the_erase_selection_tool_erases_the_area_drawn(self):
        view = self.open_doc(THROWAWAY)
        tools = self.pens(view).tools
        self.assertTrue(self.wait_for(lambda: self.pens(view).toolbar.property("visible")))
        self.click(self.toolbar_item(view, "penTool_erase_section"))
        self.assertTrue(self.wait_for(lambda: tools.property("active")))
        self.assertEqual(tools.property("tool"), "erase_section")
        self.assertTrue(tools.property("isEraser"))
        self.assertFalse(tools.property("hasOptions"))
        self.assertFalse(self.toolbar_item(view, "penOptions").property("visible"))
        self.assertTrue(self.wait_for(lambda: self.ink(view, -110.0, 500.0) == "blue"))
        self.drag(view, square(-200.0, 430.0, -60.0, 570.0, steps=8))
        self.assertTrue(self.wait_for(lambda: self.ink(view, -110.0, 500.0) == "paper"), self.notices)
        self.assertEqual(self.layer_strokes(view), [BLUE, RED, GREEN])
        self.assertEqual(self.ink(view, 0.0, 500.0), "red")
        self.assertEqual(self.ink(view, 100.0, 500.0), "blue")
        [edit] = self.source.sync.edits.of(THROWAWAY)[-1:]
        self.assertIsInstance(edit, StrokeErase)
        self.assertEqual(edit.tool, s.ERASE_SECTION)
        settings = self.source.tool_settings(self.source.sync.tree.get(THROWAWAY))
        self.assertEqual((settings.get("LastTool"), settings.get("LastEraserTool")), ("EraseSection", "EraseSection"))
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: self.ink(view, -110.0, 500.0) == "blue"), self.notices)
        self.assertEqual(self.ink(view, 0.0, 500.0), "red")
        self.click(self.toolbar_item(view, "penTool_erase_section"))     # put down
        self.assertTrue(self.wait_for(lambda: not tools.property("active")))
        self.close_document()


if __name__ == "__main__":
    unittest.main()
