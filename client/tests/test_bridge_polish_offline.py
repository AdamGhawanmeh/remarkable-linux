"""Offline unit and QML integration tests for the bridge polish (synthetic ids and
synthetic JWTs only; nothing reaches the real cloud):

- B1 SceneController.insertItemsAtCursorPosition: strokes copied, then Ctrl+V in text
  mode, put at the caret as one undo step after the text typed, as pending edits.
- B2 UserInfo from the user token's claims (id, name, username, email, account type,
  subscription level, isSubscribed, scopes, createdAt), set at start and cleared on
  log-out.
- B3 Entry.isDownloading / Entry.Downloading and DocumentSelection.includesExporting /
  includesDownloading, so ActionManager's actions are off for an entry being exported
  or downloaded; LibrarySource's download listener (cache misses only).
- B4 a page edited and not uploaded gets a new thumbnail when the document closes.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_bridge_polish_offline.py'
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import QMetaObject, QPointF, QRect  # noqa: E402

import test_edits_offline as te  # noqa: E402
import test_layers_offline as tl  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import make_jwt, paired_auth  # noqa: E402
from test_erase_undo_clear_offline import crossing_page  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.quick.bridge.library import DocumentSelection, EntryWrapper, Library  # noqa: E402
from ui.quick.bridge.platform import UserInfo  # noqa: E402
from viewer import rm_stroke  # noqa: E402
from viewer import rm_text  # noqa: E402

THROWAWAY, PAGE1, OTHER = te.THROWAWAY, te.PAGE1, tl.OTHER

# Synthetic claims, shaped as the cloud's user token (auth0-profile, level, scopes, tectonic).
CLAIMS = {"auth0-profile": {"UserID": "google-oauth2|000000000000000000001", "IsSocial": True,
                            "Connection": "google-oauth2", "Name": "Test User", "Nickname": "test.user",
                            "GivenName": "Test", "FamilyName": "User", "Email": "test.user@example.invalid",
                            "EmailVerified": True, "CreatedAt": "2020-01-02T03:04:05.000Z",
                            "UpdatedAt": "2024-01-01T00:00:00.000Z"},
          "level": "connect", "scopes": "sync:tortoise intgr screenshare hwcmail:-1 mail:-1",
          "tectonic": "eu", "sub": "auth0|synthetic-subject", "exp": 4_000_000_000}


class UserInfoTests(unittest.TestCase):
    def test_the_claims_fill_in_the_user(self):
        info = UserInfo()
        seen = []
        info.isSubscribedChanged.connect(lambda: seen.append("subscribed"))
        info.emailChanged.connect(seen.append)
        self.assertTrue(info.set_from_token(make_jwt(CLAIMS)))
        self.assertEqual((info.property("id"), info.property("name"), info.property("username"), info.property("email")),
                         ("google-oauth2|000000000000000000001", "Test User", "test.user", "test.user@example.invalid"))
        self.assertEqual(info.property("accountType"), 0)
        self.assertEqual(info.getAccountTypeString(), "Google")
        self.assertEqual(info.property("subscriptionLevel"), "connect")
        self.assertTrue(info.property("isSubscribed"))
        self.assertEqual(info.property("tectonic"), "eu")
        self.assertEqual(info.property("scopes"), ["sync:tortoise", "intgr", "screenshare", "hwcmail:-1", "mail:-1"])
        self.assertEqual(info.property("createdAt"), 1_577_934_245_000)
        self.assertGreater(info.daysSinceCreation(), 365)
        self.assertEqual(sorted(seen), ["subscribed", "test.user@example.invalid"])

    def test_account_types_and_subscription_levels(self):
        for user_id, kind in (("facebook|1", 1), ("auth0|abc", 2), ("google-oauth2|2", 0)):
            info = UserInfo()
            info.set_from_token(make_jwt({"auth0-profile": {"UserID": user_id}}))
            self.assertEqual(info.property("accountType"), kind, user_id)
        for level, subscribed in (("connect", True), ("free", False), ("", False), ("none", False)):
            info = UserInfo()
            info.set_from_token(make_jwt({"level": level}))
            self.assertEqual(info.property("isSubscribed"), subscribed, level)

    def test_a_token_without_a_profile_uses_sub_and_junk_changes_nothing(self):
        info = UserInfo()
        info.set_from_token(make_jwt({"sub": "auth0|synthetic"}))
        self.assertEqual((info.property("id"), info.property("email")), ("auth0|synthetic", ""))
        self.assertFalse(info.set_from_token("not-a-jwt"))
        self.assertEqual(info.property("id"), "auth0|synthetic")
        info.clear()
        self.assertEqual((info.property("id"), info.property("accountType"), info.property("isSubscribed")), ("", 2, False))


class BusyFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = tl.layer_cloud(crossing_page())
        self.source = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                    editable=("throwaway",))
        self.tree = self.source.refresh()
        self.library = Library()
        self.library.set_tree(self.tree)
        from ui.quick.bridge import library as lib
        lib.set_library_source(lambda: self.library)
        self.selection = DocumentSelection()
        self.library.busyChanged.connect(self.selection.busy_changed)

    def test_entries_and_the_selection_follow_downloads_and_exports(self):
        entry = self.library.entryForId(THROWAWAY)
        changed = []
        self.selection.selectionChanged.connect(lambda: changed.append(1))
        self.selection.select(THROWAWAY) if hasattr(self.selection, "select") else self.selection._set([THROWAWAY])
        changed.clear()
        self.assertFalse(self.selection.property("includesDownloading"))
        self.library.set_downloading(THROWAWAY, True)
        self.library.set_downloading(THROWAWAY, True)
        self.assertTrue(entry.property("isDownloading"))
        self.assertEqual(entry.property("status"), int(EntryWrapper.Status.Downloading))
        self.assertTrue(self.selection.property("includesDownloading"))
        self.library.set_downloading(THROWAWAY, False)
        self.assertTrue(entry.property("isDownloading"), "two downloads, one ended")
        self.library.set_downloading(THROWAWAY, False)
        self.assertFalse(entry.property("isDownloading"))
        self.assertFalse(self.selection.property("includesDownloading"))
        self.library.set_export_progress(THROWAWAY, 0.25)
        self.assertTrue(self.selection.property("includesExporting"))
        self.assertTrue(entry.property("isExporting"))
        self.library.set_export_progress(THROWAWAY, None)
        self.assertFalse(self.selection.property("includesExporting"))
        self.assertEqual(len(changed), 4, "selectionChanged on each begin and end")
        self.selection._set([OTHER])
        self.library.set_export_progress(THROWAWAY, 0.5)
        self.assertFalse(self.selection.property("includesExporting"), "another entry's export")
        self.library.set_export_progress(THROWAWAY, None)

    def test_the_download_listener_reports_cache_misses_only(self):
        calls = []
        doc = self.tree.get(OTHER)
        name = f"{OTHER}/{PAGE1}.rm"
        self.source.download_listener = lambda doc_id, active: calls.append((doc_id, active))
        cached = self.source.sync.blobs.has(doc.files[name])
        self.source.blob(doc, name)
        self.source.blob(doc, name)
        self.assertEqual(calls, [] if cached else [(OTHER, True), (OTHER, False)])
        # Not cached: one begin and one end, even when the download fails.
        path = self.source.sync.blobs.path(doc.files[name])
        if path.exists():
            path.unlink()
        calls.clear()
        self.cloud.offline = True
        with self.assertRaises(Exception):
            self.source.blob(doc, name)
        self.assertEqual(calls, [(OTHER, True), (OTHER, False)])


class BridgePolishQmlTests(unittest.TestCase):
    """Through the app's own QML: Ctrl+V of strokes in text mode, ActionManager's
    actions while an export runs, UserInfo at start, and thumbnails after editing."""

    wait_for, qml, scene_views = tq.EditingTests.wait_for, tq.EditingTests.qml, tq.EditingTests.scene_views
    close_document, handler = tq.EditingTests.close_document, tq.EditingTests.handler
    open_doc = tl.LayersQmlTests.open_doc
    tearDownClass = classmethod(tq.EditingTests.tearDownClass.__func__)
    THROWAWAY, PAGE1 = THROWAWAY, PAGE1

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = tl.layer_cloud(crossing_page())
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud, **CLAIMS), tmp / "cache"),
                                   editable=("throwaway",))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)

    def setUp(self):
        tl.LayersQmlTests.setUp(self)

    def tearDown(self):
        if self.scene_views():
            self.qml("DocumentView_").setProperty("textMode", False)
            tq.wait(100)
        self.close_document()

    def manager_js(self, expression: str):
        from PySide6.QtQml import QQmlExpression, qmlContext
        manager = self.qml("ActionManager")
        return QQmlExpression(qmlContext(manager), manager, expression).evaluate()[0]

    def node(self):
        return self.source.sync.tree.get(THROWAWAY)

    def kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.source.sync.edits.of(THROWAWAY)]

    def strokes(self, view) -> list:
        scene = view._content.scene if view._content is not None else None
        return [st for layer in scene.layers for st in layer.strokes] if scene is not None else []

    def text(self) -> str:
        doc = rm_text.page_text(self.source.staged(self.node()).page(PAGE1))
        return doc.text if doc is not None else ""

    # -- B2 --
    def test_user_info_comes_from_the_token_at_start(self):
        info = self.app.singletons.user_info
        self.assertEqual(info.property("email"), "test.user@example.invalid")
        self.assertEqual(info.property("name"), "Test User")
        self.assertEqual(info.property("accountType"), 0)
        self.assertTrue(info.property("isSubscribed"))

    # -- B1 --
    def test_strokes_pasted_in_text_mode_go_to_the_caret_after_the_text(self):
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: len(self.strokes(view)) == 3))
        ctrl = view._controller
        self.qml("DocumentView_").setProperty("selectionMode", True)
        ctrl.addSelectionRect(QRect(-200, 880, 400, 40), 0)
        self.assertTrue(self.wait_for(lambda: ctrl.property("selectionItemCount") == 1))
        items = ctrl.cloneSelectedItems(0, 1.0)
        self.app.singletons.clipboard.setProperty("items", items)
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "endItemSelection")
        self.qml("DocumentView_").setProperty("selectionMode", False)
        self.qml("DocumentView_").setProperty("textMode", True)
        self.assertTrue(self.wait_for(lambda: view.text_session is not None), self.notices)
        view.text_do(lambda t: t.type_text("Header"))
        caret = ctrl.property("textCursorPosition")
        # The app's paste action in text mode (Clipboard holds items): Ctrl+V.
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "forceActiveFocus")
        self.assertTrue(self.manager_js("pasteAction.enabled"))
        self.manager_js("pasteAction.trigger()")
        self.assertTrue(self.wait_for(lambda: len(self.strokes(view)) == 4), self.notices)
        self.assertEqual(self.kinds()[-1], "StrokePaste")
        self.assertIn("TextEdit", self.kinds())
        self.assertEqual(self.text(), "Header")
        from viewer import rm_select
        box = rm_select.line_box(rm_stroke_items(self.source, self.node())[-1].line)   # with the pen's width
        self.assertAlmostEqual(box[0], caret.x(), delta=1.5)
        self.assertAlmostEqual(box[1], caret.y(), delta=1.5)
        # Undo takes the strokes, then the text; redo brings both back.
        self.qml("DocumentView_").setProperty("textMode", False)
        self.assertTrue(self.wait_for(lambda: view.text_session is None))
        node = self.node()
        self.assertTrue(self.source.undo(node, PAGE1, te.NOW + 1))
        self.assertEqual(len(rm_stroke_items(self.source, node)), 3)
        self.assertEqual(self.text(), "Header")
        self.assertTrue(self.source.undo(node, PAGE1, te.NOW + 2))
        self.assertEqual(self.text().strip(), "")
        self.assertTrue(self.source.redo(node, PAGE1, te.NOW + 3))
        self.assertTrue(self.source.redo(node, PAGE1, te.NOW + 4))
        self.assertEqual(len(rm_stroke_items(self.source, node)), 4)
        self.assertEqual(self.notices, [])

    def test_nothing_copied_inserts_nothing(self):
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: len(self.strokes(view)) == 3))
        view._controller.insertItemsAtCursorPosition([])
        tq.wait(300)
        self.assertEqual(self.kinds(), [])

    # -- B3 --
    def test_actions_are_off_for_an_entry_being_exported_or_downloaded(self):
        selection = self.app.singletons.selection
        selection._set([THROWAWAY])
        self.assertTrue(self.wait_for(lambda: self.manager_js("canRename")))
        self.assertTrue(self.manager_js("canExport"))
        self.app.library.set_export_progress(THROWAWAY, 0.3)
        self.assertTrue(selection.property("includesExporting"))
        for name in ("canRename", "canExport", "canMove", "canDelete", "canCopy"):
            self.assertFalse(self.manager_js(name), name)
        self.app.library.set_export_progress(THROWAWAY, None)
        self.assertTrue(self.manager_js("canRename"))
        self.app.library.downloadActivity.emit(THROWAWAY, True)
        tq.wait(50)
        self.assertTrue(self.app.library.entryForId(THROWAWAY).property("isDownloading"))
        for name in ("canMove", "canDelete", "canCopy"):
            self.assertFalse(self.manager_js(name), name)
        self.app.library.downloadActivity.emit(THROWAWAY, False)
        tq.wait(50)
        self.assertTrue(self.manager_js("canMove"))
        selection._set([])

    # -- B4 --
    def test_an_edited_page_not_uploaded_gets_a_new_thumbnail_on_close(self):
        from ui.quick.bridge.images import ThumbnailProvider
        provider = ThumbnailProvider()
        provider.setProperty("document", self.app.library.entryForId(THROWAWAY))
        provider.setProperty("page", 0)
        before = provider.property("source").toString()
        emitted = []
        provider.sourceChanged.connect(lambda: emitted.append(provider.property("source").toString()))
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: len(self.strokes(view)) == 3))
        view.pen_commit("draw", rm_stroke.draw([(-300 + 20 * k, 1200 + 5 * k) for k in range(12)],
                                               rm_stroke.FINELINER_V2), 1)
        self.assertTrue(self.wait_for(lambda: len(self.strokes(view)) == 4))
        self.close_document()
        self.assertTrue(self.wait_for(lambda: provider.property("source").toString() != before),
                        (before, emitted))
        self.assertIn("StrokeAdd", self.kinds())
        self.assertEqual(self.cloud.handwriting_requests, [])


def rm_stroke_items(source, node) -> list:
    from viewer import rm_select
    return [i for i in rm_select.page_items(source.staged(node).replay(PAGE1).page) if i.is_line]


if __name__ == "__main__":
    unittest.main()
