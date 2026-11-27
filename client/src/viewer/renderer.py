"""Render a parsed PageScene to SVG (pure Python) or with QPainter (PySide6).

Both back ends share `stroke_runs()` (a stroke as runs of constant width/opacity)
and `layout_text()` (typed text as positioned lines), so they cannot drift apart.

Fidelity (ARCHITECTURE.md §5.2):
  * Stroke colours are the official ARGB palette, decoded from reMarkable.exe.
  * Highlighters are opaque palette colours composited with Darken: the binary
    has no Multiply mode, sets QPainter::CompositionMode_Darken in its paint
    paths, and its highlighter colours are fully opaque. Darken over white gives
    the exact colour and leaves darker ink underneath untouched.
  * Typed text is laid out as 3.29 lays it out, in a QTextDocument with the app's
    block and character formats (layout_text with qt_measure); the app's fonts,
    reMarkable Sans / Serif Small, are registered from the assets (ensure_fonts),
    otherwise Noto Sans / Serif stand in. Approximations: pencil/brush textures (the
    app rasterises those itself) and shader opacity.

Stroke widths come from each point's recorded width, so pressure-sensitive tools
taper naturally. Eraser strokes (legacy files) take away the ink drawn before them and
show the template or PDF page under it again, as 3.29 does (eraser_outline);
erase-section, selection and zoom tools leave no ink. Layers draw bottom to top;
hidden layers are skipped unless requested. Typed text draws above the strokes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

from .rm_parser import (PAGE_HEIGHT, PAGE_WIDTH, Color, GlyphRange, ImageItem, PageScene, ParagraphStyle, Stroke,
                        TextBlock)

# Official stroke palette (ARGB), indexed by Color; built inline by the renderer in
# reMarkable.exe (e.g. VA 0x1409d9fc1). White comes from a sign-extended 64-bit store.
PALETTE_ARGB: dict[Color, int] = {
    Color.BLACK: 0xFF000000,
    Color.GRAY: 0xFF909090,
    Color.WHITE: 0xFFFFFFFF,
    Color.HIGHLIGHTER_YELLOW: 0xFFFFED75,
    Color.HIGHLIGHTER_GREEN: 0xFFACFF85,
    Color.HIGHLIGHTER_PINK: 0xFFF29EFF,
    Color.BLUE: 0xFF304AE0,
    Color.RED: 0xFFC23132,
    Color.HIGHLIGHTER_STACKED_GRAY: 0x40000000,
    Color.GREEN: 0xFF91DA71,
    Color.CYAN: 0xFF74D2E8,
    Color.MAGENTA: 0xFFC07FD2,
    Color.YELLOW: 0xFFFAE719,
    Color.HIGHLIGHTER_BLUE: 0xFFBEEAFE,
    Color.HIGHLIGHTER_ORANGE: 0xFFFFC38C,
    Color.HIGHLIGHTER_GRAY: 0xFFC7C7C6,
}
DEFAULT_ARGB = 0xFF000000
DEFAULT_HIGHLIGHT_ARGB = PALETTE_ARGB[Color.HIGHLIGHTER_YELLOW]

NOT_DRAWN = {"erase_section", "other", "unknown"}
DARKEN = {"highlighter", "shader"}

# Highlighter strokes are drawn at full opacity. The 0.9 constant in the binary
# belongs to five helpers that open a QPainter on an existing QImage, set
# CompositionMode_DestinationIn and fillRect it at opacity 0.9, i.e. they fade a
# whole pre-rendered image; none is in the stroke rasteriser (0x140b37d00+) and
# none is tied to highlighters (ARCHITECTURE.md §5.2). Change this if a runtime
# capture shows highlighter layers going through that fade.
HIGHLIGHTER_OPACITY = 1.0

# Used when a point carries no recorded width (e.g. hand-built scenes).
FALLBACK_WIDTH = {"highlighter": 30.0, "shader": 30.0, "marker": 6.0, "paintbrush": 6.0}

TEXT_COLOR = "#000000"
FONT_FAMILIES = ("reMarkable Sans", "Noto Sans", "sans-serif")   # UI text elsewhere


def argb_to_hex(argb: int) -> str:
    return f"#{argb & 0xFFFFFF:06x}"


def argb_alpha(argb: int) -> float:
    return ((argb >> 24) & 0xFF) / 255


@dataclass(frozen=True)
class Run:
    points: tuple[tuple[float, float], ...]   # page coordinates
    width: float
    opacity: float
    # The texture tile it is drawn through (viewer.brushes: "pencil2-hard.png",
    # "brush1.png") and the tile's rotation in radians; None: solid ink.
    texture: str | None = None
    angle: float = 0.0


@dataclass(frozen=True)
class StrokeStyle:
    color: str          # #rrggbb
    alpha: float        # colour alpha from the palette / ARGB value
    blend_darken: bool
    square_cap: bool


def stroke_style(stroke: Stroke, background: str = "#ffffff") -> StrokeStyle | None:
    """Colour and blending for a stroke, or None if the tool leaves no ink."""
    family = stroke.tool.family
    if family in NOT_DRAWN:
        return None
    if family == "eraser":
        return StrokeStyle(background, 1.0, False, False)
    if stroke.color == Color.ARGB and stroke.argb is not None:
        argb = stroke.argb
    else:
        argb = PALETTE_ARGB.get(stroke.color, DEFAULT_ARGB)
    if family == "highlighter" and stroke.color in (Color.BLACK, Color.GRAY, Color.UNKNOWN):
        # Legacy files store highlighter strokes with colour 0.
        argb = DEFAULT_HIGHLIGHT_ARGB
    return StrokeStyle(argb_to_hex(argb), argb_alpha(argb), family in DARKEN,
                       family == "highlighter")


def _opacity(family: str, pressure: float) -> float:
    if family == "highlighter":
        return HIGHLIGHTER_OPACITY
    if family == "shader":
        return 0.2            # approximation: shading builds up over passes
    if family in ("pencil", "mechanical_pencil"):
        return min(1.0, 0.35 + 0.65 * pressure)   # approximation of graphite texture
    return 1.0


def _quantise(value: float, step: float) -> float:
    return round(round(value / step) * step, 4)


TEXTURED = {"pencil", "mechanical_pencil", "paintbrush"}


def _texture(family: str, point) -> tuple[str | None, float]:
    """(tile, rotation) for a point of a stroke drawn through 3.29's textures
    (viewer.brushes); (None, 0.0) for solid ink or when the tiles are not installed."""
    from . import brushes

    if family in ("pencil", "mechanical_pencil"):
        name = brushes.pencil_texture(point.x, point.y, point.pressure)
        return (name, 0.0) if brushes.available(name) else (None, 0.0)
    if family == "paintbrush":
        name = brushes.brush_texture(point.x, point.y, point.speed)
        return (name, point.direction) if name and brushes.available(name) else (None, 0.0)
    return None, 0.0


def stroke_runs(scene: PageScene, stroke: Stroke,
                offset: tuple[float, float] = (0.0, 0.0)) -> list[Run]:
    """Split a stroke into runs of constant (quantised) width and opacity.

    `offset` is added to every point (anchored strokes; see stroke_offsets).
    """
    family = stroke.tool.family
    dx, dy = offset
    pts = stroke.points
    if not pts:
        return []
    fallback = FALLBACK_WIDTH.get(family, 2.0) * (stroke.thickness_scale or 1.0)
    keyed = []
    for p in pts:
        width = _quantise(p.width if p.width > 0 else fallback, 0.25) or 0.25
        keyed.append((scene.to_page(p.x + dx, p.y + dy),
                      (width, _quantise(_opacity(family, p.pressure), 0.05), *_texture(family, p))))
    if len(keyed) == 1:
        (xy, key), = keyed
        return [Run((xy, xy), *key)]
    runs: list[Run] = []
    current = [keyed[0][0]]
    key = keyed[1][1]
    for xy, k in keyed[1:]:
        if k != key and len(current) > 1:
            runs.append(Run(tuple(current), *key))
            current = [current[-1]]   # share the joint so runs stay continuous
            key = k
        current.append(xy)
    runs.append(Run(tuple(current), *key))
    return runs


# ----------------------------------------------------------------- typed text
#
# 3.29 lays typed text out with a QTextDocument (ARCHITECTURE.md 5.1, text ctor
# 0x1409247b0, formats 0x140a3c480, fonts 0x140a3c280): document margin 4, indent width
# 48 (also the tab stop), fonts with PreferNoHinting. The block formats take their top
# margins from the body font's pixel size (32) times 0.75 / 2.75 / 1.75 / 1.25; only
# body text (and the list styles, copies of it) adds a line distance of 0.2 x the font's
# height after each line; an image block adds 20. Qt drops the first block's top margin.
# With the app's fonts the body pitch is 69.546875 and the first baseline pos_y + 4 +
# 29.59375. layout_text(scene, qt_measure) builds that QTextDocument; with any other
# measure (headless SVG) the same rules run on estimated widths.

SANS = "reMarkable Sans"
SERIF = "reMarkable Serif Small"
FALLBACK_FAMILIES = {SANS: ("Noto Sans", "sans-serif"), SERIF: ("Noto Serif", "serif")}

DOCUMENT_MARGIN = 4.0
INDENT = 48.0
BODY_PX = 32
LINE_DISTANCE = 0.2          # x the font's height (0x140d70ab0), LineDistanceHeight
IMAGE_LINE_DISTANCE = 20.0   # the image block's line distance (0x140c8d5c8)
NO_WIDTH = 702.0             # a box of width 0 lays out at half the 1404 page (0x140942d16)


@dataclass(frozen=True)
class TextFont:
    size: float          # pixels on the 1404 × 1872 page
    bold: bool = False
    italic: bool = False
    family: str = SANS
    weight: int = 400    # the style's weight; a bold span draws at 700
    strike: bool = False  # a checked checkbox's text (0x140a3c3e1)


@dataclass(frozen=True)
class BlockStyle:
    family: str
    size: int
    weight: int
    top: float                       # top margin
    line_distance: float | None      # None: none; else x the font height (body) ...
    fixed_distance: float = 0.0      # ... or a fixed distance (image block)


BODY = BlockStyle(SANS, BODY_PX, 400, 0.75 * BODY_PX, LINE_DISTANCE)
BLOCK_STYLES: dict[ParagraphStyle, BlockStyle] = {
    ParagraphStyle.TITLE: BlockStyle(SERIF, 64, 300, 2.75 * BODY_PX, None),
    ParagraphStyle.SUBHEADING: BlockStyle(SERIF, 46, 400, 1.75 * BODY_PX, None),
    ParagraphStyle.SUBHEADING_2: BlockStyle(SANS, 36, 500, 1.25 * BODY_PX, None),
    ParagraphStyle.IMAGE_BLOCK: BlockStyle(SANS, BODY_PX, 400, 0.75 * BODY_PX, None, IMAGE_LINE_DISTANCE),
}

# The line break Shift+Enter types (SceneController::keyAction 0x140443380, action
# LineBreak: the keystroke path with QString(QChar(0x2028))): a character of the
# paragraph's text, stored like any other, that ends the line but not the paragraph.
LINE_SEPARATOR = " "

# List markers, drawn by the text's paint (0x14093bb80) in the paragraph's indent; a
# list paragraph's text is indented by (level + 1) x 48 (0x140a3d3df, 0x1408b64d0).
BULLETS = ("•", "◦", "-")                 # by level (static QStrings 0x1412592d8/a8/90)
BULLET_X = 20.0                           # glyph left: block x + level x 48 + 20
NUMBER_RIGHT = 30.0                       # number + "." ends at block x + level x 48 + 30
CHECKBOX_SIZE, CHECKBOX_RAISE = 40.0, 2.0  # its SVG drawn in (block x + level x 48, top - 2, 40, 40)
CHECKBOX_MARKERS = ("☐", "☑", "▣")       # unchecked, checked, indeterminate (state 2) [I: the glyph]

_LIST_KINDS = {
    ParagraphStyle.BULLETPOINT: ("bullet", 0), ParagraphStyle.BULLETPOINT_INDENTED: ("bullet", 1),
    ParagraphStyle.CHECKBOX_UNCHECKED: ("unchecked", 0), ParagraphStyle.CHECKBOX_UNCHECKED_INDENTED: ("unchecked", 1),
    ParagraphStyle.CHECKBOX_CHECKED: ("checked", 0), ParagraphStyle.CHECKBOX_CHECKED_INDENTED: ("checked", 1),
    ParagraphStyle.NUMBERED_LIST: ("number", 0), ParagraphStyle.NUMBERED_LIST_INDENTED: ("number", 1),
}


def block_style(style: ParagraphStyle) -> BlockStyle:
    return BLOCK_STYLES.get(style, BODY)


def paragraph_font(style: ParagraphStyle) -> TextFont:
    """The paragraph's own font (0x140a3c280), before bold and italic spans."""
    bs = block_style(style)
    return TextFont(bs.size, family=bs.family, weight=bs.weight,
                    strike=_LIST_KINDS.get(style, ("", 0))[0] == "checked")


def _list_level(para) -> int | None:
    """A list paragraph's indent level (the style says 0 or "indented"; the paragraph
    carries the stored level 0..2), None for any other paragraph."""
    kind = _LIST_KINDS.get(para.style)
    return None if kind is None else max(kind[1], para.indent)


def _roman(number: int) -> str:
    """Roman numerals, lower case (0x140941a10) [I: the standard subtractive form]."""
    out = ""
    for value, digits in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while number >= value:
            out += digits
            number -= value
    return out


def _number_label(number: int, level: int) -> str:
    """The numbered list's label by level mod 3 (0x14093c49e): decimal, lower-case
    letters (A..Z, base 26, lowered), roman (0x140941a10), each followed by a dot."""
    if level % 3 == 1:
        out, n = "", number
        while n > 0:
            n, r = divmod(n - 1, 26)
            out = chr(0x61 + r) + out
        return out + "."
    if level % 3 == 2:
        return _roman(number) + "."
    return f"{number}."


def list_numbers(paragraphs) -> list[int]:
    """The QTextBlock::userState of every paragraph (0x140943ca0, in order): -1 for one
    that is not a numbered list; for a numbered one, walk back over the previous
    paragraphs: one that is not numbered stops the walk (n = 0), one at the same level
    gives n = its number, a shallower one stops (n = 0), a deeper one is skipped. Its
    number is n + 1, or 1 when that reaches 1000."""
    states: list[int] = []
    levels: list[int] = []
    for para in paragraphs:
        kind = _LIST_KINDS.get(para.style)
        level = _list_level(para) or 0
        levels.append(level)
        if kind is None or kind[0] != "number":
            states.append(-1)
            continue
        n = 0
        for j in range(len(states) - 1, -1, -1):
            if states[j] < 0 or levels[j] < level:
                break
            if levels[j] == level:
                n = states[j]
                break
        states.append(1 if n + 1 >= 1000 else n + 1)
    return states


@dataclass(frozen=True)
class TextLine:
    x: float                                  # page coordinates of the baseline start
    baseline: float
    pieces: tuple[tuple[str, TextFont], ...]  # consecutive runs of text on the line
    marker: str = ""                          # list marker (first line of a list paragraph)
    marker_font: TextFont | None = None
    paragraph: int = 0
    # Where the line is in the text (TextBlock.text, counted in UTF-16 units): its first
    # character, and where the next line starts (the paragraph's newline for its last
    # line), so that a caret can be put on it; its height; and its words as laid out
    # (spaces included), each with its index.
    start: int = 0
    end: int = 0
    height: float = 0.0
    segments: tuple[tuple[int, str, TextFont], ...] = ()
    ascent: float = 0.0                       # baseline - top of the line
    # Where the marker is drawn: a text marker's baseline start, or a checkbox's box
    # (x, y, width, height); page coordinates.
    marker_at: tuple[float, ...] = ()

    @property
    def top(self) -> float:
        return self.baseline - self.ascent

    @property
    def bottom(self) -> float:
        return self.top + self.height


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


# measure(text, font) -> advance width in page pixels
Measure = Callable[[str, TextFont], float]


def estimate_width(text: str, font: TextFont) -> float:
    """Font-free width estimate for headless SVG layout."""
    return len(text) * font.size * (0.58 if font.bold or font.weight >= 600 else 0.53)


# The app's fonts' vertical metrics are proportional to the pixel size (reMarkable Sans
# 32: ascent 29.59375, height 37.75; Serif Small 64: 59.1875, 75.5); used without Qt.
ASCENT_PER_PX = 29.59375 / 32
HEIGHT_PER_PX = 37.75 / 32


def _fixed(value: float) -> float:
    """A length as Qt's layout keeps it (QFixed, 1/64 px)."""
    return round(value * 64) / 64


def estimate_metrics(font: TextFont) -> tuple[float, float, int]:
    """(ascent, height, integer ascent) of a font without Qt."""
    ascent, height = font.size * ASCENT_PER_PX, font.size * HEIGHT_PER_PX
    return ascent, height, round(ascent)


def qt_metrics(font: TextFont) -> tuple[float, float, int]:
    from PySide6.QtGui import QFontMetrics, QFontMetricsF
    qf = _qfont(font)
    m = QFontMetricsF(qf)
    return m.ascent(), m.height(), QFontMetrics(qf).ascent()


def _spans(para, base: int) -> list[tuple[int, str, TextFont]]:
    """(text index, text, font) of each span of a paragraph."""
    own = paragraph_font(para.style)
    out, at = [], base
    for span in para.spans:
        if span.text:
            out.append((at, span.text, TextFont(own.size, span.bold, span.italic, own.family, own.weight,
                                                own.strike)))
            at += _units(span.text)
    return out


def _slice(spans, start: int, end: int) -> list[tuple[int, str, TextFont]]:
    """The parts of `spans` between text indexes start and end."""
    out = []
    for at, text, font in spans:
        n = _units(text)
        a, b = max(start, at), min(end, at + n)
        if a >= b:
            continue
        raw = text.encode("utf-16-le", "surrogatepass")[2 * (a - at):2 * (b - at)]
        out.append((a, raw.decode("utf-16-le", "surrogatepass"), font))
    return out


def _line(x: float, baseline: float, ascent: float, height: float, para_index: int,
          spans, start: int, end: int) -> TextLine:
    segments = []
    for at, text, font in _slice(spans, start, end):
        cut = text.find(LINE_SEPARATOR)
        if cut >= 0:
            text = text[:cut]
        if text:
            segments.append((at, text, font))
    pieces = _merge([(t, f) for _, t, f in segments])
    while pieces and not pieces[-1][0].strip():
        pieces.pop()
    if pieces:
        pieces[-1] = (pieces[-1][0].rstrip(" "), pieces[-1][1])
    return TextLine(x, baseline, tuple(pieces), paragraph=para_index, start=start, end=end,
                    height=height, segments=tuple(segments), ascent=ascent)


@dataclass
class _Block:
    """One laid-out paragraph: its rectangle (blockBoundingRect, page coordinates) and lines."""
    x: float
    y: float
    lines: list


def layout_text(scene: PageScene, measure: Measure = estimate_width) -> list[TextLine]:
    """The scene's typed text as positioned lines, laid out as 3.29 lays it out: with
    qt_measure in a QTextDocument configured as the app's, otherwise by the same rules
    on `measure`'s widths. Lines are in page coordinates."""
    block: TextBlock | None = scene.text
    if block is None:
        return []
    left, _ = scene.to_page(block.x, block.y)
    if getattr(measure, "qt_document", False):
        blocks, metrics = _qt_blocks(block, left), qt_metrics
    else:
        blocks, metrics = _estimated_blocks(block, left, measure), estimate_metrics
    return _with_markers(block, blocks, metrics, measure)


def _bases(block: TextBlock) -> list[int]:
    out, at = [], 0
    for para in block.paragraphs:
        out.append(at)
        at += _units(para.text) + 1
    return out


def _estimated_blocks(block: TextBlock, left: float, measure: Measure) -> list[_Block]:
    """QTextDocument's block layout, word-wrapped on `measure` (no Qt needed)."""
    width = (block.width or NO_WIDTH) - 2 * DOCUMENT_MARGIN
    y = block.y + DOCUMENT_MARGIN
    out = []
    for index, (para, base) in enumerate(zip(block.paragraphs, _bases(block))):
        bs = block_style(para.style)
        own = paragraph_font(para.style)
        ascent, height, _ = estimate_metrics(own)
        line_h = float(math.ceil(height))
        gap = (_fixed(bs.line_distance * height) if bs.line_distance is not None else bs.fixed_distance)
        if index:
            y += bs.top
        level = _list_level(para)
        indent = INDENT * (level + 1) if level is not None else 0.0
        x0, max_w = left + DOCUMENT_MARGIN + indent, max(width - indent, own.size)
        spans = _spans(para, base)
        words: list[tuple[int, str, TextFont]] = []
        for at, text, font in spans:
            for i, part in enumerate(text.split(" ")):
                if i:
                    words.append((at, " ", font))
                    at += 1
                for j, piece in enumerate(part.split(LINE_SEPARATOR)):
                    if j:
                        words.append((at, LINE_SEPARATOR, font))
                        at += 1
                    if piece:
                        words.append((at, piece, font))
                        at += _units(piece)
        end = base + _units(para.text)
        breaks, used, start, started = [], 0.0, base, False
        for at, text, font in words:
            if text == LINE_SEPARATOR:
                breaks.append((start, at + 1))
                start, used, started = at + 1, 0.0, False
                continue
            w = measure(text, font)
            if text != " " and started and used + w > max_w:
                breaks.append((start, at))
                start, used, started = at, 0.0, False
            if text == " " and not started:
                if start == at:
                    start = at + 1            # a space a line does not start with
                continue
            started = True
            used += w
        breaks.append((start, end))
        lines = []
        top = y
        for n, (a, b) in enumerate(breaks):
            nxt = breaks[n + 1][0] if n + 1 < len(breaks) else end
            lines.append(_line(x0, y + ascent, ascent, line_h, index, spans, a, nxt))
            y += line_h + gap
        out.append(_Block(left + DOCUMENT_MARGIN, top, lines))
    return out


_fonts_loaded = False


def ensure_fonts() -> None:
    """Register the app's fonts (client/src/assets/fonts) with Qt once, so that layout
    and painting use reMarkable Sans / Serif Small in the Quick app, the widgets and
    offscreen test runs alike. Needs a QGuiApplication; does nothing without one."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    from pathlib import Path
    from PySide6.QtGui import QFontDatabase, QGuiApplication
    if QGuiApplication.instance() is None:
        return
    _fonts_loaded = True
    have = set(QFontDatabase.families())
    if SANS in have and SERIF in have:
        return
    for path in sorted((Path(__file__).resolve().parents[1] / "assets" / "fonts").glob("reMarkable*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def _qt_blocks(block: TextBlock, left: float) -> list[_Block]:
    """The text in a QTextDocument set up as 3.29 sets up its text (0x1409247b0,
    0x140a3c480, 0x140a3d380), read back line by line."""
    from PySide6.QtGui import QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
    ensure_fonts()
    doc = QTextDocument()
    doc.setDocumentMargin(DOCUMENT_MARGIN)
    doc.setIndentWidth(INDENT)
    option = doc.defaultTextOption()
    option.setTabStopDistance(doc.indentWidth())
    doc.setDefaultTextOption(option)
    doc.setTextWidth(block.width or NO_WIDTH)
    cursor = QTextCursor(doc)
    bases = _bases(block)
    span_lists = []
    for index, (para, base) in enumerate(zip(block.paragraphs, bases)):
        bs = block_style(para.style)
        own = paragraph_font(para.style)
        fmt = QTextBlockFormat()
        fmt.setTopMargin(bs.top)
        if bs.line_distance is not None:
            fmt.setLineHeight(bs.line_distance * qt_metrics(own)[1], 4)       # LineDistanceHeight
        elif bs.fixed_distance:
            fmt.setLineHeight(bs.fixed_distance, 4)
        level = _list_level(para)
        if level is not None:
            fmt.setProperty(QTextFormat.Property.BlockIndent, level + 1)
        chars = QTextCharFormat()
        chars.setFont(_qfont(own))
        if index == 0:
            cursor.setBlockFormat(fmt)
            cursor.setBlockCharFormat(chars)
        else:
            cursor.insertBlock(fmt, chars)
        spans = _spans(para, base)
        span_lists.append(spans)
        for _, text, font in spans:
            run = QTextCharFormat()
            run.setFont(_qfont(font))
            cursor.insertText(text, run)
    layout = doc.documentLayout()
    out = []
    qb = doc.begin()
    index = 0
    while qb.isValid() and index < len(block.paragraphs):
        rect = layout.blockBoundingRect(qb)
        tl = qb.layout()
        base = bases[index]
        end = base + _units(block.paragraphs[index].text)
        lines = []
        count = tl.lineCount()
        for k in range(count):
            ln = tl.lineAt(k)
            a = base + ln.textStart()
            b = base + tl.lineAt(k + 1).textStart() if k + 1 < count else end
            lines.append(_line(left + rect.x() + ln.x(), block.y + rect.y() + ln.y() + ln.ascent(), ln.ascent(),
                               ln.height(), index, span_lists[index], a, b))
        out.append(_Block(left + rect.x(), block.y + rect.y(), lines))
        qb = qb.next()
        index += 1
    return out


def _with_markers(block: TextBlock, blocks: list[_Block], metrics, measure: Measure) -> list[TextLine]:
    """The lines, each list paragraph's first line with its marker placed as the text's
    paint places it (0x14093bb80): from the block's rectangle (x, y), level x 48 in."""
    from dataclasses import replace
    out: list[TextLine] = []
    numbers = list_numbers(block.paragraphs)
    for index, (para, laid) in enumerate(zip(block.paragraphs, blocks)):
        kind = _LIST_KINDS.get(para.style)
        lines = laid.lines
        if kind is not None and lines:
            what, level = kind[0], _list_level(para)
            font = paragraph_font(para.style)
            indent = level * INDENT
            _, _, int_ascent = metrics(font)
            if what == "bullet":
                marker = BULLETS[min(level, len(BULLETS) - 1)]
                at = (laid.x + indent + BULLET_X, laid.y + int_ascent)
            elif what == "number":
                marker = _number_label(numbers[index], level)
                at = (laid.x + indent + NUMBER_RIGHT - _ink_width(marker, font, measure), laid.y + int_ascent)
            else:
                # state 2 (indeterminate, 0x1408b5ef0) is drawn as its own box, no strike-out
                marker = CHECKBOX_MARKERS[1 if what == "checked" else 2 if para.state == 2 else 0]
                at = (laid.x + indent, laid.y - CHECKBOX_RAISE, CHECKBOX_SIZE, CHECKBOX_SIZE)
            lines = [replace(lines[0], marker=marker, marker_font=replace(font, strike=False), marker_at=at)] + lines[1:]
        out.extend(lines)
    return out


def _ink_width(text: str, font: TextFont, measure: Measure) -> float:
    """QFontMetrics::boundingRect(text).width() (integer), or the estimate without Qt."""
    if getattr(measure, "qt_document", False):
        from PySide6.QtGui import QFontMetrics
        return float(QFontMetrics(_qfont(font)).boundingRect(text).width())
    return float(round(measure(text, font)))


def checkbox_boxes(scene: PageScene, lines: list[TextLine],
                   measure: Measure = estimate_width) -> list[tuple[int, tuple[float, float, float, float]]]:
    """The checkboxes drawn on a page: (text index where the checkbox's paragraph
    starts, its box (x, y, width, height) in page coordinates). The index is what the
    app's checkbox links carry ("rmcheckbox://index=<n>": the scene's checkbox table
    is keyed by character position and shifted as the text changes, 0x140989280); the
    box is where the app draws the checkbox (0x14093c41a)."""
    block = scene.text
    if block is None:
        return []
    starts = _bases(block)
    out = []
    for line in lines:
        if line.marker not in CHECKBOX_MARKERS or len(line.marker_at) != 4 or line.paragraph >= len(starts):
            continue
        out.append((starts[line.paragraph], tuple(line.marker_at)))
    return out


def caret_x(line: TextLine, index: int, measure: Measure = estimate_width) -> float:
    """Page x of a caret before the character at text index `index` on `line`."""
    x = line.x
    for at, text, font in line.segments:
        if index <= at:
            break
        if index < at + _units(text):
            raw = text.encode("utf-16-le", "surrogatepass")[:2 * (index - at)]
            return x + measure(raw.decode("utf-16-le", "surrogatepass"), font)
        x += measure(text, font)
    return x


def line_of(lines: list[TextLine], index: int) -> int:
    """The line a caret at text index `index` is on: the one holding the character
    there, or the one it ends (the end of a paragraph)."""
    for n, line in enumerate(lines):
        if line.start <= index < line.end:
            return n
        if index == line.end and (n + 1 == len(lines) or lines[n + 1].paragraph != line.paragraph
                                  or lines[n + 1].start > index):
            return n
    return max(len(lines) - 1, 0)


def index_at(lines: list[TextLine], x: float, y: float, measure: Measure = estimate_width) -> int:
    """The text index nearest a point (page coordinates): the line under it (or the
    nearest), then the caret place nearest x on it."""
    if not lines:
        return 0
    n = min(range(len(lines)), key=lambda k: 0 if lines[k].top <= y < lines[k].bottom
            else min(abs(y - lines[k].top), abs(y - lines[k].bottom)))
    line = lines[n]
    last = line.end if line_of(lines, line.end) == n else max(line.start, line.end - 1)
    best, best_d = line.start, abs(caret_x(line, line.start, measure) - x)
    for i in range(line.start + 1, last + 1):
        d = abs(caret_x(line, i, measure) - x)
        if d < best_d:
            best, best_d = i, d
    return best


def _merge(pieces: list[tuple[str, TextFont]]) -> list[tuple[str, TextFont]]:
    out: list[tuple[str, TextFont]] = []
    for text, font in pieces:
        if out and out[-1][1] == font:
            out[-1] = (out[-1][0] + text, font)
        else:
            out.append((text, font))
    return out


def text_length(block: TextBlock) -> int:
    return sum(_units(p.text) for p in block.paragraphs) + max(len(block.paragraphs) - 1, 0)


def anchor_index(block: TextBlock, char_id: tuple[int, int]) -> int | None:
    """The text index an anchor resolves to (0x14097c4f0): its character's, the
    before-text sentinel -1, the after-text one the text's length, clamped to
    0..length-1; None for an id the text does not hold."""
    from .rm_parser import ANCHOR_SENTINEL_START
    length = text_length(block)
    if char_id == (0, ANCHOR_SENTINEL_START):
        i = -1
    elif char_id == (0, ANCHOR_SENTINEL_START + 1):
        i = length
    elif char_id in block.char_index:
        i = block.char_index[char_id]
    elif char_id in block.char_paragraph:
        i = _bases(block)[block.char_paragraph[char_id]]
    else:
        return None
    return max(0, min(i, length - 1))


def anchor_point(scene: PageScene, lines: list[TextLine], char_id: tuple[int, int],
                 measure: Measure = estimate_width) -> tuple[float, float] | None:
    """Where the character an anchor names is (0x14097c4f0, 0x1409342a0): x = text x
    + layout x + cursorToX(i), in scene coordinates, and y = the baseline of the visual
    line holding character i. None when the page has no text or the id is not in it."""
    block = scene.text
    if block is None or not lines:
        return None
    i = anchor_index(block, char_id)
    if i is None:
        return None
    return index_point(scene, lines, i, measure)


def index_point(scene: PageScene, lines: list[TextLine], index: int,
                measure: Measure = estimate_width) -> tuple[float, float]:
    """(scene x of a caret before character `index`, the baseline of its visual line)."""
    line = lines[line_of(lines, index)]
    return caret_x(line, index, measure) - scene.x_origin, line.baseline


def stroke_offsets(scene: PageScene, lines: list[TextLine],
                   measure: Measure = estimate_width) -> dict[int, tuple[float, float]]:
    """Offset per anchored stroke (keyed by id(stroke)): the group's anchor position,
    which 3.29 adds to every stored point (ARCHITECTURE.md 5.1): x = the stored
    anchorOriginX (type 2; type 1 follows its character sideways, x of the character),
    y = the baseline of the visual line holding the anchor character (a sentinel takes
    the first or the last character). An id the text does not hold (not even as a
    deleted character), or a page without text, leaves the group out of 3.29's manager:
    its strokes are drawn at their stored coordinates, offset (0, 0) (0x14097c4f0; the
    zero is [I]: the node's +0x1f0 starts at 0). A type 2 anchor whose originX timestamp
    is 0:0 takes the computed x of the character (Anchor.origin_set)."""
    offsets: dict[int, tuple[float, float]] = {}
    found: dict[tuple, tuple[float, float] | None] = {}
    for stroke in scene.strokes:
        a = stroke.anchor
        if a is None:
            continue
        if a.char_id not in found:
            found[a.char_id] = anchor_point(scene, lines, a.char_id, measure)
        point = found[a.char_id]
        if point is None:
            offsets[id(stroke)] = (0.0, 0.0)
        elif a.type == 1:
            offsets[id(stroke)] = point
        else:
            offsets[id(stroke)] = (a.origin_x if a.origin_set else point[0], point[1])
    return offsets


def _ink_bounds(scene: PageScene, lines: list[TextLine], offsets, measure: Measure):
    xs, ys = [], []
    for stroke in scene.strokes:
        dx, dy = offsets.get(id(stroke), (0.0, 0.0))
        for p in stroke.points:
            x, y = scene.to_page(p.x + dx, p.y + dy)
            xs.append(x); ys.append(y)
    for image in scene.placed_images:
        for vx, vy, _, _ in image.vertices:
            x, y = scene.to_page(vx, vy)
            xs.append(x); ys.append(y)
    for line in lines:
        if line.pieces:
            width = sum(measure(t, f) for t, f in line.pieces)
            xs += [line.x, line.x + width]
            ys += [line.top, line.bottom]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------- bounds

def render_bounds(scene: PageScene, crop: str = "page", margin: float = 20.0,
                  measure: Measure = estimate_width,
                  paper: tuple[float, float, float, float] | None = None) -> tuple[float, float, float, float]:
    """(x, y, width, height) of the area to render.

    "page" (default, faithful): the page, grown to include any ink or text outside
    it. "content": ink and text only. "clip": exactly the 1404 × 1872 page, cutting
    off anything beyond it, for fixed 3:4 thumbnails. Anchored strokes are measured
    where they are drawn. `paper` (x, y, width, height) is the page for "page" when
    it is not the scene's own 1404 × 1872 (a PDF page's frame).
    """
    if crop == "clip":
        return 0.0, 0.0, float(scene.width), float(scene.height)
    lines = layout_text(scene, measure)
    content = _ink_bounds(scene, lines, stroke_offsets(scene, lines, measure), measure)
    if crop == "content":
        if content is None:
            return 0.0, 0.0, float(scene.width), float(scene.height)
        x0, y0, x1, y1 = content
        return x0 - margin, y0 - margin, x1 - x0 + 2 * margin, y1 - y0 + 2 * margin
    if crop != "page":
        raise ValueError(f"unknown crop mode {crop!r}")
    if paper is None:
        x0, y0, x1, y1 = 0.0, 0.0, float(scene.width), float(scene.height)
    else:
        x0, y0, x1, y1 = paper[0], paper[1], paper[0] + paper[2], paper[1] + paper[3]
    if content:
        # Grow only on sides where something really lies outside the page; ink that
        # merely comes close to an edge must not change the page size.
        if content[0] < x0:
            x0 = content[0] - margin
        if content[1] < y0:
            y0 = content[1] - margin
        if content[2] > x1:
            x1 = content[2] + margin
        if content[3] > y1:
            y1 = content[3] + margin
    return x0, y0, x1 - x0, y1 - y0


def _visible_layers(scene: PageScene, include_hidden: bool):
    return [layer for layer in scene.layers if include_hidden or layer.visible]


# --------------------------------------------------------------------- images

def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of a PNG or JPEG file (the formats the app inserts), from its header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 9 <= len(data) and data[pos] == 0xFF:
            marker, length = data[pos + 1], int.from_bytes(data[pos + 2:pos + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):    # start of frame
                return int.from_bytes(data[pos + 7:pos + 9], "big"), int.from_bytes(data[pos + 5:pos + 7], "big")
            pos += 2 + length
    return None


def image_matrix(scene: PageScene, image: ImageItem, width: float, height: float
                 ) -> tuple[float, float, float, float, float, float] | None:
    """The affine map (a, b, c, d, e, f) from the image's pixels to page coordinates,
    page = (a*px + c*py + e, b*px + d*py + f), from three of its corners: each vertex puts
    texture point (u*width, v*height) at (x, y). None without three independent corners."""
    corners = image.vertices
    for i in range(len(corners)):
        for j in range(i + 1, len(corners)):
            for k in range(j + 1, len(corners)):
                (x0, y0, u0, v0), (x1, y1, u1, v1), (x2, y2, u2, v2) = corners[i], corners[j], corners[k]
                su, sv = (u1 - u0) * width, (v1 - v0) * height
                tu, tv = (u2 - u0) * width, (v2 - v0) * height
                det = su * tv - sv * tu
                if abs(det) < 1e-9:
                    continue
                ox, oy = scene.to_page(x0, y0)
                dx1, dy1, dx2, dy2 = x1 - x0, y1 - y0, x2 - x0, y2 - y0
                a, c = (dx1 * tv - dx2 * sv) / det, (dx2 * su - dx1 * tu) / det
                b, d = (dy1 * tv - dy2 * sv) / det, (dy2 * su - dy1 * tu) / det
                return a, b, c, d, ox - a * u0 * width - c * v0 * height, oy - b * u0 * width - d * v0 * height
    return None


def _svg_image(scene: PageScene, image: ImageItem) -> str | None:
    import base64
    if not image.data or not (size := image_size(image.data)):
        return None
    matrix = image_matrix(scene, image, *size)
    if matrix is None:
        return None
    mime = "image/png" if image.data[:4] == b"\x89PNG" else "image/jpeg"
    href = f"data:{mime};base64,{base64.b64encode(image.data).decode()}"
    return (f'<image width="{size[0]}" height="{size[1]}" preserveAspectRatio="none" '
            f'transform="matrix({" ".join(f"{v:.6g}" for v in matrix)})" href="{href}"/>')


# ------------------------------------------------------------------------ SVG

def _families(font: TextFont) -> list[str]:
    return [font.family, *FALLBACK_FAMILIES.get(font.family, FALLBACK_FAMILIES[SANS])]


def _weight(font: TextFont) -> int:
    return 700 if font.bold else font.weight


def _svg_font(font: TextFont) -> str:
    families = ", ".join(f"'{f}'" if " " in f else f for f in _families(font))
    weight = f' font-weight="{_weight(font)}"' if _weight(font) != 400 else ""
    style = ' font-style="italic"' if font.italic else ""
    strike = ' text-decoration="line-through"' if font.strike else ""
    return f'font-family="{families}" font-size="{font.size:g}"{weight}{style}{strike}'


# The checkbox when the app's own image (":/images/checkbox_*.svg", 48 x 48 drawn into
# the 40 x 40 box) is not to hand: its square, 8..40 of 48, and a tick when checked.
def _checkbox_shapes(box, checked: bool, indeterminate: bool = False):
    """(square, mark points, stroke width); the mark is a tick when checked and a
    horizontal bar through the middle when indeterminate [I: the resource's shape is
    only the fallback here]."""
    x, y, w, h = box
    k = w / 48.0
    square = (x + 8 * k, y + 8 * k, 32 * k, 32 * k)
    if checked:
        tick = ((x + 15 * k, y + 24.5 * k), (x + 21 * k, y + 30.5 * k), (x + 33 * k, y + 18.5 * k))
    elif indeterminate:
        tick = ((x + 15 * k, y + 24 * k), (x + 33 * k, y + 24 * k))
    else:
        tick = ()
    return square, tick, 2.5 * k


def is_eraser(stroke) -> bool:
    """A stored eraser stroke (Eraser 6, the masked eraser 22: legacy pages)."""
    return isinstance(stroke, Stroke) and stroke.tool.family == "eraser"


# Legacy eraser strokes (ARCHITECTURE.md 5.2). reMarkable.exe 3.29 draws a line whose
# tool has no strokev2 pipeline (0x140b2ace0 leaves Eraser 6 out) with its QPainter
# renderer (0x140b38240), which sends tool 6 to 0x140b37da0: that rasteriser copies the
# page's background image (the template or PDF page under the ink) over what the
# stroke covers. An eraser stroke therefore removes the ink drawn before it and shows
# what is under the page's ink again; it never paints white over a PDF or an ebook.
# Here: every item drawn before an eraser stroke is clipped by that stroke's outline
# (QPainter clip, SVG mask), the same in the viewer, PNG, PDF and SVG.

def eraser_outline(scene: PageScene, stroke: Stroke, offset: tuple[float, float] = (0.0, 0.0)):
    """The area an eraser stroke covers (a QPainterPath): its runs stroked at their
    widths with round caps and joins."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath, QPainterPathStroker

    area = QPainterPath()
    for run in stroke_runs(scene, stroke, offset):
        if all(pt == run.points[0] for pt in run.points):
            dot = QPainterPath()
            dot.addEllipse(QPointF(*run.points[0]), run.width / 2, run.width / 2)
            area = area.united(dot)
            continue
        path = QPainterPath(QPointF(*run.points[0]))
        for px, py in run.points[1:]:
            path.lineTo(px, py)
        stroker = QPainterPathStroker()
        stroker.setWidth(run.width)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        area = area.united(stroker.createStroke(path))
    return area


def erased_after(scene: PageScene, items: list, offsets: dict) -> dict:
    """id(item) -> the union of the outlines of the eraser strokes drawn after it (a
    QPainterPath), for the items (in drawing order) that have any."""
    later = None
    out: dict[int, object] = {}
    for item in reversed(items):
        if is_eraser(item):
            area = eraser_outline(scene, item, offsets.get(id(item), (0.0, 0.0)))
            later = area if later is None else later.united(area)
        elif later is not None:
            out[id(item)] = later
    return out


_BRUSH_TILE = 64


def _svg_pattern(ident: str, texture: str, color: str, alpha: float, origin: float, angle: float) -> str | None:
    """A texture tile as an SVG <pattern> of vector runs (the tiles are black and white:
    exact, no raster image), in `color` with the alphas texture_tile gives them."""
    from .brushes import tile_alphas, tile_runs

    runs = tile_runs(texture)
    if runs is None:
        return None
    paths = []
    for cells, opacity in zip(runs, tile_alphas(texture, alpha)):
        if cells and opacity > 0:
            d = "".join(f"M{x} {y}h{n}v1h-{n}z" for x, y, n in cells)
            paths.append(f'<path d="{d}" fill="{color}" fill-opacity="{opacity:.4g}"/>')
    return (f'<pattern id="{ident}" patternUnits="userSpaceOnUse" width="{_BRUSH_TILE}" height="{_BRUSH_TILE}" '
            f'patternTransform="translate({origin:g} 0) rotate({math.degrees(angle):.4g})">{"".join(paths)}</pattern>')


def svg_scene(scene: PageScene, *, background: str = "#ffffff", include_hidden: bool = False,
              measure: Measure = estimate_width, decimals: int = 2) -> list[str]:
    """The scene's SVG elements in page coordinates: one group per visible layer, bottom
    to top, holding a group per stroke (tool, colour, caps, highlighter blending) with a
    polyline per run of constant width and opacity; then the typed text. Eraser strokes
    mask what was drawn before them (eraser_outline); `measure` lays out the text (qt_measure: as the
    viewer wraps it); `decimals` is the precision of stroke coordinates."""
    lines = layout_text(scene, measure)
    offsets = stroke_offsets(scene, lines, measure)
    done: list[str] = []            # finished layers
    masks: list[str] = []           # one <mask> per eraser stroke (legacy pages)
    patterns: dict[tuple, tuple] = {}  # (tile, colour, alpha, angle) -> (id, <pattern>) (textured strokes)

    def pattern(run: Run, style: StrokeStyle) -> str | None:
        key = (run.texture, style.color, round(style.alpha * run.opacity, 3), round(run.angle, 4))
        if key not in patterns:
            ident = f"tex-{len(patterns)}"
            element = _svg_pattern(ident, run.texture, style.color, key[2], scene.x_origin, run.angle)
            if element is None:
                return None
            patterns[key] = (ident, element)
        return patterns[key][0]

    def wrap(elements: list[str], mask: str) -> list[str]:
        return [f'<g mask="url(#{mask})">', *elements, "</g>"] if elements else elements

    for index, layer in enumerate(_visible_layers(scene, include_hidden)):
        cur: list[str] = []
        for stroke in layer.items():
            if isinstance(stroke, ImageItem):
                if element := _svg_image(scene, stroke):
                    cur.append(element)
                continue
            if isinstance(stroke, GlyphRange):
                cur += _svg_glyphs(scene, stroke)
                continue
            if is_eraser(stroke):
                # What was drawn before it is masked where it passes (see eraser_outline).
                mask = f"erase-{len(masks)}"
                runs = [f'<polyline points="{" ".join(f"{px:.{decimals}f},{py:.{decimals}f}" for px, py in run.points)}" '
                        f'stroke-width="{run.width:g}"/>'
                        for run in stroke_runs(scene, stroke, offsets.get(id(stroke), (0.0, 0.0)))]
                masks.append(f'<mask id="{mask}" maskUnits="userSpaceOnUse" x="-100000" y="-100000" '
                             f'width="200000" height="200000"><rect x="-100000" y="-100000" width="200000" '
                             f'height="200000" fill="white"/><g stroke="black" fill="none" stroke-linecap="round" '
                             f'stroke-linejoin="round">{"".join(runs)}</g></mask>')
                done, cur = wrap(done, mask), wrap(cur, mask)
                continue
            style = stroke_style(stroke, background)
            if style is None:
                continue
            cap = "square" if style.square_cap else "round"
            blend = ' style="mix-blend-mode:darken"' if style.blend_darken else ""
            cur.append(f'<g data-tool="{stroke.tool.name.lower()}" stroke="{style.color}" '
                       f'fill="none" stroke-linecap="{cap}" stroke-linejoin="round"{blend}>')
            for run in stroke_runs(scene, stroke, offsets.get(id(stroke), (0.0, 0.0))):
                coords = " ".join(f"{px:.{decimals}f},{py:.{decimals}f}" for px, py in run.points)
                tile = pattern(run, style) if run.texture else None
                if tile is not None:            # the tile carries colour and coverage
                    cur.append(f'<polyline points="{coords}" stroke-width="{run.width:g}" stroke="url(#{tile})"/>')
                    continue
                opacity = run.opacity * style.alpha
                attr = f' stroke-opacity="{opacity:.4g}"' if opacity < 1 else ""
                cur.append(f'<polyline points="{coords}" stroke-width="{run.width:g}"{attr}/>')
            cur.append("</g>")
        done += [f'<g id="layer-{index}" data-name={quoteattr(layer.name)}>', *cur, "</g>"]
    defs = list(masks) + [element for _, element in patterns.values()]
    out = done + ([f'<defs>{"".join(defs)}</defs>'] if defs else [])     # after: ids stay in order
    if lines and (include_hidden or scene.root_document_visible):
        out.append(f'<g id="text" fill="{TEXT_COLOR}">')
        for line in lines:
            if line.marker in CHECKBOX_MARKERS and len(line.marker_at) == 4:
                (sx, sy, sw, sh), tick, width = _checkbox_shapes(line.marker_at, line.marker == CHECKBOX_MARKERS[1],
                                                                 line.marker == CHECKBOX_MARKERS[2])
                out.append(f'<rect x="{sx:.2f}" y="{sy:.2f}" width="{sw:.2f}" height="{sh:.2f}" fill="none" '
                           f'stroke="{TEXT_COLOR}" stroke-width="{width:g}"/>')
                if tick:
                    out.append(f'<polyline points="{" ".join(f"{px:.2f},{py:.2f}" for px, py in tick)}" fill="none" '
                               f'stroke="{TEXT_COLOR}" stroke-width="{width:g}"/>')
            elif line.marker and line.marker_font and len(line.marker_at) == 2:
                out.append(f'<text x="{line.marker_at[0]:.2f}" y="{line.marker_at[1]:.2f}" '
                           f'{_svg_font(line.marker_font)}>{escape(line.marker)}</text>')
            if line.pieces:
                spans = "".join(f'<tspan {_svg_font(font)}>{escape(text)}</tspan>'
                                for text, font in line.pieces)
                out.append(f'<text x="{line.x:.2f}" y="{line.baseline:.2f}" '
                           f'xml:space="preserve">{spans}</text>')
        out.append("</g>")
    return out


def render_svg(scene: PageScene, *, background: str | None = "#ffffff",
               include_hidden: bool = False, crop: str = "page",
               measure: Measure = estimate_width) -> str:
    x, y, w, h = render_bounds(scene, crop, measure=measure)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:g} {y:g} {w:g} {h:g}" '
           f'width="{w:g}" height="{h:g}">']
    if background:
        out.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{background}"/>')
    out += svg_scene(scene, background=background or "#ffffff", include_hidden=include_hidden, measure=measure)
    out.append("</svg>")
    return "\n".join(out)


# ------------------------------------------------------------------- QPainter

def _qfont(font: TextFont):
    """The QFont 3.29 makes for a paragraph style (0x140a3c280): family, pixel size,
    weight, PreferNoHinting, struck out for a checked checkbox; bold and italic spans."""
    from PySide6.QtGui import QFont
    ensure_fonts()
    qf = QFont()
    qf.setFamilies(_families(font))
    qf.setPixelSize(max(1, round(font.size)))
    qf.setWeight(QFont.Weight(_weight(font)))
    qf.setItalic(font.italic)
    qf.setStrikeOut(font.strike)
    qf.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return qf


def qt_measure(text: str, font: TextFont) -> float:
    from PySide6.QtGui import QFontMetricsF
    return QFontMetricsF(_qfont(font)).horizontalAdvance(text)


qt_measure.qt_document = True       # layout_text lays out in a real QTextDocument with it


LAYERS = ("all", "darken", "normal")


def paint(painter, scene: PageScene, *, background: str = "#ffffff",
          include_hidden: bool = False, layers: str = "all", blend_modes: bool = True) -> int:
    """Draw the scene with an active QPainter (page coordinates); returns how many
    strokes and text lines were drawn.

    layers: "all"; "darken" = only Darken-blended strokes (highlighter, shader);
    "normal" = everything else, including typed text. Splitting lets PDF export draw
    highlighters as their own layer and blend it with the PDF /BM /Darken mode, which
    QPdfWriter itself cannot express.
    blend_modes=False never changes the composition mode (devices without support).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter, QPainterPath

    if layers not in LAYERS:
        raise ValueError(f"unknown layers {layers!r}")

    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    lines = layout_text(scene, qt_measure)
    offsets = stroke_offsets(scene, lines, qt_measure)
    drawn = 0
    ordered = [item for layer in _visible_layers(scene, include_hidden) for item in layer.items()]
    erased = erased_after(scene, ordered, offsets) if any(is_eraser(i) for i in ordered) else {}
    everywhere = QPainterPath()
    everywhere.addRect(-1e6, -1e6, 2e6, 2e6)
    for stroke in ordered:
        if is_eraser(stroke):
            continue                    # it clips what came before it (erased_after)
        clip = erased.get(id(stroke))
        if clip is not None:
            painter.save()
            painter.setClipPath(everywhere.subtracted(clip), Qt.ClipOperation.IntersectClip)
        try:
            drawn += _paint_item(painter, scene, stroke, background, layers, blend_modes, offsets)
        finally:
            if clip is not None:
                painter.restore()
    if blend_modes:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    if layers == "darken":
        return drawn
    return drawn + _paint_text(painter, scene, lines, include_hidden)


def _paint_item(painter, scene: PageScene, stroke, background: str, layers: str, blend_modes: bool,
                offsets: dict) -> int:
    """Draw one item of a layer (image, text highlight or stroke); 1 if drawn."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

    drawn = 0
    if isinstance(stroke, ImageItem):
        return 1 if layers != "darken" and _paint_image(painter, scene, stroke, blend_modes) else 0
    if isinstance(stroke, GlyphRange):
        return 1 if layers != "normal" and _paint_glyphs(painter, scene, stroke, blend_modes) else 0
    style = stroke_style(stroke, background)
    if style is None or (layers == "darken" and not style.blend_darken) \
            or (layers == "normal" and style.blend_darken):
        return 0
    if blend_modes:
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Darken if style.blend_darken
            else QPainter.CompositionMode.CompositionMode_SourceOver)
    cap = Qt.PenCapStyle.SquareCap if style.square_cap else Qt.PenCapStyle.RoundCap
    drawn += 1
    for run in stroke_runs(scene, stroke, offsets.get(id(stroke), (0.0, 0.0))):
        color = QColor(style.color)
        color.setAlphaF(run.opacity * style.alpha)
        brush = _texture_brush(scene, run, style) if run.texture else None
        painter.setPen(QPen(brush if brush is not None else color, run.width, Qt.PenStyle.SolidLine, cap,
                            Qt.PenJoinStyle.RoundJoin))
        if all(pt == run.points[0] for pt in run.points):
            # A zero-length path draws nothing; dots (i, full stops) need this.
            painter.drawPoint(QPointF(*run.points[0]))
            continue
        path = QPainterPath(QPointF(*run.points[0]))
        for px, py in run.points[1:]:
            path.lineTo(px, py)
        painter.drawPath(path)
    return drawn


def _texture_brush(scene: PageScene, run: Run, style: StrokeStyle):
    """A QBrush of the run's tile in the stroke's colour at the run's opacity, laid out
    from the scene's origin (x = 0 on the paper's centre line) and turned by the run's
    angle (viewer.brushes)."""
    from PySide6.QtGui import QBrush, QTransform

    from .brushes import texture_tile

    tile = texture_tile(run.texture, style.color, round(style.alpha * run.opacity, 3))
    if tile is None:
        return None
    brush = QBrush(tile)
    brush.setTransform(QTransform().translate(scene.x_origin, 0).rotateRadians(run.angle))
    return brush


def _paint_text(painter, scene: PageScene, lines: list, include_hidden: bool) -> int:
    """Draw the typed text above the layers; how many lines."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor

    drawn = 0
    if not (include_hidden or scene.root_document_visible):
        return drawn
    painter.setPen(QColor(TEXT_COLOR))
    for line in lines:
        drawn += 1
        if line.marker in CHECKBOX_MARKERS and len(line.marker_at) == 4:
            _paint_checkbox(painter, line.marker_at, line.marker == CHECKBOX_MARKERS[1],
                            line.marker == CHECKBOX_MARKERS[2])
        elif line.marker and line.marker_font and len(line.marker_at) == 2:
            painter.setFont(_qfont(line.marker_font))
            painter.drawText(QPointF(*line.marker_at), line.marker)
        x = line.x
        for text, font in line.pieces:
            painter.setFont(_qfont(font))
            painter.drawText(QPointF(x, line.baseline), text)
            x += qt_measure(text, font)
    return drawn


def _paint_checkbox(painter, box, checked: bool, indeterminate: bool = False) -> None:
    """The app's checkbox image (":/images/checkbox_checked.svg" / "unchecked" /
    "indeterminate", when the app's resources are registered) in its box, else
    _checkbox_shapes."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPen, QPolygonF
    from PySide6.QtCore import QFile
    path = f":/images/checkbox_{'checked' if checked else 'indeterminate' if indeterminate else 'unchecked'}.svg"
    svg = None
    if QFile.exists(path):
        try:
            from PySide6.QtSvg import QSvgRenderer
            svg = QSvgRenderer(path)
        except ImportError:
            pass
    if svg is not None and svg.isValid():
        svg.render(painter, QRectF(*box))
        return
    (sx, sy, sw, sh), tick, width = _checkbox_shapes(box, checked, indeterminate)
    painter.save()
    painter.setPen(QPen(QColor(TEXT_COLOR), width))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(QRectF(sx, sy, sw, sh))
    if tick:
        painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in tick]))
    painter.restore()


def _paint_glyphs(painter, scene: PageScene, glyph: GlyphRange, blend_modes: bool) -> bool:
    """A PDF/EPUB text highlight: its rectangles filled with its colour, no outline,
    composited with Darken, as reMarkable.exe paints one (0x140a1dc00: composition
    mode 16, pen NoPen, brush = its colour). The app also looks at the rendered page
    under each rectangle (light grey pixels are tinted, dark ones kept), which Darken
    over the page gives the same way. False when it has no rectangle."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainter

    rects = [r for r in glyph.rects if r[2] > 0 and r[3] > 0]
    if not rects:
        return False
    painter.save()
    if blend_modes:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Darken)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor.fromRgba(glyph.colour_argb()))
    for x, y, w, h in rects:
        painter.drawRect(QRectF(*scene.to_page(x, y), w, h))
    painter.restore()
    return True


def _svg_glyphs(scene: PageScene, glyph: GlyphRange) -> list[str]:
    argb = glyph.colour_argb()
    colour = f"#{argb & 0xFFFFFF:06x}"
    alpha = ((argb >> 24) & 255) / 255
    opacity = f' fill-opacity="{alpha:.4g}"' if alpha < 1 else ""
    return [f'<g data-tool="text-highlight" fill="{colour}"{opacity} style="mix-blend-mode:darken">'] + [
        f'<rect x="{x + scene.x_origin:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}"/>'
        for x, y, w, h in glyph.rects if w > 0 and h > 0] + ["</g>"]


def _paint_image(painter, scene: PageScene, image: ImageItem, blend_modes: bool) -> bool:
    """Draw an image item (its file decoded once and kept on the item)."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter, QTransform

    qimage = getattr(image, "_qimage", None)
    if qimage is None:
        qimage = QImage.fromData(image.data) if image.data else QImage()
        image._qimage = qimage
    if qimage.isNull():
        return False
    matrix = image_matrix(scene, image, qimage.width(), qimage.height())
    if matrix is None:
        return False
    painter.save()
    try:
        if blend_modes:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(QTransform(*matrix), True)
        painter.drawImage(QPointF(0, 0), qimage)
    finally:
        painter.restore()
    return True


def render_image(scene: PageScene, *, scale: float = 1.0, background: str = "#ffffff",
                 include_hidden: bool = False, crop: str = "page"):
    """Render to a QImage (ARGB32 premultiplied). Needs a QGuiApplication for text."""
    from PySide6.QtGui import QColor, QImage, QPainter

    x, y, w, h = render_bounds(scene, crop, measure=qt_measure)
    image = QImage(max(1, round(w * scale)), max(1, round(h * scale)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ffffff"))      # opaque base: over transparency Darken paints raw colour
    painter = QPainter(image)
    try:
        painter.fillRect(image.rect(), QColor(background))
        painter.scale(scale, scale)
        painter.translate(-x, -y)
        paint(painter, scene, background=background, include_hidden=include_hidden)
    finally:
        painter.end()
    return image


__all__ = ["PALETTE_ARGB", "Run", "StrokeStyle", "TextFont", "TextLine", "image_matrix", "image_size",
           "CHECKBOX_MARKERS", "LINE_SEPARATOR", "checkbox_boxes", "layout_text", "caret_x", "line_of", "index_at", "paint", "render_bounds", "render_image", "render_svg", "stroke_runs", "svg_scene",
           "stroke_style", "PAGE_WIDTH", "PAGE_HEIGHT"]
