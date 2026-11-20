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

Edits are made in the app (Settings > Editing, or  client/run.py --edit-notebook NAME; see
README.md). The app uploads them itself too (Upload now, or automatically): both use
core.outbox, the same upload, conflict handling and ledger clean-up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import outbox  # noqa: E402
from core.auth import AuthClient  # noqa: E402
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
    text, status = outbox.describe_pending(sync)
    print(text)
    return status


def main(argv: list[str] | None = None, sync: SyncClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--confirm", metavar="DOCUMENT_ID", help="upload this document's pending edits")
    action.add_argument("--discard", metavar="DOCUMENT_ID", help="forget this document's pending edits")
    args = parser.parse_args(argv)
    sync = sync or SyncClient(AuthClient())

    if args.discard:
        count = outbox.discard(sync, args.discard)
        print(f"Forgot {count} pending edits of {args.discard}.")
        return 0
    if not args.confirm:
        return show(offline(sync))

    if not sync.edits.of(args.confirm):
        print(f"No pending edits of {args.confirm}.")
        return 1
    try:
        result = outbox.push_document(sync, args.confirm,
                                      lambda sent, total: print(f"  uploaded {sent} of {total} bytes"))
    except outbox.PushFailed as err:
        print(f"Not uploaded ({err.kind}): {err}\nThe edits are still pending.")
        return 1
    kept = f"; {len(result.after)} edits made meanwhile still pending" if result.after else ""
    print(f"Uploaded {len(result.uploaded)} edits of {args.confirm}; root generation {result.generation}{kept}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
