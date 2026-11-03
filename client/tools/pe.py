"""Just enough of the Windows PE format (64-bit images) for the install tools: sections,
imports, the exception directory, and the version and icon resources."""

from __future__ import annotations

import struct
from pathlib import Path

RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16


class PE:
    def __init__(self, data: bytes) -> None:
        if data[:2] != b"MZ":
            raise ValueError("not a Windows executable")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", data, pe + 24)[0] != 0x20B:
            raise ValueError("not a 64-bit PE image")
        self.data = data
        count, optional_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        optional = pe + 24
        self.image_base = struct.unpack_from("<Q", data, optional + 24)[0]
        directories = struct.unpack_from("<I", data, optional + 108)[0]
        self.directories = [struct.unpack_from("<II", data, optional + 112 + 8 * i) for i in range(directories)]
        self.sections = []
        for i in range(count):
            s = optional + optional_size + 40 * i
            vsize, rva, rsize, raw = struct.unpack_from("<IIII", data, s + 8)
            self.sections.append((data[s:s + 8].rstrip(b"\0").decode(), rva, max(vsize, rsize), raw, rsize))

    @classmethod
    def open(cls, path: Path) -> PE:
        return cls(Path(path).read_bytes())

    def offset(self, rva: int) -> int:
        for _, start, size, raw, _ in self.sections:
            if start <= rva < start + size:
                return raw + rva - start
        raise ValueError(f"RVA {rva:#x} is outside the image")

    def cstring(self, rva: int) -> bytes:
        o = self.offset(rva)
        return self.data[o:self.data.index(b"\0", o)]

    def text(self) -> tuple[int, bytes]:
        _, rva, _, raw, rsize = next(s for s in self.sections if s[0] == ".text")
        return rva, self.data[raw:raw + rsize]

    def import_slot(self, name: bytes) -> int:
        """RVA of the import address table slot for `name` (by name, any DLL)."""
        o = self.offset(self.directories[1][0])
        while True:
            lookup, _, _, dll, iat = struct.unpack_from("<IIIII", self.data, o)
            if dll == 0:
                raise LookupError(f"{name.decode()} is not imported")
            t = self.offset(lookup or iat)
            for i in range(1 << 16):
                entry = struct.unpack_from("<Q", self.data, t + 8 * i)[0]
                if entry == 0:
                    break
                if not entry >> 63 and self.cstring((entry & 0x7FFFFFFF) + 2) == name:
                    return iat + 8 * i
            o += 20

    def functions(self) -> list[tuple[int, int]]:
        """(begin, end) RVAs from the exception directory (.pdata), sorted."""
        rva, size = self.directories[3]
        o = self.offset(rva)
        return sorted(struct.unpack_from("<II", self.data, o + 12 * i) for i in range(size // 12))

    # -- resources ---------------------------------------------------------------

    def resources(self, kind: int) -> dict[int | str, bytes]:
        """{id or name: bytes} of the resources of one type (first language of each)."""
        rva, size = self.directories[2]
        if not rva:
            return {}
        root = self.offset(rva)

        def entries(directory: int):
            named, ids = struct.unpack_from("<HH", self.data, root + directory + 12)
            for i in range(named + ids):
                name, target = struct.unpack_from("<II", self.data, root + directory + 16 + 8 * i)
                yield name, target

        def leaf(target: int) -> bytes:
            while target & 0x80000000:                   # descend to the first language
                target = next(entries(target & 0x7FFFFFFF))[1]
            data_rva, data_size = struct.unpack_from("<II", self.data, root + target)
            o = self.offset(data_rva)
            return self.data[o:o + data_size]

        def key(name: int) -> int | str:
            if not name & 0x80000000:
                return name
            length = struct.unpack_from("<H", self.data, root + (name & 0x7FFFFFFF))[0]
            start = root + (name & 0x7FFFFFFF) + 2
            return self.data[start:start + 2 * length].decode("utf-16-le")

        for name, target in entries(0):
            if name == kind and target & 0x80000000:
                return {key(n): leaf(t) for n, t in entries(target & 0x7FFFFFFF)}
        return {}

    def version(self) -> str | None:
        """The file version from VS_FIXEDFILEINFO, e.g. "3.29.0.1804"."""
        for blob in self.resources(RT_VERSION).values():
            at = blob.find(struct.pack("<I", 0xFEEF04BD))
            if at >= 0:
                ms, ls = struct.unpack_from("<II", blob, at + 8)
                return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        return None

    def icon(self) -> bytes | None:
        """The first icon group as a .ico file."""
        groups, images = self.resources(RT_GROUP_ICON), self.resources(RT_ICON)
        for _, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
            count = struct.unpack_from("<H", group, 4)[0]
            entries = [struct.unpack_from("<12sH", group, 6 + 14 * i) for i in range(count)]
            entries = [(head, images[ident]) for head, ident in entries if ident in images]
            out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
            offset = 6 + 16 * len(entries)
            for head, image in entries:
                out += head[:8] + struct.pack("<II", len(image), offset)
                offset += len(image)
            for _, image in entries:
                out += image
            return bytes(out)
        return None
