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

from core.auth import AuthClient, HttpResponse, decode_jwt_claims
from core.config import AuthState, AuthStore, Settings

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
        return HttpResponse(200, {}, json.dumps(payload).encode())

    def blob_requests(self) -> list[str]:
        return [p for p in self.requests if p.startswith("/sync/v3/files/")]


def paired_auth(config_dir: Path, transport, **claims) -> AuthClient:
    """An AuthClient that already holds a long-lived user token (no auth traffic)."""
    store = AuthStore(config_dir)
    store.save(AuthState(device_id="00000000-0000-4000-8000-000000000000",
                         device_token="device", auth_host=AUTH_HOST,
                         user_token=make_jwt({"exp": 4_000_000_000, **claims})))
    return AuthClient(Settings(), store, transport)
