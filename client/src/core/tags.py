"""Tags of documents, folders and pages, changed as reMarkable.exe changes them.

The tag dialog (TagSelectionPopup.qml) ends in LibraryController::setEntryTags(id,
tags, keepTags) or setPageTags(id, pages, tags, keepTags) (librarycontroller.cpp,
0x1404e1f30, 0x1404e2750). `tags` are the tags ticked; `keepTags` the ones shown half
ticked because only some of the chosen items have them. For each item
(EntryTags::set, 0x14087fa00; per page 0x140897020):

  - a tag it has that is half ticked stays as it is;
  - a tag it has that is ticked stays, with the time it was first given;
  - any other tag it has is removed;
  - a ticked tag it does not have yet is added, timestamped now (all added at once
    get the same time, QDateTime::currentMSecsSinceEpoch);
  - names are compared exactly (case-sensitive); empty names are skipped.

Then the entry's lastModified is set to now (Entry::setLastModified, 0x1406840c0).

Stored in the item's .content:
    "tags":     [{"name": "work", "timestamp": 1715065211285}, ...]
                 sorted by (timestamp, name) (0x14087fc00); a folder's too
    "pageTags": [{"name": "todo", "pageId": "<page id>", "timestamp": ...}, ...]
Timestamps are milliseconds, written as JSON numbers (TagMoment::toJson, 0x14087b9c0).
Existing page-tag entries keep their place; new ones follow in page order.
"""

from __future__ import annotations

from typing import Any, Iterable


def _names(values: Iterable[Any]) -> list[str]:
    """Tag names as given (strings, or {"name": ...}), without empty ones or repeats."""
    out: list[str] = []
    for value in values or []:
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return out


def _tag_name(entry: Any) -> str:
    return str(entry.get("name", "")) if isinstance(entry, dict) else ""


def set_entry_tags(content: dict, tags: Iterable[Any], keep: Iterable[Any], now_ms: int) -> bool:
    """Set the tags of an item (its .content, changed in place) as the dialog asks.
    True when they changed."""
    tags, keep = _names(tags), set(_names(keep))
    old = content.get("tags")
    old = old if isinstance(old, list) else []
    kept, seen = [], set()
    for entry in old:
        name = _tag_name(entry)
        if not name or name in seen:                    # skip duplicate tag (0x14087f700)
            continue
        if name in keep or name in tags:
            kept.append(entry)
            seen.add(name)
    added = [{"name": name, "timestamp": int(now_ms)} for name in tags if name not in seen]
    new = sorted(kept + added, key=lambda e: (_timestamp(e), _tag_name(e)))
    if new == old:
        return False
    content["tags"] = new
    return True


def _timestamp(entry: dict) -> int:
    try:
        return int(entry.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def set_page_tags(content: dict, page_ids: Iterable[str], tags: Iterable[Any], keep: Iterable[Any],
                  now_ms: int) -> bool:
    """Set the tags of the pages `page_ids` (.content changed in place) as the dialog
    asks; other pages' tags are left as they are. True when they changed."""
    tags, keep = _names(tags), set(_names(keep))
    pages = list(dict.fromkeys(page_ids))
    chosen = set(pages)
    old = content.get("pageTags")
    old = old if isinstance(old, list) else []
    new, have = [], set()
    for entry in old:
        page, name = (entry.get("pageId") if isinstance(entry, dict) else None), _tag_name(entry)
        if page not in chosen:
            new.append(entry)                           # another page's tag
            continue
        if not name or (page, name) in have:
            continue
        if name in keep or name in tags:
            new.append(entry)
            have.add((page, name))
    for page in pages:
        for name in tags:
            if (page, name) not in have:
                new.append({"name": name, "pageId": page, "timestamp": int(now_ms)})
                have.add((page, name))
    if new == old:
        return False
    content["pageTags"] = new
    return True
