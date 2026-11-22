# reMarkable Desktop (Windows) — Architectural Reconnaissance

Target: `extracted/reMarkable.exe` — package `com.remarkable.xochitl` **v3.29.0**
(`components.xml`), PE32+ x86-64, SHA-256 `42390f0099e60e8712a37e1146355a16e993ba5c1ef14226750bcb2fefc376d3`.
Built from the same `xochitl` source tree as the tablet firmware (`C:\a\xochitl\xochitl\src\...`),
Qt **6.10.3** / MSVC 2022, dependencies from vcpkg.

Legend: **[O]** observed directly in the shipped bytes · **[I]** inference from observed
strings/code proximity · **[?]** unknown, needs runtime capture to confirm.

Method & limits: static only. No disassembler engine (Ghidra/IDA/Hopper) was available
to REA, so there is no decompilation. Evidence comes from REA PE import/export inspection
(evidence `ev_0476ff2a…` imports, `ev_13f6c8fc…` datachannel exports), a custom Qt rcc
carver, string extraction, and a RIP-relative `lea` scan that ties strings to the
functions that use them. Nothing was executed and no network traffic was observed.

---

## 1. Process & module layout

| Component | Role |
|---|---|
| `reMarkable.exe` (20 MB) | Entire app: C++ core + QML UI embedded as Qt resources |
| Qt6 Core/Gui/Qml/Quick/QuickControls2/Network/WebSockets/WebView/Svg/Xml/DBus | UI and networking runtime |
| `pdfium.dll` | PDF rendering **and** PDF export (78 imported `FPDF*` functions) |
| `datachannel.dll` (libdatachannel) + `juice.dll` (libjuice **v1.7.0**) | WebRTC (ICE/DTLS/SCTP) for Screen Share |
| `libssl-3` / `libcrypto-3` | Imported directly by the exe — used by a **statically linked libcurl** (`rm.http.curl`, curl.se strings) and by libdatachannel DTLS |
| `WinSparkle.dll` | Auto-update (`https://get-updates.cloud.remarkable.engineering/sparkle/…/appcast.xml`) |
| `rm_recover.exe` | Separate console tool for device recovery (USB-C) |
| `qml/` directory on disk | **Stock Qt modules only.** No app QML lives here. |

Internal C++ modules (from 201 leaked source paths, `rm.*` log categories):

- `xofm/libs/sync` (56 files), `syncinterface`, `http` (25), `network`, `user`, `library`,
  `sceneview`, `peninput`, `settings`, `recovery`, `localization`, `logout`, `navigation`
- `entry`, `entryfile` — document model & on-disk files (`.content`, `.metadata`, tags)
- `screenshare/{core,client}` + `webrtc` — LiveView
- `handwritingconversion` (MyScript JIIX via cloud), `epub`, `search`, `notifications`, `worker`
- `rm.crdt.*`, `rm.scene.*` — CRDT scene graph backing v6 `.rm` files

## 2. Embedded QML / Qt resources

The app registers 17 rcc trees via `qRegisterResourceData`, all from the C runtime's
static-initializer table. `client/tools/rcc_carve.py` finds every call to that import
(direct through the IAT or via its jmp thunk), reads the tree/names/data pointers from
the `lea r9 / mov ecx,3 / lea r8 / lea rdx` set-up before it, and walks each tree (Qt rcc
format 1–3, zlib + zstd): **717 files** (715 distinct paths). Output:
`resources/rcc_<tree file offset>/…`, index in `resources/manifest.json`, listed in
registration order (initializer-table order). That order matters: QResource returns the
first registered tree's file for a path, and `/i18n/translations/reMarkable_en.qm`
exists twice: `rcc_00d23250` (102 KB, the tablet UI's catalogue, registered first, so
the one the app loads) and `rcc_00e9e350` (29 KB, the desktop catalogue). For English
the two give the same text: every desktop-only string's English equals its source.
(An earlier heuristic carver missed the two small trees `rcc_00d23250`/`rcc_00d29230`
and so packed the 29 KB file; all its other 713 files are byte-identical to this one's.)

`install.py` gets `reMarkable.exe` out of the official installer without running it:
the installer is a Qt Installer Framework binary with its packages appended as 7z
archives, which `client/tools/ifw_extract.py` finds by their CRC-checked signature
header and decodes with the standard library's lzma (checked: all 1,453 files of the 12
archives match `7z x` byte for byte). The app icon is rebuilt from the exe's own
`RT_GROUP_ICON`/`RT_ICON` resources (`client/tools/pe.py`), byte-identical to the
installer's `remarkable.ico`.

| Tree | Contents |
|---|---|
| `rcc_00fddf90` | `qml/main.qml`, `qml/client/desktop/**` — desktop shell: `Navigator`, `DocumentView`, `PageOverview`, `ScreenShare.qml`, `Onboarding`, menus |
| `rcc_00fbcd90` | `qml/client/{dialogs,settings,navigator,controls,elements}` — `LoginWizard`, `TokenEntryDialog`, `AccountDialog`, `ScreenShareAcceptDialog`, `ExportDialog`, `RecoverySettings` |
| `rcc_00fd8610` | `qml/common/**` — selection handles, text formatting, thumbnails, `Strings`, `Values` |
| `rcc_00cf71f0`, `rcc_00c79a70` | `ark-imports/ark/{controls,tokens}` — "Ark" design system (colors, spacing, typography tokens) |
| `rcc_00cd0950` | `ark/icons` — 276 icons |
| `rcc_00d63c80` | 66 page templates (`*.template`, JSON: `templateVersion`, `formatVersion`, `constants`, expression-based geometry) |
| others | images, fonts, empty-state art, i18n `.qm`, `xofm/libs/localization` |

QML import graph: `QtQuick`, `com.remarkable` (C++-registered types, 123 imports),
`common`, `client.*`, `ark.tokens`, `ark.controls`, `xofm.libs.library`,
`xofm.libs.sceneview`. The `com.remarkable` types (e.g. `ScreenSharePainter`,
`ScreenShareClient`, library/sync controllers) are C++ — the QML is a view layer over
them. The Linux client runs this QML as shipped (§2.1).

### 2.1 Hosting the app's QML on Linux (client/src/ui/quick/)

**What runs.** `run.py` loads `qrc:/qml/main.qml` from the carved trees, repacked
byte for byte into one standard `.rcc` (`assets.pack_rcc`), with no file replaced
(`app.OVERRIDES` is empty): the window, menu bar, sidebar, grid/list views,
breadcrumbs, dialogs, settings, document view, page overview and Screen Share window
are the app's own QML. It runs on PySide6 **6.10.3, the Qt the app ships**
(`client/.venv`; the same QML engine and Controls style), set up as the app's
`main()` does (0x1400373d0): `QQuickStyle::setStyle("Basic")`, high-DPI rounding
`PassThrough`, `quitOnLastWindowClosed`, light colour scheme, the reMarkable and
EBGaramond fonts, `reMarkable_<lang>.qm` translations, image providers `dragicon`,
context properties `dpiScale` (0.48), `debugBuild` (false), `itemInspector`,
`appVersion` 3.29.0, `appBuild` 1804, `qtVersion`.

**C++ types.** `client/tools/moc_api.py` reads every QMetaObject in the exe (Qt 6 moc
layout: stringdata offset/length table + `QMetaObjectPrivate` revision 13) into
`bridge/qml_api.json`: properties (type, notify), signals, slots/invokables (argument
and return types), enum values, and the QML module/name each type is registered
under (e.g. `TriangleItem` → `com.remarkable.Triangle`, `LinkProviderWrapper` →
`LinkProvider`, `scene::ParagraphStyle` → `ParagraphStyle`). 50 types are bridged:

- *Hand-written* (`bridge/library.py`, `platform.py`, `importer.py`, `exporter.py`,
  `screenshare.py`): the library facade, `LibraryView`/`LibraryNavigator`/`LibrarySearch`,
  entry wrappers, `LibraryController` (→ Module 6 writes), `DocumentImporter`,
  `DocumentExporter` (`PlatformHelpers.exportFile` → Module 5 export), `Settings`
  (persisted to `~/.config/remarkable-linux/settings.ini`), `ScreenShareClient`
  (→ Module 7), `Authentication` (→ Module 1, below), ...

**Logging in.** Without a device token in `auth.json`, `Authentication.loggedIn` is false and
main.qml's own `LoginWizard` covers the window: "Log in" opens
`"https://" + NetworkClient.loginUrl + "#desktop"` (the app's `loginUrl` is the bare host
`my.remarkable.com`; this client returns `my.remarkable.com/device/desktop/connect`, the page
that shows the one-time code), `TokenEntryDialog` checks the code is 8 letters and calls
`legacyLogin(code)`. `QuickApp` pairs in the background (`AuthClient.login`: device/new, then
the first user token; a failure after pairing forgets the device token again) and reports
`loginResult`/`loginErrorMessage` before emitting `loggedInChanged`, which the wizard reacts to
(also on failure): HTTP 4xx from device/new → `InvalidOneTimeCode` (its help page), TLS
errors → `SslCertificateTimeError`/`SslHandshakeError`, no connection → `NetworkError`. Once
logged in, the wizard fades out and the sync fills the same window. Sync and Screen Share
start only when logged in. `PlatformApp.logout()` (LogoutConfirmDialog) deletes the device on
the server, forgets the tokens and the cached library; a device token the cloud rejects
(DeviceRevokedError during a sync) shows the wizard again. The demo library is only `--demo`.

**`Settings` is also a `com.remarkable` singleton.** Besides the `Settings` context property,
the app registers the same class as a singleton named `Settings` in `com.remarkable`
(`qmlregister(SingletonRegistration)` at 0x1400b35cd; the `ark.tokens` one is at
0x1405d9a4e). QML resolves capitalised names to imported types before context properties,
and enum values only through a type, never through an instance (checked on Qt 6.10.3 with a
C++ `Item`, too). Without the singleton, `Settings.Prod` and `Settings.HomeSortOrder` are
undefined: the code page showed "Service environment: Prod" and the sort order was not saved.
- *Reconstructed from their paint code* (`bridge/items.py`, `images.py`):
  `DocumentCover` (0x140602e00: notebook spine `decorationScale·12`, quad-curve right
  corners of radius `decorationScale·18`, PDF dog-ear `decorationScale·40`),
  `Triangle` (0x140601810), `FontExtender` (toggles `QFont::NoAntialias`), the
  `dragicon` provider (0x1405fa6f0).
- *Generated* (`bridge/generated.py`, `scene.py`): the scene stack the document view
  uses — `SceneView`, `SceneController` (113 slots), `SceneTileManager`,
  `PagePickerModel`, `SceneLayersModel`, `DocumentSearchController`, `Recognizer`, ...
  — get every member from the spec; behaviour classes implement what this client does
  (SceneView renders the page with `ui/pages.py` through the tile manager's transform;
  the tile manager maps view ↔ scene, zooms and clamps scrolling to the paper;
  `linkAt` returns the PDF's links; `DocumentSearchController` and
  `PageSearchResultOverlayItem` search and highlight, §5.7). `Recognizer` and
  `RecognitionJob` convert handwriting through the cloud (§5.12).

**Behaviour recovered from the binary**, where the QML does not define it:
`LibraryView::Filter` flags (`NativeDocuments=1, Collections=2, PDFs=4, Ebooks=8,
Pinned=16, Trashed=32, …, Documents=13`) and the sort orders; `ignoreParent` lists the
whole library, otherwise a folder's children; entry `subtext` = `Document::progressText`
("Page %1 of %2", ebooks "%1% read" = `roundf(page/(pages−1)·100)`), folders the
relative last-modified time (`StringUtils`, shared.cpp 0x140bc4e20: "just now" < 60 s,
then minutes/hours/days/30-day months/365-day years, `round`); `whatsNewJson` =
`:/misc/whatsnew_desktop.json`; language names `QLocale::nativeLanguageName` with the
first letter capitalised; 33 handwriting-conversion languages; `UserInfo.featureToggles
.clientEdit` (the editing interface: on while every document shown is one the library
lets this client edit, and in the library while editing is turned on at all;
`QuickApp._update_editing`).

**Documents without a page list.** DocumentViewport opens pages by id
(`goToPageId(document.idForPage(n))`), and SceneView loads nothing without one. A PDF or
EPUB uploaded from a computer has no page list until a tablet opens it (§3.3: our imports
too). The client therefore numbers such pages `pdf-page-<n>` (`ui.library.unlisted_page_id`,
never written to the cloud), and the wrapper and the viewer share one page list. The count
comes from `.content`'s `pageCount`, which our imports fill in with pypdf. Other uploaders
can leave it at 0: then SceneView opens the first page and gives the document the PDF's
page count (`learn_pdf_page_count`). A document with nothing to draw (an imported EPUB
before the tablet adds its PDF rendition) ends in `Page.LoadError` with the reason as the
diagnostic line, never in endless loading dots. Checked on the real library: 3 PDFs have
no page list (`pageCount` 11, 0 and 0). All 3 open, and the two counted by the viewer
have 2 and 413 pages.

**Export.** `ExportDialog` calls `PlatformHelpers.exportFile(url, id, format[, …, pages])`
with the file its save dialog chose (one PDF) or a folder (several documents, or
"Export as rM file"), which gets `<name>.pdf`/`<name>.rmdoc` per document, never over an
existing file. `DocumentExporter` runs the exports one after another on `workers.Exporter`
(`ui/export.py`). While an export runs, its entry reads `Entry.Exporting` with
`exportProgress`, as in the app: the grid shows the progress bar and an × that calls
`abortExport()`. It then turns `Ok`, which shows the app's checkmark, and the notification
shows the path. A failed or cancelled export passes through `Unknown`, so no checkmark
appears, and a failure shows the reason in the notification. PNG export writes a folder
named after the document, inside the chosen folder ("Place folder here"), with one
`<name> - <n>.png` per page at 226 DPI, rendered as the viewer renders it. SVG export writes
`<name> - <n>.svg` the same way; the binary's `SvgExporter` writes `%1/%2.svg` (a file per
item in a folder), but its exact naming is not recovered, so ours matches the PNG export.
ExportDialog offers PNG and SVG for one document at a time.

**SVG pages** (`ui.export.page_svg`) use the tablet's page pixels as user units, so a
notebook page is `0 0 1404 1872`. PDF pages are their PDF size at 226/72 px per pt. Both
grow by any ink beyond them, with the PDF and PNG exports' bounds. Bottom to top:
- the paper;
- the template, as vector paths (`templates.template_svg`). It walks the same
  `template_shapes()` as `paint_template`, with QPen's square caps and bevel joins and
  QPainterPath's even-odd fill. All 65 templates paint byte-identically after that
  split.
- or, for a PDF page, the PDF page as an embedded PNG at 226 DPI. QtPdf only rasterises,
  and no PDF-to-SVG converter is a dependency, so this is the one raster part.
- the ink, as `renderer.svg_scene` (shared with `render_svg`): a group per layer, then
  per stroke. Each run of constant width and opacity is one polyline, at the widths the
  pen recorded. Highlighters use `mix-blend-mode: darken`.
- typed text as `<text>`, wrapped with the viewer's Qt font metrics.

Templates stay small: consecutive shapes of one style share a styled group. Opaque
outlines share one path; translucent ones stay separate, since crossings must compound.
Shapes outside the page are dropped. That took "Dots small" from 293 KB to about 95 KB.

Checked with librsvg (`rsvg-convert` 2.62, which honours mix-blend-mode, as browsers do)
against the viewer:
- every demo page: at most 0.13 % of pixels differ by more than 96/255;
- 32 pages of the user's library (23 notebook, 9 annotated PDF): at most 0.04 %.

Notebook pages came to a median 98 KB as SVG, against 182 KB as PNG. Annotated PDF pages
came to 413 KB against 307 KB, because the embedded page image is base64-encoded. Qt's
own SVG renderer ignores mix-blend-mode and lays text out differently, so the in-suite
parity test uses it only on pages without highlighters or text.

**Turning pages.** DocumentViewport turns a page by setting `currentPage` first, which
re-fits `viewportScale` onto the outgoing SceneView, and then creating a new SceneView.
It applies the zoom to the new view only when `viewportScale` changes again. That never
happens between two notebook pages (the same paper), so page 2 of every notebook opened at
scale 1, 100 % tablet pixels. A page's new SceneView therefore starts at the transform its
viewport last showed (`scene._viewport_transforms`), which already holds the re-fit.

**Reading position.** `DocumentViewport.open` calls `updateLastOpened()`, and `close()`
calls `LibraryController.setLastOpenedPage(id, currentPage)`. The Tags view calls it too,
just before opening a tagged page. The Library keeps the position locally, so the next
open and "Page %1 of %2" use it at once. 1.5 s after the document is closed it saves
`lastOpenedPage`/`lastOpened` (`SyncClient.set_reading_position`), without touching
`lastModified`. `lastOpened` never moves back: the save is worked out inside the commit,
on the cloud's metadata as the commit's refresh finds it, and keeps the later of that
and this opening (`core.sync.opened_lww`, last writer wins). A live test had a save
written after another device had opened the document put its `lastOpened` 37 s back. The user's library shows reading does not change it: 105 documents were
opened after their last change, with `lastModified` kept. Saving is quiet: offline or
logged out, the position stays in the session, and a failed save is logged, not shown.
A sync no longer re-announces items whose index hash did not change: every refresh
rebuilds every node, and announcing them made an open document reload its page.

**Image providers.** `arkicon` (the Ark SVG icons) and `rmthumb` (page thumbnails) are
rendered in Python, but Qt Quick calls an asynchronous provider and its responses on its
pixmap reader thread while holding the reader's lock. A Python `requestImageResponse` or
`textureFactory` waits for the GIL there, while the GUI thread, holding the GIL in any
Python code that changes a QML property, can wait for that lock in `QQuickPixmap::load`:
the client (and its tests, about one run in eight) froze that way. The provider and
response Qt sees are therefore C++ (`native/arkcontrols/imageprovider.cpp`): the request
is queued to a Python object on the GUI thread, rendered on Python worker threads, and
handed back through `rm_deliver_image` (ctypes, which releases the GIL); `finished()` is
queued to the response's own thread, so it is never emitted before Qt has connected it.

**Pitfalls found on the way.** Every QML type must be registered before the first
`QQmlEngine` exists (registering later breaks `QObject*` assignment in that engine);
a class must be registered before another declares a property of its type, or the
property is typed `QObject*` and group syntax (`keyHandler.flavor:`) fails; no
Python may run on Qt Quick's image reader thread (above); `QTest.qWait` holds the
GIL (wait with a `QEventLoop`); offscreen screenshots need
`QT_QUICK_BACKEND=rhi QSG_RHI_BACKEND=opengl QSG_RENDER_LOOP=basic` (the software
backend cannot draw `MultiEffect`, used by every grid thumbnail). In one test process,
free the Python wrappers of QML objects (`gc.collect()`) before deleting a QML engine,
and delete closed windows. Otherwise destroying an unrelated widget later, or PySide's
clean-up of leftover windows at exit, crashed with a double free in
`Shiboken::Object::destroy` after all tests passed (most likely a wrapper outliving its
QML object leaves a stale entry in shiboken's binding map; not proven, but with both
measures 10 of 10 full runs exit cleanly, against about 1 in 4 crashing before).

**Device pixel ratio.** Nothing in SceneView reads the window's pixel ratio (the only
`QWindow::devicePixelRatio` caller is an image-grab helper, 0x140affc20), so the tile
manager's view coordinates are item pixels, as here; the QML's own `* devicePixelRatio`
(pinch/wheel anchors, "fit width") therefore behaves on a ratio-2 screen as in the app
at 200 %. This client renders the page image at device resolution, so it stays sharp.

**Remaining notices.** Qt warns once per `StandardKey` with several bindings
("Only binding to one of multiple key bindings", same check in the shipped Qt);
these are the platform's key tables, not binding errors.

### 2.2 What stays a no-op in the bridges

Every member of the app's C++ types is bridged (§2.1), but not every one does something.
`client/tools/bridge_stub_audit.py` lists the ones that do nothing: generated slots no
behaviour class replaced (they log at DEBUG and return their type's default; `setX`
slots of a property `X` set it and are not counted), and hand-written `@Slot`s whose body
is only `pass`, a constant, a log line or `_unsupported`. `--check` fails on one that is
not in its `DOCUMENTED` table, and `test_v1_finish_offline` runs the same check, so the
list below is the whole of it (88 members on 2026-11-22).

**The skip list: dead, OS-only or dangerous features of the official app**, left out on
purpose:
- **Sparkle updater** (OS-only): `PlatformApp.checkForUpdate`, `canUpdateThroughSparkle`,
  `sparkleEnabled` (false). Linux packages update themselves.
- **USB recovery** (dangerous): `DeviceRecovery` (`xofm.libs.recovery`, reflashing a
  tablet over USB) is bridged as a whole that never finds a device (discovery answers
  FAIL, so RecoveryPopup shows its connection error).
- **Retail demo reset** (dangerous): `Settings.retailDemoReset`.
- **Native share sheet** (OS-only, disabled): `ShareNative` reports "not available", and
  `ShareNative.exportFinished` does nothing.
- **`deleteAccount`** (macOS only): not in the Windows exe's metadata at all, so there is
  nothing to bridge.
- **Telemetry**: the app's usage reports (`/sync/reports/v1`) are never sent; no member
  stands for them.

**Dead in the desktop QML, or OS-only, for other reasons** (each with its reason in
`DOCUMENTED`):
- *C++-internal slots* connected only to C++ signals, whose work the bridge does in
  Python anyway: `DocumentLockManager.setSleepState/pageModified/linesStored`,
  `SceneController.onJobCompleted/onPagemapChanged/updateHasItemsToEdit/onAnnotationChanged/
  enableAutoReflow/disableAutoReflow/dumpScene`, `SceneKeyHandler.process`, nine
  `SceneTileManager` and nine `SceneView` update/on* slots, `DocumentSync.requestProtocol`
  and its three `on*` slots, `PlatformApp.onSyncProtocolChanged/updateWorkerSettings/
  onLibraryReady`, `PlatformHelpers.onCompleted`, `Authentication.fetchUserToken/abortLogin`
  (the sync fetches tokens itself; a pairing request runs to its end),
  `DocumentController.execute/onFinished/onFailed`.
- *Input methods, on-screen keyboards and mobile hooks* (OS-only):
  `SceneController.setPreeditText`, the seven compose-range / input-method-transaction slots,
  `SceneView.showVirtualKeyboard/hideVirtualKeyboard/inputMethodQuery` (Qt asks the item's
  own `inputMethodQuery`, not this slot), `PlatformHelpers.isCurrentKeyboard/
  vibrationFeedback`, `DocumentWrapper.setKeyboardUsedDuringSession`,
  `SceneController.markKeyboardUsedDuringSession`.
- *The tablet's scene API* the desktop QML never calls, done here by the client's own pen,
  selection and text code: `SceneController.addDrawingLine/highlightWithLine/
  selectWithLine/eraseWithLine/clearLines/pasteText/insertImageAsSceneItem/
  createImageSnippetFromStroke/deleteText/replaceText`, and `ParagraphStyle`'s eleven
  predicates (`isNone` … `fontSize`; the text code has its own).
- *Answered by design*: `SceneController.requestDeferredCompleted` (thumbnails of edited
  pages already refresh, §5.13), `Settings.defaultValue`,
  `HandwritingLanguageModel.defaultCode` ("en_US"), `EntryWrapper.baseFilePath/getSource`
  (there is no local document folder).

The stubs with real behaviour in 3.29 that used to do nothing here are now wired (§5.14):
`SelectionImage.image`, `Library.countArchivedDocuments/purgeThumbnails`,
`LibraryController.setScrollPosition/setZoomMode/setCustomZoomFit`,
`DocumentWrapper.scrollPosition`, `DocumentController.setViewBackgroundFilter`,
`PlatformHelpers.openFileChooser/setInhibitSleep`, `PlatformApp.shouldShowWhatsNew`.

## 3. Cloud API

### 3.1 Hosts [O]

| Host | Use |
|---|---|
| `{tectonic}.tectonic.remarkable.com` | Per-region API gateway. Live user tokens carry a plain `tectonic` claim (e.g. `"eu"`); the binary also references the namespaced `https://auth.remarkable.com/tectonic` (`usertoken.cpp`) |
| `internal.cloud.remarkable.com` | Fallback gateway: log *"No tectonic, falling back to internal domain"* (`http/src/manager.cpp`). Also `qa.`/`dev.`/`stage.` variants and `localhost:5000` (test mock) |
| `my.remarkable.com` (+ `qa.`, `development.`, `staging.`) | Web account / pairing code page (`/device/desktop`) — selected in `rm.network.client` |
| `get-updates.cloud.remarkable.engineering` | WinSparkle appcast |
| `errors.cloud.remarkable.com` | Error reporting |

### 3.2 Auth / device pairing (`xofm/libs/network/src/authentication.cpp`)

| Call | Detail |
|---|---|
| `POST /token/json/2/device/new` | [O] JSON keys referenced in the same function: `deviceID`, `deviceDesc`, `code` (one-time code), and `secret` (role [?]). Error string *"invalid one-time-code"*. Response = device token. |
| `POST /token/json/2/user/new` | [O] `Authorization: Bearer {device token}` → user token (JWT). Retries with backoff (*"Retrying fetching user token in {}, retry count: {}"*). |
| `POST /token/json/3/device/delete` | [O] logout / unpair |

Host resolution for these calls [I]: both go through an async "service hostname"
lookup (`http/src/servicehostname.cpp`: *"Could not find hostname for service {}"*,
`onRequestDeviceTokenHostNameReceived`). The UTF-16 literal `webapp` sits beside the
device-token code, so the service name is most likely `webapp`. `/discovery/v1/endpoints`
is referenced by `syncinterface/src/checker.cpp` (internet check) and is the likely
service→host map.

**Confirmed live (2026-11-01, one unauthenticated GET):**
`GET https://internal.cloud.remarkable.com/discovery/v1/endpoints` →
```json
{ "notifications": "eu.tectonic.remarkable.com",
  "webapp": "webapp-prod.cloud.remarkable.engineering",
  "mqttbroker": "vernemq-prod.cloud.remarkable.engineering" }
```
So token calls go to `https://webapp-prod.cloud.remarkable.engineering/token/json/2/...`.
The Linux client resolves via discovery and keeps that host as a fallback.

Token lifecycle [O]:
- JWT expiry is parsed and a refresh scheduled ahead of expiry (*"Scheduled token update at {}, {} from now"*).
- Response header `rm-token-ttl-hint` forces an early refresh (*"Got token TTL hint of {}, requesting new token"*).
- Response header `rm-migrate-auth-v2` → *"Detected migration to auth v2"*.
- Notification-socket close reasons: `Unauthorized`, `invalid token`, `user or device is blocklisted`.
- User-Agent format `%1/%2 (%3 %4)` (app/version (os osver)).

### 3.3 Sync (`xofm/libs/sync`, 56 source files) [O]

- `GET /sync/v4/root` (current) and `/sync/v3/root` — root hash + `generation`; `sync/root-not-found`.
- `/sync/v3/files/{hash}` — content-addressed blobs (`hashOfHashesV3`, `hashOfSchemaV4`, `schemaVersion`, `docs`).
  **Confirmed live:** every GET needs `rm-filename: <logical name>` — `root.docSchema` for
  the root index, `<id>.docSchema` for a document index, the index entry name for files.
  Without it the server returns `400 {"message":"unexpected 'rm-filename' http header"}`.
  Live account: schema 3, 5-field index lines, v3 hash-of-hashes verified on every entry of a real library.
- `/sync/v3/` + `{}missing`, `{}check-files` — batch missing-hash checks.
- `/gentree/v1/` + `GetEntries`, `GetFiles`, `GetFile`, `PutFile`, `DeleteEntry`, `EntrySession` — newer generation-tree API.
  Not implemented. 3.29 holds its sync state as a variant of `sync1_5` and `gentree`
  (accessors 0x14020e720 / 0x14020e7a0); what selects gentree for an account was not
  found (`setProtocolFlag` 0x140137500 is the Limited-sync flag, not the protocol). The
  client refuses to sync rather than guess: `core.sync.protocol_refusal` stops a sync
  whose root reply or discovery reply names gentree, or whose root `schemaVersion` is
  not 3 or 4 (`UnsupportedProtocolError`, said in the app, nothing read or written).
- `/sync/reports/v1` — sync reports/telemetry.
- Headers: `rm-filename`, `rm-batch-number`, `rm-sync-id`, `rm-parent-hash`, `rm-path`,
  `rm-expect-version`, `rm-sha256-mismatch`, `rm-recovered-{missing,empty}-hash`;
  GCS signed-upload headers `x-goog-hash`, `x-goog-if-generation-match`,
  `x-goog-content-length-range`; `if-match`; rate limiting `x-ratelimit-*`, `retry-after`.
- Options: `disableHTTP2`, `maxNetworkJobs`.
- Root reply keys [O]: `hash`, `generation`, `schemaVersion`, `hasMissingHashes`, plus server
  hints `maxNetworkJobs`, `disableHTTP2`, `deletedImmutableAction`. Index hashing schemes:
  `hashOfHashesV3`, `hashOfSchemaV4` (schemas 1–2 marked obsolete).
- **Not present** [O]: the legacy `/document-storage/json/2/*` zip API (0 occurrences).
  Zip is used only locally, for the `.rmdoc` import/export bundle.

**Writing** [O from the binary's strings and source paths; implemented in Module 6.
**Verified live on 2026-11-02**:
- A folder was created and a 1-page PDF imported into it.
- Every request returned 200. Each `PUT /sync/v3/root` reply echoed the schema-3
  hash-of-hashes we computed.
- An independent client read both items back with every existing entry unchanged.
- The user opened the PDF from the cloud. That client rewrote only `.metadata`: it set
  `lastOpened` and added the legacy keys `deleted`, `metadatamodified`, `modified`,
  `synced` and `version`. Our `.content` was left as is, without a page list.
- Deleting the folder removed both entries. The root hash returned to its value from
  before the test.]

Commit sequence:
1. `PUT /sync/v3/files/{hash}` for each new blob, with `rm-filename` and
   `x-goog-hash: crc32c=<base64 big-endian>`. The server validates the checksum:
   `"message":"invalid checksum"` sits in `upload-blob-job.cpp`'s strings.
2. Upload each changed `<id>.docSchema`.
3. Upload `root.docSchema`.
4. `PUT /sync/v3/root` with `{"generation": <base>, "hash": <new>}`. The reply carries
   the new generation ("uploaded new root hash {}, got generation {}").

Endpoints and body:
- The commit job, `upload-roothash-job.cpp`, uses `/sync/v3/root`.
- `/sync/v4/root` appears only in `download-roothash-job.cpp`.
- "broadcast" occurs only in the Screen Share broker, so the body has no such key.

Conflicts:
- A stale generation is reported as "failing precondition, likely server updated since
  last sync" (error code `RootHashGenerationMismatch`), i.e. HTTP 412.
- The client treats 409 the same way. It fetches the root again, re-applies the change
  and retries up to 3 times.
- Per-file merging of `.rm`/`.content`/`.metadata` against newer server versions: §3.5.

What the operations write:
- **Delete:** removes the entry from the root index.
- **Restore:** sets `parent` to "" ("%1 items were restored to My files").
- **Imports:** upload `.pdf`/`.epub`, `.content` and `.metadata`. The real library shows
  that the tablet adds `.pagedata` (and the EPUB's PDF rendition) itself.
- **Folders:** get `{"tags": []}` as `.content`, as real folders have.
- **Never-opened documents:** have `lastOpened` "0".

Safety:
- The new root index is built from the raw root entries, never from parsed items. An
  item this client fails to load is kept.

### 3.4 Other endpoints [O]

- `wss://{gateway}/notifications/ws/json/1` — Qt `QWebSocket`; events `updateNotificationReceived`,
  `integrationsUpdated`, `searchIndexUpdated`, `roomNotification`, `resetPasscode*`. Ping/pong keepalive.
  (Those are the `Notifications` object's Qt signals. The wire events behind them are
  `SyncComplete`, `IntegrationsUpdated`, `SearchIndexUpdated`, `PasscodeReset*` and the
  Screen Share events of §4; live incoming sync is §3.6.)
- `POST /convert/v1/handwriting` — `application/vnd.myscript.jiix`.
- Screen-share broker: see §4.

### 3.5 Sync merge: three-way file resolution and conflict resolvers

This section covers what happens when local edits meet newer `.rm`, `.content` or
`.metadata` versions on the server. The 412/409 retry in §3.3 only protects the root
hash. This is the per-file merge under it.

Traced in reMarkable.exe 3.29 [O unless marked]. Sources:
- `xofm/libs/sync/core/src/resolve-tree-helper.cpp`
- `state-action/check-downloads.cpp`
- `xochitl/src/crdtconflictresolver.cpp`
- `entryfile/src/entrytag_merge.cpp`
- `xofm/libs/syncinterface`

The Ghidra decompilation was done on demand (headless, `-noanalysis`). Function bounds
come from `.pdata`. Strings were tied to code by a rip-relative displacement scan.

**Per-file state.** Each file of an entry is a node in the sync tree. A node holds four
optional SHA-256 hashes plus marker bits:
- `final` at +0x00;
- `server` at +0x40;
- `local` at +0x80;
- `previous` at +0xC0, the hash at the last completed sync.

The order comes from the log line `final={}, server={}, local={}, previous={}`. "Absent"
is a flag at +0x20 of each hash (0x1401eb670).

**Resolution per file** (`compare`, 0x1402b9dc0). The rules below are tried in order and
the first one that applies wins. Values: 0 all gone, 1 Local (upload), 2 Server
(download), 3 Either, 4 RemoveLocal, 5 RemoveServer, 6 Conflict.

| # | Function | Applies when | Result |
|---|---|---|---|
| 1 | 0x1402b7d30 | `.metadata`, `.content` or `.rm`; local and server both present and exactly one is the empty-file hash `e3b0c442…b855` | The non-empty side wins: Local if the server copy is empty, Server if the local copy is empty. Marker 0x400 is set |
| 2 | 0x1402b7ce0 | Server present and marker 0x2000 | Server |
| 3 | 0x1402b7f10 | Server, local and previous all present, no marker 0x1000 | server = local → Either; server = previous → **Local**; local = previous → **Server**; otherwise **Conflict** |
| 4 | 0x1402b8000 | Server and local present (no previous, or marker 0x1000) | Equal → Either, else Conflict |
| 5 | 0x1402b8070 | Local absent; server and previous present | server = previous → RemoveServer (deleted here). Otherwise Server: the remote edit beats the local delete |
| 6 | 0x1402b8120 | Server absent; local and previous present | local = previous → RemoveLocal (deleted on the server). Otherwise Local: the local edit beats the remote delete |
| 7 | — | Only server → Server; only local → Local; neither → all gone | |

**Applying the resolution** (0x1402b9100):
- Local: `final` = local, marker 1 (upload).
- Server: `final` = server, marker 2 (download).
- Either: `final` = local.
- RemoveLocal: marker 4.
- RemoveServer: marker 8. For `.metadata` the tombstone is checked first ("Deleting {} without a tombstone {}").
- **Conflict**: `final` = server, markers 0x22 (download + conflict). The local file is
  not touched. The server copy is stored next to it under a conflict name. The suffix
  table holds `.remote`, and the open-document path accepts only `<pageId>.rm.remote`
  (below), so the name is `<file>.remote` [I].

**Page resources.** These are files whose id has two `/`, i.e. `<doc>/<page>/<file>`.
They then pass through `adjustResourceResolution` (0x1402b7930):
- Conflict → Server ("marked as conflict, indicating a corrupt file, re-download from server").
- Local while a server copy exists → Server.
- Server while a local copy exists → Local.
- A resource under no page of the document → RemoveLocal.

**Entry-level rules.** These are log strings in the same file:
- An entry that was deleted here without a tombstone marker is un-deleted (rule 0x1402b7780 turns RemoveServer into Server).
- "local version {} != server version {}. Force upload": an optional int per entry node. When the two differ, the entry is uploaded.
- A local remove where not all files are gone uploads instead.
- A document left with only its `.metadata` because of a conflict is removed.

**Conflicts become a `Choice`** (`onEntryUpdate`, 0x140135fe0). This runs for each
conflicting path of an entry that is not a `Document`, or whose document lock can be
taken (try-lock 0x1406621e0). Open documents handle their own conflicts (see below).
1. A `SyncConflict` is built: `{choice, entry id, ours = local path, theirs = conflict path}`.
   **`choice` starts as `Theirs`** (`mov [rbp+0x10], 2` at 0x1401362b6).
2. The kind comes from the path suffix (0x140132750): `.rm` Rm, `.json` JSON,
   `.content` Content, `.metadata` Metadata, `.pagedata` Pagedata, otherwise Undefined.
3. The resolver registered for that kind is called (map built at 0x1400a0be0). Enum names
   come from the magic_enum tables at 0x141001110 and 0x141001160:
   `Choice {None, Ours, Theirs, MergedOurs}`,
   `Kind {Undefined, JSON, Rm, Content, Metadata, Pagedata}`.
   Resolvers exist only for Rm, Content and Metadata. Pagedata and JSON keep `Theirs`.
4. `processSyncConflict` (0x1401390d0, "Resolved choice={} for {} in {}") applies the
   choice. If the theirs file is missing it aborts.
   - None: logged ("Got a resolved conflict without a resolution"); both files stay.
   - Ours: the theirs file is deleted.
   - Theirs: ours is deleted, theirs is moved onto it, and its mtime is set to now.
   - MergedOurs: the theirs file is deleted (the merge is already in ours).

   Ours and MergedOurs leave local ≠ server, so the next pass uploads the file
   (rule 3: Local).

**Resolvers:**

- **`.metadata`** (0x140131b90). It acts only when the incoming choice is `Ours`. With
  the default `Theirs` it logs "Keep choice {} for {}" and returns `Theirs`. So
  **in 3.29 a `.metadata` conflict always ends with the server's copy.**
  The `Ours` branch (not reachable from the caller above):
  1. Both files are read (≤ 10 MiB) and parsed. On a parse error the incoming choice is kept.
  2. If either `lastModified` lies outside [1.5e12 ms (2017-07-14), now + 1 day], the incoming choice is kept ("Fallback since modfied is not valid").
  3. Ours is kept if ours ≥ now − 50 days, or if theirs − ours < 1 day.
  4. Otherwise the result is `Theirs` ("Changing resolution from {} to {}, our modified: {} their modified: {}").
- **`.content`** (0x1401007e0 → 0x140101db0, "Resolving content conflict for {}"):
  1. Both files must parse as JSON objects; otherwise the incoming choice is kept.
  2. Each file is converted to formatVersion 2 if needed (0x140100cf0, "Converted our/their .content to v2"). If one cannot be converted: "Only version 2 is supported for crdt resolution", and the incoming choice is kept.
  3. `matchContentsAgainstFS` (0x140101a70) runs. A PDF/EPUB on either side skips it.
     Otherwise each page map is checked against the files in `<doc>/` (0x140101000:
     "{} references page {} which does not exist in {}"; the exact page set it requires
     is not fully decoded):
     - only ours matches → `Ours`;
     - only theirs matches → `Theirs`;
     - neither matches → `None` ("Neither pagemap matches the file system");
     - both match → merge.
  4. **Merge:**
     - Both `cPages` go through one `PageMap` (0x1406d04e0 twice). Registers merge per
       register: the newer timestamp wins (§5.4).
     - All other keys come from ours if the incoming choice is `Ours`, otherwise from
       theirs. With the default, that means the server copy.
     - `tags` and `pageTags` are merged when either side has them (0x14085f080 and
       0x14085f100, `entrytag_merge.cpp`, "found newer tag in other (add)").
     - The result is written over ours and the resolver returns **`MergedOurs`**. A
       failed write keeps the incoming choice.
- **`.rm`** (0x1400ff950):
  1. Both files must be v6 ("File versions {} and {}, fallback to {} for {}" otherwise; the incoming choice is kept).
  2. Each file is parsed (0x140a06fe0 → 0x140a02290) into a summary that holds a `CrdtId`
     (counter: low 48 bits, author: high 16).
  3. The two are compared (0x140a263e0). Ours ahead → `Ours`; theirs ahead → `Theirs`.
     On equal counters a check on the author index decides (0x140a26050, via
     0x140a977d0). An identical id, or an empty side, gives `None`.
  4. So a closed document does **not** merge strokes: one whole file wins. That the id is
     the scene's highest id (its clock) is an inference [I].
  5. With `None`, the `.rm.remote` file stays for the open-document path.

**Open documents** (`Document::processConflictingPathsAndUpdateState`, 0x140663c70):
- Only `<pageId>.rm.remote` paths are accepted ("missing conflict extension",
  "missing rm extension", "bad pageId").
- If the document is archived, the conflict file is deleted.
- Otherwise `DocumentWorker::doResolveConflict` (0x14055ec20) loads the `.remote` file into
  the page's live scene (0x1405a6bc0; "Error during load of {} {}, version: {}"). It then
  stores the page and deletes the conflict file ("Unable to remove conflict file {}").
- This is the CRDT merge: both files' items end up in one scene. The second PageStats
  block that is read counts as a merge (§5.5). 16 cached pages have `merges` > 0.
  That this loader is the same one as 0x140a02fb0 is inferred from that counter [I].

**This client** never creates conflict files. Pending edits are operations (core/edits.py).
`commit` (core/sync.py) refetches the root on every attempt and replays the operations onto
the server's current `.rm`/`.content`/`.metadata`. That yields what the app's open-document
CRDT merge would. For `.metadata` it keeps the server's other fields, as the app's default
`Theirs` does.

### 3.6 Live incoming sync: notifications to syncs

Traced in reMarkable.exe 3.29 [O unless marked]:
- `notifications/src/notifications.cpp`: `Notifications::onNotificationReceived`
- `xofm/libs/syncinterface`: `DocumentSyncInterface`, including `slumberinhibit.cpp`

Strings were tied to code by the rip-relative scan of §3.5. The handler and the
scheduler were read in `objdump` disassembly.

**Receiving** (`onNotificationReceived`, around 0x1402e94a0):
1. The handler logs `Notification originated from "{}"` with the `sourceDeviceID`.
2. If that id equals this device's, it logs "Notification originated from ourself!" and
   drops the event (0x1402e9545).
3. Otherwise it compares the `event` attribute with `"SyncComplete"`
   (`QString::compare`, case-sensitive, 0x1402e95db). On a match it emits signal index 5
   (0x1402ef380). By moc order that index is `updateNotificationReceived`.
4. Other events go to their own signals: `PasscodeReset*`, `IntegrationsUpdated`,
   `SearchIndexUpdated`, Screen Share's `roomNotification`. Anything else logs "Received
   an unknown notification from server".
5. The socket keeps a ping timer ("Restarting notification timer", " - socket failed to
   reply, reopening") and reconnects by itself ("Something changed, checking if we need to
   connect").
6. `DocumentSyncInterface` has the slots `onUpdateNotification` and
   `onConnectedNotification`. They are the receivers of `updateNotificationReceived` and
   of the socket's `connected` [I: wired at runtime, not traced to a `connect` call].

**Scheduling** (`DocumentSyncInterface`, 0x140136bb0):
- A sync request goes through one `QTimer` at +0x60.
- The interval is 100 ms when a full sync was requested (flag +0xb8, set by
  `requestSync`, "full sync requested", 0x140137f26) and 10 ms otherwise.
- If the timer already runs and its remaining time is within the interval, it is left
  running. Otherwise it is (re)started with the interval. So the first request of a
  burst sets the time, and later ones join it.
- `execute()` checks first that sync is not suspended ("Ignoring sync, suspended") and
  that the app is online ("Ignoring sync; we are not online").

**Suspend and resume** (0x140138066, 0x1401380d6):
- `suspend()` logs " - suspending sync", sets flag +0xc0, stops the timer and aborts
  the running sync through the sync engine's vtable (+0x80).
- `resume()` logs " - resuming sync" and only clears the flag. Nothing pending is
  replayed: the app catches up on its next periodic or notification-driven sync.
- `slumberinhibit.cpp` refuses to suspend while a sync runs or is about to ("Can't
  suspend, sync is running"). That is the tablet's sleep, not this client's concern.

**This client** (`ui/quick/bridge/library.py` `DocumentSync`, `liveview/session.py`,
`ui/quick/app.py`):
- **The socket.** It is the one the Screen Share service keeps open whenever the client
  is online (§4). `LiveViewService` drops events from its own device id. It passes
  `SyncComplete` on as `CloudUpdated`, and each (re)connection as
  `NotificationsState(True)`.
- **Hand-off.** `ScreenShareClient` emits `cloudUpdated` and `notificationsConnected`.
  QuickApp connects them to `DocumentSync.onUpdateNotification` and
  `onConnectedNotification`.
- **One timer.** As in the app it is single-shot and not restarted while it runs. The
  interval is ours: `REQUEST_DELAY_MS`, 1000 ms. A sync here starts with a root GET and
  shows "Syncing", so bursts are gathered for longer than the app's 100 ms.
- **During a running sync.** A request while a sync runs, whether the user's own, Check
  sync's, or one `request_sync()` finds busy, leaves exactly one follow-up. It starts
  when that sync ends, successful or failed: the running sync may have read the root
  before the change.
- **Reconnects.** Every (re)connection of the socket asks for a sync, because changes
  made while it was down were never announced. At start-up this adds one root GET,
  which costs nothing when the hash is unchanged.
- **Suspend and resume.** `suspend()` holds requests while logged out, cached-only, not
  started, shutting down, or while the network backend reports the link down.
  `resume()` syncs once if anything asked meanwhile. This differs from the app on
  purpose: this client has no periodic sync to catch up with. With no network backend
  at all, the link is treated as up.
- **Applying a sync.** A sync replaces the tree (`Library.set_tree`). Entry wrappers
  are updated in place, so the library view keeps its folder, scroll and selection.
  - `DocumentWrapper.update` emits `pageMapChanged`, `redirectionPageMapChanged` and
    `templatesChanged` only when the page list changed (ids, PDF pages, templates).
    `DocumentViewport` rebuilds its page views on `pageMapChanged`. Before this change,
    every newer version of an open document replaced its `SceneView`, losing an open
    text session, a selection, or a stroke being drawn.
  - The open `SceneView` follows its document's `nodeReplaced`. When the files its page
    is drawn from changed (its `.rm`, its `<doc>/<page>/…` images, the PDF), it reloads
    the page in place (`_load(quiet=True)`, no loading dots).
  - While a selection or a text session is open, the reload waits until it ends.
    Closing either one reloads the page anyway.
- **Pending edits.** They are never touched by a sync. They live in
  `core.edits.PendingEdits`, apart from the synced files, and `LibrarySource.staged`
  replays them over whatever version was synced last. So the reloaded page shows the
  other device's changes with this client's edits on top, as `commit` would upload
  them (§3.5).
  - The page's undo history starts again, because the page changed under it
    (`PageHistory.seen`).
  - Uploading is §3.7: automatic or by hand from the app, or `tools/push_edits.py`.

Tests: `client/tests/test_live_sync_offline.py` mocks the socket with
`liveview_fakes.FakeNotifications` over the fake cloud.

### 3.7 Outgoing sync: uploading the edits made here

Traced in reMarkable.exe 3.29 [O unless marked] (`xofm/libs/syncinterface`):
- **The trigger.** A changed document asks for an entry sync:
  `DocumentSyncInterface::requestEntrySync` (0x140137620) logs "entry sync requested,
  id={}; fullSync={}, entries={}" (string 0x141006580, referenced at 0x1401377f1),
  records the entries, sets flag +0xb8 when a full sync is asked for, and calls the
  scheduler of §3.6 (0x140136bb0, called at 0x14013785a). Local edits and remote
  notifications therefore share one timer and one running sync: the app never uploads
  and downloads at the same time.
- **Interval.** 10 ms for an entry sync (100 ms for a full one), and the timer is not
  restarted by later requests within it (§3.6). The app writes edits to files and syncs
  a document as a whole, so it can go almost at once.
- **Skipped.** `execute()` (0x140138530) drops the request while suspended ("Ignoring
  sync, suspended") or offline ("Ignoring sync; we are not online"). A request made
  while a sync runs is held ("Delay request while syncing", logged in 0x1401cafa0) [I: by the string only;
  that function was not traced to the scheduler].
- **Errors.** A document whose sync failed shows `Entry.SyncError` (QML
  `EntryErrorIcon`, `cloud_x`); clicking the icon calls
  `entry.handleSyncError(Entry.Retry)`. While a document uploads it is
  `Entry.Uploading`, with `syncProgress` (`EntryStatusIcon`, `EntryProgress`), and the
  icon shows a checkmark for 1 s when it turns `Ok` again.
- The desktop app has no setting for any of this: it always syncs. No string in the
  binary and no QML in its resources names an automatic-sync or sync-enabled switch
  (searched for autosync, auto sync, syncEnabled, disableSync, pauseSync).

**This client.** Uploads go through one module, `core/outbox.py`. It is used by the app
(`ui/outgoing.py`) and by `tools/push_edits.py --confirm`, so the two cannot drift
apart. `SyncClient.push_edits` delegates to it as well.
1. **Locks.** `push_document` takes `<cache>/pending/.push.lock` (fcntl), so only one
   upload runs at a time across processes. It then takes `SyncClient._busy`, the RLock
   that every incoming `refresh` and every other commit (`Mutator`) also hold. An
   incoming live sync therefore waits for an upload under way and then sees its
   result, and the reverse holds too. This plays the part of the app's single
   scheduler.
2. **Commit.** `SyncClient._commit` as before (§3.3): refresh, apply the snapshot of
   the document's pending edits to the cloud's current version, upload the blobs and
   indexes, then PUT the root with its generation. On 409 or 412 (another device
   synced) it refreshes and re-applies the same edits, up to `COMMIT_ATTEMPTS`. A remote
   edit that lands mid-upload is therefore merged, not overwritten.
3. **Settle.** In one locked change of `edits.json` (`PendingEdits.settle`):
   - The uploaded snapshot is forgotten.
   - Every other edit of the document is **rebased** (`core.edits.rebase_refs`).
     Edits made while the upload ran (the commit can take seconds) may name an item
     that an uploaded edit made, by that edit's uid: `("edit", uid)` for an image,
     stroke or layer, `("edit", uid, n)` for a typed character. Once the uploaded edit
     is gone, that name means nothing, and the later edit would silently do nothing (a
     delete of a just-uploaded stroke, typing after just-uploaded text).
     `uploaded_names` replays the uploaded snapshot over the version it was committed
     onto (`Staged` over the commit's base node). That gives the id each uid's item
     received in the uploaded file (`Replay.made` / `typed`), and the references are
     rewritten to `("page", author, counter)`.
   - A copied page keeps its source's ids, so one map serves the whole document.
4. **Failures.** Every pending edit is kept on any failure; nothing is discarded
   before the root PUT is accepted. `PushFailed.kind` is one of:
   - `offline`: `NetworkError`, raised by `_request` after its retries; an
     `AuthNetworkError`; or an `OSError`.
   - `conflict`: the cloud kept changing for every attempt.
   - `auth`: device revoked, or not paired.
   - `rejected`: the document is gone or in the trash, a page was removed, or similar.

**The app's uploader** (`ui/outgoing.py` `OutgoingSync`, wired in `QuickApp`):
- **Trigger.** It listens to `PendingEdits.listeners`, which fire after every change of
  the ledger from any thread, and are marshalled to the GUI thread by a queued signal.
- **Pause.** With automatic upload on, each change restarts a single-shot pause
  (`DEBOUNCE_MS`, 3000 ms), and the pause's end uploads every document with edits on
  the worker (`outbox.push_all`, one commit per document). This differs from the app's
  10 ms on purpose: here every upload is a whole root commit, so a page of strokes
  should go up as one commit, not one per stroke.
- **At once, with no pause:**
  - leaving a document (`Library.openDocumentsChanged`) while uploads are automatic;
  - **Upload now** (Settings > Editing), whether or not uploads are automatic;
  - Check sync, which uploads before its full sync, as the app's full sync sends
    local changes too;
  - the entry's `cloud_x` icon (`handleSyncError(Retry)`).
  A flush while an upload runs leaves one follow-up for when it ends.
- **Retries.** On `offline` or `conflict` the uploader retries on a backoff timer: 5 s,
  15 s, 30 s, 1 min, 2 min, then 5 min. Automatic or asked-for uploads keep at it until
  they go through. It also retries at once when the network comes back
  (`LinkProvider.availabilityChanged`) or on log-in.
- **No upload attempted** while logged out, `--offline`, or while the link is known to
  be down: edits wait (state `waiting`).
- **Refused documents.** On `rejected` the document shows `SyncError` and is not
  retried until its edits change or the user asks; the app's error notification says
  why. Documents made here and not yet in the cloud (`LibrarySource.created`) are
  skipped until their own creation commit lands.
- **Library entries.** A document with edits waiting is `Entry.Uploading`, with the
  upload's progress while it runs; a failed one is `Entry.SyncError`.
  `Library.set_upload_statuses` feeds `EntryWrapper.status` / `syncProgress`.
- **Undo across uploads.** The uploader is `LibrarySource.settle_upload`, which runs
  the ledger change under the undo-history lock. The page histories that were current
  are renamed with the same map, and their `seen` moves to the uploaded state, so an
  automatic upload does not empty undo. A sync from another device still empties it,
  as in the app.
- **Late edits.** The renaming map is kept by `LibrarySource._uploaded`. Edits named
  before an upload but handed over after it are renamed through it in `_add_edits` and
  `PageHistory.push`. Examples: the text session hands its typing over after a 1 s
  pause, and a selection names its items when it is made.
- **Shutdown.** `shutdown()` stops the timers and lets an upload under way finish.
  Pending edits stay on disk, and the next start uploads them.

**Settings** (`EditSettings`, kept in `settings.ini` as `LinuxClient/editingEnabled` and
`LinuxClient/autoSyncEdits`, both false by default):
- **Editing off** keeps every document read-only (`LibrarySource.edit_refusal` "Editing
  is not turned on"; `UserInfo.featureToggles.clientEdit` false). On sets
  `LibrarySource.edit_everything`: every notebook, PDF and ebook not in the trash.
- **`--edit-notebook NAME`** (or `REMARKABLE_EDIT_NOTEBOOK`, read by `run.py` when no
  option is given) takes precedence. Editing is then on for those names only (the
  unique-name rule of §5.6 still applies), whatever the setting says. It is only for
  someone who passes it by hand: no launcher passes it (`install.py`'s desktop entry,
  and the maintainer's own packaging, start the app with no option), so a normal start
  leaves editing to these two switches (`test_v1_finish_offline`
  `SettingsAndLauncherTests`).
- **The two switches are independent.** Uploading pending edits does not need editing
  to be on.
- **UI.** The switches live in **Settings > Editing**, an `ArkControls.Cell` that
  `install_settings_cell` creates at run time from a QML snippet and inserts into
  main.qml's `settingsDialog.model` (GeneralSettings' `ObjectModel`), after the Cloud
  cell. The shipped QML stays unmodified.
  - It is resolved in QML through the window, because PySide has no converter for the
    `GeneralSettings_QMLTYPE` property.
  - It holds an `ArkControls.Toggle` for each switch, and a status row ("N edits in M
    documents to upload", "Uploading...", "Not uploaded: ...") that is the **Upload
    now** button.
  - With `--edit-notebook` the Editing row reads "Only “NAME” (--edit-notebook)" and its
    toggle is hidden.

Tests: `client/tests/test_outgoing_sync_offline.py`, all over the fake cloud
(`FakeCloud.offline`, `before_commit`, `fail_next`). They cover:
- the shared path, the failure kinds, and conflict merge;
- edits made mid-upload, and late text;
- undo after an upload;
- incoming sync waiting for an upload, and the cross-process lock;
- the settings' defaults, precedence and persistence;
- pause coalescing, flushes, offline backoff and back-online;
- incoming and outgoing together with no edit lost;
- the QML cell and the entry states.

## 4. LiveView / Screen Share

Stack [O]: `screenshare/core` (`broker`, `roombroker`, `restbroker`, `reconnector`,
`rfb/clientprotocol`, `rfb/connection`) → `webrtc` (`WebRtc::Manager/Peer/Channel`) →
libdatachannel/libjuice. The desktop app is the **viewer**; the tablet is the source.

1. **Signalling via cloud rooms** (`roombroker.cpp`): `POST /rooms`, `/rooms/{id}/join`,
   `/rooms/join-active`, `/rooms/{id}/keepalive`, `DELETE /rooms/{id}`,
   `/rooms/{id}/messages/{broadcast,direct}`. Under `/screenshare/v1` (REST broker).
   Responses carry `ice_servers`. Room events (`room-created`, `room-joined`,
   `room-not-found`, `join-auth-room`) also arrive over the notifications WebSocket
   as `roomNotification` with `sourceDeviceID`.
2. **WebRTC**: messages `request-offer` / offer / ICE candidates with `roomId`,
   `clientId`, `targetClientId`, `senderClientId`, `iceServers`. A
   **data channel only** is used for the stream (audio/video SDP strings are
   library defaults). `directWebrtc` flag present.
3. **Payload = RFB (VNC-like)** over the data channel: handshake writes `reMarkable`
   + version (`1.3.1`, `ScreenShareVersion` Version1/Version2), then
   `FrameBufferUpdate` rects **zlib-inflated** (*"Failed to read zstream"*), plus
   custom `RotationUpdate` and `CursorUpdate` messages.
4. Client states: `Initialized`, `Active`, `Stopped`; disconnect reasons `PingTimeout`,
   `ServerShutdown`, `UserInitiated`, `WebRtcFailure`, `ProtocolError`, `NegotiationTimeout`.

**Protocol details** [decompiled with Ghidra 12.2, 2026-11-02; implemented in
`client/src/liveview/` and `ui/liveview.py`. First live session (2026-11-02):
- **Worked against the real cloud and tablet:** the notifications socket, accepting the room (the join returned 4 ICE servers: Twilio TURN plus STUN), the broadcast `request-offer`, the tablet's offer, and our direct answer.
- **The tablet's offer:** libdatachannel (`o=rtc`), a single `webrtc-datachannel` m-line with `mid:0`, and `ice-options:ice2,trickle`, with its candidates trickled separately.
- **ICE:** completed with libjuice over the server-reflexive pair.
- **Failure:** DTLS failed in aiortc. The tablet's certificate has X.509 version field 1 (v2), which `cryptography` refuses to load. Fixed in `liveview/dtls.py`, which hashes the raw DER instead.
- **Second run:** DTLS, SCTP and the `screenshare` channel all opened, our handshake was accepted, and the tablet sent its hello and a ~43 KB first update. Each update's zlib stream ends with a sync flush rather than a final block, and `zlib.decompress` rejects that ("incomplete or truncated stream"). The decoder now inflates what is there, as the app does.
- **Third run: Screen Share works end to end.**
  - Hello: version 2, 1404×1872, followed by `Rotated(0)`.
  - Then about 40 updates of one rectangle each, mostly near full screen (e.g. 1390×1858 at 7,7 or 1404×1872). Each rectangle's size is exactly w×h×2 bytes; each update inflates from roughly 16–34 KB to about 5 MB.
  - Stopping on the tablet sent type 101, which ended the session as ServerShutdown.]:

Signalling (REST broker on the gateway, all POST under `/screenshare/v1`):
- `/rooms/{id}/join` and `/rooms/join-active` reply
  `{"roomId", "iceServers": [{"url", "username", "credential"}]}`.
- `/rooms/{id}/messages/broadcast` takes `{"payload"}`.
- `/rooms/{id}/messages/direct` takes `{"payload", "targetClientId"}`.

Notifications socket:
- The host comes from discovery: `"notifications": "eu.tectonic.remarkable.com"`.
- URL `wss://<host>/notifications/ws/json/1`, with `Authorization: Bearer <user token>`.
- Each message is `{"message": {"attributes": {"event", "sourceDeviceID", ...},
  "data": base64(JSON)}}`. Messages whose `sourceDeviceID` is our own are ignored.
- Events: `ScreenshareRoomCreated` (with `attributes.roomId`) and `ScreenshareMessage`.
- `ScreenshareMessage` data is `{"type": "request-offer"}` or
  `{"type": "webtrc", "payload": <signal>}` ("webtrc" is the app's own spelling).

WebRTC:
- Signals: `{"type": "offer"|"answer", "description": sdp}` and
  `{"type": "candidate", "candidate": str}`.
- The viewer broadcasts `request-offer`. The tablet sends the offer directly to it, and the
  viewer answers directly to the tablet's `sourceDeviceID`.
- Data channel label: `screenshare`.

RFB stream (big-endian):
- The client writes `"reMarkable"` + u16 `2`.
- The server sends a u8 type:
  - 104: handshake, u16 version, width, height. The framebuffer is RGB565
    (`QImage::Format_RGB16`) and starts white.
  - 0: framebuffer update, u16 rectangle count, u32 length, then one zlib stream holding,
    per rectangle, u16 x, y, w, h, u32 size and raw little-endian RGB565 rows.
  - 100: cursor, u16 x, y.
  - 101: server shutdown.
  - 102: rotation. Version 1 sends a u8 (1 → 0°, otherwise 270°); version 2 an i32 of degrees.
  - 103: ping. The client disconnects after 20 s without one and never sends pings itself.
- Parse results: 0 = message consumed, 1 = error (disconnect), 2 = need more data,
  3 = shutdown.
- A negotiation timer of 10 s starts when the offer is requested.

UI (QML):
- Accept dialog: "Accept the connection from your reMarkable paper tablet…".
- Menu item "Join active Screen Share session". With no session, the app shows
  "No active Screen Share sessions found".
- The stream opens in a "Screen Share" window, rotated by `-rotation`, with a 15 px
  `#CCF41515` cursor.
- On disconnect: "Screen Share is no longer active".

**USB / 10.11.99.1** [O]: the only `10.11.99.1` reference is a translated help string
(*"Open the address http://10.11.99.1 in a web browser"*, "USB web interface") shared
with the tablet build. The desktop app has **no code path** to the USB web interface;
`webusb` / `webusb_import` are feature-name strings. Screen Share always goes through
cloud signalling + ICE (a USB link may still be chosen as the ICE path) [I].
For Linux, the USB web UI (`http://10.11.99.1/documents/`, `/upload`, `/download/{id}/placeholder`)
is a separate, well-known tablet-side interface that would need its own client module.

## 5. File formats

Document = UUID-named files in a library directory [O] (`%1/%2.rm` = `<uuid>/<page-uuid>.rm`):

| File | Content |
|---|---|
| `<uuid>.metadata` | JSON: `visibleName`, `parent`, `type` (`DocumentType`/`CollectionType`), `lastModified`, `lastOpened`, `createdTime`, `pinned`, `deleted`, `metadatamodified`, `synced`, `source` |
| `<uuid>.content` | JSON: `fileType` (notebook/pdf/epub), `formatVersion` (v2 with `cPages`), `pageCount`, `orientation`, `coverPageNumber`, `documentMetadata`, `extraMetadata` (tool state), `zoomMode`, `customZoom*`, `textScale`, `margins`, `lineHeight`, `fontName`, `pageTags`, `tags`, `redirectionPageMap`, `originalPageCount`, `sizeInBytes`, `keyboardMetadata` |
| `<uuid>/<page>.rm` | Stroke/scene file. Header `reMarkable .lines file, version=N` padded to 43 bytes; readers for **v2, v5 (legacy), v6**. v6 = tagged blocks (*"Unknown block starts at %u: tag=%#2x(%s), version=%d, minVersion=%d, extra len=%lld, payload size=%u"*) holding a CRDT scene tree (`rm.crdt.{tree,sequence,list,textdocument}`, `SceneInfo`, `MigrationInfo`, `SceneGroupItem`, `SceneGlyphRangeItem`, `UuidIndexMap`) |
| `.pagedata`, `.local`, `.highlights`, `.textconversion`, `.thumbnails/`, `.epubindex`, `.tombstone` | per-page templates, local state, PDF highlights, handwriting conversion, thumbnails, EPUB index, deletion marker |
| `<uuid>.pdf` / `.epub` | Original imported document; rendered with PDFium (`FPDF_RenderPageBitmap`, text search `FPDFText_*`), EPUB via internal `epub` module (HTML/CSS → PDF) |

### 5.1 `.rm` lines format: v5 vs v6

Header (all versions) [O]: `reMarkable .lines file, version=N` space-padded to 43 bytes;
the binary knows versions 2, 5 and 6 (`Lines file header length (`, `SCENE_FILE_V6`).
Parser: `client/src/viewer/rm_parser.py` (v3, v5, v6).

| | v3 / v5 (legacy) | v6 (current) |
|---|---|---|
| Structure | Flat streams: layers → strokes → points, counts as `i32` | Length-framed blocks `<u32 len><u8 0><u8 minVer><u8 ver><u8 type>` holding a CRDT scene tree (`rm.crdt.*`) |
| Values | Fixed little-endian fields | Tagged values: varuint `(index<<4)\|type`; types `0x1` u8, `0x4` 32-bit, `0x8` f64, `0xC` sub-block, `0xF` CrdtId (`u8` + varuint) |
| Stroke header | `i32 tool, i32 color, i32 _, f32 width` (+ `i32 _` in v5) | SceneLineItem (0x05) value: `u8 3`, then 1 tool, 2 color, 3 `thicknessScale` f64, 4 `startingLength` f32, 5 points, 6 timestamp, 7 `moveId`, 8 ARGB colour, 9 `sourceId` (see "Pen strokes" below) |
| Points | 6 × f32: x, y, speed, direction, width, pressure | Block v2: 14 bytes `f32 x, f32 y, u16 speed×4, u16 width×4, u8 direction×255/2π, u8 pressure×255`; block v1: 6 × f32 |
| Layers | Implicit, by position | TreeNode (0x02) with last-write-wins label/visibility, attached to root `0:1` via SceneGroupItem (0x04) |
| Coordinates | x from left edge, 0–1404 | x centred on the page (−702…702); pages can extend beyond 1404 × 1872 |
| Erasing | Eraser strokes that show the background under the ink again (§5.15) | Items deleted in the CRDT: the stroke's own block stays, without its value and with `deletedLength`; what an eraser leaves of a stroke comes back as new strokes (see "Deleting and the eraser" below) |
| Typed text | — | RootText (0x07): CRDT character sequence (left/right ids) + paragraph styles (last write wins) + box position/width |
| Images | — | SceneImagesTable (0x0E) + CrdtImageItem (0x0F) |
| Groups | — | Layers are children of root `0:1` (Layer 1 is usually `0:11`); groups nest inside layers via CrdtGroupItem. A group TreeNode may carry an **anchor** (LWW fields 7 char id, 8 type, 9 threshold, 10 origin x): its strokes' x is relative to origin x and y to the line holding that typed-text character, so handwriting follows the text |

Enums [O] from ordered name tables in `.rdata` (the 3-letter `Red` sits in an 8-byte gap
that `strings -n 4` hides):

- **Tool:** Paintbrush 0, Pencil 1, Ballpoint 2, Marker 3, Fineliner 4, Highlighter 5, Eraser 6,
  SharpPencil 7, EraseSection 8, ClearPage 9, ZoomTool 10, SelectionTool 11, Paintbrushv2 12,
  SharpPencilv2 13, Pencilv2 14, Ballpointv2 15, Markerv2 16, Finelinerv2 17, Highlighterv2 18,
  SolidPen 19, ReservedPen 20, Calligraphy 21, MaskedEraser 22, ShadingMarker 23, Undefined 24.
  The toolbar's names for the nine pens: Ballpoint pen (15), Fineliner (17), Marker (16), Pencil (14),
  Mechanical pencil (13), Paintbrush (12), Highlighter (18), Calligraphy pen (21), Shader (23).
- **Color:** Black 0, Gray 1, White 2, HighlighterYellow 3, HighlighterGreen 4,
  HighlighterPink 5, Blue 6, Red 7, HighlighterStackedGray 8, ArgbCode 9, Green 10, Cyan 11,
  Magenta 12, Yellow 13, HighlighterBlue 14, HighlighterOrange 15, HighlighterGray 16, LastColor 17.
  Both enums are also in the moc metadata of class `Line` with these values (`tools/moc_api.py`);
  the ARGB of each colour is in §5.2.

**Block types** [O]: `const char* names[16]` indexed by type (data at file offset
0x121ab20): 0 Conversion, 1 SceneTreeMove, 2 SceneTreeNode, 3 CrdtGlyphItem,
4 CrdtGroupItem, 5 CrdtLineItem, 6 CrdtTextItem, 7 RootText, 8 CrdtTombstoneItem,
9 Uuid2Index, 10 PageStats, 11 Array, 12 MigrationInfo, 13 SceneInfo,
14 SceneImagesTable, 15 CrdtImageItem. So 14/15 are images, not typed text.

**Paragraph styles** [O]: the menu's types (`scene::ParagraphStyle::Type`, magic_enum names at
0xc89000, `cmp edx,0xc; names[edx]`): 0 Title, 1 Subheading2, 2 Paragraph (default), 3 Bulletpoint,
4 BulletpointIndented, 5 CheckboxUnchecked, 6 CheckboxChecked, 7/8 the indented checkbox variants,
9 NumberedList, 10 NumberedListIndented, 11 ImageBlock, 12 Subheading. A file does not store that
index. It stores a `style_legacy` byte (0 nothing, 1 body text, 2 title, 3 subheading 2, 4 bullet,
5 indented bullet, 6 checkbox, 7 checked, 8/9 the indented ones, 10 numbered, 11 indented numbered,
12 image; 0x140a92600 reads it, 0x140a93100 writes it, 0x1408b6340 maps it to the type), which is the
community's numbering (2 = heading), and `base_style` + `style_properties` beside it where the byte
cannot say it all. A new text box starts with a title (`createRootDocument(ParagraphStyle.Type.Title)`,
DocumentViewport.qml), which is the `0:0 -> 2` every real page has. Inline styles are marker entries in
the character sequence: 1/2 bold start/end, 3/4 italic, 5/6 link (`scene::CharacterStyle::Type
{ None, Bold, Italic, Link }`; 0x140aab3c0 pairs an end with the start whose id is one less).
See `viewer.rm_parser.decode_root_text` for the whole block.

**Changing typed text** [O] (`viewer.rm_text`): the app holds the sequence as runs in reading
order. Loading (0x1409164b0): entries not listed by rising id are taken in file order (how it
writes them); otherwise each is integrated (0x1409350a0): dropped if its neighbours are not older
than it (0x140943f30); appended to the run holding `left` when it just continues it (0x14092e6d0);
else placed by scanning from `left` to `right`, two runs between the same neighbours ordered by
author UUID (0x140a984f0). Typing (0x140aae810 → 0x140aae250) makes one run with fresh ids from
the page clock, cutting the run it lands in (0x14093fd50, 0x140940520: the second half keeps its
own ids, left = the id before, right = the run's right). Deleting (0x140ab8970 → 0x14093d670)
turns stretches into tombstones (no value on disk) and leaves markers. Bold/italic (0x140aaee30)
put a start marker and an end marker with the next id around each unstyled stretch; taking a
style off (0x140ab4710) deletes the markers it reaches and re-marks what lies outside. PageStats
(0x0A) fields are `loads, merges, textChars, textLines, keyboardCount`: textChars = UTF-16 length
+ 1, textLines = paragraphs (all 34 real blocks); a text edit updates both.

**Text mode** [O] (`ui.textedit`, `SceneView`/`SceneController` in `bridge/scene.py`): the view
has the keyboard; `SceneKeyHandler::process` (0x140408d10; Windows helpers 0x140408af0,
0x140408c40) maps keys to `SceneKeyHandlerAction {type, MoveOperation, MoveMode, text}`, emitted
as `sceneKeyActionReceived` and handed back to `SceneController::keyAction`. Ctrl+1..7 = Title,
Subheading, Subheading2, Paragraph, Bullet, Numbered, Checkbox. Enter (`handleKeyEnter`
0x14094f2d0): empty list item → un-indent (0x1408b5aa0), else new paragraph that copies the style
when the old one is a list item (checkbox unchecked) or was split mid-way. Backspace
(0x14094ea70) at a list item's start → body text. `* `/`+ `/`- ` at a paragraph start → bullet
(0x14094d080). `cycleParagraphStyle` (0x14043ca20): a list button on the same list kind cycles
indent 0..2 (0x1408b5870), anything else sets. Indented lists are written with
base_style/style_properties (0x140a92b30). The cursor is drawn by the view, not QML
(`CursorBlinkManager` only toggles `setCursorVisible`). Undo of text: remove by id, put back with
new ids after the original neighbour, ids translated across the stack (as the app's undo
"translated" log).

**Line breaks and checkboxes** [O].
- Shift+Enter is the `LineBreak` key action (19). `SceneController::keyAction`
  (0x140443380) sends it down the keystroke path with a static `QString(QChar(0x2028))`
  (log "key-stroke:"): U+2028, QTextDocument's line separator, is typed into the
  paragraph as an ordinary character (stored UTF-8 in the RootText run; no newline, no
  style entry, textLines unchanged). The 32 cached text pages hold no such character
  (only `\n`), as nobody typed one. The client types it (`textedit`, LINE_BREAK) and its
  layout ends the line there (`renderer.LINE_SEPARATOR`).
- A checkbox is a link: `linkAt` (0x1408a7ac0) looks at the PDF's links first, then the
  scene's checkbox table (scene+0x378, made at 0x140981de0; records of 0x38 bytes: key,
  block number, QRectF, ParagraphStyle), returning `SceneLink {rect, url}` with url
  `QString("%1://index=%2").arg("rmcheckbox").arg(key)` (0x140987930). The key is a
  character position: the table is shifted on every contents change (0x140989280) by the
  characters added or removed. `SceneLinkHandler` emits `toggleCheckbox(key)`, which
  DocumentView passes to `toggleCheckboxByIndex` when editing is allowed (0x140449870 ->
  scene worker 0x140988a80): the record's style is toggled by 0x1408b5710 (a list with
  kind 1, checkbox: state bits 8..10 = 1 or 3 -> 0, anything else -> 1), set on the
  paragraph and pushed as one action on the undo stack. Stored: the legacy byte 6 <-> 7
  for an unindented box (the one cached checked box is `7`, no base/properties), 8 <-> 9
  with base 3 / properties for indented ones. The client keys its links by where the
  paragraph starts (`renderer.checkbox_boxes`), toggles through the text session (one
  undo step) and opens one for the click when text mode is off.

**Pen strokes** [O] (`viewer.rm_stroke`; every rule below holds on all 812,546 line blocks of
a real library, 746,526 of them live). A stroke is one CrdtLineItem block, type 0x05, always
`min_version 2, version 2` (0x06, CrdtTextItem, never occurs and is not a stroke). Payload, in
this order (writers 0x140a094e0 / 0x1409f9a90):

| Tag | Field | Type | |
|---|---|---|---|
| `1F` | parent | CrdtId | the layer's node, or a group nested in it |
| `2F` | id | CrdtId | the page's next id (`++clock`, 0x1408e43e0) |
| `3F` | left | CrdtId | the item it was put after |
| `4F` | right | CrdtId | the item it was put before; `0:0` when appended |
| `54` | deletedLength | u32 | 0 live; n: a run of n deleted ids starting at `id` |
| `6C` | value | sub-block | only when deletedLength is 0 |

The value (writer 0x140a24bf0, reader 0x140a20dd0), a `u8 3` and then:

| Tag | Field | Type | |
|---|---|---|---|
| `14` | tool | u32 | Line::Tool |
| `24` | color | u32 | Line::Color |
| `38` | thicknessScale | f64 | the pen's size setting: 1 thin, 2 medium, 3 thick |
| `44` | startingLength | f32 | 0; for a piece an eraser left, the length of the original stroke up to where the piece starts |
| `5C` | points | u32 length + n × 14 bytes | at most 30,000 are written |
| `6F` | timestamp | CrdtId | always written; `0:1` for a new item, another id only after a transform that carried one (0x1405b67f0) |
| `7F` | moveId | CrdtId | only when not `0:0`: the item this one replaced (move, undo, redo) |
| `84 01` | argb | u32 | only when color is 9 (ArgbCode) |
| `9F 01` | sourceId | CrdtId | only when not `0:0`: for a Shader's piece, the stroke it was cut from |

Bytes after the last field the app knows are kept and written back (value, item and PageStats
alike); real pages have none. No bounding box is stored (`Line::boundingRect`, 0x140b3d760,
works it out: the box of the points widened by half of what the tool can reach), and the
"timestamp" is a last-write-wins id, not a time. A point is 14 bytes for every tool
(0x14045dae0, written by memcpy):

    f32 x, f32 y                 scene units, x 0 = centre of the page
    u16 roundf(speed * 4)        u16 roundf(width * 4)
    u8  roundf(direction * 255 / 2pi)        u8 roundf(pressure * 255)

**Where a new stroke goes** [O]: `addLine` (0x1409578c0) refuses an empty line, layer `0:0`
and the tools that do not draw (Eraser, EraseSection, ClearPage, SelectionTool; the layer,
0x1408c2bc0, also ZoomTool and MaskedEraser). The layer gives the item the page's next id
and appends it to its CRDT sequence: left = the last id of the sequence (deleted ones count),
right = `0:0`. There are no fractional keys in a page: order is the left/right ids, replayed
in id order (`rm_writer.sequence`). Item blocks are stored sorted by (parent, id). PageStats
(0x0A: loads, merges, textChars, textLines, keyboardCount) has no stroke counters; the writer
stores `loads + 1` on every save and the rest as they are. On a page with typed text the
scene has an anchoring manager (0x14096ca70) that puts the stroke into a group anchored
to a text line instead (not traced). On the 30 cached pages with a RootText block, the
tablet's strokes are in such groups: all 472 on the 5 pages with real text, 3,664 of 3,907
on the 25 with an empty box; the other 243 (4 pages, all with an empty box) sit directly
on the layer, so a layer stroke beside a RootText block is a page the tablet writes and
reads. `rm_stroke` draws on such pages as on any other: the stroke goes on the current
layer, unanchored (it will not follow the text when the text reflows on the tablet, as
the tablet's own anchored strokes do; no cached page has an unanchored stroke beside real
text). The eraser takes the layer's line items and those of every group below it,
the tablet's anchored strokes included where the page draws them (see "Text-anchored
stroke groups", Eraser), leaving the text block byte for byte as it was.

A text box nothing was typed in is not made: the text session holds its `create` back
until something is typed, and a `create` with no text edit after it on its page (kept by
earlier versions of this client) makes no box when the edits are replayed.

**Pen input** [O] (`rm_stroke.StrokeBuilder`, `simplify`, `draw`). The desktop app's QML
never creates a pen surface or a toolbar, so reMarkable.exe cannot draw; but the tablet's pen
code is in the binary, mouse path included:

- `PenInputSurface` (vtable 0x140e8b250; press 0x140ad8e90, move 0x140ad8d80, release
  0x140ad9060): a mouse gives samples `{x, y, pressure 0.7, tiltX 0, tiltY 0}`: one for the
  press, one per move, one more every 25 ms while the pressed mouse rests (QTimer,
  0x140ad9200), one for the release. Alt held: the eraser ("Simulating Eraser Tip").
- `PenInputLineHandler` (vtable 0x140e8a3d8): `beginStroke` 0x140ac8060 makes the line
  (0x140b40c30: thicknessScale = the pen's size; argb only for colour 9), `addPoint`
  0x140ac9360 maps the sample into the scene and makes one point of it, `endStroke`
  0x140ac86d0 emits `stroke(Line)`.
- One point per sample: 0x140b40df0, and for tools 13, 14, 15, 16, 19, 20, 21, 23 its second
  half 0x140b41680. With T the size, p the pressure, `scale` = sqrt(|det|) of the scene-to-
  screen transform (0x140b0d080), all in single precision:

  | Tool | speed, direction, pressure | width |
  |---|---|---|
  | Ballpoint (15) | from the sample before: dist × scale; atan2 in 0..2π; p | max(2, 3p) × T′, T′ = T/1.35 if T > 1 else T |
  | Mechanical pencil (13) | same; p′ = 0.3·smoothstep(p) + 0.7 | 4 × T′ |
  | Pencil (14) | same; p′ = 0.8·(pressure stored for the point before) + 0.2·p | (15·a⁴ + p′ + 2) × T, a = tilt between 30° and 55° as 0..1 |
  | Marker (16) | same; p | (tilt² · (0.5p + 0.5) · 10 + 2.5) × T, tilt capped at 1 rad |
  | Calligraphy (21) | same; p | f = max(0, 1 − \|angle between the stroke and the nib\| / 60°), nib at 40° (right hand; 170° left) + rotation; max(1.5, (4.5·f·p + 1) × T), then ¾ of the width before + ¼ of it |
  | Shader (23) | speed = 0.9·(speed before) + 0.1·speed; p | (10T + 2) × (⅓ + ⅔t), t = p between 0.1 and 0.3 as 0..1 |
  | Fineliner (17) | means over this sample and the one before: (d₁ + d₂)/2 × scale; (a₁ + a₂)/2; min(1, (p + p₁)/2) | max(2, 2T) |
  | Highlighter (18) | same as Fineliner | 30 × T |
  | Paintbrush (12) | same, then p × clamp(1 − speed/scale/100, 0.5, 1), p′ = max(0.25, p) | max(2, 4.5·p′^4.5 + 4.5·tilt⁴) × T |
  | Eraser (6) | same as Fineliner | 10 × T |

  Tilt is atan(hypot(tan tiltX, tan tiltY)). The append (0x140b3cf40) gives a stroke whose
  first point came out with width 0 the third point's numbers for its first two. On the
  real library the Ballpoint width is exact for 99.1 % of 7.27 M points (the rest are 1/4
  unit off: the stored pressure is rounded) and the Fineliner's for every one of 456,838
  strokes; stored speed and direction cannot be checked point by point because of the next step.
- Before `addLine`, SceneController (0x1404473a0 → 0x140b3f850) thins the line out with
  Ramer-Douglas-Peucker (0x140b3ede0): between two kept points the one farthest from the
  straight line through them is kept too when it is more than 0.2 off it, or when some point
  between them differs from the first of the two by more than ⅔ in width, 0.1 in pressure
  (tools 0, 1, 2, 3, 12, 13, 14, 15, 16, 21, 23) or 2.0 in speed (tools 0, 1, 3, 12, 15, 16,
  23). A line that still has 29,990 points or more is thinned again (1.0, 0.1, 0.5, 1.0), and
  not added if that is not enough. Real Fineliner strokes are fixed points of it (99.4 %),
  and lose a third of their points at twice the tolerances.
- The size and colour of each tool are kept per document in `.content` `extraMetadata`:
  `LastPen`, `Last<Tool>Size` ("1", "2", "3"), `Last<Tool>Color`, `Last<Tool>ColorCode`
  (decimal ARGB), `SecondaryPen`…, `LastEraserTool`, `LastEraserSize`
  (`DocumentToolSettings`, 0x14060e1e0). `PenThicknessModel` and `ToolModel` exist in the
  desktop exe only as translation strings.

**Deleting and the eraser** [O] (`rm_stroke.delete_strokes`, `can_join`, `erase`,
`erase_line`).

- Deleting an item (0x1408ce450) leaves its block where it is, under the same block type,
  without field 6 and with deletedLength 1: an erased stroke is a 0x05 block, not a
  tombstone. A CrdtTombstoneItem (0x08) is written only for an item that has no value object
  in memory, i.e. one that was read from a 0x08.
- Then it is joined to the deleted item after it, and to the one before it, when the two are
  neighbours in the sequence and (0x1408c3e60) **(1)** the later one's left is the earlier
  one's last id, **(2)** both have the same author, **(3)** the later one's id is the very
  next counter, **(4)** both have the same right; both ids must be newer than their own
  neighbours (0x1408d2f40) and both deleted. The earlier one stays, with the lengths added
  up, under its own block type; the later block goes. Real pages have no pair of line or
  tombstone neighbours that this would still join, and putting a live stroke back for every
  deleted id of a page and deleting them in a shuffled order gives the page's own runs again
  (`tools/rm_roundtrip.py`: 462,685 deleted ids on 3,976 pages, none different).
- The eraser ("erase with line", 0x14041a8e0 → 0x140994f30) works on the outline of its own
  stroke (0x140a5f380): a QPainterPath through the eraser line's points, for the Eraser
  stroked as wide as the line's **first** point with round caps and joins
  (`QPainterPathStroker::createStroke`), then `toFillPolygon()`; an EraseSection line is the
  outline itself. For every live line item of the node, in sequence order (0x140996fb0 →
  0x140a63920), unless the line's bounding box misses the outline's:
  1. cuts (0x140a62500): the stroke's start, every crossing of one of its segments with an
     edge of the outline (0x140a63300), its end; each with its place in points (i + s), the
     stroke's length up to it (from `startingLength` on) and a margin
     `((1 − c)·a + c·b) × width there × ½`, c = |cos| of the angle between segment and edge,
     (a, b) = (0.7, 2.0), for highlighters (0, 1.5);
  2. erased or not (0x140a62f60): the stretch between two cuts is erased when it is shorter
     than 1e-5 points or its middle (the first whole point in it, when its ends lie in
     different segments) is inside the outline (winding ≠ 0, 0x140a63420); two kept stretches
     in a row get an empty erased one between them;
  3. margins (0x140a64210): a kept stretch is shortened at each end by that cut's margin,
     measured along the stroke, and dropped when it is no longer than the two together;
  4. pieces (0x140a612c0): one stretch covering the whole stroke means untouched. Else each
     kept stretch becomes a line with the stroke's tool, colour, ARGB and thicknessScale: a
     point worked out for a cut end (every stored number taken between its two neighbours'
     and rounded again), the whole points between, `startingLength` = the stroke's length at
     the piece's start; fewer than two points: no piece.
- All strokes hit are deleted first (and so joined into runs); then each one's pieces are
  inserted right after it (InsertItemsAfter, 0x14095b8e0 → 0x1408c8920) as new items with the
  next ids, timestamp `0:1`, no moveId: left = the erased item for the first piece, then the
  piece before; right = what followed. A run of deleted ids is cut where a piece goes in: in
  real pages the left of a live stroke is never an id inside a run, and the second part of
  such a run has the id before it as its left and the run's right. A Shader's piece carries
  `sourceId` = the first stroke it descends from. A stroke nothing is left of is simply
  deleted.
- Stored Eraser-tool strokes (white, in old pages) are legacy; the app prunes those at the
  bottom of a node (0x1408c5030).

**Undo, redo and the pen UI** [O] (`rm_stroke.restore_stroke`, `core.edits.StrokeChange`,
`StrokeRestore`, `ToolSettings`; `ui/pen.py`, `ui/quick/bridge/pen.py`).

- A stroke drawn is one AddItem action, an eraser pass one action whose remove half is the
  pieces it left and whose readd half the strokes it deleted (the item actions' shared
  redo/undo, 0x14095e690 / 0x14095f520). Undo and redo delete the remove half and put the
  readd half back as **new** items with the next ids, each right after its own deleted item
  (0x1408cbd60; a run of deleted ids is cut there), moveId = the item it first was unless the
  value has one. The client keeps each half's values (hex) and names (ItemRef, renamed via
  the page history's aliases) so the swap can be made again on a newer page. An eraser pass
  that touches nothing keeps no edit and no undo step.
- The desktop QML has no pen UI, so the client adds one without changing it: a toolbar
  (QML built in Python, the app's own `qrc:/ark/icons` pen icons) beside the page in the
  DocumentView's view container, and a `PenSurface` item over the DocumentViewport's mouse
  areas that is enabled only while a tool is in hand (otherwise a click does what the app
  does). Taking a tool ends text and selection mode; turning either on puts the tool down;
  Escape too. Mouse samples are PenInputSurface's (pressure 0.7, a sample every 25 ms the
  button is held still, Alt erases). A stylus press is left to Qt's own delivery, so
  whatever is on top (the toolbar, the page buttons) takes it first; only when nothing
  does and Qt makes a mouse press of it does the surface start a stroke, whose moves and
  release it then reads from the window's `QTabletEvent`s (pressure, tilt in radians; the
  eraser end erases). The toolbar's buttons are `TapHandler`s, which take a mouse, touch
  and a stylus alike (a `MouseArea` sees a stylus only through Qt's mouse synthesis). The line drawn so far is previewed with the page renderer and stays until the
  page has been drawn again with it.
- Settings are written as `extraMetadata` strings, the enum names real documents have:
  `LastTool`/`LastPen` (or `LastEraserTool`), `Last<Tool>Size`, `Last<Tool>Color`. Pen
  colours Black, Gray, White, Blue, Red, Green, Yellow, Magenta ("Pink"), Cyan; the
  highlighter's own yellow, green, pink, blue, orange and gray. Defaults: Ballpointv2, size 2,
  Black; highlighter size 1, yellow. A settings-only
  change uploads `.content` alone; whether the app also bumps `.metadata` lastModified then
  is not traced.
- **The highlighter draws in ARGB colours, as the tablet does.** Every highlighter line
  the tablet wrote in the cache (2026-11-20, 6 lines) is Line::Color ArgbCode (9) with
  ARGB `0xFFFFED75` in field 8 (fields 1–6 and 8), and the one document holding them keeps
  `LastHighlighterv2Color` "ArgbCode", `LastHighlighterv2ColorCode` "4294962549". In
  reMarkable.exe 3.29 the pen handler (`PenInputLineHandler`, properties `lineColor`,
  `lineArgbCode`, peninputhandler.cpp) puts the colour and ARGB it is given into the line,
  and the ARGB of each highlighter colour is the app's palette (the Line::Color table
  built at 0x1409d25a0 and 0x1409d9faf): yellow `FFFFED75`, green `FFACFF85`, pink
  `FFF29EFF`, blue `FFBEEAFE`, orange `FFFFC38C`, gray `FFC7C7C6`. The client wrote
  colour 3 (HighlighterYellow) with no ARGB before; it now writes colour 9 and that ARGB,
  keeps "ArgbCode" and the code in the settings, and reads a colour kept by name (older
  documents) as that colour's ARGB (`ui.pen.HIGHLIGHTER_ARGB`).
- **Paintbrush (Paintbrushv2, 12).** 3.29's addPoint (0x140b40df0) sends tool 12 to the
  Paintbrush (0) branch (jump table at 0x140b4161c) with the v2 flag (`+0x98`): pressure
  at least 0.25, width `max(2, p^4.5 × 4.5 + tilt^4 × 4.5) × size` (v1: × 10 and × 5),
  colours by name (no ARGB). The client's builder is the same; the 268 points of the 9
  paintbrush lines it wrote on the test notebook all match the formula, and the lines'
  fields (1–6) are those of every tablet v2 pen line. No tablet paintbrush line is cached.
- The Shader (ShadingMarker, 23) is the toolbar's tenth tool here (icon `shader`). No
  cached document has a shader stroke or setting, and the tablet's toolbar (its colour
  list) is not in reMarkable.exe: the client offers the pen colours with Gray first
  (its own choice), size 2; settings are `LastShadingMarkerSize`/`Color` like any pen.
- The calligraphy nib's two inputs: the hand is `Settings.rightHandMode`, copied into the
  handler when a stroke begins (`+0x2e0` -> `+0x410`, 0x140ac8103); the rotation is read
  off the transform the handler is given (`PenInputLineHandler::setTransform`
  0x140aca100): m12 = m21 = 0 -> 0 when m11, m22 > 0, else 180; m11 = m22 = 0 -> 90 when
  m12 > 0 and m21 < 0, else 270; anything else logs "unknown rotation" and keeps the old
  value (`rm_stroke.rotation_of`). The client's view only translates and scales the page
  (landscape documents are shown as stored, as in reMarkable.exe), so its rotation is 0;
  `SceneView.pen_rotation` reads it off the view transform all the same.
- **Stacking order after an undo** (client fix, 2026-11-20). An item put back by undo or
  redo takes the page's next id, and item blocks are stored sorted by (parent, id), so its
  block is the **last** of its node in the file while its place in the CRDT sequence is
  right after its own deleted self (left = that id, right = what followed). Nothing in the
  file shifts: ids, left/right and the value (timestamp `6F` included; moveId set to the
  first item) are what the app writes. But `rm_parser` handed each layer's strokes out in
  file order (`Layer.strokes`; only `Layer.items()`, which the renderer uses, sorted by
  stacking rank), so after erase + undo the scene model listed the same strokes in another
  order than before the erase, and anything reading `Layer.strokes` (tests, the
  `PageScene.strokes` users) saw the strokes put back on top. The pixels were right. Each
  layer's strokes and images are now listed in stacking order (the node sequences
  `_stacking` works out; file order only when a sequence cannot be worked out). Checked
  read-only on 150 cached tablet pages: 449 eraser passes over the current layer (946
  strokes hit), each undone, redone and undone again with `rm_stroke.delete_strokes` /
  `restore_stroke` as the undo stack does, gave back the page's own stacking order every
  time; and offline with a seeded random draw/erase/erase-selection/clear/undo/redo model
  through `LibrarySource` (`tests/test_erase_undo_clear_offline.py`), which fails
  without the fix.

**Erase selection and Erase all** [O] (`rm_stroke.clear_layer`, `Cleared`,
`rm_layer.clear_lines_in_layer`, `LibrarySource.clear_lines_in_layer`; toolbar in
`ui/pen.py`, `ui/quick/bridge/pen.py`).

- The tablet's eraser has three modes (icons `eraser`, `erase_selection`, `erase_all` in
  `qrc:/ark/icons`). **Erase selection** is Line::Tool EraseSection (8): the line drawn is
  handed to `SceneController::eraseWithLine` (slot 102, 0x14041a8e0, lambda
  0x140434480) like the Eraser's, with the controller's current layer index (+0xac) →
  `Scene::erase` (0x14094d9d0 → 0x140996ae0). Only the outline differs (0x140a5f380):
  the path through the points, `toFillPolygon()`, no stroker, so the line's width and
  size do not matter. Everything after (cuts, margins, pieces, ids, undo) is the
  Eraser's. Kept as a `StrokeErase` with tool 8; `LastTool`/`LastEraserTool` =
  `EraseSection`.
- **Erase all** is ClearPage (Line::Tool 9), an action, not a stroke:
  `SceneController::clearLinesInLayer(int layer)` (slot 78, 0x140439c50) resolves the
  index to the layer's node (0x1404438c0; nothing when there is none) and posts a lambda
  (0x140433ee0) to the scene: 0x140995ad0 makes a `SceneMacroAction`, fills it with
  `scene::eraser::createEraseItemsActions` (0x1409931c0) and pushes it only when it
  holds something. That walks the layer node and every group node below it (visitor
  0x1408d1670, as the eraser) and, per node, makes one `SceneDeleteItemsActionCrdt`
  (vtable 0x140d6b430; redo/undo are the item actions' shared 0x14095e690/0x14095f520)
  for the node's live items the filter keeps (0x140433d20: everything but value type 7,
  images, and 2, groups; `0x140412400`, which decides whether there is a filter, always
  returns 1). A group node all of whose items went gets a `SceneDeleteGroupActionCrdt`
  (vtable 0x140d6b468: redo detaches the node, a SceneTreeMove to `0:0`; undo
  0x14095eed0 moves it back under its parent) and, when anchored, the anchor manager's
  action (0x14096f3a0). Redo of the macro runs its actions in order (0x1408f48a0), undo
  last first (0x1408f52b0). `SceneController::clearLines()` (slot 108, 0x140439c00)
  calls `clearLinesInLayer(i)` for i = 0..4: every layer, one undo step per layer.
- So a clear deletes, takes no id, and its undo puts each item back as a new item right
  after its deleted self, the group nodes' items first, then the layer's, each node's in
  its sequence order (`Cleared.undo_order`). The client clears the **current layer**
  (the toolbar's Erase all button → `SceneView.clear_current_layer` →
  `clearLinesInLayer(currentLayer)`; the Layers foldout's slot keeps its index), keeps
  it as one `StrokeDelete` (pending edit) and one undo step (`StrokeChange` readd in
  that order), and nothing when the layer holds no stroke.
- Deviations: an emptied group node is left attached and anchored (as the client's
  eraser leaves it); strokes stored with version-1 points (which the client cannot put
  back) stay. Glyph ranges (PDF/EPUB text highlights, block 0x03) go with the strokes,
  as the filter has it (§5.11).
  Checked read-only on the same 150 cached pages: every current-layer clear takes no
  id, and putting the strokes back in `undo_order` gives the page's own stacking order.

**Text-anchored stroke groups** [O, checked on the 710 anchored nodes of a real library,
all on pages with a RootText block]. `scene::AnchorManager` (ctor 0x1409690f0, log category
`rm.scene.anchors`; scene+0x370) owns them. An anchored group is three blocks:

| Block | Fields (writer) |
|---|---|
| SceneTreeMove 0x01, v1.1 (0x1409faf50) | `1F` move id (= the node id N when the node is made), `2F` node (`0:0` then), `31` u8 is_update (1), `4C` { `1F` parent = the layer } |
| SceneTreeNode 0x02, v1.2 (0x1409fb4f0) | `1F` id N, `2C` label LWW, `3C` visible LWW; then, only when the anchor id's timestamp is not `0:0`: `7C` { `1F` ts, `2F` anchorId }, `8C` { `1F` ts, `21` u8 anchorType }, `9C` { `1F` ts, `24` f32 anchorThreshold }, `AC` { `1F` ts, `24` f32 anchorOriginX }; unknown tail kept |
| CrdtGroupItem 0x04, v1.1 | an ordinary sequence item of the layer (parent, id N+1, left, right, deletedLength), value `u8 2` + `2F` node id N |

A node has no other fields (all 9,255 real nodes are `1,2,3` or `1,2,3,7,8,9,10`): there is
no transform matrix anywhere in a page. `createGroup` (0x1408dfdb0, action
"create-group-crdt" 0x14095d470) takes N = `++clock` for the node and appends the group
item with the next id; the anchor fields are then stamped anchorId N+2, type N+3,
threshold N+4, originX N (369 real nodes; the other 341 were re-anchored later: anchorId
and originX carry later, consecutive ids). In memory (node object): +0x170 anchorId, +0x180
type, +0x190 threshold, +0x1a0 originX, each followed by its timestamp, +0x1f0 the anchor
position as a QPointF (never stored).
- **The page's clock does not take the anchor.** The anchorId is a reference, not an id
  the page made: on the 22 cached pages holding a sentinel anchor (0:0xfffffffffffe) the
  tablet's later items have ordinary counters (at most 9,746). `rm_writer.block_ids` now
  counts only field 7's stamp (`LWW_REF`); before, `highest_counter` read such a page's
  clock as 2⁴⁸ − 2, so an edit there would have taken ids from 2⁴⁸ − 1 on.
- **Made here** by `viewer.rm_anchor.add_group` (items pasted at the text cursor, §5.13):
  the three blocks above with N = clock + 1, the group item N + 1, anchorId stamped N + 2,
  type N + 3, threshold N + 4, originX N, an empty label and visible 1 at 0:0 (as all 710
  real nodes); checked byte for byte against a real node in `test_v1_finish_offline`.

- **anchorId**: the id of a RootText character, or `0:0xfffffffffffe` (before the text,
  index −1) / `0:0xffffffffffff` (after it, index = length) (0x140972ed0, 0x140972cf0).
- **anchorType**: 2 "Vertical" (700 real nodes) or 1 "Bidirectional" (10).
  **anchorThreshold**: 35.748032 (0x420efdfc at manager+0x18 = 4 mm at 227 dpi) for type
  2, 0.0 for type 1, exactly as stored in all 710. It is not half a text line.
- **Coordinate transform**: scene point = stored point + anchor position, and
  anchor position = (x, y) with (0x14097c4f0, 0x1409342a0, on the text's QTextDocument):
  i = index of the anchor character clamped to 0..length−1;
  y = text.pos_y + layout position y of i's block + y of the QTextLine holding i + that
  line's ascent, i.e. the **baseline of the visual line holding the character** (not the
  paragraph's first line);
  x = for type 2 the stored anchorOriginX (set once, to the x below, when the group is
  made); for type 1 `(boxWidth − textWidth)/2 + text.pos_x + layout x + cursorToX(i)`, so
  type 1 follows the character sideways as well. An anchor id the text does not hold is
  skipped with a warning.
- **The text layout it hangs on** (0x140a3c480, 0x140a3c280, text ctor 0x1409247b0):
  QTextDocument, document margin 4 (Qt's default), indent width 48, fonts with
  PreferNoHinting. Paragraph: reMarkable Sans 32 px weight 400, top margin 24, line
  height type LineDistanceHeight with 0.2 × the font's height. Title: reMarkable Serif
  Small 64 px weight 300, top margin 88. Subheading (12): Serif Small 46 px weight 400,
  top margin 56. Subheading2 (1): Sans 36 px weight 500, top margin 40. A checked
  checkbox is struck out. With the app's Qt (6.10.3) and fonts: body height 37.75, ascent
  29.59375, Title ascent 59.1875, body paragraph pitch 69.546875; the first block gets no
  top margin, so the first baseline is pos_y + 4 + ascent.
  Checked on real pages: every sentinel anchor has originX = text x + 4 (36 groups, box
  widths 936 and 1152); on empty Title boxes at y 234 the anchored strokes' stored y starts
  at −291…−294, which is the page top (3…6) only with the baseline 234 + 4 + 59.19, and a
  page holding both kinds ends at 1869 (anchored) and 1871 (layer strokes).
  **Deviation to fix in the client**: `renderer.stroke_offsets` uses the box top for
  sentinel anchors (63 px too high on such pages) and the paragraph's first line for the
  others; the 71.5 line height of §5.2 is a fit, not the app's 69.55.
- **Where a new stroke goes** (0x14096ca70 → 0x140977bf0): items larger than 178.74 (20
  mm) both ways, or not over text, are type 2; an item over text (0x1409741d0: its box
  grown by a third of a line, against the text lines' rectangles; for a stroke one of its
  points must lie in one) is type 1. It joins the overlapping group with the most children
  (0x140971e20, within the threshold) or a new group anchored at the character under the
  centre of its box (0x140974950: above the box → before-text sentinel, below → after-text
  sentinel), and is stored translated by −anchor position (float32 adds, 0x1405b8440).
- **Eraser** (0x140996ae0): the outline is built once in scene coordinates; the layer's own
  items are searched (0x140996fb0), then every group node below it at any depth, through
  live group items (visitor 0x1408d1670, callback 0x140991f10). In a node whose anchorId is
  set, the node's anchor position is added to each stroke point for the cut test
  (0x140a63920 takes it as dx, dy); cuts are parametric, so the pieces keep the group's
  local coordinates and are inserted after the erased item in the same group, as on a
  layer. A glyph range goes when more than half of its rectangles' centres are inside.
  Afterwards, per touched anchored group (0x140979810): what is left is clustered by the
  threshold, the largest cluster stays, the others are deleted and anchored afresh
  (clones in scene coordinates, moveId = the old item); a group left empty loses its
  anchor and is detached by a second SceneTreeMove (new id, node N, is_update 0, parent
  `0:0`; real pages have these, with the group item still live). The client
  (`rm_stroke.erase`, `anchors`) does the first part: the layer's items, then each
  group node below it (`group_nodes`, depth first in sequence order), an anchored node's
  strokes tested at the anchor position the page draws them at (`renderer.stroke_offsets`,
  so what is erased is what is seen; a node below an anchored group takes its position),
  the pieces put after the erased item in the same group, in its own coordinates; all
  hits deleted first, then the pieces given ids in hit order. The positions are kept in
  the `StrokeErase` edit (node, x, y) so the edit is made again identically; an anchored
  group with no position given is left alone. Not done: the clustering and re-anchoring
  afterwards and the detaching of an emptied group (its tree node, anchor and group item
  stay byte for byte, the group just holds fewer strokes).

**Moving, scaling and rotating a selection** [O; the stroke math checked on a real library:
4,226 cached v6 pages, 31,999 live strokes with a moveId]. There is no transform on nodes; a
moved stroke is a new item.

- **Selecting** (0x1409b2ab0, per node: the layer and every group below it). The node
  keeps the picked scene items (node+0x208, 0x1408cdb10); the selection keeps, per node, the
  set of their ids and a **clone** of each. A clone (line item copy constructor
  0x1405b5930) has the value, the timestamp and the sourceId of its original, and id,
  parent and moveId `0:0`; the selection then stamps it `moveId = the original's moveId,
  or the original's id when that is 0:0`. So a moveId always names the first item a stroke
  ever was, however often it is moved.
- **The gesture** changes nothing in the page. `moveSelectedItems`, `scaleSelectedItems`
  and `rotateSelectedItems` multiply a pending QTransform held by the SceneController
  (+0x2c8) and ask the scene only for the transformed bounds (0x1409b1e40, "previewing
  selection transform": clones of the clones, thrown away). The QML calls each once, when
  the drag is released; what moves under the pointer is a QML item.
  `applyPendingEdit(layer)` (0x140438540) does nothing when the pending transform is the
  identity; else it hands layer and transform to the scene and resets it. The desktop QML
  calls it once, when the selection closes (`endItemSelection`): every move, scale and
  rotation of one selection is **one** commit with the product of the transforms.
  `cancelPendingEdit` only clears the selection (0x14094bcb0).
- **The commit** (0x1409b48a0), per node holding selected items: **(1)** the live items
  whose ids are in the set are deleted (0x1408c5910; the blocks stay, joined into runs);
  **(2)** the clones are added (0x1408c2ae0 → 0x1408c2bc0: each the next id, appended);
  **(3)** the node's transform (0x1408c3570) deletes what (2) added, transforms each clone
  (item vtable +0x58 = 0x1405b67f0 with timestamp 0, so the timestamp stays) and adds it
  again (next id, appended). In an anchored group the transform is conjugated with the
  translation by the group's anchor position, and the group is re-anchored afterwards
  (0x140975b10). All of it is one `SceneDeleteAppendItems` action
  ("scene-delete-append-items-crdt", vtable 0x140d6dd70; undo puts the originals back,
  redo the clones, both through 0x1408cbd60); when the action on top of the stack is
  already this selection's (after a paste, selection+0x1d0), the two are merged into one
  undo step (0x1408f3920).
- **Ids used** (the calls of `++clock`, 0x1408e43e0, on this path): for n strokes **2n**
  counter values: n for the untransformed clones of step (2), which step (3) leaves as
  one run of n deleted ids, then n for the strokes that stay. Deleting takes no id, and
  neither does the timestamp (not touched). On a page with typed text the re-anchoring
  that follows takes its own (new groups, anchor stamps, above). Only a Shader's pieces cost more: before each of the two adds every distinct
  sourceId among the items is replaced by a fresh id (0x1408cb8d0), one counter value per
  distinct source each time, taken before the items' own ids (0x1408cb8d0 runs inside the
  node's add, 0x1408c2ae0, and the node transform, 0x1408c35a9; the paste's add,
  0x1409af310, goes through 0x1408c2ae0 too, so pasted shader pieces are renamed the same
  way; the eraser's insert and undo/redo do not). `rm_select._fresh_sources` does it. The previews of a gesture cost nothing.
  Real pages (tablet-written, pages without text, 356 commits of 3 or more strokes whose
  ids are consecutive, which 563 of 601 are): the n ids before the n live ones are a
  deleted run in the cleanly readable cases (exactly n deleted ids before in 31, 2n in 30,
  which is a paste and then a move, see below), and where the strokes moved were the last
  ones drawn the page reads `originals (deleted) · n deleted · n live`. **Not explained
  by this binary**: 33 of the 356 have no deleted id before them at all (13 of them none
  after either), i.e. one generation of ids; the tablet build, or a tablet-only
  operation, does that. A client that writes the single generation (delete the
  originals, append the transformed strokes with moveId) writes a page the tablet itself
  also writes; one that follows this binary to the letter writes n deleted line blocks
  more.
- **A stroke** is transformed by `Line::transform(QTransform)` (0x140b3d4d0): nothing for
  an identity; s = 1 for a pure translation, else sqrt(|det|); thicknessScale × s; each
  point's x, y mapped in double precision and stored as float32; each width
  `roundf(width_u16 × 0.25 × s × 4)` in single precision; speed, direction and pressure
  unchanged (direction is not turned by a rotation). Checked by finding the same stroke
  in two places through its pressure sequence (8 or more points, 4 or more distinct
  pressures; 10,852 strokes are in the library with more than one geometry) and fitting
  the affine map between the two:

  | Map between two copies | Pairs | thicknessScale ratio = sqrt(\|det\|) | Widths |
  |---|---|---|---|
  | translation | 12,950 | all (ratio exactly 1) | all 360,362 points equal |
  | rotation (21 by multiples of 90°, 6 free) | 27 | all (unchanged) | all 1,043 points equal |
  | scale with rotation, from an unscaled copy | 20 | all | `roundf(w × s)` on every point |
  | both copies scaled (two steps from a third) | 5 | all | within 1 (rounded twice), 9 points further off |

  Speed and direction are byte for byte the same in all 13,002 pairs (the 52 pairs where
  they differ are not affine images of each other: other strokes with the same
  pressures). Of them 366 pairs are an original and the item that names it as moveId,
  from an older version or a copy of the page: all translations, timestamp `0:1` kept.
  A source cannot be matched by id alone across pages: a copied page goes on counting
  from the same clock, so the same id is another stroke there.
  Without a second copy: every moved Fineliner stroke with thicknessScale 1, 2 or 3 has the
  pen's own width (13,853 strokes), and the 276 scaled ones have `roundf(24 × T/3)` on
  every point; moved Ballpoint strokes keep the pen's width rule within the usual
  quarter unit (all 15,804), scaled ones in 1,882 of 2,071 (the rest were scaled more
  than once). Every moveId names a deleted item of its own page (29,895 line blocks,
  2,104 tombstone blocks), 27,324 times in the same node; never a live one.
- **Copy** (`cloneSelectedItems(layer, scale)`, 0x14043a320): a clone of every selected
  item of the layer and of the groups below it: id and parent `0:0`, **moveId `0:0`**
  (the copy constructor never copies it), timestamp and sourceId kept. A clone from an
  anchored group is translated by the group's anchor position, so clipboard items are in
  scene coordinates and carry no anchor. Then all are transformed by
  `scale × the pending transform` when that is not the identity (the QML passes the
  view's `defaultScale`): a copy taken while a move is pending is the moved one. The
  clones stay in `Clipboard.items`, in memory; cut is copy and `deleteSelectedItems`.
- **Paste** (`cloneAddAndSelectItems(layer, items, scale, pos)`, 0x14043a060 → 0x1409af310):
  the clipboard items are cloned again (id, parent, moveId `0:0`), transformed by the
  scale when it is not 1 (the QML passes `1/defaultScale`, times page width / clip width
  when the clip is wider than the page; thicknessScale and widths follow as above),
  translated so that their box is centred on `pos`, and added to the **layer** node
  (0x1408c2ae0): n new ids, appended, in clipboard order, moveId `0:0`, as one
  `SceneDeleteAppendItems` action with nothing deleted. A distinct sourceId costs one more
  id, as above. The anchoring manager is not asked here: on a page with typed text the
  pasted strokes lie on the layer in scene coordinates until a commit re-anchors them.
  The pasted items are then the selection (its clones named after them), so moving them
  before the selection closes costs 2n more ids, leaves the n pasted ids deleted, and
  gives the strokes that stay `moveId = the pasted item`, never the stroke copied.
  Real pages: 1,425 times a page holds the same stroke twice or more, live (1,439 later
  twins, always by the same author, timestamp `0:1`, another position, 1,406 in the same
  node): 865 have moveId `0:0`, and the other 574 a moveId that is neither a twin nor a
  twin's moveId, i.e. the deleted pasted item. 2,033 more strokes have a thicknessScale
  that is not 1, 2 or 3 and no moveId (17 values such as 2.8358 and its half 1.4179, on 70
  pages), which is what a paste with a scale leaves [I]; with a moveId there are 397.
- **Picking** [O] (per node, 0x1408c9250: the region is first translated by −the node's
  anchor position when it has one). Only drawing tools and the legacy Eraser can be picked
  (line item vtable +0x28 = 0x1405b7530 → 0x140b3e0e0: not EraseSection, ClearPage, ZoomTool,
  SelectionTool, MaskedEraser). Two tests, chosen by an int the caller passes:
  - **any point** (vtable +0x40, 0x1405b73a0): some point, rounded (x ± 0.5 clamped to the
    int range, truncated), lies in the region. The desktop selection tool's rectangle,
    `addSelectionRect(QRect, LineSelectionMode)` (0x140437a20 → worker lambda 0x140433da0),
    passes this one; `ToggleSelection` (Shift) makes the result `was selected XOR hit`.
    An image is hit when the region meets its corners' aligned box (0x140442e80).
  - **all points** (vtable +0x38, 0x1405b7520): the line's aligned bounding rect meets the
    region's, and every rounded point lies in it. The tablet's lasso (0x1409b3600, mode 0, no
    toggle; callers 0x14041b2e9, 0x14042e279, 0x140434d4a, never reached from the desktop QML)
    builds its region in 0x1409ae1c0: the lasso line's points (× 1.0, 0x140b3fd90) as a
    polygon, `toPolygon()`, shifted by (−1,−1), (−1,1), (1,−1), (1,1), each
    `QRegion(…, OddEvenFill)`, united. Images: 0x1409a82c0 → 0x140a64ee0 (tests the image's
    mapped rect against the region; the rest not traced).
- **The client** (`viewer.rm_select`, `ui.selection`, `core.edits.ItemsTransform` /
  `StrokePaste`, `LibrarySource.transform_items` / `paste_strokes` / `delete_items`, bridge
  `SceneController` selection slots, `SelectionImage`): follows this binary, so a commit
  writes the 2n ids (originals deleted, clones added and deleted, transformed clones
  appended; moveId = the first item). The app's own QML drives it unchanged: the selection
  tool's rectangle, the box's drag / corner / rotation handles (only the pending transform
  and the box change; the page is drawn without the selected items, `SelectionImage` draws
  them through `pendingEdit`), `endItemSelection` (also Escape, a click outside, closing the
  document, undo, a pen stroke) → `applyPendingEdit` → one `ItemsTransform` and one undo step,
  merged into the paste's step when the selection is what was pasted. Copy, Cut, Paste,
  Delete/Backspace are the app's actions. The pen toolbar adds a freehand lasso (the tablet's
  rule above), which opens the same selection box. Checked on the cached library: all
  755,080 live line and image items of 4,210 pages committed with a turn and a scale; every
  page parses, every other block except the authors list is byte for byte the same, moveId,
  timestamp, speed, direction and pressure as above. **Deviations**: (1) strokes of a
  text-anchored group are transformed in their own group with the conjugated transform, but
  the group is not re-anchored afterwards (0x140975b10 not traced), and the anchor position
  comes from `renderer.stroke_offsets` (exact for moves; for scales and turns off by the
  known error of that offset); (2) a lasso takes an image when all four corners are inside;
  (3) a paste adds its strokes before its images instead of in clipboard order; (4) Shift-
  toggling while a move is pending commits the move first, then toggles on the new items.

**Straight lines and shapes ("hold at end of stroke")** [O; checked on the cached library,
5,901 files]. There is no hold timer, movement threshold or shape recogniser in
`PenInputLineHandler` or anywhere else in reMarkable.exe. The handler (vtable 0x140e8a3d8)
only builds the line, and RDP (0x140b3ede0) is the simplifier described above. The
exe has the *finishing* half of the feature: `sceneview::ShapesOverlay` (QML type
`ShapesOverlay`, moc 0x140c7bed0, log category `rm.shapesoverlay`, source
`xofm/libs/sceneview/src/shapesoverlay.cpp`). Its caller decides when a hold has happened and
what shape was drawn, and that caller is not in this build: the desktop QML never creates
a pen surface (see "Pen input"), and the build has no `addEllipse` import that could make
an ellipse outline. Strings in the build: the gesture cheat sheet's
"Hold at end of stroke to fix the shape. Drag and rotate it to adjust", the "Enable shapes"
button (`EnableShapesButton`), and `Settings.shapeSnapEnabled` (key `ShapeSnapEnabled`).
`PenInputHandler::timeSincePenUp()` (qint64) is the only timing hook it gives QML. The hold
time and the stillness tolerance therefore belong to the tablet's QML and cannot be read
from this binary.
`SceneController::highlightWithLine(line, LineSnapMode)` (`SnapDisabled` 0 / `SnapEnabled` 1)
is a different feature: highlighter snapped to text (`snapHighlighterToText`).

- **Interface.** The interface is `updateImage(QVariantMap gestureData, QPointF end,
  QTransform transform, QTransform invTransform)` (0x140462fa0), then `lastLine()`
  (0x140462400: the result, kept at +0x38), `reset(transform)` and `saveStroke(folder)`
  (dumps `%1*.line`, debug). Map keys: `line` (the stroke as drawn, a `Line`; fewer than 3
  points: "Insufficient points in line", nothing made), `type` (`ShapeGeometry::Type`:
  Unknown 0, **Line 1**, **Triangle 2**, **Rectangle 3**, **Ellipse 4**, **Quad 5**; 0 logs
  "Invalid shape type"), and for types 2–5 `path` (QPainterPath, the recognised outline
  in scene units), `bounds` (QRectF) and `rotation` (double, degrees, default 0). `end` is
  the pen's current position on screen, mapped to the scene with `invTransform`. (0, 0)
  stands for the line's last point (before the drag starts). scale = sqrt(|det transform|).
- **Line** (type 1, 0x140461c10 → 0x14046be20). The result is a copy of the drawn line
  (tool, colour, ARGB, thicknessScale, startingLength, timestamp as drawn): the same number
  of points, each keeping its own speed, width, direction and pressure bytes. Only x and y
  change. A = the first point, B = `end`, tol = min(|AB| / 3, 10) / scale. A and B are
  each rounded to whole scene units (CRT `round`, half away from zero). The line is made
  **horizontal** (B.y := A.y) when |B.y − A.y| < tol, and **vertical** (B.x := A.x) when
  |B.x − A.x| < tol. Otherwise the line keeps its slope, with no 15°/45° steps. Point i goes
  to `A + (B − A) · sᵢ/s_total`, s = arc length along the drawn points (first 0, last 1),
  computed in double and stored as float32. Then the bounding rect is reset.
- **Triangle, rectangle, ellipse, quad** (0x140461d80 → 0x14046a740). An empty path keeps
  the drawn line as it is. Otherwise a new line with the drawn line's tool, colour, ARGB
  and thicknessScale is built:
  1. Dragging and rotating: c = centre of `bounds`, L = the drawn line's last point. The
     scale is s = |c→end| / max(|c→L|, 1). The added angle is Δ = `QLineF(c, end).angleTo(QLineF(c, L))`
     in radians. The rotation r + Δ (r = `rotation`) is reduced mod 2π and **snapped to the
     nearest multiple of 45° when within 2° of it** (0x14046d220; the 45° and 2° are static
     locals). T = translate(c)·rotate(snapped)·scale(s)·translate(−c). T₀ is the same with
     rotation r and scale 1.
  2. Outline points: `path.toFillPolygon()`. Every edge (wrapping round) longer than 20
     is split into ceil(len/20) equal parts (0x14046d600 / 0x14046d9a0). This gives
     polygon P, whose last point repeats the first (a closed stroke).
  3. Attributes: the tail of the drawn line whose length just exceeds `path.length()` is
     taken (walking back from its last point; that is the part that drew the shape). Each
     output point j is T(Pⱼ), stored as float32. Its speed, width, direction and pressure
     bytes are copied **unchanged** from the tail point nearest T₀(Pⱼ) (the shape before
     the drag).
- **What the page gets.** The result is an ordinary 0x05 CrdtLineItem: `u8 3`, tool,
  colour, thicknessScale, startingLength 0, the 14-byte points
  (`f32 x, f32 y, u16 speed×4, u16 width×4, u8 direction, u8 pressure`, all four copied as
  stored, nothing recomputed for the new geometry), timestamp `0:1`, no moveId, ARGB only
  for colour 9. No field marks a stroke as a shape. It then goes through the same RDP
  thinning and `addLine` as any stroke [I: the tablet QML that hands `lastLine()` over is
  not in this build]. That is why it looks different per tool: the Fineliner's RDP ignores
  pressure, so a straight Fineliner line comes out as **2 points**. The Ballpoint's
  pressure and width test keeps the line's pressure profile along it.
- **Real pages.** On real pages, 2,667 live, never-moved strokes are exactly straight and
  have whole-unit end points. All of them have startingLength 0 and timestamp `0:1`. Every
  inner point lies on the segment, in order, matching the float32 lerp of the end points
  (0 misses). They are Fineliner 1,729, Ballpoint 891, Marker 47; 1,735 have 2 points
  (1,729 of them Fineliner). Of them, 1,319 are vertical, 658 horizontal and 690
  slanted; no slanted one has a step within the tolerance (the smallest is 2 units on a
  5.4-long line). Of the 932 longer lines, 896 vary in both pressure and width along the line,
  as the drawn samples would. 147 strokes are closed (last point = first). Shapes:
  38 five-point polygons (rectangles and quads after RDP; the four-sided ones' first edge
  sits on a multiple of 45° in 28 of 38; the others are 2.08° or more off, plus one at
  0.14°, which is a path recognised slightly off-axis with no rotation added). Circles of
  101 points, 3.6° apart, all edges equal (a 100-gon path) or 65 after Fineliner thinning. A
  Ballpoint polygon's edges are multiples of 20.226 (= L/ceil(L/20)) and 21.22.

**The client** (`rm_stroke.straighten`; hold detection in `bridge/pen.py` PenSurface) makes
the Line case by the rules above: when a pen stroke has been held within 4 screen pixels
for 500 ms (the client's own values: the tablet's are not in this binary; the timer is not
armed until the pen has moved after the press), the stroke is fixed as a straight line
from its first point, the pen then drags only its end, and on release the straightened
line goes through the usual RDP thinning and is kept as one StrokeAdd. Points are placed
along the segment by the length drawn up to them, as 0x14046be20 does, not projected.
**Deviation**: shapes (types 2–5) are not made, as their recogniser is not in this binary.

Not traced yet: sync upload rules (`insertItemsAtCursorPosition`, paste in text mode, is
§5.13), which tablet operation writes a moved stroke with one generation of ids, and the tablet-side
hold detection and shape recogniser that drive `ShapesOverlay`.

Validation: a real library's v6 pages (hundreds of thousands of strokes, up to 22 layers) parse with zero errors.
A real highlighter stroke uses ArgbCode with `0xffffed75`. Every block type present in
the corpus is interpreted or understood (no unaccounted blocks). Full-library run: every
page parses and renders with zero errors, anchored strokes included. The real text boxes
seen contain only blank paragraphs; the one page with blocks 14/15 holds two deleted images.

### 5.2 Rendering parity

**Stroke palette** [O]: ARGB array indexed by Color, built with immediate stores in
4 functions (e.g. VA 0x1409d9fc1). White comes from a sign-extended 64-bit store.

| Color | ARGB | Color | ARGB |
|---|---|---|---|
| Black | ff000000 | ArgbCode | per stroke |
| Gray | ff909090 | Green | ff91da71 |
| White | ffffffff | Cyan | ff74d2e8 |
| HighlighterYellow | ffffed75 | Magenta | ffc07fd2 |
| HighlighterGreen | ffacff85 | Yellow | fffae719 |
| HighlighterPink | fff29eff | HighlighterBlue | ffbeeafe |
| Blue | ff304ae0 | HighlighterOrange | ffffc38c |
| Red | ffc23132 | HighlighterGray | ffc7c7c6 |
| HighlighterStackedGray | 40000000 | | |

The stroke palette appears in no QML file; QML holds only UI chrome colours (Ark tokens).

**Blending** [I, strong]: of the 25 `QPainter::setCompositionMode` call sites, 6 pass Darken
(16) and none pass Multiply. Highlighter colours are fully opaque, so translucency must come
from compositing; Darken over paper gives the exact colour and leaves ink underneath black.
`setOpacity(0.9)` appears only in five helpers that open a QPainter on an existing
QImage, set `CompositionMode_DestinationIn` and `fillRect` the whole image: an alpha fade
of a pre-rendered buffer. They have no direct callers (they are reached indirectly), sit
outside the stroke rasteriser (`renderPencil`/`renderMarker…` at 0x140b37d00+), and
nothing ties them to highlighters, so highlighters render at full opacity
(`HIGHLIGHTER_OPACITY` in renderer.py).

**Text line metrics** [fitted]: body line height 71.5 px. Real anchored groups all carry
threshold 35.75 (= 71.5 / 2), consistent with the ~71 px other tools measured; with it,
anchored handwriting on a real page (100 groups, 437 strokes) lays out as clean,
non-overlapping lines. Not found as a constant in the binary.

**PDF placement** [verified on real files]: a PDF page is drawn at physical size on the
226 DPI screen grid, i.e. **226/72 page px per PDF point**, centred on the page's vertical
centre line (v6 x = 0) and top-aligned. A4 is therefore 1868 × 2643 px, wider than the
1404 px screen. Checked on annotated A4 and US Letter forms: handwriting lands inside the
form boxes and checkmarks on their checkboxes. Fit-to-page and fit-width (1872 px)
placements visibly miss. `226.0` appears in the binary, used by the template renderer.
Page mapping: `cPages.pages[].redir.value` is the PDF page; pages without `redir` were
inserted on the tablet (no background). EPUBs carry a `.pdf` rendition used the same way.
The full page-map spec, and how PDF/EPUB pages differ from notebook pages, is in §5.4.

**Pencil and paintbrush textures** (`viewer/brushes.py`). They are not rasterised from
nothing: the resources hold four sets of four 64 x 64 dithered black-and-white tiles,
`/images/pencil<n>-light.png`, `pencil<n>.png`, `pencil<n>-hard.png` and `brush<n>.png`
(`marker<n>.png` is named but not shipped); install.py copies them to
`client/src/assets/brushes/`. 0x140b2d940 loads the pencil sets as Grayscale8. Per point a
tile is picked by a hash of `(uint)(x + y + 0.0f)` (multiply by 0xD6E8FEB86659FD93, fold the
halves, again, low two bits) and, for pencils, by pressure: below 0.75 the light set,
below 0.9 the plain one, else the hard one (0x140b0f540; 0x14102b6f0, 0x140d2a18c). The
Paintbrush (0x140b2cdf0) draws each segment with a QPen whose brush is the QBitmap of
`brush<hash>.png`, rotated by the point's direction (2 pi x byte / 255), when its speed is
above 20 (0x140e8ee70); slower points are solid. Client: pencil runs are filled through
their tile at 0.5 + 0.5 x coverage (coverage = 1 - grey / 255) over the existing pressure
opacity (how 3.29's legacy pencil rasteriser 0x140b08d60 mixes tile and pressure was not
traced, and a light stroke must not vanish where its tile is blank); paintbrush runs use
the bitmap exactly; SVG export carries the tiles as `<pattern>`s of vector runs (the tiles
are pure black and white, so this is exact and the SVG stays vector).

**Not 1:1**: version 2 pencils (13, 14) go through strokev2's FillPencil (0x140b13610),
which shades with value noise (a permutation table at 0x140e8e1d0) rather than these
tiles; the Shader (23, strokev2 FillShaderAA) uses no tile, so its opacity stays an
approximation; typed-text font sizes (`crdt::TextProperties::FontSize`). The typed-text
fonts are the proprietary reMarkable Sans and reMarkable Serif Small, embedded in the exe;
they are not extracted or redistributed.

### 5.3 Page templates

The 65 built-in templates (resources/rcc_00d63c80/templates, copied to
client/src/assets/templates) are **JSON `.template` files, not images**: `constants`
(expressions), and `items` that are `group` (boundingBox + `repeat` rows/columns of
`down|up|right|left|infinite|<count>`), `path` (`M/L/C/Z` data, optional
stroke/fill colour, strokeWidth, antialiasing) or `text` (text, fontSize, position).
`templates.json` maps 79 names to files; 8 entries (Blank, Calligraphy, Hexagon) have no
`.template` file: Blank is plain paper, and the other seven 3.29 draws in C++
(SceneTemplateRenderer). Its type by file name (0x140a0e5b0): P Hexagon large/medium/small
1/2/3, P Calligraphy large/medium 4/5, LS Calligraphy large/medium 6/7; its setup
(0x140a0bf80) stores four doubles each (hexagons: horizontal pitch s 112.1/87.8/43.9, row
pitch P 194.4/151.8/75.9, x offset -102.1/-30/-8, y offset -147.4/-138/-55.2;
calligraphy: column pitch 72/45/72/45, row pitch 195/157.5/195/159, x offset
-50.5/-35/-105/0, y offset 65/99/61/64), and paint (0x140a0c740) sets QPen(Qt::black, 0.5,
SolidLine, SquareCap, BevelJoin). Both painters work in scene coordinates over the paper's
note bounds (0x1408a8df0) and the exposed rectangle, rows from max(0, int((top - oy) / P))
to int(int(bottom - oy) / P + 1), columns 0..int(int(boundsWidth - ox) / pitch).
Hexagons (0x140a0d340): per cell at x = c s + left + ox, y0 = r P + oy, the six edges
(x, y0)-(x + s/2, y0 + P/6), that point-(x + s/2, y0 + P/2), it-(x + s, y0),
(x, y0 + 2P/3)-(x + s/2, y0 + P/2), that-(x + s, y0 + 2P/3), it-(x + s, y0 + P): edges of
P/3, regular hexagons. Calligraphy (0x140a0cd40): gap 40.2 (P medium), 40.6 (LS medium),
else 48; per row three guides with the pen made cosmetic and dashed 13/17 at y0, y0 + gap,
y0 + 3 gap (from the left plus 0, 12 or 5), a solid baseline at y0 + 2 gap, then lines
from (x, y0 + 3 gap) to (x + pitch + slant, y0) per column (slant 12 for the medium ones,
which also step a pixel back every second column; 0 for the large). Client:
`viewer.templates.ProceduralTemplate` / `procedural_shapes` (`TemplateLibrary.resolve`; `get` stays
the `.template` files), drawn by `paint_template` and
`template_svg` (dashes as `stroke-dasharray` in pen widths, cosmetic as non-scaling
strokes). [O] Parser strings in the binary: the variables `templateWidth/Height`,
`parentX/Y/Width/Height`, `physicalDpi`, `pixelRatio`, `paperOriginX/Y`, `paperWidth/Height`,
`textWidth`; colours `#RRGGBB`; a path whose stroke and fill are both invisible is
illegal. The expression language is JS-like (`+ - * / ( ) < > ? : ||`).
Per-page template: `cPages.pages[].template.value` (e.g. "P Lines small"), or one name
per line in `.pagedata` for formatVersion 1 content.
Validated: all 65 render; real handwriting sits between the "P Lines small" rules (52 px).

Exports: `rm.export.{pdf,png,svg,html,bundle}`; PDF export draws strokes as PDFium path
objects (`FPDFPath_*`, `FPDFPageObj_SetStrokeColor/Width`, `FPDFAnnot_*` for highlights).

Linux client PDF export (`client/src/ui/export.py`) follows the same idea: the original
PDF pages go into the output untouched (vector, text selectable), with ink on top.
QPdfWriter draws the ink and highlighter layers. It cannot express blend modes, so pypdf
stamps each layer onto the page as a Form XObject, and the highlighter layer is painted
under `/BM /Darken`, the PDF equivalent of the viewer's `CompositionMode_Darken`. Pages
that grow past the PDF page get larger boxes, and the original content stays clipped to
its own crop box. `/Rotate` and offset crop boxes are handled. A PDF that pypdf cannot
read falls back to a 339 DPI raster background.

Verified on the real library:
- 41 PDFs (804 PDF pages): no word lost (pdftotext per page), no fallbacks.
- Exported pages rendered by PDFium match the viewer pixel for pixel, apart from stroke
  antialiasing.
- The 9-page annotated form went from 9.7 MB (raster) to 0.38 MB.

QtPdf leaves unpainted paper transparent (74 of 84 sampled pages), so
`PdfFile.render` flattens it onto white once.

The viewer draws the PDF image at its exact device-pixel rectangle. Scaling a whole-pixel
render into a fractional rectangle shifted part of the page by a pixel.

The public `rmscene` (Python) project documents the v6 block layout and is the
practical reference for a Linux parser; the strings above are consistent with it.

### 5.4 Page maps: notebooks vs PDF/EPUB (`.content` and `.rm`)

A notebook, a PDF and an EPUB are stored the same way: one `.content` page list and one
`.rm` v6 file per page, named after the page UUID (`<doc>/<page-uuid>.rm`). A PDF or EPUB
adds the original file and a PDF rendition (`<doc>.pdf`; for an EPUB the `.epub` too), and
each page that shows a PDF page says which one. Nothing in the `.rm` file says
"this is a PDF page", apart from one SceneInfo field described below.

Traced in reMarkable.exe 3.29 (`xochitl/src/entry/src/contentmetadata.cpp`,
`xochitl/src/entryfile/src/pagemap.cpp`) [O]. Checked on the cached library (343
documents: 230 notebooks, 108 PDFs, 5 EPUBs; counts only, read-only).

**`.content` keys** [O]. The parser (0x1406ce210) reads its key names from a table of
`QStringView {len, ptr}` entries at 0x140d25ea8–0x140d26068. Besides the keys listed in
§5 it also knows `lockedByPassword`, `viewBackgroundFilter` and `keyboardMetadata`.
`dummyDocument` (13 real PDFs/EPUBs, always `false`) is not in the table: the app does not
read it.

**formatVersion 2: `cPages`** (all 230 notebooks, 48 PDFs, 2 EPUBs). The writer is
0x140877e60, the reader 0x140872fb0. Key table at 0x140d540f8.

| Key | Form | Notebook | PDF / EPUB |
|---|---|---|---|
| `uuids` | `[{"first": "<author UUID>", "second": <index>}]`: the authors behind the `A` of every `"A:C"` timestamp | 1–4 authors | 1–3 authors |
| `original` | LWW int `{timestamp, value}`: page count of the PDF rendition | `-1` (230/230) | PDF page count (all 50; equal to the cached rendition's count on every cached one) |
| `lastOpened` | LWW string: page UUID | a page id (`""` in 4) | a page id |
| `pages[]` | `id` (UUID string), `idx` (LWW sort key), optional LWW `template`, `redir`, `deleted`, `verticalScroll`, `scrollTime`, plain `modifed` (sic, decimal ms string) | never `redir` (0 of 4677) | `redir` on PDF-backed pages (1489 of 1556 PDF entries, 118 of 118 EPUB entries) |

- A page entry is written by 0x140877a70. `id` and `idx` are always written. Each other
  register is written only once its timestamp counter is non-zero. So a page with no
  `redir` key never had one set: notebook pages, and pages inserted into a PDF (they have
  no background). `modifed` is written when its flag is set. Unknown page keys are kept
  verbatim (the reader 0x1408722f0 logs `Unknown page json key={}` and stores them for the
  writer). A bad `idx` is logged (`Page {} has invalid index {}`) and replaced.
- Merging is per register: the newer timestamp wins (0x140a985b0). The file lists the live
  pages in `idx` order first (0x140871430), then the deleted ones (0x140871390). This is
  why `pages[]` is not in `idx` order in 30 notebooks and 4 PDFs: those files have deleted
  pages. `deleted.value` is 1 (146 entries), or 0 for a page deleted and then put back (1).
- `redir.value` is the 0-based page of `<doc>.pdf`. On every PDF/EPUB the live `redir`
  values cover exactly 0…`original`−1. They do not have to be unique or increasing: in two
  PDFs every PDF page is listed 2–3 times, because copied pages keep their `redir`
  (`core/clone.py` and `edits.PageCopy` do the same). `pageCount` is a cached count: one of
  those two says 10 with 15 live pages. The live `cPages` entries are what counts: for a
  formatVersion 2 document 3.29's loader (0x1406669f0) sets `Document::pageCount` (+0x24c)
  to the size of its page vector and never reads the stored number; for formatVersion 1
  it takes the stored `pageCount` when it is at least 1 and grows the page list to it
  (0x14087aea0). `models.Content.page_count` does the same (§5.15).
- Pages inserted into a PDF (in 15 of 48 PDFs) carry ordinary templates (`Blank` 48,
  `P Lines small` 5, `P Grid bottom` 4, `Perspective2` 1). PDF-backed pages say `Blank`
  (1471) or have no template (18).
- `coverPageNumber`: `-1` on every notebook. On PDFs it is `0` (first page) in 99, `-1` in 9.
  On EPUBs it is `-1` in 4, `0` in 1.

**formatVersion 1: `pages` + `redirectionPageMap` + `originalPageCount`** (60 PDFs, 3
EPUBs; no notebook). `pages` is an array of page UUID strings. `redirectionPageMap[i]` is
the PDF page of `pages[i]`, `-1` for an inserted page. On real data every map has the same
length as `pages`, equals 0…n−1 and has no `-1`; `originalPageCount` equals n. Two PDFs
have `"pages": null`, no map, `originalPageCount` 0 and `pageCount` 0. They were uploaded
without a page list; the client names their pages `pdf-page-<n>`
(`ui.library.unlisted_page_id`).

The app upgrades version 1 when it loads a document (`fillContentFromDocument`,
0x1406cd640). The cloud copy stays version 1 until the document is written again:
1. The current format is 2 (0x1406cdad0). `formatVersion` is read by 0x1406cefc0
   (missing: caller's default; < 1: 1). If it is already 2, nothing happens.
2. The page ids come from `pages` (0x1406cf820). The map is used only if **both**
   `redirectionPageMap` and `originalPageCount` are present (0x1406cf100).
3. Reading the map (0x1406cf9f0): `originalPageCount` < 1 → warning *"Redirection map,
   but original page count is invalid"*, map dropped. Any value < −1 or ≥
   `originalPageCount` → *"Invalid page found in redirection map {} at {} - rejecting"*,
   **whole map** dropped.
4. `PageMap::initializePages` (0x140871670; log *"Initialize pages pagemap={}
   templates={} redirection={}, originalPageCount={} isUsingRedirectionMap={}"*) builds
   one entry per id. The template is the i-th template given (the `.pagedata` lines), else
   the default template. `redir` is `map[i]` when the map has as many entries as `pages`;
   otherwise it is `i` (*"Inconsistent pagemap vs redirection map count"*). With no usable
   map it is −1, i.e. no `redir` register [I: not traced into the entry constructor
   0x14086f560; consistent with the writer]. `original` is set to `originalPageCount`
   when > 0.
5. `cPages` is written, `formatVersion` becomes 2 and `pageCount` the number of pages.
   `originalPageCount`, `redirectionPageMap` and `pages` are removed.

*Client:* `core.models.Content.parse` follows steps 2–4: the map only with
`originalPageCount`, dropped whole when that is < 1 or any value is < −1 or ≥ it, `i` for every
page when the lengths differ, no background without a usable map. All 64 formatVersion 1
`.content` files of the cached library read the same as before the change.

The registers the upgrade writes [O, 0x1406cd640 → 0x140876c70 → 0x140871670 →
0x14086f560 → insertPage 0x140871e60]: the page map is made afresh for the loading
author; each page is **appended** with insert mode 0 (the normal mode, so every written
register is bumped once more by 0x1408715d0: `template`, `redir` and `idx` end at `A:2`,
keys `ba`, `bb`, … as `key_after` gives them); the template is the i-th `.pagedata` line,
else the default `Blank` (0x140817f50); `original` = `originalPageCount` when > 0, at
the author's next tick (`A:1`); `lastOpened` is not set (written as `{"0:0", ""}`, as on
12 cached files). Every cached cPages file has all four keys `lastOpened`, `original`,
`pages`, `uuids`; on 45 of 50 PDFs `original` is stamped `1:1`. No cached document has
both a formatVersion 1 and a 2 version, so the result is not compared with a real upgrade.
*Client:* `core.pagemap.upgrade` does this; `core.edits.current_content` applies it
whenever an edit writes `.content` (a page's `modifed`, a page added, pen settings),
which is when the app first writes the upgraded file. A document uploaded with
`"pages": null` (no page list) is not upgraded: it cannot be edited until a tablet has
made its page list.

**`.rm` v6 on PDF/EPUB pages** [O on real data]. A page only has a `.rm` once something was
put on it: 87 of 2325 live PDF-backed pages, 53 of 58 inserted pages, 1 of 125 EPUB pages,
4209 of 4540 notebook pages. Compared by page kind (notebook / PDF-backed / inserted into a
PDF / EPUB-backed):
- **Blocks: no difference.** The same block types with the same min/version: 0x00 1/1,
  0x01 1/1, 0x02 1/2, 0x04 1/1, 0x05 2/2, 0x07 1/1, 0x08 1/1, 0x09 1/1, 0x0A 0/1, 0x0D 0/1,
  0x0E 3/3, 0x0F 2/2. The same leading order (authors, 0x00, PageStats, SceneInfo,
  [images table], tree…). Block 0x00 is `{1: 1:1, 2: 1, 3: 0}` on every PDF/EPUB page and
  on all but 8 notebook pages. Text (0x07) and images (0x0E/0x0F) occur on both kinds.
- **SceneInfo (0x0D)** is read by 0x1409ef220. Its defaults come from the page loader
  (0x140a02fb0, `case 0xd`): current layer 0:0, two flags true, a null `QRectF`,
  `QSize(1404, 1872)`. On disk:

  | Field | Content | Notebook | PDF-backed |
  |---|---|---|---|
  | 1 | current layer (LWW id) | ✓ | ✓ |
  | 2, 3 | LWW bools (background visible, root text visible [I: SceneController `backgroundVisible`, `isRootDocumentVisible`]) | ✓ | ✓ |
  | 5 | paper size, 2 × u32 (rounded copy of field 8, §5.6) | 1404 × 1872 (all) | **1404 × 1872 (all)** |
  | 6 | LWW `QRectF` (4 × f64: x, y, w, h) | **0,0,0,0** (3034/3034) | **non-zero** (41/41 that have it) |
  | 7 | 2 × f64 (copy of field 8) | 1404.0 × 1872.0 | 1404.0 × 1872.0 |
  | 8 | LWW 2 × f64: the paper size (`Scene::setPaperSize`) | 1404.0 × 1872.0 | 1404.0 × 1872.0 |
  | 9 | LWW u8 (preferred text layout [I]) | 1 / 3 | 1 / 3 |

  The paper size does **not** become the PDF's size: an A4 page still says 1404 × 1872.
  Field 6 is the one difference. On PDF pages it is a square centred on x = 0, stamped
  `1:14` or `1:15` (once `1:23`) when the tablet first made the page:
  (−1717, −468, 3434, 3433) on US Letter (1921 × 2486 px),
  (−1764, −468, 3528, 3528) on 1869 × 2580, (−1796, −468, 3592, 3591) on A4 portrait,
  (−1796, −858, 3592, 3592) on A4 landscape, (−2075, −866, 4150, 4150) on 3199 × 2408,
  and (−7710, −468, 15420, 15420) / (−8609, −468, 17218, 17217) on 1397 × 14420 / 16209
  tall single-page PDFs. Its side is the PDF page's longer side + ~950 px. It looks like the scene
  area the PDF page opens up (`SceneController.sceneExteriorBoundary`/`paperPortraitBounds`
  are candidates) [?: name and exact formula not traced]. Pages inserted into a PDF carry
  zeros, like notebooks. 46 PDF pages and the one annotated EPUB page have no field 6 at all (older writers), so
  it is optional. The client writes field 6 only when it creates a page (`rm_writer.blank_page`:
  zeros); the paper fields (5, 7, 8) are traced and set as §5.6 describes.
- **Field 2 is "Hide template"** [O + corpus]: of 4,256 cached v6 pages, 4,253 have it at
  `0:0`, true; 3 carry a later timestamp (1:15, 1:18, 1:170) and are true again: the
  tablet saves the toggle in the page. The desktop QML's View > Hide template and the
  Layers foldout's Template (notebooks) / Background (PDF) row call
  `setBackgroundVisible`. Traced in §5.8 (`0x1409583c0` → `0x1408aa340`): it takes
  `++clock` for field 2's LWW timestamp and is an undoable action on `SceneActionStack`.
  The client reads the field (`PageScene.background_visible`), persists it (undoably) on
  edit-enabled pages (`SceneFlags`), and keeps the live toggle working on read-only pages.
- **Coordinates: same system, wider extent.** x is centred on the page (0 = middle),
  y = 0 at the top, in screen pixels. The PDF page fills [−W/2, W/2] × [0, H] with W, H =
  PDF size × 226/72 (§5.2). On the 46 annotated pages whose PDF is cached, 25 have ink
  beyond ±702 (the 1404 px screen) and 16 below y = 1872. None goes below the PDF's bottom.
  6 reach slightly past its side or top edge (≤ 8 % of W/2, margin scribbles). So the
  bounds follow the PDF page, not 1404 × 1872. Notebook pages also go past 1404 × 1872
  (182 of 4151 sideways, 55 downwards: scrolled or zoomed pages), so a reader must not
  clip either kind. EPUB pages behave like PDF pages: the `.pdf` rendition is the
  background.

**Editing PDF and EPUB pages (client).** The same editing as on notebook pages (pens,
straight lines, eraser, rectangle and lasso selection, move/scale/turn, clipboard,
images, undo/redo, the eraser taking text highlights (§5.11); typed text only on pages
inserted into the document, as 3.29 allows it, §5.11), gated like notebooks: Settings > Editing, or
`--edit-notebook NAME` (`LibrarySource.edit_refusal`: then one non-trashed notebook,
PDF or EPUB of that name; §3.7), and a PDF/EPUB needs its page list. Input goes through the same view ↔ scene transform as on
notebook pages; the scene bounds are the PDF page's frame united with the ink
(`ui/pages.py`), which `DocumentViewport` uses as `sceneBounds` for non-note pages (paste
clamping, scrolling), so nothing is clipped to 1404 × 1872.
- A page without a `.rm` gets `rm_writer.blank_page` (Layer 1, paper 1404 × 1872,
  PageStats loads 1, SceneInfo field 6 zeros at `0:0`: the null `QRectF` the loader
  defaults to, as on the 46 PDF pages without field 6) [I: the tablet's field 6 formula is
  not traced, so it is not made up here].
- An existing page keeps every block it does not change byte for byte, SceneInfo
  included. Glyph ranges (0x03, PDF text highlights) and text items (0x06) are items of
  their layer: their headers take part in the layer's sequence (left/right neighbours of
  new items), their values are kept unread. Unknown blocks (0x0B `Array`, or any other
  type) stay where they are; new items are inserted among the item blocks only. Ids are
  read from such blocks as far as they are tagged (top level, and sub-blocks shaped like
  LWW values); others cannot clash with new ids anyway, since those use this client's
  own author index. None of the 4,226 cached pages has a 0x03, 0x06 or 0x0B block, so
  this is checked on synthetic pages only. The eraser, Erase selection, Erase all and a
  layer's delete take glyph ranges as the app does (§5.11).
- `tools/pdf_edit_corpus.py` (read-only) makes every kind of edit on every cached PDF
  and EPUB page and works out each document's upload: 113 documents (96 worked out to an
  upload, 15 not wholly cached, 2 without a page list), 2,494 pages (141 with a `.rm`, 41
  of them with a SceneInfo field 6), 19,952 edits, 51 formatVersion 1 documents
  upgraded, 0 errors (2026-11-19).

### 5.5 Saving edits: `.metadata`, page `modifed` and PageStats

Traced in reMarkable.exe 3.29 [O unless marked]:
- `entryfile/src/metadatafile.cpp`;
- `Entry`/`Document` setters;
- `DocumentWorker`;
- the scene writer 0x140a094e0 and the scene loader 0x140a02fb0.

Checked read-only on the cache: 524 `.metadata`, 4,253 v6 pages, and 214 documents with
both `.content` and `.metadata` in one index. 30 `.metadata` names and 13 `.rm` names have
more than one cached version.

**`.metadata` keys.** The reader is 0x14084cf70, the writer 0x14084d720. Key strings are
at 0x140d53a60–0x140d53ab8; `convertLegacyFailed`, `lastOpened` and `lastOpenedPage` are
`QStringView`s at 0x140d539c8.

| Key | Field | Form | Read | Written |
|---|---|---|---|---|
| `type` | +0x60 | `"DocumentType"`, `"CollectionType"`, … (`Entry::Type {Invalid, Document, Collection, Template}`) | always | always |
| `lastModified` | +0x18 | decimal ms **string** (`QString::number`) | always (`toLongLong`) | always |
| `visibleName` | +0x30 | string | always | always |
| `createdTime` | +0x20 | decimal ms string | always | always |
| `pinned` | +0x28 | bool | always | always |
| `parent` | +0x48 | string (`""`, `"trash"`, UUID) | always | always |
| `new` | +0x64 | bool | always | always |
| `source` | +0x68 | string | always | always |
| `convertLegacyFailed` | +0x80 | bool | if `true` | documents, only when set |
| `lastOpened` | +0x88 | decimal ms string | if present, **else = `lastModified`** (0x14084cef0) | documents only |
| `lastOpenedPage` | +0x90 | int | if present | documents only |

- The writer starts from the whole object it read (kept at +0x10), so unknown keys pass
  through verbatim. That covers the legacy `deleted`, `metadatamodified`, `modified`,
  `synced` and `version`, found on 90 of 524 cached files. **3.29 never writes or bumps
  `version`.**
- Collections (2) and templates (3) stop after `source`: they get no `lastOpened` or
  `lastOpenedPage`. 46 of the 47 cached `CollectionType` files have neither. The one
  that has them also carries the legacy keys from an older writer. All 477 cached
  `DocumentType` files have both.
- Every setter (0x14084d460 `lastModified`, 0x14084d480 `lastOpened`, 0x14084d4a0
  `lastOpenedPage`, 0x14084d4c0 `parent`, 0x14084d520 `pinned`, 0x14084d5d0
  `visibleName`, 0x14084d5a0 `type`) compares the value first. It sets the file's dirty
  byte (+8) only when the value changes.

**Who moves which timestamp.** `Entry::setLastModified` (0x1406840c0) logs
"Setting lastModified of {} to {}" and emits `lastModifiedChanged` (Entry signal 11).
Every caller passes `QDateTime::currentMSecsSinceEpoch()`.

| Action | Code | `lastModified` | Other |
|---|---|---|---|
| Rename | 0x1406843c0 | now (if the name changed) | `visibleNameChanged` |
| Move / trash / restore | 0x140684270 | now (if `parent` changed) | `parentIdChanged` |
| Pin / unpin | 0x1406842c0 | now (if changed) | `isPinnedChanged` |
| Add / remove one tag | 0x140681650 / 0x1406819e0 | now | `entryTagsChanged` |
| setTags / setPageTags | 0x140683f80 / 0x140668730 | now | §5.4 tags |
| **Page saved** (`DocumentWorker::linesStored(page, pageId, size)` → slot 0x1406606b0) | | now | then that page's cPages **`modifed` = now** (a second clock read, 0x140876eb0; "Unknown page id {} when setting modified time"); then the upload timer (below) |
| Page operations (5 functor bodies 0x140645430, 0x140645570, 0x140655170, 0x140655990, 0x140659cb0; not named one by one) | | now | the same page's `modifed` = now, upload timer |
| **Page turn** (`Document::setCurrentPage`, 0x140667db0) | | **now** (unconditional once the page differs; asm 0x140667e2a–0x140667e3b) | The index is clamped to [0, pageCount − 1]. It does nothing if `lastOpenedPage` (and, for formatVersion 2, the cPages `lastOpened` register) already match. Otherwise: cPages `lastOpened` = page id, with timestamp = the document clock's next tick (0x140876db0); `lastOpenedPage` = index; `lastOpenedPageChanged` |
| Opened / created (0x140669d40; called from `createDocument` 0x1404dd3b0 and a queued functor) | | unchanged | `lastOpened` = now, `lastOpenedChanged` |
| `setOrientation`, `setCoverPageNumber`, `clearIsNew` (§5.6) | 0x140667f10, 0x1406676a0, 0x140684080 | **unchanged** | their own field only when it differs; stored and uploaded on unlock |
| Template / paper-size tasks finished (0x14068f440 → 0x14065aff0) | | now, **only if the page map changed** | §5.6 |
| Other callers (0x1404e0c10, 0x14065aff0, 0x140697340) | | now | not identified |

Corpus check:
- 117 of the 214 documents have a page `modifed` later than `lastModified`. The smallest
  gaps are 1–5 ms, which fits the two clock reads in `linesStored`: `lastModified` is read
  first, `modifed` second. 97 have `lastModified` ≥ every `modifed` (a later rename, move,
  tag or page turn).
- 87 cached `.metadata` versions, across 20 documents, differ from a sibling version
  only in `lastOpened` and/or `lastOpenedPage`. Their `lastModified`, name, parent and
  pin are the same (up to 19 versions of one notebook). The
  client that wrote those (tablet firmware or another app; the cache cannot tell) does
  **not** bump `lastModified` on a page turn. Desktop 3.29 does.
- This client's `SyncClient` (core/sync.py, "Reading is not editing") follows the
  observed files, not 3.29: a deliberate deviation.

**Upload after a save** [O; interval I].
- The `linesStored` slot calls 0x140683ab0 unless entry+0x51 is set or entry+0x2cf is
  clear (flags not named).
- 0x140683ab0 creates the entry's `Timer` "entryUploadTimer" once (`xofm/libs/timer`), logs
  "Starting upload timer for {}" and starts it. Its timeout is wired in 0x14067f8b0.
- The timer is built with interval argument 0, so it fires on the next event-loop pass and
  coalesces saves that land before it [I].
- The upload then follows §3.3 (blobs, docSchema, root, `PUT /sync/v3/root`).

**PageStats, block 0x0A.** The scene writer 0x140a094e0 emits blocks in this order:
authors 0x09, MigrationInfo 0x00, **PageStats 0x0A** (header 0x140a78ca0(…, 10, 1, 0)),
SceneInfo 0x0D. PageStats is written as tagged u32 fields:

| Field | Scene offset | Written |
|---|---|---|
| 1 `loads` | +0xc8 | **value + 1** |
| 2 `merges` | +0xcc | as is |
| 3 `textChars` | +0xd0 | as is |
| 4 `textLines` | +0xd4 | as is |
| 5 `keyboardCount` | +0xd8 | as is |
| tail | +0xe0..+0xe8 | unknown bytes, verbatim |

The writer does not store `loads + 1` back into the scene. So every save of one loaded
scene writes the same number: `loads` counts load→save generations, not saves.

The loader (0x140a02fb0, `case 10`):

```
if scene.loads == 0:  read PageStats (0x1409ee790: fields 1–5, each optional) into the scene
else:                 scene.merges += 1        # this file's own counters are dropped
```

So the first file loaded into a scene sets the counters. Every further PageStats block
merged into the same scene (a conflict `.rm.remote`, §3.5) adds one to `merges`. A brand
new page has `loads` 0 and is written with 1 (`rm_writer.blank_page` already writes
1, 0, 0, 0, 0).

Corpus check:
- All 4,253 pages have exactly fields 1–5 and no tail.
- `loads`: 1 on 3,134 pages, 2 on 718, 3 on 214, …, 14 at most. It is never 0.
- `merges`: 0 on 4,237 pages, 1 on 8, 2 on 8.
- Across the 13 multi-version `.rm` names, `loads` never falls. It rises by 1 or more
  per version (1→3, 2→4→5). Equal pairs (1, 1 and 2, 2) exist: two saves from one
  loaded scene, as the rule predicts.

**In this client.** `rm_text.edit_text` updates `textChars` and `textLines`. `core.edits.replay`
(one replay of a page's pending edits onto its file as last synced = one load→save session)
ends with `rm_writer.saved`: `loads` = the base file's + 1, once, however many edits the
session made; a page with no file yet keeps the 1 `blank_page` writes, and a replay that
changes nothing changes nothing.

### 5.6 New notebooks, Quick sheets and the library's document setters

Traced in reMarkable.exe 3.29 (`xofm/libs/library/src/librarycontroller.cpp`,
`documentcontroller`, `Document`/`Entry` setters, the `.content` writer 0x1406ce210)
[O unless marked]; checked read-only against the 232 notebooks of the cached library
(`client/tools/create_schema_check.py`, counts only). QML callers: `NamingDialog.qml`
(new notebook, `createNewAction` Ctrl+N), `LibraryHelpers.createNewQuicksheet`
(`createQuicksheetAction` Ctrl+O), `ContentEntriesBase.selectEntry` (`clearIsNew`). Both
actions are enabled only with `canEditDocuments` = `UserInfo.featureToggles.clientEdit`.

**Dispatch** (`LibraryController` moc 0x140c8e5d0, static metacall 0x140473ef0): 1–3
`createDocument(dir, visibleName[, uuid[, uuid_page]])` → 0x1404ddf10 → 0x1404dd3b0;
4–5 `createDocumentFromExisting` → 0x1404ddfc0; 7 `quickSheets()` → 0x1404e1610 (returns
a new `QmlDocumentWrapper*`); 17 `setOrientation` → 0x1404e26d0; 18
`setCoverPageNumber` → 0x1404e1b20; 26 `clearIsNew` → 0x1404dc5c0. `DocumentController`
(moc 0x140c8e550, metacall 0x140472e80): 1–2 `setTemplateForPage` → 0x140492f60; 20–21
`setPaperSizeForQuickSheetPage` → 0x140492cd0 → 0x140492a80.

**`createDocument`** (0x1404dd3b0), in order:
1. "Creating document … in …"; refuses when the library's volume has ≤ 512 MiB free
   (0x1404dff90: `> 0x20000000`, "Not enough free disk space, refusing to create new
   document"); the parent defaults to My files; it must lock and not be in the trash
   (0x140681860; "parent collection not locked.").
2. Id: a new QUuid (0x14084b930), or `uuid` when it parses ("uuid {:?} supplied, using
   it for document creation"). An existing id is refused ("refusing to create document
   with id {} that already exists").
3. `new Document` (0x840 bytes, ctor 0x14064f3c0). Defaults written by the `.content`
   writer: `coverPageNumber` −1 (+0x250), `formatVersion` 1 → 2 by "ensure correct format
   version" (0x14065e7e0), `textAlignment` justify (+0x278 = 8; written only for 8 or 1),
   `textScale` 1.0 (+0x280), `lineHeight` **100** (+0x288), `margins` **not written**
   (optional, +0x298 false), orientation 2 = portrait (+0x2a8), `zoomMode` 0 = bestFit
   (+0x2ac), `sizeInBytes` `QString::number(+0x240 = 0)` = `"0"`, `fontName` "",
   `documentMetadata`/`extraMetadata` `{}`; `viewBackgroundFilter` and the key after the
   table (+0x258 object, likely `keyboardMetadata`) only when non-empty;
   `lockedByPassword` only when set. `customZoom*` [O] are the Document constructor's
   (0x14064f68a: QRectF(−702, 0, 1404, 1872) → centre (0, 936) at +0x410, size
   1404 × 1872 at +0x420, scale 1.0 at +0x430, orientation 2 = portrait at +0x438), fixed
   whatever the paper size; only `setCustomZoomFit` (0x1406676c0) and the loader
   (0x1406672ac) write them. On all 232 cached notebooks: CenterX 0, CenterY 936,
   1404 × 1872, scale 1, portrait.
4. `setVisibleName` (lastModified now), `setFileType(Notebook)` (0x140667aa0).
5. Page: id a new QUuid or `uuid_page` ("uuid {:?} supplied, using it for page
   creation"); 0x140668010 gives one page with the template list resized to one
   **empty** name and no redirection; `initializePages` (0x140871670) inserts it in the
   normal mode: the empty template writes no register, so the entry is `id` + `idx`
   ("ba" at `A:2`); fileType Notebook → no `redir`; `original` stays −1 at `0:0`. cPages
   `lastOpened` is then set from `.metadata` `lastOpenedPage` (0x140876db0), which is
   −1 on a new entry (0x14067fef7) and is rejected: `{"0:0", ""}`.
6. `setParent` (lastModified now), "opened" 0x140669d40 (`lastOpened` = now), added to the
   folder (0x140695a60) and the library (0x14048b160), entry+0x51 set (store and upload
   on unlock, 0x140684b60 → 0x140684790). No `.rm` and no `.pagedata` are written.

`.metadata`: `createdTime`, `lastModified`, `lastOpened` now; `lastOpenedPage` −1;
`new` false; `parent`; `pinned` false; `source` ""; `type`; `visibleName` (the writer's
key set for documents, §5.5).

The dialog then calls `setOrientation(id, Qt.Vertical)` and `setCoverPageNumber(id, −1|0)`
(no-ops on the defaults unless "First page" was chosen), and
`DocumentController.setTemplateForPage(id, lastOpenedPage, "Blank", Settings.paperSize)`,
then opens the notebook in text mode. The template write gives the page `template`
"Blank" at `A:1`. The result is the shape the tablet's own new notebooks have: of the 19
cached one-page notebooks whose page was never reordered, 18 have `idx` "ba" at `A:2` and
`template` at `A:1` (the 19th `idx` at `A:1`).

**`setTemplateForPage`** (0x140492f60 → 0x140492d60): the paper size argument is a
`QJSValue`; when it holds a `QSizeF` that is empty the call is refused ("invalid paper size
when setting template"). A task (0x140690dc0, run 0x14068f060) sets the page scene's
paper size (functor → `Scene::setPaperSize` 0x1408aa520, which returns at once when the
size is the same) and the template; when it finishes (0x14068f440) 0x14065aff0 compares
the page map with the pages (0x140669620, 0x140874970): **only changed registers are
written**, and `lastModified` = now only if something changed.

**`Settings.paperSize`** (getter 0x14038bce0, key index 0x2a): the stored size when it is
one of 1404 × 1872, 1620 × 2160, 954 × 1696 (table 0x14102b900), else **1404 × 1872**.

**Quick sheets.** No marker exists. `Document::isQuickSheet` (0x140660b20):
`visibleName == tr("Quick sheets")` (context `Document`, 0x140664e70), not trashed
(entry+0x70, the `isTrashed` getter of `QmlEntryWrapper`), `fileType == Notebook`.
`Library` 0x1404d50c0 walks all entries and keeps the **smallest id** (QByteArray
compare) among those. `quickSheets()` returns that one, else
`createDocument(My files, tr("Quick sheets"))` with nothing else set. Ctrl+O
(`createNewQuicksheet`): if the last page has content, `addPageWithTemplateAndPaperSizeFromPage
(id, −1, "Blank", last)`, else, for a one-page Quick sheets,
`setTemplateForPage(id, 0, templateForPage(0), Settings.paperSize)`; then
`setLastOpenedPage(last)`, `setTemplateForPage(last, "Blank")` and open in text mode.
`setPaperSizeForQuickSheetPage(id, size[, callback])` (no QML caller on the desktop):
null document → "setPaperSizeForQuickSheetPage passed a null document"; not a Quick sheets
or ≥ 2 pages → "Changing paper size is only allowed for Quick sheets that has one page.";
else a task (0x140690e80, run 0x14068f1c0) sets page 0's scene paper size, then the
callback.

**`createDocumentFromExisting`** (0x1404ddfc0, "Creating document {} in {} from pages
{} in {}"): the same checks, a new Document (name, Notebook, parent; **no page of its
own, not marked opened**), then three tasks on the new document's worker, the second
(0x14068ffb0) moving the pages from the source. The desktop QML calls it only on mobile.

**Setters.** `setOrientation` → `Document` 0x140667f10: only when it differs; deletes
`<id>.thumbnails` (0x14065cc40 flag 1); `orientationChanged`. Read: "landscape" → 1
(`Qt::Horizontal`), anything else 2 (0x1406cf330); written: 1 → "landscape", else
"portrait" (0x1406cf420). `setCoverPageNumber` → 0x1406676a0 (+0x250, signal 5).
`clearIsNew` → only when `isNew`: `Entry::setNew(false)` (0x140684080, metadata setter
0x14084d440), `isNewChanged`. None of the three calls `setLastModified`.

*Client* (`core/create.py`, `LibrarySource.create_notebook`/`create_from_pages`/
`set_page_template`, `LibraryController`, `DocumentController`):
- A new notebook or Quick sheets is made in memory and shown at once (`view_tree`),
  marked editable (with the copies, `editable-copies.json`), and uploaded on the next
  event-loop pass through the Mutator (`SyncClient.create_document`, like an import),
  so the dialog's follow-up calls land in its files before the upload, as the app's entry
  upload timer coalesces them. A failed upload removes it. Refused offline (this client
  keeps no local-only documents) and unless editing is turned on; `clientEdit` is on in
  the library whenever editing is turned on (Ctrl+N, Ctrl+O), so the app's editing
  tutorial shows the first time a notebook opens, as in the official app.
- `setTemplateForPage` writes the template into an un-uploaded new notebook's `.content`,
  otherwise it is a pending page edit (`edits.PageTemplate`, uploaded by push_edits);
  a template the page already has changes nothing. With a paper size, the page gets it
  as below (*Paper sizes*); `setPaperSizeForQuickSheetPage` follows the checks above
  and sets page 0's paper the same way.
- `setOrientation`, `setCoverPageNumber`, `clearIsNew` are shown at once
  (`LibrarySource.show_fields`) and committed (`set_document_settings`, `clear_new`) with
  `lastModified` untouched; orientation also drops the `.thumbnails` files.
- `createDocumentFromExisting`: new page ids (not traced whether the move keeps them),
  each page's `.rm` (with pending edits), images and page files copied, the pages deleted
  from the source as a pending `PageDelete`; note pages only (a PDF page is refused).
- Deviation, unchanged: the client does not bump `lastModified` on a page turn (§5.5).

**Paper sizes** (Settings › "Document paper width": `Settings.paperSize`, table
0x14102b900: 1404 × 1872, 1620 × 2160, 954 × 1696) [O; byte layout from the writer,
since no cached page has another size]:
- *Where a page keeps it.* `Scene` holds the SceneInfo struct at +0xf8 (the reader
  0x1409ef220 and writer 0x1409f92f0 use the same offsets: field 2 at +0x110, field 6 at
  +0x130, the paper at +0x158/+0x160 with its stamp at +0x168). The paper size is
  **field 8**, an LWW pair of f64. The writer derives two more fields from it: **field 5**
  = u32 `round(w)`, u32 `round(h)` (CRT `round`, halves away from zero) and **field 7** =
  f64 w, f64 h; field 4 is never written; unknown trailing fields are kept (+0x88).
  Order on disk: 1, 2, 3, 5, 6, 7, 8, 9.
- *How it is read.* The page loader starts from QSize(1404, 1872) at 0:0 (0x140a04225).
  After reading, when field 8's stamp has counter 0 (absent or 0:0), field 7 is taken if
  both are > 0, else field 5 if both are ≥ 1 (signed), else the default stays.
- *How it is set.* `Scene::setPaperSize` (0x1408aa520): nothing when both sides are
  qFuzzyCompare-equal; else field 8 = the size, stamped `++clock` (0x1408e43e0), then
  `primaryContentRect`/`extendedContentRect` (unless the page has a content rect of
  its own, +0x320: PDF pages), `explicitPaperSizeChanged`, `paperPortraitBoundsChanged`,
  `paperNoteBoundsChanged`. Callers: the template task (functor 0x14068d6e0), the Quick
  sheets task (0x14068d6b0), doAddPage (0x140559584: the paper of page
  `paperSizeFromPage` when that is a page, else the explicit `QSizeF` when valid) and two
  more functors (0x140688b50, 0x14068c640). These go through the scene worker
  (request 0x18, 0x14056dd20), which saves the scene, so a page set to a size other than
  1404 × 1872 gets a `.rm` (an empty page then: on a page made from nothing, whose last id
  is 0:14, field 8 is stamped 1:15). Setting 1404 × 1872 on a page without a file writes
  nothing. The page map is not changed, so `lastModified` does not move for it.
- *What follows from it.* `Scene` 0x1408a8eb0 `paperPortraitBounds` = QRectF(−w/2, 0, w,
  h); 0x1408a8df0 `paperNoteBounds` = QRectF(−h/2, 0, h, w) (the "landscape" bounds the
  desktop's `DocumentViewport.updateSceneBoundingRect` gives every note page: x ±h/2,
  bottom at least 4/3 · h; fit-to-width uses them); both are (−702…)/(−936…) for an empty
  size. When a scene is shown (0x14044a4d0) these and `explicitPaperSize` are announced from
  the page's paper, on PDF pages too (their SceneInfo keeps 1404 × 1872; the PDF page
  is the content rect +0x300, not the paper).
- *Templates.* `SceneTemplateRenderer` (0x140a0de10) builds the template context from the
  page's paper (transposed when the document is landscape, +0xa8 == 1), its
  `paperNoteBounds` and physicalDpi 226 (0x140d6f670); the variable resolver 0x140a8ebd0
  (names table 0x140d72ef0, jump table 0x140a8ee7c) gives `templateWidth`/`Height` = the
  paper, `paperOriginX` = templateWidth/2 + bounds.x (= w/2 − h/2: −234 on 1404 × 1872),
  `paperOriginY` = bounds.y = 0, `paperWidth`/`Height` = the bounds' width and height (h
  and w), `pixelRatio` = physicalDpi / 227, `textWidth` from a text size when set, and
  `parent*` from the group being laid out. Template x = 0 is the paper's left edge
  (scene −w/2). "P Lines small" thus spans w/2 ± h/2 on any paper.
- *Typed text does not follow the paper.* `createRootDocument` (Scene 0x1408a55f0)
  sizes a new text box from a fixed QSize(1404, 1872): width h/2 = 936 (740 or 1152 for
  the other device modes, +0x330), at x −width/2, y 234 (0x140d654c0).

*Client* (`rm_parser.paper_size_of`, `rm_layer.set_scene_info(paper=)`, `paper_size`,
`same_paper`; `LibrarySource.set_page_paper`/`page_paper`; `SceneFlags.paper_size`):
- `PageScene.width`/`height` are the page's paper as the loader resolves it and
  `x_origin` is width/2; page coordinates put the paper's top left at (0, 0). Frames
  (`PageContent.frame`), thumbnails, the viewer's tiles, PNG/SVG/PDF export, PDF
  placement (`pdf_target_rect(size, paper)`), the template variables
  (`templates.base_environment`, as the resolver binds them: this also moves
  `paperOriginX` from w/2 to w/2 − h/2, a 1 px shift of "P Dots S" and 1.6 px of
  "Isometric" on 1404 × 1872), typed-text search hits, checkboxes and PDF links use the
  page's own origin. `SceneController` publishes `explicitPaperSize`, `paperPortraitBounds`
  and `paperNoteBounds` from the paper (so `DocumentViewport` scrolls and fits note pages
  in their landscape bounds as 3.29 does); `primaryContentRect` stays the frame.
- `setTemplateForPage(..., paperSize)` sets the paper first, then the template;
  `addPageWithTemplateAndPageSize` and `addPageWithTemplateAndPaperSizeFromPage` give
  the new page its size as doAddPage does; `setPaperSizeForQuickSheetPage` sets page 0's.
  A document made here and not uploaded yet gets the page's `.rm` in its files; another
  one gets a pending `SceneFlags` edit (not an undo step) that `push_edits` applies with
  `rm_layer.set_scene_info`, keeping every other block byte for byte. Push plans now
  describe `SceneFlags` and layer edits (describing one used to fail).
- `customZoom*` in a new notebook's `.content` stay 1404 × 1872 on any paper: they are
  the Document constructor's (§5.6 step 3) [O]; the client writes the same.

**Landscape notebooks** [O]. Nothing is turned: a landscape document's pages are drawn
in the same scene coordinates, and the page simply lies across.
- *Orientation.* `.content` "orientation" is read by 0x1406cf330 ("landscape" →
  Qt::Horizontal 1, anything else Qt::Vertical 2) and written by 0x1406cf420; the same
  reader serves a template's "orientation".
- *The page.* The thumbnail job (0x14055d170) asks 0x1405633f0 whether a page is
  landscape: for a document with PDF page sizes, whether that page is wider than tall;
  otherwise whether the document's orientation (+0x39c) is Horizontal. For a landscape
  page it transposes the thumbnail size and renders the scene rect `paperNoteBounds`
  (−h/2, 0, h, w) (or the PDF content rect when there is one); a portrait note page renders
  `paperPortraitBounds` (−w/2, 0, w, h). On the default paper a landscape page is
  therefore scene x −936…936, y 0…1404. The desktop QML follows: `PageGridItem` makes
  landscape cells wide (`height = width / aspect`), and `DocumentViewport.onUpdated` puts
  the focal point at `primaryContentRect.height / 2` instead of the default paper's.
- *Templates.* `SceneTemplateRenderer` (0x140a0de10) transposes the paper size when the
  *template's* orientation is 1 (+0xa8, from the template's own JSON: every "LS …"
  template says "landscape") and binds `paperNoteBounds` as for any template, so an LS
  template on 1404 × 1872 has templateWidth 1872, templateHeight 1404, paperOriginX
  1872/2 − 936 = 0, paperWidth 1872, paperHeight 1404. Its paint (0x140a0c740, case 6:
  JSON templates) translates by −templateWidth/2, so template x 0 is scene −936: the
  template covers the note bounds exactly.
- *Client.* `PageContent.landscape` (`ui.pages.is_landscape_document`) makes the frame of
  a page without a PDF page the note bounds, in page coordinates (w/2 − h/2, 0, h, w) =
  (−234, 0, 1872, 1404); everything that uses the frame follows: the viewer
  (`primaryContentRect`), thumbnails (`ThumbnailService.VERSION` 3), PDF/PNG/SVG export,
  ink bounds. `viewer.templates.template_size/template_left/template_environment` lay an
  LS template out on the transposed paper from page x w/2 − templateWidth/2 (`parent*`
  stay template coordinates, as the app's translated painter has them; no shipped
  template reads them). The tablet stores landscape strokes in these same scene
  coordinates, so they need nothing. `tests/test_v1_finish_offline.py` (Landscape*): the
  template variables, a lines template across the page, strokes across a wide page, wide
  thumbnails and exports, a landscape notebook opened through main.qml. The cache has no
  landscape notebook (3 landscape PDFs, whose pages are their PDF pages).
- Checked: `tests/test_paper_sizes_offline.py`; a read-only scan of the cache
  (4,282 distinct v6 pages, `.rm` files and blobs): fields 5/7/8 are 1404 × 1872 on every
  page that has them, field 8 always at 0:0; 498 pages stop after field 3, 695 after
  field 5. No page has another size, so the layout above rests on the writer's code.

### 5.7 In-document search and tables of contents

Traced in reMarkable.exe 3.29 (`xochitl/src/search/src/documentsearchcontroller.cpp`,
log category `rm.search`; moc `DocumentSearchController` 0x140cfcc50, static metacall
0x1405f8230; `PageSearchResultOverlayItem` 0x140cfcc10, metacall 0x1405f7d00, vtable
0x140cfd2c0) [O]. Client: `client/src/ui/docsearch.py` (finding), `bridge/scene.py`
(`_SearchController`, `_SearchOverlay`), `bridge/library.py`
(`LibraryController.loadTableOfContents`).

**QML.** `SearchResultsHeader.qml` (in `DocumentView.qml`; Ctrl+F toggles it, Escape
hides it) owns a `DocumentSearchController` bound to `document` and
`currentPageNumber: documentViewport.currentPage`. Typing cancels and, with "Page
only" ticked (its default), searches `documentView.currentPage` at once; Return in the
field (`onAccepted`) searches; the `Return` / `Shift+Return` shortcuts (enabled once
`totalResults > 0`) call `incrementCurrentResultIndex` / `decrementCurrentResultIndex`;
the label is `currentResultIndex + 1 / totalResults`; the progress bar is
`lastSearchedPage / pageCount`; `openPage(n)` → `documentViewport.setCurrentPage(n)`;
`resultLocationChanged(r)` → `tileManager.scrollToMakeSceneRectangleVisible(r, 0, 100,
0, 100)`. The overlay is `PageSearchResultOverlayItem { visibleResults:
filteredResults; activeResultIndex: filteredResultIndex; tileManager;
highlightColor: Colors.searchHighlightBlue (#4034A7FF); activeResultColor:
Colors.searchActiveBlue (#8035A7FF); highlightMode: ColorHighlights; anchors.fill:
parent }` inside `DocumentViewport` (so in device pixels on HiDPI screens, §2.1).

**Types** (moc + RTTI strings): `PageSearchResult {page, metrics:
QList<PageSearchResultMetrics>}` (0x28 bytes), `PageSearchResultMetrics {boundingRect:
QRectF, baseline}` (0x28 bytes); `HighlightDetails {hasDetails, currentSearchText,
hasHandwritingOnPage, pageNumber, hasDigitalOnPage}`; `DocumentChapter {name, path,
page, level}`.

**Controller state** (object offsets): `+0x80` currentResultIndex (global, counted in
page order over a `std::map<int page, vector<PageSearchResult>>` at `+0xc0`), `+0x88`
filteredResultIndex, `+0x90` filteredResults, `+0xa8` currentPageNumber, `+0xac`
running, `+0xb0` lastSearchedPage, `+0xb8` the search's start page, `+0xd8`
totalResults, `+0xe0`/`+0xe8` cursor (global index, placed flag).
- `hasNextResult` = `index + 1 < total`; `hasPreviousResult` = `index > 0`.
- increment (case 0xd): `index + 1 < total` → +1; else if running → nothing; else 0.
  decrement (0xe): `index < 1` → if running nothing, else `total - 1`; else −1. Both
  emit `currentResultIndexChanged`, which shows the hit (`focusOnActiveResult`
  0x1405f4cb0): `getPageAndResultIndex` (0x1405f54e0/0x1405f3fd0, walks pages 0..max)
  → filteredResults = that page's hits, filteredResultIndex = index on it; a page
  other than `+0xa8` is stored there and `openPage` emitted (not
  `currentPageNumberChanged`); then `resultLocationChanged(resultLocation())`.
- `resultLocation()` (0x1405f6600) = `filteredResults[filteredResultIndex].metrics[0]
  .boundingRect`, an empty QRectF when there is none.
- setting `currentPageNumber` (0x1405f4850): filteredResults = that page's hits,
  filteredResultIndex = the active one if it is there, else −1.
- `search(term, page = 0, pageOnly = false)` (0x1405f6710): logs "Search: {} for page:
  {} in doc {} {}"; no document → "Can't search without a documentWrapper"; empty
  term, or only white space → nothing. Otherwise the start page is stored and a
  worker searches (0x1405f68e0 → 0x1405f70e0: results arrive per page through
  `acceptSearchResults` 0x1405f36d0, which stores them, places the cursor when the
  start page arrives — first hit of the last page ≤ start page with any, else 0
  (0x1405f3a90) — shows it (`focusOnPageOfInterest` 0x1405f4ff0), emits
  `totalResultsChanged` and `lastSearchedPageChanged(page)`; the finish handler
  0x1405f4ba0 places the cursor if that never happened).
- `cancel()` (0x1405f3d70): drops all results, total 0, filteredResults empty,
  currentPageNumber −1, lastSearchedPage −1, currentResultIndex 0,
  filteredResultIndex −1, the worker released. Setting another `document` cancels.
- `forwardHighlightDetails(page, details)` (0x1405f51b0): a valid details' text is
  searched on that page only. Neither it nor `forwardLibrarySearcher` is called by the
  desktop QML.

**Overlay paint** (0x1405f9d70): for every metric of every visible result, `rect =
tileManager.sceneToView(boundingRect)` (0x1403fd740) `.toRect()`; the page's tile
image under it is drawn first (SourceOver, 0x1403fca80), then ColorHighlights fills
with `activeResultColor` for `i == activeResultIndex` else `highlightColor` in
CompositionMode_Darken (16); InvertAndGrayHighlights fills the active one with white in
Difference (22) and the others with dark gray in Darken. `setTileManager` (0x1405fa120)
connects the tile manager's `transformChanged` to `updateIfVisible` (0x1405fa400: `update()`
if visible). `setVisibleResults` (0x1405fa300) repaints only when the list changed.

**Where the hits come from.** `PdfRenderer::search` (PDFium, run out of process) for
PDF pages and the EPUB's PDF; `searchPageScene` / `accumulateTypedHighlights` for typed
text (glyph metrics of the text layout); handwriting through the library's
`LibrarySearcher` (hws). The client: QtPdf (PDFium too) `getAllText` and
`getSelectionAtIndex(...).bounds()` per character, merged per line, at 226/72 px per
point with the page centred on x = 0 (§5.2, §5.4); typed text through
`renderer.layout_text` with the app's fonts (the caret's x at both ends of the hit on
each line, so the box is the editor's selection box); case-insensitive (casefold),
white space runs alike. Each document page is searched through its own `redir`
(`PageRef.pdf_page`): inserted pages have no PDF text, moved and duplicated pages
theirs. Corpus (`client/tools/search_toc_corpus.py`, read-only): PDFium boxes are tight
to the outlines, the rendered glyphs' anti-aliasing reaches ≤ 0.4 pt past them (as in
the app, which uses the same boxes); some PDFs give zero-height or zero-width boxes
(made 1 pt thick here) and the tablet's EPUB renditions set running text above the
page's top (not found here: it is never seen).

**Deviations.** The app re-shows the cursor's hit each time another page's results
arrive while it searches (`focusOnPageOfInterest` with the flag set), pulling the view
back if one has stepped on; the client shows it once. The page's tile image is not
redrawn under the boxes: the overlay draws over the page instead (same tint on white
paper). Handwriting is not searched.

**Handwriting search (hws): traced, not implemented** [O]. What 3.29 has, and why the
client cannot search handwriting:
- *`.searchablepages`.* The only reference to the string (0x140d02fb8) is
  0x14065d4f0, which builds `<document base path>.searchablepages` (0x14065eb70 +
  suffix, i.e. `<uuid>.searchablepages`) and removes it through the app's filesystem
  interface (vtable +0x08 exists, +0x60 remove); its one caller is the document-delete
  routine 0x14065cde0, which logs it as "searchable pages" beside the page data,
  thumbnails and other per-document files. **No code in reMarkable.exe 3.29 reads,
  parses or writes the file**, and nothing builds a handwriting index (no recogniser
  for search, no index database, no search endpoint: §5.12's recogniser only converts).
  The format is therefore not traceable from this binary.
- *Cache* (read-only, 2026-11-21): none of the 392 cached document indexes lists a
  `.searchablepages` file or any other search/word-index entry (suffixes: `.rm` 4,699,
  `.metadata` 392, `.content` 367, `.pagedata` 118, `.pdf` 112, `.epub` 5), so the
  cloud does not carry it either: it is presumably made and kept on a tablet that has
  handwriting search [I]. No public description of it was found.
- *The search side that exists.* `DocumentSearchController` (`documentsearchcontroller.cpp`)
  runs "searchMixed" — "Doing hws inside doc search {} from page {} there is {}
  documentSearcher" — only when a `LibrarySearcher` was given to
  `forwardLibrarySearcher` (slot 20). `LibrarySearcher` is a QObject with no members of
  its own (moc 0x140e9c430, smc 0x140ba4f60); the desktop QML never calls
  `forwardLibrarySearcher`, so on the desktop hws never runs. Its results are
  `ContentSearchResults{order, origSearchStr, docWordHits, details}` and per page
  `PageSearchDetails(pageNumber, wordStrokes, typedSpans)` (`pagesearchdetails.cpp`,
  0x140508960). `typedSpans` come from `tokenSpansToWordSpans` (0x140509370): the page
  text split on `\s+`, spans of (first token, token count) turned into character spans.
  `wordStrokes` is a vector of `xofm::libs::search::WordStrokes`, each a list of scene
  item ids (CrdtIds: `extractAllWordStrokes` 0x1408158a0 pushes 16-byte ids through its
  two lambdas 0x140814f10/0x140814f30).
- *Highlights.* `searchAndHighlightPages` (0x1405eeac0) → `emitWordStrokesFromScene`
  (0x1405eb980; "Unable to get a Scene lock, skipping word stroke highlighting on page
  {}") → `accumulateHandwrittenHighlights` (0x1405e9be0): for each WordStrokes the
  scene is walked (0x1405f7640) with the lambda 0x1405f2a00: an item whose id is in the
  set contributes its bounding rect (0x140b3d760) translated by its group's offset
  (+0x1f0, the anchor position for text-anchored groups); the rects of one word are
  united into one highlight, kept with `y + height · 4/3` (0x140c8d528 / 0x140c8d420) of
  the last item as its key. A word none of whose ids is on the page logs "didn't find
  scene nodes corresponding to wordStrokes {}" and gives nothing.
- *`SearchIndexUpdated`.* `Notifications::onNotificationReceived` (0x1402e9270)
  compares the message type with "SearchIndexUpdated" (0x14101cae8) and emits signal 6
  `searchIndexUpdated()` (0x1402ef3a0, no arguments); other unknown types log "Received
  an unknown notification from server". Nothing connects to signal 6 (its only
  references are the emitter and the moc dispatcher; no desktop QML handler).
- *Client:* unchanged (handwriting is not searched; a `SearchIndexUpdated` message is
  one more notification the live-sync listener, §3.6, ignores). Building it needs the
  file's real bytes from a tablet that has handwriting search: the match and highlight
  rules above would then apply as traced.

**Tables of contents.** `LibraryController.loadTableOfContents(entry::Id)` (moc slot;
the document worker's `LoadTableOfContents` job, `requestLoadTableOfContents`
0x14056c7d0 → `doLoadTableOfContents` 0x14055e090, `LoadToCTask`) ends in the
document's `tableOfContentsUpdated(QList<DocumentChapter>)`, with `tocRunningChanged`
around it. `PdfRenderer::chapters` reads the outline (out of process); `EpubRenderer`
reads the book's navigation (its metadata dump logs "[%d]: title='%s', target='%s',
level=%d") and `xofm::libs::epub::ChapterPositionTracker` (QML element, `toc`,
`pageCount`) places chapters on its own layout. No desktop QML calls
`loadTableOfContents` or shows the list: there is no table of contents panel in
reMarkable.exe 3.29's QML. The client answers it all the same: the PDF's outline
(QPdfBookmarkModel, depth first, `level` from 0 — the app's base level is not traced),
each PDF page mapped to the first document page showing it, else the first showing a
later PDF page (a deleted chapter page lands on what follows), else −1; for an EPUB
without an outline in its rendition, the EPUB 3 `nav` (`epub:type="toc"`) or the EPUB 2
NCX, `path` = the entry's target in the book, each entry placed on the first page at or
after the previous entry's whose rendition text holds its title (the client has no EPUB
layout engine), else on the previous entry's page. The cached library: 6 of 75 PDFs and
EPUBs with a cached rendition have an outline (111 entries); no cached EPUB has its
`.epub` blob (fetched on demand), and their renditions have no outline.

**Contents panel (client-only, injected).** Since 3.29's QML has no panel, the client
adds one beside the official DocumentView without changing a QML file, as it adds the
pen toolbar (§2.1, `bridge/pen.py`) and Settings > Editing (§3.7): `bridge/toc.py`
`attach(view)` runs from `SceneView.componentComplete` and on every `document` change,
next to `pen.attach`; per `DocumentViewport` it makes a `_Toc` (child of the viewport's
QObject, so it goes with it) holding a `TocModel` and a QML item built from a Python
string (`qrc:/linux/TocSidebar.qml`, its own `QQmlContext` with `tocModel` and
`viewport`, parented to the viewport's container, z 55; the pen toolbar is at z 50 on
the left, so the panel sits on the right). `QuickApp` hands it the app's
`LibraryController` (`toc.set_controller`). For a PDF or EPUB the model asks
`loadTableOfContents(id)` once per document opened and again when the document's page
list changes (`pageMapChanged`: inserted, moved or deleted pages move the entries), and
listens to `tableOfContentsUpdated` / `tocRunningChanged` through `@Slot`s on the model
(disconnected when another document is shown, and by Qt when the viewport goes). A
notebook, or a document whose answer is empty, shows nothing. On screen: a 40 px button
with the app's own `qrc:/ark/icons/list` icon at the container's top right (tooltip
"Contents"), and when open a panel along the right edge (≤ 360 px, 45 % of the width):
"Contents", then one row per entry, indented 20 px per level, its page number counted
from 1 at the right; an entry the backend could not place (page −1) is grey and not
clickable. A click on an entry turns the page as the app turns pages
(`DocumentViewport.setCurrentPage(page)`); the panel stays open. The entry being read
(the last one placed at or before `viewport.currentPage`, bound in QML) is marked,
however the page was turned. Ctrl+Shift+T or the button opens and closes it; Escape
closes it first (a `Shortcut` enabled only while it is open, so DocumentView's own
Escape, which closes the document, waits for the next press). The panel takes the
clicks and wheel turns over it. Tests: `tests/test_toc_sidebar_offline.py` (the model's
current-entry rule; through the app's window: the button, a PDF outline with a page
inserted before Chapter 2, an EPUB's navigation, jumps, the current entry following a
page turned otherwise, Escape, Ctrl+Shift+T, no button for a notebook or a PDF without
an outline, entries placed again after a page is added before Chapter 1).

### 5.8 Layers and SceneInfo visibility

Traced in reMarkable.exe 3.29 [O] from `SceneController::qt_static_metacall`
(0x1403c99b0, slots 65–79), `SceneAction` subclasses, `SceneTree` (0x1408de730–0x1408e080f),
and `SceneLayersModel::buildItems` (0x14045bbc0); verified read-only across all 4,276
cached v6 pages (44 multi-layer / reordered pages, max 4 live layers, 0 errors).

**SceneTreeMove (0x01) and live layers.** Layers are `SceneTreeNode` (0x02) blocks
attached under `ROOT_NODE` (`0:1`) and ordered by `CrdtGroupItem` (0x04) blocks in
`ROOT_NODE`'s CRDT sequence. Deleting a layer (or detaching an emptied anchored group)
does **not** delete its `CrdtGroupItem` or its strokes; instead, `SceneTree::deleteNode`
(0x1408e03b0) appends a `SceneTreeMove` with `1F = ++clock`, `2F = nodeId`, `31 = 0`,
`4C = { 1F: 0:0 }`. Replaying all `SceneTreeMove` blocks in move-id order gives each
node's current parent: a layer is live only if its final parent is `0:1` (a detached
layer's tree node, root group item and child strokes stay in the file so undo can
re-attach it with a `SceneTreeMove` `31 = 1, 4C = { 1F: 0:1 }`, reusing the existing
root group item). Real cached pages show up to 8 root group items with 6 detached layers
(leaving 2 live layers). A tablet-written detached layer holds no live item; one that
did (a client deleting a layer with its strokes left live) is drawn by renderers that do
not follow `SceneTreeMove`s, as the web app's (seen live: ghost strokes of deleted layers
on a page).

**Layer operations (all go through `SceneController::pushAction` 0x1404470a0):**

- **Add (`addLayer` 0x140437910 → `add-layer-crdt`, init 0x14095c080, redo 0x14095d320,
  undo 0x14095ed60):** refuses at `layerCount >= 5` (*"add-layer-crdt::initialize: too many
  layers: %d"*). Default name (`0x1408a88d0`): starts at `n = len(live) + 1` and
  increments `n` while `"Layer %1"` matches any live layer's name. Takes 4 consecutive
  clock counters: `nodeId = clock+1` (`SceneTreeMove` `1F=nodeId, 2F=0:0, 31=1, 4C={1F:0:1}`),
  `labelTs = clock+2` (`SceneTreeNode` with label LWW and visible LWW `(0:0, true)`),
  `groupItemId = clock+3` (`CrdtGroupItem` appended to `ROOT_NODE`), and
  `currentLayerTs = clock+4` (`SceneInfo` field 1 set to `nodeId`). Undo detaches `nodeId`
  (`SceneTree::deleteNode`) and sets `currentLayer = oldSize - 2`; redo re-attaches `nodeId`
  (`SceneTree::moveNode` with `reuse=true`) and sets `currentLayer = count - 1`.
- **Delete (`deleteLayer` 0x14043cda0 → `delete-layer-crdt`, init 0x14095c100, redo
  0x14095d620, undo 0x14095ef80):** calls `SceneTree::deleteNode(nodeId)`. Active-layer
  fallback (`0x14095d7de`): if `deletedIdx < oldCur` or `oldCur == newCount`, sets
  `currentLayer = oldCur - 1`; otherwise keeps `oldCur` (pointing to the layer that shifted
  into that index). Undo re-attaches `nodeId` under `0:1` and restores `oldCur`.
  **Client:** before detaching, the layer is emptied: its live strokes (one
  `StrokeDelete`) and images (an `ImageDelete` each), and those of the groups below it,
  are deleted with the same tombstones as ClearPage and delete-selection, in the same
  undo step, so a detached layer holds nothing live, as the tablet's files have it. Undo
  re-attaches the layer at its index and puts each item back right after its deleted
  self (new ids, moveId = the deleted id), so the layer's stacking order comes back;
  redo empties it again. An undo of an add (or redo of a delete) whose layer holds items
  no step above it accounts for empties it too, and its redo puts them back
  (`LibrarySource._with_emptying`). Putting an image back inside a run of deleted ids
  (its deleted item joined to deleted strokes beside it, `can_join` 0x1408c3e60) cuts the
  run, as putting a stroke back does (`rm_writer.restore_image`).
- **Rename & visibility (`setLayerName` 0x1404485b0 → 0x14095e1d0 → 0x1408cfac0;
  `setLayerVisible` 0x1404486d0 → 0x14095e3d0 → 0x1408cfb60):** updates `SceneTreeNode`
  field 2 (label LWW string) or field 3 (visible LWW bool) with timestamp `++clock`,
  swapping old and new values in the action so undo calls redo.
- **Reorder (`moveLayer` 0x1404442f0 → `move-layer-crdt`, init 0x14095c6b0, redo
  0x14095df40 → `SceneTree::reorderNode` 0x1408df3c0):** deletes the layer's old
  `CrdtGroupItem` in `ROOT_NODE` (`deletedLength = 1`, no value) and inserts a new
  `CrdtGroupItem` for the same `nodeId` at the target `(left, right)` in `ROOT_NODE`'s
  CRDT sequence (no `SceneTreeMove` added; confirmed on cached reordered pages). Updates
  `currentLayer` so the active layer tracks the same node; undo moves it back to `oldIdx`.
- **Merge down (`mergeLayerDown` 0x140443de0 → `merge-layer-down-crdt`, init 0x14095c330,
  redo 0x14095d840, undo 0x14095f180):** requires `1 <= srcIdx < count`. Iterates live
  items of `src` in sequence order: child `CrdtGroupItem`s (type 2, including text-anchored
  groups) are moved to `dst` via `SceneTree::moveNode` (`0x1408de730`, adding a group item
  in `dst` and appending a `SceneTreeMove` to `dst`); non-group items (lines, images, etc.)
  are cloned (`id = 0:0, parent = 0:0, moveId = 0:0`, timestamps kept; shader `sourceId`s
  renamed via `0x1408cb8d0`), appended to `dst`, and deleted from `src`, groups and
  clones going to `dst` in `src`'s sequence order (so a group between two strokes stays
  between their clones). `src` is then detached via `SceneTree::deleteNode`, and if
  `srcIdx <= oldCur` then `currentLayer = oldCur - 1`. A real cached merge confirms the
  id allocation: clones take contiguous ids appended at `dst`'s tail with no `moveId`,
  then `deleteNode` takes the next id and `SceneInfo` field 1 the one after. Undo deletes the clones from `dst`, re-attaches `src`,
  restores `src`'s deleted items right after their deleted slots (`0x1408cbd60`), moves
  the child groups back to `src`, and restores `oldCur`.
- **Re-attach position (client deviation):** re-attaching (redo of add, undo of delete,
  undo of merge) reuses the layer's old root `CrdtGroupItem`. Every reorder deletes a
  layer's root group item and inserts a new one at the target, so after another layer
  moved (and was moved back by undo) the reused slot can sit on the wrong side of it,
  and the layer would come back at another z-position. The client records the layer's
  index with the undo step and, when the re-attached layer is elsewhere, reorders it
  there with the same delete + insert `reorderNode` writes (no new block shapes).
- **SceneInfo (0x0D) flags (`setCurrentLayer` 0x140958470 → 0x1408aa470;
  `setBackgroundVisible` 0x1409583c0 → 0x1408aa340; `setRootDocumentVisible` 0x14095e5e0 →
  0x1408aad70):** field 1 (`currentLayer`, LWW id), field 2 (`backgroundVisible`, LWW bool),
  field 3 (`rootDocumentVisible`, LWW bool), each taking `++clock` when changed.
  Deviation: in the client, `setCurrentLayer` persists field 1 without pushing an undo step
  so Ctrl+Z reverts content/layer mutations rather than layer-selection clicks (while add,
  delete, move and merge restore `currentLayer` as part of their own undo steps).
- **Hidden layer behavior:** `Scene::addLine` (0x1409578c0), `Scene::erase` (0x140996ae0),
  `Scene::cloneAddAndSelectItems` (0x1409af310), `SceneController::addSelectionRect`
  (0x140437a20) and `SceneController::selectImageAtPoint` (0x140446740) do not check
  `SceneTreeNode::visible` (+0x160); real tablet pages can also have `currentLayer`
  pointing to a hidden layer. Operations target `currentLayer` as usual, and hidden layers
  are skipped during rendering until shown again.
- **`SceneLayersModel::buildItems` (0x14045bbc0):** exposes 8 roles per row `i` of `n`
  live layers (`layerIndex = i`, `layerName`, `layerVisible`,
  `layerSelected = (i == currentLayer)`, `layerMoveUpEnabled = (i < n - 1)`,
  `layerMoveDownEnabled = (i > 0)`, `layerMergeEnabled = (i > 0)`,
  `layerTrashEnabled = (n > 1)`). `LayersFoldout.qml` rotates the repeater 180° so index
  `n - 1` sits at the top and index `0` at the bottom. **Client:** rows are updated in
  place. Each page load, eye click, layer edit or upload emits two or three of the
  controller's signals, and resetting the model on each rebuilt every row 2–3 times,
  destroying a row mid-press (the click was lost); the model now diffs the rows and
  emits `dataChanged` for those that differ, inserting or removing only rows added or
  taken away (a reset only for another controller). Keyboard focus: a removed row's
  button, or a row button, the rename field or the New layer row keeping focus inside the
  foldout, leave Undo/Redo (ProxyActions resolved from the focused item, `main.qml`
  `actionHandler`, which skips the window's root item) with nowhere to go;
  `SceneView.refocus_viewport` hands focus back to `DocumentViewport` after a row change
  or a rename when focus fell to the root item or a non-text control of the foldout.
- **Eye clicks (client).** `LayersFoldout.qml` calls `setLayerVisible(i,
  !isLayerVisible(i))`: an explicit target read from the controller's layers. Those
  were only refreshed when the page reloaded after the edit was kept, so a second
  click before then asked for the same state again (a no-op): quick clicks were
  dropped. The click now shows at once (the controller's layers and the page drawn),
  so the next click reads the state it gave; the edit is kept on the view's one worker
  in click order, and the state clicked stays over every reload queued before that
  edit (`SceneView._visible_clicks`, by load number), so a reload loaded in between
  does not flip the eye back.

### 5.9 Selecting and copying PDF / EPUB text

Traced in reMarkable.exe 3.29 [O]; EPUB pages are their tablet-rendered PDF, so both go
the same way. The whole interaction lives in the app's QML; only the glyph engine is C++.

**QML (carved, unchanged).** `DocumentViewport.qml` `mainMouse`, on a page that is not a
note page (`!notePage`): a left press inside the page records `pdfSelectAnchorPos`
(scene) and counts clicks (500 ms `clickCountTimer`, `doubleClickDistanceThreshold`);
a move with the button down past `dragDistanceThreshold x devicePixelRatio` calls
`sceneController.selectGlyphsFromPositions(anchor, cursor)` on every move; release with
`clickCount >= 2` calls `selectGlyphWordAtPoint(point)` (so a triple click selects a word
too: no line or paragraph selection), and a plain click with a selection clears it. The
right button opens the context menu (Copy = `actions.copyAction`). `mainMouse` is
disabled in selection mode (`enabled: !selectionMode`), so the lasso never starts a text
selection; the cursor over the page is `Qt.IBeamCursor`, a link's `PointingHandCursor`.
`PdfTextSelection.qml` (objectName `pdfTextSelection`) keeps `selectedText` from
`glyphsSelected` and paints the areas through `PdfGlyphSelectionOverlay.qml` ("Client-only
— device uses C++ PdfTextSelectionOverlay"): one `Colors.textSelectionBlue` (#6635A7FF)
Rectangle per area at `tileManager.sceneToView(area)`, refreshed on `transformChanged`.
It is cleared on `onDocumentChanged` and `onCurrentPageChanged`. Copy:
`copyAction` → `Clipboard.setTextFromString(pdfTextSelection.selectedText)`.

**SceneController (moc slots 98–101, metacall 0x1403c99b0 jump table 0x1403cb590).**
`selectGlyphsFromPositions(QPointF anchor, QPointF cursor)` 0x140447590,
`selectGlyphsWithAnchorIndex(int, QPointF)` 0x140447670, `selectGlyphWordAtPoint(QPointF)`
0x1404474c0, `clearPdfTextSelection()` 0x140439d40; signal `glyphsSelected(QList<QRectF>
selectedAreas, QString text, int startIndex, int endIndex, QPointF startHandlePos, QPointF
endHandlePos)`. Each slot wraps its arguments in a `std::function` (vtables 0x140c8b4b0,
0x140c8b520, 0x140c8b558; `_Do_call` 0x1404349e0, 0x1404349b0, 0x140433f30) posted to the
scene thread (0x14056dd20), which runs `Scene::selectGlyphsFromPositions` 0x1409b3bb0,
`selectGlyphWordAtPoint` 0x1409b3680 and the clear 0x1409b0750.

**Glyphs (PDFium, 0x14071e0b0 → 0x14071dae0).** `FPDFText_LoadPage`, `CountChars`, then
per index `GetUnicode`, `GetCharBox` and `GetCharOrigin`; a character is skipped only if
one of those fails, so generated and zero-size characters stay. Records are 0x38 bytes:
`+0x8` origin y (baseline), `+0x10` QRectF box, `+0x30` QChar. Lines (0x140a2af00): in
order, a glyph joins the current line when its baseline is within 1.0 of the line's last
glyph's, the line's top/bottom growing to the boxes; line entries are 32 bytes (first,
last, top, bottom, …), also kept sorted for hit testing.

**Hit test (0x140a2c5b0 → 0x140a2c160).** With a hint index, the glyphs ±2 around it on
its line if the point is within that line's top..bottom (zero-size boxes skipped); else
a binary search of the lines by y, the lines within a tolerance band (min 2.0, 0.4 x line
spacing), the glyph nearest in x on each (0x140a2c810), and of those the one with the
smallest squared distance from the point to its box. No distance limit: a drag that
starts in a margin snaps to the nearest text.

**Range (0x140a51460).** Indices ordered, inclusive at both ends; trimmed of glyphs that
are `QChar::isSpace` or not `isPrint` at either end (0x140a516b0); cut into line segments
(0x140a2ca90); per segment (0x140a4fc80, pads 0 and 0.25): an area from the segment's
leftmost to rightmost glyph, from `baseline − 1.25·ascent` with height `1.5·ascent`
(ascent = max over the whole line of baseline − box top), and a string of its characters.
The text is the strings joined with `"\n"` (0x141009850, `QStringList::join`); `endIndex =
start + count − 1`. Handles (0x1409b3e35…): the first area's left (right if right-to-left)
and the last area's right, at `y + 0.5·height` (0x14102b728 = 0.5). The old and new areas
are invalidated, then `glyphsSelected` is emitted (0x1408b08c0). An unchanged range
(+0x210/+0x214) emits nothing.

**Word (0x140a51550).** Nothing when the glyph under the point is whitespace or not
printable (0x140a508f0); else it grows backwards (0x140a50800) and forwards (0x140a50700)
over neighbours that sit next to it on the line (0x140a2bd40) and are neither `isSpace`
nor `isPunct`; the glyph itself may be punctuation ("bravo," from its comma).

**The client (ui/pdftext.py, bridge/scene.py).** The four slots and the signal as above,
on one shared single-thread glyph worker with requests coalesced (a drag asks faster
than PDFium answers). QtPdf exposes no character origins or lines (PDFium's symbols are
not exported from libQt6Pdf), so, deviations: lines are PDFium's own (the runs between
the line breaks it generates in `getAllText`), the nearest line is found by distance to
its box (where PDFium's lines overlap, as table cells or columns on one baseline do, the
one whose middle is nearest) and the character in it by bisecting prefix boxes (`getSelectionAtIndex`, ~1–2 ms
a call, per-page text built once per page); an area runs from `top − 0.2·h` to the
line box's bottom (h its height: ascent taken as 0.8·h); a segment whose glyphs QtPdf
gives no box at all (3.29 has zero-width ones) takes its line's box. Text placed off the PDF page
(an EPUB rendition's run-in above the top) is not selectable, as in search. Additions:
typographic ligatures (U+FB00–FB06) are spelled out in the copied text, and each line of
it is trimmed of the spaces at its ends, lines of spaces only left out (3.29 trims only
the range's two ends, so a multi-line copy carried trailing spaces and blank lines); Esc with text
selected clears it and keeps the document open (in 3.29 Esc reaches DocumentView, a
SheetDialog, and closes the document); taking up a pen, eraser, lasso or text mode
clears it. Inserted pages (no `redir`) have no PDF page and select nothing; the PDF page
comes from the page list (`document_pages`), so moved and inserted pages are allowed for.

### 5.10 Pages between documents, clean copies, password-protected PDFs

Traced in reMarkable.exe 3.29 [O unless marked] (Ghidra headless decompilation of the
functions named; moc static metacalls `DocumentController` 0x140472e80 and
`LibraryController` 0x140473ef0 give the slots).

**Pages copied or moved into another document.** The QML's page clipboard
(`PageActions.qml` `moveTo`) calls `DocumentController::copyPages(source, pages,
target, position)` (slot 16, 0x140490fc0 → 0x140490bb0) or `movePages(..., jsCallback)`
(slots 14/15, 0x1404922e0 → 0x140491b70). Both log "Copy|Move pages {} in {} to {} at
position {}", refuse an invalid source/target ("Invalid source and/or target while
moving..") and ensure the format version of both documents first (0x14065e7e0,
"Cannot copy|move pages, unable to ensure correct format version": the fv1 → cPages
upgrade of §5.4). With source == target the same-document tasks run (`CopyPagesTask`
0x1406904b0, keeps `redir`, §5.4). Otherwise:
- copy = `CopyExternalPagesTask` (0x14068ffb0; RTTI `.?AVCopyExternalPagesTask@@`,
  derived from `CopyPagesTask` → `DocumentTask` → `Task`), built with the source's page
  ids (0x140663160) and templates (0x1406694b0: the page's raw template register,
  0x140877a10, empty when it has none) and the position; it locks the source document
  and runs `DocumentWorker::doCopyExternalPages` (0x140559b20) on the target: position
  clamped to 0..pageCount; per source page a new QUuid (0x14084b930), its files copied
  by `pagecopyhelper` (0x14057a4e0: copyPageFiles into a work area, renameEntries into
  the target, a copy marker meanwhile; "Failed to copy source page {}" stops the loop),
  then `insertPage(index, newId, template, -1, mode 0)` (0x140871e60): **no `redir`**,
  so a PDF page arrives as its ink on plain paper; the target's own page map, `original`
  and `redir` values are untouched. The target's `.content` is saved and `pagesAdded`
  emitted with the new indices.
- move = a task group of `RequestCompleteDeferredTask(source)` (the source wholly
  downloaded), the same `CopyExternalPagesTask`, and `DeletePagesTask(source, pages)`
  (control-block RTTI of 0x14068a690 / 0x140689e90).

*Client:* `core.edits.PageImport` (kept on the target: source document, source page
ids, new ids, templates, and the page files as (pattern, sha256, size), the bytes in
the pending files by hash so that later edits of the source do not reach the copy),
`LibrarySource.import_pages` / `move_pages_to` (import, then a `PageDelete` on the
source; both must be editable here, a copy only needs the target), `Staged` origin
`("import", edit, i)`, `DocumentController.copyPages/movePages` with another target. A
page's files are taken as the viewer shows it: its `.rm` with pending edits replayed,
its folder (pictures, pending inserted ones included), `-metadata.json`, highlights and
thumbnail. Deviation: the app copies the target's page files at once; the client keeps
them as pending edits of the target, uploaded with push_edits like any page edit.

**Clean copies.** `LibraryController::cloneEntry(doc, collection, clean)` (slot 8,
0x1404dc640; the desktop QML only ever calls slot 9, without `clean`) refuses to clean
a document whose type (+0x238, `Document.FileType`) is Undefined (0) or Notebook (1):
"Cannot clean {:?} of type {}". It logs "Cleaning" (else "Cloning") "{} {:?} to {:?}" and
names the copy as a plain copy is named ("%1 copy", made unique). `Document::clone`
(0x14065a590) with the clean option (options +0x30):
- does not copy the page map (a plain copy does, 0x140551680) but makes an empty one for
  the copying author (0x1406502a0 → 0x140876c70: author UUID and index; `original` -1),
  then `setPageCount(source +0x200)` (0x140668240 → 0x140876fd0) appends that many
  pages (0x14086f560 → insertPage, new QUuid, empty template, `redir` i since the new
  Document's type is still Undefined, set in its constructor 0x14064f3c0), then
  `setFileType(source type)` (0x140667aa0 → 0x140875c40) sets `original` to the page
  count (A:1) and writes every page's redirection again (0x1408775e0, always a new tick:
  A:3);
- `DocumentCopyHelper` (0x1406b2640) pairs **no** pages, so no page file is copied (no
  `.rm`, page folder, `-metadata.json`, highlights, thumbnails); the document's other
  files are.
[I] +0x200 is read as the PDF page count (the client uses cPages `original`, else
fv1 `originalPageCount`, else the PDF counted). No cached document is a clean copy (no
`redir` stamped A:3 among the cached PDFs), so the result is not compared with a real
one. *Client:* `core.pagemap.clean`, `core.clone.clone_files(clean=True)`,
`SyncClient.clone_document(clean=True)` (the source's pending edits left out),
`LibraryController.cloneEntry(..., True)`; page tags are dropped with their pages
(client choice).

**Password-protected PDFs.** No cached `.content` has the `lockedByPassword` key the
parser knows (§5.4): the state comes from the PDF. `PasswordDialog.qml` (shown by
main.qml `requestOpenDocument` when `document.lockedByPassword`) calls
`LibraryController::unlockWithPassword(docId, password)` (slot 19, 0x1404e3f50 →
0x1404e3c90: "Setting password for", "no document specified", "could not set
password"), which starts a `PasswordTask` (vtable 0x140d0a150). Its execute
(0x14068ee50) calls 0x1406687a0: the password is stored **in memory** on the Document
(+0x800, a QString) and a 30 000 ms QBasicTimer (+0x818) started; the Document's
timerEvent (0x1406698f0) empties it when the timer fires. The worker gets a copy
(0x14056f840, +0x48) to load the PDF. On a wrong password the task's handler
(0x14068faa0) emits `Document::invalidPassword` (signal 16; the dialog shows "Incorrect
password. Please try again."), empties the password and stops the timer; success comes
as `loadingSucceeded`, on which the dialog accepts and runs the open call. Nothing
writes the password anywhere. ("Bad password" in the binary is curl's.)

*Client:* `ui/pdflock.py`: whether a cached PDF needs a password (only a file with
`/Encrypt` near its start or end is opened to see; QtPdf's `IncorrectPassword`),
passwords in process memory by PDF path; `PdfFile` uses them and raises `PdfLocked`;
`DocumentWrapper.lockedByPassword` / subtext "Password protected PDF";
`unlockWithPassword` tries the password on a worker and answers with `loadingSucceeded`
or `invalidPassword`. Deviations: the password is kept until the app quits or logs out,
not 30 s (the client opens a PDF again for every view, thumbnail and export job, where
the app keeps its unlocked PDFium document); an uncached PDF is not downloaded to find
out whether it is locked. Export: ExportDialog's password opens the source, and "keep
password protection" encrypts the exported PDF with it (pypdf, AES-256) [I: the app's
encryption scheme is not traced].

### 5.11 PDF/EPUB text highlights erased; typed text on PDF/EPUB documents

**Text highlights (`CrdtGlyphItem`, block 0x03)** [O: reMarkable.exe 3.29; none of the
6,110 cached v6 pages (4,281 blobs + 1,829 document files, 2026-11-21) has a 0x03 block,
and no `.highlights/` file is cached, so everything below is checked on synthetic pages
made to this layout]. A smart highlight is a layer item like a stroke (the item header
of §5.1), its value a `SceneGlyphRangeItem` (RTTI `.?AVSceneGlyphRangeItem@@`, vtables
0x140ca2488 / 0x140ca24e8, value type 1: 0x140a076f0 maps value types 1/2/3/6/7 to
blocks 3/4/5/6/15). Value writer 0x140a1bcf0 (called from 0x140a67180 after `u8 1`):

| Field | Written |
|---|---|
| `24` start u32, `34` length u32 | only when the first-glyph id (+0x98) is `0:0` |
| `44` colour u32 | always (scene::Color; 0 is drawn as HighlighterYellow) |
| `5C` text | varuint UTF-8 length, `u8 1`, UTF-8 bytes |
| `6C` rectangles | varuint n, n × QRectF (x, y, w, h as f64), scene coordinates |
| `7F` first glyph id, `8F` last glyph id, `91` u8 include-last | only when the first id is set |
| `A4` ARGB u32 | always |
| unknown tail | kept as read |

Block versions are the ones read (0x1405a5100 / 0x1405a7f30: stored, else 1 and 1).
Paint 0x140a1dc00: composition mode 16 (Darken), no pen, brush = colour (0 → 3; 9 →
its ARGB; ≥ 17 → black), each rectangle mapped and filled, the rendered page under it
consulted pixel by pixel (grey pixels lighter than 0xa0 tinted, darker kept). The client
(`rm_parser.GlyphRange`, `decode_glyph_value`; `renderer._paint_glyphs`/`_svg_glyphs`)
fills the rectangles under Darken, which keeps dark text dark the same way; PDF export
draws them in its Darken layer. Selection: the item's rectangle and lasso hooks (vtable
+0x38 0x1404431e0, +0x40 0x140442ec0) are `xor al,al; ret`, so highlights are never
selected (and so never moved, copied or deleted by a selection), in 3.29 or here.

**Erasing** [O]. The eraser's search 0x140996fb0 walks a node's items in sequence order
(the layer, then every group node below it, as for strokes, §5.1). For a value of type 1
it unions the rectangles (QRectF `|=`), and only if that union `intersects` the outline's
bounding rectangle (outline +0x30) counts the rectangles whose centre (QRectF::center,
taken to float and moved by the node's anchor position, float adds) is inside the
outline (the nonzero-winding test 0x140a63420); as soon as the count exceeds `n >> 1`
the item is queued for deletion **whole** (no pieces, no id taken). Strokes found in the
same pass are cut as before. So: half of the rectangles or fewer is not enough, and the
bounds test uses the stored rectangles, not the anchored ones. EraseSection (8) is the
same search with the drawn outline. ClearPage (`clearLinesInLayer`, filter 0x140433d20:
everything but value types 7 and 2) deletes highlights with the strokes. Deleting a layer
empties it (the client's `_emptying`, §5.8) of highlights too. Undo/redo are the item
actions of §5.1: the highlight comes back as a new 0x03 item (`++clock`) right after its
deleted self, with the value it had (it has no moveId to set); a deleted highlight stays
a 0x03 block without a value.

Client: `rm_stroke.glyph_hit` (the rule above), `erase` (glyph ranges in the node walk,
deleted whole, in hit order with the strokes), `clear_layer`, `delete_strokes`
(`ERASABLE_BLOCKS` = 0x05 and 0x03), `item_values` / `glyph_ranges`, `restore_item`
(glyph values back under their 0x03 block's versions); `core.edits.StrokeRestore`
accepts a glyph value; `LibrarySource._stroke_values` (eraser undo) and `_emptying` (layer
delete) include them. A `StrokeErase` stores only the eraser's line, so the highlights it
takes are found again on every replay (restart, upload, rebase); the ledger and outbox
need nothing new. `tests/test_pdf_highlights_offline.py` covers the layout, the rule (more
than half, exactly half, bounds, anchor offset), eraser/lasso/clear/layer delete with
undo and redo, a restart with pending edits, the fake-cloud upload, the drawn colour, and
the eraser over a highlight through the app's QML; `tools/pdf_edit_corpus.py` accepts a
glyph range that is unchanged or erased.

**Typed text on PDF and EPUB documents** [O]. 3.29 allows typed text on **note pages
only**. `QmlDocumentWrapper.isNotepage(int)` (moc method 34 of the wrapper's static
metacall 0x14066c400) calls `Document::isNotepage` 0x1406609e0:

```
if fileType (+0x238) == Notebook (1): true
if page < 0: false
no page map (0x14065fdb0 false): page < redirections (+0x400) ? redirection[page] == -1 : true
page map:                        page < pages ? redirection(page) (0x140876260) == -1 : false
```

so a PDF/EPUB page is a note page only when no PDF page is behind it: a page inserted
into the document (§5.4, `redir` absent). Every way into typed text in the desktop QML
is gated by it: DocumentView.qml's Text button (`visible: ... document.isNotepage(currentPage)
&& actions.canEditDocuments`, and its `onClicked` again), DocumentViewport.qml's
`notePage` (text mode on a click in the page, line 931; `selectAllAction` and text paste
`enabled: ... notePage ...`; `focusTextWhenActive && notePage` on load; "Add page" from
the page menu starts text mode on the new note page). On a PDF/EPUB page itself no text
box can be made. The client matches it: `DocumentWrapper.isNotepage` (`pdf_page is None`;
past the page map false, as with a page map) drives the official QML unchanged, and
`SceneView._text_target` refuses (`"Text can only be typed on notebook pages"`) a page
with a PDF page behind it should anything call `createRootDocument` there. On an inserted
page the text is laid out on the note page's own 1404 × 1872 scene, whatever the PDF's
page sizes, and goes through the same view ↔ scene transform as the pens; the QML test
types on such a page in a PDF of A4 and 16:9 pages, clicks the caret position of a
character where the window shows it (the caret lands there), and paints the page at
device pixel ratios 1 and 2 (the text's box agrees within 2 px). Not traced: whether the
tablet's own firmware lets text onto PDF pages (only the desktop binary is here).

### 5.12 Handwriting conversion (Convert to text)

Traced in reMarkable.exe 3.29 (`src/handwritingconversion/src/{recognizer,recognitionqueue,
recognitiontask,recognitionjob,jiixparser,hwcanalyzer}.cpp`) and the app's QML
(DocumentView.qml `hwcFullPage`/`hwcSelection`, DocumentViewport.qml, PageOverview.qml,
common/HwcNotificationBar.qml). Client: `core/handwriting.py` (request, JIIX parser,
error mapping) and `Recognizer`/`RecognitionJob` in `ui/quick/bridge/scene.py`.

**Request** (RecognitionTask, built at 0x1401a5300; posted at 0x14017e8e1):
`POST /convert/v1/handwriting` on the gateway with the user token, `Accept:
application/vnd.myscript.jiix`, a 70 s timeout (0x11170 ms), up to 3 attempts per task
(`cmp eax,3` at 0x14017dde7; "Failed to convert page {} after {} attempts: {}"). Body
(QJsonObject; the dotted keys are set through a path helper, 0x1401a4c20; each value is
made before its key, so the booleans read `mov dl,1` = true):

```json
{"xDPI": 2280, "yDPI": 2280, "width": 14040, "height": 18720,
 "contentType": "Raw Content",
 "configuration": {"lang": "en_US",
   "export": {"jiix": {"strokes": false, "text": {"chars": false, "words": true}}},
   "raw-content": {"recognition": {"text": true, "shape": false}}},
 "strokeGroups": [{"strokes": [{"x": [...], "y": [...]}]}]}
```

DPI 2280.0 (0x14102b788), width/height 14040.0/18720.0 (0x14102b790/798, the 1404 × 1872
screen × 10); point coordinates are the scene's × 10.0 (0x14102b758), only `x` and `y`.
**Deviation**: the client sends each stroke's points as the page stores them (anchored
groups not offset to their text line); recognition only depends on the shapes.

**Answer** (jiixparser.cpp): `elements` of type `Text` (`label`, `words`, `bounding-box`;
also `reflow-label`), applied to the page's root document ("No root document
available", "page {} already has a root document!"). List paragraphs come from two
regular expressions on each line (0x1008cf8, 0x1008d20): `^(\*\s|•\s|°\s|◦\s|→\s|-\s)` a
bullet, `^(0\s|o\s|O\s|✓\s|☐\s|☑\s|☒\s)` a checkbox. The client makes a paragraph of
each line of the Text elements (top to bottom), takes the marker off and gives the
paragraph that style (✓ ☑ ☒ ticked); `plainText` writes markers back as `•`/`☐`/`☑`.

**Jobs.** `RecognitionJob` {status Processing 0 / Done 1 / Error 2; error `ConvertError`
Unknown 0, NoError 1, NoTasks 2, NoText 3, NetworkError 4, ServerError 5,
AuthenticationError 6; `progress`, `finishedPages`, `pages`, `newPage`, `plainText`}.
One task per page ("Creating task for page {}"), run in turn (RecognitionQueue "{} task
left, {} finished"). Client mapping: no strokes → NoTasks; HTTP 401/403 or no token →
AuthenticationError; connection failures on all 3 attempts → NetworkError; other
refusals, 5xx after 3 attempts, or an answer that is not JIIX → ServerError; JIIX
without text → NoText. A job's tasks combine as: the first hard error wins, pages
without text are skipped while another has text. Offline (`--offline`, logged out,
network known down) a job fails with NetworkError without a request. The language is
`Settings.handwritingConversionLanguage` (the HwcLanguagePicker's), aliases as
`HandwritingLanguageModel` (en_UK → en_GB, no_DK/no_SV → no_NO).

- *Full page* (Convert to text in the viewport or the page overview,
  `startRecognitionJob(id, pages, language)`): `newPage` = last page + 1. On Done,
  DocumentView adds a page there (`addPageWithTemplateAndPageSize`) and calls
  `applyFormattedText(worker)`: the client opens a text box of body text on the new
  page, types the paragraphs and their styles as one undo step, kept as pending edits
  (`LibrarySource.add_text_edits` → PendingEdits → the outbox). The bar's Undo deletes
  the page (`DocumentController.deletePages`); `abortRecognitionJob` drops the job
  (late answers ignored).
- *Selection* (`startSelectionToolJob(items, language)`): DocumentViewport clones the
  selection, starts the job, **then deletes the selection**. On finish DocumentView
  sets `Clipboard.text = formattedTextToClipboardText()` and, on a note page,
  `selectionConversionFinished()` puts the cursor under the selection and calls
  `pasteClipboardContent(text, KeepStyle, MergeWithPreviousAction)`: the text, with
  its list styles, and the delete are one undo step (client: `add_text_edits(...,
  merge=(depth, step))` folds the paste's step into the delete's while the page's undo
  stack is still that deep).

**The app's gap, and the client's guard.** On failure the app undoes the delete only
`if (failed && textMode)` (DocumentView.qml `hwcSelection.onFailedChanged`), so outside
text mode a NetworkError/ServerError/NoText leaves the strokes deleted unless the user
presses Close (`abortSelectionToolJob(); undo()`); in text mode Close then undoes a
second time (the previous edit). The client's `_ConversionGuard` links the job to the
delete that follows it (the page's undo depth after it) and, when the job fails or is
aborted, undoes exactly that step itself on the view's worker (after the delete;
`LibrarySource.revert_top`, also dropping it from redo), whatever the mode; the QML's
own `undo()` calls during the failed status change and right after an abort are
ignored, so nothing else is undone. A failed job's `formattedTextToClipboardText`
returns the clipboard as it was (the app would empty it) and its merged paste is
dropped. A worker exception ends the task as Unknown, so no job is left hanging with
the strokes deleted.

Verified offline only (tests/test_handwriting_offline.py, a fake recogniser in
tests/fakecloud.py answering synthetic JIIX); never sent to the real cloud.

### 5.13 Bridge polish: paste in text mode, UserInfo, busy entries, thumbnails

**Paste of strokes in text mode** (`SceneController::insertItemsAtCursorPosition`,
0x1404425c0; the moc dispatcher's slot order puts it after `createImageSnippetFromStroke`
0x14043c090). DocumentViewport's paste action calls it instead of `pasteLines()` when
text mode is on and the clipboard last took items. It does nothing for an empty list,
then posts a `std::function<bool(Scene*)>` (RTTI `<lambda_1>@…insertItemsAtCursorPosition`,
`_Do_call` 0x140434630) to the page's worker [O]:
1. Each item is cloned (id and parent 0:0) and its image file copied (0x1409b1a90).
2. Nothing more happens unless the scene has a root text (+0x278) and a text editor
   (+0x380); the position is the editor's cursor (0x1404428b0: a selection is dropped,
   never below 0).
3. The box around all items is moved sideways onto x 0, the text box's centre line,
   unless it is there already (qFuzzyCompare). The first image is then scaled about 0, 0
   by min(1, text width / its width) (0x140440ce0; the width is the text's +0x500, 740
   without one, 0x140443d50).
4. 0x140951040, inside one undo macro (0x140a4f2f0 … 0x140a4f300), asks the text for room
   (0x140a4ed10) of the items' height + 50 + 38 (0x140c8bf80): at cursor p, with
   atBlockStart / atBlockEnd / blockIsEmpty ("insertionIndex=%d atBlockStart=%d
   atBlockEnd=%d blockIsEmpty=%d newlines=%d"), n = ceil((needed − that empty block's
   height) / line) for an empty block the cursor starts, else ceil(needed / line) + 1 in
   the middle of a block; line = the default style's line spacing + LineHeight + top and
   bottom margins (0x140930fd0). n newlines go in at p (one more when p is the end of the
   text, characterCount − 1), the n + atBlockEnd − u new blocks from p + u (u = 1 unless
   the cursor started a block with text) get the default style (0x140a41920), and a
   block split in two gets its style back after the room (0x14092ffa0). The room's y is
   the block before it (its top when the cursor started it, else its bottom) plus half of
   (the next block's top − that − needed) ("Incorrect spacing. This is a bug" below 0).
5. The items are moved down so their top is at 50 + that y, then handed to the anchor
   manager (0x14096ca70) with type 2 forced and "join an overlapping group" on (the same
   path as a pen stroke on a page with text, §5.1 "Where a new stroke goes"): they go into
   a group anchored to the character under the centre of their box, stored less the anchor
   position. Without an anchor manager a `SceneInsertItemsAfterActionCrdt` (vtable
   0x140d6d6e0) inserts them unanchored.

Client (`_SceneView.insert_items_at_cursor` → `_paste_anchored`, `ui.textedit.make_room` /
`anchor_under`, `ui.editing.centred_for_cursor` / `moved_down`, `viewer.rm_anchor.add_group`,
`LibrarySource.paste_anchored`): what was typed is kept first, then steps 3–5 as above on
the open text session, and the text's new lines, a new `AnchoredGroupAdd` pending edit
(anchor = the new line's character, named `("edit", uid, n)`; anchorOriginX = that
character's x; type 2, threshold 35.748) and a `StrokePaste` into it (`layer` = the
group, `("edit", uid)`) are kept as **one** undo step (the text's steps and the strokes'
`StrokeChange`; undo takes the strokes, then the styles, then the lines). The text
session is read again afterwards (the group and strokes took ids it did not know). The
strokes then reflow with the text (`renderer.stroke_offsets`). Deviations: the number of
new lines uses the app's body line (69.546875, §5.1) but positions come from this
client's layout (71.5 per body line), so the room is slightly taller here; the pasted
strokes always make one new group (no clustering, no joining a group they overlap);
images are placed in the room but not anchored (this client draws no anchored images);
undo leaves the emptied group node (as the eraser does). `AnchoredGroupAdd` replay makes
no group when its character is not on the page, and the strokes then go on the layer.
Checked: `test_bridge_polish_offline` (Ctrl+V in text mode through main.qml: the room,
the anchor fields, the strokes drawn centred in the room, one undo step, reflow when a
line is typed above), `test_v1_finish_offline` (`MakeRoomTests`, `AnchoredGroupTests`:
the group's blocks byte for byte against a real node's layout, the edit's round trip and
replay).

**UserInfo from the user token** (`src/xofm/libs/user/src/legacy.cpp` parseUserToken →
toUserInfoData, rm.user.legacy): the JWT's `auth0-profile` {`UserID` → id, `Name` → name,
`Nickname` → username, `Email` → email, `CreatedAt` → createdAt}, `scopes`, `level` →
subscriptionLevel ("Subscription changed from: {} to: {}"), `tectonic`. The account
type is Google when the UserID names "google" (0x1403735cd), Facebook for "facebook"
(0x140373613), else Undefined (the comparing import at 0x140c700f8 was not resolved: the
client tests "contains", which also covers a prefix). The claim names were checked
read-only against the cached token's structure (key names only).
**`isSubscribed`** [O] is the bool at UserInfo+0xe8 (getter 0x1403710b0), set only by
`setSubscriptionLevel` (userinfo.cpp, 0x140370820): nothing when the level equals the
string shown now; an empty level shows tr("Not subscribed") and clears it; any other
level sets it (there is no list of free levels: "free" counts as subscribed) and is
shown split at the regex `( |-|_|:|\t)` (KeepEmptyParts), each part "%1%2 " with its first
letter upper-cased and the rest lower-cased, trimmed and translated ("connect" →
"Connect"); then "Subscription changed from: {} to: {}", isSubscribedChanged when it
changed, subscriptionLevelStringSet. The constructor starts at tr("Not subscribed"),
false (0x14036cf0e). The client's `UserInfo.setSubscriptionLevel` does the same. `QuickApp` fills UserInfo at start and on log-in (`AuthClient
.cached_user_token`, no request) and clears it on log-out; nothing of it is logged.

**Busy entries.** ActionManager turns copy, move, delete, rename, export and share off
while `selection.includesExporting` / `includesDownloading` (DocumentSelection) are set;
LibraryHelpers and ContentEntriesBase also read `Entry.isExporting` / `isDownloading`.
Client: `Library.set_downloading` counts downloads per document, fed by
`LibrarySource.download_listener`, which `LibrarySource.blob` calls (begin, end, also on
failure) only when a file is not in the blob cache and the library is online; the entry
then reads `Entry.Downloading` (8) and `isDownloading`. Exports already set
`isExporting`. `Library.busyChanged` re-emits the selection's `selectionChanged`, so the
two `includes*` flags follow both.

**Thumbnails after editing** (`requestDeferredCompleted`, called by DocumentViewport on
close and by DocumentView when the page overview opens, "Make sure that thumbnail gets
updated"). Checked: a page edited here and not uploaded already gets a new thumbnail
source (`image://rmthumb/<id>/<page>?v=…`) by the time the document closes, as the
library's node for it changes with the pending edits (test
`test_an_edited_page_not_uploaded_gets_a_new_thumbnail_on_close`), so the slot stays a
no-op here.

### 5.14 The last stubs with behaviour: scroll positions, zoom, archives, files, sleep

None of these is called by 3.29's desktop QML; they are wired so that the bridged API
answers as the app's does (§2.2 lists what stays a no-op). Tests:
`client/tests/test_v1_finish_offline.py`.

- **`SelectionImage.image()`** (metacall 0x1403cc330 case 3): a copy of the QImage at
  +0x28; `reset()` empties it. `updateImage` (0x1403eed70) makes it: an
  ARGB32_Premultiplied image of `sceneRect.toAlignedRect().size()`, filled with
  Qt::transparent, the selected items of the layer drawn through
  `previewTransform * translate(−x, −y)`. Client: made on the first `image()` after an
  update, through the same transform; the item still paints vectors.
- **`LibraryFacade::countArchivedDocuments`** (0x1404aca80): the library's entries that are
  documents (type 1) with `Document::archived` (+0x2cc) set. `archived` is set by
  0x140669d10 = `QFile::exists("<dir>/<id>.cloudarchive")` (FileSystem vtable +8; the
  suffix is the UTF-16 ".cloudarchive" at 0x141012988). 3.29's sync only ever removes such
  files ("Failed to remove cloud archive file: {}", transfers-and-removal.cpp); no cached
  document has one. Client: `DocumentWrapper.archived` = a "<id>.cloudarchive" among the
  document's files; the count follows.
- **`Library::purgeThumbnails`** (library-metadata.cpp 0x1404d4df0): for every document,
  its `<id>.thumbnails` directory removed recursively ("Failed to purge thumbnails for
  {:?}"), then "Purged thumbnails for {} documents" (all documents counted) and "Thumbnail
  purge took {:.3f}s". Client: the cached thumbnail images named after each document's
  hash go (`Library.thumbnail_dir`); they are drawn again when next shown.
- **`LibraryController::setZoomMode`** (0x1404e3700): a mode above 3 → "Incorrect zoom mode
  set: {}", false; else (the entry must be lockable, "isEditable") Document 0x140668a70
  sets +0x2ac when it differs and emits zoomModeChanged. `.content` "zoomMode": the reader
  0x1406d06c0 maps "fitToWidth" 1, "fitToHeight" 2, "customFit" 3, anything else
  bestFit 0 (Document::ZoomMode).
- **`setCustomZoomFit(id, scale, center, pageSize)`** (0x1404e1e70 → Document 0x1406676c0):
  when the mode is not customFit or any of scale, centre, page size, or
  customZoomOrientation (copied from the document's orientation, +0x2a8 → +0x438)
  differs: all set, mode customFit (zoomModeChanged), customZoomChanged.
- **`setScrollPosition(id, page, position)`** (0x1404e3120 → 0x1404e2e60): "no document
  specified" without one; the page map must be in the current format (0x14065e7e0: a
  formatVersion 1 document is upgraded as on load, else "Cannot set scroll position,
  unable to ensure correct format version"); then Document 0x1406688b0 → page entry
  0x140877620: `verticalScroll` = position and `scrollTime` =
  `QDateTime::currentDateTimeUtc().toString(Qt::ISODate)`, each a register write by this
  device (its counter + 1). Real pages carry both (573 of 7,015 cached page entries, the
  two stamps always equal). **`DocumentWrapper.scrollPosition(page)`** (0x140666760 →
  0x1408766d0): the value when the register was ever written and `scrollTime` is less
  than 8 days ago (`daysTo` < 8; an unreadable time counts as now), else undefined.
  Client: `core.pagemap.PageMap.set_scroll` / `page_scroll`, `core.create.set_scroll_position`.
- **`DocumentController::setViewBackgroundFilter(id, value)`** (0x140493250 → 0x1404932d0 →
  Document 0x140668a10): "invalid document..." and false without one; else the string at
  +0x2b0 when it differs, viewBackgroundFilterChanged. The `.content` writer leaves
  "viewBackgroundFilter" out when it is empty, so "" removes the key. Nothing in 3.29
  draws a filter from it, and nothing here does.
- All four `.content` setters change their field only and keep `lastModified` (like
  setOrientation, §5.6); here they go through `LibraryController._set_setting` (shown at
  once, committed to the cloud; written into the files of a document made here and not
  uploaded yet).
- **`PlatformHelpers::openFileChooser`**: 3.29 for Windows does nothing (its metacall,
  0x140094b10, has no case for slot 12; the desktop QML imports through FileImportDialog,
  which emits `documentAccepted` per file). Linux: a QFileDialog for PDFs and EPUBs (Qt
  uses the XDG desktop portal's chooser where there is one); each chosen file goes out
  through `documentAccepted(url, "", folder, false, false)`, as FileImportDialog sends it.
- **`PlatformHelpers::setInhibitSleep(inhibit, why)`** (platformhelpers.cpp 0x1400f79b0):
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)` to
  inhibit, `ES_CONTINUOUS` to restore, then "{} to {} display sleep" (Succeeded/Failed,
  inhibit/restore). Linux: `org.freedesktop.ScreenSaver` `Inhibit("reMarkable", why)` on
  the session bus, `UnInhibit(cookie)` to restore, one inhibition at a time
  (`ScreenSaverInhibitor`), the same log line.
- **`PlatformApp::shouldShowWhatsNew(version)`** (0x14003d3d0): the stored
  WhatsNewVersionRead (`getValue(WhatsNewVersionRead, "0")`, logged "Read version:");
  true when it does not split into two parts at "." (KeepEmptyParts), else when it sorts
  before `version` UTF-16 unit by unit (a prefix first). WhatsNewDialog
  (`whatsNewJson.showFirstTime`, version "3.29") then shows the release notes and its
  `markWhatsNewAsRead` stores the version. Client: the same, so a first start shows the
  dialog once; `core.config.WHATS_NEW_READ_DEFAULT` is that "0" (the tests' helper raises
  it, so their windows start clear; `WhatsNewTests` checks the dialog with "0").

### 5.15 Format guards, page counts and legacy erasers (audit fixes)

**Documents newer than 3.29.** The loader (0x1406611a0) reads `formatVersion` as
0x1406cefc0 does (`QJsonValue::toInt(0)` of the key, at least 1; 1 when missing) and
sets `loadErrorType` UnsupportedFormatVersion (2) when it is above 2 (0x1406cf2c0), so
the document does not load; page operations on it are refused ("Cannot add page, unable
to ensure correct format version", 0x140667af0). `loadError` is `loadErrorType != 0`
(0x14065fdc0); `loadErrorDescription` (0x140661af0) is per type: 1 "Unable to view
document. File format is outdated.", 2 "There's a mismatch in format versions. Try
updating your software.", 3 "Your PDF file seems to be missing. Try importing it
again.", 4 "Document is archived.", else "Something seems to be wrong. Please try to
restart.". An archived document (`archived`, 0x140669d10) gets CloudArchive (4).
main.qml's `requestOpenDocument` then shows `SoftwareUpdateDialog` for type 2 and
`UnableToViewDialog` (with the description) for the others instead of opening.

Client: `models.format_version_of`, `MAX_FORMAT_VERSION`, `Content.unsupported`,
`check_format`; `DocumentWrapper.loadError/loadErrorType/loadErrorDescription`
(UnsupportedFormatVersion, else CloudArchive); a newer document is also refused by
`edit_refusal`, `core.edits.document_files` / `current_content` (so a pending edit is
never applied to it on upload), `SyncClient._content_change` (tags) and
`clone_document`; SceneView will not load its pages if asked another way.

**Pages newer than 3.29.** The block reader (0x140a02fb0) knows, per block type, the
newest version it reads (0x140a079c0: 0x01 1, 0x02 2, 0x03 1, 0x04 1, 0x05 2, 0x06 2,
0x07 2, 0x08 1, 0x09 1, 0x0A 1, 0x0D 1, 0x0E 3, 0x0F 2; 0 for the rest). A block newer
than that whose `min_version` is above it stops the page; with `min_version` 0 it is
kept byte for byte (0x1409f6840). `rm_parser.block_unreadable` / `check_block` refuse a
page with such a block of a known type (parser and `rm_writer.split`, so it is neither
drawn from a guess nor written back). Blocks of types 3.29 does not read stay kept byte
for byte here, as before (the writer never re-encodes them); every cached page passes.

**Pages and templates.** `Content.parse` orders `cPages` by (idx, id), as
`core.pagemap.PageMap` does, and counts live pages for formatVersion 2 (§5.4). `.pagedata`
is not read for formatVersion 2 ("Skipping template/pagedata file loading for version
2", 0x140661df0): `document_pages` uses it for formatVersion 1 only.

**Legacy eraser strokes.** 3.29 draws lines through strokev2 pipelines where it has one
(0x140b2ace0: 22 Eraser, 23 Shader, 13/14 Pencil, 15 Ballpoint, 0x3a0010 solid tools);
Eraser (6) has none and goes to the QPainter renderer 0x140b38240, which sends it to
0x140b37da0: a rasteriser that copies the page's background image (the template or PDF
page under the ink) over the pixels the stroke covers. So an eraser stroke removes the
ink drawn before it and shows what was under the ink; it never paints white over a PDF
or an ebook. Client: `renderer.eraser_outline` (runs stroked at their widths, round caps
and joins) and `erased_after`; `paint` clips every item drawn before an eraser stroke by
the union of the later erasers, and `svg_scene` masks them (`<mask id="erase-n">`), so
the viewer, PNG, PDF (vector and raster) and SVG export agree. On the cached EPUB page
with two such strokes the client used to whiten 1,994 pixels of the book's text; now
none, and every pixel inside the eraser equals the page under it.

**Ctrl+Y.** ActionManager's Redo is `StandardKey.Redo`; a QML Shortcut takes the first
of the platform's bindings: Ctrl+Y on Windows, Ctrl+Shift+Z on GNOME (Wayland and X11),
so Ctrl+Y did nothing here. `ui.quick.shortcuts.install_redo_shortcut` adds a Shortcut
for Ctrl+Y on the same `redoAction` (enabled with it) where the platform's first Redo
binding is not Ctrl+Y already (else both would be ambiguous).

### 5.16 EPUB renditions made here; the cache collected after a sync

**EPUB to PDF** (`core/epub.py`). 3.29 makes an ebook's `<id>.pdf` itself
(doGeneratePdfFromEpub, EPubRenderer: a cached rendition says "Qt 6.10.3", the desktop
app's Qt; the tablet's say 5.15.2): a QTextDocument of the spine with default font the
document's `fontName` or "EB Garamond" at weight 400 and pixel size int(textScale x 38)
(0x14070de10, 0x140d27790); style sheet `p { line-height: %1 } h1, h2, h3 { margin-top:
2em; margin-bottom: 1em } code {font-family: 'Noto Sans Mono'}` (%1 lineHeight / 100 when
above 0, else 1: 0x1407117e0); root frame margins left/right the document's `margins`,
top/bottom an ebook setting (EpubSettings+0x168, not a constant; 50 px here, measured on
the cached renditions), document margin 0; a QPdfWriter at 227 dpi with pages (1404, 1872)
x 72 / 227 points (0x1406ec2d0: 445.3 x 593.8, the cached renditions' 445 x 594) and no page
margins. `SyncClient.import_file` makes it for an EPUB and uploads it with the book (and
its page count), so an imported ebook opens at once; one that cannot be laid out goes up
without, as before. Only what Qt's rich text reads of the XHTML is kept (no EPUB style
sheets); images, an `<svg>` cover's `<image>` too, are drawn; each spine document starts
a page.

**Cache collection** (`SyncClient.collect_garbage`, run by the app after every sync that
succeeded: `Syncer.collect`). Deleted: cached blobs nothing references (old index and file
versions) and thumbnails of document versions that are gone or of an older thumbnail
format. Kept: whatever the current tree references, every file pending edits use (by
sha256), local document copies' files, and the blobs of the documents open when the sync
started. It holds the sync lock and runs only when the tree was just refreshed with every
entry loaded (never after a cache-only load or a partial refresh). On a copy of the real
cache: 696 of 6,188 blobs and 3,146 thumbnails collected, nothing referenced lost, every
item still loads from the cache.

### 5.17 The white page card of a note page

What the viewer shows of a page is four rectangles, all in scene coordinates (x = 0 on
the paper's centre line):

- **The paper and content** (`SceneView` bounds): the frame (1404 x 1872, the PDF page, or
  a landscape notebook's note bounds) grown to the ink and typed text.
- **The exterior boundary** (`SceneView::sceneExteriorBoundary`, 0x140452410; read with
  DocumentViewport's `sceneExteriorMode: ExcludeMargin` on a note page): the content made
  symmetric about x = 0, united with the paper's portrait bounds (-w/2, 0, w, h)
  (0x1408a8eb0), whose sides become the note bounds' (-h/2..h/2, 0x1408a8df0) only for a
  landscape document (Document orientation 1) or when the content reaches past the
  portrait paper's sides; all cut to the scene's rect (the scene's own rect united with
  the note bounds), whose bottom is set to the lower of the content's and the portrait
  paper's. An unexpanded portrait page's exterior boundary is its paper, 1404 x 1872.
- **The scroll range** (`SceneTileManager.sceneBoundingRect`, stored as set:
  0x1403fdf00). DocumentViewport.updateSceneBoundingRect asks for the note bounds across
  (1872 wide, "We want all notebooks to use 'landscape' bounds") down to max(4/3 h,
  content bottom + room). 3.29's C++ SceneView sets it too: updateTileManagerScrollBoundary
  (0x140459330, "updating tile manager scroll boundary for note page to {}") makes it the
  exterior boundary with its height raised to max(its bottom, the content's bottom) + 0.8
  x the view's height / scale and its top at -(+0xf0)/scale. That is what keeps an
  unexpanded portrait page 1404 wide in 3.29.
- **The white card and its shadow** (DocumentView's ShadowOverlay, `pageBounds`): the
  scroll range, drawn twice as tall on note pages so its bottom is never seen.

The client used to implement only the QML half (the C++ slots were plumbing stubs), so
every note page got the 1872-wide range and card; 8e7cbf4 then painted the template over
that card, which made an unexpanded portrait page look expanded (rules past its right
edge and on below its bottom).

Now (`bridge/scene.py`): `SceneView.exterior_boundary()` is 0x140452410's; a note page's
scroll range is set from it on load, and whenever the QML sets the note bounds instead
(`_scroll_range_set`, from the tile manager) it is put back, as 3.29's SceneView does (the
extra room `onAreaSelected` adds above and below for a selection at the page's ends, top
< 0 and the same width, is kept). `sceneExteriorBoundary` is the exterior boundary.

**Deliberate differences from 3.29** (asked for): the white card is `page_card()`, the
exterior boundary's width with the page's own height (paper, or ink and text below it):
DocumentView's ShadowOverlay `bounds` are re-set to it after the official QML sets them
(its `boundsChanged`; no QML file is changed), so the card and its shadow end at the page
instead of running on below it, and a landscape page's card is 1404 tall (3.29's exterior
boundary reaches the portrait paper's 1872). The scroll range is that card plus the room
updateSceneBoundingRect gives a note page's content, (bottomMargin + bottomPadding /
scaleFactor) x devicePixelRatio, instead of 3.29's 0.8 view heights, which would scroll
onto bare panel. The template and ink are rendered over the card only
(`render_content(area=...)`): on a page whose ink reaches past the note bounds, the card
follows the ink and the rules stop where the template's own geometry ends (P Lines small:
the note bounds), as on 3.29. Initial zoom is unchanged (DocumentView.viewportScale uses
the paper height); fit-to-width now fits the paper, as the scroll range is its width.
`SceneView._card` is the rendered card, what `paint` shows; `SceneView._image` stays the
paper's own part of it. PDF pages, thumbnails and exports still show the paper only.

## 6. Implications for the Linux client

1. **Auth**: implement device/new → user/new with discovery-based host resolution and
   configurable fallbacks; parse JWT `exp` and the tectonic claim to choose the gateway;
   honour `rm-token-ttl-hint`.
2. **Sync**: implement `/sync/v4/root` + `/sync/v3/files/{hash}` hash-tree sync first.
3. **Rendering**: PDFium has Linux builds (or use poppler/pypdfium2); v6 `.rm` via an
   rmscene-style parser.
4. **Screen Share**: libdatachannel builds natively on Linux; reimplement room
   signalling + RFB/zlib decode. Highest-risk module — needs runtime traffic capture.
5. **Open items [?]**: `deviceDesc` value accepted for Linux (client defaults to
   `desktop-linux`, configurable); meaning of `secret` in device/new; role of the
   `mqttbroker` service; exact RFB message type IDs.
   A capture of the Windows app (e.g. mitmproxy under Wine/VM) resolves all of these.
