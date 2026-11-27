"""Offline tests for missing hashes, check-files and deletedImmutableAction (ARCHITECTURE.md 3.8).

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakecloud import ABSENT, FakeCloud, paired_auth, sha  # noqa: E402

from core.auth import HttpResponse  # noqa: E402
from core.sync import (  # noqa: E402
    DELETED_IMMUTABLE_ACTIONS, REQUEST_REASONS, NetworkError, RootInfo, SyncClient, SyncError,
    SyncStatus, parse_deleted_immutable_action, parse_missing_hashes)

PDF = "aaaaaaaa-1111-4111-8111-111111111111"
EPUB = "bbbbbbbb-2222-4222-8222-222222222222"
NOTE = "cccccccc-3333-4333-8333-333333333333"
PAGE = "dddddddd-0000-4000-8000-000000000001"
TS = "1730000000000"

PDF_BYTES = b"%PDF-1.7 synthetic pdf"
EPUB_BYTES = b"PK\x03\x04 synthetic epub"
PAGE_BYTES = b"reMarkable .lines file, version=6          synthetic"


def meta(name: str) -> dict:
    return {"visibleName": name, "parent": "", "type": "DocumentType", "lastModified": TS}


def root_body(**extra) -> bytes:
    return json.dumps({"hash": "", "generation": 1, **extra}).encode()


class Gate:
    """Transport in front of a FakeCloud that can answer chosen paths itself."""

    def __init__(self, cloud: FakeCloud) -> None:
        self.cloud = cloud
        self.replies: dict[str, HttpResponse] = {}     # path -> fixed answer
        self.put_status: int | None = None             # answer every blob/root PUT with this

    def __call__(self, method, url, headers, body, timeout):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        if path in self.replies:
            return self.replies[path]
        if method == "PUT" and self.put_status is not None:
            return HttpResponse(self.put_status, {}, b"")
        return self.cloud(method, url, headers, body, timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cache = base / "cache"
        self.cloud = FakeCloud(4)
        self.gate = Gate(self.cloud)
        self.cloud.put_item(PDF, meta("Paper.pdf"), {"fileType": "pdf", "pageCount": 1, "formatVersion": 1,
                                                     "pages": []}, {f"{PDF}.pdf": PDF_BYTES})
        self.cloud.put_item(EPUB, meta("Book.epub"), {"fileType": "epub", "formatVersion": 1, "pages": []},
                            {f"{EPUB}.epub": EPUB_BYTES})
        self.cloud.put_item(NOTE, meta("Notes"), {"fileType": "notebook", "formatVersion": 2, "pages": []},
                            {f"{NOTE}/{PAGE}.rm": PAGE_BYTES})
        self.auth = paired_auth(base / "config", self.gate)
        self.sleeps: list[float] = []
        self.sync = self.new_client()

    def tearDown(self):
        self.tmp.cleanup()

    def new_client(self) -> SyncClient:
        return SyncClient(self.auth, self.cache, max_workers=2, clock=lambda: 1_800_000_000.0,
                          sleep=self.sleeps.append)

    def file_hash(self, doc_id: str, name: str) -> str:
        return sha(self.cloud.docs[doc_id][name])

    def local(self, doc_id: str, name: str) -> Path:
        return self.cache / "documents" / doc_id / name

    def puts_of(self, blob_hash: str) -> list[dict]:
        return [h for p, h in self.cloud.puts() if p.endswith("/" + blob_hash)]


class ParsingTests(unittest.TestCase):
    def test_each_action_name_is_parsed(self):
        for name in DELETED_IMMUTABLE_ACTIONS:
            self.assertEqual(RootInfo.parse(root_body(deletedImmutableAction=name)).deleted_immutable_action, name)
        self.assertEqual(DELETED_IMMUTABLE_ACTIONS, ("None", "FailEntry", "Rollback", "ReDownload"))

    def test_action_is_case_insensitive(self):
        self.assertEqual(RootInfo.parse(root_body(deletedImmutableAction="redownload")).deleted_immutable_action,
                         "ReDownload")
        self.assertEqual(RootInfo.parse(root_body(deletedImmutableAction="FAILENTRY")).deleted_immutable_action,
                         "FailEntry")
        self.assertEqual(parse_deleted_immutable_action("rOLLBACK"), "Rollback")

    def test_unknown_or_non_string_action_is_none_and_logged(self):
        for value in ("Delete", "Nonee", "Non", "", 3, None, ["None"], True):
            with self.subTest(value=value):
                with self.assertLogs("rm.sync", "WARNING") as logs:
                    info = RootInfo.parse(root_body(deletedImmutableAction=value))
                self.assertIsNone(info.deleted_immutable_action)
                self.assertIn("Unable to parse DeletedImmutableAction from JSON", logs.output[0])

    def test_absent_action_is_none_without_warning(self):
        with self.assertNoLogs("rm.sync", "WARNING"):
            self.assertIsNone(RootInfo.parse(root_body()).deleted_immutable_action)

    def test_has_missing_hashes(self):
        self.assertTrue(RootInfo.parse(root_body(hasMissingHashes=True)).has_missing_hashes)
        self.assertFalse(RootInfo.parse(root_body(hasMissingHashes=False)).has_missing_hashes)
        self.assertFalse(RootInfo.parse(root_body()).has_missing_hashes)

    def test_parse_missing_hashes(self):
        h1, h2 = sha(b"one"), sha(b"two")
        body = json.dumps({"hashes": [h1, h2.upper()]}).encode()
        self.assertEqual(parse_missing_hashes(body), {h1, h2})
        self.assertEqual(parse_missing_hashes(b'{"hashes": []}'), set())

    def test_parse_missing_hashes_skips_bad_items_and_zero_hash(self):
        good = sha(b"one")
        items = ["zz" * 32, "ab" * 31, "ab" * 33, 5, None, good, "0" * 64]
        with self.assertLogs("rm.sync", "WARNING") as logs:
            result = parse_missing_hashes(json.dumps({"hashes": items}).encode())
        self.assertEqual(result, {good})
        self.assertEqual(len(logs.output), 5)
        self.assertIn("Failed to parse missing hash from server", logs.output[0])

    def test_parse_missing_hashes_errors(self):
        for body in (b"{", b"", b"[]", b"3", b'{}', b'{"hashes": "x"}', b'{"hashes": {"a": 1}}', b"\xff\xfe"):
            with self.subTest(body=body):
                with self.assertRaises(SyncError) as ctx:
                    parse_missing_hashes(body)
                self.assertIn("in download missing hashes", str(ctx.exception))


class PersistenceTests(Base):
    def test_default_is_none(self):
        self.assertEqual(self.sync.deleted_immutable_action(), "None")
        self.sync.refresh()
        self.assertEqual(self.sync.deleted_immutable_action(), "None")
        self.assertNotIn("deleted_immutable_action", self.sync.state["root"])

    def test_change_is_logged_and_stored(self):
        self.cloud.deleted_immutable_action = "redownload"
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertTrue(any("cloud signalled deletedImmutableAction change: None -> ReDownload" in line
                            for line in logs.output))
        self.assertEqual(self.sync.deleted_immutable_action(), "ReDownload")
        self.assertEqual(self.sync.state["root"]["deleted_immutable_action"], "ReDownload")

    def test_same_value_is_not_logged_again(self):
        self.cloud.deleted_immutable_action = "Rollback"
        self.sync.refresh()
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertFalse(any("signalled" in line for line in logs.output))
        self.cloud.deleted_immutable_action = "FailEntry"
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertTrue(any("Rollback -> FailEntry" in line for line in logs.output))

    def test_absent_keeps_previous(self):
        self.cloud.deleted_immutable_action = "FailEntry"
        self.sync.refresh()
        self.cloud.deleted_immutable_action = ABSENT
        self.sync.refresh()
        self.assertEqual(self.sync.deleted_immutable_action(), "FailEntry")
        self.assertEqual(self.sync.state["root"]["deleted_immutable_action"], "FailEntry")

    def test_invalid_keeps_previous(self):
        self.cloud.deleted_immutable_action = "FailEntry"
        self.sync.refresh()
        self.cloud.deleted_immutable_action = "bogus"
        with self.assertLogs("rm.sync", "WARNING"):
            self.sync.refresh()
        self.assertEqual(self.sync.deleted_immutable_action(), "FailEntry")

    def test_survives_reload_from_state_json(self):
        self.cloud.deleted_immutable_action = "ReDownload"
        self.sync.refresh()
        self.assertEqual(self.new_client().deleted_immutable_action(), "ReDownload")
        self.cloud.deleted_immutable_action = ABSENT
        client = self.new_client()
        client.refresh()
        self.assertEqual(self.new_client().deleted_immutable_action(), "ReDownload")

    def test_absent_survives_deepcopy_of_the_cloud(self):
        self.assertIs(self.cloud.deleted_immutable_action, ABSENT)
        clone = copy.deepcopy(self.cloud)
        self.assertIs(clone.deleted_immutable_action, ABSENT)
        response = clone("GET", "https://sync.example/sync/v4/root",
                         {"Authorization": "Bearer synthetic"}, None, 10)
        self.assertEqual(response.status, 200)
        self.assertNotIn("deletedImmutableAction", json.loads(response.body))

    def test_unknown_stored_value_reads_as_none(self):
        self.sync.refresh()
        self.sync.state["root"]["deleted_immutable_action"] = "bogus"
        self.assertEqual(self.sync.deleted_immutable_action(), "None")


class MissingHashesTests(Base):
    def test_no_request_without_flag(self):
        self.cloud.root_extra = {"hasMissingHashes": False}
        self.sync.refresh()
        self.assertNotIn("/sync/v3/missing", self.cloud.requests)
        self.assertIsNone(self.sync.last_missing_hashes)
        self.cloud.root_extra = {}
        self.sync.refresh()
        self.assertNotIn("/sync/v3/missing", self.cloud.requests)

    def test_flag_makes_a_get_request(self):
        self.cloud.missing_hashes = []
        self.sync.refresh()
        self.assertIn("/sync/v3/missing", self.cloud.requests)
        self.assertIn(("GET", "/sync/v3/missing"), [(m, p) for m, p, _ in self.cloud.calls])
        self.assertIsNone(self.sync.last_missing_hashes)             # an empty answer does nothing

    def test_fetch_missing_hashes(self):
        h = sha(b"x")
        self.cloud.missing_hashes = [h, "0" * 64]
        self.assertEqual(self.sync.fetch_missing_hashes(), {h})

    def test_cached_blobs_are_reuploaded_with_their_names(self):
        self.sync.refresh()
        content_name = f"{PDF}.content"
        h_content = self.file_hash(PDF, content_name)
        h_index = self.sync.tree.get(PDF).hash
        h_root = self.sync.root.hash
        for h in (h_content, h_index, h_root):
            data = self.cloud.blobs.pop(h)
            self.cloud.filenames.pop(h)
            self.assertNotIn(h, self.cloud.blobs)
            self.assertTrue(data)
        self.cloud.missing_hashes = [h_content, h_index.upper(), h_root]
        tree = self.sync.refresh()
        self.assertIn(PDF, tree)
        for h, name in ((h_content, content_name), (h_index, f"{PDF}.docSchema"), (h_root, "root.docSchema")):
            with self.subTest(name=name):
                self.assertIn(h, self.cloud.blobs)
                puts = self.puts_of(h)
                self.assertEqual(len(puts), 1)
                self.assertEqual(puts[0]["rm-filename"], name)
        self.assertEqual(self.sync.last_missing_hashes,
                         {"reuploaded": sorted([h_content, h_index, h_root]), "not_found": []})

    def test_hash_only_in_local_copy_is_reuploaded(self):
        self.sync.refresh()
        self.sync.download_document(PDF)
        h_pdf = self.file_hash(PDF, f"{PDF}.pdf")
        self.sync.blobs.clear()
        self.sync.refresh()                      # indexes, metadata and content cached again, not the PDF
        self.assertFalse(self.sync.blobs.has(h_pdf))
        self.cloud.blobs.pop(h_pdf)
        self.cloud.missing_hashes = [h_pdf]
        self.sync.refresh()
        self.assertEqual(self.cloud.blobs[h_pdf], PDF_BYTES)
        self.assertEqual(self.puts_of(h_pdf)[0]["rm-filename"], f"{PDF}.pdf")
        self.assertEqual(self.sync.last_missing_hashes, {"reuploaded": [h_pdf], "not_found": []})

    def test_edited_local_copy_is_not_uploaded(self):
        self.sync.refresh()
        self.sync.download_document(PDF)
        h_pdf = self.file_hash(PDF, f"{PDF}.pdf")
        self.sync.blobs.clear()
        self.sync.refresh()
        self.local(PDF, f"{PDF}.pdf").write_bytes(b"edited")
        self.cloud.blobs.pop(h_pdf)
        self.cloud.missing_hashes = [h_pdf]
        self.sync.refresh()
        self.assertNotIn(h_pdf, self.cloud.blobs)
        self.assertEqual(self.sync.last_missing_hashes, {"reuploaded": [], "not_found": [h_pdf]})

    def test_unknown_hash_is_not_found_and_nothing_is_put(self):
        self.sync.refresh()
        ghost = sha(b"nobody has this")
        self.cloud.missing_hashes = [ghost]
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertEqual(self.cloud.puts(), [])
        self.assertTrue(any(" - did not find missing hashes: " + ghost in line for line in logs.output))
        self.assertEqual(self.sync.last_missing_hashes, {"reuploaded": [], "not_found": [ghost]})

    def test_hash_known_but_bytes_nowhere_is_not_found(self):
        self.sync.refresh()
        h_page = self.file_hash(NOTE, f"{NOTE}/{PAGE}.rm")         # never downloaded, never cached
        self.cloud.blobs.pop(h_page)
        self.cloud.missing_hashes = [h_page]
        self.sync.refresh()
        self.assertEqual(self.cloud.puts(), [])
        self.assertEqual(self.sync.last_missing_hashes["not_found"], [h_page])

    def test_uncached_document_index_is_skipped(self):
        self.sync.refresh()
        h_content = self.file_hash(PDF, f"{PDF}.content")
        self.sync.blobs.path(self.sync.tree.get(PDF).hash).unlink()      # its index is not cached now
        self.cloud.missing_hashes = [h_content]
        self.sync.root_index = self.sync.get_index(self.sync.root.hash, "root.docSchema", cached_only=True)
        reuploaded, not_found = self.sync.reupload_missing([h_content])
        self.assertEqual((reuploaded, not_found), (set(), {h_content}))

    def test_failed_download_of_the_list_does_not_stop_the_refresh(self):
        self.sync.refresh()
        self.cloud.missing_hashes = [sha(b"x")]
        for status in (404, 500):
            with self.subTest(status=status):
                self.gate.replies["/sync/v3/missing"] = HttpResponse(status, {}, b"")
                with self.assertLogs("rm.sync", "WARNING") as logs:
                    tree = self.sync.refresh()
                self.assertIn(PDF, tree)
                self.assertTrue(any(" - onDownloadMissingHashesFailed:" in line for line in logs.output))
                self.assertIsNone(self.sync.last_missing_hashes)
        self.assertTrue(self.sleeps)                                       # the 500 was retried

    def test_network_error_type_is_covered(self):
        self.sync.refresh()
        self.gate.replies["/sync/v3/missing"] = HttpResponse(503, {}, b"")
        with self.assertRaises(NetworkError):
            self.sync.fetch_missing_hashes()

    def test_malformed_reply_is_logged_and_the_refresh_continues(self):
        self.sync.refresh()
        self.cloud.missing_hashes = []
        for reply in (b"not json", b"[]", b'{"hashes": 3}'):
            with self.subTest(reply=reply):
                self.cloud.missing_reply = reply
                with self.assertLogs("rm.sync", "WARNING") as logs:
                    tree = self.sync.refresh()
                self.assertIn(NOTE, tree)
                self.assertTrue(any(" - onDownloadMissingHashesFailed:" in line for line in logs.output))

    def test_upload_failure_propagates(self):
        self.sync.refresh()
        h_content = self.file_hash(PDF, f"{PDF}.content")
        self.cloud.blobs.pop(h_content)
        self.cloud.missing_hashes = [h_content]
        self.gate.put_status = 400
        with self.assertRaises(SyncError):
            self.sync.refresh()

    def test_upload_failure_through_fail_next(self):
        self.sync.refresh()
        h_content = self.file_hash(PDF, f"{PDF}.content")
        self.cloud.blobs.pop(h_content)
        self.cloud.missing_hashes = [h_content]
        self.cloud.fail_next = [400]
        with self.assertRaises(SyncError):
            self.sync.reupload_missing([h_content])


class Blob404Tests(Base):
    def downloaded(self) -> str:
        self.sync.refresh()
        self.sync.download_document(PDF)
        h_pdf = self.file_hash(PDF, f"{PDF}.pdf")
        self.sync.blobs.clear()
        return h_pdf

    def test_lost_blob_is_reuploaded_from_the_local_copy(self):
        h_pdf = self.downloaded()
        self.cloud.blobs.pop(h_pdf)
        with self.assertLogs("rm.sync", "INFO") as logs:
            data = self.sync.get_blob(h_pdf, f"{PDF}.pdf")
        self.assertEqual(data, PDF_BYTES)
        self.assertTrue(any(f"Failure 404 triggers forced re-upload for {PDF}.pdf" in line for line in logs.output))
        self.assertEqual(self.cloud.blobs[h_pdf], PDF_BYTES)
        self.assertEqual(self.puts_of(h_pdf)[0]["rm-filename"], f"{PDF}.pdf")
        self.assertTrue(self.sync.blobs.has(h_pdf))

    def test_edited_local_copy_does_not_match(self):
        h_pdf = self.downloaded()
        self.local(PDF, f"{PDF}.pdf").write_bytes(b"edited")
        self.cloud.blobs.pop(h_pdf)
        with self.assertRaises(SyncError) as ctx:
            self.sync.get_blob(h_pdf, f"{PDF}.pdf")
        self.assertIn("not found on server", str(ctx.exception))
        self.assertEqual(self.cloud.puts(), [])

    def test_missing_local_copy_is_the_old_error(self):
        h_pdf = self.file_hash(PDF, f"{PDF}.pdf")
        self.sync.refresh()
        self.cloud.blobs.pop(h_pdf)
        with self.assertRaises(SyncError) as ctx:
            self.sync.get_blob(h_pdf, f"{PDF}.pdf")
        self.assertIn("not found on server", str(ctx.exception))

    def test_index_blob_404_is_unchanged(self):
        self.downloaded()
        h_index = self.sync.tree.get(PDF).hash
        self.cloud.blobs.pop(h_index)
        with self.assertRaises(SyncError) as ctx:
            self.sync.get_blob(h_index, f"{PDF}.docSchema", index=True)
        self.assertIn("not found on server", str(ctx.exception))
        self.assertEqual(self.cloud.puts(), [])


class CheckFilesTests(Base):
    def test_request_body_defaults(self):
        h1, h2 = sha(b"1"), sha(b"2")
        self.cloud.check_files_missing = [h2]
        self.assertEqual(self.sync.check_files([h1, h2]), [h2])
        self.assertEqual(self.cloud.check_files_requests, [{"files": [h1, h2], "filename": "root", "reason": "Other"}])
        method, path, headers = [c for c in self.cloud.calls if c[1] == "/sync/v3/check-files"][0]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_filename_and_each_reason(self):
        h = sha(b"1")
        for reason in REQUEST_REASONS:
            with self.subTest(reason=reason):
                self.sync.check_files(iter([h]), f"{PDF}.docSchema", reason)
                self.assertEqual(self.cloud.check_files_requests[-1],
                                 {"files": [h], "filename": f"{PDF}.docSchema", "reason": reason})
        self.assertEqual(REQUEST_REASONS, ("Other", "Logout", "Archive"))

    def test_invalid_reason_raises_without_a_request(self):
        for reason in ("other", "Delete", ""):
            with self.assertRaises(ValueError):
                self.sync.check_files([sha(b"1")], reason=reason)
        self.assertEqual(self.cloud.check_files_requests, [])

    def test_missing_key_is_empty(self):
        self.gate.replies["/sync/v3/check-files"] = HttpResponse(200, {}, b"{}")
        self.assertEqual(self.sync.check_files([sha(b"1")]), [])

    def test_bad_replies(self):
        for body in (b"nope", b"[]", b'{"missingFiles": "x"}'):
            with self.subTest(body=body):
                self.gate.replies["/sync/v3/check-files"] = HttpResponse(200, {}, body)
                with self.assertRaises(SyncError) as ctx:
                    self.sync.check_files([sha(b"1")])
                self.assertIn("in check missing files", str(ctx.exception))

    def test_http_error_is_a_sync_error(self):
        self.gate.replies["/sync/v3/check-files"] = HttpResponse(400, {}, b"")
        with self.assertRaises(SyncError):
            self.sync.check_files([sha(b"1")])

    def test_not_called_by_sync(self):
        self.cloud.missing_hashes = [sha(b"x")]
        self.sync.refresh()
        self.assertNotIn("/sync/v3/check-files", self.cloud.requests)


class ImmutableActionTests(Base):
    def prepare(self, action, doc_id=PDF) -> None:
        """Signal `action`, then download the document (this also restores deleted files)."""
        self.cloud.deleted_immutable_action = action
        self.sync.refresh()
        self.sync.download_document(doc_id)

    def test_none_changes_nothing(self):
        self.prepare("None")
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.sync.refresh()
        self.assertFalse(self.local(PDF, f"{PDF}.pdf").exists())
        self.assertEqual(self.sync.status(PDF), SyncStatus.LOCAL_MODIFIED)
        self.assertEqual(self.sync.local_changes(PDF), [f"{PDF}.pdf"])
        self.assertEqual(self.sync.entry_failures, {})

    def test_redownload_restores_the_file(self):
        self.prepare("ReDownload")
        self.local(PDF, f"{PDF}.pdf").unlink()
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertTrue(any(f" - force download of missing file:{PDF}.pdf" in line for line in logs.output))
        self.assertEqual(self.local(PDF, f"{PDF}.pdf").read_bytes(), PDF_BYTES)
        self.assertEqual(self.sync.status(PDF), SyncStatus.SYNCED)
        self.assertEqual(self.sync.entry_failures, {})

    def test_redownload_from_the_server_when_not_cached(self):
        self.prepare("ReDownload")
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.sync.blobs.clear()
        self.sync.refresh()
        self.assertEqual(self.local(PDF, f"{PDF}.pdf").read_bytes(), PDF_BYTES)

    def test_redownload_with_the_blob_gone_everywhere_is_a_recorded_failure(self):
        self.prepare("ReDownload")
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.sync.blobs.clear()
        self.cloud.blobs.pop(self.file_hash(PDF, f"{PDF}.pdf"))
        with self.assertLogs("rm.sync", "WARNING"):
            tree = self.sync.refresh()
        self.assertIn(PDF, tree)
        self.assertIn(PDF, self.sync.entry_failures)
        self.assertIn(f"{PDF}.pdf", self.sync.entry_failures[PDF])
        self.assertEqual(self.sync.status(PDF), SyncStatus.ENTRY_FAILED)
        self.assertFalse(self.local(PDF, f"{PDF}.pdf").exists())

    def test_rollback_rematerialises_the_document(self):
        self.prepare("Rollback")
        self.local(PDF, f"{PDF}.pdf").unlink()
        content = self.local(PDF, f"{PDF}.content")
        original = content.read_bytes()
        content.write_bytes(b'{"edited": true}')
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertTrue(any(f" - rollback entry due to missing file:{PDF}.pdf" in line for line in logs.output))
        self.assertEqual(self.local(PDF, f"{PDF}.pdf").read_bytes(), PDF_BYTES)
        self.assertEqual(content.read_bytes(), original)
        self.assertEqual(self.sync.status(PDF), SyncStatus.SYNCED)
        self.assertEqual(self.sync.entry_failures, {})

    def test_rollback_of_a_document_not_in_the_tree_is_skipped(self):
        self.prepare("Rollback")
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.cloud.remove_item(PDF)
        self.sync.refresh()
        self.assertFalse(self.local(PDF, f"{PDF}.pdf").exists())
        self.assertEqual(self.sync.entry_failures, {})

    def test_fail_entry(self):
        self.prepare("FailEntry")
        self.sync.download_document(EPUB)
        self.local(PDF, f"{PDF}.pdf").unlink()
        with self.assertLogs("rm.sync", "INFO") as logs:
            self.sync.refresh()
        self.assertTrue(any(f" - failing entry since file {PDF}.pdf is missing" in line for line in logs.output))
        self.assertEqual(self.sync.entry_failures, {PDF: f"Missing file:{PDF}.pdf"})
        self.assertEqual(self.sync.status(PDF), SyncStatus.ENTRY_FAILED)
        self.assertEqual(SyncStatus.ENTRY_FAILED.value, "entry-failed")
        self.assertEqual(self.sync.status(EPUB), SyncStatus.SYNCED)
        self.assertEqual(self.sync.statuses()[PDF], SyncStatus.ENTRY_FAILED)
        self.assertFalse(self.local(PDF, f"{PDF}.pdf").exists())

    def test_fail_entry_is_cleared_when_the_file_comes_back(self):
        self.prepare("FailEntry")
        path = self.local(PDF, f"{PDF}.pdf")
        path.unlink()
        self.sync.refresh()
        self.assertEqual(self.sync.status(PDF), SyncStatus.ENTRY_FAILED)
        path.write_bytes(PDF_BYTES)
        self.sync.refresh()
        self.assertEqual(self.sync.entry_failures, {})
        self.assertEqual(self.sync.status(PDF), SyncStatus.SYNCED)

    def test_fail_entry_is_cleared_by_a_new_download(self):
        self.prepare("FailEntry")
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.sync.refresh()
        self.assertEqual(self.sync.status(PDF), SyncStatus.ENTRY_FAILED)
        self.sync.download_document(PDF)
        self.assertEqual(self.sync.status(PDF), SyncStatus.SYNCED)

    def test_check_immutable_files_returns_the_failures(self):
        self.prepare("FailEntry")
        self.assertEqual(self.sync.check_immutable_files(), {})
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.assertEqual(self.sync.check_immutable_files(), {PDF: f"Missing file:{PDF}.pdf"})

    def test_non_immutable_files_are_not_affected_by_any_action(self):
        for action in DELETED_IMMUTABLE_ACTIONS:
            with self.subTest(action=action):
                self.prepare(action)
                self.local(PDF, f"{PDF}.content").unlink()
                self.sync.refresh()
                self.assertFalse(self.local(PDF, f"{PDF}.content").exists())
                self.assertEqual(self.sync.entry_failures, {})
                self.assertEqual(self.sync.status(PDF), SyncStatus.LOCAL_MODIFIED)

    def test_page_file_is_not_immutable(self):
        self.prepare("FailEntry", NOTE)
        self.local(NOTE, f"{NOTE}/{PAGE}.rm").unlink()
        self.sync.refresh()
        self.assertEqual(self.sync.entry_failures, {})

    def test_missing_local_directory_is_skipped(self):
        self.prepare("FailEntry")
        import shutil
        shutil.rmtree(self.cache / "documents" / PDF)
        self.sync.refresh()
        self.assertEqual(self.sync.entry_failures, {})

    def test_epub_variant(self):
        self.prepare("ReDownload", EPUB)
        self.local(EPUB, f"{EPUB}.epub").unlink()
        self.sync.refresh()
        self.assertEqual(self.local(EPUB, f"{EPUB}.epub").read_bytes(), EPUB_BYTES)
        self.assertEqual(self.sync.status(EPUB), SyncStatus.SYNCED)
        self.cloud.deleted_immutable_action = "FailEntry"
        self.local(EPUB, f"{EPUB}.epub").unlink()
        self.sync.refresh()
        self.assertEqual(self.sync.entry_failures, {EPUB: f"Missing file:{EPUB}.epub"})

    def test_failure_of_one_document_does_not_stop_the_others(self):
        self.prepare("ReDownload")
        self.sync.download_document(EPUB)
        self.local(PDF, f"{PDF}.pdf").unlink()
        self.local(EPUB, f"{EPUB}.epub").unlink()
        self.sync.blobs.clear()
        self.cloud.blobs.pop(self.file_hash(PDF, f"{PDF}.pdf"))
        self.sync.refresh()
        self.assertEqual(list(self.sync.entry_failures), [PDF])
        self.assertEqual(self.local(EPUB, f"{EPUB}.epub").read_bytes(), EPUB_BYTES)


if __name__ == "__main__":
    unittest.main()
