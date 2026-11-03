#!/usr/bin/env python3
"""Build and install the unofficial reMarkable Linux client from your own copy of
reMarkable's official Windows app ("bring your own asset"). Nothing proprietary ships
with this repository: the app's interface (QML), fonts, icons, templates and
translations are extracted on your machine from the installer reMarkable publishes.
Run it through ./install.sh.

    ./install.sh                     asks, then downloads the official Windows installer
    ./install.sh --exe reMarkable-3.29.0.1804-win64.exe    an installer you already have
    ./install.sh --exe /path/to/reMarkable.exe             or an installed app's exe
    ./install.sh --yes               accept every question (automated runs)
    ./install.sh --uninstall         remove the launcher, icon, fonts and dist/

It asks before each download (the reMarkable installer; a Qt SDK, if none is found)
and before installing anything into your home directory, then works unattended: get
reMarkable.exe (unpacked from the installer without running it) and check it is the
build this client was verified with; carve its Qt resources into the client's assets;
read its QML API; create a temporary build environment (build/venv); build the native
module (client/native/arkcontrols); run the offline tests; freeze the client into the
standalone app dist/remarkable-linux/ (PyInstaller) and self-test it; install the fonts
and the launcher. At the end it offers to delete everything but the source tree and
dist/.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent
CLIENT = REPO / "client"
TOOLS = CLIENT / "tools"
PACKAGING = REPO / "packaging"
BUILD = REPO / "build"
BUILD_VENV = BUILD / "venv"
VENV_PYTHON = BUILD_VENV / "bin" / "python"
EXTRACTED = REPO / "extracted"
RESOURCES = REPO / "resources"
ASSETS = CLIENT / "src" / "assets"
NATIVE_DIR = CLIENT / "src" / "ui" / "quick" / "native"
NATIVE_LIB = NATIVE_DIR / "libarkcontrols.so"
QML_API = CLIENT / "src" / "ui" / "quick" / "bridge" / "qml_api.json"
DIST = REPO / "dist"
APP_DIR = DIST / "remarkable-linux"
APP_BIN = APP_DIR / "remarkable-linux"

sys.dont_write_bytecode = True                 # leave no __pycache__ in the source tree
sys.path[:0] = [str(TOOLS), str(CLIENT / "src")]
import ifw_extract  # noqa: E402
import rcc_carve  # noqa: E402
from pe import PE  # noqa: E402
from ui.quick import assets  # noqa: E402

# The build this client was verified against.
TESTED_VERSION = "3.29.0.1804"
TESTED_INSTALLER = f"reMarkable-{TESTED_VERSION}-win64.exe"
TESTED_INSTALLER_SHA256 = "8ea2b5ee68004f0c02ee52ca2ad2bed2786899b40c514425a003c7d9ab0358aa"
TESTED_EXE_SHA256 = "42390f0099e60e8712a37e1146355a16e993ba5c1ef14226750bcb2fefc376d3"
LATEST_URL = "https://downloads.remarkable.com/latest/windows"
RELEASE_URL = "https://downloads.remarkable.com/desktop/production/win/{name}"
USER_AGENT = "remarkable-linux-installer"

QT_VERSION = "6.10.3"                 # the Qt reMarkable.exe ships, and PySide6 in requirements.txt
QT_ARCHIVES = ("qtbase", "qtdeclarative")
AQTINSTALL = "aqtinstall==3.3.0"

APP_ID = "remarkable-linux"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
FONT_DIR = DATA_HOME / "fonts" / APP_ID
ICON_THEME = DATA_HOME / "icons" / "hicolor"
DESKTOP_FILE = DATA_HOME / "applications" / f"{APP_ID}.desktop"

NOTICE = """This folder holds a standalone build of the unofficial reMarkable Linux client.
It contains files extracted from your copy of reMarkable's Windows app (its interface,
fonts, icons and templates), which belong to reMarkable AS. Keep it for your own use:
do not share, upload or redistribute it. Rebuild it from the source with ./install.sh.
"""


class InstallError(Exception):
    pass


class Declined(Exception):
    """The user said no to something the install cannot do without."""


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def info(message: str) -> None:
    print(f"    {message}", flush=True)


def home(path: Path) -> str:
    """`path` with the home directory written as ~ (for questions and messages)."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    try:
        return subprocess.run(cmd, check=True, **kwargs)
    except FileNotFoundError:
        raise InstallError(f"{cmd[0]} is not installed") from None
    except subprocess.CalledProcessError as e:
        raise InstallError(f"{' '.join(cmd[:4])}... failed (exit {e.returncode})") from None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def size_of(paths) -> int:
    total = 0
    for path in paths:
        if path.is_file() or path.is_symlink():
            total += path.lstat().st_size
        elif path.is_dir():
            for root, _, files in os.walk(path):
                total += sum(os.lstat(os.path.join(root, f)).st_size for f in files)
    return total


def human(size: int) -> str:
    return f"{size / 2**30:.1f} GB" if size >= 2**30 else f"{size / 2**20:.0f} MB"


class Prompt:
    """Yes/no questions ([Y/n]: Enter means yes). --yes answers them all."""

    def __init__(self, assume_yes: bool) -> None:
        self.assume_yes = assume_yes

    def __call__(self, question: str) -> bool:
        if self.assume_yes:
            print(f"{question}y (--yes)", flush=True)
            return True
        if not sys.stdin.isatty():
            raise InstallError("install.sh asks before it downloads or installs anything, and this is not an "
                               "interactive terminal: re-run with --yes to accept every question")
        while True:
            try:
                answer = input(question).strip().lower()
            except EOFError:
                print()
                return False
            if answer in ("", "y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("    please answer y or n")


# -- prerequisites ----------------------------------------------------------------

def check_prerequisites(python: str) -> None:
    step("Checking prerequisites")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise InstallError("run install.sh as your own user, not as root")
    version = subprocess.run([python, "-c", "import sys; print(*sys.version_info[:2])"],
                             capture_output=True, text=True)
    if version.returncode:
        raise InstallError(f"cannot run {python}")
    major, minor = map(int, version.stdout.split())
    if not (3, 10) <= (major, minor) <= (3, 14):
        raise InstallError(f"{python} is Python {major}.{minor}; PySide6 {QT_VERSION} needs Python 3.10 to 3.14 "
                           "(choose one with --python)")
    info(f"Python {major}.{minor}: {python}")
    cmake = shutil.which("cmake")
    if not cmake:
        raise InstallError("CMake is not installed (3.21 or newer)")
    found = re.search(r"(\d+)\.(\d+)", subprocess.run([cmake, "--version"], capture_output=True, text=True).stdout)
    if not found or tuple(map(int, found.groups())) < (3, 21):
        raise InstallError("CMake 3.21 or newer is needed")
    compiler = next((c for c in ("c++", "g++", "clang++") if shutil.which(c)), None)
    if not compiler:
        raise InstallError("no C++ compiler found (install g++ or clang++)")
    if not (shutil.which("ninja") or shutil.which("make")):
        raise InstallError("neither ninja nor make is installed")
    info(f"CMake {'.'.join(found.groups())}, {compiler}, {'ninja' if shutil.which('ninja') else 'make'}")


# -- reMarkable.exe ---------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def latest_installer_url() -> str:
    """Where reMarkable's 'latest Windows' link points to (without downloading it)."""
    request = urllib.request.Request(LATEST_URL, headers={"User-Agent": USER_AGENT})
    try:
        urllib.request.build_opener(_NoRedirect).open(request, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            return urllib.parse.urljoin(LATEST_URL, e.headers["Location"])
        raise InstallError(f"{LATEST_URL}: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise InstallError(f"cannot reach {LATEST_URL}: {e.reason}") from None
    raise InstallError(f"{LATEST_URL} did not redirect to an installer")


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done, shown = 0, -1
            while block := response.read(1 << 20):
                out.write(block)
                done += len(block)
                if total and sys.stdout.isatty() and done * 20 // total != shown:
                    shown = done * 20 // total
                    print(f"\r    {done >> 20} / {total >> 20} MB", end="", flush=True)
        if sys.stdout.isatty() and total:
            print()
    except urllib.error.HTTPError as e:
        partial.unlink(missing_ok=True)
        raise InstallError(f"{url}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        partial.unlink(missing_ok=True)
        raise InstallError(f"downloading {url} failed: {e}") from None
    partial.replace(target)


def cached_installer() -> Path | None:
    target = BUILD / "downloads" / TESTED_INSTALLER
    return target if target.exists() and sha256(target) == TESTED_INSTALLER_SHA256 else None


def download_installer(latest: bool) -> Path:
    step("Downloading the official reMarkable Windows installer")
    url = latest_installer_url()
    name = url.rsplit("/", 1)[-1]
    match = re.fullmatch(r"reMarkable-([\d.]+)-win64\.exe", name)
    version = match.group(1) if match else "?"
    info(f"latest: {name}")
    if version != TESTED_VERSION and not latest:
        info(f"this client was verified with {TESTED_VERSION}; downloading that build "
             f"(--latest uses {version} instead)")
        name, url = TESTED_INSTALLER, RELEASE_URL.format(name=TESTED_INSTALLER)
    target = BUILD / "downloads" / name
    if name == TESTED_INSTALLER and cached_installer():
        info(f"already downloaded: {target}")
        return target
    info(f"{url}\n    -> {target}")
    download(url, target)
    if name == TESTED_INSTALLER and sha256(target) != TESTED_INSTALLER_SHA256:
        target.unlink()
        raise InstallError(f"{name} does not have the expected SHA-256; download it again")
    return target


def is_app_exe(path: Path) -> bool:
    try:
        PE.open(path).import_slot(rcc_carve.REGISTER)
        return True
    except (ValueError, LookupError):
        return False


def obtain_exe(source: Path | None, latest: bool) -> Path:
    """reMarkable.exe from `source` (an installer is unpacked to build/unpacked/), the one
    extracted before, or a downloaded installer; install_exe() keeps it once verified."""
    target = EXTRACTED / "reMarkable.exe"
    if source is None:
        if target.exists() and sha256(target) == TESTED_EXE_SHA256 and not latest:
            step("Using the reMarkable.exe extracted before")
            info(str(target))
            return target
        source = (None if latest else cached_installer()) or download_installer(latest)
    source = source.expanduser().resolve()
    if not source.is_file():
        raise InstallError(f"{source} does not exist")
    if is_app_exe(source):
        step(f"Using {source}")
        return source
    step(f"Unpacking reMarkable.exe from {source.name} (without running it)")
    unpacked = BUILD / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    try:
        written = ifw_extract.extract(source, unpacked, {"reMarkable.exe"})
    except (ValueError, OSError) as e:
        raise InstallError(f"{source.name}: {e}") from None
    if "reMarkable.exe" not in written:
        raise InstallError(f"{source.name} is neither reMarkable.exe nor a reMarkable installer")
    return written["reMarkable.exe"]


def verify_exe(exe: Path, allow_untested: bool) -> None:
    digest = sha256(exe)
    if digest == TESTED_EXE_SHA256:
        info(f"reMarkable.exe {TESTED_VERSION}, SHA-256 verified")
        return
    version = PE.open(exe).version() or "unknown version"
    if version == TESTED_VERSION:
        message = (f"{exe.name} says it is {version} but its SHA-256 ({digest[:16]}...) differs from that "
                   "build's: it may be damaged or modified")
    else:
        message = (f"{exe.name} is {version}, not the build this client was verified with ({TESTED_VERSION}); "
                   "its QML may need C++ types the client does not provide")
    if not allow_untested:
        raise InstallError(message + ". Re-run with --allow-untested to try it anyway.")
    info("WARNING: " + message)


def install_exe(exe: Path) -> Path:
    """Keep the verified exe in extracted/ (reused by the next run)."""
    target = EXTRACTED / "reMarkable.exe"
    EXTRACTED.mkdir(exist_ok=True)
    if exe.resolve() != target.resolve():
        shutil.copyfile(exe, target)
    if (BUILD / "unpacked").exists():
        shutil.rmtree(BUILD / "unpacked")
    return target


# -- assets ------------------------------------------------------------------------

def extract_assets(exe: Path) -> None:
    step("Extracting the app's resources (QML, fonts, icons, templates, translations)")
    if RESOURCES.exists():
        shutil.rmtree(RESOURCES)
    manifest = rcc_carve.carve(exe, RESOURCES)
    info(f"{len(manifest)} files from {len({e['tree'] for e in manifest})} resource trees -> {RESOURCES}")

    files = assets.carved_files(RESOURCES)         # qrc path -> bytes; the first registered tree wins
    for sub in ("fonts", "icons", "templates"):
        if (ASSETS / sub).exists():
            shutil.rmtree(ASSETS / sub)
    assets.copy_fonts(RESOURCES, ASSETS / "fonts")
    for path, data in files.items():
        if path.startswith("/ark/"):                       # Ark icons (theme.py)
            target = ASSETS / "icons" / path[len("/ark/"):]
        elif path.startswith("/templates/"):               # page templates (viewer/templates.py)
            target = ASSETS / "templates" / path[len("/templates/"):]
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    missing = [name for name in assets.FONT_FILES if not (ASSETS / "fonts" / name).exists()]
    if missing:
        raise InstallError(f"fonts not found in reMarkable.exe: {', '.join(missing)}")
    rcc = ASSETS / "remarkable.rcc"
    assets.build_rcc(rcc, RESOURCES, overrides={})
    rcc.with_name(rcc.name + ".stamp").write_text(assets._stamp({}))
    (EXTRACTED / "remarkable.ico").write_bytes(PE.open(exe).icon() or b"")
    info(f"client assets -> {ASSETS}")

    step("Reading the QML API of the app's C++ classes (moc metadata)")
    run([sys.executable, TOOLS / "moc_api.py", exe])


# -- build environment, Qt and the native module -------------------------------------

def make_build_venv(python: str) -> None:
    step("Setting up the temporary build environment (build/venv)")
    if not VENV_PYTHON.exists():
        result = subprocess.run([python, "-m", "venv", str(BUILD_VENV)])
        if result.returncode:
            raise InstallError("creating the virtual environment failed (on Debian/Ubuntu: "
                               "sudo apt install python3-venv)")
    run([VENV_PYTHON, "-m", "pip", "install", "--disable-pip-version-check", "-q",
         "-r", CLIENT / "requirements.txt", "-r", PACKAGING / "requirements.txt"])
    info("installed client/requirements.txt and packaging/requirements.txt (PyInstaller) from PyPI")


def qt_sdk_version(prefix: Path) -> str | None:
    """The version of the Qt SDK at `prefix` if it has what the native module needs."""
    needed = (prefix / "include" / "QtQuick", prefix / "lib" / "cmake" / "Qt6Qml", prefix / "libexec" / "moc")
    if not all(p.exists() for p in needed):
        return None
    try:                                         # moc runs on this machine
        subprocess.run([str(prefix / "libexec" / "moc"), "-v"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    for name in ("Qt6CoreConfigVersionImpl.cmake", "Qt6CoreConfigVersion.cmake"):
        try:
            found = re.search(r'PACKAGE_VERSION "([\d.]+)"', (prefix / "lib" / "cmake" / "Qt6Core" / name).read_text())
        except OSError:
            continue
        if found:
            return found.group(1)
    return None


def find_qt(explicit: Path | None) -> Path | None:
    """An existing Qt 6.10 SDK (--qt, build/qt or ~/Qt), or None."""
    wanted = QT_VERSION.rsplit(".", 1)[0] + "."
    if explicit:
        explicit = explicit.expanduser().resolve()
        version = qt_sdk_version(explicit)
        if not version or not version.startswith(wanted):
            raise InstallError(f"{explicit} is not a Qt {wanted}x SDK with QtQuick (expected e.g. "
                               f"~/Qt/{QT_VERSION}/gcc_64)")
        return explicit
    for candidate in (BUILD / "qt" / QT_VERSION / "gcc_64", Path.home() / "Qt" / QT_VERSION / "gcc_64"):
        version = qt_sdk_version(candidate)
        if version and version.startswith(wanted):
            return candidate
    return None


def download_qt() -> Path:
    step(f"Downloading Qt {QT_VERSION} ({' + '.join(QT_ARCHIVES)}) from download.qt.io with aqtinstall")
    run([VENV_PYTHON, "-m", "pip", "install", "--disable-pip-version-check", "-q", AQTINSTALL])
    prefix = BUILD / "qt" / QT_VERSION / "gcc_64"
    if prefix.parent.exists():
        shutil.rmtree(prefix.parent)
    log = BUILD / "aqt.log"
    with open(log, "w") as out:
        result = subprocess.run([str(BUILD_VENV / "bin" / "aqt"), "install-qt", "linux", "desktop", QT_VERSION,
                                 "linux_gcc_64", "--outputdir", str(BUILD / "qt"), "--archives", *QT_ARCHIVES],
                                stdout=out, stderr=subprocess.STDOUT, cwd=BUILD)      # its aqtinstall.log too
    if result.returncode or not qt_sdk_version(prefix):
        print(log.read_text()[-3000:])
        raise InstallError(f"downloading Qt failed (log: {log})")
    # Only headers, CMake files, moc and the shared libraries are used. The static
    # libraries (1.1 GB) become empty archives: Qt's CMake files check that they exist, and
    # linking one by mistake would fail loudly. (aqt's qmake check fails without ICU;
    # nothing here needs it.)
    for archive in (prefix / "lib").glob("*.a"):
        archive.write_bytes(b"!<arch>\n")
    info(f"{prefix} ({human(size_of([prefix]))})")
    return prefix


def build_native(qt: Path) -> None:
    step(f"Building the native module (client/native/arkcontrols) with Qt {qt_sdk_version(qt)}")
    info(str(qt))
    build_dir = BUILD / "arkcontrols"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    generator = ["-G", "Ninja"] if shutil.which("ninja") else []
    run(["cmake", "-S", CLIENT / "native" / "arkcontrols", "-B", build_dir, *generator,
         f"-DCMAKE_PREFIX_PATH={qt}", "-DCMAKE_BUILD_TYPE=Release"], stdout=subprocess.DEVNULL)
    run(["cmake", "--build", build_dir, "--parallel"], stdout=subprocess.DEVNULL)
    if not NATIVE_LIB.exists():
        raise InstallError(f"the build did not produce {NATIVE_LIB}")
    info(str(NATIVE_LIB))


def run_tests() -> None:
    step("Running the offline test suite")
    result = subprocess.run([str(VENV_PYTHON), "-m", "unittest", "discover", "-s", str(CLIENT / "tests")],
                            capture_output=True, text=True, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    output = result.stdout + result.stderr
    ran = re.findall(r"^Ran (\d+) tests? in", output, re.M)
    if result.returncode or not ran:
        print(output[-6000:])
        raise InstallError(f"the offline tests failed (exit {result.returncode})")
    info(f"{ran[-1]} tests passed")


# -- the standalone app ---------------------------------------------------------------

def freeze() -> None:
    step("Building the standalone app (PyInstaller) -> dist/remarkable-linux/")
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    log = BUILD / "pyinstaller.log"
    with open(log, "w") as out:
        result = subprocess.run([str(VENV_PYTHON), "-m", "PyInstaller", "--noconfirm", "--clean",
                                 "--log-level", "WARN", "--workpath", str(BUILD / "pyinstaller"),
                                 "--distpath", str(DIST), str(PACKAGING / "remarkable-linux.spec")],
                                stdout=out, stderr=subprocess.STDOUT)
    if result.returncode or not APP_BIN.exists():
        print(log.read_text()[-4000:])
        raise InstallError(f"PyInstaller failed (log: {log})")
    kept = re.search(r"hook-PySide6\.QtQml: (\d+ of \d+) QML modules", log.read_text())
    # A frozen app never writes __pycache__: compile the client copy once for a fast start.
    run([VENV_PYTHON, "-m", "compileall", "-q", APP_DIR / "_internal" / "client"], stdout=subprocess.DEVNULL)
    (APP_DIR / "NOT-FOR-REDISTRIBUTION.txt").write_text(NOTICE)
    info(f"{APP_BIN} ({human(size_of([APP_DIR]))}{', ' + kept.group(1) + ' QML modules' if kept else ''})")

    step("Self-testing the standalone app (offscreen, demo library)")
    try:
        result = subprocess.run([str(APP_BIN), "--self-test"], capture_output=True, text=True, timeout=180,
                                env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    except subprocess.TimeoutExpired:
        raise InstallError(f"{APP_BIN} --self-test did not finish") from None
    verdict = [line for line in result.stdout.splitlines() if line.startswith("self-test:")]
    if result.returncode or not verdict or "FAILED" in verdict[-1]:
        print((result.stdout + result.stderr)[-4000:])
        raise InstallError("the standalone app failed its self-test")
    info(verdict[-1].removeprefix("self-test: "))


# -- desktop integration -------------------------------------------------------------

ICON_SCRIPT = """
import sys
from pathlib import Path
from PySide6.QtGui import QGuiApplication, QImageReader
app = QGuiApplication(["icons"])
source, theme, name = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
reader = QImageReader(source)
sizes = []
for index in range(max(reader.imageCount(), 1)):
    reader.jumpToImage(index)
    image = reader.read()
    if image.isNull() or image.width() != image.height() or image.width() in sizes:
        continue
    folder = theme / f"{image.width()}x{image.height()}" / "apps"
    folder.mkdir(parents=True, exist_ok=True)
    image.save(str(folder / f"{name}.png"))
    sizes.append(image.width())
print(" ".join(map(str, sorted(sizes))))
"""


def desktop_quote(arg: str) -> str:
    """One argument of a desktop entry's Exec key."""
    if re.fullmatch(r"[\w/.+,:=@-]+", arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def install_fonts() -> None:
    step("Installing the fonts for your user")
    try:
        assets.install_user_fonts(ASSETS / "fonts", FONT_DIR)
        info(f"{FONT_DIR} (font cache updated)")
    except (FileNotFoundError, subprocess.CalledProcessError):
        info(f"{FONT_DIR} (fc-cache failed or is missing: the fonts appear after the next login)")


def install_launcher() -> None:
    step("Installing the icon and the desktop launcher")
    result = subprocess.run([str(VENV_PYTHON), "-c", ICON_SCRIPT, str(EXTRACTED / "remarkable.ico"), str(ICON_THEME),
                             APP_ID], capture_output=True, text=True, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    if result.returncode or not result.stdout.strip():
        raise InstallError("converting the app icon failed:\n" + result.stderr[-2000:])
    info(f"{ICON_THEME}/{{{','.join(f'{s}x{s}' for s in result.stdout.split())}}}/apps/{APP_ID}.png")
    command = desktop_quote(str(APP_BIN))
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(f"""[Desktop Entry]
Type=Application
Name=reMarkable Linux
GenericName=reMarkable client (unofficial)
Comment=Your reMarkable library, documents and Screen Share. Unofficial; not affiliated with reMarkable AS.
Exec={command}
TryExec={APP_BIN}
Icon={APP_ID}
Terminal=false
StartupNotify=true
Categories=Office;Viewer;
Keywords=remarkable;notes;notebook;pdf;epub;tablet;screen share;
Actions=offline;demo;

[Desktop Action offline]
Name=Open Offline
Exec={command} --offline

[Desktop Action demo]
Name=Open Demo Library
Exec={command} --demo
""")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", "-q", str(DESKTOP_FILE.parent)])
    info(f"{DESKTOP_FILE} -> {APP_BIN}")


# -- cleanup and uninstall -----------------------------------------------------------

def build_artifacts() -> list[Path]:
    """What install.py generated besides dist/: downloads, the Qt SDK, the build
    environment and build trees (build/), the extracted exe and icon, the carved
    resources, the client's assets, qml_api.json, the native module, and Python caches."""
    paths = [BUILD, RESOURCES, EXTRACTED / "reMarkable.exe", EXTRACTED / "remarkable.ico", QML_API, NATIVE_LIB]
    paths += [ASSETS / name for name in ("fonts", "icons", "templates", "remarkable.rcc", "remarkable.rcc.stamp")]
    for top in (CLIENT, PACKAGING):
        paths += [p for p in top.rglob("__pycache__") if not any(part in (".venv", "venv") for part in p.parts)]
    return [p for p in paths if p.exists()]


def remove(paths: list[Path]) -> None:
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    for directory in (EXTRACTED, ASSETS, NATIVE_DIR):          # now empty, unless they hold files of yours
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def offer_cleanup(ask: Prompt) -> None:
    paths = build_artifacts()
    size = human(size_of(paths))
    print()
    if not ask(f"Build complete! Would you like to remove temporary build artifacts and downloads to free disk "
               f"space (~{size})? [Y/n]: "):
        info("kept build/, extracted/, resources/ and the client's assets: build/venv/bin/python client/run.py runs "
             "the app from source, and the tests run as in README.md")
        return
    remove(paths)
    info(f"removed {size}: build/, the extracted and carved files, the client's assets, the native module and "
         "Python caches. The checkout is back to its source files, plus dist/.")


def uninstall(ask: Prompt) -> None:
    targets = [p for p in (DESKTOP_FILE, *ICON_THEME.glob(f"*/apps/{APP_ID}.png"), FONT_DIR, APP_DIR) if p.exists()]
    if not targets:
        step("Nothing to uninstall")
        return
    step("Uninstalling")
    for path in targets:
        info(home(path))
    if not ask("Remove these? [Y/n]: "):
        raise Declined("nothing was removed")
    for path in targets:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    if FONT_DIR in targets and shutil.which("fc-cache"):
        subprocess.run(["fc-cache", "-f"], capture_output=True)
    if shutil.which("update-desktop-database") and DESKTOP_FILE.parent.exists():
        subprocess.run(["update-desktop-database", "-q", str(DESKTOP_FILE.parent)])
    if DIST.is_dir() and not any(DIST.iterdir()):
        DIST.rmdir()
    info("removed. Build files, if you kept them, are listed by `git clean -ndX`.")


# -- main ----------------------------------------------------------------------------

@dataclass
class Plan:
    download_installer: bool
    qt: Path | None                 # an existing SDK; None: download one
    fonts: bool
    launcher: bool


def plan(args, ask: Prompt) -> Plan:
    """Every question that does not depend on the build's outcome, asked before it starts."""
    step("Before starting")
    reuse = not args.latest and (cached_installer() or (
        (EXTRACTED / "reMarkable.exe").exists() and sha256(EXTRACTED / "reMarkable.exe") == TESTED_EXE_SHA256))
    need_download = args.exe is None and not reuse
    if need_download:
        if not ask("Download official reMarkable Windows installer (~95 MB) from remarkable.com? [Y/n]: "):
            raise Declined("no installer to build from. If you have one, or an installed reMarkable.exe, pass it: "
                           "./install.sh --exe PATH")
    else:
        info(f"reMarkable: {args.exe or cached_installer() or EXTRACTED / 'reMarkable.exe'}")
    qt = find_qt(args.qt)
    if qt is None:
        if platform.machine() not in ("x86_64", "AMD64"):
            raise InstallError(f"no Qt {QT_VERSION} SDK found: install one and pass --qt <sdk>/gcc_arm64")
        if not ask(f"Download Qt {QT_VERSION} build files ({' + '.join(QT_ARCHIVES)}, ~140 MB) from download.qt.io "
                   "into build/qt? [Y/n]: "):
            raise Declined("the native module needs a Qt 6.10 SDK. Install one (e.g. with the Qt online "
                           f"installer, into ~/Qt/{QT_VERSION}) or pass --qt PATH")
    else:
        info(f"Qt SDK: {qt} ({qt_sdk_version(qt)})")
    fonts = launcher = False
    if not args.no_desktop:
        fonts = ask(f"Install the reMarkable fonts into {home(FONT_DIR)}/? [Y/n]: ")
        launcher = ask(f"Install a \"reMarkable Linux\" launcher into {home(DESKTOP_FILE)} (and its icon)? [Y/n]: ")
    return Plan(need_download, qt, fonts, launcher)


def main() -> int:
    parser = argparse.ArgumentParser(prog="install.sh", description=__doc__.split("\n\n")[0].replace("\n", " "),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--exe", type=Path, metavar="PATH",
                        help="reMarkable's Windows installer (reMarkable-*-win64.exe) or an installed "
                             "reMarkable.exe, instead of downloading the installer")
    parser.add_argument("-y", "--yes", "--non-interactive", dest="yes", action="store_true",
                        help="answer yes to every question (downloads, installing, cleanup)")
    parser.add_argument("--latest", action="store_true",
                        help=f"download the newest installer even if it is not {TESTED_VERSION} (implies "
                             "--allow-untested)")
    parser.add_argument("--allow-untested", action="store_true",
                        help=f"accept a reMarkable.exe other than the verified {TESTED_VERSION} build")
    parser.add_argument("--qt", type=Path, metavar="SDK",
                        help=f"a Qt 6.10 SDK to build against, e.g. ~/Qt/{QT_VERSION}/gcc_64 (default: "
                             "look in build/qt and ~/Qt, else offer to download one)")
    parser.add_argument("--python", default=sys.executable,
                        help="the Python (3.10-3.14) for the build environment (default: this one)")
    parser.add_argument("--no-desktop", action="store_true", help="do not offer to install fonts and a launcher")
    parser.add_argument("--no-tests", action="store_true", help="skip the offline test suite")
    parser.add_argument("--keep-build", action="store_true",
                        help="keep the build files (build/, extracted/, resources/, assets) without asking")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the launcher, icon, fonts and dist/remarkable-linux")
    args = parser.parse_args()
    if args.source and args.exe:
        parser.error("give the installer or exe once")
    args.exe = args.exe or args.source
    ask = Prompt(args.yes)
    try:
        if args.uninstall:
            uninstall(ask)
            return 0
        check_prerequisites(args.python)
        decided = plan(args, ask)
        exe = obtain_exe(args.exe, args.latest)
        verify_exe(exe, args.allow_untested or args.latest)
        exe = install_exe(exe)
        extract_assets(exe)
        make_build_venv(args.python)
        build_native(decided.qt or download_qt())
        if not args.no_tests:
            run_tests()
        freeze()
        if decided.fonts:
            install_fonts()
        if decided.launcher:
            install_launcher()
        if not args.keep_build:
            offer_cleanup(ask)
    except Declined as e:
        print(f"\nStopped: {e}", file=sys.stderr)
        return 1
    except InstallError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    step("Done")
    if decided.launcher:
        info("Start \"reMarkable Linux\" from your applications, or run:")
    for flags, what in (("", "your library (the first start asks you to log in)"),
                        ("--demo", "the built-in demo library")):
        info(f"    {APP_BIN.relative_to(REPO)} {flags:<7} {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
