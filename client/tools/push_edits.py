#!/usr/bin/env python3
"""The edits made in this client that are not in the cloud yet: show them, upload them.

    client/.venv/bin/python client/tools/push_edits.py
        What uploading would send, for every document with pending edits: the document
        and page ids, the fields that change in .content and .metadata, and the files,
        in upload order. Worked out from the cached library; nothing touches the network.

    client/.venv/bin/python client/tools/push_edits.py --confirm DOCUMENT_ID
        Upload the pending edits of that document (the id the plan prints). This writes
        to your reMarkable cloud: the changed files, the document's index, the root.

    client/.venv/bin/python client/tools/push_edits.py --discard DOCUMENT_ID
        Forget the pending edits of that document. Nothing was uploaded; the page shows
        again as the cloud has it.

Edits are made with  client/run.py --edit-notebook NAME  (see README.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.auth import AuthClient, AuthError  # noqa: E402
from core.edits import EditError  # noqa: E402
from core.sync import SyncClient, SyncError  # noqa: E402


class NoNetwork(SyncError):
    pass


def offline(sync: SyncClient) -> SyncClient:
    """Make any request of `sync` an error: showing a plan must not reach the cloud."""
    def refuse(method: str, path: str, **_):
        raise NoNetwork(f"{method} {path}: not cached, and this tool only uploads with --confirm")
    sync._request = refuse
    return sync


def show(sync: SyncClient) -> int:
    sync.load_cached()
    documents = sync.edits.documents()
    if not documents:
        print("No pending edits.")
        return 0
    status = 0
    for doc_id in documents:
        node = sync.tree.get(doc_id)
        print("=" * 78)
        if node is None:
            print(f"Document  {doc_id}\nNot in the cached library any more: its {len(sync.edits.of(doc_id))} pending "
                  f"edits cannot be uploaded (--discard {doc_id} forgets them).")
            status = 1
            continue
        print(f"Name      {node.name!r}")
        try:
            print(sync.push_plan(doc_id).describe())
        except (SyncError, EditError) as err:
            print(f"Document  {doc_id}\nCannot be uploaded as it is: {err}")
            status = 1
    print("=" * 78)
    print("Nothing was sent. To upload one document's edits:  push_edits.py --confirm DOCUMENT_ID")
    return status


def main(argv: list[str] | None = None, sync: SyncClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--confirm", metavar="DOCUMENT_ID", help="upload this document's pending edits")
    action.add_argument("--discard", metavar="DOCUMENT_ID", help="forget this document's pending edits")
    args = parser.parse_args(argv)
    sync = sync or SyncClient(AuthClient())

    if args.discard:
        edits = sync.edits.of(args.discard)
        sync.edits.discard(edits)
        print(f"Forgot {len(edits)} pending edits of {args.discard}.")
        return 0
    if not args.confirm:
        return show(offline(sync))

    edits = sync.edits.of(args.confirm)
    if not edits:
        print(f"No pending edits of {args.confirm}.")
        return 1
    try:
        sync.push_edits(args.confirm, lambda sent, total: print(f"  uploaded {sent} of {total} bytes"))
    except (SyncError, AuthError, EditError, OSError) as err:
        print(f"Not uploaded: {err}\nThe edits are still pending.")
        return 1
    print(f"Uploaded {len(edits)} edits of {args.confirm}; root generation {sync.root.generation}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
