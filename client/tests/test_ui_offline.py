"""Offline tests for the desktop UI (navigator, viewer, workers) on the demo library.

Runs headless (QT_QPA_PLATFORM=offscreen).
Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import base64
import gc
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CLIENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CLIENT / "src"), str(CLIENT / "tests"), str(CLIENT)]

from PySide6.QtCore import QEvent, QPoint, QSize, QSizeF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from demo_library import IDS, LOREM, NOW, PageBuilder, build_demo_cloud, demo_id, make_pdf, pdf_px  # noqa: E402
from fakecloud import paired_auth  # noqa: E402

from core.models import Content, Document, DocumentTree, FileType, Metadata, make_node  # noqa: E402
from core.sync import NotCachedError, SyncClient  # noqa: E402
from ui.library import (SIDEBAR, Entry, LibraryQuery, LibrarySource, Location, SortOrder, ViewFilter,  # noqa: E402
                        document_pages, pdf_name, relative_time, start_page)
from ui.main_window import MainWindow  # noqa: E402
from ui.navigator import GRID, LIST, Navigator, Sidebar  # noqa: E402
from ui.pages import (PDF_PX_PER_PT, DocumentPages, PageContent, PdfFile, cover_index,  # noqa: E402
                      pdf_target_rect, render_content)
from ui.theme import Colors, icon_image  # noqa: E402
from ui.workers import Exporter, PageRenderer, Syncer, ThumbnailService  # noqa: E402
from ui import export as export_module  # noqa: E402
from ui.export import PDF_FILTER, RMDOC_FILTER, export_file_name, export_pdf, export_svg, page_svg  # noqa: E402
from viewer.renderer import render_image  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402
from viewer.templates import (TemplateError, TemplateLibrary, _repeat_offsets, compile_expression,  # noqa: E402
                              default_library, evaluate, paint_template, parse_pagedata, parse_template,
                              template_svg)

CLOUD = build_demo_cloud()          # built once: generating handwriting/PDFs takes a moment


def detach_pool(owner):
    """Detach a worker's pool from its owner so it outlives the owner's deletion."""
    pool = owner.pool
    pool.setParent(None)
    return pool


def settle(*pools, rounds: int = 3) -> None:
    """Wait for background pools and deliver their queued signals."""
    for _ in range(rounds):
        for pool in pools:
            pool.waitForDone(30000)
        QTest.qWait(30)


def dispose(widget) -> None:
    """Close a window and delete it now. Windows left alive are destroyed by PySide's
    clean-up at interpreter exit, which can crash there (after every test passed)."""
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def click(widget, pos: QPoint) -> None:
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


class DemoCase(unittest.TestCase):
    """A synced demo library in a fresh cache."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.auth = paired_auth(self.dir / "config", CLOUD)
        self.sync = SyncClient(self.auth, self.dir / "cache", sleep=lambda s: None)
        self.source = LibrarySource(self.sync)
        self.tree = self.source.refresh()
        self.query = LibraryQuery(self.tree, now=NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def doc(self, name: str) -> Document:
        return self.tree.get(IDS[name])

    def titles(self, loc: Location, sort: SortOrder = SortOrder.ALPHABETICAL) -> list[str]:
        return [e.title for e in self.query.entries(loc, sort)]


# ----------------------------------------------------------------------- library

class LibraryQueryTests(DemoCase):
    def test_sidebar_matches_official_order(self):
        self.assertEqual([i.label for i in SIDEBAR], ["My files", "Notebooks", "PDFs", "Ebooks", "Favorites",
                                                      "Tags", "Import", "Trash", "Settings"])
        self.assertEqual([i.label for i in SIDEBAR if i.view is None], ["Import", "Settings"])
        for item in SIDEBAR:                     # every icon exists in the extracted Ark set
            self.assertFalse(icon_image(item.icon, Colors.gray1, 20).isNull(), item.icon)

    def test_my_files_folders_first(self):
        self.assertEqual(self.titles(Location()), ["Personal", "Work", "A Short Story", "Sketchbook"])
        work = Location(folder_id=IDS["Work"])
        self.assertEqual(self.titles(work), ["Projects", "Research paper"])
        entries = self.query.entries(Location())
        self.assertEqual([e.kind for e in entries], ["folder", "folder", "document", "document"])
        personal = next(e for e in entries if e.title == "Personal")
        self.assertEqual((personal.subtext, personal.count), ("1 item", 1))

    def test_filters(self):
        self.assertEqual(self.titles(Location(ViewFilter.NOTEBOOKS)), ["Long page", "Meeting notes", "Sketchbook"])
        self.assertEqual(self.titles(Location(ViewFilter.PDFS)), ["Research paper"])
        self.assertEqual(self.titles(Location(ViewFilter.EBOOKS)), ["A Short Story"])
        self.assertEqual(self.titles(Location(ViewFilter.FAVORITES)), ["Work", "Sketchbook"])

    def test_tags(self):
        tags = self.query.entries(Location(ViewFilter.TAGS))
        self.assertEqual([(e.kind, e.title, e.count) for e in tags], [("tag", "reading", 2), ("tag", "work", 2)])
        loc = self.query.open_location(Location(ViewFilter.TAGS), tags[1])
        self.assertEqual(loc, Location(ViewFilter.TAGS, tag="work"))
        self.assertEqual(self.titles(loc), ["Meeting notes", "Research paper"])
        self.assertEqual([c.label for c in self.query.breadcrumbs(loc)], ["Tags", "work"])

    def test_page_tags_count_as_document_tags(self):
        notes = self.doc("Meeting notes")
        notes.content.raw["pageTags"] = [{"name": "todo", "pageId": "p", "timestamp": 1}, {"bad": 1}]
        tags = [e.title for e in LibraryQuery(self.tree).entries(Location(ViewFilter.TAGS))]
        self.assertEqual(tags, ["reading", "todo", "work"])

    def test_trash_and_trashed_folders(self):
        self.assertEqual(self.titles(Location(ViewFilter.TRASH)), ["Old draft"])
        self.assertNotIn("Old draft", self.titles(Location(ViewFilter.NOTEBOOKS)))
        # A folder inside the trash is browsed within the Trash view.
        trashed = make_node("f1", Metadata("Trashed folder", "CollectionType", "trash"))
        inner = make_node("d1", Metadata("Inner", parent="f1"))
        tree = DocumentTree(list(self.tree.nodes.values()) + [trashed, inner])
        q = LibraryQuery(tree)
        entry = next(e for e in q.entries(Location(ViewFilter.TRASH)) if e.title == "Trashed folder")
        loc = q.open_location(Location(ViewFilter.TRASH), entry)
        self.assertEqual(loc, Location(ViewFilter.TRASH, folder_id="f1"))
        self.assertEqual([c.label for c in q.breadcrumbs(loc)], ["Trash", "Trashed folder"])

    def test_search_is_library_wide_and_skips_trash(self):
        loc = Location(ViewFilter.PDFS, search="o")
        self.assertEqual(self.titles(loc), ["Personal", "Projects", "Work", "A Short Story", "Long page",
                                            "Meeting notes", "Sketchbook"])
        self.assertEqual(self.query.breadcrumbs(loc)[0].label, 'Search results for "o"')
        self.assertNotIn("Old draft", self.titles(Location(search="draft")))

    def test_sort_orders(self):
        loc = Location(ViewFilter.NOTEBOOKS)
        self.assertEqual(self.titles(loc, SortOrder.LAST_MODIFIED), ["Meeting notes", "Sketchbook", "Long page"])
        self.assertEqual(self.titles(loc, SortOrder.RECENTLY_OPENED), ["Meeting notes", "Sketchbook", "Long page"])
        self.assertEqual(self.titles(loc, SortOrder.DATE_CREATED), ["Meeting notes", "Sketchbook", "Long page"])
        self.assertEqual(self.titles(loc, SortOrder.PAGE_COUNT)[0], "Meeting notes")     # 2 pages
        self.assertEqual(SortOrder.LAST_MODIFIED.label(in_trash=True), "Date removed")
        self.assertEqual(SortOrder.ALPHABETICAL.label(in_trash=True), "Alphabetical (A-Z)")

    def test_breadcrumbs_and_validity(self):
        loc = Location(folder_id=IDS["Projects"])
        self.assertEqual([c.label for c in self.query.breadcrumbs(loc)], ["My files", "Work", "Projects"])
        self.assertEqual(self.query.breadcrumbs(loc)[1].location, Location(folder_id=IDS["Work"]))
        self.assertTrue(self.query.is_valid(loc))
        self.assertFalse(self.query.is_valid(Location(folder_id="gone")))
        self.assertFalse(self.query.is_valid(Location(folder_id=IDS["Sketchbook"])))   # a document
        with self.assertRaises(ValueError):
            self.query.entries(Location(view="bogus"))

    def test_relative_time(self):
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        for delta, text in ((timedelta(hours=1), "Today"), (timedelta(days=1), "Yesterday"),
                            (timedelta(days=3), "3 days ago"), (timedelta(days=40), "Sep 22"),
                            (timedelta(days=400), "Sep 27, 2025")):
            self.assertEqual(relative_time(now - delta, now), text)
        self.assertEqual(relative_time(None), "")
        self.assertTrue(relative_time(datetime.now(timezone.utc)))


class PageMappingTests(DemoCase):
    def test_pdf_pages_with_inserted_page(self):
        paper = self.doc("Research paper")
        pages = document_pages(paper)
        self.assertEqual([(p.pdf_page, p.rm_name is not None) for p in pages], [(0, True), (None, True), (1, False)])
        self.assertEqual(pdf_name(paper), f"{paper.id}.pdf")
        self.assertIsNone(pdf_name(self.doc("Sketchbook")))

    def test_fallbacks_without_page_list(self):
        bare = make_node("x", Metadata("Bare"), Content.parse({"fileType": "pdf"}),
                         files={"x.pdf": "h", "x/b.rm": "h", "x/a.rm": "h"})
        self.assertEqual([p.pdf_page for p in document_pages(bare, pdf_page_count=3)], [0, 1, 2])
        # Distinct ids: the viewer opens pages by id (the tablet assigns real ones later).
        self.assertEqual([p.page_id for p in document_pages(bare, pdf_page_count=2)], ["pdf-page-0", "pdf-page-1"])
        self.assertEqual([p.page_id for p in document_pages(bare)], ["a", "b"])

    def test_start_and_cover_page(self):
        notes = self.doc("Meeting notes")
        self.assertEqual(start_page(notes, 2), 0)
        notes.metadata.last_opened_page = 7
        self.assertEqual(start_page(notes, 2), 1)
        self.assertEqual(start_page(notes, 0), 0)
        notes.content.cover_page_number = -1             # "last opened page"
        self.assertEqual(cover_index(notes, 2), 1)
        notes.content.cover_page_number = 0
        self.assertEqual(cover_index(notes, 2), 0)

    def test_source_caches_scenes_and_handles_bad_pages(self):
        notes = self.doc("Meeting notes")
        page = document_pages(notes)[0]
        first = self.source.page_scene(notes, page)
        self.assertIs(self.source.page_scene(notes, page), first)        # LRU hit
        self.assertIsNone(self.source.page_scene(notes, document_pages(self.doc("Research paper"))[2]))
        small = LibrarySource(self.sync, scene_cache=1)
        small.page_scene(notes, page)
        small.page_scene(notes, document_pages(notes)[1])
        self.assertEqual(len(small._scenes), 1)
        name = f"{notes.id}/broken.rm"
        notes.files[name] = "0" * 64
        self.sync.blobs.put("0" * 64, b"not an rm file")
        broken = page.__class__(9, "broken", name, None)
        with self.assertLogs("rm.ui.library", "ERROR"):
            self.assertIsNone(self.source.page_scene(notes, broken))

    def test_offline_source_never_downloads(self):
        offline = LibrarySource(SyncClient(self.auth, self.dir / "empty-cache"), cached_only=True)
        tree = offline.refresh()                         # cached_only refresh = load from cache
        self.assertEqual(len(tree), 0)
        with self.assertRaises(NotCachedError):
            offline.pdf_path(self.doc("Research paper"))


# ------------------------------------------------------------------------- pages

class PageRenderingTests(DemoCase):
    def test_invalid_pdf_is_rejected(self):
        bad = self.dir / "bad.pdf"
        bad.write_bytes(b"this is not a pdf")
        from ui.pages import PdfFile
        with self.assertRaises(ValueError):
            PdfFile(bad)

    def test_pdf_target_rect_is_226_dpi_centred(self):
        rect = pdf_target_rect(QSizeF(595, 842))
        self.assertAlmostEqual(rect.width(), 595 * 226 / 72)
        self.assertAlmostEqual(rect.center().x(), 702)
        self.assertEqual(rect.top(), 0)
        self.assertEqual(pdf_target_rect(QSizeF(0, 0)).width(), 1404)

    def test_frames_and_bounds(self):
        pages = DocumentPages(self.source, self.doc("Research paper"))
        try:
            annotated, inserted = pages.content(0), pages.content(1)
            self.assertAlmostEqual(annotated.frame().width(), 595 * PDF_PX_PER_PT, places=3)
            self.assertEqual(annotated.bounds("clip"), (annotated.frame().x(), 0, annotated.frame().width(),
                                                        annotated.frame().height()))
            self.assertEqual(inserted.frame().width(), 1404)          # inserted page: screen page
            self.assertEqual(PageContent(None, None, None).bounds("page"), (0, 0, 1404, 1872))
            with self.assertRaises(ValueError):
                annotated.bounds("bogus")
        finally:
            pages.close()

    def test_pdf_background_and_annotations_render(self):
        pages = DocumentPages(self.source, self.doc("Research paper"))
        try:
            content = pages.content(0)
            scale = 0.25
            image = render_content(content, scale, "page")
            frame = content.frame()
            px = lambda x, y: image.pixelColor(round((x - frame.x()) * scale), round((y - frame.y()) * scale))
            hx, hy = pdf_px(250, 70)                                   # highlighter over the title, right of text
            self.assertEqual(px(hx, hy).name(), "#ffed75")
            tx, ty = pdf_px(62, 72)                                    # title glyphs stay dark (Darken)
            darkest = min(px(tx + dx, ty + dy).lightness() for dx in range(0, 60, 3) for dy in range(-20, 20, 3))
            self.assertLess(darkest, 80)
            self.assertEqual(px(*pdf_px(300, 700)).name(), "#ffffff")  # empty paper
        finally:
            pages.close()

    def test_pdf_pages_render_on_opaque_white(self):
        pdf = PdfFile(self.source.pdf_path(self.doc("Research paper")))
        try:
            size = QSize(200, 283)
            self.assertEqual(pdf.doc.render(0, size).pixelColor(5, 5).alpha(), 0)    # QtPdf: transparent paper
            image = pdf.render(0, size)
            self.assertFalse(image.hasAlphaChannel())
            self.assertEqual(image.pixelColor(5, 5).name(), "#ffffff")
        finally:
            pdf.close()

    def test_pdf_page_is_drawn_without_resampling(self):
        pdf = PdfFile(self.source.pdf_path(self.doc("Research paper")))
        try:
            content = PageContent(None, pdf, 0)
            scale = 700.5 / content.frame().width()            # the page spans a fractional 700.5 px
            image = render_content(content, scale, "clip")
            self.assertEqual(image.width(), 700)
            self.assertEqual(image.convertToFormat(QImage.Format.Format_RGB32), pdf.render(0, image.size()))
        finally:
            pdf.close()

    def test_highlighter_darkens_an_opaque_base_whatever_the_background(self):
        doc = self.doc("Meeting notes")
        scene = self.source.page_scene(doc, document_pages(doc)[0])
        image = render_image(scene, background="#00000000")
        self.assertEqual(image.pixelColor(100, 1700), QColor("#ffffff"))         # opaque, not transparent
        self.assertEqual(image.pixelColor(560, 250).name(), "#ffed75")

    def test_long_notebook_page_grows_and_clip_does_not(self):
        pages = DocumentPages(self.source, self.doc("Long page"))
        try:
            self.assertGreater(pages.content(0).bounds("page")[3], 3000)
            clipped = pages.render(0, 0.1, crop="clip")
            self.assertEqual((clipped.width(), clipped.height()), (140, 187))     # 3:4
        finally:
            pages.close()


# ------------------------------------------------------------------------ workers

class WorkerTests(DemoCase):
    def test_thumbnails_render_cache_and_invalidate(self):
        service = ThumbnailService(self.source, self.dir, width=140)
        ready = []
        service.ready.connect(ready.append)
        sketch, paper = self.doc("Sketchbook"), self.doc("Research paper")
        self.assertIsNone(service.thumbnail(sketch))                  # scheduled
        self.assertIsNone(service.thumbnail(sketch))                  # already pending
        service.thumbnail(paper)
        settle(service.pool)
        self.assertCountEqual(ready, [sketch.id, paper.id])
        image = service.thumbnail(sketch)
        self.assertEqual((image.width(), image.height()), (140, 187))
        self.assertAlmostEqual(service.thumbnail(paper).height() / 140, 842 / 595, places=1)   # PDF frame
        # A new service reads the disk cache without rendering.
        fresh = ThumbnailService(self.source, self.dir, width=140)
        self.assertIsNotNone(fresh.thumbnail(sketch))
        # A changed document hash means a new thumbnail.
        sketch.hash = "f" * 64
        self.assertIsNone(fresh.thumbnail(sketch))
        settle(fresh.pool)

    def test_thumbnail_failure_is_reported(self):
        offline = LibrarySource(SyncClient(self.auth, self.dir / "empty"), cached_only=True)
        service = ThumbnailService(offline, self.dir, width=100)
        failed = []
        service.failed.connect(lambda doc_id, why: failed.append(doc_id))
        doc = self.doc("Research paper")
        service.thumbnail(doc)
        settle(service.pool)
        self.assertEqual(failed, [doc.id])
        self.assertTrue(service.has_failed(doc))
        self.assertIsNone(service.thumbnail(doc))                     # not retried
        empty = make_node("e", Metadata("Empty"), Content.parse({"fileType": "notebook"}))
        service.thumbnail(empty)
        settle(service.pool)
        self.assertIn("e", failed)

    def test_page_renderer_success_and_failure(self):
        renderer = PageRenderer()
        got = []
        renderer.rendered.connect(lambda token, image: got.append((token, image.width())))
        renderer.failed.connect(lambda token, why: got.append((token, why)))
        pages = DocumentPages(self.source, self.doc("Sketchbook"))
        renderer.request(1, pages, 0, 702)
        settle(renderer.pool)
        # Same pages, but read through an empty offline cache: the page blob is missing.
        pages.source = LibrarySource(SyncClient(self.auth, self.dir / "empty"), cached_only=True)
        renderer.request(2, pages, 0, 702)
        settle(renderer.pool)
        self.assertEqual(got[0], (1, 702))
        self.assertEqual(got[1][0], 2)
        self.assertIn("not cached", got[1][1])

    def test_jobs_outliving_their_owner_do_not_raise(self):
        import shiboken6
        caught, hook = [], sys.excepthook
        sys.excepthook = lambda *exc: caught.append(exc[1])
        try:
            service = ThumbnailService(self.source, self.dir / "gone", width=100)
            for doc in self.tree.documents():
                service.thumbnail(doc)
            renderer = PageRenderer()
            renderer.request(1, DocumentPages(self.source, self.doc("Long page")), 0, 1404)
            syncer, exporter = Syncer(self.source), Exporter(self.source)
            syncer.refresh()
            exporter.export("pdf", self.doc("Meeting notes"), str(self.dir / "late.pdf"))
            pools = [detach_pool(o) for o in (service, renderer, syncer, exporter)]
            for owner in (service, renderer, syncer, exporter):
                shiboken6.delete(owner)              # window closed while jobs are running
            for pool in pools:
                pool.waitForDone(30000)
            QTest.qWait(30)
        finally:
            sys.excepthook = hook
        self.assertEqual(caught, [])
        live = ThumbnailService(self.source, self.dir / "live", width=100)
        from ui.workers import emit_from_worker
        with self.assertRaises(RuntimeError):                # real errors still surface
            emit_from_worker(live, lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    def test_syncer_success_failure_and_single_flight(self):
        syncer = Syncer(self.source)
        results = []
        syncer.finished.connect(lambda tree: results.append(len(tree)))
        syncer.failed.connect(results.append)
        self.assertTrue(syncer.refresh())
        self.assertFalse(syncer.refresh())                            # one at a time
        settle(syncer.pool)
        self.assertEqual(results, [len(self.tree)])

        def broken(*args):
            raise TimeoutError("offline")
        self.auth.transport = broken
        self.assertTrue(syncer.refresh())
        settle(syncer.pool)
        self.assertIn("SyncError", results[-1])
        self.assertFalse(syncer.running)


# ------------------------------------------------------------------------ widgets

class NavigatorTests(DemoCase):
    def setUp(self):
        super().setUp()
        self.thumbs = ThumbnailService(self.source, self.dir, width=120)
        self.nav = Navigator(self.thumbs)
        self.nav.resize(1100, 800)
        self.nav.show()
        self.nav.set_tree(self.tree, now=NOW)
        settle(self.thumbs.pool)

    def tearDown(self):
        self.nav.close()
        settle(self.thumbs.pool)
        dispose(self.nav)
        super().tearDown()

    def entry_pos(self, title: str) -> QPoint:
        item = next(i for i in self.nav.view.items() if i.entry.title == title)
        return item.rect.center() - QPoint(0, self.nav.view.verticalScrollBar().value())

    def titles(self):
        return [i.entry.title for i in self.nav.view.items()]

    def test_grid_layout_folder_tiles_then_cells(self):
        items = self.nav.view.items()
        tiles = [i for i in items if i.tile]
        cells = [i for i in items if not i.tile]
        self.assertEqual([i.entry.title for i in tiles], ["Work", "Personal"])          # last modified
        self.assertEqual(tiles[0].rect.height(), 40 + 16)
        self.assertTrue(all(c.rect.top() > tiles[-1].rect.bottom() for c in cells))
        cell = cells[0].rect
        self.assertAlmostEqual((cell.height() - 48) / cell.width(), 1872 / 1404, places=2)
        self.assertEqual(self.nav.crumb_row.sort.text(), "Last modified")
        grab = self.nav.grab()                                          # paints without errors
        self.assertFalse(grab.isNull())

    def test_click_folder_breadcrumbs_and_back(self):
        click(self.nav.view.viewport(), self.entry_pos("Work"))
        self.assertEqual(self.nav.location, Location(folder_id=IDS["Work"]))
        self.assertEqual(self.titles(), ["Projects", "Research paper"])
        click(self.nav.view.viewport(), self.entry_pos("Projects"))
        crumbs = [w.text() for w in self.nav.crumb_row._labels if hasattr(w, "text") and w.text()]
        self.assertEqual(crumbs, ["My files", "Work", "Projects"])
        self.nav.crumb_row._labels[0].click()                           # "My files" crumb
        self.assertEqual(self.nav.location, Location())
        self.nav.back()
        self.assertEqual(self.nav.location, Location(folder_id=IDS["Projects"]))
        self.assertTrue(self.nav.actions.back.isEnabled())

    def test_open_document_signal(self):
        opened = []
        self.nav.openDocument.connect(opened.append)
        click(self.nav.view.viewport(), self.entry_pos("Sketchbook"))
        self.assertEqual(opened, [self.doc("Sketchbook")])

    def test_sidebar_views_and_disabled_items(self):
        sidebar: Sidebar = self.nav.sidebar
        click(sidebar, sidebar.item_rect(ViewFilter.PDFS).center())
        self.assertEqual(self.nav.location.view, ViewFilter.PDFS)
        self.assertEqual(self.titles(), ["Research paper"])
        asked = []
        self.nav.ask_import_files = lambda: asked.append(1) or []
        import_rect = sidebar._layout()[[i.label for i in SIDEBAR].index("Import")]
        click(sidebar, import_rect.center())
        self.assertEqual(asked, [1])                                   # Import opens the file dialog
        self.assertEqual(self.nav.location.view, ViewFilter.PDFS)     # and is not a view
        settings_rect = sidebar._layout()[[i.label for i in SIDEBAR].index("Settings")]
        click(sidebar, settings_rect.center())
        self.assertEqual(self.nav.location.view, ViewFilter.PDFS)     # disabled: nothing happens
        QTest.mouseMove(sidebar, settings_rect.center())
        self.assertIn("Not available", sidebar.toolTip())
        self.nav.set_writable(False)
        click(sidebar, import_rect.center())
        self.assertEqual(asked, [1])                                   # offline: Import disabled
        QTest.mouseMove(sidebar, QPoint(1, 1))
        QTest.mouseMove(sidebar, import_rect.center())
        self.assertEqual(sidebar.toolTip(), "Not available offline")
        self.assertGreater(sidebar.sizeHint().height(), 300)

    def test_search_sort_and_list_mode(self):
        self.nav.actions.search.setText("notes")
        self.assertEqual(self.titles(), ["Meeting notes"])
        self.assertIsNone(self.nav.sidebar.current)
        self.nav.actions.search.setText("notes!")                       # refines, no new history
        self.assertEqual(len(self.nav.history), 1)
        self.nav.back()
        self.assertEqual(self.nav.actions.search.text(), "")
        self.nav.set_sort(SortOrder.ALPHABETICAL)
        self.assertEqual(self.titles(), ["Personal", "Work", "A Short Story", "Sketchbook"])
        self.nav.crumb_row.mode.click()
        self.assertEqual(self.nav.mode, LIST)
        rows = self.nav.view.items()
        self.assertTrue(all(r.rect.height() == 80 for r in rows))
        self.assertFalse(self.nav.grab().isNull())
        self.nav.crumb_row.mode.click()
        self.assertEqual(self.nav.mode, GRID)

    def test_empty_states(self):
        self.nav.navigate(Location(ViewFilter.FAVORITES))
        self.nav.set_tree(DocumentTree())
        self.assertEqual(self.nav.view.empty[1], "Collect your best content")
        self.nav.set_loading(True)
        self.assertEqual(self.nav.view.empty[1], "Loading your library…")
        self.nav.set_loading(False)
        self.nav.set_tree(self.tree, now=NOW)
        self.nav.actions.search.setText("zzz")
        self.assertEqual(self.nav.view.empty[1], "No results")
        self.nav.navigate(Location(folder_id=IDS["Personal"]), clear_search=True)
        self.nav.set_tree(DocumentTree([make_node(IDS["Personal"], Metadata("Personal", "CollectionType"))]))
        self.assertEqual(self.nav.view.empty[1], "This folder is empty")
        self.assertFalse(self.nav.grab().isNull())

    def test_tree_update_keeps_valid_location(self):
        self.nav.navigate(Location(folder_id=IDS["Work"]))
        self.nav.set_tree(self.tree, now=NOW)
        self.assertEqual(self.nav.location, Location(folder_id=IDS["Work"]))
        remaining = [n for n in self.tree.nodes.values() if n.id not in (IDS["Work"], IDS["Projects"])]
        self.nav.set_tree(DocumentTree(remaining), now=NOW)
        self.assertEqual(self.nav.location, Location())                 # folder vanished → view root

    def test_paint_paths_hover_scroll_and_leave(self):
        self.nav.navigate(Location(ViewFilter.TAGS))
        self.assertFalse(self.nav.grab().isNull())                       # tag tiles
        self.nav.navigate(Location(ViewFilter.NOTEBOOKS))
        view = self.nav.view
        QTest.mouseMove(view.viewport(), self.entry_pos("Sketchbook"))
        self.assertFalse(self.nav.grab().isNull())                       # hovered cell
        QTest.mouseMove(view.viewport(), QPoint(5, view.viewport().height() - 5))
        self.assertEqual(view._hover, -1)                                # off any item
        QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                         self.entry_pos("Sketchbook"))                   # right click: nothing
        self.nav.set_mode(LIST)
        QTest.mouseMove(view.viewport(), self.entry_pos("Sketchbook"))
        self.assertFalse(self.nav.grab().isNull())                       # hovered row
        QApplication.sendEvent(view, QEvent(QEvent.Type.Leave))
        QApplication.sendEvent(self.nav.sidebar, QEvent(QEvent.Type.Leave))
        self.assertEqual((view._hover, self.nav.sidebar._hover), (-1, -1))
        # A short view: rows outside the viewport are skipped, scrolling repaints.
        self.nav.resize(700, 260)
        QTest.qWait(20)
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        self.assertGreater(view.verticalScrollBar().maximum(), 0)
        self.assertFalse(self.nav.grab().isNull())

    def test_empty_state_description_and_no_tree(self):
        self.nav.navigate(Location(ViewFilter.FAVORITES))
        self.nav.set_tree(DocumentTree())
        self.assertIn("favorite", self.nav.view.empty[2])
        self.assertFalse(self.nav.grab().isNull())                       # paints the description
        self.nav.view.set_entries([], GRID, None)
        self.assertFalse(self.nav.grab().isNull())                       # nothing to paint
        bare = Navigator(None)
        self.addCleanup(dispose, bare)
        bare.reload()                                                    # no tree yet: no-op
        self.assertEqual(bare.view.items(), [])

    def test_hover_and_trash_sort_label(self):
        QTest.mouseMove(self.nav.view.viewport(), self.entry_pos("Work"))
        self.assertGreaterEqual(self.nav.view._hover, 0)
        self.nav.navigate(Location(ViewFilter.TRASH))
        self.assertEqual(self.nav.crumb_row.sort.text(), "Date removed")


class ViewerAndWindowTests(DemoCase):
    def setUp(self):
        super().setUp()
        self.window = MainWindow(self.source, self.dir, mode_note="Test")
        self.window.resize(1100, 800)
        self.window.show()

    def tearDown(self):
        dispose(self.window)
        super().tearDown()

    def settle(self):
        v = self.window.viewer
        settle(self.window.syncer.pool, v._open_pool, v.renderer.pool, self.window.thumbnails.pool)

    def test_start_from_cache_then_sync(self):
        self.window.start(sync=False)
        self.assertIn(f"{len(self.tree.documents())} documents", self.window.status.text())
        self.assertTrue(self.window.status.text().startswith("Test · "))
        updated = build_demo_cloud()
        updated.put_item(demo_id("Fresh"), {"visibleName": "Fresh", "parent": "", "type": "DocumentType",
                                                "lastModified": str(int(NOW.timestamp() * 1000))},
                            {"fileType": "notebook", "formatVersion": 2, "cPages": {"pages": []}})
        self.auth.transport = updated
        self.window.navigator.actions.sync.click()
        self.assertIn("Syncing", self.window.status.text())
        self.settle()
        self.assertIn("synced", self.window.status.text())
        self.assertIn("Fresh", [i.entry.title for i in self.window.navigator.view.items()])

    def test_sync_failure_keeps_cached_library(self):
        self.window.start(sync=False)

        def offline(*args):
            raise TimeoutError("no network")
        self.auth.transport = offline
        self.window.sync()
        self.settle()
        self.assertIn("Sync failed", self.window.status.text())
        self.assertTrue(self.window.navigator.actions.sync.isEnabled())
        self.assertTrue(self.window.navigator.view.items())

    def test_viewer_pages_zoom_and_keys(self):
        self.window.start(sync=False)
        viewer = self.window.viewer
        self.window.open_document(self.doc("Research paper"))
        self.assertIs(self.window.stack.currentWidget(), viewer)
        self.settle()
        self.assertEqual(viewer.page_label.text(), "1 / 3")
        self.assertFalse(viewer.prev.isEnabled())
        first = viewer.canvas.image
        self.assertIsNotNone(first)
        QTest.keyClick(viewer, Qt.Key.Key_Right)
        self.settle()
        self.assertEqual(viewer.page_label.text(), "2 / 3")
        QTest.keyClick(viewer, Qt.Key.Key_End)
        self.settle()
        self.assertEqual(viewer.index, 2)
        self.assertFalse(viewer.next.isEnabled())
        QTest.keyClick(viewer, Qt.Key.Key_Home)
        QTest.keyClick(viewer, Qt.Key.Key_PageDown)
        QTest.keyClick(viewer, Qt.Key.Key_PageUp)
        self.settle()
        self.assertEqual(viewer.index, 0)
        width = viewer.canvas.page_size().width()
        viewer.zoom_in.click()
        self.settle()
        self.assertEqual(viewer.zoom_label.text(), "125%")
        self.assertGreater(viewer.canvas.page_size().width(), width)
        viewer.step_zoom(-10)
        self.assertEqual(viewer.zoom, 0.5)
        self.assertFalse(viewer.zoom_out.isEnabled())
        viewer.fit.click()
        self.assertEqual(viewer.zoom, 1.0)
        viewer.resize(900, 700)
        QTest.qWait(200)
        self.settle()
        QTest.keyClick(viewer, Qt.Key.Key_A)                     # unhandled keys pass through
        QTest.keyClick(viewer, Qt.Key.Key_Escape)
        self.assertIs(self.window.stack.currentWidget(), self.window.navigator)

    def test_long_page_scrolls_full_height(self):
        self.window.start(sync=False)
        viewer = self.window.viewer
        self.window.open_document(self.doc("Long page"))
        self.settle()
        size = viewer.canvas.page_size()
        self.assertGreater(size.height() / size.width(), 3000 / 1404 * 0.95)   # not clipped to 3:4
        self.assertGreater(viewer.scroll.verticalScrollBar().maximum(), 0)
        self.assertFalse(viewer.grab().isNull())

    def test_opens_at_last_opened_page_and_handles_errors(self):
        self.window.start(sync=False)
        viewer = self.window.viewer
        notes = self.doc("Meeting notes")
        notes.metadata.last_opened_page = 1
        self.window.open_document(notes)
        self.settle()
        self.assertEqual(viewer.page_label.text(), "2 / 2")
        empty = make_node(demo_id("empty"), Metadata("Empty"), Content.parse({"fileType": "notebook"}))
        self.window.open_document(empty)
        self.settle()
        self.assertIn("no pages", viewer.canvas.message)
        missing = make_node(demo_id("missing"), Metadata("Missing"), Content.parse({"fileType": "pdf"}),
                            files={f"{demo_id('missing')}.pdf": "a" * 64})
        self.window.open_document(missing)
        self.settle()
        self.assertIn("Could not open", viewer.canvas.message)
        viewer.go_to(5)                                           # no pages: ignored
        viewer._render()                                          # no pages: ignored
        viewer._on_failed(viewer._token, "boom")
        self.assertIn("boom", viewer.canvas.message)
        viewer._on_rendered(-1, None)                             # stale: ignored
        self.assertFalse(viewer.grab().isNull())

    def test_export_button_pdf_rmdoc_cancel_and_failure(self):
        self.window.start(sync=False)
        viewer = self.window.viewer
        self.window.open_document(self.doc("Meeting notes"))
        self.settle()
        answers = [(str(self.dir / "notes.pdf"), "pdf"), (str(self.dir / "notes.rmdoc"), "rmdoc"), None]
        viewer.ask_export_path = lambda doc: answers.pop(0)
        viewer.export_button.click()
        self.assertFalse(viewer.export_button.isEnabled())
        settle(viewer.exporter.pool)
        self.assertTrue((self.dir / "notes.pdf").exists())
        self.assertIn("Exported to", self.window.status.text())
        self.assertTrue(viewer.export_button.isEnabled())
        viewer.export_button.click()
        settle(viewer.exporter.pool)
        self.assertTrue((self.dir / "notes.rmdoc").exists())
        viewer.export_button.click()                                   # cancelled: nothing happens
        settle(viewer.exporter.pool)
        self.assertFalse(viewer.exporter.running)
        viewer.source = LibrarySource(SyncClient(self.auth, self.dir / "empty"), cached_only=True)
        viewer.exporter.source = viewer.source
        viewer.ask_export_path = lambda doc: (str(self.dir / "fail.rmdoc"), "rmdoc")
        viewer.export_document()
        settle(viewer.exporter.pool)
        self.assertIn("Export failed", self.window.status.text())
        viewer.close_document()
        viewer.export_document()                                       # no document: ignored

    def test_ask_export_path_uses_the_chosen_filter(self):
        import ui.viewer as viewer_module
        viewer = self.window.viewer
        replies = [("/x/notes", RMDOC_FILTER), ("/x/notes.pdf", PDF_FILTER), ("/x/notes", PDF_FILTER), ("", "")]
        original = viewer_module.QFileDialog.getSaveFileName
        viewer_module.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: replies.pop(0))
        try:
            doc = self.doc("Meeting notes")
            self.assertEqual(viewer.ask_export_path(doc), ("/x/notes.rmdoc", "rmdoc"))
            self.assertEqual(viewer.ask_export_path(doc), ("/x/notes.pdf", "pdf"))
            self.assertEqual(viewer.ask_export_path(doc), ("/x/notes.pdf", "pdf"))
            self.assertIsNone(viewer.ask_export_path(doc))
        finally:
            viewer_module.QFileDialog.getSaveFileName = original

    def test_switching_documents_drops_stale_open(self):
        self.window.start(sync=False)
        viewer = self.window.viewer
        self.window.open_document(self.doc("Research paper"))
        self.window.open_document(self.doc("Sketchbook"))        # before the first finished
        self.settle()
        self.assertEqual(viewer.title.text(), "Sketchbook")
        self.assertEqual(viewer.page_label.text(), "1 / 1")
        viewer.close_document()
        self.assertIsNone(viewer.pages)


# ------------------------------------------------------------------------ templates

def template_image(name_or_template, area=(0, 0, 1404, 1872)):
    """Render a template on white paper; returns the QImage (1 px = 1 page px)."""
    from PySide6.QtGui import QImage, QPainter
    template = default_library().get(name_or_template) if isinstance(name_or_template, str) else name_or_template
    x, y, w, h = area
    image = QImage(int(w), int(h), QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.translate(-x, -y)
    try:
        paint_template(painter, template, area)
    finally:
        painter.end()
    return image


def dark(image, x, y, radius=1) -> bool:
    return min(image.pixelColor(x, yy).lightness() for yy in range(max(0, y - radius), min(image.height(), y + radius + 1))) < 200


def dark_columns(image, y) -> list[int]:
    return [x for x in range(image.width()) if image.pixelColor(x, y).lightness() < 200]


def runs(positions: list[int]) -> list[int]:
    """Collapse adjacent positions (antialiased line widths) to their first index."""
    return [p for i, p in enumerate(positions) if i == 0 or p != positions[i - 1] + 1]


class TemplateExpressionTests(unittest.TestCase):
    def test_grammar(self):
        env = {"a": 5.0, "b": 3.0, "c": -1.0, "templateWidth": 1404.0, "mobileMaxWidth": 1000.0}
        cases = {"1 + 2 * 3": 7, "(1 + 2) * 3": 9, "-a + 2": -3, "10 % 4": 2, "a / 2": 2.5,
                 "a > b ? 1 : c < 0 ? 2 : 3": 1, "a < b ? 1 : c < 0 ? 2 : 3": 2,
                 "a < b || c < 0": True, "a > b && c > 0": False, "a == 5": True, "a != 5": False,
                 "a <= 5": True, "a >= 6": False, "!(a > b)": False, "+a": 5,
                 "templateWidth > mobileMaxWidth ? 0 : 160": 0}
        for text, expected in cases.items():
            self.assertEqual(compile_expression(text)(env), expected, text)
        self.assertEqual(evaluate(True, {}), 1.0)
        self.assertEqual(evaluate(52, {}), 52.0)

    def test_errors(self):
        for text, message in (("nope + 1", "unknown variable"), ("a ? 1", "missing ':'"), ("a $ b", "illegal"),
                              ("1 2", "unexpected"), ("", "empty"), ("(1 + 2", r"missing '\)'"), ("*", "illegal")):
            with self.assertRaisesRegex(TemplateError, message, msg=text):
                compile_expression(text)({"a": 1.0})
        with self.assertRaises(TemplateError):
            evaluate([1], {})

    def test_repeat_offsets(self):
        self.assertEqual(_repeat_offsets(None, 0, 10, 0, 100, {}, "down", "up"), [0])
        self.assertEqual(_repeat_offsets("down", 0, 25, 0, 100, {}, "down", "up"), [0, 1, 2, 3, 4])
        self.assertEqual(_repeat_offsets("up", 100, 25, 0, 200, {}, "down", "up"), [-4, -3, -2, -1, 0])
        self.assertEqual(_repeat_offsets("infinite", 10, 25, 0, 60, {}, "down", "up"), [-1, 0, 1, 2])
        self.assertEqual(_repeat_offsets("n * 2", 0, 25, 0, 60, {"n": 2.0}, "down", "up"), [0, 1, 2, 3])
        self.assertEqual(_repeat_offsets("down", 500, 25, 0, 100, {}, "down", "up"), [0])     # below the area
        self.assertEqual(_repeat_offsets("infinite", 0, 0, 0, 100, {}, "down", "up"), [0])    # zero step

    def test_pagedata(self):
        self.assertEqual(parse_pagedata(b"Blank\nP Lines small\n"), ["Blank", "P Lines small"])


class TemplateRenderingTests(unittest.TestCase):
    def test_every_shipped_template_renders(self):
        library = default_library()
        names = library.names()
        self.assertEqual(len(names), 65)
        for name in names:
            image = template_image(name, (0, 0, 351, 468))       # top-left quarter is enough to run it
            self.assertFalse(image.isNull(), name)
        self.assertIsNone(library.get("Blank"))
        self.assertIsNone(library.get(""))
        with self.assertLogs("rm.templates", "WARNING"):
            self.assertIsNone(library.get("P Hexagon small"))      # listed by the app, no file shipped
        self.assertIsNone(library.get("P Hexagon small"))           # cached, warned once

    def test_lined(self):
        image = template_image("P Lines small")
        ys = runs([y for y in range(image.height()) if image.pixelColor(700, y).lightness() < 200])
        inner = [y for y in ys if 0 < y < image.height() - 1]
        self.assertEqual(ys[0], 0)                                   # first rule at the top (offset 0 on desktop)
        self.assertEqual(len(inner), 35)                             # 52, 104, … 1820
        # every rule within 1 px (antialiasing) of a multiple of 52
        self.assertTrue(all(abs(y - 52 * round(y / 52)) <= 1 for y in ys), ys)
        self.assertFalse(dark_columns(image, ys[0] + 26))            # nothing between rules
        self.assertTrue(dark(image, 5, ys[3]) and dark(image, 1398, ys[3]))   # full width

    def test_grid(self):
        image = template_image("P Grid medium")
        def ruled(count, sample, size):          # lines dark across >= 90% of the page
            return runs([i for i in range(count) if sum(sample(i, j) for j in range(0, size, 6)) > 0.9 * size / 6])
        xs = ruled(image.width(), lambda x, y: image.pixelColor(x, y).lightness() < 200, image.height())
        ys = ruled(image.height(), lambda y, x: image.pixelColor(x, y).lightness() < 200, image.width())
        x_steps = {b - a for a, b in zip(xs, xs[1:])}
        y_steps = {b - a for a, b in zip(ys, ys[1:])}
        self.assertGreater(len(xs), 10)
        self.assertGreater(len(ys), 10)
        self.assertLessEqual(max(x_steps) - min(x_steps), 1)          # regular spacing
        self.assertAlmostEqual(sum(x_steps) / len(x_steps), sum(y_steps) / len(y_steps), delta=1)   # square cells

    def test_margin(self):
        image = template_image("P Margin medium")
        columns = [x for x in range(image.width())
                   if sum(image.pixelColor(x, y).lightness() < 200 for y in range(0, image.height(), 8)) > 0.9 * image.height() / 8]
        self.assertTrue(columns)                                      # a vertical margin rule
        self.assertLess(max(columns), 500)                           # on the left side
        rows = runs([y for y in range(image.height()) if image.pixelColor(1000, y).lightness() < 200])
        self.assertGreater(len(rows), 20)                             # plus horizontal ruling

    def test_dots_do_not_alias_when_downscaled(self):
        from PySide6.QtGui import QImage, QPainter
        image = QImage(281, 375, QImage.Format.Format_RGB32)          # ×0.2, like a thumbnail
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.scale(0.2, 0.2)
        paint_template(painter, default_library().get("P Dots S"), (0, 0, 1404, 1872))
        painter.end()
        # Every band of the page has (faint) dots, instead of a moiré of a few rows.
        bands = [any(image.pixelColor(x, y).lightness() < 255 for x in range(281) for y in range(b, b + 25))
                 for b in range(0, 350, 25)]
        self.assertTrue(all(bands), bands)
        native = template_image("P Dots S", (0, 0, 200, 200))           # ×1: crisp, aliased squares
        self.assertEqual(native.pixelColor(round(702 - 22 + 1 - 42.5 * 12), 0).name(), "#000000")

    def test_text_labels(self):
        image = template_image("P Week", (0, 0, 1404, 300))
        self.assertTrue(any(image.pixelColor(x, y).lightness() < 128 for x in range(500, 900, 2) for y in range(0, 200, 2)))

    def test_repeats_fill_an_extended_area(self):
        image = template_image("P Lines small", (0, 1800, 1404, 1300))
        self.assertTrue(any(dark(image, 700, y - 1800) for y in (2964,)))  # rule 57 at y = 52 * 57

    def test_template_errors(self):
        def tpl(items, constants=None, version=1):
            import json
            return parse_template(json.dumps({"formatVersion": version, "constants": constants or [], "items": items}))
        path = {"type": "path", "data": ["M", 0, 0, "L", 10, 10]}
        group = lambda children, **kw: {"type": "group", "boundingBox": {"x": 0, "y": 0, "width": 10, "height": 10},
                                        "children": children, **kw}
        for bad, message in (
                ([dict(path, data=[])], "empty or missing"), ([dict(path, data=["Q", 1, 2])], "invalid command"),
                ([dict(path, data=["M", 1])], "needs 2 values"), ([dict(path, strokeColor="red")], "invalid color"),
                ([dict(path, strokeColor="#00000000")], "fully invisible"),
                ([{"type": "text", "text": "", "fontSize": 10, "position": {"x": 0, "y": 0}}], "cannot be empty"),
                ([{"type": "text", "text": "a", "fontSize": 0, "position": {"x": 0, "y": 0}}], "positive"),
                ([{"type": "text", "text": "a", "fontSize": 10}], "position"),
                ([{"type": "group", "children": [path]}], "bounds"), ([group([])], "children"),
                ([{"type": "circle"}], "unknown item type"),
                ([group([path], repeat={"rows": "infinite", "columns": "infinite"})] * 1, None)):
            if message is None:
                continue
            with self.assertRaisesRegex(TemplateError, message):
                template_image(tpl(bad), (0, 0, 50, 50))
        tiny = {"type": "group", "boundingBox": {"x": 0, "y": 0, "width": 0.01, "height": 0.01},
                "repeat": {"rows": "infinite", "columns": "infinite"}, "children": [path]}
        with self.assertRaisesRegex(TemplateError, "repeats too many times"):
            template_image(tpl([tiny]), (0, 0, 50, 50))
        filled = dict(path, data=["M", 0, 0, "L", 40, 0, "L", 40, 40, "Z"], fillColor="#000000", strokeColor="#ffffff",
                      antialiasing=False, strokeWidth=2)
        self.assertTrue(dark(template_image(tpl([filled]), (0, 0, 50, 50)), 30, 10))
        curve = dict(path, data=["M", 0, 25, "C", 10, 0, 40, 50, 50, 25])
        self.assertFalse(template_image(tpl([curve]), (0, 0, 50, 50)).isNull())
        for bad_json, message in (("{", "failed to parse"), ('{"formatVersion": 2}', "incompatible version"),
                                  ('{"formatVersion": 1, "constants": [{"a": 1, "b": 2}], "items": []}', "single"),
                                  ('{"formatVersion": 1}', "missing 'items'")):
            with self.assertRaisesRegex(TemplateError, message):
                parse_template(bad_json)

    def test_library_directory_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Mine.template").write_text('{"formatVersion": 1, "name": "Mine", "items": []}')
            library = TemplateLibrary(Path(tmp))
            self.assertEqual(library.names(), ["Mine"])
            self.assertEqual(library.get("Mine").name, "Mine")


class PageTemplateIntegrationTests(DemoCase):
    def test_templates_per_page(self):
        self.assertEqual([p.template for p in document_pages(self.doc("Meeting notes"))], ["P Lines small", "Blank"])
        self.assertEqual([p.template for p in document_pages(self.doc("Research paper"))],
                         ["Blank", "P Grid medium", "Blank"])

    def test_pagedata_fallback_for_old_documents(self):
        doc_id = demo_id("legacy")
        pagedata = b"P Grid small\nBlank\n"
        from fakecloud import sha
        self.sync.blobs.put(sha(pagedata), pagedata)
        legacy = make_node(doc_id, Metadata("Legacy"), Content.parse({"fileType": "notebook", "formatVersion": 1,
                                                                       "pages": ["a", "b"]}),
                           files={f"{doc_id}.pagedata": sha(pagedata)})
        self.assertEqual([p.template for p in document_pages(legacy, pagedata=self.source.pagedata(legacy))],
                         ["P Grid small", "Blank"])
        pages = DocumentPages(self.source, legacy)
        self.assertEqual([p.template for p in pages.pages], ["P Grid small", "Blank"])
        self.assertIsNotNone(pages.content(0).template)
        offline = LibrarySource(SyncClient(self.auth, self.dir / "empty"), cached_only=True)
        with self.assertLogs("rm.ui.pages", "INFO"):
            self.assertEqual([p.template for p in DocumentPages(offline, legacy).pages], ["", ""])
        self.assertIsNone(self.source.pagedata(self.doc("Sketchbook")))

    def test_viewer_and_thumbnail_show_paper(self):
        pages = DocumentPages(self.source, self.doc("Meeting notes"))
        try:
            lined, blank = pages.render(0, 1.0, crop="clip"), pages.render(1, 1.0, crop="clip")
            self.assertTrue(dark(lined, 1300, 52 * 34))               # a rule in an empty corner
            self.assertFalse(dark(blank, 1300, 52 * 34))
        finally:
            pages.close()
        paper = DocumentPages(self.source, self.doc("Research paper"))
        try:
            frame = paper.content(0).frame()
            on_pdf = paper.render(0, 1.0, crop="clip")
            self.assertFalse(dark(on_pdf, round(1404 - frame.x()), 1600))   # no template over a PDF page
            grid = paper.render(1, 1.0, crop="clip")                          # inserted page: P Grid medium
            self.assertGreater(len(runs(dark_columns(grid, 1700))), 10)
        finally:
            paper.close()
        service = ThumbnailService(self.source, self.dir, width=280)
        service.thumbnail(self.doc("Meeting notes"))
        settle(service.pool)
        thumb = service.thumbnail(self.doc("Meeting notes"))
        rule, between = round(52 * 34 * 280 / 1404), round(52 * 34.5 * 280 / 1404)
        faint = lambda y: min(thumb.pixelColor(260, yy).lightness() for yy in (y - 1, y, y + 1))
        self.assertLess(faint(rule), faint(between))                 # rules survive downscaling (faintly)
        self.assertEqual(faint(between), 255)
        self.assertTrue(any(p.name.endswith("-v2.png") for p in (self.dir / "thumbnails").iterdir()))


# ------------------------------------------------------------------------- export

def mismatch(a: QImage, b: QImage, step: int = 3) -> float:
    """Fraction of pixels where any channel differs by more than 96."""
    a, b = (im.convertToFormat(QImage.Format.Format_RGB32) for im in (a, b))
    da, db, bpl = bytes(a.constBits()), bytes(b.constBits()), a.bytesPerLine()
    bad = total = 0
    for y in range(0, a.height(), step):
        for i in range(y * bpl, y * bpl + a.width() * 4, 4 * step):
            total += 1
            bad += max(abs(da[i] - db[i]), abs(da[i + 1] - db[i + 1]), abs(da[i + 2] - db[i + 2])) > 96
    return bad / total


class StubPages:
    """DocumentPages over a given PDF: `pages` is a list of (scene, PDF page index)."""

    def __init__(self, pdf_path: Path, pages: list) -> None:
        self.pdf_path = pdf_path
        self.pdf = PdfFile(pdf_path)
        self.doc = SimpleNamespace(name="Stub")
        self.pages = pages

    def content(self, index: int) -> PageContent:
        scene, pdf_page = self.pages[index]
        return PageContent(scene, self.pdf, pdf_page)

    def close(self) -> None:
        self.pdf.close()


class ExportTests(DemoCase):
    def export(self, name: str, **kw):
        pages = DocumentPages(self.source, self.doc(name))
        try:
            return export_pdf(pages, self.dir / f"{name}.pdf", **kw)
        finally:
            pages.close()

    def pages(self, name: str) -> DocumentPages:
        pages = DocumentPages(self.source, self.doc(name))
        self.addCleanup(pages.close)
        return pages

    def open_pdf(self, path) -> PdfFile:
        pdf = PdfFile(path)
        self.addCleanup(pdf.close)
        return pdf

    @staticmethod
    def render_page(pdf: PdfFile, index: int, px_per_pt: float = 1.0) -> QImage:
        size = pdf.page_size(index)
        return pdf.render(index, QSize(round(size.width() * px_per_pt), round(size.height() * px_per_pt)))

    def shapes_pdf(self) -> Path:
        """A demo page four times: cropped, and turned 90/180/270 with offset crops."""
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
        reader = PdfReader(io.BytesIO(make_pdf("Shapes", [[LOREM] * 4])))
        writer = PdfWriter()
        for rotation, crop in ((0, (20, 40, 575, 770)), (90, (30, 60, 560, 800)),
                               (180, (0, 0, 595, 842)), (270, (40, 30, 590, 812))):
            page = writer.add_page(reader.pages[0])
            page.cropbox = RectangleObject(crop)
            if rotation:
                page.rotate(rotation)
        path = self.dir / "shapes.pdf"
        writer.write(path)
        return path

    @staticmethod
    def annotations(*extra):
        page = PageBuilder()
        page.handwriting("note", 300, 500, size=60)
        page.stroke([(200, 330), (900, 330)], tool=18, color=3, width=40)        # highlighter
        page.stroke([(250, 900), (1100, 1000)], color=6, width=4)
        for points in extra:
            page.stroke(points, color=7, width=5)
        return parse(page.bytes())

    def assert_matches_viewer(self, pages, path, px_per_pt: float = 1.5):
        """Exported pages, rasterised by PDFium, look like the viewer's pages. Compare
        template pages at 226/72 px per pt: scaled down, PDFium keeps 1 px rules
        solid where QPainter fades them."""
        pdf = self.open_pdf(path)
        self.assertEqual(pdf.page_count, len(pages.pages))
        for index in range(len(pages.pages)):
            viewer = render_content(pages.content(index), px_per_pt * 72 / 226, "page")
            exported = self.render_page(pdf, index, px_per_pt)
            self.assertLess(abs(exported.width() - viewer.width()), 2, index)
            self.assertLess(abs(exported.height() - viewer.height()), 2, index)
            self.assertLess(mismatch(viewer, exported.scaled(viewer.size())), 0.01, index)

    def test_notebook_export_is_vector_with_template_and_highlight(self):
        progress = []
        path = self.export("Meeting notes", progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(1, 2), (2, 2)])
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertNotIn(b"/Subtype /Image", data)                   # strokes + template are vector
        self.assertIn(b"/BM /Darken", data)
        pdf = self.open_pdf(path)
        self.assertEqual(pdf.page_count, 2)
        size = pdf.page_size(0)
        self.assertAlmostEqual(size.width(), 1404 * 72 / 226, places=2)      # exactly the tablet page
        self.assertAlmostEqual(size.height(), 1872 * 72 / 226, places=2)
        page = self.render_page(pdf, 0, 226 / 72)                    # 1 px per page pixel
        self.assertTrue(dark(page, 1300, 52 * 34))                   # template rule
        self.assertEqual(page.pixelColor(560, 250).name(), "#ffed75")   # highlighter right of the title
        self.assertEqual(page.pixelColor(560, 260).name(), "#7f7f75")   # rule under it: Darken, as on screen
        title_dark = min(page.pixelColor(x, y).lightness() for x in range(170, 420, 2) for y in range(170, 240, 2))
        self.assertLess(title_dark, 80)                               # ink stays on top of the highlight
        self.assert_matches_viewer(self.pages("Meeting notes"), path, 226 / 72)

    def test_pdf_export_keeps_the_original_pages_as_vectors(self):
        from pypdf import PdfReader
        path = self.export("Research paper")
        data = path.read_bytes()
        self.assertNotIn(b"/Subtype /Image", data)                   # nothing rasterised
        exported, original = PdfReader(path), PdfReader(self.source.pdf_path(self.doc("Research paper")))
        self.assertEqual(len(exported.pages), 3)
        self.assertEqual(list(exported.pages[0].mediabox), list(original.pages[0].mediabox))
        text = lambda page: page.extract_text().strip()
        self.assertEqual(text(exported.pages[0]), text(original.pages[0]))       # selectable, all of it
        self.assertEqual(text(exported.pages[2]), text(original.pages[1]))
        self.assertIn("The reMarkable stores every page", exported.pages[0].extract_text().replace("\t", " "))
        pdf = self.open_pdf(path)
        self.assertAlmostEqual(pdf.page_size(1).width(), 1404 * 72 / 226, places=2)   # inserted note page
        page = self.render_page(pdf, 0)
        self.assertEqual(page.pixelColor(250, 70).name(), "#ffed75")          # highlight, in PDF points
        darkest = min(page.pixelColor(x, y).lightness() for x in range(62, 120) for y in range(62, 80))
        self.assertLess(darkest, 80)                                          # title text still dark
        self.assertGreater(len(runs(dark_columns(self.render_page(pdf, 1), 300))), 5)   # grid on the note page
        self.assert_matches_viewer(self.pages("Research paper"), path, 226 / 72)

    def test_rotated_cropped_grown_and_repeated_pages_match_the_viewer(self):
        from pypdf import PdfReader
        source = self.shapes_pdf()
        pages = StubPages(source, [
            (self.annotations([(100, -60), (600, -40)]), 0),      # ink above the page: grows up
            (self.annotations([(1950, 300), (2000, 800)]), 1),    # beyond the right edge
            (self.annotations(), 2),
            (self.annotations([(-500, 1200), (-450, 1300)]), 3),  # beyond the left edge
            (None, 0),                                            # the first page again, no ink
        ])
        self.addCleanup(pages.close)
        path = export_pdf(pages, self.dir / "shapes-export.pdf")
        self.assertNotIn(b"/Subtype /Image", path.read_bytes())
        exported = PdfReader(path)
        self.assertEqual([p.rotation for p in exported.pages], [0, 90, 180, 270, 0])
        self.assertGreater(float(exported.pages[0].cropbox.top), 770)        # grown above the crop
        self.assertEqual(list(exported.pages[4].cropbox), [20, 40, 575, 770])    # untouched repeat
        self.assert_matches_viewer(pages, path)       # includes the clip: the cut title stays cut

    def test_unreadable_source_pdf_is_rasterised_with_the_same_result(self):
        from pypdf.errors import PdfReadError
        real = export_module.PdfReader

        def damaged(source, *args, **kw):          # the source PDF fails, the ink layers don't
            if isinstance(source, (str, Path)):
                raise PdfReadError("damaged xref table")
            return real(source, *args, **kw)

        with mock.patch.object(export_module, "PdfReader", side_effect=damaged), \
                self.assertLogs("rm.ui.export", "WARNING") as logs:
            path = self.export("Research paper")
        self.assertIn("rasterising", logs.output[0])
        self.assertIn(b"/Subtype /Image", path.read_bytes())
        page = self.render_page(self.open_pdf(path), 0)
        self.assertEqual(page.pixelColor(250, 70).name(), "#ffed75")
        self.assertLess(min(page.pixelColor(x, y).lightness() for x in range(62, 120) for y in range(62, 80)), 80)

    def test_source_pdf_encryption(self):
        from pypdf import PdfReader, PdfWriter
        plain = make_pdf("Locked", [[LOREM]])
        for name, user_password in (("open.pdf", ""), ("locked.pdf", "secret")):
            writer = PdfWriter(clone_from=PdfReader(io.BytesIO(plain)))
            writer.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")
            writer.write(self.dir / name)
        pages = StubPages(self.dir / "open.pdf", [(self.annotations(), 0)])   # no user password: vector
        self.addCleanup(pages.close)
        path = export_pdf(pages, self.dir / "open-export.pdf")
        self.assertNotIn(b"/Subtype /Image", path.read_bytes())
        self.assertIn("The reMarkable", PdfReader(path).pages[0].extract_text().replace("\t", " "))
        with self.assertLogs("rm.ui.export", "WARNING"):
            self.assertIsNone(export_module._open_source(self.dir / "locked.pdf"))

    def test_page_size_disagreement_falls_back_to_raster(self):
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(612, 792)                               # US Letter, but the page is A4
        letter = io.BytesIO()
        writer.write(letter)
        pdf = PdfFile(self.source.pdf_path(self.doc("Research paper")))
        try:
            with self.assertLogs("rm.ui.export", "WARNING") as logs:
                self.assertIsNone(export_module._source_page(PdfReader(letter), PageContent(None, pdf, 0)))
            self.assertIn("612.0 × 792.0", logs.output[0])
            with self.assertLogs("rm.ui.export", "WARNING") as logs:       # a page pypdf lacks
                self.assertIsNone(export_module._source_page(PdfReader(letter), PageContent(None, pdf, 1)))
            self.assertIn("IndexError", logs.output[0])
            self.assertIsNotNone(export_module._source_page(
                PdfReader(self.source.pdf_path(self.doc("Research paper"))), PageContent(None, pdf, 0)))
        finally:
            pdf.close()

    def test_failed_export_leaves_no_file(self):
        with mock.patch.object(export_module, "_assemble", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.export("Sketchbook")
        self.assertEqual(list(self.dir.glob("Sketchbook.pdf*")), [])

    def test_long_page_export_keeps_full_height(self):
        pdf = self.open_pdf(self.export("Long page"))
        self.assertGreater(pdf.page_size(0).height(), 3000 * 72 / 226)

    def test_export_errors_and_names(self):
        empty = make_node(demo_id("empty"), Metadata("Empty"), Content.parse({"fileType": "notebook"}))
        with self.assertRaisesRegex(ValueError, "no pages"):
            export_pdf(DocumentPages(self.source, empty), self.dir / "x.pdf")
        with self.assertRaises(OSError):
            export_pdf(DocumentPages(self.source, self.doc("Sketchbook")), self.dir / "missing-dir" / "x.pdf")
        with self.assertRaisesRegex(ValueError, "no such pages"):
            export_pdf(DocumentPages(self.source, self.doc("Sketchbook")), self.dir / "x.pdf", indexes=[5])
        self.assertEqual(export_file_name('a/b:c*?"d', ".pdf"), "a_b_c___d.pdf")
        self.assertEqual(export_file_name(" . ", ".rmdoc"), "document.rmdoc")

    def test_exporter_worker(self):
        exporter = Exporter(self.source)
        results = []
        exporter.finished.connect(results.append)
        exporter.failed.connect(results.append)
        self.assertTrue(exporter.export("rmdoc", self.doc("Sketchbook"), str(self.dir / "s")))
        self.assertFalse(exporter.export("pdf", self.doc("Sketchbook"), str(self.dir / "t.pdf")))   # one at a time
        settle(exporter.pool)
        self.assertTrue(results[-1].endswith("s.rmdoc"))
        import zipfile
        with zipfile.ZipFile(results[-1]) as zf:
            self.assertIn(f"{IDS['Sketchbook']}.content", zf.namelist())
        exporter.export("tiff", self.doc("Sketchbook"), str(self.dir / "x"))
        settle(exporter.pool)
        self.assertIn("unknown export format", results[-1])
        offline = Exporter(LibrarySource(SyncClient(self.auth, self.dir / "empty"), cached_only=True))
        offline.failed.connect(results.append)
        offline.export("rmdoc", self.doc("Sketchbook"), str(self.dir / "o"))
        settle(offline.pool)
        self.assertTrue(results[-1].startswith("SyncError"))        # empty offline cache: reported, not raised


# --------------------------------------------------------------------- SVG export

SVG = "{http://www.w3.org/2000/svg}"


def svg_image(svg: str, size: QSize) -> QImage:
    """`svg` drawn by Qt's SVG renderer onto white, at `size` pixels."""
    from PySide6.QtCore import QByteArray, QRectF
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    image = QImage(size, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size.width(), size.height()))
    painter.end()
    return image


def differing(a: QImage, b: QImage, threshold: int = 96, step: int = 3) -> float:
    """Fraction of sampled pixels whose colour differs by more than `threshold` (0-255)."""
    a, b = a.convertToFormat(QImage.Format.Format_RGB32), b.convertToFormat(QImage.Format.Format_RGB32)
    assert a.size() == b.size(), (a.size(), b.size())
    off = n = 0
    for y in range(0, a.height(), step):
        for x in range(0, a.width(), step):
            pa, pb = a.pixel(x, y), b.pixel(x, y)
            n += 1
            off += max(abs(((pa >> s) & 255) - ((pb >> s) & 255)) for s in (0, 8, 16)) > threshold
    return off / n


def template_named(name: str):
    template = default_library().get(name)
    assert template is not None, name
    return template


class SvgTemplateTests(unittest.TestCase):
    def svg(self, template, area=(0, 0, 1404, 1872)) -> str:
        x, y, w, h = area
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" width="{w}" height="{h}">'
                + "".join(template_svg(template, area)) + "</svg>")

    def test_draws_like_painting(self):
        # Lines, grid, dots (filled squares), curves and fills (piano), text (checklist, planner).
        for name in ("P Lines small", "P Grid medium", "P Dots S", "P Piano sheet medium", "P Checklist"):
            template = template_named(name)
            painted = template_image(template)
            drawn = svg_image(self.svg(template), painted.size())
            self.assertLess(differing(painted, drawn), 0.003, template.name)

    def test_compact(self):
        lines = ET.fromstring(self.svg(template_named("P Lines small")))
        paths = lines.findall(f".//{SVG}path")
        self.assertEqual(len(paths), 1)                                   # every rule in one path
        self.assertEqual(paths[0].get("d").count("M"), 37)                # 0, 52, ... 1872
        self.assertEqual((paths[0].get("stroke-linecap"), paths[0].get("fill")), ("square", "none"))
        dots = self.svg(template_named("P Dots S"))
        self.assertLess(len(dots), 120_000)
        group = ET.fromstring(dots).find(f"{SVG}g/{SVG}g")                # the dots share their style
        self.assertEqual(group.get("fill-rule"), "evenodd")
        self.assertGreater(len(group), 1000)
        for path in ET.fromstring(dots).iter(f"{SVG}path"):              # repeats past the page are left out
            values = [float(v) for v in path.get("d").split() if v not in "MLCZ"]
            xs, ys = values[0::2], values[1::2]
            self.assertTrue(max(xs) >= -2 and min(xs) <= 1406 and max(ys) >= -2 and min(ys) <= 1874)

    def test_translucent_lines_stay_separate(self):
        # Merged, crossing translucent lines would not darken where they cross, as Qt draws them.
        template = parse_template(json.dumps({"name": "cross", "formatVersion": 1, "items": [
            {"type": "path", "data": ["M", 0, 50, "L", 100, 50], "strokeColor": "#00000080"},
            {"type": "path", "data": ["M", 50, 0, "L", 50, 100], "strokeColor": "#00000080"}]}))
        root = ET.fromstring(self.svg(template, (0, 0, 100, 100)))
        group = root.find(f"{SVG}g/{SVG}g")
        self.assertEqual((len(group), group.get("stroke-opacity")), (2, "0.502"))


class SvgExportTests(DemoCase):
    def content(self, name: str, index: int) -> PageContent:
        pages = DocumentPages(self.source, self.doc(name))
        self.addCleanup(pages.close)
        return pages.content(index)

    def test_notebook_page(self):
        content = self.content("Sketchbook", 0)
        root = ET.fromstring(page_svg(content))
        self.assertEqual((root.get("viewBox"), root.get("width"), root.get("height")), ("0 0 1404 1872", "1404", "1872"))
        ids = [child.get("id") for child in root]
        self.assertEqual(ids[:3], ["paper", "template", "layer-0"])     # paper, template, then the ink
        strokes = root.findall(f"{SVG}g[@id='layer-0']/{SVG}g")
        self.assertEqual({g.get("stroke") for g in strokes} >= {"#000000", "#304ae0", "#c23132"}, True)
        highlighter = next(g for g in strokes if g.get("data-tool").startswith("highlighter"))
        self.assertEqual(highlighter.get("style"), "mix-blend-mode:darken")
        widths = {float(line.get("stroke-width")) for g in strokes for line in g}
        self.assertGreater(len(widths), 3)                                # widths follow the pen
        self.assertIsNone(root.find(f".//{SVG}image"))                    # no raster part

    def test_typed_text(self):
        root = ET.fromstring(page_svg(self.content("Meeting notes", 1)))
        texts = ["".join(t.itertext()) for t in root.iter(f"{SVG}text")]
        self.assertIn("Action items", texts)

    def test_annotated_pdf_page(self):
        content = self.content("Research paper", 0)
        root = ET.fromstring(page_svg(content))
        image = root.find(f"{SVG}image")
        frame = content.frame()
        self.assertEqual([float(image.get(k)) for k in ("x", "width")],
                         [round(frame.x(), 2), round(frame.width(), 2)])  # the PDF page at 226 DPI
        self.assertTrue(image.get("href").startswith("data:image/png;base64,"))
        png = QImage.fromData(base64.b64decode(image.get("href").split(",", 1)[1]))
        self.assertEqual((png.width(), png.height()), (round(frame.width()), round(frame.height())))
        self.assertEqual([c.get("id") for c in root][:3], ["paper", "pdf-page", "layer-0"])   # ink above the page
        self.assertIsNone(root.find(f"{SVG}g[@id='template']"))

    def test_draws_like_the_viewer(self):
        for name, index in (("Research paper", 1), ("Meeting notes", 0), ("Old draft", 0)):
            content = self.content(name, index)
            viewer = render_content(content, 1.0, "page")
            self.assertLess(differing(viewer, svg_image(page_svg(content), viewer.size())), 0.005, name)

    @unittest.skipUnless(shutil.which("rsvg-convert"), "needs rsvg-convert (librsvg)")
    def test_highlighters_blend_like_the_viewer(self):
        # Qt's SVG renderer ignores mix-blend-mode; librsvg, like browsers, honours it.
        content = self.content("Research paper", 0)                      # highlighter over PDF text
        svg, png = self.dir / "page.svg", self.dir / "page.png"
        svg.write_text(page_svg(content))
        subprocess.run(["rsvg-convert", "-o", str(png), str(svg)], check=True)
        viewer = render_content(content, 1.0, "page")
        self.assertLess(differing(viewer, QImage(str(png))), 0.002)

    def test_export_folder(self):
        pages = DocumentPages(self.source, self.doc("Research paper"))
        self.addCleanup(pages.close)
        steps = []
        folder = export_svg(pages, self.dir / "Research paper", progress=lambda d, t: steps.append((d, t)))
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         [f"Research paper - {n}.svg" for n in (1, 2, 3)])
        self.assertEqual(steps, [(1, 3), (2, 3), (3, 3)])
        picked = export_svg(pages, self.dir / "picked", indexes=[1])
        self.assertEqual([p.name for p in picked.iterdir()], ["Research paper - 2.svg"])

        def stop(done, total):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):                         # stopped: nothing left behind
            export_svg(pages, self.dir / "stopped", progress=stop)
        self.assertEqual(sorted(p.name for p in self.dir.glob("stopped*")), [])


# ----------------------------------------------------------------- library changes

class LibraryChangeTests(DemoCase):
    """Imports, folders, renames, moves, favorites and the trash, from the navigator
    through the background Mutator to the (fake) cloud and back."""

    def setUp(self):
        import copy
        self.cloud = copy.deepcopy(CLOUD)                 # these tests write: never touch the shared one
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.auth = paired_auth(self.dir / "config", self.cloud)
        self.sync = SyncClient(self.auth, self.dir / "cache", sleep=lambda s: None)
        self.source = LibrarySource(self.sync)
        self.tree = self.source.refresh()
        self.query = LibraryQuery(self.tree, now=NOW)
        self.window = MainWindow(self.source, self.dir / "cache", mode_note="Test")
        self.window.resize(1100, 800)
        self.window.show()
        self.window.start(sync=False)
        self.nav = self.window.navigator
        self.statuses = []
        self.window.mutator.started.connect(lambda what: self.statuses.append(f"started {what}"))
        self.window.mutator.progress.connect(lambda what, sent, total: self.statuses.append((sent, total)))
        self.window.mutator.finished.connect(lambda tree, message: self.statuses.append(message))
        self.window.mutator.failed.connect(self.statuses.append)

    def tearDown(self):
        settle(self.window.mutator.pool)
        dispose(self.window)
        super().tearDown()

    def run_changes(self):
        settle(self.window.mutator.pool)

    def shown(self) -> list[str]:
        return [i.entry.title for i in self.nav.view.items()]

    def entry(self, title: str) -> Entry:
        return next(i.entry for i in self.nav.view.items() if i.entry.title == title)

    def remote(self) -> DocumentTree:
        n = len(list(self.dir.glob("reader*")))
        return SyncClient(paired_auth(self.dir / f"reader{n}", self.cloud), self.dir / f"reader{n}" / "cache").refresh()

    def pdf_file(self, name: str) -> Path:
        path = self.dir / name
        path.write_bytes(make_pdf(Path(name).stem, [[LOREM]]))
        return path

    def test_header_actions_follow_the_view(self):
        actions = self.nav.actions
        self.assertTrue(actions.new_folder.isVisibleTo(self.nav) and actions.import_files.isVisibleTo(self.nav))
        self.assertFalse(actions.empty_trash.isVisibleTo(self.nav))
        self.nav.navigate(Location(ViewFilter.TRASH))
        self.assertFalse(actions.new_folder.isVisibleTo(self.nav))
        self.assertTrue(actions.empty_trash.isVisibleTo(self.nav) and actions.empty_trash.isEnabled())
        self.nav.navigate(Location(ViewFilter.PDFS))
        self.assertFalse(actions.import_files.isVisibleTo(self.nav) or actions.empty_trash.isVisibleTo(self.nav))
        offline = MainWindow(LibrarySource(self.sync, cached_only=True), self.dir / "cache")
        offline.start(sync=False)
        self.assertFalse(offline.navigator.actions.new_folder.isEnabled())
        self.assertEqual(offline.navigator.actions.import_files.toolTip(), "Not available offline")
        menu = offline.navigator.context_menu(offline.navigator.query.entry(self.doc("Sketchbook")))
        self.assertFalse(any(a.isEnabled() for a in menu.actions() if not a.isSeparator()))
        self.assertFalse(offline.mutator.run("Rename", lambda sync, _: None, "done"))
        dispose(offline)

    def test_new_folder_in_the_open_folder(self):
        work = self.query.entry(self.tree.get(IDS["Work"]))
        self.nav._activate(work)
        asked = []
        self.nav.ask_text = lambda *args: asked.append(args) or "Reading"
        self.nav.actions.new_folder.click()
        self.run_changes()
        self.assertEqual(asked, [("New folder", "Name", "Folder", "Create")])
        self.assertIn("Reading", self.shown())
        reading = next(n for n in self.remote().folders() if n.name == "Reading")
        self.assertEqual(reading.parent_id, IDS["Work"])
        self.assertEqual(self.statuses[-1], "Created folder Reading")
        self.nav.ask_text = lambda *args: asked.append(args) or None      # cancelled
        self.nav.actions.new_folder.click()
        self.assertEqual(asked[-1][2], "Folder")
        self.nav.navigate(Location())
        self.nav.ask_text = lambda *args: asked.append(args) or "Folder"
        self.nav.new_folder()
        self.run_changes()
        self.nav.new_folder()                                              # "Folder" is taken now
        self.assertEqual(asked[-1][2], "Folder 2")

    def test_import_by_drag_and_drop(self):
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QDragEnterEvent, QDropEvent
        pdf, txt = self.pdf_file("Dropped paper.pdf"), self.dir / "notes.txt"
        txt.write_text("hi")

        def drag(paths, event_type=QDragEnterEvent):
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
            event = event_type(QPoint(200, 200), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.nav, event)
            return event
        self.assertFalse(drag([txt]).isAccepted())                        # only PDFs and EPUBs
        self.assertEqual(self.nav.view.drop_hint, "")
        self.assertTrue(drag([pdf, txt]).isAccepted())
        self.assertEqual(self.nav.view.drop_hint, "Drop file to upload to your reMarkable")
        self.nav.view.grab()                                              # paints the overlay
        drag([pdf, txt], QDropEvent)
        self.assertEqual(self.nav.view.drop_hint, "")
        self.run_changes()
        self.assertIn("Dropped paper", self.shown())
        self.assertEqual(self.statuses[0], "started Uploading Dropped paper.pdf")
        self.assertEqual(self.statuses[-1], "Imported Dropped paper.pdf")
        sent, total = [s for s in self.statuses if isinstance(s, tuple)][-1]
        self.assertEqual(sent, total)
        doc = next(d for d in self.remote().documents() if d.name == "Dropped paper")
        self.assertEqual((doc.file_type, doc.page_count, doc.parent_id), (FileType.PDF, 1, ""))
        self.nav._activate(self.query.entry(self.tree.get(IDS["Personal"])))
        drag([pdf])
        self.assertEqual(self.nav.view.drop_hint, "Drop file to upload to Personal")
        QApplication.sendEvent(self.nav, QEvent(QEvent.Type.DragLeave))
        self.assertEqual(self.nav.view.drop_hint, "")

    def test_import_button_uploads_into_the_open_folder(self):
        from test_sync_offline import epub_bytes
        epub = self.dir / "Novel.epub"
        epub.write_bytes(epub_bytes())
        self.nav._activate(self.query.entry(self.tree.get(IDS["Personal"])))
        self.nav.ask_import_files = lambda: [str(self.pdf_file("Report.pdf")), str(epub)]
        self.nav.actions.import_files.click()
        self.run_changes()
        self.assertEqual(sorted(t for t in self.shown() if t in ("Report", "Novel")), ["Novel", "Report"])
        remote = self.remote()
        self.assertEqual({d.parent_id for d in remote.documents() if d.name in ("Report", "Novel")}, {IDS["Personal"]})
        bad = self.dir / "fake.pdf"
        bad.write_bytes(b"not really a pdf")
        self.nav.import_files([str(bad)])
        self.run_changes()
        self.assertEqual(self.statuses[-1], "Uploading fake.pdf failed: fake.pdf is not a PDF or EPUB file")

    def test_context_menu_rename_move_favorite_and_trash(self):
        entry = self.entry("Sketchbook")
        rows = self.nav.entry_actions(entry)
        self.assertEqual([r and r[0] for r in rows], ["Favorite", "Rename…", "Move…", None, "Move to trash"])
        menu = self.nav.context_menu(entry)
        favorite = menu.actions()[0]
        self.assertTrue(favorite.isCheckable() and favorite.isChecked())    # Sketchbook is a favorite
        requested = []
        self.nav.view.contextRequested.connect(lambda e, pos: requested.append(e.title))
        pos = self.nav.view.items()[[i.entry.title for i in self.nav.view.items()].index("Sketchbook")].rect.center()
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QContextMenuEvent
        shown = []
        QTimer.singleShot(0, lambda: (shown.append([a.text() for a in QApplication.activePopupWidget().actions()]),
                                      QApplication.activePopupWidget().close()))
        QApplication.sendEvent(self.nav.view.viewport(), QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, pos, self.nav.view.viewport().mapToGlobal(pos)))
        self.assertEqual(requested, ["Sketchbook"])
        self.assertEqual(shown, [["Favorite", "Rename…", "Move…", "", "Move to trash"]])   # the real menu

        self.nav.ask_text = lambda title, label, text, ok: "Sketches" if (title, text, ok) == ("Rename", "Sketchbook", "Save") else None
        next(a for a in menu.actions() if a.text() == "Rename…").trigger()
        self.run_changes()
        self.assertEqual(self.statuses[-1], "Renamed to Sketches")
        favorite.trigger()                                                 # unfavorite
        self.run_changes()
        self.assertEqual(self.statuses[-1], "Removed Sketchbook from Favorites")
        moving = []
        self.nav.ask_folder = lambda nodes: moving.append([n.name for n in nodes]) or IDS["Personal"]
        self.nav.move([self.tree.get(IDS["Sketchbook"])])
        self.run_changes()
        self.assertEqual((moving, self.statuses[-1]), ([["Sketchbook"]], "Moved to Personal"))
        sketch = self.remote().get(IDS["Sketchbook"])
        self.assertEqual((sketch.name, sketch.pinned, sketch.parent_id), ("Sketches", False, IDS["Personal"]))
        self.assertNotIn("Sketches", self.shown())                         # moved out of My files' root
        self.nav.trash([self.tree.get(IDS["Personal"])])
        self.run_changes()
        self.assertEqual(self.statuses[-1], "1 item was moved to Trash")
        self.assertTrue(self.remote().is_trashed(IDS["Sketchbook"]))
        self.nav.ask_folder = lambda nodes: None                           # cancelled move
        count = len(self.statuses)
        self.nav.move([self.tree.get(IDS["Work"])])
        self.run_changes()
        self.assertEqual(len(self.statuses), count)

    def test_trash_restore_delete_and_empty(self):
        self.nav.navigate(Location(ViewFilter.TRASH))
        draft = self.entry("Old draft")
        self.assertEqual([r and r[0] for r in self.nav.entry_actions(draft)], ["Restore", None, "Delete permanently…"])
        self.nav.restore([draft.node])
        self.run_changes()
        self.assertEqual(self.statuses[-1], "1 item was restored to My files")
        self.assertEqual(self.remote().get(IDS["Old draft"]).parent_id, "")
        self.assertFalse(self.nav.actions.empty_trash.isEnabled())         # nothing left in the trash
        self.nav.trash([self.tree.get(IDS["Old draft"]), self.tree.get(IDS["A Short Story"])])
        self.run_changes()
        self.assertEqual(self.statuses[-1], "2 items were moved to Trash")
        self.assertEqual(sorted(self.shown()), ["A Short Story", "Old draft"])
        confirmations = []
        self.nav.confirm = lambda *args: confirmations.append(args) or False
        self.nav.delete([self.tree.get(IDS["Old draft"])])
        self.nav.actions.empty_trash.click()
        self.run_changes()
        self.assertIn(IDS["Old draft"], self.remote())                     # both declined
        self.assertEqual(confirmations, [
            ("Delete item permanently?", "The item will be deleted permanently across all your synced devices.", "Delete"),
            ("Empty trash?", "The items will be deleted permanently across all your synced devices.", "Empty trash")])
        self.nav.confirm = lambda *args: True
        self.nav.delete([self.window.navigator.query.tree.get(IDS["Old draft"])])
        self.run_changes()
        self.assertEqual(self.statuses[-1], "1 item was deleted")
        self.assertNotIn(IDS["Old draft"], self.remote())
        self.nav.actions.empty_trash.click()
        self.run_changes()
        self.assertEqual(self.statuses[-1], "Trash emptied")
        self.assertEqual(self.shown(), [])
        self.assertNotIn(IDS["A Short Story"], self.remote())

    def test_failures_are_reported_in_the_status_bar(self):
        self.window.mutator.run("Renaming", lambda sync, _: sync.rename(IDS["Sketchbook"], " "), "never")
        self.run_changes()
        self.assertEqual(self.statuses[-1], "Renaming failed: a name cannot be empty")
        self.assertIn("Renaming failed", self.window.status.text())

    def test_folder_picker_lists_destinations(self):
        from PySide6.QtCore import QTimer
        from ui.navigator import FolderPicker
        tree = self.tree
        picker = FolderPicker(tree, [tree.get(IDS["Work"])])
        names = {i: (tree.get(i).name if i else "My files") for i in picker.folder_ids()}
        self.assertEqual(sorted(names.values()), ["My files", "Personal"])   # not Work, nor Projects in it
        picker = FolderPicker(tree, [tree.get(IDS["Sketchbook"])])
        self.assertEqual(sorted(tree.get(i).name if i else "My files" for i in picker.folder_ids()),
                         ["My files", "Personal", "Projects", "Work"])
        root = picker.folders.topLevelItem(0)
        projects = next(root.child(i).child(j) for i in range(root.childCount())
                        for j in range(root.child(i).childCount()))
        picker.folders.setCurrentItem(projects)
        QTimer.singleShot(0, picker.accept)
        self.assertEqual(picker.choose(), IDS["Projects"])
        QTimer.singleShot(0, picker.reject)
        self.assertIsNone(picker.choose())

    def test_default_dialogs(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        def answer(fn):
            QTimer.singleShot(0, lambda: fn(QApplication.activeModalWidget()))
        answer(lambda d: (d.setTextValue("  Plans  "), d.accept()))
        self.assertEqual(self.nav.ask_text("New folder", "Name", "Folder", "Create"), "Plans")
        answer(lambda d: d.reject())
        self.assertIsNone(self.nav.ask_text("Rename", "Name", "x", "Save"))
        answer(lambda d: next(b for b in d.buttons() if b.text() == "Delete").click())
        self.assertTrue(self.nav.confirm("Delete item permanently?", "…", "Delete"))
        answer(lambda d: next(b for b in d.buttons() if b.text() == "Cancel").click())
        self.assertFalse(self.nav.confirm("Empty trash?", "…", "Empty trash"))
        answer(lambda d: d.accept())                                       # FolderPicker: My files
        self.assertEqual(self.nav.ask_folder([self.tree.get(IDS["Sketchbook"])]), "")
        answer(lambda d: d.reject())
        self.assertEqual(self.nav.ask_import_files(), [])

    def test_guards_tags_and_plain_drags(self):
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent
        self.nav.navigate(Location(ViewFilter.TAGS))
        tag = self.nav.view.items()[0].entry
        self.assertEqual(tag.kind, "tag")
        self.assertEqual(self.nav.entry_actions(tag), [])
        self.assertIsNone(self.nav.context_menu(tag))
        self.nav.show_context_menu(tag, QPoint(0, 0))                     # nothing to show
        mime = QMimeData()
        mime.setText("just text")
        enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.nav, enter)
        self.assertFalse(enter.isAccepted())
        self.nav.view.set_drop_hint("Drop file to upload to your reMarkable")
        move = QDragMoveEvent(QPoint(6, 6), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier)
        move.ignore()
        self.nav.dragMoveEvent(move)              # Qt only routes moves after an accepted enter
        self.assertTrue(move.isAccepted())
        self.nav.view.set_drop_hint("")
        self.nav.set_writable(False)
        self.nav.ask_text = self.nav.ask_import_files = lambda *a: self.fail("no dialog offline")
        self.nav.new_folder()
        self.nav.import_files()
        self.nav._request("x", lambda sync, _: None, "y")                 # silently refused
        self.run_changes()
        self.assertEqual(self.statuses, [])


class EntryPointTests(unittest.TestCase):
    def test_demo_app_builds_and_starts(self):
        """run.py --demo: the app's main.qml over the demo library, synced from the fake cloud."""
        import run
        with tempfile.TemporaryDirectory() as tmp:
            quick = run.demo_app(Path(tmp))
            quick.load()
            quick.start(sync=True)
            settle(quick.syncer.pool)
            self.assertIsNotNone(quick.window)
            self.assertGreater(len(quick.library.tree.nodes), 0)
            self.assertTrue(quick.library.property("isReady"))
            quick.shutdown()
            quick.window.close()
            gc.collect()        # free the QML objects' Python wrappers before the engine deletes them
            quick.engine.deleteLater()
            settle()


if __name__ == "__main__":
    unittest.main()
