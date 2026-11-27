"""Parity milestone highlights-and-lists-m1-m2 (ARCHITECTURE.md 5.11 and 5.1 text mode):

- `{doc}.highlights/{page}.json`, rebuilt from the page each time the client writes it
  (0x1405a8760) in QJsonDocument::toJson(Indented)'s bytes, and removed when no highlight is left;
- autoformat of "[] " and "1. " (and "* ") at a paragraph start (0x14094d080);
- numbered-list numbering (0x140943ca0), roman level 3 labels, checkbox state 2
  (indeterminate, 0x1408b5ef0).

Synthetic documents only; nothing leaves the process (the fake cloud).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fakecloud import FakeCloud, paired_auth  # noqa: E402
from rmfixtures import ZERO, paragraph_format, root_text, text_item, v6_file  # noqa: E402

import test_pdf_edit_offline as tp  # noqa: E402
import test_pdf_highlights_offline as th  # noqa: E402
from test_textedit_offline import ME, session, styled, typed  # noqa: E402

from core.edits import PageHistory, document_files, page_with_edits, swapped  # noqa: E402
from core.highlights import highlights_json, qt_json  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from viewer import renderer, rm_layer, rm_stroke as s, rm_writer as w  # noqa: E402
from viewer.rm_parser import Paragraph, ParagraphStyle, checkbox_state, parse  # noqa: E402

DOC, P0, P2 = th.HL_DOC, th.P0, th.P2
KEPT = b"{\"kept\": true}\n"


def json_name(page_id: str) -> str:
    return f"{DOC}.highlights/{page_id}.json"


# ------------------------------------------------------------------ the Qt JSON writer

class QtJsonTests(unittest.TestCase):
    def test_indentation_sorted_keys_and_empty_containers(self):
        text = qt_json({"b": [1, 2], "a": {}, "B": []})
        self.assertEqual(text, '{\n    "B": [\n    ],\n    "a": {\n    },\n    "b": [\n        1,\n        2\n    ]\n}\n')
        self.assertEqual(qt_json({}), "{\n}\n")

    def test_numbers_are_integral_or_the_shortest_round_trip(self):
        values = [12.0, -3.0, 0.5, 0.1, 70.25, 1e-05, 1.5e20, 0.0001, 123456.789]
        out = qt_json({"v": values}).split("\n")[2:-3]
        self.assertEqual([line.strip().rstrip(",") for line in out],
                         ["12", "-3", "0.5", "0.1", "70.25", "1e-05", "1.5e+20", "0.0001", "123456.789"])
        self.assertIn('"n": 7\n', qt_json({"n": 7}))

    def test_strings_escape_quotes_backslashes_and_controls_only(self):
        text = qt_json({"s": "q\"b\\ \b\f\n\r\t\x01\x1f \u00e9\u20ac\U0001f600"})
        self.assertIn('"s": "q\\"b\\\\ \\b\\f\\n\\r\\t\\u0001\\u001f \u00e9\u20ac\U0001f600"', text)


# ------------------------------------------------------------------ the page's highlights

def two_layer_page() -> bytes:
    """A page with two layers: two highlights in the first, nothing in the second."""
    header, blocks = w.split(w.blank_page(tp.TABLET))
    first = th.glyph_value([(10.0, 20.0, 300.0, 40.0), (10.5, 70.25, 220.0, 40.0)], color=3, start=10,
                           text="say \"hi\"\n\u20ac")
    second = th.glyph_value([(-5.0, 0.1, 1.5, 2.0)], color=9, start=7, text="b\x01")
    page = (w.join(header, blocks) + th.glyph_block((1, 16), first) + th.glyph_block((1, 17), second, left=(1, 16)))
    return rm_layer.add_layer(page, author=tp.TABLET)[0]


EXPECTED = """{
    "highlights": [
        [
            {
                "color": 3,
                "length": 10,
                "rects": [
                    {
                        "height": 40,
                        "width": 300,
                        "x": 10,
                        "y": 20
                    },
                    {
                        "height": 40,
                        "width": 220,
                        "x": 10.5,
                        "y": 70.25
                    }
                ],
                "start": 10,
                "text": "say \\"hi\\"\\n\u20ac"
            },
            {
                "color": 9,
                "length": 2,
                "rects": [
                    {
                        "height": 2,
                        "width": 1.5,
                        "x": -5,
                        "y": 0.1
                    }
                ],
                "start": 7,
                "text": "b\\u0001"
            }
        ],
        [
        ]
    ]
}
"""


class HighlightsJsonTests(unittest.TestCase):
    def test_a_two_layer_page_gives_qts_exact_bytes(self):
        self.assertEqual(highlights_json(two_layer_page()), EXPECTED.encode("utf-8"))

    def test_a_page_without_glyph_ranges_has_no_file(self):
        self.assertIsNone(highlights_json(w.blank_page(tp.TABLET)))


class HighlightFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = FakeCloud()
        self.cloud.put_item(DOC, tp.meta(th.NAMES[0]),
                            tp.pdf_content("pdf", [tp.page_entry(th.P0, "ba", 0), tp.page_entry(th.P1, "bb", None, "Blank"),
                                                   tp.page_entry(th.P2, "bc", 2)], 3),
                            {f"{DOC}.pdf": tp.make_pdf(), f"{DOC}/{P0}.rm": th.highlighted_page(),
                             json_name(P0): b"{}\n", json_name(P2): KEPT})
        self.sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        self.source = LibrarySource(self.sync, editable=th.NAMES)
        self.source.refresh()

    def files(self) -> dict:
        doc = self.sync.tree.get(DOC)
        return document_files(doc, self.sync.edits.of(DOC), lambda name: self.source.blob(doc, name),
                              lambda e: b"", self.sync.author())

    def erase(self, rects, when):
        return self.source.erase_strokes(self.sync.tree.get(DOC), P0, s.eraser_line(th.across(rects), s.MEDIUM), when)

    def test_a_written_page_gets_its_highlights_and_other_pages_keep_theirs(self):
        self.source.add_stroke(self.sync.tree.get(DOC), P0, tp.line([(0, 0), (9, 9)]), 1000)
        files = self.files()
        written = files[json_name(P0)]
        self.assertEqual(written, highlights_json(files[f"{DOC}/{P0}.rm"]))
        self.assertEqual(len(parse(files[f"{DOC}/{P0}.rm"]).layers[0].glyphs), 2)
        self.assertNotIn(json_name(P2), files)              # a page not written: its file is left as it is

    def test_the_file_is_removed_when_the_last_highlight_is_erased(self):
        self.erase(th.highlight_rects(), 1000)
        files = self.files()
        self.assertEqual(files[json_name(P0)], highlights_json(files[f"{DOC}/{P0}.rm"]))
        self.assertEqual(len(parse(files[f"{DOC}/{P0}.rm"]).layers[0].glyphs), 1)
        self.erase(th.far_rects(), 1001)
        files = self.files()
        self.assertIn(json_name(P0), files)
        self.assertIsNone(files[json_name(P0)])             # removed, not written
        self.assertNotIn(json_name(P2), files)


# ------------------------------------------------------------------ autoformat

class AutoformatTests(unittest.TestCase):
    def test_a_checkbox_and_a_numbered_list_at_the_start_of_a_paragraph(self):
        s_ = session()
        typed(s_, "x\n[] item")
        self.assertEqual(styled(s_)[1], ("CHECKBOX_UNCHECKED", [("item", False, False)]))
        t = session()
        typed(t, "x\n1. item")
        self.assertEqual(styled(t)[1], ("NUMBERED_LIST", [("item", False, False)]))
        self.assertEqual(t.text.text, "x\nitem")
        u = session()
        typed(u, "x\n- item")
        self.assertEqual(styled(u)[1][0], "BULLETPOINT")

    def test_the_conversion_is_one_undo_step_that_restores_the_text(self):
        page = w.blank_page(ME)
        s_ = session(page)
        typed(s_, "x\n[] ")
        edits, steps = s_.take()
        shown = lambda e: [(p.style.name, p.text) for p in parse(page_with_edits(page, e, ME)).text.paragraphs]
        self.assertEqual(shown(edits), [("TITLE", "x"), ("CHECKBOX_UNCHECKED", "")])
        history = PageHistory(None)
        for step in steps:
            history.push(step)
        more, _ = swapped(history.undo.pop(), "doc", "page", 2, history.aliases)
        self.assertEqual(shown(edits + more), [("TITLE", "x"), ("PARAGRAPH", "[] ")])

    def test_not_in_the_middle_of_a_paragraph_and_not_on_a_list(self):
        s_ = session()
        typed(s_, "x\nab [] ")
        self.assertEqual(styled(s_)[1], ("PARAGRAPH", [("ab [] ", False, False)]))
        t = session()
        typed(t, "x\n1.5 ")
        self.assertEqual(styled(t)[1][0], "PARAGRAPH")
        u = session()
        typed(u, "x\ny")
        u.cycle_style(ParagraphStyle.BULLETPOINT)
        u.place(2)
        typed(u, "1. ")
        self.assertEqual(styled(u)[1][0], "BULLETPOINT")
        self.assertEqual(u.text.text, "x\n1. y")             # the word is kept


# ------------------------------------------------------------------ numbering and checkbox states

def numbered(level: int = 0) -> Paragraph:
    return Paragraph(ParagraphStyle.NUMBERED_LIST_INDENTED, indent=level) if level else Paragraph(ParagraphStyle.NUMBERED_LIST)


BULLET, TEXT = Paragraph(ParagraphStyle.BULLETPOINT), Paragraph(ParagraphStyle.PARAGRAPH)


class NumberingTests(unittest.TestCase):
    def test_a_deeper_sub_list_does_not_reset_the_count(self):
        paragraphs = [numbered(), numbered(), numbered(1), numbered(1), numbered()]
        self.assertEqual(renderer.list_numbers(paragraphs), [1, 2, 1, 2, 3])

    def test_bullets_and_text_between_numbered_items_restart_it(self):
        self.assertEqual(renderer.list_numbers([numbered(), numbered(), BULLET, numbered(), TEXT, numbered(),
                                                numbered()]), [1, 2, -1, 1, -1, 1, 2])

    def test_a_shallower_item_stops_the_walk(self):
        self.assertEqual(renderer.list_numbers([numbered(1), numbered(), numbered(1)]), [1, 1, 1])

    def test_it_wraps_to_1_when_it_reaches_1000(self):
        states = renderer.list_numbers([numbered() for _ in range(1002)])
        self.assertEqual((states[998], states[999], states[1000], states[1001]), (999, 1, 2, 3))

    def test_labels_by_level_mod_3(self):
        self.assertEqual([renderer._number_label(4, level) for level in range(4)], ["4.", "d.", "iv.", "4."])
        self.assertEqual(renderer._number_label(27, 1), "aa.")
        self.assertEqual(renderer._number_label(1994, 2), "mcmxciv.")

    def test_a_level_two_paragraph_is_laid_out_and_labelled_from_the_stored_indent(self):
        legacy = 11                                          # numbered, indented
        page = v6_file(root_text([text_item((1, 1), ZERO, text="a\nb")],
                                 [paragraph_format(ZERO, legacy, base=3, properties=2 | 2 << 4),
                                  paragraph_format((1, 2), legacy, base=3, properties=2 | 2 << 4)]))
        scene = parse(page)
        self.assertEqual([p.indent for p in scene.text.paragraphs], [2, 2])
        lines = renderer.layout_text(scene)
        self.assertEqual([line.marker for line in lines], ["i.", "ii."])


def state_page() -> bytes:
    """Eight checkbox paragraphs, one per state 0..7."""
    text = "\n".join(str(n) for n in range(8))
    formats = [paragraph_format(ZERO if n == 0 else (1, 2 * n), 6, base=3, properties=0x10 | n << 8) for n in range(8)]
    return v6_file(root_text([text_item((1, 1), ZERO, text=text)], formats))


class CheckboxStateTests(unittest.TestCase):
    def test_states_choose_the_box_and_the_strike_out(self):
        scene = parse(state_page())
        self.assertEqual([p.state for p in scene.text.paragraphs], list(range(8)))
        lines = renderer.layout_text(scene)
        first = {line.paragraph: line for line in lines}
        markers = [first[n].marker for n in range(8)]
        unchecked, checked, indeterminate = renderer.CHECKBOX_MARKERS
        self.assertEqual(markers, [unchecked, checked, indeterminate, checked, unchecked, unchecked, unchecked,
                                   unchecked])
        struck = [first[n].pieces[0][1].strike for n in range(8)]
        self.assertEqual(struck, [False, True, False, True, False, False, False, False])

    def test_the_helper_reads_the_state_of_a_checkbox_only(self):
        self.assertEqual([checkbox_state(3, 0x10 | n << 8) for n in range(8)], list(range(8)))
        self.assertEqual((checkbox_state(3, 0x200), checkbox_state(2, 0x210)), (0, 0))   # a bullet, not a list

    def test_the_fallback_shapes(self):
        box = (0.0, 0.0, 40.0, 40.0)
        self.assertEqual([len(renderer._checkbox_shapes(box, c, i)[1]) for c, i in
                          ((False, False), (True, False), (False, True))], [0, 3, 2])
        svg = "\n".join(renderer.svg_scene(parse(state_page())))
        self.assertEqual(svg.count("<rect "), 8)             # a square per checkbox
        self.assertEqual(svg.count("<polyline "), 3)         # a tick (1, 3) or a bar (2)


if __name__ == "__main__":
    unittest.main()
