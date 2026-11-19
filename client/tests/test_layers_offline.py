"""Offline unit and QML integration tests for Layers UI and persistent SceneInfo flags
(synthetic ids only):

- viewer.rm_layer: addLayer (up to max 5, default naming), deleteLayer (detach via
  SceneTreeMove, active-layer fallback, refusing last layer), reattach, rename, visibility,
  reorder (CRDT sequence order + rendered z-order), mergeLayerDown (strokes, images,
  text-anchored groups), unmerge, clearLinesInLayer, SceneInfo (currentLayer,
  backgroundVisible, rootDocumentVisible).
- viewer.rm_parser & renderer: CRDT stacking order of reordered layers, skipping detached
  layers/groups, skipping hidden layers and hidden typed text, pixel-level z-order and
  visibility checks.
- core.edits & ui.library: layer-targeted drawing, hold-to-straighten, erasing, lasso/rect
  selection, move/scale/rotate, copy/cut/paste, image insertion on Layer 2+, PendingEdits
  persistence across restarts, and full undo/redo round-trips for every layer mutation.
- QML integration: LayersFoldout, SceneLayersModel dynamic role flags, SceneController
  layer slots, hideTemplateAction persistence on editable pages vs live-only on read-only
  pages, and drawing/erasing/selecting/pasting on Layer 2+ through the app's DocumentView.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_layers_offline.py'
"""

from __future__ import annotations

import dataclasses
import gc
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

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

import test_bundle2_offline as tb2  # noqa: E402
import test_edits_offline as te  # noqa: E402
from core.edits import (EditError, LayerAdd, LayerDelete, LayerMergeDown, LayerMove, LayerRename, LayerVisibility,
                        PendingEdits, SceneFlags, document_files, replay)  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import FakeCloud, paired_auth  # noqa: E402
from ui import editing  # noqa: E402
from ui.library import LibrarySource, document_pages  # noqa: E402
from ui.pages import PageContent, render_content  # noqa: E402
from ui.quick.bridge import scene as scene_bridge  # noqa: E402
from viewer import renderer  # noqa: E402
from viewer import rm_layer as ly  # noqa: E402
from viewer import rm_select as sel  # noqa: E402
from viewer import rm_stroke as s  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import Color, RmParseError, parse  # noqa: E402
from viewer.templates import default_library  # noqa: E402

TABLET = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
DESKTOP = bytes.fromhex("a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
KEY_A = bytes(range(0x20, 0x30))
KEY_B = bytes(range(0x30, 0x40))
NAME_A = "11111111-1111-4111-8111-111111111111.png"
NAME_B = "22222222-2222-4222-8222-222222222222.png"
THROWAWAY, OTHER, PAGE1, PAGE2 = te.THROWAWAY, te.OTHER, te.PAGE1, te.PAGE2
NOW = te.NOW
TEMPLATE = "P Lines medium"


def stroke_line(x0: float, y0: float, x1: float, y1: float, color: int = int(Color.BLACK),
                tool: int = s.MARKER_V2, thickness: float = s.THICK) -> s.LineValue:
    pts = [(x0 + (x1 - x0) * k / 10, y0 + (y1 - y0) * k / 10) for k in range(11)]
    return s.draw(pts, tool, color, thickness)


def pixel_rgb(scene, x_scene: float, y_scene: float) -> tuple[int, int, int]:
    image = renderer.render_image(scene, crop="clip")
    c = QColor(image.pixel(round(x_scene + scene.x_origin), round(y_scene)))
    return c.red(), c.green(), c.blue()


def two_layer_page() -> bytes:
    """Layer 1 with a thick blue horizontal stroke, Layer 2 with a thick red vertical one
    crossing it at (0, 500): the crossing shows the top layer's colour."""
    page = s.append_stroke(w.blank_page(TABLET), author=TABLET,
                           line=stroke_line(-150.0, 500.0, 150.0, 500.0, color=int(Color.BLUE)), layer=w.LAYER_1)
    page, l2 = ly.add_layer(page, author=TABLET)
    return s.append_stroke(page, author=TABLET, line=stroke_line(0.0, 350.0, 0.0, 650.0, color=int(Color.RED)),
                           layer=l2)


def layer_cloud(page_bytes: bytes | None = None, template: str = TEMPLATE) -> FakeCloud:
    content = te.notebook_content(PAGE1, PAGE2)
    for entry in content["cPages"]["pages"]:
        entry["template"]["value"] = template
    cloud = FakeCloud()
    files = {f"{THROWAWAY}/{PAGE1}.rm": page_bytes} if page_bytes is not None else {}
    cloud.put_item(THROWAWAY, te.meta("throwaway"), content, files)
    cloud.put_item(OTHER, te.meta("ReadOnly"), content, {f"{OTHER}/{PAGE1}.rm": two_layer_page()})
    return cloud


# ------------------------------------------------------------------ 1. rm_layer CRDT engine

class RmLayerTests(unittest.TestCase):
    def test_add_up_to_five_layers_and_default_naming(self):
        page = None
        nodes = []
        for expected in ("Layer 2", "Layer 3", "Layer 4", "Layer 5"):
            page, node = ly.add_layer(page, author=DESKTOP)
            nodes.append(node)
            scene = parse(page)
            self.assertEqual(scene.layers[-1].name, expected)
            self.assertEqual(scene.current_layer, node)
        self.assertEqual(len(parse(page).layers), ly.MAX_LAYERS)
        with self.assertRaises(ly.LayerError):
            ly.add_layer(page, author=DESKTOP)
        # Deleting Layer 2 and adding a new layer skips existing "Layer 5" if needed:
        # 4 layers left ("Layer 1", "Layer 3", "Layer 4", "Layer 5") -> starts at 5 (taken) -> "Layer 6".
        page, _ = ly.detach_layer(page, author=DESKTOP, layer=nodes[0])
        self.assertEqual([l.name for l in parse(page).layers], ["Layer 1", "Layer 3", "Layer 4", "Layer 5"])
        page, node6 = ly.add_layer(page, author=DESKTOP)
        self.assertEqual([l.name for l in parse(page).layers], ["Layer 1", "Layer 3", "Layer 4", "Layer 5", "Layer 6"])
        self.assertEqual(parse(page).current_layer, node6)

    def test_rename_and_visibility_pixels(self):
        page, l2 = ly.add_layer(w.blank_page(TABLET), author=DESKTOP)
        red = stroke_line(-100.0, 400.0, 100.0, 400.0, color=int(Color.RED))
        page = s.append_stroke(page, author=DESKTOP, line=red, layer=l2)
        page = ly.set_layer_name(page, author=DESKTOP, layer=l2, name="Highlights")
        self.assertEqual([l.name for l in parse(page).layers], ["Layer 1", "Highlights"])

        # Visible: red pixels at (0, 400)
        r, g, b = pixel_rgb(parse(page), 0.0, 400.0)
        self.assertGreater(r, 150)
        self.assertLess(g, 100)

        # Hidden: white paper at (0, 400)
        page = ly.set_layer_visible(page, author=DESKTOP, layer=l2, visible=False)
        self.assertFalse(parse(page).layers[1].visible)
        self.assertEqual(pixel_rgb(parse(page), 0.0, 400.0), (255, 255, 255))

        # Shown again: red pixels return
        page = ly.set_layer_visible(page, author=DESKTOP, layer=l2, visible=True)
        r, g, b = pixel_rgb(parse(page), 0.0, 400.0)
        self.assertGreater(r, 150)
        self.assertLess(g, 100)

    def test_reorder_z_order_in_rendered_pixels(self):
        # Blue thick stroke on Layer 1, Red thick stroke on Layer 2 crossing at (0, 500).
        page = s.append_stroke(w.blank_page(TABLET), author=DESKTOP,
                               line=stroke_line(-120.0, 500.0, 120.0, 500.0, color=int(Color.BLUE)), layer=w.LAYER_1)
        page, l2 = ly.add_layer(page, author=DESKTOP)
        page = s.append_stroke(page, author=DESKTOP,
                               line=stroke_line(0.0, 380.0, 0.0, 620.0, color=int(Color.RED)), layer=l2)
        # Layer 2 is above Layer 1: intersection (0, 500) is RED.
        r, g, b = pixel_rgb(parse(page), 0.0, 500.0)
        self.assertGreater(r, b + 50)

        # Move Layer 2 down to index 0 (below Layer 1): intersection (0, 500) becomes BLUE.
        page = ly.move_layer(page, author=DESKTOP, layer=l2, new_index=0)
        scene = parse(page)
        self.assertEqual([l.node_id for l in scene.layers], [l2, w.LAYER_1])
        self.assertEqual(scene.current_layer, l2)
        r, g, b = pixel_rgb(scene, 0.0, 500.0)
        self.assertGreater(b, r + 50)

        # Move Layer 2 back up to index 1: RED again.
        page = ly.move_layer(page, author=DESKTOP, layer=l2, new_index=1)
        scene = parse(page)
        self.assertEqual([l.node_id for l in scene.layers], [w.LAYER_1, l2])
        r, g, b = pixel_rgb(scene, 0.0, 500.0)
        self.assertGreater(r, b + 50)

    def test_merge_down_transfers_strokes_images_and_anchored_groups_and_unmerges(self):
        # Start from a page with typed text + an anchored group on Layer 1, then move that
        # group or create a 2nd layer with a stroke, an image, AND a nested child group!
        page = w.blank_page(TABLET)
        page, l2 = ly.add_layer(page, author=DESKTOP)
        red = stroke_line(-100.0, 300.0, 100.0, 300.0, color=int(Color.RED))
        page = s.append_stroke(page, author=DESKTOP, line=red, layer=l2)
        corners = editing.corners(0.0, 700.0, 120.0, 120.0)
        page = w.append_image(page, author=DESKTOP, key=KEY_A, file_name=NAME_A, vertices=corners, layer=l2)
        # Add a nested group inside Layer 2 holding a blue stroke.
        header, blocks = w.split(page)
        clock = w.highest_counter(blocks)
        grp = (2, clock + 1)
        grp_item = (2, clock + 2)
        stroke_in_grp = (2, clock + 3)
        blue = stroke_line(-80.0, 900.0, 80.0, 900.0, color=int(Color.BLUE))
        order_l2 = w.sequence(blocks, l2)
        ly._insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move(grp, w.ZERO_ID, 1, l2)))
        ly._insert_tree_node(blocks, w.new_block(
            w.BLOCK_TREE_NODE, w.f_id(1, grp) + w.f_sub(2, w.lww(w.ZERO_ID, w.string_value("")))
            + w.f_sub(3, w.f_id(1, w.ZERO_ID) + w.f_u8(2, 1))))
        blocks = w.insert_items(blocks, [
            w.new_block(w.BLOCK_GROUP_ITEM, w.ItemHeader(l2, grp_item, order_l2[-1][0], w.ZERO_ID, 0,
                                                         b"\x02" + w.f_id(2, grp)).encode()),
            w.new_block(w.BLOCK_LINE_ITEM, w.ItemHeader(grp, stroke_in_grp, w.ZERO_ID, w.ZERO_ID, 0,
                                                        blue.encode()).encode()),
        ])
        page = w.join(header, blocks)
        before = parse(page)
        self.assertEqual((len(before.layers), len(before.layers[1].strokes), len(before.layers[1].images)), (2, 2, 1))

        # Merge Layer 2 down into Layer 1.
        merged = ly.merge_layer_down(page, author=DESKTOP, layer=l2)
        after = parse(merged.page)
        self.assertEqual([l.node_id for l in after.layers], [w.LAYER_1])
        self.assertEqual(after.current_layer, w.LAYER_1)
        self.assertEqual(len(after.layers[0].strokes), 2)
        self.assertEqual(len(after.layers[0].images), 1)
        self.assertEqual(after.layers[0].images[0].image_ref, KEY_A)
        self.assertEqual(merged.moved_groups, (grp,))
        # Both red and blue strokes render on Layer 1.
        self.assertGreater(pixel_rgb(after, 0.0, 300.0)[0], 150)
        self.assertGreater(pixel_rgb(after, 0.0, 900.0)[2], 150)

        # Unmerge restores Layer 2, its strokes, image, and child group.
        restore_items = tuple((old_id, kind, raw, None) for old_id, kind, raw, _ in merged.transferred)
        remove_dst = tuple(new_id for _, _, _, new_id in merged.transferred)
        undone, restored_map = ly.unmerge_layer(
            merged.page, author=DESKTOP, src=l2, dst=w.LAYER_1,
            moved_groups=merged.moved_groups, restore_items=restore_items,
            remove_from_dst=remove_dst, old_current=merged.old_current)
        back = parse(undone)
        self.assertEqual([l.node_id for l in back.layers], [w.LAYER_1, l2])
        self.assertEqual(back.current_layer, l2)
        self.assertEqual((len(back.layers[0].strokes), len(back.layers[0].images)), (0, 0))
        self.assertEqual((len(back.layers[1].strokes), len(back.layers[1].images)), (2, 1))
        self.assertEqual(len(restored_map), 2)

    def test_merge_down_keeps_groups_between_strokes_in_z_order(self):
        # Layer 2 holds stroke A, then a child group G (a text-anchored group, say), then
        # stroke B. The app walks src in sequence order (0x14095d840), so dst must get
        # A', G, B' in that order: G stays above A and below B.
        page, l2 = ly.add_layer(w.blank_page(TABLET), author=DESKTOP)
        page = s.append_stroke(page, author=DESKTOP, line=stroke_line(-90, 300, 90, 300), layer=l2)
        header, blocks = w.split(page)
        clock = w.highest_counter(blocks)
        grp, grp_item = (2, clock + 1), (2, clock + 2)
        ly._insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move(grp, w.ZERO_ID, 1, l2)))
        ly._insert_tree_node(blocks, w.new_block(
            w.BLOCK_TREE_NODE, w.f_id(1, grp) + w.f_sub(2, w.lww(w.ZERO_ID, w.string_value("")))
            + w.f_sub(3, w.f_id(1, w.ZERO_ID) + w.f_u8(2, 1))))
        node = sel._Node(blocks, l2)
        node.append(grp_item, b"\x02" + w.f_id(2, grp), (w.BLOCK_GROUP_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_GROUP_ITEM]))
        page = w.join(header, node.write(blocks))
        page = s.append_stroke(page, author=DESKTOP, line=stroke_line(-90, 700, 90, 700), layer=l2)

        merged = ly.merge_layer_down(page, author=DESKTOP, layer=l2)
        blocks = w.split(merged.page)[1]
        kinds = []
        for _, block in w.sequence(blocks, w.LAYER_1):
            item = w.decode_item(block.payload)
            if item.value is not None and not item.deleted_length:
                kinds.append("group" if block.type == w.BLOCK_GROUP_ITEM else "line")
        self.assertEqual(kinds, ["line", "group", "line"])
        self.assertEqual(merged.moved_groups, (grp,))
        self.assertEqual(w.tree_parents(blocks)[grp], w.LAYER_1)
        ys = [st.points[0].y for st in parse(merged.page).layers[0].strokes]
        self.assertEqual([round(y) for y in ys], [300, 700])

    def test_delete_layer_and_reattach_and_refuse_last(self):
        page = w.blank_page(TABLET)
        with self.assertRaises(ly.LayerError):
            ly.detach_layer(page, author=DESKTOP, layer=w.LAYER_1)
        page, l2 = ly.add_layer(page, author=DESKTOP)
        page = s.append_stroke(page, author=DESKTOP, line=stroke_line(-50, 200, 50, 200), layer=l2)
        page, l3 = ly.add_layer(page, author=DESKTOP)
        # Deleting middle layer l2 while l3 is active keeps l3 active.
        page, active = ly.detach_layer(page, author=DESKTOP, layer=l2)
        self.assertEqual(active, l3)
        self.assertEqual([l.node_id for l in parse(page).layers], [w.LAYER_1, l3])
        self.assertEqual(len(parse(page).strokes), 0)  # detached layer's strokes are hidden
        # Reattaching l2 brings its stroke back!
        page = ly.reattach_layer(page, author=DESKTOP, layer=l2, set_current=l2)
        scene = parse(page)
        self.assertEqual([l.node_id for l in scene.layers], [w.LAYER_1, l2, l3])
        self.assertEqual(len(scene.layers[1].strokes), 1)
        self.assertEqual(scene.current_layer, l2)
        # Deleting active top layer l3 falls back to l2.
        page = ly.set_scene_info(page, author=DESKTOP, current_layer=l3)
        page, active = ly.detach_layer(page, author=DESKTOP, layer=l3)
        self.assertEqual(active, l2)
        # Deleting active bottom layer Layer 1 falls back to remaining index 0 (l2).
        page = ly.set_scene_info(page, author=DESKTOP, current_layer=w.LAYER_1)
        page, active = ly.detach_layer(page, author=DESKTOP, layer=w.LAYER_1)
        self.assertEqual(active, l2)
        self.assertEqual([l.node_id for l in parse(page).layers], [l2])

    def test_scene_info_flags_preserve_other_blocks(self):
        page = tb2.text_page()
        page2 = ly.set_scene_info(page, author=DESKTOP, background_visible=False, root_document_visible=False)
        sc = parse(page2)
        self.assertFalse(sc.background_visible)
        self.assertFalse(sc.root_document_visible)
        # RootText is still parsed, but renderer skips drawing text when root_document_visible is False.
        self.assertIsNotNone(sc.text)
        svg_hidden = renderer.render_svg(sc)
        self.assertNotIn('id="text"', svg_hidden)
        page3 = ly.set_scene_info(page2, author=DESKTOP, root_document_visible=True)
        self.assertIn('id="text"', renderer.render_svg(parse(page3)))


# ------------------------------------------------------------------ 2. LibrarySource + PendingEdits + Undo/Redo

class LayerEditsLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = layer_cloud(w.blank_page(TABLET))
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=("throwaway",))
        self.doc = self.source.refresh().get(THROWAWAY)

    def page(self, page_id: str = PAGE1) -> bytes | None:
        return self.source.staged(self.source.sync.tree.get(THROWAWAY)).page(page_id)

    def scene(self, page_id: str = PAGE1):
        return parse(self.page(page_id))

    def test_every_layer_operation_round_trips_undo_redo_and_pending_persistence(self):
        # 1. Add Layer 2 -> undo -> redo
        self.source.add_layer(self.doc, PAGE1, NOW)
        self.assertEqual([l.name for l in self.scene().layers], ["Layer 1", "Layer 2"])
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 1))
        self.assertEqual([l.name for l in self.scene().layers], ["Layer 1"])
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 2))
        self.assertEqual([l.name for l in self.scene().layers], ["Layer 1", "Layer 2"])

        # 2. Rename Layer 2 -> undo -> redo
        self.source.rename_layer(self.doc, PAGE1, 1, "Ink", NOW + 3)
        self.assertEqual(self.scene().layers[1].name, "Ink")
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 4))
        self.assertEqual(self.scene().layers[1].name, "Layer 2")
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 5))
        self.assertEqual(self.scene().layers[1].name, "Ink")

        # 3. Visibility -> undo -> redo
        self.source.set_layer_visible(self.doc, PAGE1, 1, False, NOW + 6)
        self.assertFalse(self.scene().layers[1].visible)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 7))
        self.assertTrue(self.scene().layers[1].visible)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 8))
        self.assertFalse(self.scene().layers[1].visible)
        self.source.set_layer_visible(self.doc, PAGE1, 1, True, NOW + 9)

        # 4. Move Layer 2 down to 0 -> undo -> redo
        self.source.move_layer(self.doc, PAGE1, 1, 0, NOW + 10)
        self.assertEqual([l.name for l in self.scene().layers], ["Ink", "Layer 1"])
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 11))
        self.assertEqual([l.name for l in self.scene().layers], ["Layer 1", "Ink"])
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 12))
        self.assertEqual([l.name for l in self.scene().layers], ["Ink", "Layer 1"])
        # Move back to top so we can draw & merge down
        self.source.move_layer(self.doc, PAGE1, 0, 1, NOW + 13)

        # 5. Draw stroke + insert image on Layer 2, then merge down -> undo -> redo -> undo
        self.source.add_stroke(self.doc, PAGE1, stroke_line(-50, 200, 50, 200, color=int(Color.RED)),
                               NOW + 14, layer_index=1)
        self.source.insert_image(self.doc, PAGE1, te.png(), "png", te.box(), NOW + 15, layer_index=1)
        self.assertEqual((len(self.scene().layers[0].strokes), len(self.scene().layers[1].strokes),
                          len(self.scene().layers[1].images)), (0, 1, 1))
        self.source.merge_layer_down(self.doc, PAGE1, 1, NOW + 16)
        sc = self.scene()
        self.assertEqual((len(sc.layers), len(sc.layers[0].strokes), len(sc.layers[0].images)), (1, 1, 1))
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 17))
        sc = self.scene()
        self.assertEqual((len(sc.layers), len(sc.layers[0].strokes), len(sc.layers[1].strokes),
                          len(sc.layers[1].images)), (2, 0, 1, 1))
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 18))
        sc = self.scene()
        self.assertEqual((len(sc.layers), len(sc.layers[0].strokes), len(sc.layers[0].images)), (1, 1, 1))
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 19))
        self.assertEqual(len(self.scene().layers), 2)

        # 6. Delete Layer 2 -> undo -> redo -> undo
        self.source.delete_layer(self.doc, PAGE1, 1, NOW + 20)
        self.assertEqual(len(self.scene().layers), 1)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 21))
        self.assertEqual((len(self.scene().layers), len(self.scene().layers[1].strokes),
                          len(self.scene().layers[1].images)), (2, 1, 1))
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 22))
        self.assertEqual(len(self.scene().layers), 1)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 23))

        # 7. backgroundVisible & rootDocumentVisible -> undo -> redo
        self.source.set_background_visible(self.doc, PAGE1, False, NOW + 24)
        self.assertFalse(self.scene().background_visible)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 25))
        self.assertTrue(self.scene().background_visible)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 26))
        self.assertFalse(self.scene().background_visible)

        self.source.set_root_document_visible(self.doc, PAGE1, False, NOW + 27)
        self.assertFalse(self.scene().root_document_visible)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 28))
        self.assertTrue(self.scene().root_document_visible)

        # 8. Persist across PendingEdits reload (restart)
        restarted = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                  editable=("throwaway",), cached_only=True)
        doc2 = restarted.load_cached().get(THROWAWAY)
        reloaded = restarted.page_scene(doc2, document_pages(doc2)[0])
        self.assertEqual([l.name for l in reloaded.layers], ["Layer 1", "Ink"])
        self.assertFalse(reloaded.background_visible)
        self.assertEqual((len(reloaded.layers[1].strokes), len(reloaded.layers[1].images)), (1, 1))

    def snapshot(self):
        """What the page shows: layers bottom to top (name, visible, strokes in z-order,
        images), and the SceneInfo flags."""
        sc = self.scene()
        return ([(l.name, l.visible, [tuple((round(p.x, 2), round(p.y, 2)) for p in st.points) for st in l.strokes],
                  [i.image_ref for i in l.images]) for l in sc.layers], sc.background_visible, sc.root_document_visible)

    def test_multi_step_undo_all_then_redo_all_restores_every_state(self):
        # One mutation of each kind, chained, then Ctrl+Z all the way down and Ctrl+Y all
        # the way back up: every intermediate page must come back exactly (layer order,
        # names, visibility, each layer's strokes in z-order, images, flags).
        steps = [
            lambda t: self.source.add_stroke(self.doc, PAGE1, stroke_line(-100, 300, 100, 300, int(Color.BLUE)), t,
                                             layer_index=0),
            lambda t: self.source.add_layer(self.doc, PAGE1, t),
            lambda t: self.source.add_stroke(self.doc, PAGE1, stroke_line(0, 200, 0, 400, int(Color.RED)), t,
                                             layer_index=1),
            lambda t: self.source.rename_layer(self.doc, PAGE1, 1, "Ink", t),
            lambda t: self.source.add_layer(self.doc, PAGE1, t),
            lambda t: self.source.insert_image(self.doc, PAGE1, te.png(), "png", te.box(), t, layer_index=2),
            lambda t: self.source.move_layer(self.doc, PAGE1, 2, 0, t),
            lambda t: self.source.set_layer_visible(self.doc, PAGE1, 1, False, t),
            lambda t: self.source.erase_strokes(self.doc, PAGE1, s.eraser_line([(-0.5, 150.0), (-0.5, 450.0)], s.MEDIUM),
                                                t, layer_index=2),
            lambda t: self.source.merge_layer_down(self.doc, PAGE1, 2, t),
            lambda t: self.source.set_background_visible(self.doc, PAGE1, False, t),
            lambda t: self.source.set_root_document_visible(self.doc, PAGE1, False, t),
            lambda t: self.source.delete_layer(self.doc, PAGE1, 0, t),
        ]
        states = [self.snapshot()]
        t = NOW
        for step in steps:
            t += 1
            self.assertIsNotNone(step(t))
            states.append(self.snapshot())
            self.assertNotEqual(states[-1], states[-2])
        for n in range(len(steps), 0, -1):
            t += 1
            self.assertTrue(self.source.undo(self.doc, PAGE1, t))
            self.assertEqual(self.snapshot(), states[n - 1], f"undo of step {n}")
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (False, True))
        for n in range(1, len(steps) + 1):
            t += 1
            self.assertTrue(self.source.redo(self.doc, PAGE1, t))
            self.assertEqual(self.snapshot(), states[n], f"redo of step {n}")
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))

    def test_reattached_layers_come_back_where_they_were_after_other_layers_moved(self):
        # Re-attaching reuses the layer's old root group item; a layer moved since (each
        # move puts a new group item for it in the root) can leave that slot above or below
        # the wrong neighbours. Redo of an add, undo of a delete and undo of a merge must
        # put the layer back at its own index anyway.
        t = NOW
        script = [("add",), ("move", 0, 1), ("move", 0, 1), ("del", 0), ("add",), ("del", 0), ("add",)]
        states = [[l.name for l in self.scene().layers]]
        for op in script:
            t += 1
            if op[0] == "add":
                self.source.add_layer(self.doc, PAGE1, t)
            elif op[0] == "move":
                self.source.move_layer(self.doc, PAGE1, op[1], op[2], t)
            else:
                self.source.delete_layer(self.doc, PAGE1, op[1], t)
            states.append([l.name for l in self.scene().layers])
        for n in range(len(script), 0, -1):
            t += 1
            self.source.undo(self.doc, PAGE1, t)
            self.assertEqual([l.name for l in self.scene().layers], states[n - 1])
        for n in range(1, len(script) + 1):
            t += 1
            self.source.redo(self.doc, PAGE1, t)
            self.assertEqual([l.name for l in self.scene().layers], states[n], f"redo of {script[n - 1]}")

        # Undo of a merge after a layer below it moved away and back.
        self.source.add_layer(self.doc, PAGE1, t + 1)                    # Layer 3 / 2 / 3 on top
        names = [l.name for l in self.scene().layers]
        self.source.add_stroke(self.doc, PAGE1, stroke_line(-50, 900, 50, 900), t + 2, layer_index=2)
        self.source.merge_layer_down(self.doc, PAGE1, 2, t + 3)
        self.source.move_layer(self.doc, PAGE1, 0, 1, t + 4)
        self.source.undo(self.doc, PAGE1, t + 5)                       # the move
        self.source.undo(self.doc, PAGE1, t + 6)                       # the merge
        sc = self.scene()
        self.assertEqual([l.name for l in sc.layers], names)
        self.assertEqual(len(sc.layers[2].strokes), 1)

    def test_redone_merge_takes_the_place_of_the_clones_later_steps_name(self):
        # Merge, then erase a clone the merge made; undo both, redo both: the erase must
        # find the redone merge's clone (the first merge's clones were deleted by undo).
        self.source.add_layer(self.doc, PAGE1, NOW)
        self.source.add_stroke(self.doc, PAGE1, stroke_line(-100, 500, 100, 500, int(Color.RED)), NOW + 1,
                               layer_index=1)
        self.source.merge_layer_down(self.doc, PAGE1, 1, NOW + 2)
        eraser = s.eraser_line([(0.25, 400.0), (0.25, 600.0)], s.MEDIUM)
        self.assertIsNotNone(self.source.erase_strokes(self.doc, PAGE1, eraser, NOW + 3, layer_index=0))
        erased = self.snapshot()
        self.assertEqual(len(erased[0][0][2]), 2)                   # cut in two
        for n in range(2):
            self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 4 + n))
        for n in range(2):
            self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 6 + n))
        self.assertEqual(self.snapshot(), erased)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 8))
        self.assertEqual(len(self.snapshot()[0][0][2]), 1)          # the whole clone is back

    def test_layer_targeted_edits_leave_overlapping_strokes_on_other_layers_alone(self):
        # The same line on Layer 1 and Layer 2: erasing, selecting and moving on Layer 2
        # must not touch Layer 1's copy, point for point.
        line = stroke_line(-100, 300, 100, 300, int(Color.BLUE))
        self.source.add_stroke(self.doc, PAGE1, line, NOW, layer_index=0)
        self.source.add_layer(self.doc, PAGE1, NOW + 1)
        self.source.add_stroke(self.doc, PAGE1, line, NOW + 2, layer_index=1)
        layer1 = self.snapshot()[0][0]
        self.assertEqual(self.snapshot()[0][1][2], layer1[2])       # identical strokes, both kept
        eraser = s.eraser_line([(0.25, 250.0), (0.25, 350.0)], s.MEDIUM)
        self.source.erase_strokes(self.doc, PAGE1, eraser, NOW + 3, layer_index=1)
        self.assertEqual(self.snapshot()[0][0], layer1)
        self.assertEqual(len(self.snapshot()[0][1][2]), 2)
        # Drawing the very same line again on Layer 1 is a new stroke there, not a match
        # for Layer 2's (the uploaded-already check in _draw is per layer).
        self.source.add_stroke(self.doc, PAGE1, line, NOW + 4, layer_index=0)
        self.assertEqual(len(self.snapshot()[0][0][2]), 2)
        self.assertEqual(len(self.snapshot()[0][1][2]), 2)

    def test_draw_erase_paste_and_current_layer_persistence_on_layer_2(self):
        # Stroke on Layer 1
        self.source.add_stroke(self.doc, PAGE1, stroke_line(-100, 300, 100, 300, color=int(Color.BLUE)),
                               NOW, layer_index=0)
        # Add Layer 2 (becomes active) and draw overlapping stroke on Layer 2
        self.source.add_layer(self.doc, PAGE1, NOW + 1)
        self.source.add_stroke(self.doc, PAGE1, stroke_line(-100, 300, 100, 300, color=int(Color.RED)),
                               NOW + 2, layer_index=1)
        # Erase on Layer 2: ONLY Layer 2's stroke is cut; Layer 1's blue stroke is untouched!
        eraser = s.eraser_line([(0.25, 250.0), (0.25, 350.0)], s.MEDIUM)
        self.assertIsNotNone(self.source.erase_strokes(self.doc, PAGE1, eraser, NOW + 3, layer_index=1))
        sc = self.scene()
        self.assertEqual(len(sc.layers[0].strokes), 1)
        self.assertEqual(len(sc.layers[1].strokes), 2)  # split into 2 pieces

        # Paste a stroke onto Layer 2
        pasted_line = stroke_line(0, 600, 100, 600, color=int(Color.GREEN))
        self.source.paste_strokes(self.doc, PAGE1, [pasted_line], NOW + 4, layer_index=1)
        sc = self.scene()
        self.assertEqual((len(sc.layers[0].strokes), len(sc.layers[1].strokes)), (1, 3))

        # Switch active layer back to Layer 1 without disturbing undo stack
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))
        self.source.set_current_layer(self.doc, PAGE1, 0, NOW + 5)
        self.assertEqual(self.scene().current_layer, w.LAYER_1)
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))
        # Undo still undoes the paste on Layer 2!
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 6))
        self.assertEqual(len(self.scene().layers[1].strokes), 2)

        # Clear lines in Layer 2 -> undo restores them
        self.assertIsNotNone(self.source.clear_lines_in_layer(self.doc, PAGE1, 1, NOW + 7))
        self.assertEqual((len(self.scene().layers[0].strokes), len(self.scene().layers[1].strokes)), (1, 0))
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 8))
        self.assertEqual(len(self.scene().layers[1].strokes), 2)


# ------------------------------------------------------------------ 3. QML integration

class LayersQmlTests(unittest.TestCase):
    """Drive SceneController, SceneLayersModel, LayersFoldout, hideTemplateAction, and
    active-layer drawing/selection/pasting through the app's own QML."""

    wait_for, qml, scene_views = tq.EditingTests.wait_for, tq.EditingTests.qml, tq.EditingTests.scene_views
    close_document, pending = tq.EditingTests.close_document, tq.EditingTests.pending
    pens, toolbar_item = tq.PenTests.pens, tq.PenTests.toolbar_item
    click, window_point, drag = tq.PenTests.click, tq.PenTests.window_point, tq.PenTests.drag
    document_view = tq.TypingTests.document_view
    THROWAWAY, PAGE1 = THROWAWAY, PAGE1

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = layer_cloud(tb2.text_page())
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
        self.source.sync.edits.clear()
        self.source.forget_history(THROWAWAY)
        with self.source._lock:
            self.source._scenes.clear()
            self.source._staged.clear()

    def open_doc(self, doc_id: str = THROWAWAY, page_id: str = PAGE1):
        self.close_document()
        view = tq.PenTests.open_document(self, doc_id, page_id)
        view.parentItem().forceActiveFocus()
        self.wait_for(lambda: view._image is not None)
        return view

    def model_rows(self, model) -> list[dict]:
        roles = {k: bytes(v).decode() for k, v in model.roleNames().items()}
        rows = []
        for row in range(model.rowCount(QModelIndex())):
            idx = model.index(row, 0)
            rows.append({name: model.data(idx, role) for role, name in roles.items()})
        return rows

    def test_scene_layers_model_and_foldout_operations(self):
        view = self.open_doc(THROWAWAY)
        ctrl = view._controller
        model = scene_bridge.SceneLayersModel()
        model.setProperty("controller", ctrl)
        tq.wait(50)

        # Single layer: no trash/up/down/merge enabled.
        rows = self.model_rows(model)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["layerName"], rows[0]["layerSelected"], rows[0]["layerTrashEnabled"],
                          rows[0]["layerMoveUpEnabled"], rows[0]["layerMoveDownEnabled"],
                          rows[0]["layerMergeEnabled"]),
                         ("Layer 1", True, False, False, False, False))

        # Add Layer 2 via SceneController.addLayer()
        ctrl.addLayer()
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 2 and ctrl.property("currentLayer") == 1)
        rows = self.model_rows(model)
        self.assertEqual((rows[0]["layerSelected"], rows[0]["layerMoveUpEnabled"], rows[0]["layerMoveDownEnabled"],
                          rows[0]["layerMergeEnabled"], rows[0]["layerTrashEnabled"]),
                         (False, True, False, False, True))
        self.assertEqual((rows[1]["layerSelected"], rows[1]["layerMoveUpEnabled"], rows[1]["layerMoveDownEnabled"],
                          rows[1]["layerMergeEnabled"], rows[1]["layerTrashEnabled"]),
                         (True, False, True, True, True))

        # Rename, hide, move down, merge down, delete, and undo through SceneController
        ctrl.setLayerName(1, "Notes")
        self.wait_for(lambda: ctrl.layerName(1) == "Notes")
        ctrl.setLayerVisible(1, False)
        self.wait_for(lambda: not ctrl.isLayerVisible(1))
        ctrl.moveLayer(1, 0)
        self.wait_for(lambda: ctrl.layerName(0) == "Notes" and ctrl.property("currentLayer") == 0)
        ctrl.undo()
        self.wait_for(lambda: ctrl.layerName(1) == "Notes" and ctrl.property("currentLayer") == 1)
        ctrl.setLayerVisible(1, True)
        self.wait_for(lambda: ctrl.isLayerVisible(1))

        # Add up to 5 layers; 6th notifies error and keeps 5.
        for target in (3, 4, 5):
            ctrl.addLayer()
            self.wait_for(lambda t=target: model.rowCount(QModelIndex()) == t)
        self.assertEqual(model.property("count"), 5)
        ctrl.addLayer()
        self.wait_for(lambda: any("5" in n for n in self.notices))
        self.assertEqual(model.rowCount(QModelIndex()), 5)

        # Merge top layer down -> 4 layers; undo -> 5 layers
        ctrl.mergeLayerDown(4)
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 4)
        ctrl.undo()
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 5)

        # Delete top layer -> 4 layers; undo -> 5 layers
        ctrl.deleteLayer(4)
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 4)
        ctrl.undo()
        self.wait_for(lambda: model.rowCount(QModelIndex()) == 5)

        self.assertEqual(tq.real_warnings(self.app.warnings), [])
        model.deleteLater()
        self.close_document()

    def test_draw_select_move_and_paste_target_active_layer_2(self):
        view = self.open_doc(THROWAWAY)
        ctrl = view._controller
        # Layer 1 strokes inside every region Layer 2 is about to select, move, paste on,
        # straighten on and erase: none of it may touch them.
        for n, y in enumerate((600.0, 700.0, 900.0, 1100.0, 1300.0)):
            view.pen_commit("draw", stroke_line(-150.0, y, 150.0, y, color=int(Color.BLUE)), 10 + n)
            self.wait_for(lambda k=n: len(view._content.scene.layers[0].strokes) == k + 1)
        layer1 = lambda: [[(p.x, p.y) for p in st.points] for st in view._content.scene.layers[0].strokes]  # noqa: E731
        kept = layer1()
        ctrl.addLayer()
        self.wait_for(lambda: ctrl.property("layerCount") == 2 and ctrl.property("currentLayer") == 1)

        # Draw a stroke on Layer 2 via pen_commit (as PenSurface does)
        line = stroke_line(-150.0, 600.0, 150.0, 600.0, color=int(Color.RED))
        before_l1 = len(view._content.scene.layers[0].strokes)
        view.pen_commit("draw", line, 1)
        self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 1)
        self.assertEqual(len(view._content.scene.layers[0].strokes), before_l1)

        # Select on active Layer 2 via addSelectionRect, move, and commit
        ctrl.addSelectionRect(QRect(-200, 550, 400, 100), 0)
        self.wait_for(lambda: ctrl.property("selectionItemCount") == 1)
        ctrl.moveSelectedItems(1, QPointF(0.0, 100.0))
        ctrl.applyPendingEdit(1)
        self.wait_for(lambda: view._content.scene.layers[1].strokes[0].points[0].y > 680.0)
        self.assertEqual(len(view._content.scene.layers[0].strokes), before_l1)

        # Copy and paste onto Layer 2
        ctrl.addSelectionRect(QRect(-200, 650, 400, 100), 0)
        self.wait_for(lambda: ctrl.property("selectionItemCount") == 1)
        copied = ctrl.cloneSelectedItems(1, 1.0)
        self.assertEqual(len(copied), 1)
        ctrl.clearSelectedItems()
        ctrl.cloneAddAndSelectItems(1, copied, 1.0, QPointF(0.0, 900.0))
        self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 2)
        self.assertEqual(len(view._content.scene.layers[0].strokes), before_l1)

        # Lasso selection, scale and rotate on Layer 2
        lasso = [(-250.0, 840.0), (250.0, 840.0), (250.0, 960.0), (-250.0, 960.0)]
        self.assertTrue(view.select_lasso(lasso))
        self.assertEqual(ctrl.property("selectionItemCount"), 1)
        ctrl.scaleSelectedItems(1, QPointF(0.0, 900.0), 1.2, 1.2)
        ctrl.rotateSelectedItems(1, QPointF(0.0, 900.0), 15.0)
        ctrl.applyPendingEdit(1)
        self.wait_for(lambda: not view._hidden)
        self.assertEqual(len(view._content.scene.layers[0].strokes), before_l1)

        # Hold-to-straighten stroke committed onto Layer 2
        straight_raw = stroke_line(-100.0, 1100.0, 100.0, 1104.0, color=int(Color.BLUE))
        straight = s.simplify(s.straighten(straight_raw, (100.0, 1104.0), 1.0))
        view.pen_commit("draw", straight, 2)
        self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 3)
        self.assertEqual(len(view._content.scene.layers[0].strokes), before_l1)

        # Eraser on Layer 2 cuts only Layer 2's stroke, leaving Layer 1 untouched
        eraser = s.eraser_line([(0.25, 1050.0), (0.25, 1150.0)], s.MEDIUM)
        view.pen_commit("erase", eraser, 3)
        self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 4)
        self.assertEqual(len(view._content.scene.layers[0].strokes), before_l1)

        # Image insertion targets active Layer 2
        img_path = self.dir / "drop.png"
        img_path.write_bytes(te.png())
        ctrl.insertImageFileAsSceneItem(QUrl.fromLocalFile(str(img_path)), QPointF(0.0, 1300.0))
        self.wait_for(lambda: len(view._content.scene.layers[1].images) == 1)
        self.assertEqual(len(view._content.scene.layers[0].images), 0)
        self.assertEqual(layer1(), kept)
        # Back on Layer 1, a rect selection over the same area takes Layer 1's strokes only.
        ctrl.setCurrentLayer(0)
        self.wait_for(lambda: ctrl.property("currentLayer") == 0)
        ctrl.addSelectionRect(QRect(-200, 550, 400, 800), 0)
        self.wait_for(lambda: ctrl.property("selectionItemCount") == 5)
        tq.wait(100)
        self.assertEqual(ctrl.property("selectionItemCount"), 5)
        ctrl.clearSelectedItems()
        self.close_document()

    # -- the app's own LayersFoldout, driven by mouse and keyboard --

    def walk(self, root):
        todo = [root]
        while todo:
            item = todo.pop()
            yield item
            todo.extend(item.childItems())

    def foldout(self):
        """(the Foldout popup item, its LayersFoldout) of the document view."""
        content = next((i for i in self.walk(self.window.contentItem())
                        if i.metaObject().className().startswith("LayersFoldout")), None)
        self.assertIsNotNone(content, "no LayersFoldout")
        popup = content
        while popup is not None and not popup.metaObject().className().startswith("Foldout"):
            popup = popup.parentItem()
        self.assertIsNotNone(popup, "no Foldout around LayersFoldout")
        return popup, content

    def show_foldout(self, shown: bool = True):
        popup, content = self.foldout()
        popup.setProperty("visible", shown)       # as the toolbar's Layers button does
        tq.wait(120)
        return content

    def row(self, content, name: str):
        rows = [i for i in self.walk(content) if i.metaObject().className().startswith("LayerComponent")
                and i.property("text") == name and i.isVisible()]
        self.assertEqual(len(rows), 1, f"layer row {name!r}")
        return rows[0]

    def press(self, item, hold_ms: int = 0) -> None:
        from PySide6.QtTest import QTest
        at = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        if not hold_ms:
            QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
        else:
            QTest.mousePress(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
            tq.wait(hold_ms)
            QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
        tq.wait(60)

    def action(self, row, key: str):
        found = [i for i in self.walk(row) if i.property("actionKey") == key]
        self.assertEqual(len(found), 1, f"{key} button of {row.property('text')!r}")
        return found[0]

    def eye(self, row):
        found = [i for i in self.walk(row) if i.property("actionKey") is None
                 and "eye" in str(i.property("iconSource") or "")]
        self.assertTrue(found, f"eye of {row.property('text')!r}")
        return found[0]

    def key(self, key, modifiers=Qt.KeyboardModifier.ControlModifier) -> None:
        from PySide6.QtTest import QTest
        self.window.requestActivate()
        QTest.keyClick(self.window, key, modifiers)
        tq.wait(40)

    def ink(self, view, x: float, y: float, r: int = 2) -> str:
        """'red', 'blue' or 'paper': what the window shows around scene point (x, y)."""
        image = self.window.grabWindow()
        dpr = image.devicePixelRatio() or 1.0
        at = view.mapToScene(view._tiles.to_view(QPointF(x, y)))
        red = blue = 0
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                c = QColor(image.pixel(round((at.x() + dx) * dpr), round((at.y() + dy) * dpr)))
                if c.red() > 150 and c.red() > c.blue() + 60 and c.green() < 140:
                    red += 1
                elif c.blue() > 120 and c.blue() > c.red() + 40:
                    blue += 1
        return "red" if red > blue and red > 2 else "blue" if blue > red and blue > 2 else "paper"

    def test_foldout_rows_drive_layers_with_rendered_z_order_visibility_and_keyboard_undo(self):
        view = self.open_doc(THROWAWAY)
        ctrl = view._controller
        cross = (300.0, 1300.0)
        base = len(view._content.scene.layers[0].strokes)          # the fixture's own strokes
        view.pen_commit("draw", stroke_line(150.0, 1300.0, 450.0, 1300.0, color=int(Color.BLUE)), 1)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.layers[0].strokes) == base + 1))

        # New layer row -> Layer 2 on top and active; red stroke on it covers the blue one.
        content = self.show_foldout()
        new_row = next(i for i in self.walk(content) if i.property("label") == "New layer")
        self.press(new_row)
        self.assertTrue(self.wait_for(lambda: ctrl.property("layerCount") == 2 and ctrl.property("currentLayer") == 1))
        self.show_foldout(False)
        view.pen_commit("draw", stroke_line(300.0, 1150.0, 300.0, 1450.0, color=int(Color.RED)), 2)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 1))
        self.assertTrue(self.wait_for(lambda: self.ink(view, *cross) == "red"))

        # Layer 2's down arrow: blue now on top at the crossing.
        content = self.show_foldout()
        self.press(self.action(self.row(content, "Layer 2"), "down"))
        self.assertTrue(self.wait_for(lambda: ctrl.layerName(0) == "Layer 2" and ctrl.property("currentLayer") == 0))
        self.show_foldout(False)
        self.assertTrue(self.wait_for(lambda: self.ink(view, *cross) == "blue"))

        # Layer 1's eye: hidden -> only red left, and the blue line away from it is gone.
        content = self.show_foldout()
        self.press(self.eye(self.row(content, "Layer 1")))
        self.assertTrue(self.wait_for(lambda: not ctrl.isLayerVisible(1)))
        self.show_foldout(False)
        self.assertTrue(self.wait_for(lambda: self.ink(view, *cross) == "red"))
        self.assertEqual(self.ink(view, 200.0, 1300.0), "paper")

        # Ctrl+Z with the foldout open: visibility back; again: the move undone. Ctrl+Y redoes.
        content = self.show_foldout()
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: ctrl.isLayerVisible(1)))
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: ctrl.layerName(1) == "Layer 2"))
        self.key(Qt.Key.Key_Y)
        self.assertTrue(self.wait_for(lambda: ctrl.layerName(0) == "Layer 2"))
        self.show_foldout(False)
        self.assertTrue(self.wait_for(lambda: self.ink(view, *cross) == "blue"))

        # Rename through the row's own text input (press and hold, type, Enter), then
        # Ctrl+Z from the keyboard must undo the rename (focus back on the page).
        content = self.show_foldout()
        row = self.row(content, "Layer 2")
        name_area = next(i for i in self.walk(row) if i.metaObject().className() == "QQuickMouseArea"
                         and i.parentItem() is not None and i.parentItem().property("text") == "Layer 2")
        self.press(name_area, hold_ms=1000)
        text_input = next(i for i in self.walk(row) if i.metaObject().className() == "QQuickTextInput")
        self.assertTrue(text_input.isVisible())
        text_input.forceActiveFocus()
        from PySide6.QtCore import QMetaObject
        from PySide6.QtTest import QTest
        QMetaObject.invokeMethod(text_input, "selectAll")
        for c in "inked":
            QTest.keyClick(self.window, Qt.Key(ord(c.upper())))
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assertTrue(self.wait_for(lambda: ctrl.layerName(0) == "inked"))
        tq.wait(80)
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: ctrl.layerName(0) == "Layer 2", 3000),
                        "Ctrl+Z after renaming a layer did not reach the page")
        self.key(Qt.Key.Key_Y)
        self.assertTrue(self.wait_for(lambda: ctrl.layerName(0) == "inked", 3000))

        # Merge (top row's merge button) and trash, each undone by Ctrl+Z.
        self.press(self.action(self.row(content, "Layer 1"), "merge"))
        self.assertTrue(self.wait_for(lambda: ctrl.property("layerCount") == 1))
        self.assertEqual(len(view._content.scene.layers[0].strokes), base + 2)
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: ctrl.property("layerCount") == 2))
        self.press(self.action(self.row(content, "inked"), "delete"))
        self.assertTrue(self.wait_for(lambda: ctrl.property("layerCount") == 1))
        self.key(Qt.Key.Key_Z)
        self.assertTrue(self.wait_for(lambda: ctrl.property("layerCount") == 2))
        self.assertEqual(sorted(len(l.strokes) for l in view._content.scene.layers), sorted([1, base + 1]))
        self.show_foldout(False)
        self.assertTrue(self.wait_for(lambda: self.ink(view, *cross) == "blue"))
        self.assertEqual(tq.real_warnings(self.app.warnings), [])
        self.close_document()

    def test_drawing_and_erasing_on_a_hidden_active_layer(self):
        # As the app (Scene::addLine / Scene::erase never read the layer's visible flag):
        # the stroke goes to the active layer even while it is hidden, unseen until shown;
        # the eraser works on that layer only.
        view = self.open_doc(THROWAWAY)
        ctrl = view._controller
        base = len(view._content.scene.layers[0].strokes)
        view.pen_commit("draw", stroke_line(150.0, 1300.0, 450.0, 1300.0, color=int(Color.BLUE)), 1)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.layers[0].strokes) == base + 1))
        ctrl.addLayer()
        self.assertTrue(self.wait_for(lambda: ctrl.property("currentLayer") == 1))
        ctrl.setLayerVisible(1, False)
        self.assertTrue(self.wait_for(lambda: not ctrl.isLayerVisible(1)))
        view.pen_commit("draw", stroke_line(300.0, 1150.0, 300.0, 1450.0, color=int(Color.RED)), 2)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 1))
        tq.wait(200)
        self.assertEqual(self.ink(view, 300.0, 1400.0), "paper")
        self.assertEqual(self.ink(view, 200.0, 1300.0), "blue")
        view.pen_commit("erase", s.eraser_line([(250.0, 1300.0), (350.0, 1300.0)], s.MEDIUM), 3)
        self.assertTrue(self.wait_for(lambda: len(view._content.scene.layers[1].strokes) == 2))
        self.assertEqual(len(view._content.scene.layers[0].strokes), base + 1)
        ctrl.setLayerVisible(1, True)
        self.assertTrue(self.wait_for(lambda: self.ink(view, 300.0, 1400.0) == "red"))
        self.close_document()

    def test_readonly_layer_toggle_leaves_the_cached_scene_alone(self):
        ro_view = self.open_doc(OTHER)
        ro_ctrl = ro_view._controller
        self.assertEqual(ro_ctrl.property("layerCount"), 2)
        self.assertTrue(self.wait_for(lambda: self.ink(ro_view, 0.0, 500.0) == "red"))
        ro_ctrl.setLayerVisible(1, False)
        self.wait_for(lambda: not ro_ctrl.isLayerVisible(1))
        self.assertTrue(self.wait_for(lambda: self.ink(ro_view, 0.0, 500.0) == "blue"))
        doc = self.source.sync.tree.get(OTHER)
        cached = self.source.page_scene(doc, document_pages(doc)[0])
        self.assertTrue(cached.layers[1].visible, "the live toggle leaked into the cached page")
        self.assertEqual(self.source.sync.edits.of(OTHER), [])
        ro_view = self.open_doc(OTHER)                       # opened again: as stored
        self.assertTrue(ro_view._controller.isLayerVisible(1))
        self.assertTrue(self.wait_for(lambda: self.ink(ro_view, 0.0, 500.0) == "red"))
        self.close_document()

    def test_background_visible_persists_on_editable_and_stays_live_on_readonly(self):
        # Editable document: setBackgroundVisible persists to SceneInfo field 2 and is undoable.
        view = self.open_doc(THROWAWAY)
        ctrl = view._controller
        self.assertTrue(ctrl.property("backgroundVisible"))
        ctrl.setBackgroundVisible(False)
        self.wait_for(lambda: any(isinstance(e, SceneFlags) and e.background_visible is False
                                  for e in self.source.sync.edits.of(THROWAWAY)))
        self.assertFalse(ctrl.property("backgroundVisible"))
        self.assertFalse(view._content.scene.background_visible)
        ctrl.undo()
        self.wait_for(lambda: ctrl.property("backgroundVisible") is True and view._content.scene.background_visible)
        ctrl.redo()
        self.wait_for(lambda: ctrl.property("backgroundVisible") is False and not view._content.scene.background_visible)

        # Read-only document: setBackgroundVisible works live without creating any pending edits!
        ro_view = self.open_doc(OTHER)
        ro_ctrl = ro_view._controller
        self.assertTrue(ro_ctrl.property("backgroundVisible"))
        ro_ctrl.setBackgroundVisible(False)
        tq.wait(150)
        self.assertFalse(ro_ctrl.property("backgroundVisible"))
        self.assertEqual(self.source.sync.edits.of(OTHER), [])
        ro_ctrl.setBackgroundVisible(True)
        tq.wait(150)
        self.assertTrue(ro_ctrl.property("backgroundVisible"))
        self.assertEqual(self.source.sync.edits.of(OTHER), [])
        self.close_document()


if __name__ == "__main__":
    unittest.main()
