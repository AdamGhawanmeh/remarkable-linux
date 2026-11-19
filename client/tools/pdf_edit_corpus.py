#!/usr/bin/env python3
"""Editing PDF and EPUB pages, tried on every one in the library cache, in memory only.

    QT_QPA_PLATFORM=offscreen client/.venv/bin/python client/tools/pdf_edit_corpus.py [cache directory]

The cache (default ~/.cache/remarkable-linux) is only read: the tree is rebuilt from
state.json and the cached index blobs, files are read from the blob store, and nothing
is written anywhere. For every PDF and EPUB document with a page list:
  - for every live page (with or without a .rm), a run of edits is made on it as
    core.edits.replay makes them before an upload: a stroke across the PDF page, from
    beyond its left edge to its right and down to its bottom (the page's own size at
    226/72 px per point; not clipped to 1404 x 1872), an eraser pass over it, an image
    put on the page, moved, the stroke copied and pasted, every live stroke of the
    page's layers turned and moved, one deleted, and the image deleted. Then:
      the page parses; it splits and joins back to its bytes; SceneInfo, glyph ranges
      (0x03), text items (0x06), Array blocks (0x0B) and blocks of unknown types are
      byte for byte as they were; every block the page had before is still there with
      the same bytes or as the same item (an edit only deletes items, keeping their
      headers); no id is used twice; every new id is this client's, and newer than
      every id the page had; every layer's items still replay as a sequence; ink drawn
      past 1404 x 1872 is still there;
  - a stroke drawn on the first page and a page added after it are worked out as an
    upload would send them (core.edits.document_files): formatVersion 1 .content is
    upgraded as the app upgrades it, and the live pages read the same (ids, order, PDF
    pages) with the new page in its place; the edited page's "modifed" and .metadata's
    "lastModified" are the edit's time; no other .content field changes except the page
    list's own.
It prints counts only, nothing of the documents' content. Exit status 1 if anything failed.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QSizeF  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtPdf import QPdfDocument  # noqa: E402

from core.edits import (ImageDelete, ImageInsert, ImageTransform, ItemsTransform, PageAdd, StrokeAdd,  # noqa: E402
                        StrokeDelete, StrokeErase, StrokePaste, document_files, replay)
from core.models import Content, FileType, Metadata, make_node  # noqa: E402
from core.pagemap import is_legacy  # noqa: E402
from core.sync import BlobCache, parse_index  # noqa: E402
from ui.pages import pdf_target_rect  # noqa: E402
from viewer import rm_select, rm_stroke, rm_writer as w  # noqa: E402
from viewer.rm_parser import PAGE_HEIGHT, PAGE_WIDTH, parse  # noqa: E402

AUTHOR = bytes.fromhex("5eed0000000040008000000000000001")      # not anyone's device
NOW = 1_900_000_000_000
KEPT = (w.BLOCK_SCENE_INFO, w.BLOCK_GLYPH_ITEM, w.BLOCK_TEXT_ITEM, 0x0B)
KNOWN = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0D, 0x0E, 0x0F}


def load_tree(root: Path, blobs: BlobCache):
    """The cached library's documents, read as SyncClient.load_cached reads them."""
    state = json.loads((root / "state.json").read_text())
    root_hash = (state.get("root") or {}).get("hash", "")
    data = blobs.get(root_hash) if root_hash else None
    if data is None:
        raise SystemExit("no cached root index")
    docs = []
    for entry in parse_index(data).entries:
        index = blobs.get(entry.hash)
        if index is None:
            continue
        files = {e.id: e.hash for e in parse_index(index).entries}
        meta, content = files.get(f"{entry.id}.metadata"), files.get(f"{entry.id}.content")
        if meta is None or content is None or blobs.get(meta) is None or blobs.get(content) is None:
            continue
        metadata = Metadata.parse(blobs.get(meta))
        if metadata.is_collection:
            continue
        docs.append(make_node(entry.id, metadata, Content.parse(blobs.get(content)), hash=entry.hash, files=files))
    return docs


def page_frames(blobs: BlobCache, doc) -> dict[int, tuple[float, float, float, float]]:
    """The scene rectangle (x, y, w, h) of every page of the document's PDF rendition."""
    name = f"{doc.id}.pdf"
    if name not in doc.files or not blobs.has(doc.files[name]):
        return {}
    pdf = QPdfDocument()
    pdf.load(str(blobs.path(doc.files[name])))
    try:
        out = {}
        for n in range(pdf.pageCount()):
            r = pdf_target_rect(QSizeF(pdf.pagePointSize(n)))
            out[n] = (r.x() - PAGE_WIDTH / 2, r.y(), r.width(), r.height())
        return out
    finally:
        pdf.close()


def items_of(page: bytes) -> dict[tuple[int, int], bytes]:
    out = {}
    for b in w.split(page)[1]:
        if b.type in w.ITEM_BLOCKS:
            out[w.decode_item(b.payload).item] = b.payload
    return out


def check_page(doc, page_id: str, base: bytes | None, frame, stats: Counter) -> None:
    x, y, fw, fh = frame
    # From beyond the PDF page's left edge to beyond its right, down to its bottom.
    across = [(x - 40 + (fw + 80) * k / 20, y + fh * 0.2 + (fh * 0.75) * k / 20) for k in range(21)]
    line = rm_stroke.draw(across, rm_stroke.FINELINER_V2)
    key = "0f" * 16
    corners = [(x + fw * 0.6, y + 100, 0.0, 0.0), (x + fw * 0.6 + 200, y + 100, 1.0, 0.0),
               (x + fw * 0.6 + 200, y + 300, 1.0, 1.0), (x + fw * 0.6, y + 300, 0.0, 1.0)]
    moved = [(cx + 50, cy + 70, u, v) for cx, cy, u, v in corners]
    add = StrokeAdd.new(doc.id, page_id, line, NOW)
    erase = StrokeErase.new(doc.id, page_id, rm_stroke.eraser_line([(x + fw * 0.5, y), (x + fw * 0.5, y + fh)]), NOW)
    image = ImageInsert(doc.id, page_id, "00000000-0000-4000-8000-000000000000.png", key, tuple(corners),
                        "0" * 64, 1, NOW, None, "img")
    edits = [add, erase, image, ImageTransform.new(doc.id, page_id, key, moved, NOW)]
    done = replay(base, edits, AUTHOR)
    page = done.page
    # Copied and pasted; then every live stroke of the layers turned and moved; one deleted.
    blocks = w.split(page)[1]
    layers = set(w.layers(blocks))
    live = [s for s in rm_stroke.strokes(page) if s.parent in layers]
    values = [s.line for s in live[-3:]]
    edits.append(StrokePaste.new(doc.id, page_id, rm_select.pasted(values, 1.0, (x + fw / 2, y + fh / 2)), NOW))
    page = replay(base, edits, AUTHOR).page
    live = [s for s in rm_stroke.strokes(page) if s.parent in layers]
    if live:
        edits.append(ItemsTransform.new(doc.id, page_id, [("page", *s.item) for s in live],
                                        (0.0, 1.0, -1.0, 0.0, 30.0, 40.0), NOW))
    page = replay(base, edits, AUTHOR).page
    live = [s for s in rm_stroke.strokes(page) if s.parent in layers]
    if live:
        edits.append(StrokeDelete.new(doc.id, page_id, [("page", *live[0].item)], NOW))
    edits.append(ImageDelete.new(doc.id, page_id, key, NOW))
    page = replay(base, edits, AUTHOR).page
    stats["edits made"] += len(edits)

    header, after = w.split(page)
    assert w.join(header, after) == page, "the page does not join back to its bytes"
    parse(page)
    if base is not None:
        _, before = w.split(base)
        for kind in KEPT:
            assert [b.encode() for b in before if b.type == kind] == [b.encode() for b in after if b.type == kind], \
                f"block {kind:#x} changed"
        unknown = [b.encode() for b in before if b.type not in KNOWN]
        assert unknown == [b.encode() for b in after if b.type not in KNOWN], "an unknown block changed"
        stats["glyph ranges kept"] += sum(b.type == w.BLOCK_GLYPH_ITEM for b in before)
        stats["SceneInfo field 6 kept"] += sum(b.type == w.BLOCK_SCENE_INFO and any(
            i == 6 and v != w.lww(w.ZERO_ID, bytes(32)) for i, _, v in w.read_values(b.payload)) for b in before)
        old_items, new_items = items_of(base), items_of(page)
        for item, payload in old_items.items():
            assert item in new_items or any(i[0] == item[0] and i[1] < item[1] for i in new_items), \
                f"item {item} is gone"
        highest = w.highest_counter(before)
        me = dict((u, i) for u, i in w.decode_authors(after[0].payload))[AUTHOR]
        assert me not in [i for _, i in w.decode_authors(before[0].payload)], "the author index is someone's"
        new = [i for i in new_items if i not in old_items and i[0] == me]
        assert all(c > highest for _, c in new), "a new id is not newer than the page's"
        stats["pages with a .rm"] += 1
    else:
        stats["pages without a .rm"] += 1
    ids = [w.decode_item(b.payload).item for b in after if b.type in w.ITEM_BLOCKS]
    assert len(ids) == len(set(ids)), "an id is used twice"
    for layer in w.layers(after):
        w.sequence(after, layer)
    xs = [p.x for s in parse(page).strokes for p in s.points]
    ys = [p.y for s in parse(page).strokes for p in s.points]
    if xs and (min(xs) < -PAGE_WIDTH / 2 or max(xs) > PAGE_WIDTH / 2 or max(ys) > PAGE_HEIGHT):
        stats["pages with ink beyond 1404 x 1872"] += 1


def check_document(doc, read, stats: Counter) -> None:
    pages = [p for p in doc.content.pages if not p.deleted]
    first = pages[0].id
    edits = [StrokeAdd.new(doc.id, first, rm_stroke.draw([(0, 100), (40, 140)], rm_stroke.FINELINER_V2), NOW),
             PageAdd.new(doc.id, 1, first, "Blank", NOW)]
    files = document_files(doc, edits, read, lambda e: b"", AUTHOR)
    old = json.loads(read(f"{doc.id}.content"))
    new = json.loads(files[f"{doc.id}.content"])
    if is_legacy(old):
        stats["formatVersion 1 documents upgraded"] += 1
        assert new["formatVersion"] == 2 and "cPages" in new and "pages" not in new
    after = Content.parse(new)
    live = [(p.id, p.redirect) for p in after.pages if not p.deleted]
    want = [(p.id, p.redirect) for p in pages]
    assert live[:1] + live[2:] == want and live[1][1] is None and live[1][0] == edits[1].new_id, "the page list changed"
    entry = next(e for e in new["cPages"]["pages"] if e["id"] == first)
    assert entry["modifed"] == str(NOW), "the page's modifed was not set"
    assert json.loads(files[f"{doc.id}.metadata"])["lastModified"] == str(NOW)
    if not is_legacy(old):
        others = {k for k in set(old) | set(new) if old.get(k) != new.get(k)} - {"cPages", "pageCount"}
        assert not others, f".content fields changed: {sorted(others)}"
        assert new["cPages"]["original"] == old["cPages"]["original"]
    assert f"{doc.id}/{first}.rm" in files
    stats["documents saved"] += 1


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".cache" / "remarkable-linux"
    app = QGuiApplication.instance() or QGuiApplication([])     # QtPdf and the eraser's QPainterPathStroker
    blobs = BlobCache(root)
    stats: Counter = Counter()
    failures: Counter = Counter()
    for doc in load_tree(root, blobs):
        if doc.content is None or doc.content.file_type not in (FileType.PDF, FileType.EPUB):
            continue
        stats[f"{doc.content.file_type.value} documents"] += 1
        pages = [p for p in doc.content.pages if not p.deleted]
        if not pages:
            stats["documents without a page list (refused)"] += 1
            continue

        def read(name, doc=doc):
            data = blobs.get(doc.files[name])
            if data is None:
                raise KeyError(f"{name} is not cached")
            return data
        frames = page_frames(blobs, doc)
        for page in pages:
            name = f"{doc.id}/{page.id}.rm"
            try:
                base = read(name) if name in doc.files else None
            except KeyError:
                stats["pages not cached"] += 1
                continue
            if base is not None and not base.startswith(w.V6_HEADER[:33]):
                stats["pages not v6 (not edited here)"] += 1
                continue
            frame = frames.get(page.redirect) if page.redirect is not None else None
            if page.redirect is None:
                stats["pages inserted into a PDF (no PDF page)"] += 1
            else:
                stats["PDF-backed pages" + ("" if frame else " (PDF not cached: 1404 x 1872 frame used)")] += 1
            try:
                check_page(doc, page.id, base, frame or (-PAGE_WIDTH / 2, 0, PAGE_WIDTH, PAGE_HEIGHT), stats)
            except Exception as err:            # counted, and the first few shown without content
                failures[f"page: {type(err).__name__}: {err}"] += 1
                if sum(failures.values()) <= 3:
                    traceback.print_exc()
        try:
            check_document(doc, read, stats)
        except KeyError:
            stats["documents not fully cached"] += 1
        except Exception as err:
            failures[f"document: {type(err).__name__}: {err}"] += 1
            if sum(failures.values()) <= 3:
                traceback.print_exc()
    del app
    for key, value in sorted(stats.items()):
        print(f"{value:8d}  {key}")
    for key, value in sorted(failures.items()):
        print(f"{value:8d}  FAILED {key}")
    print("errors:", sum(failures.values()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
