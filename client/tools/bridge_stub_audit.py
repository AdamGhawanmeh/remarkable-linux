"""List the QML-facing members of the bridges (client/src/ui/quick/bridge) that do nothing:

- generated: slots qml_class made from the app's moc metadata (qml_api.json) that no
  behaviour class replaced (generated._slot_stub: they log at DEBUG and return their
  type's default; setX(value) slots of a property X are not counted, they set it);
- hand-written: @Slot methods whose body (after the docstring) is only `pass`, a
  `return` of a constant or an empty container, a log call, or self._unsupported(...).

Also says, for each, whether the app's own QML (resources/, carved by install.py) calls
it: `qml` lists the files that mention the member name after a dot.

    client/.venv/bin/python client/tools/bridge_stub_audit.py [--json | --check]

--check exits 1 when a no-op member is not in DOCUMENTED (the skip list and the other
reasons ARCHITECTURE.md 2.2 gives).
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BRIDGE = ROOT / "src" / "ui" / "quick" / "bridge"
RESOURCES = ROOT.parent / "resources"


# Every member the audit may find, with why it stays a no-op (ARCHITECTURE.md 2.2). The
# first group is the skip list: dead, OS-only or dangerous features of the app.
SKIPPED = {
    # Sparkle, the macOS/Windows self-updater: Linux packages update themselves.
    "PlatformApp.checkForUpdate": "os-only: Sparkle updater",
    "PlatformApp.canUpdateThroughSparkle": "os-only: Sparkle updater",
    "PlatformApp.sparkleEnabled": "os-only: Sparkle updater",
    # The tablet's retail demo mode.
    "Settings.retailDemoReset": "dangerous: retail demo reset",
    # Share sheets: none on Linux (the app's own fallback logs "not implemented").
    "ShareNative.exportFinished": "os-only: disabled ShareNative",
}
DEAD = "dead: never called by 3.29's desktop QML"
PLUMBING = "dead: C++-internal slot (connected to C++ signals only); the bridge does this work in Python"
INPUT = "os-only: input-method / on-screen keyboard / mobile hook"
SCENE_API = "dead: tablet scene API the desktop QML never calls; done by the client's own pen, selection and text code"
DOCUMENTED = dict(SKIPPED)
DOCUMENTED.update({m: PLUMBING for m in (
    "DocumentLockManager.setSleepState", "DocumentLockManager.pageModified", "DocumentLockManager.linesStored",
    "SceneController.onJobCompleted", "SceneController.onPagemapChanged", "SceneController.updateHasItemsToEdit",
    "SceneController.onAnnotationChanged", "SceneController.enableAutoReflow", "SceneController.disableAutoReflow",
    "SceneController.dumpScene", "SceneKeyHandler.process",
    "SceneTileManager.renderLineToTiles", "SceneTileManager.updatePolish", "SceneTileManager.resetPendingJobs",
    "SceneTileManager.setTextClampToBackground", "SceneTileManager.onTilesReady",
    "SceneTileManager.onSegmentTileReady", "SceneTileManager.onSceneChanged", "SceneTileManager.onJobQueueSizeChanged",
    "SceneTileManager.onWorkerDeleted", "SceneView.updateStatus", "SceneView.checkInitialTileSetReady",
    "SceneView.updateTileManagerScrollBoundary", "SceneView.updateSceneExteriorBoundary",
    "SceneView.updateTextContentBoundary", "SceneView.updateViewFromCursor", "SceneView.updateTileManagerRenderMargins",
    "SceneView.onDocumentZoomModeChanged", "SceneView.updateFocalPoint",
    "DocumentSync.requestProtocol", "DocumentSync.onDiskSpaceAvailableChangedSignificantly",
    "DocumentSync.onNewClientRegistration", "DocumentSync.onSyncProgressChanged",
    "PlatformApp.onSyncProtocolChanged", "PlatformApp.updateWorkerSettings", "PlatformApp.onLibraryReady",
    "PlatformHelpers.onCompleted", "Authentication.fetchUserToken", "Authentication.abortLogin",
    "DocumentController.execute", "DocumentController.onFinished", "DocumentController.onFailed")})
DOCUMENTED.update({m: INPUT for m in (
    "SceneController.setPreeditText", "SceneController.setComposeRange", "SceneController.clearComposeRange",
    "SceneController.composeWordAtCursorEnabled", "SceneController.setComposeWordAtCursorEnabled",
    "SceneController.replaceComposeText", "SceneController.beginInputMethodTransaction",
    "SceneController.endInputMethodTransaction", "SceneView.showVirtualKeyboard", "SceneView.hideVirtualKeyboard",
    "SceneView.inputMethodQuery", "PlatformHelpers.isCurrentKeyboard", "PlatformHelpers.vibrationFeedback",
    "DocumentWrapper.setKeyboardUsedDuringSession", "Controller.markKeyboardUsedDuringSession")})
DOCUMENTED.update({m: SCENE_API for m in (
    "SceneController.addDrawingLine", "SceneController.highlightWithLine", "SceneController.selectWithLine",
    "SceneController.eraseWithLine", "SceneController.clearLines", "SceneController.pasteText",
    "SceneController.insertImageAsSceneItem", "SceneController.createImageSnippetFromStroke",
    "SceneController.deleteText", "SceneController.replaceText",
    "ParagraphStyle.isNone", "ParagraphStyle.isImageBlock", "ParagraphStyle.isIndentable",
    "ParagraphStyle.isVisuallyIndented", "ParagraphStyle.visualIndentLevel", "ParagraphStyle.maxIndentLevel",
    "ParagraphStyle.isCheckboxChecked", "ParagraphStyle.isPartiallyChecked", "ParagraphStyle.isImplicitlyChecked",
    "ParagraphStyle.isUnchecked", "ParagraphStyle.fontSize")})
DOCUMENTED.update({
    "SceneController.requestDeferredCompleted": "nothing to do: thumbnails of edited pages already refresh (5.13)",
    "Settings.defaultValue": DEAD, "HandwritingLanguageModel.defaultCode": DEAD + " (answers en_US)",
    "EntryWrapper.baseFilePath": DEAD + " (no local document folder)",
    "EntryWrapper.getSource": DEAD + " (no local document folder)",
})
# Skipped as a whole, so not single no-op slots the audit finds: DeviceRecovery (USB
# reflashing: discovery answers FAIL), ShareNative's share calls (report "not available"),
# deleteAccount (macOS only: not in the Windows exe's metadata, nothing to bridge) and the
# telemetry upload /sync/reports/v1 (never sent).
SKIPPED_WHOLE = ("DeviceRecovery", "ShareNative", "deleteAccount", "/sync/reports/v1")


def generated_stubs() -> list[tuple[str, str]]:
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import ui.quick.bridge  # noqa: F401  (builds every class)
    from ui.quick.bridge import generated
    code = generated._slot_stub("x", "x", None, None).__code__
    found = []
    for name, cls in sorted(generated.CLASSES.items()):
        spec_props = set()
        for cpp in generated.API["classes"]:
            if cpp.split("::")[-1] == name:
                spec_props |= {p["name"] for p in generated.API["classes"][cpp]["properties"]}
        for member, value in vars(cls).items():
            func = getattr(value, "__wrapped__", value)
            if getattr(func, "__code__", None) is not code:
                continue
            prop = member[3].lower() + member[4:] if member.startswith("set") and len(member) > 3 else None
            if prop in spec_props:
                continue            # sets its property
            found.append((name, member))
    return found


def _is_noop(body: list[ast.stmt]) -> bool:
    stmts = body[1:] if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
        and isinstance(body[0].value.value, str) else body
    if not stmts:
        return True
    if len(stmts) > 2:
        return False
    for stmt in stmts:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Return):
            v = stmt.value
            if v is None or isinstance(v, ast.Constant) or (isinstance(v, (ast.List, ast.Dict, ast.Tuple)) and not
                                                             (getattr(v, "elts", None) or getattr(v, "keys", None))):
                continue
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in ("QImage", "QUrl", "QRectF",
                                                                                         "QSizeF", "QPointF") \
                    and not v.args:
                continue
            return False
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            f = stmt.value.func
            if isinstance(f, ast.Attribute) and (f.attr == "_unsupported" or
                                                 (isinstance(f.value, ast.Name) and f.value.id == "log")):
                continue
        return False
    return True


def handwritten_noops() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(BRIDGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                slot = any(isinstance(d, ast.Call) and getattr(d.func, "id", "") == "Slot" for d in fn.decorator_list)
                if slot and _is_noop(fn.body):
                    found.append((path.name, cls.name.lstrip("_"), fn.name))
    return found


def qml_callers(member: str) -> list[str]:
    if not RESOURCES.is_dir():
        return []
    pattern = re.compile(rf"\.{re.escape(member)}\b")
    hits = []
    for path in RESOURCES.rglob("*"):
        if path.suffix in (".qml", ".js") and pattern.search(path.read_text(errors="replace")):
            hits.append(str(path.relative_to(RESOURCES)))
    return sorted(hits)


def main() -> None:
    rows = [{"kind": "generated", "class": c, "member": m, "qml": qml_callers(m)} for c, m in generated_stubs()]
    rows += [{"kind": "hand-written", "file": f, "class": c, "member": m, "qml": qml_callers(m)}
             for f, c, m in handwritten_noops()]
    for r in rows:
        r["why"] = DOCUMENTED.get(f"{r['class']}.{r['member']}", "")
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
        return
    if "--check" in sys.argv:
        missing = [f"{r['class']}.{r['member']}" for r in rows if not r["why"]]
        print("undocumented no-op members:", missing or "none")
        sys.exit(1 if missing else 0)
    for r in rows:
        where = r.get("file", "generated")
        print(f"{r['kind']:12} {r['class'] + '.' + r['member']:58} {where:12} qml {len(r['qml'])}  {r['why']}")
    print(f"{len(rows)} no-op members")


if __name__ == "__main__":
    main()
