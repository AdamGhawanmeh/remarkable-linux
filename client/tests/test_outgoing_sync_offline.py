"""Offline tests for outgoing sync and the editing settings: the shared upload pipeline
(core.outbox, used by the app and tools/push_edits.py alike), the app's background
uploader (ui.outgoing.OutgoingSync: automatic after a pause, or Upload now), retries when
offline or when the cloud keeps changing, uploads and incoming syncs one at a time, and
Settings > Editing in the app's own QML. Everything runs against the in-memory fake cloud
with synthetic ids; nothing reaches the network or ~/.cache/remarkable-linux.

Run: python3 -m unittest discover -s client/tests -p 'test_outgoing_sync_offline.py'
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)
import test_edits_offline as te  # noqa: E402

from PySide6.QtCore import QMetaObject, QObject  # noqa: E402

import push_edits  # noqa: E402
from core import outbox  # noqa: E402
from core.edits import EditError, PendingEdits, StrokeDelete, TextEdit, rebase_refs  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import paired_auth  # noqa: E402
from rmfixtures import line_item, points_v2  # noqa: E402
from ui.library import LibrarySource, document_pages  # noqa: E402
from ui.outgoing import (AUTO_SYNC_KEY, EDITING_KEY, SYNC_ERROR, UPLOADING, EditSettings,  # noqa: E402
                         OutgoingSync)
from ui.quick.app import QuickApp  # noqa: E402
from ui.quick.bridge.platform import SettingsStore  # noqa: E402
from viewer import rm_stroke, rm_text  # noqa: E402
from viewer.rm_parser import ParagraphStyle, parse  # noqa: E402

wait = tq.wait
OK, UPLOADING_STATUS, SYNC_ERROR_STATUS = 2, UPLOADING, SYNC_ERROR


def line(y: float) -> rm_stroke.LineValue:
    return rm_stroke.draw([(0.0, y), (90.0, y + 10.0)], rm_stroke.FINELINER_V2)


def tablet_page(*counters: int) -> bytes:
    """The throwaway page as the tablet has it after drawing more strokes."""
    page = te.drawn_page()
    for n, counter in enumerate(counters):
        page += line_item(te.LAYER, (1, counter), tool=17, color=0,
                          points=points_v2([(10 * n, 600, 0, 0, 2, 255), (10 * n + 40, 640, 0, 0, 2, 255)]))
    return page


def tablet_draws(cloud, *counters: int) -> None:
    cloud.put_item(te.THROWAWAY, te.meta("throwaway"), te.notebook_content(te.PAGE1, te.PAGE2),
                   {f"{te.THROWAWAY}/{te.PAGE1}.rm": tablet_page(*counters)})


def wait_for(condition, ms: int = 6000) -> bool:
    for _ in range(ms // 20):
        if condition():
            return True
        wait(20)
    return condition()


class Base(te.Base):
    def setUp(self):
        super().setUp()
        self.sync._sleep = lambda seconds: None          # network retries without waiting

    def roots(self) -> int:
        """Root commits so far (each upload is one)."""
        return sum(1 for path, _ in self.cloud.puts() if path == "/sync/v3/root")

    def cloud_page(self) -> bytes:
        return self.cloud.item(te.THROWAWAY)[f"{te.THROWAWAY}/{te.PAGE1}.rm"]

    def cloud_strokes(self) -> int:
        return len(parse(self.cloud_page()).strokes)

    def cloud_text(self) -> str:
        return rm_text.page_text(self.cloud_page()).text

    def on_disk(self) -> list:
        """The ledger as another process (a restart) would read it."""
        return PendingEdits(self.dir / "cache" / "pending").all()

    def push(self):
        return outbox.push_document(self.sync, te.THROWAWAY, settle=self.source.settle_upload)


class OutboxTests(Base):
    """core.outbox: the one upload path, its failures and the ledger after it."""

    def run_tool(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        sync = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache")
        with contextlib.redirect_stdout(out):
            status = push_edits.main(list(argv), sync)
        return status, out.getvalue()

    def test_the_tool_and_the_app_upload_through_the_same_function(self):
        with mock.patch.object(outbox, "push_document", wraps=outbox.push_document) as spy:
            self.insert()
            status, text = self.run_tool("--confirm", te.THROWAWAY)
            self.assertEqual(status, 0, text)
            self.assertEqual(spy.call_args.args[1], te.THROWAWAY)
            self.source.add_stroke(self.doc, te.PAGE1, line(300), te.NOW)
            out = OutgoingSync(self.source, EditSettings(SettingsStore()), debounce_ms=50)
            self.addCleanup(out.shutdown)
            out.uploadNow()
            self.assertTrue(wait_for(lambda: not self.sync.edits.all() and not out.busy))
            self.sync.push_edits(te.THROWAWAY)              # nothing pending: no upload, same path
        self.assertEqual(spy.call_count, 3)
        self.assertEqual(self.roots(), 2)

    def test_offline_keeps_every_edit_on_disk_until_the_network_is_back(self):
        edit = self.insert()
        self.cloud.offline = True
        with self.assertRaises(outbox.PushFailed) as failed:
            self.push()
        self.assertEqual(failed.exception.kind, outbox.OFFLINE)
        self.assertEqual((self.on_disk(), self.roots()), ([edit], 0))
        self.cloud.offline = False
        self.push()
        self.assertEqual((self.on_disk(), self.roots()), ([], 1))
        self.assertEqual(self.cloud.item(te.THROWAWAY)[edit.path], self.image)

    def test_the_cloud_answering_errors_is_offline_too(self):
        self.insert()
        self.cloud.fail_next = [503] * 20
        with self.assertRaises(outbox.PushFailed) as failed:
            self.push()
        self.assertEqual(failed.exception.kind, outbox.OFFLINE)
        self.assertEqual(len(self.on_disk()), 1)

    def test_a_remote_change_before_the_commit_is_merged_and_retried(self):
        mine = self.source.add_stroke(self.doc, te.PAGE1, line(300), te.NOW)
        self.cloud.before_commit = lambda: (setattr(self.cloud, "before_commit", None),
                                            tablet_draws(self.cloud, 16))
        result = self.push()
        self.assertEqual(result.uploaded, [mine])
        self.assertEqual(self.cloud_strokes(), 3)               # the tablet's two and mine
        self.assertEqual((self.on_disk(), self.roots()), ([], 2))   # the stale commit, then the merged one

    def test_a_cloud_that_keeps_changing_is_a_conflict_and_nothing_is_lost(self):
        mine = self.source.add_stroke(self.doc, te.PAGE1, line(300), te.NOW)
        counter = iter(range(16, 100))
        self.cloud.before_commit = lambda: tablet_draws(self.cloud, next(counter))
        with self.assertRaises(outbox.PushFailed) as failed:
            self.push()
        self.assertEqual(failed.exception.kind, outbox.CONFLICT)
        self.assertEqual(self.on_disk(), [mine])
        self.cloud.before_commit = None
        self.push()
        self.assertEqual(self.on_disk(), [])
        self.assertEqual(self.cloud_strokes(), 3)

    def test_a_trashed_document_is_refused_and_keeps_its_edits(self):
        edit = self.insert()
        self.cloud.put_item(te.THROWAWAY, te.meta("throwaway", parent="trash"), te.notebook_content(te.PAGE1, te.PAGE2),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page()})
        with self.assertRaises(outbox.PushFailed) as failed:
            self.push()
        self.assertEqual(failed.exception.kind, outbox.REJECTED)
        self.assertEqual(self.on_disk(), [edit])

    def test_an_edit_made_during_the_upload_on_what_it_uploads_is_kept(self):
        """A stroke deleted while the upload of that stroke runs: the delete names the
        stroke by the uploaded edit's uid, renamed as the cloud's page names it."""
        first = self.source.add_stroke(self.doc, te.PAGE1, line(300), te.NOW)
        made = {}

        def meanwhile():
            self.cloud.before_commit = None
            done = self.source.staged(self.source.synced(te.THROWAWAY)).replay(te.PAGE1)
            ref = done.ref(done.made[first.uid])
            self.assertEqual(ref, ("edit", first.uid))
            made["delete"] = self.source.delete_strokes(self.doc, te.PAGE1, [ref], te.NOW + 1)
        self.cloud.before_commit = meanwhile
        result = self.push()
        self.assertEqual(result.uploaded, [first])
        [kept] = self.on_disk()
        self.assertIsInstance(kept, StrokeDelete)
        self.assertEqual(kept.items, (("page", *result.made[first.uid]),))
        self.assertEqual(self.cloud_strokes(), 2)
        self.push()
        self.assertEqual((self.on_disk(), self.cloud_strokes()), ([], 1))     # deleted, as asked

    def test_text_typed_during_the_upload_goes_on_after_the_uploaded_text(self):
        create = TextEdit.new(te.THROWAWAY, te.PAGE1, "create", te.NOW, paragraph=int(ParagraphStyle.TITLE))
        hello = TextEdit.new(te.THROWAWAY, te.PAGE1, "insert", te.NOW, text="Hello")
        self.source.add_text_edits(self.doc, te.PAGE1, [create, hello], [])

        def meanwhile():
            self.cloud.before_commit = None
            more = TextEdit.new(te.THROWAWAY, te.PAGE1, "insert", te.NOW + 1, first=("edit", hello.uid, 4),
                                text=" world")
            self.source.add_text_edits(self.doc, te.PAGE1, [more], [])
        self.cloud.before_commit = meanwhile
        self.push()
        self.assertEqual(self.cloud_text(), "Hello")
        self.assertEqual(self.on_disk()[0].first[0], "page")
        self.push()
        self.assertEqual((self.cloud_text(), self.on_disk()), ("Hello world", []))

    def test_typing_named_before_an_upload_and_kept_after_it_is_renamed(self):
        """The text session names characters when the page is opened and hands typing
        over after a pause: an upload may come in between."""
        create = TextEdit.new(te.THROWAWAY, te.PAGE1, "create", te.NOW, paragraph=int(ParagraphStyle.TITLE))
        hello = TextEdit.new(te.THROWAWAY, te.PAGE1, "insert", te.NOW, text="Hello")
        self.source.add_text_edits(self.doc, te.PAGE1, [create, hello], [])
        named = ("edit", hello.uid, 4)                          # as the session named it
        self.push()
        late = TextEdit.new(te.THROWAWAY, te.PAGE1, "insert", te.NOW + 1, first=named, text="!")
        self.source.add_text_edits(self.doc, te.PAGE1, [late], [])
        self.assertEqual(self.on_disk()[0].first[0], "page")
        self.push()
        self.assertEqual(self.cloud_text(), "Hello!")

    def test_undo_still_works_after_its_edit_was_uploaded(self):
        self.source.add_stroke(self.doc, te.PAGE1, line(300), te.NOW)
        self.push()
        self.assertEqual(self.cloud_strokes(), 2)
        self.assertEqual(self.source.history_state(self.doc, te.PAGE1), (True, False))
        self.assertTrue(self.source.undo(self.doc, te.PAGE1, te.NOW + 5))
        self.push()
        self.assertEqual((self.cloud_strokes(), self.on_disk()), (1, []))
        self.assertTrue(self.source.redo(self.doc, te.PAGE1, te.NOW + 6))
        self.push()
        self.assertEqual(self.cloud_strokes(), 2)

    def test_an_incoming_sync_waits_for_the_upload_under_way(self):
        edit = self.insert()
        seen = {}

        def meanwhile():
            self.cloud.before_commit = None
            incoming = threading.Thread(target=lambda: seen.setdefault("tree", self.source.refresh()))
            incoming.start()
            incoming.join(0.3)
            seen["blocked"], seen["thread"] = incoming.is_alive(), incoming
        self.cloud.before_commit = meanwhile
        self.push()
        seen["thread"].join(10)
        self.assertTrue(seen["blocked"])                        # it did not run halfway through
        self.assertIn(edit.path, seen["tree"].get(te.THROWAWAY).files)   # it saw the upload

    def test_an_upload_waits_for_another_process_uploading(self):
        self.insert()
        with outbox.upload_lock(self.sync):
            pusher = threading.Thread(target=self.push)
            pusher.start()
            pusher.join(0.3)
            self.assertTrue(pusher.is_alive())
            self.assertEqual(self.roots(), 0)
        pusher.join(10)
        self.assertEqual((self.roots(), self.on_disk()), (1, []))

    def test_rebase_leaves_unrelated_references_alone(self):
        delete = StrokeDelete.new(te.THROWAWAY, te.PAGE1, [("edit", "x" * 32), ("page", 1, 15)], te.NOW)
        self.assertIs(rebase_refs(delete, {"y" * 32: (2, 9)}, {}), delete)
        moved = rebase_refs(delete, {"x" * 32: (2, 9)}, {})
        self.assertEqual(moved.items, (("page", 2, 9), ("page", 1, 15)))


class SettingsTests(Base):
    """Settings > Editing: off by default, kept in settings.ini, --edit-notebook first."""

    def settings(self) -> EditSettings:
        return EditSettings(SettingsStore(self.dir / "config" / "settings.ini"))

    def diary(self):
        return self.tree.get(te.OTHER)

    def test_editing_and_automatic_upload_are_off_by_default(self):
        settings = self.settings()
        self.assertEqual((settings.editing_enabled, settings.auto_sync), (False, False))
        source = LibrarySource(self.sync)
        out = OutgoingSync(source, settings)
        self.addCleanup(out.shutdown)
        self.assertFalse(source.editing_on)
        self.assertEqual(source.edit_refusal(self.doc), "Editing is not turned on")
        self.assertEqual(source.creation_refusal(""), "Editing is not turned on")
        with self.assertRaises(EditError):
            source.add_stroke(self.doc, te.PAGE1, line(300), te.NOW)
        self.assertEqual(self.on_disk(), [])

    def test_the_setting_opens_every_notebook(self):
        settings = self.settings()
        settings.editing_enabled = True
        source = LibrarySource(self.sync)
        out = OutgoingSync(source, settings)
        self.addCleanup(out.shutdown)
        self.assertTrue(source.can_edit(self.doc) and source.can_edit(self.diary()))

    def test_edit_notebook_limits_editing_whatever_the_setting_says(self):
        for on in (False, True):
            settings = self.settings()
            settings.editing_enabled = on
            source = LibrarySource(self.sync, editable=("throwaway",))
            out = OutgoingSync(source, settings)
            self.addCleanup(out.shutdown)
            self.assertTrue(source.can_edit(self.doc))
            self.assertIn("only turned on for the document named 'throwaway'", source.edit_refusal(self.diary()))
            self.assertEqual(out.property("editingLimitedTo"), "throwaway")

    def test_edit_notebook_comes_from_the_option_or_the_environment(self):
        import run
        given = lambda *names: argparse.Namespace(edit_notebook=list(names))    # noqa: E731
        env = {"REMARKABLE_EDIT_NOTEBOOK": "throwaway"}
        self.assertEqual(run.edit_notebooks(given(), env), ("throwaway",))
        self.assertEqual(run.edit_notebooks(given("Diary"), env), ("Diary",))     # the option wins
        self.assertEqual(run.edit_notebooks(given(), {}), ())
        self.assertEqual(run.parse_args(["--edit-notebook", "throwaway"]).edit_notebook, ["throwaway"])

    def test_the_settings_are_kept_across_a_restart(self):
        out = OutgoingSync(LibrarySource(self.sync), self.settings())
        out.setProperty("editingEnabled", True)
        out.setProperty("autoSync", True)
        out.shutdown()
        ini = (self.dir / "config" / "settings.ini").read_text()
        self.assertIn("editingEnabled=true", ini)
        self.assertIn("autoSyncEdits=true", ini)
        restarted = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"))
        out = OutgoingSync(restarted, self.settings())
        self.addCleanup(out.shutdown)
        self.assertEqual((out.property("editingEnabled"), out.property("autoSync")), (True, True))
        self.assertTrue(restarted.edit_everything)
        self.assertEqual((EDITING_KEY, AUTO_SYNC_KEY), ("LinuxClient/editingEnabled", "LinuxClient/autoSyncEdits"))


class AutoSyncTests(Base):
    """ui.outgoing.OutgoingSync over the fake cloud: the pause, Upload now, retries."""

    def setUp(self):
        super().setUp()
        self.store = SettingsStore()
        self.settings = EditSettings(self.store)
        self.out = self.uploader()
        self.statuses: list[dict] = []
        self.out.statuses.connect(self.statuses.append)

    def uploader(self, retry_ms=(100, 100, 100)) -> OutgoingSync:
        out = OutgoingSync(self.source, self.settings, debounce_ms=250, retry_ms=retry_ms)
        self.addCleanup(out.shutdown)
        return out

    def stroke(self, y: float = 300.0, when: int = te.NOW):
        return self.source.add_stroke(self.doc, te.PAGE1, line(y), when)

    def settled(self, ms: int = 8000) -> bool:
        return wait_for(lambda: not self.sync.edits.all() and not self.out.busy, ms)

    def test_without_automatic_upload_edits_wait_for_upload_now(self):
        self.stroke()
        wait(600)
        self.assertEqual((self.roots(), len(self.on_disk())), (0, 1))
        self.assertEqual(self.out.property("state"), "pending")
        self.assertEqual(self.statuses[-1], {te.THROWAWAY: (UPLOADING_STATUS, 0.0)})
        self.out.uploadNow()
        self.assertTrue(self.settled())
        self.assertEqual((self.roots(), self.on_disk(), self.cloud_strokes()), (1, [], 2))
        self.assertEqual((self.out.property("state"), self.statuses[-1]), ("idle", {}))

    def test_rapid_edits_go_up_in_one_upload_after_the_pause(self):
        self.out.setProperty("autoSync", True)
        for n in range(5):
            self.stroke(300 + 20 * n, te.NOW + n)
            wait(30)
        self.assertEqual(self.roots(), 0)                       # still within the pause
        self.assertTrue(self.settled())
        self.assertEqual((self.roots(), self.on_disk(), self.cloud_strokes()), (1, [], 6))

    def test_leaving_a_document_uploads_at_once(self):
        self.out.setProperty("autoSync", True)
        self.out.documents_open([te.THROWAWAY])
        self.stroke()
        self.out.documents_open([])
        self.assertTrue(self.out.busy)                          # no pause
        self.assertTrue(self.settled())
        self.assertEqual(self.roots(), 1)

    def test_offline_edits_stay_on_disk_and_go_once_back_online(self):
        self.out.setProperty("autoSync", True)
        self.stroke()                                           # (the page is cached: it is on screen)
        self.cloud.offline = True
        self.assertTrue(wait_for(lambda: self.out.property("state") == "retrying"))
        self.assertEqual(len(self.on_disk()), 1)
        self.assertEqual(self.statuses[-1], {te.THROWAWAY: (SYNC_ERROR_STATUS, 0.0)})
        self.cloud.offline = False
        self.assertTrue(self.settled())                         # the retry timer
        self.assertEqual((self.roots(), self.on_disk(), self.out.property("state")), (1, [], "idle"))

    def test_back_online_uploads_without_waiting_for_the_retry(self):
        self.out.shutdown()
        self.out = self.uploader(retry_ms=(600_000,))
        self.stroke()
        self.cloud.offline = True
        self.out.uploadNow()                                    # asked for: kept at until it goes
        self.assertTrue(wait_for(lambda: self.out.property("state") == "retrying"))
        self.cloud.offline = False
        self.out.network_available()
        self.assertTrue(self.settled(3000))
        self.assertEqual(self.roots(), 1)

    def test_while_offline_or_logged_out_nothing_is_tried(self):
        self.out.can_upload = lambda: False
        self.out.setProperty("autoSync", True)
        self.stroke()
        wait(600)
        self.assertEqual((self.out.property("state"), len(self.cloud.calls) > 0), ("waiting", True))
        calls = len(self.cloud.calls)
        self.out.uploadNow()
        wait(200)
        self.assertEqual((len(self.cloud.calls), len(self.on_disk())), (calls, 1))

    def test_a_refused_document_shows_its_error_and_is_retried_when_asked(self):
        self.out.setProperty("autoSync", True)
        self.insert()
        self.cloud.put_item(te.THROWAWAY, te.meta("throwaway", parent="trash"), te.notebook_content(te.PAGE1, te.PAGE2),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page()})
        self.assertTrue(wait_for(lambda: self.out.property("state") == "error"))
        self.assertIn("no longer in the library", self.out.property("statusText"))
        self.assertEqual(self.statuses[-1], {te.THROWAWAY: (SYNC_ERROR_STATUS, 0.0)})
        calls = len(self.cloud.calls)
        wait(600)
        self.assertEqual(len(self.cloud.calls), calls)          # not retried by itself
        self.cloud.put_item(te.THROWAWAY, te.meta("throwaway"), te.notebook_content(te.PAGE1, te.PAGE2),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page()})
        self.out.retry(te.THROWAWAY)                            # the entry's cloud_x icon
        self.assertTrue(self.settled())
        self.assertEqual(self.out.property("state"), "idle")

    def test_incoming_and_outgoing_sync_lose_no_edit(self):
        """A remote edit arrives while a local one is pending, another lands while the
        upload runs, and the user draws again during it: all of them end in the cloud."""
        self.out.setProperty("autoSync", True)
        self.stroke(300)
        tablet_draws(self.cloud, 16)                            # the tablet syncs...
        tree = self.source.refresh()                            # ...and the live sync brings it in
        doc = tree.get(te.THROWAWAY)
        scene = self.source.page_scene(doc, document_pages(doc)[0])
        self.assertEqual(len(scene.strokes), 3)                 # theirs and the pending one, both shown

        def meanwhile():
            self.cloud.before_commit = None
            tablet_draws(self.cloud, 16, 17)                    # the tablet again, mid-upload
            self.stroke(500, te.NOW + 9)                        # and the user, mid-upload
        self.cloud.before_commit = meanwhile
        self.assertTrue(self.settled(10000))
        self.assertTrue(wait_for(lambda: self.roots() >= 3 and not self.out.busy and not self.sync.edits.all()))
        # the tablet's three, my two; one stale commit retried, then the second upload
        self.assertEqual(self.cloud_strokes(), 5)
        self.assertEqual((self.roots(), self.on_disk()), (3, []))


class SettingsCellQmlTests(unittest.TestCase):
    """Settings > Editing in the app's own General settings, and the library entries'
    upload states, over the fake cloud."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = te.build_cloud()
        sync = SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache")
        sync._sleep = lambda seconds: None
        cls.source = LibrarySource(sync)                       # no --edit-notebook
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp, network_information=tq.FakeNetwork())
        cls.app.singletons.settings.setRawValue("Onboarding", "ClientEdit", True)
        cls.app.load()
        cls.app.start(sync=False)
        wait(500)
        cls.window = cls.app.window

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def tearDown(self):
        out = self.app.outgoing
        out.setProperty("autoSync", False)
        out.setProperty("editingEnabled", False)
        self.cloud.offline = False
        self.source.editable = ()
        self.assertTrue(wait_for(lambda: not out.busy))
        self.source.sync.edits.clear()
        wait_for(lambda: out.property("state") == "idle", 2000)

    def find(self, name: str) -> QObject:
        found = self.window.findChildren(QObject, name)
        self.assertTrue(found, name)
        return found[0]

    def flip(self, toggle_name: str) -> None:
        """The user flips a toggle (Switch.toggle + toggled, as a click does)."""
        toggle = self.find(toggle_name)
        QMetaObject.invokeMethod(toggle, "toggle")
        QMetaObject.invokeMethod(toggle, "toggled")
        wait(50)

    def doc(self):
        return self.source.synced(te.THROWAWAY)

    def entry_status(self) -> int:
        return self.app.library.entryForId(te.THROWAWAY).property("status")

    def ini(self) -> str:
        return (self.dir / "settings.ini").read_text() if (self.dir / "settings.ini").exists() else ""

    def test_the_editing_cell_is_in_general_settings_with_both_toggles_off(self):
        cell = self.find("linuxEditingCell")
        self.assertIsNotNone(cell.parentItem())                 # placed in the dialog's column
        self.assertEqual(cell.property("title"), "Editing")
        self.assertFalse(self.find("linuxEditingToggle").property("checked"))
        self.assertFalse(self.find("linuxAutoSyncToggle").property("checked"))
        self.assertFalse(self.app.singletons.user_info.property("featureToggles")["clientEdit"])
        self.assertEqual(self.find("linuxUploadNowItem").property("status"), "All edits are in the cloud")
        self.assertEqual([w for w in self.app.warnings if "linux" in w.lower()], [])

    def test_turning_editing_on_lets_the_app_edit_and_is_kept(self):
        self.flip("linuxEditingToggle")
        self.assertTrue(self.app.outgoing.property("editingEnabled"))
        self.assertTrue(self.app.singletons.user_info.property("featureToggles")["clientEdit"])
        self.assertTrue(self.source.can_edit(self.doc()))
        self.assertIn("editingEnabled=true", self.ini())
        self.flip("linuxEditingToggle")
        self.assertFalse(self.app.singletons.user_info.property("featureToggles")["clientEdit"])
        self.assertIn("editingEnabled=false", self.ini())

    def test_upload_now_sends_what_waits_and_the_entry_shows_it(self):
        self.flip("linuxEditingToggle")
        self.source.add_stroke(self.doc(), te.PAGE1, line(300), te.NOW)
        self.assertTrue(wait_for(lambda: self.entry_status() == UPLOADING_STATUS))
        item = self.find("linuxUploadNowItem")
        self.assertEqual(item.property("text"), "Upload now")
        self.assertIn("1 edit in 1 document", item.property("status"))
        roots = sum(1 for p, _ in self.cloud.puts() if p == "/sync/v3/root")
        QMetaObject.invokeMethod(item, "clicked")
        self.assertTrue(wait_for(lambda: not self.source.sync.edits.all() and not self.app.outgoing.busy))
        self.assertEqual(sum(1 for p, _ in self.cloud.puts() if p == "/sync/v3/root"), roots + 1)
        self.assertTrue(wait_for(lambda: self.entry_status() == OK))

    def test_automatic_upload_from_the_toggle(self):
        self.flip("linuxEditingToggle")
        self.flip("linuxAutoSyncToggle")
        self.assertIn("autoSyncEdits=true", self.ini())
        self.source.add_stroke(self.doc(), te.PAGE1, line(320), te.NOW)
        self.assertTrue(wait_for(lambda: not self.source.sync.edits.all() and not self.app.outgoing.busy, 10000))

    def test_a_failed_upload_shows_cloud_x_and_its_click_retries(self):
        self.flip("linuxEditingToggle")
        self.source.add_stroke(self.doc(), te.PAGE1, line(340), te.NOW)
        self.cloud.offline = True
        self.app.outgoing.uploadNow()
        self.assertTrue(wait_for(lambda: self.entry_status() == SYNC_ERROR_STATUS))
        self.assertEqual(len(PendingEdits(self.dir / "cache" / "pending").all()), 1)
        self.cloud.offline = False
        entry = self.app.library.entryForId(te.THROWAWAY)
        entry.handleSyncError(1)                                # Entry.Retry, as EntryErrorIcon's click
        self.assertTrue(wait_for(lambda: not self.source.sync.edits.all() and not self.app.outgoing.busy))
        self.assertTrue(wait_for(lambda: self.entry_status() == OK))

    def test_edit_notebook_shows_in_the_cell_and_hides_the_toggle(self):
        self.source.editable = ("throwaway",)
        self.app.outgoing.changed.emit()
        wait(50)
        item = self.find("linuxEditingToggleItem")
        self.assertEqual(item.property("status"), "Only “throwaway” (--edit-notebook)")
        self.assertFalse(item.property("enabled"))
        self.assertFalse(self.find("linuxEditingToggle").property("visible"))


if __name__ == "__main__":
    unittest.main()
