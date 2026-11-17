"""Offline tests for selecting, moving, scaling, turning, copying and pasting strokes and
images (viewer.rm_select, ui.selection, core.edits.ItemsTransform / StrokePaste,
ui.library.LibrarySource.transform_items / paste_strokes / delete_items), against
ARCHITECTURE.md 5.1 "Moving, scaling and rotating a selection".

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import math
import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTransform  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.edits import (ItemsTransform, KINDS, PendingEdits, StrokeDelete, StrokePaste,  # noqa: E402
                        StrokeRestore, replay)
from ui import editing, selection  # noqa: E402
from viewer import rm_select as sel  # noqa: E402
from viewer import rm_stroke as rs  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

from test_edits_offline import LAYER, NOW, PAGE1, THROWAWAY, Base  # noqa: E402

AUTHOR = w.author_uuid("selection-tests")


def f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


def page_with(*lines: rs.LineValue) -> tuple[bytes, list]:
    return rs.append_strokes(None, author=AUTHOR, lines=list(lines))


def line_at(x: float, y: float, tool: int = rs.BALLPOINT_V2, n: int = 8, thickness: float = 2.0) -> rs.LineValue:
    return rs.draw([(x + 7 * k, y + 3 * k * (k % 3)) for k in range(n)], tool, thickness=thickness)


def items(page: bytes) -> list[w.ItemHeader]:
    return [w.decode_item(b.payload) for b in w.split(page)[1] if b.type == w.BLOCK_LINE_ITEM]


class MatrixTests(unittest.TestCase):
    """Products and maps in QTransform's order, checked against Qt itself."""

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def qt_of(self, m) -> QTransform:
        return QTransform(*m)

    def test_products_map_as_qtransform_does(self):
        a = sel.multiply(sel.translation(10, -4), sel.rotation(30, 40, 37.5))
        b = sel.multiply(a, sel.scaling(-5, 6, 1.5, -0.75))
        q = QTransform().translate(10, -4) * (QTransform().translate(30, 40).rotate(37.5).translate(-30, -40)) \
            * (QTransform().translate(-5, 6).scale(1.5, -0.75).translate(5, -6))
        for x, y in ((0, 0), (12.5, -3), (1000, 700)):
            p = q.map(float(x), float(y))
            got = sel.map_point(b, x, y)
            self.assertAlmostEqual(got[0], p[0], places=9)
            self.assertAlmostEqual(got[1], p[1], places=9)

    def test_quarter_turns_are_exact_as_qt_makes_them(self):
        self.assertEqual(sel.rotation(0, 0, 90)[:4], (0.0, 1.0, -1.0, 0.0))
        self.assertEqual(sel.rotation(0, 0, 180)[:4], (-1.0, 0.0, -0.0, -1.0))
        self.assertEqual(sel.map_point(sel.rotation(0, 0, 90), 1, 0), (0.0, 1.0))     # clockwise on screen

    def test_identity_and_translation_are_fuzzy_like_qt(self):
        self.assertTrue(sel.is_identity((1.0, 0.0, 0.0, 1.0 + 1e-13, 0.0, 0.0)))
        self.assertFalse(sel.is_identity(sel.translation(0.01, 0)))
        self.assertTrue(sel.is_translation(sel.translation(5, 6)))
        self.assertFalse(sel.is_translation(sel.rotation(0, 0, 1)))

    def test_conjugation_with_an_anchor(self):
        m = sel.rotation(0, 0, 90)
        c = sel.conjugated(m, (100.0, 50.0))
        # stored s, scene = s + a: the new stored point is m(s + a) - a.
        x, y = sel.map_point(c, 10, 20)
        sx, sy = sel.map_point(m, 110, 70)
        self.assertAlmostEqual(x, sx - 100)
        self.assertAlmostEqual(y, sy - 50)
        self.assertIs(sel.conjugated(m, (0.0, 0.0)), m)


class LineTransformTests(unittest.TestCase):
    """Line::transform (0x140b3d4d0)."""

    def setUp(self):
        self.line = rs.LineValue(rs.BALLPOINT_V2, 0, 2.0, 0.0, rs.pack_points(
            [rs.Point(1.25, 2.5, 7, 13, 40, 200), rs.Point(-30.75, 400.125, 9, 21, 255, 180)]),
            (1, 99), (1, 7), None, None, b"")

    def test_the_identity_changes_nothing(self):
        self.assertIs(sel.transform_line(self.line, sel.IDENTITY), self.line)

    def test_a_translation_moves_points_and_keeps_every_size(self):
        out = sel.transform_line(self.line, sel.translation(0.1, -1000.3))
        self.assertEqual(out.thickness_scale, 2.0)
        for a, b in zip(self.line.unpacked(), out.unpacked()):
            self.assertEqual(b.x, f32(a.x + 0.1))                   # mapped in double, stored as float32
            self.assertEqual(b.y, f32(a.y - 1000.3))
            self.assertEqual((b.speed, b.width, b.direction, b.pressure), (a.speed, a.width, a.direction, a.pressure))
        self.assertEqual((out.timestamp, out.move_id), (self.line.timestamp, self.line.move_id))

    def test_a_turn_keeps_widths_and_direction(self):
        out = sel.transform_line(self.line, sel.rotation(0, 0, 33))
        self.assertEqual(out.thickness_scale, 2.0)                  # sqrt(|det|) of a turn is 1
        self.assertEqual([(p.width, p.direction, p.speed) for p in out.unpacked()],
                         [(p.width, p.direction, p.speed) for p in self.line.unpacked()])

    def test_a_scale_scales_thickness_and_rounds_widths_in_single_precision(self):
        out = sel.transform_line(self.line, sel.scaling(0, 0, 1.7, 1.7))
        s = math.sqrt(1.7 * 1.7)
        self.assertEqual(out.thickness_scale, 2.0 * s)
        for a, b in zip(self.line.unpacked(), out.unpacked()):
            self.assertEqual(b.width, rs._roundf(f32(f32(f32(a.width * 0.25) * f32(s)) * 4.0)))
            self.assertEqual((b.speed, b.direction, b.pressure), (a.speed, a.direction, a.pressure))
        # A non-uniform scale uses sqrt(|det|).
        out = sel.transform_line(self.line, sel.scaling(0, 0, 2.0, -0.5))
        self.assertEqual(out.thickness_scale, 2.0)
        self.assertEqual(out.unpacked()[1].width, 21)                # sqrt(|2 * -0.5|) = 1

    def test_widths_after_a_scale_follow_the_corpus_rule(self):
        # Fineliner points have width 2T*4 = 16 at thickness 2; scaled by T'/T = 1/3 * ...:
        # the app's scaled Fineliner strokes have roundf(24 * T/3) (ARCHITECTURE.md 5.1).
        line = rs.draw([(0, 0), (10, 0), (20, 5)], rs.FINELINER_V2, thickness=3.0)
        self.assertEqual({p.width for p in line.unpacked()}, {24})
        out = sel.transform_line(line, sel.scaling(0, 0, 0.4, 0.4))
        self.assertAlmostEqual(out.thickness_scale, 1.2)
        self.assertEqual({p.width for p in out.unpacked()}, {rs._roundf(24 * f32(0.4))})


class CommitTests(unittest.TestCase):
    """The selection commit (0x1409b48a0): delete, add clones, delete them, add the
    transformed ones: 2n ids."""

    def test_two_generations_of_ids_and_one_deleted_run(self):
        page, ids = page_with(line_at(0, 100), line_at(300, 100))
        out, made, old = sel.transform_items(page, author=AUTHOR, items=ids, m=sel.translation(10, 20))
        clock = max(i[1] for i in ids)
        self.assertEqual(made, {ids[0]: (1, clock + 3), ids[1]: (1, clock + 4)})
        line_items = items(out)
        # originals (deleted) . n deleted clones . n live: the strokes were the last drawn,
        # so originals and clones are one run (as real pages read).
        run = line_items[0]
        self.assertEqual((run.item, run.deleted_length, run.value), (ids[0], 4, None))
        live = line_items[1:]
        self.assertEqual([i.item for i in live], [(1, clock + 3), (1, clock + 4)])
        self.assertEqual(live[0].left, (1, clock + 2))
        self.assertEqual(live[1].left, (1, clock + 3))
        self.assertTrue(all(i.right == w.ZERO_ID for i in live))
        self.assertEqual(set(old), set(ids))

    def test_move_id_names_the_first_item_however_often_it_moves(self):
        page, [first] = page_with(line_at(0, 100))
        page, made, _ = sel.transform_items(page, author=AUTHOR, items=[first], m=sel.translation(5, 5))
        again, made2, _ = sel.transform_items(page, author=AUTHOR, items=list(made.values()), m=sel.rotation(0, 0, 45))
        [stroke] = rs.strokes(again)
        self.assertEqual(stroke.line.move_id, first)
        self.assertEqual(stroke.line.timestamp, rs.DEFAULT_TIMESTAMP)     # the transform carries no timestamp
        # The second commit used 2 more ids.
        self.assertEqual(stroke.item[1], list(made.values())[0][1] + 2)

    def test_identity_and_gone_items_change_nothing(self):
        page, ids = page_with(line_at(0, 100))
        self.assertIs(sel.transform_items(page, author=AUTHOR, items=ids, m=sel.IDENTITY)[0], page)
        self.assertIs(sel.transform_items(page, author=AUTHOR, items=[(1, 999)], m=sel.translation(1, 1))[0], page)

    def test_items_not_selected_stay_byte_for_byte(self):
        page, ids = page_with(line_at(0, 100), line_at(300, 100), line_at(600, 100))
        out, made, _ = sel.transform_items(page, author=AUTHOR, items=[ids[1]], m=sel.translation(0, 50))
        before = {b.payload for b in w.split(page)[1] if b.type == w.BLOCK_LINE_ITEM}
        after = {b.payload for b in w.split(out)[1] if b.type == w.BLOCK_LINE_ITEM}
        for item in (ids[0], ids[2]):
            payload = next(p for p in before if w.decode_item(p).item == item)
            self.assertIn(payload, after)
        # The moved one goes on top: z-order of the layer is the sequence.
        order = [i for i, _ in w.sequence(w.split(out)[1], LAYER)]
        self.assertEqual(order[-1], made[ids[1]])

    def test_images_move_in_the_same_commit(self):
        page, ids = page_with(line_at(0, 100))
        key = bytes(range(16))
        page = w.append_image(page, author=AUTHOR, key=key, file_name="a.png",
                              vertices=[(-50, 0, 0, 0), (50, 0, 1, 0), (50, 40, 1, 1), (-50, 40, 0, 1)])
        image_id, value = w.image_item(page, key)
        out, made, old = sel.transform_items(page, author=AUTHOR, items=[ids[0], image_id], m=sel.translation(10, 0))
        moved_id, moved = w.image_item(out, key)
        self.assertEqual(moved_id, made[image_id])
        self.assertEqual(moved.move_id, image_id)
        self.assertEqual(moved.ref, key)
        self.assertEqual([v[0] for v in moved.vertices], [-40, 60, 60, -40])
        self.assertEqual(moved.bounds_timestamp, value.bounds_timestamp)
        self.assertIsInstance(old[image_id], w.ImageValue)
        self.assertEqual(parse(out).layers[0].images[0].vertices[0][0], -40)

    def test_an_anchored_node_gets_the_conjugated_transform(self):
        page, ids = page_with(line_at(0, 100))
        anchor = (40.0, 300.0)
        m = sel.rotation(0, 0, 90)
        out, made, _ = sel.transform_items(page, author=AUTHOR, items=ids, m=m, anchors={LAYER: anchor})
        [stroke] = rs.strokes(out)
        for a, b in zip(rs.strokes(page)[0].line.unpacked(), stroke.line.unpacked()):
            x, y = sel.map_point(m, a.x + anchor[0], a.y + anchor[1])
            self.assertEqual((b.x, b.y), (f32(x - anchor[0]), f32(y - anchor[1])))

    def test_the_legacy_eraser_stroke_can_be_moved(self):
        eraser = rs.LineValue(rs.ERASER, 2, 2.0, 0.0, rs.pack_points([rs.Point(0, 0, 0, 80, 0, 200),
                                                                       rs.Point(9, 9, 0, 80, 0, 200)]))
        page, ids = page_with(line_at(0, 0))
        page, [legacy] = sel.append_items(page, author=AUTHOR, values=[eraser])
        out, made, _ = sel.transform_items(page, author=AUTHOR, items=[legacy], m=sel.translation(1, 1))
        self.assertIn(legacy, made)


class PasteTests(unittest.TestCase):
    def test_clones_lose_move_id_and_keep_timestamp(self):
        line = rs.LineValue(rs.FINELINER_V2, 0, 2.0, 0.0, rs.pack_points([rs.Point(0, 0, 0, 16, 0, 179)] * 2),
                            (3, 4), (1, 15))
        c = sel.clone(line)
        self.assertIsNone(c.move_id)
        self.assertEqual(c.timestamp, (3, 4))

    def test_pasted_items_are_centred_together_and_appended(self):
        a, b = line_at(0, 0), line_at(400, 300)
        placed = sel.pasted([a, b], 1.0, (100.0, 1000.0))
        box = sel.union([sel.line_box(v) for v in placed])
        self.assertAlmostEqual((box[0] + box[2]) / 2, 100.0, places=3)
        self.assertAlmostEqual((box[1] + box[3]) / 2, 1000.0, places=3)
        # Relative placement kept: one translation for all.
        da = a.unpacked()[0].x - placed[0].unpacked()[0].x
        db = b.unpacked()[0].x - placed[1].unpacked()[0].x
        self.assertAlmostEqual(da, db, places=3)
        page, ids = page_with(line_at(0, 0))
        out, new = sel.append_items(page, author=AUTHOR, values=placed)
        self.assertEqual(new, [(1, ids[0][1] + 1), (1, ids[0][1] + 2)])
        self.assertTrue(all(s.line.move_id is None for s in rs.strokes(out)))

    def test_a_scaled_paste_scales_sizes(self):
        placed = sel.pasted([line_at(0, 0, thickness=2.0)], 0.5, (0, 0))
        self.assertEqual(placed[0].thickness_scale, 1.0)

    def test_tools_a_layer_refuses_are_not_pasted(self):
        bad = rs.LineValue(rs.SELECTION_TOOL, 0, 2.0, 0.0, rs.pack_points([rs.Point(0, 0, 0, 8, 0, 1)] * 2))
        with self.assertRaises(sel.SelectError):
            sel.append_items(None, author=AUTHOR, values=[bad])


class PickingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page, self.ids = page_with(line_at(0, 100), line_at(300, 100), line_at(0, 600))
        self.scene = parse(self.page)
        self.items = selection.selectable(self.scene, 0)

    def test_a_rectangle_takes_a_stroke_any_point_of_which_it_holds(self):
        # A rectangle around only the first point of the first stroke still takes it.
        p = self.scene.layers[0].strokes[0].points[0]
        rect = (round(p.x) - 1, round(p.y) - 1, 3, 3)
        self.assertEqual(selection.pick_rect(self.items, rect), [self.ids[0]])
        self.assertEqual(selection.pick_rect(self.items, (-2000, -2000, 5000, 5000)), self.ids)
        self.assertEqual(selection.pick_rect(self.items, (5000, 5000, 10, 10)), [])

    def test_points_are_rounded_and_the_right_edge_is_inside_minus_one(self):
        self.assertTrue(sel.rect_hits_line([9.4], [0.0], (0, 0, 10, 1)))       # 9 <= 0 + 10 - 1
        self.assertFalse(sel.rect_hits_line([9.5], [0.0], (0, 0, 10, 1)))      # rounds to 10
        self.assertTrue(sel.rect_hits_line([-0.5], [0.0], (-1, 0, 1, 1)))      # -0.5 -> -1

    def test_shift_toggles(self):
        self.assertEqual(selection.toggled([self.ids[0], self.ids[1]], [self.ids[1], self.ids[2]]),
                         [self.ids[0], self.ids[2]])

    def test_a_lasso_takes_only_strokes_wholly_inside(self):
        box = self.items[0].box()
        around = [(box[0] - 5, box[1] - 5), (box[2] + 5, box[1] - 5), (box[2] + 5, box[3] + 5), (box[0] - 5, box[3] + 5)]
        self.assertEqual(selection.pick_lasso(self.items, around), [self.ids[0]])
        # Cutting through the stroke takes nothing.
        half = [(box[0] - 5, box[1] - 5), ((box[0] + box[2]) / 2, box[1] - 5),
                ((box[0] + box[2]) / 2, box[3] + 5), (box[0] - 5, box[3] + 5)]
        self.assertEqual(selection.pick_lasso(self.items, half), [])
        self.assertEqual(selection.pick_lasso(self.items, around[:2]), [])

    def test_the_selection_box_follows_the_pending_transform(self):
        s = selection.Selection(0, [self.ids[0]], picked={p.item: p for p in self.items})
        before = s.box()
        s.apply(sel.translation(10, 20))
        after = s.box()
        self.assertAlmostEqual(after[0], before[0] + 10)
        self.assertAlmostEqual(after[1], before[1] + 20)
        self.assertTrue(s.has_stroke and not s.has_image)
        self.assertEqual(len(selection.line_rects(s.chosen(), s.pending)), 1)


class EditTests(unittest.TestCase):
    def test_edits_round_trip_through_json(self):
        import json
        from dataclasses import asdict
        t = ItemsTransform.new("d", "p", [("page", 1, 15), ("edit", "x/0")], sel.rotation(1, 2, 3), NOW,
                               [(0, 11, 4.0, 5.0)])
        p = StrokePaste.new("d", "p", [line_at(0, 0)], NOW)
        for edit, kind in ((t, "items-transform"), (p, "stroke-paste")):
            raw = json.loads(json.dumps({"kind": kind, **asdict(edit)}))
            self.assertEqual(KINDS[raw["kind"]].load(raw), edit)

    def test_replay_names_the_new_items_and_is_made_again_on_a_newer_page(self):
        page, ids = page_with(line_at(0, 100), line_at(300, 100))
        t = ItemsTransform.new("d", "p", [("page", *ids[0]), ("page", *ids[1])], sel.translation(5, 0), NOW)
        done = replay(page, [t], AUTHOR)
        self.assertEqual(set(done.made), {f"{t.uid}/0", f"{t.uid}/1"})
        # The tablet drew another stroke meanwhile: the same edit fits that page too.
        newer, _ = rs.append_strokes(page, author=w.author_uuid("tablet"), lines=[line_at(600, 0)])
        again = replay(newer, [t], AUTHOR)
        self.assertEqual(len(rs.strokes(again.page)), 3)
        moved = [s for s in rs.strokes(again.page) if s.line.move_id is not None]
        self.assertEqual({s.line.move_id for s in moved}, set(ids))

    def test_a_paste_and_a_move_of_it_replay_in_order(self):
        page, _ = page_with(line_at(0, 0))
        p = StrokePaste.new("d", "p", [line_at(500, 500)], NOW)
        t = ItemsTransform.new("d", "p", [("edit", f"{p.uid}/0")], sel.translation(0, 10), NOW)
        done = replay(page, [p, t], AUTHOR)
        [pasted_moved] = [s for s in rs.strokes(done.page) if s.line.move_id is not None]
        self.assertEqual(pasted_moved.line.move_id, done.made[f"{p.uid}/0"])   # the pasted item
        self.assertEqual(done.made[f"{t.uid}/0"], pasted_moved.item)


class LibrarySelectionTests(Base):
    """LibrarySource: commit, paste, delete, undo and redo as the app's actions do them."""

    def strokes(self):
        done = self.source.staged(self.source.sync.tree.get(THROWAWAY)).replay(PAGE1)
        return rs.strokes(done.page)

    def draw(self, x, y):
        self.source.add_stroke(self.doc, PAGE1, line_at(x, y), NOW)
        return self.strokes()[-1].item

    def test_move_then_undo_then_redo(self):
        tablet = (1, 15)
        mine = self.draw(200, 400)
        before = {s.item: s.line.unpacked() for s in self.strokes()}
        edit, moved = self.source.transform_items(self.doc, PAGE1, [tablet, mine], sel.translation(30, 40), NOW + 1)
        self.assertIsInstance(edit, ItemsTransform)
        self.assertEqual(set(moved), {tablet, mine})
        now = {s.item: s for s in self.strokes()}
        self.assertEqual(now[moved[tablet]].line.move_id, tablet)
        self.assertEqual(now[moved[mine]].line.unpacked()[0].x, f32(before[mine][0].x + 30))
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))
        # Undo: the moved ones go, the originals come back (new ids, moveId = themselves).
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 2))
        back = self.strokes()
        self.assertEqual(sorted(tuple(s.line.unpacked()) for s in back), sorted(tuple(p) for p in before.values()))
        self.assertEqual({s.line.move_id for s in back}, {tablet, mine})
        kinds = [type(e) for e in self.sync.edits.all()]
        self.assertEqual(kinds[-3:], [StrokeDelete, StrokeRestore, StrokeRestore])
        # Redo: moved again.
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 3))
        xs = sorted(s.line.unpacked()[0].x for s in self.strokes())
        self.assertEqual(xs, sorted(f32(p[0].x + 30) for p in before.values()))
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))

    def test_identity_commits_nothing(self):
        self.assertEqual(self.source.transform_items(self.doc, PAGE1, [(1, 15)], sel.IDENTITY, NOW), (None, {}))
        self.assertEqual(self.sync.edits.all(), [])

    def test_paste_then_move_is_one_undo_step(self):
        values = self.source.item_values(self.doc, PAGE1, [(1, 15)])
        clip = [sel.clone(values[(1, 15)])]
        placed = sel.pasted(clip, 1.0, (0.0, 1000.0))
        with self.source.edit_action(self.doc, PAGE1) as action:
            _, ids = self.source.paste_strokes(self.doc, PAGE1, placed, NOW, action)
        step = self.source.undo_top(self.doc, PAGE1)
        self.assertEqual(len(self.strokes()), 2)
        pasted = next(s for s in self.strokes() if s.item == ids[0])
        self.assertIsNone(pasted.line.move_id)                       # moveId 0:0 on a paste
        _, moved = self.source.transform_items(self.doc, PAGE1, ids, sel.translation(0, 50), NOW + 1, merge=step)
        moved_stroke = next(s for s in self.strokes() if s.item == moved[ids[0]])
        self.assertEqual(moved_stroke.line.move_id, ids[0])        # the pasted item, never the copied one
        # One undo takes the paste and its move away together.
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 2))
        self.assertEqual([s.item for s in self.strokes()], [(1, 15)])
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (False, True))
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 3))
        self.assertEqual(len(self.strokes()), 2)
        ys = sorted(s.line.unpacked()[0].y for s in self.strokes())
        self.assertIn(moved_stroke.line.unpacked()[0].y, ys)

    def test_delete_and_undo(self):
        mine = self.draw(200, 400)
        self.assertTrue(self.source.delete_items(self.doc, PAGE1, [mine, (1, 15)], NOW + 1))
        self.assertEqual(self.strokes(), [])
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 2))
        self.assertEqual(len(self.strokes()), 2)
        self.assertFalse(self.source.delete_items(self.doc, PAGE1, [(9, 9)], NOW + 3))

    def test_copy_paste_through_clipboard_records(self):
        values = self.source.item_values(self.doc, PAGE1, [(1, 15)])
        record = editing.clip_line(sel.clone(values[(1, 15)]))
        [line] = editing.clip_lines([record, {"type": "image"}, None])
        self.assertEqual(line, sel.clone(values[(1, 15)]))
        x, y, bw, bh = editing.items_bounds([record])
        placed = editing.placed_items([line], [], 1.0, (0.0, 900.0))
        self.assertEqual(placed[0][0], "line")
        box = sel.line_box(placed[0][1])
        self.assertAlmostEqual((box[1] + box[3]) / 2, 900.0, places=3)
        self.assertAlmostEqual(box[2] - box[0], bw, places=3)

    def test_pending_edits_survive_a_restart(self):
        self.source.transform_items(self.doc, PAGE1, [(1, 15)], sel.rotation(0, 0, 10), NOW)
        again = PendingEdits(self.sync.edits.root)
        self.assertEqual(again.all(), self.sync.edits.all())

    def test_the_push_plan_describes_them(self):
        from core.sync import describe_edit
        edit, _ = self.source.transform_items(self.doc, PAGE1, [(1, 15)], sel.translation(1, 2), NOW)
        self.assertIn("move 1 selected item", describe_edit(edit))
        with self.source.edit_action(self.doc, PAGE1) as action:
            paste, _ = self.source.paste_strokes(self.doc, PAGE1, [line_at(0, 0)], NOW, action)
        self.assertIn("paste 1 stroke", describe_edit(paste))
        self.assertTrue(self.sync.push_plan(THROWAWAY).strokes)


if __name__ == "__main__":
    unittest.main()
