"""The QML desktop client: the app's own main.qml and QML, hosted by PySide6's QML engine.

    app = QuickApp(source, cache_dir, config_dir=..., liveview_factory=...)
    app.load()            # registers resources, fonts, translations, types; loads main.qml
    app.start(sync=True)  # the cached library, then (logged in) Screen Share and a sync

Logging in and out is the app's own flow: logged out (no device token in auth.json), main.qml's
LoginWizard covers the window and asks for the one-time code from my.remarkable.com; QuickApp
pairs in the background (AuthClient.login) and, once paired, syncs the library into the same
window. Log out (LogoutConfirmDialog) deletes this device on the server, the tokens and the
cached library, and shows the wizard again; so does the cloud rejecting the device token.

Everything on screen is the official QML from the app's resources (qrc:/qml/main.qml and
what it imports, unmodified), over Python implementations of the app's C++ types
(bridge/); pages are drawn by this client's renderer inside the app's SceneView
(bridge/scene.py).

QML warnings are collected in `app.warnings` (and logged), so tests and screenshots can
assert that every binding resolved.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTranslator, QUrl, QtMsgType, qInstallMessageHandler, qVersion
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from core.auth import AuthError, AuthNetworkError
from core.models import DocumentTree

from ..library import LibrarySource
from ..workers import Account, InternetCheck, Mutator, Syncer
from . import assets
from .bridge import Singletons, register_types, use_singletons
from .bridge.exporter import DocumentExporter
from .bridge.images import DragIconImageProvider, ThumbnailImageProvider
from .bridge.importer import DocumentImporter
from .bridge.library import DocumentSelection, DocumentSync, Library, LibraryController, SyncStatus
from .bridge.platform import (LOGIN_RESULTS, ArkIconProvider, ArkTokensSettings, Authentication, Clipboard,
                              DocumentController, HandwritingLanguageModel, LanguageSettings, LinkProvider,
                              NetworkClient, PlatformApp, PlatformHelpers, Settings, SettingsStore, UserInfo,
                              login_result)
from .bridge.scene import Recognizer, set_clipboard, set_edit_notifier, set_page_source
from .bridge.screenshare import ScreenShareClient

log = logging.getLogger("rm.ui.quick")
MAIN_URL = QUrl("qrc:/qml/main.qml")
OFFLINE_LOGIN = "reMarkable is running offline (--offline). Restart it without --offline to log in."
LOGGED_OUT_BY_CLOUD = "This app is no longer connected to your reMarkable account. Log in again with a new code."
IMPORT_PATHS = ("qrc:/qml", "qrc:/ark-imports")
TRANSLATION = ":/i18n/translations/reMarkable_{}.qm"
# qrc files replaced by this client's (path -> file); none: the app's QML runs as shipped.
OVERRIDES: dict[str, Path] = {}
# The UI version these QML files are (reMarkable desktop 3.29.0, build 1804).
APP_VERSION, APP_BUILD = "3.29.0", "1804"
# Fonts main() adds besides the reMarkable families (assets.FONT_FILES).
QRC_FONTS = (":/fonts/EBGaramond-Italic-VariableFont_wght.ttf", ":/fonts/EBGaramond-VariableFont_wght.ttf")


def prepare() -> None:
    """What the app's main() sets before creating its QGuiApplication."""
    QQuickStyle.setStyle("Basic")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def configure(application: QGuiApplication) -> None:
    """...and after: names, closing behaviour and the light colour scheme."""
    application.setOrganizationName("remarkable")
    application.setOrganizationDomain("remarkable.com")
    application.setQuitOnLastWindowClosed(True)
    application.styleHints().setColorScheme(Qt.ColorScheme.Light)


def sync_failure_status(error: Exception | None) -> SyncStatus:
    """rmsync::Status for a failed sync. Network failures inside the sync itself arrive
    as SyncError after its retries, and get the app's general failure text."""
    if isinstance(error, (OSError, AuthNetworkError)):
        return SyncStatus.ConnectivityFailure
    if isinstance(error, AuthError):
        return SyncStatus.AuthenticationFailure
    return SyncStatus.Failed


class QuickApp:
    def __init__(self, source: LibrarySource, cache_dir: Path, *, config_dir: Path | None = None,
                 liveview_factory: Callable | None = None, now=None, main: QUrl = MAIN_URL,
                 network_information=None) -> None:
        self.source = source
        self.cache_dir = cache_dir
        self.main = main
        self.warnings: list[str] = []
        self.closing = False          # set by shutdown(): bindings torn down with the engine are not errors
        self.library = Library(now=now)
        self.mutator = Mutator(source)
        self.syncer = Syncer(source)
        self.auth = source.sync.auth
        self.internet = InternetCheck(self.auth)
        self.account = Account(self.auth)
        self.sync_on_login = True
        self._check_waiting = False      # "Check sync" asked for a sync while one was running
        self._online = False             # Screen Share started for this log-in
        store = SettingsStore(config_dir / "settings.ini" if config_dir else None)
        # ExportDialog -> PlatformHelpers.exportFile; results show as the app's notifications.
        self.exporter = DocumentExporter(source, self.library,
                                         notify=lambda message: self.singletons.platform_helpers.errorMessage.emit(message))
        self.library.abort_export = self.exporter.abort
        self.library.save_reading_position = self._save_reading_position
        self.singletons = Singletons(
            library=self.library,
            controller=LibraryController(self.library, self.mutator.run,
                                         inherit_editing=getattr(source, "inherit_editing", None)),
            importer=DocumentImporter(self.mutator.run), selection=DocumentSelection(),
            document_sync=DocumentSync(),
            screen_share=ScreenShareClient(liveview_factory),
            settings=Settings(store), ark_settings=ArkTokensSettings(),
            platform_helpers=PlatformHelpers(store, export=self.exporter.export),
            platform_app=PlatformApp(on_logout=self.logout),
            # Offline there is no logging in: the cached library is all there is.
            authentication=Authentication(self.auth.is_paired or source.cached_only, login=self.login),
            user_info=UserInfo(),
            network_client=NetworkClient(), document_controller=DocumentController(self.library),
            clipboard=Clipboard(), language=LanguageSettings(store), link=LinkProvider(information=network_information),
            handwriting=HandwritingLanguageModel(), recognizer=Recognizer())
        self.item_inspector = QObject()
        s = self.singletons
        self.engine: QQmlApplicationEngine | None = None
        self.translator: QTranslator | None = None
        self.thumbnails: ThumbnailImageProvider | None = None
        self.icons: ArkIconProvider | None = None
        # FileImportDialog hands picked files to PlatformHelpers.documentAccepted.
        s.platform_helpers.documentAccepted.connect(
            lambda url, name, parent_id, scanned, delete_later: s.importer.importFromUrls([url], parent_id))
        self.library.openDocumentsChanged.connect(self._update_editing)
        self.mutator.finished.connect(lambda tree, message: self.set_tree(tree))
        self.mutator.failed.connect(self.library.errorMessage.emit)
        self.syncer.started.connect(lambda: s.document_sync.set_status(SyncStatus.Syncing))
        self.syncer.progress.connect(s.document_sync.set_progress)
        self.syncer.finished.connect(self._synced)
        self.syncer.failed.connect(self._sync_failed)
        self.syncer.unauthorized.connect(self._unauthorized)
        self.account.loggedIn.connect(self._logged_in)
        self.account.loginFailed.connect(self._login_failed)
        self.account.loggedOut.connect(self._logged_out)
        # Settings > Cloud > "Check sync".
        checker = s.document_sync.syncChecker
        checker.online = lambda: s.link.isOnline
        checker.paired = lambda: self.logged_in and not self.source.cached_only
        checker.check_internet = self.internet.start
        checker.request_sync = self._sync_for_check
        self.internet.finished.connect(checker.internet_checked)

    def _save_reading_position(self, doc_id: str, page: int, opened_ms: int | None) -> None:
        """Library.save_reading_position: lastOpenedPage/lastOpened to the cloud, as the app
        syncs them. Offline or logged out they stay in this session only."""
        if self.source.writable and self.logged_in:
            self.mutator.run("Saving the reading position",
                             lambda sync, _: sync.set_reading_position(doc_id, page, opened_ms), "", quiet=True)

    def _update_editing(self) -> None:
        """The app's editing interface (UserInfo.featureToggles.clientEdit) is on while
        every document shown is one the library lets this client edit."""
        shown = self.library.open_documents()
        self.singletons.user_info.set_client_edit(bool(shown) and all(self.source.can_edit(d) for d in shown))

    @property
    def logged_in(self) -> bool:
        return self.singletons.authentication._logged_in

    # -- logging in and out ----------------------------------------------------------

    def login(self, code: str) -> None:
        """Authentication.legacyLogin: pair with the one-time code in the background."""
        if self.source.cached_only:
            self.singletons.authentication.failed(LOGIN_RESULTS["NetworkError"], OFFLINE_LOGIN)
            return
        self.account.login(code)

    def _logged_in(self) -> None:
        log.info("logged in")
        self.singletons.authentication.succeeded()
        self._go_online(sync=self.sync_on_login)

    def _login_failed(self, error: Exception) -> None:
        log.warning("log-in failed: %s: %s", type(error).__name__, error)
        self.singletons.authentication.failed(*login_result(error))

    def logout(self) -> None:
        """PlatformApp.logout: delete this device on the server and forget the tokens and
        the cached library ("Logging out will remove all documents from this app")."""
        self._go_offline()
        self.account.logout(remote=not self.source.cached_only, after=self._forget_library)

    def _forget_library(self) -> None:          # on the Account worker thread
        self.syncer.wait()
        self.mutator.wait()
        self.exporter.wait()
        self.source.forget()
        shutil.rmtree(self.cache_dir / "thumbnails", ignore_errors=True)

    def _logged_out(self) -> None:
        log.info("logged out")
        self.library.set_tree(DocumentTree(), reset=True)
        self.singletons.authentication.logged_out()

    def _unauthorized(self, message: str) -> None:
        """The cloud rejected the device token (revoked on my.remarkable.com): log in again.
        The cached library stays until then."""
        self._sync_failed(message, SyncStatus.AuthenticationFailure)
        self._go_offline()
        self.singletons.authentication.failed(LOGIN_RESULTS["GenericError"], LOGGED_OUT_BY_CLOUD)

    def _go_online(self, sync: bool) -> None:
        if not self._online:
            self._online = True
            self.singletons.screen_share.start()
        if sync:
            self.syncer.refresh()

    def _go_offline(self) -> None:
        if self._online:
            self._online = False
            self.singletons.screen_share.shutdown()

    def _synced(self, tree: DocumentTree) -> None:
        self.set_tree(tree)
        self.singletons.document_sync.set_status(SyncStatus.Completed)
        self._run_waiting_check()

    def _sync_failed(self, message: str, status: SyncStatus | None = None) -> None:
        self.singletons.document_sync.set_status(
            sync_failure_status(self.syncer.last_error) if status is None else status, message)
        log.warning("sync failed: %s", message)
        self._run_waiting_check()

    def _sync_for_check(self) -> None:
        """The full sync "Check sync" asks for; while one runs, it follows that one, as
        the app's requestFullSync queues behind the running sync."""
        if not self.syncer.refresh():
            self._check_waiting = True

    def _run_waiting_check(self) -> None:
        if self._check_waiting:
            self._check_waiting = False
            if self.logged_in and not self.closing:
                self.syncer.refresh()

    def _message(self, kind, context, message: str) -> None:
        if self.closing:
            return
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            self.warnings.append(message)
            log.warning("qml: %s", message)

    def load(self) -> QQmlApplicationEngine:
        assets.register_resources(overrides=OVERRIDES)
        assets.register_fonts()
        for font in QRC_FONTS:
            QFontDatabase.addApplicationFont(font)
        register_types()
        set_page_source(lambda: self.source)
        set_edit_notifier(self.library.errorMessage.emit)
        controller = self.singletons.document_controller
        controller.source = lambda: self.source
        controller.refresh = lambda: self.set_tree(self.source.sync.tree)
        controller.notify = self.library.errorMessage.emit
        set_clipboard(self.singletons.clipboard)
        self.translator = QTranslator()
        self._translate(self.singletons.language.languageCode)
        self.singletons.language.on_change = self._language_changed
        qInstallMessageHandler(self._message)
        engine = self.engine = QQmlApplicationEngine()
        for path in IMPORT_PATHS:
            engine.addImportPath(path)
        self.icons = ArkIconProvider()
        self.icons.install(engine, "arkicon")
        self.thumbnails = ThumbnailImageProvider(self.source, self.library, self.cache_dir)
        self.thumbnails.install(engine, "rmthumb")
        engine.addImageProvider("dragicon", DragIconImageProvider())
        use_singletons(engine, self.singletons)
        # main() and PlatformApp's context properties.
        context = engine.rootContext()
        for name, value in (("dpiScale", 0.48), ("debugBuild", False), ("itemInspector", self.item_inspector),
                            ("appVersion", APP_VERSION), ("appBuild", APP_BUILD), ("qtVersion", qVersion())):
            context.setContextProperty(name, value)
        engine.load(self.main)
        if not engine.rootObjects():
            raise RuntimeError(f"{self.main.toString()} failed to load:\n" + "\n".join(self.warnings))
        return engine

    def _translate(self, code: str) -> None:
        QCoreApplication.removeTranslator(self.translator)
        if self.translator.load(TRANSLATION.format(code)):
            QCoreApplication.installTranslator(self.translator)

    def _language_changed(self, code: str) -> None:
        self._translate(code)
        if self.engine is not None:
            self.engine.retranslate()

    @property
    def window(self) -> QObject | None:
        return self.engine.rootObjects()[0] if self.engine and self.engine.rootObjects() else None

    def set_tree(self, tree: DocumentTree) -> None:
        """Show `tree`, its documents with their pending page edits (LibrarySource.view)."""
        self.library.set_tree(self.source.view_tree(tree))
        self._update_editing()

    def start(self, sync: bool = True) -> None:
        """Show the cached library; logged in, also start Screen Share and (`sync`) a sync.
        Logged out, both wait for the log-in, which always syncs (unless sync_on_login is
        cleared)."""
        self.set_tree(self.source.load_cached())
        if self.logged_in:
            # Offline (cached_only) a "sync" would only reload the cache just shown.
            self._go_online(sync and not self.source.cached_only)

    def shutdown(self) -> None:
        self.closing = True
        self.library.save_reading_positions(include_open=True)
        self.singletons.screen_share.shutdown()
        self.account.wait(60000)
        self.mutator.wait(120000)
        self.exporter.wait(120000)          # an export the user started still gets written
        self.syncer.wait(5000)
        self.internet.wait(5000)
        for provider in (self.thumbnails, self.icons):
            if provider is not None:
                provider.close()
        if self.translator is not None:
            QCoreApplication.removeTranslator(self.translator)
