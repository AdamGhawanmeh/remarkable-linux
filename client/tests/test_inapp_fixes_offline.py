"""Offline regression tests for four fixes found live-testing in-app editing (synthetic
ids only, no cloud call):

1. Deleting a layer (and undoing the add of a layer that holds items) deletes the
   layer's live strokes and images first, with the same tombstones as ClearPage /
   delete: a detached layer holds nothing live, as the tablet's files have it (the web
   app, which does not follow SceneTreeMoves, drew them). Undo brings the layer back
   where it was with every item in its stacking order; redo empties it again.
2. Layer eye clicks are optimistic and explicit (the state wanted, read from the state
   the click before gave), so quick clicks alternate instead of the second being a
   no-op; the Layers foldout's rows are updated in place (no model reset) across
   reloads and uploads.
3. The highlighter draws with Line::Color ArgbCode (9) and reMarkable.exe 3.29's palette
   ARGB in field 8, as the tablet does; the paintbrush (v2) widths are 3.29's.
4. Saving where a document was left never moves its .metadata lastOpened back behind a
   later opening on another device.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_inapp_fixes_offline.py'
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import QModelIndex  # noqa: E402

import test_edits_offline as te  # noqa: E402
import test_layers_offline as tl  # noqa: E402
from core import outbox  # noqa: E402
from core.edits import LayerVisibility  # noqa: E402
from core.sync import SyncClient, opened_lww  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from ui import pen as pens  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.quick.bridge import scene as scene_bridge  # noqa: E402
from viewer import renderer  # noqa: E402
from viewer import rm_layer as ly  # noqa: E402
from viewer import rm_select as sel  # noqa: E402
from viewer import rm_stroke as s  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import Color, parse  # noqa: E402

THROWAWAY, PAGE1, NOW = te.THROWAWAY, te.PAGE1, te.NOW
DESKTOP_AUTHOR = tl.DESKTOP
stroke_line = tl.stroke_line


def live_in(page: bytes, nodes) -> list[tuple[int, tuple[int, int], bytes]]:
    """(block type, id, value) of the live items of `nodes`, each node in its sequence
    (stacking) order."""
    _, blocks = w.split(page)
    out = []
    for node in nodes:
        seen = set()
        for item_id, block in w.sequence(blocks, tuple(node)):
            if block.type not in (w.BLOCK_LINE_ITEM, w.BLOCK_IMAGE_ITEM) or id(block) in seen:
                continue
            seen.add(id(block))
            item = w.decode_item(block.payload)
            if item.value is not None and not item.deleted_length:
                out.append((block.type, item.item, item.value))
    return out


# ------------------------------------------------------------------ 1. layer delete empties the layer

class LayerDeleteEmptiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = tl.layer_cloud(w.blank_page(tl.TABLET))
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway",))
        self.doc = self.source.refresh().get(THROWAWAY)

    def page(self) -> bytes:
        return self.source.staged(self.source.sync.tree.get(THROWAWAY)).page(PAGE1)

    def layer2(self):
        """Layer 1 with one stroke; Layer 2 (on top, current) with a stroke, an image and
        two more strokes, interleaved in its stacking order."""
        src, t = self.source, NOW
        src.add_stroke(self.doc, PAGE1, stroke_line(-100, 300, 100, 300, int(Color.BLUE)), t, layer_index=0)
        src.add_layer(self.doc, PAGE1, t + 1)
        src.add_stroke(self.doc, PAGE1, stroke_line(-50, 200, 50, 200, int(Color.RED)), t + 2, layer_index=1)
        src.insert_image(self.doc, PAGE1, te.png(), "png", te.box(), t + 3, layer_index=1)
        src.add_stroke(self.doc, PAGE1, stroke_line(-50, 400, 50, 400, int(Color.RED)), t + 4, layer_index=1)
        src.add_stroke(self.doc, PAGE1, stroke_line(-50, 500, 50, 500, int(Color.BLACK)), t + 5, layer_index=1)
        return ly.layers_info(self.page())[1].node_id

    def snapshot(self, node):
        """What the layers show: their names in order, and the items of layer `node`
        in stacking order (a stroke by its points, an image by its file). Items put back
        by undo are new items (with a moveId naming the deleted one), so not their bytes."""
        page = self.page()
        layer = next(layer for layer in parse(page).layers if layer.node_id == node)
        items = [("image", i.image_ref) if hasattr(i, "image_ref") else
                 ("line", int(i.color), tuple((round(p.x, 2), round(p.y, 2), round(p.width, 2)) for p in i.points))
                 for i in layer.items()]
        return [i.name for i in ly.layers_info(page)], items

    def assert_detached_and_empty(self, node):
        page = self.page()
        self.assertNotIn(node, [i.node_id for i in ly.layers_info(page)])
        self.assertEqual(live_in(page, [node]), [], "a detached layer holds nothing live")
        self.assertEqual(len(sel.page_items(page)), 1, "only Layer 1's stroke is live")

    def test_delete_tombstones_items_and_undo_redo_restore_them_in_order(self):
        node = self.layer2()
        before = self.snapshot(node)
        self.assertEqual([i[0] for i in before[1]], ["line", "image", "line", "line"])
        self.source.delete_layer(self.doc, PAGE1, 1, NOW + 10)
        self.assert_detached_and_empty(node)

        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 11))
        self.assertEqual(self.snapshot(node), before, "layer back at its index, items in stacking order")
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 12))
        self.assert_detached_and_empty(node)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 13))
        self.assertEqual(self.snapshot(node), before)
        sc = parse(self.page())
        self.assertEqual((len(sc.layers[1].strokes), len(sc.layers[1].images)), (3, 1))

    def test_deleting_a_middle_layer_restores_it_at_its_position(self):
        node = self.layer2()
        self.source.add_layer(self.doc, PAGE1, NOW + 6)            # Layer 3 on top
        before = self.snapshot(node)
        self.source.delete_layer(self.doc, PAGE1, 1, NOW + 7)
        self.assert_detached_and_empty(node)
        self.source.undo(self.doc, PAGE1, NOW + 8)
        self.assertEqual(self.snapshot(node), before)
        self.assertEqual([i.node_id for i in ly.layers_info(self.page())].index(node), 1)

    def test_undo_of_add_empties_a_layer_holding_items_and_redo_brings_them_back(self):
        # Items on the added layer that no undo step above the add accounts for (here:
        # the strokes' own steps taken off the stack): undoing the add deletes them too.
        node = self.layer2()
        history = self.source._history(THROWAWAY, PAGE1)
        add_step = next(n for n, step in enumerate(history.undo)
                        if any(type(c).__name__ == "LayerAttachChange" for c in step))
        del history.undo[add_step + 1:]
        before = self.snapshot(node)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 10))   # the add
        self.assert_detached_and_empty(node)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 11))
        self.assertEqual(self.snapshot(node), before)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 12))
        self.assert_detached_and_empty(node)

    def test_redo_of_a_delete_does_not_delete_twice(self):
        node = self.layer2()
        self.source.delete_layer(self.doc, PAGE1, 1, NOW + 10)
        self.source.undo(self.doc, PAGE1, NOW + 11)
        n = len(self.sync.edits.of(THROWAWAY))
        self.source.redo(self.doc, PAGE1, NOW + 12)
        made = self.sync.edits.of(THROWAWAY)[n:]
        # One StrokeDelete for the three strokes, one ImageDelete, the LayerDelete.
        self.assertEqual([type(e).__name__ for e in made], ["StrokeDelete", "ImageDelete", "LayerDelete"])
        self.assertEqual(len(made[0].items), 3)
        self.assert_detached_and_empty(node)

    def test_an_image_put_back_inside_a_run_of_deleted_ids_cuts_the_run(self):
        # Image deleted first, then the strokes on each side of it: the stroke deletes
        # join the image's deleted item into one run (can_join, 0x1408c3e60). Putting the
        # image back must cut that run, as putting a stroke back does.
        page = s.append_stroke(w.blank_page(tl.TABLET), author=tl.TABLET, line=stroke_line(0, 100, 50, 100),
                               layer=w.LAYER_1)
        page = w.append_image(page, author=tl.TABLET, key=tl.KEY_A, file_name=tl.NAME_A,
                              vertices=[tuple(v) for v in te.box()], layer=w.LAYER_1)
        page = s.append_stroke(page, author=tl.TABLET, line=stroke_line(0, 200, 50, 200), layer=w.LAYER_1)
        [s1, img, s2] = [(t, i) for t, i, _ in live_in(page, [w.LAYER_1])]
        image_value = w.image_item(page, tl.KEY_A)[1]
        page, _ = w.delete_image(page, author=tl.TABLET, key=tl.KEY_A, prune=False)
        page = s.delete_strokes(page, [s1[1], s2[1]])
        runs = [w.decode_item(b.payload).deleted_length for b in w.split(page)[1]
                if b.type in w.ITEM_BLOCKS and w.decode_item(b.payload).parent == w.LAYER_1]
        if max(runs) < 2:
            self.skipTest("the deletes did not join into a run")
        page = w.restore_image(page, author=DESKTOP_AUTHOR, item=img[1], value=image_value)
        s._Chain(w.split(page)[1], w.LAYER_1)            # the runs are in one piece again
        self.assertEqual([t for t, _, _ in live_in(page, [w.LAYER_1])], [w.BLOCK_IMAGE_ITEM])
        order = [i for i, _ in w.sequence(w.split(page)[1], w.LAYER_1)]
        new = live_in(page, [w.LAYER_1])[0][1]
        self.assertEqual(order.index(new), order.index(img[1]) + 1, "right after the deleted image")
        self.assertLess(order.index(new), order.index(s2[1]))

    def test_an_empty_layer_is_deleted_as_before(self):
        self.source.add_layer(self.doc, PAGE1, NOW)
        self.source.delete_layer(self.doc, PAGE1, 1, NOW + 1)
        self.assertEqual([type(e).__name__ for e in self.sync.edits.of(THROWAWAY)], ["LayerAdd", "LayerDelete"])


# ------------------------------------------------------------------ 2. eye clicks and the foldout's rows

class EyeClickTests(unittest.TestCase):
    """Through the app's own QML (LayersQmlTests' app and helpers)."""

    wait_for, open_doc, model_rows = tl.LayersQmlTests.wait_for, tl.LayersQmlTests.open_doc, tl.LayersQmlTests.model_rows
    qml, scene_views, close_document = tl.LayersQmlTests.qml, tl.LayersQmlTests.scene_views, tl.LayersQmlTests.close_document
    pending, document_view = tl.LayersQmlTests.pending, tl.LayersQmlTests.document_view
    THROWAWAY, PAGE1 = THROWAWAY, PAGE1
    setUpClass = classmethod(tl.LayersQmlTests.setUpClass.__func__)
    tearDownClass = classmethod(tl.LayersQmlTests.tearDownClass.__func__)
    setUp = tl.LayersQmlTests.setUp

    def two_layers(self):
        view = self.open_doc(THROWAWAY)
        ctrl = view._controller
        model = scene_bridge.SceneLayersModel()
        model.setProperty("controller", ctrl)
        self.addCleanup(model.deleteLater)
        ctrl.addLayer()
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 2 and ctrl.property("currentLayer") == 1)
        self.wait_for(lambda: view._pool.activeThreadCount() == 0)
        tq.wait(50)
        signals = {"reset": 0, "changed": 0, "inserted": 0, "removed": 0}
        model.modelReset.connect(lambda: signals.__setitem__("reset", signals["reset"] + 1))
        model.dataChanged.connect(lambda *_: signals.__setitem__("changed", signals["changed"] + 1))
        model.rowsInserted.connect(lambda *_: signals.__setitem__("inserted", signals["inserted"] + 1))
        model.rowsRemoved.connect(lambda *_: signals.__setitem__("removed", signals["removed"] + 1))
        return view, ctrl, model, signals

    def visibility_edits(self):
        return [e.visible for e in self.source.sync.edits.of(THROWAWAY) if isinstance(e, LayerVisibility)]

    def click_eye(self, ctrl, index):
        """What LayersFoldout's eye does: setLayerVisible(index, !isLayerVisible(index))."""
        ctrl.setLayerVisible(index, not ctrl.isLayerVisible(index))

    def test_quick_clicks_alternate_and_are_all_kept(self):
        view, ctrl, model, signals = self.two_layers()
        seen = []
        for _ in range(5):                       # no wait in between: none is kept yet
            self.click_eye(ctrl, 1)
            seen.append(ctrl.isLayerVisible(1))
            self.assertEqual(self.model_rows(model)[1]["layerVisible"], seen[-1], "the eye shows it at once")
        self.assertEqual(seen, [False, True, False, True, False])
        self.wait_for(lambda: len(self.visibility_edits()) == 5)
        self.wait_for(lambda: view._pool.activeThreadCount() == 0 and not view._visible_clicks)
        tq.wait(100)
        self.assertEqual(self.visibility_edits(), [False, True, False, True, False])
        self.assertFalse(ctrl.isLayerVisible(1))
        self.assertFalse(view._content.scene.layers[1].visible)
        self.assertEqual(signals["reset"], 0, "rows changed in place, never rebuilt")
        self.assertEqual((signals["inserted"], signals["removed"]), (0, 0))
        self.assertGreater(signals["changed"], 0)

    def test_a_reload_loaded_before_a_click_was_kept_does_not_undo_it(self):
        view, ctrl, model, signals = self.two_layers()
        stale = view._loads_queued               # a load queued before the click ...
        self.click_eye(ctrl, 1)
        self.assertFalse(ctrl.isLayerVisible(1))
        visible_page = view._content
        from dataclasses import replace
        old = replace(visible_page, scene=replace(visible_page.scene, layers=[
            replace(layer, visible=True) for layer in visible_page.scene.layers]))
        # ... arriving after it: still shows the click.
        self.assertFalse(view._with_visible_clicks(old, stale).scene.layers[1].visible)
        self.wait_for(lambda: self.visibility_edits() == [False])
        self.wait_for(lambda: view._pool.activeThreadCount() == 0 and not view._visible_clicks)
        self.assertFalse(ctrl.isLayerVisible(1))

    def test_rows_are_updated_in_place_across_reloads_and_uploads(self):
        view, ctrl, model, signals = self.two_layers()
        rows = self.model_rows(model)
        view._load(quiet=True)                   # a reload: same rows
        self.wait_for(lambda: view._pool.activeThreadCount() == 0)
        tq.wait(100)
        self.assertEqual(self.model_rows(model), rows)
        self.assertEqual(signals, {"reset": 0, "changed": 0, "inserted": 0, "removed": 0})
        # An upload (to the fake cloud) and the reload after it, as the app does it
        # (OutgoingSync.uploaded -> QuickApp.set_tree): still in place.
        outbox.push_document(self.source.sync, THROWAWAY, settle=self.source.settle_upload)
        self.assertEqual(self.source.sync.edits.of(THROWAWAY), [])
        self.app.set_tree(self.source.sync.tree)
        tq.wait(300)
        self.wait_for(lambda: view._pool.activeThreadCount() == 0)
        tq.wait(100)
        self.assertEqual([r["layerName"] for r in self.model_rows(model)], ["Layer 1", "Layer 2"])
        self.assertEqual((signals["reset"], signals["inserted"], signals["removed"]), (0, 0, 0))
        # Selecting another layer changes the two rows' layerSelected, in place.
        ctrl.setCurrentLayer(0)
        self.wait_for(lambda: self.model_rows(model)[0]["layerSelected"])
        self.assertEqual(signals["reset"], 0)
        # Deleting a layer removes its row; adding one inserts a row.
        ctrl.deleteLayer(1)
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 1)
        ctrl.addLayer()
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 2)
        self.assertEqual((signals["reset"], signals["inserted"], signals["removed"]), (0, 1, 1))
        self.assertEqual(model.property("count"), 2)


# ------------------------------------------------------------------ 3. highlighter and paintbrush encoding

class StrokeEncodingTests(unittest.TestCase):
    # reMarkable.exe 3.29's palette (0x1409d25a0) for the highlighter's colours.
    EXPECTED = {"yellow": 0xFFFFED75, "green": 0xFFACFF85, "pink": 0xFFF29EFF, "blue": 0xFFBEEAFE,
                "orange": 0xFFFFC38C, "grey": 0xFFC7C7C6}

    def test_every_highlighter_colour_is_argb_code_with_the_palette_colour(self):
        tool = pens.BY_KEY["highlighter"]
        self.assertEqual({c.key for c in tool.colors}, set(self.EXPECTED))
        for key, argb in self.EXPECTED.items():
            values = pens.color_values(tool, key)
            self.assertEqual(values, {"LastHighlighterv2Color": "ArgbCode", "LastHighlighterv2ColorCode": str(argb)})
            pen = pens.pen_of(values, tool)
            self.assertEqual((pen.color.key, pen.line_color, pen.argb), (key, 9, argb))
            # As the renderer's palette has it.
            color = next(c for c in tool.colors if c.key == key).color
            self.assertEqual(renderer.PALETTE_ARGB[color], argb)
            # A colour kept by name (older documents) is drawn the same way.
            named = pens.pen_of({"LastHighlighterv2Color": color.name.title().replace("_", "")}, tool)
            self.assertEqual((named.line_color, named.argb), (9, argb))
        # The tablet's own settings (the cached document's): yellow.
        tablet = pens.pen_of({"LastHighlighterv2Color": "ArgbCode", "LastHighlighterv2ColorCode": "4294962549"}, tool)
        self.assertEqual((tablet.color.key, tablet.line_color, tablet.argb), ("yellow", 9, 0xFFFFED75))
        # Other pens keep their named colours.
        self.assertEqual(pens.color_values(pens.BY_KEY["fineliner"], "blue"), {"LastFinelinerv2Color": "Blue"})
        self.assertIsNone(pens.pen_of({}, pens.BY_KEY["paintbrush"]).argb)

    def test_highlighter_line_bytes_are_the_tablets(self):
        pen = pens.pen_of(pens.color_values(pens.BY_KEY["highlighter"], "pink"), pens.BY_KEY["highlighter"])
        line = s.draw([(0.0, 0.0), (40.0, 0.0), (80.0, 0.0)], pen.tool.tool, pen.line_color, float(pen.size), pen.argb)
        expected = (b"\x03" + w.f_u32(1, s.HIGHLIGHTER_V2) + w.f_u32(2, 9) + w.f_f64(3, 1.0)
                    + w.f_f32(4, line.starting_length) + w.f_sub(5, line.points) + w.f_id(6, line.timestamp)
                    + w.f_u32(8, 0xFFF29EFF))
        self.assertEqual(line.encode(), expected)
        # Fields 1-6 and 8, as each highlighter line the tablet wrote.
        self.assertEqual([i for i, _, _ in w.read_values(line.encode()[1:])], [1, 2, 3, 4, 5, 6, 8])
        self.assertEqual(s.decode_line_value(line.encode()), line)

    def test_highlighter_drawn_in_the_app_is_kept_as_argb_code(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        cloud = tl.layer_cloud(w.blank_page(tl.TABLET))
        source = LibrarySource(SyncClient(paired_auth(d / "auth", cloud), d / "cache"), editable=("throwaway",))
        doc = source.refresh().get(THROWAWAY)
        pen = pens.pen_of(pens.color_values(pens.BY_KEY["highlighter"], "green"), pens.BY_KEY["highlighter"])
        source.add_stroke(doc, PAGE1, s.draw([(0.0, 300.0), (90.0, 300.0)], pen.tool.tool, pen.line_color,
                                             float(pen.size), pen.argb), NOW)
        [item] = sel.page_items(source.staged(source.sync.tree.get(THROWAWAY)).page(PAGE1))
        self.assertEqual((item.line.tool, item.line.color, item.line.argb), (s.HIGHLIGHTER_V2, 9, 0xFFACFF85))
        stroke = parse(source.staged(source.sync.tree.get(THROWAWAY)).page(PAGE1)).strokes[0]
        style = renderer.stroke_style(stroke)
        self.assertEqual(style.color.lower(), "#acff85")

    def test_paintbrush_v2_widths_are_3_29s(self):
        # 0x140b40df0, tool 12 shares tool 0's branch with the v2 constants: pressure at
        # least 0.25, base max(2, p^4.5 * 4.5 + tilt^4 * 4.5), width max(base * size, 2).
        samples = [(x * 10.0, 0.0, p) for x, p in enumerate((0.2, 0.5, 0.8, 1.0, 0.9, 0.6, 0.3))]
        for size in (1.0, 2.0, 3.0):
            line = s.draw(samples, s.PAINTBRUSH_V2, int(Color.BLACK), size)
            self.assertEqual((line.color, line.argb), (0, None))
            self.assertEqual([i for i, _, _ in w.read_values(line.encode()[1:])], [1, 2, 3, 4, 5, 6])
            for point in line.unpacked():
                p = point.pressure / 255.0
                base = max(2.0, max(0.25, p) ** 4.5 * 4.5)
                self.assertLessEqual(abs(round(max(base * size, 2.0) * 4) - point.width), 1)


# ------------------------------------------------------------------ 4. lastOpened never goes back

class LastOpenedTests(unittest.TestCase):
    LATER = te.NOW + 3_600_000           # another device opened the document an hour later

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.cloud = FakeCloud()
        self.cloud.put_item(THROWAWAY, te.meta("throwaway", lastOpened=str(self.LATER), lastOpenedPage=0),
                            te.notebook_content(PAGE1))
        self.sync = SyncClient(paired_auth(d / "auth", self.cloud), d / "cache")
        self.sync.refresh()

    def test_an_older_opening_keeps_the_later_time_and_saves_the_page(self):
        self.sync.set_reading_position(THROWAWAY, 3, opened_ms=te.NOW)
        meta = self.cloud.metadata(THROWAWAY)
        self.assertEqual((meta["lastOpened"], meta["lastOpenedPage"]), (str(self.LATER), 3))

    def test_a_newer_opening_moves_it_forward(self):
        self.sync.set_reading_position(THROWAWAY, 1, opened_ms=self.LATER + 5)
        self.assertEqual(self.cloud.metadata(THROWAWAY)["lastOpened"], str(self.LATER + 5))

    def test_the_cloud_as_the_commit_finds_it_counts(self):
        # Another device opens the document after this client last synced: the commit's
        # refresh sees it, and the save keeps it.
        meta = te.meta("throwaway", lastOpened=str(self.LATER + 99), lastOpenedPage=4)
        self.cloud.put_item(THROWAWAY, meta, te.notebook_content(PAGE1))
        self.cloud.publish()
        self.sync.set_reading_position(THROWAWAY, 2, opened_ms=self.LATER + 1)
        self.assertEqual(self.cloud.metadata(THROWAWAY)["lastOpened"], str(self.LATER + 99))

    def test_nothing_is_sent_when_only_an_older_time_differs(self):
        puts = len(self.cloud.puts())
        self.sync.set_reading_position(THROWAWAY, 0, opened_ms=te.NOW)
        self.assertEqual(len(self.cloud.puts()), puts)

    def test_opened_lww(self):
        self.assertEqual(opened_lww("200", 100), "200")
        self.assertEqual(opened_lww("100", 200), "200")
        self.assertEqual(opened_lww(None, 5), "5")
        self.assertEqual(opened_lww("garbage", 5), "5")


if __name__ == "__main__":
    unittest.main()
