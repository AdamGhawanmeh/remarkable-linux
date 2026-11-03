#!/usr/bin/env python3
"""Carve the Qt resource trees compiled into reMarkable.exe.

    python3 client/tools/rcc_carve.py extracted/reMarkable.exe [resources/]

writes resources/rcc_<tree>/<path> for every file and resources/manifest.json, a list
of {"tree", "path", "flags", "status", "size"} (what ui/quick/assets.py packs into the
client's .rcc). <tree> is the file offset of the tree table, as 8 hex digits.

How: rcc compiles each .qrc into three arrays (tree, names, data) and an initializer
that registers them:

    lea   r9,  [data]
    mov   ecx, 3                     ; rcc format version
    lea   r8,  [names]
    lea   rdx, [tree]
    call  qRegisterResourceData      ; through the import table, or its jmp thunk

Every call to the Qt6Core import ?qRegisterResourceData@@YA_NHPEBE00@Z is found, its
four arguments are read from the instructions just before it, and each tree is walked.

Order matters: when two trees hold the same path, QResource returns the one registered
first. The initializers run from the C runtime's static-initializer table (in address
order), so trees are listed in the order of their table entries (the function holding
the call, or a function calling it). In 3.29 this decides /i18n/translations/
reMarkable_en.qm, present in two trees.

Tree layout:

    tree   nodes of 14 bytes (format 1) or 22 (2 and 3, + u64 last-modified):
           u32 name offset, u16 flags (1 zlib, 2 directory, 4 zstd), then
           u32 child count + u32 first child index, or u16 territory, u16 language,
           u32 data offset
    names  u16 length, u32 hash, UTF-16BE characters
    data   u32 size, then the bytes (zlib: u32 uncompressed size + zlib stream)
"""

from __future__ import annotations

import bisect
import json
import re
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pe import PE  # noqa: E402

REGISTER = b"?qRegisterResourceData@@YA_NHPEBE00@Z"
_COMPRESSED, _DIRECTORY, _ZSTD = 1, 2, 4
_LEA = {b"\x48\x8d\x15": "tree", b"\x4c\x8d\x05": "names", b"\x4c\x8d\x0d": "data"}   # rdx, r8, r9


def _rel_targets(code: bytes, base: int, opcode: bytes, length: int):
    """(offset in code, target RVA) of every rip-relative jump/call with this opcode."""
    for m in re.finditer(re.escape(opcode), code):
        at = m.start()
        if at + length <= len(code):
            yield at, base + at + length + struct.unpack_from("<i", code, at + length - 4)[0]


def registration_order(pe: PE, calls: dict[int, int]) -> dict[int, tuple]:
    """tree -> sort key: the static-initializer table entry that leads to its call
    (the containing function, or one calling it), else after all of them by address."""
    funcs = pe.functions()
    begins = [b for b, _ in funcs]

    def containing(rva: int) -> int | None:
        i = bisect.bisect_right(begins, rva) - 1
        return funcs[i][0] if i >= 0 and rva < funcs[i][1] else None

    base, code = pe.text()
    owners = {tree: containing(rva) for tree, rva in calls.items()}
    wanted = {f for f in owners.values() if f is not None}
    callers: dict[int, set[int]] = {f: set() for f in wanted}
    for op in (b"\xe8", b"\xe9"):
        for at, target in _rel_targets(code, base, op, 5):
            if target in callers and (c := containing(base + at)) is not None:
                callers[target].add(c)
    keys = {}
    for tree, owner in owners.items():
        entries = [pe.data.find(struct.pack("<Q", pe.image_base + f))
                   for f in ({owner} | callers.get(owner, set()) if owner is not None else set())]
        entries = [e for e in entries if e >= 0]
        keys[tree] = (0, min(entries)) if entries else (1, calls[tree])
    return keys


def registrations(pe: PE) -> list[dict[str, int]]:
    """(version, tree, names, data) of each qRegisterResourceData call, as file offsets,
    in registration order."""
    slot = pe.import_slot(REGISTER)
    base, code = pe.text()
    calls = [at for at, target in _rel_targets(code, base, b"\xff\x15", 6) if target == slot]
    thunks = {base + at for at, target in _rel_targets(code, base, b"\xff\x25", 6) if target == slot}
    calls += [at for op in (b"\xe8", b"\xe9") for at, target in _rel_targets(code, base, op, 5) if target in thunks]
    found: dict[int, dict[str, int]] = {}
    call_of: dict[int, int] = {}
    for call in sorted(calls):
        args: dict[str, int] = {}
        # The argument set-up is the 4 instructions (7+5+7+7 bytes) before the call.
        window = range(call - 1, max(call - 40, 0), -1)
        for at in window:
            op = code[at:at + 3]
            if op in _LEA and _LEA[op] not in args:
                args[_LEA[op]] = pe.offset(base + at + 7 + struct.unpack_from("<i", code, at + 3)[0])
            elif code[at] == 0xB9 and "version" not in args and at + 5 <= call:
                version = struct.unpack_from("<I", code, at + 1)[0]
                if 1 <= version <= 3:
                    args["version"] = version
            if len(args) == 4:
                break
        if {"tree", "names", "data"} <= args.keys():
            args.setdefault("version", 3)
            found.setdefault(args["tree"], args)
            call_of.setdefault(args["tree"], base + call)
    order = registration_order(pe, call_of)
    return [found[k] for k in sorted(found, key=lambda tree: order[tree])]


def _zstd(blob: bytes) -> bytes:
    try:
        from compression import zstd           # Python 3.14+
        return zstd.decompress(blob)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        raise RuntimeError("a resource is zstd-compressed: use Python 3.14+ or pip install zstandard") from None
    return zstandard.ZstdDecompressor().decompressobj().decompress(blob)


def walk(data: bytes, reg: dict[str, int]):
    """Yield (path, flags, bytes) for every file in one registered tree."""
    node_size = 14 if reg["version"] == 1 else 22
    tree, names, blobs = reg["tree"], reg["names"], reg["data"]

    def name(offset: int) -> str:
        length = struct.unpack_from(">H", data, names + offset)[0]
        start = names + offset + 6
        return data[start:start + 2 * length].decode("utf-16-be")

    def visit(index: int, path: str):
        at = tree + index * node_size
        name_offset, flags = struct.unpack_from(">IH", data, at)
        here = path + "/" + name(name_offset) if index else ""
        if flags & _DIRECTORY:
            count, first = struct.unpack_from(">II", data, at + 6)
            for child in range(first, first + count):
                yield from visit(child, here)
            return
        offset = struct.unpack_from(">I", data, at + 10)[0]
        size = struct.unpack_from(">I", data, blobs + offset)[0]
        blob = data[blobs + offset + 4:blobs + offset + 4 + size]
        if flags & _COMPRESSED:
            blob = zlib.decompress(blob[4:])
        elif flags & _ZSTD:
            blob = _zstd(blob)
        yield here, flags, blob

    yield from visit(0, "")


def carve(exe: Path, out: Path) -> list[dict]:
    pe = PE.open(exe)
    regs = registrations(pe)
    if not regs:
        raise RuntimeError(f"{exe}: no qRegisterResourceData calls found")
    manifest = []
    for reg in regs:
        tree = f"rcc_{reg['tree']:08x}"
        for path, flags, blob in walk(pe.data, reg):
            target = out / tree / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            manifest.append({"tree": tree, "path": path, "flags": flags, "status": "ok", "size": len(blob)})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    exe = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) == 3 else Path(__file__).resolve().parents[2] / "resources"
    manifest = carve(exe, out)
    trees = len({e["tree"] for e in manifest})
    print(f"{len(manifest)} files from {trees} resource trees -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
