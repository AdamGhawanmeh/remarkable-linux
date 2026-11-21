"""Handwriting conversion: the cloud's MyScript recogniser, as reMarkable.exe 3.29 calls it
(src/handwritingconversion, ARCHITECTURE.md §5.12).

One request per task (a page, or the strokes selected): POST /convert/v1/handwriting on
the gateway with the user token, `Accept: application/vnd.myscript.jiix`, and a JSON
body built as RecognitionTask builds it (0x1401a5300):

    {"xDPI": 2280, "yDPI": 2280, "width": 14040, "height": 18720,
     "contentType": "Raw Content",
     "configuration": {"lang": "en_US",
                       "export": {"jiix": {"strokes": false,
                                           "text": {"chars": false, "words": true}}},
                       "raw-content": {"recognition": {"text": true, "shape": false}}},
     "strokeGroups": [{"strokes": [{"x": [...], "y": [...]}, ...]}]}

Coordinates are scene coordinates times 10 (the double 10.0 at 0x14102b758); the DPI is
2280 (0x14102b788) and the size the 1404 x 1872 screen times 10 (0x14102b790/798). A
task is tried 3 times (0x14017dde7, "Failed to convert page {} after {} attempts") with
a 70 s timeout (0x11170 ms).

The answer is JIIX; jiixparser.cpp reads its `elements` of type "Text" (`label`,
`words`, `bounding-box`) and makes paragraphs of the lines, recognising list markers with
two regular expressions (0x1008cf8, 0x1008d20): a line starting with one of
`* • ° ◦ → -` is a bullet, one starting with `0 o O ✓ ☐ ☑ ☒` a checkbox (the ticked
glyphs a ticked one).

This module only builds requests, sends them through a transport (core.auth's
authorized_request) and parses answers. Tests use a fake handler; nothing here is
called without the bridge asking for it.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

PATH = "/convert/v1/handwriting"
JIIX = "application/vnd.myscript.jiix"
SCALE = 10.0
DPI = 2280
WIDTH, HEIGHT = 14040, 18720
ATTEMPTS = 3
TIMEOUT_S = 70.0

BULLET = re.compile(r"^(\*\s|•\s|°\s|◦\s|→\s|-\s)")
CHECKBOX = re.compile(r"^(0\s|o\s|O\s|✓\s|☐\s|☑\s|☒\s)")
CHECKED = ("✓", "☑", "☒")


class ConvertError(IntEnum):
    """RecognitionJob::ConvertError, as the app numbers it."""
    Unknown = 0
    NoError = 1
    NoTasks = 2
    NoText = 3
    NetworkError = 4
    ServerError = 5
    AuthenticationError = 6


class Kind(IntEnum):
    """Paragraph kinds the parser makes (mapped to ParagraphStyle by the bridge)."""
    TEXT = 0
    BULLET = 1
    CHECKBOX = 2
    CHECKBOX_CHECKED = 3


@dataclass(frozen=True)
class Paragraph:
    kind: Kind
    text: str


@dataclass
class Recognition:
    """What one task gave: its paragraphs, or the error it ended with."""
    paragraphs: list[Paragraph] = field(default_factory=list)
    error: ConvertError = ConvertError.NoError

    @property
    def plain(self) -> str:
        return plain_text(self.paragraphs)


class RecognitionFailed(Exception):
    def __init__(self, error: ConvertError, detail: str = "") -> None:
        super().__init__(detail or error.name)
        self.error = error


Stroke = Sequence[tuple[float, float]]


def request_body(strokes: Iterable[Stroke], language: str) -> bytes:
    """The JSON the app posts for one task: its strokes (scene coordinates) times 10."""
    group = []
    for points in strokes:
        pts = list(points)
        if not pts:
            continue
        group.append({"x": [round(x * SCALE, 2) for x, _ in pts], "y": [round(y * SCALE, 2) for _, y in pts]})
    body = {
        "xDPI": DPI, "yDPI": DPI, "width": WIDTH, "height": HEIGHT,
        "contentType": "Raw Content",
        "configuration": {
            "lang": language,
            "export": {"jiix": {"strokes": False, "text": {"chars": False, "words": True}}},
            "raw-content": {"recognition": {"text": True, "shape": False}},
        },
        "strokeGroups": [{"strokes": group}],
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def _box_key(element: dict) -> tuple[float, float]:
    box = element.get("bounding-box") if isinstance(element.get("bounding-box"), dict) else {}
    try:
        return float(box.get("y", 0.0)), float(box.get("x", 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def parse_jiix(data: bytes | str | dict) -> list[Paragraph]:
    """The paragraphs of a JIIX answer: the Text elements from top to bottom, each line a
    paragraph, list markers recognised (and taken off). ValueError for what is not JIIX."""
    doc = json.loads(data) if isinstance(data, (bytes, str)) else data
    if not isinstance(doc, dict):
        raise ValueError("JIIX is not a JSON object")
    elements = doc.get("elements")
    if elements is None and doc.get("type") == "Text":
        elements = [doc]
    if not isinstance(elements, list):
        raise ValueError("JIIX has no elements")
    texts = [e for e in elements if isinstance(e, dict) and e.get("type") == "Text"]
    paragraphs: list[Paragraph] = []
    for element in sorted(texts, key=_box_key):
        label = element.get("label")
        if not isinstance(label, str):
            words = element.get("words") if isinstance(element.get("words"), list) else []
            label = "".join(str(w.get("label", "")) for w in words if isinstance(w, dict))
        for line in label.replace("\r\n", "\n").split("\n"):
            paragraphs.append(paragraph_of(line))
    while paragraphs and not paragraphs[-1].text and paragraphs[-1].kind == Kind.TEXT:
        paragraphs.pop()
    while paragraphs and not paragraphs[0].text and paragraphs[0].kind == Kind.TEXT:
        paragraphs.pop(0)
    return paragraphs


def paragraph_of(line: str) -> Paragraph:
    line = line.rstrip()
    m = BULLET.match(line)
    if m:
        return Paragraph(Kind.BULLET, line[m.end():].strip())
    m = CHECKBOX.match(line)
    if m:
        checked = m.group(1).strip() in CHECKED
        return Paragraph(Kind.CHECKBOX_CHECKED if checked else Kind.CHECKBOX, line[m.end():].strip())
    return Paragraph(Kind.TEXT, line.strip())


def plain_text(paragraphs: Iterable[Paragraph]) -> str:
    """RecognitionJob.plainText: the paragraphs' text, one per line, markers as typed."""
    marks = {Kind.TEXT: "", Kind.BULLET: "• ", Kind.CHECKBOX: "☐ ", Kind.CHECKBOX_CHECKED: "☑ "}
    return "\n".join(marks[p.kind] + p.text for p in paragraphs)


def clipboard_html(paragraphs: Sequence[Paragraph]) -> str:
    """formattedTextToClipboardText's rich side: paragraphs, bullet runs as a list."""
    out: list[str] = []
    in_list = False
    for p in paragraphs:
        text = html.escape(p.text)
        if p.kind == Kind.BULLET:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{text}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if p.kind in (Kind.CHECKBOX, Kind.CHECKBOX_CHECKED):
            box = "☑" if p.kind == Kind.CHECKBOX_CHECKED else "☐"
            out.append(f"<p>{box} {text}</p>")
        else:
            out.append(f"<p>{text}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def clipboard_text(paragraphs: Sequence[Paragraph]) -> dict:
    """SceneClipboardText as the bridge's Clipboard takes it."""
    return {"text": plain_text(paragraphs), "html": clipboard_html(paragraphs)}


# A request function: (method, path, headers, body) -> HttpResponse-like (status, body).
Request = Callable[..., object]


def recognize(request: Request, strokes: Sequence[Stroke], language: str,
              cancelled: Callable[[], bool] = lambda: False) -> Recognition:
    """One task: post the strokes, parse the answer. The app's error mapping:
    no strokes -> NoTasks; HTTP 401/403 or no token -> AuthenticationError; a
    connection that fails every attempt -> NetworkError; any other refusal, or an answer
    that is not JIIX -> ServerError; JIIX without text -> NoText."""
    strokes = [s for s in strokes if len(s)]
    if not strokes:
        return Recognition(error=ConvertError.NoTasks)
    body = request_body(strokes, language)
    headers = {"Accept": JIIX, "Content-Type": "application/json"}
    problem = ""
    for attempt in range(ATTEMPTS):
        if cancelled():
            raise RecognitionFailed(ConvertError.Unknown, "aborted")
        try:
            resp = request("POST", PATH, headers=headers, body=body)
        except RecognitionFailed:
            raise
        except PermissionError as err:              # no token: not paired
            log.info("handwriting conversion refused: %s", err)
            return Recognition(error=ConvertError.AuthenticationError)
        except OSError as err:
            problem = f"{type(err).__name__}: {err}"
            log.info("handwriting conversion attempt %d failed: %s", attempt + 1, problem)
            continue
        status = int(getattr(resp, "status", 0))
        if status in (401, 403):
            return Recognition(error=ConvertError.AuthenticationError)
        if status == 429 or status >= 500:
            problem = f"HTTP {status}"
            log.info("handwriting conversion attempt %d failed: %s", attempt + 1, problem)
            continue
        if status != 200:
            log.info("handwriting conversion refused: HTTP %d", status)
            return Recognition(error=ConvertError.ServerError)
        try:
            paragraphs = parse_jiix(getattr(resp, "body", b""))
        except (ValueError, json.JSONDecodeError) as err:
            log.info("handwriting conversion answer is not JIIX: %s", err)
            return Recognition(error=ConvertError.ServerError)
        if not any(p.text for p in paragraphs):
            return Recognition(error=ConvertError.NoText)
        return Recognition(paragraphs)
    log.info("Failed to convert after %d attempts: %s", ATTEMPTS, problem)
    return Recognition(error=ConvertError.ServerError if problem.startswith("HTTP") else ConvertError.NetworkError)


def combine(results: Sequence[Recognition]) -> Recognition:
    """A job's outcome from its tasks' (RecognitionQueue::onTaskFinished): the first
    error of a task that failed outright wins; tasks with no text are skipped as long as
    another one has text (NoText when none has)."""
    hard = [r.error for r in results if r.error not in (ConvertError.NoError, ConvertError.NoText,
                                                         ConvertError.NoTasks)]
    if hard:
        return Recognition(error=hard[0])
    paragraphs = [p for r in results if r.error == ConvertError.NoError for p in r.paragraphs]
    if paragraphs:
        return Recognition(paragraphs)
    if results and all(r.error == ConvertError.NoTasks for r in results):
        return Recognition(error=ConvertError.NoTasks)
    return Recognition(error=ConvertError.NoText)
