"""Python implementations of the app's C++ QML types, registered under the names and in
the modules the app registers them in (bridge/qml_api.json records the app's own
metadata for each; tests check these classes against it)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtQml import QQmlEngine, qmlRegisterSingletonType, qmlRegisterType, qmlRegisterUncreatableType

from . import native
from .generated import REGISTERED
from .images import DragIconImageProvider, ThumbnailImageProvider, ThumbnailProvider
from .importer import DocumentImporter
from .items import DocumentCover, FontExtender, ShareNative, TriangleItem
from .library import (DocumentIOEnums, DocumentSelection, DocumentSync, DocumentWrapper, EntryWrapper, Library,
                      LibraryController, LibraryNavigator, LibrarySearch, LibraryView, PageSelection,
                      set_library_source)
from .platform import (ArkIconProvider, ArkTokensSettings, Authentication, Clipboard, DeviceRecovery,
                       DocumentController,
                       HandwritingLanguageModel, LanguageSettings, LinkProvider, NetworkClient, PlatformApp, PlatformHelpers,
                       Settings, UserInfo)
from .scene import (DocumentLockManager, DocumentSearchController, Page, PagePickerModel, PageSearchResultOverlayItem,
                    ParagraphStyle, RecognitionJob, Recognizer, SceneController, SceneKeyHandler, SceneKeyHandlerAction,
                    SceneLayersModel, SceneLinkHandler, SceneParticipant, SceneRenderContext, SceneTileManager, SceneView,
                    SelectionImage)
from .screenshare import ScreenShareClient, ScreenSharePainter

REMARKABLE, LIBRARY, ARK_CONTROLS, ARK_TOKENS = "com.remarkable", "xofm.libs.library", "ark.controls", "ark.tokens"
RECOVERY, SCENEVIEW = "xofm.libs.recovery", "xofm.libs.sceneview"
_native_registered = False


def register_native() -> None:
    """arkImageInfo and ArkImageDataProvider (ark.controls), from the C++ module."""
    global _native_registered
    if not _native_registered:
        native.library().rm_register_ark_controls()
        _native_registered = True


@dataclass
class Singletons:
    library: Library
    controller: LibraryController
    importer: DocumentImporter
    selection: DocumentSelection
    document_sync: DocumentSync
    screen_share: ScreenShareClient
    settings: Settings
    ark_settings: ArkTokensSettings
    platform_helpers: PlatformHelpers
    platform_app: PlatformApp
    authentication: Authentication
    user_info: UserInfo
    network_client: NetworkClient
    document_controller: DocumentController
    clipboard: Clipboard
    language: LanguageSettings
    link: LinkProvider
    handwriting: HandwritingLanguageModel
    recognizer: Recognizer


# (QML module, QML name, class) as the app registers them.
UNCREATABLE = ((REMARKABLE, "LibraryView", LibraryView), (REMARKABLE, "Entry", EntryWrapper),
               (REMARKABLE, "Document", DocumentWrapper), (REMARKABLE, "DocumentIO", DocumentIOEnums),
               (REMARKABLE, "SceneController", SceneController), (REMARKABLE, "SceneTileManager", SceneTileManager),
               (REMARKABLE, "SceneKeyHandler", SceneKeyHandler),
               (REMARKABLE, "SceneKeyHandlerAction", SceneKeyHandlerAction),
               (REMARKABLE, "SceneParticipant", SceneParticipant), (REMARKABLE, "SceneRenderContext", SceneRenderContext),
               (REMARKABLE, "ParagraphStyle", ParagraphStyle), (REMARKABLE, "Page", Page),
               (REMARKABLE, "RecognitionJob", RecognitionJob))
CREATABLE = ((REMARKABLE, "LibraryNavigator", LibraryNavigator), (REMARKABLE, "LibrarySearch", LibrarySearch),
             (REMARKABLE, "DocumentCover", DocumentCover), (REMARKABLE, "Triangle", TriangleItem),
             (REMARKABLE, "ThumbnailProvider", ThumbnailProvider), (REMARKABLE, "ShareNative", ShareNative),
             (REMARKABLE, "ScreenSharePainter", ScreenSharePainter), (REMARKABLE, "PageSelection", PageSelection),
             (ARK_CONTROLS, "FontExtender", FontExtender),
             (RECOVERY, "DeviceRecovery", DeviceRecovery),
             (REMARKABLE, "SceneView", SceneView), (REMARKABLE, "DocumentLockManager", DocumentLockManager),
             (REMARKABLE, "SceneLinkHandler", SceneLinkHandler),
             (REMARKABLE, "PageSearchResultOverlayItem", PageSearchResultOverlayItem),
             (REMARKABLE, "DocumentSearchController", DocumentSearchController),
             (REMARKABLE, "PagePickerModel", PagePickerModel), (REMARKABLE, "SelectionImage", SelectionImage),
             (REMARKABLE, "SceneLayersModel", SceneLayersModel), (SCENEVIEW, "SceneLayersModel", SceneLayersModel))
# (attribute of Singletons, QML module, QML name, class). Registered once per process
# with a callback that hands each engine the current app's object: Qt requires every
# type to be registered before the first engine exists (registering later breaks
# QObject* property assignment in that engine), and tests create several apps.
SINGLETONS = (("library", LIBRARY, "Library", Library), ("controller", LIBRARY, "LibraryController", LibraryController),
              ("importer", LIBRARY, "DocumentImporter", DocumentImporter),
              ("selection", LIBRARY, "DocumentSelection", DocumentSelection),
              ("document_sync", REMARKABLE, "DocumentSync", DocumentSync),
              ("screen_share", REMARKABLE, "ScreenShareClient", ScreenShareClient),
              ("platform_helpers", REMARKABLE, "PlatformHelpers", PlatformHelpers),
              # Also the "Settings" context property (use_singletons): as a type, QML reads its enums.
              ("settings", REMARKABLE, "Settings", Settings),
              ("platform_app", REMARKABLE, "PlatformApp", PlatformApp),
              ("authentication", REMARKABLE, "Authentication", Authentication),
              ("user_info", REMARKABLE, "UserInfo", UserInfo),
              ("network_client", REMARKABLE, "NetworkClient", NetworkClient),
              ("document_controller", REMARKABLE, "DocumentController", DocumentController),
              ("clipboard", REMARKABLE, "Clipboard", Clipboard),
              ("language", REMARKABLE, "LanguageSettings", LanguageSettings),
              ("link", REMARKABLE, "LinkProvider", LinkProvider),
              ("handwriting", REMARKABLE, "HandwritingLanguageModel", HandwritingLanguageModel),
              ("recognizer", REMARKABLE, "Recognizer", Recognizer),
              ("ark_settings", ARK_TOKENS, "Settings", ArkTokensSettings))
_registered_types = False
_current: Singletons | None = None


def _provider(attribute: str):
    def provide(engine):
        if _current is None:
            raise RuntimeError("use_singletons() was not called before loading QML")
        obj = getattr(_current, attribute)
        QQmlEngine.setObjectOwnership(obj, QQmlEngine.ObjectOwnership.CppOwnership)
        return obj
    return provide


def register_types() -> None:
    """Every QML type and singleton; call before creating the first QML engine."""
    global _registered_types
    if _registered_types:
        return
    _registered_types = True
    register_native()
    for uri, name, cls in UNCREATABLE:
        if (cls, uri, name) not in REGISTERED:
            qmlRegisterUncreatableType(cls, uri, 1, 0, name, f"{name} is provided by the app")
    for uri, name, cls in CREATABLE:
        if (cls, uri, name) not in REGISTERED:
            qmlRegisterType(cls, uri, 1, 0, name)
    for attribute, uri, name, cls in SINGLETONS:
        qmlRegisterSingletonType(cls, uri, 1, 0, name, _provider(attribute))
    set_library_source(lambda: _current.library if _current is not None else None)


def use_singletons(engine: QQmlEngine, s: Singletons) -> None:
    """Make `s` the objects the next engines' singletons resolve to (they are created on
    first use), and set the app's Settings global on `engine`."""
    global _current
    _current = s
    engine.rootContext().setContextProperty("Settings", s.settings)
