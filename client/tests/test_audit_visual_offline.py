"""The audit's visual and ebook work (ARCHITECTURE.md 5.2, 5.3, 5.16, 3.3): 3.29's
pencil and paintbrush textures, its procedural templates (Hexagon, Calligraphy), EPUB
renditions made on import, and the cache collected after a sync. Synthetic ids only."""

from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from core.edits import ImageInsert
from core.epub import EpubError, generate_rendition, line_height, spine
from core.sync import SyncClient
from demo_library import build_demo_cloud, demo_id, ms
from fakecloud import paired_auth
from rmfixtures import ROOT, group_item, line_item, points_v2, scene_tree, tree_node, v6_file
from viewer import brushes, renderer, rm_parser
from viewer.templates import (PROCEDURAL_TEMPLATES, ProceduralTemplate, default_library, paint_template,
                              template_shapes, template_svg)

LAYER = (0, 11)
TEXTURES_INSTALLED = brushes.available("pencil0.png") and brushes.available("brush0.png")


def png(width: int, height: int, color: str) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def make_epub(chapters: list[str], image: bytes | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr("META-INF/container.xml",
                      '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                      'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                      'media-type="application/oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(len(chapters)))
        if image:
            items += '<item id="img" href="images/pic.png" media-type="image/png"/>'
        order = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        book.writestr("OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                                           f'version="3.0"><manifest>{items}</manifest><spine>{order}</spine></package>')
        for i, body in enumerate(chapters):
            book.writestr(f"OEBPS/text/c{i}.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t'
                                                    f'</title></head><body>{body}</body></html>')
        if image:
            book.writestr("OEBPS/images/pic.png", image)
    return out.getvalue()


class TextureTests(unittest.TestCase):
    def test_tile_choice_follows_the_apps_hash_and_pressure(self):
        # 0x140b0f540: below 0.75 the light set, below 0.9 the plain one, else hard.
        self.assertTrue(brushes.pencil_texture(10, 20, 0.5).endswith("-light.png"))
        self.assertNotIn("-", brushes.pencil_texture(10, 20, 0.8)[len("pencil0"):])
        self.assertTrue(brushes.pencil_texture(10, 20, 0.95).endswith("-hard.png"))
        variants = {brushes.texture_variant(x, y) for x in range(0, 200, 7) for y in range(0, 200, 11)}
        self.assertEqual(variants, {0, 1, 2, 3})
        self.assertEqual(brushes.texture_variant(10.25, 20.5), brushes.texture_variant(10.25, 20.5))
        # The variant depends on (uint)(x + y) only.
        self.assertEqual(brushes.texture_variant(10.0, 20.0), brushes.texture_variant(25.0, 5.0))
        # 0x140b2cdf0: the paintbrush is textured only faster than 20.
        self.assertIsNone(brushes.brush_texture(1, 2, 20.0))
        self.assertRegex(brushes.brush_texture(1, 2, 20.5), r"^brush[0-3]\.png$")

    @unittest.skipUnless(TEXTURES_INSTALLED, "the app's brush textures are not installed (install.py)")
    def test_pencil_runs_are_drawn_through_the_tiles(self):
        stroke = line_item(LAYER, (1, 40), tool=14, color=0, points=points_v2(
            [(-200 + 10 * n, 400, 1, 0, 40, 220) for n in range(41)]))
        scene = rm_parser.parse(v6_file(tree_node(LAYER, "Layer 1"), scene_tree(LAYER),
                                        group_item(ROOT, (1, 30), LAYER), stroke))
        runs = renderer.stroke_runs(scene, scene.strokes[0])
        self.assertTrue(all(r.texture and r.texture.startswith("pencil") and "-light" not in r.texture
                            for r in runs))
        image = QImage(1404, 1872, QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        renderer.paint(painter, scene)
        painter.end()
        values = {image.pixelColor(x, 400).lightness() for x in range(520, 880)}
        self.assertGreater(len(values), 2)                 # grain, not one flat grey
        self.assertLess(min(values), 200)
        svg = "\n".join(renderer.svg_scene(scene))
        self.assertIn("<pattern id=\"tex-0\"", svg)
        self.assertIn('stroke="url(#tex-', svg)
        self.assertNotIn("<image", svg)                     # the tiles are drawn as vector runs

    @unittest.skipUnless(TEXTURES_INSTALLED, "the app's brush textures are not installed (install.py)")
    def test_brush_tiles_are_the_apps_bitmaps(self):
        tile = brushes.texture_tile("brush0.png", "#204080", 1.0)
        alphas = {tile.pixelColor(x, y).alpha() for x in range(64) for y in range(64)}
        self.assertEqual(alphas, {0, 255})                   # a QBitmap: set or not
        self.assertEqual(tile.pixelColor(*next((x, y) for x in range(64) for y in range(64)
                                               if tile.pixelColor(x, y).alpha())).name(), "#204080")


class InstallTests(unittest.TestCase):
    def test_install_copies_the_sixteen_textures(self):
        import re
        source = (HERE.parents[1] / "install.py").read_text()
        pattern = re.search(r're\.fullmatch\(r"(/images/[^"]+)", path\)', source).group(1)
        names = [f"/images/pencil{n}{s}.png" for n in range(4) for s in ("", "-light", "-hard")] + \
                [f"/images/brush{n}.png" for n in range(4)]
        self.assertTrue(all(re.fullmatch(pattern, n) for n in names))
        for other in ("/images/marker0.png", "/images/pencil0-hard.svg", "/images/menu_shadow.png"):
            self.assertIsNone(re.fullmatch(pattern, other))
        self.assertIn('"brushes"', source)


class ProceduralTemplateTests(unittest.TestCase):
    def test_every_procedural_template_is_drawn(self):
        library = default_library()
        for name in PROCEDURAL_TEMPLATES:
            template = library.resolve(name)
            self.assertIsInstance(template, ProceduralTemplate, name)
            self.assertEqual(template.orientation, "landscape" if name.startswith("LS ") else "portrait")
            shapes = list(template_shapes(template, (0, 0, 1404, 1872)))
            self.assertGreater(len(shapes), 50, name)
            image = QImage(702, 936, QImage.Format.Format_ARGB32)
            image.fill(QColor("white"))
            painter = QPainter(image)
            painter.scale(0.5, 0.5)
            paint_template(painter, template, (0, 0, 1404, 1872))
            painter.end()
            self.assertTrue(any(image.pixelColor(x, y).lightness() < 250
                                for x in range(0, 702, 3) for y in range(0, 936, 3)), name)

    def test_hexagons_are_regular(self):
        # 0x140a0d340: edges from (x, y0) to (x + s/2, y0 + P/6) and so on, P = s sqrt 3.
        template = default_library().resolve("P Hexagon large")
        lines = [s.commands for s in template_shapes(template, (0, 0, 1404, 1872))][:6]
        lengths = [math.dist(c[0][1][0], c[1][1][0]) for c in lines]
        for length in lengths:
            self.assertAlmostEqual(length, 194.4 / 3, delta=0.2)
        self.assertEqual({s.stroke_width for s in template_shapes(template, (0, 0, 1404, 1872))}, {0.5})

    def test_calligraphy_has_dashed_cosmetic_guides_and_a_solid_baseline(self):
        template = default_library().resolve("P Calligraphy medium")
        shapes = list(template_shapes(template, (0, 0, 1404, 1872)))
        dashed = [s for s in shapes if s.dash]
        self.assertTrue(dashed and all(s.cosmetic and s.dash == (13.0, 17.0) for s in dashed))
        solid = [s for s in shapes if not s.dash]
        horizontal = [s for s in solid if s.commands[0][1][0][1] == s.commands[1][1][0][1]]
        self.assertTrue(horizontal)                         # the baselines
        svg = "\n".join(template_svg(template, (0, 0, 1404, 1872)))
        self.assertIn('stroke-dasharray="6.5 8.5"', svg)    # 13 and 17 pen widths of 0.5
        self.assertIn('vector-effect="non-scaling-stroke"', svg)


class EpubRenditionTests(unittest.TestCase):
    def test_a_rendition_is_laid_out_as_3_29_lays_it_out(self):
        from pypdf import PdfReader
        book = make_epub(["<h1>One</h1>" + "<p>Lorem ipsum dolor sit amet.</p>" * 120,
                          '<p>Two</p><img src="../images/pic.png"/>'], png(300, 200, "#c03030"))
        pdf, pages = generate_rendition(book)
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), pages)
        self.assertGreater(pages, 2)
        box = reader.pages[0].mediabox
        self.assertEqual((round(float(box.width)), round(float(box.height))), (445, 594))   # 1404 x 1872 at 227 dpi
        self.assertIn("One", reader.pages[0].extract_text())
        self.assertIn("Two", reader.pages[-1].extract_text())   # a chapter starts a page
        self.assertEqual(len(reader.pages[-1].images), 1)
        self.assertEqual((line_height(-1), line_height(150)), (1.0, 1.5))

    def test_a_broken_epub_is_refused(self):
        with self.assertRaises(EpubError):
            generate_rendition(b"not a zip")
        with zipfile.ZipFile(io.BytesIO(make_epub(["x"]))) as book:
            self.assertEqual(spine(book), ["OEBPS/text/c0.xhtml"])

    def test_an_imported_epub_goes_up_with_its_rendition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cloud = build_demo_cloud()
            sync = SyncClient(paired_auth(tmp / "auth", cloud), tmp / "cache")
            sync.refresh()
            path = tmp / "Synthetic book.epub"
            path.write_bytes(make_epub(["<p>Hello</p>", "<p>World</p>"]))
            doc_id = sync.import_file(path)
            files = cloud.docs[doc_id]
            self.assertIn(f"{doc_id}.pdf", files)
            self.assertIn(f"{doc_id}.epub", files)
            content = json.loads(files[f"{doc_id}.content"])
            self.assertEqual((content["fileType"], content["pageCount"]), ("epub", 2))
            broken = tmp / "Broken book.epub"
            broken.write_bytes(b"PK\x03\x04 not really")
            try:
                other = sync.import_file(broken)
            except Exception:               # refused as not an EPUB at all: also fine
                other = None
            if other is not None:
                self.assertNotIn(f"{other}.pdf", cloud.docs[other])


class CacheCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.cloud = build_demo_cloud()
        self.sync = SyncClient(paired_auth(tmp / "auth", self.cloud), tmp / "cache")
        self.thumbs = tmp / "cache" / "thumbnails"

    def test_only_what_nothing_uses_is_collected(self):
        self.sync.refresh()
        first = set(self.sync.blobs.hashes())
        # Another device changes a document: its old index and .metadata become stale.
        changed = demo_id("gc doc")
        self.cloud.put_item(changed, {"visibleName": "v1", "parent": "", "type": "DocumentType",
                                      "lastModified": ms(2)}, {"fileType": "notebook"})
        self.sync.refresh()
        old_doc = self.sync.tree.get(changed).hash
        self.cloud.put_item(changed, {"visibleName": "v2", "parent": "", "type": "DocumentType",
                                      "lastModified": ms(1)}, {"fileType": "notebook"})
        self.sync.refresh()
        self.assertTrue(self.sync.blobs.has(old_doc))
        # A pending edit's file, and a protected blob, stay.
        data = b"\x89PNG pending image"
        page = demo_id("gc page")
        from ui import editing
        edit = ImageInsert.new(changed, page, "png", editing.corners(0, 300, 200, 100),
                               hashlib.sha256(data).hexdigest(), len(data), 1)
        self.sync.edits.add(edit, data)
        pending = hashlib.sha256(data).hexdigest()
        self.sync.blobs.put(pending, data)
        protected = hashlib.sha256(b"open").hexdigest()
        self.sync.blobs.put(protected, b"open")
        # Thumbnails: one of the current version, one of the old, one of an old format.
        self.thumbs.mkdir(parents=True)
        current = self.sync.tree.get(changed).hash
        for name in (f"{current}-328-p0-v3.png", f"{old_doc}-328-p0-v3.png", f"{current}-156-p0-v2.png"):
            (self.thumbs / name).write_bytes(b"png")
        before = set(self.sync.blobs.hashes())
        result = self.sync.collect_garbage(self.thumbs, protect={protected}, thumbnail_version=3)
        self.assertEqual(result["skipped"], 0)
        self.assertGreater(result["blobs"], 0)
        self.assertFalse(self.sync.blobs.has(old_doc))
        for h in (self.sync.referenced_hashes() | {pending, protected}) & before:   # what was cached stays
            self.assertTrue(self.sync.blobs.has(h), h)
        self.assertFalse((before - self.sync.blobs.hashes()) & (self.sync.referenced_hashes() | {pending, protected}))
        self.assertTrue(first & self.sync.blobs.hashes())
        self.assertEqual(sorted(p.name for p in self.thumbs.iterdir()), [f"{current}-328-p0-v3.png"])
        # Every document still opens from the cache alone.
        cached = SyncClient(paired_auth(Path(self.tmp.name) / "auth2", self.cloud), Path(self.tmp.name) / "cache")
        self.assertEqual(len(cached.load_cached().nodes), len(self.sync.tree.nodes))

    def test_nothing_is_collected_without_a_whole_refreshed_tree(self):
        self.sync.refresh()
        stale = hashlib.sha256(b"stale").hexdigest()
        self.sync.blobs.put(stale, b"stale")
        cached = SyncClient(paired_auth(Path(self.tmp.name) / "auth2", self.cloud), Path(self.tmp.name) / "cache")
        cached.load_cached()
        self.assertEqual(cached.collect_garbage()["skipped"], 1)        # cached only
        self.sync.tree_complete = False                                 # a refresh that lost an entry
        self.assertEqual(self.sync.collect_garbage()["skipped"], 1)
        self.assertTrue(self.sync.blobs.has(stale))


if __name__ == "__main__":
    unittest.main()
