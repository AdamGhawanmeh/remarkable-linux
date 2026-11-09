"""Offline tests for viewer.rm_parser and viewer.renderer with synthetic payloads.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rmfixtures import (  # noqa: E402
    ROOT, ZERO, block, f_id, f_sub, group_item, header, image_entries, image_item, image_value, images_table,
    line_item, paragraph_format, points_v1, points_v2, root_text, sample_text_block, scene_tree,
    text_item, tombstone, tree_node, v5_file, v6_file)

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from viewer.rm_parser import (  # noqa: E402
    ANCHOR_SENTINEL_START, Anchor, Color, PageScene, ParagraphStyle, Point, RmParseError,
    Stroke, Tool, block_name, parse, parse_file, parse_header)
from viewer.renderer import (  # noqa: E402
    LINE_PX, PALETTE_ARGB, estimate_width, image_matrix, image_size, layout_text, render_bounds, render_image,
    render_svg, stroke_offsets, stroke_runs, stroke_style)

# Fonts need an application; a QApplication (not just QGuiApplication) so the UI tests
# can share it when the whole suite runs in one process.
APP = QApplication.instance() or QApplication([])

LAYER_A, LAYER_B = (1, 10), (1, 20)


def sample_v6() -> bytes:
    """Two named layers (B listed first under root, B hidden), mixed tools."""
    return v6_file(
        tree_node(ROOT),
        tree_node(LAYER_A, "Sketch", True),
        tree_node(LAYER_B, "Notes", False),
        scene_tree(LAYER_A), scene_tree(LAYER_B),
        group_item(ROOT, (1, 30), LAYER_B),
        group_item(ROOT, (1, 31), LAYER_A),
        line_item(LAYER_A, (1, 40), tool=17, color=0, thickness=2.0, starting_length=1.5,
                  points=points_v2([(-100.0, 200.0, 2.5, 64, 3.0, 255),
                                    (-90.5, 210.25, 3.0, 128, 4.5, 128)])),
        line_item(LAYER_A, (1, 41), tool=18, color=9, argb=0xFFFFED75, thickness=1.0,
                  points=points_v2([(0, 500, 0, 0, 30.0, 255), (300, 500, 0, 0, 30.0, 255)])),
        line_item(LAYER_B, (1, 42), tool=14, color=6,
                  points=points_v2([(50, 50, 1, 0, 2, 51)])),
        # erased: deleted_length set; and one removed by tombstone; and one with no value
        line_item(LAYER_A, (1, 43), tool=4, color=0, deleted=1, points=b""),
        line_item(LAYER_A, (1, 44), tool=4, color=0, points=points_v2([(1, 1, 0, 0, 1, 1)])),
        tombstone(LAYER_A, (1, 44)),
        line_item(LAYER_A, (1, 45), tool=4, color=0, with_value=False, points=b""),
        sample_text_block(),
        images_table(),
        image_item(LAYER_A, (1, 60), deleted=1),
        block(0x03, b"glyph highlight: not interpreted"),
    )


class HeaderTests(unittest.TestCase):
    def test_versions(self):
        for v in (3, 5, 6):
            self.assertEqual(parse_header(header(v) + b"rest"), v)

    def test_rejects(self):
        for bad in (b"", b"reMarkable .lines", b"PDF-1.7".ljust(43),
                    b"reMarkable .lines file, version=x".ljust(43)):
            with self.assertRaises(RmParseError):
                parse_header(bad)
        with self.assertRaisesRegex(RmParseError, "unsupported .lines version 2"):
            parse(header(2))


class V5Tests(unittest.TestCase):
    STROKES = [
        [{"tool": 4, "color": 0, "width": 2.0,
          "points": [(10, 20, 1.0, 0.5, 2.0, 0.25), (30, 40, 2.0, 1.0, 2.5, 0.75)]}],
        [{"tool": 5, "color": 0, "width": 2.125, "points": [(100, 100, 0, 0, 30, 1)]},
         {"tool": 1, "color": 1, "width": 1.875, "points": []}],
    ]

    def check(self, scene: PageScene, version: int):
        self.assertEqual(scene.version, version)
        self.assertEqual(scene.x_origin, 0)
        self.assertEqual([len(l.strokes) for l in scene.layers], [1, 2])
        self.assertEqual([l.name for l in scene.layers], ["Layer 1", "Layer 2"])
        s = scene.layers[0].strokes[0]
        self.assertEqual((s.tool, s.color, s.thickness_scale), (Tool.FINELINER, Color.BLACK, 2.0))
        self.assertEqual(s.points[1], Point(30, 40, 2.0, 1.0, 2.5, 0.75))
        self.assertEqual(s.points[0].tilt, 0.5)
        hl, pencil = scene.layers[1].strokes
        self.assertEqual((hl.tool.family, pencil.tool, pencil.color), ("highlighter", Tool.PENCIL, Color.GRAY))
        self.assertEqual(pencil.points, [])

    def test_v5(self):
        self.check(parse(v5_file(self.STROKES, 5)), 5)

    def test_v3_has_no_extra_stroke_field(self):
        self.check(parse(v5_file(self.STROKES, 3)), 3)

    def test_errors(self):
        data = v5_file(self.STROKES)
        with self.assertRaisesRegex(RmParseError, "unexpected end"):
            parse(data[:-5])
        with self.assertRaisesRegex(RmParseError, "trailing"):
            parse(data + b"\x00")
        with self.assertRaisesRegex(RmParseError, "negative count"):
            parse(header(5) + struct.pack("<i", -1))
        with self.assertRaisesRegex(RmParseError, "exceeds remaining"):
            parse(header(5) + struct.pack("<iiiiifi", 1, 1, 4, 0, 0, 2.0, 0) + struct.pack("<i", 10**6))

    def test_unknown_enums_are_kept_raw(self):
        s = parse(v5_file([[{"tool": 99, "color": 77, "width": 1, "points": []}]])).strokes[0]
        self.assertEqual((s.tool, s.color, s.tool_id, s.color_id), (Tool.UNKNOWN, Color.UNKNOWN, 99, 77))


class V6Tests(unittest.TestCase):
    def setUp(self):
        self.scene = parse(sample_v6())

    def test_layers_order_names_visibility(self):
        sc = self.scene
        self.assertEqual(sc.version, 6)
        self.assertEqual(sc.x_origin, 702)
        self.assertEqual([(l.name, l.visible, l.node_id) for l in sc.layers],
                         [("Notes", False, LAYER_B), ("Sketch", True, LAYER_A)])
        self.assertEqual([len(l.strokes) for l in sc.layers], [1, 2])
        self.assertEqual(sc.block_counts, {0x00: 1, 0x01: 2, 0x02: 3, 0x03: 1, 0x04: 2,
                                           0x05: 6, 0x07: 1, 0x08: 1, 0x0A: 1, 0x0E: 1, 0x0F: 1})
        self.assertEqual(sc.skipped_blocks, {0x03: 1})   # only the glyph block
        self.assertEqual(sc.images, [])                  # the one image item is deleted

    def test_points_decode_and_normalise(self):
        fineliner = self.scene.layers[1].strokes[0]
        self.assertEqual((fineliner.tool, fineliner.color), (Tool.FINELINER_V2, Color.BLACK))
        self.assertEqual((fineliner.thickness_scale, fineliner.starting_length, fineliner.item_id),
                         (2.0, 1.5, (1, 40)))
        p0, p1 = fineliner.points
        self.assertEqual((p0.x, p0.y, p0.speed, p0.width, p0.pressure), (-100.0, 200.0, 2.5, 3.0, 1.0))
        self.assertAlmostEqual(p0.direction, 64 * 2 * math.pi / 255)
        self.assertEqual((p1.x, p1.y, p1.width), (-90.5, 210.25, 4.5))
        self.assertAlmostEqual(p1.pressure, 128 / 255)
        self.assertEqual(self.scene.to_page(p0.x, p0.y), (602.0, 200.0))

    def test_argb_highlighter_and_pencil(self):
        hl = self.scene.layers[1].strokes[1]
        self.assertEqual((hl.tool, hl.tool.family, hl.color, hl.argb),
                         (Tool.HIGHLIGHTER_V2, "highlighter", Color.ARGB, 0xFFFFED75))
        pencil = self.scene.layers[0].strokes[0]
        self.assertEqual((pencil.tool.family, pencil.color), ("pencil", Color.BLUE))

    def test_deleted_items_dropped(self):
        ids = {s.item_id for s in self.scene.strokes}
        self.assertEqual(ids, {(1, 40), (1, 41), (1, 42)})

    def test_version1_points(self):
        data = v6_file(line_item(LAYER_A, (1, 2), tool=4, color=7, version=1,
                                 points=points_v1([(1, 2, 3, 0.5, 5, 0.25)])))
        s = parse(data).strokes[0]
        self.assertEqual(s.points, [Point(1, 2, 3, 0.5, 5, 0.25)])
        self.assertEqual(s.color, Color.RED)

    def test_strokes_without_layer_go_to_root_layer(self):
        data = v6_file(line_item(ROOT, (1, 2), tool=4, color=0,
                                 points=points_v2([(0, 0, 0, 0, 1, 1)])))
        sc = parse(data)
        self.assertEqual([(l.name, l.node_id, len(l.strokes)) for l in sc.layers],
                         [("Root", ROOT, 1)])

    def test_unnamed_layer_gets_default_name(self):
        data = v6_file(tree_node(LAYER_A, ""), group_item(ROOT, (1, 3), LAYER_A),
                       group_item(ROOT, (1, 4), LAYER_A))
        self.assertEqual([l.name for l in parse(data).layers], ["Layer 1"])

    def test_malformed_blocks_report_offset(self):
        bad_points = v6_file(line_item(LAYER_A, (1, 2), tool=4, color=0, points=b"\x00" * 13))
        with self.assertRaisesRegex(RmParseError, r"block type 0x5 at offset \d+.*multiple of 14"):
            parse(bad_points)
        bad_v1 = v6_file(line_item(LAYER_A, (1, 2), tool=4, color=0, version=1, points=b"\x00" * 23))
        with self.assertRaisesRegex(RmParseError, "multiple of 24"):
            parse(bad_v1)
        with self.assertRaisesRegex(RmParseError, "unknown tag type"):
            parse(header(6) + block(0x02, b"\x13"))
        with self.assertRaisesRegex(RmParseError, "missing field 1"):
            parse(header(6) + block(0x02, f_id(2, (0, 1))))
        with self.assertRaisesRegex(RmParseError, "unexpected end"):
            parse(sample_v6()[:-3])
        with self.assertRaisesRegex(RmParseError, "varuint too long"):
            parse(header(6) + block(0x02, b"\xff" * 12))
        wrong_type = header(6) + block(0x05, f_id(1, LAYER_A) + f_id(2, (1, 2)) + f_sub(6, b"\x07"))
        with self.assertRaisesRegex(RmParseError, "unexpected item type 7"):
            parse(wrong_type)

    def test_parse_file(self):
        with tempfile.NamedTemporaryFile(suffix=".rm") as fh:
            fh.write(sample_v6())
            fh.flush()
            self.assertEqual(len(parse_file(fh.name).strokes), 3)

    def test_content_bounds(self):
        x0, y0, x1, y1 = self.scene.content_bounds()
        self.assertEqual((x0, y0, x1, y1), (602.0, 50.0, 1002.0, 500.0))
        self.assertIsNone(PageScene(version=6).content_bounds())
        self.assertIsNone(Stroke(Tool.PENCIL, Color.BLACK, []).bounds())


class TextAndImageTests(unittest.TestCase):
    def test_text_paragraphs_styles_and_crdt_order(self):
        text = parse(v6_file(sample_text_block())).text
        self.assertEqual((text.x, text.y, text.width), (-468, 234, 1000))   # LWW width wins
        self.assertEqual([(p.style, p.text) for p in text.paragraphs],
                         [(ParagraphStyle.TITLE, "TiXtle"), (ParagraphStyle.BULLETPOINT, "Hello")])
        spans = text.paragraphs[1].spans
        self.assertEqual([(s.text, s.bold, s.italic) for s in spans],
                         [("Hel", True, False), ("lo", False, False)])
        self.assertEqual(text.text, "TiXtle\nHello")
        self.assertFalse(text.is_blank)

    def test_italic_markers_and_defaults(self):
        data = v6_file(root_text([text_item((1, 1), ZERO, fmt=3), text_item((1, 2), (1, 1), text="it"),
                                  text_item((1, 4), (1, 3), fmt=4), text_item((1, 5), (1, 4), text="!")],
                                 []))
        text = parse(data).text
        self.assertEqual(text.paragraphs[0].style, ParagraphStyle.PARAGRAPH)   # no format entry
        self.assertEqual([(s.text, s.italic) for s in text.paragraphs[0].spans], [("it", True), ("!", False)])
        self.assertEqual(text.width, 936)

    def test_blank_text_like_real_files(self):
        data = v6_file(root_text([text_item((1, 1), ZERO, text="\n\n "), text_item((1, 9), (1, 3), deleted=4)],
                                 [paragraph_format(ZERO, 2)]))
        text = parse(data).text
        self.assertEqual(len(text.paragraphs), 3)
        self.assertTrue(text.is_blank)

    def test_text_errors(self):
        good_item = text_item((1, 1), ZERO, text="a")
        for payload, message in (
                ([b"\x1c" + good_item[1:]], "not a sub-block"),
                ([text_item((1, 1), (1, 2), text="a"), text_item((1, 2), (1, 1), text="b")], "cycle")):
            with self.assertRaisesRegex(RmParseError, message):
                parse(v6_file(root_text(payload, [])))
        bad_formats = (
            (bytes([0, 0]) + f_sub(2, bytes([17, 2])), "missing its timestamp"),
            (bytes([0, 0]) + f_id(1, (1, 1)) + f_id(2, (1, 1)), "missing its value"),
            (bytes([0, 0]) + f_id(1, (1, 1)) + f_sub(2, b"\x11"), "expected 2"),
        )
        for fmt, message in bad_formats:
            with self.assertRaisesRegex(RmParseError, message):
                parse(v6_file(root_text([good_item], [fmt])))

    def test_unknown_paragraph_style(self):
        data = v6_file(root_text([text_item((1, 1), ZERO, text="a")], [paragraph_format(ZERO, 99)]))
        self.assertEqual(parse(data).text.paragraphs[0].style, ParagraphStyle.UNKNOWN)

    def test_live_and_deleted_images(self):
        data = v6_file(images_table(), image_item(LAYER_A, (1, 1), value=b"\x05payload"),
                       image_item(LAYER_A, (1, 2), deleted=3), image_item(LAYER_A, (1, 3), value=b"\x05x"),
                       tombstone(LAYER_A, (1, 3)))
        images = parse(data).images
        self.assertEqual([(i.item_id, i.parent_id, i.value) for i in images],
                         [((1, 1), LAYER_A, b"\x05payload")])

    def test_block_names(self):
        self.assertEqual([block_name(t) for t in (0, 5, 7, 13, 14, 15, 16, -1)],
                         ["Conversion", "CrdtLineItem", "RootText", "SceneInfo", "SceneImagesTable",
                          "CrdtImageItem", "Unknown16", "Unknown-1"])



def image_file(width: int, height: int, color: str = "#ff0000", fmt: str = "PNG") -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt)
    return bytes(data)


def placed(x0, y0, x1, y1):
    """Image corners for the page rectangle x0..x1, y0..y1 (v6 x is centred), as the
    tablet lists them: top left, top right, bottom right, bottom left."""
    return [(x0 - 702, y0, 0.0, 0.0), (x1 - 702, y0, 1.0, 0.0), (x1 - 702, y1, 1.0, 1.0), (x0 - 702, y1, 0.0, 1.0)]


KEY = bytes(range(16))
IMAGE_NAME = "0a1b2c3d-0000-4000-8000-000000000001.png"


def image_page(*extra: bytes, vertices=None) -> bytes:
    """A page as the tablet writes one after an image was inserted, then moved: Layer 1,
    the images table, the first item deleted and its moved replacement."""
    return v6_file(tree_node(LAYER, "Layer 1"), group_item(ROOT, (0, 13), LAYER),
                   images_table(image_entries({KEY: IMAGE_NAME})),
                   image_item(LAYER, (1, 16), deleted=2),
                   image_item(LAYER, (1, 18), image_value(KEY, vertices or placed(100, 200, 300, 300), move_id=(1, 16)),
                              left=(1, 17)),
                   *extra)


class ImageItemTests(unittest.TestCase):
    """Images inserted on the tablet (CrdtImageItem + SceneImagesTable). The fixture
    builders write the blocks byte for byte as a real tablet page has them."""

    def test_tablet_image_decodes(self):
        scene = parse(image_page())
        [image] = scene.placed_images
        self.assertEqual((image.item_id, image.parent_id, image.image_ref, image.file_name),
                         ((1, 18), LAYER, KEY, IMAGE_NAME))
        self.assertEqual(image.vertices, placed(100, 200, 300, 300))
        self.assertEqual(image.indices, [0, 1, 2, 2, 3, 0])
        self.assertEqual([(l.name, len(l.images)) for l in scene.layers], [("Layer 1", 1)])
        self.assertEqual(scene.content_bounds(), (100, 200, 300, 300))

    def test_images_stack_with_strokes_in_file_order(self):
        below = line_item(LAYER, (1, 2), tool=17, color=0, points=points_v2([(0, 0, 0, 0, 2, 255)]))
        above = line_item(LAYER, (1, 30), tool=17, color=0, points=points_v2([(1, 1, 0, 0, 2, 255)]))
        data = v6_file(tree_node(LAYER, "Layer 1"), group_item(ROOT, (0, 13), LAYER), below,
                       images_table(image_entries({KEY: IMAGE_NAME})),
                       image_item(LAYER, (1, 18), image_value(KEY, placed(0, 0, 10, 10))), above)
        layer = parse(data).layers[0]
        self.assertEqual([type(item).__name__ for item in layer.items()], ["Stroke", "ImageItem", "Stroke"])

    def test_undecodable_image_is_kept_but_not_drawn(self):
        data = v6_file(images_table(), image_item(LAYER, (1, 1), value=b"\x05unknown"))
        with self.assertLogs("rm.viewer.parser", "WARNING"):
            scene = parse(data)
        self.assertEqual([i.value for i in scene.images], [b"\x05unknown"])
        self.assertEqual(scene.placed_images, [])
        render_image(scene)                                  # nothing to draw, nothing breaks

    def test_image_drawn_where_the_tablet_put_it(self):
        scene = parse(image_page())
        scene.placed_images[0].data = image_file(20, 10)
        image = render_image(scene)
        red, white = QColor("#ff0000").rgb(), QColor("#ffffff").rgb()
        for x, y in ((105, 205), (200, 250), (295, 295)):
            self.assertEqual(image.pixel(x, y), red, (x, y))
        for x, y in ((95, 250), (305, 250), (200, 195), (200, 305)):
            self.assertEqual(image.pixel(x, y), white, (x, y))

    def test_image_without_its_file_is_not_drawn(self):
        image = render_image(parse(image_page()))
        self.assertEqual(image.pixel(200, 250), QColor("#ffffff").rgb())

    def test_image_in_svg(self):
        scene = parse(image_page())
        scene.placed_images[0].data = image_file(20, 10)
        root = ET.fromstring(render_svg(scene))
        [element] = root.iter("{http://www.w3.org/2000/svg}image")
        self.assertEqual((element.get("width"), element.get("height")), ("20", "10"))
        self.assertEqual(element.get("transform"), "matrix(10 0 0 10 100 200)")
        self.assertTrue(element.get("href").startswith("data:image/png;base64,"))

    def test_rotated_and_jpeg_images(self):
        # Turned a quarter clockwise: the image's top edge runs down the page.
        scene = parse(image_page(vertices=[(400 - 702, 100, 0, 0), (400 - 702, 300, 1, 0),
                                           (300 - 702, 300, 1, 1), (300 - 702, 100, 0, 1)]))
        jpeg = image_file(40, 20, fmt="JPG")
        self.assertEqual(image_size(jpeg), (40, 20))
        self.assertEqual(image_size(image_file(7, 3)), (7, 3))
        a, b, c, d, e, f = image_matrix(scene, scene.placed_images[0], 40, 20)
        corner = lambda px, py: (a * px + c * py + e, b * px + d * py + f)
        self.assertEqual([tuple(round(v, 6) for v in corner(*p)) for p in ((0, 0), (40, 0), (0, 20))],
                         [(400, 100), (400, 300), (300, 100)])
        scene.placed_images[0].data = jpeg
        self.assertGreater(QColor(render_image(scene).pixel(350, 200)).red(), 200)


LAYER = (0, 11)          # real files: Layer 1 is node 0:11
GROUP, NESTED = (1, 100), (1, 101)


def anchored_page(anchor_char=(1, 7), origin_x=-464.0) -> bytes:
    """Text: 'Title' + '\n' + 'Hello'; a stroke in a group anchored to 'Hello'
    (nested one level deeper), plus a free stroke directly in the layer."""
    return v6_file(
        tree_node(ROOT), tree_node(LAYER, "Layer 1", True),
        tree_node(GROUP, "", True, anchor=(anchor_char, origin_x)),
        tree_node(NESTED, "", True),
        group_item(ROOT, (1, 1), LAYER),
        group_item(LAYER, (1, 2), GROUP),
        group_item(GROUP, (1, 3), NESTED),
        root_text([text_item((1, 1), ZERO, text="Title\nHello")], [paragraph_format(ZERO, 2)]),
        line_item(NESTED, (1, 10), tool=17, color=0, points=points_v2([(10, -5, 0, 0, 3, 255), (60, 5, 0, 0, 3, 255)])),
        line_item(LAYER, (1, 11), tool=17, color=0, points=points_v2([(0, 900, 0, 0, 3, 255), (5, 905, 0, 0, 3, 255)])))


class AnchorTests(unittest.TestCase):
    def test_groups_fold_into_their_layer_with_anchor(self):
        sc = parse(anchored_page())
        self.assertEqual([(l.name, l.node_id, len(l.strokes)) for l in sc.layers], [("Layer 1", LAYER, 2)])
        nested, free = sc.layers[0].strokes
        self.assertEqual(nested.anchor, Anchor(char_id=(1, 7), origin_x=-464.0, threshold=35.75, type=2))
        self.assertIsNone(free.anchor)
        self.assertEqual(sc.text.char_paragraph[(1, 7)], 1)    # 'H' of "Hello"
        self.assertEqual(sc.text.char_paragraph[(1, 6)], 0)    # newline belongs to the line it ends

    def test_anchor_with_only_char_id(self):
        node = block(0x02, f_id(1, GROUP) + f_sub(7, f_id(1, (1, 1)) + f_id(2, (1, 7))), version=2)
        data = v6_file(node, group_item(LAYER, (1, 2), GROUP),
                       line_item(GROUP, (1, 3), tool=17, color=0, points=points_v2([(0, 0, 0, 0, 1, 1)])))
        self.assertEqual(parse(data).strokes[0].anchor, Anchor((1, 7), origin_x=0.0))

    def test_sentinel_anchor(self):
        self.assertTrue(Anchor((0, ANCHOR_SENTINEL_START), 0).is_special)
        self.assertFalse(Anchor((1, 7), 0).is_special)

    def test_group_cycle_does_not_hang(self):
        data = v6_file(group_item(GROUP, (1, 2), NESTED), group_item(NESTED, (1, 3), GROUP),
                       line_item(NESTED, (1, 4), tool=17, color=0, points=points_v2([(0, 0, 0, 0, 1, 1)])))
        self.assertEqual(len(parse(data).strokes), 1)

    def test_offsets_follow_text_lines(self):
        sc = parse(anchored_page())
        lines = layout_text(sc)
        offsets = stroke_offsets(sc, lines)
        nested, free = sc.layers[0].strokes
        hello_baseline = next(l.baseline for l in lines if l.paragraph == 1)
        self.assertEqual(offsets[id(nested)], (-464.0, hello_baseline))
        self.assertNotIn(id(free), offsets)
        self.assertAlmostEqual(hello_baseline - next(l.baseline for l in lines if l.paragraph == 0), LINE_PX)
        runs = stroke_runs(sc, nested, offsets[id(nested)])
        self.assertEqual(runs[0].points[0], (702 - 464 + 10, hello_baseline - 5))

    def test_sentinel_or_unknown_anchor_sits_at_text_top(self):
        for char in ((0, ANCHOR_SENTINEL_START), (9, 9)):
            sc = parse(anchored_page(anchor_char=char))
            nested = sc.layers[0].strokes[0]
            self.assertEqual(stroke_offsets(sc, layout_text(sc))[id(nested)], (-464.0, 234.0))

    def test_anchor_without_text_has_no_vertical_offset(self):
        data = v6_file(tree_node(GROUP, "", True, anchor=((1, 7), 100.0)), group_item(LAYER, (1, 2), GROUP),
                       line_item(GROUP, (1, 3), tool=17, color=0, points=points_v2([(0, 50, 0, 0, 1, 1)])))
        sc = parse(data)
        self.assertEqual(stroke_offsets(sc, layout_text(sc))[id(sc.strokes[0])], (100.0, 0.0))

    def test_bounds_include_offset_ink_and_text(self):
        many = "\n" * 40 + "bottom line"
        data = v6_file(root_text([text_item((1, 1), ZERO, text=many)], []))
        x, y, w, h = render_bounds(parse(data))
        self.assertGreater(y + h, 234 + 40 * LINE_PX)           # page grew to fit the text
        svg = ET.fromstring(render_svg(parse(data)))
        self.assertGreater(float(svg.get("height")), 1872)


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.scene = parse(sample_v6())

    def stroke(self, tool, color=Color.BLACK, points=None, argb=None, thickness=1.0):
        pts = points if points is not None else [Point(0, 0, width=2, pressure=1),
                                                 Point(10, 0, width=2, pressure=1)]
        return Stroke(tool=tool, color=color, points=pts, argb=argb, thickness_scale=thickness)

    def test_styles(self):
        hl = stroke_style(self.stroke(Tool.HIGHLIGHTER_V2, Color.ARGB, argb=0xFF94D8FF))
        self.assertEqual((hl.color, hl.alpha, hl.blend_darken, hl.square_cap),
                         ("#94d8ff", 1.0, True, True))
        legacy = stroke_style(self.stroke(Tool.HIGHLIGHTER, Color.BLACK))
        self.assertEqual(legacy.color, "#ffed75")
        self.assertEqual(stroke_style(self.stroke(Tool.FINELINER, Color.RED)).color, "#c23132")
        stacked = stroke_style(self.stroke(Tool.HIGHLIGHTER_V2, Color.HIGHLIGHTER_STACKED_GRAY))
        self.assertEqual((stacked.color, stacked.alpha), ("#000000", 0x40 / 255))
        self.assertEqual(stroke_style(self.stroke(Tool.FINELINER, Color.UNKNOWN)).color, "#000000")
        self.assertEqual(stroke_style(self.stroke(Tool.ERASER), "#eeeeee").color, "#eeeeee")
        for tool in (Tool.ERASE_SECTION, Tool.SELECTION_TOOL, Tool.UNKNOWN):
            self.assertIsNone(stroke_style(self.stroke(tool)))
        self.assertTrue(stroke_style(self.stroke(Tool.SHADING_MARKER)).blend_darken)

    def test_palette_is_the_official_table(self):
        # 17 entries, one per Color value 0..16 except the per-stroke ARGB slot.
        self.assertEqual(sorted(PALETTE_ARGB), sorted(c for c in Color if c.value >= 0 and c != Color.ARGB))
        self.assertEqual(PALETTE_ARGB[Color.HIGHLIGHTER_YELLOW], 0xFFFFED75)
        self.assertEqual(PALETTE_ARGB[Color.WHITE], 0xFFFFFFFF)

    def test_runs_split_on_width_and_stay_continuous(self):
        pts = [Point(0, 0, width=2), Point(1, 0, width=2), Point(2, 0, width=4),
               Point(3, 0, width=4), Point(4, 0, width=4.1)]
        runs = stroke_runs(PageScene(version=5), self.stroke(Tool.FINELINER, points=pts))
        self.assertEqual([(r.width, len(r.points)) for r in runs], [(2, 2), (4, 4)])
        self.assertEqual(runs[0].points[-1], runs[1].points[0])

    def test_runs_opacity_and_fallbacks(self):
        scene = PageScene(version=6, x_origin=702)
        pencil = self.stroke(Tool.PENCIL_V2, points=[Point(0, 0, width=2, pressure=0),
                                                     Point(5, 5, width=2, pressure=0)])
        self.assertEqual(stroke_runs(scene, pencil)[0].opacity, 0.35)
        hl = stroke_runs(scene, self.stroke(Tool.HIGHLIGHTER_V2, points=[Point(0, 0), Point(9, 0)]))
        self.assertEqual((hl[0].width, hl[0].opacity), (30, 1.0))   # opaque; Darken blends
        shader = stroke_runs(scene, self.stroke(Tool.SHADING_MARKER))
        self.assertEqual(shader[0].opacity, 0.2)
        single = stroke_runs(scene, self.stroke(Tool.FINELINER, points=[Point(1, 2, width=3)]))
        self.assertEqual(single[0].points, ((703, 2), (703, 2)))
        self.assertEqual(stroke_runs(scene, self.stroke(Tool.FINELINER, points=[])), [])

    def test_bounds(self):
        self.assertEqual(render_bounds(self.scene), (0, 0, 1404, 1872))
        x, y, w, h = render_bounds(self.scene, "content", margin=10)
        # ink spans x 602..1002; the typed text starts at x 234 (-468 + 702)
        self.assertEqual((x, y, w, h), (224, 40, 788, 470))
        off_page = PageScene(version=5, layers=[])
        off_page.layers.append(type(self.scene.layers[0])(strokes=[
            self.stroke(Tool.FINELINER, points=[Point(-50, 2500, width=1)])]))
        self.assertEqual(render_bounds(off_page, margin=0), (-50, 0, 1454, 2500))
        self.assertEqual(render_bounds(PageScene(version=6), "content"), (0, 0, 1404, 1872))
        near_edge = PageScene(version=5, layers=[])
        near_edge.layers.append(type(self.scene.layers[0])(strokes=[
            self.stroke(Tool.FINELINER, points=[Point(1400, 1870, width=1), Point(2, 3, width=1)])]))
        self.assertEqual(render_bounds(near_edge), (0, 0, 1404, 1872))   # close to edges: no growth
        above = PageScene(version=5, layers=[])
        above.layers.append(type(self.scene.layers[0])(strokes=[
            self.stroke(Tool.FINELINER, points=[Point(10, -100, width=1), Point(20, 50, width=1)])]))
        self.assertEqual(render_bounds(above, margin=0), (0, -100, 1404, 1972))   # grows upwards too

    def test_paint_layers(self):
        from PySide6.QtGui import QImage, QPainter
        from viewer.renderer import paint
        image = QImage(100, 100, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        try:
            with self.assertRaisesRegex(ValueError, "unknown layers"):
                paint(painter, self.scene, layers="ink")
            for layers in ("darken", "normal"):
                paint(painter, self.scene, layers=layers, blend_modes=False)
        finally:
            painter.end()
        with self.assertRaises(ValueError):
            render_bounds(self.scene, "bogus")

    def test_clip_mode_is_exactly_the_page(self):
        long_page = v6_file(root_text([text_item((1, 1), ZERO, text="\n" * 60 + "far below")], []),
                            line_item(LAYER_A, (1, 2), tool=17, color=0,
                                      points=points_v2([(-900, 3000, 0, 0, 3, 255), (900, 3100, 0, 0, 3, 255)])))
        scene = parse(long_page)
        self.assertGreater(render_bounds(scene)[3], 3000)              # faithful default grows
        self.assertEqual(render_bounds(scene, "clip"), (0, 0, 1404, 1872))
        img = render_image(scene, scale=0.25, crop="clip")
        self.assertEqual((img.width(), img.height()), (351, 468))     # 3:4
        svg = ET.fromstring(render_svg(scene, crop="clip"))
        self.assertEqual(svg.get("viewBox"), "0 0 1404 1872")

    def test_svg(self):
        svg = render_svg(self.scene)
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        layers = [g for g in root.findall(f"{ns}g") if g.get("id", "").startswith("layer")]
        self.assertEqual([g.get("data-name") for g in layers], ["Sketch"])   # hidden "Notes" skipped
        tools = [g.get("data-tool") for g in layers[0]]
        self.assertEqual(tools, ["fineliner_v2", "highlighter_v2"])
        hl = layers[0][1]
        self.assertEqual((hl.get("stroke"), hl.get("stroke-linecap")), ("#ffed75", "square"))
        self.assertIn("darken", hl.get("style"))
        self.assertIsNone(hl[0].get("stroke-opacity"))
        first = layers[0][0][0]
        self.assertEqual(first.get("points").split()[0], "602.00,200.00")
        all_layers = ET.fromstring(render_svg(self.scene, include_hidden=True, background=None))
        self.assertEqual(len([g for g in all_layers.findall(f"{ns}g")
                              if g.get("id", "").startswith("layer")]), 2)
        self.assertIsNone(all_layers.find(f"{ns}rect"))

    def test_non_ink_tools_are_not_drawn(self):
        data = v6_file(tree_node(LAYER_A, "L"), group_item(ROOT, (1, 3), LAYER_A),
                       line_item(LAYER_A, (1, 4), tool=8, color=0,      # erase section
                                 points=points_v2([(0, 100, 0, 0, 40, 255), (100, 100, 0, 0, 40, 255)])))
        scene = parse(data)
        layer = ET.fromstring(render_svg(scene)).find("{http://www.w3.org/2000/svg}g[@id='layer-0']")
        self.assertEqual(list(layer), [])
        img = render_image(scene, scale=0.5)
        self.assertEqual(img.pixelColor(376, 50).lightness(), 255)

    def test_text_layout_wraps_and_marks_lists(self):
        items = [text_item((1, 1), ZERO, text="one two three four five six seven eight\nnext\nlast")]
        formats = [paragraph_format((1, 40), 9), paragraph_format((1, 45), 6)]
        scene = parse(v6_file(root_text(items, formats, width=300)))
        lines = layout_text(scene)
        texts = ["".join(t for t, _ in line.pieces) for line in lines]
        self.assertEqual(texts[-2:], ["next", "last"])
        self.assertGreater(len(lines), 3)                        # first paragraph wrapped
        self.assertTrue(all(estimate_width(t, f) <= 300 for line in lines[:-2] for t, f in line.pieces))
        self.assertEqual((lines[-2].marker, lines[-1].marker), ("1.", "☑"))
        self.assertEqual(lines[0].x, 234)                        # centred x -468 + 702
        baselines = [line.baseline for line in lines]
        self.assertEqual(baselines, sorted(baselines))

    def test_layout_whitespace_edges(self):
        # Leading space dropped; a bold word followed by a plain space and a wrap
        # leaves no whitespace-only piece at the end of the line.
        items = [text_item((1, 1), ZERO, fmt=1), text_item((1, 2), (1, 1), text=" boldword"),
                 text_item((1, 11), (1, 10), fmt=2), text_item((1, 12), (1, 11), text=" " + "w" * 30)]
        lines = layout_text(parse(v6_file(root_text(items, [], width=400))))
        self.assertEqual([[(t, f.bold) for t, f in line.pieces] for line in lines],
                         [[("boldword", True)], [("w" * 30, False)]])

    def test_list_markers(self):
        cases = {3: "•", 4: "•", 5: "☐", 7: "☐", 8: "☑", 9: "1.", 10: "1.", 0: ""}
        for style, marker in cases.items():
            scene = parse(v6_file(root_text([text_item((1, 1), ZERO, text="x")],
                                            [paragraph_format(ZERO, style)])))
            self.assertEqual(layout_text(scene)[0].marker, marker, style)
        indented = parse(v6_file(root_text([text_item((1, 1), ZERO, text="x")],
                                           [paragraph_format(ZERO, 4)])))
        self.assertEqual(layout_text(indented)[0].x, 234 + 96)

    def test_text_in_svg_and_image(self):
        scene = parse(v6_file(sample_text_block()))
        root = ET.fromstring(render_svg(scene))
        ns = "{http://www.w3.org/2000/svg}"
        texts = root.find(f"{ns}g[@id='text']").findall(f"{ns}text")
        spans = ["".join(ts.text for ts in t.findall(f"{ns}tspan")) for t in texts]
        self.assertIn("TiXtle", spans)
        self.assertIn("•", [t.text for t in texts])
        bold = [ts for t in texts for ts in t.findall(f"{ns}tspan") if ts.text == "Hel"]
        self.assertEqual(bold[0].get("font-weight"), "bold")
        self.assertIn("reMarkable Sans", bold[0].get("font-family"))

        img = render_image(scene, scale=0.5)
        title_band = [img.pixelColor(x, y).lightness() for x in range(117, 260) for y in range(118, 150)]
        self.assertLess(min(title_band), 100)                    # title glyphs drawn
        self.assertEqual(img.pixelColor(650, 900).lightness(), 255)

    def test_highlighter_darken_keeps_ink(self):
        ink_then_hl = v6_file(
            tree_node(LAYER_A, "L"), group_item(ROOT, (1, 2), LAYER_A),
            line_item(LAYER_A, (1, 3), tool=17, color=0, points=points_v2([(0, 300, 0, 0, 8, 255), (0, 400, 0, 0, 8, 255)])),
            line_item(LAYER_A, (1, 4), tool=18, color=3, points=points_v2([(-60, 350, 0, 0, 40, 255), (60, 350, 0, 0, 40, 255)])))
        img = render_image(parse(ink_then_hl))
        self.assertEqual(img.pixelColor(702, 350).getRgb()[:3], (0, 0, 0))          # ink stays black
        self.assertEqual(img.pixelColor(702 + 40, 350).getRgb()[:3], (255, 237, 117))  # exact yellow

    def test_svg_escapes_layer_names(self):
        data = v6_file(tree_node(LAYER_A, 'a<b>&"c'), group_item(ROOT, (1, 3), LAYER_A))
        root = ET.fromstring(render_svg(parse(data)))
        self.assertEqual(root.find("{http://www.w3.org/2000/svg}g").get("data-name"), 'a<b>&"c')

    def test_qimage_pixels(self):
        img = render_image(self.scene, scale=0.5)
        self.assertEqual((img.width(), img.height()), (702, 936))
        px = lambda x, y: img.pixelColor(round(x * 0.5), round(y * 0.5))
        ink = px(602, 200)                       # fineliner start (page coords)
        self.assertLess(ink.lightness(), 80)
        hl = px(702 + 150, 500)                  # middle of the highlighter
        self.assertGreater(hl.red(), 200)
        self.assertLess(hl.blue(), hl.red() - 30)  # yellow tint over white
        self.assertEqual(px(752, 50).lightness(), 255)  # hidden pencil layer not drawn
        shown = render_image(self.scene, scale=0.5, include_hidden=True)
        self.assertLess(shown.pixelColor(376, 25).lightness(), 255)


if __name__ == "__main__":
    unittest.main()
