"""Layer and SceneInfo CRDT operations for .rm v6 pages, as reMarkable.exe 3.29 writes them.

Every operation edits only the blocks it must and keeps every other block of the page byte
for byte, preserving the v6 block order (authors, migration, stats, scene info,
[images table], tree moves, [root text], tree nodes, items sorted by parent then id).

Traced in reMarkable.exe 3.29 and checked on real cached multi-layer pages (ARCHITECTURE.md):
  - Layers are SceneTreeNode (0x02) blocks attached under ROOT_NODE (0:1) via SceneTreeMove
    (0x01) and ordered by CrdtGroupItem (0x04) items in ROOT_NODE's CRDT sequence.
  - Max live layers: 5 (add-layer-crdt::initialize 0x14095c080).
  - Default naming: "Layer N" starting at len(live) + 1, incrementing while taken (0x1408a88d0).
  - Deleting a layer (delete-layer-crdt 0x14095d620 -> SceneTree::deleteNode 0x1408e03b0):
    appends a SceneTreeMove with is_update=0, parent=0:0. The SceneTreeNode, its root
    CrdtGroupItem and its child items stay in the file so undo can re-attach it with a
    SceneTreeMove (is_update=1, parent=0:1).
  - Renaming / visibility (0x14095e1d0, 0x14095e3d0): updates the SceneTreeNode's LWW
    label (field 2) or visible (field 3) with timestamp ++clock.
  - Moving a layer (0x14095df40 -> SceneTree::reorderNode 0x1408df3c0): deletes the old
    root CrdtGroupItem (deleted_length=1, value=None) and inserts a new CrdtGroupItem for
    the same node at the target (left, right) in ROOT_NODE's sequence; updates SceneInfo
    currentLayer so the active layer tracks the same node.
  - Merging a layer down (0x14095d840): iterates live items of src in sequence order;
    child CrdtGroupItems (including text-anchored groups) are moved to dst via
    SceneTree::moveNode (new group item in dst + SceneTreeMove to dst), while non-group
    items (lines, images, glyphs, text items) are cloned (id/parent/moveId cleared,
    timestamps kept; shader sourceIds renamed via rm_select._fresh_sources), appended to
    dst and deleted from src; then src is detached via SceneTreeMove to 0:0.
  - SceneInfo (0x0D): field 1 currentLayer (LWW id), field 2 backgroundVisible (LWW bool),
    field 3 rootDocumentVisible (LWW bool), each stamped with ++clock (0x1408aa470,
    0x1408aa340, 0x1408aad70).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from . import rm_select as rsel
from . import rm_stroke as rs
from . import rm_writer as w
from .rm_parser import RmParseError, _lww_bool, _lww_string

MAX_LAYERS = 5
ZERO_ID = w.ZERO_ID
ROOT_NODE = w.ROOT_NODE


class LayerError(RmParseError):
    """A layer edit that cannot be made on this page."""


@dataclass(frozen=True)
class LayerInfo:
    node_id: tuple[int, int]
    name: str
    visible: bool


@dataclass(frozen=True)
class MergeResult:
    """What merge_layer_down changed on the page, for undo."""

    page: bytes
    src: tuple[int, int]
    dst: tuple[int, int]
    moved_groups: tuple[tuple[int, int], ...]
    # (original item id in src, block type, raw value bytes, new clone id in dst)
    transferred: tuple[tuple[tuple[int, int], int, bytes, tuple[int, int]], ...]
    old_current: tuple[int, int]
    new_current: tuple[int, int]


# ------------------------------------------------------------------ inspection

def _tree_node_fields(block: w.Block) -> tuple[tuple[int, int], list[tuple[int, int, object]]]:
    values = w.read_values(block.payload)
    node_id = next((v for i, k, v in values if i == 1 and k == w.TAG_ID), None)
    if node_id is None:
        raise RmParseError("tree node has no id")
    return node_id, values


def _node_block_index(blocks: list[w.Block], node_id: tuple[int, int]) -> int | None:
    for n, block in enumerate(blocks):
        if block.type == w.BLOCK_TREE_NODE:
            found, _ = _tree_node_fields(block)
            if found == node_id:
                return n
    return None


def _node_meta(blocks: list[w.Block]) -> dict[tuple[int, int], tuple[str, bool]]:
    meta: dict[tuple[int, int], tuple[str, bool]] = {}
    for block in blocks:
        if block.type != w.BLOCK_TREE_NODE:
            continue
        node_id, values = _tree_node_fields(block)
        name, visible = "", True
        for i, k, v in values:
            if i == 2 and k == w.TAG_LENGTH4:
                name = _lww_string(v)
            elif i == 3 and k == w.TAG_LENGTH4:
                visible = _lww_bool(v)
        meta[node_id] = (name, visible)
    return meta


def layers_info(page: bytes | None) -> list[LayerInfo]:
    """The live layers of `page`, bottom to top (empty page: default Layer 1)."""
    if page is None:
        return [LayerInfo(w.LAYER_1, "Layer 1", True)]
    blocks = w.split(page)[1]
    meta = _node_meta(blocks)
    out = []
    for n, node_id in enumerate(w.layers(blocks)):
        name, visible = meta.get(node_id, ("", True))
        out.append(LayerInfo(node_id, name or f"Layer {n + 1}", visible))
    return out


def scene_info(page: bytes | None) -> tuple[tuple[int, int] | None, bool, bool]:
    """(current_layer_node_id, background_visible, root_document_visible) from SceneInfo (0x0D)."""
    if page is None:
        return None, True, True
    cur: tuple[int, int] | None = None
    bg = True
    root_doc = True
    for block in w.split(page)[1]:
        if block.type != w.BLOCK_SCENE_INFO:
            continue
        for i, k, v in w.read_values(block.payload):
            if i == 1 and k == w.TAG_LENGTH4:
                chosen = [val for idx, kind, val in w.read_values(v) if (idx, kind) == (2, w.TAG_ID)]
                if chosen and chosen[0] != ZERO_ID:
                    cur = chosen[0]
            elif i == 2 and k == w.TAG_LENGTH4:
                bg = _lww_bool(v)
            elif i == 3 and k == w.TAG_LENGTH4:
                root_doc = _lww_bool(v)
    return cur, bg, root_doc


def next_layer_name(blocks: list[w.Block]) -> str:
    """Next unused default layer name ("Layer N", starting at len(live) + 1: 0x1408a88d0)."""
    live = w.layers(blocks)
    meta = _node_meta(blocks)
    existing = {meta.get(node, ("", True))[0] or f"Layer {n + 1}" for n, node in enumerate(live)}
    num = len(live) + 1
    while f"Layer {num}" in existing:
        num += 1
    return f"Layer {num}"


# ------------------------------------------------------------------ block placement

def _insert_tree_move(blocks: list[w.Block], move_block: w.Block) -> None:
    """Insert a SceneTreeMove (0x01) sorted by move_id among existing tree moves."""
    new_key = w.id_key(w.decode_tree_move(move_block.payload)[0])
    moves = [n for n, b in enumerate(blocks) if b.type == w.BLOCK_TREE_MOVE]
    for n in moves:
        if w.id_key(w.decode_tree_move(blocks[n].payload)[0]) > new_key:
            blocks.insert(n, move_block)
            return
    if moves:
        blocks.insert(moves[-1] + 1, move_block)
        return
    # No existing tree moves: place before RootText, TreeNode, or items.
    at = next((n for n, b in enumerate(blocks)
               if b.type in (w.BLOCK_ROOT_TEXT, w.BLOCK_TREE_NODE, *w.ITEM_BLOCKS)), len(blocks))
    blocks.insert(at, move_block)


def _insert_tree_node(blocks: list[w.Block], node_block: w.Block) -> None:
    """Insert a SceneTreeNode (0x02) among existing tree nodes (after the last one with a
    smaller id, or before the item blocks)."""
    new_key = w.id_key(_tree_node_fields(node_block)[0])
    nodes = [n for n, b in enumerate(blocks) if b.type == w.BLOCK_TREE_NODE]
    for n in nodes:
        if w.id_key(_tree_node_fields(blocks[n])[0]) > new_key:
            blocks.insert(n, node_block)
            return
    if nodes:
        blocks.insert(nodes[-1] + 1, node_block)
        return
    at = next((n for n, b in enumerate(blocks) if b.type in w.ITEM_BLOCKS), len(blocks))
    blocks.insert(at, node_block)


def _set_lww_field(values: list[tuple[int, int, object]], field_index: int,
                   new_sub: bytes) -> list[tuple[int, int, object]]:
    """Replace `field_index` (a TAG_LENGTH4 LWW sub-block) in-place, or insert it in
    rising field-index order if absent."""
    out = list(values)
    for n, (idx, _, _) in enumerate(out):
        if idx == field_index:
            out[n] = (field_index, w.TAG_LENGTH4, new_sub)
            return out
    at = next((n for n, (idx, _, _) in enumerate(out) if idx > field_index), len(out))
    out.insert(at, (field_index, w.TAG_LENGTH4, new_sub))
    return out


def _update_scene_info(blocks: list[w.Block], me: int, clock: int, *,
                       current_layer: tuple[int, int] | None = None,
                       background_visible: bool | None = None,
                       root_document_visible: bool | None = None) -> int:
    """Update SceneInfo (0x0D) LWW fields in `blocks` in-place; returns the updated clock."""
    info_idx = next((n for n, b in enumerate(blocks) if b.type == w.BLOCK_SCENE_INFO), None)
    if info_idx is None:
        values: list[tuple[int, int, object]] = [
            (1, w.TAG_LENGTH4, w.f_id(1, ZERO_ID) + w.f_id(2, ZERO_ID)),
            (2, w.TAG_LENGTH4, w.f_id(1, ZERO_ID) + w.f_u8(2, 1)),
            (3, w.TAG_LENGTH4, w.f_id(1, ZERO_ID) + w.f_u8(2, 1)),
        ]
        stats = [n for n, b in enumerate(blocks) if b.type == w.BLOCK_PAGE_STATS]
        info_idx = (stats[-1] + 1) if stats else min(1, len(blocks))
        blocks.insert(info_idx, w.new_block(w.BLOCK_SCENE_INFO, w.write_values(values)))
    else:
        values = w.read_values(blocks[info_idx].payload)

    changed = False
    if current_layer is not None:
        clock += 1
        values = _set_lww_field(values, 1, w.f_id(1, (me, clock)) + w.f_id(2, tuple(current_layer)))
        changed = True
    if background_visible is not None:
        cur_bg = True
        for i, k, v in values:
            if i == 2 and k == w.TAG_LENGTH4:
                cur_bg = _lww_bool(v)
        if bool(background_visible) != cur_bg:
            clock += 1
            values = _set_lww_field(values, 2, w.f_id(1, (me, clock)) + w.f_u8(2, 1 if background_visible else 0))
            changed = True
    if root_document_visible is not None:
        cur_rd = True
        for i, k, v in values:
            if i == 3 and k == w.TAG_LENGTH4:
                cur_rd = _lww_bool(v)
        if bool(root_document_visible) != cur_rd:
            clock += 1
            values = _set_lww_field(values, 3, w.f_id(1, (me, clock)) + w.f_u8(2, 1 if root_document_visible else 0))
            changed = True
    if changed:
        orig = blocks[info_idx]
        blocks[info_idx] = w.Block(orig.type, orig.min_version, orig.version, w.write_values(values))
    return clock


def _has_live_group_item(blocks: list[w.Block], parent: tuple[int, int], child: tuple[int, int]) -> bool:
    for _, block in w.sequence(blocks, parent):
        if block.type != w.BLOCK_GROUP_ITEM:
            continue
        item = w.decode_item(block.payload)
        if item.value is not None and not item.deleted_length:
            target = next((v for i, k, v in w.read_values(item.value[1:]) if (i, k) == (2, w.TAG_ID)), None)
            if target == child:
                return True
    return False


def _live_group_item_id(blocks: list[w.Block], parent: tuple[int, int],
                        child: tuple[int, int]) -> tuple[int, int] | None:
    for _, block in w.sequence(blocks, parent):
        if block.type != w.BLOCK_GROUP_ITEM:
            continue
        item = w.decode_item(block.payload)
        if item.value is not None and not item.deleted_length:
            target = next((v for i, k, v in w.read_values(item.value[1:]) if (i, k) == (2, w.TAG_ID)), None)
            if target == child:
                return item.item
    return None


def _group_value(child: tuple[int, int]) -> bytes:
    return b"\x02" + w.f_id(2, child)


# ------------------------------------------------------------------ operations

def set_scene_info(page: bytes | None, *, author: bytes,
                   current_layer: tuple[int, int] | None = None,
                   background_visible: bool | None = None,
                   root_document_visible: bool | None = None) -> bytes:
    """Update SceneInfo (0x0D) fields 1 (currentLayer), 2 (backgroundVisible) and/or 3
    (rootDocumentVisible), keeping every other block of the page byte for byte."""
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    if current_layer is not None:
        current_layer = tuple(current_layer)
        if current_layer not in w.layers(blocks):
            raise LayerError(f"{current_layer} is not a live layer of the page")
    # Avoid mutating authors block if nothing changes.
    cur_layer, cur_bg, cur_rd = scene_info(w.join(header, blocks))
    need_layer = current_layer is not None and current_layer != cur_layer
    need_bg = background_visible is not None and bool(background_visible) != cur_bg
    need_rd = root_document_visible is not None and bool(root_document_visible) != cur_rd
    if page is not None and not (need_layer or need_bg or need_rd):
        return page
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    _update_scene_info(blocks, me, clock,
                       current_layer=current_layer if need_layer else None,
                       background_visible=background_visible if need_bg else None,
                       root_document_visible=root_document_visible if need_rd else None)
    return w.join(header, blocks)


def add_layer(page: bytes | None, *, author: bytes, name: str | None = None) -> tuple[bytes, tuple[int, int]]:
    """Create a new layer on top of the page (max 5 live layers) and make it active, as
    add-layer-crdt::redo (0x14095d320) does. Returns (new page bytes, new layer node_id)."""
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    live = w.layers(blocks)
    if len(live) >= MAX_LAYERS:
        raise LayerError(f"Limit of {MAX_LAYERS} layers reached")
    label = next_layer_name(blocks) if not name else str(name)
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    node_id = (me, clock + 1)
    label_ts = (me, clock + 2)
    group_id = (me, clock + 3)
    clock = clock + 3

    order = w.sequence(blocks, ROOT_NODE)
    left = order[-1][0] if order else ZERO_ID

    move_block = w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move(node_id, ZERO_ID, 1, ROOT_NODE))
    node_payload = (w.f_id(1, node_id)
                    + w.f_sub(2, w.lww(label_ts, w.string_value(label)))
                    + w.f_sub(3, w.f_id(1, ZERO_ID) + w.f_u8(2, 1)))
    node_block = w.new_block(w.BLOCK_TREE_NODE, node_payload)
    group_block = w.new_block(w.BLOCK_GROUP_ITEM,
                              w.ItemHeader(ROOT_NODE, group_id, left, ZERO_ID, 0, _group_value(node_id)).encode())

    _insert_tree_move(blocks, move_block)
    _insert_tree_node(blocks, node_block)
    blocks = w.insert_items(blocks, [group_block])
    _update_scene_info(blocks, me, clock, current_layer=node_id)
    return w.join(header, blocks), node_id


def detach_layer(page: bytes, *, author: bytes, layer: tuple[int, int],
                 new_current: tuple[int, int] | None = None) -> tuple[bytes, tuple[int, int]]:
    """Delete (detach) a live layer by appending a SceneTreeMove to (0, 0), as
    delete-layer-crdt::redo (0x14095d620) and add-layer-crdt::undo (0x14095ed60) do.
    Refuses to delete the last remaining layer. Returns (new page, active layer node_id)."""
    header, blocks = w.split(page)
    live = w.layers(blocks)
    layer = tuple(layer)
    if layer not in live:
        raise LayerError(f"{layer} is not a live layer of the page")
    if len(live) <= 1:
        raise LayerError("Cannot delete the last layer")
    deleted_idx = live.index(layer)
    old_cur = w.current_layer(blocks)
    old_cur_idx = live.index(old_cur)
    remaining = [n for n in live if n != layer]

    if new_current is not None and tuple(new_current) in remaining:
        active = tuple(new_current)
    elif deleted_idx == old_cur_idx:
        # 0x14095d7de: if deleting the top active layer, fall back to old_cur_idx - 1;
        # else stay at old_cur_idx (the layer that shifted into its slot).
        active = remaining[old_cur_idx - 1] if old_cur_idx == len(remaining) else remaining[old_cur_idx]
    else:
        active = old_cur

    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks) + 1
    _insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move((me, clock), layer, 0, ZERO_ID)))
    _update_scene_info(blocks, me, clock, current_layer=active)
    return w.join(header, blocks), active


def _restore_index(page: bytes, author: bytes, layer: tuple[int, int], index: int | None) -> bytes:
    """`page` with live `layer` back at `index` (0 = bottom) when re-attaching it reused a
    root group item that no longer sits there: another layer moved since (each move
    re-inserts that layer's group item), so the old slot can now be above or below the
    wrong neighbours. Uses move_layer, the same CrdtGroupItem delete + insert the app's
    reorderNode writes; a no-op when the layer is already at `index`."""
    if index is None:
        return page
    live = w.layers(w.split(page)[1])
    if layer not in live:
        return page
    index = max(0, min(int(index), len(live) - 1))
    if live.index(layer) == index:
        return page
    return move_layer(page, author=author, layer=layer, new_index=index)


def reattach_layer(page: bytes, *, author: bytes, layer: tuple[int, int],
                   set_current: tuple[int, int] | None = None, index: int | None = None) -> bytes:
    """Re-attach a previously detached layer under ROOT_NODE (SceneTreeMove is_update=1,
    parent=0:1), as delete-layer-crdt::undo (0x14095ef80) and add-layer-crdt::redo do,
    reusing its root group item. `index`: where undo/redo must put it back (the root
    group item it reuses can be out of place after other layers moved), see _restore_index."""
    header, blocks = w.split(page)
    layer = tuple(layer)
    live = w.layers(blocks)
    if layer in live:
        return _restore_index(page, author, layer, index)
    if len(live) >= MAX_LAYERS:
        raise LayerError(f"Limit of {MAX_LAYERS} layers reached")
    if _node_block_index(blocks, layer) is None:
        raise LayerError(f"layer node {layer} is not in the page")
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    if not _has_live_group_item(blocks, ROOT_NODE, layer):
        order = w.sequence(blocks, ROOT_NODE)
        left = order[-1][0] if order else ZERO_ID
        clock += 1
        blocks = w.insert_items(blocks, [w.new_block(
            w.BLOCK_GROUP_ITEM, w.ItemHeader(ROOT_NODE, (me, clock), left, ZERO_ID, 0, _group_value(layer)).encode())])
    clock += 1
    _insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move((me, clock), layer, 1, ROOT_NODE)))
    target_cur = tuple(set_current) if set_current is not None else layer
    if target_cur in w.layers(blocks):
        _update_scene_info(blocks, me, clock, current_layer=target_cur)
    # move_layer keeps currentLayer on the node just made current.
    return _restore_index(w.join(header, blocks), author, layer, index)


def set_layer_name(page: bytes | None, *, author: bytes, layer: tuple[int, int], name: str) -> bytes:
    """Rename a layer by updating its SceneTreeNode (0x02) field 2 LWW string (0x14095e1d0)."""
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    layer = tuple(layer)
    idx = _node_block_index(blocks, layer)
    if idx is None:
        raise LayerError(f"layer node {layer} is not in the page")
    _, values = _tree_node_fields(blocks[idx])
    current = next((_lww_string(v) for i, k, v in values if i == 2 and k == w.TAG_LENGTH4), "")
    if page is not None and current == name:
        return page
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks) + 1
    new_values = _set_lww_field(values, 2, w.lww((me, clock), w.string_value(str(name))))
    orig = blocks[idx]
    blocks[idx] = w.Block(orig.type, orig.min_version, orig.version, w.write_values(new_values))
    return w.join(header, blocks)


def set_layer_visible(page: bytes | None, *, author: bytes, layer: tuple[int, int], visible: bool) -> bytes:
    """Set a layer's visibility by updating its SceneTreeNode (0x02) field 3 LWW bool (0x14095e3d0)."""
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    layer = tuple(layer)
    idx = _node_block_index(blocks, layer)
    if idx is None:
        raise LayerError(f"layer node {layer} is not in the page")
    _, values = _tree_node_fields(blocks[idx])
    current = next((_lww_bool(v) for i, k, v in values if i == 3 and k == w.TAG_LENGTH4), True)
    if page is not None and current == bool(visible):
        return page
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks) + 1
    new_values = _set_lww_field(values, 3, w.f_id(1, (me, clock)) + w.f_u8(2, 1 if visible else 0))
    orig = blocks[idx]
    blocks[idx] = w.Block(orig.type, orig.min_version, orig.version, w.write_values(new_values))
    return w.join(header, blocks)


def move_layer(page: bytes, *, author: bytes, layer: tuple[int, int], new_index: int) -> bytes:
    """Reorder a live layer to `new_index` (0 = bottom, count - 1 = top), as
    move-layer-crdt::redo (0x14095df40) -> SceneTree::reorderNode (0x1408df3c0) does:
    deletes its old root CrdtGroupItem and inserts a new one at the target (left, right)
    in ROOT_NODE's CRDT sequence, keeping currentLayer on the same active layer node."""
    header, blocks = w.split(page)
    live = w.layers(blocks)
    layer = tuple(layer)
    if layer not in live:
        raise LayerError(f"{layer} is not a live layer of the page")
    if not 0 <= new_index < len(live):
        raise LayerError(f"invalid target layer index {new_index} of {len(live)}")
    if live.index(layer) == new_index:
        return page
    active = w.current_layer(blocks)
    old_item_id = _live_group_item_id(blocks, ROOT_NODE, layer)
    if old_item_id is None:
        raise LayerError(f"layer {layer} has no live root group item")

    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    chain = rsel._Node(blocks, ROOT_NODE)
    chain.delete(old_item_id)
    blocks = chain.write(blocks)

    remaining = [n for n in live if n != layer]
    root_ids = [i for i, _ in w.sequence(blocks, ROOT_NODE)]
    clock += 1
    new_item_id = (me, clock)
    val = _group_value(layer)
    kind = (w.BLOCK_GROUP_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_GROUP_ITEM])

    if new_index == 0:
        left = ZERO_ID
        right = root_ids[0] if root_ids else ZERO_ID
        blocks = w.insert_items(blocks, [w.Block(*kind, w.ItemHeader(ROOT_NODE, new_item_id, left, right, 0, val).encode())])
    elif new_index >= len(remaining):
        node = rsel._Node(blocks, ROOT_NODE)
        node.append(new_item_id, val, kind)
        blocks = node.write(blocks)
    else:
        target_item_id = _live_group_item_id(blocks, ROOT_NODE, remaining[new_index])
        if target_item_id is None or target_item_id not in root_ids:
            raise LayerError("target layer has no live root group item")
        at = root_ids.index(target_item_id)
        if at == 0:
            blocks = w.insert_items(blocks, [w.Block(
                *kind, w.ItemHeader(ROOT_NODE, new_item_id, ZERO_ID, target_item_id, 0, val).encode())])
        else:
            left = root_ids[at - 1]
            c2 = rs._Chain(blocks, ROOT_NODE)
            # Insert right after `left` (which cuts any deleted run containing `left` cleanly)
            link = c2.at[left]
            cut = left[1] - link.item.item[1] + 1
            if link.item.deleted_length > cut:
                rest = w.ItemHeader(ROOT_NODE, (left[0], left[1] + 1), left, link.item.right,
                                    link.item.deleted_length - cut, None)
                link.item = replace(link.item, deleted_length=cut)
                link.changed = True
                c2._after(link, rs._Link(rest, None, link.kind))
            right = link.next.item.item if link.next is not None else ZERO_ID
            c2._after(link, rs._Link(w.ItemHeader(ROOT_NODE, new_item_id, left, right, 0, val), None, kind))
            blocks = c2.write(blocks)

    _update_scene_info(blocks, me, clock, current_layer=active)
    return w.join(header, blocks)


def merge_layer_down(page: bytes, *, author: bytes, layer: tuple[int, int]) -> MergeResult:
    """Merge `layer` down into the layer directly beneath it, as merge-layer-down-crdt::redo
    (0x14095d840) does. Child groups (including text-anchored groups) move to dst via
    SceneTree::moveNode; strokes, images and other items are cloned (moveId cleared,
    timestamps kept; shader sourceIds renamed) onto the top of dst and deleted from src;
    then src is detached via SceneTreeMove to (0, 0)."""
    header, blocks = w.split(page)
    live = w.layers(blocks)
    layer = tuple(layer)
    if layer not in live:
        raise LayerError(f"{layer} is not a live layer of the page")
    src_idx = live.index(layer)
    if src_idx < 1:
        raise LayerError("Cannot merge the bottom layer down")
    dst = live[src_idx - 1]
    old_cur = w.current_layer(blocks)
    old_cur_idx = live.index(old_cur)
    parents = w.tree_parents(blocks)

    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)

    # Collect live items of src in CRDT sequence order.
    src_items: list[tuple[w.Block, w.ItemHeader]] = []
    seen_blocks: set[int] = set()
    for _, block in w.sequence(blocks, layer):
        if id(block) in seen_blocks:
            continue
        seen_blocks.add(id(block))
        item = w.decode_item(block.payload)
        if item.value is not None and not item.deleted_length:
            src_items.append((block, item))

    dst_node = rsel._Node(blocks, dst)
    src_chain = rs._Chain(blocks, layer)
    moved_groups: list[tuple[int, int]] = []
    new_moves: list[w.Block] = []

    # Shader sourceIds are renamed over all the strokes merged, before any is appended
    # (0x1408c2ae0), so the clones of one shader stroke keep sharing one new sourceId.
    lines = []
    for block, item in src_items:
        if block.type == w.BLOCK_LINE_ITEM:
            if block.version < 2:
                raise LayerError("cannot merge a version 1 stroke")
            lines.append(replace(rs.decode_line_value(item.value), move_id=None))
    if lines:
        lines, clock = rsel._fresh_sources(lines, me, clock)
    it_lines = iter(lines)

    # One pass in src's sequence order, as 0x14095d840 iterates it: groups and clones are
    # appended to dst in the order src held them, so a text-anchored group between two
    # strokes stays between their clones (z-order unchanged by the merge).
    transferred: list[tuple[tuple[int, int], int, bytes, tuple[int, int]]] = []
    for block, item in src_items:
        if block.type == w.BLOCK_GROUP_ITEM:
            child = next((v for i, k, v in w.read_values(item.value[1:]) if (i, k) == (2, w.TAG_ID)), None)
            if child is None or parents.get(child, layer) != layer:
                continue
            if not _has_live_group_item(blocks, dst, child):
                clock += 1
                dst_node.append((me, clock), _group_value(child),
                                (w.BLOCK_GROUP_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_GROUP_ITEM]))
            clock += 1
            new_moves.append(w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move((me, clock), child, 1, dst)))
            moved_groups.append(child)
            continue
        clock += 1
        new_id = (me, clock)
        if block.type == w.BLOCK_LINE_ITEM:
            encoded = next(it_lines).encode()
            bkind = (w.BLOCK_LINE_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_LINE_ITEM])
        elif block.type == w.BLOCK_IMAGE_ITEM:
            img = w.decode_image_value(item.value)
            encoded = w.ImageValue(img.ref, img.ref_timestamp, img.bounds_timestamp,
                                   list(img.vertices), img.indices, None).encode()
            bkind = (w.BLOCK_IMAGE_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_IMAGE_ITEM])
        else:
            encoded = item.value
            bkind = (block.type, *w.BLOCK_VERSIONS.get(block.type, (1, 1)))
        dst_node.append(new_id, encoded, bkind)
        src_chain.delete(item.item)
        transferred.append((item.item, block.type, item.value, new_id))

    blocks = dst_node.write(blocks)
    blocks = src_chain.write(blocks)
    for mb in new_moves:
        _insert_tree_move(blocks, mb)

    # Detach src layer (SceneTree::deleteNode).
    clock += 1
    _insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move((me, clock), layer, 0, ZERO_ID)))

    remaining = [n for n in live if n != layer]
    new_cur = remaining[old_cur_idx - 1] if src_idx <= old_cur_idx else old_cur
    _update_scene_info(blocks, me, clock, current_layer=new_cur)
    return MergeResult(w.join(header, blocks), layer, dst, tuple(moved_groups), tuple(transferred), old_cur, new_cur)


def unmerge_layer(page: bytes, *, author: bytes, src: tuple[int, int], dst: tuple[int, int],
                  moved_groups: tuple[tuple[int, int], ...],
                  restore_items: tuple[tuple[tuple[int, int], int, bytes, tuple[int, int] | None], ...],
                  remove_from_dst: tuple[tuple[int, int], ...],
                  old_current: tuple[int, int] | None = None,
                  src_index: int | None = None
                  ) -> tuple[bytes, dict[tuple[int, int], tuple[int, int]]]:
    """Undo a merge_layer_down (matching 0x14095f180): deletes the cloned items from dst,
    re-attaches src under ROOT_NODE, restores src's deleted items right after themselves,
    moves moved_groups back to src, and restores old_current. `src_index`: where src
    stood before the merge (see _restore_index).
    Returns (new page, {deleted_src_id: newly_restored_id})."""
    header, blocks = w.split(page)
    src, dst = tuple(src), tuple(dst)
    if _node_block_index(blocks, src) is None:
        raise LayerError(f"layer node {src} is not in the page")
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)

    # 1. Delete cloned items from dst first (so no image key has two live items).
    if remove_from_dst:
        dst_chain = rs._Chain(blocks, dst)
        for item_id in remove_from_dst:
            dst_chain.delete(tuple(item_id))
        blocks = dst_chain.write(blocks)

    # 2. Re-attach src under ROOT_NODE.
    if src not in w.layers(blocks):
        if not _has_live_group_item(blocks, ROOT_NODE, src):
            order = w.sequence(blocks, ROOT_NODE)
            left = order[-1][0] if order else ZERO_ID
            clock += 1
            blocks = w.insert_items(blocks, [w.new_block(
                w.BLOCK_GROUP_ITEM, w.ItemHeader(ROOT_NODE, (me, clock), left, ZERO_ID, 0, _group_value(src)).encode())])
        clock += 1
        _insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move((me, clock), src, 1, ROOT_NODE)))

    # 3. Restore src's deleted items right after their deleted slots.
    restored: dict[tuple[int, int], tuple[int, int]] = {}
    if restore_items:
        src_chain = rs._Chain(blocks, src)
        for item_id, kind, raw_value, origin in restore_items:
            item_id = tuple(item_id)
            if item_id not in src_chain.at:
                continue
            clock += 1
            new_id = (me, clock)
            move_id = tuple(origin) if origin is not None else item_id
            if kind == w.BLOCK_LINE_ITEM:
                line = rs.decode_line_value(raw_value)
                if line.move_id in (None, ZERO_ID):
                    line = replace(line, move_id=move_id)
                encoded = line.encode()
                bkind = (w.BLOCK_LINE_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_LINE_ITEM])
            elif kind == w.BLOCK_IMAGE_ITEM:
                img = w.decode_image_value(raw_value)
                back = w.ImageValue(img.ref, img.ref_timestamp, img.bounds_timestamp, list(img.vertices),
                                    img.indices, img.move_id if img.move_id not in (None, ZERO_ID) else move_id)
                encoded = back.encode()
                bkind = (w.BLOCK_IMAGE_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_IMAGE_ITEM])
            else:
                encoded = raw_value
                bkind = (kind, *w.BLOCK_VERSIONS.get(kind, (1, 1)))
            link = src_chain.at[item_id]
            cut = item_id[1] - link.item.item[1] + 1
            if link.item.deleted_length > cut:
                rest = w.ItemHeader(src, (item_id[0], item_id[1] + 1), item_id, link.item.right,
                                    link.item.deleted_length - cut, None)
                link.item = replace(link.item, deleted_length=cut)
                link.changed = True
                src_chain._after(link, rs._Link(rest, None, link.kind))
            right = link.next.item.item if link.next is not None else ZERO_ID
            src_chain._after(link, rs._Link(w.ItemHeader(src, new_id, item_id, right, 0, encoded), None, bkind))
            restored[item_id] = new_id
        blocks = src_chain.write(blocks)

    # 4. Move child groups back to src.
    for child in moved_groups:
        child = tuple(child)
        if not _has_live_group_item(blocks, src, child):
            src_node = rsel._Node(blocks, src)
            clock += 1
            src_node.append((me, clock), _group_value(child),
                            (w.BLOCK_GROUP_ITEM, *w.BLOCK_VERSIONS[w.BLOCK_GROUP_ITEM]))
            blocks = src_node.write(blocks)
        clock += 1
        _insert_tree_move(blocks, w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move((me, clock), child, 1, src)))

    # 5. Restore old_current.
    if old_current is not None and tuple(old_current) in w.layers(blocks):
        _update_scene_info(blocks, me, clock, current_layer=tuple(old_current))
    return _restore_index(w.join(header, blocks), author, src, src_index), restored


def clear_lines_in_layer(page: bytes, *, author: bytes,
                         layer: tuple[int, int]) -> tuple[bytes, list[tuple[tuple[int, int], bytes]]]:
    """ClearPage on the live layer `layer` (rm_stroke.clear_layer: its strokes and those
    of the groups below it deleted, as SceneController::clearLinesInLayer does).
    Returns (new page, [(deleted item id, line value bytes)]) in the order the app's
    undo puts them back (rm_stroke.Cleared.undo_order). `author` is not used: a
    clear takes no id."""
    _, blocks = w.split(page)
    layer = tuple(layer)
    if layer not in w.layers(blocks):
        raise LayerError(f"{layer} is not a live layer of the page")
    cleared = rs.clear_layer(page, layer=layer)
    return cleared.page, list(cleared.undo_order())


__all__ = [
    "LayerError", "LayerInfo", "MAX_LAYERS", "MergeResult",
    "add_layer", "clear_lines_in_layer", "detach_layer", "layers_info",
    "merge_layer_down", "move_layer", "next_layer_name", "reattach_layer",
    "scene_info", "set_layer_name", "set_layer_visible", "set_scene_info", "unmerge_layer",
]
