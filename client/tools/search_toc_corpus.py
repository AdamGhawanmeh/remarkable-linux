#!/usr/bin/env python3
"""In-document search and tables of contents, tried on every document in the library
cache, in memory only.

    QT_QPA_PLATFORM=offscreen client/.venv/bin/python client/tools/search_toc_corpus.py [cache directory]

The cache (default ~/.cache/remarkable-linux) is only read: the tree is rebuilt from
state.json and the cached index blobs, and files are read from the blob store
(BlobCache.get, a plain file read); nothing is fetched and nothing is written. For:
  - every PDF and EPUB whose PDF rendition is cached: its table of contents is loaded
    (ui.docsearch.table_of_contents; an EPUB's own navigation too when the .epub is
    cached); every entry's page is a page of the document or -1, levels start at 0 and
    go down one at a time; then every page is searched (ui.docsearch.page_hits, its PDF
    page through the page list plus the typed text of its .rm) for a few common terms:
    every hit is on its page, every box valid, inside the page's PDF frame when it is a
    PDF hit;
  - every notebook page with typed text (.rm v6 RootText): the same search on it, its
    boxes inside the laid-out text's lines.
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

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtPdf import QPdfDocument  # noqa: E402

from core.models import FileType  # noqa: E402
from core.sync import BlobCache  # noqa: E402
from pdf_edit_corpus import load_tree  # noqa: E402
from ui import docsearch  # noqa: E402
from ui.library import document_pages  # noqa: E402
from ui.pages import PageContent  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

TERMS = ("the", "e", "and", "zqxjv")          # common words, a letter, and one no text has


class _Pdf:
    """ui.pages.PdfFile over bytes in memory."""

    def __init__(self, data: bytes) -> None:
        self._buffer = QBuffer()
        self._buffer.setData(QByteArray(data))
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.doc = QPdfDocument()
        self.doc.load(self._buffer)
        if self.doc.status() != QPdfDocument.Status.Ready:
            raise ValueError("PDF not readable")

    @property
    def page_count(self) -> int:
        return self.doc.pageCount()

    def page_size(self, index: int):
        return self.doc.pagePointSize(index)

    def close(self) -> None:
        self.doc.close()
        self._buffer.close()


def check_hits(results: list[dict], page: int, frame: QRectF | None, stats: Counter) -> None:
    for found in results:
        assert found["page"] == page, "hit on another page"
        assert found["metrics"], "hit without a box"
        for m in found["metrics"]:
            box = m["boundingRect"]
            assert box.isValid() and box.width() > 0 and box.height() > 0, "empty box"
            if frame is not None and frame.isValid():
                assert frame.adjusted(-2, -2, 2, 2).intersects(box), "PDF hit off its page"
        stats["hits"] += 1
        stats["boxes"] += len(found["metrics"])


def check_toc(chapters: list[dict], page_count: int, stats: Counter) -> None:
    last = -1
    for c in chapters:
        assert set(c) == {"name", "path", "page", "level"}, "chapter fields"
        assert -1 <= c["page"] < page_count, "chapter page out of range"
        assert 0 <= c["level"] <= last + 1, "level skips"
        last = c["level"]
        stats["table of contents entries"] += 1
        if c["page"] == -1:
            stats["entries not placed (-1)"] += 1


def scene_of(blobs: BlobCache, doc, page) -> object | None:
    name = page.rm_name
    if name is None:
        return None
    data = blobs.get(doc.files[name])
    if data is None:
        return None
    try:
        return parse(data)
    except Exception:
        return None


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "-v"]
    root = Path(args[0]) if args else Path.home() / ".cache" / "remarkable-linux"
    QGuiApplication.instance() or QGuiApplication([])          # QtPdf, and the app's fonts for typed text
    blobs = BlobCache(root)
    stats: Counter = Counter()
    failures: Counter = Counter()
    started = time.monotonic()
    for doc in load_tree(root, blobs):
        if doc.content is None:
            continue
        kind = doc.content.file_type
        try:
            if kind in (FileType.PDF, FileType.EPUB):
                pdf_hash = doc.files.get(f"{doc.id}.pdf")
                data = blobs.get(pdf_hash) if pdf_hash else None
                if data is None:
                    stats[f"{kind.value} documents without a cached PDF (skipped)"] += 1
                    continue
                stats[f"{kind.value} documents"] += 1
                pdf = _Pdf(data)
                try:
                    pages = document_pages(doc, pdf.page_count)
                    pdf_pages = [p.pdf_page for p in pages]
                    epub_hash = doc.files.get(f"{doc.id}.epub")
                    epub = (lambda h=epub_hash: blobs.get(h)) if kind is FileType.EPUB and epub_hash else None
                    if epub is not None and blobs.get(epub_hash) is None:
                        stats["EPUBs without a cached .epub (rendition outline only)"] += 1
                    chapters = docsearch.table_of_contents(pdf.doc, pdf_pages, epub)
                    check_toc(chapters, len(pages), stats)
                    stats["documents with a table of contents" if chapters else "documents without one"] += 1
                    if any(p.pdf_page is None or p.pdf_page != p.index for p in pages):
                        stats["documents with inserted or moved pages"] += 1
                    for page in pages:
                        content = PageContent(scene_of(blobs, doc, page), pdf, page.pdf_page)
                        frame = None
                        if content.has_pdf_page:
                            frame = docsearch.pdf_frame(pdf.page_size(page.pdf_page))
                        cache: dict[int, str] = {}
                        for term in TERMS:
                            results = docsearch.page_hits(content, page.index, term, cache)
                            check_hits([r for r in results], page.index, None, stats)
                            if content.has_pdf_page:
                                check_hits(docsearch.page_hits(PageContent(None, pdf, page.pdf_page), page.index,
                                                               term, cache), page.index, frame, Counter())
                        stats["pages searched"] += 1
                finally:
                    pdf.close()
            elif kind is FileType.NOTEBOOK:
                stats["notebooks"] += 1
                for page in document_pages(doc):
                    scene = scene_of(blobs, doc, page)
                    if scene is None or scene.text is None or scene.text.is_blank:
                        continue
                    stats["typed text pages searched"] += 1
                    lines = docsearch.layout_text(scene, docsearch.qt_measure)
                    for term in TERMS:
                        hits = docsearch.text_hits(scene, term)
                        check_hits([docsearch.result(page.index, h) for h in hits], page.index, None, stats)
                        for h in hits:
                            for m in h:
                                assert any(abs(m["baseline"] - line.baseline) < 1e-6 for line in lines), \
                                    "typed hit off its line"
                        stats["typed text hits"] += len(hits)
        except Exception as err:
            failures[f"{type(err).__name__}: {err}"] += 1
            stats["documents failed"] += 1
            if "-v" in sys.argv:
                traceback.print_exc()
    stats["seconds"] = round(time.monotonic() - started)
    for key in sorted(stats):
        print(f"{key}: {stats[key]}")
    for key, n in failures.most_common():
        print(f"FAILED {n}x: {key}")
    print("crashes/failures:", sum(failures.values()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
