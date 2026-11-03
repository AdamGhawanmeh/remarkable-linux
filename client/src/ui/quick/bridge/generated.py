"""Classes with the QML-facing surface of the app's C++ classes, built from their moc
metadata (qml_api.json): every property (stored, with its notify signal), signal,
slot and invokable method, and enum.

    SceneController = qml_class("SceneController", behaviour=ControllerBehaviour)

Generated slots return their type's default, except setX(value), which sets property
x. `behaviour` is a plain class whose members (Slot-decorated methods, helpers,
_DEFAULTS) replace the generated ones: what this client actually does. The rest is
what the app does in features this client does not have (text editing, handwriting
conversion): accepted and answered with "nothing".

Properties are read from self._values and changed with self._set(name, value), which
emits the property's notify signal when the value changes.
"""

from __future__ import annotations

import json
import logging
from enum import IntEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import (Property, QByteArray, QDate, QDateTime, QEnum, QLine, QLineF, QObject, QPoint, QPointF, QRect,
                            QRectF, QSize, QSizeF, QTime, QUrl, Signal, Slot)
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QTransform

log = logging.getLogger("rm.ui.quick.generated")

API = json.loads((Path(__file__).with_name("qml_api.json")).read_text())

# QMetaType ids of builtin types → PySide types.
_BUILTIN: dict[int, Any] = {
    1: bool, 2: int, 3: int, 4: "qlonglong", 5: "qulonglong", 6: float, 7: str, 8: "QVariantMap", 9: "QVariantList",
    10: str, 11: "QStringList", 12: QByteArray, 14: QDate, 15: QTime, 16: QDateTime, 17: QUrl, 19: QRect, 20: QRectF,
    21: QSize, 22: QSizeF, 23: QLine, 24: QLineF, 25: QPoint, 26: QPointF, 31: "QVariant", 32: int, 33: int, 34: int,
    35: int, 36: int, 37: int, 38: float, 39: QObject, 40: int, 41: "QVariant", 45: "QVariant", 46: "QVariantMap",
    47: "QVariantList", 4096: QFont, 4099: QColor, 4102: QImage, 4107: QKeySequence, 4112: QTransform,
}
_NAMED: dict[str, Any] = {
    "QSize": QSize, "QSizeF": QSizeF, "QPoint": QPoint, "QPointF": QPointF, "QRect": QRect, "QRectF": QRectF,
    "QColor": QColor, "QTransform": QTransform, "QImage": QImage, "QUrl": QUrl, "QFont": QFont, "QLineF": QLineF,
    "QJsonArray": "QVariantList", "QJsonObject": "QVariantMap", "QJsonValue": "QVariant", "QVariant": "QVariant",
    "QVariantMap": "QVariantMap", "QVariantList": "QVariantList", "QJSValue": "QJSValue", "QByteArray": QByteArray,
    "QList<int>": "QVariantList", "QList<QUrl>": "QVariantList", "QList<QRectF>": "QVariantList",
    "QList<entry::Id>": "QStringList", "entry::Id": str, "qsizetype": "qlonglong", "qint64": "qlonglong",
    "QStringList": "QStringList", "QString": str,
}
_ENUM_NAMES = {e for c in API["classes"].values() for e in c["enums"]} | {"Qt::Edge", "Qt::Edges", "Qt::Orientation",
                                                                          "Qt::Alignment", "QImage::Format", "Qt::Key",
                                                                          "Qt::KeyboardModifiers", "Qt::InputMethodHints",
                                                                          "Qt::InputMethodQuery", "Qt::CursorShape"}


# Classes built by qml_class, by C++ class name (without namespace).
CLASSES: dict[str, type] = {}
# (class, module, name) registered as QML types by qml_class(register=...).
REGISTERED: set[tuple[type, str, str]] = set()


def qt_type(t: int | str) -> Any:
    """The PySide type for a moc type (builtin id or name)."""
    if isinstance(t, int):
        return _BUILTIN.get(t, "QVariant")
    if t in _NAMED:
        return _NAMED[t]
    if t.endswith("*"):     # a pointer to a class built here is typed as that class
        return CLASSES.get(t[:-1].replace("const ", "").split("::")[-1], QObject)
    if t.split("::")[-1] in _ENUM_NAMES or t in _ENUM_NAMES:
        return int
    return "QVariant"


def default(t: int | str) -> Any:
    kind = qt_type(t)
    if t == 43 or (isinstance(t, str) and t.endswith("*")):     # void, object pointers
        return None
    simple = {bool: False, int: 0, float: 0.0, str: "", "qlonglong": 0, "qulonglong": 0, "QStringList": [],
              "QVariantList": [], "QVariantMap": {}, QObject: None, "QVariant": None, "QJSValue": None}
    if kind in simple:
        return simple[kind]
    return kind()


class GeneratedBase:
    """Storage for generated properties."""

    _NOTIFY: dict[str, str | None] = {}
    _DEFAULTS: dict[str, Any] = {}

    def _get(self, name: str) -> Any:
        values = self.__dict__.setdefault("_values", {})
        if name not in values:
            spec_default = self._DEFAULTS.get(name, self._TYPE_DEFAULTS.get(name))
            if callable(spec_default) and not isinstance(spec_default, type):
                spec_default = spec_default()
            elif isinstance(spec_default, (list, dict)):
                spec_default = type(spec_default)(spec_default)
            values[name] = spec_default
        return values[name]

    def _set(self, name: str, value: Any) -> bool:
        if self._get(name) == value and type(self._get(name)) is type(value):
            return False
        self._values[name] = value
        self._property_changed(name)
        signal = self._NOTIFY.get(name)
        if signal:
            sig = getattr(self, signal)
            try:
                sig.emit()
            except TypeError:       # notify signals that carry the new value
                sig.emit(value)
        return True


    def _property_changed(self, name: str) -> None:
        """Called after a property changes (from QML or _set); behaviours override."""


def _getter(name: str):
    return lambda self: self._get(name)


def _setter(name: str):
    return lambda self, value: self._set(name, value)


def _slot_stub(cls_name: str, name: str, result: Any, prop: str | None):
    def stub(self, *args):
        if prop is not None and args:
            self._set(prop, args[0])
            return None
        log.debug("%s.%s: not available in this client", cls_name, name)
        return result() if callable(result) and not isinstance(result, type) else result
    stub.__name__ = name
    return stub


def qml_class(cpp: str | list[str], base: type = QObject, behaviour: type | None = None,
              name: str | None = None, register: tuple[str, str, str] | None = None) -> type:
    """A class with the members of the app's class `cpp` (a name, or the class and its
    bases, most derived first), on `base`, with `behaviour`'s members taking over.

    register=(module, name, "creatable" | "uncreatable") registers it as a QML type
    at once, as QmlElement does: a class must be registered before another class
    declares a property of its type, or that property is typed QObject* and QML
    cannot resolve its members (e.g. SceneView's keyHandler.flavor)."""
    names = [cpp] if isinstance(cpp, str) else list(cpp)
    name = name or names[0].split("::")[-1]
    own = {k: v for k, v in vars(behaviour).items()
           if k not in ("__dict__", "__weakref__", "__module__", "__qualname__", "__doc__")} if behaviour else {}
    inherited = base.staticMetaObject
    taken = {inherited.property(i).name() for i in range(inherited.propertyCount())}
    taken |= {bytes(inherited.method(i).name()).decode() for i in range(inherited.methodCount())}

    members: dict[str, Any] = {}
    enums: list[str] = []
    notify: dict[str, str | None] = {}
    type_defaults: dict[str, Any] = {}
    for cls in reversed(names):     # bases first; the derived class wins
        spec = API["classes"][cls]
        signals = [m for m in spec["methods"] if m["kind"] == "signal"]
        by_name: dict[str, list[list]] = {}
        for m in signals:
            by_name.setdefault(m["name"], []).append([qt_type(a[0]) for a in m["args"]])
        for sig, overloads in by_name.items():
            if sig not in taken and sig not in own:
                members[sig] = Signal(*overloads) if len(overloads) > 1 else Signal(*overloads[0])
        for enum, info in spec["enums"].items():
            if enum not in own:
                members[enum] = IntEnum(enum, info["keys"])
                enums.append(enum)
        properties = {p["name"] for p in spec["properties"]}
        for p in spec["properties"]:
            if p["name"] in taken or p["name"] in own:
                continue
            signal = signals[p["notify"]]["name"] if p["notify"] is not None and p["notify"] < len(signals) else None
            notify[p["name"]] = signal
            type_defaults[p["name"]] = default(p["type"])
            kwargs = {"notify": members[signal]} if signal in members else {"constant": True} if p["constant"] else {}
            members[p["name"]] = Property(qt_type(p["type"]), _getter(p["name"]),
                                          _setter(p["name"]) if p["writable"] else None, **kwargs)
        slots: dict[str, list[dict]] = {}
        for m in spec["methods"]:
            if m["kind"] in ("slot", "method") and not (not m["args"] and m["name"] in properties):
                slots.setdefault(m["name"], []).append(m)
        for slot, overloads in slots.items():
            if slot in taken or slot in own or slot in members:
                continue
            prop = slot[3].lower() + slot[4:] if slot.startswith("set") and len(slot) > 3 else None
            prop = prop if prop in properties and all(len(o["args"]) == 1 for o in overloads) else None
            func = _slot_stub(name, slot, default(overloads[0]["returns"]), prop)
            for o in overloads:
                result = qt_type(o["returns"])
                kwargs = {} if o["returns"] == 43 else {"result": result}
                func = Slot(*[qt_type(a[0]) for a in o["args"]], **kwargs)(func)
            members[slot] = func

    members.update(own)
    enums += [k for k, v in own.items() if isinstance(v, type) and issubclass(v, IntEnum)]
    members["_NOTIFY"] = {**notify, **own.get("_NOTIFY", {})}
    members["_TYPE_DEFAULTS"] = type_defaults
    bases = (base, GeneratedBase)
    namespace: dict[str, Any] = {}
    source = f"class {name}(*_bases):\n    locals().update(_members)\n" + "".join(
        f"    QEnum({e})\n" for e in dict.fromkeys(enums))
    exec(source, {"_bases": bases, "_members": members, "QEnum": QEnum}, namespace)
    cls = namespace[name]
    cls.__module__ = behaviour.__module__ if behaviour else __name__
    CLASSES[names[0].split("::")[-1]] = cls
    if register is not None:
        from PySide6.QtQml import qmlRegisterType, qmlRegisterUncreatableType
        uri, qml_name, kind = register
        if kind == "creatable":
            qmlRegisterType(cls, uri, 1, 0, qml_name)
        else:
            qmlRegisterUncreatableType(cls, uri, 1, 0, qml_name, f"{qml_name} is provided by the app")
        REGISTERED.add((cls, uri, qml_name))
    return cls
