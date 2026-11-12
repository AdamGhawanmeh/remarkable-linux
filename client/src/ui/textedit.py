"""Typing on a page, as the app's text mode does it (SceneController's text members,
SceneKeyHandler, the scene's text document: reMarkable.exe 3.29).

Keys become actions (key_action, SceneKeyHandler::process, 0x140408d10, Windows
flavour): Ctrl+1..7 Title, Subheading, Subheading 2, Body, Bullet, Numbered list,
Checkbox; Ctrl+A select all, Ctrl+B bold, Ctrl+I italic, Ctrl+C/X/V copy, cut,
paste (Ctrl+Shift+V matching the style), Ctrl+Z undo, Ctrl+Y and Ctrl+Shift+Z redo;
Enter a new paragraph, Shift+Enter a line break; Tab and Shift+Tab indent;
Backspace and Delete (with Ctrl: a word, 0x140408af0); the arrows, Home and End move
the cursor (Ctrl: by word, paragraph, or to the start and end; Shift selects,
0x140408c40); any other key with text and without Ctrl types it.

A TextSession is the text of one page while it is typed on: the page's text
(viewer.rm_text.TextDocument), the cursor and selection (UTF-16 text indices, as the
app's QTextCursor counts), and what was done to it, as TextEdits (core.edits) and
undo steps, kept until they are handed over (take). Each edit is made on the text
the way it is made again when the page is worked out (core.edits.apply_text_edit),
with the same ids. Text typed in a row is one edit and one undo step.

What the app does around typing, and this does too:
  Enter (handleKeyEnter, 0x14094f2d0) on an empty list item un-indents it, or makes
  it body text (0x1408b5aa0), instead of adding a line. Otherwise it starts a new
  paragraph; the new one has the style of the one it was split from when that is a
  list item (a checkbox unchecked) or when the cursor was not at its end; else it is
  body text (no style entry).
  Backspace (handleKeyDelete, 0x14094ea70) at the start of a list item makes it body
  text instead of joining it to the line above; backspacing a paragraph into an
  empty one above keeps its own style. Deleting a selection that starts a paragraph
  keeps the style of the paragraph it ends in.
  A space typed after "*", "+" or "-" alone at the start of a paragraph that is not
  a list turns it into a bullet point (0x14094d080), the marker and the space taken
  out.
  Paragraph style buttons (cycleParagraphStyle, 0x14043ca20): a list button on a
  paragraph of that list kind cycles its indent (0, 1, 2); any other sets the style.
  Bold and italic (setTextStyle, 0x140954a40): on a selection, all of it styled, or
  unstyled when all of it has the style; without one, the style of what is typed next.

Not done as the app does: a line break (Shift+Enter: the app's character for it is
not decoded) does nothing; the checkbox and numbered-list typing shortcuts (their
strings live in the app's settings, not decoded) are not made; clicking below the text
moves the cursor to its end rather than adding empty lines down to the click.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from core.edits import (BODY_TEXT, CharRef, Replay, StyleChange, TextChange, TextEdit, TextPart,
                        apply_text_edit)
from viewer import rm_text, rm_writer as w
from viewer.renderer import BASELINE_AT, TextLine, caret_x, index_at, layout_text, line_of
from viewer.rm_parser import (FORMAT_BOLD_START, FORMAT_ITALIC_START, STYLE_LIST, STYLE_TEXT, PageScene,
                              ParagraphStyle, StyleEntry, _style_from_legacy, text_block)

# SceneKeyHandlerAction::Type
(NONE, KEYSTROKE, CURSOR_MOVE, ENTER, BACKSPACE, DELETE, DELETE_END_OF_LINE, DELETE_END_OF_WORD,
 DELETE_START_OF_WORD, DELETE_START_OF_LINE, SELECT_ALL, PAGE_UP, PAGE_DOWN, UNDO, REDO, COPY, CUT, PASTE,
 PASTE_AND_MATCH_STYLE, LINE_BREAK, SELECTION_TOOL_TOGGLE, INDENT, UNINDENT, TITLE_STYLE, SUBHEADING_STYLE,
 SUBHEADING2_STYLE, PARAGRAPH_STYLE, BULLETPOINT_STYLE, CHECKBOX_STYLE, NUMBERED_LIST, BOLD_STYLE,
 ITALIC_STYLE) = range(32)

# SceneController::MoveOperation (QTextCursor::MoveOperation)
(NO_MOVE, START, UP, START_OF_LINE, START_OF_BLOCK, START_OF_WORD, PREVIOUS_BLOCK, PREVIOUS_CHARACTER,
 PREVIOUS_WORD, LEFT, WORD_LEFT, END, DOWN, END_OF_LINE, END_OF_WORD, END_OF_BLOCK, NEXT_BLOCK, NEXT_CHARACTER,
 NEXT_WORD, RIGHT, WORD_RIGHT) = range(21)
MOVE_ANCHOR, KEEP_ANCHOR = 0, 1

SHIFT, CTRL, ALT = 0x02000000, 0x04000000, 0x08000000
KEY_RETURN, KEY_ENTER, KEY_TAB, KEY_BACKTAB = 0x01000004, 0x01000005, 0x01000001, 0x01000002
KEY_BACKSPACE, KEY_DELETE = 0x01000003, 0x01000007
KEY_HOME, KEY_END, KEY_LEFT, KEY_UP, KEY_RIGHT, KEY_DOWN = 0x01000010, 0x01000011, 0x01000012, 0x01000013, \
    0x01000014, 0x01000015
KEY_PAGE_UP, KEY_PAGE_DOWN, KEY_UNDO, KEY_REDO, KEY_TOOL = 0x01000016, 0x01000017, 0x01000123, 0x01000124, 0x01000054

_CTRL_KEYS = {0x31: TITLE_STYLE, 0x32: SUBHEADING_STYLE, 0x33: SUBHEADING2_STYLE, 0x34: PARAGRAPH_STYLE,
              0x35: BULLETPOINT_STYLE, 0x36: NUMBERED_LIST, 0x37: CHECKBOX_STYLE, 0x41: SELECT_ALL,
              0x42: BOLD_STYLE, 0x43: COPY, 0x49: ITALIC_STYLE, 0x56: PASTE, 0x58: CUT, 0x5A: UNDO}
_MOVES = {KEY_HOME: START, KEY_END: END, KEY_LEFT: LEFT, KEY_UP: UP, KEY_RIGHT: RIGHT, KEY_DOWN: DOWN}
_PLAIN_MOVES = {START: START_OF_LINE, END: END_OF_LINE}
_CTRL_MOVES = {LEFT: WORD_LEFT, RIGHT: WORD_RIGHT, START: START, UP: START_OF_BLOCK, DOWN: END_OF_BLOCK, END: END}

# The paragraph style each style action, and button, sets (ParagraphStyle::Type)
STYLE_ACTIONS = {TITLE_STYLE: ParagraphStyle.TITLE, SUBHEADING_STYLE: ParagraphStyle.SUBHEADING,
                 SUBHEADING2_STYLE: ParagraphStyle.SUBHEADING_2, PARAGRAPH_STYLE: ParagraphStyle.PARAGRAPH,
                 BULLETPOINT_STYLE: ParagraphStyle.BULLETPOINT, CHECKBOX_STYLE: ParagraphStyle.CHECKBOX_UNCHECKED,
                 NUMBERED_LIST: ParagraphStyle.NUMBERED_LIST}
BOLD_FLAG, ITALIC_FLAG, LINK_FLAG = 1, 2, 4          # scene::CharacterStyle::Types
_BULLET_TYPED = re.compile(r"^[\*\+\-]$")
_WORD = re.compile(r"\w", re.UNICODE)


def key_action(key: int, modifiers: int, auto_repeat: bool, text: str) -> dict:
    """The action for a key (SceneKeyHandler::process, Windows flavour), as the dict
    SceneView hands to QML and back: {"type", "operation", "mode", "text"}."""
    def act(kind: int, operation: int = 0, mode: int = 0, typed: str = "") -> dict:
        return {"type": kind, "operation": operation, "mode": mode, "text": typed}
    modifiers &= SHIFT | CTRL | ALT | 0x10000000
    if modifiers == CTRL and key in _CTRL_KEYS:
        return act(_CTRL_KEYS[key])
    if modifiers == CTRL | SHIFT and key == 0x56:
        return act(PASTE_AND_MATCH_STYLE)
    if key in (KEY_RETURN, KEY_ENTER):
        return act(LINE_BREAK if modifiers & SHIFT else ENTER)
    if key in (KEY_PAGE_UP, KEY_PAGE_DOWN):
        return act(PAGE_UP if key == KEY_PAGE_UP else PAGE_DOWN)
    if key == KEY_TAB:
        return act(UNINDENT if modifiers == SHIFT else INDENT)
    if key == KEY_BACKTAB:
        return act(UNINDENT)
    if key == KEY_TOOL and not auto_repeat:
        return act(SELECTION_TOOL_TOGGLE)
    if key == KEY_UNDO:
        return act(UNDO)
    if key == KEY_REDO or (modifiers == CTRL and key == 0x59) or (modifiers == CTRL | SHIFT and key == 0x5A):
        return act(REDO)
    if key == KEY_BACKSPACE:
        if modifiers == CTRL | SHIFT:
            return act(NONE)
        return act(DELETE_START_OF_WORD if modifiers == CTRL else BACKSPACE)
    if key == KEY_DELETE:
        return act(DELETE_END_OF_WORD if modifiers == CTRL else DELETE)
    if key in _MOVES:
        if modifiers & ALT:
            return act(NONE)
        operation = _MOVES[key]
        if modifiers & CTRL:
            if operation not in _CTRL_MOVES:
                return act(NONE)
            operation = _CTRL_MOVES[operation]
        else:
            operation = _PLAIN_MOVES.get(operation, operation)
        return act(CURSOR_MOVE, operation, KEEP_ANCHOR if modifiers & SHIFT else MOVE_ANCHOR)
    if text and not modifiers & CTRL:
        return act(KEYSTROKE, typed=text)
    return act(NONE)


# ------------------------------------------------------------------ styles as stored

def stored_of(entry: StyleEntry | None) -> tuple | None:
    return None if entry is None else (entry.legacy, entry.base_style, entry.properties)


def held(stored: tuple | None) -> tuple[int, int]:
    """(base, properties) of a stored style, as the app holds it (None: body text)."""
    if stored is None:
        return STYLE_TEXT, 0
    return StyleEntry((0, 0), (0, 0), *stored).base


def store(base: int, properties: int) -> tuple:
    """A held style as stored: the legacy byte always (0x140a93100), base_style and
    style_properties too when the byte cannot say it all (0x140a92b30: a list item
    indented or partly checked, a heading level past 2)."""
    from viewer.rm_parser import _style_to_legacy
    legacy = _style_to_legacy(base, properties)
    if base == STYLE_LIST:
        plain = properties & 0xF == 0 and properties >> 8 & 7 <= 1
    elif base == STYLE_TEXT:
        plain = properties & 0xFF < 3
    else:
        plain = True
    return (legacy, None, None) if plain else (legacy, base, properties)


def is_list(base: int, properties: int) -> bool:
    return base == STYLE_LIST and properties >> 4 & 0xF in (0, 1, 2)


def type_of(stored: tuple | None) -> ParagraphStyle:
    return ParagraphStyle.PARAGRAPH if stored is None else StyleEntry((0, 0), (0, 0), *stored).style


# ------------------------------------------------------------------ the session

@dataclass
class Caret:
    x: float            # scene coordinates (x 0 on the page's centre line)
    top: float
    height: float
    line: int
    empty_line: bool


@dataclass
class TextSession:
    """The text of a page while it is typed on. `text`: the page's text as it is
    with everything done here; `done`: what the page's pending edits made, and these
    (core.edits.Replay), to name items in new edits; `clock`: the page's clock for
    this author; `scene`: the page as parsed, whose text is replaced as it changes."""

    doc_id: str
    page_id: str
    text: rm_text.TextDocument
    done: Replay
    clock: rm_text.Clock
    scene: PageScene | None
    authors: dict[int, bytes] = field(default_factory=dict)
    measure: Callable = None
    now: Callable[[], int] = None
    cursor: int = 0
    anchor: int = 0
    edits: list[TextEdit] = field(default_factory=list)          # not handed over yet
    steps: list[list] = field(default_factory=list)              # undo steps, likewise
    typing: TextEdit | None = None                                # the edit typed text goes on with
    pending_style: dict | None = None                             # bold/italic for what is typed next
    _lines: list[TextLine] | None = None

    @classmethod
    def open(cls, doc_id: str, page_id: str, page: bytes | None, done: Replay, author: bytes,
             scene: PageScene | None, measure: Callable, now: Callable[[], int],
             style: ParagraphStyle = ParagraphStyle.TITLE) -> "TextSession":
        """The session of a page (`page`: its .rm with the pending edits, None: it has
        none; `done`: what those edits made). A page without a text box gets one, as
        the app's createRootDocument makes it, and that is the session's first edit."""
        blocks = w.split(page if page is not None else w.blank_page(author))[1]
        authors = {index: uuid for uuid, index in w.decode_authors(blocks[0].payload)}
        me = next((index for index, uuid in authors.items() if uuid == author), max(authors, default=0) + 1)
        authors.setdefault(me, author)
        clock = rm_text.Clock(me, w.highest_counter(blocks))
        text = rm_text.page_text(page)
        session = cls(doc_id, page_id, None, done, clock, scene, authors, measure, now)
        if text is None:
            text = rm_text.TextDocument.new(clock, style=style, authors=authors)
            session.edits.append(TextEdit.new(doc_id, page_id, "create", now(), paragraph=int(style)))
        session.text = text
        session.cursor = session.anchor = text.length
        return session

    # -- what is shown --------------------------------------------------------------
    def shown_scene(self) -> PageScene:
        """The page with the text as it is now."""
        from dataclasses import replace
        block = text_block(self.text.root_text(), self.authors)
        if self.scene is None:
            return PageScene(version=6, x_origin=w.PAPER[0] / 2, text=block)
        return replace(self.scene, text=block)

    def lines(self) -> list[TextLine]:
        if self._lines is None:
            self._lines = layout_text(self.shown_scene(), self.measure)
        return self._lines

    def _changed(self) -> None:
        self._lines = None

    @property
    def origin(self) -> float:
        return (self.scene.x_origin if self.scene is not None else w.PAPER[0] / 2)

    def caret(self, index: int | None = None) -> Caret:
        lines = self.lines()
        index = self.cursor if index is None else index
        if not lines:
            root = self.text.root
            return Caret(root.x, root.y, 71.5, 0, True)
        n = line_of(lines, index)
        line = lines[n]
        top = line.baseline - line.height * BASELINE_AT
        return Caret(caret_x(line, index, self.measure) - self.origin, top, line.height, n, line.start == line.end)

    def selection(self) -> tuple[int, int]:
        return min(self.cursor, self.anchor), max(self.cursor, self.anchor)

    def has_selection(self) -> bool:
        return self.cursor != self.anchor

    def selection_rects(self) -> list[tuple[float, float, float, float]]:
        """The selected text, a rectangle (scene coordinates) per line."""
        start, end = self.selection()
        out = []
        for line in self.lines():
            a, b = max(start, line.start), min(end, line.end)
            if a >= b and not (line.start == line.end and start <= line.start < end):
                continue
            top = line.baseline - line.height * BASELINE_AT
            x0 = caret_x(line, a, self.measure) - self.origin
            x1 = caret_x(line, b, self.measure) - self.origin
            out.append((x0, top, max(x1 - x0, 6.0), line.height))
        return out

    def paragraph_bounds(self) -> tuple[float, float, float, float]:
        """The cursor's paragraph (textParagraphBounds), scene coordinates."""
        lines = self.lines()
        if not lines:
            c = self.caret()
            return c.x, c.top, self.text.root.text_width, c.height
        para = lines[line_of(lines, self.cursor)].paragraph
        mine = [l for l in lines if l.paragraph == para]
        top = mine[0].baseline - mine[0].height * BASELINE_AT
        bottom = mine[-1].baseline + mine[-1].height * (1 - BASELINE_AT)
        return self.text.root.x, top, self.text.root.text_width, bottom - top

    def index_at(self, x: float, y: float) -> int:
        """The text index nearest a scene position."""
        lines = self.lines()
        return index_at(lines, x + self.origin, y, self.measure) if lines else 0

    # -- paragraphs -----------------------------------------------------------------
    def plain(self) -> str:
        return self.text.text

    def paragraph_of(self, index: int) -> int:
        return rm_text.cut(self.plain(), 0, index).count("\n")

    def paragraph_range(self, index: int) -> tuple[int, int]:
        """(start, end) text indices of the paragraph holding `index` (end: its newline)."""
        plain = self.plain()
        before = rm_text.cut(plain, 0, index)
        start = rm_text.units(before[:before.rfind("\n") + 1]) if "\n" in before else 0
        after = rm_text.cut(plain, index)
        end = index + (rm_text.units(after[:after.find("\n")]) if "\n" in after else rm_text.units(after))
        return start, end

    def paragraph_key(self, index: int) -> tuple[int, int]:
        return self.text.paragraph_keys()[self.paragraph_of(index)]

    def stored_style(self, index: int | None = None) -> tuple | None:
        return stored_of(self.text.style_entry(self.paragraph_key(self.cursor if index is None else index)))

    def paragraph_type(self) -> ParagraphStyle:
        return type_of(self.stored_style())

    def char_styles(self) -> int:
        """textStyles: the style of what is typed at the cursor."""
        if self.pending_style is not None:
            return (BOLD_FLAG if self.pending_style["bold"] else 0) | (ITALIC_FLAG if self.pending_style["italic"] else 0)
        _, _, depth = self.text.locate(self.cursor)
        return (BOLD_FLAG if depth["bold"] > 0 else 0) | (ITALIC_FLAG if depth["italic"] > 0 else 0) | \
            (LINK_FLAG if depth["link"] > 0 else 0)

    def selection_styles(self) -> int:
        """textSelectionStyles: the styles all of the selection has."""
        start, end = self.selection()
        if start == end:
            return 0
        out = 0
        for flag, name in ((BOLD_FLAG, rm_text.BOLD), (ITALIC_FLAG, rm_text.ITALIC), (LINK_FLAG, rm_text.LINK)):
            if self.text.has_format(start, end - start, name):
                out |= flag
        return out

    # -- naming items -------------------------------------------------------------------
    def ref(self, item: tuple[int, int] | None) -> CharRef | None:
        return None if item is None or item == rm_text.ZERO else self.done.char_ref(item)

    def _before(self, item: tuple[int, int]) -> tuple[int, int] | None:
        """The item right before `item` in the sequence (None: it is the first)."""
        n = self.text.sequence.find(item)
        run = self.text.runs[n]
        if run.id != item:
            return item[0], item[1] - 1
        return self.text.runs[n - 1].last if n else None

    # -- making edits ---------------------------------------------------------------------
    def _edit(self, op: str, **kw) -> TextEdit:
        """Make an edit on the text, and keep it for handing over."""
        edit = TextEdit.new(self.doc_id, self.page_id, op, self.now(), **kw)
        if not apply_text_edit(self.text, self.clock, edit, self.done):
            raise rm_text.TextError(f"the {op} does not fit the text")
        self.edits.append(edit)
        self._changed()
        return edit

    def _step(self, changes: list) -> None:
        if changes:
            self.steps.append(changes)

    def _set_cursor(self, index: int, keep: bool = False) -> None:
        index = max(0, min(index, self.text.length))
        if rm_text._splits_pair(self.plain(), index):
            index += 1
        self.cursor = index
        if not keep:
            self.anchor = index

    def _stop_typing(self) -> None:
        self.typing = None

    def _insert(self, text: str, changes: list, bold=None, italic=None) -> None:
        """Type `text` at the cursor: one edit, or more of the one being typed."""
        at = self.cursor
        last = self.typing
        if last is not None and self.edits and self.edits[-1] is last and bold is None and italic is None:
            made = self.done.typed.get(last.uid) or []
            if made and self.text.position_of(made[-1]) == (at - 1, True):
                first = self.text.insert(at, text, self.clock)
                ids = [(first[0], first[1] + k) for k in range(rm_text.units(text))]
                merged = TextEdit(last.doc_id, last.page_id, "insert", last.created_ms, last.first, None,
                                  last.text + text, "", 0, None, None, last.uid)
                self.edits[-1] = merged
                self.typing = merged
                self.done.typed[last.uid] = made + ids
                self._changed()
                step = self.steps[-1] if self.steps else None
                part = step[-1].remove[-1] if step and isinstance(step[-1], TextChange) and step[-1].remove else None
                if part is not None and part.first == ("edit", last.uid, 0):
                    names = tuple(("edit", last.uid, k) for k in range(len(made + ids)))
                    step[-1] = TextChange((TextPart(part.after, part.text + text, 0, part.first, names[-1], names),),
                                          step[-1].readd)
                    self.cursor = self.anchor = at + rm_text.units(text)
                    changes.clear()
                    return
                names = tuple(("edit", last.uid, len(made) + k) for k in range(len(ids)))
                changes.append(TextChange((TextPart(self.ref(self._before(ids[0])), text, 0, names[0], names[-1],
                                                    names),)))
                self.cursor = self.anchor = at + rm_text.units(text)
                return
        before = self.text.id_at(at - 1) if at else None
        edit = self._edit("insert", first=self.ref(before), text=text, bold=bold, italic=italic)
        self.typing = edit
        ids = self.done.typed[edit.uid]
        after = self._before(ids[0])
        names = tuple(("edit", edit.uid, k) for k in range(len(ids)))
        changes.append(TextChange((TextPart(self.ref(after), text, 0, names[0], names[-1], names),)))
        self.cursor = self.anchor = at + rm_text.units(text)

    def _delete(self, start: int, end: int, changes: list) -> None:
        """Delete text: what goes is recorded, part by part with what it came after,
        for undo."""
        if end <= start:
            return
        parts = []
        n, offset, _ = self.text.locate(start)
        left = end - start
        while left > 0 and n < len(self.text.runs):
            run = self.text.runs[n]
            if run.deleted or run.marker:
                n += 1
                continue
            count = min(left, run.length - offset)
            first = (run.id[0], run.id[1] + offset)
            last = (run.id[0], run.id[1] + offset + count - 1)
            parts.append(TextPart(self.ref(self._before(first)), rm_text.cut(run.text, offset, offset + count), 0,
                                  self.ref(first), self.ref(last),
                                  tuple(self.ref((first[0], first[1] + k)) for k in range(count))))
            left -= count
            n, offset = n + 1, 0
        self._edit("delete", first=parts[0].first, last=parts[-1].last)
        changes.append(TextChange((), tuple(TextPart(p.after, p.text, items=p.items) for p in parts)))
        self._set_cursor(start)

    def _set_style(self, index: int, stored: tuple, changes: list) -> None:
        key = self.paragraph_key(index)
        old = stored_of(self.text.style_entry(key))
        if old == stored or (old is None and stored == BODY_TEXT):
            return
        ref = None if key == rm_text.ZERO else self.ref(key)
        self._edit("style", first=ref, stored=stored)
        changes.append(StyleChange(ref, old, stored))

    def _delete_selection(self, changes: list) -> None:
        """Delete the selection; when it starts a paragraph, that paragraph keeps the
        style of the one the selection ends in."""
        start, end = self.selection()
        if start == end:
            return
        plain = self.plain()
        keep = None
        if end != self.text.length and rm_text.cut(plain, end, end + 1) != "\n" and \
                (start == 0 or rm_text.cut(plain, start - 1, start) == "\n"):
            keep = self.stored_style(end) or BODY_TEXT
        self._delete(start, end, changes)
        if keep is not None:
            self._set_style(start, keep, changes)

    # -- what QML asks for ---------------------------------------------------------------
    def type_text(self, typed: str) -> None:
        """A key typed (handleKeyStroke, 0x14094fad0)."""
        typed = "".join(c for c in typed if c == "\t" or c >= " ")
        if not typed:
            return
        changes: list = []
        if self.has_selection():
            self._stop_typing()
            self._delete_selection(changes)
        style = self.pending_style
        if style is not None:
            self._stop_typing()
            start = self.cursor
            self._insert(typed, changes)
            self._style_range(start, self.cursor, style, changes)
            self._step(changes)
            return
        self._insert(typed, changes)
        self._step(changes)
        if typed == " ":
            self._bullet_typed()

    def _style_range(self, start: int, end: int, style: dict, changes: list) -> None:
        for name in (rm_text.BOLD, rm_text.ITALIC):
            want = style[name]
            if want != self.text.has_format(start, end - start, name):
                self._format(start, end, name, want, changes)

    def _bullet_typed(self) -> None:
        """"* " (or "+ ", "- ") typed at the start of a paragraph that is not a list
        makes it a bullet point (0x14094d080)."""
        start, _ = self.paragraph_range(self.cursor)
        word = rm_text.cut(self.plain(), start, self.cursor - 1)
        if self.cursor - start != 2 or not _BULLET_TYPED.match(word) or is_list(*held(self.stored_style())):
            return
        self._stop_typing()
        changes: list = []
        self._delete(start, self.cursor, changes)
        self._set_style(start, rm_text.stored_style(ParagraphStyle.BULLETPOINT), changes)
        self._step(changes)

    def enter(self) -> None:
        """A new paragraph (handleKeyEnter, 0x14094f2d0)."""
        self._stop_typing()
        changes: list = []
        stored = self.stored_style()
        base, properties = held(stored)
        start, end = self.paragraph_range(self.cursor)
        if is_list(base, properties) and start == end and not self.has_selection():
            self._set_style(self.cursor, self._unindented(base, properties), changes)
            self._step(changes)
            return
        self._delete_selection(changes)
        start, end = self.paragraph_range(self.cursor)
        at_end = self.cursor == end
        self._insert("\n", changes)
        self._stop_typing()
        if is_list(base, properties) or not at_end:
            if base == STYLE_LIST and properties >> 4 & 0xF == 1:
                properties &= ~0x700                                   # a new checkbox is unchecked
            self._set_style(self.cursor, store(base, properties), changes)
        self._step(changes)

    @staticmethod
    def _unindented(base: int, properties: int) -> tuple:
        """0x1408b5aa0: one indent less, or body text when there is none."""
        level = properties & 0xF if is_list(base, properties) else properties >> 8 & 0xF
        if level == 0:
            return rm_text.stored_style(ParagraphStyle.PARAGRAPH)
        if is_list(base, properties):
            return store(base, (properties & ~0xF) | (level - 1))
        return store(base, (properties & ~0xF00) | ((level - 1) << 8))

    def backspace(self) -> None:
        """handleKeyDelete with Backspace (0x14094ea70)."""
        self._stop_typing()
        changes: list = []
        if self.has_selection():
            self._delete_selection(changes)
            self._step(changes)
            return
        start, end = self.paragraph_range(self.cursor)
        base, properties = held(self.stored_style())
        if self.cursor == start and is_list(base, properties):
            self._set_style(self.cursor, rm_text.stored_style(ParagraphStyle.PARAGRAPH), changes)
            self._step(changes)
            return
        if self.cursor == 0:
            return
        keep = None
        if self.cursor == start and end > start:
            above = self.paragraph_range(self.cursor - 1)
            if above[0] == above[1]:
                keep = self.stored_style() or BODY_TEXT
        width = 2 if rm_text._splits_pair(self.plain(), self.cursor - 1) else 1
        self._delete(self.cursor - width, self.cursor, changes)
        if keep is not None:
            self._set_style(self.cursor, keep, changes)
        self._step(changes)

    def delete_forward(self) -> None:
        self._stop_typing()
        changes: list = []
        if self.has_selection():
            self._delete_selection(changes)
        elif self.cursor < self.text.length:
            width = 2 if rm_text._splits_pair(self.plain(), self.cursor + 1) else 1
            self._delete(self.cursor, self.cursor + width, changes)
        self._step(changes)

    def delete_to(self, operation: int) -> None:
        """Delete from the cursor to where `operation` would move it (Ctrl+Backspace,
        Ctrl+Delete and the like)."""
        self._stop_typing()
        changes: list = []
        if self.has_selection():
            self._delete_selection(changes)
        else:
            target = self.target(operation)
            self._delete(min(target, self.cursor), max(target, self.cursor), changes)
        self._step(changes)

    def _format(self, start: int, end: int, name: str, on: bool, changes: list) -> None:
        if on:
            edit = self._edit("format", first=self.ref(self.text.id_at(start)), last=self.ref(self.text.id_at(end - 1)),
                              style=name)
            made = self.done.typed.get(edit.uid, [])
            changes.append(TextChange(tuple(self._marker_part(m, ("edit", edit.uid, k)) for k, m in enumerate(made)), ()))
            return
        spans = [s for s in self.text._spans(name) if s[2] < end and s[3] > start and s[2] < s[3]]
        gone = []
        for first, last, _, _ in spans:
            for n in (first, last):
                marker = self.text.runs[n]
                gone.append(TextPart(self.ref(self._before(marker.id)), "", marker.format, items=(self.ref(marker.id),)))
        edit = self._edit("unformat", first=self.ref(self.text.id_at(start)), last=self.ref(self.text.id_at(end - 1)),
                          style=name)
        made = self.done.typed.get(edit.uid, [])
        changes.append(TextChange(tuple(self._marker_part(m, ("edit", edit.uid, k)) for k, m in enumerate(made)),
                                  tuple(gone)))

    def _marker_part(self, marker: tuple[int, int], ref: CharRef) -> TextPart:
        code = self.text.runs[self.text.sequence.find(marker)].format
        return TextPart(self.ref(self._before(marker)), "", code, ref, ref, (ref,))

    def toggle_style(self, flag: int) -> None:
        """setTextStyle (0x140954a40): Bold (1) or Italic (2)."""
        name = {BOLD_FLAG: rm_text.BOLD, ITALIC_FLAG: rm_text.ITALIC}.get(flag)
        if name is None:
            return
        self._stop_typing()
        if not self.has_selection():
            current = self.char_styles()
            style = {rm_text.BOLD: bool(current & BOLD_FLAG), rm_text.ITALIC: bool(current & ITALIC_FLAG)}
            style[name] = not style[name]
            self.pending_style = style
            return
        start, end = self.selection()
        changes: list = []
        self._format(start, end, name, not self.text.has_format(start, end - start, name), changes)
        self._step(changes)

    def cycle_style(self, button: ParagraphStyle) -> None:
        """cycleParagraphStyle (0x14043ca20) for the paragraphs of the selection."""
        self._stop_typing()
        wanted = held(rm_text.stored_style(button))
        changes: list = []
        start, end = self.selection()
        keys = []
        for index in self._paragraph_starts(start, end):
            base, properties = held(self.stored_style(index))
            if is_list(*wanted) and base == wanted[0] and properties >> 4 & 0xF == wanted[1] >> 4 & 0xF:
                level = ((properties & 0xF) + 1) % 3                 # 0x1408b5870: indent 0, 1, 2
                stored = store(base, (properties & ~0xF) | level)
            else:
                stored = rm_text.stored_style(button)
            if self.paragraph_key(index) not in keys:
                keys.append(self.paragraph_key(index))
                self._set_style(index, stored, changes)
        self._step(changes)

    def set_style(self, style: ParagraphStyle) -> None:
        self._stop_typing()
        changes: list = []
        for index in self._paragraph_starts(*self.selection()):
            self._set_style(index, rm_text.stored_style(style), changes)
        self._step(changes)

    def indent(self, step: int) -> None:
        """Tab and Shift+Tab: a list item's indent, 0 to 2."""
        self._stop_typing()
        changes: list = []
        for index in self._paragraph_starts(*self.selection()):
            base, properties = held(self.stored_style(index))
            if is_list(base, properties):
                level = max(0, min(2, (properties & 0xF) + step))
                self._set_style(index, store(base, (properties & ~0xF) | level), changes)
        self._step(changes)

    def _paragraph_starts(self, start: int, end: int) -> list[int]:
        out = [self.paragraph_range(start)[0]]
        while True:
            _, stop = self.paragraph_range(out[-1])
            if stop >= end or stop >= self.text.length:
                return out
            out.append(stop + 1)

    def paste(self, typed: str) -> None:
        """Text pasted: typed in one go, its newlines new paragraphs (body text)."""
        typed = typed.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n")
        typed = "".join(c for c in typed if c in "\n\t" or c >= " ")
        if not typed:
            return
        self._stop_typing()
        changes: list = []
        self._delete_selection(changes)
        self._insert(typed, changes)
        self._stop_typing()
        self._step(changes)

    def selected_text(self) -> str:
        start, end = self.selection()
        return rm_text.cut(self.plain(), start, end)

    def cut_selection(self) -> str:
        text = self.selected_text()
        if text:
            self._stop_typing()
            changes: list = []
            self._delete_selection(changes)
            self._step(changes)
        return text

    # -- the cursor --------------------------------------------------------------------
    def target(self, operation: int) -> int:
        """Where a cursor move goes (QTextCursor::MoveOperation)."""
        plain, at, length = self.plain(), self.cursor, self.text.length
        def step(index: int, by: int) -> int:
            index += by
            if 0 < index < length and rm_text._splits_pair(plain, index):
                index += by
            return max(0, min(index, length))
        if operation in (LEFT, PREVIOUS_CHARACTER):
            return step(at, -1)
        if operation in (RIGHT, NEXT_CHARACTER):
            return step(at, 1)
        if operation == START:
            return 0
        if operation == END:
            return length
        if operation in (START_OF_BLOCK, END_OF_BLOCK):
            start, end = self.paragraph_range(at)
            return start if operation == START_OF_BLOCK else end
        if operation == PREVIOUS_BLOCK:
            start, _ = self.paragraph_range(at)
            return self.paragraph_range(start - 1)[0] if start else 0
        if operation == NEXT_BLOCK:
            _, end = self.paragraph_range(at)
            return min(end + 1, length)
        if operation in (WORD_LEFT, PREVIOUS_WORD, START_OF_WORD):
            chars = rm_text.cut(plain, 0, at)
            k = len(chars)
            while k and not _WORD.match(chars[k - 1]):
                k -= 1
            while k and _WORD.match(chars[k - 1]):
                k -= 1
            return rm_text.units(chars[:k])
        if operation in (WORD_RIGHT, NEXT_WORD, END_OF_WORD):
            chars = rm_text.cut(plain, at)
            k = 0
            if operation == END_OF_WORD:
                while k < len(chars) and not _WORD.match(chars[k]):
                    k += 1
                while k < len(chars) and _WORD.match(chars[k]):
                    k += 1
            else:
                while k < len(chars) and _WORD.match(chars[k]):
                    k += 1
                while k < len(chars) and not _WORD.match(chars[k]):
                    k += 1
            return at + rm_text.units(chars[:k])
        lines = self.lines()
        if not lines:
            return at
        n = line_of(lines, at)
        if operation == START_OF_LINE:
            return lines[n].start
        if operation == END_OF_LINE:
            line = lines[n]
            return line.end if n + 1 == len(lines) or lines[n + 1].paragraph != line.paragraph else max(line.start,
                                                                                                          line.end - 1)
        if operation in (UP, DOWN):
            m = n - 1 if operation == UP else n + 1
            if m < 0:
                return 0
            if m >= len(lines):
                return length
            caret = self.caret(at)
            line = lines[m]
            return index_at([line], caret.x + self.origin, line.baseline, self.measure)
        return at

    def move(self, operation: int, mode: int = MOVE_ANCHOR) -> None:
        self._stop_typing()
        self.pending_style = None
        self._set_cursor(self.target(operation), keep=mode == KEEP_ANCHOR)

    def place(self, index: int, keep: bool = False) -> None:
        self._stop_typing()
        self.pending_style = None
        self._set_cursor(index, keep)

    def select_all(self) -> None:
        self.place(0)
        self._set_cursor(self.text.length, keep=True)

    def select_range(self, start: int, end: int) -> None:
        self.place(start)
        self._set_cursor(end, keep=True)

    def select_word(self) -> None:
        start, end = self.target(START_OF_WORD), self.target(END_OF_WORD)
        if end <= self.cursor:
            start = self.cursor
        self.select_range(start, max(end, self.cursor))

    def select_paragraph(self) -> None:
        self.select_range(*self.paragraph_range(self.cursor))

    # -- handing over ---------------------------------------------------------------------
    def take(self) -> tuple[list[TextEdit], list[list]]:
        """The edits made, and their undo steps, to keep (and forget here)."""
        edits, steps = self.edits, self.steps
        self.edits, self.steps = [], []
        self._stop_typing()
        return edits, steps

    def key(self, action: dict) -> bool:
        """Do a key action on the text; whether it was one this does (copy, paste,
        undo and redo are the caller's)."""
        kind = int(action.get("type", NONE))
        if kind == KEYSTROKE:
            self.type_text(str(action.get("text") or ""))
        elif kind == ENTER:
            self.enter()
        elif kind == BACKSPACE:
            self.backspace()
        elif kind == DELETE:
            self.delete_forward()
        elif kind == DELETE_START_OF_WORD:
            self.delete_to(WORD_LEFT)
        elif kind == DELETE_END_OF_WORD:
            self.delete_to(END_OF_WORD)
        elif kind == DELETE_START_OF_LINE:
            self.delete_to(START_OF_LINE)
        elif kind == DELETE_END_OF_LINE:
            self.delete_to(END_OF_LINE)
        elif kind == CURSOR_MOVE:
            self.move(int(action.get("operation", 0)), int(action.get("mode", 0)))
        elif kind == SELECT_ALL:
            self.select_all()
        elif kind in STYLE_ACTIONS:
            self.cycle_style(STYLE_ACTIONS[kind])
        elif kind == BOLD_STYLE:
            self.toggle_style(BOLD_FLAG)
        elif kind == ITALIC_STYLE:
            self.toggle_style(ITALIC_FLAG)
        elif kind == INDENT:
            self.indent(1)
        elif kind == UNINDENT:
            self.indent(-1)
        elif kind == LINE_BREAK:
            pass
        else:
            return False
        return True
