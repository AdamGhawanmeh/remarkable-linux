"""com.remarkable platform types, the Settings objects and the arkicon image provider.

Members and enum values are the app's (bridge/qml_api.json). Features this client
does not have (app updates, What's new, scanning, page editing) report
"not available" rather than pretending.
"""

from __future__ import annotations

import json
import logging
import os
import platform as _platform
import ssl
import sys
from enum import IntEnum
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote

from PySide6.QtCore import (Property, QByteArray, QCoreApplication, QDate, QFile, QIODevice, QLocale, QObject, QRectF, QSettings,
                            QSize, QSizeF, QUrl, Qt, Signal, Slot, QEnum)
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QImage, QPainter
from PySide6.QtNetwork import QNetworkInformation

from .images import AsyncImageProvider
from PySide6.QtSvg import QSvgRenderer

from core.auth import PAIRING_URL, AuthNetworkError, PairingError, user_agent

log = logging.getLogger("rm.ui.quick.platform")


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
        value = self._store.get("paperSize")
        return value if isinstance(value, QSizeF) else QSizeF()

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
    connectionIsMetered = Property(bool, lambda self: False, constant=True)


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
        log.info("openFileChooser: the desktop QML uses FileImportDialog")

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
        pass

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
        return bool(self._export(path, document_id, kind, [int(p) for p in pages] if pages else None))

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


class PlatformApp(QObject):
    """PlatformApp (XoApp): updates are the distribution's job, so no Sparkle. logout()
    (LogoutConfirmDialog) runs `on_logout`; the dialog waits for Authentication.loggedInChanged."""

    def __init__(self, on_logout: Callable[[], None] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.on_logout = on_logout

    @Slot()
    def logout(self) -> None:
        if self.on_logout is not None:
            self.on_logout()

    @Slot(str, result=bool)
    def shouldShowWhatsNew(self, version: str) -> bool:
        return False

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
        self._values = {"id": "", "name": "", "username": "", "email": email, "tectonic": ""}
        self._account_type = 2
        self._created_at = 0
        self._client_edit = False

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
    subscriptionLevel = Property(str, lambda self: "", notify=subscriptionLevelStringSet)
    tectonic = Property(str, _value("tectonic"), notify=tectonicChanged)
    isSubscribed = Property(bool, lambda self: False, notify=isSubscribedChanged)

    @Slot(result=str)
    def getAccountTypeString(self) -> str:
        return {0: "Google", 1: "Facebook"}.get(self._account_type, "")

    @Slot(result=int)
    def daysSinceCreation(self) -> int:
        return 0


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
    """xofm::libs::library::DocumentController (TaskTracker): page editing, which this
    client does not do."""

    libraryChanged = Signal()
    isRunningTasksChanged = Signal()

    def __init__(self, library: QObject | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library

    def _set_library(self, library: QObject) -> None:
        self._library = library
        self.libraryChanged.emit()

    library = Property(QObject, lambda self: self._library, _set_library, notify=libraryChanged)
    isRunningTasks = Property(bool, lambda self: False, notify=isRunningTasksChanged)

    def _unsupported(self, what: str) -> None:
        log.info("%s: page editing is not available in this client", what)

    @Slot(str, int, str)
    @Slot(str, int, str, QSizeF)
    def setTemplateForPage(self, document_id: str, page: int, template: str, paper_size=None) -> None:
        self._unsupported("setTemplateForPage")

    @Slot(str)
    @Slot(str, int)
    @Slot(str, int, str)
    @Slot(str, int, str, QSizeF)
    @Slot(str, int, str, QSizeF, "QJSValue")
    @Slot(str, int, str, QSizeF, "QJSValue", str)
    def addPageWithTemplateAndPageSize(self, document_id: str, *args) -> None:
        self._unsupported("addPageWithTemplateAndPageSize")

    @Slot(str)
    @Slot(str, int)
    @Slot(str, int, str)
    @Slot(str, int, str, int)
    @Slot(str, int, str, int, "QJSValue")
    def addPageWithTemplateAndPaperSizeFromPage(self, document_id: str, *args) -> None:
        self._unsupported("addPageWithTemplateAndPaperSizeFromPage")

    @Slot(str, "QVariantList", str, int)
    @Slot(str, "QVariantList", str, int, "QJSValue")
    def movePages(self, document_id: str, pages: list, target_id: str, position: int, callback=None) -> None:
        self._unsupported("movePages")

    @Slot(str, "QVariantList", str, int)
    def copyPages(self, document_id: str, pages: list, target_id: str, position: int) -> None:
        self._unsupported("copyPages")

    @Slot(str, "QVariantList")
    @Slot(str, "QVariantList", "QJSValue")
    def deletePages(self, document_id: str, pages: list, callback=None) -> None:
        self._unsupported("deletePages")

    @Slot(str, str)
    def setViewBackgroundFilter(self, document_id: str, value: str) -> None:
        self._unsupported("setViewBackgroundFilter")

    @Slot(str, QSizeF)
    @Slot(str, QSizeF, "QJSValue")
    def setPaperSizeForQuickSheetPage(self, document_id: str, size: QSizeF, callback=None) -> None:
        self._unsupported("setPaperSizeForQuickSheetPage")

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
    """The scene clipboard (strokes and text copied inside documents); the library
    clipboard state (copy/cut/move of entries) lives in main.qml."""

    Action = IntEnum("Action", {"None": 0, "Copy": 1, "Cut": 2, "Move": 3})
    LastCopy = IntEnum("LastCopy", {"Text": 0, "Items": 1})
    QEnum(Action)
    QEnum(LastCopy)

    itemsChanged = Signal()
    textChanged = Signal()
    lastCopiedTypeChanged = Signal()
    imagePastingEnabledChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._image_pasting = False

    hasItems = Property(bool, lambda self: False, notify=itemsChanged)
    items = Property("QVariantList", lambda self: [], notify=itemsChanged)
    hasText = Property(bool, lambda self: bool(self._text), notify=textChanged)
    text = Property(str, lambda self: self._text, notify=textChanged)
    lastCopiedType = Property(int, lambda self: 0, notify=lastCopiedTypeChanged)

    def _set_image_pasting(self, value: bool) -> None:
        self._image_pasting = bool(value)
        self.imagePastingEnabledChanged.emit()

    imagePastingEnabled = Property(bool, lambda self: self._image_pasting, _set_image_pasting,
                                   notify=imagePastingEnabledChanged)

    @Slot()
    def systemClipboardDataChanged(self) -> None:
        pass

    @Slot(result=str)
    def textString(self) -> str:
        return self._text

    @Slot(str)
    def setTextFromString(self, text: str) -> None:
        self._text = text
        self.textChanged.emit()


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
