"""The `{doc}.highlights/{page}.json` file of a PDF/EPUB page.

reMarkable.exe 3.29 rebuilds it from the scene each time it stores a page (0x1405a8760,
ARCHITECTURE.md section 5.11): for each layer, in the scene's layer order, an array of the
layer's own glyph-range items in sequence order, each {"color", "length", "rects": [{"height",
"width", "x", "y"}], "start", "text"}; the document is {"highlights": [layer arrays]}. It is
written with QJsonDocument::toJson(Indented) when a glyph range exists anywhere, and the file
is removed when none does. `qt_json` writes the same bytes Qt 6 does.
"""

from __future__ import annotations

import math

from viewer import rm_parser


def _string(text: str) -> str:
    """A JSON string as Qt 6's writer escapes it: UTF-8 raw except \\" \\\\ and the control
    characters (\\b \\f \\n \\r \\t named, the others \\u00xx in lowercase hex) [I: the Qt
    writer's details are Qt source knowledge, not traced in the binary]."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in "\b\f\n\r\t":
            out.append({"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _number(value: float) -> str:
    """A double as Qt 6 writes it [I]: an integral value as an integer, otherwise
    QByteArray::number(d, 'g', FloatingPointShortest) (Python's repr is the shortest round
    trip and switches to an exponent exactly where %g does); NaN and infinity are null."""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == math.floor(value) and abs(value) < 2.0 ** 63:
        return str(int(value))
    return repr(float(value))


def qt_json(value) -> str:
    """`value` (dict, list, str, int, float, bool, None) as QJsonDocument::toJson(Indented)
    would write it as a document (a trailing newline at the top level) [I]: 4 spaces per
    level, `"key": value`, keys sorted by UTF-16 code units, members joined by ",\\n", an
    empty array or object as the opening bracket, a newline and the closing bracket."""
    return _value(value, 0) + "\n"


def _value(value, level: int) -> str:
    pad = "    " * level
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
        body = ",\n".join(f"{pad}    {_string(k)}: {_value(value[k], level + 1)}" for k in keys)
        return "{\n" + (body + "\n" if body else "") + pad + "}"
    if isinstance(value, (list, tuple)):
        body = ",\n".join(f"{pad}    {_value(v, level + 1)}" for v in value)
        return "[\n" + (body + "\n" if body else "") + pad + "]"
    if isinstance(value, str):
        return _string(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _number(value)


def highlights_json(page: bytes) -> bytes | None:
    """The highlights file of a v6 page, or None when no layer holds a glyph range (the
    file is then removed, not written). RmParseError when the page cannot be read."""
    scene = rm_parser.parse(page)
    if scene.version != 6:
        return None
    layers = [[{"color": g.color, "length": g.length or 0,
                "rects": [{"height": h, "width": w, "x": x, "y": y} for x, y, w, h in g.rects],
                "start": g.start or 0, "text": g.text}
               for g in sorted(layer.glyphs, key=lambda g: g.seq)]
              for layer in scene.layers]
    if not any(layers):
        return None
    return qt_json({"highlights": layers}).encode("utf-8")
