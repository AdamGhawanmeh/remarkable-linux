"""The official app's resources and fonts, loaded the way the app loads them.

reMarkable.exe embeds its QML, icons, images and fonts as Qt resource trees
(qRegisterResourceData). Phase 1 carved those trees into ../resources/ (with
manifest.json listing each file's tree and qrc path). pack_rcc() repacks every carved
file, byte for byte, into one standard .rcc file at its original path, so the official
QML resolves "qrc:/qml/...", "qrc:/ark/icons/..." and its qmldir modules exactly as in
the app. The fonts (reMarkable Sans, reMarkable Serif Small, the icon fonts) come from
the same trees.

.rcc format (Qt rcc, version 1, big-endian):
    "qres", u32 version, u32 tree offset, u32 data offset, u32 names offset
    tree   14-byte nodes, breadth first, each directory's children contiguous and
           sorted by name hash: u32 name offset, u16 flags (2 = directory),
           then u32 child count + u32 first child index, or u16 territory,
           u16 language (1 = C), u32 data offset
    names  u16 length, u32 qt hash, UTF-16BE characters
    data   u32 size, bytes
"""

from __future__ import annotations

import json
import logging
import shutil
import struct
import subprocess
from pathlib import Path

log = logging.getLogger("rm.ui.quick.assets")

CLIENT = Path(__file__).resolve().parents[3]
RESOURCES = CLIENT.parent / "resources"
RCC_FILE = CLIENT / "src" / "assets" / "remarkable.rcc"
FONT_DIR = CLIENT / "src" / "assets" / "fonts"
USER_FONT_DIR = Path.home() / ".local" / "share" / "fonts" / "remarkable-linux"

FONT_FILES = (
    "reMarkableSans-Light.ttf", "reMarkableSans-LightItalic.ttf", "reMarkableSans-Regular.ttf",
    "reMarkableSans-RegularItalic.ttf", "reMarkableSans-Medium.ttf", "reMarkableSans-MediumItalic.ttf",
    "reMarkableSans-Bold.ttf", "reMarkableSans-BoldItalic.ttf", "reMarkableSans-ExtraBold.ttf",
    "reMarkableSans-ExtraBoldItalic.ttf", "reMarkableSerifSmall-Regular.ttf",
    "reMarkableSerifSmall-RegularItalic.ttf", "reMarkableSerifSmall-Bold.ttf",
    "reMarkableSerifSmall-BoldItalic.ttf", "icomoon.ttf", "icomoon_clients.ttf", "reMarkableSymbols.ttf",
)

_DIRECTORY = 2
_LANGUAGE_C = 1


def qt_hash(name: str) -> int:
    h = 0
    for ch in name:
        h = ((h << 4) + ord(ch)) & 0xFFFFFFFF
        h ^= (h & 0xF0000000) >> 23
        h &= 0x0FFFFFFF
    return h


def pack_rcc(files: dict[str, bytes]) -> bytes:
    """Build an .rcc from {"/qrc/path": data}."""
    root: dict = {}
    for path, data in files.items():
        parts = [p for p in path.split("/") if p]
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"{path}: a file is in the way")
        node[parts[-1]] = data

    names, name_offsets = bytearray(), {}

    def name_offset(name: str) -> int:
        if name not in name_offsets:
            name_offsets[name] = len(names)
            names.extend(struct.pack(">HI", len(name), qt_hash(name)) + name.encode("utf-16-be"))
        return name_offsets[name]

    data_blob = bytearray()
    tree: list[bytes] = []
    queue: list[tuple[str, object]] = [("", root)]
    tree.append(b"")                                    # the root, filled in below
    slots = [0]                                         # tree index of each queued directory
    index = 0
    while index < len(queue):
        name, node = queue[index]
        children = sorted(node.items(), key=lambda kv: qt_hash(kv[0]))
        first = len(tree)
        offset = name_offset(name) if name else 0
        tree[slots[index]] = struct.pack(">IHII", offset, _DIRECTORY, len(children), first)
        for child_name, child in children:
            if isinstance(child, dict):
                slots.append(len(tree))
                queue.append((child_name, child))
                tree.append(b"")
            else:
                tree.append(struct.pack(">IHHHI", name_offset(child_name), 0, 0, _LANGUAGE_C, len(data_blob)))
                data_blob.extend(struct.pack(">I", len(child)) + child)
        index += 1
    tree_bytes = b"".join(tree)
    header_size = 20
    tree_offset = header_size
    data_offset = tree_offset + len(tree_bytes)
    names_offset = data_offset + len(data_blob)
    return (b"qres" + struct.pack(">IIII", 1, tree_offset, data_offset, names_offset)
            + tree_bytes + bytes(data_blob) + bytes(names))


def carved_files(resources: Path = RESOURCES) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for entry in json.loads((resources / "manifest.json").read_text()):
        if entry["status"] == "ok":
            path = resources / entry["tree"] / entry["path"].lstrip("/")
            if path.is_file():
                files.setdefault(entry["path"], path.read_bytes())
    return files


def build_rcc(target: Path = RCC_FILE, resources: Path = RESOURCES,
              overrides: dict[str, Path] | None = None) -> Path:
    """Pack the carved files; `overrides` (qrc path -> local file) replace a few files
    that depend on parts of the app this client does not have (see quick/app.py)."""
    files = carved_files(resources)
    for path, source in (overrides or {}).items():
        if path not in files:
            raise KeyError(f"override for {path}, which the app does not have")
        files[path] = source.read_bytes()
    target.write_bytes(pack_rcc(files))
    log.info("packed %s", target)
    return target


def _stamp(overrides: dict[str, Path]) -> str:
    """Which overrides (by content) a pack was built with."""
    import hashlib
    return json.dumps({path: hashlib.sha256(src.read_bytes()).hexdigest() for path, src in sorted(overrides.items())})


def register_resources(rcc: Path = RCC_FILE, overrides: dict[str, Path] | None = None) -> None:
    """Make the app's resource trees available under qrc:/ (packing them again when the
    pack is missing or was built with other overrides)."""
    from PySide6.QtCore import QResource
    overrides = overrides or {}
    stamp_file = rcc.with_name(rcc.name + ".stamp")
    stamp = _stamp(overrides)
    if not rcc.exists() or not stamp_file.exists() or stamp_file.read_text() != stamp:
        build_rcc(rcc, overrides=overrides)
        stamp_file.write_text(stamp)
    if not QResource.registerResource(str(rcc)):
        raise RuntimeError(f"cannot register {rcc}")


def copy_fonts(resources: Path = RESOURCES, target: Path = FONT_DIR) -> None:
    """Copy the fonts out of the carved resource trees into the assets."""
    target.mkdir(parents=True, exist_ok=True)
    for entry in json.loads((resources / "manifest.json").read_text()):
        name = entry["path"].rsplit("/", 1)[-1]
        if name in FONT_FILES and entry["status"] == "ok":
            shutil.copyfile(resources / entry["tree"] / entry["path"].lstrip("/"), target / name)


def register_fonts(font_dir: Path = FONT_DIR) -> list[str]:
    """Load the fonts into the application (needs a QGuiApplication); returns families."""
    from PySide6.QtGui import QFontDatabase
    families: set[str] = set()
    for name in FONT_FILES:
        font_id = QFontDatabase.addApplicationFont(str(font_dir / name))
        if font_id < 0:
            raise RuntimeError(f"cannot load font {name}")
        families.update(QFontDatabase.applicationFontFamilies(font_id))
    return sorted(families)


def install_user_fonts(font_dir: Path = FONT_DIR, target: Path = USER_FONT_DIR) -> Path:
    """Install the fonts for the desktop user (fontconfig) and refresh its cache."""
    target.mkdir(parents=True, exist_ok=True)
    for name in FONT_FILES:
        shutil.copyfile(font_dir / name, target / name)
    subprocess.run(["fc-cache", "-f", str(target)], check=True, capture_output=True)
    return target
