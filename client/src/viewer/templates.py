"""Page templates (lined, grid, dots, planners …) from the app's `.template` files.

The 65 templates shipped in reMarkable.exe (extracted to assets/templates/) are JSON,
not images (ARCHITECTURE.md §5.3):

    {"formatVersion": 1, "orientation": "portrait",
     "constants": [{"name": number | "expression"}, ...],
     "items": [ group | path | text ]}

    group: {"boundingBox": {x, y, width, height}, "repeat": {rows, columns}, "children": [...]}
           repeat values: "down" | "up" | "right" | "left" | "infinite" | count expression
    path:  {"data": ["M", x, y, "L", x, y, "C", x1, y1, x2, y2, x, y, "Z"],
            "strokeColor"?, "fillColor"?, "strokeWidth"?, "antialiasing"?}
    text:  {"text", "fontSize", "position": {x, y}, "color"?, "bold"?, "italic"?, "rotate"?}
           (x, y is the baseline start; ARCHITECTURE.md §5.3 "Text items")

Numbers may be expressions in a small JavaScript-like language: + - * / %, comparisons,
&& ||, ?:, parentheses, and the variables the app's parser defines (templateWidth,
templateHeight, parentX/Y/Width/Height, physicalDpi, pixelRatio, paperOriginX/Y,
paperWidth/Height, textWidth) plus the template's constants. They are parsed here,
never passed to eval().

Coordinates are page pixels: (0, 0) is the top left of the page's paper (1404 × 1872
portrait unless the page's SceneInfo gives another paper size, ARCHITECTURE.md §5.6),
whose centre line is the scene's x = 0. Children are positioned relative to their
parent group instance. Repeats fill the area being drawn, so a lined template
continues down an extended page, as on the tablet. Defaults not stated in the files
(stroke black, 1 px, no fill) are assumptions;
paperOriginX is the left edge of the scene's "note bounds" (base_environment).
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .rm_parser import PAGE_HEIGHT, PAGE_WIDTH

log = logging.getLogger("rm.templates")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "assets" / "templates"
BLANK = "Blank"
MAX_INSTANCES = 20000        # guard against runaway repeats
FONT_FAMILIES = ("reMarkable Sans", "Noto Sans", "sans-serif")


class TemplateError(ValueError):
    """A template file or expression could not be understood."""


# --------------------------------------------------------------------- expressions

_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\.\d+|\d+)|([A-Za-z_][A-Za-z_0-9]*)|(\|\||&&|==|!=|<=|>=|[-+*/%()<>?:!]))")

_BINARY = {   # operator: (precedence, function)
    "||": (2, lambda a, b: a or b), "&&": (3, lambda a, b: a and b),
    "==": (4, lambda a, b: a == b), "!=": (4, lambda a, b: a != b),
    "<": (5, lambda a, b: a < b), ">": (5, lambda a, b: a > b),
    "<=": (5, lambda a, b: a <= b), ">=": (5, lambda a, b: a >= b),
    "+": (6, lambda a, b: a + b), "-": (6, lambda a, b: a - b),
    "*": (7, lambda a, b: a * b), "/": (7, lambda a, b: a / b), "%": (7, lambda a, b: math.fmod(a, b)),
}
_TERNARY_PRECEDENCE = 1


def _tokenize(text: str) -> list[tuple[str, Any]]:
    tokens, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise TemplateError(f"illegal expression {text!r} at {pos}")
        number, name, op = m.groups()
        if number is not None:
            tokens.append(("num", float(number)))
        elif name is not None:
            tokens.append(("name", name))
        else:
            tokens.append(("op", op))
        pos = m.end()
    return tokens


@lru_cache(maxsize=4096)
def compile_expression(text: str) -> Callable[[dict], Any]:
    """Compile an expression to a function of the variable environment."""
    tokens = _tokenize(text)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else ("end", None)

    def take():
        nonlocal pos
        token = peek()
        pos += 1
        return token

    def primary():
        kind, value = take()
        if kind == "num":
            return lambda env, v=value: v
        if kind == "name":
            def lookup(env, n=value):
                if n not in env:
                    raise TemplateError(f"unknown variable {n!r} in {text!r}")
                return env[n]
            return lookup
        if (kind, value) == ("op", "("):
            inner = expression(0)
            if take() != ("op", ")"):
                raise TemplateError(f"missing ')' in {text!r}")
            return inner
        if (kind, value) == ("op", "-"):
            operand = primary()
            return lambda env: -operand(env)
        if (kind, value) == ("op", "+"):
            return primary()
        if (kind, value) == ("op", "!"):
            operand = primary()
            return lambda env: not operand(env)
        raise TemplateError(f"illegal expression {text!r}")

    def expression(min_prec: int):
        left = primary()
        while True:
            kind, op = peek()
            if kind != "op":
                return left
            if op == "?" and _TERNARY_PRECEDENCE >= min_prec:
                take()
                then = expression(0)
                if take() != ("op", ":"):
                    raise TemplateError(f"missing ':' in ternary operator in {text!r}")
                other = expression(_TERNARY_PRECEDENCE)
                left = (lambda c, a, b: lambda env: a(env) if c(env) else b(env))(left, then, other)
                continue
            if op not in _BINARY or _BINARY[op][0] < min_prec:
                return left
            prec, fn = _BINARY[take()[1]]
            right = expression(prec + 1)
            left = (lambda f, a, b: lambda env: f(a(env), b(env)))(fn, left, right)

    if not tokens:
        raise TemplateError("empty expression")
    result = expression(0)
    if pos != len(tokens):
        raise TemplateError(f"unexpected {tokens[pos][1]!r} in {text!r}")
    return result


def evaluate(value: Any, env: dict) -> float:
    """A template number: a JSON number/bool, or an expression string."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        result = compile_expression(value)(env)
        return float(result)
    raise TemplateError(f"expected a number or expression, got {value!r}")


# ------------------------------------------------------------------------ templates

@dataclass(frozen=True)
class Template:
    name: str
    filename: str
    orientation: str
    constants: tuple[tuple[str, Any], ...]
    items: tuple[dict, ...]


@dataclass(frozen=True)
class ProceduralTemplate(Template):
    """A template 3.29 draws in C++ (SceneTemplateRenderer) rather than from a .template
    file: Hexagon small/medium/large and Calligraphy medium/large, portrait and landscape
    (template_shapes draws them, procedural_shapes)."""
    kind: int = 0                       # SceneTemplateRenderer::Type (0x140a0e5b0)
    params: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


# SceneTemplateRenderer::Type by template file name (0x140a0e5b0), and the doubles its
# setup (0x140a0bf80) stores at +0x20, +0x28, +0x30, +0x38 for each: the hexagon side's
# horizontal pitch s, the row pitch P, and the x and y origin offsets (hexagons); the
# column pitch, the row pitch and the x and y offsets (calligraphy).
PROCEDURAL_TEMPLATES = {
    "P Hexagon large": (1, (112.1, 194.4, -102.1, -147.4)),
    "P Hexagon medium": (2, (87.8, 151.8, -30.0, -138.0)),
    "P Hexagon small": (3, (43.9, 75.9, -8.0, -55.2)),
    "P Calligraphy large": (4, (72.0, 195.0, -50.5, 65.0)),
    "P Calligraphy medium": (5, (45.0, 157.5, -35.0, 99.0)),
    "LS Calligraphy large": (6, (72.0, 195.0, -105.0, 61.0)),
    "LS Calligraphy medium": (7, (45.0, 159.0, 0.0, 64.0)),
}
PROCEDURAL_PEN_WIDTH = 0.5              # QPen(Qt::black, 0.5, SolidLine, SquareCap, BevelJoin) (0x140a0c740)
CALLIGRAPHY_DASH = (13.0, 17.0)         # its guide lines: that pen, cosmetic, dashed (0x140a0cd40)


def procedural_template(name: str) -> ProceduralTemplate | None:
    entry = PROCEDURAL_TEMPLATES.get(name)
    if entry is None:
        return None
    kind, params = entry
    return ProceduralTemplate(name, name, "landscape" if name.startswith("LS ") else "portrait", (), (),
                              kind=kind, params=params)


def procedural_shapes(template: ProceduralTemplate, area: tuple[float, float, float, float],
                      width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT):
    """The lines 3.29 draws for a procedural template over `area` (page coordinates),
    as TemplatePaths in page coordinates. Its painters work in scene coordinates (x = 0
    on the paper's centre line) over the paper's note bounds (0x1408a8df0: (-h/2, 0, h,
    w)) and the exposed rectangle; rows and columns are counted with C++ truncation."""
    ax, ay, aw, ah = area
    half = width / 2                                    # page x of scene x = 0
    rl, rt, rr, rb = ax - half, ay, ax + aw - half, ay + ah   # the exposed rect, scene coordinates
    paper_left, paper_width = -height / 2, height       # note bounds
    paper_right = paper_left + paper_width
    black = ("#000000", 255)

    def line(x1, y1, x2, y2, dash=(), cosmetic=False) -> TemplatePath:
        return TemplatePath((("M", ((x1 + half, y1),)), ("L", ((x2 + half, y2),))), black, None,
                            PROCEDURAL_PEN_WIDTH, True, dash, cosmetic)

    a, pitch, ox, oy = template.params
    first = max(0, int((rt - oy) / pitch))
    last = int(int(rb - oy) / pitch + 1.0)
    columns = int(int(paper_width - ox) / a)
    if template.kind in (1, 2, 3):
        # Hexagons (0x140a0d340): per cell, six edges of P/3 (s = P / sqrt 3).
        for row in range(first, last + 1):
            y0 = row * pitch + oy
            for c in range(columns + 1):
                x = c * a + paper_left + ox
                if not (rl - a < x <= rr):
                    continue
                mx, xr = x + a / 2, x + a
                y1, y2, y3, y4 = y0 + pitch / 6, y0 + pitch / 2, y0 + 2 * pitch / 3, y0 + pitch
                yield line(x, y0, mx, y1)
                yield line(mx, y1, mx, y2)
                yield line(mx, y1, xr, y0)
                yield line(x, y3, mx, y2)
                yield line(mx, y2, xr, y3)
                yield line(xr, y3, xr, y4)
        return
    # Calligraphy (0x140a0cd40): per row three dashed guides and a solid baseline, then
    # slanted lines across the row (the medium ones a pixel back every second column).
    kind = template.kind
    medium = kind in (5, 7)
    if kind == 5:
        shift, slant, gap = 0.0, 12.0, 40.2
    elif kind == 7:
        shift, slant, gap = 12.0, 12.0, 40.6
    else:
        shift, slant, gap = 5.0, 0.0, 48.0
    start = (paper_left if medium else rl) + shift
    for row in range(first, last + 1):
        y0 = row * pitch + oy
        for y in (y0, y0 + gap, y0 + 3 * gap):
            yield line(start, y, paper_right, y, CALLIGRAPHY_DASH, True)
        yield line(rl, y0 + 2 * gap, paper_right, y0 + 2 * gap)
        back = 0.0
        for c in range(columns + 1):
            if medium:
                if c and c % 2 == 0:
                    back += 1.0
                x = c * a + paper_left + ox - back
            else:
                x = c * a + paper_left + ox
                if not (rl - 30.0 <= x <= rr):
                    continue
            yield line(x, y0 + 3 * gap, x + a + slant, y0)


def parse_template(data: str | bytes, filename: str = "") -> Template:
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as err:
        raise TemplateError(f"failed to parse template file {filename}: {err}") from err
    if raw.get("formatVersion") != 1:
        raise TemplateError(f"incompatible version in {filename}: {raw.get('formatVersion')!r}")
    constants = []
    for entry in raw.get("constants") or []:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise TemplateError(f"value in 'constants' array should be a single 'key': 'value' pair ({filename})")
        constants.append(next(iter(entry.items())))
    items = raw.get("items")
    if not isinstance(items, list):
        raise TemplateError(f"missing 'items' array ({filename})")
    return Template(raw.get("name", filename), filename, raw.get("orientation", "portrait"),
                    tuple(constants), tuple(items))


class TemplateLibrary:
    """Templates by the name stored in .content / .pagedata ("P Lines small")."""

    def __init__(self, directory: Path = TEMPLATE_DIR) -> None:
        self.directory = directory
        self._cache: dict[str, Template | None] = {}

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.template"))

    def get(self, name: str | None) -> Template | None:
        """The template file, or None for Blank / unknown names (drawn as blank paper).
        The seven templates 3.29 draws itself have no file: see resolve()."""
        if not name or name == BLANK:
            return None
        if name not in self._cache:
            path = self.directory / f"{name}.template"
            try:
                self._cache[name] = parse_template(path.read_text(encoding="utf-8"), path.name)
            except FileNotFoundError:
                log.warning("no template file for %r; drawing blank paper", name)
                self._cache[name] = None
        return self._cache[name]


    def resolve(self, name: str | None) -> Template | None:
        """What a page with template `name` is drawn with: one of the procedural
        templates (Hexagon, Calligraphy: procedural_template), else get(name)."""
        return procedural_template(name or "") or self.get(name)


_default_library: TemplateLibrary | None = None


def default_library() -> TemplateLibrary:
    global _default_library
    if _default_library is None:
        _default_library = TemplateLibrary()
    return _default_library


# ------------------------------------------------------------------------ painting

PHYSICAL_DPI = 226.0       # the template context's dpi (0x140a0de10, the double at 0x140d6f670)


def base_environment(width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> dict:
    """The variables of a page of paper `width` x `height`, as 3.29 binds them
    (SceneTemplateRenderer 0x140a0de10 makes the context: the scene's paper size, and
    Scene's paperNoteBounds (0x1408a8df0), QRectF(-h/2, 0, h, w); the resolver
    0x140a8ebd0 reads it): templateWidth/Height the paper size, paperOriginX
    templateWidth / 2 + bounds.x (= w/2 - h/2, the left edge of those bounds in template
    coordinates), paperOriginY bounds.y, paperWidth/Height the bounds' width and height
    (h and w), physicalDpi 226, pixelRatio physicalDpi / 227. parent* are the area of the
    group being laid out, here the page at the top level."""
    return {"templateWidth": width, "templateHeight": height, "physicalDpi": PHYSICAL_DPI,
            "pixelRatio": PHYSICAL_DPI / 227.0,
            "paperOriginX": width / 2 - height / 2, "paperOriginY": 0.0, "paperWidth": height, "paperHeight": width,
            "parentX": 0.0, "parentY": 0.0, "parentWidth": width, "parentHeight": height}


def is_landscape(template: Template) -> bool:
    """The template's "orientation" read as 3.29 reads it (0x1406cf330): "landscape" is
    Qt::Horizontal (1), anything else Qt::Vertical."""
    return template.orientation == "landscape"


def template_size(template: Template, width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> tuple[float, float]:
    """templateWidth x templateHeight: the paper, transposed for a landscape template
    (SceneTemplateRenderer 0x140a0de10: QSizeF::transpose when its orientation is 1)."""
    return (float(height), float(width)) if is_landscape(template) else (float(width), float(height))


def template_left(template: Template, width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> float:
    """Page x of the template's x = 0. The renderer's paint (0x140a0c740, JSON templates)
    translates by -templateWidth / 2 from the scene's x = 0, the paper's centre line at
    page x width / 2: 0 on a portrait template, width/2 - height/2 (-234 on 1404 x 1872)
    on a landscape one, which then covers the paper's note bounds (-h/2, 0, h, w)."""
    return width / 2 - template_size(template, width, height)[0] / 2


def template_environment(template: Template, width: float = PAGE_WIDTH,
                         height: float = PAGE_HEIGHT) -> dict:
    """The variables for `template` on a page of paper `width` x `height`: those of
    base_environment, with templateWidth/Height transposed for a landscape template.
    paperOriginX is templateWidth / 2 + bounds.x with the note bounds of the paper
    (-h/2, 0, h, w), so 0 for a landscape template; paperWidth/Height stay h and w."""
    env = base_environment(width, height)
    if is_landscape(template):
        tw, th = template_size(template, width, height)
        env.update(templateWidth=tw, templateHeight=th, paperOriginX=tw / 2 - height / 2,
                   parentWidth=tw, parentHeight=th)
    for name, value in template.constants:
        env[name] = evaluate(value, env)
    return env


def _repeat_offsets(mode: Any, start: float, step: float, lo: float, hi: float, env: dict,
                    forward: str, backward: str) -> list[int]:
    """Instance indices along one axis for a repeat mode, covering [lo, hi)."""
    if mode is None:
        return [0]
    if isinstance(mode, str) and mode in (forward, backward, "infinite"):
        if step <= 0:
            return [0]
        first = 0 if mode == forward else math.floor((lo - start) / step)
        last = 0 if mode == backward else math.ceil((hi - start) / step)
        if mode == forward:
            last = max(0, last)
        if mode == backward:
            first = min(0, first)
        return list(range(first, last + 1))
    count = int(evaluate(mode, env))
    return list(range(max(0, count)))


@dataclass(frozen=True)
class TemplatePath:
    """One path instance of a template, in page coordinates."""
    commands: tuple[tuple[str, tuple[tuple[float, float], ...]], ...]   # ("M"|"L"|"C"|"Z", points)
    stroke: tuple[str, int] | None      # ("#rrggbb", alpha 0..255); None: no stroke
    fill: tuple[str, int] | None
    stroke_width: float
    antialiasing: bool
    dash: tuple[float, ...] = ()        # QPen dash pattern (in pen widths); () solid
    cosmetic: bool = False              # QPen::setCosmetic: the width is in device pixels


TEXT_FONT = "reMarkable Sans"      # the family the drawer sets (0x140a91750)
DEFAULT_TEXT_ARGB = 0xFF000000


@dataclass(frozen=True)
class TemplateText:
    """One text item instance, as 3.29's drawer (0x140a91750) draws it: (x, y) is the
    baseline start, in page coordinates; rotate (degrees) turns it about (x, y)."""
    text: str
    size: int            # pixel size, as drawn
    x: float
    y: float
    bold: bool = False
    italic: bool = False
    argb: int = DEFAULT_TEXT_ARGB
    rotate: float = 0.0


# text_metrics(text, pixel_size, bold, italic) -> textWidth: the whole-pixel width of
# QFontMetrics(font).boundingRect(text)
TextMetrics = Callable[[str, int, bool, bool], int]


def _qt_font(size: int, bold: bool = False, italic: bool = False):
    from PySide6.QtGui import QFont
    font = QFont(TEXT_FONT)
    font.setPixelSize(max(1, size))
    font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)       # 700 / 400
    font.setItalic(italic)
    return font


def estimate_text_width(text: str, size: int, bold: bool = False, italic: bool = False) -> int:
    """A headless textWidth [I]: no font is available, so each character is taken as
    0.55 em (0.6 bold), in whole pixels like boundingRect().width()."""
    return round(len(text) * size * (0.6 if bold else 0.55))


def qt_text_metrics(text: str, size: int, bold: bool = False, italic: bool = False) -> int:
    """textWidth as 3.29 measures it: QFontMetrics(font).boundingRect(text).width() in
    reMarkable Sans (the app's fonts registered, renderer.ensure_fonts); the estimate
    when Qt or a QGuiApplication is missing."""
    try:
        from PySide6.QtGui import QFontMetrics, QGuiApplication
    except ImportError:
        return estimate_text_width(text, size, bold, italic)
    if QGuiApplication.instance() is None:
        return estimate_text_width(text, size, bold, italic)
    from .renderer import ensure_fonts
    ensure_fonts()
    return QFontMetrics(_qt_font(size, bold, italic)).boundingRect(text).width()


def _text_color(item: dict) -> int:
    """The item's "color" as an ARGB int (parser 0x140a8b580): absent opaque black, null
    transparent (0), "#RRGGBB" opaque, "#AARRGGBB" as written; anything else an error."""
    if "color" not in item:
        return DEFAULT_TEXT_ARGB
    value = item["color"]
    if value is None:
        return 0
    if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value):
        number = int(value[1:], 16)
        return number if len(value) == 9 else 0xFF000000 | number
    raise TemplateError(f"invalid color, expected '#RRGGBB' or '#AARRGGBB': {value!r}")


def _is_visible(item: dict, env: dict) -> bool:
    """An item's "visible" (items loop 0x140a89fb0, any item type): absent or neither a
    bool nor a string → visible; a bool as given; a string is an expression, visible
    iff |value| > 1e-5. Invisible items are skipped without error."""
    value = item.get("visible")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return abs(evaluate(value, env)) > 1e-5
    return True


def _text_bool(item: dict, key: str) -> bool:
    value = item.get(key, False)
    if not isinstance(value, bool):
        raise TemplateError(f"{key!r}: boolean value expected")        # 0x140a8acb0
    return value


def _color(value: str | None, default: str | None) -> tuple[str, int] | None:
    value = value or default
    if value is None:
        return None
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value):
        raise TemplateError(f"invalid color, expected '#RRGGBB'-style string: {value!r}")
    # #RRGGBBAA as in the shipped "#00000000"
    return value[:7].lower(), int(value[7:], 16) if len(value) == 9 else 255


def template_shapes(template: Template, area: tuple[float, float, float, float],
                    width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT,
                    text_metrics: TextMetrics = qt_text_metrics):
    """The template's paths and texts, in drawing order and page coordinates, covering
    `area` (x, y, w, h): repeats extend to fill it. paint_template and template_svg
    both draw these, so the two cannot differ."""
    if isinstance(template, ProceduralTemplate):
        yield from procedural_shapes(template, area, width, height)
        return
    ax, ay, aw, ah = area
    budget = [MAX_INSTANCES]
    left = template_left(template, width, height)       # page x of the template's x = 0

    def path(item: dict, env: dict, ox: float, oy: float) -> TemplatePath:
        data = item.get("data") or []
        if not data:
            raise TemplateError("path 'data' is empty or missing")
        commands, i = [], 0
        args = {"M": 2, "L": 2, "C": 6, "Z": 0}
        while i < len(data):
            cmd = data[i]
            if cmd not in args:
                raise TemplateError(f"invalid command in path data: {cmd!r}")
            values = [evaluate(v, env) for v in data[i + 1:i + 1 + args[cmd]]]
            if len(values) != args[cmd]:
                raise TemplateError(f"path command {cmd!r} needs {args[cmd]} values")
            commands.append((cmd, tuple((ox + values[j], oy + values[j + 1]) for j in range(0, len(values), 2))))
            i += 1 + args[cmd]
        stroke = _color(item.get("strokeColor"), "#000000")
        fill = _color(item.get("fillColor"), None)
        if (stroke is None or stroke[1] == 0) and (fill is None or fill[1] == 0):
            raise TemplateError("illegal combination of 'strokeColor' and 'fillColor', path is fully invisible")
        return TemplatePath(tuple(commands), stroke if stroke is not None and stroke[1] else None,
                            fill if fill is not None and fill[1] else None,
                            float(item.get("strokeWidth", 1)), bool(item.get("antialiasing", True)))

    def text(item: dict, env: dict, ox: float, oy: float) -> TemplateText:
        """A text item as 3.29's parser reads it (0x140a8e190); an item with an error
        raises TemplateError, as a path does."""
        value = item.get("text")
        if not isinstance(value, str) or not value:
            raise TemplateError("'text' property cannot be empty")
        size = item.get("fontSize", 0)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise TemplateError("'fontSize' must be a positive value")
        argb = _text_color(item)
        position = item.get("position")
        if not isinstance(position, dict):
            raise TemplateError("'position' is empty or missing")
        bold, italic = _text_bool(item, "bold"), _text_bool(item, "italic")
        rotate = item.get("rotate", 0)
        if isinstance(rotate, bool) or not isinstance(rotate, (int, float)):
            raise TemplateError("'rotate' must be a number")
        if abs(rotate) > 360:
            raise TemplateError("'rotate' must be within +/- 0-360 degrees.")
        # textWidth is bound before position.x / position.y are evaluated
        local = dict(env, textWidth=int(text_metrics(value, size, bold, italic)))
        for axis in ("x", "y"):             # both must evaluate (0x140a8d130)
            if axis not in position:
                raise TemplateError(f"'position' needs '{axis}'")
        x, y = evaluate(position["x"], local), evaluate(position["y"], local)
        return TemplateText(value, size, ox + x, oy + y, bold, italic, argb, float(rotate))

    def items(entries, env: dict, ox: float, oy: float):
        for item in entries:
            kind = item.get("type")
            if not _is_visible(item, env):
                continue
            if kind == "group":
                box = item.get("boundingBox")
                if not box:
                    raise TemplateError("missing or invalid bounds")
                children = item.get("children")
                if not children:
                    raise TemplateError("missing or empty 'children' list")
                bx, by = ox + evaluate(box.get("x", 0), env), oy + evaluate(box.get("y", 0), env)
                bw, bh = evaluate(box.get("width", 0), env), evaluate(box.get("height", 0), env)
                repeat = item.get("repeat") or {}
                rows = _repeat_offsets(repeat.get("rows"), by, bh, ay, ay + ah, env, "down", "up")
                cols = _repeat_offsets(repeat.get("columns"), bx, bw, ax, ax + aw, env, "right", "left")
                for r in rows:
                    for c in cols:
                        budget[0] -= 1
                        if budget[0] < 0:
                            raise TemplateError(f"template {template.name!r} repeats too many times")
                        ix, iy = bx + c * bw, by + r * bh
                        # parent* in template coordinates, as the app's painter is translated
                        child_env = dict(env, parentX=ix - left, parentY=iy, parentWidth=bw, parentHeight=bh)
                        yield from items(children, child_env, ix, iy)
            elif kind == "path":
                yield path(item, env, ox, oy)
            elif kind == "text":
                yield text(item, env, ox, oy)
            else:
                raise TemplateError(f"unknown item type {kind!r}")

    yield from items(template.items, template_environment(template, width, height), left, 0.0)


def paint_template(painter, template: Template, area: tuple[float, float, float, float],
                   width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> None:
    """Draw `template` with an active QPainter in page coordinates, covering `area`
    (x, y, w, h); repeats extend to fill it."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen

    def qcolor(value: tuple[str, int]) -> QColor:
        color = QColor(value[0])
        color.setAlpha(value[1])
        return color

    painter.save()
    try:
        for shape in template_shapes(template, area, width, height):
            if isinstance(shape, TemplateText):
                # 0x140a91750: pen = the colour, font reMarkable Sans; rotate 0 draws at
                # the baseline start (x, y), else translate, rotate, draw at (0, 0).
                from .renderer import ensure_fonts
                ensure_fonts()
                painter.save()
                painter.setFont(_qt_font(shape.size, shape.bold, shape.italic))
                painter.setPen(QColor.fromRgba(shape.argb))
                if shape.rotate == 0:
                    painter.drawText(QPointF(shape.x, shape.y), shape.text)
                else:
                    painter.translate(shape.x, shape.y)
                    painter.rotate(shape.rotate)
                    painter.drawText(QPointF(0, 0), shape.text)
                painter.restore()
                continue
            path = QPainterPath()
            for cmd, pts in shape.commands:
                points = [QPointF(*p) for p in pts]
                if cmd == "M":
                    path.moveTo(points[0])
                elif cmd == "L":
                    path.lineTo(points[0])
                elif cmd == "C":
                    path.cubicTo(*points)
                else:
                    path.closeSubpath()
            # "antialiasing": false keeps 1-px features (dots) crisp at native size; when
            # downscaled it would make them alias into moiré bands, so it is honoured
            # only at >= 1 device pixel per page pixel.
            native = abs(painter.worldTransform().determinant()) >= 1.0
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, shape.antialiasing or not native)
            if shape.stroke:
                pen = QPen(qcolor(shape.stroke), shape.stroke_width)
                if shape.dash:
                    pen.setDashPattern(list(shape.dash))
                pen.setCosmetic(shape.cosmetic)
                painter.setPen(pen)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(qcolor(shape.fill)) if shape.fill else Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
    finally:
        painter.restore()


def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _svg_paint(attr: str, value: tuple[str, int] | None) -> str:
    if value is None:
        return f' {attr}="none"'
    opacity = f' {attr}-opacity="{value[1] / 255:.4g}"' if value[1] < 255 else ""
    return f' {attr}="{value[0]}"{opacity}'


def _outside(shape: TemplatePath, area: tuple[float, float, float, float]) -> bool:
    """Whether a path cannot touch `area`: a path lies within the box of its points
    (Bézier curves within their control points), grown by the pen."""
    xs = [x for _, pts in shape.commands for x, _ in pts]
    ys = [y for _, pts in shape.commands for _, y in pts]
    if not xs:
        return True
    pad = shape.stroke_width / 2 + 1 if shape.stroke else 0.0
    ax, ay, aw, ah = area
    return max(xs) + pad < ax or min(xs) - pad > ax + aw or max(ys) + pad < ay or min(ys) - pad > ay + ah


def template_svg(template: Template, area: tuple[float, float, float, float],
                 width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT,
                 text_metrics: TextMetrics = qt_text_metrics) -> list[str]:
    """The template as SVG elements in page coordinates (one group), drawn as
    paint_template draws it: QPen's square caps and bevel joins, QPainterPath's even-odd
    fill, and a zero stroke width as Qt's one-pixel cosmetic pen. Everything is
    antialiased (an SVG has no native pixel size).

    Kept small: consecutive paths of one style share a group that carries the style;
    outlines in an opaque colour share one <path> (translucent ones would darken where
    they cross, as separate paths do); paths wholly outside `area` (repeats run past the
    page) are left out."""
    from xml.sax.saxutils import escape, quoteattr

    out = [f'<g id="template" data-name={quoteattr(template.name)}>']
    pending: tuple[tuple, list[str]] | None = None

    def attributes(style: tuple) -> str:
        stroke, fill, stroke_width, dash, is_cosmetic = style
        if stroke is None:
            text = ' stroke="none"'
        else:
            cosmetic = ' vector-effect="non-scaling-stroke"' if stroke_width == 0 or is_cosmetic else ""
            # A QPen dash pattern counts in pen widths.
            dashes = (' stroke-dasharray="' + " ".join(_num(d * (stroke_width or 1)) for d in dash) + '"'
                      if dash else "")
            text = (_svg_paint("stroke", stroke) + f' stroke-width="{_num(stroke_width or 1)}"'
                    f' stroke-linecap="square" stroke-linejoin="bevel"{cosmetic}{dashes}')
        return text + _svg_paint("fill", fill) + (' fill-rule="evenodd"' if fill is not None else "")

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        style, data = pending
        stroke, fill = style[0], style[1]
        if len(data) == 1 or (fill is None and stroke is not None and stroke[1] == 255):
            out.append(f'<path d="{" ".join(data)}"{attributes(style)}/>')
        else:
            out.append(f'<g{attributes(style)}>')
            out.extend(f'<path d="{d}"/>' for d in data)
            out.append("</g>")
        pending = None

    for shape in template_shapes(template, area, width, height, text_metrics):
        if isinstance(shape, TemplateText):
            flush()
            families = ", ".join(f"'{f}'" if " " in f else f for f in FONT_FAMILIES)
            alpha = (shape.argb >> 24) & 255
            opacity = f' fill-opacity="{alpha / 255:.4g}"' if alpha < 255 else ""
            if shape.rotate == 0:
                place = f'x="{_num(shape.x)}" y="{_num(shape.y)}"'
            else:
                place = f'x="0" y="0" transform="translate({_num(shape.x)} {_num(shape.y)}) rotate({_num(shape.rotate)})"'
            out.append(f'<text {place} font-family="{families}" font-size="{shape.size}" '
                       f'font-weight="{700 if shape.bold else 400}" '
                       f'font-style="{"italic" if shape.italic else "normal"}" '
                       f'fill="#{shape.argb & 0xFFFFFF:06x}"{opacity}>{escape(shape.text)}</text>')
            continue
        if _outside(shape, area):
            continue
        data = " ".join(cmd + "".join(f" {_num(x)} {_num(y)}" for x, y in pts) for cmd, pts in shape.commands)
        style = (shape.stroke, shape.fill, shape.stroke_width, shape.dash, shape.cosmetic)
        if pending is not None and pending[0] == style:
            pending[1].append(data)
        else:
            flush()
            pending = (style, [data])
    flush()
    out.append("</g>")
    return out


def parse_pagedata(data: bytes) -> list[str]:
    """Legacy per-page template names (formatVersion 1 content): one name per line."""
    return [line.strip() for line in data.decode("utf-8", errors="replace").splitlines()]
