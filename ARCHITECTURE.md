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
  `linkAt` returns the PDF's links). Editing and handwriting conversion members exist
  and do nothing.

**Behaviour recovered from the binary**, where the QML does not define it:
`LibraryView::Filter` flags (`NativeDocuments=1, Collections=2, PDFs=4, Ebooks=8,
Pinned=16, Trashed=32, …, Documents=13`) and the sort orders; `ignoreParent` lists the
whole library, otherwise a folder's children; entry `subtext` = `Document::progressText`
("Page %1 of %2", ebooks "%1% read" = `roundf(page/(pages−1)·100)`), folders the
relative last-modified time (`StringUtils`, shared.cpp 0x140bc4e20: "just now" < 60 s,
then minutes/hours/days/30-day months/365-day years, `round`); `whatsNewJson` =
`:/misc/whatsnew_desktop.json`; language names `QLocale::nativeLanguageName` with the
first letter capitalised; 33 handwriting-conversion languages; `UserInfo.featureToggles
.clientEdit` (notebook editing, off here).

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
`lastModified`. The user's library shows reading does not change it: 105 documents were
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
- `POST /convert/v1/handwriting` — `application/vnd.myscript.jiix`.
- Screen-share broker: see §4.

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
| Stroke header | `i32 tool, i32 color, i32 _, f32 width` (+ `i32 _` in v5) | SceneLineItem (0x05) value: `u8 3`, then 1 tool, 2 color, 3 `thicknessScale` f64, 4 `startingLength` f32, 5 points, 6 timestamp, 7 `moveId`, 8 ARGB colour [O names; field numbers verified on real files] |
| Points | 6 × f32: x, y, speed, direction, width, pressure | Block v2: 14 bytes `f32 x, f32 y, u16 speed×4, u16 width×4, u8 direction×255/2π, u8 pressure×255`; block v1: 6 × f32 |
| Layers | Implicit, by position | TreeNode (0x02) with last-write-wins label/visibility, attached to root `0:1` via SceneGroupItem (0x04) |
| Coordinates | x from left edge, 0–1404 | x centred on the page (−702…702); pages can extend beyond 1404 × 1872 |
| Erasing | Eraser strokes drawn in white | Items deleted in the CRDT (`deleted_length`, tombstone 0x08); erase-section is not ink |
| Typed text | — | RootText (0x07): CRDT character sequence (left/right ids) + paragraph styles (last write wins) + box position/width |
| Images | — | SceneImagesTable (0x0E) + CrdtImageItem (0x0F) |
| Groups | — | Layers are children of root `0:1` (Layer 1 is usually `0:11`); groups nest inside layers via CrdtGroupItem. A group TreeNode may carry an **anchor** (LWW fields 7 char id, 8 type, 9 threshold, 10 origin x): its strokes' x is relative to origin x and y to the line holding that typed-text character, so handwriting follows the text |

Enums [O] from ordered name tables in `.rdata` (the 3-letter `Red` sits in an 8-byte gap
that `strings -n 4` hides):

- **Tool:** Paintbrush 0, Pencil 1, Ballpoint 2, Marker 3, Fineliner 4, Highlighter 5, Eraser 6,
  SharpPencil 7, EraseSection 8, ClearPage 9, ZoomTool 10, SelectionTool 11, Paintbrushv2 12,
  SharpPencilv2 13, Pencilv2 14, Ballpointv2 15, Markerv2 16, Finelinerv2 17, Highlighterv2 18,
  SolidPen 19, ReservedPen 20, Calligraphy 21, MaskedEraser 22, ShadingMarker 23.
- **Color:** Black 0, Gray 1, White 2, HighlighterYellow 3, HighlighterGreen 4,
  HighlighterPink 5, Blue 6, Red 7, HighlighterStackedGray 8, ArgbCode 9, Green 10, Cyan 11,
  Magenta 12, Yellow 13, HighlighterBlue 14, HighlighterOrange 15, HighlighterGray 16.

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

**Not 1:1** (needs decompilation; no engine was configured for REA): pencil and brush
textures (the app rasterises these itself: `renderPencil`, `renderPaintBrush_v2`, ...),
shader opacity, and typed-text font sizes (`crdt::TextProperties::FontSize`). The typed-text
fonts are the proprietary reMarkable Sans and reMarkable Serif Small, embedded in the exe;
they are not extracted or redistributed.

### 5.3 Page templates

The 65 built-in templates (resources/rcc_00d63c80/templates, copied to
client/src/assets/templates) are **JSON `.template` files, not images**: `constants`
(expressions), and `items` that are `group` (boundingBox + `repeat` rows/columns of
`down|up|right|left|infinite|<count>`), `path` (`M/L/C/Z` data, optional
stroke/fill colour, strokeWidth, antialiasing) or `text` (text, fontSize, position).
`templates.json` maps 79 names to files; 8 entries (Blank, Calligraphy, Hexagon) have no
`.template` file. [O] Parser strings in the binary: the variables `templateWidth/Height`,
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
