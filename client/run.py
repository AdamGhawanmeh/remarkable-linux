#!/usr/bin/env python3
"""reMarkable for Linux: the reMarkable desktop app's own interface (its QML, fonts and
icons, from reMarkable.exe) on this client's library, sync, renderer and Screen Share.

    client/.venv/bin/python client/run.py            your library: cached copy first, then a
                                                     background sync; imports, folders, moves and
                                                     the trash change your cloud; Screen Share from
                                                     your tablet (accept, or Account > Screen Share)
    client/.venv/bin/python client/run.py --offline  your cached library only, no network, read-only
    client/.venv/bin/python client/run.py --demo     built-in demo library on a fake cloud

The venv runs Qt 6.10.3, the Qt the app ships (see requirements.txt). Not logged in yet
(no device token in ~/.config/remarkable-linux/auth.json), the app's own log-in screen
asks for the one-time code from https://my.remarkable.com/device/desktop/connect and then
syncs your library into the window. The library is cached in ~/.cache/remarkable-linux/,
settings in ~/.config/remarkable-linux/settings.ini.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

# The app's own interface, fonts and API description are extracted from reMarkable.exe on
# this machine by install.py; none of them ship with the source.
_MISSING = [p for p in (HERE / "src" / "ui" / "quick" / "bridge" / "qml_api.json",
                        HERE / "src" / "ui" / "quick" / "native" / "libarkcontrols.so",
                        HERE / "src" / "assets" / "remarkable.rcc", HERE / "src" / "assets" / "fonts")
            if not p.exists()]
if _MISSING:
    sys.exit(f"{_MISSING[0]} is missing: set up the app first with  python3 install.py  (see README.md)")

from PySide6.QtCore import qVersion  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.auth import AuthClient  # noqa: E402
from core.config import cache_dir, config_dir  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from liveview.session import LiveViewService  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.quick.app import QuickApp, configure, prepare  # noqa: E402


def demo_app(workdir: Path) -> QuickApp:
    """The demo library (tests/demo_library.py) served by the FakeCloud."""
    sys.path.insert(0, str(HERE / "tests"))
    from demo_library import build_demo_cloud
    from fakecloud import paired_auth

    auth = paired_auth(workdir / "config", build_demo_cloud())
    source = LibrarySource(SyncClient(auth, workdir / "cache"))
    return QuickApp(source, workdir / "cache", config_dir=workdir / "config")


def make_app(args: argparse.Namespace, workdir: Path) -> tuple[QuickApp, bool]:
    """The app run.py starts and whether it syncs. The demo library only when asked for
    (--demo, --self-test): otherwise your account, and without a device token in auth.json
    the real app starts on its log-in screen; logging in there opens your library."""
    if args.demo or args.self_test:
        return demo_app(workdir), True          # the "sync" talks to the in-memory fake cloud
    auth = AuthClient()
    if args.offline and not auth.is_paired:
        print("Not logged in: showing the cached library only. Run without --offline to log in.")
    source = LibrarySource(SyncClient(auth), cached_only=args.offline)
    liveview = None if args.offline else (lambda emit: LiveViewService(auth, emit))
    quick = QuickApp(source, cache_dir(), config_dir=config_dir(), liveview_factory=liveview)
    return quick, not args.offline


# Qt notices that are not binding errors (the shipped Qt prints them too; ARCHITECTURE.md §2.1).
KNOWN_NOTICES: tuple[str, ...] = ("Only binding to one of multiple key bindings",)
# Qt 6.11 added a check the app's Qt (6.10.3) does not have: some of the app's components
# shadow a base member (RMIconButton.enabled, Background.data). The tests ignore it too.
if tuple(int(x) for x in qVersion().split(".")[:2]) >= (6, 11):
    KNOWN_NOTICES += ("overrides a member of the base object",)
APP_MODULES = ("common", "client", "ark", "xofm", "com.remarkable")


def self_test(app: QApplication, quick: QuickApp, seconds: float = 4.0) -> int:
    """Run the demo library for a few seconds, then check that main.qml loaded without QML
    warnings and that every Qt QML module the app's QML imports (including dialogs that
    load later) is available: install.py runs this on the standalone build."""
    import re

    from PySide6.QtCore import QDirIterator, QFile, QIODevice, QTimer
    from PySide6.QtQml import QQmlComponent

    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()
    problems = [w for w in quick.warnings if not any(n in w for n in KNOWN_NOTICES)]
    if quick.window is None:
        problems.append("main.qml did not load")
    imports: set[str] = set()
    for root in (":/qml", ":/ark-imports"):
        files = QDirIterator(root, ["*.qml", "*.js"], flags=QDirIterator.IteratorFlag.Subdirectories)
        while files.hasNext():
            file = QFile(files.next())
            if file.open(QIODevice.OpenModeFlag.ReadOnly):
                text = bytes(file.readAll()).decode("utf-8", "replace")
                imports.update(re.findall(r"^\s*import\s+([A-Za-z][\w.]*)", text, re.M))
    modules = sorted(m for m in imports if m.split(".")[0] not in APP_MODULES and m != "QtWebView")
    for module in modules:          # QtWebView: only the login web view, deliberately not bundled
        component = QQmlComponent(quick.engine)
        component.setData(f"import {module}\nimport QtQml\nQtObject {{}}\n".encode(), "selftest.qml")
        if component.isError():
            problems.append(f"QML module {module}: " + "; ".join(e.toString() for e in component.errors()))
    for problem in problems:
        print(f"self-test: {problem}")
    print(f"self-test: {'FAILED' if problems else 'ok'}: main.qml over the demo library, "
          f"{len(modules)} Qt QML modules, {len(problems)} problems")
    return 1 if problems else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="use the cached library only, no network")
    mode.add_argument("--demo", action="store_true", help="use the built-in demo library")
    mode.add_argument("--self-test", action="store_true",
                      help="start on the demo library, check the interface loads, and quit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    # websockets logs request headers (the Bearer token) at DEBUG; aioice and aiortc's SCTP every packet.
    for noisy in ("websockets", "aioice", "aiortc.rtcsctptransport"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    prepare()
    app = QApplication(sys.argv)
    app.setApplicationName("reMarkable")
    if not app.desktopFileName():             # the launcher install.py writes (Wayland app id)
        app.setDesktopFileName("remarkable-linux")
    configure(app)

    with tempfile.TemporaryDirectory(prefix="remarkable-demo-") as tmp:
        quick, sync = make_app(args, Path(tmp))
        quick.load()
        quick.start(sync=sync)
        try:
            return self_test(app, quick) if args.self_test else app.exec()
        finally:
            quick.shutdown()


if __name__ == "__main__":
    sys.exit(main())
