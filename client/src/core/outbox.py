"""Uploading the edits made here: the one code path from PendingEdits to the cloud.

Both the app's outgoing sync (ui.outgoing: automatic after a pause, or the Sync / Upload
now button) and tools/push_edits.py --confirm upload through push_document(), so they
cannot drift apart:

    push_document(sync, doc_id)
        1. takes the upload lock (<cache>/pending/.push.lock, fcntl): one upload at a
           time across processes (the app and the tool);
        2. takes SyncClient._busy, the lock incoming syncs (refresh) and every other
           commit hold: an incoming sync never runs halfway through an upload, nor an
           upload halfway through an incoming sync;
        3. snapshots the document's pending edits and commits them (SyncClient._commit:
           refresh, apply the edits to the document as the cloud has it now, upload the
           changed files, its index, the root index, then PUT the root with the
           generation it was built on; a stale generation (another device synced)
           refreshes and applies the same edits to the newer version, up to
           COMMIT_ATTEMPTS times);
        4. settles the ledger in one locked change (PendingEdits.settle): the uploaded
           edits are forgotten and the document's other edits (made while it uploaded)
           are rebased (core.edits.rebase_refs): what they named by an uploaded edit's
           uid is named by the id the upload gave it, so no edit made meanwhile loses
           the item it was made on.

Failures leave every edit pending (nothing is forgotten before the root commit is
accepted) and come out as PushFailed with a `kind`: OFFLINE (no network, the cloud
unreachable or failing: try again later), CONFLICT (the cloud kept changing under the
commit), AUTH (log in again) or REJECTED (the edits cannot go as they are: the
document is gone or in the trash, a page they were made on was removed...).
"""

from __future__ import annotations

import fcntl
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .auth import AuthError, AuthNetworkError, DeviceRevokedError, NotPairedError
from .config import ensure_private_dir
from .edits import EditError, Staged, rebase_refs, uploaded_names
from .models import Document, DocumentTree
from .sync import ConflictError, ItemChange, NetworkError, SyncClient, SyncError, file_entry

log = logging.getLogger("rm.outbox")

PUSH_LOCK = ".push.lock"

OFFLINE, CONFLICT, AUTH, REJECTED = "offline", "conflict", "auth", "rejected"


class PushFailed(SyncError):
    """An upload that did not go through; the edits are all still pending."""

    def __init__(self, doc_id: str, kind: str, error: BaseException) -> None:
        super().__init__(str(error))
        self.doc_id, self.kind, self.error = doc_id, kind, error


def classify(error: BaseException) -> str:
    """What kind of failure `error` is, for retrying (see the module's doc)."""
    if isinstance(error, (NetworkError, AuthNetworkError, OSError)):
        return OFFLINE
    if isinstance(error, ConflictError):
        return CONFLICT
    if isinstance(error, (DeviceRevokedError, NotPairedError)):
        return AUTH
    if isinstance(error, AuthError):
        return OFFLINE              # the auth host could not be asked (a token refresh)
    return REJECTED


@dataclass
class PushResult:
    """One document's upload: the edits the cloud has now, the document's other edits
    as they were and as rebased, and the names the upload gave (core.edits.uploaded_names)."""

    doc_id: str
    uploaded: list = field(default_factory=list)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    made: dict = field(default_factory=dict)
    typed: dict = field(default_factory=dict)
    generation: int | None = None

    def rebase(self, obj):
        """`obj` (an edit, an undo entry...) with references to what the upload made
        renamed as the cloud's page names them (core.edits.rebase_refs)."""
        return rebase_refs(obj, self.made, self.typed)


Settler = Callable[[Callable[[], PushResult]], PushResult]


@contextmanager
def upload_lock(sync: SyncClient):
    """One upload at a time across processes (the app, tools/push_edits.py)."""
    ensure_private_dir(sync.edits.root)
    with open(sync.edits.root / PUSH_LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def push_document(sync: SyncClient, doc_id: str, progress: Callable[[int, int], None] | None = None,
                  settle: Settler | None = None) -> PushResult:
    """Upload the pending edits of a document (see the module's doc). `settle(fn)`, if
    given, runs the ledger change `fn()` and returns its result: the app wraps it in its
    undo-history lock so that the history is renamed with the ledger (LibrarySource).
    PushFailed when it did not go through."""
    try:
        with upload_lock(sync), sync._busy:
            return _push(sync, doc_id, progress, settle)
    except PushFailed:
        raise
    except (SyncError, AuthError, EditError, OSError) as err:
        kind = classify(err)
        log.log(logging.INFO if kind == OFFLINE else logging.WARNING,
                "edits of %s not uploaded (%s): %s", doc_id, kind, err)
        raise PushFailed(doc_id, kind, err) from err


def _push(sync: SyncClient, doc_id: str, progress, settle: Settler | None) -> PushResult:
    edits = sync.edits.of(doc_id)
    if not edits:
        return PushResult(doc_id)
    base: dict[str, Document] = {}

    def edit(tree: DocumentTree) -> list[ItemChange]:
        node = tree.get(doc_id)
        if not isinstance(node, Document) or tree.is_trashed(doc_id):
            raise SyncError("the edited document is no longer in the library")
        base["node"] = node
        try:
            files = sync.staged_files(node, cached_only=False, edits=edits)
        except EditError as err:
            raise SyncError(str(err)) from err
        if not files:
            return []                               # the cloud has these edits already
        entries = {e.id: e for e in sync.get_index(node.hash, f"{node.id}.docSchema").entries}
        data: dict[str, bytes] = {}
        for name, blob in files.items():
            if blob is None:                        # no longer used: out of the document
                entries.pop(name, None)
                continue
            entries[name] = entry = file_entry(name, blob)
            data[entry.hash] = blob
        return [ItemChange(node.id, entries, data)]

    sync._commit(f"edits of {doc_id}", edit, progress)
    result = PushResult(doc_id, list(edits), generation=sync.root.generation if sync.root else None)
    node = base.get("node")
    if node is not None:
        try:
            staged = Staged(node, edits, lambda name: sync.get_blob(node.files[name], name), sync.author(),
                            sync.edits.file)
            result.made, result.typed = uploaded_names(staged)
        except (EditError, SyncError, KeyError, ValueError) as err:
            # The upload went through; only the renaming is lost (edits made meanwhile
            # that name what it made then find nothing, as if it was deleted).
            log.error("cannot name what the edits of %s made: %s", doc_id, err)

    def apply() -> PushResult:
        result.before, result.after = sync.edits.settle(result.uploaded, doc_id, result.rebase)
        return result

    result = settle(apply) if settle is not None else apply()
    log.info("uploaded %d edits of %s (generation %s); %d made meanwhile kept", len(result.uploaded), doc_id,
             result.generation, len(result.after))
    return result


def push_all(sync: SyncClient, progress: Callable[[str, int, int], None] | None = None,
             settle: Settler | None = None, skip: Iterable[str] = ()) -> tuple[list[PushResult], list[PushFailed]]:
    """Upload every document's pending edits, one document (one commit) after another,
    except those in `skip`. A document whose edits are refused is left pending and the
    others still go; once the network is gone, the rest wait too."""
    results: list[PushResult] = []
    failures: list[PushFailed] = []
    skipped = set(skip)
    for doc_id in sync.edits.documents():
        if doc_id in skipped:
            continue
        report = (lambda sent, total, d=doc_id: progress(d, sent, total)) if progress is not None else None
        try:
            results.append(push_document(sync, doc_id, report, settle))
        except PushFailed as failed:
            failures.append(failed)
            if failed.kind in (OFFLINE, AUTH):
                break
    return results, failures


def discard(sync: SyncClient, doc_id: str) -> int:
    """Forget a document's pending edits without uploading them; how many there were."""
    with upload_lock(sync):
        edits = sync.edits.of(doc_id)
        sync.edits.discard(edits)
    return len(edits)


def describe_pending(sync: SyncClient) -> tuple[str, int]:
    """(what uploading would send, for every document with pending edits, and 0, or 1
    when some cannot be uploaded as they are), worked out from the cached library."""
    sync.load_cached()
    documents = sync.edits.documents()
    if not documents:
        return "No pending edits.", 0
    lines: list[str] = []
    status = 0
    for doc_id in documents:
        node = sync.tree.get(doc_id)
        lines.append("=" * 78)
        if node is None:
            lines.append(f"Document  {doc_id}\nNot in the cached library any more: its {len(sync.edits.of(doc_id))} "
                         f"pending edits cannot be uploaded (--discard {doc_id} forgets them).")
            status = 1
            continue
        lines.append(f"Name      {node.name!r}")
        try:
            lines.append(sync.push_plan(doc_id).describe())
        except (SyncError, EditError) as err:
            lines.append(f"Document  {doc_id}\nCannot be uploaded as it is: {err}")
            status = 1
    lines.append("=" * 78)
    lines.append("Nothing was sent. To upload one document's edits:  push_edits.py --confirm DOCUMENT_ID")
    return "\n".join(lines), status

