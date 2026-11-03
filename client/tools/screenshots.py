#!/usr/bin/env python3
"""Screenshots of the client (the app's own QML) over the demo library, rendered on
the GPU offscreen, for checking the UI by eye.

    client/.venv/bin/python client/tools/screenshots.py [out_dir]

Prints the QML warnings each step produced (expected: none).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Qt Quick's software backend cannot draw MultiEffect (grid thumbnails); render with
# OpenGL, on the GUI thread (the threaded render loop hangs offscreen).
os.environ.setdefault("QT_QUICK_BACKEND", "rhi")
os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
os.environ.setdefault("QSG_RENDER_LOOP", "basic")

CLIENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CLIENT / "src"), str(CLIENT / "tests")]

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.quick.app import QuickApp, configure, prepare  # noqa: E402


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else CLIENT / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    prepare()
    app = QApplication([])
    configure(app)
    from core.sync import SyncClient
    from demo_library import build_demo_cloud
    from fakecloud import paired_auth
    from liveview.rfb import FramebufferUpdate, Rect, ServerHello
    from liveview.session import RoomOffered
    from ui.library import LibrarySource
    from ui.pages import DocumentPages

    tmp = Path(tempfile.mkdtemp())
    source = LibrarySource(SyncClient(paired_auth(tmp / "auth", build_demo_cloud()), tmp / "cache"))
    source.refresh()
    quick = QuickApp(source, tmp / "cache", config_dir=tmp)
    quick.load()
    quick.start(sync=False)
    window = quick.window
    window.resize(1270, 875)
    wait(2500)
    seen = len(quick.warnings)

    def shot(name: str, action=None, ms: int = 1500) -> None:
        nonlocal seen
        if action:
            action()
        wait(ms)
        window.grabWindow().save(str(out / f"{name}.png"))
        new = [w for w in quick.warnings[seen:] if "Only binding to one of multiple key bindings" not in w]
        seen = len(quick.warnings)
        print(f"{name}: {len(new)} warnings", *new, sep="\n  ")

    def click(x, y, double=False):
        (QTest.mouseDClick if double else QTest.mouseClick)(window, Qt.MouseButton.LeftButton,
                                                             Qt.KeyboardModifier.NoModifier, QPoint(x, y))

    escape = lambda: QTest.keyClick(window, Qt.Key.Key_Escape)
    shot("library", None, 500)
    shot("favorites", lambda: click(90, 388))
    shot("settings", lambda: click(90, 838))
    shot("notebook", lambda: (click(90, 68), wait(600), click(295, 370, True)), 3000)
    shot("page_overview", lambda: click(1153, 65), 2000)
    shot("back", lambda: (escape(), wait(400), escape()), 1000)
    shot("pdf", lambda: (click(90, 244), wait(800), click(295, 370, True)), 3500)
    escape()
    wait(800)

    # Screen Share: the share window main.qml opens, fed a demo page as the tablet would.
    client = quick.singletons.screen_share
    client._handle(RoomOffered("demo-room", "tablet"))
    shot("screen_share_offer", None, 1200)
    client.acceptConnection()
    for o in window.findChildren(object):
        if o.metaObject().className().startswith("ScreenShareAcceptDialog"):
            o.hide() if hasattr(o, "hide") else None
    doc = next(d for d in quick.library.tree.documents() if d.name == "Sketchbook")
    pages = DocumentPages(source, doc)
    page = pages.render(0, 0.5, crop="clip").convertToFormat(QImage.Format.Format_RGB16)
    pages.close()
    width, height = page.width(), page.height()
    client._handle(ServerHello(2, width, height))
    rows = b"".join(bytes(page.constScanLine(y))[:width * 2] for y in range(height))
    client._handle(FramebufferUpdate(((Rect(0, 0, width, height), rows),)))
    wait(1500)
    share = window.property("screenShareWindowObject")
    if share is not None:
        share.grabWindow().save(str(out / "screen_share.png"))
        print("screen_share: window", share.width(), "x", share.height())
    quick.shutdown()
    print("saved to", out)


if __name__ == "__main__":
    main()
