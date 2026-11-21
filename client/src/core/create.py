"""New notebooks, Quick sheets, and the document settings the library sets, as
reMarkable.exe 3.29 makes them (ARCHITECTURE.md 5.6).

LibraryController::createDocument (librarycontroller.cpp, 0x1404dd3b0):
  - refuses when the library's disk has 512 MiB or less free (0x1404dff90, "Not enough
    free disk space, refusing to create new document"), when the parent folder cannot be
    locked or is in the trash (0x140681860), and when a document with the given id exists
    ("refusing to create document with id {} that already exists");
  - the document id is a new QUuid, or `uuid` when that is a valid UUID ("uuid {:?}
    supplied, using it for document creation"); the same for the page (`uuid_page`);
  - a new Document (ctor 0x14064f3c0: coverPageNumber -1, textAlignment justify,
    textScale 1, lineHeight 100, no margins, orientation portrait, zoomMode bestFit,
    sizeInBytes 0), setVisibleName (lastModified now), setFileType(Notebook)
    (0x140667aa0), one page (0x140668010 -> initializePages 0x140871670 with an empty
    template name: the page gets `idx` only, written and bumped once more, "A:2", key
    "ba"; no template, no redirection, `original` stays -1 at 0:0), setParent
    (lastModified now), "opened" (0x140669d40: .metadata lastOpened = now), and it is
    added to its folder and the library. cPages `lastOpened` is not set: the new
    metadata's lastOpenedPage is -1 (0x14067fef7), which 0x140876db0 rejects.
  - no .rm, no .pagedata: the page has no scene until something is drawn on it.
The .content keys and values are the 3.29 writer's (0x1406ce210) for those defaults;
checked key by key and type by type against the 232 notebooks of a real library
(tools/create_schema_check.py).

The new-notebook dialog (NamingDialog.qml) then calls setOrientation(Qt.Vertical) and
setCoverPageNumber(-1 or 0), and DocumentController.setTemplateForPage(page,
"Blank", Settings.paperSize), which writes the page's `template` register ("A:1")
and moves lastModified (0x14065aff0: the page map changed). A notebook made so has
the same shape as the ones the tablet makes: template at A:1, idx "ba" at A:2.

Quick sheets (LibraryController::quickSheets 0x1404e1610, Document::isQuickSheet
0x140660b20, Library 0x1404d50c0): no marker is stored. A document is the Quick
sheets when its visibleName is tr("Quick sheets") (context Document), it is not in
the trash, and it is a notebook; of several, the one with the smallest id (QByteArray
order). When there is none, quickSheets() creates it: createDocument in My files with
that name, nothing else.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Iterable

from .models import DOCUMENT_TYPE, TRASH_ID, Document, DocumentTree, FileType, json_bytes
from .pagemap import PageMap, PageMapError

QUICK_SHEETS = "Quick sheets"       # tr() source text, context "Document" (0x140664e70)
MIN_FREE_BYTES = 0x20000000         # createDocument needs more than 512 MiB free (0x1404dff90)

# Qt::Orientation, as Document::orientation and setOrientation take it.
HORIZONTAL, VERTICAL = 1, 2


class CreateError(ValueError):
    """A document the app would refuse to create."""


def orientation_name(orientation: int) -> str:
    """The .content "orientation" for a Qt::Orientation: "landscape" for Horizontal,
    "portrait" for anything else (0x1406cf420)."""
    return "landscape" if int(orientation) == HORIZONTAL else "portrait"


def orientation_value(name: Any) -> int:
    """Qt::Orientation for a .content "orientation": Horizontal only for "landscape";
    "portrait", a missing key or anything else is Vertical (0x1406cf330)."""
    return HORIZONTAL if name == "landscape" else VERTICAL


def given_uuid(value: str | None) -> str | None:
    """`value` when it is a UUID QUuid::fromString takes (braces allowed), else None."""
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    try:
        return str(uuid.UUID(text)) if len(text) == 36 else None
    except ValueError:
        return None


def notebook_content(author: bytes | str, page_id: str | None) -> dict[str, Any]:
    """.content of a new notebook as 3.29 writes it, with one page `page_id` (None: no
    page, as createDocumentFromExisting starts)."""
    content: dict[str, Any] = {
        "cPages": {"lastOpened": {"timestamp": "0:0", "value": ""},
                   "original": {"timestamp": "0:0", "value": -1}, "pages": [], "uuids": []},
        "coverPageNumber": -1,
        "customZoomCenterX": 0,
        "customZoomCenterY": 936,
        "customZoomOrientation": "portrait",
        "customZoomPageHeight": 1872,
        "customZoomPageWidth": 1404,
        "customZoomScale": 1,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": FileType.NOTEBOOK.value,
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": 100,
        "orientation": "portrait",
        "pageCount": 0,
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }
    pagemap = PageMap(content, author)          # this device in cPages.uuids, index 1
    if page_id is not None:
        pagemap.insert(0, page_id, "")
    pagemap.save()
    return content


def notebook_metadata(name: str, parent: str, now_ms: int, opened: bool = True) -> dict[str, Any]:
    """.metadata of a new notebook: created and modified now; `opened` (createDocument
    marks it opened: lastOpened now; createDocumentFromExisting does not: "0", as the
    writer puts a lastOpened of 0); lastOpenedPage -1 until a page is turned to."""
    return {"createdTime": str(now_ms), "lastModified": str(now_ms),
            "lastOpened": str(now_ms) if opened else "0", "lastOpenedPage": -1, "new": False,
            "parent": parent, "pinned": False, "source": "", "type": DOCUMENT_TYPE, "visibleName": name}


def notebook_files(doc_id: str, content: dict, metadata: dict) -> dict[str, bytes]:
    return {f"{doc_id}.content": json_bytes(content), f"{doc_id}.metadata": json_bytes(metadata)}


# -- settings the library sets on a document (librarycontroller.cpp) ----------------
#
# setOrientation (0x1404e26d0 -> 0x1404e2470 -> Document 0x140667f10), setCoverPageNumber
# (0x1404e1b20 -> 0x1404e1900 -> 0x1406676a0) and clearIsNew (0x1404dc5c0 -> 0x1404dc400 ->
# Entry::setNew 0x140684080) each change their own field only when it differs and mark
# the entry to be stored and uploaded when it is unlocked (entry+0x51, 0x140684b60 ->
# 0x140684790). None of them calls Entry::setLastModified: lastModified stays.

def set_orientation(content: dict, orientation: int) -> bool:
    """.content "orientation" for a Qt::Orientation; False when it already says so."""
    name = orientation_name(orientation)
    if orientation_value(content.get("orientation")) == orientation_value(name):
        return False
    content["orientation"] = name
    return True


def set_cover_page(content: dict, page: int) -> bool:
    """.content "coverPageNumber": -1 the last page visited, 0 the first page."""
    if content.get("coverPageNumber", -1) == int(page):
        return False
    content["coverPageNumber"] = int(page)
    return True


# Document::ZoomMode (moc) and the .content names the reader 0x1406d06c0 maps to it
# ("fitToWidth" 1, "fitToHeight" 2, "customFit" 3, anything else bestFit 0).
ZOOM_MODES = ("bestFit", "fitToWidth", "fitToHeight", "customFit")
CUSTOM_FIT = 3


def zoom_mode_value(name: Any) -> int:
    return ZOOM_MODES.index(name) if name in ZOOM_MODES[1:] else 0


def set_zoom_mode(content: dict, mode: int) -> bool:
    """LibraryController::setZoomMode (0x1404e3700 -> 0x1404e34a0 -> Document 0x140668a70):
    "zoomMode" for a Document::ZoomMode; False when it is already that mode. The caller
    refuses a mode above 3 ("Incorrect zoom mode set: {}")."""
    if zoom_mode_value(content.get("zoomMode")) == int(mode):
        return False
    content["zoomMode"] = ZOOM_MODES[int(mode)]
    return True


def set_custom_zoom(content: dict, scale: float, center: tuple[float, float], page_size: tuple[float, float]) -> bool:
    """LibraryController::setCustomZoomFit (0x1404e1e70 -> 0x1404e1ba0 -> Document
    0x1406676c0): when the mode is not customFit, or the scale, centre, page size or
    customZoomOrientation (which follows the document's own orientation) differ, all of
    them are set and the mode becomes customFit. False when nothing differs."""
    orientation = content.get("orientation") if content.get("orientation") == "landscape" else "portrait"
    wanted = {"customZoomCenterX": float(center[0]), "customZoomCenterY": float(center[1]),
              "customZoomPageWidth": float(page_size[0]), "customZoomPageHeight": float(page_size[1]),
              "customZoomScale": float(scale)}
    same = (zoom_mode_value(content.get("zoomMode")) == CUSTOM_FIT
            and all(_number(content.get(k)) == v for k, v in wanted.items())
            and (content.get("customZoomOrientation") == "landscape") == (orientation == "landscape"))
    if same:
        return False
    content.update(wanted)
    content["customZoomOrientation"] = orientation
    content["zoomMode"] = ZOOM_MODES[CUSTOM_FIT]
    return True


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def set_view_background_filter(content: dict, value: str) -> bool:
    """DocumentController::setViewBackgroundFilter (0x140493250 -> 0x1404932d0 ->
    Document 0x140668a10): the string, when it differs. The .content writer (0x1406ce210)
    writes "viewBackgroundFilter" only when it is not empty, so "" removes the key."""
    if str(content.get("viewBackgroundFilter") or "") == str(value or ""):
        return False
    if value:
        content["viewBackgroundFilter"] = str(value)
    else:
        content.pop("viewBackgroundFilter", None)
    return True


def set_scroll_position(content: dict, index: int, position: float, author: bytes | str, now_utc) -> bool:
    """LibraryController::setScrollPosition (0x1404e3120 -> 0x1404e2e60): the page map in
    the current format first (a formatVersion 1 .content is upgraded as on load; one with
    no page list cannot be: "Cannot set scroll position, unable to ensure correct format
    version"), then the page's verticalScroll / scrollTime (PageMap.set_scroll). False
    when nothing could be written."""
    from .pagemap import PageMap, is_legacy, upgrade
    if is_legacy(content):
        if not isinstance(content.get("pages"), list):
            return False
        upgraded = upgrade(content, author)
        content.clear()
        content.update(upgraded)
    try:
        pagemap = PageMap(content, author)
    except PageMapError:
        return False
    if not pagemap.set_scroll(int(index), float(position), now_utc):
        return False
    pagemap.save()
    return True


def thumbnail_files(doc_id: str, names: Iterable[str]) -> list[str]:
    """The document's page thumbnails: setOrientation deletes the "<id>.thumbnails"
    directory when the orientation changes (0x140667f10 -> 0x14065cc40 with flag 1), so
    they are made again the new way round."""
    folder = f"{doc_id}.thumbnails/"
    return sorted(n for n in names if n.startswith(folder))


def set_page_template(content: dict, page_id: str, template: str, author: bytes | str) -> bool:
    """The page's `template` register, written only when it differs (PageMap.set_template)."""
    try:
        pagemap = PageMap(content, author)
    except PageMapError:
        return False
    if not pagemap.set_template(page_id, template):
        return False
    pagemap.save()
    return True


# -- Quick sheets -------------------------------------------------------------------

def is_quick_sheet(node: Any, tree: DocumentTree | None, name: str = QUICK_SHEETS) -> bool:
    """Document::isQuickSheet: named tr("Quick sheets"), not in the trash, a notebook."""
    if not isinstance(node, Document) or node.name != name or node.file_type is not FileType.NOTEBOOK:
        return False
    return not (tree.is_trashed(node.id) if tree is not None and node.id in tree else node.parent_id == TRASH_ID)


def quick_sheets_id(tree: DocumentTree, name: str = QUICK_SHEETS) -> str:
    """The Quick sheets document's id ("": none): the smallest id among the documents
    that are Quick sheets (0x1404d50c0 keeps the lesser QByteArray)."""
    ids = [n.id for n in tree.nodes.values() if is_quick_sheet(n, tree, name)]
    return min(ids, key=lambda i: i.encode("utf-8")) if ids else ""


# -- a notebook made from pages of another (createDocumentFromExisting) ---------------

def notebook_from_pages(author: bytes | str, pages: list[tuple[str, str]]) -> dict[str, Any]:
    """.content of a notebook made from moved pages: no page of its own, then each of
    `pages` ((new page id, template)) added at the end in order, as the page-move task
    adds pages to a document (normal insert mode: template and idx "A:2")."""
    content = notebook_content(author, None)
    pagemap = PageMap(content, author)
    for page_id, template in pages:
        pagemap.insert(len(pagemap.live), page_id, template)
    pagemap.save()
    return content


def updated(content: dict) -> dict:
    """A deep copy to change (the caller's dicts stay as they were)."""
    return copy.deepcopy(content)
