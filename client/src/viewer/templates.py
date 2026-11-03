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
    text:  {"text", "fontSize", "position": {x, y}}

Numbers may be expressions in a small JavaScript-like language: + - * / %, comparisons,
&& ||, ?:, parentheses, and the variables the app's parser defines (templateWidth,
templateHeight, parentX/Y/Width/Height, physicalDpi, pixelRatio, paperOriginX/Y,
paperWidth/Height, textWidth) plus the template's constants. They are parsed here,
never passed to eval().

Coordinates are page pixels (1404 × 1872 portrait). Children are positioned relative
to their parent group instance. Repeats fill the area being drawn, so a lined template
continues down an extended page, as on the tablet. Defaults not stated in the files
(stroke black, 1 px, no fill; text black, top-left anchored) are assumptions; the
paper origin is the scene origin (page x 702, y 0).
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
        """The template, or None for Blank / unknown names (drawn as blank paper)."""
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


_default_library: TemplateLibrary | None = None


def default_library() -> TemplateLibrary:
    global _default_library
    if _default_library is None:
        _default_library = TemplateLibrary()
    return _default_library


# ------------------------------------------------------------------------ painting

def base_environment(width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> dict:
    return {"templateWidth": width, "templateHeight": height, "physicalDpi": 226.0, "pixelRatio": 1.0,
            "paperOriginX": width / 2, "paperOriginY": 0.0, "paperWidth": width, "paperHeight": height,
            "parentX": 0.0, "parentY": 0.0, "parentWidth": width, "parentHeight": height}


def template_environment(template: Template, width: float = PAGE_WIDTH,
                         height: float = PAGE_HEIGHT) -> dict:
    env = base_environment(width, height)
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


@dataclass(frozen=True)
class TemplateText:
    text: str
    size: int            # pixel size, as drawn
    x: float             # baseline start, page coordinates
    baseline: float


# text_metrics(text, pixel_size) -> (advance width, ascent), in page pixels
TextMetrics = Callable[[str, int], tuple[float, float]]


def _qt_font(size: int):
    from PySide6.QtGui import QFont
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPixelSize(max(1, size))
    return font


def qt_text_metrics(text: str, size: int) -> tuple[float, float]:
    from PySide6.QtGui import QFontMetricsF
    metrics = QFontMetricsF(_qt_font(size))
    return metrics.horizontalAdvance(text), metrics.ascent()


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
    ax, ay, aw, ah = area
    budget = [MAX_INSTANCES]

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
        value = item.get("text")
        if not value:
            raise TemplateError("'text' property cannot be empty")
        size = evaluate(item.get("fontSize", 0), env)
        if size <= 0:
            raise TemplateError("'fontSize' must be a positive value")
        position = item.get("position")
        if not position:
            raise TemplateError("'position' is empty or missing")
        pixels = max(1, round(size))
        advance, ascent = text_metrics(value, pixels)
        local = dict(env, textWidth=advance)
        x, y = evaluate(position.get("x", 0), local), evaluate(position.get("y", 0), local)
        return TemplateText(value, pixels, ox + x, oy + y + ascent)

    def items(entries, env: dict, ox: float, oy: float):
        for item in entries:
            kind = item.get("type")
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
                        child_env = dict(env, parentX=ix, parentY=iy, parentWidth=bw, parentHeight=bh)
                        yield from items(children, child_env, ix, iy)
            elif kind == "path":
                yield path(item, env, ox, oy)
            elif kind == "text":
                yield text(item, env, ox, oy)
            else:
                raise TemplateError(f"unknown item type {kind!r}")

    yield from items(template.items, template_environment(template, width, height), 0.0, 0.0)


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
                painter.setFont(_qt_font(shape.size))
                painter.setPen(QColor("#000000"))
                painter.drawText(QPointF(shape.x, shape.baseline), shape.text)
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
            painter.setPen(QPen(qcolor(shape.stroke), shape.stroke_width) if shape.stroke else Qt.PenStyle.NoPen)
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
        stroke, fill, stroke_width = style
        if stroke is None:
            text = ' stroke="none"'
        else:
            cosmetic = ' vector-effect="non-scaling-stroke"' if stroke_width == 0 else ""
            text = (_svg_paint("stroke", stroke) + f' stroke-width="{_num(stroke_width or 1)}"'
                    f' stroke-linecap="square" stroke-linejoin="bevel"{cosmetic}')
        return text + _svg_paint("fill", fill) + (' fill-rule="evenodd"' if fill is not None else "")

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        style, data = pending
        stroke, fill, _ = style
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
            out.append(f'<text x="{_num(shape.x)}" y="{_num(shape.baseline)}" font-family="{families}" '
                       f'font-size="{shape.size}" fill="#000000">{escape(shape.text)}</text>')
            continue
        if _outside(shape, area):
            continue
        data = " ".join(cmd + "".join(f" {_num(x)} {_num(y)}" for x, y in pts) for cmd, pts in shape.commands)
        style = (shape.stroke, shape.fill, shape.stroke_width)
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
