"""Template text items and PDF text-highlight annotations, as reMarkable.exe 3.29 does
both (ARCHITECTURE.md §5.3: "Text items", parser 0x140a8e190, drawer 0x140a91750; "Text
highlights in a PDF export", setting Experimental/AnnotatePdfs).

Synthetic data only; nothing leaves the process.
Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from viewer import renderer  # noqa: E402
from viewer.rm_parser import GlyphRange, parse  # noqa: E402
from viewer.templates import (TemplateError, TemplateText, estimate_text_width, paint_template,  # noqa: E402
                              parse_template, qt_text_metrics, template_shapes, template_svg)

AREA = (0, 0, 300, 200)


def template(*items) -> object:
    return parse_template(json.dumps({"formatVersion": 1, "constants": [], "items": list(items)}))


def text_item(**kw) -> dict:
    return {"type": "text", "text": "H", "fontSize": 40, "position": {"x": 20, "y": 100}, **kw}


def fixed(width: int, calls: list | None = None):
    def metrics(text, size, bold, italic):
        if calls is not None:
            calls.append((text, size, bold, italic))
        return width
    return metrics


def shapes(*items, metrics=None) -> list:
    return list(template_shapes(template(*items), AREA, text_metrics=metrics or fixed(10)))


def image_of(item: dict) -> QImage:
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    paint_template(painter, template(item), AREA)
    painter.end()
    return image


def ink(image: QImage, test=lambda c: c.lightness() < 160) -> list[tuple[int, int]]:
    return [(x, y) for y in range(image.height()) for x in range(image.width()) if test(image.pixelColor(x, y))]


class TemplateTextParseTests(unittest.TestCase):
    def test_defaults(self):
        [t] = shapes(text_item())
        self.assertEqual(t, TemplateText("H", 40, 20.0, 100.0, False, False, 0xFF000000, 0.0))

    def test_errors(self):
        for override, message in (
                ({"text": ""}, "cannot be empty"), ({"text": None}, "cannot be empty"), ({"text": 5}, "cannot be empty"),
                ({"fontSize": 0}, "positive"), ({"fontSize": -3}, "positive"), ({"fontSize": 12.5}, "positive"),
                ({"fontSize": "20"}, "positive"), ({"fontSize": True}, "positive"),
                ({"position": None}, "'position' is empty or missing"), ({"position": [1, 2]}, "'position'"),
                ({"color": "red"}, "invalid color"), ({"color": 5}, "invalid color"), ({"color": "#12345"}, "invalid color"),
                ({"bold": 1}, "boolean value expected"), ({"bold": "true"}, "boolean value expected"),
                ({"italic": None}, "boolean value expected"),
                ({"rotate": 361}, r"within \+/- 0-360"), ({"rotate": -360.5}, r"within \+/- 0-360"),
                ({"rotate": "10"}, "number")):
            with self.subTest(override=override), self.assertRaisesRegex(TemplateError, message):
                shapes(text_item(**override))
        with self.assertRaisesRegex(TemplateError, "'position'"):
            shapes({"type": "text", "text": "a", "fontSize": 10})
        for position in ({}, {"x": 1}, {"y": 1}):       # both x and y must evaluate
            with self.subTest(position=position), self.assertRaisesRegex(TemplateError, "'position' needs"):
                shapes(text_item(position=position))

    def test_visible_on_every_item_type(self):
        path = {"type": "path", "data": ["M", 0, 0, "L", 10, 10]}
        group = {"type": "group", "boundingBox": {"x": 0, "y": 0, "width": 10, "height": 10}, "children": [path]}
        for make in (lambda **kw: dict(path, **kw), lambda **kw: dict(group, **kw), lambda **kw: text_item(**kw)):
            def count(**kw):
                return len(shapes(make(**kw)))
            self.assertEqual(count(), 1)
            self.assertEqual(count(visible=True), 1)
            self.assertEqual(count(visible=False), 0)
            self.assertEqual(count(visible=7), 1)                  # neither bool nor string: visible
            self.assertEqual(count(visible=None), 1)
            self.assertEqual(count(visible="1"), 1)
            self.assertEqual(count(visible="0"), 0)
            self.assertEqual(count(visible="0.000001"), 0)         # |v| <= 1e-5
            self.assertEqual(count(visible="-1"), 1)
            self.assertEqual(count(visible="templateWidth > 1000"), 1)
            self.assertEqual(count(visible="templateWidth < 1000"), 0)

    def test_colour(self):
        self.assertEqual(shapes(text_item(color="#336699"))[0].argb, 0xFF336699)           # #RRGGBB: opaque
        self.assertEqual(shapes(text_item(color="#80336699"))[0].argb, 0x80336699)         # #AARRGGBB verbatim
        self.assertEqual(shapes(text_item(color="#ABCDEF"))[0].argb, 0xFFABCDEF)
        self.assertEqual(shapes(text_item(color=None))[0].argb, 0)                         # null: transparent
        self.assertEqual(shapes(text_item())[0].argb, 0xFF000000)                          # absent: opaque black

    def test_bold_italic_rotate(self):
        t = shapes(text_item(bold=True, italic=True, rotate=-90))[0]
        self.assertEqual((t.bold, t.italic, t.rotate), (True, True, -90.0))
        self.assertEqual(shapes(text_item(rotate=360))[0].rotate, 360.0)                  # the limit is allowed
        self.assertEqual(shapes(text_item(rotate=0.5))[0].rotate, 0.5)

    def test_text_width_is_bound_before_the_position_and_uses_the_font(self):
        calls: list = []
        [t] = shapes(text_item(text="Hello", fontSize=24, bold=True, italic=True,
                               position={"x": "templateWidth / 2 - textWidth / 2", "y": "textWidth"}),
                     metrics=fixed(100, calls))
        self.assertEqual(calls, [("Hello", 24, True, True)])
        self.assertEqual((t.x, t.y), (702 - 50, 100))

    def test_group_offsets_move_the_baseline_start_only(self):
        group = {"type": "group", "boundingBox": {"x": 5, "y": 7, "width": 100, "height": 100}, "children": [text_item()]}
        [t] = shapes(group)
        self.assertEqual((t.x, t.y), (25, 107))

    def test_the_text_width_is_the_whole_pixel_bounding_rect_in_remarkable_sans(self):
        QApplication.instance() or QApplication([])
        renderer.ensure_fonts()
        for text, size, bold, italic in (("Week", 72, False, False), ("DEADLINE", 24, True, False), ("Wi", 32, False, True)):
            font = QFont("reMarkable Sans")
            font.setPixelSize(size)
            font.setWeight(QFont.Weight(700 if bold else 400))
            font.setItalic(italic)
            expected = QFontMetrics(font).boundingRect(text).width()
            width = qt_text_metrics(text, size, bold, italic)
            self.assertIsInstance(width, int)
            self.assertEqual(width, expected, (text, size, bold, italic))
        self.assertGreater(qt_text_metrics("Week", 72), qt_text_metrics("Week", 36))

    def test_the_headless_estimate_is_whole_pixels(self):
        self.assertEqual(estimate_text_width("abcd", 20), 44)
        self.assertEqual(estimate_text_width("abcd", 20, bold=True), 48)
        self.assertIsInstance(estimate_text_width("abc", 25), int)


class TemplateTextDrawTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_y_is_the_baseline_with_no_ascent_shift(self):
        # "H" has no descender: all its ink is above the baseline y = 100, and reaches
        # up towards it (the old client drew it with y as the top, so below 100).
        pixels = ink(image_of(text_item()))
        self.assertTrue(pixels)
        self.assertLessEqual(max(y for _, y in pixels), 101)
        self.assertGreater(max(y for _, y in pixels), 95)
        self.assertGreaterEqual(min(x for x, _ in pixels), 18)
        self.assertLess(min(y for _, y in pixels), 100 - 20)

    def test_colour_and_transparency(self):
        red = ink(image_of(text_item(color="#ff0000")), lambda c: c.red() > 200 and c.green() < 60)
        self.assertTrue(red)
        self.assertFalse(ink(image_of(text_item(color=None))))                             # transparent: nothing

    def test_bold_and_italic_change_the_drawing(self):
        plain = image_of(text_item())
        bold = image_of(text_item(bold=True))
        italic = image_of(text_item(italic=True))
        self.assertGreater(len(ink(bold)), len(ink(plain)))
        self.assertNotEqual(italic, plain)

    def test_rotate_turns_about_the_baseline_start(self):
        pixels = ink(image_of(text_item(position={"x": 150, "y": 50}, rotate=90)))
        self.assertTrue(pixels)
        # 90 degrees clockwise: the glyph's up points to +x and its run to +y from (150, 50).
        self.assertGreaterEqual(min(x for x, _ in pixels), 148)
        self.assertGreaterEqual(min(y for _, y in pixels), 48)
        self.assertGreater(max(x for x, _ in pixels), 165)


class TemplateTextSvgTests(unittest.TestCase):
    def svg(self, **kw) -> str:
        return "\n".join(template_svg(template(text_item(**kw)), AREA, text_metrics=fixed(10)))

    def test_baseline_text(self):
        svg = self.svg()
        self.assertIn('<text x="20" y="100" ', svg)
        self.assertIn('font-size="40" font-weight="400" font-style="normal" fill="#000000">H</text>', svg)
        self.assertIn("'reMarkable Sans'", svg)
        self.assertNotIn("transform", svg)
        self.assertNotIn("fill-opacity", svg)

    def test_bold_italic_colour_opacity(self):
        svg = self.svg(bold=True, italic=True, color="#80ff0000")
        self.assertIn('font-weight="700" font-style="italic" fill="#ff0000" fill-opacity="0.502"', svg)

    def test_transparent_colour(self):
        self.assertIn('fill-opacity="0"', self.svg(color=None))

    def test_rotate(self):
        svg = self.svg(position={"x": 150, "y": 50}, rotate=90)
        self.assertIn('<text x="0" y="0" transform="translate(150 50) rotate(90)" ', svg)

    def test_text_is_escaped(self):
        self.assertIn(">a &lt; b</text>", self.svg(text="a < b"))


# ------------------------------------------------------------------ PDF text-highlight annotations

def _export_fixtures():
    import test_pdf_highlights_offline as th
    from ui.pages import PageContent
    scene = parse(th.highlighted_page())
    return th, scene, PageContent(scene, None, None)


class AnnotatePdfsSettingTests(unittest.TestCase):
    def test_the_setting_defaults_to_false(self):
        from ui.export import ANNOTATE_PDFS_KEY, annotate_pdfs_enabled
        self.assertEqual(ANNOTATE_PDFS_KEY, "Experimental/AnnotatePdfs")

        class Store(dict):
            pass
        self.assertFalse(annotate_pdfs_enabled(None))
        self.assertFalse(annotate_pdfs_enabled(Store()))
        for value, expected in ((True, True), ("true", True), ("1", True), ("false", False), (False, False), ("", False)):
            self.assertEqual(annotate_pdfs_enabled(Store({"Experimental/AnnotatePdfs": value})), expected, value)

    def test_reads_the_apps_settings_store(self):
        from ui.export import annotate_pdfs_enabled
        from ui.quick.bridge.platform import SettingsStore
        store = SettingsStore()
        self.assertFalse(annotate_pdfs_enabled(store))
        store.set("Experimental/AnnotatePdfs", True)
        self.assertTrue(annotate_pdfs_enabled(store))

    def test_the_worker_default_is_off(self):
        from ui.workers import Exporter
        QApplication.instance() or QApplication([])
        self.assertFalse(Exporter(object()).annotate_pdfs())


class AnnotatedExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.th, cls.scene, cls.content = _export_fixtures()

    def export(self, **kw):
        from pypdf import PdfReader

        from ui.export import export_pdf
        content = self.content

        class Pages:
            pages, pdf, pdf_path = [object()], None, None
            doc = type("Doc", (), {"name": "Synthetic"})()

            def content(self, index):
                return content
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = export_pdf(Pages(), Path(tmp.name) / "out.pdf", **kw)
        return PdfReader(str(out)).pages[0]

    def glyphs(self) -> list[GlyphRange]:
        return [i for layer in self.scene.layers for i in layer.items() if isinstance(i, GlyphRange)]

    def test_default_draws_rectangles_and_no_annotations(self):
        from ui.export import _draw_layers

        page = self.export()
        annots = [a.get_object() for a in page["/Annots"]] if "/Annots" in page else []   # QPdfWriter may leave an empty array
        self.assertEqual([a for a in annots if a.get("/Subtype") == "/Highlight"], [])
        # the rectangles are in the Darken layer
        class Pages:
            pages = [object()]

            def content(self, index):
                return self_content
        self_content = self.content
        _, [placed] = _draw_layers(Pages(), None, 1.0, None, [0])
        self.assertTrue(placed.drawn["darken"])
        self.assertEqual(placed.highlights, [])

    def test_annotation_mode_adds_one_highlight_per_glyph_range_and_draws_no_rectangles(self):
        from ui.export import PT_PER_PX, _draw_layers

        class Pages:
            pages = [object()]

            def content(self, index):
                return self_content
        self_content = self.content
        _, [placed] = _draw_layers(Pages(), None, 1.0, None, [0], True)
        self.assertFalse(placed.drawn["darken"])                  # no rectangles drawn
        self.assertTrue(placed.drawn["normal"])                   # the stroke still is
        self.assertEqual(len(placed.highlights), 2)

        page = self.export(annotate=True)
        annots = [a.get_object() for a in page["/Annots"]]
        self.assertEqual(len(annots), 2)
        glyphs = self.glyphs()
        bx, by = self.content.bounds("page")[:2]
        top = float(page.mediabox.top)
        for annot, glyph, argb in zip(annots, glyphs, (self.th.YELLOW, 0xFFACFF85)):
            self.assertEqual(annot["/Subtype"], "/Highlight")
            self.assertEqual([round(float(v), 4) for v in annot["/C"]],
                             [round(((argb >> s) & 255) / 255, 4) for s in (16, 8, 0)])
            self.assertAlmostEqual(float(annot["/CA"]), 1.0)
            expected = []
            for x, y, w, h in glyph.rects:
                px, py = self.scene.to_page(x, y)
                for cx, cy in ((px, py), (px + w, py), (px, py + h), (px + w, py + h)):     # TL, TR, BL, BR
                    expected += [(cx - bx) * PT_PER_PX, top - (cy - by) * PT_PER_PX]
            quad = [float(v) for v in annot["/QuadPoints"]]
            self.assertEqual(len(quad), 8 * len(glyph.rects))
            for got, want in zip(quad, expected):
                self.assertAlmostEqual(got, want, delta=0.01)
            # corner order: TL, TR (same y, larger x), BL (below TL), BR
            self.assertGreater(quad[2], quad[0])
            self.assertAlmostEqual(quad[3], quad[1], places=3)
            self.assertLess(quad[5], quad[1])
            self.assertAlmostEqual(quad[4], quad[0], places=3)
            rect = [float(v) for v in annot["/Rect"]]
            self.assertAlmostEqual(rect[0], min(quad[0::2]), places=3)
            self.assertAlmostEqual(rect[1], min(quad[1::2]), places=3)
            self.assertAlmostEqual(rect[2], max(quad[0::2]), places=3)
            self.assertAlmostEqual(rect[3], max(quad[1::2]), places=3)

    def test_alpha_goes_to_ca(self):
        from viewer.rm_parser import Color
        glyph = self.glyphs()[1]
        old = (glyph.color, glyph.argb)
        glyph.color, glyph.argb = Color.ARGB, 0x80102030
        self.addCleanup(lambda: setattr(glyph, "color", old[0]) or setattr(glyph, "argb", old[1]))
        annot = [a.get_object() for a in self.export(annotate=True)["/Annots"]][1]
        self.assertAlmostEqual(float(annot["/CA"]), 128 / 255, places=3)
        self.assertEqual([round(float(v) * 255) for v in annot["/C"]], [0x10, 0x20, 0x30])


if __name__ == "__main__":
    unittest.main()
