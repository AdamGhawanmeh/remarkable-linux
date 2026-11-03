#!/usr/bin/env python3
"""Read the QML-facing API of reMarkable.exe's C++ classes from their Qt moc metadata.

    python3 client/tools/moc_api.py extracted/reMarkable.exe

writes client/src/ui/quick/bridge/qml_api.json: for each class the bridges implement,
its properties (type, writable, constant, notify signal), signals, slots and invokable
methods (argument and return types) and enums (keys and values), plus the QML module,
name and kind each type is registered under.

How: Qt 6 moc emits, per class, a QMetaObject {superdata, stringdata, data,
static_metacall, ...} in .data/.rdata. stringdata is an offset/length table followed by
the strings; data is the uint array of QMetaObjectPrivate (revision 12/13). Every
QMetaObject in the image is found by that shape, then decoded.
"""

from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "ui" / "quick" / "bridge" / "qml_api.json"

# QML module, QML name, kind, C++ class, as the app registers them. "uncreatable" types
# are used for their enums or as property types.
TYPES = [
    ("com.remarkable", "LibraryView", "uncreatable", "LibraryView"),
    ("com.remarkable", "LibraryNavigator", "creatable", "LibraryNavigator"),
    ("com.remarkable", "LibrarySearch", "creatable", "LibrarySearch"),
    ("com.remarkable", "Entry", "uncreatable", "Entry"),
    ("com.remarkable", "Document", "uncreatable", "Document"),
    ("com.remarkable", "DocumentIO", "uncreatable", "DocumentIO"),
    ("com.remarkable", "DocumentCover", "creatable", "DocumentCover"),
    ("com.remarkable", "Triangle", "creatable", "TriangleItem"),
    ("com.remarkable", "ThumbnailProvider", "creatable", "ThumbnailProvider"),
    ("com.remarkable", "ShareNative", "creatable", "ShareNative"),
    ("com.remarkable", "PageSelection", "creatable", "PageSelection"),
    ("com.remarkable", "ScreenSharePainter", "creatable", "screenshare::Painter"),
    ("com.remarkable", "Clipboard", "singleton", "Clipboard"),
    ("com.remarkable", "PlatformApp", "singleton", "PlatformApp"),
    ("com.remarkable", "PlatformHelpers", "singleton", "PlatformHelpers"),
    ("com.remarkable", "DocumentController", "singleton", "xofm::libs::library::DocumentController"),
    ("com.remarkable", "ScreenShareClient", "singleton", "screenshare::ClientFacade"),
    ("com.remarkable", "DocumentSync", "singleton", "xofm::libs::syncinterface::DocumentSyncInterface"),
    ("com.remarkable", "Authentication", "singleton", "Authentication"),
    ("com.remarkable", "UserInfo", "singleton", "xofm::libs::user::UserInfo"),
    ("com.remarkable", "NetworkClient", "singleton", "NetworkClient"),
    ("com.remarkable", "LanguageSettings", "singleton", "xofm::libs::localization::LanguageSettings"),
    ("com.remarkable", "LinkProvider", "singleton", "LinkProviderWrapper"),
    ("com.remarkable", "HandwritingLanguageModel", "singleton", "HandwritingLanguageModel"),
    ("com.remarkable", "SceneView", "creatable", "SceneView"),
    ("com.remarkable", "SceneController", "uncreatable", "SceneController"),
    ("com.remarkable", "SceneTileManager", "uncreatable", "SceneTileManager"),
    ("com.remarkable", "SceneKeyHandler", "uncreatable", "SceneKeyHandler"),
    ("com.remarkable", "SceneKeyHandlerAction", "uncreatable", "SceneKeyHandlerAction"),
    ("com.remarkable", "SceneParticipant", "uncreatable", "SceneParticipant"),
    ("com.remarkable", "SceneRenderContext", "uncreatable", "SceneRenderContext"),
    ("com.remarkable", "ParagraphStyle", "uncreatable", "scene::ParagraphStyle"),
    ("com.remarkable", "Page", "uncreatable", "Page"),
    ("com.remarkable", "DocumentLockManager", "creatable", "DocumentLockManager"),
    ("com.remarkable", "SceneLinkHandler", "creatable", "SceneLinkHandler"),
    ("com.remarkable", "PageSearchResultOverlayItem", "creatable", "PageSearchResultOverlayItem"),
    ("com.remarkable", "DocumentSearchController", "creatable", "DocumentSearchController"),
    ("com.remarkable", "PagePickerModel", "creatable", "PagePickerModel"),
    ("com.remarkable", "Recognizer", "singleton", "Recognizer"),
    ("com.remarkable", "RecognitionJob", "uncreatable", "RecognitionJob"),
    ("com.remarkable", "SelectionImage", "creatable", "SelectionImage"),
    ("com.remarkable", "SceneLayersModel", "creatable", "xofm::libs::sceneview::SceneLayersModel"),
    ("xofm.libs.sceneview", "SceneLayersModel", "creatable", "xofm::libs::sceneview::SceneLayersModel"),
    ("xofm.libs.library", "Library", "singleton", "xofm::libs::library::qml::LibraryFacade"),
    ("xofm.libs.library", "LibraryController", "singleton", "xofm::libs::library::LibraryController"),
    ("xofm.libs.library", "DocumentSelection", "singleton", "xofm::libs::library::DocumentSelection"),
    ("xofm.libs.library", "DocumentImporter", "singleton", "xofm::libs::library::DocumentImporter"),
    ("xofm.libs.recovery", "DeviceRecovery", "creatable", "xofm::libs::recovery::DeviceRecovery"),
    ("ark.controls", "FontExtender", "creatable", "ark::controls::FontExtender"),
    ("ark.tokens", "Settings", "singleton", "ark::tokens::Settings"),
    # Also a com.remarkable singleton (qmlregister(SingletonRegistration) at 0x1400b35cd), so
    # QML can read its enums (Settings.Prod, Settings.HomeSortOrder): an instance can't.
    ("com.remarkable", "Settings", "singleton", "Settings"),
    ("", "Settings", "context", "Settings"),
    ("", "", "object", "QmlEntryWrapper"),
    ("", "", "object", "QmlDocumentWrapper"),
]


class Image:
    def __init__(self, path: str) -> None:
        self.data = data = Path(path).read_bytes()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        count = struct.unpack_from("<H", data, pe + 6)[0]
        optional = pe + 24
        self.base = struct.unpack_from("<Q", data, optional + 24)[0]
        first = optional + struct.unpack_from("<H", data, pe + 20)[0]
        self.sections = []
        for i in range(count):
            s = first + 40 * i
            vsize, va, rsize, raw = struct.unpack_from("<IIII", data, s + 8)
            self.sections.append((data[s:s + 8].rstrip(b"\0").decode(), va, max(vsize, rsize), raw))

    def off(self, va: int) -> int | None:
        for _, rva, size, raw in self.sections:
            if self.base + rva <= va < self.base + rva + size:
                return raw + va - self.base - rva
        return None

    def va(self, off: int) -> int | None:
        for _, rva, size, raw in self.sections:
            if raw <= off < raw + size:
                return self.base + rva + off - raw
        return None

    def u32(self, off: int) -> int:
        return struct.unpack_from("<I", self.data, off)[0]


class MetaObject:
    def __init__(self, image: Image, offset: int) -> None:
        self.image = image
        q = struct.unpack_from("<4Q", image.data, offset + 8)
        self.super_va, self.strings, self.ints_at = q[0], image.off(q[1]), image.off(q[2])

    def s(self, index: int) -> str:
        o, n = struct.unpack_from("<II", self.image.data, self.strings + 8 * index)
        return self.image.data[self.strings + o:self.strings + o + n].decode()

    def ints(self, index: int, count: int) -> tuple:
        return struct.unpack_from(f"<{count}I", self.image.data, self.ints_at + 4 * index)

    def type_name(self, t: int):
        return self.s(t & 0x7FFFFFFF) if t & 0x80000000 else t

    def decode(self) -> tuple[str, dict]:
        _, cls, ccount, cdata, mcount, mdata, pcount, pdata, ecount, edata = self.ints(0, 10)
        out = {"super": None, "classinfo": {}, "properties": [], "methods": [], "enums": {}}
        for i in range(ccount):
            k, v = self.ints(cdata + 2 * i, 2)
            out["classinfo"][self.s(k)] = self.s(v)
        for i in range(pcount):
            name, typ, flags, notify, _ = self.ints(pdata + 5 * i, 5)
            out["properties"].append({"name": self.s(name), "type": self.type_name(typ), "writable": bool(flags & 2),
                                      "constant": bool(flags & 0x400),
                                      "notify": notify if notify != 0xFFFFFFFF else None})
        for i in range(mcount):
            name, argc, params, _, flags, _ = self.ints(mdata + 6 * i, 6)
            args = [[self.type_name(self.ints(params + 1 + a, 1)[0]), self.s(self.ints(params + 1 + argc + a, 1)[0])]
                    for a in range(argc)]
            out["methods"].append({"kind": ("method", "signal", "slot", "ctor")[(flags >> 2) & 3], "name": self.s(name),
                                   "args": args, "returns": self.type_name(self.ints(params, 1)[0])})
        for i in range(ecount):
            name, _, flags, kc, kd = self.ints(edata + 5 * i, 5)
            out["enums"][self.s(name)] = {"flag": bool(flags & 1), "keys": {
                self.s(self.ints(kd + 2 * k, 1)[0]): self.ints(kd + 2 * k + 1, 1)[0] for k in range(kc)}}
        return self.s(cls), out


def metaobjects(image: Image) -> dict[str, dict]:
    text = next(s for s in image.sections if s[0] == ".text")
    lo, hi = image.base + text[1], image.base + text[1] + text[2]
    found: dict[str, dict] = {}
    by_va: dict[int, str] = {}
    supers: dict[str, int] = {}
    for name, rva, size, raw in image.sections:
        if name not in (".data", ".rdata"):
            continue
        for off in range(raw, raw + size - 64, 8):
            sup, sd, dd, smc = struct.unpack_from("<4Q", image.data, off + 8)
            if not ((lo <= smc < hi or smc == 0) and (sup == 0 or lo <= sup < hi)):
                continue
            sdo, ddo = image.off(sd), image.off(dd)
            if sdo is None or ddo is None or image.u32(ddo) not in (12, 13):
                continue
            try:
                cls, info = MetaObject(image, off).decode()
            except (struct.error, UnicodeDecodeError, IndexError):
                continue
            if re.fullmatch(r"[A-Za-z_][\w:]*", cls) and cls not in found:
                found[cls] = info
                by_va[image.va(off)] = cls
                supers[cls] = sup
    for cls, sup in supers.items():     # superdata: a getter returning a metaobject
        if not sup:
            continue
        code = image.data[image.off(sup):image.off(sup) + 7]
        if code[:3] == b"\x48\x8d\x05":
            found[cls]["super"] = by_va.get(sup + 7 + struct.unpack_from("<i", code, 3)[0])
        elif code[:3] == b"\x48\x8b\x05":
            found[cls]["super"] = "<import>"
    return found


def main() -> None:
    every = metaobjects(Image(sys.argv[1]))
    classes: dict[str, dict] = {}

    def add(cls: str) -> None:
        if cls in classes or cls not in every:
            return
        classes[cls] = every[cls]
        if every[cls]["super"] in every:
            add(every[cls]["super"])

    for _, _, _, cls in TYPES:
        add(cls)
    missing = sorted({cls for *_, cls in TYPES} - set(classes))
    OUT.write_text(json.dumps({
        "source": "reMarkable.exe 3.29.0 (Qt 6.10.3) moc metadata, read by client/tools/moc_api.py",
        "types": [{"module": m, "name": n, "kind": k, "class": c} for m, n, k, c in TYPES],
        "classes": classes}, indent=1, sort_keys=True) + "\n")
    print(f"{len(classes)} classes -> {OUT}" + (f"; not found: {missing}" if missing else ""))


if __name__ == "__main__":
    main()
