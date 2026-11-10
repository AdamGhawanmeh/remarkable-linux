"""Writing .rm v6 pages: the safe base the editing features build on.

An edit never rewrites what it does not change. split() cuts a page into its blocks,
each kept as the exact bytes it was read from; join() puts them back, so an untouched
page comes out byte for byte as it went in. An edit replaces or adds only the blocks
it must, and those are encoded by the functions here, whose output for every block
type is checked against real pages (tools/rm_roundtrip.py: re-encoding each real
block of a type must give back its bytes) before an edit may use them.

The format (see rm_parser): after a 43-byte header, blocks of
    u32 payload length, u8 0, u8 min_version, u8 version, u8 type, payload
where the payload is a run of tagged values: tag = varuint(index << 4 | kind), kinds
1 = u8, 4 = u32 or f32, 8 = f64, 0xC = u32-length sub-block, 0xF = CrdtId
(u8 author, varuint counter).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .rm_parser import (HEADER_LENGTH, TAG_BYTE1, TAG_BYTE4, TAG_BYTE8, TAG_ID, TAG_LENGTH4, RmParseError)

V6_HEADER = b"reMarkable .lines file, version=6".ljust(HEADER_LENGTH)
BLOCK_HEADER = struct.Struct("<IBBBB")


@dataclass
class Block:
    """One block of a page. `payload` is exactly what was read (or what an encoder
    made); `offset` is where it was read from (None for a new block)."""

    type: int
    min_version: int
    version: int
    payload: bytes
    offset: int | None = None

    def encode(self) -> bytes:
        return BLOCK_HEADER.pack(len(self.payload), 0, self.min_version, self.version, self.type) + self.payload


def split(data: bytes) -> tuple[bytes, list[Block]]:
    """(header, blocks) of a v6 page; join() of the result gives `data` back."""
    if not data.startswith(V6_HEADER):
        raise RmParseError("not a v6 page")
    blocks, pos = [], HEADER_LENGTH
    while pos < len(data):
        if pos + BLOCK_HEADER.size > len(data):
            raise RmParseError(f"truncated block header at offset {pos}")
        length, reserved, min_version, version, block_type = BLOCK_HEADER.unpack_from(data, pos)
        if reserved != 0:
            raise RmParseError(f"reserved byte {reserved} at offset {pos + 4}")
        end = pos + BLOCK_HEADER.size + length
        if end > len(data):
            raise RmParseError(f"block at offset {pos} runs past the end of the page")
        blocks.append(Block(block_type, min_version, version, data[pos + BLOCK_HEADER.size:end], pos))
        pos = end
    return data[:HEADER_LENGTH], blocks


def join(header: bytes, blocks: list[Block]) -> bytes:
    return header + b"".join(block.encode() for block in blocks)


# ------------------------------------------------------------------ tagged values

def varuint(n: int) -> bytes:
    if n < 0:
        raise ValueError(f"varuint of negative {n}")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(index: int, kind: int) -> bytes:
    return varuint(index << 4 | kind)


def crdt_id(value: tuple[int, int]) -> bytes:
    author, counter = value
    if not 0 <= author <= 0xFF:
        raise ValueError(f"author {author} does not fit a byte")
    return bytes([author]) + varuint(counter)


def f_id(index: int, value: tuple[int, int]) -> bytes:
    return tag(index, TAG_ID) + crdt_id(value)


def f_u8(index: int, value: int) -> bytes:
    return tag(index, TAG_BYTE1) + struct.pack("<B", value)


def f_u32(index: int, value: int) -> bytes:
    return tag(index, TAG_BYTE4) + struct.pack("<I", value)


def f_f32(index: int, value: float) -> bytes:
    return tag(index, TAG_BYTE4) + struct.pack("<f", value)


def f_f64(index: int, value: float) -> bytes:
    return tag(index, TAG_BYTE8) + struct.pack("<d", value)


def f_sub(index: int, data: bytes) -> bytes:
    return tag(index, TAG_LENGTH4) + struct.pack("<I", len(data)) + data


def lww(timestamp: tuple[int, int], value: bytes) -> bytes:
    """A last-write-wins value: {1: timestamp, 2: value sub-block}."""
    return f_id(1, timestamp) + f_sub(2, value)


def string_value(text: str) -> bytes:
    """A string as stored in sub-blocks: varuint byte length, u8 is_ascii, UTF-8 bytes."""
    data = text.encode("utf-8")
    return varuint(len(data)) + bytes([int(text.isascii())]) + data


# ------------------------------------------------------------------ scene items

@dataclass
class ItemHeader:
    """The fields every CRDT sequence item block starts with (group 0x04, line 0x05,
    tombstone 0x08, image 0x0F): its parent node, its own id, its left and right
    neighbours in the parent's sequence, how much of it is deleted, and its value."""

    parent: tuple[int, int]
    item: tuple[int, int]
    left: tuple[int, int]
    right: tuple[int, int]
    deleted_length: int = 0
    value: bytes | None = None

    def encode(self) -> bytes:
        out = (f_id(1, self.parent) + f_id(2, self.item) + f_id(3, self.left) + f_id(4, self.right)
               + f_u32(5, self.deleted_length))
        return out if self.value is None else out + f_sub(6, self.value)


def decode_item(payload: bytes) -> ItemHeader:
    from .rm_parser import _field, _read_fields
    f = _read_fields(payload)
    if set(f) - {1, 2, 3, 4, 5, 6}:
        raise RmParseError(f"item has unexpected fields {sorted(set(f) - {1, 2, 3, 4, 5, 6})}")
    value = _field(f, 6, TAG_LENGTH4, "item").raw if 6 in f else None
    return ItemHeader(_field(f, 1, TAG_ID, "item").raw, _field(f, 2, TAG_ID, "item").raw,
                      _field(f, 3, TAG_ID, "item").raw, _field(f, 4, TAG_ID, "item").raw,
                      _field(f, 5, TAG_BYTE4, "item").as_int(), value)


# ------------------------------------------------------------------ tagged values, read in order

BLOCK_MIGRATION, BLOCK_TREE_MOVE, BLOCK_TREE_NODE = 0x00, 0x01, 0x02
BLOCK_GROUP_ITEM, BLOCK_LINE_ITEM, BLOCK_ROOT_TEXT, BLOCK_TOMBSTONE = 0x04, 0x05, 0x07, 0x08
BLOCK_AUTHORS, BLOCK_PAGE_STATS, BLOCK_SCENE_INFO = 0x09, 0x0A, 0x0D
BLOCK_IMAGES_TABLE, BLOCK_IMAGE_ITEM = 0x0E, 0x0F
ITEM_BLOCKS = (BLOCK_GROUP_ITEM, BLOCK_LINE_ITEM, BLOCK_TOMBSTONE, BLOCK_IMAGE_ITEM)
# (min_version, version) of every block type, the same in every real page.
BLOCK_VERSIONS = {BLOCK_MIGRATION: (1, 1), BLOCK_TREE_MOVE: (1, 1), BLOCK_TREE_NODE: (1, 2),
                  BLOCK_GROUP_ITEM: (1, 1), BLOCK_LINE_ITEM: (2, 2), BLOCK_ROOT_TEXT: (1, 1),
                  BLOCK_TOMBSTONE: (1, 1), BLOCK_AUTHORS: (1, 1), BLOCK_PAGE_STATS: (0, 1),
                  BLOCK_SCENE_INFO: (0, 1), BLOCK_IMAGES_TABLE: (3, 3), BLOCK_IMAGE_ITEM: (2, 2)}
ZERO_ID = (0, 0)
ROOT_NODE, LAYER_1 = (0, 1), (0, 11)


def new_block(block_type: int, payload: bytes) -> Block:
    min_version, version = BLOCK_VERSIONS[block_type]
    return Block(block_type, min_version, version, payload)


def read_values(data: bytes) -> list[tuple[int, int, object]]:
    """A run of tagged values as (index, kind, value), in order, duplicates kept
    (rm_parser._read_fields keeps only the last of an index, which loses bytes)."""
    from .rm_parser import _Reader
    r, out = _Reader(data), []
    while r.pos < len(data):
        t = r.varuint()
        index, kind = t >> 4, t & 0xF
        if kind == TAG_BYTE1:
            value: object = r.u8()
        elif kind == TAG_BYTE4:
            value = r.take(4)               # u32 or f32: the bytes say which
        elif kind == TAG_BYTE8:
            value = r.take(8)
        elif kind == TAG_LENGTH4:
            value = r.take(r.unpack("<I")[0])
        elif kind == TAG_ID:
            value = r.crdt_id()
        else:
            raise RmParseError(f"unknown tag type {kind:#x}")
        out.append((index, kind, value))
    return out


def write_values(values: list[tuple[int, int, object]]) -> bytes:
    out = bytearray()
    for index, kind, value in values:
        out += tag(index, kind)
        if kind == TAG_BYTE1:
            out.append(value)
        elif kind == TAG_LENGTH4:
            out += struct.pack("<I", len(value)) + value
        elif kind == TAG_ID:
            out += crdt_id(value)
        else:
            out += value
    return bytes(out)


# ------------------------------------------------------------------ authors (Uuid2Index, block 0x09)

def author_uuid(device_id: str) -> bytes:
    """The author UUID of a device, as reMarkable.exe derives its own (Library::setDeviceId):
    the version 5 UUID of the device id in the X.500 namespace. Every author UUID in real
    pages is a version 5 UUID stored in this byte order."""
    import uuid
    if not device_id:
        raise ValueError("no device id")
    return uuid.uuid5(uuid.NAMESPACE_X500, device_id).bytes


def decode_authors(payload: bytes) -> list[tuple[bytes, int]]:
    """[(16-byte author UUID, author index)], in the order stored. The index is the
    author byte of the CrdtIds that author made; 0 is nobody (the built-in ids)."""
    from .rm_parser import _Reader
    r, out = _Reader(payload), []
    for _ in range(r.varuint()):
        if r.varuint() != TAG_LENGTH4:
            raise RmParseError("author entry is not a sub-block")
        entry = _Reader(r.take(r.unpack("<I")[0]))
        if entry.varuint() != 16:
            raise RmParseError("author UUID is not 16 bytes")
        uuid = entry.take(16)
        (index,) = entry.unpack("<H")
        if entry.pos != len(entry.data):
            raise RmParseError("author entry has trailing bytes")
        out.append((uuid, index))
    if r.pos != len(payload):
        raise RmParseError("authors block has trailing bytes")
    return out


def encode_authors(authors: list[tuple[bytes, int]]) -> bytes:
    out = varuint(len(authors))
    for uuid, index in authors:
        if len(uuid) != 16:
            raise ValueError("author UUID is not 16 bytes")
        out += f_sub(0, varuint(16) + uuid + struct.pack("<H", index))
    return out


# ------------------------------------------------------------------ images table (block 0x0E)

@dataclass
class ImageEntry:
    """One image of the page: the 16-byte key image items refer to, the name of its file
    in <document>/<page>/, the timestamp of that name, and the entry's second value kept
    as stored (the tablet writes LWW (0:0) {1: u8 0})."""

    key: bytes
    file_name: str
    timestamp: tuple[int, int]
    rest: bytes = b""

    def encode(self) -> bytes:
        return self.key + f_sub(1, lww(self.timestamp, string_value(self.file_name))) + self.rest


ENTRY_REST = f_sub(2, lww(ZERO_ID, f_u8(1, 0)))


def decode_images_table(payload: bytes) -> list[ImageEntry]:
    from .rm_parser import _Reader
    values = read_values(payload)
    if [(i, k) for i, k, _ in values] != [(1, TAG_LENGTH4)]:
        raise RmParseError("images table is not one sub-block")
    r, out = _Reader(values[0][2]), []
    for _ in range(r.varuint()):
        if r.varuint() != TAG_LENGTH4:
            raise RmParseError("images table entry is not a sub-block")
        entry = r.take(r.unpack("<I")[0])
        key, fields = entry[:16], read_values(entry[16:])
        if len(key) != 16 or not fields or fields[0][:2] != (1, TAG_LENGTH4):
            raise RmParseError("images table entry has no file name")
        name = read_values(fields[0][2])
        if [(i, k) for i, k, _ in name] != [(1, TAG_ID), (2, TAG_LENGTH4)]:
            raise RmParseError("image file name is not a last-write-wins value")
        s = _Reader(name[1][2])
        text = s.take(s.varuint() + 1)[1:].decode("utf-8")
        out.append(ImageEntry(key, text, name[0][2], write_values(fields[1:])))
    if r.pos != len(values[0][2]):
        raise RmParseError("images table has trailing bytes")
    return out


def encode_images_table(entries: list[ImageEntry]) -> bytes:
    return f_sub(1, varuint(len(entries)) + b"".join(f_sub(0, e.encode()) for e in entries))


# ------------------------------------------------------------------ image item value

IMAGE_INDICES = (0, 1, 2, 2, 3, 0)          # two triangles over the four corners
IMAGE_UV = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


@dataclass
class ImageValue:
    """The value of a CrdtImageItem: which image (a key of the images table, with its
    timestamp), where (four corners x y u v, clockwise from the top left, in scene
    coordinates, x 0 at the centre of the page), and the ids the app keeps with it."""

    ref: bytes
    ref_timestamp: tuple[int, int]
    bounds_timestamp: tuple[int, int]
    vertices: list[tuple[float, float, float, float]]
    indices: tuple[int, ...] = IMAGE_INDICES
    move_id: tuple[int, int] | None = None

    def encode(self) -> bytes:
        floats = [c for vertex in self.vertices for c in vertex]
        out = (b"\x07" + f_sub(1, lww(self.ref_timestamp, self.ref)) + f_id(2, self.bounds_timestamp)
               + f_sub(3, varuint(len(floats)) + struct.pack(f"<{len(floats)}f", *floats))
               + f_sub(4, varuint(len(self.indices)) + struct.pack(f"<{len(self.indices)}I", *self.indices)))
        return out if self.move_id is None else out + f_id(5, self.move_id)


def decode_image_value(value: bytes) -> ImageValue:
    from .rm_parser import _Reader
    if value[:1] != b"\x07":
        raise RmParseError("not an image item value")
    v = read_values(value[1:])
    shape = [(i, k) for i, k, _ in v]
    if shape[:4] != [(1, TAG_LENGTH4), (2, TAG_ID), (3, TAG_LENGTH4), (4, TAG_LENGTH4)] or shape[4:] not in ([], [(5, TAG_ID)]):
        raise RmParseError(f"image item value has fields {shape}")
    ref = read_values(v[0][2])
    if [(i, k) for i, k, _ in ref] != [(1, TAG_ID), (2, TAG_LENGTH4)]:
        raise RmParseError("image ref is not a last-write-wins value")
    r = _Reader(v[2][2])
    n = r.varuint()
    floats = r.unpack(f"<{n}f")
    r = _Reader(v[3][2])
    indices = r.unpack(f"<{r.varuint()}I")
    return ImageValue(ref[1][2], ref[0][2], v[1][2], [tuple(floats[i:i + 4]) for i in range(0, n, 4)],
                      tuple(indices), v[4][2] if len(v) > 4 else None)


# ------------------------------------------------------------------ every CrdtId of a page

RAW = None      # a sub-block that holds no ids (a string, points, numbers)
LWW = {2: RAW}  # a last-write-wins value: {1: timestamp, 2: the value, an id or a plain value}
_SCENE_INFO = {1: LWW, 2: LWW, 3: LWW, 4: LWW, 5: RAW, 6: LWW, 7: RAW, 8: LWW, 9: LWW}
_TREE_NODE = {2: LWW, 3: LWW, 7: LWW, 8: LWW, 9: LWW, 10: LWW}
_ITEM_VALUES = {0x02: {}, 0x03: {5: RAW}, 0x07: {1: LWW, 3: RAW, 4: RAW}}


def _ids(data: bytes, below: dict) -> list[tuple[int, int]]:
    """The ids among a run of tagged values. `below` says what each sub-block is; one
    it does not name is not understood, and the page is then not ours to edit."""
    out = []
    for index, kind, value in read_values(data):
        if kind == TAG_ID:
            out.append(value)
        elif kind == TAG_LENGTH4:
            if index not in below:
                raise RmParseError(f"sub-block {index} is not understood")
            if below[index] is not RAW:
                out += _ids(value, below[index])
    return out


def _root_text_ids(payload: bytes) -> list[tuple[int, int]]:
    from .rm_parser import _Reader
    out = []
    for index, kind, value in read_values(payload):
        if kind == TAG_ID:
            out.append(value)
        elif kind != TAG_LENGTH4:
            continue
        elif index == 2:
            parts = {i: v for i, k, v in read_values(value) if k == TAG_LENGTH4}
            items, formats = (read_values(parts[i]) for i in (1, 2))
            if any([(i, k) for i, k, _ in part] != [(1, TAG_LENGTH4)] for part in (items, formats)):
                raise RmParseError("root text contents are not understood")
            r = _Reader(items[0][2])
            for _ in range(r.varuint()):
                if r.varuint() != TAG_LENGTH4:
                    raise RmParseError("text item is not a sub-block")
                run, first = 1, None
                for i, k, v in read_values(r.take(r.unpack("<I")[0])):
                    if k == TAG_ID:
                        out.append(v)
                        first = v if i == 2 else first
                    elif i == 5 and k == TAG_BYTE4:
                        run = max(run, struct.unpack("<I", v)[0])
                    elif i == 6 and k == TAG_LENGTH4:
                        s = _Reader(v)
                        run = max(run, s.varuint())     # bytes: at least as many as characters
                    elif k == TAG_LENGTH4:
                        raise RmParseError(f"text item sub-block {i} is not understood")
                if first is None:
                    raise RmParseError("text item has no id")
                out.append((first[0], first[1] + run - 1))
            r = _Reader(formats[0][2])
            for _ in range(r.varuint()):
                out.append(r.crdt_id())
                if r.varuint() != tag(1, TAG_ID)[0]:
                    raise RmParseError("paragraph format has no timestamp")
                out.append(r.crdt_id())
                if r.varuint() != tag(2, TAG_LENGTH4)[0]:
                    raise RmParseError("paragraph format has no value")
                r.take(r.unpack("<I")[0])
        elif index == 5:
            out += _ids(value, {1: LWW})
        elif index != 3:
            raise RmParseError(f"root text sub-block {index} is not understood")
    return out


def block_ids(block: Block) -> list[tuple[int, int]]:
    """Every CrdtId a block holds (for a run of deleted ids, also its last). Raises
    RmParseError for a block it does not fully understand."""
    t, payload = block.type, block.payload
    if t in (BLOCK_AUTHORS, BLOCK_PAGE_STATS):
        return []
    if t == BLOCK_MIGRATION:
        return _ids(payload, {})
    if t == BLOCK_SCENE_INFO:
        return _ids(payload, _SCENE_INFO)
    if t == BLOCK_TREE_MOVE:
        return _ids(payload, {4: LWW})
    if t == BLOCK_TREE_NODE:
        return _ids(payload, _TREE_NODE)
    if t == BLOCK_ROOT_TEXT:
        return _root_text_ids(payload)
    if t == BLOCK_IMAGES_TABLE:
        out = []
        for entry in decode_images_table(payload):
            out += [entry.timestamp] + _ids(entry.rest, {2: LWW})
        return out
    if t in ITEM_BLOCKS:
        item = decode_item(payload)
        out = [item.parent, item.item, item.left, item.right]
        if item.deleted_length > 1:
            out.append((item.item[0], item.item[1] + item.deleted_length - 1))
        if item.value is not None:
            kind = item.value[0] if item.value else None
            if kind not in _ITEM_VALUES:
                raise RmParseError(f"item value of type {kind} is not understood")
            out += _ids(item.value[1:], _ITEM_VALUES[kind])
        return out
    raise RmParseError(f"block type {t:#x} is not understood")


def highest_counter(blocks: list[Block]) -> int:
    return max((counter for block in blocks for _, counter in block_ids(block)), default=0)


# ------------------------------------------------------------------ the order of a node's items

def id_key(value: tuple[int, int]) -> tuple[int, int]:
    """CrdtIds order by counter, then author: the order blocks are stored in."""
    return value[1], value[0]


def sequence(blocks: list[Block], parent: tuple[int, int]) -> list[tuple[tuple[int, int], Block]]:
    """The items of `parent` in their order on the page (bottom to top), as (id, block),
    a run of deleted ids giving one entry per id. Items are stored in id order, each
    naming the neighbour it was put after: replaying them in that order gives the
    sequence (it reproduces every stored right neighbour of the single-author real pages)."""
    after: dict[tuple, tuple] = {}          # id -> the id that follows it; ZERO_ID is the start
    owner: dict[tuple, Block] = {}
    items = [(decode_item(b.payload), b) for b in blocks if b.type in ITEM_BLOCKS]
    for item, block in sorted((x for x in items if x[0].parent == parent), key=lambda x: id_key(x[0].item)):
        left = item.left
        for n in range(max(item.deleted_length, 1)):
            this = (item.item[0], item.item[1] + n)
            if this in owner or (left != ZERO_ID and left not in owner):
                raise RmParseError(f"item {this} of node {parent} does not fit its sequence")
            after[this], after[left] = after.get(left), this
            owner[this] = block
            left = this
    out, this = [], after.get(ZERO_ID)
    while this is not None:
        out.append((this, owner[this]))
        this = after.get(this)
    return out


def layers(blocks: list[Block]) -> list[tuple[int, int]]:
    """The layer nodes of the page, bottom to top: the live group items of the root."""
    out = []
    for _, block in sequence(blocks, ROOT_NODE):
        item = decode_item(block.payload)
        if block.type == BLOCK_GROUP_ITEM and item.value is not None:
            child = [v for i, k, v in read_values(item.value[1:]) if (i, k) == (2, TAG_ID)]
            if child and child[0] not in out:
                out.append(child[0])
    return out


def current_layer(blocks: list[Block]) -> tuple[int, int]:
    """The layer the page was last drawn on (SceneInfo currentLayer) when that is still
    a layer; else Layer 1 (currentLayer is not set until another layer is chosen), else
    the top one."""
    found = layers(blocks)
    if not found:
        raise RmParseError("the page has no layer")
    for block in blocks:
        if block.type == BLOCK_SCENE_INFO:
            for index, kind, value in read_values(block.payload):
                if (index, kind) == (1, TAG_LENGTH4):
                    chosen = [v for i, k, v in read_values(value) if (i, k) == (2, TAG_ID)]
                    if chosen and chosen[0] in found:
                        return chosen[0]
    return LAYER_1 if LAYER_1 in found else found[-1]


# ------------------------------------------------------------------ a new page

PAPER = (1404, 1872)


def blank_page(author: bytes) -> bytes:
    """A page with one empty layer, as the tablet stores a page nobody has drawn on:
    the built-in root node (0:1), Layer 1 (0:11, named at 0:12), the group item (0:13)
    that puts it under the root, and the tablet's current SceneInfo (its preferredLayout
    is stamped 0:14). `author` gets index 1, so its first id is 1:15."""
    visible = f_sub(3, f_id(1, ZERO_ID) + f_u8(2, 1))
    blocks = [
        new_block(BLOCK_AUTHORS, encode_authors([(author, 1)])),
        new_block(BLOCK_MIGRATION, f_id(1, (1, 1)) + f_u8(2, 0) + f_u8(3, 0)),      # 2: not a tablet
        new_block(BLOCK_PAGE_STATS, b"".join(f_u32(i, v) for i, v in enumerate((1, 0, 0, 0, 0), 1))),
        new_block(BLOCK_SCENE_INFO, f_sub(1, f_id(1, ZERO_ID) + f_id(2, ZERO_ID))      # currentLayer: not set
                  + f_sub(2, f_id(1, ZERO_ID) + f_u8(2, 1)) + f_sub(3, f_id(1, ZERO_ID) + f_u8(2, 1))
                  + f_sub(5, struct.pack("<II", *PAPER)) + f_sub(6, lww(ZERO_ID, bytes(32)))
                  + f_sub(7, struct.pack("<dd", *PAPER)) + f_sub(8, lww(ZERO_ID, struct.pack("<dd", *PAPER)))
                  + f_sub(9, lww((0, 14), b"\x01"))),
        new_block(BLOCK_TREE_MOVE, f_id(1, LAYER_1) + f_id(2, ZERO_ID) + f_u8(3, 1) + f_sub(4, f_id(1, ROOT_NODE))),
        new_block(BLOCK_TREE_NODE, f_id(1, ROOT_NODE) + f_sub(2, lww(ZERO_ID, string_value(""))) + visible),
        new_block(BLOCK_TREE_NODE, f_id(1, LAYER_1) + f_sub(2, lww((0, 12), string_value("Layer 1"))) + visible),
        new_block(BLOCK_GROUP_ITEM, ItemHeader(ROOT_NODE, (0, 13), ZERO_ID, ZERO_ID, 0, b"\x02" + f_id(2, LAYER_1)).encode()),
    ]
    return join(V6_HEADER, blocks)


# ------------------------------------------------------------------ editing

def _author_index(blocks: list[Block], author: bytes) -> int:
    """The index of `author` on this page, added to the authors block if it is new: an
    author new to a page takes the next index, as the app's Scene::setAuthor does. Ids
    made with an index nobody else has cannot collide with anyone's."""
    if not blocks or blocks[0].type != BLOCK_AUTHORS:
        raise RmParseError("the page does not start with its authors")
    authors = decode_authors(blocks[0].payload)
    if encode_authors(authors) != blocks[0].payload:
        raise RmParseError("the authors block is not understood")
    for uuid, index in authors:
        if uuid == author:
            return index
    index = max((i for _, i in authors), default=0) + 1
    if index > 0xFF:
        raise RmParseError("the page has no free author index")
    blocks[0] = new_block(BLOCK_AUTHORS, encode_authors(authors + [(author, index)]))
    return index


def _insert_item(blocks: list[Block], block: Block) -> None:
    """Put an item block where the app stores it: after the other blocks, the items
    sorted by parent then id."""
    first = next((n for n, b in enumerate(blocks) if b.type in ITEM_BLOCKS), len(blocks))
    if any(b.type not in ITEM_BLOCKS for b in blocks[first:]):
        raise RmParseError("the page's items are not at its end")

    def key(b: Block) -> tuple:
        item = decode_item(b.payload)
        return id_key(item.parent), id_key(item.item)

    at = next((n for n in range(first, len(blocks)) if key(blocks[n]) > key(block)), len(blocks))
    blocks.insert(at, block)


def append_image(page: bytes | None, *, author: bytes, key: bytes, file_name: str,
                 vertices: list[tuple[float, float, float, float]],
                 layer: tuple[int, int] | None = None) -> bytes:
    """`page` (None: a page with no .rm yet) with one more image, on top of `layer`
    (default: the current layer). Every block of the page is kept as it is, byte for
    byte; added are an entry of the images table, one image item, and `author` in the
    authors block if it is not there.

    The new ids are taken from the page's clock in the order reMarkable.exe takes them
    when it inserts an image file: the file name's timestamp (storing the image,
    0x14094c420), the item's boundsTimestamp and imageRef timestamp (creating the item,
    0x14043c3c0), then the item's id (adding it to the layer after its last item,
    0x1408c2bc0). A new item has no moveId: the app writes that field only for an item
    that was moved (0x1409a52d0)."""
    if len(key) != 16:
        raise ValueError("an image needs a 16-byte key")
    check_corners(vertices)
    header, blocks = split(blank_page(author) if page is None else page)
    before, original = list(blocks), [b.payload for b in blocks]
    me = _author_index(blocks, author)
    clock = highest_counter(blocks)
    name_at, bounds_at, ref_at, item_id = ((me, clock + n) for n in (1, 2, 3, 4))
    layer = current_layer(blocks) if layer is None else layer
    if layer not in layers(blocks):
        raise RmParseError(f"{layer} is not a layer of the page")
    order = sequence(blocks, layer)
    left = order[-1][0] if order else ZERO_ID

    entry = ImageEntry(key, file_name, name_at, ENTRY_REST)
    table = next((n for n, b in enumerate(blocks) if b.type == BLOCK_IMAGES_TABLE), None)
    if table is None:
        info = [n for n, b in enumerate(blocks) if b.type == BLOCK_SCENE_INFO]
        if len(info) != 1:
            raise RmParseError("the page does not have one SceneInfo")
        blocks.insert(info[0] + 1, new_block(BLOCK_IMAGES_TABLE, encode_images_table([entry])))
    else:
        entries = decode_images_table(blocks[table].payload)
        if encode_images_table(entries) != blocks[table].payload:
            raise RmParseError("the images table is not understood")
        if any(e.key == key for e in entries):
            raise ValueError("the page already has an image with this key")
        blocks[table] = new_block(BLOCK_IMAGES_TABLE, encode_images_table(entries + [entry]))

    value = ImageValue(key, ref_at, bounds_at, [tuple(v) for v in vertices], IMAGE_INDICES)
    _insert_item(blocks, new_block(BLOCK_IMAGE_ITEM, ItemHeader(layer, item_id, left, ZERO_ID, 0, value.encode()).encode()))

    replaced = {0} | ({table} if table is not None else set())
    _check_untouched(before, original, blocks, replaced)
    return join(header, blocks)


def _check_untouched(before: list[Block], original: list[bytes], blocks: list[Block], replaced: set[int]) -> None:
    """Everything of the page an edit did not mean to replace (`replaced`: indices into
    `before`) must still be there, the same objects with the same bytes, in order."""
    expected = [b for n, b in enumerate(before) if n not in replaced]
    wanted = {id(b) for b in expected}
    kept = [b for b in blocks if id(b) in wanted]
    payloads = [p for n, p in enumerate(original) if n not in replaced]
    if len(kept) != len(expected) or any(k is not e or k.payload != p for k, e, p in zip(kept, expected, payloads)):
        raise RmParseError("the edit changed blocks it must not")


def check_corners(vertices) -> None:
    """An image item's corners must be what reMarkable.exe accepts (SceneImageItem's
    geometry check: "Expected 4 points and 2 triangles", texture coordinates (0, 0)
    (1, 0) (1, 1) (0, 1), opposing sides equal and parallel, right angles): a
    rectangle, possibly turned or mirrored, of a usable size. ValueError otherwise."""
    if len(vertices) != 4 or any(len(v) != 4 for v in vertices):
        raise ValueError("an image needs four corners of x, y, u, v")
    if [tuple(float(c) for c in v[2:]) for v in vertices] != [tuple(map(float, uv)) for uv in IMAGE_UV]:
        raise ValueError("an image's corners must have the texture coordinates (0,0) (1,0) (1,1) (0,1)")
    (x0, y0, *_), (x1, y1, *_), (x2, y2, *_), (x3, y3, *_) = vertices
    top, right = (x1 - x0, y1 - y0), (x2 - x1, y2 - y1)
    bottom, left = (x3 - x2, y3 - y2), (x0 - x3, y0 - y3)
    width, height = (top[0] ** 2 + top[1] ** 2) ** 0.5, (right[0] ** 2 + right[1] ** 2) ** 0.5
    if not (width >= 1 and height >= 1 and max(width, height) <= 100_000):
        raise ValueError("an image must be at least one unit wide and high")
    tolerance = 1e-3 * max(width, height)
    if (abs(top[0] + bottom[0]) > tolerance or abs(top[1] + bottom[1]) > tolerance
            or abs(right[0] + left[0]) > tolerance or abs(right[1] + left[1]) > tolerance):
        raise ValueError("an image's opposing sides must be equal and parallel")
    if abs(top[0] * right[0] + top[1] * right[1]) > tolerance * max(width, height):
        raise ValueError("an image's corners must be right angles")


def live_image(blocks: list[Block], key: bytes) -> int | None:
    """The index of the one live image item showing the image `key`; None when there is
    none (deleted, or never there). RmParseError when several show it: which one an
    edit meant cannot be told."""
    found = [n for n, b in enumerate(blocks) if b.type == BLOCK_IMAGE_ITEM
             and (item := decode_item(b.payload)).value is not None and decode_image_value(item.value).ref == key]
    if len(found) > 1:
        raise RmParseError("several items show this image")
    return found[0] if found else None


def _image_page(page: bytes, author: bytes, key: bytes):
    header, blocks = split(page)
    before, original = list(blocks), [b.payload for b in blocks]
    index = live_image(blocks, key)
    if index is None:
        return None
    _author_index(blocks, author)
    item = decode_item(blocks[index].payload)
    if item.encode() != blocks[index].payload:
        raise RmParseError("the image item is not understood")
    value = decode_image_value(item.value)
    if value.encode() != item.value:
        raise RmParseError("the image item's value is not understood")
    return header, blocks, before, original, index, item, value


def _deleted(item: ItemHeader) -> Block:
    """An item as the app leaves it when it is deleted or moved away: the same block
    type, place and neighbours, no value, one deleted id (CRDT sequence delete,
    0x1408ce450)."""
    return new_block(BLOCK_IMAGE_ITEM, ItemHeader(item.parent, item.item, item.left, item.right, 1, None).encode())


def transform_image(page: bytes, *, author: bytes, key: bytes,
                    vertices: list[tuple[float, float, float, float]]) -> bytes | None:
    """`page` with the image `key` moved, scaled or turned to new corners, as
    reMarkable.exe commits a selection transform (0x1409b48a0): the item is deleted
    where it is, and a copy with the new corners is added on top of the same layer
    under a new id (0x1408c2bc0), its moveId set to the item it replaced unless it
    already had one, so it keeps naming the first (0x1408cbd60). The copy keeps the
    item's imageRef and boundsTimestamp: the app transforms it with no new timestamp
    (0x1408c3570). Unchanged when the image already has these corners; None when the
    page no longer shows the image (deleted meanwhile: the delete wins)."""
    check_corners(vertices)
    found = _image_page(page, author, key)
    if found is None:
        return None
    header, blocks, before, original, index, item, value = found
    corners = [tuple(float(c) for c in v) for v in vertices]
    if [tuple(v) for v in value.vertices] == [tuple(struct.unpack("<4f", struct.pack("<4f", *v))) for v in corners]:
        return page
    me = _author_index(blocks, author)
    blocks[index] = _deleted(item)
    order = sequence(blocks, item.parent)
    new_id = (me, highest_counter(blocks) + 1)
    moved = ImageValue(value.ref, value.ref_timestamp, value.bounds_timestamp, corners, value.indices,
                       value.move_id if value.move_id not in (None, ZERO_ID) else item.item)
    _insert_item(blocks, new_block(BLOCK_IMAGE_ITEM, ItemHeader(item.parent, new_id, order[-1][0], ZERO_ID, 0,
                                                                moved.encode()).encode()))
    _check_untouched(before, original, blocks, {0, index})
    return join(header, blocks)


def delete_image(page: bytes, *, author: bytes, key: bytes) -> tuple[bytes, str | None]:
    """(`page` with the image `key` deleted, the name of the image file the page no
    longer uses or None). The item is deleted where it is (0x1408ce450), and the
    images table loses the entries no live item shows any more, as the app's
    pruneImageTable does when it saves; the table itself stays, even empty, as in real
    pages. Unchanged when the page no longer shows the image."""
    found = _image_page(page, author, key)
    if found is None:
        return page, None
    header, blocks, before, original, index, item, value = found
    blocks[index] = _deleted(item)
    replaced = {0, index}
    gone = None
    table = next((n for n, b in enumerate(blocks) if b.type == BLOCK_IMAGES_TABLE), None)
    if table is not None and live_image(blocks, key) is None:
        entries = decode_images_table(blocks[table].payload)
        if encode_images_table(entries) != blocks[table].payload:
            raise RmParseError("the images table is not understood")
        names = [e.file_name for e in entries if e.key == key]
        if names:
            blocks[table] = new_block(BLOCK_IMAGES_TABLE, encode_images_table([e for e in entries if e.key != key]))
            replaced.add(table)
            gone = names[0]
    _check_untouched(before, original, blocks, replaced)
    return join(header, blocks), gone
