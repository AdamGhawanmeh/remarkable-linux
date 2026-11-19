#!/usr/bin/env python3
"""New notebooks as this client writes them (core.create), compared key by key and type
by type with the notebooks of a real library cache.

    client/.venv/bin/python client/tools/create_schema_check.py [cache directory]

The cache (default ~/.cache/remarkable-linux) is only read: the tree is rebuilt from
state.json and the cached index blobs; nothing is written anywhere and no network is
used. For every cached notebook (.metadata type DocumentType, .content fileType
notebook):
  - every .content / .metadata key this client writes for a new notebook must be found
    in the cached files with the same JSON type, and every key all cached notebooks
    have must be written (keys only some have, such as "margins", are reported);
  - cPages: the same register layout ({"timestamp": "A:C", "value": ...}) for
    lastOpened / original and for page id / idx / template, the same uuids rows;
  - values every cached notebook agrees on are compared with ours;
  - a cached one-page notebook's page entry has the shape ours ends with (template
    at A:1, idx "ba" at A:2) when its single page was never moved.
It prints counts and key names only, nothing of the documents' content. Exit status 1
when a difference is found.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.create import notebook_content, notebook_metadata, set_page_template  # noqa: E402
from core.sync import BlobCache, parse_index  # noqa: E402

AUTHOR = bytes.fromhex("5eed0000000040008000000000000001")      # not anyone's device
PAGE = "5eed0000-0000-4000-8000-000000000002"
OPTIONAL_OK = {"margins", "new", "source", "createdTime", "deleted", "metadatamodified", "modified", "synced",
               "version", "lockedByPassword", "viewBackgroundFilter", "keyboardMetadata", "dummyDocument"}


def kind(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "int" if value.is_integer() else "float"
    return type(value).__name__


def load(root: Path):
    blobs = BlobCache(root)
    state = json.loads((root / "state.json").read_text())
    data = blobs.get((state.get("root") or {}).get("hash", ""))
    if data is None:
        raise SystemExit("no cached root index")
    for entry in parse_index(data).entries:
        index = blobs.get(entry.hash)
        if index is None:
            continue
        files = {e.id: e.hash for e in parse_index(index).entries}
        m, c = files.get(f"{entry.id}.metadata"), files.get(f"{entry.id}.content")
        if not m or not c or blobs.get(m) is None or blobs.get(c) is None:
            continue
        metadata, content = json.loads(blobs.get(m)), json.loads(blobs.get(c))
        if metadata.get("type") == "DocumentType" and content.get("fileType") == "notebook":
            yield metadata, content


def main() -> int:
    root = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / ".cache" / "remarkable-linux"
    ours_c = notebook_content(AUTHOR, PAGE)
    set_page_template(ours_c, PAGE, "Blank", AUTHOR)
    ours_m = notebook_metadata("Notebook", "", 1_900_000_000_000)
    notebooks = list(load(root))
    if not notebooks:
        print("no cached notebooks")
        return 1
    problems: list[str] = []
    for label, ours, theirs in (("content", ours_c, [c for _, c in notebooks]),
                                (".metadata", ours_m, [m for m, _ in notebooks])):
        types: dict[str, Counter] = defaultdict(Counter)
        values: dict[str, Counter] = defaultdict(Counter)
        for raw in theirs:
            for key, value in raw.items():
                types[key][kind(value)] += 1
                values[key][json.dumps(value, sort_keys=True)] += 1
        for key, value in sorted(ours.items()):
            if key not in types:
                problems.append(f"{label}: we write {key!r}, no cached notebook has it")
            elif kind(value) not in types[key]:
                problems.append(f"{label}: {key!r} is {kind(value)} here, {dict(types[key])} in the cache")
        for key, count in sorted((k, sum(t.values())) for k, t in types.items()):
            if key in ours:
                continue
            if count == len(theirs):
                problems.append(f"{label}: every cached notebook has {key!r}, we do not write it")
            else:
                print(f"{label}: {key!r} in {count} of {len(theirs)} cached notebooks, not written here "
                      f"({'expected' if key in OPTIONAL_OK else 'CHECK'})")
                if key not in OPTIONAL_OK:
                    problems.append(f"{label}: {key!r} not accounted for")
        agreed = {k: v for k, v in values.items() if len(v) == 1 and sum(v.values()) == len(theirs)}
        for key, counter in sorted(agreed.items()):
            (only,) = counter
            if key in ours and key not in ("cPages",) and json.dumps(ours[key], sort_keys=True) != only:
                problems.append(f"{label}: every cached notebook has {key}={only}, we write "
                                f"{json.dumps(ours[key], sort_keys=True)}")
        print(f"{label}: {len(ours)} keys written, checked against {len(theirs)} cached notebooks")

    # cPages layout.
    shapes = Counter()
    one_page = Counter()
    for _, content in notebooks:
        cp = content["cPages"]
        shapes[tuple(sorted(cp))] += 1
        for reg in ("lastOpened", "original"):
            r = cp.get(reg)
            if not (isinstance(r, dict) and sorted(r) == ["timestamp", "value"]):
                problems.append(f"cPages.{reg}: unexpected shape in the cache")
        for row in cp.get("uuids", []):
            if sorted(row) != ["first", "second"]:
                problems.append("cPages.uuids: unexpected row shape in the cache")
        pages = cp.get("pages", [])
        if len(pages) == 1 and set(pages[0]) <= {"id", "idx", "template", "modifed"}:
            p = pages[0]
            one_page[(p["idx"]["timestamp"].split(":")[1], p["idx"]["value"],
                      p.get("template", {}).get("timestamp", "-").split(":")[-1])] += 1
    if shapes != Counter({tuple(sorted(ours_c["cPages"])): len(notebooks)}):
        problems.append(f"cPages keys differ: {dict(shapes)}")
    ours_page = ours_c["cPages"]["pages"][0]
    ours_shape = (ours_page["idx"]["timestamp"].split(":")[1], ours_page["idx"]["value"],
                  ours_page["template"]["timestamp"].split(":")[1])
    print(f"cPages: {len(notebooks)} cached notebooks with the keys {sorted(ours_c['cPages'])}")
    print(f"one-page notebooks never reordered, (idx counter, key, template counter): {dict(one_page)}; "
          f"ours {ours_shape}")
    if one_page and ours_shape not in one_page:
        problems.append("no cached one-page notebook has our page entry's shape")
    for problem in problems:
        print("DIFFERENCE:", problem)
    print("OK" if not problems else f"{len(problems)} difference(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
