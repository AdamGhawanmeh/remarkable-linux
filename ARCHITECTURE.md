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
| Erasing | Eraser strokes drawn in white | Items deleted in the CRDT: the stroke's own block stays, without its value and with `deletedLength`; what an eraser leaves of a stroke comes back as new strokes (see "Deleting and the eraser" below) |
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
text), and the eraser takes the layer's own line items only, leaving the text block byte
for byte as it was (checked on all 30 pages: draw, erase, put back). Anchored strokes
store coordinates relative to their text line, so the eraser does not reach the tablet's
anchored strokes yet.

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
  highlighter's own HighlighterYellow/Green/Pink/Blue/Gray. Defaults: Ballpointv2, size 2,
  Black; highlighter size 1, HighlighterYellow (as every cached document has). A settings-only
  change uploads `.content` alone; whether the app also bumps `.metadata` lastModified then
  is not traced.

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
  `0:0`; real pages have these, with the group item still live). A client eraser needs
  only the first part to erase the tablet's anchored strokes: compute the anchor position
  as above, test in scene coordinates, write the pieces into the same group.

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
  distinct source each time. The previews of a gesture cost nothing.
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

**Client deviation**: the client has no hold-to-straighten or shapes yet. To add them, a
client would have to supply what the tablet supplies here: the hold detection (time
and stillness, unknown) and the shape path for types 2–5 (unknown recogniser; the
ellipse is a 100-gon). The line case needs neither and could follow the rules above
exactly.

Not traced yet: sync upload rules, `insertItemsAtCursorPosition` (paste in text mode),
which tablet operation writes a moved stroke with one generation of ids, and the tablet-side
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
  those two says 10 with 15 live pages. The live `cPages` entries are what counts.
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

*Client deviation (edge cases only, not changed):* `core.models.Content.parse` takes
`redirectionPageMap[i]` per page, or no background past the map's end. The app uses `i` for
every page when the lengths differ, and drops every background when one value is out of
range. No real file reaches either case (all 61 maps are valid identity maps).

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
  | 5 | paper size, 2 × u32 | 1404 × 1872 (all) | **1404 × 1872 (all)** |
  | 6 | LWW `QRectF` (4 × f64: x, y, w, h) | **0,0,0,0** (3034/3034) | **non-zero** (41/41 that have it) |
  | 7 | 2 × f64 | 1404.0 × 1872.0 | 1404.0 × 1872.0 |
  | 8 | LWW 2 × f64 | 1404.0 × 1872.0 | 1404.0 × 1872.0 |
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
  it is optional. The client only writes SceneInfo when it creates a page (`rm_writer.blank_page`:
  zeros); it never rewrites an existing one.
- **Coordinates: same system, wider extent.** x is centred on the page (0 = middle),
  y = 0 at the top, in screen pixels. The PDF page fills [−W/2, W/2] × [0, H] with W, H =
  PDF size × 226/72 (§5.2). On the 46 annotated pages whose PDF is cached, 25 have ink
  beyond ±702 (the 1404 px screen) and 16 below y = 1872. None goes below the PDF's bottom.
  6 reach slightly past its side or top edge (≤ 8 % of W/2, margin scribbles). So the
  bounds follow the PDF page, not 1404 × 1872. Notebook pages also go past 1404 × 1872
  (182 of 4151 sideways, 55 downwards: scrolled or zoomed pages), so a reader must not
  clip either kind. EPUB pages behave like PDF pages: the `.pdf` rendition is the
  background.

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

**Deviations in this client.** `rm_text.edit_text` updates `textChars` and `textLines`.
The stroke and image writers leave `loads` as it was. The app would write `loads + 1` in
the first save after loading the page. To match it, add 1 once per page per editing
session (one replay of a page's pending edits onto its base), not once per stroke.

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
