"""3.29's strokev2 Shader (tool 23) and v2 pencils (13, 14), shaded per pixel
(ARCHITECTURE.md 5.2, "strokev2 Shader and v2 pencil"; viewer/brushes.py). Exact integer
results of the blenders, the value noise and FillPencil's threshold, then the renderer:
a Shader stroke multiplied over a white page, a v2 pencil of pure colour, the SVG's raster
images. Synthetic data only.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from viewer import brushes, renderer
from viewer.rm_parser import Color, Layer, PageScene, Point, Stroke, Tool

WHITE = 0xFFFFFFFF
BLUE = 0xFF304AE0            # Color.BLUE in the palette


def pixels(image: QImage) -> memoryview:
    return memoryview(image.constBits()).cast("B").cast("I")


def scene_of(tool: Tool, points: list[Point], color: Color = Color.BLUE) -> PageScene:
    return PageScene(version=5, layers=[Layer(strokes=[Stroke(tool=tool, color=color, points=points)])])


def stroke_points(width: float = 20.0, pressure: float = 1.0, speed: float = 0.0) -> list[Point]:
    return [Point(100, 100, speed=speed, width=width, pressure=pressure),
            Point(300, 100, speed=speed, width=width, pressure=pressure)]


def white_page(w: int = 400, h: int = 200, fmt=QImage.Format.Format_ARGB32_Premultiplied) -> QImage:
    image = QImage(w, h, fmt)
    image.fill(0xFFFFFFFF)
    return image


def painted(scene: PageScene, image: QImage) -> QImage:
    painter = QPainter(image)
    try:
        renderer.paint(painter, scene)
    finally:
        painter.end()
    return image


class TableTests(unittest.TestCase):
    def test_the_permutation_table_is_stored_twice(self):
        # 0x140e8e1d0: 177da134 ... 201605
        self.assertEqual(len(brushes.PERM), 512)
        self.assertEqual(brushes.PERM[:256], brushes.PERM[256:])
        self.assertEqual(brushes.PERM[:4], (0x17, 0x7D, 0xA1, 0x34))
        self.assertEqual(brushes.PERM[253:256], (0x20, 0x16, 0x05))
        self.assertEqual((brushes.PERM[23], brushes.PERM[24], brushes.PERM[26], brushes.PERM[30]), (0x0A, 0xC0, 0x04, 0x23))


class BlenderTests(unittest.TestCase):
    GRAY = (255, 144, 144, 144)          # premultiplied (A, R, G, B)

    def test_the_composite_divide_is_exact_at_the_ends(self):
        self.assertEqual([brushes.div255(x) for x in (0, 255, 32640, 65025)], [0, 1, 128, 255])

    def test_multiply_over_white(self):
        self.assertEqual(brushes.shade_pixel(WHITE, self.GRAY, 255, False), 0xFF909090)
        self.assertEqual(brushes.shade_pixel(WHITE, self.GRAY, 128, False), 0xFFC7C7C7)   # 199
        self.assertEqual(brushes.shade_pixel(WHITE, self.GRAY, 0, False), WHITE)

    def test_a_dark_destination_takes_less_with_the_luminance_weighting(self):
        dark = 0xFF333333
        self.assertEqual(brushes.shade_pixel(dark, self.GRAY, 255, False), 0xFF2E2E2E)    # c weighted to 51
        self.assertEqual(brushes.shade_pixel(dark, self.GRAY, 255, True), 0xFF1C1C1C)     # SHADING_NORMAL_BLEND
        self.assertEqual(brushes.shade_pixel(0xFF000000, self.GRAY, 255, False), 0xFF000000)
        self.assertEqual(brushes.luminance_weighted(255, 255, 51, 51, 51), 51)
        self.assertEqual(brushes.luminance_weighted(255, 255, 255, 255, 255), 255)
        self.assertEqual(brushes.luminance_weighted(200, 0, 0, 0, 0), 200)                 # nothing under it

    def test_normal_blend_follows_the_environment(self):
        dark = 0xFF333333
        with mock.patch.dict(os.environ, {brushes.SHADING_NORMAL_BLEND: "1"}):
            self.assertTrue(brushes.normal_blend_set())
            self.assertEqual(brushes.shade_pixel(dark, self.GRAY, 255), 0xFF1C1C1C)
        with mock.patch.dict(os.environ, {brushes.SHADING_NORMAL_BLEND: ""}):
            self.assertFalse(brushes.normal_blend_set())                                  # empty: unset
            self.assertEqual(brushes.shade_pixel(dark, self.GRAY, 255), 0xFF2E2E2E)

    def test_a_white_tint_is_additive(self):
        tint = (100, 100, 100, 100)
        dst = (200 << 24) | (50 << 16) | (60 << 8) | 70
        self.assertEqual(brushes.shade_pixel(dst, tint, 128), (250 << 24) | (100 << 16) | (110 << 8) | 120)
        self.assertEqual(brushes.shade_pixel(dst, tint, 255), (255 << 24) | (150 << 16) | (160 << 8) | 170)
        self.assertEqual(brushes.shade_pixel(dst, tint, 0), dst)

    def test_the_dither_masks_the_coverage_with_the_table(self):
        # c &= perm[perm[y & 255] + (x & 255)] | 0xF0: perm[23 + x] along y = 0.
        self.assertEqual([brushes.dither(255, x, 0) for x in range(4)], [0xFA, 0xF0, 0xF8, 0xF4])
        self.assertEqual(brushes.dither(0x0F, 1, 0), 0)
        self.assertEqual(brushes.dither(255, 256, 256), brushes.dither(255, 0, 0))

    def test_the_dithered_variant_in_a_region(self):
        for dithered, expected in ((False, [40] * 4), (True, [39, 37, 38, 38])):
            image = QImage(4, 1, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            brushes.shade_region(image, b"\xff" * 4, b"\xff" * 4, (40, 40, 40, 40), dithered=dithered)
            self.assertEqual([v & 255 for v in pixels(image)], expected)
            self.assertEqual([v >> 24 for v in pixels(image)], expected)

    def test_the_opacity_factor_curve(self):
        # 0.8 + 0.2 (1 - smoothstep(clamp((k - 2) / 8))), k = speed / 4.
        for k, factor in ((1, 1.0), (2, 1.0), (6, 0.9), (10, 0.8), (12, 0.8)):
            self.assertAlmostEqual(brushes.shader_opacity(k), factor, places=9, msg=f"k={k}")
        self.assertAlmostEqual(brushes.shader_opacity(4), 0.8 + 0.2 * (1 - 0.15625), places=9)   # t = 0.25
        self.assertGreater(brushes.shader_opacity(3), brushes.shader_opacity(5))


class PencilTests(unittest.TestCase):
    def test_value_noise(self):
        # perm[perm[b & 255] + (a & 255)]; along b = 0 that is perm[23 + a].
        self.assertEqual([brushes.value_noise(a, 0) for a in range(4)], [0x0A, 0xC0, 0x88, 0x04])
        self.assertEqual(brushes.value_noise(256, 0), 0x0A)
        self.assertEqual((brushes.value_noise(-1, 0), brushes.value_noise(255, 0)), (0xFD, 0xFD))
        self.assertEqual(brushes.value_noise(0.5, 0), (10 * 127 + 192 * 128) // 255)     # 101
        self.assertEqual(brushes.value_noise(0.5, 0), 101)
        self.assertEqual(brushes.value_noise(1.25, 0), (192 * 191 + 136 * 64) // 255)    # 177
        self.assertEqual(brushes.value_noise(1.25, 0), 177)

    def test_the_pencil_noise_sums_four_scales(self):
        self.assertEqual(brushes.pencil_noise(0, 0, 0, 0), 1 + (60 // 6 * 7 >> 3))        # 9
        self.assertEqual(brushes.pencil_noise(0, 0, 0, 0), 9)
        self.assertEqual(brushes.pencil_noise(1, 0, 0, 0), (192 >> 3) + 8)                # 32
        self.assertEqual(brushes.pencil_noise(0, 0, 1, 0), 1 + ((35 + 3 * 4 + 2 * 192) // 6 * 7 >> 3))   # 63
        self.assertEqual(brushes.pencil_noise(0, 0, 1, 0), 63)

    def test_the_density_falls_off_toward_the_edge(self):
        self.assertEqual(brushes.pencil_density(100, 2, 2), 100)         # h <= 2: d
        self.assertEqual(brushes.pencil_density(100, 10, 0), 100)
        self.assertEqual(brushes.pencil_density(100, 10, 5), 100)        # |v| / h = 0.5
        self.assertEqual(brushes.pencil_density(100, 10, -7.5), 50)      # 1 - smoothstep(0.5)
        self.assertEqual(brushes.pencil_density(100, 10, 10), 0)
        self.assertEqual(brushes.pencil_density(100, 10, 12), 0)

    def test_the_threshold_is_n_at_most_d(self):
        self.assertTrue(brushes.pencil_hit(0, 0, 0, 0, 9, 1))            # n = 9
        self.assertFalse(brushes.pencil_hit(0, 0, 0, 0, 8, 1))
        self.assertTrue(brushes.pencil_hit(1, 0, 0, 0, 32, 1))           # n = 32
        self.assertFalse(brushes.pencil_hit(1, 0, 0, 0, 31, 1))
        self.assertTrue(brushes.pencil_hit(0, 0, 1, 0, 63, 1))           # n = 63
        self.assertFalse(brushes.pencil_hit(0, 0, 1, 0, 62, 1))
        self.assertFalse(brushes.pencil_hit(0, 10, 0, 0, 200, 10))       # on the edge: d' = 0

    def test_a_v2_pencil_is_pure_colour_or_untouched(self):
        for tool in (Tool.PENCIL_V2, Tool.SHARP_PENCIL_V2):
            points = [Point(60 + 10 * n, 100 + (n % 3), width=8, pressure=0.2) for n in range(29)]
            image = painted(scene_of(tool, points, Color.BLACK), white_page())
            seen = {image.pixel(x, y) for x in range(image.width()) for y in range(image.height())}
            self.assertEqual(seen, {WHITE, 0xFF000000}, tool.name)       # no anti-aliasing, no tint
            inside = [image.pixel(x, 101) for x in range(80, 330)]
            self.assertIn(0xFF000000, inside)
            self.assertIn(WHITE, inside)                                  # density 68: a grain, not a bar
            outside = [image.pixel(x, y) for x in range(image.width()) for y in (60, 140)]
            self.assertEqual(set(outside), {WHITE})
            self.assertTrue(all(image.pixel(x, 101) == WHITE for x in (20, 40, 350, 380)))

    def test_a_v2_pencil_stroke_has_no_tile_and_full_opacity(self):
        scene = scene_of(Tool.PENCIL_V2, stroke_points(width=8, pressure=0.1), Color.BLACK)
        [run] = renderer.stroke_runs(scene, scene.layers[0].strokes[0])
        self.assertEqual((run.opacity, run.texture), (1.0, None))
        legacy = scene_of(Tool.PENCIL, stroke_points(width=8, pressure=0.1), Color.BLACK)
        [run] = renderer.stroke_runs(legacy, legacy.layers[0].strokes[0])
        self.assertLess(run.opacity, 1.0)                                  # the legacy pencil keeps its tiles' model


class ShaderRenderTests(unittest.TestCase):
    def test_a_shader_stroke_multiplies_toward_its_colour_inside_its_bounds_only(self):
        scene = scene_of(Tool.SHADING_MARKER, stroke_points(width=20))
        image = painted(scene, white_page())
        self.assertEqual(image.pixel(200, 100), BLUE)                      # c = 255 over white: the colour
        self.assertEqual(image.pixel(200, 95), BLUE)
        changed = {(x, y) for x in range(image.width()) for y in range(image.height())
                   if image.pixel(x, y) != WHITE}
        self.assertTrue(changed)
        xs, ys = [x for x, _ in changed], [y for _, y in changed]
        self.assertGreaterEqual(min(xs), 99)                               # stroke 100..300 x 90..110
        self.assertLessEqual(max(xs), 301)
        self.assertGreaterEqual(min(ys), 89)
        self.assertLessEqual(max(ys), 111)
        self.assertEqual(image.pixel(200, 120), WHITE)
        self.assertEqual(image.pixel(50, 100), WHITE)

    def test_a_fast_stroke_is_lighter(self):
        slow = painted(scene_of(Tool.SHADING_MARKER, stroke_points(speed=0.0)), white_page())
        fast = painted(scene_of(Tool.SHADING_MARKER, stroke_points(speed=12.0)), white_page())   # k = 12: 0.8
        self.assertEqual(slow.pixel(200, 100), BLUE)
        lighter = fast.pixelColor(200, 100)
        self.assertGreater(lighter.red(), 0x30)
        self.assertGreater(lighter.green(), 0x4A)
        self.assertEqual(lighter.alpha(), 255)

    def test_it_blends_with_what_is_below(self):
        image = white_page()
        image.fill(0xFF808080)
        painted(scene_of(Tool.SHADING_MARKER, stroke_points()), image)
        pixel = image.pixelColor(200, 100)
        self.assertLess(pixel.red(), 0x80)                                 # darker than the page
        self.assertEqual(image.pixel(200, 140), 0xFF808080)
        self.assertEqual(pixels(image)[100 * 400 + 50], 0xFF808080)

    def test_a_clip_is_honoured(self):
        from PySide6.QtGui import QPainterPath
        image = white_page()
        painter = QPainter(image)
        try:
            clip = QPainterPath()
            clip.addRect(0, 0, 200, 200)
            painter.setClipPath(clip)
            renderer.paint(painter, scene_of(Tool.SHADING_MARKER, stroke_points()))
        finally:
            painter.end()
        self.assertEqual(image.pixel(150, 100), BLUE)
        self.assertEqual(image.pixel(250, 100), WHITE)

    def test_a_scale_is_followed(self):
        image = white_page(200, 100)
        painter = QPainter(image)
        try:
            painter.scale(0.5, 0.5)
            renderer.paint(painter, scene_of(Tool.SHADING_MARKER, stroke_points()))
        finally:
            painter.end()
        self.assertEqual(image.pixel(100, 50), BLUE)
        self.assertEqual(image.pixel(100, 70), WHITE)

    def test_a_painter_that_cannot_be_read_back_multiplies_the_stroke_image(self):
        pixmap = QPixmap(400, 200)
        pixmap.fill(QColor("white"))
        painter = QPainter(pixmap)
        try:
            renderer.paint(painter, scene_of(Tool.SHADING_MARKER, stroke_points()))
        finally:
            painter.end()
        got = pixmap.toImage().pixelColor(200, 100)
        for have, want in zip((got.red(), got.green(), got.blue()), (0x30, 0x4A, 0xE0)):
            self.assertAlmostEqual(have, want, delta=2)
        self.assertEqual(pixmap.toImage().pixelColor(50, 100).name(), "#ffffff")


class SvgTests(unittest.TestCase):
    def test_per_pixel_tools_are_embedded_as_png_images(self):
        for tool in (Tool.SHADING_MARKER, Tool.SHARP_PENCIL_V2, Tool.PENCIL_V2):
            scene = scene_of(tool, stroke_points(width=12, pressure=0.5), Color.BLACK)
            svg = renderer.render_svg(scene)
            root = ET.fromstring(svg)
            ns = "{http://www.w3.org/2000/svg}"
            images = list(root.iter(f"{ns}image"))
            self.assertEqual(len(images), 1, tool.name)
            group = [g for g in root.iter(f"{ns}g") if g.get("data-tool") == tool.name.lower()]
            self.assertEqual(len(group), 1)
            self.assertEqual("multiply" in (group[0].get("style") or ""), tool == Tool.SHADING_MARKER)
            self.assertEqual(list(root.iter(f"{ns}polyline")), [])         # nothing vector for it
            href = images[0].get("href")
            self.assertTrue(href.startswith("data:image/png;base64,"))
            decoded = QImage.fromData(base64.b64decode(href.split(",", 1)[1]))
            self.assertEqual((decoded.width(), decoded.height()),
                             (int(images[0].get("width")), int(images[0].get("height"))))
            made = renderer.stroke_raster(scene, scene.layers[0].strokes[0],
                                          renderer.stroke_style(scene.layers[0].strokes[0]))
            image, x, y = made
            self.assertEqual((int(images[0].get("x")), int(images[0].get("y"))), (x, y))
            self.assertEqual((decoded.width(), decoded.height()), (image.width(), image.height()))
            self.assertTrue(all(decoded.pixelColor(px, py).alpha() == image.pixelColor(px, py).alpha()
                                for px in range(image.width()) for py in range(image.height())))   # the PNG is the image
            self.assertTrue(any(image.pixelColor(px, py).alpha() for px in range(image.width())
                                for py in range(image.height())))
            self.assertLessEqual(x, 100)
            self.assertGreaterEqual(x + image.width(), 300)

    def test_the_other_tools_stay_vector(self):
        scene = scene_of(Tool.FINELINER, stroke_points(width=4), Color.BLACK)
        svg = renderer.render_svg(scene)
        self.assertIn("<polyline", svg)
        self.assertNotIn("<image", svg)


if __name__ == "__main__":
    unittest.main()
