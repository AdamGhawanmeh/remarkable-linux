"""A PDF rendition of an EPUB, as reMarkable.exe 3.29 makes one (doGeneratePdfFromEpub).

The cloud's EPUB documents carry `<id>.pdf` next to `<id>.epub`: the pages every app
draws and annotates. The tablet makes it the first time it opens an ebook, and so does
3.29 on the desktop (EPubRenderer; a real rendition in the library says "Qt 6.10.3",
the desktop app's Qt, the tablet's says 5.15.2). This client makes it when it imports an
EPUB, so the ebook can be read here at once (core.sync.import_file uploads it with the
EPUB, as 3.29's files do).

3.29's layout (ARCHITECTURE.md 5.16):
- a QTextDocument of the book's spine, its default font the document's `fontName` or
  "EB Garamond" (0x14070de10), weight 400, pixel size int(textScale x 38) (0x140d27790);
- default style sheet `p { line-height: %1 } h1, h2, h3 { margin-top: 2em; margin-bottom:
  1em } code {font-family: 'Noto Sans Mono'}`, %1 = 1 unless `lineHeight` > 0
  (0x1407117e0: lineHeight / 100 for the default font);
- root frame margins: left and right the document's `margins`, top and bottom a
  setting of the ebook module (here VERTICAL_MARGIN, measured on real renditions: it is
  not a constant of the binary); document margin 0;
- pages of 1404 x 1872 written by a QPdfWriter at 227 dpi (0x1406ec2d0): page size
  (1404, 1872) x 72 / 227 points (445.3 x 593.8, the real renditions' 445 x 594), page
  margins 0.

Only what Qt's rich text understands of the book's XHTML is kept (no EPUB style sheets);
images (also an <svg> cover's <image>) are drawn.
"""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

log = logging.getLogger("rm.epub")

PAGE_PX = (1404, 1872)
PDF_DPI = 227
FONT_PX = 38.0                  # pixel size of textScale 1
DEFAULT_FAMILY = "EB Garamond"
VERTICAL_MARGIN = 50.0          # top and bottom, pixels
STYLE_SHEET = ("p { line-height: %1 } h1, h2, h3 { margin-top: 2em; margin-bottom: 1em } "
               "code {font-family: 'Noto Sans Mono'}")
_QRC_FONTS = (":/fonts/EBGaramond-VariableFont_wght.ttf", ":/fonts/EBGaramond-Italic-VariableFont_wght.ttf")


class EpubError(ValueError):
    """The file is not an EPUB this module can lay out."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def spine(book: zipfile.ZipFile) -> list[str]:
    """The paths in the archive of the book's spine documents, in reading order."""
    try:
        container = ElementTree.fromstring(book.read("META-INF/container.xml"))
    except (KeyError, ElementTree.ParseError) as err:
        raise EpubError(f"no readable META-INF/container.xml: {err}") from err
    rootfile = next((e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"), None)
    if not rootfile:
        raise EpubError("the container names no package document")
    try:
        package = ElementTree.fromstring(book.read(rootfile))
    except (KeyError, ElementTree.ParseError) as err:
        raise EpubError(f"no readable package document {rootfile}: {err}") from err
    base = posixpath.dirname(rootfile)
    manifest = {e.get("id"): e.get("href") for e in package.iter() if _local(e.tag) == "item"}
    order = [e.get("idref") for e in package.iter() if _local(e.tag) == "itemref" and e.get("linear") != "no"]
    paths = [posixpath.normpath(posixpath.join(base, manifest[i])) for i in order if manifest.get(i)]
    if not paths:
        raise EpubError("the spine is empty")
    return paths


_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.S | re.I)
_SVG_IMAGE = re.compile(r"<svg\b.*?<image\b[^>]*?(?:xlink:)?href=[\"']([^\"']+)[\"'][^>]*>.*?</svg>", re.S | re.I)
_IMG_SRC = re.compile(r"(<img\b[^>]*?\bsrc=)([\"'])([^\"']+)\2", re.S | re.I)


def book_html(book: zipfile.ZipFile, resources: dict[str, bytes]) -> str:
    """The spine as one HTML body, each document from a new page; `resources` gets the
    images by the names the HTML uses."""
    parts = []
    for index, path in enumerate(spine(book)):
        try:
            text = book.read(path).decode("utf-8", errors="replace")
        except KeyError:
            log.warning("spine document %s is missing", path)
            continue
        match = _BODY.search(text)
        body = match.group(1) if match else text
        here = posixpath.dirname(path)

        def resource(href: str) -> str:
            target = posixpath.normpath(posixpath.join(here, href.split("#", 1)[0]))
            name = f"epub-resource/{target}"
            if name not in resources:
                try:
                    resources[name] = book.read(target)
                except KeyError:
                    return href
            return name

        body = _SVG_IMAGE.sub(lambda m: f'<img src="{resource(m.group(1))}"/>', body)
        body = _IMG_SRC.sub(lambda m: f"{m.group(1)}{m.group(2)}{resource(m.group(3))}{m.group(2)}", body)
        brk = ' style="page-break-before: always"' if index else ""
        parts.append(f"<div{brk}>{body}</div>")
    if not parts:
        raise EpubError("no spine document could be read")
    return "<html><body>" + "".join(parts) + "</body></html>"


def _family(name: str) -> str:
    from PySide6.QtGui import QFontDatabase

    if name:
        return name
    if DEFAULT_FAMILY not in QFontDatabase.families():
        for path in _QRC_FONTS:                 # the app's own copy (main() adds it too)
            QFontDatabase.addApplicationFont(path)
    return DEFAULT_FAMILY if DEFAULT_FAMILY in QFontDatabase.families() else "serif"


def line_height(value: float) -> float:
    """The `p { line-height }` of a document's `lineHeight` (0x1407117e0, default font)."""
    return value / 100.0 if value and value > 0 else 1.0


def generate_rendition(data: bytes, *, margins: float = 125.0, text_scale: float = 1.0,
                       line_height_value: float = -1.0, font_name: str = "") -> tuple[bytes, int]:
    """(PDF bytes, page count) of the EPUB `data`, laid out as 3.29 lays it out."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMarginsF, QRectF, QSizeF, QUrl
    from PySide6.QtGui import QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QTextDocument

    try:
        book = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as err:
        raise EpubError(f"not a zip archive: {err}") from err
    resources: dict[str, bytes] = {}
    with book:
        html = book_html(book, resources)
    document = QTextDocument()
    font = QFont(_family(font_name))
    font.setPixelSize(max(1, int(text_scale * FONT_PX)))
    font.setWeight(QFont.Weight(400))
    document.setDefaultFont(font)
    document.setDefaultStyleSheet(STYLE_SHEET.replace("%1", f"{line_height(line_height_value):g}"))
    for name, raw in resources.items():
        image = QImage.fromData(QByteArray(raw))
        if not image.isNull():
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)
    document.setHtml(html)
    document.setDocumentMargin(0)
    frame = document.rootFrame()
    fmt = frame.frameFormat()
    fmt.setLeftMargin(margins)
    fmt.setRightMargin(margins)
    fmt.setTopMargin(VERTICAL_MARGIN)
    fmt.setBottomMargin(VERTICAL_MARGIN)
    frame.setFrameFormat(fmt)
    width, height = PAGE_PX
    document.setPageSize(QSizeF(width, height))
    pages = max(1, document.pageCount())

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setPageSize(QPageSize(QSizeF(width * 72 / PDF_DPI, height * 72 / PDF_DPI), QPageSize.Unit.Point))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Point)
    writer.setResolution(PDF_DPI)
    writer.setCreator("reMarkable")
    painter = QPainter(writer)
    try:
        for page in range(pages):
            if page:
                writer.newPage()
            painter.save()
            painter.translate(0, -page * height)
            document.drawContents(painter, QRectF(0, page * height, width, height))
            painter.restore()
    finally:
        painter.end()
    return bytes(buffer.data()), pages
