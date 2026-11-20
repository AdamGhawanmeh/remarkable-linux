"""The injected table of contents panel (bridge/toc.py): LibraryController.loadTableOfContents
shown beside the official DocumentView, whose QML is not changed (ARCHITECTURE.md §5.7).

Synthetic documents only (test_search_toc_offline's fake cloud): a PDF with a nested
outline and a page inserted before its second chapter, an EPUB placed on its PDF
rendition, a notebook and a PDF without an outline."""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakecloud import paired_auth  # noqa: E402

import test_search_toc_offline as ts  # noqa: E402

from core.sync import SyncClient  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.quick.bridge.toc import TocModel  # noqa: E402

CHAPTERS = [{"name": "One", "path": "", "page": 0, "level": 0},
            {"name": "Lost", "path": "", "page": -1, "level": 0},
            {"name": "Two", "path": "", "page": 3, "level": 0},
            {"name": "Two, part a", "path": "", "page": 5, "level": 1},
            {"name": "Two, part b", "path": "", "page": 5, "level": 1}]


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_the_entry_being_read_is_the_last_placed_at_or_before_the_page(self):
        model = TocModel()
        model.set_chapters(CHAPTERS)
        self.assertTrue(model.property("available"))
        for page, row in ((0, 0), (1, 0), (2, 0), (3, 2), (4, 2), (5, 4), (40, 4)):
            model.setProperty("page", page)
            self.assertEqual(model.property("current"), row, page)

    def test_it_opens_only_with_entries_and_jumps_only_to_placed_ones(self):
        model = TocModel()
        model.toggle()
        self.assertFalse(model.property("open"))          # nothing to show
        model.set_chapters(CHAPTERS)
        model.toggle()
        self.assertTrue(model.property("open"))
        jumped = []
        model.jumped.connect(jumped.append)
        model.jump(1)                                     # page -1: not placed
        model.jump(9)                                     # no such row
        self.assertEqual(jumped, [])                      # (and no viewport to turn either)
        model.set_chapters([])
        self.assertFalse(model.property("open"))
        self.assertEqual(model.property("current"), -1)


class QmlTests(unittest.TestCase):
    """The panel through the app's own window: the Contents button, the entries, a click
    turning the page as the app turns pages, Ctrl+Shift+T and Escape."""

    @classmethod
    def setUpClass(cls):
        import test_quick_offline as tq
        from ui.quick.app import QuickApp
        cls.tq = tq
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", ts.build_cloud()), tmp / "cache"),
                                   editable=("Search paper",))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        del cls.window
        gc.collect()
        cls.app.engine.deleteLater()
        cls.tq.wait(50)
        cls.tmp.cleanup()

    def setUp(self):
        self.notices.clear()
        del self.app.warnings[:]

    def tearDown(self):
        self.tq.EditingTests.close_document(self)
        self.source.sync.edits.clear()

    def wait_for(self, condition, ms=8000):
        return self.tq.WorkflowTests.wait_for(self, condition, ms)

    def qml(self, prefix):
        return self.tq.WorkflowTests.qml(self, prefix)

    def scene_views(self):
        return self.tq.WorkflowTests.scene_views(self)

    def open_document(self, doc_id):
        return self.tq.EditingTests.open_document(self, doc_id)

    def items(self) -> list:
        todo, out = [self.window.contentItem()], []
        while todo:
            item = todo.pop()
            out.append(item)
            todo.extend(item.childItems())
        return out

    def item(self, name: str):
        return next((i for i in self.items() if i.objectName() == name), None)

    def shown(self, name: str) -> bool:
        item = self.item(name)
        return item is not None and item.isVisible()

    def click(self, item) -> None:
        from PySide6.QtTest import QTest
        centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        self.tq.wait(40)

    def key(self, key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        from PySide6.QtTest import QTest
        self.window.requestActivate()
        QTest.keyClick(self.window, key, modifiers)
        self.tq.wait(40)

    def viewport(self):
        return self.qml("DocumentViewport")

    def entries(self) -> list[str]:
        out = []
        for n in range(50):
            entry = self.item(f"tocEntry_{n}")
            if entry is None:
                break
            out.append(next(c.property("text") for c in entry.childItems() if c.objectName() == "tocName"))
        return out

    def page_labels(self) -> list[str]:
        """The page number each entry shows (counted from 1)."""
        out = []
        for n in range(50):
            entry = self.item(f"tocEntry_{n}")
            if entry is None:
                break
            out.append(next(c.property("text") for c in entry.childItems() if c.objectName() == "tocPage"))
        return out

    def test_a_pdf_outline_turns_to_its_chapters(self):
        self.open_document(ts.PDF_DOC)
        self.assertTrue(self.wait_for(lambda: self.shown("tocButton")), "no Contents button")
        self.assertFalse(self.shown("tocPanel"))
        self.click(self.item("tocButton"))
        self.assertTrue(self.wait_for(lambda: self.shown("tocPanel")))
        self.assertEqual(self.entries(), ["Chapter 1", "Chapter 2", "Section 2.1", "Section 2.2"])
        self.assertEqual(self.page_labels(), ["1", "3", "4", "4"])
        # Indented by level; the page number shown counts from 1 (Chapter 2 after the inserted page).
        indents = [next(c for c in self.item(f"tocEntry_{n}").childItems() if c.objectName() == "tocName").x()
                   for n in range(4)]
        self.assertEqual(indents[0], indents[1])
        self.assertGreater(indents[2], indents[0])
        self.click(self.item("tocEntry_2"))                                  # Section 2.1: page 3
        self.assertTrue(self.wait_for(lambda: self.viewport().property("currentPage") == 3))
        self.assertTrue(self.wait_for(lambda: any(v.property("pageId") == ts.P[3] and v.property("status") == 2
                                                  for v in self.scene_views())))
        model = self.item("tocSidebar")
        self.assertTrue(self.shown("tocPanel"), "the panel stays open after a jump")
        self.click(self.item("tocEntry_1"))                                  # Chapter 2: page 2
        self.assertTrue(self.wait_for(lambda: self.viewport().property("currentPage") == 2))
        # The entry being read follows the page, however it was turned.
        from PySide6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self.viewport(), "setCurrentPage", Q_ARG("QVariant", 0))
        self.assertTrue(self.wait_for(lambda: self.item("tocList").property("currentIndex") == 0))
        self.assertIsNotNone(model)
        # Escape closes the panel, not the document; Ctrl+Shift+T opens and closes it.
        self.key(Qt.Key.Key_Escape)
        self.assertTrue(self.wait_for(lambda: not self.shown("tocPanel")))
        self.assertTrue(self.qml("DocumentView_").property("visible"), "Escape closed the document")
        self.key(Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(self.wait_for(lambda: self.shown("tocPanel")))
        self.key(Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(self.wait_for(lambda: not self.shown("tocPanel")))
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_an_ebook_navigation_turns_to_its_chapters(self):
        self.open_document(ts.EPUB_DOC)
        self.assertTrue(self.wait_for(lambda: self.shown("tocButton")))
        self.click(self.item("tocButton"))
        self.assertTrue(self.wait_for(lambda: self.shown("tocPanel")))
        self.assertEqual(self.entries(), ["Prologue", "The Long Road", "A Detour", "Epilogue"])
        self.click(self.item("tocEntry_3"))
        self.assertTrue(self.wait_for(lambda: self.viewport().property("currentPage") == 4))
        self.assertEqual(self.tq.real_warnings(self.app.warnings), [])

    def test_no_button_without_a_table_of_contents(self):
        for doc_id in (ts.PLAIN_DOC, ts.NOTE_DOC):
            self.open_document(doc_id)
            self.tq.wait(600)                                 # the backend has answered by now
            self.assertFalse(self.shown("tocButton"), doc_id)
            self.key(Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            self.assertFalse(self.shown("tocPanel"), doc_id)
            self.tq.EditingTests.close_document(self)
        # Back to a document with one: the button is there again.
        self.open_document(ts.PDF_DOC)
        self.assertTrue(self.wait_for(lambda: self.shown("tocButton")))

    def test_entries_follow_pages_inserted_into_the_document(self):
        self.open_document(ts.PDF_DOC)
        self.assertTrue(self.wait_for(lambda: self.shown("tocButton")))
        self.click(self.item("tocButton"))
        self.assertTrue(self.wait_for(lambda: len(self.entries()) == 4))
        doc = self.source.sync.tree.get(ts.PDF_DOC)
        self.source.add_page(doc, 0, "Blank", 1000)           # a note page before Chapter 1
        self.app.set_tree(self.source.sync.tree)              # as the app shows a page added
        self.assertTrue(self.wait_for(lambda: self.app.library.entryForId(ts.PDF_DOC).property("pageCount") == 5))
        # Placed again on the new page list (asked for again, answered on the worker).
        self.assertTrue(self.wait_for(lambda: self.page_labels()[:2] == ["2", "4"]), self.page_labels())
        self.click(self.item("tocEntry_0"))
        self.assertTrue(self.wait_for(lambda: self.viewport().property("currentPage") == 1),
                        self.viewport().property("currentPage"))


if __name__ == "__main__":
    unittest.main()
