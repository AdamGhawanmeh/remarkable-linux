"""In-memory stand-in for the reMarkable sync v3/v4 API, used by offline tests.

Reads: GET root and blobs (rm-filename required). Writes, as the live server is
understood from the app (ARCHITECTURE.md §3.3): PUT blobs with rm-filename and an
x-goog-hash CRC-32C that must match ("invalid checksum"), and PUT /sync/v3/root with
the generation the change is based on; a stale generation is refused with 412.

Auth (ARCHITECTURE.md §3.2): discovery names AUTH_HOST as "webapp"; a one-time code from
`codes` (None: any code) buys a device token once, a device token a user token; deleted or
`revoked` devices get HTTP 401.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from core import config as _config
from core.auth import AuthClient, HttpResponse, decode_jwt_claims
from core.config import AuthState, AuthStore, Settings

# Test apps start with a fresh settings.ini, where 3.29 would show its release notes
# (WhatsNewDialog) over the window. They start as if this version's were read already;
# test_v1_finish_offline checks the dialog with the real default.
_config.WHATS_NEW_READ_DEFAULT = "99.99"

COLLECTION = "80000000"
AUTH_HOST = "auth.invalid"
DISCOVERY_URL = "https://internal.cloud.remarkable.com/discovery/v1/endpoints"


def make_jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_of_hashes(entries: list[tuple[str, str]]) -> str:
    """Schema-3 index hash over (id, hash) pairs."""
    return sha(b"".join(bytes.fromhex(h) for _, h in sorted(entries)))


def crc32c_bitwise(data: bytes) -> int:
    """CRC-32C written independently of the client's (bit by bit, no table)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 & -(crc & 1))
    return crc ^ 0xFFFFFFFF


def index_rows(data: bytes) -> list[list[str]]:
    lines = [ln for ln in data.decode().splitlines() if ln]
    return [ln.split(":") for ln in lines[1:] if len(ln.split(":")) == 5]


class _Absent:
    """Marks a root-reply key the fake cloud leaves out; one instance, kept by copies."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (_Absent, ())

    def __repr__(self):
        return "ABSENT"


ABSENT = _Absent()     # FakeCloud.deleted_immutable_action: the key is left out of the root reply


class FakeCloud:
    def __init__(self, schema: int = 4) -> None:
        self.schema = schema
        self.blobs: dict[str, bytes] = {}
        self.docs: dict[str, dict[str, bytes]] = {}
        self.root_hash = ""
        self.generation = 0
        self.v4_available = True
        self.root_extra: dict = {}
        self.requests: list[str] = []
        self.hosts: list[str] = []
        self.filenames: dict[str, str] = {}  # blob hash -> logical name
        self.calls: list[tuple[str, str, dict]] = []   # (method, path, headers)
        self.conflict_status = 412
        self.before_commit = None            # called on each root PUT: another device syncing
        self.fail_next: list[int] = []       # statuses to answer the next requests with
        self.codes: set[str] | None = None   # one-time codes still valid (None: any code)
        self.device_tokens = {"device"}      # paired_auth's
        self.revoked = False                 # the account no longer accepts this device
        self.offline = False                 # no network: every request fails to connect
        # POST /convert/v1/handwriting: the request bodies (JSON) received, and the answer:
        # None -> synthetic JIIX of `handwriting_text`; a callable(request) -> HttpResponse.
        self.handwriting_requests: list[dict] = []
        self.handwriting_text = "hello world"
        self.handwriting = None
        # Missing hashes (ARCHITECTURE.md 3.8): a list makes the root reply say
        # hasMissingHashes and GET /sync/v3/missing answer it; `missing_reply` is a raw body
        # override. `deleted_immutable_action` goes into the root reply verbatim unless ABSENT.
        self.missing_hashes: list | None = None
        self.missing_reply: bytes | None = None
        self.deleted_immutable_action: object = ABSENT
        self.check_files_missing: list[str] = []
        self.check_files_requests: list[dict] = []     # POST /sync/v3/check-files bodies (JSON)

    # -- content ------------------------------------------------------------

    def put_item(self, item_id: str, metadata: dict, content: dict | None = None,
                 extra: dict[str, bytes] | None = None) -> None:
        files = {f"{item_id}.metadata": json.dumps(metadata).encode()}
        if content is not None:
            files[f"{item_id}.content"] = json.dumps(content).encode()
        files.update(extra or {})
        self.docs[item_id] = files
        self.publish()

    def remove_item(self, item_id: str) -> None:
        del self.docs[item_id]
        self.publish()

    def _index(self, header_id: str, rows: list[tuple[str, str, str, int, int]]) -> tuple[str, bytes]:
        lines = [str(self.schema)]
        if self.schema == 4:
            lines.append(f"0:{header_id}:{len(rows)}:{sum(r[4] for r in rows)}")
        lines += [f"{h}:{t}:{i}:{n}:{s}" for h, t, i, n, s in rows]
        data = ("\n".join(lines) + "\n").encode()
        if self.schema == 4:
            index_hash = sha(data)
        else:
            index_hash = hash_of_hashes([(i, h) for h, _, i, _, _ in rows])
        self.blobs[index_hash] = data
        self.filenames[index_hash] = ("root" if header_id == "." else header_id) + ".docSchema"
        return index_hash, data

    def publish(self) -> None:
        root_rows = []
        for item_id, files in sorted(self.docs.items()):
            rows = []
            for name, data in sorted(files.items()):
                self.blobs[sha(data)] = data
                self.filenames[sha(data)] = name
                rows.append((sha(data), "0", name, 0, len(data)))
            doc_hash, _ = self._index(item_id, rows)
            root_rows.append((doc_hash, COLLECTION, item_id, len(rows),
                              sum(r[4] for r in rows)))
        self.root_hash, _ = self._index(".", root_rows) if root_rows else ("", b"")
        self.generation += 1

    def corrupt(self, blob_hash: str) -> None:
        self.blobs[blob_hash] = b"corrupted" + self.blobs[blob_hash]

    # -- transport ----------------------------------------------------------

    def __call__(self, method, url, headers, body, timeout) -> HttpResponse:
        host, path = url.split("://", 1)[1].split("/", 1)
        path = "/" + path
        if self.offline:
            raise ConnectionRefusedError(f"offline: {method} {path}")
        self.requests.append(path)
        self.hosts.append(host)
        self.calls.append((method, path, dict(headers)))
        if url == DISCOVERY_URL:
            return HttpResponse(200, {}, json.dumps({"webapp": AUTH_HOST}).encode())
        if host == AUTH_HOST:
            return self._auth(path, headers, body)
        assert headers.get("Authorization", "").startswith("Bearer "), "missing auth"
        if self.fail_next:
            return HttpResponse(self.fail_next.pop(0), {}, b"")
        if self.revoked:
            return HttpResponse(401, {}, b"")
        if method == "POST" and path == "/convert/v1/handwriting":
            return self._handwriting(headers, body)
        if method == "POST" and path == "/sync/v3/check-files":
            self.check_files_requests.append(json.loads(body or b"{}"))
            return HttpResponse(200, {}, json.dumps({"missingFiles": self.check_files_missing}).encode())
        if method == "GET" and path == "/sync/v3/missing":
            if self.missing_reply is not None:
                return HttpResponse(200, {}, self.missing_reply)
            return HttpResponse(200, {}, json.dumps({"hashes": self.missing_hashes or []}).encode())
        if method == "PUT":
            return self._put(path, headers, body)
        if path == "/sync/v4/root":
            if not self.v4_available:
                return HttpResponse(404, {}, b"")
            return self._root()
        if path == "/sync/v3/root":
            return self._root()
        if path.startswith("/sync/v3/files/"):
            blob_hash = path.rsplit("/", 1)[1]
            # Live server: blob GETs need rm-filename; a missing or wrong name on an
            # index is rejected with this (misleading) message.
            name = headers.get("rm-filename")
            expected = self.filenames.get(blob_hash, "")
            if name is None or (expected.endswith(".docSchema") and name != expected):
                return HttpResponse(400, {}, b'{"message":"unexpected \'rm-filename\' http header"}')
            data = self.blobs.get(blob_hash)
            return HttpResponse(200, {}, data) if data is not None else HttpResponse(404, {}, b"")
        return HttpResponse(404, {}, b"")

    def _handwriting(self, headers: dict, body: bytes | None) -> HttpResponse:
        """The cloud's MyScript recogniser, synthetic: JIIX whose one Text element is
        `handwriting_text` (one word per space, boxes made up), as MyScript answers a
        "Raw Content" request with text recognition and words export on."""
        if headers.get("Accept") != "application/vnd.myscript.jiix":
            return HttpResponse(406, {}, b"")
        try:
            request = json.loads(body or b"")
        except ValueError:
            return HttpResponse(400, {}, b'{"message":"invalid JSON"}')
        self.handwriting_requests.append(request)
        if self.handwriting is not None:
            return self.handwriting(request)
        return HttpResponse(200, {"Content-Type": "application/vnd.myscript.jiix"}, synthetic_jiix(self.handwriting_text))

    def _auth(self, path: str, headers: dict, body: bytes | None) -> HttpResponse:
        bearer = headers.get("Authorization", "").removeprefix("Bearer ")
        if path == "/token/json/2/device/new":
            code = json.loads(body or b"{}").get("code", "")
            if self.codes is not None and code not in self.codes:
                return HttpResponse(400, {}, b"invalid one-time-code")
            if self.codes is not None:
                self.codes.discard(code)         # one-time
            token = f"device-{len(self.device_tokens)}"
            self.device_tokens.add(token)
            return HttpResponse(200, {}, token.encode())
        if path == "/token/json/2/user/new":
            if self.revoked or bearer not in self.device_tokens:
                return HttpResponse(401, {}, b"")
            return HttpResponse(200, {}, make_jwt({"exp": 4_000_000_000, "device": bearer}).encode())
        if path == "/token/json/3/device/delete":       # with the user token, or the device token
            try:
                device = decode_jwt_claims(bearer).get("device", bearer)
            except ValueError:
                device = bearer
            self.device_tokens.discard(device)
            return HttpResponse(200, {}, b"")
        return HttpResponse(404, {}, b"")

    def _put(self, path: str, headers: dict, body: bytes) -> HttpResponse:
        if path.startswith("/sync/v3/files/"):
            blob_hash = path.rsplit("/", 1)[1]
            name = headers.get("rm-filename")
            if not name:
                return HttpResponse(400, {}, b'{"message":"missing rm-filename"}')
            crc = base64.b64encode(crc32c_bitwise(body).to_bytes(4, "big")).decode()
            if headers.get("x-goog-hash") != f"crc32c={crc}":
                return HttpResponse(400, {}, b'{"message":"invalid checksum"}')
            if name.endswith(".docSchema"):
                rows = index_rows(body)
                ok = sha(body) == blob_hash if self.schema == 4 else \
                    hash_of_hashes([(r[2], r[0]) for r in rows]) == blob_hash
            else:
                ok = sha(body) == blob_hash
            if not ok:
                return HttpResponse(400, {}, b'{"message":"hash mismatch"}')
            self.blobs[blob_hash] = body
            self.filenames[blob_hash] = name
            return HttpResponse(200, {}, b"")
        if path == "/sync/v3/root":
            request = json.loads(body)
            if self.before_commit is not None:
                self.before_commit()
            if request["generation"] != self.generation:
                return HttpResponse(self.conflict_status, {}, b'{"message":"generation mismatch"}')
            if request["hash"] not in self.blobs:
                return HttpResponse(400, {}, b'{"message":"unknown root hash"}')
            self.root_hash = request["hash"]
            self.generation += 1
            self._load_docs()
            return HttpResponse(200, {}, json.dumps({"hash": self.root_hash,
                                                      "generation": self.generation}).encode())
        return HttpResponse(404, {}, b"")

    def _load_docs(self) -> None:
        """Rebuild the item model from the committed root (so put_item still works)."""
        self.docs = {}
        for doc_hash, _, item_id, _, _ in index_rows(self.blobs[self.root_hash]):
            self.docs[item_id] = {name: self.blobs[h] for h, _, name, _, _ in index_rows(self.blobs[doc_hash])}

    def item(self, item_id: str) -> dict[str, bytes]:
        return self.docs[item_id]

    def metadata(self, item_id: str) -> dict:
        return json.loads(self.docs[item_id][f"{item_id}.metadata"])

    def puts(self) -> list[tuple[str, dict]]:
        return [(path, headers) for method, path, headers in self.calls if method == "PUT"]

    def _root(self) -> HttpResponse:
        if not self.root_hash:
            return HttpResponse(404, {}, b"sync/root-not-found")
        payload = {"hash": self.root_hash, "generation": self.generation,
                   "schemaVersion": self.schema, **self.root_extra}
        if self.missing_hashes is not None:
            payload["hasMissingHashes"] = True
        if self.deleted_immutable_action is not ABSENT:
            payload["deletedImmutableAction"] = self.deleted_immutable_action
        return HttpResponse(200, {}, json.dumps(payload).encode())

    def blob_requests(self) -> list[str]:
        return [p for p in self.requests if p.startswith("/sync/v3/files/")]


def synthetic_jiix(text: str, y: float = 10.0) -> bytes:
    """A JIIX answer as MyScript's iink REST API gives it for Raw Content: lines of
    `text` in one Text element, its words with made-up bounding boxes (mm)."""
    words, x = [], 5.0
    for line_no, line in enumerate(text.split("\n")):
        for n, word in enumerate(line.split(" ")):
            if n:
                words.append({"label": " "})
            words.append({"label": word, "candidates": [word, word.lower()],
                          "bounding-box": {"x": x, "y": y + 9.0 * line_no, "width": 4.0 * len(word), "height": 7.5}})
            x += 4.0 * len(word) + 2.0
        words.append({"label": "\n"})
        x = 5.0
    if words:
        words.pop()
    element = {"type": "Text", "label": text, "words": words, "id": "MainBlock/1",
               "bounding-box": {"x": 5.0, "y": y, "width": 120.0, "height": 9.0 * (text.count("\n") + 1)}}
    return json.dumps({"type": "Raw Content", "version": "3", "id": "MainBlock",
                       "bounding-box": element["bounding-box"],
                       "elements": [element, {"type": "Raw Content", "kind": "drawing", "id": "MainBlock/2"}]}).encode()


def paired_auth(config_dir: Path, transport, **claims) -> AuthClient:
    """An AuthClient that already holds a long-lived user token (no auth traffic)."""
    store = AuthStore(config_dir)
    store.save(AuthState(device_id="00000000-0000-4000-8000-000000000000",
                         device_token="device", auth_host=AUTH_HOST,
                         user_token=make_jwt({"exp": 4_000_000_000, **claims})))
    return AuthClient(Settings(), store, transport)
