"""Offline tests for viewer.rm_stroke and the stroke edits of core.edits: a stroke's
value comes back byte for byte, points are made from pen samples and stored as
reMarkable.exe does both, a drawn stroke goes on top of its layer, a deleted one joins
its deleted neighbours, and an eraser cuts strokes into the pieces it leaves. The same
engine runs over real pages with tools/rm_roundtrip.py.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import math
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rmfixtures import f_id, f_sub, line_item, points_v2, root_text, tombstone  # noqa: E402
from test_edits_offline import NOW, OTHER, PAGE1, PAGE2, THEN, THROWAWAY, Base, drawn_page  # noqa: E402

import push_edits  # noqa: E402
from core.edits import (EditError, PendingEdits, StrokeAdd, StrokeDelete, StrokeErase, StrokeRestore,  # noqa: E402
                        ToolSettings, document_files, page_with_edits, replay)
from core.sync import SyncClient, describe_edit, json_changes  # noqa: E402
from fakecloud import paired_auth  # noqa: E402
from viewer import rm_stroke as s  # noqa: E402
from viewer import rm_writer as w  # noqa: E402
from viewer.rm_parser import Color, RmParseError, Tool, parse  # noqa: E402

TABLET = bytes.fromhex("00112233445566778899aabbccddeeff")
DESKTOP = bytes.fromhex("ffeeddccbbaa99887766554433221100")
LAYER = (0, 11)
ZERO = (0, 0)
DOT = points_v2([(0, 0, 0, 0, 2, 255), (5, 5, 0, 0, 2, 255)])


def item_block(data: bytes, **header) -> bytes:
    """A fixture block with its item header's fields set (left, right, ...)."""
    _, [b] = w.split(w.V6_HEADER + data)
    item = dataclasses.replace(w.decode_item(b.payload), **header)
    return w.new_block(b.type, item.encode()).encode()          # under the versions real pages have


def stroke(item, left=ZERO, right=ZERO, parent=LAYER, points: bytes = DOT, **line) -> bytes:
    return item_block(line_item(parent, item, tool=line.pop("tool", 17), color=line.pop("color", 0), points=points,
                                **line), left=left, right=right)


def gone(item, left=ZERO, right=ZERO, length=1, parent=LAYER) -> bytes:
    """A tombstone block: `length` deleted ids from `item` on."""
    return item_block(tombstone(parent, item), left=left, right=right, deleted_length=length)


def page(*items: bytes, author: bytes = TABLET, extra: bytes = b"") -> bytes:
    header, blocks = w.split(w.blank_page(author))
    return w.join(header, blocks[:5]) + extra + b"".join(b.encode() for b in blocks[5:]) + b"".join(items)


def chain(count: int, first: int = 15) -> bytes:
    """A page the tablet drew `count` strokes on, one after the other."""
    return page(*(stroke((1, first + n), (1, first + n - 1) if n else ZERO) for n in range(count)))


def items(data: bytes, parent=LAYER) -> list[tuple]:
    """(block type, id, left, right, deleted length, has a value) of a node's item blocks, in file order."""
    return [(b.type, i.item, i.left, i.right, i.deleted_length, i.value is not None)
            for b in w.split(data)[1] if b.type in w.ITEM_BLOCKS
            for i in [w.decode_item(b.payload)] if i.parent == parent]


def ruled(y: float = 100.0, x0: float = -100.5, x1: float = 100.5, step: float = 7.3, **pen) -> s.LineValue:
    """A level stroke from x0 to x1, a mouse sample every `step`."""
    count = int((x1 - x0) / step)
    return s.draw([(x0 + step * n, y) for n in range(count)] + [(x1, y)], pen.pop("tool", s.FINELINER_V2), **pen)


def upright(x: float = 0.25, y0: float = 60.0, y1: float = 140.0, thickness: float = s.MEDIUM) -> s.LineValue:
    """An eraser taken straight down at `x`: 20 wide for MEDIUM."""
    return s.eraser_line([(x, y0), (x, (y0 + y1) / 2), (x, y1)], thickness)


class LineValueTests(unittest.TestCase):
    def value(self, **more) -> s.LineValue:
        return s.LineValue(17, 0, 2.0, 0.0, DOT, **more)

    def test_a_value_is_written_field_by_field_as_the_app_writes_it(self):
        self.assertEqual(self.value().encode().hex(),
                         "03" "1411000000" "2400000000" "380000000000000040" "4400000000"
                         "5c1c000000" + DOT.hex() + "6f0001")
        full = s.LineValue(18, 9, 1.0, 31.5, DOT, (1, 40), (1, 600), 0xFFFFED75, (1, 7), b"\xaf\x01\x02")
        self.assertEqual(full.encode().hex(),
                         "03" "1412000000" "2409000000" "38000000000000f03f" "440000fc41" "5c1c000000" + DOT.hex()
                         + "6f0128" "7f01d804" "840175edffff" "9f010107" "af0102")

    def test_decoding_gives_back_every_field_and_the_same_bytes(self):
        for line in (self.value(), self.value(move_id=(1, 600)), self.value(timestamp=None),
                     s.LineValue(18, 9, 1.0, 31.5, DOT, (1, 40), (1, 600), 0xFFFFED75, (1, 7), b"\xaf\x01\x02"),
                     s.LineValue(23, 9, 2.0, 0.0, DOT, argb=0x4020F21C, source_id=(2, 9))):
            self.assertEqual(s.decode_line_value(line.encode()), line)
            self.assertEqual(s.decode_line_value(line.encode()).encode(), line.encode())

    def test_a_value_that_would_not_come_back_the_same_is_refused(self):
        good = self.value().encode()
        for bad in (b"", b"\x07" + good[1:], good[:-1], good[:1] + good[6:],
                    self.value().encode().replace(DOT, DOT[:-1]).replace(b"\x5c\x1c", b"\x5c\x1b"),
                    b"\x03" + w.f_u32(2, 0) + good[1:6] + good[11:]):                 # colour before tool
            with self.assertRaises(RmParseError):
                s.decode_line_value(bad)

    def test_a_new_line_has_the_timestamp_of_a_new_item_and_argb_only_for_argb(self):
        black = s.new_line(17, 0, 2.0, DOT, argb=0xFF112233)
        self.assertEqual((black.timestamp, black.move_id, black.argb, black.source_id, black.tail),
                         ((0, 1), None, None, None, b""))
        self.assertEqual(s.new_line(18, 9, 1.0, DOT, argb=0xFFFFED75).argb, 0xFFFFED75)
        self.assertEqual(s.new_line(18, 9, 1.0, DOT).argb, 0xFF000000)

    def test_the_parser_keeps_what_it_used_to_drop(self):
        value = s.LineValue(17, 0, 2.0, 12.5, DOT, (1, 40), (1, 16), None, (1, 9), b"\xaf\x01\x02")
        block = w.new_block(w.BLOCK_LINE_ITEM, w.ItemHeader(LAYER, (1, 20), (1, 16), (1, 17), 0, value.encode()).encode())
        [found] = parse(page(gone((1, 15), length=2), stroke((1, 17), (1, 16)), block.encode())).strokes[-1:]
        self.assertEqual((found.item_id, found.left, found.right, found.timestamp, found.move_id, found.source_id,
                          found.tail, found.starting_length),
                         ((1, 20), (1, 16), (1, 17), (1, 40), (1, 16), (1, 9), b"\xaf\x01\x02", 12.5))


class PointTests(unittest.TestCase):
    def test_a_point_is_fourteen_bytes(self):
        raw = bytes.fromhex("0000f7c200003542" "0200" "1000" "db" "35")      # x -123.5, y 45.25
        [point] = s.unpack_points(raw)
        self.assertEqual(point, s.Point(-123.5, 45.25, 2, 16, 219, 53))
        self.assertEqual(s.pack_points([point]), raw)
        again = s.make_point(point.x, point.y, 0.5, 4.0, 5.3961, 0.2078)
        self.assertEqual(s.pack_points([again]), raw)

    def test_values_are_rounded_half_away_from_zero_and_not_clamped(self):
        point = s.make_point(1.0, 2.0, 0.625, 0.875, 2 * math.pi, 0.7)
        self.assertEqual(point[2:], (3, 4, 255, 179))                        # 2.5 -> 3, 3.5 -> 4, 178.5 -> 179
        self.assertEqual(s.make_point(0, 0, 0, 0, -1.0, 0)[4], 215)         # -40.6 -> -41, as a byte
        with self.assertRaises(RmParseError):
            s.unpack_points(bytes(15))


class SynthesisTests(unittest.TestCase):
    """What a mouse draws: samples of pressure 0.7 and no tilt (PenInputSurface)."""

    PATH = [(0.0, 0.0), (3.0, 4.0), (6.0, 8.0)]

    def drawn(self, tool, thickness=s.MEDIUM, path=None, **how) -> list[s.Point]:
        pen = s.StrokeBuilder(tool, 0, thickness, **how)
        for sample in path or self.PATH:
            pen.add(*sample)
        return pen.points

    def test_every_sample_becomes_a_point_where_it_is(self):
        for tool in s.PENS:
            self.assertEqual([(p.x, p.y) for p in self.drawn(tool)], self.PATH, tool)
        self.assertEqual(len(self.drawn(s.FINELINER_V2, path=[(1.0, 1.0), (1.0, 1.0)])), 2)     # a click

    def test_a_fineliner_is_twice_its_size_wide_and_averages_two_samples(self):
        first, second, third = self.drawn(s.FINELINER_V2)
        self.assertEqual(first, s.Point(0.0, 0.0, 0, 16, 0, 179))
        # speed: the mean of the last two steps (0 and 5); direction: the mean of their angles (0 and 0.927)
        self.assertEqual(second[2:], (10, 16, 19, 179))
        self.assertEqual(third[2:], (20, 16, 38, 179))
        self.assertEqual({p.width for p in self.drawn(s.FINELINER_V2, s.THIN)}, {8})
        self.assertEqual({p.width for p in self.drawn(s.FINELINER_V2, s.THICK)}, {24})

    def test_a_ballpoint_takes_speed_and_direction_from_the_sample_before(self):
        first, second, third = self.drawn(s.BALLPOINT_V2)
        self.assertEqual(first[2:], (0, 12, 0, 179))                 # max(2, 3 * 0.7) * 2 / 1.35 = 3.11
        self.assertEqual(second[2:], (20, 12, 38, 179))
        self.assertEqual(third[2:], (20, 12, 38, 179))
        self.assertEqual({p.width for p in self.drawn(s.BALLPOINT_V2, s.THIN)}, {8})        # 2.1, not thinned
        pen = s.StrokeBuilder(s.BALLPOINT_V2, 0, s.THIN)
        for pressure in (0.2, 0.9, 1.0):
            pen.add(0.0, 0.0, pressure)
        self.assertEqual([(p.width, p.pressure) for p in pen.points], [(8, 51), (11, 230), (12, 255)])

    def test_the_scale_of_the_view_only_changes_speed(self):
        plain, zoomed = self.drawn(s.BALLPOINT_V2), self.drawn(s.BALLPOINT_V2, scale=2.0)
        self.assertEqual([p.speed for p in zoomed], [0, 40, 40])
        self.assertEqual([p._replace(speed=0) for p in zoomed], [p._replace(speed=0) for p in plain])

    def test_the_other_pens(self):
        self.assertEqual({p.width for p in self.drawn(s.MARKER_V2, s.THIN)}, {10})           # 2.5 * size
        self.assertEqual({p.width for p in self.drawn(s.HIGHLIGHTER_V2, s.THIN)}, {120})     # 30 * size
        self.assertEqual({p.width for p in self.drawn(s.SHADING_MARKER)}, {88})              # 10 * size + 2
        sharp = self.drawn(s.SHARP_PENCIL_V2)
        self.assertEqual({(p.width, p.pressure) for p in sharp}, {(24, 238)})                # 4 * 2 / 1.35; eased pressure
        pencil = self.drawn(s.PENCIL_V2)
        self.assertEqual([p.width for p in pencil], [22, 22, 22])                            # (pressure + 2) * size
        self.assertEqual([p.pressure for p in pencil], [179, 179, 179])
        brush = self.drawn(s.PAINTBRUSH_V2)
        self.assertEqual(brush[0][2:], (0, 16, 0, 179))                                      # 0.7 ** 4.5 * 4.5 < 2
        self.assertEqual(brush[2].pressure, 170)                                             # faster, so lighter
        nib = self.drawn(s.CALLIGRAPHY)
        self.assertEqual(nib[0].width, 12)                           # across the stroke, the nib is thin
        self.assertEqual([p.width for p in self.drawn(s.CALLIGRAPHY, path=[(0.0, 0.0), (8.0, 9.5), (16.0, 19.0)])],
                         [12, 17, 21])                               # along 50 degrees it is broad, and eases in
        self.assertEqual([p.width for p in self.drawn(s.CALLIGRAPHY, path=[(0.0, 0.0), (8.0, -9.5), (16.0, -19.0)])],
                         [12, 11, 10])                               # across that, thin
        self.assertEqual([p.width for p in self.drawn(s.CALLIGRAPHY, path=[(0.0, 0.0), (1.74, -9.85), (3.48, -19.7)],
                                                      right_handed=False)], [8, 14, 19])    # the nib at 170 degrees: broad along 280

    def test_a_tilted_pen_broadens_marker_and_pencil(self):
        def with_tilt(tool):
            pen = s.StrokeBuilder(tool, 0, s.THIN)
            pen.add(0.0, 0.0, 0.5, 0.9, 0.0)
            return pen.points[0].width
        self.assertEqual(with_tilt(s.MARKER_V2), 34)                 # (0.9 ** 2 * 0.75 * 10 + 2.5) * 4
        self.assertEqual(with_tilt(s.PENCIL_V2), 43)                 # ((0.9 - 0.524) / 0.436) ** 4 * 15 + 0.5 + 2

    def test_tools_that_do_not_draw(self):
        for tool in (s.PENCIL, s.BALLPOINT, s.MARKER, s.CLEAR_PAGE, 24, -1):
            with self.assertRaises(s.StrokeError):
                s.StrokeBuilder(tool)
        self.assertIsNone(s.draw([], s.FINELINER_V2))

    def test_an_eraser_line_is_ten_units_wide_for_each_unit_of_size(self):
        self.assertEqual({p.width for p in upright().unpacked()}, {80})
        self.assertEqual({p.width for p in upright(thickness=s.THICK).unpacked()}, {120})
        self.assertEqual((upright().tool, upright().count), (s.ERASER, 3))

    def test_a_first_point_without_width_takes_the_third_points_numbers(self):
        pen = s.StrokeBuilder(s.FINELINER_V2)
        for n, point in enumerate([s.Point(0.0, 0.0, 0, 0, 0, 0), s.Point(1.0, 0.0, 4, 16, 1, 9),
                                   s.Point(2.0, 0.0, 8, 12, 2, 7)]):
            pen._append(point)
            self.assertEqual(pen.points[0].width, 0 if n < 2 else 12)
        self.assertEqual(pen.points, [s.Point(0.0, 0.0, 8, 12, 2, 7), s.Point(1.0, 0.0, 8, 12, 2, 7),
                                      s.Point(2.0, 0.0, 8, 12, 2, 7)])


class SimplifyTests(unittest.TestCase):
    def points(self, ys, **same) -> list[s.Point]:
        base = dict(speed=8, width=16, direction=0, pressure=179) | same
        return [s.Point(float(10 * n), float(y), **base) for n, y in enumerate(ys)]

    def test_points_on_the_way_are_dropped(self):
        straight = self.points([0, 0, 0, 0, 0])
        self.assertEqual(s.simplify_points(s.FINELINER_V2, straight), [straight[0], straight[-1]])
        self.assertEqual(s.simplify_points(s.FINELINER_V2, straight[:1]), straight[:1])

    def test_a_point_more_than_a_fifth_off_the_line_is_kept(self):
        near, far = self.points([0, 0.19, 0]), self.points([0, 0.21, 0])
        self.assertEqual(len(s.simplify_points(s.FINELINER_V2, near)), 2)
        self.assertEqual(s.simplify_points(s.FINELINER_V2, far), far)
        bends = self.points([0, 5, 0, 5, 0, 5, 0])
        self.assertEqual(s.simplify_points(s.FINELINER_V2, bends), bends)

    def test_a_change_of_width_keeps_the_farthest_point_of_the_stretch(self):
        line = self.points([0, 0, 0, 0])
        line[2] = line[2]._replace(width=19)                         # 0.75 wider than the first: more than 2/3
        kept = s.simplify_points(s.FINELINER_V2, line)
        self.assertEqual(kept, [line[0], line[1], line[2], line[3]])
        line[2] = line[2]._replace(width=18)                         # 0.5 wider: not enough
        self.assertEqual(len(s.simplify_points(s.FINELINER_V2, line)), 2)

    def test_pressure_and_speed_count_only_for_pens_that_vary_in_them(self):
        pressed = self.points([0, 0, 0])
        pressed[1] = pressed[1]._replace(pressure=210)               # 0.12 more
        self.assertEqual(len(s.simplify_points(s.FINELINER_V2, pressed)), 2)
        self.assertEqual(len(s.simplify_points(s.BALLPOINT_V2, pressed)), 3)
        self.assertEqual(len(s.simplify_points(s.CALLIGRAPHY, pressed)), 3)
        fast = self.points([0, 0, 0])
        fast[1] = fast[1]._replace(speed=17)                         # 2.25 faster
        self.assertEqual(len(s.simplify_points(s.CALLIGRAPHY, fast)), 2)
        self.assertEqual(len(s.simplify_points(s.BALLPOINT_V2, fast)), 3)

    def test_a_drawn_line_is_thinned_before_it_is_kept(self):
        samples = [(float(n), 20.0) for n in range(50)]
        pen = s.StrokeBuilder(s.FINELINER_V2)
        for sample in samples:
            pen.add(*sample)
        self.assertEqual(pen.line().count, 50)
        line = s.draw(samples, s.FINELINER_V2)
        self.assertEqual([(p.x, p.speed) for p in line.unpacked()], [(0.0, 0), (49.0, 4)])       # straight: its two ends
        self.assertEqual(s.simplify(line), line)                     # nothing more to take

    def test_a_line_that_stays_too_long_is_not_added(self):
        zigzag = s.new_line(17, 0, 2.0, self.points([0, 9] * 6))
        with mock.patch.object(s, "MAX_LINE_POINTS", 12):
            self.assertIsNone(s.simplify(zigzag))
        with mock.patch.object(s, "MAX_LINE_POINTS", 13):
            self.assertEqual(s.simplify(zigzag), zigzag)
        gentle = s.new_line(17, 0, 2.0, self.points([0, 0.5] * 6))  # within 1 of the line: the second pass takes them
        with mock.patch.object(s, "MAX_LINE_POINTS", 12):
            self.assertEqual(s.simplify(gentle).count, 2)


class AppendTests(unittest.TestCase):
    def test_the_first_stroke_of_a_page_without_a_file(self):
        line = ruled()
        data = s.append_stroke(None, author=DESKTOP, line=line)
        header, blocks = w.split(data)
        self.assertEqual(w.join(header, blocks[:-1]), w.blank_page(DESKTOP))
        last = blocks[-1]
        self.assertEqual((last.type, last.min_version, last.version), (0x05, 2, 2))
        self.assertEqual(last.payload, f_id(1, LAYER) + f_id(2, (1, 15)) + f_id(3, ZERO) + f_id(4, ZERO)
                         + bytes.fromhex("5400000000") + f_sub(6, line.encode()))
        [found] = parse(data).strokes
        self.assertEqual((found.tool, found.color, found.thickness_scale, found.starting_length, found.timestamp,
                          found.move_id, len(found.points)), (Tool.FINELINER_V2, Color.BLACK, 2.0, 0.0, (0, 1), None, 2))

    def test_a_stroke_goes_on_top_of_the_layer_under_the_next_id(self):
        before = chain(3)
        data = s.append_stroke(before, author=DESKTOP, line=ruled())
        old, new = w.split(before)[1], w.split(data)[1]
        self.assertEqual([b.encode() for b in new[1:-1]], [b.encode() for b in old[1:]])
        self.assertEqual(w.decode_authors(new[0].payload), [(TABLET, 1), (DESKTOP, 2)])
        self.assertEqual(items(data)[-1], (0x05, (2, 18), (1, 17), ZERO, 0, True))
        again = s.append_stroke(data, author=DESKTOP, line=ruled(y=140.0))
        self.assertEqual(items(again)[-1], (0x05, (2, 19), (2, 18), ZERO, 0, True))
        self.assertEqual([i for i, _ in w.sequence(w.split(again)[1], LAYER)], [(1, 15), (1, 16), (1, 17), (2, 18), (2, 19)])

    def test_several_strokes_at_once_are_as_one_after_the_other(self):
        lines = [ruled(), ruled(y=140.0), ruled(y=180.0, tool=s.BALLPOINT_V2)]
        one_by_one = chain(2)
        for line in lines:
            one_by_one = s.append_stroke(one_by_one, author=DESKTOP, line=line)
        together, ids = s.append_strokes(chain(2), author=DESKTOP, lines=lines)
        self.assertEqual((together, ids), (one_by_one, [(2, 17), (2, 18), (2, 19)]))

    def test_the_stroke_follows_deleted_items_too(self):
        data = s.append_stroke(page(stroke((1, 15)), gone((1, 16), (1, 15), length=3)), author=TABLET, line=ruled())
        self.assertEqual(items(data)[-1], (0x05, (1, 19), (1, 18), ZERO, 0, True))

    def test_the_colour_is_stored_only_for_argb(self):
        yellow = ruled(tool=s.HIGHLIGHTER_V2, color=9, thickness=1.0, argb=0xFFFFED75)
        [found] = parse(s.append_stroke(None, author=DESKTOP, line=yellow)).strokes
        self.assertEqual((found.tool, found.color, found.argb), (Tool.HIGHLIGHTER_V2, Color.ARGB, 0xFFFFED75))
        stray = dataclasses.replace(ruled(), argb=0xFF123456)
        [found] = parse(s.append_stroke(None, author=DESKTOP, line=stray)).strokes
        self.assertIsNone(found.argb)

    def test_what_the_app_would_not_add_is_refused(self):
        with self.assertRaises(s.StrokeError):
            s.append_stroke(None, author=DESKTOP, line=s.new_line(17, 0, 2.0, b""))
        for tool in s.NOT_DRAWN:
            with self.assertRaises(s.StrokeError):
                s.append_stroke(None, author=DESKTOP, line=s.new_line(tool, 0, 2.0, DOT))
        with self.assertRaises(s.StrokeError):
            s.append_stroke(chain(1), author=DESKTOP, line=ruled(), layer=(1, 15))
        line = ruled()
        with mock.patch.object(s, "MAX_LINE_POINTS", 2), self.assertRaises(s.StrokeError):
            s.append_stroke(None, author=DESKTOP, line=line)

    def test_a_page_with_typed_text_is_drawn_and_erased_on_its_layer(self):
        from viewer import rm_text
        empty_box = page(stroke((1, 15)), extra=root_text([], []))
        typed = rm_text.edit_text(page(stroke((1, 15))), author=DESKTOP,
                                  change=lambda doc, clock: doc.insert(0, "meeting notes", clock))
        for data in (empty_box, typed):
            self.assertTrue(s.has_text(w.split(data)[1]))
            text_block = [b.payload for b in w.split(data)[1] if b.type == w.BLOCK_ROOT_TEXT]
            drawn = s.append_stroke(data, author=DESKTOP, line=ruled())
            [new] = [x for x in s.strokes(drawn) if x.item not in {y.item for y in s.strokes(data)}]
            # On the layer itself, unanchored, after the layer's last item.
            self.assertEqual((new.parent, new.left), (LAYER, w.sequence(w.split(data)[1], LAYER)[-1][0]))
            self.assertEqual([b.payload for b in w.split(drawn)[1] if b.type == w.BLOCK_ROOT_TEXT], text_block)
            erased = s.erase(drawn, author=DESKTOP, eraser=upright())
            self.assertIn(new.item, erased.deleted)
            self.assertEqual([b.payload for b in w.split(erased.page)[1] if b.type == w.BLOCK_ROOT_TEXT], text_block)
            self.assertEqual(parse(erased.page).text.text if parse(erased.page).text else "",
                             parse(data).text.text if parse(data).text else "")
        # Undo of the erasure puts the stroke back on the text page too.
        restored, _ = s.restore_stroke(erased.page, author=DESKTOP, item=new.item, line=new.line)
        self.assertIn(new.line.points, [x.line.points for x in s.strokes(restored)])

    def test_strokes_and_find(self):
        line = ruled()
        data = s.append_stroke(chain(2), author=DESKTOP, line=line)
        self.assertEqual([(x.item, x.left, x.right) for x in s.strokes(data)],
                         [((1, 15), ZERO, ZERO), ((1, 16), (1, 15), ZERO), ((2, 17), (1, 16), ZERO)])
        self.assertEqual(s.strokes(data)[-1].line, line)
        self.assertEqual((s.find_strokes(data, line), s.find_strokes(chain(2), line), s.find_strokes(None, line)),
                         ([(2, 17)], [], []))
        self.assertEqual(s.strokes(None), [])


class DeleteTests(unittest.TestCase):
    def test_a_deleted_stroke_keeps_its_block_without_its_value(self):
        before = chain(3)
        data = s.delete_strokes(before, [(1, 16)])
        self.assertEqual(items(data), [(0x04, (0, 13), ZERO, ZERO, 0, True) for _ in ()] + [
            (0x05, (1, 15), ZERO, ZERO, 0, True), (0x05, (1, 16), (1, 15), ZERO, 1, False),
            (0x05, (1, 17), (1, 16), ZERO, 0, True)])
        old, new = w.split(before)[1], w.split(data)[1]
        self.assertEqual([b.encode() for n, b in enumerate(new) if n != len(new) - 2],
                         [b.encode() for n, b in enumerate(old) if n != len(old) - 2])
        self.assertEqual(new[-2].encode().hex(), "11000000" "00020205" "1f000b" "2f0110" "3f010f" "4f0000" "5401000000")
        self.assertEqual([x.item_id for x in parse(data).strokes], [(1, 15), (1, 17)])

    def test_deleted_neighbours_become_one_run_whatever_the_order(self):
        want = [(0x05, (1, 15), ZERO, ZERO, 0, True), (0x05, (1, 16), (1, 15), ZERO, 3, False),
                (0x05, (1, 19), (1, 18), ZERO, 0, True)]
        for order in ([16, 17, 18], [18, 17, 16], [17, 16, 18], [16, 18, 17]):
            self.assertEqual(items(s.delete_strokes(chain(5), [(1, n) for n in order])), want, order)
        one_by_one = chain(5)
        for n in (17, 16, 18):
            one_by_one = s.delete_strokes(one_by_one, [(1, n)])
        self.assertEqual(items(one_by_one), want)
        self.assertEqual(items(s.delete_strokes(chain(3), [(1, 15), (1, 16), (1, 17)])),
                         [(0x05, (1, 15), ZERO, ZERO, 3, False)])

    def test_the_earlier_of_two_keeps_its_place_and_its_kind(self):
        after_tombstone = page(gone((1, 15)), stroke((1, 16), (1, 15)), stroke((1, 17), (1, 16)))
        self.assertEqual(items(s.delete_strokes(after_tombstone, [(1, 16)])),
                         [(0x08, (1, 15), ZERO, ZERO, 2, False), (0x05, (1, 17), (1, 16), ZERO, 0, True)])
        before_tombstone = page(stroke((1, 15)), gone((1, 16), (1, 15), length=2), stroke((1, 18), (1, 17)))
        self.assertEqual(items(s.delete_strokes(before_tombstone, [(1, 15)])),
                         [(0x05, (1, 15), ZERO, ZERO, 3, False), (0x05, (1, 18), (1, 17), ZERO, 0, True)])

    def test_the_four_conditions_of_a_join(self):
        a = w.ItemHeader(LAYER, (1, 20), (1, 19), ZERO, 2, None)
        b = w.ItemHeader(LAYER, (1, 22), (1, 21), ZERO, 1, None)
        self.assertTrue(s.can_join(a, b))
        for other in (dataclasses.replace(b, left=(1, 20)),                  # not put after a's last id
                      dataclasses.replace(b, item=(2, 22)),                  # another author
                      dataclasses.replace(b, item=(1, 23)),                  # not the very next id
                      dataclasses.replace(b, right=(1, 9)),                  # another right neighbour
                      dataclasses.replace(b, deleted_length=0, value=b"\x03")):      # not deleted
            self.assertFalse(s.can_join(a, other), other)
        self.assertFalse(s.can_join(b, a))
        self.assertFalse(s.can_join(dataclasses.replace(a, left=(1, 30)), b))       # an id older than its neighbour

    def test_what_stands_between_two_deleted_items_keeps_them_apart(self):
        # 2:17 was put between 1:15 and 1:16 by another author.
        data = page(stroke((1, 15)), stroke((1, 16), (1, 15)), stroke((2, 17), (1, 15), (1, 16)), author=TABLET)
        self.assertEqual([i for i, _ in w.sequence(w.split(data)[1], LAYER)], [(1, 15), (2, 17), (1, 16)])
        self.assertEqual(items(s.delete_strokes(data, [(1, 15), (1, 16)])),
                         [(0x05, (1, 15), ZERO, ZERO, 1, False), (0x05, (1, 16), (1, 15), ZERO, 1, False),
                          (0x05, (2, 17), (1, 15), (1, 16), 0, True)])
        other_author = page(stroke((1, 15)), stroke((2, 16), (1, 15)))
        self.assertEqual([i[4] for i in items(s.delete_strokes(other_author, [(1, 15), (2, 16)]))], [1, 1])

    def test_what_is_not_a_live_stroke_is_passed_over(self):
        data = page(stroke((1, 15)), gone((1, 16), (1, 15)))
        self.assertIs(s.delete_strokes(data, [(1, 16), (1, 99), (0, 13)]), data)
        self.assertEqual(s.delete_strokes(data, [(1, 99), (1, 15)]), s.delete_strokes(data, [(1, 15)]))

    def test_strokes_of_several_nodes(self):
        inner = (1, 30)
        data = page(stroke((1, 15)), stroke((1, 16), (1, 15)), stroke((1, 40), parent=inner),
                    stroke((1, 41), (1, 40), parent=inner))
        done = s.delete_strokes(data, [(1, 41), (1, 16), (1, 40)])
        self.assertEqual([i[1:5] for i in items(done)], [((1, 15), ZERO, ZERO, 0), ((1, 16), (1, 15), ZERO, 1)])
        self.assertEqual([i[1:5] for i in items(done, inner)], [((1, 40), ZERO, ZERO, 2)])


class OutlineTests(unittest.TestCase):
    SQUARE = s.Outline(((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)))

    def test_inside_is_where_the_winding_is_not_zero(self):
        for point, inside in (((5.0, 5.0), True), ((0.5, 9.5), True), ((-1.0, 5.0), False), ((5.0, 11.0), False),
                              ((10.5, 5.0), False), ((0.0, 5.0), False), ((5.0, 0.0), True), ((5.0, 10.0), False)):
            self.assertEqual(self.SQUARE.contains(*point), inside, point)
        self.assertEqual(self.SQUARE.bounds(), (0.0, 0.0, 10.0, 10.0))
        self.assertFalse(s.Outline(()).edges)

    def test_where_two_stretches_cross(self):
        self.assertEqual(s._crossing((0.0, 0.0), (10.0, 0.0), (2.5, -1.0), (2.5, 1.0)), 0.25)
        self.assertIsNone(s._crossing((0.0, 0.0), (10.0, 0.0), (2.5, 1.0), (2.5, 3.0)))        # short of it
        self.assertIsNone(s._crossing((0.0, 0.0), (10.0, 0.0), (0.0, 1.0), (10.0, 1.0)))       # alongside
        self.assertIsNone(s._crossing((0.0, 0.0), (1.0, 0.0), (2.5, -1.0), (2.5, 1.0)))

    def test_the_erasers_outline_is_its_line_as_wide_as_its_first_point(self):
        outline = s.eraser_outline(upright())
        self.assertEqual(outline.box, (-9.75, 50.0, 10.25, 150.0))           # 20 wide, round ends
        self.assertEqual(outline.points[0], outline.points[-1])
        self.assertTrue(outline.contains(0.0, 100.0) and outline.contains(9.0, 61.0) and outline.contains(0.0, 51.0))
        self.assertFalse(outline.contains(11.0, 100.0) or outline.contains(9.0, 51.0))
        self.assertEqual(s.eraser_outline(s.new_line(s.ERASER, 2, 2.0, b"")).points, ())

    def test_an_erase_areas_line_is_the_outline_itself(self):
        lasso = s.new_line(s.ERASE_SECTION, 0, 2.0, [s.Point(x, y, 0, 16, 0, 0) for x, y in
                                                      ((0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0))])
        outline = s.eraser_outline(lasso)
        self.assertEqual(outline.points, ((0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0), (0.0, 0.0)))
        self.assertTrue(outline.contains(20.0, 15.0))

    def test_the_bounds_of_a_line_reach_as_far_as_its_tool(self):
        self.assertEqual(s.line_bounds(s.new_line(17, 0, 2.0, DOT)), (-4.0, -4.0, 9.0, 9.0))     # (3 * 2 + 2) / 2
        self.assertEqual(s.line_bounds(s.new_line(18, 0, 1.0, DOT)), (-16.0, -16.0, 21.0, 21.0))
        self.assertIsNone(s.line_bounds(s.new_line(17, 0, 2.0, b"")))


class EraseLineTests(unittest.TestCase):
    """A level fineliner stroke (4 wide) crossed by an upright eraser (x -9.75 to 10.25)."""

    def setUp(self):
        self.line = s.new_line(17, 0, 2.0, [s.Point(float(x), 100.0, 4, 16, 0, 179) for x in range(-50, 51, 20)])
        self.outline = s.eraser_outline(upright())

    def test_a_stroke_the_eraser_does_not_reach_is_untouched(self):
        self.assertIsNone(s.erase_line(self.outline, self.line, dy=200.0))
        self.assertIsNone(s.erase_line(s.eraser_outline(upright(x=300.0)), self.line))
        # Its box overlaps the eraser's, but no part of it is inside.
        beside = s.new_line(17, 0, 2.0, [s.Point(11.5, 60.0, 4, 16, 0, 179), s.Point(11.5, 140.0, 4, 16, 0, 179)])
        self.assertIsNone(s.erase_line(self.outline, beside))

    def test_what_is_left_are_the_points_between_the_cuts(self):
        left, right = s.erase_line(self.outline, self.line)
        # The eraser's edge is met squarely: the pieces end 0.7 * 4 / 2 = 1.4 short of it.
        self.assertEqual([(round(p.x, 3), p.y) for p in left.unpacked()], [(-50.0, 100.0), (-30.0, 100.0), (-11.15, 100.0)])
        self.assertEqual([(round(p.x, 3), p.y) for p in right.unpacked()], [(11.65, 100.0), (30.0, 100.0), (50.0, 100.0)])
        self.assertEqual(left.unpacked()[:2], self.line.unpacked()[:2])
        self.assertEqual(right.unpacked()[1:], self.line.unpacked()[4:])
        self.assertEqual((left.starting_length, round(right.starting_length, 3)), (0.0, 61.65))
        for piece in (left, right):
            self.assertEqual((piece.tool, piece.color, piece.thickness_scale, piece.timestamp, piece.move_id,
                              piece.source_id), (17, 0, 2.0, (0, 1), None, None))

    def test_a_cut_end_takes_its_numbers_from_both_neighbours(self):
        points = self.line.unpacked()
        points[2] = points[2]._replace(speed=40, width=32, direction=100, pressure=255)
        left, _ = s.erase_line(self.outline, dataclasses.replace(self.line, points=s.pack_points(points)))
        end = left.unpacked()[-1]
        # Point 2 is 8 wide, so the margin is 0.7 * 8 / 2 = 2.8: the end lies 0.873 of the way from point 1 to it.
        part = (end.x + 30.0) / 20.0
        self.assertAlmostEqual(part, 0.873, 2)
        self.assertEqual(end[2:], (round(4 + 36 * part), round(16 + 16 * part), round(100 * part), round(179 + 76 * part)))

    def test_a_piece_starts_where_the_strokes_own_length_says(self):
        later = dataclasses.replace(self.line, starting_length=40.0)
        left, right = s.erase_line(self.outline, later)
        self.assertEqual((left.starting_length, round(right.starting_length, 3)), (40.0, 101.65))

    def test_a_stroke_inside_the_eraser_leaves_nothing(self):
        inside = s.new_line(17, 0, 2.0, [s.Point(-3.0, 90.0, 4, 16, 0, 179), s.Point(4.0, 110.0, 4, 16, 0, 179)])
        self.assertEqual(s.erase_line(self.outline, inside), [])

    def test_an_end_shorter_than_the_margin_goes_too(self):
        stub = s.new_line(17, 0, 2.0, [s.Point(-10.9, 100.0, 4, 16, 0, 179), s.Point(5.0, 100.0, 4, 16, 0, 179)])
        self.assertEqual(s.erase_line(self.outline, stub), [])           # 1.15 sticks out, the margin is 1.4
        longer = s.new_line(17, 0, 2.0, [s.Point(-14.0, 100.0, 4, 16, 0, 179), s.Point(5.0, 100.0, 4, 16, 0, 179)])
        [piece] = s.erase_line(self.outline, longer)
        self.assertEqual([round(p.x, 3) for p in piece.unpacked()], [-14.0, -11.15])

    def test_a_highlighter_met_squarely_is_cut_at_the_erasers_edge(self):
        marker = dataclasses.replace(self.line, tool=18)
        left, right = s.erase_line(self.outline, marker)
        self.assertEqual((round(left.unpacked()[-1].x, 3), round(right.unpacked()[0].x, 3)), (-9.75, 10.25))

    def test_a_slanting_stroke_loses_more(self):
        slanted = s.new_line(17, 0, 2.0, [s.Point(-50.0, 60.0, 4, 16, 0, 179), s.Point(50.0, 140.0, 4, 16, 0, 179)])
        left, right = s.erase_line(self.outline, slanted)
        # cos of the angle to the edge is 0.625: margin (0.375 * 0.7 + 0.625 * 2) * 4 / 2 = 3.02 along the stroke
        gap = math.hypot(left.unpacked()[-1].x + 9.75, left.unpacked()[-1].y - (60.0 + 0.8 * 40.25))
        self.assertAlmostEqual(gap, 3.025, 2)
        self.assertEqual(len(right.unpacked()), 2)

    def test_the_eraser_drawn_at_an_offset(self):
        moved = s.erase_line(self.outline, self.line, dx=5.0)
        self.assertEqual([round(p.x, 3) for p in moved[0].unpacked()], [-50.0, -30.0, -16.15])


class EraseTests(unittest.TestCase):
    def drawn(self) -> bytes:
        """Three level strokes of the desktop at y 80, 100 and 120, then a short one far off."""
        data = None
        for y in (80.0, 100.0, 120.0):
            data = s.append_stroke(data, author=DESKTOP, line=ruled(y=y))
        return s.append_stroke(data, author=DESKTOP, line=ruled(y=400.0, x0=300.0, x1=340.0))

    def test_an_eraser_that_meets_nothing_changes_nothing(self):
        data = self.drawn()
        erased = s.erase(data, author=DESKTOP, eraser=upright(x=250.0))
        self.assertIs(erased.page, data)
        self.assertEqual((erased.deleted, erased.pieces, erased.made), ((), (), ()))

    def test_each_stroke_is_deleted_and_its_pieces_put_right_after_it(self):
        data = self.drawn()
        erased = s.erase(data, author=DESKTOP, eraser=upright(y0=92.0, y1=108.0))      # round ends: y 82 to 118
        self.assertEqual((erased.deleted, erased.pieces), (((1, 16),), (((1, 19), (1, 20)),)))
        self.assertEqual(items(erased.page), [
            (0x05, (1, 15), ZERO, ZERO, 0, True), (0x05, (1, 16), (1, 15), ZERO, 1, False),
            (0x05, (1, 17), (1, 16), ZERO, 0, True), (0x05, (1, 18), (1, 17), ZERO, 0, True),
            (0x05, (1, 19), (1, 16), (1, 17), 0, True), (0x05, (1, 20), (1, 19), (1, 17), 0, True)])
        self.assertEqual([i for i, _ in w.sequence(w.split(erased.page)[1], LAYER)],
                         [(1, 15), (1, 16), (1, 19), (1, 20), (1, 17), (1, 18)])
        old = {b.encode() for b in w.split(data)[1]}
        self.assertEqual(len([b for b in w.split(erased.page)[1] if b.encode() not in old]), 3)
        left, right = [x.line for x in s.strokes(erased.page) if x.item in erased.made]
        self.assertEqual((round(left.unpacked()[-1].x, 2), round(right.unpacked()[0].x, 2)), (-11.15, 11.65))
        self.assertEqual((left.starting_length, round(right.starting_length, 2)), (0.0, 112.15))

    def test_neighbours_erased_together_are_one_run_cut_where_pieces_go_in(self):
        erased = s.erase(self.drawn(), author=DESKTOP, eraser=upright())
        self.assertEqual(erased.deleted, ((1, 15), (1, 16), (1, 17)))
        self.assertEqual(erased.pieces, (((1, 19), (1, 20)), ((1, 21), (1, 22)), ((1, 23), (1, 24))))
        self.assertEqual([i[1:5] for i in items(erased.page)], [
            ((1, 15), ZERO, ZERO, 1), ((1, 16), (1, 15), ZERO, 1), ((1, 17), (1, 16), ZERO, 1),
            ((1, 18), (1, 17), ZERO, 0),
            ((1, 19), (1, 15), (1, 16), 0), ((1, 20), (1, 19), (1, 16), 0),
            ((1, 21), (1, 16), (1, 17), 0), ((1, 22), (1, 21), (1, 17), 0),
            ((1, 23), (1, 17), (1, 18), 0), ((1, 24), (1, 23), (1, 18), 0)])
        self.assertEqual(len(parse(erased.page).strokes), 7)

    def test_strokes_that_leave_nothing_become_one_run(self):
        data = None
        for y in (90.0, 100.0, 110.0):
            data = s.append_stroke(data, author=DESKTOP, line=ruled(y=y, x0=-4.0, x1=4.5, step=2.0))
        data = s.append_stroke(data, author=DESKTOP, line=ruled(y=400.0))
        erased = s.erase(data, author=TABLET, eraser=upright())
        self.assertEqual((erased.deleted, erased.made), (((1, 15), (1, 16), (1, 17)), ()))
        self.assertEqual([i[1:5] for i in items(erased.page)], [((1, 15), ZERO, ZERO, 3), ((1, 18), (1, 17), ZERO, 0)])
        self.assertEqual(w.split(erased.page)[1][0].payload, w.split(data)[1][0].payload)      # no new author: no new id

    def test_another_author_erasing_takes_the_next_index(self):
        erased = s.erase(self.drawn(), author=TABLET, eraser=upright(y0=92.0, y1=108.0))
        self.assertEqual(erased.pieces, (((2, 19), (2, 20)),))
        self.assertEqual(w.decode_authors(w.split(erased.page)[1][0].payload), [(DESKTOP, 1), (TABLET, 2)])

    def test_a_shaders_pieces_name_the_stroke_they_come_from(self):
        data = s.append_stroke(None, author=DESKTOP, line=ruled(tool=s.SHADING_MARKER, color=9, argb=0x4020F21C))
        erased = s.erase(data, author=DESKTOP, eraser=upright())
        pieces = [x.line for x in s.strokes(erased.page)]
        self.assertEqual([(p.source_id, p.argb, p.move_id) for p in pieces], [((1, 15), 0x4020F21C, None)] * 2)
        again = s.erase(erased.page, author=DESKTOP, eraser=upright(x=50.0))
        self.assertEqual({x.line.source_id for x in s.strokes(again.page)}, {(1, 15)})

    def test_an_erase_area_takes_what_is_inside_its_outline(self):
        lasso = s.new_line(s.ERASE_SECTION, 0, 2.0, [s.Point(x, y, 0, 16, 0, 0) for x, y in
                                                      ((-20.3, 90.0), (20.3, 90.0), (20.3, 110.0), (-20.3, 110.0))])
        erased = s.erase(self.drawn(), author=DESKTOP, eraser=lasso)
        self.assertEqual(erased.deleted, ((1, 16),))
        left, right = [x.line.unpacked() for x in s.strokes(erased.page) if x.item in erased.made]
        self.assertEqual((round(left[-1].x, 2), round(right[0].x, 2)), (-21.7, 21.7))

    def test_only_the_layer_named_and_only_erasers(self):
        with self.assertRaises(s.StrokeError):
            s.erase(self.drawn(), author=DESKTOP, eraser=ruled())
        with self.assertRaises(s.StrokeError):
            s.erase(self.drawn(), author=DESKTOP, eraser=upright(), layer=(1, 15))
        with mock.patch.object(s, "MAX_STORED_POINTS", 3), self.assertRaises(s.StrokeError):
            s.erase(self.drawn(), author=DESKTOP, eraser=upright())


class StrokeEditTests(Base):
    def line(self, y: float = 100.0, **pen) -> s.LineValue:
        return ruled(y=y, **pen)

    def files(self, edits):
        return document_files(self.doc, edits, lambda name: self.source.blob(self.doc, name), lambda e: b"",
                              self.sync.author())

    def test_edits_are_records_that_survive_a_restart(self):
        add = StrokeAdd.new(THROWAWAY, PAGE1, self.line(tool=s.HIGHLIGHTER_V2, color=9, thickness=1.0, argb=0xFFFFED75), NOW)
        erase = StrokeErase.new(THROWAWAY, PAGE1, upright(), NOW + 1)
        delete = StrokeDelete.new(THROWAWAY, PAGE1, [("page", 1, 15), ("edit", add.uid), ("edit", erase.uid + "/0")], NOW + 2)
        for edit in (add, erase, delete):
            self.assertEqual(type(edit).load(json.loads(json.dumps(dataclasses.asdict(edit)))), edit)
            self.sync.edits.add(edit)
        self.assertEqual(PendingEdits(self.dir / "cache" / "pending").all(), [add, erase, delete])
        self.assertEqual(add.line(), self.line(tool=s.HIGHLIGHTER_V2, color=9, thickness=1.0, argb=0xFFFFED75))
        self.assertEqual((erase.line().tool, erase.line().points), (s.ERASER, upright().points))

    def test_records_are_checked(self):
        for bad in (lambda: StrokeAdd.new(THROWAWAY, PAGE1, upright(), NOW),
                    lambda: StrokeErase.new(THROWAWAY, PAGE1, self.line(), NOW),
                    lambda: StrokeDelete.new(THROWAWAY, PAGE1, [], NOW),
                    lambda: StrokeDelete.new(THROWAWAY, PAGE1, [("page", 1)], NOW),
                    lambda: StrokeAdd.load({"doc_id": "d", "page_id": "p", "tool": 17, "color": 0, "thickness": 2,
                                            "points": "00" * 15, "created_ms": 1}),
                    lambda: StrokeAdd.load({"doc_id": "d", "page_id": "p", "tool": 17, "color": 0,
                                            "thickness": float("nan"), "points": "00" * 14, "created_ms": 1})):
            with self.assertRaises(EditError):
                bad()

    def test_replaying_draws_erases_and_deletes_in_order(self):
        first, second = StrokeAdd.new(THROWAWAY, PAGE2, self.line(80.0), NOW), StrokeAdd.new(THROWAWAY, PAGE2, self.line(), NOW)
        erase = StrokeErase.new(THROWAWAY, PAGE2, upright(y0=92.0, y1=108.0), NOW)
        author = self.sync.author()
        done = replay(None, [first, second, erase], author)
        self.assertEqual(done.made, {first.uid: (1, 15), second.uid: (1, 16), erase.uid + "/0": (1, 17),
                                     erase.uid + "/1": (1, 18)})
        self.assertEqual(done.page, s.erase(s.append_strokes(None, author=author, lines=[first.line(), second.line()])[0],
                                            author=author, eraser=upright(y0=92.0, y1=108.0)).page)
        delete = StrokeDelete.new(THROWAWAY, PAGE2, [("edit", erase.uid + "/1"), ("edit", first.uid), ("page", 9, 9)], NOW)
        self.assertEqual([x.item for x in s.strokes(replay(None, [first, second, erase, delete], author).page)], [(1, 17)])
        # An eraser or a delete on a page without a file does nothing.
        self.assertIsNone(replay(None, [erase, delete], author).page)

    def test_a_stroke_the_page_already_has_is_not_drawn_twice(self):
        author = self.sync.author()
        first, second = StrokeAdd.new(THROWAWAY, PAGE1, self.line(80.0), NOW), StrokeAdd.new(THROWAWAY, PAGE1, self.line(), NOW)
        once = page_with_edits(drawn_page(), [first, second], author)
        again = replay(once, [first, second], author)
        self.assertEqual((again.page, again.made), (once, {first.uid: (2, 16), second.uid: (2, 17)}))
        uploaded = page_with_edits(drawn_page(), [first], author)
        partly = replay(uploaded, [first, second], author)
        # The same page, saved in a new session of the uploaded file: loads one more again.
        self.assertEqual((partly.page, partly.made), (w.saved(once, uploaded), again.made))
        self.assertEqual(w.page_loads(partly.page), w.page_loads(drawn_page()) + 2)

    def test_the_same_line_drawn_twice_is_two_strokes(self):
        author = self.sync.author()
        dots = [StrokeAdd.new(THROWAWAY, PAGE2, s.draw([(5.0, 5.0), (5.0, 5.0)], s.FINELINER_V2), NOW) for _ in range(3)]
        self.assertEqual(dots[0].points, dots[1].points)
        done = replay(None, dots, author)
        self.assertEqual((len(s.strokes(done.page)), sorted(done.made.values())), (3, [(1, 15), (1, 16), (1, 17)]))
        self.assertEqual(replay(done.page, dots, author).page, done.page)
        self.assertEqual(len(s.strokes(replay(replay(None, dots[:2], author).page, dots, author).page)), 3)

    def test_a_stroke_changes_the_page_and_two_time_fields(self):
        files = self.files([StrokeAdd.new(THROWAWAY, PAGE1, self.line(), NOW)])
        rm, content, metadata = f"{THROWAWAY}/{PAGE1}.rm", f"{THROWAWAY}.content", f"{THROWAWAY}.metadata"
        self.assertEqual(sorted(files), sorted([rm, content, metadata]))
        drawn = s.append_stroke(drawn_page(), author=self.sync.author(), line=self.line())
        self.assertEqual(files[rm], w.saved(drawn, drawn_page()))         # + PageStats loads + 1
        self.assertEqual(w.page_loads(files[rm]), w.page_loads(drawn_page()) + 1)
        self.assertEqual(json_changes(json.loads(self.source.blob(self.doc, content)), json.loads(files[content])),
                         [(f"cPages.pages[id={PAGE1}].modifed", None, str(NOW))])
        self.assertEqual(json_changes(self.doc.metadata.raw, json.loads(files[metadata])),
                         [("lastModified", str(THEN), str(NOW))])

    def test_a_page_without_a_file_gets_one(self):
        files = self.files([StrokeAdd.new(THROWAWAY, PAGE2, self.line(), NOW)])
        [found] = parse(files[f"{THROWAWAY}/{PAGE2}.rm"]).strokes
        self.assertEqual((found.tool, found.item_id), (Tool.FINELINER_V2, (1, 15)))

    def test_an_eraser_that_meets_nothing_uploads_nothing(self):
        self.assertEqual(self.files([StrokeErase.new(THROWAWAY, PAGE1, upright(x=600.0), NOW)]), {})
        self.assertEqual(self.files([StrokeDelete.new(THROWAWAY, PAGE1, [("page", 1, 99)], NOW)]), {})

    def test_erasing_the_tablets_stroke(self):
        # The tablet's stroke runs from (0, 0) to (50, 50).
        files = self.files([StrokeErase.new(THROWAWAY, PAGE1, s.eraser_line([(25.3, 10.0), (25.3, 45.0)], s.THIN), NOW)])
        self.assertEqual([i[1:5] for i in items(files[f"{THROWAWAY}/{PAGE1}.rm"])],
                         [((1, 15), ZERO, ZERO, 1), ((2, 16), (1, 15), ZERO, 0), ((2, 17), (2, 16), ZERO, 0)])

    def test_the_library_keeps_strokes_of_the_editable_notebook_only(self):
        edit = self.source.add_stroke(self.doc, PAGE1, self.line(), NOW)
        erase = self.source.erase_strokes(self.doc, PAGE1, upright(), NOW + 1)
        delete = self.source.delete_strokes(self.doc, PAGE1, [("page", 1, 15)], NOW + 2)
        self.assertEqual(self.sync.edits.of(THROWAWAY), [edit, erase, delete])
        scene = self.source.staged(self.tree.get(THROWAWAY)).page(PAGE1)
        self.assertEqual(len(parse(scene).strokes), 2)                   # the tablet's deleted, ours in two pieces
        with self.assertRaises(EditError):
            self.source.add_stroke(self.tree.get(OTHER), PAGE1, self.line(), NOW)
        with self.assertRaises(EditError):
            self.source.add_stroke(self.doc, "aaaaaaaa-0000-4000-8000-00000000dead", self.line(), NOW)
        self.assertEqual(len(self.sync.edits.all()), 3)

    def test_a_page_with_typed_text_takes_strokes_and_erasers(self):
        from core.edits import TextEdit
        from viewer import rm_text
        # The page as the tablet left it, with typed text added (pending text edits).
        create = TextEdit.new(THROWAWAY, PAGE1, "create", NOW, paragraph=0)
        typed = TextEdit.new(THROWAWAY, PAGE1, "insert", NOW, text="agenda")
        self.sync.edits.add(create)
        self.sync.edits.add(typed)
        self.assertEqual(rm_text.page_text(self.source.staged(self.doc).page(PAGE1)).text, "agenda")
        edit = self.source.add_stroke(self.doc, PAGE1, self.line(), NOW + 1)
        erase = self.source.erase_strokes(self.doc, PAGE1, upright(), NOW + 2)
        self.assertIsNotNone(erase)
        self.assertEqual(self.sync.edits.of(THROWAWAY), [create, typed, edit, erase])
        uploaded = self.files(self.sync.edits.of(THROWAWAY))[f"{THROWAWAY}/{PAGE1}.rm"]
        self.assertEqual(rm_text.page_text(uploaded).text, "agenda")              # the text as typed
        self.assertEqual(len(s.strokes(uploaded)), 3)               # the tablet's (out of reach), ours in two pieces
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 3))
        self.assertEqual(len(s.strokes(self.source.staged(self.doc).page(PAGE1))), 2)      # ours whole again

    def test_a_text_box_nothing_was_typed_in_is_not_made(self):
        from core.edits import TextEdit
        from viewer import rm_text
        create = TextEdit.new(THROWAWAY, PAGE1, "create", NOW, paragraph=0)
        self.assertIsNone(rm_text.page_text(replay(drawn_page(), [create], self.sync.author()).page))
        self.assertEqual(self.files([create]), {})                            # nothing to upload
        # With a stroke after it, the page gets the stroke and still no box.
        files = self.files([create, StrokeAdd.new(THROWAWAY, PAGE1, self.line(), NOW)])
        self.assertIsNone(rm_text.page_text(files[f"{THROWAWAY}/{PAGE1}.rm"]))
        # Typed in, it is made.
        typed = TextEdit.new(THROWAWAY, PAGE1, "insert", NOW, text="x")
        self.assertEqual(rm_text.page_text(replay(drawn_page(), [create, typed], self.sync.author()).page).text, "x")

    def test_the_plan_says_what_was_drawn_and_erased(self):
        yellow = self.line(tool=s.HIGHLIGHTER_V2, color=9, thickness=1.0, argb=0xFFFFED75)
        self.source.add_stroke(self.doc, PAGE1, self.line(), NOW)
        self.source.add_stroke(self.doc, PAGE1, yellow, NOW + 1)
        self.source.erase_strokes(self.doc, PAGE1, upright(), NOW + 2)
        self.source.delete_strokes(self.doc, PAGE1, [("page", 1, 15)], NOW + 3)
        requests = len(self.cloud.calls)
        plan = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache").push_plan(THROWAWAY)
        self.assertEqual(len(self.cloud.calls), requests)
        self.assertEqual([f.name for f in plan.files],
                         [f"{THROWAWAY}.content", f"{THROWAWAY}.metadata", f"{THROWAWAY}/{PAGE1}.rm"])
        text = plan.describe()
        for part in (f"2 strokes on page {PAGE1}, 4 points", "1 x Finelinerv2, Black, size 2",
                     "1 x Highlighterv2, ARGB ffffed75, size 1", "drawn over x -100.5..100.5, y 100.0..100.0",
                     f"erase on page {PAGE1}: eraser of 3 points, 20 wide", "over x 0.2..0.2, y 60.0..140.0",
                     f"delete 1 stroke on page {PAGE1}", "Edits     4, applied in this order"):
            self.assertIn(part, text)
        self.assertIn(f"Strokes   {THROWAWAY}/{PAGE1}.rm: 1 live before, 4 after (1 deleted, 4 new)", text)
        lasso = StrokeErase.new(THROWAWAY, PAGE1, s.new_line(s.ERASE_SECTION, 0, 2.0, DOT), NOW)
        self.assertIn("erase-area outline of 2 points", describe_edit(lasso))

    def test_the_plan_sums_up_strokes_drawn_one_after_the_other(self):
        yellow = self.line(140.0, tool=s.HIGHLIGHTER_V2, color=9, thickness=1.0, argb=0xFFFFED75)
        for n, line in enumerate([self.line(80.0), self.line(), yellow, self.line(120.0)]):
            self.source.add_stroke(self.doc, PAGE1, line, NOW + n)
        self.source.add_stroke(self.doc, PAGE2, self.line(), NOW + 4)
        for n, x in enumerate((0.25, 40.25)):
            self.source.erase_strokes(self.doc, PAGE1, upright(x=x), NOW + 5 + n)
        text = SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache").push_plan(THROWAWAY).describe()
        for part in ("Edits     7, applied in this order",
                     f"  4 strokes on page {PAGE1}, 8 points\n    3 x Finelinerv2, Black, size 2\n"
                     "    1 x Highlighterv2, ARGB ffffed75, size 1\n    drawn over x -100.5..100.5, y 80.0..140.0",
                     f"  stroke on page {PAGE2}: Finelinerv2, Black, size 2, 2 points",
                     f"  2 eraser passes on page {PAGE1}\n    over x 0.2..40.2, y 60.0..140.0",
                     f"Strokes   {THROWAWAY}/{PAGE1}.rm: 1 live before, 13 after (0 deleted, 12 new)",
                     f"Strokes   {THROWAWAY}/{PAGE2}.rm: 0 live before, 1 after (0 deleted, 1 new)"):
            self.assertIn(part, text)

    def test_the_tool_lists_strokes_and_sends_nothing(self):
        self.source.add_stroke(self.doc, PAGE1, self.line(), NOW)
        requests = len(self.cloud.calls)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = push_edits.main([], SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"))
        self.assertEqual((status, len(self.cloud.calls)), (0, requests))
        for part in ("stroke on page", "Finelinerv2", "Nothing was sent", "--confirm"):
            self.assertIn(part, out.getvalue())

    def test_pushed_strokes_reach_the_cloud_and_are_no_longer_pending(self):
        self.source.add_stroke(self.doc, PAGE1, self.line(), NOW)
        self.source.erase_strokes(self.doc, PAGE1, upright(), NOW + 1)
        self.sync.push_edits(THROWAWAY)
        self.assertEqual(self.sync.edits.all(), [])
        uploaded = self.cloud.item(THROWAWAY)[f"{THROWAWAY}/{PAGE1}.rm"]
        self.assertEqual([x.item for x in s.strokes(uploaded)], [(1, 15), (2, 17), (2, 18)])
        self.assertEqual(struct.unpack_from("<I", uploaded, w.split(uploaded)[1][2].offset)[0],
                         len(w.split(drawn_page())[1][2].payload))      # PageStats: as it was


class StrokeUndoTests(Base):
    """Stroke undo and redo as the app's item actions do them: what was drawn is deleted,
    what was taken off comes back as a new item right after its deleted self, its
    moveId the item it first was."""

    files = StrokeEditTests.files

    def line(self, y: float = 100.0, **pen) -> s.LineValue:
        return ruled(y=y, **pen)

    def page(self) -> bytes:
        return self.source.staged(self.tree.get(THROWAWAY)).page(PAGE1)

    def live(self) -> list:
        return [(x.item, x.line.points) for x in s.strokes(self.page())]

    def test_restore_puts_a_new_item_right_after_the_deleted_one(self):
        data = chain(3)
        deleted = s.delete_strokes(data, [(1, 16)])
        value = s.strokes(data)[1].line
        restored, new_id = s.restore_stroke(deleted, author=DESKTOP, item=(1, 16), line=value)
        self.assertEqual(new_id, (2, 18))
        self.assertEqual(sorted(i[1:5] for i in items(restored)),
                         [((1, 15), ZERO, ZERO, 0), ((1, 16), (1, 15), ZERO, 1), ((1, 17), (1, 16), ZERO, 0),
                          ((2, 18), (1, 16), (1, 17), 0)])
        self.assertEqual([i for i, _ in w.sequence(w.split(restored)[1], LAYER)], [(1, 15), (1, 16), (2, 18), (1, 17)])
        [back] = [x for x in s.strokes(restored) if x.item == new_id]
        self.assertEqual(back.line, dataclasses.replace(value, move_id=(1, 16)))
        # In the middle of a run of deleted ids the run is cut there; a moveId it has is kept.
        run = s.delete_strokes(data, [(1, 15), (1, 16), (1, 17)])
        self.assertEqual([i[1:5] for i in items(run)], [((1, 15), ZERO, ZERO, 3)])
        again, new_id = s.restore_stroke(run, author=TABLET, item=(1, 16),
                                         line=dataclasses.replace(value, move_id=(1, 3)))
        self.assertEqual(sorted(i[1:5] for i in items(again)),
                         [((1, 15), ZERO, ZERO, 2), ((1, 17), (1, 16), ZERO, 1), ((1, 18), (1, 16), (1, 17), 0)])
        self.assertEqual([i for i, _ in w.sequence(w.split(again)[1], LAYER)], [(1, 15), (1, 16), (1, 18), (1, 17)])
        self.assertEqual(s.strokes(again)[0].line.move_id, (1, 3))
        with self.assertRaises(s.StrokeError):
            s.restore_stroke(data, author=TABLET, item=(1, 16), line=value)         # not deleted

    def test_undo_and_redo_of_a_stroke(self):
        tablet = self.live()
        edit = self.source.add_stroke(self.doc, PAGE1, self.line(), NOW)
        drawn = self.live()
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 1))
        self.assertEqual(self.live(), tablet)
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (False, True))
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 2))
        self.assertEqual([p for _, p in self.live()], [p for _, p in drawn])
        [back] = [x for x in s.strokes(self.page()) if x.item not in dict(tablet)]
        self.assertEqual(back.line.move_id, dict((uid, i) for uid, i in [(edit.uid, drawn[-1][0])])[edit.uid])
        # Undo again deletes the stroke the redo put back (named by its new item).
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 3))
        self.assertEqual(self.live(), tablet)
        self.assertEqual([type(e).__name__ for e in self.sync.edits.of(THROWAWAY)],
                         ["StrokeAdd", "StrokeDelete", "StrokeRestore", "StrokeDelete"])
        # Uploaded, the page is the tablet's with three deleted items.
        files = self.files(self.sync.edits.of(THROWAWAY))
        self.assertEqual([x.item for x in s.strokes(files[f"{THROWAWAY}/{PAGE1}.rm"])], [i for i, _ in tablet])

    def test_undo_and_redo_of_an_eraser_pass(self):
        self.source.add_stroke(self.doc, PAGE1, self.line(), NOW)
        drawn = self.live()
        self.assertIsNotNone(self.source.erase_strokes(self.doc, PAGE1, upright(), NOW + 1))
        erased = self.live()
        self.assertEqual(len(erased), len(drawn) + 1)               # ours cut in two
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 2))
        self.assertEqual(sorted(p for _, p in self.live()), sorted(p for _, p in drawn))
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 3))         # the stroke, put back by the undo
        self.assertEqual(len(self.live()), len(drawn) - 1)
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 4))
        self.assertTrue(self.source.redo(self.doc, PAGE1, NOW + 5))
        self.assertEqual(sorted(p for _, p in self.live()), sorted(p for _, p in erased))
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (True, False))
        # An eraser that meets nothing keeps nothing, and is no undo step.
        count = len(self.sync.edits.of(THROWAWAY))
        self.assertIsNone(self.source.erase_strokes(self.doc, PAGE1, upright(x=600.0), NOW + 6))
        self.assertEqual(len(self.sync.edits.of(THROWAWAY)), count)
        self.assertTrue(self.source.undo(self.doc, PAGE1, NOW + 7))
        self.assertEqual(sorted(p for _, p in self.live()), sorted(p for _, p in drawn))

    def test_a_restore_edit_round_trips_and_is_described(self):
        value = self.line().encode()
        edit = StrokeRestore.new(THROWAWAY, PAGE1, ("page", 1, 15), value, NOW, ("edit", "abc"))
        raw = json.loads(json.dumps({"kind": "stroke-restore", **dataclasses.asdict(edit)}))
        self.assertEqual(StrokeRestore.load(raw), edit)
        self.assertIn("put back a stroke", describe_edit(edit))
        with self.assertRaises(EditError):
            StrokeRestore.new(THROWAWAY, PAGE1, ("page", 1, 15), b"\x03\x01", NOW)
        # The item it was is gone: nothing is put back.
        self.assertEqual(replay(drawn_page(), [StrokeRestore.new(THROWAWAY, PAGE1, ("page", 1, 15), value, NOW)],
                                self.sync.author()).page, drawn_page())


class PenSettingsTests(Base):
    """A document's pen settings: its .content extraMetadata, as the app keeps them."""

    files = StrokeEditTests.files

    def test_the_settings_are_read_with_defaults_where_missing(self):
        from ui import pen
        self.assertEqual(pen.current_tool({}).key, "ballpoint")
        self.assertEqual(pen.current_tool({"LastTool": "SelectionTool", "LastPen": "Markerv2"}).key, "marker")
        p = pen.pen_of({"LastFinelinerv2Size": "3", "LastFinelinerv2Color": "Blue"}, pen.BY_KEY["fineliner"])
        self.assertEqual((p.size, p.color.key, p.line_color), (3, "blue", int(Color.BLUE)))
        p = pen.pen_of({}, pen.BY_KEY["highlighter"])
        self.assertEqual((p.size, p.color.name, p.line_color), (1, "HighlighterYellow", int(Color.HIGHLIGHTER_YELLOW)))
        p = pen.pen_of({"LastFinelinerv2Color": "ArgbCode", "LastFinelinerv2ColorCode": "4294962549"},
                       pen.BY_KEY["fineliner"])
        self.assertEqual((p.color, p.argb, p.line_color), (None, 0xFFFFED75, 9))
        self.assertEqual(pen.pen_of({"LastPencilv2Size": "7"}, pen.BY_KEY["pencil"]).size, 2)
        self.assertEqual({c.name for c in pen.PEN_COLORS},
                         {"Black", "Gray", "White", "Blue", "Red", "Green", "Yellow", "Magenta", "Cyan"})
        self.assertEqual(pen.tool_values(pen.BY_KEY["eraser"]), {"LastTool": "Eraser", "LastEraserTool": "Eraser"})
        with self.assertRaises(ValueError):
            pen.color_values(pen.BY_KEY["highlighter"], "black")

    def test_settings_are_one_pending_edit_and_change_only_the_content(self):
        self.assertEqual(self.source.tool_settings(self.doc).get("LastTool"), None)
        first = self.source.set_tool_settings(self.doc, {"LastTool": "Finelinerv2", "LastPen": "Finelinerv2"}, NOW)
        self.source.set_tool_settings(self.doc, {"LastFinelinerv2Size": "3"}, NOW + 1)
        self.assertIsNone(self.source.set_tool_settings(self.doc, {"LastFinelinerv2Size": "3"}, NOW + 2))
        [edit] = self.sync.edits.of(THROWAWAY)
        self.assertIsInstance(edit, ToolSettings)
        self.assertNotEqual(edit, first)
        self.assertEqual(dict(edit.values), {"LastTool": "Finelinerv2", "LastPen": "Finelinerv2",
                                             "LastFinelinerv2Size": "3"})
        self.assertEqual(self.source.tool_settings(self.doc)["LastFinelinerv2Size"], "3")
        files = self.files([edit])
        content = f"{THROWAWAY}.content"
        self.assertEqual(sorted(files), [content])
        self.assertEqual(json_changes(json.loads(self.source.blob(self.doc, content)), json.loads(files[content])),
                         [("extraMetadata.LastFinelinerv2Size", None, "3"), ("extraMetadata.LastPen", None, "Finelinerv2"),
                          ("extraMetadata.LastTool", None, "Finelinerv2")])
        self.assertIn("pen settings: LastFinelinerv2Size = 3", describe_edit(edit))
        # With a stroke, the page's times change as well.
        self.source.add_stroke(self.doc, PAGE1, ruled(), NOW + 3)
        self.assertEqual(sorted(self.files(self.sync.edits.of(THROWAWAY))),
                         sorted([content, f"{THROWAWAY}.metadata", f"{THROWAWAY}/{PAGE1}.rm"]))
        with self.assertRaises(EditError):
            ToolSettings.new(THROWAWAY, {"Orientation": "landscape"}, NOW)
        with self.assertRaises(EditError):
            self.source.set_tool_settings(self.tree.get(OTHER), {"LastTool": "Eraser"}, NOW)
        self.sync.push_edits(THROWAWAY)
        uploaded = json.loads(self.cloud.item(THROWAWAY)[content])
        self.assertEqual(uploaded["extraMetadata"]["LastFinelinerv2Size"], "3")
        self.assertEqual(self.sync.edits.all(), [])


@unittest.skipUnless(os.environ.get("RM_TEST_CACHED_LIBRARY"),
                     "set RM_TEST_CACHED_LIBRARY=1 to read every stroke of the library cached on this machine")
class CachedLibraryTests(unittest.TestCase):
    def test_every_stroke_block_comes_back_byte_for_byte(self):
        root = Path.home() / ".cache" / "remarkable-linux" / "blobs"
        seen = lines = live = 0
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            with path.open("rb") as f:
                if f.read(len(w.V6_HEADER)) != w.V6_HEADER:
                    continue
            seen += 1
            for block in w.split(path.read_bytes())[1]:
                if block.type != w.BLOCK_LINE_ITEM:
                    continue
                lines += 1
                item = w.decode_item(block.payload)
                again = item
                if item.value is not None:
                    live += 1
                    line = s.decode_line_value(item.value)
                    self.assertEqual(s.pack_points(line.unpacked()), line.points)
                    again = dataclasses.replace(item, value=line.encode())
                self.assertEqual(w.new_block(w.BLOCK_LINE_ITEM, again.encode()).encode(), block.encode())
        print(f"\n{seen} pages, {lines} line blocks ({live} live) re-encoded to their bytes")
        self.assertTrue(seen == 0 or lines > 0)


if __name__ == "__main__":
    unittest.main()


class StraightenTests(unittest.TestCase):
    """Hold to straighten: ShapesOverlay's Line (0x140461c10 -> 0x14046be20)."""

    def drawn(self, tool=s.BALLPOINT_V2, samples=None):
        samples = samples or [(10.3 + 6 * k, 20.6 + 2.0 * k + (k % 3), 0.3 + 0.05 * k) for k in range(12)]
        pen = s.StrokeBuilder(tool, 0, s.MEDIUM)
        for sample in samples:
            pen.add(*sample)
        return pen.line()

    def test_fewer_than_three_points_make_nothing(self):
        self.assertIsNone(s.straighten(self.drawn(samples=[(0, 0), (5, 5)]), (50, 50)))

    def test_points_go_along_the_segment_by_length_drawn_keeping_their_numbers(self):
        line = self.drawn()
        out = s.straighten(line, (300.4, 251.7))
        a, b = out.unpacked()[0], out.unpacked()[-1]
        self.assertEqual((a.x, a.y, b.x, b.y), (10.0, 21.0, 300.0, 252.0))      # rounded to whole units
        drawn = line.unpacked()
        along = [0.0]
        for p, q in zip(drawn, drawn[1:]):
            along.append(along[-1] + math.hypot(q.x - p.x, q.y - p.y))
        for n, (p, q) in enumerate(zip(drawn, out.unpacked())):
            t = along[n] / along[-1]
            self.assertEqual((q.x, q.y), (s.f32(10 + 290 * t), s.f32(21 + 231 * t)))
            self.assertEqual((q.speed, q.width, q.direction, q.pressure), (p.speed, p.width, p.direction, p.pressure))
        self.assertEqual((out.tool, out.thickness_scale, out.timestamp, out.count), (line.tool, 2.0, (0, 1), line.count))

    def test_snaps_horizontal_and_vertical_within_the_tolerance_only(self):
        line = self.drawn()
        # |AB| / 3 caps at 10: a 9-unit step on a long line is flattened...
        flat = s.straighten(line, (400.0, 30.0)).unpacked()
        self.assertEqual({p.y for p in flat}, {21.0})
        upright = s.straighten(line, (19.0, 500.0)).unpacked()
        self.assertEqual({p.x for p in upright}, {10.0})
        # ...a 10-unit step is not (no angle steps either)...
        self.assertNotEqual(len({p.y for p in s.straighten(line, (400.0, 31.0)).unpacked()}), 1)
        # ...and the tolerance is in screen units: zoomed in 2x it halves.
        self.assertNotEqual(len({p.y for p in s.straighten(line, (400.0, 27.0), scale=2.0).unpacked()}), 1)
        self.assertEqual({p.y for p in s.straighten(line, (400.0, 25.0), scale=2.0).unpacked()}, {21.0})
        # On a short line the tolerance is a third of its length.
        short = s.straighten(line, (22.0, 24.0)).unpacked()                # |AB| ~ 12.4 -> 4.1
        self.assertEqual({p.y for p in short}, {21.0})

    def test_thinning_after_it_as_for_any_stroke(self):
        # The Fineliner ignores pressure: a straight one thins to its two ends.
        fine = s.simplify(s.straighten(self.drawn(s.FINELINER_V2), (300.0, 21.0)))
        self.assertEqual(fine.count, 2)
        # The Ballpoint keeps its pressure profile along the line.
        ball = s.simplify(s.straighten(self.drawn(s.BALLPOINT_V2), (300.0, 21.0)))
        self.assertGreater(ball.count, 2)
        self.assertEqual({p.y for p in ball.unpacked()}, {21.0})


class PageStatsLoadsTests(unittest.TestCase):
    """PageStats loads + 1 once per editing session of a page (ARCHITECTURE.md 5.5)."""

    def test_one_more_however_many_edits_the_session_made(self):
        from core.edits import StrokeAdd, replay
        author = w.author_uuid("loads-test")
        base = drawn_page()
        loads = w.page_loads(base)
        lines = [s.draw([(x, 0.0), (x + 20.0, 30.0)], s.FINELINER_V2) for x in (0.0, 50.0, 100.0)]
        edits = [StrokeAdd.new(THROWAWAY, PAGE1, line, NOW) for line in lines]
        self.assertEqual(w.page_loads(replay(base, edits[:1], author).page), loads + 1)
        self.assertEqual(w.page_loads(replay(base, edits, author).page), loads + 1)
        self.assertIs(replay(base, [], author).page, base)                           # no edit, no save
        self.assertEqual(w.page_loads(replay(None, edits, author).page), 1)           # a new page: 0 -> 1
        # Only field 1 changes.
        stats = lambda page: [b.payload for b in w.split(page)[1] if b.type == w.BLOCK_PAGE_STATS][0]  # noqa: E731
        before, after = w.read_values(stats(base)), w.read_values(stats(replay(base, edits, author).page))
        self.assertEqual([v for v in before if v[0] != 1], [v for v in after if v[0] != 1])
