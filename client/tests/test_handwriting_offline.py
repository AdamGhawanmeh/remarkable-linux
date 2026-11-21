"""Offline unit and QML integration tests for handwriting conversion (synthetic ids and a
fake /convert/v1/handwriting only; nothing reaches the real cloud):

- core.handwriting: the request body as reMarkable.exe 3.29 builds it, the JIIX parser
  (lines, list markers), the error mapping and retries, combining a job's tasks.
- The fake cloud's recogniser through AuthClient: bearer token and JIIX Accept header.
- ui.library: revert_top and a text step merged with the step below it.
- The app's own DocumentView: a page converted to a new page of typed text (undo/redo,
  pending edits), a selection converted and pasted as one undo step with its delete,
  and a failed, offline or cancelled selection conversion putting the strokes back
  exactly once, in and out of text mode.

Run: QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s client/tests -p 'test_handwriting_offline.py'
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE), str(HERE.parent / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_quick_offline as tq  # noqa: E402  (QApplication and the app's QML set up)

from PySide6.QtCore import QMetaObject, QObject, QRect, Qt  # noqa: E402

import test_edits_offline as te  # noqa: E402
import test_layers_offline as tl  # noqa: E402
from core import handwriting as hwc  # noqa: E402
from core.auth import HttpResponse  # noqa: E402
from core.edits import StrokeDelete, TextEdit  # noqa: E402
from core.sync import SyncClient  # noqa: E402
from fakecloud import FakeCloud, paired_auth, synthetic_jiix  # noqa: E402
from test_erase_undo_clear_offline import crossing_page  # noqa: E402
from ui.library import LibrarySource  # noqa: E402
from ui.quick.bridge import scene  # noqa: E402
from viewer import rm_text  # noqa: E402
from viewer.rm_parser import parse  # noqa: E402

THROWAWAY, PAGE1 = te.THROWAWAY, te.PAGE1


class _Resp:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status, self.body = status, body


def scripted(*answers):
    """A request function answering in turn: an int status, bytes (200 with that body),
    or an exception to raise."""
    calls = []

    def request(method, path, headers=None, body=None):
        calls.append((method, path, dict(headers or {}), body))
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return _Resp(200, answer) if isinstance(answer, bytes) else _Resp(answer)
    request.calls = calls
    return request


STROKE = [(0.0, 100.0), (10.5, 110.25), (20.0, 100.0)]


class RequestBodyTests(unittest.TestCase):
    def test_the_body_is_the_apps_raw_content_request_at_ten_times_scale(self):
        body = json.loads(hwc.request_body([STROKE, [(-5.0, 7.0)]], "de_DE"))
        self.assertEqual((body["xDPI"], body["yDPI"], body["width"], body["height"]), (2280, 2280, 14040, 18720))
        self.assertEqual(body["contentType"], "Raw Content")
        self.assertEqual(body["configuration"], {
            "lang": "de_DE",
            "export": {"jiix": {"strokes": False, "text": {"chars": False, "words": True}}},
            "raw-content": {"recognition": {"text": True, "shape": False}}})
        strokes = body["strokeGroups"][0]["strokes"]
        self.assertEqual(len(body["strokeGroups"]), 1)
        self.assertEqual(strokes[0], {"x": [0.0, 105.0, 200.0], "y": [1000.0, 1102.5, 1000.0]})
        self.assertEqual(strokes[1], {"x": [-50.0], "y": [70.0]})

    def test_empty_strokes_are_left_out(self):
        body = json.loads(hwc.request_body([[], STROKE], "en_US"))
        self.assertEqual(len(body["strokeGroups"][0]["strokes"]), 1)


class JiixParserTests(unittest.TestCase):
    def test_text_elements_top_to_bottom_one_paragraph_per_line(self):
        jiix = {"type": "Raw Content", "elements": [
            {"type": "Text", "label": "second block", "bounding-box": {"x": 0, "y": 50, "width": 9, "height": 5}},
            {"type": "Raw Content", "kind": "drawing"},
            {"type": "Text", "label": "Shopping\nmilk", "bounding-box": {"x": 0, "y": 10, "width": 9, "height": 5}}]}
        self.assertEqual(hwc.plain_text(hwc.parse_jiix(jiix)), "Shopping\nmilk\nsecond block")

    def test_list_markers_the_app_recognises_become_list_paragraphs(self):
        label = "Todo\n* eggs\n• flour\n→ sugar\n- salt\n☐ call\n☑ done\no open\n✓ ticked\nplain-dash"
        paragraphs = hwc.parse_jiix(synthetic_jiix(label))
        self.assertEqual([(p.kind.name, p.text) for p in paragraphs], [
            ("TEXT", "Todo"), ("BULLET", "eggs"), ("BULLET", "flour"), ("BULLET", "sugar"), ("BULLET", "salt"),
            ("CHECKBOX", "call"), ("CHECKBOX_CHECKED", "done"), ("CHECKBOX", "open"),
            ("CHECKBOX_CHECKED", "ticked"), ("TEXT", "plain-dash")])
        self.assertEqual(hwc.plain_text(paragraphs).splitlines()[1:3], ["• eggs", "• flour"])

    def test_words_are_used_when_an_element_has_no_label(self):
        jiix = {"elements": [{"type": "Text", "words": [{"label": "hi"}, {"label": " "}, {"label": "there"}]}]}
        self.assertEqual(hwc.plain_text(hwc.parse_jiix(jiix)), "hi there")

    def test_blank_lines_round_the_text_are_dropped_and_junk_is_refused(self):
        self.assertEqual(hwc.plain_text(hwc.parse_jiix(synthetic_jiix("\nhello\n"))), "hello")
        for junk in (b"[]", b'{"type": "Raw Content"}', b"not json"):
            with self.assertRaises(ValueError):
                hwc.parse_jiix(junk)

    def test_clipboard_text_has_plain_and_html_sides(self):
        paragraphs = hwc.parse_jiix(synthetic_jiix("Title\n- a\n- b & c\n☐ d"))
        clip = hwc.clipboard_text(paragraphs)
        self.assertEqual(clip["text"], "Title\n• a\n• b & c\n☐ d")
        self.assertEqual(clip["html"], "<p>Title</p><ul><li>a</li><li>b &amp; c</li></ul><p>☐ d</p>")


class RecognizeTests(unittest.TestCase):
    def test_a_good_answer_and_the_request_sent(self):
        request = scripted(synthetic_jiix("hello world"))
        result = hwc.recognize(request, [STROKE], "en_US")
        self.assertEqual((result.error, result.plain), (hwc.ConvertError.NoError, "hello world"))
        method, path, headers, body = request.calls[0]
        self.assertEqual((method, path), ("POST", "/convert/v1/handwriting"))
        self.assertEqual(headers, {"Accept": "application/vnd.myscript.jiix", "Content-Type": "application/json"})
        self.assertEqual(json.loads(body)["configuration"]["lang"], "en_US")

    def test_errors_map_as_the_app_maps_them(self):
        cases = [((401,), hwc.ConvertError.AuthenticationError, 1),
                 ((403,), hwc.ConvertError.AuthenticationError, 1),
                 ((PermissionError("not paired"),), hwc.ConvertError.AuthenticationError, 1),
                 ((400,), hwc.ConvertError.ServerError, 1),
                 ((500,), hwc.ConvertError.ServerError, 3),
                 ((ConnectionRefusedError("offline"),), hwc.ConvertError.NetworkError, 3),
                 ((b"<html>",), hwc.ConvertError.ServerError, 1),
                 ((synthetic_jiix(""),), hwc.ConvertError.NoText, 1)]
        for answers, error, attempts in cases:
            with self.subTest(answers=answers):
                request = scripted(*answers)
                self.assertEqual(hwc.recognize(request, [STROKE], "en_US").error, error)
                self.assertEqual(len(request.calls), attempts)

    def test_a_task_is_tried_three_times(self):
        request = scripted(TimeoutError("slow"), 503, synthetic_jiix("third time"))
        self.assertEqual(hwc.recognize(request, [STROKE], "en_US").plain, "third time")
        self.assertEqual(len(request.calls), 3)

    def test_no_strokes_is_no_tasks_without_a_request(self):
        request = scripted(500)
        self.assertEqual(hwc.recognize(request, [[]], "en_US").error, hwc.ConvertError.NoTasks)
        self.assertEqual(request.calls, [])

    def test_combining_tasks(self):
        ok = hwc.Recognition([hwc.Paragraph(hwc.Kind.TEXT, "a")])
        none = hwc.Recognition(error=hwc.ConvertError.NoText)
        net = hwc.Recognition(error=hwc.ConvertError.NetworkError)
        self.assertEqual(hwc.combine([none, ok]).plain, "a")
        self.assertEqual(hwc.combine([ok, net]).error, hwc.ConvertError.NetworkError)
        self.assertEqual(hwc.combine([none, none]).error, hwc.ConvertError.NoText)
        self.assertEqual(hwc.combine([]).error, hwc.ConvertError.NoText)

    def test_the_fake_cloud_answers_through_the_auth_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            cloud = FakeCloud()
            auth = paired_auth(Path(tmp), cloud)

            def request(method, path, headers=None, body=None):
                return auth.authorized_request(method, path, headers=headers, body=body)
            result = hwc.recognize(request, [STROKE], "fr_FR")
            self.assertEqual(result.plain, "hello world")
            method, path, headers = cloud.calls[-1]
            self.assertEqual((method, path), ("POST", "/convert/v1/handwriting"))
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            self.assertEqual(cloud.handwriting_requests[-1]["configuration"]["lang"], "fr_FR")
            cloud.fail_next = [401, 401]
            self.assertEqual(hwc.recognize(request, [STROKE], "fr_FR").error, hwc.ConvertError.AuthenticationError)
            cloud.offline = True
            self.assertEqual(hwc.recognize(request, [STROKE], "fr_FR").error, hwc.ConvertError.NetworkError)


class LibraryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cloud = tl.layer_cloud(crossing_page())
        self.source = LibrarySource(SyncClient(paired_auth(self.dir / "auth", self.cloud), self.dir / "cache"),
                                    editable=("throwaway",))
        self.doc = self.source.refresh().get(THROWAWAY)

    def items(self):
        from viewer import rm_select
        return [i.item for i in rm_select.page_items(self.source.staged(self.doc).replay(PAGE1).page) if i.is_line]

    def test_revert_top_undoes_the_delete_and_forgets_it(self):
        items = self.items()
        self.assertTrue(self.source.delete_items(self.doc, PAGE1, items[:2], te.NOW))
        depth = self.source.undo_depth(self.doc, PAGE1)
        self.assertEqual(depth, 1)
        self.assertFalse(self.source.revert_top(self.doc, PAGE1, depth + 1, te.NOW + 1))
        self.assertTrue(self.source.revert_top(self.doc, PAGE1, depth, te.NOW + 1))
        self.assertEqual(len(self.items()), 3)
        self.assertEqual(self.source.history_state(self.doc, PAGE1), (False, False))
        self.assertFalse(self.source.revert_top(self.doc, PAGE1, depth, te.NOW + 2))

    def test_a_text_step_merged_with_the_delete_below_it_is_one_undo_step(self):
        from ui import textedit
        from viewer.renderer import qt_measure
        items = self.items()
        self.source.delete_items(self.doc, PAGE1, items, te.NOW)
        depth = self.source.undo_depth(self.doc, PAGE1)
        done = self.source.text_page(self.doc, PAGE1)
        session = textedit.TextSession.open(THROWAWAY, PAGE1, done.page, done, self.source.sync.author(), None,
                                            qt_measure, lambda: te.NOW + 5)
        scene.paste_paragraphs(session, hwc.parse_jiix(synthetic_jiix("Groceries\n- milk\n☐ bread")))
        edits, steps = session.take()
        self.assertEqual(len(steps), 1, "the text and its list styles are one step")
        self.source.add_text_edits(self.doc, PAGE1, edits, steps, merge=(depth, 0))
        self.assertEqual(self.source.undo_depth(self.doc, PAGE1), 1)
        page = self.source.staged(self.doc).page(PAGE1)
        self.assertEqual(rm_text.page_text(page).text, "Groceries\nmilk\nbread")
        self.assertEqual(self.items(), [])
        self.source.undo(self.doc, PAGE1, te.NOW + 10)
        page = self.source.staged(self.doc).page(PAGE1)
        self.assertEqual(len(self.items()), 3)
        self.assertEqual(rm_text.page_text(page).text.strip(), "")
        self.source.redo(self.doc, PAGE1, te.NOW + 11)
        self.assertEqual(self.items(), [])
        self.assertEqual(rm_text.page_text(self.source.staged(self.doc).page(PAGE1)).text, "Groceries\nmilk\nbread")


class HandwritingQmlTests(unittest.TestCase):
    """Convert to text through the app's own DocumentView, DocumentViewport and
    HwcNotificationBar, against the fake cloud's recogniser."""

    wait_for, qml, scene_views = tq.EditingTests.wait_for, tq.EditingTests.qml, tq.EditingTests.scene_views
    close_document, handler = tq.EditingTests.close_document, tq.EditingTests.handler
    open_doc, key = tl.LayersQmlTests.open_doc, tl.LayersQmlTests.key
    tearDownClass = classmethod(tq.EditingTests.tearDownClass.__func__)
    THROWAWAY, PAGE1 = THROWAWAY, PAGE1

    @classmethod
    def setUpClass(cls):
        from ui.quick.app import QuickApp
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cloud = tl.layer_cloud(crossing_page())
        cls.source = LibrarySource(SyncClient(paired_auth(tmp / "auth", cls.cloud), tmp / "cache"),
                                   editable=("throwaway",))
        cls.source.refresh()
        cls.app = QuickApp(cls.source, tmp / "cache", config_dir=tmp)
        cls.app.load()
        cls.app.start(sync=False)
        tq.wait(800)
        cls.window = cls.app.window
        cls.window.resize(1270, 875)
        cls.notices: list[str] = []
        cls.app.library.errorMessage.connect(cls.notices.append)
        cls.recognizer = cls.app.singletons.recognizer
        cls.online = (cls.recognizer.online,)

    def setUp(self):
        tl.LayersQmlTests.setUp(self)
        self.cloud.handwriting, self.cloud.handwriting_text = None, "hello world"
        self.cloud.offline = False
        self.cloud.fail_next = []
        self.recognizer.online = self.online[0]
        self.app.singletons.clipboard.setProperty("text", {"text": "kept"})

    def tearDown(self):
        from PySide6.QtCore import Q_ARG
        if self.scene_views():
            self.qml("DocumentView_").setProperty("textMode", False)
            QMetaObject.invokeMethod(self.qml("DocumentViewport"), "setCurrentPage", Q_ARG("QVariant", 0))
            tq.wait(200)
        self.close_document()

    def _has_text_mode(self) -> bool:
        try:
            return self.qml("DocumentView_").property("textMode") is not None
        except StopIteration:
            return False

    def bar(self, name: str) -> QObject:
        """hwcFullPage or hwcSelection (the HwcNotificationBar bound to that job)."""
        bars = [o for o in self.window.findChildren(QObject)
                if o.metaObject().className().startswith("HwcNotificationBar")]
        selection = name != "full"     # hwcSelection declares inProgress, hwcFullPage newPage
        return next(b for b in bars if (b.metaObject().indexOfProperty("inProgress") >= 0) == selection)

    def node(self):
        return self.source.sync.tree.get(THROWAWAY)

    def kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.source.sync.edits.of(THROWAWAY)]

    def strokes(self, view) -> int:
        scene_ = view._content.scene if view._content is not None else None
        return sum(len(layer.strokes) for layer in scene_.layers) if scene_ is not None else -1

    def page_text(self, page_id: str) -> str:
        text = rm_text.page_text(self.source.staged(self.app.library.tree.get(THROWAWAY)).page(page_id))
        return text.text if text is not None else ""

    def manager_js(self, expression: str):
        """Evaluate `expression` in ActionManager's QML context (its actions are QML
        types Python cannot take as values)."""
        from PySide6.QtQml import QQmlExpression, qmlContext
        manager = self.qml("ActionManager")
        return QQmlExpression(qmlContext(manager), manager, expression).evaluate()[0]

    def select_all(self, view):
        self.qml("DocumentView_").setProperty("selectionMode", True)
        view._controller.addSelectionRect(QRect(-1000, -1000, 3000, 3000), 0)
        self.assertTrue(self.wait_for(lambda: self.handler().property("visible")), self.notices)
        self.assertEqual(view._controller.property("selectionItemCount"), 3)

    def convert_selection(self, view):
        self.select_all(view)
        self.manager_js("convertSelectionToTextAction.trigger()")
        self.assertIsNotNone(self.job("selectionToolJob"))

    def status(self, name: str) -> int:
        job = self.job(name)
        return -1 if job is None else job.property("status")

    def job(self, name: str):
        """Recognizer.recognitionJob / selectionToolJob (as Python holds them: PySide has
        no converter for the RecognitionJob* QML reads)."""
        return self.recognizer._jobs.get(name)

    # -- a page converted to a new page ------------------------------------------------

    def test_a_page_is_converted_to_a_new_page_of_typed_text(self):
        self.cloud.handwriting_text = "Meeting notes\n- agenda\n☐ follow up"
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3))
        before = self.source.page_ids(self.node())
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "forceActiveFocus")
        self.assertTrue(self.wait_for(lambda: self.manager_js("convertPageToTextAction.enabled")))
        self.manager_js("convertPageToTextAction.trigger()")
        self.assertTrue(self.wait_for(lambda: self.status("recognitionJob") == 1), self.notices)
        job = self.job("recognitionJob")
        self.assertEqual((job.property("pages"), job.property("newPage")), ([0], 1))
        self.assertEqual(job.property("progress"), 1.0)
        request = self.cloud.handwriting_requests[-1]
        self.assertEqual(len(request["strokeGroups"][0]["strokes"]), 3)
        self.assertEqual(request["configuration"]["lang"], "en_US")
        self.assertTrue(self.wait_for(lambda: len(self.source.page_ids(self.node())) == len(before) + 1), self.notices)
        new = self.source.page_ids(self.node())[1]
        self.assertNotIn(new, before)
        self.assertTrue(self.wait_for(lambda: self.page_text(new) == "Meeting notes\nagenda\nfollow up"), self.notices)
        self.assertIn("TextEdit", self.kinds())
        self.assertEqual(self.page_text(PAGE1), "", "the converted page keeps its handwriting")
        self.assertTrue(self.bar("full").property("visible"))
        # The new page's text is undone and redone as one step.
        self.assertEqual(self.source.history_state(self.node(), new), (True, False))
        self.source.undo(self.node(), new, te.NOW + 1)
        self.assertEqual(self.page_text(new).strip(), "")
        self.source.redo(self.node(), new, te.NOW + 2)
        self.assertEqual(self.page_text(new), "Meeting notes\nagenda\nfollow up")
        # The bar's Undo deletes the new page.
        QMetaObject.invokeMethod(self.bar("full"), "undo")
        self.assertTrue(self.wait_for(lambda: self.source.page_ids(self.node()) == before), self.notices)
        self.assertEqual(self.notices, [])

    def test_a_page_conversion_error_shows_and_adds_nothing(self):
        self.cloud.handwriting = lambda request: HttpResponse(400, {}, b"{}")
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3))
        before = self.source.page_ids(self.node())
        self.recognizer.startRecognitionJob(THROWAWAY, [0], "en_US")
        self.assertTrue(self.wait_for(lambda: self.status("recognitionJob") == 2),
                        (self.status("recognitionJob"), self.cloud.handwriting_requests[-1:], self.notices))
        job = self.job("recognitionJob")
        self.assertEqual(job.property("error"), int(hwc.ConvertError.ServerError))
        tq.wait(200)
        self.assertEqual(self.source.page_ids(self.node()), before)
        self.assertEqual(self.kinds(), [])
        self.recognizer.abortRecognitionJob()
        self.assertIsNone(self.job("recognitionJob"))

    # -- a selection converted in place --------------------------------------------------

    def test_a_selection_is_converted_pasted_and_undone_as_one_step(self):
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3))
        self.convert_selection(view)
        self.assertTrue(self.wait_for(lambda: self.status("selectionToolJob") == 1), self.notices)
        self.assertEqual(self.app.singletons.clipboard.property("text")["text"], "hello world")
        self.assertTrue(self.wait_for(lambda: self.page_text(PAGE1) == "hello world"), self.notices)
        self.assertTrue(self.wait_for(lambda: "TextEdit" in self.kinds()))
        self.assertEqual(self.kinds()[0], "StrokeDelete")
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 0))
        self.assertEqual(len(self.cloud.handwriting_requests[-1]["strokeGroups"][0]["strokes"]), 3)
        self.assertTrue(self.wait_for(lambda: self.source.undo_depth(self.node(), PAGE1) == 1), self.notices)
        # The bar's Undo: strokes back and the text gone, in one step.
        QMetaObject.invokeMethod(self.bar("selection"), "undo")
        self.assertTrue(self.wait_for(lambda: self.page_text(PAGE1).strip() == ""), self.notices)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3), self.notices)
        self.assertEqual(self.source.history_state(self.node(), PAGE1), (False, True))
        self.assertEqual(self.notices, [])

    def _failed_selection(self, text_mode: bool, fail) -> None:
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3))
        # An earlier edit the conversion must not undo: one stroke moved.
        ctrl = view._controller
        self.qml("DocumentView_").setProperty("selectionMode", True)
        ctrl.addSelectionRect(QRect(-200, 880, 400, 40), 0)
        self.assertTrue(self.wait_for(lambda: ctrl.property("selectionItemCount") == 1))
        ctrl.moveSelectedItems(0, tq.QPointF(0.0, 30.0))
        ctrl.applyPendingEdit(0)
        QMetaObject.invokeMethod(self.qml("DocumentViewport"), "endItemSelection")
        self.assertTrue(self.wait_for(lambda: self.source.undo_depth(self.node(), PAGE1) == 1))
        fail()
        self.convert_selection(view)
        if text_mode:
            self.qml("DocumentView_").setProperty("textMode", True)
        self.assertTrue(self.wait_for(lambda: self.status("selectionToolJob") == 2), self.notices)
        job = self.job("selectionToolJob")
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3 and self.source.undo_depth(self.node(), PAGE1) == 1),
                        (self.strokes(view), self.source.undo_depth(self.node(), PAGE1), self.notices))
        tq.wait(300)
        # Close on the bar (abort, then DocumentView's undo) changes nothing more.
        error = job.property("error")
        QMetaObject.invokeMethod(self.bar("selection"), "cancel")
        tq.wait(400)
        self.assertEqual(self.strokes(view), 3)
        self.assertEqual(self.source.undo_depth(self.node(), PAGE1), 1, "the earlier move was not undone")
        self.assertEqual(self.source.history_state(self.node(), PAGE1), (True, False), "no redo of the delete")
        self.assertEqual(self.page_text(PAGE1).strip(), "")
        self.assertEqual(self.app.singletons.clipboard.property("text")["text"], "kept", "the clipboard is kept")
        self.assertNotIn("TextEdit", self.kinds())
        return error

    def test_a_server_error_puts_the_strokes_back_outside_text_mode(self):
        error = self._failed_selection(False, lambda: setattr(self.cloud, "handwriting",
                                                                lambda request: HttpResponse(500, {}, b"")))
        self.assertEqual(error, int(hwc.ConvertError.ServerError))

    def test_no_text_puts_the_strokes_back_in_text_mode(self):
        error = self._failed_selection(True, lambda: setattr(self.cloud, "handwriting_text", ""))
        self.assertEqual(error, int(hwc.ConvertError.NoText))

    def test_offline_puts_the_strokes_back_without_a_request(self):
        sent = len(self.cloud.handwriting_requests)
        error = self._failed_selection(False, lambda: setattr(self.recognizer, "online", lambda: False))
        self.assertEqual(error, int(hwc.ConvertError.NetworkError))
        self.assertEqual(len(self.cloud.handwriting_requests), sent)

    def test_an_authentication_error_puts_the_strokes_back(self):
        error = self._failed_selection(False, lambda: setattr(self.cloud, "fail_next", [401, 401]))
        self.assertEqual(error, int(hwc.ConvertError.AuthenticationError))

    def test_cancel_while_converting_puts_the_strokes_back_once(self):
        gate = threading.Event()

        def slow(request):
            gate.wait(10)
            return HttpResponse(200, {}, synthetic_jiix("too late"))
        self.cloud.handwriting = slow
        self.addCleanup(gate.set)
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3))
        self.convert_selection(view)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 0 and "StrokeDelete" in self.kinds()))
        self.assertEqual(self.status("selectionToolJob"), 0)
        QMetaObject.invokeMethod(self.bar("selection"), "cancel")     # Cancel: abort, then undo
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3), self.notices)
        gate.set()
        tq.wait(500)
        self.assertEqual(self.strokes(view), 3)
        self.assertEqual(self.page_text(PAGE1).strip(), "")
        self.assertEqual(self.source.history_state(self.node(), PAGE1), (False, False))
        self.assertIsNone(self.job("selectionToolJob"))

    def test_the_language_setting_is_sent(self):
        settings = self.app.singletons.settings
        old = settings.property("handwritingConversionLanguage")
        settings.setProperty("handwritingConversionLanguage", "en_UK")
        self.addCleanup(settings.setProperty, "handwritingConversionLanguage", old)
        view = self.open_doc(THROWAWAY)
        self.assertTrue(self.wait_for(lambda: self.strokes(view) == 3))
        self.convert_selection(view)
        self.assertTrue(self.wait_for(lambda: self.status("selectionToolJob") == 1), self.notices)
        self.assertEqual(self.cloud.handwriting_requests[-1]["configuration"]["lang"], "en_GB")
        self.assertTrue(self.wait_for(lambda: self.page_text(PAGE1) == "hello world"), self.notices)


if __name__ == "__main__":
    unittest.main()
