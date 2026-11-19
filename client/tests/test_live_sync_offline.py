"""Live incoming sync: the notifications socket's "SyncComplete" events and reconnections
turn into syncs (DocumentSync, as reMarkable.exe 3.29's DocumentSyncInterface does), and
a sync that brings a changed page into an open document reloads it in place, its pending
edits replayed over the new version.

Everything is offline: the socket is liveview_fakes.FakeNotifications (fed by hand), the
cloud is fakecloud.FakeCloud, and every id is synthetic.
"""

from __future__ import annotations

import gc
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtNetwork import QNetworkInformation  # noqa: E402

import test_edits_offline as te  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import paired_auth  # noqa: E402
from liveview.session import CloudUpdated, LiveViewService, NotificationsState  # noqa: E402
from liveview_fakes import DESKTOP, TABLET, FakeNotifications, notification  # noqa: E402
from rmfixtures import line_item, points_v2  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.quick.app import QuickApp  # noqa: E402
from ui.quick.bridge import ScreenShareClient  # noqa: E402
from ui.quick.bridge.library import DocumentSync, SyncStatus  # noqa: E402
from viewer import rm_stroke  # noqa: E402

wait = tq.wait
PHONE = "99999999-8888-4777-8666-555555555555"     # another device of the account
DELAY_MS = 120                                      # DocumentSync.REQUEST_DELAY_MS in these tests


def wait_for(condition, ms: int = 5000) -> bool:
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        if condition():
            return True
        wait(20)
    return bool(condition())


def tablet_page(*extra_ids: int) -> bytes:
    """te.drawn_page() with one more stroke per id: the tablet drew on the page."""
    page = te.drawn_page()
    for n, item in enumerate(extra_ids):
        page += line_item(te.LAYER, (1, item), tool=17, color=0,
                          points=points_v2([(9 + 20 * n, 9, 0, 0, 2, 255), (60 + 20 * n, 90, 0, 0, 2, 255)]))
    return page


class FakeSyncer:
    """DocumentSync.request_sync over a fake: a sync runs until finish() is called."""

    def __init__(self, sync: DocumentSync) -> None:
        self.sync = sync
        self.started = 0
        self.running = False

    def request(self) -> bool:
        if self.running:
            return False
        self.running = True
        self.started += 1
        self.sync.set_status(SyncStatus.Syncing)
        return True

    def finish(self, status: SyncStatus = SyncStatus.Completed) -> None:
        self.running = False
        self.sync.set_status(status)


class DocumentSyncTests(unittest.TestCase):
    """DocumentSync's scheduling, on its own."""

    def setUp(self):
        self.sync = DocumentSync()
        self.sync._timer.setInterval(DELAY_MS)
        self.syncer = FakeSyncer(self.sync)
        self.sync.request_sync = self.syncer.request

    def settle(self, ms: int = DELAY_MS * 3) -> None:
        wait(ms)

    def test_an_update_notification_syncs_after_the_delay(self):
        self.sync.onUpdateNotification()
        self.assertEqual(self.syncer.started, 0)              # not at once
        self.assertTrue(self.sync.pending)
        self.settle()
        self.assertEqual(self.syncer.started, 1)
        self.syncer.finish()
        self.settle()
        self.assertEqual(self.syncer.started, 1)
        self.assertFalse(self.sync.pending)

    def test_a_burst_of_notifications_makes_one_sync(self):
        for _ in range(20):
            self.sync.onUpdateNotification()
            wait(DELAY_MS // 10)
        self.settle()
        self.syncer.finish()
        self.settle()
        # The timer is not restarted by later notifications: at most one more sync for
        # those that came after it fired, never one per notification.
        self.assertLessEqual(self.syncer.started, 2)
        self.syncer.finish()
        self.settle()
        self.assertLessEqual(self.syncer.started, 2)

    def test_the_timer_is_not_restarted(self):
        """The app's scheduler keeps a running timer: a steady trickle of notifications
        still syncs within the delay of the first."""
        start = time.monotonic()
        while self.syncer.started == 0 and time.monotonic() - start < 2:
            self.sync.onUpdateNotification()
            wait(DELAY_MS // 4)
        self.assertEqual(self.syncer.started, 1)
        self.assertLess(time.monotonic() - start, DELAY_MS / 1000 + 0.5)

    def test_notifications_while_a_sync_runs_leave_one_sync_after_it(self):
        self.syncer.request()                                  # a sync the user asked for
        for _ in range(5):
            self.sync.onUpdateNotification()
        self.settle()
        self.assertEqual(self.syncer.started, 1)              # it runs: nothing more yet
        self.syncer.finish()
        self.settle()
        self.assertEqual(self.syncer.started, 2)              # one follow-up for the five
        self.syncer.finish()
        self.settle()
        self.assertEqual(self.syncer.started, 2)

    def test_a_sync_running_outside_is_followed_too(self):
        """request_sync says False when the Syncer is busy without DocumentSync knowing
        (the status arrives later): the request is kept for after it."""
        busy = [True]
        started = []
        self.sync.request_sync = lambda: False if busy[0] else (started.append(1) or True)
        self.sync.onUpdateNotification()
        self.settle()
        self.assertEqual(started, [])
        busy[0] = False
        self.sync.set_status(SyncStatus.Completed)
        self.settle()
        self.assertEqual(started, [1])

    def test_a_failed_sync_is_followed_too(self):
        self.syncer.request()
        self.sync.onUpdateNotification()
        self.settle()
        self.syncer.finish(SyncStatus.ConnectivityFailure)
        self.settle()
        self.assertEqual(self.syncer.started, 2)

    def test_a_reconnection_syncs(self):
        self.sync.onConnectedNotification()
        self.settle()
        self.assertEqual(self.syncer.started, 1)

    def test_suspended_nothing_syncs_and_resume_syncs_once(self):
        self.sync.suspend()
        self.assertTrue(self.sync.suspended)
        for _ in range(3):
            self.sync.onUpdateNotification()
        self.sync.onConnectedNotification()
        self.settle()
        self.assertEqual(self.syncer.started, 0)
        self.sync.resume()
        self.settle()
        self.assertEqual(self.syncer.started, 1)
        self.syncer.finish()
        self.settle()
        self.assertEqual(self.syncer.started, 1)

    def test_suspend_stops_a_waiting_request(self):
        self.sync.onUpdateNotification()
        self.sync.suspend()
        self.settle()
        self.assertEqual(self.syncer.started, 0)
        self.sync.resume()
        self.settle()
        self.assertEqual(self.syncer.started, 1)

    def test_resume_without_requests_does_not_sync(self):
        self.sync.suspend()
        self.sync.resume()
        self.sync.resume()                                     # twice: no harm
        self.settle()
        self.assertEqual(self.syncer.started, 0)

    def test_a_follow_up_due_while_suspended_waits_for_resume(self):
        self.syncer.request()
        self.sync.onUpdateNotification()
        self.settle()
        self.sync.suspend()
        self.syncer.finish()
        self.settle()
        self.assertEqual(self.syncer.started, 1)
        self.sync.resume()
        self.settle()
        self.assertEqual(self.syncer.started, 2)


class NotificationFeedTests(unittest.TestCase):
    """The LiveViewService's notifications socket (mocked): what reaches the library."""

    def start(self, **kw):
        self.notifications = FakeNotifications()
        self.events: queue.Queue = queue.Queue()
        self.service = LiveViewService(None, self.events.put, rooms=object(), notifications=self.notifications,
                                       device_id=DESKTOP, **kw)
        self.service.start()
        self.addCleanup(self.service.stop)
        self.assertEqual(self.events.get(timeout=5), NotificationsState(True))

    def push(self, raw: str) -> None:
        self.service.loop.call_soon_threadsafe(self.notifications.push, raw)

    def test_sync_complete_from_another_device_is_passed_on(self):
        self.start()
        self.push(notification("SyncComplete", TABLET))
        self.assertEqual(self.events.get(timeout=5), CloudUpdated(TABLET))

    def test_our_own_sync_complete_is_ignored(self):
        self.start()
        self.push(notification("SyncComplete", DESKTOP))                  # our own commit
        self.push(notification("IntegrationsUpdated", PHONE))              # not the library's
        self.push("not json")
        self.push(notification("SyncComplete", PHONE))
        self.assertEqual(self.events.get(timeout=5), CloudUpdated(PHONE))
        wait(100)
        self.assertTrue(self.events.empty())

    def test_a_reconnection_is_announced(self):
        self.start(reconnect_delays=(0.05,))
        self.service.loop.call_soon_threadsafe(self.notifications.drop)         # the socket is lost
        seen = [self.events.get(timeout=5) for _ in range(2)]
        self.assertEqual(seen, [NotificationsState(False), NotificationsState(True)])
        self.assertEqual(self.notifications.connections, 2)
        self.push(notification("SyncComplete", TABLET))                        # and works again
        self.assertEqual(self.events.get(timeout=5), CloudUpdated(TABLET))

    def test_the_screen_share_client_hands_them_to_the_library(self):
        client = ScreenShareClient(None)
        updates, states = [], []
        client.cloudUpdated.connect(updates.append)
        client.notificationsConnected.connect(states.append)
        client._handle(NotificationsState(True))
        client._handle(CloudUpdated(TABLET))
        client._handle(NotificationsState(False))
        self.assertEqual((updates, states), ([TABLET], [True, False]))
        self.assertFalse(client.server_available)             # not a Screen Share offer


class LiveSyncAppTests(unittest.TestCase):
    """The whole app over the fake cloud, with the notifications socket mocked: what the
    user sees when another device syncs."""

    wait_for = tq.WorkflowTests.wait_for
    qml, scene_views = tq.WorkflowTests.qml, tq.WorkflowTests.scene_views
    open_document, close_document = tq.WorkflowTests.open_document, tq.WorkflowTests.close_document

    @classmethod
    def setUpClass(cls):
        cls.delay = DocumentSync.REQUEST_DELAY_MS
        DocumentSync.REQUEST_DELAY_MS = DELAY_MS
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = cls.dir = Path(cls.tmp.name)
        cls.cloud = te.build_cloud()
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway",))
        cls.source.refresh()
        cls.network = tq.FakeNetwork()
        cls.notifications = FakeNotifications()

        def service(emit):
            return LiveViewService(None, emit, rooms=object(), notifications=cls.notifications, device_id=DESKTOP,
                                   reconnect_delays=(0.05,))
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp, liveview_factory=service,
                           network_information=cls.network)
        cls.app.singletons.settings.setRawValue("Onboarding", "ClientEdit", True)
        cls.app.load()
        cls.app.start(sync=False)
        wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.service = cls.app.singletons.screen_share.service

    @classmethod
    def tearDownClass(cls):
        DocumentSync.REQUEST_DELAY_MS = cls.delay
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        wait(50)
        cls.tmp.cleanup()

    def setUp(self):
        self.sync = self.app.singletons.document_sync
        self.settle()
        self.reset_cloud()
        self.settle()
        self.puts = len(self.cloud.puts())

    def uploaded_pages(self) -> list[str]:
        """Pages (.rm) uploaded since the test started: pending edits stay local (the
        reading position saved on closing a document is a .metadata upload)."""
        return [h.get("rm-filename", "") for _, h in self.cloud.puts()[self.puts:]
                if h.get("rm-filename", "").endswith(".rm")]

    def tearDown(self):
        self.close_document()
        self.source.sync.edits.clear()
        self.network.set(QNetworkInformation.Reachability.Online)

    def settle(self) -> None:
        """Until no sync is running or waiting."""
        self.assertTrue(self.wait_for(lambda: not self.app.syncer.running and not self.sync.pending, 10000))
        wait(DELAY_MS * 2)
        self.assertTrue(self.wait_for(lambda: not self.app.syncer.running and not self.sync.pending, 10000))

    def reset_cloud(self) -> None:
        self.cloud.put_item(te.THROWAWAY, te.meta("throwaway"), te.notebook_content(te.PAGE1, te.PAGE2),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page()})
        self.push(notification("SyncComplete", TABLET))

    def push(self, raw: str) -> None:
        self.service.loop.call_soon_threadsafe(self.notifications.push, raw)

    def roots(self) -> int:
        return sum(1 for p in self.cloud.requests if p in ("/sync/v3/root", "/sync/v4/root"))

    def tablet_draws(self, *ids: int, pages=(te.PAGE1, te.PAGE2), name: str = "throwaway") -> None:
        self.cloud.put_item(te.THROWAWAY, te.meta(name), te.notebook_content(*pages),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": tablet_page(*ids)})
        self.push(notification("SyncComplete", TABLET))

    @staticmethod
    def strokes(view) -> int:
        scene = view._content.scene if view._content is not None else None
        return len(scene.strokes) if scene is not None else 0

    def test_the_socket_connecting_synced_and_live_sync_is_on(self):
        self.assertFalse(self.sync.suspended)
        self.assertTrue(self.app._online)

    def test_another_device_syncing_updates_the_library(self):
        roots = self.roots()
        self.tablet_draws(16, name="renamed on the tablet")
        entry = self.app.library.entryForId(te.THROWAWAY)
        self.assertTrue(self.wait_for(lambda: entry.property("visibleName") == "renamed on the tablet"))
        self.settle()
        self.assertEqual(self.roots() - roots, 1)
        self.assertEqual(self.cloud.puts()[self.puts:], [])   # nothing is uploaded

    def test_our_own_sync_complete_does_not_sync(self):
        roots = self.roots()
        self.push(notification("SyncComplete", DESKTOP))
        wait(DELAY_MS * 4)
        self.settle()
        self.assertEqual(self.roots(), roots)

    def test_a_burst_of_notifications_syncs_once(self):
        roots = self.roots()
        self.cloud.put_item(te.THROWAWAY, te.meta("burst"), te.notebook_content(te.PAGE1, te.PAGE2),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page()})
        for _ in range(10):
            self.push(notification("SyncComplete", TABLET))
        self.settle()
        self.assertEqual(self.roots() - roots, 1)
        self.assertEqual(self.app.library.entryForId(te.THROWAWAY).property("visibleName"), "burst")

    def test_a_notification_during_a_sync_queues_one_more(self):
        release, transport = threading.Event(), self.app.auth.transport

        def slow(method, url, headers, body, timeout):
            if "/sync/" in url:
                release.wait(10)
            return transport(method, url, headers, body, timeout)
        roots = self.roots()
        self.app.auth.transport = slow
        try:
            self.assertTrue(self.app.syncer.refresh())         # "Check sync", say: it reads the old root
            self.tablet_draws(16, name="during the sync")
            self.tablet_draws(16, 17, name="during the sync")
            wait(DELAY_MS * 3)
            self.assertTrue(self.sync.pending)                 # waiting for the running sync
            release.set()
            self.settle()
        finally:
            self.app.auth.transport = transport
        self.assertEqual(self.roots() - roots, 2)              # the running one, then one more
        self.assertEqual(self.app.library.entryForId(te.THROWAWAY).property("visibleName"), "during the sync")

    def test_a_reconnection_syncs(self):
        """What changed while the socket was down was not announced: a reconnection syncs."""
        roots, connections = self.roots(), self.notifications.connections
        self.cloud.put_item(te.THROWAWAY, te.meta("missed while away"), te.notebook_content(te.PAGE1, te.PAGE2),
                            {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page()})
        self.service.loop.call_soon_threadsafe(self.notifications.drop)
        self.assertTrue(self.wait_for(lambda: self.notifications.connections == connections + 1))
        self.settle()
        self.assertEqual(self.roots() - roots, 1)
        self.assertEqual(self.app.library.entryForId(te.THROWAWAY).property("visibleName"), "missed while away")

    def test_offline_nothing_syncs_until_the_network_is_back(self):
        roots = self.roots()
        self.network.set(QNetworkInformation.Reachability.Disconnected)
        self.assertTrue(self.sync.suspended)
        self.tablet_draws(16, name="while offline")
        self.push(notification("SyncComplete", TABLET))
        wait(DELAY_MS * 4)
        self.assertEqual(self.roots(), roots)
        self.network.set(QNetworkInformation.Reachability.Online)
        self.assertFalse(self.sync.suspended)
        self.settle()
        self.assertEqual(self.roots() - roots, 1)
        self.assertEqual(self.app.library.entryForId(te.THROWAWAY).property("visibleName"), "while offline")

    def test_the_open_page_is_reloaded_in_place(self):
        view = self.open_document(te.THROWAWAY)
        self.assertEqual(self.strokes(view), 1)
        self.tablet_draws(16)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 2))
        self.settle()
        self.assertEqual(self.scene_views(), [view])           # the same page view: not reopened
        self.assertTrue(self.qml("DocumentView_").property("visible"))
        self.assertEqual(view.property("status"), 2)
        self.assertEqual(tq.real_warnings(self.app.warnings), [])

    def test_a_change_to_another_page_does_not_reload_the_open_one(self):
        view = self.open_document(te.THROWAWAY)
        loads = []
        original = view._load
        view._load = lambda quiet=False: (loads.append(quiet), original(quiet))
        try:
            self.cloud.put_item(te.THROWAWAY, te.meta("throwaway"), te.notebook_content(te.PAGE1, te.PAGE2),
                                {f"{te.THROWAWAY}/{te.PAGE1}.rm": te.drawn_page(),
                                 f"{te.THROWAWAY}/{te.PAGE2}.rm": tablet_page(16)})
            self.push(notification("SyncComplete", TABLET))
            self.settle()
        finally:
            del view._load
        self.assertEqual(loads, [])
        self.assertEqual(self.scene_views(), [view])

    def test_pending_edits_are_replayed_over_the_new_version(self):
        view = self.open_document(te.THROWAWAY)
        node = view._get("document").node
        ours = self.source.add_stroke(node, te.PAGE1, rm_stroke.draw([(200 + 9 * k, 500 + 4 * k) for k in range(10)],
                                                                    rm_stroke.FINELINER_V2), te.NOW)
        view._load(quiet=True)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 2))
        self.tablet_draws(16, 17)                              # the tablet draws two strokes on the same page
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 4))     # its three and ours
        self.settle()
        self.assertEqual(self.scene_views(), [view])
        self.assertEqual(self.source.sync.edits.all(), [ours])               # still pending, not dropped
        self.assertEqual(self.uploaded_pages(), [])                          # and not uploaded
        # Reopened later, the page still has both.
        self.close_document()
        view = self.open_document(te.THROWAWAY)
        self.assertEqual(self.strokes(view), 4)

    def test_a_selection_puts_the_reload_off_until_it_ends(self):
        view = self.open_document(te.THROWAWAY)
        view._sel = SimpleNamespace(items=())                  # something is selected
        try:
            self.tablet_draws(16)
            self.settle()
            wait(200)
            self.assertEqual(self.strokes(view), 1)            # not under the user's selection
            self.assertTrue(view._stale)
        finally:
            view._set_selection(None)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 2))
        self.assertFalse(view._stale)

    def test_a_page_removed_by_the_tablet_rebuilds_the_pages(self):
        view = self.open_document(te.THROWAWAY)
        self.tablet_draws(16, pages=(te.PAGE1,))
        entry = self.app.library.entryForId(te.THROWAWAY)
        self.assertTrue(self.wait_for(lambda: entry.property("pageCount") == 1))
        self.settle()
        self.assertTrue(self.qml("DocumentView_").property("visible"))       # still in the document
        self.assertTrue(self.wait_for(lambda: any(self.strokes(v) == 2 for v in self.scene_views())))
        del view

    def test_logged_out_live_sync_is_suspended(self):
        self.app._go_offline()
        try:
            self.assertTrue(self.sync.suspended)
        finally:
            self.app._go_online(sync=False)
        self.assertFalse(self.sync.suspended)


if __name__ == "__main__":
    unittest.main()
