#!/usr/bin/env python3
"""Extract files from the official reMarkable Windows installer, without running it.

    python3 client/tools/ifw_extract.py reMarkable-3.29.0.1804-win64.exe out/ [name ...]

writes the named files (default: reMarkable.exe) from the installer's payload to out/.

The installer is a Qt Installer Framework binary: the packages are 7z archives
appended to the executable. They are found by their signature header (whose CRC must
match), and read with the standard library: the 7z container format is parsed here and
the streams are decoded with lzma (LZMA, LZMA2, BCJ and delta filters; the 3.29
installer uses LZMA only). Every extracted file is checked against its CRC32.
"""

from __future__ import annotations

import lzma
import mmap
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SIGNATURE = b"7z\xbc\xaf\x27\x1c"

# Property ids (7zFormat.txt).
END, HEADER, ARCHIVE_PROPERTIES, ADDITIONAL_STREAMS, MAIN_STREAMS, FILES_INFO = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
PACK_INFO, UNPACK_INFO, SUBSTREAMS_INFO, SIZE, CRC, FOLDER, CODERS_UNPACK_SIZE = 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C
NUM_UNPACK_STREAM, EMPTY_STREAM, EMPTY_FILE, NAME, ATTRIBUTES, ENCODED_HEADER, DUMMY = 0x0D, 0x0E, 0x0F, 0x11, 0x15, 0x17, 0x19

FILTERS = {
    b"\x03\x03\x01\x03": lzma.FILTER_X86, b"\x03\x03\x02\x05": lzma.FILTER_POWERPC,
    b"\x03\x03\x04\x01": lzma.FILTER_IA64, b"\x03\x03\x05\x01": lzma.FILTER_ARM,
    b"\x03\x03\x07\x01": lzma.FILTER_ARMTHUMB, b"\x03\x03\x08\x05": lzma.FILTER_SPARC,
}


class FormatError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise FormatError("truncated header")
        self.pos += 1
        return self.data[self.pos - 1]

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise FormatError("truncated header")
        self.pos += n
        return self.data[self.pos - n:self.pos]

    def number(self) -> int:
        first, mask, value = self.byte(), 0x80, 0
        for i in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def bits(self, n: int) -> list[bool]:
        out, mask, b = [], 0, 0
        for _ in range(n):
            if not mask:
                b, mask = self.byte(), 0x80
            out.append(bool(b & mask))
            mask >>= 1
        return out

    def defined(self, n: int) -> list[bool]:
        return [True] * n if self.byte() else self.bits(n)

    def digests(self, n: int) -> list[int | None]:
        return [struct.unpack("<I", self.bytes(4))[0] if d else None for d in self.defined(n)]


@dataclass
class Coder:
    method: bytes
    inputs: int
    outputs: int
    properties: bytes


@dataclass
class Folder:
    coders: list[Coder]
    bind_pairs: list[tuple[int, int]]      # (in index, out index)
    packed: list[int]                      # in indexes fed by pack streams
    unpack_sizes: list[int] = field(default_factory=list)
    crc: int | None = None

    def main_output(self) -> int:
        bound = {out for _, out in self.bind_pairs}
        return next(i for i in range(sum(c.outputs for c in self.coders)) if i not in bound)

    @property
    def size(self) -> int:
        return self.unpack_sizes[self.main_output()]


@dataclass
class Streams:
    pack_pos: int = 0
    pack_sizes: list[int] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)
    substream_sizes: list[list[int]] = field(default_factory=list)
    substream_crcs: list[list[int | None]] = field(default_factory=list)


def _folder(r: Reader) -> Folder:
    coders = []
    for _ in range(r.number()):
        flags = r.byte()
        method = r.bytes(flags & 0x0F)
        inputs, outputs = (r.number(), r.number()) if flags & 0x10 else (1, 1)
        properties = r.bytes(r.number()) if flags & 0x20 else b""
        if flags & 0x80:
            raise FormatError("alternative coder methods are not supported")
        coders.append(Coder(method, inputs, outputs, properties))
    total_in, total_out = sum(c.inputs for c in coders), sum(c.outputs for c in coders)
    bind_pairs = [(r.number(), r.number()) for _ in range(total_out - 1)]
    packed_count = total_in - len(bind_pairs)
    if packed_count == 1:
        bound = {i for i, _ in bind_pairs}
        packed = [next(i for i in range(total_in) if i not in bound)]
    else:
        packed = [r.number() for _ in range(packed_count)]
    return Folder(coders, bind_pairs, packed)


def _streams(r: Reader) -> Streams:
    s = Streams()
    while (kind := r.byte()) != END:
        if kind == PACK_INFO:
            s.pack_pos, count = r.number(), r.number()
            while (sub := r.byte()) != END:
                if sub == SIZE:
                    s.pack_sizes = [r.number() for _ in range(count)]
                elif sub == CRC:
                    r.digests(count)
                else:
                    raise FormatError(f"unexpected property {sub:#x} in pack info")
        elif kind == UNPACK_INFO:
            if r.byte() != FOLDER:
                raise FormatError("expected folder list")
            count = r.number()
            if r.byte():
                raise FormatError("external folder lists are not supported")
            s.folders = [_folder(r) for _ in range(count)]
            if r.byte() != CODERS_UNPACK_SIZE:
                raise FormatError("expected coder unpack sizes")
            for f in s.folders:
                f.unpack_sizes = [r.number() for _ in range(sum(c.outputs for c in f.coders))]
            while (sub := r.byte()) != END:
                if sub == CRC:
                    for f, crc in zip(s.folders, r.digests(count)):
                        f.crc = crc
                else:
                    raise FormatError(f"unexpected property {sub:#x} in unpack info")
        elif kind == SUBSTREAMS_INFO:
            counts = [1] * len(s.folders)
            sizes: list[list[int]] | None = None
            crcs: list[int | None] | None = None
            while (sub := r.byte()) != END:
                if sub == NUM_UNPACK_STREAM:
                    counts = [r.number() for _ in s.folders]
                elif sub == SIZE:
                    sizes = []
                    for f, n in zip(s.folders, counts):
                        part = [r.number() for _ in range(n - 1)] if n else []
                        sizes.append(part + [f.size - sum(part)] if n else [])
                elif sub == CRC:
                    unknown = sum(n for f, n in zip(s.folders, counts) if not (n == 1 and f.crc is not None))
                    crcs = r.digests(unknown)
                else:
                    raise FormatError(f"unexpected property {sub:#x} in substreams info")
            if sizes is None:
                sizes = [[f.size] if n == 1 else [] for f, n in zip(s.folders, counts)]
            pending = iter(crcs or [])
            for f, n in zip(s.folders, counts):
                if n == 1 and f.crc is not None:
                    s.substream_crcs.append([f.crc])
                else:
                    s.substream_crcs.append([next(pending, None) for _ in range(n)])
            s.substream_sizes = sizes
        else:
            raise FormatError(f"unexpected property {kind:#x} in streams info")
    if not s.substream_sizes:
        s.substream_sizes = [[f.size] for f in s.folders]
        s.substream_crcs = [[f.crc] for f in s.folders]
    return s


def _filters(folder: Folder) -> list[dict] | None:
    """The folder's coder chain as an lzma raw filter chain (None: a plain copy)."""
    if any(c.inputs != 1 or c.outputs != 1 for c in folder.coders):
        raise FormatError("multi-stream coders (BCJ2) are not supported")
    # With 1-in/1-out coders, coder i reads input i and writes output i; a bind pair
    # (input, output) feeds one coder's output into another's input. Walk from the
    # folder's result back to the packed stream: that is lzma's filter order.
    source = dict(folder.bind_pairs)
    chain, index = [], folder.main_output()
    while True:
        chain.append(folder.coders[index])
        if index not in source:
            break
        index = source[index]
    filters = []
    for c in chain:
        if c.method == b"\x00":
            continue
        if c.method == b"\x03\x01\x01":                            # LZMA
            d = c.properties[0]
            filters.append({"id": lzma.FILTER_LZMA1, "lc": d % 9, "lp": d // 9 % 5, "pb": d // 45,
                            "dict_size": struct.unpack_from("<I", c.properties, 1)[0]})
        elif c.method == b"\x21":                                  # LZMA2
            p = c.properties[0]
            size = 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)
            filters.append({"id": lzma.FILTER_LZMA2, "dict_size": size})
        elif c.method == b"\x03":                                  # delta
            filters.append({"id": lzma.FILTER_DELTA, "dist": c.properties[0] + 1})
        elif c.method in FILTERS:
            filters.append({"id": FILTERS[c.method]})
        else:
            raise FormatError(f"unsupported 7z method {c.method.hex()}")
    return filters or None


def _decode(data, start: int, size: int, folder: Folder, limit: int, chunk: int = 1 << 20):
    """Yield the folder's unpacked bytes, up to `limit` bytes."""
    filters = _filters(folder)
    if filters is None:
        yield data[start:start + min(size, limit)]
        return
    decoder = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters)
    produced = 0
    for at in range(start, start + size, chunk):
        out = decoder.decompress(data[at:min(at + chunk, start + size)])
        if out:
            yield out[:limit - produced]
            produced += len(out)
            if produced >= limit:
                return


@dataclass
class Entry:
    name: str
    size: int
    crc: int | None
    folder: int | None          # None: empty file or directory
    offset: int                 # in the folder's unpacked stream
    directory: bool


@dataclass
class Archive:
    data: object
    base: int                   # offset of the archive in the file
    streams: Streams
    entries: list[Entry]

    def unpack(self, index: int, limit: int):
        f = self.streams.folders[index]
        start = self.base + 32 + self.streams.pack_pos + sum(self.streams.pack_sizes[:self._first_pack(index)])
        size = sum(self.streams.pack_sizes[self._first_pack(index):self._first_pack(index) + len(f.packed)])
        return _decode(self.data, start, size, f, limit)

    def _first_pack(self, index: int) -> int:
        return sum(len(f.packed) for f in self.streams.folders[:index])


def _header(data, base: int, raw: bytes) -> tuple[Streams, list[Entry]]:
    r = Reader(raw)
    kind = r.byte()
    while kind == ENCODED_HEADER:                  # the header is itself packed
        packed = _streams(r)
        tmp = Archive(data, base, packed, [])
        raw = b"".join(tmp.unpack(0, packed.folders[0].size))
        if packed.folders[0].crc is not None and zlib.crc32(raw) != packed.folders[0].crc:
            raise FormatError("header CRC mismatch")
        r = Reader(raw)
        kind = r.byte()
    if kind != HEADER:
        raise FormatError("no archive header")
    streams, names, empty_stream, empty_file, attributes = Streams(), [], [], [], []
    while (kind := r.byte()) != END:
        if kind == ARCHIVE_PROPERTIES:
            while r.byte() != END:
                r.bytes(r.number())
        elif kind == ADDITIONAL_STREAMS:
            _streams(r)
        elif kind == MAIN_STREAMS:
            streams = _streams(r)
        elif kind == FILES_INFO:
            count = r.number()
            while (prop := r.byte()) != END:
                body = Reader(r.bytes(r.number()))
                if prop == EMPTY_STREAM:
                    empty_stream = body.bits(count)
                elif prop == EMPTY_FILE:
                    empty_file = body.bits(sum(empty_stream))
                elif prop == NAME:
                    if body.byte():
                        raise FormatError("external names are not supported")
                    names = body.data[body.pos:].decode("utf-16-le").split("\0")[:count]
                elif prop == ATTRIBUTES:
                    defined = body.defined(count)
                    body.byte()                      # external
                    attributes = [struct.unpack("<I", body.bytes(4))[0] if d else 0 for d in defined]
        else:
            raise FormatError(f"unexpected property {kind:#x} in header")
    entries: list[Entry] = []
    sizes = [(fi, size, crc) for fi, (ss, cc) in enumerate(zip(streams.substream_sizes, streams.substream_crcs))
             for size, crc in zip(ss, cc)]
    stream_iter = iter(sizes)
    offsets: dict[int, int] = {}
    empty_index = 0
    for i, name in enumerate(names):
        is_empty = empty_stream[i] if empty_stream else False
        if is_empty:
            is_file = empty_file[empty_index] if empty_index < len(empty_file) else False
            empty_index += 1
            directory = not is_file or bool(attributes and attributes[i] & 0x10)
            entries.append(Entry(name, 0, None, None, 0, directory))
            continue
        fi, size, crc = next(stream_iter)
        entries.append(Entry(name, size, crc, fi, offsets.get(fi, 0), False))
        offsets[fi] = offsets.get(fi, 0) + size
    return streams, entries


def archives(data) -> list[Archive]:
    """Every 7z archive embedded in `data` (bytes or mmap), in file order."""
    found, at = [], 0
    while (at := data.find(SIGNATURE, at)) >= 0:
        head = data[at:at + 32]
        if len(head) == 32 and zlib.crc32(head[12:32]) == struct.unpack_from("<I", head, 8)[0]:
            offset, size, crc = struct.unpack_from("<QQI", head, 12)
            raw = data[at + 32 + offset:at + 32 + offset + size]
            if len(raw) == size and zlib.crc32(raw) == crc:
                streams, entries = _header(data, at, raw)
                found.append(Archive(data, at, streams, entries))
                at += 32 + offset + size
                continue
        at += 1
    return found


def _safe(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise FormatError(f"unsafe path in archive: {name!r}")
    return path


def extract(installer: Path, out: Path, names: set[str]) -> dict[str, Path]:
    """Extract the files whose path (or base name) is in `names`; returns {name: path}."""
    written: dict[str, Path] = {}
    with open(installer, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for archive in archives(data):
            by_folder: dict[int, list[Entry]] = {}
            for e in archive.entries:
                path = _safe(e.name)
                if not e.directory and (str(path) in names or path.name in names) and path.name not in written:
                    by_folder.setdefault(e.folder, []).append(e)
            for folder, wanted in by_folder.items():
                if folder is None:                               # empty files
                    for e in wanted:
                        target = out / _safe(e.name).name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"")
                        written[_safe(e.name).name] = target
                    continue
                wanted.sort(key=lambda e: e.offset)
                end = max(e.offset + e.size for e in wanted)
                buffers = {id(e): bytearray() for e in wanted}
                position = 0
                for chunk in archive.unpack(folder, end):
                    for e in wanted:
                        lo, hi = max(e.offset, position), min(e.offset + e.size, position + len(chunk))
                        if lo < hi:
                            buffers[id(e)] += chunk[lo - position:hi - position]
                    position += len(chunk)
                for e in wanted:
                    blob = bytes(buffers[id(e)])
                    if len(blob) != e.size or (e.crc is not None and zlib.crc32(blob) != e.crc):
                        raise FormatError(f"{e.name}: CRC mismatch")
                    target = out / _safe(e.name).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob)
                    written[_safe(e.name).name] = target
    return written


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    names = set(sys.argv[3:]) or {"reMarkable.exe"}
    written = extract(Path(sys.argv[1]), Path(sys.argv[2]), names)
    for name in sorted(names - set(written)):
        print(f"not found in the installer: {name}", file=sys.stderr)
    for name, path in sorted(written.items()):
        print(f"{name} -> {path}")
    return 0 if written.keys() >= {Path(n).name for n in names} else 1


if __name__ == "__main__":
    sys.exit(main())
