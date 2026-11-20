"""Password-protected PDFs: which documents are locked, and their passwords.

reMarkable.exe 3.29 (ARCHITECTURE.md 5.10): a document whose PDF needs a password is
`lockedByPassword`; the library shows it with a padlock ("Password protected PDF"),
and opening it shows the app's own PasswordDialog, whose "Open" calls
LibraryController::unlockWithPassword (0x1404e3c90). That starts a PasswordTask: its
execute (0x14068ee50) gives the password to the Document (0x1406687a0: kept in memory
at Document+0x800, a 30 s QBasicTimer started; the Document's timerEvent 0x1406698f0
empties it again) and to the document worker that loads the PDF. A wrong password ends
in the task's handler 0x14068faa0: Document::invalidPassword() (the dialog's "Incorrect
password. Please try again."), the password emptied and the timer stopped. A right one
ends in loadingSucceeded, on which the dialog accepts and the document opens. The
password is never written anywhere.

This client: PdfFile (ui/pages.py) opens a PDF with the password known for its file,
if any, and raises PdfLocked when PDFium (QtPdf) says a password is needed. Passwords
are kept here, in this process's memory only, by the local path of the PDF file, from
a successful unlock until the app ends (or forget()): this client opens a PDF again
for each view, thumbnail and export job, where the app keeps the one PDFium document it
unlocked open, so the app's 30 s lifetime would lock a document again while it is
being read. Whether a cached PDF needs a password is worked out once per file
(needs_password): only a file whose start or end mentions /Encrypt is opened to see.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("rm.ui.pdflock")

SCAN_BYTES = 1 << 20       # how much of each end of a PDF is looked at for /Encrypt


class PdfLocked(ValueError):
    """A PDF that needs a password (none known, or the one known is wrong)."""


_lock = threading.Lock()
_passwords: dict[str, str] = {}                 # PDF path -> password (memory only)
_needs: dict[tuple[str, int, int], bool] = {}   # (path, size, mtime_ns) -> needs a password


def _key(path: str | Path) -> str:
    return os.path.realpath(str(path))


def password_for(path: str | Path) -> str | None:
    """The password known for the PDF at `path`, if any."""
    with _lock:
        return _passwords.get(_key(path))


def remember(path: str | Path, password: str) -> None:
    with _lock:
        _passwords[_key(path)] = str(password)


def forget(path: str | Path | None = None) -> None:
    """Forget one PDF's password, or all of them (logout)."""
    with _lock:
        if path is None:
            _passwords.clear()
        else:
            _passwords.pop(_key(path), None)


def _mentions_encrypt(path: Path) -> bool:
    """Whether the start or the end of the file mentions /Encrypt: an encrypted PDF's
    trailer (or cross-reference stream, at the end or, linearised, the start) does."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(SCAN_BYTES)
            if size > SCAN_BYTES:
                fh.seek(max(size - SCAN_BYTES, SCAN_BYTES))
                tail = fh.read(SCAN_BYTES)
            else:
                tail = b""
    except OSError:
        return False
    return b"/Encrypt" in head or b"/Encrypt" in tail


def open_status(path: str | Path, password: str | None = None):
    """QPdfDocument.Error of opening the PDF at `path` with `password` (None_: it opens)."""
    from PySide6.QtPdf import QPdfDocument
    doc = QPdfDocument()
    try:
        if password:
            doc.setPassword(password)
        return doc.load(str(path))
    finally:
        doc.close()


def needs_password(path: str | Path) -> bool:
    """Whether the PDF at `path` cannot be opened without a password (its user
    password is not empty). Worked out once per file version."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return False
    key = (_key(path), st.st_size, st.st_mtime_ns)
    with _lock:
        known = _needs.get(key)
    if known is not None:
        return known
    locked = False
    if _mentions_encrypt(path):
        from PySide6.QtPdf import QPdfDocument
        locked = open_status(path) == QPdfDocument.Error.IncorrectPassword
    with _lock:
        _needs[key] = locked
    return locked


def locked(path: str | Path | None) -> bool:
    """Whether the PDF at `path` needs a password that is not known (yet)."""
    if path is None or not Path(path).exists():
        return False
    # A known password was checked when it was given (unlock); the cache names files by
    # their content hash, so the file it opened is the file at this path.
    return password_for(path) is None and needs_password(path)


def unlock(path: str | Path, password: str) -> bool:
    """Try `password` on the PDF at `path`: kept (in memory) when it opens it. A wrong
    one is forgotten, as the app empties the Document's password."""
    from PySide6.QtPdf import QPdfDocument
    ok = bool(password) and open_status(path, password) == QPdfDocument.Error.None_
    if ok:
        remember(path, password)
    else:
        forget(path)
    return ok


__all__ = ["PdfLocked", "forget", "locked", "needs_password", "open_status", "password_for", "remember", "unlock"]
