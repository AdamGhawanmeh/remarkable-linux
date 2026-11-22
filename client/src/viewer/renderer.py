"""Render a parsed PageScene to SVG (pure Python) or with QPainter (PySide6).

Both back ends share `stroke_runs()` (a stroke as runs of constant width/opacity)
and `layout_text()` (typed text as positioned lines), so they cannot drift apart.

Fidelity (ARCHITECTURE.md §5.2):
  * Stroke colours are the official ARGB palette, decoded from reMarkable.exe.
  * Highlighters are opaque palette colours composited with Darken: the binary
    has no Multiply mode, sets QPainter::CompositionMode_Darken in its paint
    paths, and its highlighter colours are fully opaque. Darken over white gives
    the exact colour and leaves darker ink underneath untouched.
  * Approximations: pencil/brush textures (the app rasterises those itself),
    shader opacity, and typed-text font sizes (held in C++ enums, not recoverable
    statically). The app's own fonts are reMarkable Sans / Serif Small; those are
    used if installed, otherwise Noto Sans / sans-serif.

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
FONT_FAMILIES = ("reMarkable Sans", "Noto Sans", "sans-serif")


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

@dataclass(frozen=True)
class TextFont:
    size: float          # pixels on the 1404 × 1872 page
    bold: bool = False
    italic: bool = False


# Approximate sizes: the real values live in C++ (crdt::TextProperties::FontSize).
# Body line height: fitted, not decoded. Anchored groups in real files carry
# threshold 35.75 (half of 71.5), matching the ~71 px measured by other tools.
LINE_PX = 71.5
_BODY = 34.0
STYLE_FONTS: dict[ParagraphStyle, tuple[float, bool]] = {
    ParagraphStyle.TITLE: (2.0, True),
    ParagraphStyle.SUBHEADING: (1.4, True),
    ParagraphStyle.SUBHEADING_2: (1.15, True),
}
BASELINE_AT = 0.75     # baseline position within a line box (fraction of its height)
# The line break Shift+Enter types (SceneController::keyAction 0x140443380, action
# LineBreak: the keystroke path with QString(QChar(0x2028))): a character of the
# paragraph's text, stored like any other, that ends the line but not the paragraph.
LINE_SEPARATOR = "\u2028"
INDENT = 48.0


def _prefix(style: ParagraphStyle, number: int) -> tuple[str, float]:
    """Marker drawn before a list paragraph, and the paragraph's indent."""
    indented = style in (ParagraphStyle.BULLETPOINT_INDENTED,
                         ParagraphStyle.CHECKBOX_UNCHECKED_INDENTED,
                         ParagraphStyle.CHECKBOX_CHECKED_INDENTED,
                         ParagraphStyle.NUMBERED_LIST_INDENTED)
    indent = INDENT * (2 if indented else 1)
    if style in (ParagraphStyle.BULLETPOINT, ParagraphStyle.BULLETPOINT_INDENTED):
        return "•", indent
    if style in (ParagraphStyle.CHECKBOX_UNCHECKED, ParagraphStyle.CHECKBOX_UNCHECKED_INDENTED):
        return "☐", indent
    if style in (ParagraphStyle.CHECKBOX_CHECKED, ParagraphStyle.CHECKBOX_CHECKED_INDENTED):
        return "☑", indent
    if style in (ParagraphStyle.NUMBERED_LIST, ParagraphStyle.NUMBERED_LIST_INDENTED):
        return f"{number}.", indent
    return "", 0.0


@dataclass(frozen=True)
class TextLine:
    x: float                                  # page coordinates of the baseline start
    baseline: float
    pieces: tuple[tuple[str, TextFont], ...]  # consecutive runs of text on the line
    marker: str = ""                          # list marker drawn left of x
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


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


# measure(text, font) -> advance width in page pixels
Measure = Callable[[str, TextFont], float]


def estimate_width(text: str, font: TextFont) -> float:
    """Font-free width estimate for headless SVG layout."""
    return len(text) * font.size * (0.58 if font.bold else 0.53)


def layout_text(scene: PageScene, measure: Measure = estimate_width) -> list[TextLine]:
    """Greedy word-wrap of the scene's typed text into positioned lines."""
    block: TextBlock | None = scene.text
    if block is None:
        return []
    left, _ = scene.to_page(block.x, block.y)
    y = block.y
    lines: list[TextLine] = []
    number = 0
    base = 0                                    # text index of the paragraph's first character
    for index, para in enumerate(block.paragraphs):
        scale, heading_bold = STYLE_FONTS.get(para.style, (1.0, False))
        size = _BODY * scale
        line_h = LINE_PX * scale
        number = number + 1 if para.style in (ParagraphStyle.NUMBERED_LIST,
                                              ParagraphStyle.NUMBERED_LIST_INDENTED) else 0
        marker, indent = _prefix(para.style, number)
        x0, max_w = left + indent, max(block.width - indent, size)
        words: list[tuple[str, TextFont, int]] = []
        at = base
        for span in para.spans:
            font = TextFont(size, span.bold or heading_bold, span.italic)
            for i, part in enumerate(span.text.split(" ")):
                if i:
                    words.append((" ", font, at))
                    at += 1
                for j, piece in enumerate(part.split(LINE_SEPARATOR)):
                    if j:
                        words.append((LINE_SEPARATOR, font, at))
                        at += 1
                    if piece:
                        words.append((piece, font, at))
                        at += _units(piece)
        end = base + _units(para.text)
        current: list[tuple[str, TextFont, int]] = []
        width = 0.0
        first = True
        start = base

        def flush(next_start: int):
            nonlocal current, width, first, y, start
            y += line_h
            pieces = _merge([(t, f) for t, f, _ in current])
            while pieces and not pieces[-1][0].strip():
                pieces.pop()
            if pieces:
                pieces[-1] = (pieces[-1][0].rstrip(" "), pieces[-1][1])
            lines.append(TextLine(x0, y - line_h * (1 - BASELINE_AT), tuple(pieces),
                                  marker if first else "",
                                  TextFont(size, heading_bold) if first and marker else None,
                                  index, start, next_start, line_h,
                                  tuple((i, t, f) for t, f, i in current)))
            current, width, first, start = [], 0.0, False, next_start

        for text, font, i in words:
            if text == LINE_SEPARATOR:
                # A line break typed with Shift+Enter: the line ends here, the
                # paragraph goes on (QTextDocument's QChar::LineSeparator).
                flush(i + 1)
                continue
            w = measure(text, font)
            if text != " " and current and width + w > max_w:
                flush(i)
            if text == " " and not current:
                if start == i:
                    start = i + 1                   # a space a line does not start with
                continue
            current.append((text, font, i))
            width += w
        flush(end)
        base = end + 1
    return lines


CHECKBOX_MARKERS = ("☐", "☑")


def checkbox_boxes(scene: PageScene, lines: list[TextLine],
                   measure: Measure = estimate_width) -> list[tuple[int, tuple[float, float, float, float]]]:
    """The checkboxes drawn on a page: (text index where the checkbox's paragraph
    starts, its box (x, y, width, height) in page coordinates). The index is what the
    app's checkbox links carry ("rmcheckbox://index=<n>": the scene's checkbox table
    is keyed by character position and shifted as the text changes, 0x140989280); the
    box is the marker drawn left of the paragraph, with the gap to the text."""
    block = scene.text
    if block is None:
        return []
    starts, at = [], 0
    for para in block.paragraphs:
        starts.append(at)
        at += _units(para.text) + 1
    out = []
    for line in lines:
        if line.marker not in CHECKBOX_MARKERS or line.marker_font is None or line.paragraph >= len(starts):
            continue
        width = measure(line.marker, line.marker_font)
        size = line.marker_font.size
        out.append((starts[line.paragraph], (line.x - 8 - width, line.baseline - size, width + 8, size * 1.25)))
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
    def top(line):
        return line.baseline - line.height * BASELINE_AT
    n = min(range(len(lines)), key=lambda k: 0 if top(lines[k]) <= y < top(lines[k]) + lines[k].height
            else min(abs(y - top(lines[k])), abs(y - top(lines[k]) - lines[k].height)))
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


def stroke_offsets(scene: PageScene, lines: list[TextLine]) -> dict[int, tuple[float, float]]:
    """Offset per anchored stroke (keyed by id(stroke)): (anchor origin x, line baseline).

    The line is the first line of the paragraph holding the anchor character.
    Sentinel anchors and anchors without a matching character sit at the top of
    the text box (their exact meaning is not decoded).
    """
    text = scene.text
    baselines: dict[int, float] = {}
    for line in lines:
        baselines.setdefault(line.paragraph, line.baseline)
    offsets: dict[int, tuple[float, float]] = {}
    for stroke in scene.strokes:
        a = stroke.anchor
        if a is None:
            continue
        dy = 0.0
        if text is not None:
            para = text.char_paragraph.get(a.char_id)
            dy = baselines.get(para, text.y) if para is not None else text.y
        offsets[id(stroke)] = (a.origin_x, dy)
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
            size = max(f.size for _, f in line.pieces)
            xs += [line.x, line.x + width]
            ys += [line.baseline - size, line.baseline + size * 0.3]
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
    content = _ink_bounds(scene, lines, stroke_offsets(scene, lines), measure)
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

def _svg_font(font: TextFont) -> str:
    families = ", ".join(f"'{f}'" if " " in f else f for f in FONT_FAMILIES)
    weight = ' font-weight="bold"' if font.bold else ""
    style = ' font-style="italic"' if font.italic else ""
    return f'font-family="{families}" font-size="{font.size:g}"{weight}{style}'


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
    offsets = stroke_offsets(scene, lines)
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
            if line.marker and line.marker_font:
                out.append(f'<text x="{line.x - 8:.2f}" y="{line.baseline:.2f}" text-anchor="end" '
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
    from PySide6.QtGui import QFont
    qf = QFont()
    qf.setFamilies(list(FONT_FAMILIES))
    qf.setPixelSize(max(1, round(font.size)))
    qf.setBold(font.bold)
    qf.setItalic(font.italic)
    return qf


def qt_measure(text: str, font: TextFont) -> float:
    from PySide6.QtGui import QFontMetricsF
    return QFontMetricsF(_qfont(font)).horizontalAdvance(text)


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
    offsets = stroke_offsets(scene, lines)
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
        if line.marker and line.marker_font:
            mf = _qfont(line.marker_font)
            painter.setFont(mf)
            painter.drawText(QPointF(line.x - 8 - qt_measure(line.marker, line.marker_font),
                                     line.baseline), line.marker)
        x = line.x
        for text, font in line.pieces:
            painter.setFont(_qfont(font))
            painter.drawText(QPointF(x, line.baseline), text)
            x += qt_measure(text, font)
    return drawn


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
