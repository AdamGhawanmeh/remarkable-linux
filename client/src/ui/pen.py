"""The pens and the eraser this client draws with, and how a document keeps the choice.

Qt-free. The tools, sizes and colours are the tablet's (reMarkable.exe has the same
toolbar code, though its desktop QML never shows it): each pen is a Line::Tool, its size
the line's thicknessScale (1 thin, 2 medium, 3 thick), its colour a Line::Color.

A document keeps the settings in its .content "extraMetadata" (DocumentToolSettings,
0x14060e1e0), all strings, named by the Line::Tool and Line::Color enum names:

    LastTool          the tool in hand: "Ballpointv2", ..., "Eraser"
    LastPen           the last pen (not the eraser)
    LastEraserTool    "Eraser" or "EraseSection" (Erase selection)
    Last<Tool>Size    "1" | "2" | "3"
    Last<Tool>Color   "Black", "Gray", ..., "HighlighterYellow", or "ArgbCode" with
    Last<Tool>ColorCode  the ARGB colour, decimal

(every one of the 92 cached documents that has them, as read 2026-11-15).

The highlighter is the one pen whose colour goes as an ARGB colour: the tablet draws it
with Line::Color ArgbCode (9) and the colour itself in the line's field 8, and keeps
"ArgbCode" and that colour (decimal) in the settings. Every highlighter line the tablet
wrote in the cache (2026-11-20) is so: colour 9, ARGB 0xFFFFED75 (yellow), and the one
document holding them has LastHighlighterv2Color "ArgbCode", ColorCode "4294962549". The
ARGB of each highlighter colour is reMarkable.exe 3.29's palette (the Line::Color table
the app builds at 0x1409d25a0 and 0x1409d9faf: HIGHLIGHTER_ARGB below); its pen handler
(PenInputLineHandler, lineColor / lineArgbCode) puts them in the line as it is given
them. A highlighter colour kept by name (HighlighterYellow, older documents) is drawn
so too.
"""

from __future__ import annotations

from dataclasses import dataclass

from viewer import rm_stroke as s
from viewer.rm_parser import Color


@dataclass(frozen=True)
class PenTool:
    key: str                    # the toolbar's name for it
    label: str
    tool: int                   # Line::Tool
    name: str                   # its Line::Tool enum name, as extraMetadata has it
    icon: str                   # qrc:/ark/icons/<icon>
    default_size: int = 2
    default_color: str = "black"

    @property
    def is_eraser(self) -> bool:
        return self.tool in (s.ERASER, s.ERASE_SECTION)

    @property
    def is_area(self) -> bool:
        """An eraser whose line is the outline of the area it takes (EraseSection)."""
        return self.tool == s.ERASE_SECTION

    @property
    def sizes(self) -> bool:
        """Whether the tool comes in sizes (all but the area eraser)."""
        return not self.is_area

    @property
    def colors(self) -> tuple["PenColor", ...]:
        """The colours the tool offers (none for the eraser; the highlighter's own)."""
        if self.is_eraser:
            return ()
        return HIGHLIGHTER_COLORS if self.tool == s.HIGHLIGHTER_V2 else PEN_COLORS


TOOLS: tuple[PenTool, ...] = (
    PenTool("fineliner", "Fineliner", s.FINELINER_V2, "Finelinerv2", "fineliner"),
    PenTool("ballpoint", "Ballpoint", s.BALLPOINT_V2, "Ballpointv2", "ballpoint"),
    PenTool("marker", "Marker", s.MARKER_V2, "Markerv2", "marker"),
    PenTool("pencil", "Pencil", s.PENCIL_V2, "Pencilv2", "pencil"),
    PenTool("mechanical_pencil", "Mechanical pencil", s.SHARP_PENCIL_V2, "SharpPencilv2", "mechanical_pencil"),
    PenTool("paintbrush", "Paintbrush", s.PAINTBRUSH_V2, "Paintbrushv2", "paintbrush"),
    # Every cached document with highlighter settings has size "1" and a highlighter colour.
    PenTool("highlighter", "Highlighter", s.HIGHLIGHTER_V2, "Highlighterv2", "highlighter", 1, "yellow"),
    PenTool("calligraphy", "Calligraphy pen", s.CALLIGRAPHY, "Calligraphy", "calligraphy_pen"),
    # The Shader (Line::Tool ShadingMarker, the toolbar's ninth pen). No cached document
    # holds a stroke or a setting of it, and the tablet's toolbar (its colours) is not in
    # reMarkable.exe: it offers the pen colours, grey first, which is this client's choice.
    PenTool("shader", "Shader", s.SHADING_MARKER, "ShadingMarker", "shader", 2, "grey"),
    PenTool("eraser", "Eraser", s.ERASER, "Eraser", "eraser"),
    # The tablet's other two eraser modes (its icons erase_selection and erase_all are in
    # the app's qrc:/ark/icons). Erase selection (Line::Tool EraseSection) erases what a
    # drawn area covers: the line is the area's outline (rm_stroke.eraser_outline), with
    # no size or colour. Erase all (ClearPage) is no tool in hand but an action:
    # clearLinesInLayer on the current layer (CLEAR_ALL below).
    PenTool("erase_section", "Erase selection", s.ERASE_SECTION, "EraseSection", "erase_selection"),
)
CLEAR_ALL = ("erase_all", "Erase all", "erase_all")       # key, label, icon of the ClearPage action
BY_KEY = {t.key: t for t in TOOLS}
BY_NAME = {t.name: t for t in TOOLS}
DEFAULT_TOOL = BY_KEY["ballpoint"]          # LastPen of 75 of the 92 cached documents
SIZES = (1, 2, 3)
SIZE_LABELS = {1: "Thin", 2: "Medium", 3: "Thick"}


@dataclass(frozen=True)
class PenColor:
    key: str
    label: str
    color: Color

    @property
    def name(self) -> str:
        """The Line::Color enum name ("HighlighterYellow")."""
        return "".join(part.title() for part in self.color.name.split("_")).replace("Argb", "ArgbCode")


PEN_COLORS: tuple[PenColor, ...] = (
    PenColor("black", "Black", Color.BLACK), PenColor("grey", "Grey", Color.GRAY),
    PenColor("white", "White", Color.WHITE), PenColor("blue", "Blue", Color.BLUE),
    PenColor("red", "Red", Color.RED), PenColor("green", "Green", Color.GREEN),
    PenColor("yellow", "Yellow", Color.YELLOW), PenColor("pink", "Pink", Color.MAGENTA),
    PenColor("cyan", "Cyan", Color.CYAN),
)
# The highlighter draws in its own, lighter colours (Line::Color Highlighter*), as ARGB
# colours (see the module's doc).
HIGHLIGHTER_COLORS: tuple[PenColor, ...] = (
    PenColor("yellow", "Yellow", Color.HIGHLIGHTER_YELLOW), PenColor("green", "Green", Color.HIGHLIGHTER_GREEN),
    PenColor("pink", "Pink", Color.HIGHLIGHTER_PINK), PenColor("blue", "Blue", Color.HIGHLIGHTER_BLUE),
    PenColor("orange", "Orange", Color.HIGHLIGHTER_ORANGE), PenColor("grey", "Grey", Color.HIGHLIGHTER_GRAY),
)
# reMarkable.exe 3.29's palette entries of the highlighter colours (0x1409d25a0).
HIGHLIGHTER_ARGB: dict[Color, int] = {
    Color.HIGHLIGHTER_YELLOW: 0xFFFFED75, Color.HIGHLIGHTER_GREEN: 0xFFACFF85, Color.HIGHLIGHTER_PINK: 0xFFF29EFF,
    Color.HIGHLIGHTER_BLUE: 0xFFBEEAFE, Color.HIGHLIGHTER_ORANGE: 0xFFFFC38C, Color.HIGHLIGHTER_GRAY: 0xFFC7C7C6,
}


@dataclass(frozen=True)
class Pen:
    """A tool as set: what a stroke is drawn with."""

    tool: PenTool
    size: int
    color: PenColor | None              # None: the eraser, or an ARGB colour (`argb`) of no swatch
    argb: int | None = None             # set: the line is drawn with colour ArgbCode and this

    @property
    def line_color(self) -> int:
        if self.argb is not None:
            return s.COLOR_ARGB
        return int(self.color.color) if self.color is not None else int(Color.BLACK)


def _size(value) -> int | None:
    try:
        size = int(float(str(value)))
    except ValueError:
        return None
    return size if size in SIZES else None


def pen_of(settings: dict[str, str], tool: PenTool) -> Pen:
    """How `tool` is set in a document's extraMetadata (its defaults where it is not)."""
    size = _size(settings.get(f"Last{tool.name}Size")) or tool.default_size
    if tool.is_eraser:
        return Pen(tool, size, None)
    name = settings.get(f"Last{tool.name}Color")
    if name == "ArgbCode":
        try:
            argb = int(settings.get(f"Last{tool.name}ColorCode", "")) & 0xFFFFFFFF
        except ValueError:
            pass
        else:
            # The swatch of that colour, if the tool has one (the highlighter's).
            color = next((c for c in tool.colors if _argb_of(tool, c) == argb), None)
            return Pen(tool, size, color, argb)
    color = next((c for c in tool.colors if c.name == name), None)
    if color is None:
        color = next(c for c in tool.colors if c.key == tool.default_color)
    return Pen(tool, size, color, _argb_of(tool, color))


def _argb_of(tool: PenTool, color: PenColor) -> int | None:
    """The ARGB colour a line of `tool` in `color` is drawn with (the highlighter's,
    see the module's doc), or None: the line has the colour itself."""
    return HIGHLIGHTER_ARGB.get(color.color) if tool.tool == s.HIGHLIGHTER_V2 else None


def current_tool(settings: dict[str, str]) -> PenTool:
    """The tool a document was last used with (LastTool; LastPen when that is no tool
    here, e.g. the selection tool)."""
    for key in ("LastTool", "LastPen"):
        tool = BY_NAME.get(settings.get(key, ""))
        if tool is not None:
            return tool
    return DEFAULT_TOOL


def tool_values(tool: PenTool) -> dict[str, str]:
    """The extraMetadata keys set when `tool` is taken in hand."""
    if tool.is_eraser:
        return {"LastTool": tool.name, "LastEraserTool": tool.name}
    return {"LastTool": tool.name, "LastPen": tool.name}


def size_values(tool: PenTool, size: int) -> dict[str, str]:
    if size not in SIZES:
        raise ValueError(f"not a pen size: {size!r}")
    return {f"Last{tool.name}Size": str(size)}


def color_values(tool: PenTool, key: str) -> dict[str, str]:
    color = next((c for c in tool.colors if c.key == key), None)
    if color is None:
        raise ValueError(f"{tool.label} has no colour {key!r}")
    argb = _argb_of(tool, color)
    if argb is not None:
        return {f"Last{tool.name}Color": "ArgbCode", f"Last{tool.name}ColorCode": str(argb)}
    return {f"Last{tool.name}Color": color.name}
