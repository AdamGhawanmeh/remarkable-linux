"""Offline tests for typing on a page (ui.textedit): the app's key map, what Enter,
Backspace and the style keys do, that what is typed is made again the same from the
edits kept, and undo and redo of it all.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.edits import PageHistory, page_with_edits, swapped, text_of  # noqa: E402
from ui import textedit as T  # noqa: E402
from viewer import rm_text, rm_writer as w  # noqa: E402
from viewer.renderer import estimate_width  # noqa: E402
from viewer.rm_parser import ParagraphStyle, parse  # noqa: E402

ME = bytes(range(16))
CTRL, SHIFT = T.CTRL, T.SHIFT


def session(page: bytes | None = None, edits=()) -> T.TextSession:
    page = w.blank_page(ME) if page is None else page
    done = text_of(page, list(edits), ME)[1]
    return T.TextSession.open("doc", "page", done.page, done, ME, parse(done.page), estimate_width, lambda: 1)


def styled(s: T.TextSession) -> list:
    from viewer.rm_parser import text_block
    return [(p.style.name, [(x.text, x.bold, x.italic) for x in p.spans])
            for p in text_block(s.text.root_text(), s.authors).paragraphs]


def typed(s: T.TextSession, text: str) -> None:
    for c in text:
        if c == "\n":
            s.enter()
        else:
            s.type_text(c)


class KeyMapTests(unittest.TestCase):
    """SceneKeyHandler::process (0x140408d10), Windows flavour."""

    def kind(self, key, mods=0, text=""):
        return T.key_action(key, mods, False, text)

    def test_the_keys(self):
        cases = [((ord("A"), 0, "a"), (T.KEYSTROKE, 0, 0, "a")),
                 ((ord("1"), CTRL), (T.TITLE_STYLE,)), ((ord("2"), CTRL), (T.SUBHEADING_STYLE,)),
                 ((ord("3"), CTRL), (T.SUBHEADING2_STYLE,)), ((ord("4"), CTRL), (T.PARAGRAPH_STYLE,)),
                 ((ord("5"), CTRL), (T.BULLETPOINT_STYLE,)), ((ord("6"), CTRL), (T.NUMBERED_LIST,)),
                 ((ord("7"), CTRL), (T.CHECKBOX_STYLE,)), ((ord("B"), CTRL), (T.BOLD_STYLE,)),
                 ((ord("I"), CTRL), (T.ITALIC_STYLE,)), ((ord("Z"), CTRL), (T.UNDO,)), ((ord("Y"), CTRL), (T.REDO,)),
                 ((ord("Z"), CTRL | SHIFT), (T.REDO,)), ((ord("V"), CTRL | SHIFT), (T.PASTE_AND_MATCH_STYLE,)),
                 ((ord("A"), CTRL), (T.SELECT_ALL,)), ((ord("Q"), CTRL, "q"), (T.NONE,)),
                 ((T.KEY_RETURN, 0), (T.ENTER,)), ((T.KEY_ENTER, SHIFT), (T.LINE_BREAK,)),
                 ((T.KEY_TAB, 0), (T.INDENT,)), ((T.KEY_TAB, SHIFT), (T.UNINDENT,)), ((T.KEY_BACKTAB, SHIFT), (T.UNINDENT,)),
                 ((T.KEY_BACKSPACE, 0), (T.BACKSPACE,)), ((T.KEY_BACKSPACE, CTRL), (T.DELETE_START_OF_WORD,)),
                 ((T.KEY_DELETE, 0), (T.DELETE,)), ((T.KEY_DELETE, CTRL), (T.DELETE_END_OF_WORD,)),
                 ((T.KEY_LEFT, 0), (T.CURSOR_MOVE, T.LEFT, 0)), ((T.KEY_LEFT, CTRL), (T.CURSOR_MOVE, T.WORD_LEFT, 0)),
                 ((T.KEY_RIGHT, SHIFT), (T.CURSOR_MOVE, T.RIGHT, 1)), ((T.KEY_HOME, 0), (T.CURSOR_MOVE, T.START_OF_LINE, 0)),
                 ((T.KEY_HOME, CTRL), (T.CURSOR_MOVE, T.START, 0)), ((T.KEY_END, 0), (T.CURSOR_MOVE, T.END_OF_LINE, 0)),
                 ((T.KEY_UP, CTRL), (T.CURSOR_MOVE, T.START_OF_BLOCK, 0)),
                 ((T.KEY_DOWN, CTRL | SHIFT), (T.CURSOR_MOVE, T.END_OF_BLOCK, 1)),
                 ((T.KEY_LEFT, T.ALT), (T.NONE,)), ((T.KEY_PAGE_DOWN, 0), (T.PAGE_DOWN,))]
        for args, want in cases:
            action = self.kind(*args)
            got = (action["type"], action["operation"], action["mode"], action["text"])
            self.assertEqual(got[:len(want)], want, args)


class TypingRuleTests(unittest.TestCase):
    def test_a_new_box_is_a_title_and_enter_starts_body_text(self):
        s = session()
        self.assertEqual([e.op for e in s.edits], ["create"])
        typed(s, "Title\nbody")
        self.assertEqual([p[0] for p in styled(s)], ["TITLE", "PARAGRAPH"])

    def test_splitting_a_paragraph_keeps_its_style(self):
        s = session()
        typed(s, "Heading")
        s.place(4)
        s.enter()
        self.assertEqual(styled(s), [("TITLE", [("Head", False, False)]), ("TITLE", [("ing", False, False)])])

    def test_lists_go_on_and_end_with_an_empty_item(self):
        s = session()
        typed(s, "x\n")
        s.cycle_style(ParagraphStyle.CHECKBOX_UNCHECKED)
        typed(s, "a\nb\n")
        self.assertEqual([p[0] for p in styled(s)], ["TITLE", "CHECKBOX_UNCHECKED", "CHECKBOX_UNCHECKED",
                                                     "CHECKBOX_UNCHECKED"])
        s.enter()                                           # on the empty item: it stops being one
        self.assertEqual(styled(s)[-1][0], "PARAGRAPH")
        self.assertEqual(s.text.text, "x\na\nb\n")

    def test_backspace_at_a_list_item_ends_the_list_first(self):
        s = session()
        typed(s, "x\ny")
        s.cycle_style(ParagraphStyle.BULLETPOINT)
        s.place(2)
        s.backspace()
        self.assertEqual((s.text.text, styled(s)[1][0]), ("x\ny", "PARAGRAPH"))
        s.backspace()
        self.assertEqual(s.text.text, "xy")

    def test_backspacing_into_an_empty_paragraph_keeps_the_style(self):
        s = session()
        typed(s, "x\n\nz")
        s.cycle_style(ParagraphStyle.SUBHEADING)
        s.place(3)
        s.backspace()
        self.assertEqual(styled(s), [("TITLE", [("x", False, False)]), ("SUBHEADING", [("z", False, False)])])

    def test_a_star_and_space_make_a_bullet(self):
        s = session()
        typed(s, "x\n* item")
        self.assertEqual(styled(s)[1], ("BULLETPOINT", [("item", False, False)]))
        typed(s, " * no")                                    # only at the start of a paragraph
        self.assertEqual(styled(s)[1][1][0][0], "item * no")

    def test_list_buttons_cycle_the_indent(self):
        s = session()
        typed(s, "x\ny")
        stored = []
        for _ in range(4):
            s.cycle_style(ParagraphStyle.BULLETPOINT)
            stored.append(s.stored_style())
        self.assertEqual(stored, [(4, None, None), (5, 3, 1), (5, 3, 2), (4, None, None)])
        s.cycle_style(ParagraphStyle.TITLE)
        s.cycle_style(ParagraphStyle.TITLE)                  # a text style stays what it is
        self.assertEqual(s.stored_style(), (2, None, None))
        s.cycle_style(ParagraphStyle.NUMBERED_LIST)
        s.indent(1)
        self.assertEqual(s.paragraph_type(), ParagraphStyle.NUMBERED_LIST_INDENTED)
        s.indent(-1)
        s.indent(-1)
        self.assertEqual(s.paragraph_type(), ParagraphStyle.NUMBERED_LIST)

    def test_bold_and_italic(self):
        s = session()
        typed(s, "one two")
        s.select_range(0, 3)
        s.toggle_style(T.BOLD_FLAG)
        self.assertEqual(s.selection_styles(), T.BOLD_FLAG)
        s.toggle_style(T.BOLD_FLAG)
        self.assertEqual(s.selection_styles(), 0)
        s.place(7)
        s.toggle_style(T.ITALIC_FLAG)                        # what is typed next
        self.assertEqual(s.char_styles(), T.ITALIC_FLAG)
        typed(s, "!")
        self.assertEqual(styled(s)[0][1], [("one two", False, False), ("!", False, True)])

    def test_surrogate_pairs_move_and_delete_whole(self):
        s = session()
        typed(s, "a\U0001F600b")
        self.assertEqual(s.cursor, 4)
        s.move(T.LEFT)
        s.move(T.LEFT)
        self.assertEqual(s.cursor, 1)
        s.move(T.RIGHT)
        s.backspace()
        self.assertEqual(s.text.text, "ab")

    def test_moves_by_word_line_and_block(self):
        s = session()
        typed(s, "one two\nthree")
        s.move(T.WORD_LEFT)
        self.assertEqual(s.cursor, 8)
        s.move(T.START_OF_BLOCK, T.KEEP_ANCHOR)
        s.move(T.PREVIOUS_BLOCK)
        self.assertEqual(s.cursor, 0)
        s.move(T.WORD_RIGHT)
        self.assertEqual(s.cursor, 4)
        s.move(T.DOWN)
        self.assertEqual(s.paragraph_of(s.cursor), 1)
        s.move(T.END)
        self.assertEqual(s.cursor, s.text.length)

    def test_click_and_caret_agree(self):
        s = session()
        typed(s, "hello\nworld")
        for index in range(s.text.length + 1):
            caret = s.caret(index)
            self.assertEqual(s.index_at(caret.x, caret.top + caret.height / 2), index)


class KeptEditTests(unittest.TestCase):
    def script(self, s: T.TextSession) -> None:
        typed(s, "Notes\nfirst * x")
        s.place(s.text.length)
        typed(s, "\n- item\nsecond")
        s.select_range(6, 11)
        s.toggle_style(T.BOLD_FLAG)
        s.place(s.text.length)
        s.backspace()
        s.select_all()
        s.toggle_style(T.ITALIC_FLAG)
        s.place(0)
        s.move(T.WORD_RIGHT, T.KEEP_ANCHOR)
        s.paste("Pasted\ntext ")

    def test_the_edits_kept_make_the_same_page(self):
        page = w.blank_page(ME)
        s = session(page)
        self.script(s)
        edits, steps = s.take()
        self.assertEqual(rm_text.page_text(page_with_edits(page, edits, ME)).encode(), s.text.encode())
        # Typing merged: far fewer edits than keys.
        self.assertLess(len(edits), 20)

    def test_a_session_goes_on_from_edits_kept_before(self):
        page = w.blank_page(ME)
        first = session(page)
        typed(first, "abc")
        kept, _ = first.take()
        second = session(page, kept)
        second.place(1)
        typed(second, "X")
        more, _ = second.take()
        self.assertEqual(parse(page_with_edits(page, kept + more, ME)).text.text, "aXbc")

    def test_undo_all_then_redo_all(self):
        page = w.blank_page(ME)
        s = session(page)
        self.script(s)
        edits, steps = s.take()

        def shown(e):
            return [(p.style.name, [(x.text, x.bold, x.italic) for x in p.spans])
                    for p in parse(page_with_edits(page, e, ME)).text.paragraphs]
        history, now, states = PageHistory(None), list(edits), [shown(edits)]
        for step in steps:
            history.push(step)
        while history.undo:
            more, back = swapped(history.undo.pop(), "doc", "page", 2, history.aliases)
            now += more
            history.redo.append(back)
            states.append(shown(now))
        self.assertEqual(states[-1], [("TITLE", [])])
        k = len(states) - 1
        while history.redo:
            more, back = swapped(history.redo.pop(), "doc", "page", 3, history.aliases)
            now += more
            history.undo.append(back)
            k -= 1
            self.assertEqual(shown(now), states[k])
        # and undone once more: as it was before the last action
        more, _ = swapped(history.undo.pop(), "doc", "page", 4, history.aliases)
        self.assertEqual(shown(now + more), states[1])


if __name__ == "__main__":
    unittest.main()
