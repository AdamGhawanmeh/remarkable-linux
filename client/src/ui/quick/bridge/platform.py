"""com.remarkable platform types, the Settings objects and the arkicon image provider.

Members and enum values are the app's (bridge/qml_api.json). Features this client
does not have (app updates, What's new, scanning, page editing) report
"not available" rather than pretending.
"""

from __future__ import annotations

from datetime import datetime, timezone

import json
import logging
import os
import platform as _platform
import re
import ssl
import sys
from enum import IntEnum
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote

from PySide6.QtCore import (Property, QByteArray, QCoreApplication, QDate, QDateTime, QFile, QIODevice, QLocale, QObject, QRectF, QSettings,
                            QSize, QSizeF, QTimer, QUrl, Qt, Signal, Slot, QEnum)
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QImage, QPainter
from PySide6.QtNetwork import QNetworkInformation

from .images import AsyncImageProvider
from PySide6.QtSvg import QSvgRenderer

from core import config as _config
from core.auth import PAIRING_URL, AuthNetworkError, PairingError, user_agent

log = logging.getLogger("rm.ui.quick.platform")


# Settings::paperSize accepts these (0x14102b900) and falls back to the first; a page's
# scene is 1404 x 1872 until set otherwise (the page loader's default, 0x140a02fb0).
PAGE_PAPER_SIZE = (1404.0, 1872.0)
PAPER_SIZES = (PAGE_PAPER_SIZE, (1620.0, 2160.0), (954.0, 1696.0))


class SettingsStore:
    """Where Settings keeps its values: an INI file (or memory, for tests)."""

    def __init__(self, path: Path | None = None) -> None:
        self._settings = QSettings(str(path), QSettings.Format.IniFormat) if path else None
        self._memory: dict[str, object] = {}

    def get(self, key: str, default=None):
        if self._settings is None:
            return self._memory.get(key, default)
        return self._settings.value(key, default)

    def set(self, key: str, value) -> None:
        if self._settings is None:
            self._memory[key] = value
        else:
            self._settings.setValue(key, value)

    def remove(self, key: str) -> None:
        if self._settings is None:
            self._memory.pop(key, None)
        else:
            self._settings.remove(key)

    def sync(self) -> None:
        if self._settings is not None:
            self._settings.sync()

    def clear(self) -> None:
        if self._settings is None:
            self._memory.clear()
        else:
            self._settings.clear()


def _cast(value, kind):
    if kind is bool:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true")
    try:
        return kind(value)
    except (TypeError, ValueError):
        return kind()


class Settings(QObject):
    """The app's Settings (a QML global; SettingsBase + Settings members). Desktop-
    relevant values persist; device-only ones keep the app's defaults."""

    Key = IntEnum("Key", {name: i for i, name in enumerate((
        "AccessibleMode", "AirplaneMode", "AscendingSort", "AutoUpdate", "BetaEnrolled", "EnablePowerLogging",
        "GridViewZoomFactor", "HandwritingSearchLanguage", "HomeSortOrder", "IdleSuspendDelay",
        "IdleToSuspendDelay", "Keyboard", "LastWritingTool", "ListMode", "OnboardingBanner", "OnboardingProgress",
        "OnboardingUpdateScreen", "OutdatedSoftwareDisabledVersion", "Password", "PreviousVersion",
        "REMARKABLE_RELEASE_APPID", "RecentTemplates", "RemoveLegacyLines", "RightHanded", "Rotation",
        "Screenshot", "SeabirdSetupCompleted", "ServerAddress", "ServiceEnvironment", "ShapeSnapEnabled",
        "ShowInactiveWarning", "ShowSleepScreenCarousel", "SleepScreenPath", "SuspendPowerOffDelay",
        "ThirdPartySortOrder", "UpdateChannel", "Username", "WebInterfaceEnabled", "InputFlavor", "InputLocale",
        "LastOpen", "LightSleepEnabled", "PaperSize", "ToolbarPortraitPosition", "ToolbarLandscapePosition"))})
    Environment = IntEnum("Environment", {"Prod": 0, "Staging": 1, "Dev": 2, "QA": 3, "Localhost": 4})
    InputFlavorType = IntEnum("InputFlavorType", {"Windows": 0, "Apple": 1})
    QEnum(Key)
    QEnum(Environment)
    QEnum(InputFlavorType)

    # SettingsBase
    settingChanged = Signal()
    # Settings
    developerPasswordChanged = Signal(str)
    idleSuspendDelayChanged = Signal(int)
    idleToSuspendDelayChanged = Signal(int)
    keyboardLanguageChanged = Signal(str)
    previousVersionChanged = Signal()
    onboardingUpdateScreenChanged = Signal()
    outdatedSoftwareDisabledVersionChanged = Signal(str)
    powerOffDelayChanged = Signal(int)
    powerOffDelaySetChanged = Signal(bool)
    rightHandModeChanged = Signal(bool)
    accessibilityModeChanged = Signal(bool)
    shareEmailAddressesChanged = Signal()
    serviceEnvironmentChanged = Signal(str)
    updateChannelChanged = Signal(str)
    webInterfaceEnabledChanged = Signal(bool)
    listModeChanged = Signal(bool)
    homeSortOrderChanged = Signal(int)
    setupStateChanged = Signal()
    thirdPartySortOrderChanged = Signal(int)
    showInactiveWarningChanged = Signal()
    gridViewZoomFactorChanged = Signal()
    handwritingConversionLanguageChanged = Signal(str)
    handwritingSearchLanguageChanged = Signal(str)
    inputFlavorChanged = Signal(int)
    inputLocaleChanged = Signal(str)
    seabirdSetupChanged = Signal(bool)
    lastOpenChanged = Signal()
    screenshotChanged = Signal(bool)
    idleDetailsVisibleChanged = Signal(bool)
    idleNameChanged = Signal(str)
    idleContactChanged = Signal(str)
    ascendingOrderChanged = Signal()
    lightSleepEnabledChanged = Signal(bool)
    shapeSnapEnabledChanged = Signal(bool)
    paperSizeChanged = Signal(QSizeF)
    airplaneModeChanged = Signal(bool)
    toolbarPortraitPositionChanged = Signal()
    toolbarLandscapePositionChanged = Signal()
    showSleepScreenCarouselChanged = Signal(bool)

    def __init__(self, store: SettingsStore | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store or SettingsStore()

    def _stored(key: str, kind, default, notify: str, with_value: bool = True):
        """A persisted property; `notify` names its signal (declared above), which
        carries the new value when the app's does."""
        def get(self):
            return _cast(self._store.get(key, default), kind)

        def set_(self, value) -> None:
            value = _cast(value, kind)
            if get(self) != value:
                self._store.set(key, value)
                getattr(self, notify).emit(*([value] if with_value else []))
                self.settingChanged.emit()

        return Property(kind, get, set_, notify=sys._getframe(1).f_locals[notify])

    isDevice = Property(bool, lambda self: False, constant=True)
    def _set_environment(self, value: int) -> None:
        if self.serviceEnvironment != int(value):
            self._store.set("serviceEnvironment", int(value))
            self.serviceEnvironmentChanged.emit(Settings.Environment(int(value)).name)
            self.settingChanged.emit()

    serviceEnvironment = Property(int, lambda self: _cast(self._store.get("serviceEnvironment", 0), int),
                                  _set_environment, notify=serviceEnvironmentChanged)
    setupState = _stored("setupState", str, "", "setupStateChanged", False)
    serviceEnvironmentString = Property(str, lambda self: Settings.Environment(self.serviceEnvironment).name,
                                        notify=serviceEnvironmentChanged)
    updateChannelString = Property(str, lambda self: "", notify=updateChannelChanged)
    keyboardLanguage = _stored("keyboardLanguage", str, "", "keyboardLanguageChanged")
    rightHandMode = _stored("rightHandMode", bool, True, "rightHandModeChanged")
    accessibilityMode = _stored("accessibilityMode", bool, False, "accessibilityModeChanged")
    ascendingOrder = _stored("ascendingOrder", bool, False, "ascendingOrderChanged", False)
    developerPassword = _stored("developerPassword", str, "", "developerPasswordChanged")
    shareEmailAddresses = Property("QStringList", lambda self: list(self._store.get("shareEmailAddresses", []) or []),
                                   notify=shareEmailAddressesChanged)
    idleSuspendDelay = _stored("idleSuspendDelay", int, 0, "idleSuspendDelayChanged")
    idleToSuspendDelay = _stored("idleToSuspendDelay", int, 0, "idleToSuspendDelayChanged")
    powerOffDelay = _stored("powerOffDelay", int, 0, "powerOffDelayChanged")
    powerOffDelaySet = Property(bool, lambda self: False, notify=powerOffDelaySetChanged)
    previousVersion = _stored("previousVersion", str, "", "previousVersionChanged", False)
    outdatedSoftwareDisabledVersion = _stored("outdatedSoftwareDisabledVersion", str, "",
                                              "outdatedSoftwareDisabledVersionChanged")
    onboardingUpdateScreen = _stored("onboardingUpdateScreen", bool, False, "onboardingUpdateScreenChanged", False)
    webInterfaceEnabled = _stored("webInterfaceEnabled", bool, False, "webInterfaceEnabledChanged")
    listMode = _stored("listMode", bool, False, "listModeChanged")
    homeSortOrder = _stored("homeSortOrder", int, 0, "homeSortOrderChanged")
    thirdPartySortOrder = _stored("thirdPartySortOrder", int, 0, "thirdPartySortOrderChanged")
    showInactiveWarning = _stored("showInactiveWarning", bool, True, "showInactiveWarningChanged", False)
    gridViewZoomFactor = _stored("gridViewZoomFactor", int, 0, "gridViewZoomFactorChanged", False)
    handwritingConversionLanguage = _stored("handwritingConversionLanguage", str, "en_US",
                                            "handwritingConversionLanguageChanged")
    inputFlavor = _stored("inputFlavor", int, 0, "inputFlavorChanged")
    inputLocale = _stored("inputLocale", str, "", "inputLocaleChanged")
    seabirdSetup = _stored("seabirdSetup", bool, False, "seabirdSetupChanged")
    lastOpen = _stored("lastOpen", str, "", "lastOpenChanged", False)
    screenshot = _stored("screenshot", bool, False, "screenshotChanged")
    lightSleepEnabled = _stored("lightSleepEnabled", bool, False, "lightSleepEnabledChanged")
    shapeSnapEnabled = _stored("shapeSnapEnabled", bool, False, "shapeSnapEnabledChanged")
    airplaneMode = _stored("airplaneMode", bool, False, "airplaneModeChanged")
    showSleepScreenCarousel = _stored("showSleepScreenCarousel", bool, False, "showSleepScreenCarouselChanged")

    def _paper_size(self) -> QSizeF:
        """The stored paper size when it is one the app knows (1404 x 1872, 1620 x 2160,
        954 x 1696), else 1404 x 1872, as Settings::paperSize returns it (0x14038bce0)."""
        value = self._store.get("paperSize")
        if isinstance(value, QSizeF) and (value.width(), value.height()) in PAPER_SIZES:
            return QSizeF(value)
        return QSizeF(*PAGE_PAPER_SIZE)

    def _set_paper_size(self, size: QSizeF) -> None:
        self._store.set("paperSize", QSizeF(size))
        self.paperSizeChanged.emit(QSizeF(size))

    paperSize = Property(QSizeF, _paper_size, _set_paper_size, notify=paperSizeChanged)

    # Settings::Key → the property it backs (the rest are raw values).
    _KEY_PROPERTY = {"GridViewZoomFactor": "gridViewZoomFactor", "HomeSortOrder": "homeSortOrder",
                     "ListMode": "listMode", "ShowInactiveWarning": "showInactiveWarning",
                     "ThirdPartySortOrder": "thirdPartySortOrder", "ServiceEnvironment": "serviceEnvironment",
                     "InputLocale": "inputLocale", "LastOpen": "lastOpen", "PaperSize": "paperSize"}

    def _key(self, key: int) -> str:
        return Settings.Key(int(key)).name

    @Slot(int, "QVariant")
    def setValue(self, key: int, value) -> None:
        name = self._key(key)
        if name in self._KEY_PROPERTY:
            self.setProperty(self._KEY_PROPERTY[name], value)
        else:
            self._store.set(f"key/{name}", value)
            self.settingChanged.emit()

    @Slot(int, result="QVariant")
    @Slot(int, "QVariant", result="QVariant")
    def getValue(self, key: int, default=None):
        name = self._key(key)
        if name in self._KEY_PROPERTY:
            return self.property(self._KEY_PROPERTY[name])
        return self._store.get(f"key/{name}", default)

    @Slot(int, result="QVariant")
    def defaultValue(self, key: int):
        return None

    @Slot(int, result=str)
    @Slot(int, str, result=str)
    def getString(self, key: int, default: str = "") -> str:
        value = self.getValue(key)
        return default if value is None else str(value)

    @Slot(str, str, "QVariant")
    def setRawValue(self, group: str, key: str, value) -> None:
        self._store.set(f"{group}/{key}", value)
        self.settingChanged.emit()

    @Slot(str, str, result="QVariant")
    @Slot(str, str, "QVariant", result="QVariant")
    def rawValue(self, group: str, key: str, default=None):
        return self._store.get(f"{group}/{key}", default)

    @Slot()
    def sync(self) -> None:
        self._store.sync()

    @Slot()
    def clear(self) -> None:
        self._store.clear()
        self.settingChanged.emit()

    @Slot()
    def retailDemoReset(self) -> None:
        pass

    @Slot("QVariantMap")
    def setLastWritingTool(self, tool: dict) -> None:
        self._store.set("key/LastWritingTool", tool)

    @Slot(result="QVariantMap")
    def lastWritingTool(self) -> dict:
        return dict(self._store.get("key/LastWritingTool", {}) or {})

    @Slot(str)
    def addShareEmailAddress(self, address: str) -> None:
        addresses = [a for a in self.shareEmailAddresses if a != address] + [address]
        self._store.set("shareEmailAddresses", addresses)
        self.shareEmailAddressesChanged.emit()

    @Slot(str)
    def removeShareEmailAddress(self, address: str) -> None:
        self._store.set("shareEmailAddresses", [a for a in self.shareEmailAddresses if a != address])
        self.shareEmailAddressesChanged.emit()

    @Slot(result=str)
    def readFileLocale(self) -> str:
        return QLocale.system().name()

    @Slot(str, result=str)
    def fileLocaleToInputLocale(self, locale: str) -> str:
        return locale


class ArkTokensSettings(QObject):
    """ark::tokens::Settings, the `Settings` singleton the ark token files read."""

    landscapeChanged = Signal()
    accessibilityChanged = Signal()
    usingDebugColorsChanged = Signal()
    textRulesPolicyChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._landscape = False
        self._accessibility = False
        self._debug = False
        self._text_rules = 0

    def _setter(attr: str, signal: str):
        def set_(self, value) -> None:
            if getattr(self, attr) != value:
                setattr(self, attr, value)
                getattr(self, signal).emit()
        return set_

    landscape = Property(bool, lambda self: self._landscape, _setter("_landscape", "landscapeChanged"),
                         notify=landscapeChanged)
    accessibility = Property(bool, lambda self: self._accessibility, notify=accessibilityChanged)
    usingDebugColors = Property(bool, lambda self: self._debug, _setter("_debug", "usingDebugColorsChanged"),
                                notify=usingDebugColorsChanged)
    textRulesPolicy = Property(int, lambda self: self._text_rules, _setter("_text_rules", "textRulesPolicyChanged"),
                               notify=textRulesPolicyChanged)

    @Slot(QColor, result=QColor)
    def convertColor(self, color: QColor) -> QColor:
        return color


class LanguageSettings(QObject):
    """xofm::libs::localization::LanguageSettings: the UI languages are the app's
    translations (qrc :/i18n/translations/reMarkable_<code>.qm); choosing one switches
    the installed translator (`on_change(code)`, set by QuickApp)."""

    TRANSLATIONS = ":/i18n/translations"
    languageCodeChanged = Signal()

    def __init__(self, store: SettingsStore | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store or SettingsStore()
        self.on_change = lambda code: None

    def _codes(self) -> list[str]:
        from PySide6.QtCore import QDir
        names = QDir(self.TRANSLATIONS).entryList(["reMarkable_*.qm"], QDir.Filter.Files, QDir.SortFlag.Name)
        return [n[len("reMarkable_"):-len(".qm")] for n in names]

    def _code(self) -> str:
        code = str(self._store.get("language/code", "en"))
        return code if code in self._codes() else "en"

    def _set_code(self, code: str) -> None:
        if code != self._code() and code in self._codes():
            self._store.set("language/code", code)
            self.on_change(code)
            self.languageCodeChanged.emit()

    availableLanguageCodes = Property("QStringList", _codes, constant=True)
    languageCode = Property(str, _code, _set_code, notify=languageCodeChanged)

    @Slot(str, result=str)
    def nativeLanguageName(self, code: str) -> str:
        """QLocale's native name with its first letter capitalised ("Other (code)" if
        Qt has none)."""
        name = QLocale(code).nativeLanguageName()
        if not name:
            return QCoreApplication.translate("xofm::libs::localization::LanguageSettings", "Other (%1)").replace("%1", code)
        return name[:1].upper() + name[1:]

    @Slot(str, result=str)
    def languageName(self, code: str) -> str:
        return QLocale.languageToString(QLocale(code).language())

    @Slot(str, result=str)
    def getLanguageDisplayName(self, code: str) -> str:
        return self.nativeLanguageName(code)


def network_information() -> QNetworkInformation | None:
    """QNetworkInformation with a reachability backend, loaded as LinkProviderQt6 loads it
    (linkprovider-qt6.cpp): None, logged, when no backend can be."""
    if not QNetworkInformation.loadBackendByFeatures(QNetworkInformation.Feature.Reachability):
        log.warning("QNetworkInformation: failed to load any backend")
        log.warning("Backends available: %s", ", ".join(QNetworkInformation.availableBackends()))
        return None
    information = QNetworkInformation.instance()
    log.info("QNetworkInformation, backend: %s", information.backendName())
    return information


class LinkProvider(QObject):
    """`LinkProvider` in QML (LinkProviderWrapper over LinkProviderQt6): the network link
    as QNetworkInformation sees it. Reachability Local, Site or Online is Online; Unknown,
    Disconnected or no backend at all is Offline (0x14074d4c0). main.qml shows "No cloud
    connection" when it is not Online, and the sync check says "You're not connected to
    Wi-Fi". `information` stands in for QNetworkInformation (tests)."""

    Availability = IntEnum("Availability", {"Offline": 0, "Connecting": 1, "Online": 2})
    QEnum(Availability)
    ONLINE = (QNetworkInformation.Reachability.Local, QNetworkInformation.Reachability.Site,
              QNetworkInformation.Reachability.Online)

    availabilityChanged = Signal(int)
    meteredConnectionStatus = Signal(bool)

    def __init__(self, parent: QObject | None = None, information=None) -> None:
        super().__init__(parent)
        self._information = information if information is not None else network_information()
        if self._information is not None:
            self._information.reachabilityChanged.connect(lambda _: self.availabilityChanged.emit(self._availability()))

    def _availability(self) -> int:
        if self._information is None:
            log.debug("network backend not loaded")
            return 0
        return 2 if self._information.reachability() in self.ONLINE else 0

    isOnline = Property(bool, lambda self: self._availability() == 2, notify=availabilityChanged)
    availability = Property(int, lambda self: self._availability(), notify=availabilityChanged)

    @property
    def known_offline(self) -> bool:
        """The backend says the network is down (with no backend, nothing is known)."""
        return self._information is not None and not self.isOnline
    connectionIsMetered = Property(bool, lambda self: False, constant=True)


class ScreenSaverInhibitor:
    """org.freedesktop.ScreenSaver on the session bus (GNOME, KDE and others provide it):
    Inhibit(application, reason) returns a cookie that UnInhibit takes back. One inhibition
    at a time, as SetThreadExecutionState has one state per thread."""

    SERVICE, PATH, INTERFACE = "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver"

    def __init__(self, bus=None) -> None:
        self._bus = bus
        self._cookie: int | None = None

    def _interface(self):
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
        bus = self._bus if self._bus is not None else QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None
        interface = QDBusInterface(self.SERVICE, self.PATH, self.INTERFACE, bus)
        return interface if interface.isValid() else None

    def set(self, inhibit: bool, why: str) -> bool:
        interface = self._interface()
        if interface is None:
            return False
        if inhibit:
            if self._cookie is not None:
                return True
            reply = interface.call("Inhibit", "reMarkable", why or "reMarkable")
            values = reply.arguments() if reply is not None else []
            if not values or reply.errorName():
                return False
            self._cookie = int(values[0])
            return True
        if self._cookie is None:
            return True
        reply = interface.call("UnInhibit", self._cookie)
        self._cookie = None
        return not (reply is not None and reply.errorName())


class PlatformHelpers(QObject):
    """PlatformHelpers: desktop integration. documentAccepted is how FileImportDialog
    hands over picked files; the app imports them (QuickApp connects it)."""

    ExportType = IntEnum("ExportType", {"ExportPdf": 0, "ExportPng": 1, "ExportSvg": 2, "ExportRmBundle": 3,
                                        "ExportRmHtml": 4})
    SettingsKey = IntEnum("SettingsKey", {"WindowWidth": 0, "WindowHeight": 1, "ListMode": 2, "Uninstalled": 3,
                                          "WhatsNewVersionRead": 4, "LanguageCode": 5})
    VibrationType = IntEnum("VibrationType", {"None": 0, "LongPress": 1})
    QEnum(ExportType)
    QEnum(SettingsKey)
    QEnum(VibrationType)

    documentAccepted = Signal(QUrl, str, str, bool, bool)
    errorMessage = Signal(str)
    storagePermissionResult = Signal(bool)
    finishedSFSafariViewController = Signal()
    keyboardAboutToShow = Signal(QRectF)
    finishedDocumentCameraViewController = Signal(str)
    keyboardAboutToHide = Signal()

    def __init__(self, store: SettingsStore | None = None, export=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store or SettingsStore()
        self._export = export
        # openFileChooser: shows a file dialog and calls back with the chosen file URLs
        # (tests replace it); the folder the files go to (the app may set it).
        self.choose_files: Callable[[Callable[[list], None]], None] = self._file_dialog
        self.import_folder: Callable[[], str] = lambda: ""
        # setInhibitSleep: (inhibit, why) -> whether it worked (tests replace it).
        self.inhibit_sleep: Callable[[bool, str], bool] = ScreenSaverInhibitor().set
        self._dialog = None

    doubleClickInterval = Property(int, lambda self: QGuiApplication.styleHints().mouseDoubleClickInterval(),
                                   constant=True)
    osVersionMajor = Property(int, lambda self: _os_version(0), constant=True)
    osVersionMinor = Property(int, lambda self: _os_version(1), constant=True)
    osVersionMicro = Property(int, lambda self: _os_version(2), constant=True)

    @Slot(str)
    def openUrl(self, url: str) -> None:
        log.info("opening %s in the default browser", url)     # run.py -v: the URL, if it does not open
        QDesktopServices.openUrl(QUrl(url))

    @Slot(str, result=bool)
    def isCurrentKeyboard(self, keyboard: str) -> bool:
        return False

    @Slot(str, result=bool)
    def envVariableIsSet(self, name: str) -> bool:
        return bool(os.environ.get(name))

    @Slot(result=str)
    def buildDate(self) -> str:
        return QDate.currentDate().toString(Qt.DateFormat.ISODate)

    @Slot(result=str)
    def aboutText(self) -> str:
        return f"reMarkable for Linux · {_platform.platform()}"

    @Slot()
    def openFileChooser(self) -> None:
        """PlatformHelpers::openFileChooser. reMarkable.exe 3.29 (Windows) does nothing:
        its metacall (0x140094b10) has no case for slot 12, and the desktop QML imports
        through FileImportDialog, which emits documentAccepted for each file. Here, the
        Linux way: a file dialog for PDFs and EPUBs (Qt uses the XDG desktop portal's
        chooser where there is one), whose files go out through documentAccepted just as
        FileImportDialog sends them (no visual name, the import folder, not scanned, not
        deleted after), so the app imports them."""
        def chosen(urls: list) -> None:
            for url in urls:
                url = QUrl(url)
                if url.isLocalFile() and url.toLocalFile():
                    self.documentAccepted.emit(url, "", self.import_folder(), False, False)
        self.choose_files(chosen)

    def _file_dialog(self, done: Callable[[list], None]) -> None:
        from PySide6.QtWidgets import QApplication, QFileDialog
        if not isinstance(QApplication.instance(), QApplication):
            log.warning("openFileChooser: no widgets application to show a file dialog")
            return
        dialog = QFileDialog(None, QCoreApplication.translate("FileImportDialog", "Import"))
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setNameFilters(["Documents (*.pdf *.epub)", "PDF (*.pdf)", "EPUB (*.epub)"])
        dialog.urlsSelected.connect(lambda urls: done(list(urls)))
        dialog.finished.connect(lambda _: dialog.deleteLater())
        self._dialog = dialog           # kept until it closes
        dialog.open()

    @Slot(int, "QVariant")
    def setValue(self, key: int, value) -> None:
        self._store.set(f"platform/{PlatformHelpers.SettingsKey(int(key)).name}", value)

    @Slot(int, result="QVariant")
    @Slot(int, "QVariant", result="QVariant")
    def getValue(self, key: int, default=None):
        return self._store.get(f"platform/{PlatformHelpers.SettingsKey(int(key)).name}", default)

    @Slot(QUrl, str, str, bool, bool)
    def handleDocumentAccepted(self, url: QUrl, visual_name: str, parent_id: str, scanned: bool,
                               delete_later: bool) -> None:
        self.documentAccepted.emit(url, visual_name, parent_id, scanned, delete_later)

    @Slot(QUrl)
    def openFile(self, url: QUrl) -> None:
        QDesktopServices.openUrl(url)

    @Slot(bool, str)
    def setInhibitSleep(self, inhibit: bool, why: str) -> None:
        """PlatformHelpers::setInhibitSleep (platformhelpers.cpp 0x1400f79b0): Windows'
        SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        to inhibit, ES_CONTINUOUS alone to restore, then "{} to {} display sleep"
        (Succeeded/Failed, inhibit/restore). Here the freedesktop way: the session's
        org.freedesktop.ScreenSaver Inhibit(app, why) / UnInhibit(cookie)."""
        ok = bool(self.inhibit_sleep(bool(inhibit), str(why or "")))
        log.info("%s to %s display sleep", "Succeeded" if ok else "Failed", "inhibit" if inhibit else "restore")

    @Slot()
    def scanDocument(self) -> None:
        self.errorMessage.emit("Scanning documents is not available on Linux")

    @Slot()
    def requestStoragePermission(self) -> None:
        self.storagePermissionResult.emit(True)

    @Slot()
    def onCompleted(self) -> None:
        pass

    @Slot(str, str, int, result=bool)
    @Slot(str, str, int, str, result=bool)
    @Slot(str, str, int, str, bool, result=bool)
    @Slot(str, str, int, str, bool, "QVariantList", result=bool)
    def exportFile(self, file_path: str, document_id: str, export_type: int, password: str = "",
                   keep_password: bool = False, pages: list | None = None) -> bool:
        """ExportDialog's performExport: `file_path` is the file (one PDF) or folder the
        dialog chose, as a URL; `pages` the page indexes, when exporting a selection.
        Starts the export in the background (`export(path, id, kind, pages)`)."""
        kind = {int(PlatformHelpers.ExportType["ExportPdf"]): "pdf",
                int(PlatformHelpers.ExportType["ExportPng"]): "png",
                int(PlatformHelpers.ExportType["ExportSvg"]): "svg",
                int(PlatformHelpers.ExportType["ExportRmBundle"]): "rmdoc"}.get(int(export_type))
        if self._export is None or kind is None:
            self.errorMessage.emit("This export format is not available on Linux")
            return False
        path = QUrl(file_path).toLocalFile() if str(file_path).startswith("file:") else str(file_path)
        pages = [int(p) for p in pages] if pages else None
        if password:            # a password-protected PDF: the password the dialog asked for
            return bool(self._export(path, document_id, kind, pages, str(password), bool(keep_password)))
        return bool(self._export(path, document_id, kind, pages))

    @Slot(result=int)
    def topLevelWindowCount(self) -> int:
        return len(QGuiApplication.topLevelWindows())

    @Slot(result=float)
    def displayRefreshRate(self) -> float:
        screen = QGuiApplication.primaryScreen()
        return screen.refreshRate() if screen else 60.0

    @Slot(int)
    def vibrationFeedback(self, kind: int) -> None:
        pass


def _os_version(part: int) -> int:
    numbers = [int(x) for x in _platform.release().split("-")[0].split(".") if x.isdigit()]
    return numbers[part] if part < len(numbers) else 0


def _utf16(text: str) -> list[int]:
    data = text.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


class PlatformApp(QObject):
    """PlatformApp (XoApp): updates are the distribution's job, so no Sparkle. logout()
    (LogoutConfirmDialog) runs `on_logout`; the dialog waits for Authentication.loggedInChanged."""

    def __init__(self, on_logout: Callable[[], None] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.on_logout = on_logout
        # PlatformHelpers' stored WhatsNewVersionRead (the app sets it; core.config's default).
        self.whats_new_read: Callable[[], object] = lambda: _config.WHATS_NEW_READ_DEFAULT

    @Slot()
    def logout(self) -> None:
        if self.on_logout is not None:
            self.on_logout()

    @Slot(str, result=bool)
    def shouldShowWhatsNew(self, version: str) -> bool:
        """PlatformApp::shouldShowWhatsNew (0x14003d3d0): the version last marked read
        (PlatformHelpers WhatsNewVersionRead, "0" when never set; logged as "Read
        version:"); true when it is not two parts split at "." (KeepEmptyParts), else
        when it sorts before `version` UTF-16 unit by unit (a prefix sorts first).
        WhatsNewDialog shows the release notes then, and marks the version read."""
        value = self.whats_new_read()
        read = "" if value is None else str(value)
        log.info("Read version: %s", read)
        if len(read.split(".")) != 2:
            return True
        return _utf16(read) < _utf16(str(version or ""))

    @Slot(result="QVariantMap")
    def whatsNewJson(self) -> dict:
        """The release notes the app ships (qrc :/misc/whatsnew_desktop.json)."""
        file = QFile(":/misc/whatsnew_desktop.json")
        if not file.open(QIODevice.OpenModeFlag.ReadOnly):
            log.warning("Cannot read WhatsNew JSON file")
            return {}
        try:
            data = json.loads(bytes(file.readAll()).decode("utf-8"))
        except ValueError:
            log.warning("Cannot parse WhatsNew JSON data")
            return {}
        return data if isinstance(data, dict) else {}

    @Slot()
    def checkForUpdate(self) -> None:
        pass

    @Slot(result=bool)
    def canUpdateThroughSparkle(self) -> bool:
        return False

    @Slot(result=bool)
    def sparkleEnabled(self) -> bool:
        return False

    @Slot()
    def onSyncProtocolChanged(self) -> None:
        pass

    @Slot()
    def updateWorkerSettings(self) -> None:
        pass

    @Slot(bool)
    def onLibraryReady(self, ready: bool) -> None:
        pass


LOGIN_RESULTS = {"Ok": 0, "DeviceAlreadyConnected": 1, "InvalidOneTimeCode": 2, "SslCertificateTimeError": 3,
                 "SslHandshakeError": 4, "GenericError": 5, "NetworkError": 6}
NETWORK_MESSAGE = "Unable to connect to the reMarkable cloud. Check your internet connection and try again."
# OpenSSL verify codes for a certificate outside its validity period (a wrong clock).
X509_NOT_YET_VALID, X509_EXPIRED = 9, 10


def _ssl_error(error: BaseException) -> ssl.SSLError | None:
    """The TLS error behind a network failure (urllib wraps it in URLError.reason)."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        for candidate in (error, getattr(error, "reason", None)):
            if isinstance(candidate, ssl.SSLError):
                return candidate
        error = error.__cause__ or error.__context__
    return None


def login_result(error: Exception) -> tuple[int, str]:
    """Authentication.LoginResult and loginErrorMessage for a failed log-in. LoginWizard
    shows its own help page for a rejected code and for TLS errors, and the message (in a
    notification) otherwise; it recognises the cloud's "You must connect a rM device"."""
    if isinstance(error, PairingError) and error.status is not None and 400 <= error.status < 500:
        return LOGIN_RESULTS["InvalidOneTimeCode"], str(error)
    tls = _ssl_error(error)
    if isinstance(tls, ssl.SSLCertVerificationError) and tls.verify_code in (X509_NOT_YET_VALID, X509_EXPIRED):
        return LOGIN_RESULTS["SslCertificateTimeError"], str(tls)
    if tls is not None:
        return LOGIN_RESULTS["SslHandshakeError"], str(tls)
    if isinstance(error, AuthNetworkError):
        return LOGIN_RESULTS["NetworkError"], NETWORK_MESSAGE
    return LOGIN_RESULTS["GenericError"], f"Unable to log in: {error}"


class Authentication(QObject):
    """Authentication: whether this client is logged in, which main.qml's LoginWizard
    follows. Logged out, the wizard covers the window and asks for a one-time code;
    legacyLogin(code) hands it to `login` (QuickApp pairs in the background), which reports
    back through succeeded() or failed(). The wizard reacts to loggedInChanged, also after
    a failed attempt (then loginResult says why), so it is emitted every time.
    """

    LoginResult = IntEnum("LoginResult", LOGIN_RESULTS)
    QEnum(LoginResult)

    newLoginSuccess = Signal()
    userTokenErrorMessageReceived = Signal(str)
    badRequestErrorMessageChanged = Signal()
    loggedInChanged = Signal()
    loginErrorMessageChanged = Signal()
    deviceMissingChanged = Signal(bool)
    authTokenChanged = Signal()
    loginResultChanged = Signal()
    loggedOutFromCloud = Signal()

    def __init__(self, logged_in: bool, login: Callable[[str], None] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logged_in = logged_in
        self._login = login
        self._result = LOGIN_RESULTS["Ok"]
        self._message = ""

    deviceMissing = Property(bool, lambda self: False, notify=deviceMissingChanged)
    loggedIn = Property(bool, lambda self: self._logged_in, notify=loggedInChanged)
    loginErrorMessage = Property(str, lambda self: self._message, notify=loginErrorMessageChanged)
    loginResult = Property(int, lambda self: self._result, notify=loginResultChanged)

    def _set(self, logged_in: bool, result: int, message: str) -> None:
        # Result and message first: LoginWizard reads them when loggedInChanged arrives.
        if result != self._result:
            self._result = result
            self.loginResultChanged.emit()
        if message != self._message:
            self._message = message
            self.loginErrorMessageChanged.emit()
        self._logged_in = logged_in
        self.loggedInChanged.emit()

    # -- reported by the app --
    def succeeded(self) -> None:
        self._set(True, LOGIN_RESULTS["Ok"], "")
        self.authTokenChanged.emit()
        self.newLoginSuccess.emit()

    def failed(self, result: int, message: str) -> None:
        """A log-in failed, or (logged in) the cloud stopped accepting this client."""
        self._set(False, result, message)

    def logged_out(self) -> None:
        self._set(False, LOGIN_RESULTS["Ok"], "")

    # -- called by the QML --
    @Slot()
    def fetchUserToken(self) -> None:
        pass            # the sync fetches user tokens itself, when it needs one

    @Slot(str)
    def legacyLogin(self, code: str) -> None:
        if self._login is None:
            self.failed(LOGIN_RESULTS["GenericError"], "Logging in is not available here.")
        else:
            self._login(code)

    @Slot()
    def abortLogin(self) -> None:
        pass            # the app's QML never calls it; a pairing request runs to its end

    @Slot(result=bool)
    def refreshAuthToken(self) -> bool:
        return self._logged_in


class UserInfo(QObject):
    """xofm::libs::user::UserInfo."""

    AccountType = IntEnum("AccountType", {"Google": 0, "Facebook": 1, "Undefined": 2})
    QEnum(AccountType)

    idChanged = Signal(str)
    nameChanged = Signal(str)
    usernameChanged = Signal(str)
    emailChanged = Signal(str)
    accountTypeChanged = Signal()
    subscriptionLevelStringSet = Signal()
    tectonicChanged = Signal(str)
    scopesChanged = Signal("QStringList")
    createdAtChanged = Signal("qlonglong")
    isSubscribedChanged = Signal()

    def __init__(self, email: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._values = {"id": "", "name": "", "username": "", "email": email, "tectonic": "",
                        "subscriptionLevel": self._tr(self.NOT_SUBSCRIBED)}
        self._subscribed = False
        self._account_type = 2
        self._created_at = 0
        self._scopes: list[str] = []
        self._client_edit = False

    # UserInfo::setSubscriptionLevel (userinfo.cpp, 0x140370820): the level's words, split
    # at this pattern (QString::split, KeepEmptyParts), each made "Upper" + "lower" and
    # followed by a space ("%1%2 "), then trimmed and translated; an empty level shows
    # tr("Not subscribed"), which the constructor (0x14036cf0e) also starts with.
    # isSubscribed (getter 0x1403710b0 = the bool at +0xe8) is true exactly when the level
    # set last was not empty, whatever it says.
    LEVEL_SEPARATORS = re.compile(r"(?: |-|_|:|\t)")
    NOT_SUBSCRIBED = "Not subscribed"

    @staticmethod
    def _tr(text: str) -> str:
        return QCoreApplication.translate("xofm::libs::user::UserInfo", text)

    @classmethod
    def subscription_text(cls, level: str) -> str:
        """The subscriptionLevel string the app shows for a token's "level"."""
        if not level:
            return cls._tr(cls.NOT_SUBSCRIBED)
        words = "".join(w[:1].upper() + w[1:].lower() + " " for w in cls.LEVEL_SEPARATORS.split(level))
        return cls._tr(words.strip())

    def set_from_token(self, token: str) -> bool:
        """Fill in the user from the user token's claims, as the app's
        legacy.cpp parseUserToken / toUserInfoData read them: "auth0-profile" {UserID,
        Name, Nickname, Email, CreatedAt}, "scopes", "level" (subscriptionLevel),
        "tectonic"; the account type is Google or Facebook as the UserID names one
        (0x1403735cd, 0x140373613), else Undefined. False (and nothing changed) when the
        token is not a JWT. Nothing of it is logged."""
        from core.auth import decode_jwt_claims
        try:
            claims = decode_jwt_claims(token or "")
        except ValueError:
            log.info("Invalid user token: not a JWT")
            return False
        profile = claims.get("auth0-profile")
        profile = profile if isinstance(profile, dict) else {}
        text = lambda v: v if isinstance(v, str) else ""          # noqa: E731
        user_id = text(profile.get("UserID")) or text(claims.get("sub"))
        self.setId(user_id)
        self.setName(text(profile.get("Name")))
        self.setUsername(text(profile.get("Nickname")))
        self.setEmail(text(profile.get("Email")))
        self.setTectonic(text(claims.get("tectonic")))
        lowered = user_id.lower()
        self.setAccountType(0 if "google" in lowered else 1 if "facebook" in lowered else 2)
        scopes = claims.get("scopes")
        self._scopes = scopes.split() if isinstance(scopes, str) else [str(x) for x in scopes or []] \
            if isinstance(scopes, list) else []
        self.scopesChanged.emit(list(self._scopes))
        created = text(profile.get("CreatedAt"))
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
            self.setCreatedAt(int(stamp.timestamp() * 1000) if stamp is not None else 0)
        except ValueError:
            self.setCreatedAt(0)
        self.setSubscriptionLevel(text(claims.get("level")))
        log.info("user info set from the user token (account type %s)", self.getAccountTypeString() or "Undefined")
        return True

    def clear(self) -> None:
        """Logged out: no user."""
        for setter in (self.setId, self.setName, self.setUsername, self.setEmail, self.setTectonic,
                       self.setSubscriptionLevel):
            setter("")
        self.setAccountType(2)
        self.setCreatedAt(0)
        self._scopes = []
        self.scopesChanged.emit([])

    @Slot(str)
    def setSubscriptionLevel(self, value: str) -> None:
        """As 0x140370820: nothing when `value` equals the string shown now (the app
        compares the formatted string with the raw level), else the formatted level,
        isSubscribed = level not empty, isSubscribedChanged if that changed, then
        subscriptionLevelStringSet."""
        value = value or ""
        if value == self._values["subscriptionLevel"]:
            return
        was = self._subscribed
        self._values["subscriptionLevel"] = self.subscription_text(value)
        self._subscribed = bool(value)
        if was != self._subscribed:
            self.isSubscribedChanged.emit()
        self.subscriptionLevelStringSet.emit()

    def _value(key: str):
        return lambda self: self._values[key]

    @Slot(str)
    def setId(self, value: str) -> None:
        self._values["id"] = value
        self.idChanged.emit(value)

    @Slot(str)
    def setName(self, value: str) -> None:
        self._values["name"] = value
        self.nameChanged.emit(value)

    @Slot(str)
    def setUsername(self, value: str) -> None:
        self._values["username"] = value
        self.usernameChanged.emit(value)

    @Slot(str)
    def setEmail(self, value: str) -> None:
        self._values["email"] = value
        self.emailChanged.emit(value)

    @Slot(int)
    def setAccountType(self, value: int) -> None:
        self._account_type = int(value)
        self.accountTypeChanged.emit()

    @Slot(str)
    def setTectonic(self, value: str) -> None:
        self._values["tectonic"] = value
        self.tectonicChanged.emit(value)

    @Slot("qlonglong")
    def setCreatedAt(self, value: int) -> None:
        self._created_at = int(value)
        self.createdAtChanged.emit(self._created_at)

    def set_client_edit(self, on: bool) -> None:
        """Turn the app's notebook editing interface on or off (main.qml:
        actions.canEditDocuments). On only while the document shown may be edited here
        (LibrarySource.editable): elsewhere the interface stays the reader's."""
        if bool(on) != self._client_edit:
            self._client_edit = bool(on)
            self.scopesChanged.emit([])

    featureToggles = Property("QVariantMap", lambda self: {"clientEdit": self._client_edit}, notify=scopesChanged)
    id = Property(str, _value("id"), notify=idChanged)
    name = Property(str, _value("name"), notify=nameChanged)
    username = Property(str, _value("username"), notify=usernameChanged)
    email = Property(str, _value("email"), setEmail, notify=emailChanged)
    accountType = Property(int, lambda self: self._account_type, setAccountType, notify=accountTypeChanged)
    subscriptionLevel = Property(str, _value("subscriptionLevel"), notify=subscriptionLevelStringSet)
    tectonic = Property(str, _value("tectonic"), notify=tectonicChanged)
    isSubscribed = Property(bool, lambda self: self._subscribed, notify=isSubscribedChanged)
    scopes = Property("QStringList", lambda self: list(self._scopes), notify=scopesChanged)
    createdAt = Property("qlonglong", lambda self: self._created_at, notify=createdAtChanged)

    @Slot(result=str)
    def getAccountTypeString(self) -> str:
        return {0: "Google", 1: "Facebook"}.get(self._account_type, "")

    @Slot(result=int)
    def daysSinceCreation(self) -> int:
        if not self._created_at:
            return 0
        return max(0, int((datetime.now(timezone.utc).timestamp() * 1000 - self._created_at) // 86_400_000))


class NetworkClient(QObject):
    """NetworkClient (AbstractNetworkClient)."""

    serviceEnvironmentChanged = Signal()
    userAgentChanged = Signal()
    configurationChanged = Signal()

    # Without the scheme: Values.loginUrl is "https://" + this. The app's is the bare host
    # (my.remarkable.com); this is the page with the one-time code (LoginWizard adds "#desktop").
    loginUrl = Property(str, lambda self: PAIRING_URL.removeprefix("https://"), notify=serviceEnvironmentChanged)
    userAgent = Property(QByteArray, lambda self: QByteArray(user_agent().encode()), notify=userAgentChanged)


class DocumentController(QObject):
    """xofm::libs::library::DocumentController (TaskTracker): page editing. Adding,
    duplicating, moving and deleting pages are made as pending edits of the library
    (LibrarySource.add_page and the rest, worked as reMarkable.exe's DocumentWorker
    does, see core.pagemap), shown at once and uploaded by push_edits. The app runs these
    on a worker and calls the QML callback when done; here they are quick, so they run at
    once and the callback follows on the event loop. Templates, paper sizes and moving
    pages between documents are not made yet."""

    libraryChanged = Signal()
    isRunningTasksChanged = Signal()

    def __init__(self, library: QObject | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library
        # Set by the app: its LibrarySource, how it shows the library again after an edit
        # (with the pending page edits), and how it tells the user why an edit was not made.
        self.source: Callable[[], object] = lambda: None
        self.refresh: Callable[[], None] = lambda: None
        self.notify: Callable[[str], None] = lambda message: log.warning("%s", message)
        # The LibraryController's .content setter (the app sets it): (id, what, change) -> bool.
        self.set_setting: Callable[[str, str, Callable[[dict], bool]], bool] | None = None

    def _set_library(self, library: QObject) -> None:
        self._library = library
        self.libraryChanged.emit()

    library = Property(QObject, lambda self: self._library, _set_library, notify=libraryChanged)
    isRunningTasks = Property(bool, lambda self: False, notify=isRunningTasksChanged)

    def _unsupported(self, what: str, tell: bool = False) -> None:
        """A call this client does not make yet: logged (WARNING, so that it is in the
        journal, with its arguments). `tell`: the user asked for it (pages pasted into
        another document), so say so; calls the app makes on its own (a template or
        paper size set while opening a quick sheet) stay silent, as they were."""
        log.warning("not available in this client yet: %s", what)
        if tell:
            self.notify("Not available on Linux yet")

    def _now_ms(self) -> int:
        now = getattr(self._library, "now_ms", None)
        return int(now()) if callable(now) else int(QDateTime.currentMSecsSinceEpoch())

    def _edit(self, document_id: str, what: str, run, callback=None, signal=None) -> bool:
        """Make a page edit of document `document_id` with `run(source, node)`; then show
        the library again, emit `signal(entry, result)` and call the QML callback, as the
        app does when its task finishes. False (and the user told why) when not made."""
        source = self.source()
        library = self._library
        node = library.tree.get(str(document_id)) if library is not None else None
        if source is None or node is None:
            self.notify(f"Unable to {what}")
            return False
        from core.edits import EditError       # here: platform.py loads before the editing modules
        from core.sync import SyncError
        try:
            result = run(source, node)
        except (EditError, SyncError) as err:
            log.warning("%s of %s: %s", what, document_id, err)
            self.notify(str(err))
            return False
        self.refresh()
        entry = library.entryForId(str(document_id))
        if signal is not None and entry is not None:
            signal(entry, result)
        if callback is not None and callable(getattr(callback, "call", None)) and not callback.isUndefined():
            QTimer.singleShot(0, self, lambda: callback.call([]))
        return True

    @Slot(str, int, str)
    @Slot(str, int, str, QSizeF)
    def setTemplateForPage(self, document_id: str, page: int, template: str, paper_size=None) -> bool:
        """The page's template (LibrarySource.set_page_template: written only when it
        differs; lastModified moves), as the app's template task makes it. With a paper
        size, as the new-notebook dialog and Quick sheets pass Settings.paperSize, the
        task first sets the page scene's paper size (functor 0x14068d6e0 ->
        Scene::setPaperSize 0x1408aa520: nothing when the page has that size already,
        else SceneInfo fields 5, 7, 8; LibrarySource.set_page_paper); an empty size
        refuses the whole call ("invalid paper size when setting template")."""
        paper = None
        if paper_size is not None:
            size = QSizeF(paper_size)
            if size.isEmpty():
                log.warning("setTemplateForPage(%r, page %d): invalid paper size when setting template",
                            document_id, page)
                return False
            paper = (size.width(), size.height())

        def run(source, node):
            changed = False
            if paper is not None:
                ids = source.page_ids(source.synced(node.id) or node)
                if 0 <= int(page) < len(ids):
                    changed = source.set_page_paper(node, ids[int(page)], paper, self._now_ms())
            return source.set_page_template(node, int(page), str(template or ""), self._now_ms()) or changed

        return self._edit(document_id, "set template", run,
                          None, lambda entry, changed: entry.templatesChanged.emit() if changed else None)

    def _add_page(self, document_id: str, insert_index: int = -1, template=None, callback=None,
                  page_uuid: str = "", paper=None, paper_from_page: int = -1) -> bool:
        """doAddPage (0x140558ea0): no template given (or an empty one) takes the page
        before's, or "Blank" at the front. The new page's scene then gets a paper size
        (0x140559533): that of page `paper_from_page` when it is a page of the document,
        else `paper` when it is a valid size; else it keeps 1404 x 1872. Setting
        1404 x 1872 writes nothing (Scene::setPaperSize returns at once), so a default
        page still has no .rm until something is drawn on it."""
        def run(source, node):
            ids = source.page_ids(node)
            index = len(ids) if insert_index < 0 or insert_index > len(ids) else insert_index
            name = template or ""
            if not name:
                pages = node.content.pages
                before = next((p for p in pages if index > 0 and p.id == ids[index - 1]), None)
                name = before.template if before is not None and before.template else "Blank"
            size = None
            if 0 <= paper_from_page < len(ids):
                size = source.page_paper(node, ids[paper_from_page])
            elif paper is not None and paper[0] > 0 and paper[1] > 0:
                size = paper
            edit = source.add_page(node, index, name, self._now_ms(), page_uuid)
            if size is not None:
                source.set_page_paper(node, edit.new_id, size, self._now_ms())
            return source.page_ids(node).index(edit.new_id)

        return self._edit(document_id, "add page", run, callback,
                          lambda entry, index: (entry.pageAdded.emit(index), entry.pagesAdded.emit([index])))

    @Slot(str, result=bool)
    @Slot(str, int, result=bool)
    @Slot(str, int, str, result=bool)
    @Slot(str, int, str, QSizeF, result=bool)
    @Slot(str, int, str, QSizeF, "QJSValue", result=bool)
    @Slot(str, int, str, QSizeF, "QJSValue", str, result=bool)
    def addPageWithTemplateAndPageSize(self, document_id: str, insert_index: int = -1, template=None,
                                       paper_size=None, callback=None, page_uuid: str = "") -> bool:
        paper = None
        if paper_size is not None:
            size = QSizeF(paper_size)
            if size.isValid():
                paper = (size.width(), size.height())
        return self._add_page(document_id, insert_index, template, callback, page_uuid, paper=paper)

    @Slot(str, result=bool)
    @Slot(str, int, result=bool)
    @Slot(str, int, str, result=bool)
    @Slot(str, int, str, int, result=bool)
    @Slot(str, int, str, int, "QJSValue", result=bool)
    def addPageWithTemplateAndPaperSizeFromPage(self, document_id: str, insert_index: int = -1, template=None,
                                                paper_size_from_page: int = -1, callback=None) -> bool:
        return self._add_page(document_id, insert_index, template, callback,
                              paper_from_page=int(paper_size_from_page))

    def _external(self, document_id: str, target_id: str, what: str, run, callback=None, moved: bool = False) -> bool:
        """Pages of `document_id` copied (or moved) into another document `target_id`,
        as the app's CopyExternalPagesTask does (LibrarySource.import_pages /
        move_pages_to): `run(source, source node, target node)` returns the PageImport;
        then the library is shown again, the target emits pagesAdded with the new
        pages' indices (and the source pagesRemoved, for a move), and the QML callback
        runs. False (and the user told why) when not made."""
        source = self.source()
        library = self._library
        node = library.tree.get(str(document_id)) if library is not None else None
        target = library.tree.get(str(target_id)) if library is not None else None
        if source is None or node is None or target is None:
            log.warning("%s: invalid source and/or target (%r -> %r)", what, document_id, target_id)
            self.notify(f"Unable to {what}")
            return False
        from core.edits import EditError
        from core.sync import SyncError
        try:
            edit = run(source, node, target)
        except (EditError, SyncError) as err:
            log.warning("%s of %s into %s: %s", what, document_id, target_id, err)
            self.notify(str(err))
            return False
        self.refresh()
        ids = source.page_ids(source.synced(target.id) or target)
        added = [ids.index(n) for n in edit.new_ids if n in ids]
        entry = library.entryForId(str(target_id))
        if entry is not None:
            entry.pagesAdded.emit(added)
        if moved:
            gone = library.entryForId(str(document_id))
            if gone is not None:
                gone.pagesRemoved.emit()
        if callback is not None and callable(getattr(callback, "call", None)) and not callback.isUndefined():
            QTimer.singleShot(0, self, lambda: callback.call([]))
        return True

    @Slot(str, "QVariantList", str, int, result=bool)
    @Slot(str, "QVariantList", str, int, "QJSValue", result=bool)
    def movePages(self, document_id: str, pages: list, target_id: str, position: int, callback=None) -> bool:
        """Pages moved within a document (doMovePages), or into another one: copied in
        (CopyExternalPagesTask) and then deleted from `document_id` (DeletePagesTask), as
        the app's task group does (0x140491b70)."""
        if target_id and target_id != document_id:
            return self._external(document_id, target_id, "move pages",
                                  lambda source, node, target: source.move_pages_to(
                                      target, node, list(pages), int(position), self._now_ms())[0],
                                  callback, moved=True)
        return self._edit(document_id, "move pages",
                          lambda source, node: source.move_pages(node, list(pages), position, self._now_ms()),
                          callback, lambda entry, _: entry.pagesMoved.emit())

    @Slot(str, "QVariantList", str, int, result=bool)
    def copyPages(self, document_id: str, pages: list, target_id: str, position: int) -> bool:
        """Pages duplicated within a document (CopyPagesTask), or copied into another one
        (CopyExternalPagesTask, 0x140490bb0 -> 0x14068ffb0)."""
        if target_id and target_id != document_id:
            return self._external(document_id, target_id, "paste pages",
                                  lambda source, node, target: source.import_pages(
                                      target, node, list(pages), int(position), self._now_ms())[0])

        def run(source, node):
            edit = source.copy_pages(node, list(pages), position, self._now_ms())
            ids = source.page_ids(node)
            return [ids.index(n) for n in edit.new_ids if n in ids]

        return self._edit(document_id, "duplicate page", run, None,
                          lambda entry, added: entry.pagesAdded.emit(added))

    @Slot(str, "QVariantList", result=bool)
    @Slot(str, "QVariantList", "QJSValue", result=bool)
    def deletePages(self, document_id: str, pages: list, callback=None) -> bool:
        return self._edit(document_id, "delete page",
                          lambda source, node: source.delete_pages(node, list(pages), self._now_ms()),
                          callback, lambda entry, _: entry.pagesRemoved.emit())

    @Slot(str, str, result=bool)
    def setViewBackgroundFilter(self, document_id: str, value: str) -> bool:
        """DocumentController::setViewBackgroundFilter (0x140493250 -> 0x1404932d0 ->
        Document 0x140668a10): the document's .content "viewBackgroundFilter" string,
        when it differs (an empty one removes the key, as the writer leaves it out);
        "invalid document..." and False without a document. The desktop QML never
        calls it; the value is stored as given (nothing here draws a filter)."""
        from core.create import set_view_background_filter
        if self.set_setting is None:
            log.warning("setViewBackgroundFilter: invalid document... (%r)", document_id)
            return False
        ok = self.set_setting(str(document_id), "Setting view background filter",
                              lambda content: set_view_background_filter(content, str(value or "")))
        library = self._library
        entry = library.entryForId(str(document_id)) if ok and library is not None else None
        if entry is not None and hasattr(entry, "viewBackgroundFilterChanged"):
            entry.viewBackgroundFilterChanged.emit()
        return ok

    @Slot(str, QSizeF, result=bool)
    @Slot(str, QSizeF, "QJSValue", result=bool)
    def setPaperSizeForQuickSheetPage(self, document_id: str, size: QSizeF, callback=None) -> bool:
        """DocumentController::setPaperSizeForQuickSheetPage (0x140492cd0 -> 0x140492a80):
        only for the Quick sheets document while it has fewer than 2 pages, it sets the
        paper size of its first page's scene (task 0x14068f1c0 -> functor 0x14068d6b0 ->
        Scene::setPaperSize 0x1408aa520; LibrarySource.set_page_paper), then calls back.
        The task warns and stops on an invalid size."""
        from core.create import is_quick_sheet
        from .library import tr
        library = self._library
        node = library.tree.get(str(document_id)) if library is not None else None
        if node is None:
            log.warning("setPaperSizeForQuickSheetPage passed a null document")
            return False
        entry = library.entryForId(node.id)
        if not is_quick_sheet(node, library.tree, tr("Document", "Quick sheets")) or entry.pageCount >= 2:
            log.warning("Changing paper size is only allowed for Quick sheets that has one page.")
            return False
        size = QSizeF(size)
        if not size.isValid():
            log.warning("setPaperSizeForQuickSheetPage(%r): invalid paper size", document_id)
            return False
        source = self.source()
        if source is None:
            return False
        from core.edits import EditError
        from core.sync import SyncError
        try:
            ids = source.page_ids(source.synced(node.id) or node)
            if ids:
                source.set_page_paper(node, ids[0], (size.width(), size.height()), self._now_ms())
        except (EditError, SyncError) as err:
            log.warning("setPaperSizeForQuickSheetPage(%r): %s", document_id, err)
            self.notify(str(err))
            return False
        self.refresh()
        log.info("Set paper size for Quick sheet finished")
        if callback is not None and callable(getattr(callback, "call", None)) and not callback.isUndefined():
            QTimer.singleShot(0, self, lambda: callback.call([]))
        return True

    @Slot("QVariant")
    def execute(self, task) -> None:
        pass

    @Slot("QVariant")
    def onFinished(self, task) -> None:
        pass

    @Slot("QVariant")
    def onFailed(self, task) -> None:
        pass


class Clipboard(QObject):
    """The app's Clipboard (xofm/libs/ui/src/clipboard.cpp): items copied in a document
    (`items`: clones of scene items, kept here only), and the system clipboard's content
    (`text`, the app's SceneClipboardText) for pasting text or a picture from other
    programs. The library clipboard (copy/cut/move of entries) lives in main.qml.

    `text` reaches QML as {"text", "html", "hasImage", "imageSerial"}; the picture itself
    stays here (image_of), as QML only hands the content back to the SceneController."""

    Action = IntEnum("Action", {"None": 0, "Copy": 1, "Cut": 2, "Move": 3})
    LastCopy = IntEnum("LastCopy", {"Text": 0, "Items": 1})
    QEnum(Action)
    QEnum(LastCopy)

    itemsChanged = Signal()
    textChanged = Signal()
    lastCopiedTypeChanged = Signal()
    imagePastingEnabledChanged = Signal()

    def __init__(self, parent: QObject | None = None, system=None) -> None:
        super().__init__(parent)
        self._content: dict = {}
        self._image = QImage()
        self._serial = 0
        self._items: list = []
        self._last = 0
        # The desktop app turns image pasting on when it starts (0x14009cda7).
        self._image_pasting = True
        self._system = system if system is not None else (QGuiApplication.clipboard()
                                                          if QGuiApplication.instance() is not None else None)
        if self._system is not None:
            self._system.dataChanged.connect(self.systemClipboardDataChanged)
            self.systemClipboardDataChanged()

    # -- items --
    def _set_items(self, items) -> None:
        self._items = list(items or [])
        self._set_last(1)
        self.itemsChanged.emit()

    hasItems = Property(bool, lambda self: bool(self._items), notify=itemsChanged)
    items = Property("QVariantList", lambda self: self._items, _set_items, notify=itemsChanged)

    # -- text and pictures --
    def _set_content(self, content) -> None:
        if isinstance(content, str):
            content = {"text": content}
        content = dict(content or {})
        self._content = {"text": str(content.get("text") or ""), "html": str(content.get("html") or ""),
                         "hasImage": False, "imageSerial": 0}
        self._image = QImage()
        self._set_last(0)
        self.textChanged.emit()

    def _set_last(self, kind: int) -> None:
        if kind != self._last:
            self._last = kind
            self.lastCopiedTypeChanged.emit()

    hasText = Property(bool, lambda self: bool(self._content.get("text") or self._content.get("html")
                                               or self._content.get("hasImage")), notify=textChanged)
    text = Property("QVariant", lambda self: dict(self._content), _set_content, notify=textChanged)
    lastCopiedType = Property(int, lambda self: self._last, _set_last, notify=lastCopiedTypeChanged)

    def _set_image_pasting(self, value: bool) -> None:
        self._image_pasting = bool(value)
        self.imagePastingEnabledChanged.emit()

    imagePastingEnabled = Property(bool, lambda self: self._image_pasting, _set_image_pasting,
                                   notify=imagePastingEnabledChanged)

    def image_of(self, content) -> QImage:
        """The picture of a clipboard content QML handed back, while it is the current one."""
        serial = (dict(content) if hasattr(content, "keys") else {}).get("imageSerial")
        return QImage(self._image) if serial and serial == self._serial else QImage()

    @Slot()
    def systemClipboardDataChanged(self) -> None:
        """Take what another program put on the system clipboard
        (transferDataFromSystemClipboard, 0x140607f30): a picture from a copied image file,
        the picture itself, image data, or a picture in copied HTML, in that order; and
        the text and HTML, unless the picture is what was copied (_prefers_image)."""
        board = self._system
        if board is None or board.ownsClipboard():
            return
        mime = board.mimeData()
        if mime is None:
            return
        image, from_url, from_html = QImage(), False, False
        if self._image_pasting and mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    image = QImage(url.toLocalFile())
                    if not image.isNull():
                        from_url = True
                        break
        if image.isNull() and self._image_pasting and mime.hasImage():
            image = board.image()
        if image.isNull() and self._image_pasting:
            image = _image_data(mime)
        if image.isNull() and self._image_pasting and mime.hasHtml():
            image = _html_image(mime.html())
            from_html = not image.isNull()
        prefer = not image.isNull() and (from_url or from_html or _prefers_image(mime))
        html = mime.html() if mime.hasHtml() and not prefer else ""
        text = mime.text() if mime.hasText() and not prefer else ""
        if not text and not html and image.isNull():
            log.debug("empty clipboard content, ignore")
            return
        self._serial += 1
        self._content = {"text": text, "html": html, "hasImage": not image.isNull(),
                         "imageSerial": self._serial if not image.isNull() else 0}
        self._image = image
        self._set_last(0)
        self.textChanged.emit()

    @Slot(result=str)
    def textString(self) -> str:
        return self._content.get("text", "")

    @Slot(str)
    def setTextFromString(self, text: str) -> None:
        """Text copied in the app, also to the system clipboard (the app's setText)."""
        self._set_content(text)
        if self._system is not None:
            self._system.setText(text)


def _prefers_image(mime) -> bool:
    """Whether a copied picture is meant rather than the text with it (0x140607db0): the
    clipboard has files, or a picture with HTML and no text, or a web address as text
    with an <img> in its HTML (a picture copied from a browser)."""
    if mime.hasUrls():
        return True
    if mime.hasImage() and mime.hasHtml() and not mime.hasText():
        return True
    if mime.hasText() and mime.hasHtml():
        text = mime.text().strip()
        return text.startswith(("http://", "https://")) and "<img" in mime.html()
    return False


def _image_data(mime) -> QImage:
    """A picture among the clipboard's formats (0x140606a80): an image/* one, else one
    that is not text or HTML."""
    formats = mime.formats()
    for kind in [f for f in formats if f.startswith("image/")] + \
                [f for f in formats if not f.startswith("text/") and "html" not in f]:
        data = mime.data(kind)
        image = QImage()
        if not data.isEmpty() and image.loadFromData(data):
            return image
    return QImage()


def _html_image(html: str) -> QImage:
    """The picture of the first <img> in copied HTML: inline (data:) or a local file."""
    import base64
    import re
    from urllib.parse import unquote, urlparse
    for src in re.findall(r"""<img[^>]*\ssrc\s*=\s*["']([^"']+)["']""", html, re.I):
        image = QImage()
        if src.startswith("data:") and ";base64," in src:
            try:
                image.loadFromData(base64.b64decode(src.split(";base64,", 1)[1]))
            except ValueError:
                continue
        elif src.startswith("file:"):
            image = QImage(unquote(urlparse(src).path))
        if not image.isNull():
            return image
    return QImage()


# -- images -------------------------------------------------------------------------

class ArkIconProvider(AsyncImageProvider):
    """image://arkicon/<qrc url>?border=&fill=: the official SVG icons in the asked
    colours (resolved by the native ArkImageDataProvider). Icons draw in black; black
    takes the icon colour, white takes a distinct fill colour."""

    def __init__(self) -> None:
        super().__init__(threads=1)

    def render(self, ident: str, requested: QSize) -> QImage:
        path_part, _, query = ident.partition("?")
        source = unquote(path_part)
        params = {k: v[0] for k, v in parse_qs(query).items()}
        path = source[3:] if source.startswith("qrc:") else source
        file = QFile(path)
        if not file.open(QIODevice.OpenModeFlag.ReadOnly):
            return QImage()
        svg = bytes(file.readAll()).decode("utf-8")
        border = params.get("border") or "#000000"
        fill = params.get("fill") or border
        svg = svg.replace('"black"', f'"{_svg_color(border)}"')
        if fill != border:
            svg = svg.replace('"white"', f'"{_svg_color(fill)}"')
        opacity = QColor(border).alphaF()
        renderer = QSvgRenderer(QByteArray(svg.encode()))
        target = requested if requested.isValid() and not requested.isEmpty() else renderer.defaultSize()
        image = QImage(target, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setOpacity(opacity)
        renderer.render(painter, QRectF(0, 0, target.width(), target.height()))
        painter.end()
        return image


def _svg_color(name: str) -> str:
    """#AARRGGBB (Qt) → #RRGGBB (SVG); alpha is applied as opacity."""
    color = QColor(name)
    return color.name(QColor.NameFormat.HexRgb) if color.isValid() else "#000000"


class DeviceRecovery(QObject):
    """xofm.libs.recovery DeviceRecovery: reflashing a tablet over USB, which this
    client does not do; discovery reports FAIL, so RecoveryPopup shows its connection
    error state."""

    Result = IntEnum("Result", {"UNEXPECTED_MESSAGE": 0, "OK": 1, "FAIL": 2, "PENDING": 3})
    QEnum(Result)

    progressChanged = Signal()
    lastTaskChanged = Signal()
    discoverDeviceFinished = Signal()
    recoveryFinished = Signal()
    discoverDeviceResultChanged = Signal()
    recoveryResultChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._discovery = 3
        self._recovery = 3

    progress = Property(float, lambda self: 0.0, notify=progressChanged)
    lastTask = Property(str, lambda self: "", notify=lastTaskChanged)
    discoverDeviceResult = Property(int, lambda self: self._discovery, notify=discoverDeviceResultChanged)
    recoveryResult = Property(int, lambda self: self._recovery, notify=recoveryResultChanged)

    @Slot()
    def discoverDevice(self) -> None:
        log.info("device recovery over USB is not available in this client")
        self._discovery = 2
        self.discoverDeviceResultChanged.emit()
        self.discoverDeviceFinished.emit()

    @Slot(bool)
    def startRecovery(self, keep_user_data: bool) -> None:
        self._recovery = 2
        self.recoveryResultChanged.emit()
        self.recoveryFinished.emit()


# Handwriting conversion languages (the app's list: display name, MyScript locale).
HWC_LANGUAGES = (
    ("Basque", "eu_ES"), ("Bulgarian", "bg_BG"), ("Catalan", "ca_ES"), ("Croatian", "hr_HR"), ("Czech", "cs_CZ"),
    ("Danish", "da_DK"), ("Dutch (BE)", "nl_BE"), ("Dutch (NL)", "nl_NL"), ("English (CA)", "en_CA"),
    ("English (UK)", "en_GB"), ("English (US)", "en_US"), ("Estonian", "et_EE"), ("Finnish", "fi_FI"),
    ("French (CA)", "fr_CA"), ("French (FR)", "fr_FR"), ("Galician", "gl_ES"), ("German (AT)", "de_AT"),
    ("German (DE)", "de_DE"), ("Greek", "el_GR"), ("Hungarian", "hu_HU"), ("Icelandic", "is_IS"),
    ("Irish", "ga_IE"), ("Italian", "it_IT"), ("Latvian", "lv_LV"), ("Lithuanian", "lt_LT"),
    ("Norwegian", "no_NO"), ("Polish", "pl_PL"), ("Portuguese", "pt_PT"), ("Romanian", "ro_RO"),
    ("Slovak", "sk_SK"), ("Slovenian", "sl_SI"), ("Spanish", "es_ES"), ("Swedish", "sv_SE"))
# Older codes the app still accepts.
HWC_ALIASES = {"en_UK": "en_GB", "no_DK": "no_NO", "no_SV": "no_NO"}


class HandwritingLanguageModel(QObject):
    """The handwriting conversion languages for the settings picker."""

    def _code(self, code: str) -> str:
        return HWC_ALIASES.get(code, code)

    @Slot(str, result=bool)
    def validCode(self, code: str) -> bool:
        return any(c == self._code(code) for _, c in HWC_LANGUAGES)

    @Slot(result=str)
    def defaultCode(self) -> str:
        return "en_US"

    @Slot(result="QStringList")
    def languageNames(self) -> list:
        return [name for name, _ in HWC_LANGUAGES]

    @Slot(str, result=str)
    def languageCode(self, language: str) -> str:
        return next((c for name, c in HWC_LANGUAGES if name == language), "")

    @Slot(str, result=str)
    def language(self, code: str) -> str:
        """The name without its region: "English (US)" -> "English"."""
        return self.languageWithRegion(code).split(" (")[0]

    @Slot(str, result=str)
    def languageWithRegion(self, code: str) -> str:
        return next((name for name, c in HWC_LANGUAGES if c == self._code(code)), "")

    @Slot(str, result=int)
    def getLanguageNameIndex(self, language: str) -> int:
        names = self.languageNames()
        return names.index(language) if language in names else -1
