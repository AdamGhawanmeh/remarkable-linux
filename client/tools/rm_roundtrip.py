#!/usr/bin/env python3
"""The safety net of the page writer (src/viewer/rm_writer.py): run it over real pages.

    client/.venv/bin/python client/tools/rm_roundtrip.py [directory]

For every v6 page under the directory (default: the library cache) it checks that
  - the page splits into blocks and joins back to the same bytes;
  - each block the writer can encode (item headers, authors, images table, image
    values) re-encodes to the bytes it was read from;
  - the writer understands every block (finds all its ids), and the blocks are in the
    order it assumes: authors, migration, stats, scene info, [images table], tree
    moves, [root text], tree nodes, then the items sorted by parent and id;
  - replaying each node's items gives a sequence in which every item is followed by
    the right neighbour it stores (pages with one author);
  - adding an image leaves every existing block untouched, and the result parses with
    one image more and the same strokes;
  - moving, then deleting, each image of the page gives the corners asked for, then
    no such image, and nothing else changes.
It prints counts only, nothing of the pages' content. Exit status 1 if anything failed.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from viewer import rm_parser, rm_writer as w  # noqa: E402

ORDER = re.compile(r"^UCPSI?M+T?N+[GLXJ]*$")
LETTER = {0x09: "U", 0x00: "C", 0x0A: "P", 0x0D: "S", 0x0E: "I", 0x01: "M", 0x07: "T", 0x02: "N",
          0x04: "G", 0x05: "L", 0x08: "X", 0x0F: "J"}
AUTHOR = bytes(range(16))
KEY = bytes(range(16, 32))
CORNERS = [(-100.0, 100.0, 0.0, 0.0), (100.0, 100.0, 1.0, 0.0), (100.0, 300.0, 1.0, 1.0), (-100.0, 300.0, 0.0, 1.0)]


def rights_hold(blocks: list[w.Block], stats: Counter) -> None:
    """Replay as rm_writer.sequence does, checking at each insertion that the item
    lands directly before the right neighbour it stores."""
    by_parent: dict[tuple, list] = {}
    for block in blocks:
        if block.type in w.ITEM_BLOCKS:
            item = w.decode_item(block.payload)
            by_parent.setdefault(item.parent, []).append(item)
    for items in by_parent.values():
        after: dict[tuple, tuple] = {}
        for item in sorted(items, key=lambda i: w.id_key(i.item)):
            stats["right neighbours"] += 1
            if after.get(item.left, w.ZERO_ID) != item.right:
                stats["right neighbours: WRONG"] += 1
            left = item.left
            for n in range(max(item.deleted_length, 1)):
                this = (item.item[0], item.item[1] + n)
                after[this], after[left] = after.get(left, w.ZERO_ID), this
                left = this


def check(data: bytes, stats: Counter, skeleton: dict[bytes, str]) -> None:
    header, blocks = w.split(data)
    stats["pages"] += 1
    stats["blocks"] += len(blocks)
    if w.join(header, blocks) != data:
        stats["join: WRONG"] += 1
    if not ORDER.match("".join(LETTER.get(b.type, "?") for b in blocks)):
        stats["block order: WRONG"] += 1
    keys = []
    for block in blocks:
        if w.BLOCK_VERSIONS.get(block.type) != (block.min_version, block.version):
            stats[f"versions of block {block.type:#x}: WRONG"] += 1
        name = skeleton.get(block.encode())
        if name:
            stats[f"blank page's {name} found in real pages"] += 1
        try:
            w.block_ids(block)
        except (rm_parser.RmParseError, KeyError, IndexError, ValueError) as e:
            stats[f"ids of block {block.type:#x}: NOT UNDERSTOOD ({e})"] += 1
        again = None
        if block.type in w.ITEM_BLOCKS:
            item = w.decode_item(block.payload)
            keys.append((w.id_key(item.parent), w.id_key(item.item)))
            again = item.encode()
            if block.type == w.BLOCK_IMAGE_ITEM and item.value is not None:
                stats["image values"] += 1
                if w.decode_image_value(item.value).encode() != item.value:
                    stats["image value: WRONG"] += 1
        elif block.type == w.BLOCK_AUTHORS:
            authors = w.decode_authors(block.payload)
            stats[f"pages with {len(authors)} authors"] += 1
            again = w.encode_authors(authors)
        elif block.type == w.BLOCK_IMAGES_TABLE:
            entries = w.decode_images_table(block.payload)
            stats["images table entries"] += len(entries)
            again = w.encode_images_table(entries)
        if again is not None:
            stats[f"re-encoded blocks {block.type:#x}"] += 1
            if again != block.payload:
                stats[f"re-encoding block {block.type:#x}: WRONG"] += 1
    if keys != sorted(keys):
        stats["item order: WRONG"] += 1
    single = len(w.decode_authors(blocks[0].payload)) == 1
    if single:
        rights_hold(blocks, stats)

    try:
        before = rm_parser.parse(data)
        edited = w.append_image(data, author=AUTHOR, key=KEY, file_name="x.png", vertices=CORNERS)
    except rm_parser.RmParseError as e:
        stats[f"append refused: {re.sub(r'[0-9]+', 'N', str(e))}"] += 1
        return
    stats["appends"] += 1
    after = rm_parser.parse(edited)
    _, new_blocks = w.split(edited)
    old = Counter(b.encode() for b in blocks[1:] if b.type != w.BLOCK_IMAGES_TABLE)
    new = Counter(b.encode() for b in new_blocks[1:] if b.type != w.BLOCK_IMAGES_TABLE)
    added = list((new - old).elements())
    item = w.decode_item(added[0][8:]) if len(added) == 1 else None
    ok = (not old - new and item is not None and item.item[1] > item.left[1] and item.right == w.ZERO_ID
          and len(after.placed_images) == len(before.placed_images) + 1
          and len(after.strokes) == len(before.strokes)
          and [layer.name for layer in after.layers] == [layer.name for layer in before.layers]
          and ORDER.match("".join(LETTER.get(b.type, "?") for b in new_blocks))
          and w.highest_counter(new_blocks) == item.item[1]
          and w.sequence(new_blocks, item.parent)[-1][0] == item.item)
    if ok:
        keys = [(w.id_key(i.parent), w.id_key(i.item)) for i in (w.decode_item(b.payload) for b in new_blocks
                                                                 if b.type in w.ITEM_BLOCKS)]
        ok = keys == sorted(keys)
    if not ok:
        stats["append: WRONG"] += 1

    # Moving and deleting each image of the page (the tablet's, and the one just added).
    for key in (e.key for b in new_blocks if b.type == w.BLOCK_IMAGES_TABLE for e in w.decode_images_table(b.payload)):
        index = w.live_image(new_blocks, key)
        if index is None:
            continue
        old_value = w.decode_image_value(w.decode_item(new_blocks[index].payload).value)
        corners = [(x + 7, y - 3, u, v) for x, y, u, v in old_value.vertices]
        try:
            moved = w.transform_image(edited, author=AUTHOR, key=key, vertices=corners)
            deleted, gone = w.delete_image(moved, author=AUTHOR, key=key)
        except (rm_parser.RmParseError, ValueError) as e:
            stats[f"move/delete refused: {re.sub(r'[0-9]+', 'N', str(e))}"] += 1
            continue
        stats["moves and deletes"] += 1
        after_move, after_delete = rm_parser.parse(moved), rm_parser.parse(deleted)
        shown = [i for i in after_move.placed_images if i.image_ref == key]
        if (len(shown) != 1 or [tuple(v) for v in shown[0].vertices] != [tuple(map(float, c)) for c in corners]
                or len(after_move.placed_images) != len(after.placed_images)
                or len(after_move.strokes) != len(before.strokes)
                or any(i.image_ref == key for i in after_delete.placed_images)
                or len(after_delete.placed_images) != len(after.placed_images) - 1
                or gone is None or not ORDER.match("".join(LETTER.get(b.type, "?") for b in w.split(deleted)[1]))):
            stats["move/delete: WRONG"] += 1


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".cache" / "remarkable-linux" / "blobs"
    _, blank = w.split(w.blank_page(AUTHOR))
    names = ["authors", "migration", "page stats", "scene info", "tree move", "root node", "layer node", "layer group item"]
    skeleton = {b.encode(): n for b, n in zip(blank, names) if n != "authors"}
    stats: Counter = Counter()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        with path.open("rb") as f:
            if f.read(len(w.V6_HEADER)) != w.V6_HEADER:
                continue
        try:
            check(path.read_bytes(), stats, skeleton)
        except Exception as e:  # noqa: BLE001 - a page that breaks the checker is a finding
            stats[f"page: FAILED ({type(e).__name__}: {re.sub(r'[0-9]+', 'N', str(e))[:80]})"] += 1
    for name, count in sorted(stats.items()):
        print(f"{count:>9}  {name}")
    bad = sum(c for n, c in stats.items() if any(word in n for word in ("WRONG", "FAILED", "NOT UNDERSTOOD")))
    print("ok" if not bad else f"{bad} problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
