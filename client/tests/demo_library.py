"""A self-contained demo library on the in-memory FakeCloud.

Used by `run.py --demo` (and automatically when no account is paired) and by the UI
tests. Needs a QGuiApplication: handwriting is generated from font outlines and the
PDFs with QPdfWriter.

Contents
  Work/ (pinned)            Research paper.pdf   PDF, annotated page + inserted note page
    Projects/               Meeting notes        notebook, handwriting + typed text page
  Personal/                 Long page            notebook page scrolling to y≈3300
  Sketchbook (pinned)       notebook, colours, highlighter, pencil
  A Short Story             EPUB with a PDF rendition
  Old draft                 in the trash
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QRectF, Qt
from PySide6.QtGui import QFont, QPageSize, QPainter, QPainterPath, QPdfWriter

from fakecloud import FakeCloud
from rmfixtures import (ROOT, ZERO, group_item, line_item, paragraph_format, points_v2, root_text,
                        text_item, tree_node, v6_file)

LAYER = (0, 11)
PX_PER_PT = 226 / 72
NAMESPACE = uuid.UUID("6f0e8a4e-6d2b-4c51-9c87-3b1f6d2e9a10")
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def demo_id(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


def ms(days_ago: float) -> str:
    return str(int((NOW - timedelta(days=days_ago)).timestamp() * 1000))


# ------------------------------------------------------------------------- pages

class PageBuilder:
    """Builds a v6 page in page pixel coordinates (x from the left edge)."""

    def __init__(self) -> None:
        self.blocks = [tree_node(ROOT), tree_node(LAYER, "Layer 1", True), group_item(ROOT, (1, 1), LAYER)]
        self._next = 100

    def stroke(self, pts, tool: int = 17, color: int = 0, width: float = 3.0, pressure: int = 200,
               argb: int | None = None) -> None:
        data = points_v2([(x - 702, y, 2.0, 0, width, pressure) for x, y in pts])
        self.blocks.append(line_item(LAYER, (1, self._next), tool=tool, color=color, points=data, argb=argb))
        self._next += 1

    def handwriting(self, text: str, x: float, y: float, size: int = 44, color: int = 0,
                    tool: int = 17, width: float = 2.5) -> None:
        f = QFont()
        f.setFamilies(["Noto Serif", "DejaVu Serif", "serif"])
        f.setItalic(True)
        f.setPixelSize(size)
        path = QPainterPath()
        path.addText(x, y, f, text)
        for poly in path.toSubpathPolygons():
            pts = [(poly[i].x(), poly[i].y()) for i in range(0, poly.size(), 2)]
            if len(pts) > 1:
                self.stroke(pts, tool=tool, color=color, width=width)

    def circle(self, cx: float, cy: float, r: float, **kw) -> None:
        self.stroke([(cx + r * math.cos(t / 40 * 2 * math.pi), cy + r * math.sin(t / 40 * 2 * math.pi))
                     for t in range(41)], **kw)

    def text(self, paragraphs: list[tuple[int, str]], x: float = 234, y: float = 234, width: float = 936) -> None:
        body = "\n".join(t for _, t in paragraphs)
        start, formats, pos = 1000, [], 0
        for i, (style, t) in enumerate(paragraphs):
            char = ZERO if i == 0 else (1, start + pos - 1)     # id of the preceding "\n"
            formats.append(paragraph_format(char, style))
            pos += len(t) + 1
        self.blocks.append(root_text([text_item((1, start), ZERO, text=body)], formats,
                                     x=x - 702, y=y, width=width))

    def bytes(self) -> bytes:
        return v6_file(*self.blocks)


def pdf_px(x_pt: float, y_pt: float, page_w_pt: float = 595) -> tuple[float, float]:
    """PDF point → page pixel (226 DPI, centred on the page's vertical centre line)."""
    return 702 + (x_pt - page_w_pt / 2) * PX_PER_PT, y_pt * PX_PER_PT


def make_pdf(title: str, pages: list[list[str]]) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(72)                       # painter units = PDF points
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    painter = QPainter(writer)
    for n, paragraphs in enumerate(pages):
        if n:
            writer.newPage()
        heading = QFont(); heading.setFamilies(["Noto Serif", "serif"]); heading.setPointSize(20); heading.setBold(True)
        body = QFont(); body.setFamilies(["Noto Serif", "serif"]); body.setPointSize(11)
        painter.setFont(heading)
        painter.drawText(QRectF(60, 55, 475, 40), Qt.AlignmentFlag.AlignLeft, title if n == 0 else f"{title} (cont.)")
        painter.setFont(body)
        y = 110.0
        for text in paragraphs:
            rect = painter.boundingRect(QRectF(60, y, 475, 400), Qt.TextFlag.TextWordWrap, text)
            painter.drawText(rect, Qt.TextFlag.TextWordWrap, text)
            y = rect.bottom() + 12
        painter.drawText(QRectF(60, 800, 475, 20), Qt.AlignmentFlag.AlignHCenter, f"— {n + 1} —")
    painter.end()
    return bytes(buffer.data())


LOREM = ("The reMarkable stores every page as a scene of strokes, and every document as a "
         "set of small files addressed by their SHA-256 hash. A Linux client only needs to "
         "fetch the files it shows, cache them, and render them at the right scale.")


# --------------------------------------------------------------------- documents

def _content(file_type: str, page_ids: list[str], redirects: list[int | None] | None = None,
             tags: list[str] = (), extra: dict | None = None, templates: list[str] | None = None) -> dict:
    pages = []
    for i, pid in enumerate(page_ids):
        template = templates[i] if templates else "Blank"
        page = {"id": pid, "idx": {"timestamp": "1:2", "value": f"b{chr(ord('a') + i)}"},
                "template": {"timestamp": "1:1", "value": template}}
        if redirects and redirects[i] is not None:
            page["redir"] = {"timestamp": "1:2", "value": redirects[i]}
        pages.append(page)
    return {"fileType": file_type, "formatVersion": 2, "pageCount": len(page_ids), "orientation": "portrait",
            "cPages": {"pages": pages}, "tags": [{"name": t, "timestamp": 1} for t in tags],
            "coverPageNumber": 0, **(extra or {})}


def _meta(name: str, parent: str, days_ago: float, kind: str = "DocumentType", pinned: bool = False,
          last_opened_page: int = 0) -> dict:
    return {"visibleName": name, "parent": parent, "type": kind, "pinned": pinned,
            "lastModified": ms(days_ago), "lastOpened": ms(days_ago), "createdTime": ms(days_ago + 30),
            "lastOpenedPage": last_opened_page}


def build_demo_cloud() -> FakeCloud:
    cloud = FakeCloud(schema=3)
    work, projects, personal = demo_id("Work"), demo_id("Projects"), demo_id("Personal")
    cloud.put_item(work, _meta("Work", "", 1, "CollectionType", pinned=True))
    cloud.put_item(projects, _meta("Projects", work, 2, "CollectionType"))
    cloud.put_item(personal, _meta("Personal", "", 5, "CollectionType"))

    # Meeting notes: handwriting + highlighter, then a typed-text page.
    notes, p1, p2 = demo_id("Meeting notes"), demo_id("notes-p1"), demo_id("notes-p2")
    a = PageBuilder()
    a.handwriting("Weekly sync", 160, 220, size=72)
    a.stroke([(150, 250), (600, 252)], tool=18, color=3, width=36)            # highlighter under title
    for i, line in enumerate(["Ship the Linux client", "Review sync retries", "Render PDFs at 226 dpi"]):
        a.handwriting(f"- {line}", 180, 360 + i * 90, size=44)
    a.handwriting("Done!", 900, 700, size=52, color=7)                         # red
    a.circle(1000, 690, 110, color=6, width=3)                                 # blue circle
    b = PageBuilder()
    b.text([(0, "Action items"), (2, "Typed with the Type Folio:"), (3, "Pair a device"),
            (3, "Sync the library"), (6, "Open a notebook")])
    b.handwriting("signed off", 820, 820, size=40, color=6)
    cloud.put_item(notes, _meta("Meeting notes", projects, 0.2, last_opened_page=0),
                   _content("notebook", [p1, p2], tags=["work"], templates=["P Lines small", "Blank"]),
                   {f"{notes}/{p1}.rm": a.bytes(), f"{notes}/{p2}.rm": b.bytes()})

    # Sketchbook: colours and tools.
    sketch, s1 = demo_id("Sketchbook"), demo_id("sketch-p1")
    c = PageBuilder()
    for i, (color, tool) in enumerate([(0, 17), (6, 17), (7, 17), (10, 15), (12, 15), (1, 14)]):
        c.stroke([(200 + t * 6, 300 + i * 110 + 30 * math.sin(t / 8)) for t in range(160)],
                 tool=tool, color=color, width=4 if tool != 14 else 6)
    c.stroke([(220, 1000), (1180, 1000)], tool=18, color=14, width=50)         # highlighter blue
    c.circle(702, 1400, 220, color=0, width=5)
    c.handwriting("Sketchbook", 420, 1430, size=80, color=6)
    cloud.put_item(sketch, _meta("Sketchbook", "", 3, pinned=True),
                   _content("notebook", [s1], templates=["P Dots S"]),
                   {f"{sketch}/{s1}.rm": c.bytes()})

    # Long page: content well beyond the 1872 px screen height.
    long_doc, l1 = demo_id("Long page"), demo_id("long-p1")
    d = PageBuilder()
    for i in range(34):
        d.handwriting(f"Line {i + 1}: the page keeps going", 120, 140 + i * 95, size=44)
    cloud.put_item(long_doc, _meta("Long page", personal, 6),
                   _content("notebook", [l1], templates=["P Lines medium"]),
                   {f"{long_doc}/{l1}.rm": d.bytes()})

    # Research paper: 2-page PDF, annotations on page 1, an inserted notebook page.
    paper, r1, rnote, r2 = (demo_id("Research paper"), demo_id("paper-p1"), demo_id("paper-note"),
                            demo_id("paper-p2"))
    pdf = make_pdf("Research paper", [[LOREM] * 4, [LOREM] * 3])
    e = PageBuilder()
    x0, y0 = pdf_px(58, 70)
    x1, _ = pdf_px(300, 70)
    e.stroke([(x0, y0), (x1, y0)], tool=18, color=3, width=60)                # highlight the title
    ux0, uy = pdf_px(60, 152)
    ux1, _ = pdf_px(530, 152)
    e.stroke([(ux0, uy), (ux1, uy)], tool=17, color=7, width=4)              # red underline
    mx, my = pdf_px(470, 300)
    e.handwriting("key point!", mx - 220, my, size=46, color=6)
    f = PageBuilder()
    f.handwriting("Notes on the paper", 160, 260, size=60)
    f.handwriting("- placement: 226 px per inch", 180, 400, size=42)
    cloud.put_item(paper, _meta("Research paper", work, 0.5),
                   _content("pdf", [r1, rnote, r2], [0, None, 1], tags=["work", "reading"],
                            templates=["Blank", "P Grid medium", "Blank"]),
                   {f"{paper}.pdf": pdf, f"{paper}/{r1}.rm": e.bytes(), f"{paper}/{rnote}.rm": f.bytes()})

    # Ebook: EPUB plus the PDF rendition the cloud stores alongside it.
    book, b1, b2 = demo_id("A Short Story"), demo_id("book-p1"), demo_id("book-p2")
    story = make_pdf("A Short Story", [["Once upon a time, a tablet met a penguin. " * 6] * 3,
                                       ["They synced happily ever after. " * 8] * 2])
    cloud.put_item(book, _meta("A Short Story", "", 10), _content("epub", [b1, b2], [0, 1], tags=["reading"]),
                   {f"{book}.epub": b"PK\x03\x04 demo epub", f"{book}.pdf": story})

    # Trash.
    draft, d1 = demo_id("Old draft"), demo_id("draft-p1")
    g = PageBuilder()
    g.handwriting("an old idea", 200, 300, size=50)
    cloud.put_item(draft, _meta("Old draft", "trash", 20),
                   _content("notebook", [d1], templates=["P Margin medium"]),
                   {f"{draft}/{d1}.rm": g.bytes()})
    return cloud


IDS = {name: demo_id(name) for name in ("Work", "Projects", "Personal", "Meeting notes", "Sketchbook",
                                        "Long page", "Research paper", "A Short Story", "Old draft")}

__all__ = ["IDS", "NOW", "build_demo_cloud", "demo_id"]
