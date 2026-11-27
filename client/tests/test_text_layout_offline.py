"""Offline tests for typed-text layout as reMarkable.exe 3.29 lays it out (ARCHITECTURE.md
5.1, "The text layout it hangs on"): the QTextDocument's formats, the list markers, and
the anchor position strokes anchored to the text are drawn from (the baseline of the
visual line holding the anchor character; sentinels on the first / last line), and
that pasting or transforming an anchored group stores points the tablet draws in the
same place. All ids are synthetic.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from rmfixtures import (  # noqa: E402
    ROOT, ZERO, group_item, line_item, paragraph_format, points_v2, root_text, text_item, tree_node, v6_file)
from viewer import renderer as R  # noqa: E402
from viewer import rm_anchor, rm_select, rm_stroke  # noqa: E402
from viewer.rm_parser import ANCHOR_SENTINEL_START, ParagraphStyle as P, parse  # noqa: E402

APP = QApplication.instance() or QApplication([])
R.ensure_fonts()
FONTS = (Path(__file__).resolve().parents[1] / "src" / "assets" / "fonts" / "reMarkableSans-Regular.ttf").exists()
needs_fonts = unittest.skipUnless(FONTS, "the app's fonts are not installed (install.py)")

AUTHOR = bytes(range(16))
LAYER = (0, 11)
GROUP = (1, 500)
STROKE = (1, 510)
BEFORE, AFTER = (0, ANCHOR_SENTINEL_START), (0, ANCHOR_SENTINEL_START + 1)
# Legacy style bytes (rmfixtures.paragraph_format): 1 body, 2 title, 3 subheading 2,
# 4 bullet, 5 indented bullet, 6/7 checkbox, 10/11 numbered; subheading needs base 2 level 3.
WRAPPED = "Hello " + "word " * 60


def styled_page(paragraphs: list[tuple[str, int | tuple]], *, extra=b"", y: float = 234.0) -> bytes:
    """A page whose text is `paragraphs` (text, style), one text item from id 1:1."""
    text = "\n".join(t for t, _ in paragraphs)
    formats, at = [], 0
    for n, (t, style) in enumerate(paragraphs):
        char = ZERO if n == 0 else (1, 1 + at - 1)          # the newline that starts it
        if isinstance(style, tuple):
            formats.append(paragraph_format(char, 1, base=style[0], properties=style[1]))
        else:
            formats.append(paragraph_format(char, style))
        at += len(t) + 1
    return v6_file(tree_node(ROOT), tree_node(LAYER, "Layer 1", True), group_item(ROOT, (1, 400), LAYER),
                   root_text([text_item((1, 1), ZERO, text=text)], formats, y=y), extra)


def anchored(char_id, origin_x: float = -464.0, kind: int = 2, points=((10.0, -5.0), (60.0, 5.0)),
             paragraphs=((WRAPPED, 1),), y: float = 234.0) -> bytes:
    """styled_page with a group anchored to `char_id` holding one stroke."""
    pts = points_v2([(x, py, 0, 0, 3, 255) for x, py in points])
    extra = (tree_node(GROUP, "", True, anchor=(char_id, origin_x, 0.0 if kind == 1 else 35.75, kind))
             + group_item(LAYER, (1, 401), GROUP)
             + line_item(GROUP, STROKE, tool=17, color=0, points=pts))
    return styled_page(list(paragraphs), extra=extra, y=y)


def offset(data: bytes, measure=R.qt_measure) -> tuple[float, float]:
    scene = parse(data)
    (stroke,) = [s for s in scene.strokes if s.anchor is not None]
    return R.stroke_offsets(scene, R.layout_text(scene, measure), measure)[id(stroke)]


class LayoutTests(unittest.TestCase):
    """The QTextDocument 3.29 builds (0x1409247b0, 0x140a3c480, 0x140a3c280)."""

    def lines(self, paragraphs, measure=R.qt_measure, **kw):
        return R.layout_text(parse(styled_page(paragraphs, **kw)), measure)

    def test_body_text_metrics(self):
        for measure in (R.qt_measure, R.estimate_width):
            a, b = self.lines([("a", 1), ("b", 1)], measure)
            self.assertEqual((a.x, b.x), (238.0, 238.0))                 # box x 234 + margin 4
            self.assertAlmostEqual(a.baseline, 234 + 4 + 29.59375, places=4)
            self.assertAlmostEqual(b.baseline - a.baseline, 69.546875, places=4)   # the app's body pitch
            self.assertEqual(a.height, 38.0)
            self.assertEqual(a.pieces[0][1], R.TextFont(32, family=R.SANS, weight=400))

    @needs_fonts
    def test_style_fonts_and_top_margins(self):
        lines = self.lines([("x", 1), ("T", 2), ("S", (2, 3)), ("U", 3), ("y", 1)])
        fonts = [l.pieces[0][1] for l in lines]
        self.assertEqual([(f.family, f.size, f.weight) for f in fonts],
                         [(R.SANS, 32, 400), (R.SERIF, 64, 300), (R.SERIF, 46, 400), (R.SANS, 36, 500),
                          (R.SANS, 32, 400)])
        # Tops: the body line 38 + 0.2 x 37.75 (7.546875 in 1/64 px), then each block's
        # top margin (32 x 2.75 / 1.75 / 1.25 / 0.75); headings add no line distance.
        tops = [238.0, 238 + 38 + 7.546875 + 88]
        tops.append(tops[-1] + 76 + 56)
        tops.append(tops[-1] + 55 + 40)
        tops.append(tops[-1] + 43 + 24)
        for line, top in zip(lines, tops):
            self.assertAlmostEqual(line.top, top, places=4)
        self.assertEqual([l.height for l in lines], [38.0, 76.0, 55.0, 43.0, 38.0])
        self.assertAlmostEqual(lines[1].ascent, 59.1875, places=4)

    def test_the_first_block_has_no_top_margin(self):
        # Checked on real pages: an empty Title box at y 234 has its baseline at
        # 234 + 4 + 59.1875 (ARCHITECTURE.md 5.1).
        for measure in (R.qt_measure, R.estimate_width):
            (line,) = self.lines([("", 2)], measure)
            self.assertAlmostEqual(line.baseline, 297.1875, places=4)

    def test_wrapped_lines(self):
        for measure in (R.qt_measure, R.estimate_width):
            body = self.lines([(WRAPPED, 1)], measure)
            self.assertGreater(len(body), 2)
            for a, b in zip(body, body[1:]):
                self.assertAlmostEqual(b.baseline - a.baseline, 45.546875, places=4)   # 38 + 7.546875
            self.assertEqual(body[0].start, 0)
            self.assertEqual([l.start for l in body[1:]], [l.end for l in body[:-1]])
            self.assertEqual(body[-1].end, len(WRAPPED))
            # Lines wrap inside the box less the margins (936 - 8).
            self.assertTrue(all(sum(measure(t, f) for t, f in l.pieces) <= 928 for l in body))
        title = self.lines([("Title " * 30, 2)])
        self.assertGreater(len(title), 1)
        self.assertAlmostEqual(title[1].baseline - title[0].baseline, 76.0, places=4)   # no line distance

    def test_estimate_agrees_with_qt_vertically(self):
        paras = [("x", 1), ("T", 2), ("S", (2, 3)), ("U", 3), ("y", 4), ("", 1)]
        qt, est = self.lines(paras), self.lines(paras, R.estimate_width)
        self.assertEqual(len(qt), len(est))
        for a, b in zip(qt, est):
            self.assertAlmostEqual(a.top, b.top, delta=0.01)
            self.assertAlmostEqual(a.baseline, b.baseline, delta=0.05)

    def test_list_indents_and_markers(self):
        # Text indent (level + 1) x 48 (0x140a3d3df); markers drawn at level x 48 in
        # from the block's x (0x14093bb80): a bullet's glyph at +20, a checkbox's 40 x 40
        # box 2 above the line, a number ending at +30.
        lines = self.lines([("b", 4), ("i", 5), ("c", 6), ("n", 10), ("m", 10), ("a", 11)])
        self.assertEqual([l.x for l in lines], [238 + 48, 238 + 96, 238 + 48, 238 + 48, 238 + 48, 238 + 96])
        self.assertEqual([l.marker for l in lines], ["•", "◦", "☐", "1.", "2.", "a."])
        bullet, indented, box, one, two, letter = lines
        from PySide6.QtGui import QFontMetrics
        int_ascent = QFontMetrics(R._qfont(R.TextFont(32))).ascent()
        self.assertEqual(bullet.marker_at, (238 + 20, bullet.top + int_ascent))
        self.assertEqual(indented.marker_at, (238 + 48 + 20, indented.top + int_ascent))
        self.assertEqual(box.marker_at, (238, box.top - 2, 40, 40))
        width = QFontMetrics(R._qfont(R.TextFont(32))).boundingRect("1.").width()
        self.assertEqual(one.marker_at, (238 + 30 - width, one.top + int_ascent))
        boxes = R.checkbox_boxes(parse(styled_page([("b", 4), ("c", 6)])), self.lines([("b", 4), ("c", 6)]))
        self.assertEqual(boxes, [(2, (238.0, self.lines([("b", 4), ("c", 6)])[1].top - 2, 40.0, 40.0))])

    def test_a_checked_checkbox_is_struck_out(self):
        (line,) = self.lines([("done", 7)])
        self.assertTrue(line.pieces[0][1].strike)
        self.assertIn('text-decoration="line-through"', R.render_svg(parse(styled_page([("done", 7)]))))

    def test_a_state_3_checkbox_is_struck_out_and_marked_checked(self):
        # 0x1408b5d20: state 3 displays like checked (state 1), although the legacy byte says unchecked.
        (line,) = self.lines([("done", (3, 0x310))])
        self.assertTrue(line.pieces[0][1].strike)
        self.assertEqual(line.marker, "☑")
        (line,) = self.lines([("open", (3, 0x210))])
        self.assertFalse(line.pieces[0][1].strike)
        self.assertEqual(line.marker, "▣")      # state 2 is indeterminate in 3.29 (0x1408b5ef0), not struck out

    @needs_fonts
    def test_the_apps_fonts_are_registered(self):
        from PySide6.QtGui import QFontDatabase, QFontInfo
        self.assertIn(R.SANS, QFontDatabase.families())
        self.assertIn(R.SERIF, QFontDatabase.families())
        self.assertEqual(QFontInfo(R._qfont(R.TextFont(64, family=R.SERIF, weight=300))).family(), R.SERIF)


class AnchorTests(unittest.TestCase):
    """The anchor position (0x14097c4f0): the baseline of the visual line holding the
    character; x the stored anchorOriginX (type 2) or the character's x (type 1)."""

    def wrapped_lines(self):
        scene = parse(styled_page([(WRAPPED, 1)]))
        return R.layout_text(scene, R.qt_measure)

    def test_a_character_on_a_later_visual_line(self):
        lines = self.wrapped_lines()
        third = lines[2]
        index = third.start + 2
        for measure in (R.qt_measure, R.estimate_width):
            scene = parse(anchored((1, 1 + index)))
            laid = R.layout_text(scene, measure)
            line = laid[R.line_of(laid, index)]
            self.assertEqual(offset(anchored((1, 1 + index)), measure), (-464.0, line.baseline))
        self.assertNotAlmostEqual(offset(anchored((1, 1 + index)))[1], lines[0].baseline)
        self.assertAlmostEqual(offset(anchored((1, 1 + index)))[1], third.baseline)

    def test_sentinels_take_the_first_and_last_lines(self):
        lines = self.wrapped_lines()
        self.assertEqual(offset(anchored(BEFORE)), (-464.0, lines[0].baseline))
        self.assertEqual(offset(anchored(AFTER)), (-464.0, lines[-1].baseline))
        # An empty Title box: the before-text group hangs on its baseline, 63.19 below the
        # box top that the fitted model used (the drift H1 of the 2026-11-27 audit).
        self.assertAlmostEqual(offset(anchored(BEFORE, paragraphs=(("", 2),)))[1], 297.1875, places=4)

    def test_a_bidirectional_anchor_follows_its_character_sideways(self):
        lines = self.wrapped_lines()
        index = lines[1].start + 3
        x, y = offset(anchored((1, 1 + index), origin_x=0.0, kind=1))
        self.assertAlmostEqual(x, R.caret_x(lines[1], index, R.qt_measure) - 702, places=4)
        self.assertAlmostEqual(y, lines[1].baseline, places=4)

    def test_svg_and_image_draw_anchored_ink_at_the_anchor(self):
        data = anchored(BEFORE, paragraphs=(("", 2),))
        scene = parse(data)
        (stroke,) = scene.strokes
        dx, dy = R.stroke_offsets(scene, R.layout_text(scene))[id(stroke)]
        runs = R.stroke_runs(scene, stroke, (dx, dy))
        self.assertAlmostEqual(runs[0].points[0][1], 297.1875 - 5, places=3)


def f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


class StoredCoordinateTests(unittest.TestCase):
    """What is written for an anchored group is relative to the anchor position 3.29
    computes, so the tablet draws it where it was put here."""

    @staticmethod
    def text_page() -> bytes:
        from viewer import rm_text, rm_writer as w
        return rm_text.edit_text(w.blank_page(AUTHOR), author=AUTHOR,
                                 change=lambda text, clock: text.insert(0, WRAPPED, clock))

    @staticmethod
    def char_at(page: bytes, index: int):
        return next(c for c, i in parse(page).text.char_index.items() if i == index)

    def drawn(self, page: bytes) -> list[tuple[float, float]]:
        scene = parse(page)
        offsets = R.stroke_offsets(scene, R.layout_text(scene, R.qt_measure), R.qt_measure)
        return [(p.x + offsets[id(s)][0], p.y + offsets[id(s)][1]) for s in scene.strokes if s.anchor is not None
                for p in s.points]

    def paste(self, page: bytes, index: int, wanted):
        """`wanted` (scene points) pasted into a new group anchored to character `index`."""
        scene = parse(page)
        lines = R.layout_text(scene, R.qt_measure)
        char = self.char_at(page, index)
        ax, ay = R.anchor_point(scene, lines, char, R.qt_measure)
        page, node = rm_anchor.add_group(page, author=AUTHOR, layer=None, char_id=char, origin_x=ax)
        stored = rm_select.clone(rm_stroke.draw(wanted, 17), rm_select.translation(-ax, -ay))
        page, ids = rm_select.append_items(page, author=AUTHOR, values=[stored], layer=node)
        return page, node, ids, (ax, ay), lines

    def test_pasting_into_a_new_group_on_a_later_line(self):
        page = self.text_page()
        lines = R.layout_text(parse(page), R.qt_measure)
        index = lines[3].start + 1
        y = lines[3].baseline
        wanted = [(-100.0, y - 12.0), (40.0, y + 3.0)]
        page, _, _, (ax, ay), _ = self.paste(page, index, wanted)
        self.assertAlmostEqual(ay, lines[3].baseline)
        self.assertNotAlmostEqual(ay, lines[0].baseline)
        for (x, y), (wx, wy) in zip(self.drawn(page), wanted):
            self.assertAlmostEqual(x, f32(wx), delta=0.01)
            self.assertAlmostEqual(y, f32(wy), delta=0.01)

    def test_anchor_under_uses_the_line_under_the_items(self):
        from core.edits import text_of
        from ui import textedit as T
        page = self.text_page()
        done = text_of(page, [], AUTHOR)[1]
        session = T.TextSession.open("doc", "page", done.page, done, AUTHOR, parse(done.page), R.qt_measure,
                                     lambda: 1)
        lines = session.lines()
        target = lines[2]
        x = R.caret_x(target, target.start + 4, R.qt_measure) - 702 + 1
        char, origin_x, y = T.anchor_under(session, x, target.top + 5)
        self.assertAlmostEqual(y, target.baseline)
        self.assertNotAlmostEqual(y, lines[0].baseline)
        # Above the text: the before-text sentinel on the first line, at the text's x.
        char, origin_x, y = T.anchor_under(session, 0.0, lines[0].top - 50)
        self.assertEqual(char, ("page", *rm_anchor.BEFORE_TEXT))
        self.assertEqual((origin_x, y), (lines[0].x - 702, lines[0].baseline))
        char, origin_x, y = T.anchor_under(session, 0.0, lines[-1].bottom + 50)
        self.assertEqual(char, ("page", *rm_anchor.AFTER_TEXT))
        self.assertAlmostEqual(y, lines[-1].baseline)

    def test_moving_and_scaling_an_anchored_stroke(self):
        page = self.text_page()
        lines = R.layout_text(parse(page), R.qt_measure)
        y = lines[2].baseline
        page, node, ids, anchor, _ = self.paste(page, lines[2].start, [(0.0, y - 10.0), (50.0, y - 10.0)])
        before = self.drawn(page)
        moved, _, _ = rm_select.transform_items(page, author=AUTHOR, items=ids, m=rm_select.translation(25.0, -30.0),
                                                anchors={node: anchor})
        for (x0, y0), (x1, y1) in zip(before, self.drawn(moved)):
            self.assertAlmostEqual(x1 - x0, 25.0, delta=0.01)
            self.assertAlmostEqual(y1 - y0, -30.0, delta=0.01)
        cx, cy = before[0]
        scaled, _, _ = rm_select.transform_items(page, author=AUTHOR, items=ids, m=rm_select.scaling(cx, cy, 2.0, 2.0),
                                                 anchors={node: anchor})
        for (x0, y0), (x1, y1) in zip(before, self.drawn(scaled)):
            self.assertAlmostEqual(x1, cx + 2 * (x0 - cx), delta=0.02)
            self.assertAlmostEqual(y1, cy + 2 * (y0 - cy), delta=0.02)


if __name__ == "__main__":
    unittest.main()
