"""Groups of strokes anchored to a page's typed text, as reMarkable.exe 3.29's anchor
manager (scene::AnchorManager, "rm.scene.anchors") makes them (ARCHITECTURE.md 5.1,
"Text-anchored stroke groups"; 5.13 for pasting at the text cursor).

An anchored group is three blocks: a SceneTreeMove (move id = the node id N, node 0:0,
is_update 1, parent the layer), a SceneTreeNode N (an empty label and visible 1, both
stamped 0:0, then the anchor: anchorId stamped N+2, anchorType N+3, anchorThreshold
N+4, anchorOriginX N), and a group item N+1 at the end of the layer's sequence. The
group's strokes store their points less the anchor position: x - anchorOriginX (type 2,
"Vertical"), y - the baseline of the line holding the anchor character.

Checked byte for byte against the 710 anchored nodes of a real library (all have the
empty label and visible 1 at 0:0; createGroup 0x1408dfdb0 takes N, then the group item;
the anchor fields are then stamped N+2..N+4, originX N).
"""

from __future__ import annotations

import struct

from . import rm_layer
from . import rm_writer as w
from .rm_parser import ANCHOR_SENTINEL_START

ZERO_ID = (0, 0)
BIDIRECTIONAL, VERTICAL = 1, 2               # anchorType (2 on 700 of 710 real nodes)
# manager+0x18: 4 mm at 227 dpi, as stored (f32 0x420efdfc) on every type 2 node.
THRESHOLD = struct.unpack("<f", bytes.fromhex("fcfd0e42"))[0]
BEFORE_TEXT = (0, ANCHOR_SENTINEL_START)     # above the text box (0x140972ed0)
AFTER_TEXT = (0, ANCHOR_SENTINEL_START + 1)  # below it (0x140972cf0)


class AnchorError(ValueError):
    pass


def _lww_id(stamp: tuple[int, int], value: tuple[int, int]) -> bytes:
    return w.f_id(1, stamp) + w.f_id(2, value)


def add_group(page: bytes | None, *, author: bytes, layer: tuple[int, int] | None, char_id: tuple[int, int],
              origin_x: float, kind: int = VERTICAL, threshold: float | None = None) -> tuple[bytes, tuple[int, int]]:
    """`page` with a new group on `layer` (None: the current layer) anchored to the
    character `char_id` of its text (or a sentinel), and the group's node id. `threshold`
    defaults to THRESHOLD for type 2 and 0 for type 1, as the manager stores them."""
    header, blocks = w.split(w.blank_page(author) if page is None else page)
    layer = w.current_layer(blocks) if layer is None else tuple(layer)
    if layer not in w.layers(blocks):
        raise AnchorError(f"{layer} is not a layer of the page")
    if threshold is None:
        threshold = THRESHOLD if kind == VERTICAL else 0.0
    me = w._author_index(blocks, author)
    clock = w.highest_counter(blocks)
    node = (me, clock + 1)
    group_id = (me, clock + 2)
    order = w.sequence(blocks, layer)
    left = order[-1][0] if order else ZERO_ID
    payload = (w.f_id(1, node)
               + w.f_sub(2, w.lww(ZERO_ID, w.string_value("")))
               + w.f_sub(3, w.f_id(1, ZERO_ID) + w.f_u8(2, 1))
               + w.f_sub(7, _lww_id((me, clock + 3), tuple(char_id)))
               + w.f_sub(8, w.f_id(1, (me, clock + 4)) + w.f_u8(2, int(kind)))
               + w.f_sub(9, w.f_id(1, (me, clock + 5)) + w.f_f32(2, float(threshold)))
               + w.f_sub(10, w.f_id(1, node) + w.f_f32(2, float(origin_x))))
    move = w.new_block(w.BLOCK_TREE_MOVE, w.encode_tree_move(node, ZERO_ID, 1, layer))
    node_block = w.new_block(w.BLOCK_TREE_NODE, payload)
    group = w.new_block(w.BLOCK_GROUP_ITEM,
                        w.ItemHeader(layer, group_id, left, ZERO_ID, 0, rm_layer._group_value(node)).encode())
    rm_layer._insert_tree_move(blocks, move)
    rm_layer._insert_tree_node(blocks, node_block)
    blocks = w.insert_items(blocks, [group])
    # The page's clock is its highest id: the anchor's stamps (up to N+4) are ids of the
    # node block, so what is added next comes after them.
    return w.join(header, blocks), node


def is_group_node(page: bytes | None, node: tuple[int, int]) -> bool:
    """Whether `node` is a group node of the page (its tree node is there, it is no layer)."""
    if page is None:
        return False
    blocks = w.split(page)[1]
    return tuple(node) not in w.layers(blocks) and rm_layer._node_block_index(blocks, tuple(node)) is not None
