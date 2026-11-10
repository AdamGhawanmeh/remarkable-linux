"""Offline tests for core.models and core.sync against an in-memory fake cloud.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakecloud import FakeCloud, hash_of_hashes, paired_auth, sha  # noqa: E402

from core.auth import HttpResponse  # noqa: E402
from core.models import (  # noqa: E402
    Content, Document, DocumentTree, FileType, Folder, Metadata, is_uuid, make_node,
    parse_timestamp, parse_uuid)
from core.sync import (  # noqa: E402
    BlobCache, ConflictError, IndexEntry, IntegrityError, NotCachedError, RootInfo, SyncClient,
    SyncError, SyncStatus, _crc32c_python, build_index, crc32c, goog_hash, index_hash_matches,
    parse_index, pdf_page_count, sniff_import)

# Fixed IDs so failures are readable.
WORK = "11111111-1111-4111-8111-111111111111"
PROJECTS = "22222222-2222-4222-8222-222222222222"
NOTES = "33333333-3333-4333-8333-333333333333"
PAPER = "44444444-4444-4444-8444-444444444444"
OLD = "55555555-5555-4555-8555-555555555555"
ORPHAN = "66666666-6666-4666-8666-666666666666"
PAGE1 = "aaaaaaaa-0000-4000-8000-000000000001"
PAGE2 = "aaaaaaaa-0000-4000-8000-000000000002"
PAGE3 = "aaaaaaaa-0000-4000-8000-000000000003"

TS = "1730000000000"  # 2024-10-27T03:33:20Z


def meta(name, parent="", type_="DocumentType", **extra):
    return {"visibleName": name, "parent": parent, "type": type_,
            "lastModified": TS, **extra}


NOTES_CONTENT = {
    "fileType": "notebook", "formatVersion": 2, "orientation": "portrait",
    "cPages": {"pages": [
        {"id": PAGE2, "idx": {"value": "bb"}, "template": {"value": "P Lines small"}},
        {"id": PAGE1, "idx": {"value": "ba"}, "template": {"value": "Blank"}},
        {"id": PAGE3, "idx": {"value": "bc"}, "deleted": 1},
    ]},
    "tags": [{"name": "work", "timestamp": 1}],
}


def populate(cloud: FakeCloud) -> None:
    cloud.put_item(WORK, meta("Work", type_="CollectionType"))
    cloud.put_item(PROJECTS, meta("Projects", WORK, "CollectionType", pinned=True))
    cloud.put_item(NOTES, meta("Meeting notes", PROJECTS), NOTES_CONTENT, {
        f"{NOTES}/{PAGE1}.rm": b"reMarkable .lines file, version=6          page1",
        f"{NOTES}/{PAGE2}.rm": b"reMarkable .lines file, version=6          page2",
    })
    cloud.put_item(PAPER, meta("Paper.pdf"),
                   {"fileType": "pdf", "pageCount": 12, "formatVersion": 1, "pages": []},
                   {f"{PAPER}.pdf": b"%PDF-1.7 fake"})
    cloud.put_item(OLD, meta("Old meeting", "trash", bookmarked=True),
                   {"fileType": "notebook", "formatVersion": 1, "pages": [PAGE1]})
    cloud.put_item(ORPHAN, meta("Lost", "99999999-9999-4999-8999-999999999999"), {})


class ModelTests(unittest.TestCase):
    def test_metadata_parsing(self):
        m = Metadata.parse(json.dumps(meta("A", WORK, pinned=True, createdTime="0")))
        self.assertEqual((m.visible_name, m.parent, m.pinned), ("A", WORK, True))
        self.assertEqual(m.last_modified, datetime(2024, 10, 27, 3, 33, 20, tzinfo=timezone.utc))
        self.assertIsNone(m.created_time)
        self.assertTrue(Metadata.parse({"bookmarked": True}).pinned)
        self.assertTrue(Metadata.parse({"type": "CollectionType"}).is_collection)
        self.assertEqual(Metadata.parse({"lastModified": "2024-10-27T03:33:20Z"}).last_modified,
                         datetime(2024, 10, 27, 3, 33, 20, tzinfo=timezone.utc))

    def test_content_v2_pages_order_and_deletions(self):
        c = Content.parse(json.dumps(NOTES_CONTENT))
        self.assertEqual(c.file_type, FileType.NOTEBOOK)
        self.assertEqual(c.page_ids, [PAGE1, PAGE2])          # sorted by idx, deleted dropped
        self.assertEqual(c.pages[1].template, "P Lines small")
        self.assertEqual(c.page_count, 2)
        self.assertEqual(c.tags, ["work"])

    def test_pdf_page_redirects_and_last_opened_page(self):
        c = Content.parse({"fileType": "pdf", "formatVersion": 2, "cPages": {"pages": [
            {"id": PAGE1, "idx": {"value": "ba"}, "redir": {"value": 0}},
            {"id": PAGE2, "idx": {"value": "bb"}},                         # inserted page
            {"id": PAGE3, "idx": {"value": "bc"}, "redir": {"value": 1}}]}})
        self.assertEqual([p.redirect for p in c.pages], [0, None, 1])
        v1 = Content.parse({"formatVersion": 1, "pages": [PAGE1, PAGE2, PAGE3],
                            "redirectionPageMap": [0, -1]})
        self.assertEqual([p.redirect for p in v1.pages], [0, None, None])
        self.assertEqual(Metadata.parse({"lastOpenedPage": 4}).last_opened_page, 4)
        self.assertEqual(Metadata.parse({"lastOpenedPage": "x"}).last_opened_page, 0)

    def test_content_v1_and_unknown(self):
        c = Content.parse({"fileType": "epub", "formatVersion": 1, "pages": [PAGE1, PAGE2]})
        self.assertEqual((c.file_type, c.page_ids), (FileType.EPUB, [PAGE1, PAGE2]))
        self.assertEqual(Content.parse({"fileType": "weird"}).file_type, FileType.UNKNOWN)
        self.assertEqual(Content.parse(None).page_count, 0)

    def test_uuid_parsing(self):
        self.assertEqual(parse_uuid(" " + WORK.upper() + " "), WORK)
        with self.assertRaises(ValueError):
            parse_uuid("trash")

    def test_helpers_and_edge_cases(self):
        self.assertTrue(is_uuid(WORK))
        self.assertFalse(is_uuid("trash"))
        self.assertIsNone(parse_timestamp("yesterday"))
        with self.assertRaises(ValueError):
            Metadata.parse("[1, 2]")

    def test_tree_mutation_and_filters(self):
        folder = make_node(WORK, Metadata("Work", "CollectionType"))
        doc = make_node(NOTES, Metadata("Notes", parent=WORK))
        tree = DocumentTree([folder])
        self.assertEqual(tree.children(WORK), [])
        tree.add(doc)
        self.assertEqual(tree.children(WORK), [doc])
        self.assertEqual((tree.documents(), tree.folders()), ([doc], [folder]))
        tree.remove(WORK)
        self.assertEqual(tree.orphans, [NOTES])  # parent gone -> shown under root
        self.assertEqual(tree.children(), [doc])
        tree.remove("missing")  # no-op

    def test_new_file_helpers(self):
        from core.models import json_bytes, new_metadata, unique_name
        self.assertEqual(unique_name("Folder", set()), "Folder")
        self.assertEqual(unique_name("Folder", {"Folder", "Folder 2"}), "Folder 3")
        data = json_bytes({"visibleName": "Ünïcode", "b": [], "a": 1})
        # As Qt's QJsonDocument writes it (an empty array on two lines): parsing a file the
        # tablet wrote and writing it again gives the same bytes.
        self.assertEqual(data, '{\n    "a": 1,\n    "b": [\n    ],\n    "visibleName": "Ünïcode"\n}\n'.encode())
        self.assertNotIn("lastOpened", new_metadata("F", "", "CollectionType", "1"))

    def test_tree_cycle_is_safe(self):
        a = make_node(WORK, Metadata("A", "CollectionType", PROJECTS))
        b = make_node(PROJECTS, Metadata("B", "CollectionType", WORK))
        tree = DocumentTree([a, b])
        self.assertEqual(len(tree.breadcrumbs(WORK)), 2)
        self.assertEqual(list(tree.walk(WORK)), [(b, 0), (a, 1)])


class IndexTests(unittest.TestCase):
    H1, H2 = sha(b"one"), sha(b"two")

    def test_schema4(self):
        data = f"4\n0:.:2:30\n{self.H1}:80000000:{WORK}:3:10\n{self.H2}:0:x.pdf:0:20\n".encode()
        idx = parse_index(data)
        self.assertEqual(idx.schema, 4)
        self.assertEqual([(e.id, e.type, e.subfiles, e.size) for e in idx.entries],
                         [(WORK, "80000000", 3, 10), ("x.pdf", "0", 0, 20)])
        self.assertTrue(index_hash_matches(data, sha(data)))

    def test_schema3_hash_of_hashes(self):
        data = f"3\n{self.H2}:0:b:0:1\n{self.H1}:0:a:0:1\n".encode()
        self.assertEqual(len(parse_index(data).entries), 2)
        self.assertTrue(index_hash_matches(data, hash_of_hashes([("b", self.H2), ("a", self.H1)])))
        self.assertFalse(index_hash_matches(data, sha(b"nope")))

    def test_malformed(self):
        self.assertFalse(index_hash_matches(b"garbage", sha(b"x")))
        for bad in (b"", b"9\n", b"four\n", b"4\nnot:an:entry\n", b"3\nshort:0:a:0:1\n",
                    f"3\n{self.H1}:0:a:x:1\n".encode()):
            with self.assertRaises(SyncError, msg=bad):
                parse_index(bad)

    def test_root_info(self):
        r = RootInfo.parse(b'{"hash": "ab", "generation": "7", "schemaVersion": 4, '
                           b'"maxNetworkJobs": 3, "hasMissingHashes": true}')
        self.assertEqual((r.hash, r.generation, r.schema_version, r.max_network_jobs,
                          r.has_missing_hashes), ("ab", 7, 4, 3, True))
        for bad in (b"[]", b"{", b'{"hash": 5}', b'{"hash": "a", "generation": "x"}'):
            with self.assertRaises(SyncError):
                RootInfo.parse(bad)


class SyncTests(unittest.TestCase):
    schema = 4

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cache = base / "cache"
        self.cloud = FakeCloud(self.schema)
        populate(self.cloud)
        self.auth = paired_auth(base / "config", self.cloud)
        self.now = 1_800_000_000.0
        self.sleeps: list[float] = []
        self.sync = self.new_client()

    def tearDown(self):
        self.tmp.cleanup()

    def new_client(self) -> SyncClient:
        return SyncClient(self.auth, self.cache, max_workers=2, clock=lambda: self.now,
                          sleep=self.sleeps.append)

    def test_hierarchy(self):
        tree = self.sync.refresh()
        self.assertEqual(len(tree), 6)
        self.assertEqual([n.name for n in tree.children()], ["Work", "Lost", "Paper.pdf"])
        self.assertEqual([n.name for n in tree.children(WORK)], ["Projects"])
        self.assertEqual([n.name for n in tree.children("trash")], ["Old meeting"])
        self.assertEqual(tree.orphans, [ORPHAN])
        self.assertIsInstance(tree.get(PROJECTS), Folder)
        self.assertTrue(tree.get(PROJECTS).pinned)

        self.assertEqual([n.name for n in tree.breadcrumbs(NOTES)],
                         ["Work", "Projects", "Meeting notes"])
        self.assertEqual(tree.path(NOTES), "/Work/Projects/Meeting notes")
        self.assertEqual(tree.path(OLD), "trash:/Old meeting")
        self.assertEqual([(n.name, d) for n, d in tree.walk()],
                         [("Work", 0), ("Projects", 1), ("Meeting notes", 2),
                          ("Lost", 0), ("Paper.pdf", 0)])

        self.assertEqual([n.id for n in tree.search("MEETING")], [NOTES])
        self.assertEqual({n.id for n in tree.search("meeting", include_trash=True)}, {NOTES, OLD})
        self.assertEqual(tree.search("o", documents=False)[0].name, "Projects")

    def test_metadata_and_content_extraction(self):
        tree = self.sync.refresh()
        notes = tree.get(NOTES)
        self.assertIsInstance(notes, Document)
        self.assertEqual(notes.file_type, FileType.NOTEBOOK)
        self.assertEqual(notes.content.page_ids, [PAGE1, PAGE2])
        self.assertEqual(notes.last_modified.year, 2024)
        self.assertIn(f"{NOTES}/{PAGE1}.rm", notes.files)
        paper = tree.get(PAPER)
        self.assertEqual((paper.file_type, paper.page_count), (FileType.PDF, 12))
        self.assertTrue(tree.get(OLD).pinned)  # legacy "bookmarked"
        state = json.loads((self.cache / "state.json").read_text())
        self.assertEqual(state["root"]["generation"], self.cloud.generation)
        self.assertEqual(state["docs"][NOTES]["name"], "Meeting notes")

    def test_incremental_refresh_uses_cache(self):
        self.sync.refresh()
        first = len(self.cloud.blob_requests())
        self.assertGreater(first, 0)

        self.cloud.requests.clear()
        self.sync.refresh()
        self.assertEqual(self.cloud.blob_requests(), [])  # unchanged root: all cached

        self.cloud.put_item(PAPER, meta("Paper (renamed).pdf"),
                            {"fileType": "pdf", "pageCount": 12, "formatVersion": 1},
                            {f"{PAPER}.pdf": b"%PDF-1.7 fake"})
        self.cloud.requests.clear()
        tree = self.sync.refresh()
        # new root index + new doc index + new metadata + new content; the PDF is cached
        self.assertEqual(len(self.cloud.blob_requests()), 4)
        self.assertEqual(tree.get(PAPER).name, "Paper (renamed).pdf")

    def test_download_status_lifecycle(self):
        self.sync.refresh()
        self.assertEqual(self.sync.status(NOTES), SyncStatus.NOT_DOWNLOADED)

        path = self.sync.download_document(NOTES)
        self.assertEqual((path / f"{NOTES}/{PAGE1}.rm").read_bytes()[:33],
                         b"reMarkable .lines file, version=6")
        self.assertTrue((path / f"{NOTES}.content").exists())
        self.assertEqual(self.sync.status(NOTES), SyncStatus.SYNCED)
        rec = self.sync.state["docs"][NOTES]
        self.assertEqual(rec["downloaded_at"], "2027-01-15T08:00:00+00:00")

        # Local edit is detected, and survives a reload of the persisted state.
        (path / f"{NOTES}/{PAGE2}.rm").write_bytes(b"edited")
        self.assertEqual(self.new_client().status(NOTES), SyncStatus.LOCAL_MODIFIED)
        self.assertEqual(self.sync.local_changes(NOTES), [f"{NOTES}/{PAGE2}.rm"])

        # Re-download restores it; a later remote change makes it outdated.
        self.sync.download_document(NOTES)
        self.assertEqual(self.sync.status(NOTES), SyncStatus.SYNCED)
        self.cloud.put_item(NOTES, meta("Meeting notes v2", PROJECTS), NOTES_CONTENT)
        self.sync.refresh()
        self.assertEqual(self.sync.status(NOTES), SyncStatus.OUTDATED)
        # The new version dropped the page files; re-download removes stale ones.
        self.sync.download_document(NOTES)
        self.assertFalse((path / f"{NOTES}/{PAGE1}.rm").exists())

        # Remote removal: tracked as removed while the local copy exists.
        self.cloud.remove_item(NOTES)
        self.sync.refresh()
        self.assertEqual(self.sync.status(NOTES), SyncStatus.REMOVED_REMOTE)
        self.assertNotIn(NOTES, self.sync.tree)

    def test_prune_after_refresh_keeps_everything_in_use(self):
        self.sync.refresh()
        self.assertEqual(self.sync.prune_cache(), {"blobs": 0, "documents": 0})
        self.cloud.requests.clear()
        self.sync.refresh()
        self.assertEqual(self.cloud.blob_requests(), [])

    def test_prune_and_clear_cache(self):
        self.sync.refresh()
        self.sync.download_document(PAPER)
        self.sync.download_document(NOTES)
        pdf_blob = self.sync.tree.get(PAPER).files[f"{PAPER}.pdf"]

        self.cloud.remove_item(PAPER)
        self.sync.refresh()
        result = self.sync.prune_cache()
        self.assertEqual(result["documents"], 1)
        self.assertGreater(result["blobs"], 0)
        self.assertFalse(self.sync.local_dir(PAPER).exists())
        self.assertNotIn(PAPER, self.sync.state["docs"])
        self.assertFalse(self.sync.blobs.has(pdf_blob))
        self.assertLessEqual(self.sync.blobs.hashes(), self.sync.referenced_hashes())
        self.assertEqual(self.sync.status(NOTES), SyncStatus.SYNCED)  # still intact

        self.sync.clear_cache()
        self.assertEqual(self.sync.blobs.size(), 0)
        self.assertEqual(self.sync.status(NOTES), SyncStatus.NOT_DOWNLOADED)

    def test_prune_keeps_locally_modified_removed_docs(self):
        self.sync.refresh()
        path = self.sync.download_document(PAPER)
        (path / f"{PAPER}.pdf").write_bytes(b"my annotations")
        self.cloud.remove_item(PAPER)
        self.sync.refresh()
        self.assertEqual(self.sync.prune_cache()["documents"], 0)
        self.assertTrue(path.exists())

    def test_export_rmdoc(self):
        self.sync.refresh()
        out = self.sync.export_rmdoc(NOTES, Path(self.tmp.name) / "notes")
        self.assertEqual(out.suffix, ".rmdoc")
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(sorted(zf.namelist()), sorted([
                f"{NOTES}.content", f"{NOTES}.metadata",
                f"{NOTES}/{PAGE1}.rm", f"{NOTES}/{PAGE2}.rm"]))

    def test_integrity_and_unsafe_names(self):
        self.sync.refresh()
        self.cloud.corrupt(self.sync.tree.get(PAPER).files[f"{PAPER}.pdf"])
        with self.assertRaises(IntegrityError):
            self.sync.download_document(PAPER)
        self.assertFalse(BlobCache(self.cache).has(sha(b"%PDF-1.7 fake")))

        self.cloud.put_item(PAPER, meta("Evil"), {}, {"../../escape": b"x"})
        self.sync.refresh()
        with self.assertRaises(SyncError):
            self.sync.download_document(PAPER)
        self.assertFalse((self.cache / "escape").exists())

    def test_root_fallbacks(self):
        self.cloud.v4_available = False
        self.sync.refresh()
        self.assertIn("/sync/v3/root", self.cloud.requests)
        self.assertEqual(len(self.sync.tree), 6)

        empty = FakeCloud(self.schema)
        auth = paired_auth(Path(self.tmp.name) / "config2", empty)
        client = SyncClient(auth, Path(self.tmp.name) / "cache2", sleep=self.sleeps.append)
        self.assertEqual(len(client.refresh()), 0)
        self.assertTrue(client.root.is_empty)

    def test_bad_entry_is_skipped_not_fatal(self):
        self.cloud.put_item(PAPER, {"visibleName": "no metadata file"})
        self.cloud.docs[PAPER] = {f"{PAPER}.pdf": b"x"}
        self.cloud.publish()
        with self.assertLogs("rm.sync", "ERROR") as logs:
            tree = self.sync.refresh()
        self.assertIn("has no .metadata file", logs.output[0])
        self.assertNotIn(PAPER, tree)
        self.assertIn(NOTES, tree)

    def test_blob_requests_carry_rm_filename(self):
        sent = []
        def spy(method, url, headers, body, timeout):
            if "/sync/v3/files/" in url:
                sent.append((url.rsplit("/", 1)[1], headers.get("rm-filename")))
            return self.cloud(method, url, headers, body, timeout)
        self.auth.transport = spy
        self.sync.refresh()
        self.sync.download_document(NOTES)
        names = dict(sent)
        self.assertEqual(names[self.cloud.root_hash], "root.docSchema")
        self.assertEqual(names[self.sync.tree.get(NOTES).hash], f"{NOTES}.docSchema")
        for name, blob_hash in self.sync.tree.get(NOTES).files.items():
            self.assertEqual(names[blob_hash], name)
        self.assertTrue(all(n for _, n in sent))

    def test_fake_server_rejects_missing_rm_filename(self):
        # Guards the fake itself: it must reproduce the live 400.
        resp = self.auth.authorized_request("GET", f"/sync/v3/files/{self.cloud.root_hash}")
        self.assertEqual(resp.status, 400)
        self.assertIn(b"unexpected 'rm-filename'", resp.body)

    def test_routes_to_regional_gateway(self):
        self.sync.refresh()
        self.assertEqual(set(self.cloud.hosts), {"internal.cloud.remarkable.com"})

        cloud = FakeCloud(self.schema)
        populate(cloud)
        auth = paired_auth(Path(self.tmp.name) / "config-eu", cloud, tectonic="eu")
        SyncClient(auth, Path(self.tmp.name) / "cache-eu", sleep=self.sleeps.append).refresh()
        self.assertEqual(set(cloud.hosts), {"eu.tectonic.remarkable.com"})

    def test_folder_is_not_downloadable(self):
        self.sync.refresh()
        with self.assertRaises(SyncError):
            self.sync.download_document(WORK)
        with self.assertRaises(SyncError):
            self.sync.download_document("77777777-7777-4777-8777-777777777777")

    def test_server_errors(self):
        self.sync.refresh()
        doc = self.sync.tree.get(PAPER)
        pdf = doc.files[f"{PAPER}.pdf"]
        del self.cloud.blobs[pdf]
        with self.assertRaises(SyncError, msg="404 blob"):
            self.sync.download_document(PAPER)

        original = self.cloud.__class__.__call__
        self.auth.transport = lambda *a: HttpResponse(500, {}, b"boom")
        with self.assertRaisesRegex(SyncError, "HTTP 500"):
            self.sync.fetch_root()
        self.auth.transport = self.cloud
        del original

    def test_index_hash_mismatch_is_logged_not_fatal(self):
        # Tamper with an entry so the index matches neither hashing rule.
        root = self.cloud.blobs[self.cloud.root_hash]
        first_entry = root.decode().splitlines()[-1]
        self.cloud.blobs[self.cloud.root_hash] = root + first_entry.replace(
            first_entry.split(":")[2], "dup").encode() + b"\n"
        with self.assertLogs("rm.sync", "WARNING") as logs:
            self.sync.refresh()
        self.assertTrue(any("does not match its hash" in m for m in logs.output))
        self.assertEqual(len(self.sync.tree), 6)  # bogus "dup" entry skipped

    def test_server_job_limit_and_statuses(self):
        self.cloud.root_extra = {"maxNetworkJobs": 3}
        client = SyncClient(self.auth, self.cache, sleep=self.sleeps.append)
        client.refresh()
        self.assertEqual(client._workers(), 3)
        client.download_document(PAPER)
        statuses = client.statuses()
        self.assertEqual(statuses[PAPER], SyncStatus.SYNCED)
        self.assertEqual(statuses[NOTES], SyncStatus.NOT_DOWNLOADED)

    def test_untracked_local_file_and_forgotten_removals(self):
        self.sync.refresh()
        path = self.sync.download_document(PAPER)
        (path / "scratch.txt").write_text("mine")
        self.assertEqual(self.sync.local_changes(PAPER), ["scratch.txt"])
        # Removed remotely but never downloaded: dropped from state entirely.
        self.cloud.remove_item(ORPHAN)
        self.sync.refresh()
        self.assertNotIn(ORPHAN, self.sync.state["docs"])

    def test_blob_cache_basics(self):
        cache = BlobCache(Path(self.tmp.name) / "fresh")
        self.assertEqual((cache.hashes(), cache.size()), (set(), 0))
        with self.assertRaises(ValueError):
            cache.get("../../etc/passwd")
        h = sha(b"data")
        cache.put(h, b"data")
        cache.put(h, b"ignored: blobs are immutable")
        self.assertEqual((cache.get(h), cache.size()), (b"data", 4))


class NetworkResilienceTests(unittest.TestCase):
    """Transient failures are retried; whatever is left is a SyncError."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud = FakeCloud(4)
        populate(self.cloud)
        self.failures: list = []      # queued exceptions/responses to inject before the fake
        self.auth = paired_auth(Path(self.tmp.name) / "config", self.flaky)
        self.sleeps: list[float] = []
        self.sync = SyncClient(self.auth, Path(self.tmp.name) / "cache", max_workers=1,
                               sleep=self.sleeps.append)

    def tearDown(self):
        self.tmp.cleanup()

    def flaky(self, method, url, headers, body, timeout):
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return self.cloud(method, url, headers, body, timeout)

    def test_timeouts_are_retried_then_succeed(self):
        self.failures = [TimeoutError("The read operation timed out"),
                         ConnectionResetError("reset by peer")]
        self.assertEqual(self.sync.fetch_root().hash, self.cloud.root_hash)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_persistent_timeout_becomes_sync_error(self):
        self.failures = [TimeoutError("timed out")] * 10
        with self.assertRaises(SyncError) as ctx:
            self.sync.get_blob(self.cloud.root_hash, "root.docSchema")
        self.assertNotIsInstance(ctx.exception, TimeoutError)
        self.assertIn("TimeoutError", str(ctx.exception))
        self.assertIn("4 attempts", str(ctx.exception))
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_http_protocol_errors_and_server_errors_are_retried(self):
        import http.client
        self.failures = [http.client.IncompleteRead(b"partial"),
                         HttpResponse(503, {}, b"busy"),
                         HttpResponse(429, {"Retry-After": "7"}, b"")]
        self.assertEqual(self.sync.fetch_root().hash, self.cloud.root_hash)
        self.assertEqual(self.sleeps, [1.0, 2.0, 7.0])

    def test_client_errors_are_not_retried(self):
        self.failures = [HttpResponse(403, {}, b"forbidden")]
        with self.assertRaisesRegex(SyncError, "HTTP 403"):
            self.sync.fetch_root()
        self.assertEqual(self.sleeps, [])

    def test_refresh_survives_a_flaky_entry(self):
        def flaky_for_one(method, url, headers, body, timeout):
            if headers.get("rm-filename") == f"{NOTES}.docSchema":
                raise TimeoutError("timed out")
            return self.cloud(method, url, headers, body, timeout)
        self.auth.transport = flaky_for_one
        with self.assertLogs("rm.sync", "ERROR"):
            tree = self.sync.refresh()
        self.assertNotIn(NOTES, tree)      # skipped after retries, not fatal
        self.assertIn(PAPER, tree)


class CachedLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        self.cloud = FakeCloud(4)
        populate(self.cloud)
        self.auth = paired_auth(Path(self.tmp.name) / "config", self.cloud)

    def tearDown(self):
        self.tmp.cleanup()

    def offline_client(self) -> SyncClient:
        def no_network(*args):
            raise AssertionError("network used while loading from cache")
        self.auth.transport = no_network
        return SyncClient(self.auth, self.cache)

    def test_load_cached_rebuilds_tree_without_network(self):
        online = SyncClient(self.auth, self.cache)
        online.refresh()
        tree = self.offline_client().load_cached()
        self.assertEqual({n.id for n in tree.nodes.values()}, set(online.tree.nodes))
        self.assertEqual(tree.path(NOTES), "/Work/Projects/Meeting notes")

    def test_missing_blobs_are_skipped_and_misses_raise(self):
        online = SyncClient(self.auth, self.cache)
        online.refresh()
        meta_hash = online.tree.get(PAPER).files[f"{PAPER}.metadata"]
        online.blobs.prune(online.referenced_hashes() - {meta_hash})
        client = self.offline_client()
        with self.assertLogs("rm.sync", "ERROR"):
            tree = client.load_cached()
        self.assertNotIn(PAPER, tree)
        self.assertIn(NOTES, tree)
        with self.assertRaises(NotCachedError):
            client.get_blob(meta_hash, f"{PAPER}.metadata", cached_only=True)

    def test_empty_cache(self):
        tree = self.offline_client().load_cached()
        self.assertEqual(len(tree), 0)


class SyncSchema3Tests(SyncTests):
    """Same scenarios with schema-3 indexes (hash-of-hashes)."""

    schema = 3


# ------------------------------------------------------------------------ writing

def pdf_bytes(pages: int = 3) -> bytes:
    from pypdf import PdfWriter
    import io
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def epub_bytes() -> bytes:
    import io
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", "<container/>")
    return out.getvalue()


class ProtocolHelperTests(unittest.TestCase):
    def test_crc32c_and_goog_hash(self):
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)          # the standard check value
        self.assertEqual(_crc32c_python(b"123456789"), 0xE3069283)
        for size in (0, 1, 65535, 65536, 65537, 200_000):           # across kernel chunk edges
            data = bytes(range(256)) * (size // 256) + bytes(size % 256)
            self.assertEqual(crc32c(data), _crc32c_python(data), size)
        self.assertEqual(goog_hash(b"123456789"), "crc32c=4waSgw==")
        from unittest import mock
        with mock.patch("core.sync._crc32c_kernel", side_effect=OSError("no AF_ALG")):
            self.assertEqual(crc32c(b"123456789"), 0xE3069283)

    def test_build_index_round_trips_under_both_schemas(self):
        rows = [IndexEntry(sha(b"b"), "0", "x/b.rm", 0, 1), IndexEntry(sha(b"a"), "0", "x.content", 0, 2)]
        for schema in (3, 4):
            index_hash, data = build_index(schema, rows, "x")
            parsed = parse_index(data)
            self.assertEqual((parsed.schema, [e.id for e in parsed.entries]), (schema, ["x.content", "x/b.rm"]))
            self.assertTrue(index_hash_matches(data, index_hash))
            self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(build_index(3, rows)[0], hash_of_hashes([(e.id, e.hash) for e in rows]))
        self.assertTrue(build_index(4, rows, "x")[1].splitlines()[1].startswith(b"0:x:2:3"))

    def test_sniff_import(self):
        self.assertEqual(sniff_import(Path("a.PDF"), b"%PDF-1.7 ..."), FileType.PDF)
        self.assertEqual(sniff_import(Path("b.epub"), epub_bytes()), FileType.EPUB)
        for name, data in (("c.pdf", b"not a pdf"), ("d.txt", b"%PDF-1.7"), ("e.epub", b"PK\x03\x04junk"),
                           ("f.epub", pdf_bytes())):
            with self.assertRaisesRegex(ValueError, "not a PDF or EPUB"):
                sniff_import(Path(name), data)
        self.assertEqual(pdf_page_count(pdf_bytes(4)), 4)
        with self.assertLogs("pypdf", "WARNING"):
            self.assertEqual(pdf_page_count(b"%PDF-1.7 truncated"), 0)


class WriteTests(unittest.TestCase):
    """Imports, folders, renames, moves, favorites and the trash, against a fake cloud
    that checks the upload protocol; everything is read back by a second client."""

    schema = 4

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cloud = FakeCloud(self.schema)
        populate(self.cloud)
        self.now = 1_800_000_000.0
        self.sync = SyncClient(paired_auth(self.base / "config", self.cloud), self.base / "cache",
                               max_workers=2, clock=lambda: self.now, sleep=lambda s: None)
        self.sync.refresh()

    def tearDown(self):
        self.tmp.cleanup()

    def remote(self) -> DocumentTree:
        """The cloud as another device sees it."""
        n = len(list(self.base.glob("reader*")))
        return SyncClient(paired_auth(self.base / f"reader{n}" / "config", self.cloud),
                          self.base / f"reader{n}" / "cache", max_workers=2).refresh()

    def file(self, name: str, data: bytes) -> Path:
        path = self.base / name
        path.write_bytes(data)
        return path

    def root_puts(self) -> list[dict]:
        return [h for p, h in self.cloud.puts() if p == "/sync/v3/root"]

    def test_import_pdf(self):
        data = pdf_bytes(3)
        progress = []
        doc_id = self.sync.import_file(self.file("Quarterly report.pdf", data), WORK,
                                       progress=lambda done, total: progress.append((done, total)))
        self.assertTrue(is_uuid(doc_id))
        remote = self.remote()
        doc = remote.get(doc_id)
        self.assertEqual((doc.name, doc.parent_id, doc.file_type, doc.page_count), ("Quarterly report", WORK, FileType.PDF, 3))
        self.assertEqual(sorted(doc.files), sorted(f"{doc_id}{s}" for s in (".pdf", ".content", ".metadata")))
        self.assertEqual(self.cloud.item(doc_id)[f"{doc_id}.pdf"], data)
        meta_raw = self.cloud.metadata(doc_id)
        self.assertEqual(meta_raw, {"createdTime": "1800000000000", "lastModified": "1800000000000",
                                    "lastOpened": "0", "lastOpenedPage": 0, "parent": WORK,
                                    "pinned": False, "type": "DocumentType", "visibleName": "Quarterly report"})
        content = json.loads(self.cloud.item(doc_id)[f"{doc_id}.content"])
        self.assertEqual((content["fileType"], content["sizeInBytes"]), ("pdf", str(len(data))))
        self.assertEqual(progress[-1], (progress[-1][1], progress[-1][1]))     # content blobs, all uploaded
        # The client's own tree is current and fully cached: no re-download needed.
        self.assertEqual(self.sync.tree.get(doc_id).name, "Quarterly report")
        self.assertEqual(self.sync.get_blob(doc.files[f"{doc_id}.pdf"], "x", cached_only=True), data)
        self.assertEqual(self.sync.state["root"]["hash"], self.cloud.root_hash)
        self.assertEqual(remote.get(PAPER).name, "Paper.pdf")                   # other items untouched

    def test_import_epub_and_rejections(self):
        doc_id = self.sync.import_file(self.file("Novel.epub", epub_bytes()), name="A Novel")
        doc = self.remote().get(doc_id)
        self.assertEqual((doc.name, doc.parent_id, doc.file_type, doc.page_count), ("A Novel", "", FileType.EPUB, 0))
        self.assertIn(f"{doc_id}.epub", doc.files)
        generation = self.cloud.generation
        with self.assertRaisesRegex(ValueError, "not a PDF or EPUB"):
            self.sync.import_file(self.file("notes.txt", b"hello"))
        with self.assertRaisesRegex(SyncError, "destination folder"):
            self.sync.import_file(self.file("x.pdf", pdf_bytes(1)), NOTES)     # a document, not a folder
        with self.assertRaisesRegex(SyncError, "destination folder"):
            self.sync.import_file(self.file("y.pdf", pdf_bytes(1)), OLD)       # in the trash
        self.assertEqual(self.cloud.generation, generation)                    # nothing committed

    def test_upload_protocol(self):
        self.sync.create_folder("Inbox")
        puts = self.cloud.puts()
        files = [(p, h) for p, h in puts if p.startswith("/sync/v3/files/")]
        names = [h["rm-filename"] for _, h in files]
        # content first, then the folder's index, then the root index; then the root hash
        self.assertEqual(len(files), 4)
        self.assertTrue(names[0].endswith((".content", ".metadata")) and names[1].endswith((".content", ".metadata")))
        self.assertTrue(names[2].endswith(".docSchema") and names[2] != "root.docSchema")
        self.assertEqual(names[3], "root.docSchema")
        self.assertTrue(all(h["x-goog-hash"].startswith("crc32c=") for _, h in files))
        self.assertEqual([p for p, _ in puts][-1], "/sync/v3/root")
        self.assertNotIn("/sync/v4/root", [p for p, _ in puts])

    def test_root_commit_sends_the_base_generation(self):
        bodies = []
        real = self.cloud._put

        def spy(path, headers, body):
            if path == "/sync/v3/root":
                bodies.append(json.loads(body))
            return real(path, headers, body)
        self.cloud._put = spy
        generation = self.cloud.generation
        self.sync.create_folder("Inbox")
        self.assertEqual(bodies, [{"generation": generation, "hash": self.cloud.root_hash}])
        self.assertEqual(self.sync.root.generation, generation + 1)

    def test_folders_rename_move_and_favorite(self):
        inbox = self.sync.create_folder("Inbox", WORK)
        self.sync.rename(NOTES, "Standup notes")
        self.sync.move([NOTES, PAPER], inbox)
        self.sync.set_pinned([PAPER], True)
        remote = self.remote()
        self.assertEqual(remote.get(inbox).parent_id, WORK)
        self.assertTrue(remote.get(inbox).is_folder)
        self.assertEqual(json.loads(self.cloud.item(inbox)[f"{inbox}.content"]), {"tags": []})
        self.assertEqual(remote.get(NOTES).name, "Standup notes")
        self.assertEqual({remote.get(NOTES).parent_id, remote.get(PAPER).parent_id}, {inbox})
        self.assertTrue(remote.get(PAPER).pinned)
        notes = self.cloud.item(NOTES)
        self.assertEqual(notes[f"{NOTES}/{PAGE1}.rm"], b"reMarkable .lines file, version=6          page1")
        self.assertEqual(self.cloud.metadata(NOTES)["lastModified"], "1800000000000")
        self.assertEqual(remote.get(NOTES).content.tags, ["work"])                # other files kept

    def test_unknown_metadata_keys_are_kept(self):
        self.cloud.put_item(PAPER, meta("Paper.pdf", futureKey={"a": 1}, source="com.remarkable.ios"),
                            {"fileType": "pdf"}, {f"{PAPER}.pdf": b"%PDF-1.7 fake"})
        self.sync.rename(PAPER, "Paper (final)")
        raw = self.cloud.metadata(PAPER)
        self.assertEqual((raw["futureKey"], raw["source"], raw["visibleName"]),
                         ({"a": 1}, "com.remarkable.ios", "Paper (final)"))

    def test_moves_into_a_folder_s_own_subtree_are_refused(self):
        with self.assertRaisesRegex(SyncError, "into itself"):
            self.sync.move([WORK], PROJECTS)
        with self.assertRaisesRegex(SyncError, "into itself"):
            self.sync.move([WORK], WORK)
        with self.assertRaisesRegex(SyncError, "destination folder"):
            self.sync.move([PAPER], "99999999-9999-4999-8999-999999999999")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.sync.rename(PAPER, "  ")
        with self.assertRaisesRegex(ValueError, "name"):
            self.sync.create_folder("")

    def test_trash_lifecycle(self):
        self.sync.trash([PROJECTS, PAPER])
        remote = self.remote()
        self.assertTrue(remote.is_trashed(NOTES))                                # inside a trashed folder
        self.assertEqual(remote.get(PAPER).parent_id, "trash")
        self.sync.restore([PAPER])
        self.assertEqual(self.remote().get(PAPER).parent_id, "")                 # back to My files
        self.sync.delete([PROJECTS])
        remote = self.remote()
        self.assertNotIn(PROJECTS, remote)
        self.assertNotIn(NOTES, remote)                                          # contents go too
        self.assertIn(OLD, remote)
        self.sync.empty_trash()
        remote = self.remote()
        self.assertNotIn(OLD, remote)
        self.assertEqual(sorted(n.name for n in remote.nodes.values()), ["Lost", "Paper.pdf", "Work"])

    def test_no_op_changes_commit_nothing(self):
        generation = self.cloud.generation
        self.sync.set_pinned([PROJECTS], True)                                   # already pinned
        self.sync.trash([OLD])                                                   # already trashed
        self.sync.move([PAPER], "")                                              # already there
        self.sync.delete(["77777777-7777-4777-8777-777777777777"])               # never existed
        self.sync.move(["77777777-7777-4777-8777-777777777777"], WORK)
        from core.sync import ItemChange
        node = self.sync.tree.get(PAPER)                                         # same files: same root
        files = {e.id: e for e in self.sync.get_index(node.hash, f"{PAPER}.docSchema").entries}
        self.sync._commit("rewrite", lambda tree: [ItemChange(PAPER, files)])
        self.assertEqual(self.cloud.generation, generation)
        self.assertEqual(self.root_puts(), [])

    def test_unexpected_root_reply_is_an_error(self):
        real = self.cloud._put

        def wrong_hash(path, headers, body):
            resp = real(path, headers, body)
            if path == "/sync/v3/root":
                return HttpResponse(200, {}, json.dumps({"hash": "0" * 64, "generation": 99}).encode())
            return resp
        self.cloud._put = wrong_hash
        with self.assertRaisesRegex(SyncError, "root commit answered with hash 000000000000"):
            self.sync.create_folder("Inbox")

    def test_conflict_is_retried_on_the_new_root(self):
        for status in (412, 409):
            with self.subTest(status=status):
                self.cloud.conflict_status = status
                other = f"7777777{status % 10}-7777-4777-8777-777777777777"
                calls = []

                def another_device():
                    if not calls:
                        self.cloud.put_item(other, meta(f"From the tablet {status}"))   # commits first
                    calls.append(1)
                self.cloud.before_commit = another_device
                with self.assertLogs("rm.sync", "WARNING") as logs:
                    doc_id = self.sync.import_file(self.file(f"r{status}.pdf", pdf_bytes(1)))
                self.cloud.before_commit = None
                self.assertIn("is stale (another device synced); retrying", logs.output[0])
                self.assertEqual(len(calls), 2)                                  # refused once, then accepted
                remote = self.remote()
                self.assertIn(doc_id, remote)
                self.assertIn(other, remote)                                     # nobody's change was lost

    def test_retry_re_applies_the_change_to_the_newer_state(self):
        calls = []

        def another_device():
            if not calls:
                self.cloud.put_item(PAPER, meta("Paper.pdf", pinned=True, extra="tablet"),
                                    {"fileType": "pdf"}, {f"{PAPER}.pdf": b"%PDF-1.7 fake"})
            calls.append(1)
        self.cloud.before_commit = another_device
        with self.assertLogs("rm.sync", "WARNING"):
            self.sync.rename(PAPER, "Paper v2")
        raw = self.cloud.metadata(PAPER)
        self.assertEqual((raw["visibleName"], raw["pinned"], raw["extra"]), ("Paper v2", True, "tablet"))

    def test_persistent_conflict_gives_up(self):
        self.cloud.before_commit = lambda: self.cloud.publish()                 # always one step ahead
        with self.assertRaisesRegex(ConflictError, "gave up after 4 attempts"), \
                self.assertLogs("rm.sync", "WARNING") as logs:
            self.sync.create_folder("Never")
        self.assertEqual(len(logs.output), 4)
        self.assertEqual(len(self.root_puts()), 4)                               # 1 + 3 retries

    def test_transient_errors_during_upload_are_retried(self):
        self.cloud.fail_next = [503]
        self.sync.create_folder("Inbox")                                         # the root GET is retried
        self.assertIn("Inbox", [n.name for n in self.remote().nodes.values()])
        real = self.cloud._put
        failures = [503, 429]

        def flaky(path, headers, body):
            if path.startswith("/sync/v3/files/") and failures:
                from core.auth import HttpResponse
                return HttpResponse(failures.pop(0), {}, b"")
            return real(path, headers, body)
        self.cloud._put = flaky
        self.sync.create_folder("Outbox")
        self.assertIn("Outbox", [n.name for n in self.remote().nodes.values()])

    def test_rejected_upload_is_an_error_and_nothing_is_committed(self):
        real = self.cloud._put

        def bad_checksum(path, headers, body):
            return real(path, {**headers, "x-goog-hash": "crc32c=AAAAAA=="}, body)
        self.cloud._put = bad_checksum
        generation = self.cloud.generation
        with self.assertRaisesRegex(SyncError, "invalid checksum"):
            self.sync.create_folder("Inbox")
        self.assertEqual(self.cloud.generation, generation)

    def test_items_this_client_cannot_read_are_kept(self):
        from fakecloud import index_rows
        self.cloud.corrupt(sha(self.cloud.docs[PAPER][f"{PAPER}.metadata"]))
        sync = SyncClient(paired_auth(self.base / "fresh", self.cloud), self.base / "fresh-cache",
                          sleep=lambda s: None)
        with self.assertLogs("rm.sync", "ERROR"):
            sync.refresh()
            self.assertNotIn(PAPER, sync.tree)                   # this client cannot load it...
            sync.create_folder("Inbox")
        self.assertIn(PAPER, [row[2] for row in index_rows(self.cloud.blobs[self.cloud.root_hash])])   # ...and keeps it

    def test_import_into_an_empty_account(self):
        self.cloud = FakeCloud(3)                     # no root yet (404): the client writes schema 3
        sync = SyncClient(paired_auth(self.base / "empty", self.cloud), self.base / "empty-cache",
                          sleep=lambda s: None)
        doc_id = sync.import_file(self.file("first.pdf", pdf_bytes(1)))
        self.assertEqual(list(self.remote().nodes), [doc_id])
        self.assertEqual(self.cloud.generation, 1)


class WriteSchema3Tests(WriteTests):
    """The same with schema-3 indexes (hash-of-hashes), as on the live account."""

    schema = 3


if __name__ == "__main__":
    unittest.main()
