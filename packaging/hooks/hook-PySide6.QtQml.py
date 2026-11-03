# PyInstaller's hook-PySide6.QtQml, collecting only the QML modules the reMarkable app uses.
#
# The stock hook collects every QML module PySide6 ships (102), and through their plugins
# the Qt libraries behind them (Qt WebEngine alone is ~300 MB). Here the modules are those
# the app's QML imports (read from the carved resources, $REMARKABLE_RESOURCES), plus what
# their qmldir files require ("depends", "import", "default import"; not "optional import",
# which is how QtQuick.Controls lists the styles it is not using), plus the Basic style the
# client selects. In 3.29 that is 18 modules. QtWebView is left out on purpose: only
# WebViewWrapper.qml imports it, loaded by the login wizard and token dialog (the client is
# always logged in) and by a macOS-only entry of the account settings.
import os
import re
from pathlib import Path, PurePath

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info

APP_INTERNAL = re.compile(r"^(common|client|ark|xofm|com\.remarkable)(\.|$)")
ALWAYS = {"QtQuick.Controls.Basic", "QtQuick.Controls.Basic.impl"}     # app.prepare(): setStyle("Basic")
LEFT_OUT = {"QtWebView"}


def app_imports(resources: Path) -> set[str]:
    found = set()
    for path in resources.rglob("*"):
        if path.suffix in (".qml", ".js") and path.is_file():
            found.update(re.findall(r"^\s*import\s+([A-Za-z][\w.]*)", path.read_text(errors="ignore"), re.M))
    return {m for m in found if not APP_INTERNAL.match(m)}


def shipped_modules(qml_dir: Path) -> dict[str, tuple[str, set[str]]]:
    """module name -> (directory relative to qml/, the modules its qmldir requires)."""
    modules = {}
    for qmldir in qml_dir.rglob("qmldir"):
        text = qmldir.read_text(errors="ignore")
        name = re.search(r"^module\s+(\S+)", text, re.M)
        if name:
            requires = set(re.findall(r"^(?:depends|import|default import)\s+(\S+)", text, re.M))
            modules[name.group(1)] = (qmldir.parent.relative_to(qml_dir).as_posix(), requires)
    return modules


resources = Path(os.environ.get("REMARKABLE_RESOURCES", ""))
if not (resources / "manifest.json").is_file():
    raise SystemExit("hook-PySide6.QtQml: set REMARKABLE_RESOURCES to the carved resources/ (install.py does)")
qml_dir = Path(pyside6_library_info.location["QmlImportsPath"])
modules = shipped_modules(qml_dir)
keep, todo = set(), list((app_imports(resources) | ALWAYS) - LEFT_OUT)
while todo:
    name = todo.pop()
    if name in keep or name not in modules or name in LEFT_OUT:
        continue
    keep.add(name)
    todo += modules[name][1]
module_dirs = {directory: name for name, (directory, _) in modules.items()}


def used(dest: str) -> bool:
    """Whether a collected file (dest: its directory, PySide6/Qt/qml/<module dirs>) belongs
    to a kept module."""
    parts = PurePath(dest).parts
    inside = parts[parts.index("qml") + 1:]
    for depth in range(len(inside), 0, -1):                # the deepest module directory
        name = module_dirs.get("/".join(inside[:depth]))
        if name:
            return name in keep
    return True


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
qml_binaries, qml_datas = pyside6_library_info.collect_qtqml_files()
binaries += [(src, dest) for src, dest in qml_binaries if used(dest)]
datas += [(src, dest) for src, dest in qml_datas if used(dest)]
print(f"hook-PySide6.QtQml: {len(keep)} of {len(modules)} QML modules: {', '.join(sorted(keep))}")
