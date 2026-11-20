"""Text selection on PDF and EPUB pages (EPUBs show their tablet-rendered PDF), as
reMarkable.exe 3.29's desktop client selects and copies it (ARCHITECTURE.md §5.9).

The app's QML (DocumentViewport.qml) drives it: a left drag on a PDF page calls
SceneController.selectGlyphsFromPositions(anchor, cursor), a double (or triple) click
selectGlyphWordAtPoint(point), and the controller answers with glyphsSelected(areas,
text, startIndex, endIndex, startHandlePos, endHandlePos), which PdfTextSelection.qml
paints (one rectangle per line) and copies (Clipboard.setTextFromString).

What 3.29 does, and what this module does with QtPdf (PDFium inside, no API for a
character's origin or line):
  - Glyphs: every character PDFium's text page has (FPDFText_GetUnicode / GetCharBox /
    GetCharOrigin, 0x14071dae0), generated and zero-size ones included. Here: the same
    characters through QPdfDocument.getAllText (one per PDFium index) and their boxes
    through getSelectionAtIndex, both PDFium's.
  - Lines: 3.29 groups consecutive glyphs whose baselines are within 1.0 (0x140a2af00).
    QtPdf gives no baselines; the lines here are PDFium's own, the runs between the line
    breaks it generates in the page text (deviation, documented).
  - Hit-testing (0x140a2c160): the glyph nearest the point among the lines nearest it,
    with no distance limit, so a drag that starts in a margin still selects. Here: the
    nearest line by distance to its box, then the character under (or nearest to) x in
    it, by bisecting the line's prefix boxes.
  - A range is inclusive at both ends, trimmed of whitespace and non-printable glyphs at
    either end (0x140a516b0), cut into line segments; each segment is one area and one
    string, the strings joined with "\\n" (QStringList::join, 0x1409b4027).
  - Areas (0x140a4fc80): the segment's left..right, from 0.25 x ascent above the line's
    top to 0.25 x ascent below its baseline (ascent = baseline - top over the line).
    Here, without baselines: ascent taken as 0.8 of the line's box height, so the area
    runs from top - 0.2 h to the box's bottom (deviation: within a fraction of a line).
  - Handles: the first area's left and the last area's right, at half their height.
  - Word (double click, 0x140a51550): nothing on whitespace or a non-printable glyph;
    else the glyph and its neighbours on the same line that are neither whitespace nor
    punctuation (QChar::isSpace / isPunct).
  - Copied text: as 3.29 joins it, with typographic ligatures (U+FB00-FB06, which PDFium
    reports as one character) spelled out so they paste as letters, and each line trimmed
    of the spaces at its ends, lines of spaces only left out (client additions).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF

PDF_PX_PER_PT = 226 / 72          # scene pixels per PDF point (ui.pages.PDF_PX_PER_PT)
MIN_BOX_PT = 1.0                  # a box flat in one direction is made this thick
AREA_PAD = 0.2                    # area above a line's box, in line heights (see above)
LINE_BREAKS = "\r\n  "
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
             "ﬅ": "st", "ﬆ": "st"}


# ------------------------------------------------------------------ shared with docsearch

def pdf_frame(page_size) -> QRectF:
    """Where a PDF page of `page_size` points lies in scene coordinates."""
    w, h = page_size.width() * PDF_PX_PER_PT, page_size.height() * PDF_PX_PER_PT
    return QRectF(-w / 2, 0, w, h)


def to_scene(rect: QRectF, frame: QRectF, scale: float) -> QRectF:
    """A rectangle in PDF points (top-left origin, as QtPdf gives them) -> scene."""
    return QRectF(frame.x() + rect.x() * scale, frame.y() + rect.y() * scale,
                  rect.width() * scale, rect.height() * scale)


def solid(box: QRectF) -> QRectF:
    """`box` at least MIN_BOX_PT wide and high, about its centre."""
    w, h = max(box.width(), MIN_BOX_PT), max(box.height(), MIN_BOX_PT)
    c = box.center()
    return QRectF(c.x() - w / 2, c.y() - h / 2, w, h)


def selection_boxes(doc, pdf_page: int, start: int, length: int) -> list[QRectF]:
    """PDFium's boxes (points) for characters start..start+length of a page: one per run
    it reports, else the selection's bounding rectangle."""
    selection = doc.getSelectionAtIndex(pdf_page, start, length)
    boxes = [polygon.boundingRect() for polygon in selection.bounds()]
    if not boxes:
        box = selection.boundingRectangle()
        boxes = [box] if box.isValid() else []
    return boxes


def _union(boxes) -> QRectF | None:
    """The union of boxes with an area; boxes flat in one direction (spaces, generated
    characters) only when nothing else has one, made MIN_BOX_PT thick."""
    boxes = list(boxes)
    solid_boxes = [b for b in boxes if b.width() > 0 and b.height() > 0]
    if not solid_boxes:
        solid_boxes = [solid(b) for b in boxes if b.width() > 0 or b.height() > 0]
    if not solid_boxes:
        return None
    out = QRectF(solid_boxes[0])
    for b in solid_boxes[1:]:
        out = out.united(b)
    return out


# ------------------------------------------------------------------ text helpers

def is_blank(ch: str) -> bool:
    """Whitespace or not printable (QChar::isSpace || !QChar::isPrint): trimmed from a
    range's ends, and never the start of a word."""
    return ch.isspace() or not ch.isprintable() or ch in LINE_BREAKS


def is_word_break(ch: str) -> bool:
    """What ends a word (QChar::isSpace || QChar::isPunct)."""
    return is_blank(ch) or unicodedata.category(ch).startswith("P")


def clean(text: str) -> str:
    """Copied text: ligatures spelled out, line breaks the page text carries inside a
    segment (PDFium's \\r\\n) as plain newlines."""
    for lig, letters in LIGATURES.items():
        text = text.replace(lig, letters)
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ------------------------------------------------------------------ the page's text

@dataclass
class Line:
    start: int                 # first character (PDFium index)
    end: int                   # one past the last character (line break excluded)
    box: QRectF                # scene coordinates


@dataclass
class Selection:
    areas: list[QRectF]
    text: str
    start: int = -1
    end: int = -1              # inclusive, as glyphsSelected's endIndex
    start_handle: QPointF = field(default_factory=QPointF)
    end_handle: QPointF = field(default_factory=QPointF)

    @property
    def empty(self) -> bool:
        return not self.areas


EMPTY = Selection([], "")


class PageText:
    """The text of one PDF page, for selecting it: built once (one QtPdf call per line),
    then each hit test bisects a line in a few calls. Scene coordinates throughout."""

    def __init__(self, doc, pdf_page: int) -> None:
        self.doc, self.pdf_page = doc, int(pdf_page)
        size = doc.pagePointSize(self.pdf_page)
        self.frame = pdf_frame(size) if size.width() > 0 and size.height() > 0 else QRectF()
        self.scale = self.frame.width() / size.width() if self.frame.isValid() else 1.0
        self.text = doc.getAllText(self.pdf_page).text() if self.frame.isValid() else ""
        self._boxes: dict[tuple[int, int], QRectF | None] = {}
        self.lines: list[Line] = []
        page = QRectF(0, 0, size.width(), size.height())
        start = 0
        n = len(self.text)
        while start < n:
            end = start
            while end < n and self.text[end] not in LINE_BREAKS:
                end += 1
            if end > start:
                box = self._points(start, end - start)
                # Text set outside the page (an EPUB's run-in above the top) is never shown,
                # so never selectable, as in search.
                if box is not None and page.intersects(box):
                    self.lines.append(Line(start, end, to_scene(box, self.frame, self.scale)))
            start = end + 1

    # -- boxes
    def _points(self, start: int, length: int) -> QRectF | None:
        key = (start, length)
        if key not in self._boxes:
            self._boxes[key] = _union(selection_boxes(self.doc, self.pdf_page, start, length))
        return self._boxes[key]

    def box(self, start: int, length: int = 1) -> QRectF | None:
        """Scene box of characters start..start+length (None: no area at all)."""
        points = self._points(start, length)
        return to_scene(points, self.frame, self.scale) if points is not None else None

    # -- hit testing
    def line_of(self, index: int) -> Line | None:
        for line in self.lines:
            if line.start <= index < line.end:
                return line
        return None

    def hit(self, pos: QPointF) -> int:
        """The character nearest scene point `pos` (-1: the page has no text)."""
        if not self.lines:
            return -1
        x, y = pos.x(), pos.y()

        def distance(line: Line) -> tuple[float, float]:
            # PDFium's lines can overlap (table cells, columns on one baseline): among
            # those the point is in, the one whose middle is nearest.
            b = line.box
            dx = b.left() - x if x < b.left() else x - b.right() if x > b.right() else 0.0
            dy = b.top() - y if y < b.top() else y - b.bottom() if y > b.bottom() else 0.0
            c = b.center()
            return dx * dx + dy * dy, (c.x() - x) ** 2 + (c.y() - y) ** 2

        line = min(self.lines, key=distance)
        if x <= line.box.left():
            return line.start
        if x >= line.box.right():
            return line.end - 1
        # Smallest prefix whose box reaches x: its last character is under x.
        lo, hi = 1, line.end - line.start
        while lo < hi:
            mid = (lo + hi) // 2
            box = self.box(line.start, mid)
            if box is not None and box.right() >= x:
                hi = mid
            else:
                lo = mid + 1
        return line.start + lo - 1

    # -- selections
    def select(self, a: int, b: int) -> Selection:
        """Characters a..b, inclusive, in either order (3.29's range selection)."""
        if a < 0 or b < 0 or not self.text:
            return EMPTY
        a, b = min(a, b), max(a, b)
        b = min(b, len(self.text) - 1)
        while a <= b and is_blank(self.text[a]):
            a += 1
        while b >= a and is_blank(self.text[b]):
            b -= 1
        if a > b:
            return EMPTY
        areas, strings = [], []
        for line in self.lines:
            s, e = max(a, line.start), min(b + 1, line.end)
            if s >= e:
                continue
            # Glyphs QtPdf gives no box at all (3.29 still has their zero-width ones):
            # the line's box stands in, so the text is never selected unseen.
            box = self.box(s, e - s) or line.box
            full = line.box
            pad = AREA_PAD * full.height()
            areas.append(QRectF(box.left(), full.top() - pad, box.width(), full.height() + pad))
            # Each line's text without the spaces at its ends, and no line for one that is
            # only spaces (client: 3.29 trims the range's two ends only, so a copy of
            # several lines carried trailing spaces and blank lines).
            segment = self.text[s:e]
            i, j = 0, len(segment)
            while i < j and is_blank(segment[i]):
                i += 1
            while j > i and is_blank(segment[j - 1]):
                j -= 1
            if i < j:
                strings.append(clean(segment[i:j]))
        if not areas:
            return EMPTY
        first, last = areas[0], areas[-1]
        return Selection(areas, "\n".join(strings), a, b,
                         QPointF(first.left(), first.y() + first.height() * 0.5),
                         QPointF(last.right(), last.y() + last.height() * 0.5))

    def select_points(self, anchor: QPointF, cursor: QPointF) -> Selection:
        return self.select(self.hit(anchor), self.hit(cursor))

    def word(self, pos: QPointF) -> Selection:
        """The word at scene point `pos` (3.29's double click)."""
        i = self.hit(pos)
        if i < 0 or is_blank(self.text[i]):
            return EMPTY
        line = self.line_of(i)
        if line is None:
            return EMPTY
        a = b = i
        while a - 1 >= line.start and not is_word_break(self.text[a - 1]):
            a -= 1
        while b + 1 < line.end and not is_word_break(self.text[b + 1]):
            b += 1
        return self.select(a, b)
