"""Typed text of a page, written and changed the way reMarkable.exe (3.29) does it.

A page's text is one RootText block (rm_parser.decode_root_text): a sequence of runs,
each with the ids of its characters and the neighbours it was put between, and the
styles of its paragraphs. Here it is held as the app holds it, a list of runs in
reading order (TextSequence), and changed as the app's text document changes it
(TextDocument); encode_root_text writes it back, and edit_text does all of it to a
page.

Reading a block (0x1409164b0). Entries that are not listed by rising id (counter,
then the author's UUID) are taken in the order listed, one run each: that is how the
app writes them. Entries listed by rising id are put in one by one (0x1409350a0):

  - an entry is dropped when its id has no counter, when its left or right is not
    older than it, or when it is live text with nothing in it (0x140943f30);
  - the run holding its `left` is found. When that run also holds its `right`, the
    run is cut after `left`. Otherwise, when the entry just continues that run (same
    author, the next id, the same right, both live text), it is added to it
    (0x14092e6d0, "concatted") and that is all;
  - from there on to its `right`, every run `o` in the way is looked at:
      o has the same left as the entry
          and the same right:  the entry goes before o when its id is the smaller
                               (same author: the counter; else the author's UUID,
                               0x140a984f0), else the search goes on past o;
          o's right is nearer: o may come first; the place before o is remembered
                               and the search goes on;
          else:                the search goes on past o;
      o's left is before the entry's left: the entry goes here;
      o's left is after it: o belongs to something already passed; the search goes on.
    The entry goes before the place remembered, or else where the search stopped.
    "No left" counts as the first run: the app does not tell the start of the text
    from its first run there, and neither does this.

Changing it (text positions count UTF-16 units of live text; markers and deleted
runs take none):

  - a position is the first live run of text it falls in, so a position between two
    runs is before the second, after whatever markers and deleted runs lie between
    (0x140aa5cb0);
  - typed text is one run with fresh ids, one per unit; its left is the last id of
    the run before that place, its right the first id of the run there. A run it
    falls inside is cut in two, the second half starting at its own id, with the id
    before it as left and the run's right as right (0x140aae250, 0x140940520). Text
    that just continues the run before it is added to that run;
  - deleting marks runs deleted, cutting them where the range starts or ends
    inside; markers in the range stay (0x140ab8970, 0x14093d670). A deleted run
    has no value on disk, only its length. A run deleted right before one deleted
    earlier in the same go joins it, as when backspacing;
  - bold and italic are a start marker before the first run of the range and an end
    marker after its last, each a run of its own with a fresh id, the end's one more
    than the start's. Text of the range that has the style already is left as it is:
    the markers go around the rest (0x140aaee30). Taking a style off deletes the
    markers of every stretch that reaches into the range and puts new ones around
    what of it lies outside (0x140ab4710);
  - a paragraph's style is kept under the id of the newline before it ((0, 0) for
    the first) with a fresh id as its timestamp (0x140a92f70).

Ids come from the page's clock: one past the highest counter of the page, under the
author's index on that page (rm_writer). The page's PageStats block counts the text:
textChars is its length in UTF-16 units plus one, textLines its paragraphs (so on all
34 text blocks of a real library).

Not rebuilt: joining an entry whose ids the text already has (0x140942e00; a file
does not hold one twice, and such text is refused here), and the order of the styles
on disk beyond "as they were, new ones last" (the app keeps them in a hash map).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

from . import rm_writer as w
from .rm_parser import (FORMAT_BOLD_END, FORMAT_BOLD_START, FORMAT_ITALIC_END, FORMAT_ITALIC_START,
                        FORMAT_LINK_END, FORMAT_LINK_START, STYLE_IMAGE, STYLE_LIST, STYLE_TEXT,
                        ParagraphStyle, RmParseError, RootText, StyleEntry, TextItem, _style_from_legacy,
                        _style_to_legacy, decode_root_text)

ZERO = (0, 0)
OBJECT = 0xFFFFFFFF             # the "format" of an inline object (an image): one unit long
BOLD, ITALIC, LINK = "bold", "italic", "link"
MARKERS = {BOLD: (FORMAT_BOLD_START, FORMAT_BOLD_END), ITALIC: (FORMAT_ITALIC_START, FORMAT_ITALIC_END),
           LINK: (FORMAT_LINK_START, FORMAT_LINK_END)}
STYLE_OF = {code: (name, n == 0) for name, codes in MARKERS.items() for n, code in enumerate(codes)}

TEXT_Y = 234.0                  # where a new text box is put
TEXT_WIDTH = 936.0


class TextError(RmParseError):
    """Text this module does not change (or an edit that does not fit it)."""


# ------------------------------------------------------------------ writing a block

def encode_text_item(item: TextItem) -> bytes:
    """One entry of the sequence (0x14091f9d0): its value only when nothing is deleted,
    the format only when it is set."""
    body = w.f_id(2, item.item_id) + w.f_id(3, item.left) + w.f_id(4, item.right) + w.f_u32(5, item.deleted_length)
    if item.data is not None:
        value = w.varuint(len(item.data)) + bytes([item.utf_type]) + item.data
        if item.format:
            value += w.f_u32(2, item.format)
        body += w.f_sub(6, value + item.value_rest)
    return w.f_sub(0, body + item.rest)


def encode_style_entry(entry: StyleEntry) -> bytes:
    """One paragraph style (0x1409103b0)."""
    value = b"" if entry.legacy is None else w.f_u8(1, entry.legacy)
    if entry.base_style is not None:
        value += w.f_u8(2, entry.base_style)
    if entry.properties is not None:
        value += w.f_u32(3, entry.properties)
    return w.crdt_id(entry.char_id) + w.f_id(1, entry.timestamp) + w.f_sub(2, value + entry.rest)


def encode_root_text(root: RootText) -> bytes:
    """The payload of a RootText block (0x14091ff90, 0x1409203b0, 0x14091fd50): what
    decode_root_text read, byte for byte."""
    unknown = root.unknown.get
    items = b"".join(encode_text_item(item) for item in root.items)
    styles = b"".join(encode_style_entry(entry) for entry in root.styles)
    sequence = w.f_sub(1, w.varuint(len(root.items)) + items + unknown("items", b"")) + unknown("sequence", b"")
    formatting = w.f_sub(1, w.varuint(len(root.styles)) + styles + unknown("styles", b"")) + unknown("formatting", b"")
    payload = (w.f_id(1, root.block_id)
               + w.f_sub(2, w.f_sub(1, sequence) + w.f_sub(2, formatting) + unknown("text", b""))
               + w.f_sub(3, struct.pack("<dd", root.x, root.y) + unknown("position", b""))
               + w.f_f32(4, root.width_legacy))
    if root.width is not None or "settings" in root.unknown:
        lww = b"" if root.width is None else \
            w.f_sub(1, w.f_id(1, root.width_timestamp) + w.f_f32(2, root.width) + unknown("width", b""))
        payload += w.f_sub(5, lww + unknown("settings", b""))
    return payload + unknown("root", b"")


# ------------------------------------------------------------------ UTF-16 units

def units(text: str) -> int:
    """The length of `text` as the app counts it (QString: UTF-16 units)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def cut(text: str, start: int, end: int | None = None) -> str:
    """text[start:end] counted in UTF-16 units."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return raw[2 * start:None if end is None else 2 * end].decode("utf-16-le", "surrogatepass")


def _utf8(text: str) -> bytes:
    """`text` as stored; half of a character (a pair cut in two) becomes U+FFFD."""
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in text).encode("utf-8")


def _splits_pair(text: str, at: int) -> bool:
    raw = text.encode("utf-16-le", "surrogatepass")
    if not 0 < at < len(raw) // 2:
        return False
    return 0xDC00 <= int.from_bytes(raw[2 * at:2 * at + 2], "little") <= 0xDFFF


# ------------------------------------------------------------------ the sequence

@dataclass
class Run:
    """A run of the sequence, as the app holds one in memory. A deleted run keeps the
    text it had while the app runs (`text`), which no file has."""

    id: tuple[int, int]
    left: tuple[int, int] = ZERO
    right: tuple[int, int] = ZERO
    text: str = ""
    format: int = 0
    deleted: int = 0
    source: TextItem | None = field(default=None, repr=False)    # the entry it was read from, while unchanged

    @classmethod
    def of(cls, item: TextItem) -> "Run":
        return cls(item.item_id, item.left, item.right, item.text, item.format if item.data is not None else 0,
                   item.deleted_length, item)

    @property
    def length(self) -> int:
        if self.deleted:
            return self.deleted
        return 1 if self.format else units(self.text)

    @property
    def last(self) -> tuple[int, int]:
        return self.id[0], self.id[1] + self.length - 1

    @property
    def live_text(self) -> bool:
        return not self.deleted and self.format == 0

    @property
    def marker(self) -> bool:
        return not self.deleted and self.format not in (0, OBJECT)

    def holds(self, char_id: tuple[int, int]) -> bool:
        return self.id[0] == char_id[0] and self.id[1] <= char_id[1] < self.id[1] + self.length

    def valid(self) -> bool:
        """0x140943f30."""
        counter = self.id[1]
        if not counter or self.left[1] >= counter or self.right[1] >= counter:
            return False
        return bool(self.deleted or self.format or self.text)

    def item(self) -> TextItem:
        if self.source is not None:
            return self.source
        if self.deleted:
            return TextItem(self.id, self.left, self.right, self.deleted)
        return TextItem(self.id, self.left, self.right, 0, _utf8(self.text), 1, self.format)


class TextSequence:
    """The runs of a text in reading order, and the highest counter seen of each
    author. `authors`: author index -> 16-byte UUID, which decides between two devices
    that typed at one place at once."""

    def __init__(self, authors: dict[int, bytes] | None = None) -> None:
        self.runs: list[Run] = []
        self.seen: dict[int, int] = {}
        self.authors = dict(authors or {})

    # ids ---------------------------------------------------------------------
    def _uuid(self, author: int) -> bytes:
        return self.authors.get(author, bytes(16)) if author else bytes(16)

    def before_by_counter(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        """a < b as entries are listed (0x140a98550): counter, then the author's UUID."""
        return a[1] < b[1] if a[1] != b[1] else self._uuid(a[0]) < self._uuid(b[0])

    def before_by_author(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        """a < b as two runs at one place are told apart (0x140a984f0)."""
        return a[1] < b[1] if a[0] == b[0] else self._uuid(a[0]) < self._uuid(b[0])

    def note(self, last: tuple[int, int]) -> int:
        """Take ids up to `last` as seen; how many of them are new (0x140943c20)."""
        known = self.seen.get(last[0], 0)
        if known < last[1]:
            self.seen[last[0]] = last[1]
            return last[1] - known
        return 0

    def unseen(self, char_id: tuple[int, int]) -> bool:
        return char_id[0] not in self.seen or self.seen[char_id[0]] < char_id[1]

    def find(self, char_id: tuple[int, int], start: int = 0) -> int | None:
        if not char_id[1] or self.unseen(char_id):
            return None
        return next((n for n in range(start, len(self.runs)) if self.runs[n].holds(char_id)), None)

    # reading -------------------------------------------------------------------
    @classmethod
    def load(cls, items: Iterable[TextItem], authors: dict[int, bytes] | None = None) -> "TextSequence":
        seq = cls(authors)
        runs = [Run.of(item) for item in items]
        if any(seq.before_by_counter(b.id, a.id) for a, b in zip(runs, runs[1:])):
            seq.runs = runs
            for run in runs:
                seq.note(run.last)
        else:
            for run in runs:
                seq.integrate(run)
        return seq

    def split(self, n: int, char_id: tuple[int, int]) -> int:
        """Cut run `n` so that a run starts at `char_id`; the index of that run
        (0x14093fd50)."""
        run = self.runs[n]
        offset = char_id[1] - run.id[1]
        if run.id[0] != char_id[0] or offset <= 0 or offset >= run.length:
            return n
        second = Run((run.id[0], run.id[1] + offset), (run.id[0], run.id[1] + offset - 1), run.right)
        if run.deleted:
            second.deleted, run.deleted = run.deleted - offset, offset
        else:
            if _splits_pair(run.text, offset):
                raise TextError("a run would be cut inside a character")
            second.text, run.text = cut(run.text, offset), cut(run.text, 0, offset)
        run.source = None
        self.runs.insert(n + 1, second)
        return n + 1

    def joins(self, a: Run, b: Run, either_deleted: bool = False) -> bool:
        """Whether `b` just continues `a` (0x14092e6d0)."""
        if a.last != b.left or a.id[0] != b.id[0] or a.last[1] + 1 != b.id[1] or a.right != b.right:
            return False
        if not a.valid() or not b.valid():
            return False
        if not either_deleted and bool(a.deleted) != bool(b.deleted):
            return False
        return a.format == 0 and b.format == 0 and bool(a.text) and bool(b.text)

    def join(self, a: Run, b: Run) -> None:
        """0x140942320."""
        if a.deleted:
            a.deleted += b.deleted
        else:
            a.text += b.text
        a.source = None

    def integrate(self, run: Run) -> None:
        """Put in a run that was made elsewhere (0x1409350a0)."""
        if not run.valid():
            return
        new = self.note(run.last)
        runs = self.runs
        if not runs:
            runs.append(run)
            return
        if new < run.length:
            raise TextError(f"the text has the ids of entry {run.id} already")

        left = self.find(run.left)
        if left is None:
            left = at = 0                           # "no left" and "the first run" are one
        else:
            if runs[left].holds(run.right):
                left = self.split(left, (run.left[0], run.left[1] + 1)) - 1
            elif self.joins(runs[left], run):
                self.join(runs[left], run)
                return
            at = left + 1
        end = len(runs)
        right = self.find(run.right, left)
        right = end if right is None else right

        waiting, place = False, at
        while True:
            place = place if waiting else at
            if at >= end or at == right:
                break
            o = runs[at]
            o_left = self.find(o.left, left)
            o_left = 0 if o_left is None else o_left
            o_right = self.find(o.right, o_left)
            o_right = end if o_right is None else o_right
            right_known = not o.right[1] or o_right != end
            if o_left == left:
                if o_right == right:
                    if right_known and self.before_by_author(run.id, o.id):
                        break
                    waiting = False
                elif o_right != end and (right == end or o_right <= right):
                    waiting = True
                else:
                    waiting = False
            elif o_left <= left:
                break
            at += 1
        runs.insert(place, run)

    # writing -------------------------------------------------------------------
    def items(self) -> list[TextItem]:
        return [run.item() for run in self.runs]


# ------------------------------------------------------------------ paragraph styles

def stored_style(style: ParagraphStyle) -> tuple[int, int | None, int | None]:
    """(style_legacy, base_style, style_properties) of a paragraph type as the app stores
    it: the legacy byte always (0x1408b4ee0, 0x140a93100), the other two only when the
    byte cannot say it all (0x140a92b30)."""
    style = ParagraphStyle(style)
    if style is ParagraphStyle.SUBHEADING:
        return 3, STYLE_TEXT, 3
    legacy = {ParagraphStyle.TITLE: 2, ParagraphStyle.SUBHEADING_2: 3, ParagraphStyle.PARAGRAPH: 1}.get(
        style, int(style) + 1)
    base, properties = _style_from_legacy(legacy)
    if base == STYLE_LIST and (properties & 0xF or properties >> 8 & 7 > 1):
        return legacy, base, properties
    return _style_to_legacy(base, properties), None, None


# ------------------------------------------------------------------ the text document

class Clock:
    """The page's clock, as far as one author takes ids from it."""

    def __init__(self, author: int, highest: int) -> None:
        self.author, self.highest = author, highest

    def take(self, count: int = 1) -> tuple[int, int]:
        """The first of `count` fresh ids in a row."""
        first = (self.author, self.highest + 1)
        self.highest += count
        return first


class TextDocument:
    """A page's text, read from its RootText block and changed as the app changes it."""

    def __init__(self, root: RootText, authors: dict[int, bytes] | None = None) -> None:
        self.root = root
        self.sequence = TextSequence.load(root.items, authors)
        self.styles = list(root.styles)

    @classmethod
    def new(cls, clock: Clock, width: float = TEXT_WIDTH, style: ParagraphStyle = ParagraphStyle.TITLE,
            authors: dict[int, bytes] | None = None) -> "TextDocument":
        """The text box of a page that had none (SceneController::createRootDocument,
        called with Title by DocumentViewport.qml): centred, its first paragraph styled."""
        legacy, base, properties = stored_style(style)
        return cls(RootText(ZERO, [], [StyleEntry(ZERO, clock.take(), legacy, base, properties)],
                            -width / 2, TEXT_Y, width), authors)

    # reading -------------------------------------------------------------------
    @property
    def runs(self) -> list[Run]:
        return self.sequence.runs

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs if run.live_text)

    @property
    def length(self) -> int:
        return sum(run.length for run in self.runs if not run.deleted and not run.marker)

    def stats(self) -> tuple[int, int]:
        """(textChars, textLines) of the page's PageStats."""
        return self.length + 1, self.text.count("\n") + 1

    def locate(self, index: int) -> tuple[int, int, dict[str, int]]:
        """(run, offset in it, how deep in bold, italic and link) of a text position;
        the run is len(runs) at the end of the text (0x140aa5cb0)."""
        depth = {BOLD: 0, ITALIC: 0, LINK: 0}
        at = 0
        for n, run in enumerate(self.runs):
            if run.deleted:
                continue
            if run.marker:
                if run.format in STYLE_OF:
                    name, start = STYLE_OF[run.format]
                    depth[name] += 1 if start else -1
                continue
            if index < at + run.length:
                return n, index - at, depth
            at += run.length
        return len(self.runs), 0, depth

    def position_of(self, char_id: tuple[int, int]) -> tuple[int, bool] | None:
        """(text position, whether it is live) of the character with this id; a
        deleted one is where it was. None: the text has no such character."""
        at = 0
        for run in self.runs:
            counts = not run.deleted and not run.marker
            if run.holds(char_id):
                return (at + char_id[1] - run.id[1], True) if counts else (at, False)
            if counts:
                at += run.length
        return None

    def id_at(self, index: int) -> tuple[int, int] | None:
        """The id of the character at a text position."""
        n, offset, _ = self.locate(index)
        return None if n == len(self.runs) else (self.runs[n].id[0], self.runs[n].id[1] + offset)

    def paragraph_keys(self) -> list[tuple[int, int]]:
        """What each paragraph's style is kept under: (0, 0), then the newlines' ids."""
        keys = [ZERO]
        for run in self.runs:
            if run.live_text:
                at = 0
                for char in run.text:
                    if char == "\n":
                        keys.append((run.id[0], run.id[1] + at))
                    at += 2 if ord(char) > 0xFFFF else 1
        return keys

    def style_of(self, key: tuple[int, int]) -> ParagraphStyle:
        best = None
        for entry in self.styles:
            if entry.char_id == key and (best is None or w.id_key(entry.timestamp) >= w.id_key(best.timestamp)):
                best = entry
        return best.style if best else ParagraphStyle.PARAGRAPH

    def has_format(self, index: int, length: int, style: str) -> bool:
        """Whether all the text of a range has the style (none: False)."""
        start, end = MARKERS[style]
        depth = at = 0
        covered = False
        for run in self.runs:
            if run.deleted:
                continue
            if run.marker:
                depth += (run.format == start) - (run.format == end)
                continue
            if at < index + length and at + run.length > index:
                if depth <= 0:
                    return False
                covered = True
            at += run.length
        return covered

    # changing ------------------------------------------------------------------
    def _check(self, index: int, length: int = 0) -> None:
        if index < 0 or length < 0 or index + length > self.length:
            raise TextError(f"range {index}+{length} is outside the text ({self.length})")
        text = self.text
        if _splits_pair(text, index) or _splits_pair(text, index + length):
            raise TextError("the position is inside a character")

    def _put(self, run: Run, n: int, offset: int) -> int:
        """Put `run` before run `n`, or inside it at `offset`; where it is now
        (0x140aae250)."""
        seq, runs = self.sequence, self.runs
        if offset:
            n = seq.split(n, (runs[n].id[0], runs[n].id[1] + offset))
        run.right = runs[n].id if n < len(runs) else ZERO
        run.left = runs[n - 1].last if n else ZERO
        if not run.valid():
            raise TextError(f"entry {run.id} does not fit the text")
        seq.note(run.last)
        if n and not offset and seq.joins(runs[n - 1], run):
            seq.join(runs[n - 1], run)
            return n - 1
        runs.insert(n, run)
        return n

    def insert(self, index: int, text: str, clock: Clock, *, bold: bool | None = None,
               italic: bool | None = None) -> tuple[int, int] | None:
        """Type `text` at a position; the id of its first character (0x140aae810).
        Where the position lies between a styled stretch and what follows, `bold` and
        `italic` say on which side of the markers the text goes (0x140aa5a30); not
        given, it goes after them."""
        self._check(index)
        if not text:
            return None
        if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
            raise TextError("the text has half a character")
        n, offset, depth = self.locate(index)
        wanted = {BOLD: bold, ITALIC: italic}
        if not offset and any(v is not None and v != (depth[k] > 0) for k, v in wanted.items()):
            at, state = n, dict(depth)
            while at > 0:
                at -= 1
                run = self.runs[at]
                if run.deleted:
                    continue
                if not run.marker:
                    break
                if run.format in STYLE_OF:
                    name, start = STYLE_OF[run.format]
                    state[name] -= 1 if start else -1
                if all(v is None or v == (state[k] > 0) for k, v in wanted.items()):
                    n = at
                    break
        first = clock.take(units(text))
        self._put(Run(first, text=text), n, offset)
        return first

    def _delete_in(self, n: int, offset: int, count: int) -> int:
        """Delete `count` units of run `n` from `offset`; the index of the run after
        what was deleted (0x14093d670)."""
        seq, runs = self.sequence, self.runs
        run = runs[n]
        length = run.length
        after = runs[n + 1] if n + 1 < len(runs) else None
        if offset + count == length and after is not None and after.deleted and seq.joins(run, after, True):
            if offset == 0:
                run.deleted = after.length + count
                run.source = None
                del runs[n + 1]
                return n + 1
            run.text = cut(run.text, 0, offset)
            run.source = None
            after.deleted += length - offset
            after.id, after.left = (run.id[0], run.last[1] + 1), run.last
            after.source = None
            return n + 1
        if offset:
            n = seq.split(n, (run.id[0], run.id[1] + offset))
        if count < runs[n].length:
            seq.split(n, (runs[n].id[0], runs[n].id[1] + count))
        runs[n].deleted, runs[n].source = count, None
        return n + 1

    def delete(self, index: int, length: int) -> int:
        """Delete a range of the text; how many units went (0x140ab8970, 0x140ab92c0)."""
        self._check(index, length)
        n, offset, _ = self.locate(index)
        done = 0
        while done < length and n < len(self.runs):
            run = self.runs[n]
            if run.deleted or run.marker:
                n += 1
                continue
            count = min(length - done, run.length - offset)
            n = self._delete_in(n, offset, count)
            done, offset = done + count, 0
        return done

    def _marker(self, code: int, clock: Clock, n: int, offset: int) -> int:
        return self._put(Run(clock.take(), format=code), n, offset)

    def format(self, index: int, length: int, style: str, clock: Clock) -> list[tuple[int, int]]:
        """Give a range of the text a style; the ids of the markers put in, in the
        order they were made (0x140aaee30)."""
        self._check(index, length)
        start, end = MARKERS[style]
        n, offset, depth = self.locate(index)
        if n == len(self.runs) or length <= 0:
            return []
        nested, stop, at, opened, made = depth[style], index + length, index, False, []
        while at < stop and n < len(self.runs):
            run = self.runs[n]
            if not run.deleted:
                if nested or opened or run.marker:
                    if not run.marker:
                        at, offset = at + run.length - offset, 0
                    elif run.format == start:
                        if not nested and opened:
                            n = self._marker(end, clock, n, 0)
                            made.append(self.runs[n].id)
                            n += 1
                            opened = False
                        nested += 1
                    elif run.format == end:
                        nested -= 1
                else:
                    n = self._marker(start, clock, n, offset)
                    made.append(self.runs[n].id)
                    opened, offset = True, 0
            n += 1
        if opened:
            if at > stop:
                n = self._marker(end, clock, n - 1, self.runs[n - 1].length - (at - stop))
            else:
                n = self._marker(end, clock, n, 0)
            made.append(self.runs[n].id)
        return made

    def _spans(self, style: str) -> list[tuple[int, int, int, int]]:
        """(start marker, end marker, from, to) of each styled stretch: run indices
        and text positions."""
        start, end = MARKERS[style]
        open_, spans, at = [], [], 0
        for n, run in enumerate(self.runs):
            if run.deleted:
                continue
            if run.marker:
                if run.format == start:
                    open_.append((n, at))
                elif run.format == end and open_:
                    first, begun = open_.pop()
                    spans.append((first, n, begun, at))
                continue
            at += run.length
        return spans

    def unformat(self, index: int, length: int, style: str, clock: Clock
                 ) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
        """Take a style off a range of the text (0x140ab4710): the markers of every
        stretch reaching into it are deleted, and what those stretches had outside
        the range is styled anew. (markers deleted, markers made), None when nothing
        changed."""
        self._check(index, length)
        stop = index + length
        hit = [span for span in self._spans(style) if span[2] < stop and span[3] > index and span[2] < span[3]]
        if not hit or length <= 0:
            return None
        again, gone, made = [], [], []
        for first, last, begun, ended in hit:
            for n in (first, last):
                gone.append(self.runs[n].id)
                self.runs[n].deleted, self.runs[n].source = 1, None
            if begun < index:
                again.append((begun, index))
            if ended > stop:
                again.append((stop, ended))
        for begun, ended in sorted(set(again)):
            made += self.format(begun, ended - begun, style, clock)
        return gone, made

    def toggle(self, index: int, length: int, style: str, clock: Clock) -> bool:
        """Style a range, or take the style off when all of it has it; whether it is
        on now."""
        if self.has_format(index, length, style):
            self.unformat(index, length, style, clock)
            return False
        self.format(index, length, style, clock)
        return True

    def style_entry(self, key: tuple[int, int]) -> StyleEntry | None:
        """The style entry in force for a paragraph (None: it has none, body text)."""
        best = None
        for entry in self.styles:
            if entry.char_id == key and (best is None or w.id_key(entry.timestamp) >= w.id_key(best.timestamp)):
                best = entry
        return best

    def set_style(self, paragraph: int | tuple[int, int], style, clock: Clock) -> None:
        """Give a paragraph (its number, or the id its style is kept under) a style: a
        ParagraphStyle, or as stored, (style_legacy, base_style, style_properties)."""
        keys = self.paragraph_keys()
        key = keys[paragraph] if isinstance(paragraph, int) else tuple(paragraph)
        if key not in keys:
            raise TextError(f"{key} does not start a paragraph")
        stored = tuple(style) if isinstance(style, tuple) else stored_style(style)
        entry = StyleEntry(key, clock.take(), *stored)
        places = [n for n, old in enumerate(self.styles) if old.char_id == key]
        if places:
            self.styles[places[0]] = entry
            for n in reversed(places[1:]):
                del self.styles[n]
        else:
            self.styles.append(entry)

    def _after(self, item: tuple[int, int] | None) -> tuple[int, int] | None:
        """(run, offset) where something put right after the item `item` goes (None:
        at the start); None when the text has no such item."""
        if item is None or item == ZERO:
            return 0, 0
        n = self.sequence.find(item)
        if n is None:
            return None
        run = self.runs[n]
        offset = item[1] - run.id[1] + 1
        return (n + 1, 0) if offset >= run.length else (n, offset)

    def reinsert(self, after: tuple[int, int] | None, clock: Clock, text: str = "", code: int = 0
                 ) -> list[tuple[int, int]] | None:
        """Put text (or a marker, `code`) back right after the item `after`, whatever
        it is now (deleted, a marker), as the app's undo and redo put back what they
        took away (0x140ab3620, 0x140ab2160); the ids it takes, None when the text has
        no such item."""
        place = self._after(after)
        if place is None:
            return None
        if code:
            first = clock.take()
            self._put(Run(first, format=code), *place)
            return [first]
        if not text:
            return []
        first = clock.take(units(text))
        self._put(Run(first, text=text), *place)
        return [(first[0], first[1] + k) for k in range(units(text))]

    def remove(self, first: tuple[int, int], last: tuple[int, int]) -> int:
        """Delete the items from `first` to `last`, in the order of the sequence, text
        and markers alike, as the app's undo deletes what an action added by id
        (0x140ab7d10, 0x140ab92c0); how many ids went."""
        start, end = self.sequence.find(first), self.sequence.find(last)
        if start is None or end is None or end < start:
            return 0
        seq = self.sequence
        start = seq.split(start, first)
        end = self.sequence.find(last, start)
        seq.split(end, (last[0], last[1] + 1))           # nothing to cut when `last` ends its run
        done = 0
        for n in range(start, end + 1):
            run = self.runs[n]
            if not run.deleted:
                done += run.length
                run.deleted, run.source = run.length, None
        return done

    # writing -------------------------------------------------------------------
    def root_text(self) -> RootText:
        return replace(self.root, items=self.sequence.items(), styles=list(self.styles))

    def encode(self) -> bytes:
        return encode_root_text(self.root_text())


# ------------------------------------------------------------------ a page's text

def page_authors(blocks: list) -> dict[int, bytes]:
    """Author index -> UUID of a page."""
    if not blocks or blocks[0].type != w.BLOCK_AUTHORS:
        return {}
    return {index: uuid for uuid, index in w.decode_authors(blocks[0].payload)}


def page_text(page: bytes | None) -> TextDocument | None:
    """The text of a page as it is; None when it has no text box."""
    if page is None:
        return None
    blocks = w.split(page)[1]
    block = _text_block(blocks)
    return None if block is None else TextDocument(decode_root_text(blocks[block].payload), page_authors(blocks))


def _text_block(blocks: list) -> int | None:
    found = [n for n, b in enumerate(blocks) if b.type == w.BLOCK_ROOT_TEXT]
    if len(found) > 1:
        raise TextError("the page has more than one text")
    return found[0] if found else None


def _stats(payload: bytes, chars: int, lines: int) -> bytes:
    """A PageStats payload (1 loads, 2 merges, 3 textChars, 4 textLines, 5
    keyboardCount) with the text counted anew."""
    values = w.read_values(payload)
    if [(i, k) for i, k, _ in values] != [(i, w.TAG_BYTE4) for i in range(1, 6)] or w.write_values(values) != payload:
        raise TextError("the page's stats are not understood")
    new = {3: chars, 4: lines}
    return w.write_values([(i, k, struct.pack("<I", new[i]) if i in new else v) for i, k, v in values])


def edit_text(page: bytes | None, *, author: bytes, change: Callable[[TextDocument, Clock], object],
              style: ParagraphStyle = ParagraphStyle.TITLE) -> bytes:
    """`page` (None: a page with no .rm yet) after `change(text, clock)` has changed its
    text; a page without a text box gets one first. Every other block is kept byte for
    byte; replaced are the RootText block and the text counts of PageStats, and
    `author` joins the authors block if it is new there. A change that leaves the text
    as it was gives the page back as it was.

    The text box goes where the app has it: after the scene's SceneTreeMove blocks,
    before its tree nodes (so on every real page with text)."""
    given = page
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    before, original = list(blocks), [b.payload for b in blocks]
    me = w._author_index(blocks, author)
    clock = Clock(me, w.highest_counter(blocks))
    at = _text_block(blocks)
    authors = page_authors(blocks)
    if at is None:
        text = TextDocument.new(clock, style=style, authors=authors)
        was = None
    else:
        was = blocks[at].payload
        root = decode_root_text(was)
        if encode_root_text(root) != was:
            raise TextError("the page's text is not understood")
        text = TextDocument(root, authors)
    change(text, clock)
    now = text.encode()
    if at is not None and now == was:
        return given

    replaced = {0}
    if at is None:
        moves = [n for n, b in enumerate(blocks) if b.type == w.BLOCK_TREE_MOVE]
        nodes = [n for n, b in enumerate(blocks) if b.type == w.BLOCK_TREE_NODE]
        if not moves and not nodes:
            raise TextError("the page has no scene tree to put the text by")
        blocks.insert(moves[-1] + 1 if moves else nodes[0], w.new_block(w.BLOCK_ROOT_TEXT, now))
    else:
        blocks[at] = w.new_block(w.BLOCK_ROOT_TEXT, now)
        replaced.add(at)
    stats = [n for n, b in enumerate(before) if b.type == w.BLOCK_PAGE_STATS]
    if len(stats) != 1:
        raise TextError("the page does not have one PageStats")
    place = next(n for n, b in enumerate(blocks) if b is before[stats[0]])
    counted = _stats(before[stats[0]].payload, *text.stats())
    if counted != before[stats[0]].payload:
        blocks[place] = w.new_block(w.BLOCK_PAGE_STATS, counted)
        replaced.add(stats[0])
    w._check_untouched(before, original, blocks, replaced)
    w.block_ids(blocks[_text_block(blocks)])            # the page's clock must still read it
    return w.join(header, blocks)
