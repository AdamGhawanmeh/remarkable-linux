"""Keyboard shortcuts 3.29 has on Windows that Linux's key bindings leave out.

main.qml's ActionManager binds Redo to StandardKey.Redo. Qt resolves that per platform:
on Windows its first binding is Ctrl+Y (the one a QML Shortcut takes), but on GNOME
(Wayland and X11) StandardKey.Redo is Ctrl+Shift+Z only, so Ctrl+Y did nothing here.
install_redo_shortcut adds Ctrl+Y beside it: a Shortcut owned by the window that
triggers the same ActionManager.redoAction, only while that action is enabled, so it
redoes exactly what the menu's Redo does. The official QML is not changed.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtQml import QQmlComponent, QQmlContext

log = logging.getLogger("rm.ui.quick.shortcuts")

REDO_SEQUENCES = ("Ctrl+Y",)

REDO_QML = b"""
import QtQuick

Shortcut {
    objectName: "linuxRedoShortcut"
    sequences: redoSequences
    context: Qt.WindowShortcut
    enabled: !!appWindow.actions && appWindow.actions.redoAction.enabled
    onActivated: appWindow.actions.redoAction.trigger()
}
"""


def missing_redo_sequences(bindings: list[str] | None = None) -> list[str]:
    """The REDO_SEQUENCES the platform's Redo does not already bind. A QML Shortcut on
    StandardKey.Redo takes only the first of the platform's bindings (Qt warns "Only
    binding to one of multiple key bindings"): where that is Ctrl+Y already (Windows,
    Qt's offscreen platform) adding it again would make both ambiguous, and neither
    would fire."""
    if bindings is None:
        from PySide6.QtGui import QKeySequence
        bindings = [k.toString() for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Redo)]
    first = bindings[0] if bindings else ""
    return [s for s in REDO_SEQUENCES if s != first]


def install_redo_shortcut(engine, window, sequences: list[str] | None = None) -> QObject | None:
    """Ctrl+Y for ActionManager.redoAction on `window` (main.qml's), where the platform's
    Redo does not bind it already (missing_redo_sequences, or `sequences`); None when
    nothing is to be added or the window has no ActionManager."""
    sequences = missing_redo_sequences() if sequences is None else list(sequences)
    if not sequences:
        return None
    if window is None or window.metaObject().indexOfProperty("actions") < 0:
        log.warning("no ActionManager in main.qml: Ctrl+Y not added")
        return None
    context = QQmlContext(engine.rootContext(), window)
    context.setContextProperty("appWindow", window)
    context.setContextProperty("redoSequences", sequences)
    component = QQmlComponent(engine)
    component.setData(QByteArray(REDO_QML), QUrl("qrc:/linux/RedoShortcut.qml"))
    shortcut = component.beginCreate(context)
    if shortcut is None:
        log.error("Ctrl+Y: %s", "; ".join(e.toString() for e in component.errors()))
        return None
    shortcut.setParent(window.contentItem())     # the window it listens in
    component.completeCreate()
    return shortcut
