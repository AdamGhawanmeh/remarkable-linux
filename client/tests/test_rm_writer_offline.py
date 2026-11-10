"""Offline tests for viewer.rm_writer: pages come back byte for byte, and an edit adds
only what it must. The same checks run over real pages with tools/rm_roundtrip.py.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rmfixtures import (  # noqa: E402
    ROOT, ZERO, block, f_id, f_sub, f_u8, f_u32, group_item, image_entries, image_item, image_value,
    images_table, line_item, points_v2, tombstone, tree_node, varuint)

from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import RmParseError, parse  # noqa: E402

TABLET = bytes.fromhex("00112233445566778899aabbccddeeff")
DESKTOP = bytes.fromhex("ffeeddccbbaa99887766554433221100")
KEY = bytes(range(16))
KEY_2 = bytes(range(16, 32))
NAME = "0a1b2c3d-0000-4000-8000-000000000001.png"
LAYER = (0, 11)


def corners(x0, y0, x1, y1):
    return [(x0, y0, 0.0, 0.0), (x1, y0, 1.0, 0.0), (x1, y1, 1.0, 1.0), (x0, y1, 0.0, 1.0)]


def stroke(item, left=ZERO, parent=LAYER) -> bytes:
    data = line_item(parent, item, tool=17, color=0, points=points_v2([(0, 0, 0, 0, 2, 255)]))
    _, [b] = w.split(w.V6_HEADER + data)
    header = w.decode_item(b.payload)
    header.left = left
    return w.Block(b.type, b.min_version, b.version, header.encode()).encode()


def page(*items: bytes, author: bytes = TABLET, extra: bytes = b"") -> bytes:
    """A blank page of `author` with more blocks: `extra` after SceneInfo, `items` at the end."""
    header, blocks = w.split(w.blank_page(author))
    data = w.join(header, blocks[:4]) + extra + b"".join(b.encode() for b in blocks[4:]) + b"".join(items)
    return data


def types(data: bytes) -> list[int]:
    return [b.type for b in w.split(data)[1]]


def blocks_of(data: bytes, block_type: int) -> list[w.Block]:
    return [b for b in w.split(data)[1] if b.type == block_type]


class SplitJoinTests(unittest.TestCase):
    def test_a_page_comes_back_byte_for_byte(self):
        data = page(stroke((1, 15)), tombstone(LAYER, (1, 16)))
        header, blocks = w.split(data)
        self.assertEqual(w.join(header, blocks), data)
        self.assertEqual([b.offset for b in blocks][:2], [43, 43 + 8 + len(blocks[0].payload)])

    def test_unknown_blocks_are_kept(self):
        data = page() + block(0x7E, b"\x01\x02\x03", version=9, min_version=4)
        header, blocks = w.split(data)
        self.assertEqual((blocks[-1].type, blocks[-1].min_version, blocks[-1].version), (0x7E, 4, 9))
        self.assertEqual(w.join(header, blocks), data)

    def test_damaged_pages_are_refused(self):
        data = page()
        for bad in (b"not a page", data[:-1], data[:44], data[:47] + b"\x01" + data[48:]):
            with self.assertRaises(RmParseError):
                w.split(bad)

    def test_values_keep_order_and_duplicates(self):
        data = f_u8(3, 1) + f_id(1, (1, 300)) + f_u8(3, 2) + f_sub(2, b"xy") + f_u32(4, 7)
        self.assertEqual(w.write_values(w.read_values(data)), data)
        self.assertEqual([i for i, _, _ in w.read_values(data)], [3, 1, 3, 2, 4])


class CodecTests(unittest.TestCase):
    def test_authors(self):
        payload = varuint(2) + f_sub(0, varuint(16) + DESKTOP + b"\x02\x00") + f_sub(0, varuint(16) + TABLET + b"\x01\x00")
        self.assertEqual(w.decode_authors(payload), [(DESKTOP, 2), (TABLET, 1)])
        self.assertEqual(w.encode_authors(w.decode_authors(payload)), payload)
        with self.assertRaises(RmParseError):
            w.decode_authors(payload + b"\x00")

    def test_author_uuid_is_the_v5_uuid_of_the_device_id(self):
        made = w.author_uuid("0f1e2d3c-4b5a-4978-8695-a4b3c2d1e0ff")
        self.assertEqual(made, uuid.uuid5(uuid.UUID("6ba7b814-9dad-11d1-80b4-00c04fd430c8"),
                                          "0f1e2d3c-4b5a-4978-8695-a4b3c2d1e0ff").bytes)
        self.assertEqual((made[6] >> 4, made[8] >> 6), (5, 2))
        with self.assertRaises(ValueError):
            w.author_uuid("")

    def test_images_table_as_the_tablet_writes_it(self):
        _, [table] = w.split(w.V6_HEADER + images_table(image_entries({KEY: NAME})))
        [entry] = w.decode_images_table(table.payload)
        self.assertEqual((entry.key, entry.file_name, entry.timestamp, entry.rest), (KEY, NAME, (1, 15), w.ENTRY_REST))
        self.assertEqual(w.encode_images_table([entry]), table.payload)
        _, [empty] = w.split(w.V6_HEADER + images_table())
        self.assertEqual(w.decode_images_table(empty.payload), [])
        self.assertEqual(w.encode_images_table([]), empty.payload)

    def test_image_value_as_the_tablet_writes_it(self):
        raw = image_value(KEY, corners(-100, 50, 200, 350), move_id=(1, 16))
        value = w.decode_image_value(raw)
        self.assertEqual((value.ref, value.ref_timestamp, value.bounds_timestamp, value.move_id, value.indices),
                         (KEY, (1, 18), (1, 17), (1, 16), w.IMAGE_INDICES))
        self.assertEqual(value.encode(), raw)
        self.assertEqual(w.decode_image_value(image_value(KEY, corners(0, 0, 1, 1))).encode(),
                         image_value(KEY, corners(0, 0, 1, 1)))

    def test_blank_page_parses_as_one_empty_layer(self):
        scene = parse(w.blank_page(TABLET))
        self.assertEqual([(layer.name, layer.visible, len(layer.strokes)) for layer in scene.layers], [("Layer 1", True, 0)])
        self.assertEqual(types(w.blank_page(TABLET)), [0x09, 0x00, 0x0A, 0x0D, 0x01, 0x02, 0x02, 0x04])
        self.assertEqual(w.highest_counter(w.split(w.blank_page(TABLET))[1]), 14)


class IdTests(unittest.TestCase):
    def test_highest_counter_counts_deleted_runs_and_timestamps(self):
        blocks = w.split(page(stroke((1, 15)), image_item(LAYER, (1, 16), deleted=5, left=(1, 15))))[1]
        self.assertEqual(w.highest_counter(blocks), 20)
        blocks = w.split(page(stroke((1, 15)), extra=images_table(image_entries({KEY: NAME}, timestamp=(1, 77)))))[1]
        self.assertEqual(w.highest_counter(blocks), 77)

    def test_a_block_that_is_not_understood_is_refused(self):
        for bad in (block(0x7E, b""), block(0x0D, f_sub(11, b"")), block(0x05, w.ItemHeader(LAYER, (1, 9), ZERO, ZERO, 0, b"\x55").encode(), 2, 2)):
            _, [b] = w.split(w.V6_HEADER + bad)
            with self.assertRaises(RmParseError):
                w.block_ids(b)

    def test_sequence_replays_insertions(self):
        # 15 drawn, 16 drawn after it, 17 put between them, 18..19 a deleted run after 17.
        data = page(stroke((1, 15)), stroke((1, 16), (1, 15)), stroke((1, 17), (1, 15)),
                    image_item(LAYER, (1, 18), deleted=2, left=(1, 17)))
        self.assertEqual([i for i, _ in w.sequence(w.split(data)[1], LAYER)],
                         [(1, 15), (1, 17), (1, 18), (1, 19), (1, 16)])
        self.assertEqual(w.sequence(w.split(w.blank_page(TABLET))[1], LAYER), [])

    def test_sequence_refuses_an_item_after_a_missing_neighbour(self):
        with self.assertRaises(RmParseError):
            w.sequence(w.split(page(stroke((1, 16), (1, 15))))[1], LAYER)

    def test_layers_and_current_layer(self):
        second = (1, 20)
        data = page(group_item(ROOT, (1, 21), second))
        _, blocks = w.split(data)
        blocks[-1] = w.Block(0x04, 1, 1, w.ItemHeader(ROOT, (1, 21), (0, 13), ZERO, 0, b"\x02" + f_id(2, second)).encode())
        self.assertEqual(w.layers(blocks), [LAYER, second])
        self.assertEqual(w.current_layer(blocks), LAYER)              # currentLayer not set
        info = next(n for n, b in enumerate(blocks) if b.type == 0x0D)
        values = w.read_values(blocks[info].payload)
        values[0] = (1, values[0][1], f_id(1, (1, 22)) + f_id(2, second))
        blocks[info] = w.Block(0x0D, 0, 1, w.write_values(values))
        self.assertEqual(w.current_layer(blocks), second)


class AppendImageTests(unittest.TestCase):
    def add(self, data, **kw):
        kw = {"author": TABLET, "key": KEY, "file_name": NAME, "vertices": corners(-100, 50, 200, 350), **kw}
        return w.append_image(data, **kw)

    def test_first_image_is_written_as_the_tablet_writes_one(self):
        """On a fresh page the new blocks are encoded as the tablet encodes its own (the
        fixture builders reproduce a real page), with the ids in the order the desktop
        app takes them: file name 1:15, boundsTimestamp 1:16, imageRef 1:17, item 1:18,
        and no moveId."""
        data = self.add(w.blank_page(TABLET))
        self.assertEqual(types(data), [0x09, 0x00, 0x0A, 0x0D, 0x0E, 0x01, 0x02, 0x02, 0x04, 0x0F])
        [table], [item] = blocks_of(data, 0x0E), blocks_of(data, 0x0F)
        self.assertEqual(table.encode(), images_table(image_entries({KEY: NAME})))
        self.assertEqual(item.encode(), image_item(LAYER, (1, 18), image_value(
            KEY, corners(-100, 50, 200, 350), timestamp=(1, 17), bounds_timestamp=(1, 16))))
        [image] = parse(data).placed_images
        self.assertEqual((image.file_name, image.parent_id, image.vertices), (NAME, LAYER, corners(-100, 50, 200, 350)))

    def test_no_page_yet_starts_from_a_blank_one(self):
        self.assertEqual(self.add(None), self.add(w.blank_page(TABLET)))

    def test_existing_blocks_are_untouched(self):
        before = page(stroke((1, 15)), stroke((1, 16), (1, 15)), tombstone(LAYER, (1, 17)))
        after = self.add(before)
        old, new = w.split(before)[1], w.split(after)[1]
        self.assertEqual([b.encode() for b in new if b.type not in (0x0E, 0x0F)], [b.encode() for b in old])
        self.assertEqual(len(parse(after).strokes), 2)

    def test_goes_on_top_of_the_layer_in_sequence_order(self):
        # 17 was put under 16: the top of the layer is 16, not the last block.
        data = self.add(page(stroke((1, 15)), stroke((1, 16), (1, 15)), stroke((1, 17), (1, 15))))
        item = w.decode_item(blocks_of(data, 0x0F)[0].payload)
        self.assertEqual((item.item, item.left, item.right, item.deleted_length), ((1, 21), (1, 16), ZERO, 0))

    def test_goes_after_a_deleted_run_at_the_top(self):
        data = self.add(page(image_item(LAYER, (1, 16), deleted=2), extra=images_table()))
        item = w.decode_item(blocks_of(data, 0x0F)[-1].payload)
        self.assertEqual((item.item, item.left), ((1, 21), (1, 17)))

    def test_another_author_is_added_with_the_next_index(self):
        data = self.add(page(stroke((1, 15))), author=DESKTOP)
        self.assertEqual(w.decode_authors(w.split(data)[1][0].payload), [(TABLET, 1), (DESKTOP, 2)])
        item = w.decode_item(blocks_of(data, 0x0F)[0].payload)
        value = w.decode_image_value(item.value)
        self.assertEqual((item.item, item.left), ((2, 19), (1, 15)))
        self.assertEqual((value.ref_timestamp, value.bounds_timestamp, value.move_id), ((2, 18), (2, 17), None))
        self.assertEqual(w.decode_images_table(blocks_of(data, 0x0E)[0].payload)[0].timestamp, (2, 16))
        again = self.add(data, author=DESKTOP, key=KEY_2, file_name="b.jpg")
        self.assertEqual(w.split(again)[1][0].payload, w.split(data)[1][0].payload)
        self.assertEqual(w.decode_item(blocks_of(again, 0x0F)[1].payload).item, (2, 23))

    def test_second_image_joins_the_table_and_stacks_above(self):
        once = self.add(w.blank_page(TABLET))
        twice = self.add(once, key=KEY_2, file_name="b.jpg", vertices=corners(0, 0, 10, 10))
        self.assertEqual([(e.key, e.file_name, e.timestamp) for e in w.decode_images_table(blocks_of(twice, 0x0E)[0].payload)],
                         [(KEY, NAME, (1, 15)), (KEY_2, "b.jpg", (1, 19))])
        first, second = (w.decode_item(b.payload) for b in blocks_of(twice, 0x0F))
        self.assertEqual((second.item, second.left), ((1, 22), first.item))
        self.assertEqual(blocks_of(twice, 0x0F)[0].encode(), blocks_of(once, 0x0F)[0].encode())
        self.assertEqual([i.file_name for i in parse(twice).placed_images], [NAME, "b.jpg"])
        with self.assertRaises(ValueError):
            self.add(once)                                  # the same key again

    def test_item_blocks_stay_sorted_by_parent_then_id(self):
        second = (1, 20)
        _, blocks = w.split(page(stroke((1, 15))))
        blocks.insert(-1, w.Block(0x04, 1, 1, w.ItemHeader(ROOT, (1, 21), (0, 13), ZERO, 0, b"\x02" + f_id(2, second)).encode()))
        _, [late] = w.split(w.V6_HEADER + stroke((1, 22), parent=second))
        data = self.add(w.join(w.V6_HEADER, blocks + [late]), layer=LAYER)
        self.assertEqual([(w.decode_item(b.payload).parent, w.decode_item(b.payload).item) for b in w.split(data)[1][-5:]],
                         [(ROOT, (0, 13)), (ROOT, (1, 21)), (LAYER, (1, 15)), (LAYER, (1, 26)), (second, (1, 22))])

    def test_refuses_what_it_cannot_do_safely(self):
        good = page(stroke((1, 15)))
        with self.assertRaises(RmParseError):               # a block it does not understand
            self.add(good + block(0x7E, b""))
        with self.assertRaises(RmParseError):               # not a layer of this page
            self.add(good, layer=(1, 99))
        with self.assertRaises(ValueError):
            self.add(good, key=b"short")
        with self.assertRaises(ValueError):
            self.add(good, vertices=corners(0, 0, 1, 1)[:3])
        with self.assertRaises(RmParseError):               # no authors block
            self.add(w.V6_HEADER + b"".join(b.encode() for b in w.split(good)[1][1:]))
        full = w.V6_HEADER + block(0x09, w.encode_authors([(TABLET, 255)])) + b"".join(b.encode() for b in w.split(good)[1][1:])
        with self.assertRaises(RmParseError):               # no author index left
            self.add(full, author=DESKTOP)


if __name__ == "__main__":
    unittest.main()
