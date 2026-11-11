"""A copy of a document, as reMarkable.exe makes one (Copy, then Paste, in the library).

LibraryController::cloneEntry (librarycontroller.cpp, 0x1404dc640) names the copy
tr("%1 copy") with the source's name (context xofm::libs::library::LibraryController:
"%1 Kopie", "Copie %1", ...), made unique among all entries as uniqueVisibleName does
("<name> 2", "<name> 3", ...: core.models.unique_name), and Document::clone
(document.cpp, 0x14065a590) makes it:

  - a new document id (QUuid);
  - a new page list: every live page gets a new page id, and the pages are added in
    their order by the copying device, as new pages are (core.pagemap: keys "ba",
    "bb", ..., template and PDF page kept, registers "A:2"); cPages "original" (the
    source's value) and "lastOpened" (the first page) are written anew ("A:1"), the
    authors (uuids) are the source's. Deleted pages are not copied. Checked against a
    copy the tablet made in a real library: same layout, and its untouched pages'
    .rm files byte-identical to the source's;
  - the page files under the new ids (DocumentCopyHelper pairs each source page with
    the copy's page of the same index, 0x1406b2640), every other file of the document
    under the new document id: the same bytes;
  - the rest of .content as it was, pageCount the number of pages copied;
  - .metadata with the new name and parent, created and modified now, not opened yet.

The cloud stores files by content, so the files a copy shares with its source are
not uploaded again; only .content and .metadata are new.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Callable, Iterable

from .models import json_bytes
from .pagemap import PageMap, format_stamp

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
COPY_NAME = "%1 copy"           # tr() source text, context xofm::libs::library::LibraryController


def copy_name(source_name: str, template: str = COPY_NAME) -> str:
    """The copy's name before it is made unique (template: the translated "%1 copy")."""
    return template.replace("%1", source_name)


def _clone_content(content: dict, author: bytes, new_id: Callable[[], str]) -> tuple[dict, dict[str, str]]:
    """(.content of the copy, old page id -> new page id)."""
    pages: dict[str, str] = {}
    cpages = content.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        source = PageMap(json.loads(json.dumps(content)), author)       # the live pages in order
        live = [(entry["id"], source.info(entry["id"])) for entry in source.live]
        fresh = {"pages": [], "uuids": source.cpages.get("uuids", [])}
        copy = {**content, "cPages": fresh}
        pagemap = PageMap(copy, author)
        for index, (old, info) in enumerate(live):
            pages[old] = new_id()
            pagemap.insert(index, pages[old], info.template, info.redirect)
        original = cpages.get("original")
        fresh["original"] = {"timestamp": format_stamp((pagemap.author, 1)),
                             "value": original.get("value", -1) if isinstance(original, dict) else -1}
        fresh["lastOpened"] = {"timestamp": format_stamp((pagemap.author, 1)),
                               "value": pages[live[0][0]] if live else ""}
        content = pagemap.save()
    elif isinstance(content.get("pages"), list):                         # formatVersion 1
        for old in content["pages"]:
            pages[str(old)] = new_id()
        content = {**content, "pages": [pages[str(p)] for p in content["pages"]]}
    tags = content.get("pageTags")
    if isinstance(tags, list) and pages:
        content["pageTags"] = [{**t, "pageId": pages[t["pageId"]]} for t in tags
                               if isinstance(t, dict) and t.get("pageId") in pages]
    return content, pages


def _rename(name: str, source_id: str, copy_id: str, pages: dict[str, str], dropped: set[str]) -> str | None:
    """A file's name in the copy (None: not copied): the document id, and a page id in it,
    replaced. Files of pages that are not copied (deleted ones) stay behind."""
    if not name.startswith(source_id):
        return None
    rest = name[len(source_id):]
    for found in _UUID.findall(rest):
        if found in pages:
            rest = rest.replace(found, pages[found])
        elif found in dropped:
            return None
    return copy_id + rest


def clone_files(source_id: str, names: Iterable[str], read: Callable[[str], bytes], *, copy_id: str,
                name: str, parent: str, now_ms: int, author: bytes,
                new_page_id: Callable[[], str] | None = None) -> tuple[dict[str, str], dict[str, bytes]]:
    """The copy of document `source_id` (its files `names`, `read(name)` their bytes;
    only .content and .metadata are read): (source file -> copy's name, for files copied
    as they are; copy's .content and .metadata, new bytes)."""
    new_page_id = new_page_id or (lambda: str(uuid.uuid4()))
    names = list(names)
    content_name, metadata_name = f"{source_id}.content", f"{source_id}.metadata"
    content = json.loads(read(content_name))
    known = {e.get("id") for e in (content.get("cPages") or {}).get("pages") or [] if isinstance(e, dict)} \
        if isinstance(content.get("cPages"), dict) else set()
    content, pages = _clone_content(content, author, new_page_id)
    dropped = {p for p in known if p not in pages}
    metadata = json.loads(read(metadata_name))
    metadata.update(visibleName=name, parent=parent, createdTime=str(now_ms), lastModified=str(now_ms),
                    lastOpened="0", lastOpenedPage=0, pinned=False)
    if "new" in metadata:
        metadata["new"] = False
    copied = {}
    for old in names:
        if old in (content_name, metadata_name):
            continue
        new = _rename(old, source_id, copy_id, pages, dropped)
        if new is not None:
            copied[old] = new
    return copied, {f"{copy_id}.content": json_bytes(content), f"{copy_id}.metadata": json_bytes(metadata)}
