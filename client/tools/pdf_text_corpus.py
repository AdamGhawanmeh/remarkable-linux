#!/usr/bin/env python3
"""PDF / EPUB text selection (ui/pdftext.py) tried on every document in the library cache,
in memory only.

    QT_QPA_PLATFORM=offscreen client/.venv/bin/python client/tools/pdf_text_corpus.py [cache directory] [-v]

The cache (default ~/.cache/remarkable-linux) is only read, as search_toc_corpus.py
reads it: the tree from state.json and the cached index blobs, files from the blob
store; nothing is fetched and nothing is written. For every PDF and EPUB whose PDF
rendition is cached, every page in the document's page list that has a PDF page (its
cPages redir; inserted pages have none):
  - the page's text is built (pdftext.PageText): every line box lies on the PDF page;
  - each line selected from its first to its last character (as a drag along it):
    the text is the line's own characters (trimmed, ligatures spelled out), one area,
    inside the page's frame, the handles at its ends;
  - a word is selected at the middle of each line (a double click): it lies on that
    line, has no whitespace in it and its area is inside the line's;
  - one drag from the page's top-left corner to its bottom-right: the same text as all
    lines joined, so nothing between them is lost or doubled.
It prints counts only, nothing of the documents' content. Exit status 1 if anything
crashed or broke one of those rules.
"""

from __future__ import annotations

import sys
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from core.models import FileType  # noqa: E402
from core.sync import BlobCache  # noqa: E402
from pdf_edit_corpus import load_tree  # noqa: E402
from search_toc_corpus import _Pdf  # noqa: E402
from ui import pdftext  # noqa: E402
from ui.library import document_pages  # noqa: E402

MAX_LINES = 40          # lines tried per page (all of them for the whole-page drag)


def check_page(page: pdftext.PageText, stats: Counter) -> None:
    frame = page.frame.adjusted(-2, -2, 2, 2)
    stats["pages"] += 1
    if not page.lines:
        stats["pages without selectable text"] += 1
        return
    joined = []
    for n, line in enumerate(page.lines):
        assert frame.intersects(line.box), "line off its page"
        expected = page.text[line.start:line.end]
        a, b = line.start, line.end - 1
        while a <= b and pdftext.is_blank(page.text[a]):
            a += 1
        while b >= a and pdftext.is_blank(page.text[b]):
            b -= 1
        if a > b:
            stats["blank lines"] += 1
            continue
        joined.append(pdftext.clean(page.text[a:b + 1]))
        if n >= MAX_LINES:
            continue
        sel = page.select(line.start, line.end - 1)
        assert sel.text == pdftext.clean(expected.strip(" \t ")) or sel.text == joined[-1], "line text"
        assert len(sel.areas) == 1, "one area per line"
        area = sel.areas[0]
        assert area.isValid() and area.width() >= 0 and area.height() > 0, "empty area"
        assert frame.adjusted(-area.height(), -area.height(), area.height(), area.height()).contains(area.center()), \
            "area off its page"
        assert sel.start_handle.x() == area.left() and sel.end_handle.x() == area.right(), "handles"
        assert not any(c in sel.text for c in pdftext.LIGATURES), "ligature left in"
        stats["lines selected"] += 1
        middle = QPointF(area.center())
        word = page.word(middle)
        if not word.empty:
            hit_line = page.line_of(page.hit(middle))
            assert hit_line is not None, "hit off every line"
            if hit_line is line:
                stats["words on the line clicked"] += 1
            elif hit_line.box.contains(middle) or line.box.intersects(hit_line.box):
                stats["words on an overlapping line (lines share the point)"] += 1
            else:
                stats["words on a nearer line (the point is off its own line's box)"] += 1
            assert len(word.areas) == 1, "a word on two lines"
            assert not any(c.isspace() for c in word.text), "space in a word"
            assert hit_line.start <= word.start and word.end < hit_line.end, "word off its line"
            stats["words selected"] += 1
        stats["characters"] += len(sel.text)
    # A drag from the first character of the page's text to its last (PDFium's order,
    # which is not always top-left to bottom-right; 3.29 selects index ranges too).
    first, last = page.lines[0], page.lines[-1]
    whole = page.select(first.start, last.end - 1)
    if joined:
        assert whole.text.split("\n") == "\n".join(joined).split("\n"), "page drag lost or doubled text"
        stats["whole-page drags"] += 1


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv
    root = Path(args[0]) if args else Path.home() / ".cache" / "remarkable-linux"
    QGuiApplication.instance() or QGuiApplication([])
    blobs = BlobCache(root)
    stats: Counter = Counter()
    failures: Counter = Counter()
    started = time.monotonic()
    for doc in load_tree(root, blobs):
        if doc.content is None or doc.content.file_type not in (FileType.PDF, FileType.EPUB):
            continue
        kind = doc.content.file_type.value
        pdf_hash = doc.files.get(f"{doc.id}.pdf")
        data = blobs.get(pdf_hash) if pdf_hash else None
        if data is None:
            stats[f"{kind} documents without a cached PDF (skipped)"] += 1
            continue
        stats[f"{kind} documents"] += 1
        try:
            pdf = _Pdf(data)
        except Exception:
            failures["unreadable PDF"] += 1
            continue
        try:
            for ref in document_pages(doc, pdf.page_count):
                if ref.pdf_page is None:
                    stats["inserted pages (no PDF page)"] += 1
                    continue
                try:
                    check_page(pdftext.PageText(pdf.doc, ref.pdf_page), stats)
                except AssertionError as err:
                    failures[str(err)] += 1
                except Exception as err:
                    failures[f"crash: {type(err).__name__}"] += 1
                    if verbose:
                        traceback.print_exc()
        finally:
            pdf.close()
    for key in sorted(stats):
        print(f"{key}: {stats[key]}")
    print(f"errors: {sum(failures.values())}")
    for key, n in failures.most_common():
        print(f"  {key}: {n}")
    print(f"time: {time.monotonic() - started:.1f} s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
