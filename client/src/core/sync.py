"""Cloud sync engine for the reMarkable sync v3/v4 hash tree: download and commit.

Protocol (ARCHITECTURE.md §3.3; observed endpoints/keys, index layout as used by
existing open-source clients):

    GET /sync/v4/root            -> {"hash", "generation", "schemaVersion", ...}
                                    (404 -> /sync/v3/root; both 404 -> empty account)
    GET /sync/v3/files/{hash}    -> content-addressed blob; the request MUST carry
                                    `rm-filename: <name>` (root.docSchema for the root
                                    index, <id>.docSchema for a document index, the
                                    index entry name for files) or the server answers
                                    400 "unexpected 'rm-filename' http header"
    PUT /sync/v3/files/{hash}    <- blob upload, with rm-filename and
                                    `x-goog-hash: crc32c=<base64>` (the server checks
                                    it: "invalid checksum")
    PUT /sync/v3/root            <- {"generation": <the one the change is based on>,
                                    "hash": <new root index>} -> {"hash", "generation"}.
                                    The app commits here (upload-roothash-job.cpp);
                                    /sync/v4/root only appears in the download job.
                                    A stale generation is refused as a failed
                                    precondition (412; 409 handled the same way).

The root hash names a *root index*; each line names a *document index*; each
document index lists the document's files (`<id>.metadata`, `<id>.content`,
`<id>/<page>.rm`, `<id>.pdf`, ...). Index text format:

    3                                       schema 3
    <hash>:<type>:<id>:<subfiles>:<size>    one line per entry

    4                                       schema 4: same, plus a header line
    0:<id or .>:<count>:<total size>

There is no zip download in this API (the legacy /document-storage zip API is
not used by this app). Blobs are cached by hash in
~/.cache/remarkable-linux/blobs/, and documents are materialised to
~/.cache/remarkable-linux/documents/<id>/ or exported as a `.rmdoc` zip.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .auth import AuthClient, HttpResponse
from .config import atomic_write_json, cache_dir, ensure_private_dir, read_json
from .edits import (EditError, ImageDelete, ImageInsert, ImageRestore, ImageTransform, PageAdd, PageCopy, PageDelete,
                    PageMove, PendingEdits, document_files)
from .models import (COLLECTION_TYPE, DOCUMENT_TYPE, ROOT_ID, TRASH_ID, Content, Document,
                     DocumentTree, FileType, Metadata, Node, json_bytes, make_node, ms_timestamp,
                     new_document_content, new_folder_content, new_metadata)

log = logging.getLogger("rm.sync")

ROOT_V4_PATH = "/sync/v4/root"
ROOT_V3_PATH = "/sync/v3/root"
FILES_PATH = "/sync/v3/files/"
FILENAME_HEADER = "rm-filename"
ROOT_INDEX_NAME = "root.docSchema"

ROOT_ENTRY_TYPE = "80000000"   # root index rows, documents and folders alike
FILE_ENTRY_TYPE = "0"          # document index rows
CONFLICT_STATUSES = (409, 412)
COMMIT_ATTEMPTS = 1 + 3        # first try, then up to 3 retries after a conflict
IMPORT_TYPES = {".pdf": FileType.PDF, ".epub": FileType.EPUB}

DEFAULT_NETWORK_JOBS = 8
NETWORK_RETRIES = 3          # extra attempts after the first, for transient failures
MAX_RETRY_DELAY = 30.0
STATE_FILE = "state.json"


class SyncError(Exception):
    """A sync request failed or returned unusable data."""


class IntegrityError(SyncError):
    """A downloaded blob does not match its content hash."""


class NotCachedError(SyncError):
    """A blob was needed while working from the cache only, and is not cached."""


class ConflictError(SyncError):
    """The cloud kept changing under a commit (another device syncing meanwhile)."""


class SyncStatus(str, Enum):
    NOT_DOWNLOADED = "not_downloaded"   # metadata known, files not fetched
    SYNCED = "synced"                   # local copy matches the remote version
    OUTDATED = "outdated"               # remote changed since the local copy
    LOCAL_MODIFIED = "local_modified"   # local files edited since download
    REMOVED_REMOTE = "removed_remote"   # gone from the cloud, still cached locally


def _now(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------- root/index

@dataclass
class RootInfo:
    hash: str
    generation: int
    schema_version: int = 3
    has_missing_hashes: bool = False
    max_network_jobs: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.hash

    @classmethod
    def parse(cls, body: bytes) -> "RootInfo":
        try:
            raw = json.loads(body)
        except json.JSONDecodeError as err:
            raise SyncError(f"JSON parse error in root hash reply: {err}") from err
        if not isinstance(raw, dict):
            raise SyncError("root hash reply is not a JSON object")
        root_hash = raw.get("hash", "")
        if not isinstance(root_hash, str):
            raise SyncError("hash value is not a string in root hash reply")
        try:
            generation = int(raw.get("generation", 0))
        except (TypeError, ValueError) as err:
            raise SyncError(f"invalid generation {raw.get('generation')!r}") from err
        jobs = raw.get("maxNetworkJobs")
        return cls(
            hash=root_hash,
            generation=generation,
            schema_version=int(raw.get("schemaVersion") or 3),
            has_missing_hashes=bool(raw.get("hasMissingHashes", False)),
            max_network_jobs=int(jobs) if isinstance(jobs, int) and jobs > 0 else None,
            raw=raw,
        )


@dataclass(frozen=True)
class IndexEntry:
    hash: str
    type: str
    id: str
    subfiles: int
    size: int


@dataclass
class Index:
    schema: int
    entries: list[IndexEntry]


def parse_index(data: bytes) -> Index:
    lines = [ln for ln in data.decode("utf-8").splitlines() if ln.strip()]
    if not lines:
        raise SyncError("empty index file")
    try:
        schema = int(lines[0])
    except ValueError as err:
        raise SyncError(f"bad index schema line {lines[0]!r}") from err
    if schema not in (3, 4):
        raise SyncError(f"unsupported index schema {schema}")
    body = lines[1:]
    if schema == 4 and body and len(body[0].split(":")) == 4:
        body = body[1:]  # header: 0:<id>:<count>:<size>
    entries = []
    for line in body:
        parts = line.split(":")
        if len(parts) != 5:
            raise SyncError(f"malformed index line {line!r}")
        h, typ, ident, sub, size = parts
        if len(h) != 64:
            raise SyncError(f"bad hash in index line {line!r}")
        try:
            entries.append(IndexEntry(h, typ, ident, int(sub), int(size)))
        except ValueError as err:
            raise SyncError(f"malformed index line {line!r}") from err
    return Index(schema, entries)


def index_hash_matches(data: bytes, expected: str) -> bool:
    """True if an index blob matches `expected` under either schema's hashing.

    Schema 4 hashes the index file itself; schema 3 hashes the concatenated child
    hashes sorted by entry id ("hashOfHashesV3").
    """
    if sha256_hex(data) == expected:
        return True
    try:
        entries = parse_index(data).entries
    except SyncError:
        return False
    joined = b"".join(bytes.fromhex(e.hash) for e in sorted(entries, key=lambda e: e.id))
    return sha256_hex(joined) == expected


def build_index(schema: int, entries: Iterable[IndexEntry], header_id: str = ".") -> tuple[str, bytes]:
    """Serialise an index; returns (hash, data) under the schema's hashing rule.

    Rows are sorted by id, which is also the order schema 3 hashes them in.
    """
    rows = sorted(entries, key=lambda e: e.id)
    lines = [str(schema)]
    if schema == 4:
        lines.append(f"0:{header_id}:{len(rows)}:{sum(e.size for e in rows)}")
    lines += [f"{e.hash}:{e.type}:{e.id}:{e.subfiles}:{e.size}" for e in rows]
    data = ("\n".join(lines) + "\n").encode()
    if schema == 4:
        return sha256_hex(data), data
    return sha256_hex(b"".join(bytes.fromhex(e.hash) for e in rows)), data


def file_entry(name: str, data: bytes) -> IndexEntry:
    return IndexEntry(sha256_hex(data), FILE_ENTRY_TYPE, name, 0, len(data))


# ------------------------------------------------------------------- checksums

def _crc32c_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32C_TABLE = _crc32c_table()


def _crc32c_python(data: bytes) -> int:
    crc, table = 0xFFFFFFFF, _CRC32C_TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _crc32c_kernel(data: bytes) -> int:
    """CRC-32C from the Linux kernel crypto API (about 100× the Python loop)."""
    with socket.socket(socket.AF_ALG, socket.SOCK_SEQPACKET, 0) as alg:
        alg.bind(("hash", "crc32c"))
        op, _ = alg.accept()
        with op:
            view, chunk = memoryview(data), 1 << 16
            for start in range(0, max(len(view), 1), chunk):
                last = start + chunk >= len(view)
                op.send(view[start:start + chunk], 0 if last else socket.MSG_MORE)
            return int.from_bytes(op.recv(4), "little")


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), as used by Google Cloud Storage's x-goog-hash."""
    try:
        return _crc32c_kernel(data)
    except (OSError, AttributeError):          # not Linux, or no crc32c in the kernel
        return _crc32c_python(data)


def goog_hash(data: bytes) -> str:
    return "crc32c=" + base64.b64encode(crc32c(data).to_bytes(4, "big")).decode()


# ---------------------------------------------------------------------- imports

def sniff_import(path: Path, data: bytes) -> FileType:
    """The type of a file to import, checked by extension and content."""
    file_type = IMPORT_TYPES.get(path.suffix.lower())
    if file_type is FileType.PDF and b"%PDF-" in data[:1024]:
        return file_type
    if file_type is FileType.EPUB and data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if zf.read("mimetype").strip() == b"application/epub+zip":
                    return file_type
        except (zipfile.BadZipFile, KeyError):
            pass
    raise ValueError(f"{path.name} is not a PDF or EPUB file")


def pdf_page_count(data: bytes) -> int:
    """Pages in a PDF, or 0 if unknown (the tablet counts them when it opens it)."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:          # no pypdf, damaged or encrypted file: pypdf raises anything
        return 0


@dataclass
class ItemChange:
    """One item's part of a commit: its complete new file list (None removes the
    item) and the data of files the cloud does not have yet, by hash."""
    id: str
    files: dict[str, IndexEntry] | None
    data: dict[str, bytes] = field(default_factory=dict)


@dataclass
class FileChange:
    """One file an upload adds (old_* None) or replaces."""
    name: str
    size: int
    sha256: str
    old_sha256: str | None
    old_size: int | None


def json_changes(old: Any, new: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """(field, old value, new value) for each difference between two JSON values; a
    field of an entry of a list of objects with ids reads "pages[id=...].field"."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            where = f"{path}.{key}" if path else str(key)
            if key not in old:
                out.append((where, None, new[key]))
            elif key not in new:
                out.append((where, old[key], None))
            else:
                out += json_changes(old[key], new[key], where)
        return out
    if isinstance(old, list) and isinstance(new, list) and _by_id(old) is not None and _by_id(new) is not None \
            and (len(old) != len(new) or [e["id"] for e in old] != [e["id"] for e in new]):
        # A list of entries with ids (cPages.pages) whose entries were added or reordered:
        # by id, so that a page added in the middle does not read as every page changed.
        before, after = _by_id(old), _by_id(new)
        out = []
        for key in list(before) + [k for k in after if k not in before]:
            out += json_changes(before.get(key), after.get(key), f"{path}[id={key}]")
        moved = [k for k in after if k in before]
        if [k for k in before if k in after] != moved:
            out.append((f"{path} (order of entries)", None, None))
        return out
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        out = []
        for n, (a, b) in enumerate(zip(old, new)):
            label = f"id={b['id']}" if isinstance(b, dict) and isinstance(a, dict) and a.get("id") == b.get("id") \
                and "id" in b else str(n)
            out += json_changes(a, b, f"{path}[{label}]")
        return out
    return [] if old == new and type(old) is type(new) else [(path, old, new)]


def _by_id(entries: list) -> dict | None:
    """A list of objects with distinct string ids, by id; None for any other list."""
    if not all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries):
        return None
    out = {e["id"]: e for e in entries}
    return out if len(out) == len(entries) else None


def describe_edit(e) -> str:
    """One pending edit, as push plans list them."""
    if isinstance(e, PageAdd):
        return f"  add page {e.new_id} (template {e.template!r}) at {e.index}, after page {e.after or '(front)'}"
    if isinstance(e, PageCopy):
        pairs = ", ".join(f"{s} -> {n}" for s, n in zip(e.sources, e.new_ids))
        return f"  duplicate pages {pairs}\n    at {e.index}, after page {e.after or '(front)'}"
    if isinstance(e, PageMove):
        return f"  move pages {', '.join(e.pages)}\n    to {e.index}, after page {e.after or '(front)'}"
    if isinstance(e, PageDelete):
        return (f"  delete pages {', '.join(e.pages)}\n    (if no page is left: new \"Blank\" page {e.blank_id})")
    if isinstance(e, ImageDelete):
        return f"  delete image {e.key} on page {e.page_id}"
    if isinstance(e, ImageRestore):
        return f"  put back image {e.key} on page {e.page_id} (undo/redo)"
    xs, ys = [v[0] for v in e.vertices], [v[1] for v in e.vertices]
    where = f"x {min(xs):g}..{max(xs):g}, y {min(ys):g}..{max(ys):g} (scene units, x 0 = page centre)"
    if isinstance(e, ImageTransform):
        return f"  move/resize image {e.key} on page {e.page_id}\n    to {where}"
    return (f"  image on page {e.page_id}\n"
            f"    file {e.file_name}  ({e.size} bytes, sha256 {e.sha256[:12]}…), images-table key {e.key}\n"
            f"    placed {where}")


@dataclass
class PushPlan:
    """What uploading a document's pending edits would send (SyncClient.push_plan)."""
    doc_id: str
    edits: list  # core.edits.Edit
    generation: int                 # of the cached root the plan was worked out from
    root_hash: str
    schema: int
    files: list[FileChange]         # document files to upload, in upload order
    fields: dict[str, list[tuple[str, Any, Any]]]    # .content/.metadata: (field, old, new)
    index_hash: str                 # the document's new index (<id>.docSchema)
    index_data: bytes
    old_index_hash: str
    trashed: bool
    removed: list[FileChange] = field(default_factory=list)     # files the document no longer has

    def describe(self) -> str:
        lines = [f"Document  {self.doc_id}" + ("   (IN THE TRASH: the upload would be refused)" if self.trashed else ""),
                 f"Based on  root generation {self.generation}, root index {self.root_hash[:12]}… (schema {self.schema}), "
                 f"as cached; the upload re-reads the cloud first"]
        if not self.files:
            return "\n".join(lines + ["Nothing to upload: the document already has these edits."])
        lines.append(f"Edits     {len(self.edits)}, applied in this order")
        lines += [describe_edit(e) for e in self.edits]
        for name, changes in self.fields.items():
            lines.append(f"Fields changed in {name}")
            lines += [f"  {field}: {json.dumps(old)} -> {json.dumps(new)}" for field, old, new in changes] or ["  (none)"]
        lines.append(f"Files to upload ({len(self.files)} document files, then 2 indexes)")
        for n, f in enumerate(self.files, 1):
            what = "new" if f.old_sha256 is None else f"replaces {f.old_size} bytes, sha256 {f.old_sha256[:12]}…"
            lines.append(f"  {n}. PUT {FILES_PATH}{f.sha256}\n       rm-filename: {f.name}   {f.size} bytes   ({what})")
        for f in self.removed:
            lines.append(f"  (removed from the document's index, not deleted from the cloud's storage: "
                         f"{f.name}, {f.old_size} bytes)")
        n = len(self.files)
        lines.append(f"  {n + 1}. PUT {FILES_PATH}{self.index_hash}\n       rm-filename: {self.doc_id}.docSchema   "
                     f"{len(self.index_data)} bytes   (the document's index; replaces {self.old_index_hash[:12]}…)")
        lines.append(f"  {n + 2}. PUT {FILES_PATH}<new root index>\n       rm-filename: {ROOT_INDEX_NAME}   "
                     f"(the root index with this document's row changed; every other row as the cloud has it)")
        lines.append(f"Then      PUT {ROOT_V3_PATH}  {{\"generation\": <current, {self.generation} as cached>, "
                     f"\"hash\": <new root index>}}\n"
                     f"          refused by the cloud if another device synced meanwhile: then re-read, re-apply, retry")
        lines.append("The document's new index:")
        lines += ["  " + row for row in self.index_data.decode().splitlines()]
        return "\n".join(lines)


# ------------------------------------------------------------------ blob cache

class BlobCache:
    """Content-addressed, immutable blob store under <cache>/blobs/ab/<hash>."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or cache_dir()
        self.blob_dir = self.root / "blobs"

    def _path(self, blob_hash: str) -> Path:
        if len(blob_hash) != 64 or any(c not in "0123456789abcdef" for c in blob_hash):
            raise ValueError(f"not a sha256 hex digest: {blob_hash!r}")
        return self.blob_dir / blob_hash[:2] / blob_hash

    def path(self, blob_hash: str) -> Path:
        """Where a blob is (or would be) stored; check has() before reading it."""
        return self._path(blob_hash)

    def has(self, blob_hash: str) -> bool:
        return self._path(blob_hash).exists()

    def get(self, blob_hash: str) -> bytes | None:
        try:
            return self._path(blob_hash).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, blob_hash: str, data: bytes) -> None:
        path = self._path(blob_hash)
        if path.exists():
            return
        ensure_private_dir(path.parent)
        fd, tmp = tempfile.mkstemp(prefix=".blob.", dir=path.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def hashes(self) -> set[str]:
        if not self.blob_dir.exists():
            return set()
        return {p.name for p in self.blob_dir.glob("??/*") if not p.name.startswith(".")}

    def size(self) -> int:
        if not self.blob_dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.blob_dir.glob("??/*"))

    def prune(self, keep: Iterable[str]) -> int:
        """Delete every blob not in `keep`; returns the number removed."""
        keep = set(keep)
        removed = 0
        for h in self.hashes() - keep:
            self._path(h).unlink(missing_ok=True)
            removed += 1
        return removed

    def clear(self) -> None:
        shutil.rmtree(self.blob_dir, ignore_errors=True)


# ---------------------------------------------------------------- sync engine

class SyncClient:
    """Builds the document tree from the cloud and downloads documents on demand."""

    def __init__(self, auth: AuthClient, cache_root: Path | None = None,
                 max_workers: int | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.auth = auth
        self._sleep = sleep
        self.root_dir = cache_root or cache_dir()
        ensure_private_dir(self.root_dir)
        self.blobs = BlobCache(self.root_dir)
        self.documents_dir = self.root_dir / "documents"
        self.state_path = self.root_dir / STATE_FILE
        self.max_workers = max_workers
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = threading.RLock()        # one refresh or commit at a time
        self.state: dict[str, Any] = read_json(self.state_path) or {"docs": {}}
        self.state.setdefault("docs", {})
        self.root: RootInfo | None = None
        self.tree = DocumentTree()
        self.root_index: Index | None = None
        self.throttled = False                # the cloud answered 429 during this refresh
        self.edits = PendingEdits(self.root_dir / "pending")    # made here, not uploaded yet

    # -- transport ------------------------------------------------------------

    def _request(self, method: str, path: str, *, headers: dict[str, str] | None = None,
                 body: bytes | None = None, accept: tuple[int, ...] = (200, 404)) -> HttpResponse:
        """Send with retries for transient failures; returns replies with a status in
        `accept`.

        Timeouts, connection errors, HTTP 429 and 5xx are retried with exponential
        backoff (honouring Retry-After); anything left over becomes a SyncError, so
        no raw socket/HTTP exception escapes the sync layer. Retrying a PUT is safe:
        blobs are content-addressed, and a root commit that did land before a timeout
        makes the retry fail its generation check, which the commit loop handles.
        """
        delay, problem = 1.0, ""
        for attempt in range(NETWORK_RETRIES + 1):
            if attempt:
                log.info("retrying %s %s in %.0fs (%s)", method, path, delay, problem)
                self._sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY)
            try:
                resp = self.auth.authorized_request(method, path, headers=headers, body=body)
            except (OSError, http.client.HTTPException) as err:
                problem = f"{type(err).__name__}: {err}"
                continue
            if resp.status in accept:
                return resp
            if resp.status == 429 or resp.status >= 500:
                problem = f"HTTP {resp.status}"
                self.throttled = self.throttled or resp.status == 429
                retry_after = resp.header("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = min(max(delay, float(retry_after)), MAX_RETRY_DELAY)
                continue
            raise SyncError(f"{method} {path} failed: HTTP {resp.status} {resp.text[:200]}")
        raise SyncError(f"{method} {path} failed after {NETWORK_RETRIES + 1} attempts: {problem}")

    def _get(self, path: str, headers: dict[str, str] | None = None) -> HttpResponse:
        return self._request("GET", path, headers=headers)

    def fetch_root(self) -> RootInfo:
        for path in (ROOT_V4_PATH, ROOT_V3_PATH):
            resp = self._get(path)
            if resp.status == 200:
                root = RootInfo.parse(resp.body)
                log.info("root %s generation %d schema %d (%s)", root.hash[:12],
                         root.generation, root.schema_version, path)
                return root
            log.info("%s returned 404", path)
        log.info("no root hash available; new account?")
        return RootInfo(hash="", generation=0)

    def get_blob(self, blob_hash: str, filename: str, *, index: bool = False,
                 cached_only: bool = False) -> bytes:
        """Fetch a blob by hash; `filename` is its logical name, sent as rm-filename.

        With cached_only, never touches the network (NotCachedError on a miss).
        """
        cached = self.blobs.get(blob_hash)
        if cached is not None:
            return cached
        if cached_only:
            raise NotCachedError(f"{filename} ({blob_hash[:12]}) is not cached")
        resp = self._get(FILES_PATH + blob_hash, {FILENAME_HEADER: filename})
        if resp.status == 404:
            raise SyncError(f"blob {blob_hash} not found on server")
        data = resp.body
        if index:
            if not index_hash_matches(data, blob_hash):
                # The schema-3 hashing rule is inferred, so don't fail hard on indexes.
                log.warning("index %s does not match its hash under v3/v4 rules", blob_hash)
        elif sha256_hex(data) != blob_hash:
            raise IntegrityError(f"blob {blob_hash} failed integrity check")
        self.blobs.put(blob_hash, data)
        return data

    def get_index(self, blob_hash: str, filename: str, cached_only: bool = False) -> Index:
        return parse_index(self.get_blob(blob_hash, filename, index=True, cached_only=cached_only))

    # -- tree -----------------------------------------------------------------

    def _workers(self) -> int:
        if self.max_workers:
            return self.max_workers
        if self.root and self.root.max_network_jobs:
            return self.root.max_network_jobs
        return DEFAULT_NETWORK_JOBS

    def _load_entry(self, entry: IndexEntry, cached_only: bool = False):
        doc_index = self.get_index(entry.hash, f"{entry.id}.docSchema", cached_only)
        files = {e.id: e.hash for e in doc_index.entries}
        meta_name, content_name = f"{entry.id}.metadata", f"{entry.id}.content"
        if meta_name not in files:
            raise SyncError(f"entry {entry.id} has no .metadata file")
        metadata = Metadata.parse(self.get_blob(files[meta_name], meta_name, cached_only=cached_only))
        content = None
        if content_name in files and not metadata.is_collection:
            content = Content.parse(self.get_blob(files[content_name], content_name,
                                                  cached_only=cached_only))
        return make_node(entry.id, metadata, content, hash=entry.hash, files=files)

    def _load_entries(self, entries: list[IndexEntry], cached_only: bool,
                      done: Callable[[IndexEntry], None] | None = None) -> tuple[list, list]:
        nodes, failures = [], []
        with ThreadPoolExecutor(max_workers=self._workers()) as pool:
            futures = {pool.submit(self._load_entry, e, cached_only): e for e in entries}
            if done is not None:
                for fut, entry in futures.items():
                    fut.add_done_callback(lambda _, entry=entry: done(entry))
            for fut, entry in futures.items():
                try:
                    nodes.append(fut.result())
                except SyncError as err:
                    log.error("skipping %s: %s", entry.id, err)
                    failures.append(entry.id)
        return nodes, failures

    def load_cached(self) -> DocumentTree:
        """Rebuild the tree from the last synced root using cached blobs only.

        No network access: lets the app open instantly (and offline). Entries whose
        blobs are missing from the cache are skipped. Sync state is not changed.
        """
        root = self.state.get("root") or {}
        self.root = RootInfo(hash=root.get("hash", ""), generation=int(root.get("generation", 0)),
                             schema_version=int(root.get("schema_version") or 3))
        if self.root.is_empty or not self.blobs.has(self.root.hash):
            self.root_index = Index(4, [])
            self.tree = DocumentTree()
            return self.tree
        self.root_index = self.get_index(self.root.hash, ROOT_INDEX_NAME, cached_only=True)
        nodes, failures = self._load_entries(self.root_index.entries, cached_only=True)
        self.tree = DocumentTree(nodes)
        log.info("loaded %d items from cache (%d missing)", len(nodes), len(failures))
        return self.tree

    def refresh(self, progress: Callable[[int, int, bool], None] | None = None) -> DocumentTree:
        """Fetch the remote tree. Only indexes/metadata not already cached are downloaded.

        `progress(transferred, scheduled, throttled)` follows those downloads entry by entry,
        as the app's sync reports its ProgressStats; `throttled` once the cloud has answered
        429 (it is retried)."""
        with self._busy:
            self.throttled = False
            return self._apply_root(self.fetch_root(), progress)

    def forget(self) -> None:
        """Delete the cached library (blobs, materialised documents, sync state), as logging
        out does; the cache directory itself and anything else in it stay."""
        with self._busy:
            self.blobs.clear()
            shutil.rmtree(self.documents_dir, ignore_errors=True)
            self.state_path.unlink(missing_ok=True)
            with self._lock:
                self.state = {"docs": {}}
            self.root = None
            self.root_index = None
            self.tree = DocumentTree()
            self.edits.clear()

    def _apply_root(self, root: RootInfo,
                    progress: Callable[[int, int, bool], None] | None = None) -> DocumentTree:
        """Make `root` the current tree and record it in the sync state."""
        self.root = root
        now = _now(self._clock)
        if root.is_empty:
            entries: list[IndexEntry] = []
            self.root_index = Index(4, [])
        else:
            self.root_index = self.get_index(root.hash, ROOT_INDEX_NAME)
            entries = self.root_index.entries

        done = None
        if progress is not None:
            # Entries to download: those whose index is not cached yet.
            scheduled = {e.id for e in entries if not self.blobs.has(e.hash)}
            transferred: list[str] = []
            count = threading.Lock()
            progress(0, len(scheduled), self.throttled)

            def done(entry: IndexEntry) -> None:
                if entry.id in scheduled:
                    with count:
                        transferred.append(entry.id)
                        progress(len(transferred), len(scheduled), self.throttled)
        nodes, failures = self._load_entries(entries, cached_only=False, done=done)
        self.tree = DocumentTree(nodes)

        with self._lock:
            docs = self.state["docs"]
            remote_ids = {n.id for n in nodes}
            for node in nodes:
                rec = docs.setdefault(node.id, {})
                if rec.get("hash") != node.hash:
                    rec["hash"] = node.hash
                    rec["remote_changed_at"] = now
                rec["name"] = node.name
                rec["last_modified"] = (node.last_modified.isoformat()
                                        if node.last_modified else None)
                rec["seen_at"] = now
                rec.pop("removed_at", None)
            for doc_id, rec in list(docs.items()):
                if doc_id in remote_ids or doc_id in failures:
                    continue
                if rec.get("downloaded_hash"):
                    rec.setdefault("removed_at", now)
                else:
                    del docs[doc_id]
            self.state["root"] = {"hash": root.hash, "generation": root.generation,
                                  "schema_version": root.schema_version, "fetched_at": now}
            self.state["last_sync_at"] = now
            self._save_state()
        log.info("tree refreshed: %d items (%d failed)", len(nodes), len(failures))
        return self.tree

    # -- documents ------------------------------------------------------------

    def _document(self, doc_id: str) -> Document:
        node = self.tree.get(doc_id)
        if node is None:
            raise SyncError(f"unknown document {doc_id}")
        if not isinstance(node, Document):
            raise SyncError(f"{doc_id} is a folder, not a document")
        return node

    @staticmethod
    def _safe_relpath(name: str) -> PurePosixPath:
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise SyncError(f"refusing unsafe file name in index: {name!r}")
        return rel

    def local_dir(self, doc_id: str) -> Path:
        return self.documents_dir / doc_id

    def download_document(self, doc_id: str, cached_only: bool = False) -> Path:
        """Fetch every file of a document and materialise it under documents/<id>/.

        With cached_only, builds it from the blob cache (NotCachedError on a miss).
        """
        doc = self._document(doc_id)
        fetch = lambda h, name: self.get_blob(h, name, cached_only=cached_only)  # noqa: E731
        with ThreadPoolExecutor(max_workers=self._workers()) as pool:
            blobs = dict(zip(doc.files, pool.map(fetch, doc.files.values(), doc.files)))

        target = self.local_dir(doc_id)
        ensure_private_dir(self.documents_dir)
        staging = Path(tempfile.mkdtemp(prefix=f".{doc_id}.", dir=self.documents_dir))
        try:
            for name, data in blobs.items():
                dest = staging / self._safe_relpath(name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            # Swap in the complete new version so a crash never leaves a mix.
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        with self._lock:
            rec = self.state["docs"].setdefault(doc_id, {})
            rec.update(downloaded_hash=doc.hash, downloaded_at=_now(self._clock),
                       files=dict(doc.files))
            rec.pop("removed_at", None)
            self._save_state()
        log.info("downloaded %s (%d files) to %s", doc.name, len(blobs), target)
        return target

    def export_rmdoc(self, doc_id: str, dest: Path, cached_only: bool = False) -> Path:
        """Write the document as a .rmdoc zip (the app's import/export bundle)."""
        rec = self.state["docs"].get(doc_id, {})
        if self.status(doc_id) != SyncStatus.SYNCED or not rec.get("files"):
            self.download_document(doc_id, cached_only=cached_only)
        src = self.local_dir(doc_id)
        dest = Path(dest)
        if dest.suffix != ".rmdoc":
            dest = dest.with_suffix(".rmdoc")
        tmp = dest.with_name(f".{dest.name}.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(p for p in src.rglob("*") if p.is_file()):
                zf.write(path, path.relative_to(src).as_posix())
        os.replace(tmp, dest)
        return dest

    # -- writing ----------------------------------------------------------------

    def upload_blob(self, blob_hash: str, filename: str, data: bytes) -> None:
        """PUT a blob (idempotent: blobs are named by their hash) and cache it."""
        headers = {FILENAME_HEADER: filename, "x-goog-hash": goog_hash(data),
                   "Content-Type": "application/octet-stream"}
        self._request("PUT", FILES_PATH + blob_hash, headers=headers, body=data, accept=(200, 201, 204))
        self.blobs.put(blob_hash, data)

    def _put_root(self, generation: int, root_hash: str) -> RootInfo | None:
        """Commit a new root hash; None if `generation` is no longer the current one."""
        body = json.dumps({"generation": generation, "hash": root_hash}).encode()
        resp = self._request("PUT", ROOT_V3_PATH, headers={"Content-Type": "application/json"},
                             body=body, accept=(200,) + CONFLICT_STATUSES)
        if resp.status in CONFLICT_STATUSES:
            return None
        reply = RootInfo.parse(resp.body)
        if reply.hash and reply.hash != root_hash:
            raise SyncError(f"root commit answered with hash {reply.hash[:12]}, not {root_hash[:12]}")
        return RootInfo(root_hash, reply.generation, self.root.schema_version)

    def _commit(self, what: str, edit: Callable[[DocumentTree], list[ItemChange]],
                progress: Callable[[int, int], None] | None = None) -> DocumentTree:
        """Apply `edit` to the latest cloud tree and commit the result.

        Order: new content blobs, then document indexes, then the root index, then the
        root hash with the generation it was built on. The new root index starts from
        the raw root entries, so items this client could not load are kept. When
        another device committed meanwhile, the server refuses the generation: fetch
        the new root, re-apply `edit` to it and try again.
        """
        uploaded: set[str] = set()
        with self._busy:
            for attempt in range(COMMIT_ATTEMPTS):
                tree = self.refresh()
                base = self.root
                changes = edit(tree)
                if not changes:
                    return tree
                entries = {e.id: e for e in self.root_index.entries}
                blobs: dict[str, tuple[str, bytes]] = {}       # hash -> (name, data), in upload order
                indexes: dict[str, tuple[str, bytes]] = {}
                for change in changes:
                    if change.files is None:
                        entries.pop(change.id, None)
                        continue
                    for name, entry in sorted(change.files.items()):
                        if entry.hash in change.data:
                            blobs[entry.hash] = (name, change.data[entry.hash])
                    doc_hash, doc_data = build_index(base.schema_version, change.files.values(), change.id)
                    indexes[doc_hash] = (f"{change.id}.docSchema", doc_data)
                    entries[change.id] = IndexEntry(doc_hash, ROOT_ENTRY_TYPE, change.id, len(change.files),
                                                    sum(e.size for e in change.files.values()))
                root_hash, root_data = build_index(base.schema_version, entries.values())
                if root_hash == base.hash:
                    return tree                                 # nothing to change
                blobs = {h: v for h, v in blobs.items() if h not in uploaded}
                total, done = sum(len(data) for _, data in blobs.values()), 0
                for blob_hash, (name, data) in [*blobs.items(), *indexes.items(),
                                                (root_hash, (ROOT_INDEX_NAME, root_data))]:
                    if blob_hash in uploaded:
                        continue
                    self.upload_blob(blob_hash, name, data)
                    uploaded.add(blob_hash)
                    if blob_hash in blobs:
                        done += len(data)
                        if progress is not None:
                            progress(done, total)
                committed = self._put_root(base.generation, root_hash)
                if committed is not None:
                    log.info("%s: committed root %s, generation %d", what, root_hash[:12], committed.generation)
                    return self._apply_root(committed)
                log.warning("%s: generation %d is stale (another device synced); retrying",
                            what, base.generation)
        raise ConflictError(f"{what}: the cloud kept changing, gave up after {COMMIT_ATTEMPTS} attempts")

    def _replace_file(self, node: Node, name: str, data: bytes) -> ItemChange:
        files = {e.id: e for e in self.get_index(node.hash, f"{node.id}.docSchema").entries}
        files[name] = entry = file_entry(name, data)
        return ItemChange(node.id, files, {entry.hash: data})

    def _metadata_change(self, node: Node, touch: bool = True, **fields: Any) -> ItemChange | None:
        """`node` with some metadata fields changed, or None if they already match.
        `touch` also sets lastModified, as editing an item does (not reading it)."""
        if all(node.metadata.raw.get(k) == v for k, v in fields.items()):
            return None
        raw = {**node.metadata.raw, **fields}
        if touch:
            raw["lastModified"] = ms_timestamp(self._clock())
        return self._replace_file(node, f"{node.id}.metadata", json_bytes(raw))

    @staticmethod
    def _check_folder(tree: DocumentTree, folder_id: str) -> None:
        folder = tree.get(folder_id)
        if folder_id != ROOT_ID and (folder is None or not folder.is_folder or tree.is_trashed(folder_id)):
            raise SyncError("the destination folder no longer exists")

    def _update(self, what: str, item_ids: Iterable[str], touch: bool = True, **fields: Any) -> DocumentTree:
        """Change metadata fields of existing items (ones gone from the cloud are skipped)."""
        item_ids = list(item_ids)

        def edit(tree: DocumentTree) -> list[ItemChange]:
            nodes = [tree.get(i) for i in item_ids]
            changes = [self._metadata_change(n, touch, **fields) for n in nodes if n is not None]
            return [c for c in changes if c is not None]
        return self._commit(what, edit)

    def set_reading_position(self, doc_id: str, page: int, opened_ms: int | None = None) -> DocumentTree:
        """Record where a document was left (lastOpenedPage) and, with `opened_ms`, when it
        was opened (lastOpened). Reading is not editing: lastModified stays, as the
        tablet's own metadata shows (documents opened after their last change keep it)."""
        fields: dict[str, Any] = {"lastOpenedPage": int(page)}
        if opened_ms is not None:
            fields["lastOpened"] = str(int(opened_ms))
        return self._update(f"reading position of {doc_id}", [doc_id], touch=False, **fields)

    def import_file(self, path: str | Path, parent: str = ROOT_ID, name: str | None = None,
                    progress: Callable[[int, int], None] | None = None) -> str:
        """Upload a PDF or EPUB as a new document; returns its id.

        Files: <id>.pdf|.epub, <id>.content, <id>.metadata, as the official apps
        upload them; the tablet adds .pagedata and (for EPUBs) the PDF rendition.
        """
        path = Path(path)
        data = path.read_bytes()
        file_type = sniff_import(path, data)
        doc_id = str(uuid.uuid4())                      # fixed across commit retries
        now = ms_timestamp(self._clock())
        pages = pdf_page_count(data) if file_type is FileType.PDF else 0
        contents = {
            f"{doc_id}.{file_type.value}": data,
            f"{doc_id}.content": json_bytes(new_document_content(file_type, pages, len(data))),
            f"{doc_id}.metadata": json_bytes(new_metadata(name or path.stem, parent, DOCUMENT_TYPE, now)),
        }
        files = {n: file_entry(n, d) for n, d in contents.items()}
        change = ItemChange(doc_id, files, {files[n].hash: d for n, d in contents.items()})

        def edit(tree: DocumentTree) -> list[ItemChange]:
            self._check_folder(tree, parent)
            return [change]
        self._commit(f"import {path.name}", edit, progress)
        return doc_id

    def create_folder(self, name: str, parent: str = ROOT_ID) -> str:
        if not name.strip():
            raise ValueError("a folder needs a name")
        folder_id = str(uuid.uuid4())
        contents = {
            f"{folder_id}.content": json_bytes(new_folder_content()),
            f"{folder_id}.metadata": json_bytes(new_metadata(name.strip(), parent, COLLECTION_TYPE,
                                                             ms_timestamp(self._clock()))),
        }
        files = {n: file_entry(n, d) for n, d in contents.items()}
        change = ItemChange(folder_id, files, {files[n].hash: d for n, d in contents.items()})

        def edit(tree: DocumentTree) -> list[ItemChange]:
            self._check_folder(tree, parent)
            return [change]
        self._commit(f"create folder {name!r}", edit)
        return folder_id

    def rename(self, item_id: str, name: str) -> DocumentTree:
        if not name.strip():
            raise ValueError("a name cannot be empty")
        return self._update(f"rename {item_id}", [item_id], visibleName=name.strip())

    def move(self, item_ids: Iterable[str], parent: str) -> DocumentTree:
        item_ids = list(item_ids)

        def edit(tree: DocumentTree) -> list[ItemChange]:
            self._check_folder(tree, parent)
            changes = []
            for item_id in item_ids:
                node = tree.get(item_id)
                if node is None:
                    continue
                if node.is_folder and tree.is_within(parent, item_id):
                    raise SyncError(f"cannot move {node.name!r} into itself")
                change = self._metadata_change(node, parent=parent)
                if change is not None:
                    changes.append(change)
            return changes
        return self._commit(f"move {len(item_ids)} item(s)", edit)

    def set_pinned(self, item_ids: Iterable[str], pinned: bool) -> DocumentTree:
        return self._update("favorite" if pinned else "unfavorite", item_ids, pinned=pinned)

    def trash(self, item_ids: Iterable[str]) -> DocumentTree:
        return self._update("move to trash", item_ids, parent=TRASH_ID)

    def restore(self, item_ids: Iterable[str]) -> DocumentTree:
        """Back out of the trash, to My files ("%1 items were restored to My files")."""
        return self._update("restore", item_ids, parent=ROOT_ID)

    def delete(self, item_ids: Iterable[str]) -> DocumentTree:
        """Remove items, and everything inside deleted folders, from the cloud tree."""
        item_ids = list(item_ids)

        def edit(tree: DocumentTree) -> list[ItemChange]:
            doomed: dict[str, None] = {}
            for item_id in item_ids:
                if item_id in tree:
                    doomed[item_id] = None
                    doomed.update((n.id, None) for n in tree.descendants(item_id))
            return [ItemChange(i, None) for i in doomed]
        return self._commit(f"delete {len(item_ids)} item(s)", edit)

    def empty_trash(self) -> DocumentTree:
        with self._busy:
            tree = self.refresh()
            return self.delete(n.id for n in tree.nodes.values() if n.in_trash)

    # -- edits made here ---------------------------------------------------------

    def author(self) -> bytes:
        """This device's author UUID in the pages it edits (viewer.rm_writer.author_uuid)."""
        from viewer.rm_writer import author_uuid
        device_id = self.auth.state.device_id if self.auth.is_paired else ""
        if not device_id:
            raise EditError("editing needs this app to be logged in: pages record who changed them")
        return author_uuid(device_id)

    def staged_files(self, doc: Document, cached_only: bool = True,
                     edits: list | None = None) -> dict[str, bytes | None]:
        """The files of `doc` that its pending edits add or change, by name
        (core.edits.document_files): what the viewer shows and push_edits uploads."""
        edits = self.edits.of(doc.id) if edits is None else edits
        if not edits:
            return {}
        return document_files(doc, edits, lambda name: self.get_blob(doc.files[name], name, cached_only=cached_only),
                              self.edits.data, self.author())

    def push_plan(self, doc_id: str) -> "PushPlan":
        """What push_edits(doc_id) would upload, worked out from the cached library
        without any network access."""
        if self.root is None:
            self.load_cached()
        doc = self._document(doc_id)
        edits = self.edits.of(doc_id)
        files = self.staged_files(doc, cached_only=True, edits=edits)
        entries = {e.id: e for e in self.get_index(doc.hash, f"{doc.id}.docSchema", cached_only=True).entries}
        changes, fields = [], {}
        removed = []
        for name, data in sorted(files.items()):
            old = entries.get(name)
            if data is None:
                entries.pop(name, None)
                removed.append(FileChange(name, 0, "", old.hash if old else None, old.size if old else None))
                continue
            entries[name] = entry = file_entry(name, data)
            changes.append(FileChange(name, entry.size, entry.hash, old.hash if old else None, old.size if old else None))
            if name.endswith((".content", ".metadata")) and old is not None:
                before = json.loads(self.get_blob(old.hash, name, cached_only=True))
                fields[name] = json_changes(before, json.loads(data))
        index_hash, index_data = build_index(self.root.schema_version, entries.values(), doc.id)
        return PushPlan(doc.id, edits, self.root.generation, self.root.hash, self.root.schema_version, changes,
                        fields, index_hash, index_data, doc.hash, self.tree.is_trashed(doc_id), removed)

    def push_edits(self, doc_id: str, progress: Callable[[int, int], None] | None = None) -> DocumentTree:
        """Upload the pending edits of a document: its changed files, its index, the
        root (_commit). The edits are applied to the document as the cloud has it at
        that moment, so what another device changed meanwhile is kept; once the cloud
        has them they are no longer pending."""
        edits = self.edits.of(doc_id)
        if not edits:
            return self.tree

        def edit(tree: DocumentTree) -> list[ItemChange]:
            node = tree.get(doc_id)
            if not isinstance(node, Document) or tree.is_trashed(doc_id):
                raise SyncError("the edited document is no longer in the library")
            try:
                files = self.staged_files(node, cached_only=False, edits=edits)
            except EditError as err:
                raise SyncError(str(err)) from err
            if not files:
                return []                               # the cloud has these edits already
            entries = {e.id: e for e in self.get_index(node.hash, f"{node.id}.docSchema").entries}
            data: dict[str, bytes] = {}
            for name, blob in files.items():
                if blob is None:                        # no longer used: out of the document
                    entries.pop(name, None)
                    continue
                entries[name] = entry = file_entry(name, blob)
                data[entry.hash] = blob
            return [ItemChange(node.id, entries, data)]

        tree = self._commit(f"edits of {doc_id}", edit, progress)
        self.edits.discard(edits)
        return tree

    # -- local vs remote state --------------------------------------------------

    def local_changes(self, doc_id: str) -> list[str]:
        """Files of the downloaded copy whose content differs from what was downloaded."""
        rec = self.state["docs"].get(doc_id, {})
        files: dict[str, str] = rec.get("files") or {}
        base = self.local_dir(doc_id)
        changed = []
        for name, expected in files.items():
            path = base / self._safe_relpath(name)
            if not path.exists() or sha256_hex(path.read_bytes()) != expected:
                changed.append(name)
        if base.exists():
            for path in base.rglob("*"):
                rel = path.relative_to(base).as_posix()
                if path.is_file() and rel not in files:
                    changed.append(rel)
        return sorted(changed)

    def status(self, doc_id: str) -> SyncStatus:
        rec = self.state["docs"].get(doc_id)
        if rec is None or not rec.get("downloaded_hash"):
            return SyncStatus.NOT_DOWNLOADED
        if rec.get("removed_at"):
            return SyncStatus.REMOVED_REMOTE
        if self.local_changes(doc_id):
            return SyncStatus.LOCAL_MODIFIED
        if rec["downloaded_hash"] != rec.get("hash"):
            return SyncStatus.OUTDATED
        return SyncStatus.SYNCED

    def statuses(self) -> dict[str, SyncStatus]:
        return {doc_id: self.status(doc_id) for doc_id in self.state["docs"]}

    # -- cache management -------------------------------------------------------

    def referenced_hashes(self) -> set[str]:
        keep: set[str] = set()
        if self.root and self.root.hash:
            keep.add(self.root.hash)
        for node in self.tree.nodes.values():
            keep.add(node.hash)
            keep.update(node.files.values())
        return keep

    def prune_cache(self) -> dict[str, int]:
        """Drop blobs not in the current tree and local copies of removed documents.

        Local copies with unsynced edits are kept.
        """
        removed_docs = 0
        with self._lock:
            for doc_id, rec in list(self.state["docs"].items()):
                if rec.get("removed_at") and not self.local_changes(doc_id):
                    shutil.rmtree(self.local_dir(doc_id), ignore_errors=True)
                    del self.state["docs"][doc_id]
                    removed_docs += 1
            self._save_state()
        removed_blobs = self.blobs.prune(self.referenced_hashes())
        return {"blobs": removed_blobs, "documents": removed_docs}

    def clear_cache(self) -> None:
        with self._lock:
            self.blobs.clear()
            shutil.rmtree(self.documents_dir, ignore_errors=True)
            self.state = {"docs": {}}
            self._save_state()

    def _save_state(self) -> None:
        atomic_write_json(self.state_path, self.state)
