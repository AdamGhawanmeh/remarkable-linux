# reMarkable Linux client (unofficial)

> **Unofficial, independent community project.** It is not affiliated with, endorsed
> by, sponsored by or supported by reMarkable AS. "reMarkable" is a trademark of
> reMarkable AS, used here only to say what this software works with. This repository
> contains **no** reMarkable code, interface files, fonts, icons or other assets; see
> [Legal](#legal).

A native Linux desktop client for your reMarkable library. It runs the official
reMarkable desktop app's own interface (its QML, fonts and icons, unmodified) on a
from-scratch Python/Qt implementation of everything underneath: cloud sync, the `.rm`
notebook renderer, PDF/EPUB viewing, page templates, PDF, PNG, SVG and `.rmdoc` export,
imports, folders, moves and the trash, and Screen Share from the tablet. It is a reader
and organiser: editing pages is not there yet. See
[Feature status and roadmap](#feature-status-and-roadmap) for exactly what works.

It works on a **bring-your-own-asset** basis: `install.sh` takes reMarkable's official
Windows installer (downloaded from reMarkable, or a copy you already have), unpacks
`reMarkable.exe` from it **without running it**, and extracts the interface, fonts,
icons, templates and translations on your machine. Nothing extracted is ever part of
this repository, and nothing leaves your computer.

## Prerequisites

- Linux on x86_64 (on other architectures, provide a Qt 6.10 SDK with `--qt`)
- Python 3.10 to 3.14, with `venv`
- CMake 3.21 or newer, a C++17 compiler (g++ or clang++), and ninja or make
- fontconfig (`fc-cache`), for installing the fonts
- A network connection, and about 2.5 GB of free space while building (most of it
  briefly, while Qt's archives unpack); the app itself takes 350 MB once the build files
  are removed

| Distribution | Command |
|---|---|
| Fedora | `sudo dnf install python3 cmake gcc-c++ ninja-build fontconfig` |
| Debian, Ubuntu | `sudo apt install python3 python3-venv cmake g++ ninja-build fontconfig` |
| Arch | `sudo pacman -S python cmake gcc ninja fontconfig` |

X11 sessions on Debian/Ubuntu may also need `libxcb-cursor0` for Qt.

## Quick start

```sh
git clone https://github.com/AdamGhawanmeh/remarkable-linux.git && cd remarkable-linux && ./install.sh
```

`install.sh` asks first, then works unattended for a few minutes:

```
Download official reMarkable Windows installer (~95 MB) from remarkable.com? [Y/n]:
Download Qt 6.10.3 build files (qtbase + qtdeclarative, ~140 MB) from download.qt.io into build/qt? [Y/n]:
Install the reMarkable fonts into ~/.local/share/fonts/remarkable-linux/? [Y/n]:
Install a "reMarkable Linux" launcher into ~/.local/share/applications/remarkable-linux.desktop (and its icon)? [Y/n]:
```

(The Qt question only appears if no Qt 6.10 SDK is found.) It then checks the
installer's SHA-256, unpacks `reMarkable.exe` from it without running it, extracts what
the client needs, builds the small native module in a temporary build environment, runs
the offline tests, and compiles the client into a **standalone app**,
`dist/remarkable-linux/`, which it self-tests before installing the launcher. Finally:

```
Build complete! Would you like to remove temporary build artifacts and downloads to free disk space (~1.1 GB)? [Y/n]:
```

Yes leaves the checkout as cloned plus `dist/`. No keeps everything, for development.

Already have the installer, or an installed `reMarkable.exe` (for example from a
Windows machine)? Pass it instead of downloading:

```sh
./install.sh --exe ~/Downloads/reMarkable-3.29.0.1804-win64.exe
./install.sh --exe /path/to/reMarkable.exe
```

For automated runs, `./install.sh --yes` answers every question with yes.

### Options

| Option | Effect |
|---|---|
| `--exe PATH` | Use this installer or `reMarkable.exe` instead of downloading |
| `-y`, `--yes`, `--non-interactive` | Answer yes to every question (downloads, fonts, launcher, cleanup) |
| `--qt SDK` | Build against this Qt 6.10 SDK (e.g. `~/Qt/6.10.3/gcc_64`) instead of looking in `build/qt` and `~/Qt` and otherwise downloading `qtbase` + `qtdeclarative` 6.10.3 with [aqtinstall](https://github.com/miurahr/aqtinstall) |
| `--python PATH` | The Python for the build environment (`PYTHON=... ./install.sh` also picks the one that runs the installer) |
| `--no-desktop` | Do not offer the fonts and launcher |
| `--no-tests` | Skip the offline tests |
| `--keep-build` | Keep the build files without asking |
| `--latest` | Use whatever installer reMarkable currently serves, even if untested |
| `--allow-untested` | Accept a `reMarkable.exe` other than the verified build |
| `--uninstall` | Remove the launcher, icon, fonts and `dist/remarkable-linux/` |

Re-running `install.sh` is safe; it reuses what it already downloaded, if you kept it.

### Supported version

The client is verified against **reMarkable desktop 3.29.0 (build 1804)**, whose
`reMarkable.exe` has SHA-256
`42390f0099e60e8712a37e1146355a16e993ba5c1ef14226750bcb2fefc376d3`. A newer release can
change its QML or the C++ types the QML expects, so by default `install.sh` downloads
3.29.0.1804 even when reMarkable's "latest" link points to something newer, and refuses
other builds unless you pass `--allow-untested` or `--latest`.

## Usage

Start **reMarkable Linux** from your applications menu (it also offers *Open Offline*
and *Open Demo Library*), or run the standalone app directly:

```sh
dist/remarkable-linux/remarkable-linux            # your library, then a background sync
dist/remarkable-linux/remarkable-linux --offline  # the cached library only, no network
dist/remarkable-linux/remarkable-linux --demo     # a built-in demo library, no account needed
```

The first time you start it, the app shows reMarkable's own log-in screen. Click
**Log in**: your browser opens <https://my.remarkable.com/device/desktop/connect>, which
shows an 8-letter one-time code. Paste the code into the app and click **Pair the app**.
Your library then syncs into the same window, and the app stays logged in from then on.
**Account > Log out** removes this computer from your account and deletes the local
copy of your library. The demo library opens only with `--demo`.

Your data stays in your home directory, outside the checkout:

| Path | Contents |
|---|---|
| `~/.config/remarkable-linux/` | Device and user tokens (`auth.json`, mode 0600), settings |
| `~/.cache/remarkable-linux/` | The synced library and thumbnails |

Imports, new folders, renames, moves, favorites and the trash change your reMarkable
cloud, as the official app does. So does closing a document: the page you were on and
when you opened it are saved (`lastOpenedPage`, `lastOpened`), so the document reopens
there on every device. Nothing else about the document changes. `--offline` never writes.

`dist/remarkable-linux/` contains reMarkable's extracted assets: it is for your own use,
not for sharing (see [Legal](#legal)).

### Development and tests

Keep the build files (`./install.sh --keep-build`, or answer no to the cleanup
question), then run the client from source and the tests with the build environment:

```sh
build/venv/bin/python client/run.py --demo
QT_QPA_PLATFORM=offscreen build/venv/bin/python -m unittest discover -s client/tests
```

The 403 offline tests use a fake cloud and synthetic documents; they need no account
or network. `install.sh` runs them before building the standalone app.
`client/tools/rm_roundtrip.py` checks the page writer against your own cached pages:
every page must come back byte for byte, and take an image without any existing block
changing (it prints counts only).

## Feature status and roadmap

What runs on screen is always the official app's interface. Behind it, some of the
app's features are implemented and some are not yet. Those not implemented are
disabled the way the official app disables them, or say "not available on Linux yet"
when you use them.

### Working

| Area | What works |
|---|---|
| Interface | The official desktop app's own QML, fonts, icons and translations, unmodified (1:1 UI) |
| Log in | The app's log-in wizard with a one-time code from my.remarkable.com; **Log out** removes this computer from your account |
| Cloud sync | Your library from reMarkable's cloud (sync v3/v4); documents download on demand |
| Sync status | Settings > Cloud > **Check sync** runs the official app's checks (network, log-in, internet, then a full sync) with its messages; sync progress ("Syncing (3 of 10)"), "No cloud connection" while the network is down, and "Unable to sync" when a sync fails |
| Offline cache | The cached library opens at once, also without a network; `--offline` never connects or writes |
| Library | My files and folders, Notebooks, PDFs, Ebooks, Favorites, Trash, sort orders, grid and list views, thumbnails |
| Search | Title search across the library (Ctrl+F) |
| Tags | Tags set on the tablet are listed in Tags, and the library filters by them |
| File operations | New folder, rename, move (drag, Move, Cut and Paste), favorite, move to trash, restore, delete, empty trash |
| Import | PDF and EPUB, from Import or by dragging files onto the window; imported PDFs open at once |
| Viewer | Notebooks (`.rm` v3/v5/v6: ink, highlighters, pencils, typed text, page templates), PDFs and EPUBs with your annotations, PDF links, page overview, zoom (Ctrl++, Ctrl+-, Ctrl+0), page turning (Left/Right arrows) |
| Reading position | Documents reopen at the page you left; synced like the official app |
| Export | PDF (vector: the original PDF pages, with your ink on top), SVG (one file per page: vector ink, templates and typed text; a PDF page underneath as an embedded image), PNG (one image per page at 226 DPI), `.rmdoc`; whole documents or selected pages, with progress and cancel |
| Screen Share | LiveView of your tablet's screen (Account > Screen Share, or accept the tablet's request) |

### In progress and known limitations

| Area | Status |
|---|---|
| Editing pages | Drawing, typed text (creating or editing text notes), new notebooks and quick sheets are not available. The app's own client-editing feature is off, so the UI hides or disables them, as the official app does without it. |
| Images on notebook pages (experimental) | Being built first of the editing features, and off unless asked for: `client/run.py --edit-notebook NAME` turns the app's editing interface on for the one notebook with that name. There, a PNG or JPEG file dropped on a page is put on it, stored as the official app stores one (scaled to at most 3200 px, PNG with transparency or else JPEG, at most 3 MiB); the page's existing content is kept byte for byte. The dropped image is selected in the app's own selection box; clicking an image (also one placed on the tablet) selects it. Drag the box to move it, a corner to resize, the round handle to turn it; Delete, Backspace or the bin icon deletes it. A move is saved the way the tablet saves one (the old item deleted, a new one with a `moveId`), and a deleted image's file leaves the document. The edit stays on this computer until `client/tools/push_edits.py` uploads it: run without arguments it prints exactly what would be sent, and uploads only with `--confirm DOCUMENT_ID`. Copy, cut, undo and selecting strokes are not there yet (cut refuses rather than deleting), and the other editing tools the interface shows (pens, text, page actions) do nothing yet. The first time editing turns on, the official app's "Desktop note-taking" introduction is shown. |
| Page management | Adding, moving, copying, duplicating and deleting pages are part of editing: disabled. |
| Editing tags | Adding or removing tags says "not available on Linux yet". Tags made on the tablet show and filter. |
| Duplicating documents | Copy, then Paste, of a document says "not available on Linux yet". Cut and Paste (moving) works. |
| Search in documents | The search bar inside a document opens but does not find anything yet. |
| EPUBs | Shown through the PDF version your tablet makes of them. An EPUB imported here opens once the tablet has opened it and synced; until then the viewer says so. There is no reflowing, font or margin setting. |
| Password-protected PDFs | Not supported. |
| Not on Linux | Converting handwriting to text, the share sheet (Share), scanning documents and in-app updates. |

## What install.sh puts where

| Location | What | Origin | Removed by the cleanup |
|---|---|---|---|
| `build/downloads/` | The official installer | reMarkable | yes |
| `build/qt/` | Qt 6.10.3 headers and libraries (only if no SDK was found) | download.qt.io | yes |
| `build/venv/` | PySide6 6.10.3, pypdf, aiortc, websockets, pyOpenSSL, PyInstaller | PyPI | yes |
| `build/arkcontrols/`, `build/pyinstaller/` | Build trees | generated | yes |
| `extracted/reMarkable.exe`, `extracted/remarkable.ico` | The app, its icon | the installer | yes |
| `resources/` | The app's Qt resource trees and `manifest.json` | `reMarkable.exe` | yes |
| `client/src/assets/` | `remarkable.rcc` (QML, images, translations), fonts, icons, page templates | `reMarkable.exe` | yes |
| `client/src/ui/quick/bridge/qml_api.json` | The QML API of the app's C++ classes | `reMarkable.exe` (moc metadata) | yes |
| `client/src/ui/quick/native/libarkcontrols.so` | The native module | built from `client/native/arkcontrols` | yes |
| `dist/remarkable-linux/` | The standalone app, with all of the above it needs | PyInstaller (`packaging/`) | no |
| `~/.local/share/fonts/remarkable-linux/` | reMarkable Sans / Serif and the icon fonts | `reMarkable.exe` | no |
| `~/.local/share/icons/hicolor/*/apps/remarkable-linux.png` | The app icon | `reMarkable.exe` | no |
| `~/.local/share/applications/remarkable-linux.desktop` | The launcher, starting `dist/remarkable-linux/remarkable-linux` | generated | no |

Everything in the checkout is listed in `.gitignore`, so `git status` stays clean.
`./install.sh --uninstall` removes the app and the last three.

### The standalone app

`packaging/remarkable-linux.spec` freezes `client/run.py` with PyInstaller into a
one-folder app. The client's own files stay plain files inside it, laid out as in the
repository, because the client finds its assets next to its source. To keep it small,
it leaves out the PySide6 modules the client never imports (Bluetooth, 3D, Sensors,
Test, WebEngine, Multimedia, ...), and `packaging/hooks/hook-PySide6.QtQml.py` bundles
only the QML modules the app's QML imports plus their dependencies (18 of the 102
PySide6 ships). `remarkable-linux --self-test` starts it on the demo library offscreen
and checks that the interface loads and that every Qt QML module the app's QML imports
is present; `install.sh` runs that before installing the launcher.

## How it works

`ARCHITECTURE.md` describes the reverse-engineered pieces: the cloud API (auth,
sync v3/v4, writes), the `.rm` v3/v5/v6 formats, the page templates, Screen Share
(WebRTC from the tablet), and how the app's QML is hosted on Python implementations
of its C++ types. The tools that extract the assets are in `client/tools/`:

| Tool | Does |
|---|---|
| `ifw_extract.py` | Reads the 7z archives appended to the Qt Installer Framework installer, using only the standard library |
| `rcc_carve.py` | Finds the app's `qRegisterResourceData` calls and carves each Qt resource tree, in registration order |
| `moc_api.py` | Reads the Qt moc metadata of the C++ classes the QML uses |
| `pe.py` | The PE parsing they share (imports, version and icon resources) |

```
install.sh, install.py      the bring-your-own-asset installer
packaging/                  the PyInstaller spec, launcher and QML hook for dist/
client/run.py               the app
client/src/core/            auth, sync, data model
client/src/viewer/          .rm parser and renderer, page templates
client/src/liveview/        Screen Share (signaling, DTLS, RFB)
client/src/ui/              pages, export, workers; ui/quick/ hosts the app's QML
client/native/arkcontrols/  C++: the ark.controls value type and the image providers
client/tests/               offline tests, fakes, and two manual CLIs (test_auth, test_sync)
client/tools/               the extraction tools above, and screenshots.py
```

## License

This project's code is free software, released under the
[GNU General Public License v3.0](LICENSE). The license covers only the code in this
repository. reMarkable's software, and the assets `install.sh` extracts from it on your
machine, are not part of this project and remain the property of reMarkable AS.

## Legal

- This is an independent, unofficial project. It is not affiliated with, endorsed by,
  or supported by reMarkable AS. All trademarks belong to their owners.
- The repository distributes only original code. It contains no part of reMarkable's
  software, interface files, fonts, icons, images or documentation. Those are extracted
  by `install.sh`, on your own machine, from an installer you obtain from reMarkable,
  and stay there. Do not commit, upload or redistribute them (the `.gitignore` keeps
  them out of git).
- The project follows an interoperability, bring-your-own-asset model: it exists so
  that people who use reMarkable's products can use their own account and documents on
  Linux, for which reMarkable offers no desktop app. You are responsible for having the
  right to use reMarkable's software and service, and their license and terms of
  service still apply to you.
- The client talks to reMarkable's cloud with your own account, the way the official
  app does. reMarkable can change that service at any time, which may break this
  client. Use it at your own risk; it comes with no warranty.
- None of this is legal advice. If you are unsure whether you may use this software
  where you live, ask a lawyer.
