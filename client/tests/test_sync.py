#!/usr/bin/env python3
"""Live sync check against a real reMarkable account (read-only).

Pair first with tests/test_auth.py. Then:

    python3 client/tests/test_sync.py                    # fetch tree, print it
    python3 client/tests/test_sync.py --search meeting   # find by name
    python3 client/tests/test_sync.py --download "Meeting notes"
    python3 client/tests/test_sync.py --export "Meeting notes" -o ~/notes.rmdoc
    python3 client/tests/test_sync.py --prune            # drop unreferenced cache blobs
    python3 client/tests/test_sync.py --clear-cache

Only GET requests are made; nothing in the cloud is changed. Data is cached in
~/.cache/remarkable-linux/. Like test_auth.py this is a manual CLI with no test_*
functions, so test runners collect nothing from it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.auth import AuthClient, AuthError  # noqa: E402
from core.models import Document, DocumentTree, Node  # noqa: E402
from core.sync import SyncClient, SyncError, SyncStatus  # noqa: E402

STATUS_MARK = {
    SyncStatus.NOT_DOWNLOADED: " ",
    SyncStatus.SYNCED: "✓",
    SyncStatus.OUTDATED: "↓",
    SyncStatus.LOCAL_MODIFIED: "✎",
    SyncStatus.REMOVED_REMOTE: "✗",
}


def describe(node: Node, sync: SyncClient) -> str:
    when = node.last_modified.astimezone().strftime("%F %H:%M") if node.last_modified else "-"
    pin = " 📌" if node.pinned else ""
    if isinstance(node, Document):
        mark = STATUS_MARK[sync.status(node.id)]
        kind = node.file_type.value or "?"
        return f"[{mark}] {node.name}  ({kind}, {node.page_count}p, {when}){pin}"
    return f"[/] {node.name}/{pin}"


def print_tree(tree: DocumentTree, sync: SyncClient) -> None:
    for node, depth in tree.walk():
        print("  " * depth + describe(node, sync))
    trash = tree.children("trash")
    if trash:
        print(f"Trash ({len(trash)}):")
        for node in trash:
            print("  " + describe(node, sync))


def resolve(tree: DocumentTree, ref: str) -> Node:
    node = tree.get(ref.lower())
    if node:
        return node
    exact = [n for n in tree.search(ref, include_trash=True) if n.name == ref]
    matches = exact or tree.search(ref, include_trash=True)
    if not matches:
        sys.exit(f"No item named or with id {ref!r}")
    if len(matches) > 1:
        lines = "\n".join(f"  {n.id}  {tree.path(n.id)}" for n in matches)
        sys.exit(f"{ref!r} is ambiguous; use an id:\n{lines}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--search", metavar="TEXT")
    parser.add_argument("--download", metavar="NAME_OR_ID")
    parser.add_argument("--export", metavar="NAME_OR_ID")
    parser.add_argument("-o", "--output", type=Path, help="export destination (.rmdoc)")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    auth = AuthClient()
    if not auth.is_paired:
        print("Not paired. Run client/tests/test_auth.py first.", file=sys.stderr)
        return 1
    sync = SyncClient(auth)

    if args.clear_cache:
        sync.clear_cache()
        print(f"Cleared {sync.root_dir}")
        return 0

    try:
        started = time.monotonic()
        tree = sync.refresh()
        root = sync.root
        print(f"Gateway   : {auth.gateway()}")
        print(f"Root      : {root.hash[:16] or '<empty account>'}  generation {root.generation}"
              f"  schema {root.schema_version}")
        print(f"Items     : {len(tree.documents())} documents, {len(tree.folders())} folders"
              f"  ({time.monotonic() - started:.1f}s)")
        if tree.orphans:
            print(f"Orphans   : {len(tree.orphans)} (parent missing; shown at top level)")
        print(f"Cache     : {sync.root_dir}  ({sync.blobs.size() / 1e6:.1f} MB)\n")

        if args.search:
            for node in tree.search(args.search, include_trash=True):
                print(f"{node.id}  {tree.path(node.id)}")
        elif not (args.download or args.export):
            print_tree(tree, sync)

        if args.download:
            node = resolve(tree, args.download)
            path = sync.download_document(node.id)
            print(f"\nDownloaded {tree.path(node.id)} -> {path}")
            for f in sorted(p for p in path.rglob("*") if p.is_file()):
                print(f"  {f.relative_to(path)}  ({f.stat().st_size} bytes)")
        if args.export:
            node = resolve(tree, args.export)
            dest = args.output or Path.cwd() / f"{node.name}.rmdoc"
            print(f"\nExported {tree.path(node.id)} -> {sync.export_rmdoc(node.id, dest)}")
        if args.prune:
            result = sync.prune_cache()
            print(f"\nPruned {result['blobs']} blobs and {result['documents']} removed documents")
    except (AuthError, SyncError) as err:
        print(f"Sync failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
