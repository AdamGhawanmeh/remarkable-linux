# PyInstaller spec for the standalone app, dist/remarkable-linux/ (install.py runs it).
#
# The client finds its assets relative to its own source files (Path(__file__) in
# ui/quick/assets.py, ui/quick/bridge/native.py, viewer/templates.py, ...), so its modules
# are not frozen: the bundle carries client/ at _internal/client/ (run.py; src/ with the
# extracted assets, remarkable.rcc, fonts, qml_api.json and libarkcontrols.so; the demo
# library from tests/), laid out as in the repository, and launcher.py runs run.py from
# there. Analysis still reads every client module (hiddenimports) to collect what they
# import; the modules are then dropped from the frozen archive so that Python imports the
# plain copies.
#
# Size: the PySide6 bindings the client never imports are excluded, the QML modules are
# limited to those the app's QML uses (hooks/hook-PySide6.QtQml.py), and Qt libraries and
# plugins of unused modules that plugins would pull in are dropped (DROP).
import os
import re
from pathlib import Path

REPO = Path(SPECPATH).resolve().parent
CLIENT = REPO / "client"
SRC = CLIENT / "src"
os.environ["REMARKABLE_RESOURCES"] = str(REPO / "resources")       # read by the QtQml hook
DEMO = ("demo_library", "fakecloud", "rmfixtures")                  # run.py --demo
ASSETS = ("fonts", "icons", "templates", "remarkable.rcc", "remarkable.rcc.stamp")

# PySide6 bindings the client does not use (it imports QtCore, QtGui, QtPdf, QtQml, QtQuick,
# QtQuickControls2, QtSvg and QtWidgets).
UNUSED_BINDINGS = (
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender", "QtBluetooth",
    "QtCharts", "QtConcurrent", "QtDataVisualization", "QtDesigner", "QtGraphs", "QtGraphsWidgets", "QtHelp",
    "QtHttpServer", "QtLocation", "QtMultimedia", "QtMultimediaWidgets", "QtNetworkAuth", "QtNfc",
    "QtOpenGLWidgets", "QtPdfWidgets", "QtPositioning", "QtPrintSupport", "QtQuick3D", "QtQuickTest",
    "QtQuickWidgets", "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus", "QtSerialPort",
    "QtSpatialAudio", "QtSql", "QtStateMachine", "QtSvgWidgets", "QtTest", "QtTextToSpeech", "QtUiTools",
    "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick", "QtWebEngineWidgets", "QtWebSockets", "QtWebView",
    "QtXml",
)
# Qt libraries and plugins of modules nothing kept links to (checked with readelf: the
# kept libraries and plugins need none of these). Libraries match wherever PyInstaller puts
# them: those found as dependencies land at the bundle's top level.
DROP = re.compile(
    r"(^|/)libQt6(3D\w*|Bluetooth|Charts\w*|DataVisualization\w*|Graphs\w*|Location|Multimedia\w*"
    r"|Nfc|Positioning\w*|Quick3D\w*|QuickParticles|QuickTimeline\w*|QuickTest|RemoteObjects\w*|Scxml\w*"
    r"|Sensors\w*|SerialBus|SerialPort|SpatialAudio|Sql|StateMachine\w*|Test|TextToSpeech|VirtualKeyboard\w*"
    r"|WebChannel\w*|WebEngine\w*|WebSockets|WebView\w*|QmlLocalStorage|QmlXmlListModel|PdfQuick"
    r"|QuickControls2(Material|Universal|Imagine|FluentWinUI3)\w*)\.so"
    r"|^PySide6/Qt/plugins/(qmltooling|sensors|position|geoservices|texttospeech|sqldrivers|multimedia|canbus"
    r"|webview|sceneparsers|renderers|geometryloaders|renderplugins|assetimporters|designer)/"
    r"|^PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin\.so$"
)


def client_modules():
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).with_suffix("")
        yield ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)


def data(dest, src):
    return (dest, str(src), "DATA")


def client_files():
    for path in sorted(SRC.rglob("*")):
        rel = path.relative_to(SRC)
        if not path.is_file() or "__pycache__" in rel.parts:
            continue
        if rel.parts[0] == "assets" and rel.parts[1] not in ASSETS:
            continue                    # anything else in assets/ is not used by the client
        yield data(f"client/src/{rel.as_posix()}", path)


CLIENT_MODULES = [*client_modules(), *DEMO]
CLIENT_TOP = {name.split(".")[0] for name in CLIENT_MODULES}

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[str(SRC), str(CLIENT / "tests")],
    hiddenimports=CLIENT_MODULES,
    hookspath=[os.path.join(SPECPATH, "hooks")],
    excludes=["tkinter", *(f"PySide6.{name}" for name in UNUSED_BINDINGS)],
)
a.pure = [entry for entry in a.pure if entry[0].split(".")[0] not in CLIENT_TOP]
a.binaries = [entry for entry in a.binaries if not DROP.search(entry[0])]
a.datas = [entry for entry in a.datas if not DROP.search(entry[0])]

client = [data("client/run.py", CLIENT / "run.py")]
client += [data(f"client/tests/{name}.py", CLIENT / "tests" / f"{name}.py") for name in DEMO]
client += list(client_files())

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="remarkable-linux", strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, client, name="remarkable-linux", strip=False, upx=False)
