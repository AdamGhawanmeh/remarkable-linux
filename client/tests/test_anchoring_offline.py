"""Offline tests for viewer.rm_anchoring (synthetic ids and authors only): where a stroke
drawn on a page with typed text goes (3.29's anchorItems, ARCHITECTURE.md 5.1 "Where a new
stroke goes"), the proximity and clustering it works from, the regroup after an eraser,
and the history of both through the library (one undo step each, replayed from the
ledger).

The text is laid out with the renderer's estimates (default_measure is patched), so the
numbers below do not depend on which fonts, or whether a Qt application, a run has.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_edits_offline as te  # noqa: E402
from core.edits import AnchorSet, replay  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from rmfixtures import (group_item, line_item, paragraph_format, points_v2, root_text, scene_tree,  # noqa: E402
                        text_item, tree_node)
from ui.library import LibrarySource  # noqa: E402
from viewer import renderer, rm_anchor  # noqa: E402
from viewer import rm_anchoring as ra  # noqa: E402
from viewer import rm_stroke as s  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

TABLET = bytes(range(16))
LAYER, ZERO = (0, 11), (0, 0)
EST = renderer.estimate_width
TEXT = "Hello world"                       # ids 1:15 .. 1:25: the page's clock is 25
GROUP = (1, 30)
BEFORE, AFTER = ra.BEFORE_TEXT, ra.AFTER_TEXT


def text_page(*blocks: bytes) -> bytes:
    """A page of the tablet (author index 1) with a text box at (-468, 234), 936 wide, holding TEXT."""
    return (w.blank_page(TABLET) + root_text([text_item((1, 15), ZERO, text=TEXT)], [paragraph_format(ZERO, 1)])
            + b"".join(blocks))


def pts(x0: float, x1: float, y: float) -> list[tuple]:
    return [(x0, y, 0, 0, 3, 255), (x1, y, 0, 0, 3, 255)]


def line_block(parent, item, points, left=ZERO) -> bytes:
    """A line item of the tablet as a page stores it (versions 2, 2), its left neighbour set."""
    _, [block] = w.split(w.V6_HEADER + line_item(parent, item, tool=17, color=0, points=points_v2(points)))
    return w.new_block(block.type, dataclasses.replace(w.decode_item(block.payload), left=left).encode()).encode()


def group_blocks(node, item, char, origin_x, kind=2, threshold=rm_anchor.THRESHOLD) -> bytes:
    """An anchored group on the layer: tree move, node and group item."""
    return (scene_tree(node, LAYER) + tree_node(node, "", True, anchor=(char, origin_x, threshold, kind))
            + group_item(LAYER, item, node))


def bar(x0: float, x1: float, y: float) -> s.LineValue:
    """A level stroke as the app would add it (Fineliner v2, medium: bounds 4 wider each way)."""
    return s.draw([(x0, y), (x1, y)], s.FINELINER_V2)


def layout_of(page: bytes):
    scene = parse(page)
    return scene, renderer.layout_text(scene, EST)


def baseline() -> float:
    scene, lines = layout_of(text_page())
    return renderer.index_point(scene, lines, 0, EST)[1]


def scene_state(page: bytes) -> list:
    """Every live stroke as the page draws it: (its anchor character and type, its points
    in scene coordinates), sorted: what undo and redo must put back."""
    scene = parse(page)
    by_object = renderer.stroke_offsets(scene, renderer.layout_text(scene, EST), EST)
    out = []
    for stroke in scene.strokes:
        dx, dy = by_object.get(id(stroke), (0.0, 0.0))
        anchor = (stroke.anchor.char_id, stroke.anchor.type) if stroke.anchor else None
        out.append((anchor, tuple((round(p.x + dx, 2), round(p.y + dy, 2)) for p in stroke.points)))
    return sorted(out, key=repr)


def anchors_of(page: bytes) -> dict:
    return ra.read_anchors(w.split(page)[1])


def parents_of(page: bytes) -> dict:
    out: dict = {}
    for x in s.strokes(page):
        out.setdefault(x.parent, []).append(x)
    return out


def moves_of(page: bytes, node) -> list:
    """The SceneTreeMoves of `node`, in move-id order: (move id, node, is_update, parent)."""
    found = [w.decode_tree_move(b.payload) for b in w.split(page)[1] if b.type == w.BLOCK_TREE_MOVE]
    return sorted((m for m in found if m[1] == node), key=lambda m: w.id_key(m[0]))


class Estimated(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ra, "default_measure", lambda: EST)
        patcher.start()
        self.addCleanup(patcher.stop)


def geo(left, top, right, bottom, *points, t=1.0) -> ra.Geo:
    return ra.Geo((left, top, right, bottom), tuple(points) or (((left + right) / 2, (top + bottom) / 2),), t)


# ------------------------------------------------------------------ geometry

class GeometryTests(unittest.TestCase):
    def test_the_tool_factor_table_and_the_tolerance(self):
        table = {0: 50, 1: 50, 2: 2, 15: 2, 19: 2, 20: 2, 3: 20, 16: 20, 4: 3, 7: 3, 8: 3, 11: 3, 17: 3, 5: 30,
                 18: 30, 10: 10, 23: 10, 12: 15, 13: 4, 14: 18, 21: 6, 9: 1, 24: 1}
        for tool, factor in table.items():
            self.assertEqual(ra.tool_factor(tool, 2.0), factor, tool)
        self.assertEqual((ra.tool_factor(6, 2.0), ra.tool_factor(22, 3.0)), (20.0, 30.0))   # 10 x thickness
        self.assertEqual((ra.width_factor(17, 2.0), ra.tolerance(17, 2.0)), (8.0, 2.0))
        self.assertEqual(ra.tolerance(6, 2.0), 0.25 * (2.0 * 20.0 + 2.0))
        for tool in range(24):                  # the same numbers as rm_stroke's reach (0x140b3e5f0)
            for thickness in (1.0, 2.0, 3.0):
                self.assertAlmostEqual(ra.width_factor(tool, thickness), s._reach(tool, thickness), delta=1e-4)

    def test_small_and_radius(self):
        self.assertTrue(ra.is_small(geo(0, 0, 89.3, 89.3)))
        self.assertFalse(ra.is_small(geo(0, 0, 89.4, 10)))
        self.assertFalse(ra.is_small(geo(0, 0, 10, 89.4)))
        self.assertAlmostEqual(ra.radius(geo(0, 0, 20, 10, t=2.0)), 20 / (math.pi / 2) + 2.0)

    def test_near(self):
        self.assertTrue(ra.near((0, 0), (3, 4), 3.0, 2.5, 0.0))         # 5 < 5.5
        self.assertFalse(ra.near((0, 0), (3, 4), 2.0, 2.5, 0.0))        # 5 < 4.5 is not so
        self.assertTrue(ra.near((1, 1), (1, 1), 0.0, 0.0, 0.0))         # under 1e-6 apart
        self.assertTrue(ra.near((0, 0), (3, 4), 0.0, 0.0, 10.0))        # (3/5 + 1) x 10 = 16
        self.assertFalse(ra.near((0, 0), (30, 0), 0.0, 0.0, 10.0))      # sideways counts twice: 20
        self.assertFalse(ra.near((0, 0), (0, 30), 0.0, 0.0, 10.0))      # straight up once: 10
        self.assertTrue(ra.near((0, 0), (0, 9), 0.0, 0.0, 10.0))

    def test_densify_and_closed(self):
        self.assertEqual(ra.densify([(0, 0), (40, 0)]), [(0, 0), (40, 0)])
        out = ra.densify([(0, 0), (100, 0)])
        self.assertEqual(len(out), 4)
        self.assertAlmostEqual(out[1][0], ra.STEP)
        self.assertAlmostEqual(out[2][0], 2 * ra.STEP)
        self.assertEqual(out[3], (100, 0))
        self.assertTrue(ra.is_closed([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]))
        self.assertTrue(ra.is_closed([(0, 0), (0, 10), (10, 10), (10, 0)]))       # gap 10 of 30: under a half
        self.assertFalse(ra.is_closed([(0, 0), (100, 0)]))                        # gap = length
        self.assertFalse(ra.is_closed([(0, 0), (3, 0), (0, 0)]))                  # shorter than 10
        self.assertFalse(ra.is_closed([(0, 0)]))

    def test_within_for_small_items_by_their_centres(self):
        a, near_b, far_b = geo(0, 0, 10, 10), geo(30, 0, 40, 10), geo(100, 0, 110, 10)
        self.assertFalse(ra.within(a, near_b, 10.0))        # A grown by (20, 10) only reaches x 30
        self.assertTrue(ra.within(a, near_b, 12.0))         # centres 30 apart: (1 + 1) x 12 + 2 x 7.37 = 38.7
        self.assertFalse(ra.within(a, far_b, 12.0))
        self.assertTrue(ra.within([a], [near_b, far_b], 12.0))                   # groups: any leaf pair

    def test_within_for_long_items_by_their_points(self):
        a = geo(0, 0, 200, 10, *[(x, 5.0) for x in range(0, 201, 20)])
        b = geo(0, 30, 200, 40, *[(x, 35.0) for x in range(0, 201, 20)])
        self.assertFalse(ra.within(a, b, 20.0))             # the boxes do not meet yet
        # Points 20 apart along the lines: a pair 36.06 apart is near when (20 / 36.06 + 1) d + 2
        # (the two tolerances) exceeds it, that is from d = 21.9 on; straight across (30) from d = 28.
        self.assertFalse(ra.within(a, b, 21.0))
        self.assertTrue(ra.within(a, b, 23.0))

    def test_within_for_a_closed_polygon_holding_the_other(self):
        square = geo(0, 0, 100, 100, (0, 0), (100, 0), (100, 100), (0, 100), (0, 0), t=0.1)
        inner = geo(40, 40, 60, 60, (50, 50), (55, 55), t=0.1)
        self.assertTrue(ra.within(square, inner, 0.5))      # both its points inside the closed polygon
        open_u = geo(0, 0, 100, 100, (0, 0), (100, 0), (100, 100), t=0.1)
        self.assertFalse(ra.within(open_u, inner, 0.5))     # the polygon is open (gap > half its length)

    def test_within_for_items_of_more_than_256_points(self):
        long_line = geo(0, -2, 299, 2, *[(float(x), 0.0) for x in range(300)])
        dot = geo(100, 10, 110, 14, (105.0, 12.0))
        self.assertTrue(ra.within(long_line, dot, 15.0))    # the dot's point in the line's box grown by (30, 15)
        self.assertFalse(ra.within(long_line, dot, 5.0))

    def test_cluster(self):
        a, b, c = geo(0, 0, 10, 10), geo(20, 0, 30, 10), geo(1000, 0, 1010, 10)
        self.assertEqual(ra.cluster([a, b, c], 20.0), [[0, 1], [2]])
        # c sits between a and b' (two clusters): it joins both, which merge into the first.
        one, two, middle = geo(0, 0, 10, 10), geo(60, 0, 70, 10), geo(30, 0, 40, 10)
        self.assertEqual(ra.cluster([one, two, middle], 12.0), [[0, 1, 2]])
        # Clusters come out in the order they were made (items taken by the bottom of their bounds).
        x, y, z = geo(0, 0, 10, 10), geo(0, 500, 10, 510), geo(1000, 0, 1010, 10)
        self.assertEqual(ra.cluster([x, y, z], 20.0), [[0], [2], [1]])
        self.assertEqual(ra.cluster([], 20.0), [])

    def test_gravity(self):
        rect = (-100.0, 0.0, 100.0, 50.0)
        self.assertEqual(ra.gravity([geo(0, 0, 0, 0, (0.0, 0.0), (10.0, 10.0))], rect), (5.0, 5.0))
        # 200 is 50 beyond the box widened by 50: weight ((100 - 50) / 100)^2 = 0.25.
        x, y = ra.gravity([geo(0, 0, 0, 0, (0.0, 0.0), (200.0, 0.0))], rect)
        self.assertAlmostEqual(x, 200.0 * 0.25 / 1.25)
        # Far out the weight is floored at 0.01^2.
        x, _ = ra.gravity([geo(0, 0, 0, 0, (0.0, 0.0), (400.0, 0.0))], rect)
        self.assertAlmostEqual(x, 400.0 * 1e-4 / (1.0 + 1e-4))
        self.assertEqual(ra.gravity([], rect), (0.0, 25.0))                      # the text box's centre


# ------------------------------------------------------------------ the text

class TextTests(Estimated):
    def layout(self) -> ra.TextLayout:
        return ra.text_layout(w.split(text_page())[1], EST)

    def test_the_layout(self):
        layout = self.layout()
        self.assertEqual((layout.length, layout.rect[:3]), (11, (-468.0, 234.0, 468.0)))
        self.assertAlmostEqual(layout.rect[3], layout.lines[-1].bottom + 4.0)    # the document margin
        self.assertAlmostEqual(ra.body_line_height(EST), 38 + 0.2 * 37.75 + 24, places=4)     # 69.55
        self.assertIsNone(ra.text_layout(w.split(w.blank_page(TABLET))[1], EST))

    def test_index_at(self):
        layout = self.layout()
        self.assertEqual(layout.index_at((0.0, 100.0)), -1)                       # above the box: before the text
        self.assertEqual(layout.index_at((0.0, 500.0)), 11)                       # below it: the length
        self.assertEqual(ra.index_at(layout, (-600.0, 255.0)), 0)
        self.assertEqual(layout.index_at((480.0, 255.0)), 10)                     # clamped to length - 1
        self.assertEqual(layout.index_at((238 + 3 * 16.96 + 1 - 702, 255.0)), 3)  # the caret place nearest
        self.assertEqual([layout.anchor_id(i) for i in (-1, 0, 10, 11)], [BEFORE, (1, 15), (1, 25), AFTER])
        x, y = layout.position(0)
        self.assertAlmostEqual(x, 238 - 702)
        self.assertAlmostEqual(y, 234 + 4 + 29.59375, places=4)
        self.assertEqual(layout.position(-1), layout.position(0))
        self.assertEqual(layout.position(11), layout.position(10))

    def test_over_text(self):
        layout = self.layout()
        on = geo(-404, 251, -336, 259, (-400.0, 255.0), (-340.0, 255.0), t=2.0)
        beside = geo(476, 251, 544, 259, (480.0, 255.0), (540.0, 255.0), t=2.0)
        far_above = geo(-404, 96, -336, 104, (-400.0, 100.0), (-340.0, 100.0), t=2.0)
        near_above = geo(-404, 222, -336, 232, (-400.0, 230.0), (-340.0, 230.0), t=2.0)       # 8 above the line
        too_high = geo(-404, 216, -336, 226, (-400.0, 224.0), (-340.0, 224.0), t=2.0)         # 14 above it
        self.assertTrue(ra.over_text(layout, on))
        self.assertFalse(ra.over_text(layout, beside))
        self.assertFalse(ra.over_text(layout, far_above))
        self.assertTrue(ra.over_text(layout, near_above))       # the first line's top is 10 higher
        self.assertFalse(ra.over_text(layout, too_high))
        top = layout.line_rects(on.box)[0]
        self.assertAlmostEqual(top[1], layout.lines[0].top - 10.0)
        self.assertAlmostEqual(top[3], layout.lines[0].bottom + 10.0)


# ------------------------------------------------------------------ a new stroke

class PlacementTests(Estimated):
    def test_a_stroke_over_a_text_line_makes_a_bidirectional_group_anchored_at_the_character(self):
        page = text_page()
        self.assertEqual(w.highest_counter(w.split(page)[1]), 25)
        out = s.append_stroke(page, author=TABLET, line=bar(-400.0, -340.0, 255.0))
        scene, lines = layout_of(page)
        i = renderer.index_at(lines, -370.0 + scene.x_origin, 255.0, EST)
        x, base = renderer.index_point(scene, lines, i, EST)
        [stroke] = parse(out).strokes
        a = stroke.anchor
        self.assertEqual((a.char_id, a.type, a.threshold), ((1, 15 + i), 1, 0.0))
        self.assertAlmostEqual(a.origin_x, x, places=3)
        # Stored at -P; ids N .. N+4 for the group (26 .. 30), then the stroke.
        self.assertAlmostEqual(stroke.points[0].x, -400.0 - x, places=3)
        self.assertAlmostEqual(stroke.points[0].y, 255.0 - base, places=3)
        self.assertEqual((stroke.item_id, parents_of(out)[(1, 26)][0].item), ((1, 31), (1, 31)))
        self.assertEqual(list(anchors_of(out)), [(1, 26)])
        self.assertEqual(w.sequence(w.split(out)[1], LAYER)[-1][0], (1, 27))
        self.assertEqual(w.highest_counter(w.split(out)[1]), 31)
        # The three blocks of the group and the stroke are all it changed.
        old, new = w.split(page)[1], w.split(out)[1]
        self.assertEqual(len(new), len(old) + 4)

    def test_a_stroke_beside_the_text_makes_a_vertical_group(self):
        out = s.append_stroke(text_page(), author=TABLET, line=bar(480.0, 540.0, 255.0))
        [stroke] = parse(out).strokes
        a = stroke.anchor
        self.assertEqual((a.type, a.threshold, a.char_id), (2, rm_anchor.THRESHOLD, (1, 25)))
        self.assertAlmostEqual(a.threshold, 35.748032, places=5)

    def test_a_large_stroke_is_vertical_even_over_the_text(self):
        line = s.draw([(-300.0 + 20 * n, 250.0 + 20 * n) for n in range(11)], s.FINELINER_V2)
        box = s.line_bounds(line)
        self.assertGreater(box[2] - box[0], 178.74015748)
        self.assertGreater(box[3] - box[1], 178.74015748)
        [stroke] = parse(s.append_stroke(text_page(), author=TABLET, line=line)).strokes
        self.assertEqual((stroke.anchor.type, stroke.anchor.threshold), (2, rm_anchor.THRESHOLD))
        # The same stroke, no longer taller than 20 mm, over the text: bidirectional.
        small = s.draw([(-300.0, 250.0), (-100.0, 260.0)], s.FINELINER_V2)
        [stroke] = parse(s.append_stroke(text_page(), author=TABLET, line=small)).strokes
        self.assertEqual(stroke.anchor.type, 1)

    def test_a_stroke_above_the_text_box_is_before_the_text_and_one_below_it_after(self):
        scene, lines = layout_of(text_page())
        above = s.append_stroke(text_page(), author=TABLET, line=bar(-400.0, -340.0, 100.0))
        [stroke] = parse(above).strokes
        first = renderer.index_point(scene, lines, 0, EST)
        self.assertEqual((stroke.anchor.char_id, stroke.anchor.type), (BEFORE, 2))
        self.assertAlmostEqual(stroke.anchor.origin_x, first[0], places=3)
        self.assertAlmostEqual(stroke.points[0].y, 100.0 - first[1], places=3)
        below = s.append_stroke(text_page(), author=TABLET, line=bar(-400.0, -340.0, 500.0))
        [stroke] = parse(below).strokes
        last = renderer.index_point(scene, lines, 10, EST)
        self.assertEqual((stroke.anchor.char_id, stroke.anchor.type), (AFTER, 2))
        self.assertAlmostEqual(stroke.anchor.origin_x, last[0], places=3)
        self.assertAlmostEqual(stroke.points[0].x, -400.0 - last[0], places=3)

    def test_a_stroke_near_a_group_of_its_type_joins_it(self):
        first = s.append_stroke(text_page(), author=TABLET, line=bar(480.0, 540.0, 500.0))
        second = s.append_stroke(first, author=TABLET, line=bar(480.0, 540.0, 505.0))
        self.assertEqual(list(anchors_of(second)), [(1, 26)])
        self.assertEqual([x.item for x in parents_of(second)[(1, 26)]], [(1, 31), (1, 34)])
        # No group made, but the join always re-anchors (0x1409773b0): anchorId stamped 32, originX
        # 33 (reset to the character's x, here what it was), the stroke 34.
        self.assertEqual(w.highest_counter(w.split(second)[1]), 34)
        self.assertEqual(anchors_of(second), anchors_of(first))
        base = s.strokes(second)[1].line.unpacked()[0].y
        self.assertAlmostEqual(base, 505.0 - renderer.index_point(*layout_of(text_page()), 10, EST)[1], places=3)
        # Far from it: a group of its own.
        third = s.append_stroke(second, author=TABLET, line=bar(480.0, 540.0, 700.0))
        self.assertEqual(sorted(anchors_of(third)), [(1, 26), (1, 35)])

    def test_with_two_groups_near_it_the_one_with_more_children_is_kept_and_the_other_merged(self):
        page = text_page()
        for y in (500.0, 505.0, 600.0):
            page = s.append_stroke(page, author=TABLET, line=bar(480.0, 540.0, y))
        # Ids: group 26 (stroke 31), the join stamps 32, 33 (stroke 34), group 35 (stroke 40).
        self.assertEqual(sorted(anchors_of(page)), [(1, 26), (1, 35)])
        self.assertEqual([len(v) for _, v in sorted(parents_of(page).items())], [2, 1])
        before = scene_state(page)
        bridge = s.draw([(510.0, 500.0), (510.0, 600.0)], s.FINELINER_V2)
        out = s.append_stroke(page, author=TABLET, line=bridge)
        blocks = w.split(out)[1]
        # The kept group (1:26) holds its two strokes, a clone of the merged one's (moveId its id) and the bridge.
        self.assertEqual(list(parents_of(out)), [(1, 26)])
        # The re-anchor stamps 41, 42; the merge: anchor removed 43, clone 44, detach 45; the bridge 46.
        self.assertEqual([x.item for x in parents_of(out)[(1, 26)]], [(1, 31), (1, 34), (1, 44), (1, 46)])
        self.assertEqual(s.strokes(out)[2].line.move_id, (1, 40))
        self.assertEqual(len(parse(out).strokes), 4)
        # The merged group lost its anchor and was detached: a SceneTreeMove with a new id,
        # is_update 0, parent 0:0; its group item is still live.
        self.assertEqual(anchors_of(out)[(1, 35)].char_id, ZERO)
        self.assertEqual(moves_of(out, (1, 35))[-1], ((1, 45), (1, 35), 0, ZERO))
        self.assertEqual(w.tree_parents(blocks)[(1, 35)], ZERO)
        self.assertEqual(anchors_of(out)[(1, 26)].char_id, AFTER)
        self.assertIn((1, 36), [i for i, _ in w.sequence(blocks, LAYER)])
        self.assertEqual([n for n, _ in s.group_nodes(blocks, LAYER)], [(1, 26)])
        # Seen from the page nothing moved but the bridge came.
        scene = scene_state(out)
        for state in before:
            self.assertIn(state, scene)
        self.assertEqual(len(scene), len(before) + 1)

    def test_on_a_tie_the_first_group_is_kept(self):
        page = text_page()
        for y in (500.0, 600.0):
            page = s.append_stroke(page, author=TABLET, line=bar(480.0, 540.0, y))
        out = s.append_stroke(page, author=TABLET, line=s.draw([(510.0, 500.0), (510.0, 600.0)], s.FINELINER_V2))
        self.assertEqual(list(parents_of(out)), [(1, 26)])
        self.assertEqual(w.tree_parents(w.split(out)[1])[(1, 32)], ZERO)      # the second group: node 32

    def test_loose_strokes_near_it_are_absorbed_and_far_clusters_each_get_a_group(self):
        loose = [line_block(LAYER, (1, 26), pts(480.0, 520.0, 255.0)),
                 line_block(LAYER, (1, 27), pts(480.0, 520.0, 262.0), (1, 26)),
                 line_block(LAYER, (1, 28), pts(-300.0, -250.0, 600.0), (1, 27)),
                 line_block(LAYER, (1, 29), pts(-300.0, -250.0, 1000.0), (1, 28))]
        page = text_page(*loose)
        before = scene_state(page)
        placed = ra.place(page, author=TABLET, layer=LAYER, values=[bar(480.0, 540.0, 275.0)])
        # In 3.29's order: the new group, the absorbed cluster's clones, each far cluster's
        # group with its clone, last the new stroke.
        self.assertEqual((placed.nodes, placed.clones, placed.ids), ([(1, 30), (1, 37), (1, 43)],
                                                                     [(1, 35), (1, 36), (1, 42), (1, 48)], [(1, 49)]))
        out = placed.page
        self.assertEqual(parents_of(out).get(LAYER), None)
        self.assertEqual({x.line.move_id for x in s.strokes(out) if x.item in placed.clones},
                         {(1, 26), (1, 27), (1, 28), (1, 29)})
        self.assertEqual([x.item for x in parents_of(out)[(1, 30)]], [(1, 35), (1, 36), (1, 49)])
        anchors = anchors_of(out)
        self.assertEqual([(anchors[n].char_id, anchors[n].kind) for n in placed.nodes],
                         [((1, 25), 2), (AFTER, 2), (AFTER, 2)])
        # Every loose stroke is drawn where it was, now anchored; the new stroke is one more.
        state = scene_state(out)
        shown = [polyline for _, polyline in state]
        for _, polyline in before:
            self.assertIn(polyline, shown)
        self.assertTrue(all(anchor is not None for anchor, _ in state))
        self.assertEqual(len(state), len(before) + 1)

    def test_a_page_without_text_is_drawn_on_as_before(self):
        page = w.blank_page(TABLET)
        line = bar(-100.0, 100.0, 300.0)
        self.assertIsNone(ra.place_strokes(page, author=TABLET, lines=[line]))
        self.assertIsNone(ra.place_strokes(None, author=TABLET, lines=[line]))
        header, blocks = w.split(page)
        new = w.new_block(w.BLOCK_LINE_ITEM, w.ItemHeader(LAYER, (1, 15), ZERO, ZERO, 0, line.encode()).encode())
        self.assertEqual(s.append_stroke(page, author=TABLET, line=line), w.join(header, blocks + [new]))
        self.assertIsNone(ra.regroup(page, author=TABLET, layer=LAYER, nodes=[(1, 30)]))

    def test_several_strokes_are_placed_one_after_the_other(self):
        lines = [bar(480.0, 540.0, 500.0), bar(480.0, 540.0, 505.0)]
        together, ids = s.append_strokes(text_page(), author=TABLET, lines=lines)
        one = s.append_stroke(s.append_stroke(text_page(), author=TABLET, line=lines[0]), author=TABLET, line=lines[1])
        self.assertEqual((together, ids), (one, [(1, 31), (1, 34)]))


# ------------------------------------------------------------------ the eraser

def erase_page() -> bytes:
    """A group (1:30) anchored to the last character, origin x 480, holding three strokes at
    y -20, -10 and 0 (1:32 .. 1:34) and one far below at y 200 (1:35)."""
    stored = [line_block(GROUP, (1, 32 + n), pts(0.0, 60.0, y), (1, 31 + n) if n else ZERO)
              for n, y in enumerate((-20.0, -10.0, 0.0, 200.0))]
    return text_page(group_blocks(GROUP, (1, 31), (1, 25), 480.0), *stored)


class EraseTests(Estimated):
    def eraser(self, y: float) -> s.LineValue:
        return s.eraser_line([(510.25, y - 40.0), (510.25, y), (510.25, y + 40.0)], s.MEDIUM)

    def test_an_erase_that_splits_a_group_leaves_the_smaller_cluster_and_re_anchors_the_other(self):
        page = erase_page()
        base = baseline()
        before = scene_state(page)
        erased = s.erase(page, author=TABLET, eraser=self.eraser(base + 200.0), anchors={GROUP: (480.0, base)})
        self.assertEqual((erased.deleted, erased.pieces), (((1, 35),), (((1, 36), (1, 37)),)))
        # The three strokes near the text and the two pieces far below are two clusters; the
        # fewer (the pieces) stay, the three are cloned into a group of their own (1:38 ..).
        self.assertEqual((erased.nodes, erased.clones), (((1, 38),), ((1, 43), (1, 44), (1, 45))))
        out = erased.page
        groups = parents_of(out)
        self.assertEqual([x.item for x in groups[GROUP]], [(1, 36), (1, 37)])
        self.assertEqual([x.item for x in groups[(1, 38)]], [(1, 43), (1, 44), (1, 45)])
        self.assertEqual([x.line.move_id for x in groups[(1, 38)]], [(1, 32), (1, 33), (1, 34)])
        anchors = anchors_of(out)
        self.assertEqual((anchors[(1, 38)].char_id, anchors[(1, 38)].kind), ((1, 25), 2))
        # What stayed is re-anchored to the gravity of what it holds: below the text, the after-text sentinel.
        self.assertEqual((anchors[GROUP].char_id, anchors[GROUP].kind), (AFTER, 2))
        scene, lines = layout_of(page)
        self.assertAlmostEqual(anchors[GROUP].origin_x, renderer.index_point(scene, lines, 10, EST)[0], places=3)
        # Every piece is drawn where it was (the groups' strokes moved in place or cloned).
        state = scene_state(out)
        old = [p for a, p in before if p[0][1] < base + 100.0]
        self.assertEqual(sorted(p for a, p in state if p[0][1] < base + 100.0), sorted(old))
        pieces = [p for a, p in state if p[0][1] > base + 100.0]
        self.assertEqual((len(pieces), min(p[0][0] for p in pieces), max(p[-1][0] for p in pieces)), (2, 480.0, 540.0))

    def test_an_erase_that_empties_a_group_detaches_it(self):
        page = text_page(group_blocks(GROUP, (1, 31), (1, 25), 480.0), line_block(GROUP, (1, 32), pts(0.0, 60.0, 0.0)))
        base = baseline()
        lasso = s.new_line(s.ERASE_SECTION, 0, 2.0,
                           [s.Point(x, y, 0, 16, 0, 0) for x, y in ((470.3, base - 15), (550.3, base - 15),
                                                                     (550.3, base + 15), (470.3, base + 15))])
        erased = s.erase(page, author=TABLET, eraser=lasso, anchors={GROUP: (480.0, base)})
        self.assertEqual((erased.deleted, erased.made, erased.nodes), (((1, 32),), (), ()))
        blocks = w.split(erased.page)[1]
        self.assertEqual(moves_of(erased.page, GROUP)[-1], ((1, 33), GROUP, 0, ZERO))
        self.assertEqual(w.tree_parents(blocks)[GROUP], ZERO)
        self.assertEqual(s.group_nodes(blocks, LAYER), [])
        self.assertIn((1, 31), [i for i, _ in w.sequence(blocks, LAYER)])      # its group item stays live
        self.assertEqual(parse(erased.page).strokes, [])

    def test_a_group_without_a_position_is_left_alone(self):
        page = erase_page()
        base = baseline()
        erased = s.erase(page, author=TABLET, eraser=self.eraser(base + 200.0))
        self.assertEqual(erased.deleted, ())
        self.assertIs(erased.page, page)


# ------------------------------------------------------------------ writers and edits

class WriterTests(Estimated):
    def test_set_node_brings_a_node_to_a_state_and_writes_only_what_differs(self):
        page = erase_page()
        self.assertIs(ra.set_node(page, author=TABLET, node=GROUP, char_id=(1, 25), origin_x=480.0, attached=True,
                                  parent=LAYER), page)
        moved = ra.set_node(page, author=TABLET, node=GROUP, char_id=AFTER, origin_x=-300.0, attached=False,
                            values={(1, 32): s.new_line(17, 0, 2.0, points_v2(pts(1.0, 2.0, 3.0))).encode()})
        a = anchors_of(moved)[GROUP]
        self.assertEqual((a.char_id, a.origin_x), (AFTER, -300.0))
        self.assertEqual(w.tree_parents(w.split(moved)[1])[GROUP], ZERO)
        self.assertEqual(s.strokes(moved)[0].line.unpacked()[0].x, 1.0)
        back = ra.set_node(moved, author=TABLET, node=GROUP, char_id=(1, 25), origin_x=480.0, attached=True,
                           parent=LAYER)
        self.assertEqual(anchors_of(back)[GROUP], anchors_of(page)[GROUP])
        self.assertEqual(w.tree_parents(w.split(back)[1])[GROUP], LAYER)

    def test_an_anchor_set_edit_is_kept_in_the_ledger_as_it_is(self):
        edit = AnchorSet.new(te.THROWAWAY, te.PAGE1, ("edit", "abc/g0"), te.NOW, ("page", 0, 2 ** 48 - 1), -12.5, False,
                             ("page", 0, 11), [(("page", 1, 32), "00ff")], uid="u1")
        raw = json.loads(json.dumps({"kind": "anchor-set", **dataclasses.asdict(edit)}))
        self.assertEqual(AnchorSet.load(raw), edit)
        self.assertEqual(hash(AnchorSet.load(raw)), hash(edit))


def library_cloud(page: bytes) -> FakeCloud:
    cloud = FakeCloud()
    cloud.put_item(te.THROWAWAY, te.meta("throwaway"), te.notebook_content(te.PAGE1, te.PAGE2),
                   {f"{te.THROWAWAY}/{te.PAGE1}.rm": page})
    return cloud


class LibraryTests(Estimated):
    """One user action, one undo step: undone, the page looks as it did; redone, as it did after."""

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def start(self, page: bytes):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = library_cloud(page)
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway",))
        self.doc = self.source.refresh().get(te.THROWAWAY)

    def page(self, source=None) -> bytes:
        source = source or self.source
        return source.staged(source.sync.tree.get(te.THROWAWAY)).page(te.PAGE1)

    def restarted(self) -> LibrarySource:
        source = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                               editable=("throwaway",))
        source.refresh()
        return source

    def offsets(self) -> dict:
        scene = parse(self.page())
        by_object = renderer.stroke_offsets(scene, renderer.layout_text(scene, EST), EST)
        return {tuple(x.item_id): by_object[id(x)] for x in scene.strokes if id(x) in by_object}

    def check_step(self, act, now: int):
        """Do `act`, undo it, redo it: the scene before, after, before again, after again;
        the ledger replays to the same page (also from disk), twice the same."""
        before = scene_state(self.page())
        act()
        after = scene_state(self.page())
        self.assertNotEqual(after, before)
        self.assertTrue(self.source.undo(self.doc, te.PAGE1, now + 1))
        self.assertEqual(scene_state(self.page()), before)
        self.assertTrue(self.source.redo(self.doc, te.PAGE1, now + 2))
        self.assertEqual(scene_state(self.page()), after)
        edits = self.sync.edits.of(te.THROWAWAY)
        base = self.source.sync.tree.get(te.THROWAWAY)
        one = replay(self.source.blob(base, f"{te.THROWAWAY}/{te.PAGE1}.rm"), edits, self.sync.author()).page
        two = replay(self.source.blob(base, f"{te.THROWAWAY}/{te.PAGE1}.rm"), edits, self.sync.author()).page
        self.assertEqual(one, two)
        self.assertEqual(self.page(self.restarted()), self.page())
        self.assertTrue(self.source.undo(self.doc, te.PAGE1, now + 3))
        self.assertEqual(scene_state(self.page()), before)
        self.assertEqual(self.page(self.restarted()), self.page())
        self.assertTrue(self.source.redo(self.doc, te.PAGE1, now + 4))
        self.assertEqual(scene_state(self.page()), after)

    def test_strokes_that_make_join_and_merge_groups_are_one_undo_step_each(self):
        self.start(text_page())
        now = te.NOW
        for n, y in enumerate((500.0, 505.0, 600.0)):
            now += 10
            self.check_step(lambda y=y, now=now: self.source.add_stroke(self.doc, te.PAGE1, bar(480.0, 540.0, y), now),
                            now)
        self.assertEqual(self.source.history_state(self.doc, te.PAGE1), (True, False))
        bridge = s.draw([(510.0, 500.0), (510.0, 600.0)], s.FINELINER_V2)
        now += 10
        self.check_step(lambda: self.source.add_stroke(self.doc, te.PAGE1, bridge, now), now)
        out = self.page()
        self.assertEqual(len([n for n, _ in s.group_nodes(w.split(out)[1], LAYER)]), 1)
        self.assertEqual(len(parse(out).strokes), 4)

    def test_loose_strokes_absorbed_and_anchored_are_one_undo_step(self):
        loose = [line_block(LAYER, (1, 26), pts(480.0, 520.0, 255.0)),
                 line_block(LAYER, (1, 27), pts(480.0, 520.0, 262.0), (1, 26)),
                 line_block(LAYER, (1, 28), pts(-300.0, -250.0, 600.0), (1, 27))]
        self.start(text_page(*loose))
        before = scene_state(self.page())
        self.check_step(lambda: self.source.add_stroke(self.doc, te.PAGE1, bar(480.0, 540.0, 275.0), te.NOW + 10),
                        te.NOW + 10)
        self.assertEqual(len(scene_state(self.page())), len(before) + 1)
        self.assertEqual(parents_of(self.page()).get(LAYER), None)

    def test_an_erase_that_splits_a_group_is_one_undo_step(self):
        self.start(erase_page())
        base = baseline()
        offsets = self.offsets()
        self.assertEqual(sorted(offsets.values())[0], (480.0, base))

        def erase():
            edit = self.source.erase_strokes(self.doc, te.PAGE1,
                                             s.eraser_line([(510.25, base + 160.0), (510.25, base + 200.0),
                                                            (510.25, base + 240.0)], s.MEDIUM), te.NOW, offsets)
            self.assertIsNotNone(edit)
        self.check_step(erase, te.NOW)
        self.assertEqual(len(s.group_nodes(w.split(self.page())[1], LAYER)), 2)

    def test_an_erase_that_empties_a_group_is_one_undo_step(self):
        self.start(text_page(group_blocks(GROUP, (1, 31), (1, 25), 480.0), line_block(GROUP, (1, 32), pts(0.0, 60.0, 0.0))))
        base = baseline()
        lasso = s.new_line(s.ERASE_SECTION, 0, 2.0,
                           [s.Point(x, y, 0, 16, 0, 0) for x, y in ((470.3, base - 15), (550.3, base - 15),
                                                                     (550.3, base + 15), (470.3, base + 15))])
        offsets = self.offsets()
        self.check_step(lambda: self.assertIsNotNone(self.source.erase_strokes(self.doc, te.PAGE1, lasso, te.NOW,
                                                                              offsets)), te.NOW)
        self.assertEqual(s.group_nodes(w.split(self.page())[1], LAYER), [])


if __name__ == "__main__":
    unittest.main()
