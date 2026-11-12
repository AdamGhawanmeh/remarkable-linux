"""Offline tests for typed text written and changed as reMarkable.exe does it
(viewer.rm_text): the sequence of runs, typing and deleting inside them, UTF-16 ids,
paragraph styles, bold and italic markers, the order two devices' text takes, and the
page around the text left as it was.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import os
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rmfixtures import ZERO, line_item, paragraph_format, points_v2, root_text, text_item  # noqa: E402

from viewer import rm_text, rm_writer as w  # noqa: E402
from viewer.rm_parser import (ParagraphStyle, RootText, StyleEntry, TextItem, decode_root_text, parse,  # noqa: E402
                              text_block)
from viewer.rm_text import BOLD, ITALIC, Clock, TextDocument, TextError, TextSequence  # noqa: E402

ME = bytes(range(16))
OTHER = bytes(range(16, 32))
LAYER = (0, 11)


def drawn_page() -> bytes:
    header, blocks = w.split(w.blank_page(ME))
    return w.join(header, blocks) + line_item(LAYER, (1, 15), tool=17, color=0,
                                              points=points_v2([(0, 0, 0, 0, 2, 255), (50, 50, 0, 0, 2, 255)]))


def doc_of(*items: bytes, formats=()) -> TextDocument:
    return TextDocument(decode_root_text(root_text(list(items), list(formats))[8:]))


def shape(doc: TextDocument) -> list[tuple]:
    """(id, left, right, what) of each run, in reading order."""
    out = []
    for run in doc.runs:
        what = f"del{run.deleted}" if run.deleted else f"fmt{run.format}" if run.format else run.text
        out.append((run.id, run.left, run.right, what))
    return out


def spans(page_or_doc) -> list[list[tuple]]:
    text = parse(page_or_doc).text if isinstance(page_or_doc, bytes) else text_block(page_or_doc.root_text())
    return [[(s.text, s.bold, s.italic) for s in p.spans] for p in text.paragraphs]


class NewTextTests(unittest.TestCase):
    def test_a_page_without_text_gets_a_box_as_the_app_makes_one(self):
        page = drawn_page()
        edited = rm_text.edit_text(page, author=ME, change=lambda d, c: d.insert(0, "Hello", c))
        header, blocks = w.split(edited)
        types = [b.type for b in blocks]
        self.assertEqual(types[types.index(w.BLOCK_TREE_MOVE) + 1], w.BLOCK_ROOT_TEXT)    # by the scene tree
        root = decode_root_text(blocks[types.index(w.BLOCK_ROOT_TEXT)].payload)
        # The box's style takes the first fresh id (1:16), the text the ones after it.
        self.assertEqual(root, RootText((0, 0), [TextItem((1, 17), ZERO, ZERO, 0, b"Hello", 1)],
                                        [StyleEntry(ZERO, (1, 16), 2)], -468.0, 234.0, 936.0))
        self.assertEqual(rm_text.encode_root_text(root), blocks[types.index(w.BLOCK_ROOT_TEXT)].payload)
        scene = parse(edited)
        self.assertEqual([(p.style, p.text) for p in scene.text.paragraphs], [(ParagraphStyle.TITLE, "Hello")])
        self.assertEqual(len(scene.strokes), 1)
        # Every other block as it was, but the text counts of PageStats.
        old = [b.encode() for b in w.split(page)[1] if b.type != w.BLOCK_PAGE_STATS]
        self.assertEqual([b.encode() for b in blocks if b.type not in (w.BLOCK_PAGE_STATS, w.BLOCK_ROOT_TEXT)], old)
        stats = [int.from_bytes(v, "little") for _, _, v in w.read_values(blocks[types.index(w.BLOCK_PAGE_STATS)].payload)]
        self.assertEqual(stats[2:4], [6, 1])                    # textChars: length + 1, textLines
        self.assertEqual(w.highest_counter(blocks), 21)

    def test_a_page_with_no_file_and_an_unknown_author(self):
        edited = rm_text.edit_text(None, author=OTHER, change=lambda d, c: d.insert(0, "x", c))
        self.assertEqual(parse(edited).text.text, "x")
        self.assertEqual(w.decode_authors(w.split(edited)[1][0].payload), [(OTHER, 1)])
        page = drawn_page()
        again = rm_text.edit_text(page, author=OTHER, change=lambda d, c: d.insert(0, "y", c))
        self.assertEqual(w.decode_authors(w.split(again)[1][0].payload), [(ME, 1), (OTHER, 2)])
        self.assertEqual(decode_root_text([b for b in w.split(again)[1] if b.type == 7][0].payload).items[0].item_id,
                         (2, 17))

    def test_a_change_that_changes_nothing_gives_the_page_back(self):
        page = rm_text.edit_text(drawn_page(), author=ME, change=lambda d, c: d.insert(0, "Hi", c))
        self.assertIs(rm_text.edit_text(page, author=OTHER, change=lambda d, c: None), page)
        self.assertIs(rm_text.edit_text(page, author=ME, change=lambda d, c: d.delete(0, 0)), page)


class RunTests(unittest.TestCase):
    def test_typing_inside_a_run_cuts_it(self):
        doc = doc_of(text_item((1, 20), ZERO, text="Hello"))
        first = doc.insert(2, "XY", Clock(1, 30))
        self.assertEqual(first, (1, 31))
        self.assertEqual(shape(doc), [((1, 20), ZERO, ZERO, "He"),
                                      ((1, 31), (1, 21), (1, 22), "XY"),
                                      ((1, 22), (1, 21), ZERO, "llo")])
        self.assertEqual(doc.text, "HeXYllo")
        # Written in reading order, which is how it is read back.
        again = TextDocument(decode_root_text(doc.encode()))
        self.assertEqual(shape(again), shape(doc))

    def test_typing_on_continues_the_run(self):
        doc = doc_of(text_item((1, 20), ZERO, text="Hello"))
        clock = Clock(1, 24)
        doc.insert(5, " you", clock)
        self.assertEqual(shape(doc), [((1, 20), ZERO, ZERO, "Hello you")])
        doc.insert(0, ">", clock)                           # at the start: a run of its own
        self.assertEqual(shape(doc)[0], ((1, 29), ZERO, (1, 20), ">"))
        other = Clock(2, 40)
        doc.insert(10, "!", other)                          # another author never joins my run
        self.assertEqual(shape(doc)[-1], ((2, 41), (1, 28), ZERO, "!"))

    def test_deleting_inside_a_run_leaves_a_tombstone(self):
        doc = doc_of(text_item((1, 20), ZERO, text="Hello"))
        self.assertEqual(doc.delete(1, 3), 3)
        self.assertEqual(shape(doc), [((1, 20), ZERO, ZERO, "H"), ((1, 21), (1, 20), ZERO, "del3"),
                                      ((1, 24), (1, 23), ZERO, "o")])
        self.assertEqual(doc.text, "Ho")
        items = decode_root_text(doc.encode()).items
        self.assertEqual(items[1], TextItem((1, 21), (1, 20), ZERO, 3))     # no value on disk, just its length
        self.assertIsNone(items[1].data)

    def test_backspacing_grows_the_tombstone(self):
        doc = doc_of(text_item((1, 20), ZERO, text="Hello"))
        doc.delete(3, 1)
        doc.delete(2, 1)
        doc.delete(1, 1)
        self.assertEqual(shape(doc), [((1, 20), ZERO, ZERO, "H"), ((1, 21), (1, 20), ZERO, "del3"),
                                      ((1, 24), (1, 23), ZERO, "o")])
        doc.delete(0, 1)
        self.assertEqual(shape(doc)[0], ((1, 20), ZERO, ZERO, "del4"))
        self.assertEqual(doc.text, "o")

    def test_deleting_across_runs_and_markers(self):
        doc = doc_of(text_item((1, 1), ZERO, text="ab"), text_item((1, 3), (1, 2), fmt=1),
                     text_item((1, 4), (1, 3), text="cd"), text_item((1, 6), (1, 5), fmt=2),
                     text_item((1, 7), (1, 6), text="ef"))
        doc.delete(1, 4)
        self.assertEqual(doc.text, "af")
        self.assertEqual([what for *_, what in shape(doc)], ["a", "del1", "fmt1", "del2", "fmt2", "del1", "f"])
        with self.assertRaises(TextError):
            doc.delete(1, 5)

    def test_positions_and_ids(self):
        doc = doc_of(text_item((1, 10), ZERO, text="ab"), text_item((1, 12), (1, 11), deleted=2),
                     text_item((1, 14), (1, 13), text="cd"))
        self.assertEqual([doc.id_at(n) for n in range(4)], [(1, 10), (1, 11), (1, 14), (1, 15)])
        self.assertEqual(doc.position_of((1, 15)), (3, True))
        self.assertEqual(doc.position_of((1, 13)), (2, False))     # deleted: where it was
        self.assertIsNone(doc.position_of((1, 99)))


class Utf16Tests(unittest.TestCase):
    def test_a_character_outside_the_bmp_takes_two_ids(self):
        doc = doc_of(text_item((1, 1), ZERO, text="a\U0001F600b"))
        self.assertEqual(doc.length, 4)
        self.assertEqual(doc.id_at(3), (1, 4))
        clock = Clock(1, 4)
        first = doc.insert(3, "\U0001F44D", clock)
        self.assertEqual((first, clock.highest), ((1, 5), 6))
        self.assertEqual(shape(doc)[1], ((1, 5), (1, 3), (1, 4), "\U0001F44D"))
        self.assertEqual(doc.text, "a\U0001F600\U0001F44Db")
        doc.insert(6, "c", clock)
        self.assertEqual(shape(doc)[-1], ((1, 7), (1, 4), ZERO, "bc")[:3] + ("c",))   # after b: its own run
        with self.assertRaisesRegex(TextError, "inside a character"):
            doc.insert(2, "x", clock)
        with self.assertRaisesRegex(TextError, "inside a character"):
            doc.delete(1, 1)
        doc.delete(1, 2)
        self.assertEqual(doc.text, "a\U0001F44Dbc")
        self.assertEqual(shape(doc)[1], ((1, 2), (1, 1), ZERO, "del2"))
        self.assertEqual(TextDocument(decode_root_text(doc.encode())).text, doc.text)

    def test_stats_count_units(self):
        doc = doc_of(text_item((1, 1), ZERO, text="\U0001F600\nx"))
        self.assertEqual(doc.stats(), (5, 2))


class ParagraphStyleTests(unittest.TestCase):
    def test_styles_are_kept_under_the_newline(self):
        doc = doc_of(text_item((1, 10), ZERO, text="Title\nBody\nItem"), formats=[paragraph_format(ZERO, 2)])
        self.assertEqual(doc.paragraph_keys(), [ZERO, (1, 15), (1, 20)])
        clock = Clock(1, 30)
        doc.set_style(2, ParagraphStyle.BULLETPOINT, clock)
        doc.set_style((1, 15), ParagraphStyle.SUBHEADING, clock)
        styles = decode_root_text(doc.encode()).styles
        self.assertEqual(styles, [StyleEntry(ZERO, (1, 1), 2), StyleEntry((1, 20), (1, 31), 4),
                                  StyleEntry((1, 15), (1, 32), 3, 2, 3)])
        self.assertEqual([p.style for p in text_block(doc.root_text()).paragraphs],
                         [ParagraphStyle.TITLE, ParagraphStyle.SUBHEADING, ParagraphStyle.BULLETPOINT])
        doc.set_style(0, ParagraphStyle.PARAGRAPH, clock)                  # in place, a new timestamp
        self.assertEqual(doc.styles[0], StyleEntry(ZERO, (1, 33), 1))
        with self.assertRaisesRegex(TextError, "does not start a paragraph"):
            doc.set_style((1, 11), ParagraphStyle.TITLE, clock)

    def test_what_each_type_stores(self):
        stored = {style.name: rm_text.stored_style(style) for style in ParagraphStyle}
        self.assertEqual(stored, {
            "TITLE": (2, None, None), "SUBHEADING_2": (3, None, None), "PARAGRAPH": (1, None, None),
            "BULLETPOINT": (4, None, None), "BULLETPOINT_INDENTED": (5, 3, 0x001),
            "CHECKBOX_UNCHECKED": (6, None, None), "CHECKBOX_CHECKED": (7, None, None),
            "CHECKBOX_UNCHECKED_INDENTED": (8, 3, 0x011), "CHECKBOX_CHECKED_INDENTED": (9, 3, 0x111),
            "NUMBERED_LIST": (10, None, None), "NUMBERED_LIST_INDENTED": (11, 3, 0x021),
            "IMAGE_BLOCK": (12, None, None), "SUBHEADING": (3, 2, 3)})
        for style in ParagraphStyle:                                       # and reads back as itself
            self.assertEqual(StyleEntry(ZERO, (1, 1), *rm_text.stored_style(style)).style, style)


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.doc = doc_of(text_item((1, 1), ZERO, text="one two three"))
        self.clock = Clock(1, 20)

    def test_a_range_gets_a_pair_of_markers(self):
        made = self.doc.format(4, 3, BOLD, self.clock)
        self.assertEqual(made, [(1, 21), (1, 22)])
        self.assertEqual([(i, what) for i, _, _, what in shape(self.doc)],
                         [((1, 1), "one "), ((1, 21), "fmt1"), ((1, 5), "two"), ((1, 22), "fmt2"), ((1, 8), " three")])
        self.assertEqual(spans(self.doc), [[("one ", False, False), ("two", True, False), (" three", False, False)]])
        self.assertTrue(self.doc.has_format(4, 3, BOLD))
        self.assertFalse(self.doc.has_format(3, 2, BOLD))
        self.assertEqual(self.doc.length, 13)                  # markers take no place in the text

    def test_text_styled_already_is_left_alone(self):
        self.doc.format(4, 3, BOLD, self.clock)
        self.doc.format(0, 13, BOLD, self.clock)
        codes = [what for *_, what in shape(self.doc) if what.startswith("fmt")]
        # Its own stretch is closed where the old one starts and opened again after it
        # ends: three pairs, none inside another (0x140aaee30, "marker end inserted").
        self.assertEqual(codes, ["fmt1", "fmt2", "fmt1", "fmt2", "fmt1", "fmt2"])
        ids = {r.id: r.format for r in self.doc.runs if r.format}
        for (author, counter), code in ids.items():          # every end right after its start
            if code == 2:
                self.assertEqual(ids[(author, counter - 1)], 1)
        self.assertEqual(spans(self.doc), [[("one two three", True, False)]])

    def test_bold_and_italic_overlap(self):
        self.doc.format(0, 7, BOLD, self.clock)
        self.doc.format(4, 9, ITALIC, self.clock)
        self.assertEqual(spans(self.doc), [[("one ", True, False), ("two", True, True), (" three", False, True)]])

    def test_taking_a_style_off_part_of_a_stretch(self):
        self.doc.format(0, 13, BOLD, self.clock)
        self.assertTrue(self.doc.unformat(4, 3, BOLD, self.clock))
        self.assertEqual(spans(self.doc), [[("one ", True, False), ("two", False, False), (" three", True, False)]])
        self.assertFalse(self.doc.unformat(4, 3, BOLD, self.clock))
        self.assertFalse(self.doc.toggle(0, 13, ITALIC, self.clock) is False)
        self.assertFalse(self.doc.toggle(0, 13, ITALIC, self.clock))
        self.assertEqual(spans(self.doc), [[("one ", True, False), ("two", False, False), (" three", True, False)]])

    def test_typing_at_the_end_of_a_stretch(self):
        self.doc.format(4, 3, BOLD, self.clock)
        self.doc.insert(7, "!", self.clock, bold=True)
        self.doc.insert(4, "^", self.clock)                   # at its start: inside, after the marker
        self.doc.insert(4, "~", self.clock, bold=False)        # asked to stay out
        self.assertEqual(spans(self.doc), [[("one ~", False, False), ("^two!", True, False), (" three", False, False)]])


class SequenceTests(unittest.TestCase):
    """How runs listed by rising id are put together (0x1409350a0)."""

    def order(self, items, authors=None) -> str:
        return "".join(r.text for r in TextSequence.load(items, authors).runs if r.live_text)

    def test_two_devices_typing_at_one_place(self):
        base = TextItem((1, 1), ZERO, ZERO, 0, b"ab", 1)
        mine = TextItem((1, 3), (1, 1), (1, 2), 0, b"X", 1)
        theirs = TextItem((2, 3), (1, 1), (1, 2), 0, b"Y", 1)
        low, high = bytes(16), bytes([0xFF] * 16)
        # Listed by counter, then the authors' UUIDs: the lower UUID's text comes first.
        self.assertEqual(self.order([base, mine, theirs], {1: low, 2: high}), "aXYb")
        self.assertEqual(self.order([base, theirs, mine], {1: high, 2: low}), "aYXb")
        # The text written back is in that order, and reads the same.
        seq = TextSequence.load([base, mine, theirs], {1: low, 2: high})
        self.assertEqual(self.order(seq.items(), {1: low, 2: high}), "aXYb")

    def test_a_later_insert_between_goes_before_the_older_one(self):
        base = TextItem((1, 1), ZERO, ZERO, 0, b"ab", 1)
        first = TextItem((1, 3), (1, 1), (1, 2), 0, b"X", 1)
        later = TextItem((1, 4), (1, 1), (1, 3), 0, b"Y", 1)              # typed before X
        self.assertEqual(self.order([base, first, later]), "aYXb")

    def test_the_file_order_wins_when_not_listed_by_id(self):
        items = [TextItem((1, 5), ZERO, ZERO, 0, b"b", 1), TextItem((1, 1), ZERO, (1, 5), 0, b"a", 1)]
        self.assertEqual(self.order(items), "ba")

    def test_entries_the_app_drops(self):
        items = [TextItem((1, 1), ZERO, ZERO, 0, b"ab", 1),
                 TextItem((1, 3), (1, 5), ZERO, 0, b"x", 1),         # its left is newer than it
                 TextItem((1, 4), (1, 2), ZERO, 0, b"", 1),          # live text with nothing in it
                 TextItem((0, 5), (1, 2), ZERO, 0, b"y", 1)]         # (0, 5): fine, a built-in author
        self.assertEqual(self.order(items), "aby")

    def test_text_holding_an_id_twice_is_refused(self):
        items = [TextItem((1, 1), ZERO, ZERO, 0, b"ab", 1), TextItem((1, 2), (1, 1), ZERO, 0, b"c", 1)]
        with self.assertRaises(TextError):
            TextSequence.load(items)


@unittest.skipUnless(os.environ.get("RM_TEST_CACHED_LIBRARY"),
                     "set RM_TEST_CACHED_LIBRARY=1 to edit the typed text of the library cached on this machine")
class CachedLibraryEditTests(unittest.TestCase):
    """Every typed-text block of the pages cached here (read from disk only; nothing of
    them is printed): written back byte for byte, then edited in memory at random and
    checked against the same edits made to a plain string."""

    def test_edit_every_cached_text_block(self):
        from core.auth import AuthClient
        from core.sync import NotCachedError, SyncClient

        def no_network(*args):
            raise AssertionError("network used while reading the cache")

        sync = SyncClient(AuthClient(transport=no_network))
        rnd, seen = random.Random(11), 0
        for doc in sync.load_cached().documents():
            for name, blob in doc.files.items():
                if not name.endswith(".rm"):
                    continue
                try:
                    data = sync.get_blob(blob, name, cached_only=True)
                except NotCachedError:
                    continue
                if not data.startswith(w.V6_HEADER):
                    continue
                text = rm_text.page_text(data)
                if text is None:
                    continue
                seen += 1
                payload = [b for b in w.split(data)[1] if b.type == w.BLOCK_ROOT_TEXT][0].payload
                self.assertEqual(text.encode(), payload)
                self.assertEqual(rm_text.encode_root_text(decode_root_text(payload)), payload)
                plain, clock = text.text, Clock(9, w.highest_counter(w.split(data)[1]))
                for _ in range(25):
                    n = text.length
                    op = rnd.choice("iidfs")
                    try:
                        if op == "i":
                            at = rnd.randint(0, n)
                            text.insert(at, "x\U0001F600\n", clock)
                            plain = rm_text.cut(plain, 0, at) + "x\U0001F600\n" + rm_text.cut(plain, at)
                        elif op == "d" and n:
                            at = rnd.randint(0, n - 1)
                            count = rnd.randint(1, min(5, n - at))
                            text.delete(at, count)
                            plain = rm_text.cut(plain, 0, at) + rm_text.cut(plain, at + count)
                        elif op == "f" and n:
                            at = rnd.randint(0, n - 1)
                            text.toggle(at, rnd.randint(1, n - at), rnd.choice([BOLD, ITALIC]), clock)
                        elif op == "s":
                            text.set_style(rnd.randrange(len(text.paragraph_keys())),
                                           rnd.choice(list(ParagraphStyle)), clock)
                    except TextError:
                        continue                                    # a position inside a character
                    self.assertEqual(text.text, plain)
                out = text.encode()
                back = TextDocument(decode_root_text(out))
                self.assertEqual((back.text, back.encode()), (plain, out))
                self.assertEqual(text_block(decode_root_text(out)).text, plain)
        self.assertGreater(seen, 0, "no typed text in the cache")


if __name__ == "__main__":
    unittest.main()
