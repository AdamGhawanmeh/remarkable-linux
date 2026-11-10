"""A document's page list (.content "cPages"), changed as reMarkable.exe changes it.

The app keeps the pages of a document as a CRDT (xochitl src/entryfile/src/pagemap.cpp
in reMarkable.exe 3.29). Each page is an entry of cPages.pages:

    {"id": "<page UUID>",
     "idx": {"timestamp": "A:C", "value": "bb"},          its place: pages read in the
                                                          order of these keys
     "template": {"timestamp": "A:C", "value": "Blank"},
     "redir": {"timestamp": "A:C", "value": 3},            the PDF page behind it, if any
     "deleted": {"timestamp": "A:C", "value": 1},          a deleted page stays listed
     "modifed": "<ms>", "scrollTime"..., "verticalScroll"...}

Every {timestamp, value} field is a last-writer-wins register: "A:C" is the author's
index in cPages.uuids and a counter, which every write sets to (writer, counter + 1)
(0x140876d20, 0x140877720, 0x140877590). A field whose counter is 0 is not written
(page toJson, 0x140877a70). The file lists the live pages by idx, then the deleted
pages by id: a map keyed by id (every real file in the library is laid out so).

Page keys are strings of a..z ("ba", "bb", ...), made by the app's key functions
(0x140a99620 after, 0x140a98c30 between, 0x140a99a30 jump), reproduced here as
key_after, keys_between and jump. Inserting a page (0x140871970), moving pages
(0x140875020) and deleting one (0x1408704d0) are PageMap.insert, move and delete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

FIRST, LAST, MIDDLE = "a", "z", "n"     # the key alphabet a..z (0x140d54270) and its middle
JUMP_FLOOR, JUMP_GROW = "n", "r"


class PageMapError(ValueError):
    """The page list cannot be changed as asked."""


# ------------------------------------------------------------------ keys

def _increment(key: str, fill: str) -> str:
    """The key one step after `key` at its own length: its last letter that is not "z"
    goes up by one and the "z"s after it become `fill`; all "z" (or empty): `key` + "a"."""
    chars = list(key)
    for i in range(len(chars) - 1, -1, -1):
        if chars[i] != LAST:
            chars[i] = chr(ord(chars[i]) + 1)
            chars[i + 1:] = fill * (len(chars) - i - 1)
            return "".join(chars)
    return key + FIRST


def key_after(key: str) -> str:
    """The key the app gives the page after one keyed `key` (0x140a99620, as called by
    0x140870ba0): one step up; a one-letter key, or one whose first half is all "n" or
    later, grows by an "a" so that there is room after it."""
    out = _increment(key, FIRST)
    if len(out) == 1:
        return out + FIRST
    for i, char in enumerate(out):
        if char < MIDDLE:
            break
        if i >= len(out) // 2:
            return out + FIRST
    return out


def keys_after(key: str, count: int) -> list[str]:
    """`count` keys in a row after `key` (0x140870ba0)."""
    out: list[str] = []
    for _ in range(count):
        key = key_after(key)
        out.append(key)
    return out


def jump(key: str) -> str:
    """A key well after `key` (0x140a99a30): its first half raised to at least "n" and
    stepped up, the rest kept. The app jumps (author index % 7) times before adding
    pages after a page another author placed, so that two devices adding pages at the
    end of the same document do not pick the same keys."""
    if len(key) == 1:
        key += FIRST
    half = len(key) // 2
    head = _increment("".join(max(c, JUMP_FLOOR) for c in key[:half]), JUMP_FLOOR)
    if len(head) == 1:
        head += JUMP_FLOOR
    else:
        for i, char in enumerate(head):
            if char < JUMP_GROW:
                break
            if i >= len(head) // 2:
                head += JUMP_FLOOR
                break
    return head + key[half:]


def not_before(a: str, b: str) -> bool:
    """True when key `b` does not sort after key `a`: b <= a, or b is a followed only by
    "a"s, which the app counts as the same place (0x140a99780)."""
    n = min(len(a), len(b))
    if a[:n] != b[:n]:
        return a[:n] > b[:n]
    if len(b) <= len(a):
        return True
    return all(c == FIRST for c in b[len(a):])


def keys_between(a: str, b: str, count: int) -> list[str]:
    """`count` keys in a row after `a` and before `b` (0x140a98c30): the keys share the
    longest start with which there is room for them, then count up from there. Empty
    when there is no room (a and b empty, or b not after a)."""
    if not a or not b or not_before(a, b):
        return []
    prefix, i = "", 0
    while i < len(b):
        ca = a[i] if i < len(a) else FIRST
        if ca != b[i]:
            break
        prefix += b[i]
        i += 1
    ca = a[i] if i < len(a) else FIRST
    cb = b[i] if i < len(b) else LAST
    span = 0 if not prefix else ord(cb) - ord(ca)
    while 0 <= span <= count:
        prefix += ca
        i += 1
        ca = a[i] if i < len(a) else FIRST
        cb = b[i] if i < len(b) else LAST
        span = max(span, 1) * (ord(cb) - ord(ca))
    key = _increment(prefix + ca, FIRST)
    out = [key]
    while len(out) < count:
        key = _increment(key, FIRST)
        out.append(key)
    return out


# ------------------------------------------------------------------ timestamps

def parse_stamp(value: Any) -> tuple[int, int]:
    """"A:C" -> (author index, counter); (0, 0) for anything else."""
    try:
        author, counter = str(value).split(":")
        return int(author), int(counter)
    except (TypeError, ValueError):
        return 0, 0


def format_stamp(stamp: tuple[int, int]) -> str:
    return f"{stamp[0]}:{stamp[1]}"


def _set(entry: dict, name: str, value: Any, author: int) -> None:
    """Write a register of a page as the app does: (writer, counter + 1)."""
    old = entry.get(name)
    counter = parse_stamp(old.get("timestamp"))[1] if isinstance(old, dict) else 0
    entry[name] = {"timestamp": format_stamp((author, counter + 1)), "value": value}


def _bump(entry: dict, name: str) -> None:
    """One more on a register's counter, when it has been written (0x1408715d0)."""
    old = entry.get(name)
    if isinstance(old, dict):
        stamp = parse_stamp(old.get("timestamp"))
        if stamp[1]:
            old["timestamp"] = format_stamp((stamp[0], stamp[1] + 1))


def _value(entry: dict, name: str, default: Any = None) -> Any:
    field = entry.get(name)
    return field.get("value", default) if isinstance(field, dict) else default


# ------------------------------------------------------------------ the page list

@dataclass(frozen=True)
class PageInfo:
    id: str
    template: str
    redirect: int | None


class PageMap:
    """The cPages of a .content, as a list of live pages in order and the deleted ones,
    changed in place by insert, move and delete as the app changes it, for the author
    `author_uuid` (added to cPages.uuids if new). `content` is the parsed .content; the
    changes are written back into it by save()."""

    def __init__(self, content: dict, author_uuid: bytes | str) -> None:
        cpages = content.get("cPages")
        if not isinstance(cpages, dict) or not isinstance(cpages.get("pages"), list):
            raise PageMapError("the document has no page list (cPages) to change")
        self.content = content
        self.cpages = cpages
        entries = [e for e in cpages["pages"] if isinstance(e, dict) and isinstance(e.get("id"), str)]
        self.live = sorted((e for e in entries if not _value(e, "deleted")), key=self._order)
        self.dead = [e for e in entries if _value(e, "deleted")]
        name = str(uuid.UUID(bytes=author_uuid)) if isinstance(author_uuid, bytes) else str(author_uuid)
        self.author = self._author_index(name)

    @staticmethod
    def _order(entry: dict) -> tuple[str, str]:
        return str(_value(entry, "idx", "")), entry["id"]

    def _author_index(self, name: str) -> int:
        """The author's index in cPages.uuids; one past the highest, for an author new to
        the document (as the page scenes number authors, Scene::setAuthor)."""
        uuids = self.cpages.setdefault("uuids", [])
        if not isinstance(uuids, list):
            raise PageMapError("cPages.uuids is not a list")
        for row in uuids:
            if isinstance(row, dict) and str(row.get("first", "")).lower() == name.lower():
                return int(row.get("second") or 0)
        # One past the highest index in use: listed, or found in a timestamp (a file
        # without its uuids list must still not have us write as someone else).
        used = [int(r.get("second") or 0) for r in uuids if isinstance(r, dict)]
        used += [parse_stamp(field.get("timestamp"))[0] for entry in self.cpages.get("pages") or []
                 if isinstance(entry, dict) for field in entry.values() if isinstance(field, dict)]
        index = max(used + [0]) + 1
        uuids.insert(0, {"first": name, "second": index})
        return index

    # -- reading --

    def ids(self) -> list[str]:
        return [e["id"] for e in self.live]

    def index(self, page_id: str) -> int:
        """The live page `page_id`'s place, or -1."""
        for i, entry in enumerate(self.live):
            if entry["id"] == page_id:
                return i
        return -1

    def info(self, page_id: str) -> PageInfo | None:
        i = self.index(page_id)
        if i < 0:
            return None
        entry = self.live[i]
        redirect = _value(entry, "redir")
        return PageInfo(page_id, str(_value(entry, "template", "") or ""),
                        int(redirect) if isinstance(redirect, (int, float)) and not isinstance(redirect, bool) else None)

    def known(self, page_id: str) -> bool:
        return any(e["id"] == page_id for e in self.live + self.dead)

    def _key(self, i: int) -> str:
        return str(_value(self.live[i], "idx", ""))

    def _author_of(self, i: int) -> int:
        return parse_stamp((self.live[i].get("idx") or {}).get("timestamp"))[0]

    # -- keys for new places (0x14086fa20) --

    def _keys(self, start: int, stop: int, count: int) -> list[str]:
        """`count` keys after the page before `start` ("a" at the front) and before the
        page `stop` (none: the end). At the end, after a page another author placed, the
        app first jumps ahead by its author index (see jump)."""
        prev = self._key(start - 1) if start > 0 else FIRST
        if stop < len(self.live):
            keys = keys_between(prev, self._key(stop), count)
        else:
            if self.live and start > 0 and self._author_of(start - 1) != self.author:
                for _ in range(self.author % 7):
                    prev = jump(prev)
            keys = keys_after(prev, count)
        if len(keys) != count:
            raise PageMapError(f"no room for {count} pages between keys {prev!r} and {self._key(stop)!r}")
        return keys

    def _rekey(self, entry: dict, key: str) -> None:
        _set(entry, "idx", key, self.author)            # written even when it is the same key

    # -- changes --

    def insert(self, index: int, page_id: str, template: str = "", redirect: int | None = None,
               normal: bool = True) -> int:
        """Put a new page at `index` (clamped to the list), as insertPage (0x140871e60):
        template and redirection written if given, the place keyed between its
        neighbours (pages after it whose keys leave no room are keyed again with it), and
        in the normal insert mode every written field's counter one more (0x1408715d0).
        Returns the page's index."""
        if self.known(page_id):
            raise PageMapError(f"page {page_id} is in the document already")
        index = min(max(int(index), 0), len(self.live))
        entry: dict = {"id": page_id}
        if template:
            _set(entry, "template", template, self.author)
        if redirect is not None and redirect != -1:
            _set(entry, "redir", int(redirect), self.author)
        prev = self._key(index - 1) if index > 0 else FIRST
        crowded = 0
        while index + crowded < len(self.live) and not_before(prev, self._key(index + crowded)):
            crowded += 1
        keys = self._keys(index, index + crowded, crowded + 1)
        _set(entry, "idx", keys[0], self.author)
        for n in range(crowded):
            self._rekey(self.live[index + n], keys[n + 1])
        self.live.insert(index, entry)
        if normal:
            for name in ("template", "redir", "idx"):
                _bump(entry, name)
        return index

    def move(self, indices: list[int], insert_index: int) -> bool:
        """Move the pages at `indices` (places in the list as it is) to before the page
        at `insert_index`, as movePages (0x140875020): the moved pages, in their order,
        and the pages after the place whose keys leave no room, get keys between the page
        before the place and the next page that stays. False when there is nothing to
        move or an index is out of range."""
        moved = sorted(set(int(i) for i in indices))
        if not moved or moved[0] < 0 or moved[-1] >= len(self.live):
            return False
        insert_index = min(max(int(insert_index), 0), len(self.live))
        prev = self._key(insert_index - 1) if insert_index > 0 else FIRST
        crowded: list[int] = []
        stop = insert_index
        while stop < len(self.live):
            if stop in moved:
                stop += 1
                continue
            if not not_before(prev, self._key(stop)):
                break
            crowded.append(stop)
            stop += 1
        keys = self._keys(insert_index, stop, len(moved) + len(crowded))
        for i, key in zip(moved + crowded, keys):
            self._rekey(self.live[i], key)
        self.live.sort(key=self._order)
        return True

    def delete(self, index: int) -> str:
        """Delete the live page at `index` (deletePage, 0x1408704d0): it stays listed,
        marked deleted. Returns its id."""
        if not 0 <= index < len(self.live):
            raise PageMapError(f"no page {index} to delete")
        entry = self.live.pop(index)
        _set(entry, "deleted", 1, self.author)
        self.dead.append(entry)
        return entry["id"]

    def save(self) -> dict:
        """Write the page list back into the .content: live pages by key, then deleted
        pages by id, and pageCount, the number of live pages."""
        self.cpages["pages"] = self.live + sorted(self.dead, key=lambda e: e["id"])
        self.content["pageCount"] = len(self.live)
        return self.content
