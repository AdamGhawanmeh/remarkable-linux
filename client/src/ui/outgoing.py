"""Outgoing sync: the edits made here go to the cloud in the background.

reMarkable.exe uploads a changed document by itself: an edit marks the document and asks
for an entry sync (DocumentSyncInterface::requestEntrySync, 0x140137620, "entry sync
requested, id={}; fullSync={}, entries={}"), which goes through the same 10 ms
scheduler timer as incoming syncs (0x140136bb0) and is skipped while suspended or
offline ("Ignoring sync, suspended", "Ignoring sync; we are not online", 0x140138530).
Here an upload is a whole root commit (core.outbox), so edits are gathered for a pause
(DEBOUNCE_MS) first: a page of strokes goes up as one commit, not one per stroke.

    EditSettings        the two local settings, in settings.ini: editing on (off by
                        default: the app stays a reader until it is turned on) and
                        automatic upload (off by default: "Upload now" uploads)
    OutgoingSync        the uploader: watches PendingEdits, uploads on a worker thread
                        (core.outbox.push_all, the path tools/push_edits.py uses too),
                        retries with backoff when offline or when the cloud kept
                        changing, and shows each document's state on its library entry
                        (Entry.Uploading, Entry.SyncError) and in Settings > Editing
    install_settings_cell
                        Settings > Editing: the two toggles, and what is waiting to
                        upload with an Upload now button; added at run time to the app's
                        own General settings (main.qml's settingsDialog), whose QML is
                        left as shipped

Uploads and incoming syncs never overlap: both hold SyncClient._busy (core.outbox).
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Property, QByteArray, QObject, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtQml import QQmlComponent, QQmlContext

from core import outbox

from .library import LibrarySource
from .workers import _Job, emit_from_worker

log = logging.getLogger("rm.ui.outgoing")

EDITING_KEY = "LinuxClient/editingEnabled"
AUTO_SYNC_KEY = "LinuxClient/autoSyncEdits"

DEBOUNCE_MS = 3000                                    # edits gathered for this long, then uploaded
RETRY_MS = (5_000, 15_000, 30_000, 60_000, 120_000, 300_000)   # after a failed upload, one after another

UPLOADING, SYNC_ERROR = 4, 64                         # Entry.Status (bridge.library.EntryWrapper)

IDLE, PENDING, UPLOADING_STATE, RETRYING, ERROR, WAITING = (
    "idle", "pending", "uploading", "retrying", "error", "waiting")


def _flag(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class EditSettings:
    """Editing on and automatic upload, kept with the app's settings (a SettingsStore:
    ~/.config/remarkable-linux/settings.ini). Both off unless turned on."""

    def __init__(self, store) -> None:
        self.store = store

    @property
    def editing_enabled(self) -> bool:
        return _flag(self.store.get(EDITING_KEY), False)

    @editing_enabled.setter
    def editing_enabled(self, value: bool) -> None:
        self.store.set(EDITING_KEY, bool(value))
        self.store.sync()

    @property
    def auto_sync(self) -> bool:
        return _flag(self.store.get(AUTO_SYNC_KEY), False)

    @auto_sync.setter
    def auto_sync(self, value: bool) -> None:
        self.store.set(AUTO_SYNC_KEY, bool(value))
        self.store.sync()


class OutgoingSync(QObject):
    """Uploads pending edits in the background (see the module's doc). The app sets
    `can_upload()` (logged in, online, not --offline), connects `uploaded` (show the
    tree), `statuses` (Library.set_upload_statuses), `unauthorized` and `editingChanged`,
    and calls network_available(), documents_open() and flush() as things happen."""

    changed = Signal()                       # any property the QML reads
    editingChanged = Signal()                # editing turned on or off
    uploaded = Signal(object)                # DocumentTree after an upload
    statuses = Signal(object)                # {doc id: (Entry status, progress)}
    unauthorized = Signal(str)
    _ledger = Signal()                       # PendingEdits changed (from any thread)
    _done = Signal(object)                   # (results, failures) from the worker
    _progress = Signal(str, int, int)        # doc id, bytes sent, total

    def __init__(self, source: LibrarySource, settings: EditSettings, notify: Callable[[str], None] | None = None,
                 parent: QObject | None = None, debounce_ms: int = DEBOUNCE_MS,
                 retry_ms: tuple[int, ...] = RETRY_MS) -> None:
        super().__init__(parent)
        self.source = source
        self.settings = settings
        self.notify = notify or (lambda message: None)
        self.can_upload: Callable[[], bool] = lambda: source.writable
        self.retry_ms = retry_ms
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._debounce = QTimer(self, singleShot=True, interval=debounce_ms)
        self._debounce.timeout.connect(lambda: self._start("pause after editing"))
        self._retry = QTimer(self, singleShot=True)
        self._retry.timeout.connect(lambda: self._start("retry"))
        self._backoff = 0
        self._uploading = False
        self._again = False                  # flush() while uploading: once more right after
        self._requested = False              # Upload now asked: keep at it until it goes through
        self._failed: dict[str, str] = {}    # doc id -> why its last upload failed (shown as SyncError)
        self._rejected: dict[str, tuple] = {}   # doc id -> its edits when refused (not retried until they change)
        self._progress_of: dict[str, float] = {}
        self._state = IDLE
        self._message = ""
        self._open: set[str] = set()
        self._closed = False
        self._pending = self._count()
        self._ledger.connect(self._ledger_changed)
        self._done.connect(self._finished)
        self._progress.connect(self._progressed)
        self._listener = self._on_ledger
        source.sync.edits.listeners.append(self._listener)
        source.edit_everything = settings.editing_enabled
        self._update_state()

    # -- settings -------------------------------------------------------------------------

    def _get_editing(self) -> bool:
        return self.settings.editing_enabled

    def _set_editing(self, value: bool) -> None:
        if bool(value) == self.settings.editing_enabled:
            return
        self.settings.editing_enabled = bool(value)
        self.source.edit_everything = bool(value)
        log.info("editing turned %s", "on" if value else "off")
        self.changed.emit()
        self.editingChanged.emit()

    def _get_auto(self) -> bool:
        return self.settings.auto_sync

    def _set_auto(self, value: bool) -> None:
        if bool(value) == self.settings.auto_sync:
            return
        self.settings.auto_sync = bool(value)
        log.info("automatic upload of edits turned %s", "on" if value else "off")
        if value:
            self._schedule()
        else:
            self._debounce.stop()
        self._update_state()

    editingEnabled = Property(bool, _get_editing, _set_editing, notify=changed)
    autoSync = Property(bool, _get_auto, _set_auto, notify=changed)
    editingLimitedTo = Property(str, lambda self: ", ".join(self.source.editable), notify=changed)
    editingActive = Property(bool, lambda self: self.source.editing_on, notify=changed)

    # -- what QML shows -------------------------------------------------------------------

    pendingCount = Property(int, lambda self: self._pending, notify=changed)
    state = Property(str, lambda self: self._state, notify=changed)
    statusText = Property(str, lambda self: self._message, notify=changed)
    busy = Property(bool, lambda self: self._uploading, notify=changed)

    def _count(self) -> int:
        try:
            return len(self.source.sync.edits.all())
        except Exception:               # an unreadable ledger reads as empty (PendingEdits logs it)
            return 0

    def _documents(self) -> list[str]:
        return self.source.sync.edits.documents()

    def _update_state(self) -> None:
        self._pending = self._count()
        docs = self._documents()
        if self._uploading:
            state = UPLOADING_STATE
        elif not docs:
            state = IDLE
        elif self._retry.isActive():
            state = RETRYING
        elif any(d in self._rejected for d in docs) and not self._waiting_docs(docs):
            state = ERROR
        elif not self.can_upload():
            state = WAITING
        else:
            state = PENDING
        n, d = self._pending, len(docs)
        texts = {
            IDLE: "All edits are in the cloud",
            PENDING: f"{n} edit{'s' * (n != 1)} in {d} document{'s' * (d != 1)} to upload",
            UPLOADING_STATE: "Uploading edits...",
            RETRYING: f"Not uploaded ({next(iter(self._failed.values()), 'offline')}): trying again soon",
            ERROR: "Not uploaded: " + "; ".join(sorted({self._failed.get(x, "") for x in docs if x in self._rejected})),
            WAITING: f"{n} edit{'s' * (n != 1)} to upload once online",
        }
        self._state, self._message = state, texts[state]
        statuses = {}
        for doc_id in docs:
            if doc_id in self._failed and not (self._uploading and doc_id in self._progress_of):
                statuses[doc_id] = (SYNC_ERROR, 0.0)
            else:
                statuses[doc_id] = (UPLOADING, self._progress_of.get(doc_id, 0.0))
        for doc_id in self._progress_of:            # uploading now (its edits may be settled already)
            statuses.setdefault(doc_id, (UPLOADING, self._progress_of[doc_id]))
        self.statuses.emit(statuses)
        self.changed.emit()

    def _waiting_docs(self, docs) -> list[str]:
        """Documents with edits that would go with the next upload (not refused as they are)."""
        out = []
        for doc_id in docs:
            refused = self._rejected.get(doc_id)
            if refused is None or refused != tuple(self.source.sync.edits.of(doc_id)):
                out.append(doc_id)
        return out

    # -- what starts an upload ------------------------------------------------------------

    def _on_ledger(self) -> None:           # any thread
        emit_from_worker(self, self._ledger.emit)

    def _ledger_changed(self) -> None:
        if self._closed:
            return
        for doc_id in list(self._rejected):
            if self._rejected[doc_id] != tuple(self.source.sync.edits.of(doc_id)):
                del self._rejected[doc_id]          # changed since it was refused: try it again
        docs = set(self._documents())
        for doc_id in list(self._failed):
            if doc_id not in docs:
                del self._failed[doc_id]            # nothing of it pending any more
        if self.settings.auto_sync:
            self._schedule()
        self._update_state()

    def _schedule(self) -> None:
        """Automatic upload: start (or restart) the pause after the last edit."""
        if self._closed or self._uploading or self._retry.isActive():
            return
        if self._waiting_docs(self._documents()):
            self._debounce.start()

    @Slot()
    def uploadNow(self) -> None:
        """Settings > Editing > Upload now (and Check sync): upload what is pending now,
        automatic upload or not, and keep trying until it goes through."""
        self._rejected.clear()                      # asked for: what was refused is tried again
        self.flush("asked for", requested=True)

    def retry(self, doc_id: str) -> None:
        """Entry.handleSyncError(Retry) on a document's cloud_x icon."""
        self._rejected.pop(doc_id, None)
        self.flush(f"retry of {doc_id}", requested=True)

    def flush(self, why: str = "flush", requested: bool = False) -> bool:
        """Upload now what is pending (no pause): leaving a document with automatic
        upload on, Upload now, Check sync. False when nothing was started."""
        if self._closed:
            return False
        if requested:
            self._requested = True
            self._backoff = 0
        if not self._documents():
            self._update_state()
            return False
        if self._uploading:
            self._again = True
            return False
        self._debounce.stop()
        self._retry.stop()
        return self._start(why)

    def network_available(self) -> None:
        """Back online (or logged in): what waits for the network goes now."""
        if self._closed or self._uploading:
            return
        if self._documents() and (self.settings.auto_sync or self._requested):
            self._retry.stop()
            self._backoff = 0
            self._start("back online")
        else:
            self._update_state()

    def documents_open(self, ids) -> None:
        """The documents shown now; leaving one uploads its edits at once when uploads
        are automatic (the app syncs a document as it closes)."""
        ids = set(ids)
        left = self._open - ids
        self._open = ids
        if left and self.settings.auto_sync and set(self._documents()) & left:
            self.flush("document closed")

    def start(self) -> None:
        """The app started: edits left from before go up (automatic upload on)."""
        if self.settings.auto_sync:
            self._schedule()
        self._update_state()

    # -- the upload -----------------------------------------------------------------------

    def _start(self, why: str) -> bool:
        if self._closed or self._uploading:
            return False
        if not self.can_upload():
            log.info("edits not uploaded (%s): offline or logged out; they wait", why)
            self._update_state()
            return False
        docs = self._documents()
        skip = {d for d in docs if self.source.created(d) is not None}   # being made in the cloud
        skip |= {d for d in docs if d in self._rejected and d not in self._waiting_docs([d])}
        if not [d for d in docs if d not in skip]:
            if skip - set(self._rejected) and (self.settings.auto_sync or self._requested):
                self._retry.start(self.retry_ms[0])       # a new document still being made
            self._update_state()
            return False
        log.info("uploading edits (%s): %d documents", why, len(docs) - len(skip))
        self._uploading = True
        self._again = False
        self._progress_of = {d: 0.0 for d in docs if d not in skip}
        self._update_state()
        sync, settle = self.source.sync, self.source.settle_upload

        def job():
            try:
                done = outbox.push_all(sync, lambda d, sent, total: emit_from_worker(
                    self, lambda: self._progress.emit(d, sent, total)), settle, skip)
            except Exception as err:        # never lose the worker's result: report it as a failure
                log.exception("uploading edits")
                done = ([], [outbox.PushFailed("", outbox.REJECTED, err)])
            emit_from_worker(self, lambda: self._done.emit(done))
        self.pool.start(_Job(job))
        return True

    def _progressed(self, doc_id: str, sent: int, total: int) -> None:
        if doc_id in self._progress_of:
            self._progress_of[doc_id] = sent / total if total else 0.0
            self._update_state()

    def _finished(self, done) -> None:
        results, failures = done
        self._uploading = False
        self._progress_of = {}
        for result in results:
            self._failed.pop(result.doc_id, None)
            self._rejected.pop(result.doc_id, None)
        transient = [f for f in failures if f.kind in (outbox.OFFLINE, outbox.CONFLICT)]
        for failed in failures:
            name = self._name(failed.doc_id)
            reason = "offline" if failed.kind == outbox.OFFLINE else str(failed)
            self._failed[failed.doc_id] = reason
            if failed.kind == outbox.REJECTED:
                self._rejected[failed.doc_id] = tuple(self.source.sync.edits.of(failed.doc_id))
                self.notify(f"The edits of {name} could not be uploaded: {failed}. They are kept on this computer.")
            elif failed.kind == outbox.AUTH:
                self.unauthorized.emit(str(failed))
        if results:
            self.uploaded.emit(self.source.sync.tree)
        if self._closed:
            return
        if transient:
            if self.settings.auto_sync or self._requested:
                delay = self.retry_ms[min(self._backoff, len(self.retry_ms) - 1)]
                self._backoff += 1
                log.info("edits not uploaded (%s); trying again in %d s", transient[0].kind, delay // 1000)
                self._retry.start(delay)
        else:
            self._backoff = 0
            if not failures and not self._waiting_docs(self._documents()):
                self._requested = False
            if self._again:
                self._again = False
                if self._waiting_docs(self._documents()):
                    self._start("asked for while uploading")
                    return
            elif self.settings.auto_sync:
                self._schedule()                     # edits made while it uploaded
        self._update_state()

    def _name(self, doc_id: str) -> str:
        node = self.source.synced(doc_id) if doc_id else None
        return f"“{node.name}”" if node is not None else (doc_id or "a document")

    # -- the end --------------------------------------------------------------------------

    def wait(self, msecs: int = 60000) -> bool:
        return self.pool.waitForDone(msecs)

    def shutdown(self, msecs: int = 60000) -> None:
        """Stop the timers (pending edits stay on disk: the next start uploads them) and
        let an upload under way finish."""
        self._closed = True
        self._debounce.stop()
        self._retry.stop()
        try:
            self.source.sync.edits.listeners.remove(self._listener)
        except ValueError:
            pass
        self.wait(msecs)
        self.can_upload = lambda: False         # drop the app's closures (no cycle left for the GC)
        self.notify = lambda message: None


# -- Settings > Editing -------------------------------------------------------------------

SETTINGS_CELL_QML = b"""
import QtQuick
import QtQuick.Layouts
import ark.controls as ArkControls

ArkControls.Cell {
    id: cell
    objectName: "linuxEditingCell"
    title: "Editing"
    readonly property var settingsDialog: appWindow ? appWindow.settingsDialog : null
    width: settingsDialog ? settingsDialog.cellWidth : 500

    Component.onCompleted: {
        // After the Cloud cell (Check sync), in the app's own General settings.
        if (settingsDialog && settingsDialog.model)
            settingsDialog.model.insert(Math.min(2, settingsDialog.model.count), cell);
    }

    ArkControls.CellItem {
        objectName: "linuxEditingToggleItem"
        text: "Edit notebooks, PDFs and ebooks"
        status: editing.editingLimitedTo !== "" ? "Only \\u201c" + editing.editingLimitedTo + "\\u201d (--edit-notebook)" : ""
        iconSource: ""
        enabled: editing.editingLimitedTo === ""
        onClicked: editing.editingEnabled = !editing.editingEnabled
        ArkControls.Toggle {
            objectName: "linuxEditingToggle"
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            visible: editing.editingLimitedTo === ""
            checked: editing.editingEnabled
            onToggled: editing.editingEnabled = checked
        }
    }

    ArkControls.CellItem {
        objectName: "linuxAutoSyncItem"
        text: "Upload edits automatically"
        iconSource: ""
        onClicked: editing.autoSync = !editing.autoSync
        ArkControls.Toggle {
            objectName: "linuxAutoSyncToggle"
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            checked: editing.autoSync
            onToggled: editing.autoSync = checked
        }
    }

    ArkControls.CellItem {
        objectName: "linuxUploadNowItem"
        text: editing.busy ? "Uploading..." : (editing.pendingCount > 0 ? "Upload now" : "Edits")
        status: editing.statusText
        iconSource: editing.state === "error" || editing.state === "retrying" ? "qrc:/ark/icons/cloud_x"
                    : (editing.pendingCount > 0 ? "qrc:/ark/icons/cloud_upload" : "qrc:/ark/icons/checkmark")
        enabled: !editing.busy && editing.pendingCount > 0
        onClicked: editing.uploadNow()
    }
}
"""


def install_settings_cell(engine, window, outgoing: OutgoingSync) -> QObject | None:
    """Add Settings > Editing to main.qml's General settings (its `settingsDialog`). None
    when the window has no such dialog."""
    if window is None or window.metaObject().indexOfProperty("settingsDialog") < 0:
        log.warning("no settingsDialog in main.qml: Settings > Editing not added")
        return None
    # Qt owns everything made here (the window is the parent): nothing of it is left for
    # Python's garbage collector to destroy later, on whatever thread it then runs.
    context = QQmlContext(engine.rootContext(), window)
    context.setContextProperty("editing", outgoing)
    context.setContextProperty("appWindow", window)
    component = QQmlComponent(engine)
    component.setData(QByteArray(SETTINGS_CELL_QML), QUrl("qrc:/linux/EditingSettings.qml"))
    cell = component.create(context)
    if cell is None:
        log.error("Settings > Editing: %s", "; ".join(e.toString() for e in component.errors()))
        return None
    cell.setParent(window)
    return cell

