"""Builders for synthetic .rm files (v3/v5 streams and v6 tagged blocks).

Kept independent of viewer.rm_parser so tests check the parser against the
format description rather than against itself.
"""

from __future__ import annotations

import struct


def header(version: int) -> bytes:
    return f"reMarkable .lines file, version={version}".ljust(43).encode()


# ----------------------------------------------------------------------- v3/v5

def v5_file(layers: list[list[dict]], version: int = 5) -> bytes:
    """layers -> strokes: {tool, color, width, points: [(x, y, speed, dir, width, pressure)]}"""
    out = [header(version), struct.pack("<i", len(layers))]
    for strokes in layers:
        out.append(struct.pack("<i", len(strokes)))
        for s in strokes:
            out.append(struct.pack("<iiif", s["tool"], s["color"], 0, s["width"]))
            if version == 5:
                out.append(struct.pack("<i", 0))
            out.append(struct.pack("<i", len(s["points"])))
            out += [struct.pack("<6f", *p) for p in s["points"]]
    return b"".join(out)


# -------------------------------------------------------------------------- v6

def varuint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(index: int, kind: int) -> bytes:
    return varuint((index << 4) | kind)


def f_id(index: int, crdt: tuple[int, int]) -> bytes:
    return tag(index, 0xF) + bytes([crdt[0]]) + varuint(crdt[1])


def f_u8(index: int, value: int) -> bytes:
    return tag(index, 0x1) + bytes([value])


def f_u32(index: int, value: int) -> bytes:
    return tag(index, 0x4) + struct.pack("<I", value)


def f_f32(index: int, value: float) -> bytes:
    return tag(index, 0x4) + struct.pack("<f", value)


def f_f64(index: int, value: float) -> bytes:
    return tag(index, 0x8) + struct.pack("<d", value)


def f_sub(index: int, data: bytes) -> bytes:
    return tag(index, 0xC) + struct.pack("<I", len(data)) + data


def block(block_type: int, payload: bytes, version: int = 1, min_version: int = 1) -> bytes:
    return struct.pack("<IBBBB", len(payload), 0, min_version, version, block_type) + payload


TS = (1, 1)
ZERO = (0, 0)
ROOT = (0, 1)


def tree_node(node, label: str | None = None, visible: bool | None = None,
              anchor: tuple | None = None) -> bytes:
    """anchor: (char_id, origin_x[, threshold, type]) for groups anchored to text."""
    payload = f_id(1, node)
    if label is not None:
        text = label.encode()
        payload += f_sub(2, f_id(1, TS) + f_sub(2, varuint(len(text)) + b"\x01" + text))
    if visible is not None:
        payload += f_sub(3, f_id(1, TS) + f_u8(2, int(visible)))
    if anchor is not None:
        char_id, origin_x, threshold, kind = (*anchor, 35.75, 2)[:4]
        payload += (f_sub(7, f_id(1, TS) + f_id(2, char_id))
                    + f_sub(8, f_id(1, TS) + f_u8(2, kind))
                    + f_sub(9, f_id(1, TS) + f_f32(2, threshold))
                    + f_sub(10, f_id(1, TS) + f_f32(2, origin_x)))
    return block(0x02, payload, version=2)


def scene_tree(node, parent=ROOT) -> bytes:
    return block(0x01, f_id(1, node) + f_id(2, ZERO) + f_u8(3, 1) + f_sub(4, f_id(1, parent)))


def _item_header(parent, item, deleted: int = 0) -> bytes:
    return f_id(1, parent) + f_id(2, item) + f_id(3, ZERO) + f_id(4, ZERO) + f_u32(5, deleted)


def group_item(parent, item, child) -> bytes:
    return block(0x04, _item_header(parent, item) + f_sub(6, b"\x02" + f_id(2, child)))


def points_v2(points: list[tuple]) -> bytes:
    """points: (x, y, speed, direction_byte, width, pressure_byte), stored as the tablet does."""
    return b"".join(struct.pack("<ffHHBB", x, y, round(speed * 4), round(width * 4), d, p)
                    for x, y, speed, d, width, p in points)


def points_v1(points: list[tuple]) -> bytes:
    return b"".join(struct.pack("<6f", *p) for p in points)


def line_item(parent, item, *, tool: int, color: int, points: bytes, thickness: float = 2.0,
              starting_length: float = 0.0, argb: int | None = None, deleted: int = 0,
              with_value: bool = True, version: int = 2) -> bytes:
    payload = _item_header(parent, item, deleted)
    if with_value:
        value = (b"\x03" + f_u32(1, tool) + f_u32(2, color) + f_f64(3, thickness)
                 + f_f32(4, starting_length) + f_sub(5, points) + f_id(6, TS))
        if argb is not None:
            value += f_u32(8, argb)
        payload += f_sub(6, value)
    return block(0x05, payload, version=version)


def tombstone(parent, item) -> bytes:
    return block(0x08, _item_header(parent, item))


def v6_file(*blocks: bytes) -> bytes:
    # Leading blocks every real file has: migration info, page info (opaque here).
    return header(6) + block(0x00, f_id(1, TS) + f_u8(2, 1)) + b"".join(blocks) \
        + block(0x0A, f_u32(1, 1) + f_u32(2, 0))


# ------------------------------------------------------------- v6 typed text

def text_item(item, left, right=ZERO, text: str | None = None, deleted: int = 0,
              fmt: int | None = None) -> bytes:
    """One entry of a RootText item list (wrapped in subblock 0)."""
    body = f_id(2, item) + f_id(3, left) + f_id(4, right) + f_u32(5, deleted)
    if text is not None or fmt is not None:
        raw = (text or "").encode()
        body += f_sub(6, varuint(len(raw)) + b"\x01" + raw + (f_u32(2, fmt) if fmt is not None else b""))
    return tag(0, 0xC) + struct.pack("<I", len(body)) + body


def paragraph_format(char_id, style: int, timestamp=TS) -> bytes:
    return bytes([char_id[0]]) + varuint(char_id[1]) + f_id(1, timestamp) + f_sub(2, bytes([17, style]))


def root_text(items: list[bytes], formats: list[bytes], *, x: float = -468.0, y: float = 234.0,
              width: float = 936.0, lww_width: float | None = None) -> bytes:
    contents = (f_sub(1, f_sub(1, varuint(len(items)) + b"".join(items)))
                + f_sub(2, f_sub(1, varuint(len(formats)) + b"".join(formats))))
    payload = f_id(1, ZERO) + f_sub(2, contents) + f_sub(3, struct.pack("<dd", x, y)) + f_f32(4, width)
    if lww_width is not None:
        payload += f_sub(5, f_sub(1, f_id(1, TS) + f_f32(2, lww_width)))
    return block(0x07, payload)


def sample_text_block() -> bytes:
    """'TiXtle' (Title) + bullet 'Hello' with 'Hel' bold.

    Items are deliberately out of file order: 'X' is inserted inside 'Title' and
    listed first; a deleted run and a later-timestamped style override are included.
    """
    return root_text(
        items=[
            text_item((1, 50), left=(1, 2), right=(1, 3), text="X"),
            text_item((1, 1), left=ZERO, text="Title"),
            text_item((1, 6), left=(1, 5), text="\n"),
            text_item((1, 40), left=(1, 6), right=(1, 7), fmt=1),     # bold on
            text_item((1, 7), left=(1, 40), text="Hello"),
            text_item((1, 41), left=(1, 9), right=(1, 10), fmt=2),    # bold off
            text_item((1, 30), left=(1, 11), deleted=2),
        ],
        formats=[
            paragraph_format(ZERO, 0),                       # Title
            paragraph_format((1, 6), 2, timestamp=(1, 1)),   # Paragraph ...
            paragraph_format((1, 6), 3, timestamp=(1, 9)),   # ... overridden: Bulletpoint
        ],
        lww_width=1000.0)


def image_item(parent, item, value: bytes = b"", deleted: int = 0, left=ZERO) -> bytes:
    payload = f_id(1, parent) + f_id(2, item) + f_id(3, left) + f_id(4, ZERO) + f_u32(5, deleted)
    if value:
        payload += f_sub(6, value)
    return block(0x0F, payload, version=2, min_version=2)


def images_table(entries: bytes = b"\x00") -> bytes:
    return block(0x0E, f_sub(1, entries), version=3, min_version=3)


def lww_string(timestamp, text: str) -> bytes:
    data = text.encode()
    return f_id(1, timestamp) + f_sub(2, varuint(len(data)) + b"\x01" + data)


def image_value(ref: bytes, vertices, indices=(0, 1, 2, 2, 3, 0), *, timestamp=(1, 18),
                bounds_timestamp=(1, 17), move_id=None) -> bytes:
    """A CrdtImageItem value as the tablet writes one: type 7, {1: LWW imageRef},
    {2: boundsTimestamp}, {3: vertices x y u v}, {4: indices}, {5: moveId}."""
    floats = [c for vertex in vertices for c in vertex]
    value = (b"\x07" + f_sub(1, f_id(1, timestamp) + f_sub(2, ref)) + f_id(2, bounds_timestamp)
             + f_sub(3, varuint(len(floats)) + struct.pack(f"<{len(floats)}f", *floats))
             + f_sub(4, varuint(len(indices)) + struct.pack(f"<{len(indices)}I", *indices)))
    if move_id is not None:
        value += f_id(5, move_id)
    return value


def image_entries(files: dict, timestamp=(1, 15)) -> bytes:
    """SceneImagesTable entries: per image its 16-byte key, {1: LWW file name},
    {2: LWW flag (0:0, {1: 0}) as the tablet writes it}."""
    out = varuint(len(files))
    for key, name in files.items():
        out += f_sub(0, key + f_sub(1, lww_string(timestamp, name)) + f_sub(2, f_id(1, ZERO) + f_sub(2, f_u8(1, 0))))
    return out
