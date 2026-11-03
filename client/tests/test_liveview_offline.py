"""Offline tests for Screen Share: the display stream decoder, the signalling client, the
session service against a simulated tablet (real aiortc on loopback), and the window.

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import base64
import json
import os
import queue
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CLIENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CLIENT / "src"), str(CLIENT / "tests")]

from liveview_fakes import (DESKTOP, ROOM, TABLET, FakeNotifications, FakeRooms, FakeTablet,  # noqa: E402
                            notification, rect_update)

from core.auth import HttpResponse  # noqa: E402
from liveview import rfb  # noqa: E402
from liveview.rfb import (CursorMoved, Framebuffer, FramebufferUpdate, Ping, ProtocolError, Rect,  # noqa: E402
                          Rotated, ServerHello, Shutdown, StreamDecoder, client_handshake)
from liveview.session import (Connected, Connecting, Disconnected, LiveViewService, NoActiveSession,  # noqa: E402
                              NotificationsState, RoomOffered, _mid)
from liveview.signaling import (Room, RoomClient, RoomNotFound, SignalingError, notifications_url,  # noqa: E402
                                parse_notification)

RED, BLUE = b"\x00\xf8", b"\x1f\x00"          # RGB565 little-endian


def hello(version=2, w=8, h=6) -> bytes:
    return bytes([104]) + struct.pack(">HHH", version, w, h)


class StreamDecoderTests(unittest.TestCase):
    def test_client_handshake(self):
        self.assertEqual(client_handshake(), b"reMarkable\x00\x02")

    def test_every_message_type_byte_by_byte(self):
        stream = (hello(2, 4, 3) + rect_update([(1, 1, 2, 2, RED * 4), (0, 0, 1, 1, BLUE)])
                  + bytes([100]) + struct.pack(">HH", 7, 9) + bytes([102]) + struct.pack(">i", -90)
                  + bytes([103, 101]))
        decoder, events = StreamDecoder(), []
        for byte in stream:
            events += decoder.feed(bytes([byte]))
        self.assertEqual(events[0], ServerHello(2, 4, 3))
        self.assertEqual(events[1].rects, ((Rect(1, 1, 2, 2), RED * 4), (Rect(0, 0, 1, 1), BLUE)))
        self.assertEqual(events[2:], [CursorMoved(7, 9), Rotated(-90), Ping(), Shutdown()])
        self.assertEqual(decoder.buffer, bytearray())
        self.assertEqual(StreamDecoder().feed(stream), events)                  # all at once too

    def test_updates_end_with_a_flush_or_a_final_block(self):
        """Live, the tablet ends each update's zlib stream with a flush: zlib.decompress()
        rejects that ("incomplete or truncated stream"), the decoder must not."""
        flushed = rect_update([(0, 0, 1, 1, RED)])
        with self.assertRaisesRegex(zlib.error, "incomplete or truncated"):
            zlib.decompress(flushed[7:])
        finished = rect_update([(0, 0, 1, 1, RED)], finish=True)
        for update in (flushed, finished):
            self.assertEqual(StreamDecoder().feed(update), [FramebufferUpdate(((Rect(0, 0, 1, 1), RED),))])

    def test_version_1_rotation_is_one_byte(self):
        decoder = StreamDecoder()
        events = decoder.feed(hello(0) + bytes([102, 1, 102, 0]))               # version 0 means 1
        self.assertEqual(events, [ServerHello(1, 8, 6), Rotated(0), Rotated(270)])

    def test_errors(self):
        with self.assertRaisesRegex(ProtocolError, "unknown message type 7"):
            StreamDecoder().feed(bytes([7]))
        with self.assertLogs("rm.liveview.rfb", "WARNING") as logs:             # dropped, like the app
            self.assertEqual(StreamDecoder().feed(bytes([0]) + struct.pack(">HI", 1, 4) + b"oops" + bytes([103])),
                             [Ping()])
        self.assertIn("failed to read zstream", logs.output[0])
        short = zlib.compress(struct.pack(">HHHHI", 0, 0, 2, 2, 8) + b"abc")    # rect data cut short
        bad = bytes([0]) + struct.pack(">HI", 1, len(short)) + short
        with self.assertLogs("rm.liveview.rfb", "WARNING"):
            self.assertEqual(StreamDecoder().feed(bad + bytes([103])), [Ping()])   # dropped, not fatal
        headless = zlib.compress(b"\x00\x01")
        with self.assertLogs("rm.liveview.rfb", "WARNING"):
            self.assertEqual(StreamDecoder().feed(bytes([0]) + struct.pack(">HI", 1, len(headless)) + headless), [])

    def test_framebuffer(self):
        fb = Framebuffer(4, 3)
        self.assertEqual(fb.pixels, bytearray(b"\xff" * 24))                    # starts white
        changed = fb.apply(FramebufferUpdate(((Rect(1, 1, 2, 2), RED * 4), (Rect(0, 0, 1, 1), BLUE))))
        self.assertEqual(changed, Rect(0, 0, 3, 3))
        self.assertEqual(fb.pixels[8:16].hex(), "ffff00f800f8ffff")
        self.assertEqual(fb.pixels[0:2], bytearray(BLUE))
        padded = Framebuffer(2, 2, pixels=bytearray(b"\xff" * 12), stride=6)    # rows padded to 6 bytes
        padded.apply(FramebufferUpdate(((Rect(0, 1, 2, 1), RED * 2),)))
        self.assertEqual(padded.pixels.hex(), "ffffffffffff" + "00f800f8ffff")
        with self.assertLogs("rm.liveview.rfb", "WARNING") as logs:
            self.assertIsNone(fb.apply(FramebufferUpdate(((Rect(3, 0, 2, 1), RED * 2),
                                                           (Rect(0, 0, 2, 2), RED * 3)))))
        self.assertEqual(len(logs.output), 2)                                    # outside, too short
        self.assertEqual(Rect(0, 0, 1, 1).united(Rect(2, 3, 1, 1)), Rect(0, 0, 3, 4))


class SignalingTests(unittest.TestCase):
    def test_parse_notification(self):
        note = parse_notification(notification("ScreenshareMessage", TABLET, {"type": "webtrc", "payload": {"a": 1}}))
        self.assertEqual((note.event, note.source, note.data), ("ScreenshareMessage", TABLET,
                                                                {"type": "webtrc", "payload": {"a": 1}}))
        created = parse_notification(notification("ScreenshareRoomCreated", TABLET, roomId="r9"))
        self.assertEqual((created.attributes["roomId"], created.data), ("r9", None))
        for raw in ("not json", "[]", '{"message": {}}', '{"message": {"attributes": {}}}'):
            self.assertIsNone(parse_notification(raw))
        with self.assertLogs("rm.liveview.signaling", "WARNING"):
            broken = parse_notification(json.dumps({"message": {"attributes": {"event": "x"}, "data": "%%%"}}))
        self.assertIsNone(broken.data)

    def test_room_parsing(self):
        room = Room.parse({"roomId": "r1", "iceServers": [{"url": "turn:t", "username": "u", "credential": "c"},
                                                          {"url": "stun:s"}, {"nope": 1}, "junk"]})
        self.assertEqual([s.url for s in room.ice_servers], ["turn:t", "stun:s"])
        self.assertEqual((room.ice_servers[1].username, room.ice_servers[1].credential), ("", ""))
        with self.assertRaisesRegex(SignalingError, "roomId"):
            Room.parse({"iceServers": []})

    def test_room_client_requests(self):
        from fakecloud import paired_auth
        sent, replies = [], []

        def transport(method, url, headers, body, timeout):
            if url.endswith("/discovery/v1/endpoints"):                       # as the live service answers
                return HttpResponse(200, {}, b'{"notifications": "eu.tectonic.remarkable.com"}')
            sent.append((method, url.split("/", 3)[3], headers.get("Content-Type"), json.loads(body)))
            return replies.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            client = RoomClient(paired_auth(Path(tmp), transport, tectonic="eu"))
            replies[:] = [HttpResponse(200, {}, json.dumps({"roomId": "r1", "iceServers": []}).encode()),
                          HttpResponse(200, {}, b'{"roomId": "r2"}'), HttpResponse(200, {}, b""),
                          HttpResponse(204, {}, b""), HttpResponse(404, {}, b""), HttpResponse(500, {}, b"down"),
                          HttpResponse(200, {}, b"<html>")]
            self.assertEqual(client.join("r1").id, "r1")
            self.assertEqual(client.join_active().id, "r2")
            client.request_offer("r2")
            client.send_signal("r2", TABLET, {"type": "answer", "description": "v=0"})
            with self.assertRaises(RoomNotFound):
                client.join_active()
            with self.assertRaisesRegex(SignalingError, "HTTP 500"):
                client.join("r3")
            with self.assertRaisesRegex(SignalingError, "not JSON"):
                client.join("r4")
            self.assertEqual(notifications_url(client.auth), "wss://eu.tectonic.remarkable.com/notifications/ws/json/1")
        self.assertEqual(sent[:4], [
            ("POST", "screenshare/v1/rooms/r1/join", "application/json", {}),
            ("POST", "screenshare/v1/rooms/join-active", "application/json", {}),
            ("POST", "screenshare/v1/rooms/r2/messages/broadcast", "application/json",
             {"payload": {"type": "request-offer"}}),
            ("POST", "screenshare/v1/rooms/r2/messages/direct", "application/json",
             {"payload": {"type": "webtrc", "payload": {"type": "answer", "description": "v=0"}},
              "targetClientId": TABLET})])

    def test_mid_of_the_data_channel(self):
        sdp = "v=0\r\nm=audio 9 UDP\r\na=mid:a0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:dc\r\n"
        self.assertEqual(_mid(sdp), "dc")
        self.assertIsNone(_mid("v=0\r\nm=application 9 x\r\n"))
        self.assertIsNone(_mid("v=0\r\n"))


class DtlsTests(unittest.TestCase):
    """The tablet's certificate has X.509 version field 1, which `cryptography` refuses."""

    @staticmethod
    def v2_certificate():
        import warnings
        from OpenSSL import crypto
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            key = crypto.PKey()
            key.generate_key(crypto.TYPE_RSA, 2048)
            cert = crypto.X509()
            cert.set_version(1)
            cert.get_subject().CN = "libdatachannel"
            cert.set_serial_number(1)
            cert.gmtime_adj_notBefore(0)
            cert.gmtime_adj_notAfter(3600)
            cert.set_issuer(cert.get_subject())
            cert.set_pubkey(key)
            cert.sign(key, "sha256")
        return cert

    def test_fingerprints_are_checked_without_parsing_the_certificate(self):
        import hashlib
        from types import SimpleNamespace
        from aiortc.rtcdtlstransport import RTCDtlsFingerprint, RTCDtlsParameters, RTCDtlsTransport, State
        from cryptography.x509 import InvalidVersion
        from OpenSSL import crypto
        from liveview import dtls
        cert = self.v2_certificate()
        with self.assertRaisesRegex(InvalidVersion, "1 is not a valid X509 version"):
            cert.to_cryptography()                                               # aiortc's own route
        self.assertIs(RTCDtlsTransport._validate_peer_identity, dtls.validate_peer_identity)   # installed
        der = crypto.dump_certificate(crypto.FILETYPE_ASN1, cert)
        good = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())

        def check(*prints):
            states = []
            transport = SimpleNamespace(_ssl=SimpleNamespace(get_peer_certificate=lambda: cert),
                                        _set_state=states.append)
            dtls.validate_peer_identity(transport, RTCDtlsParameters(
                fingerprints=[RTCDtlsFingerprint(a, v) for a, v in prints], role="auto"))
            return states
        self.assertEqual(check(("sha-256", good)), [])
        self.assertEqual(check(("SHA-256", good.lower())), [])
        with self.assertLogs("rm.liveview.dtls", "WARNING"):
            self.assertEqual(check(("sha-256", "00:" * 31 + "00")), [State.FAILED])
        with self.assertLogs("rm.liveview.dtls", "WARNING"):
            self.assertEqual(check(("md5", "00")), [State.FAILED])                # nothing supported
        with self.assertLogs("rm.liveview.dtls", "WARNING"):
            self.assertEqual(check(("sha-256", good), ("sha-512", "00")), [State.FAILED])   # all must match
        self.assertEqual(dtls.fingerprint(der, "sha-1"), ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest()))


class ServiceCase(unittest.TestCase):
    """A LiveViewService wired to a simulated tablet."""

    def start(self, script=None, **kw):
        tablet_kw = {k: kw.pop(k) for k in ("version", "size", "answers", "extra_channel", "tamper_fingerprint")
                     if k in kw}
        active = kw.pop("active", True)
        self.notifications = FakeNotifications()
        self.tablet = FakeTablet(self.notifications, script, **tablet_kw)
        self.rooms = FakeRooms(self.tablet, active=active)
        self.events: queue.Queue = queue.Queue()
        self.service = LiveViewService(None, self.events.put, rooms=self.rooms, notifications=self.notifications,
                                       device_id=DESKTOP, **kw)
        self.service.start()
        self.rooms.loop = self.service.loop
        self.addCleanup(self.stop)
        self.assertEqual(self.next_event(), NotificationsState(True))

    def stop(self):
        import asyncio
        if self.service.loop is not None and self.service.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.tablet.close(), self.service.loop).result(5)
        self.service.stop()

    def next_event(self, timeout=15.0):
        return self.events.get(timeout=timeout)

    def events_until(self, kind, timeout=20.0) -> list:
        seen, deadline = [], time.monotonic() + timeout
        while True:
            event = self.events.get(timeout=max(0.1, deadline - time.monotonic()))
            seen.append(event)
            if isinstance(event, kind):
                return seen

    def push(self, raw: str):
        self.service.loop.call_soon_threadsafe(self.notifications.push, raw)


class SessionTests(ServiceCase):
    def test_join_active_session_streams_the_screen(self):
        script = [rect_update([(1, 1, 2, 2, RED * 4)]), bytes([100]) + struct.pack(">HH", 3, 4),
                  bytes([102]) + struct.pack(">i", 90), bytes([103]), bytes([101])]
        self.start(script)
        self.notifications.end_first = 0
        self.service.join_active()
        events = self.events_until(Disconnected)
        kinds = [type(e).__name__ for e in events]
        self.assertEqual(kinds, ["Connecting", "Connected", "ServerHello", "FramebufferUpdate",
                                 "CursorMoved", "Rotated", "Disconnected"])
        self.assertEqual(events[2], ServerHello(2, 8, 6))
        self.assertEqual(events[3].rects, ((Rect(1, 1, 2, 2), RED * 4),))
        self.assertEqual((events[4], events[5]), (CursorMoved(3, 4), Rotated(90)))
        self.assertEqual(events[-1].reason, "ServerShutdown")
        self.assertEqual(self.tablet.received[0], b"reMarkable\x00\x02")      # our handshake
        self.assertEqual(self.rooms.calls[:2], [("join_active",), ("broadcast", ROOM, {"type": "request-offer"})])
        self.assertIn(("direct", ROOM, TABLET, "answer"), self.rooms.calls)

    def test_tablet_room_is_offered_then_accepted(self):
        self.start([bytes([103])])
        self.push(self.rooms.room_created(source=DESKTOP))                      # from ourselves: ignored
        self.push(notification("SyncComplete", TABLET))                         # not Screen Share
        self.push(self.rooms.room_created("r7"))
        self.assertEqual(self.next_event(), RoomOffered("r7", TABLET))
        self.service.accept("r7")
        self.assertEqual(self.events_until(Connected)[0], Connecting())
        self.assertEqual(self.rooms.calls[0], ("join", "r7"))
        self.assertEqual(self.next_event(), ServerHello(2, 8, 6))
        self.push(self.rooms.room_created("r8"))                                # busy: not offered
        self.service.join_active()                                              # busy: ignored
        self.service.disconnect()
        self.assertEqual(self.next_event(), Disconnected("UserInitiated"))
        self.assertTrue(self.events.empty())

    def test_no_active_session(self):
        self.start(active=False)
        self.service.join_active()
        self.assertEqual([self.next_event(), self.next_event()], [Connecting(), NoActiveSession()])

    def test_negotiation_timeout(self):
        self.start(answers=False, offer_timeout=0.3)
        self.service.join_active()
        events = self.events_until(Disconnected)
        self.assertEqual(events[-1].reason, "NegotiationTimeout")
        self.assertNotIn(Connected(), events)

    def test_ping_timeout(self):
        self.start([bytes([103])], ping_timeout=0.4)
        self.service.join_active()
        self.assertEqual(self.events_until(Disconnected)[-1].reason, "PingTimeout")

    def test_protocol_error(self):
        self.start([bytes([42])])
        self.service.join_active()
        last = self.events_until(Disconnected)[-1]
        self.assertEqual((last.reason, last.detail), ("ProtocolError", "unknown message type 42"))

    def test_notifications_reconnect(self):
        self.notifications = FakeNotifications()
        self.notifications.fail_first = 2
        self.notifications.end_first = 1
        self.tablet = FakeTablet(self.notifications)
        self.rooms = FakeRooms(self.tablet)
        self.events = queue.Queue()
        self.service = LiveViewService(None, self.events.put, rooms=self.rooms, notifications=self.notifications,
                                       device_id=DESKTOP, reconnect_delays=(0.01, 0.02))
        with self.assertLogs("rm.liveview.session", "INFO"):
            self.service.start()
            self.addCleanup(self.stop)
            self.assertEqual([self.next_event() for _ in range(5)],
                             [NotificationsState(False), NotificationsState(False), NotificationsState(True),
                              NotificationsState(False), NotificationsState(True)])   # ended cleanly, back
        self.assertEqual(self.notifications.connections, 4)

    def test_odd_signals_during_a_session_are_ignored(self):
        self.start([bytes([103])], extra_channel=True)                          # plus a second channel
        self.service.join_active()
        self.assertIn(ServerHello(2, 8, 6), self.events_until(ServerHello))
        stray = {"type": "webtrc", "payload": {"type": "offer", "description": "v=0"}}
        self.push(notification("ScreenshareMessage", "someone-else", stray))   # not our tablet
        self.push(notification("ScreenshareMessage", TABLET, {"type": "webtrc", "payload": {"type": "bogus"}}))
        self.push(notification("ScreenshareMessage", TABLET, {"type": "request-offer"}))
        self.push(notification("ScreenshareMessage", TABLET, {"type": "pong"}))
        with self.assertLogs("rm.liveview.session", "WARNING"):
            self.push(notification("ScreenshareMessage", TABLET,
                                   {"type": "webtrc", "payload": {"type": "candidate", "candidate": "candidate:junk"}}))
            time.sleep(0.3)
        self.service.disconnect()
        self.assertEqual(self.events_until(Disconnected)[-1], Disconnected("UserInitiated"))

    def test_room_api_failures_end_the_attempt(self):
        self.start()
        self.rooms.fail["join"] = SignalingError("HTTP 500")
        self.service.accept("r1")
        self.assertEqual(self.events_until(Disconnected)[-1],
                         Disconnected("NegotiationTimeout", "cannot join: HTTP 500"))
        del self.rooms.fail["join"]
        self.rooms.fail["request_offer"] = OSError("offline")
        self.service.accept("r1")
        self.assertEqual(self.events_until(Disconnected)[-1].detail, "cannot request an offer: offline")
        del self.rooms.fail["request_offer"]
        self.rooms.fail["send_signal"] = SignalingError("HTTP 403")
        self.service.accept("r1")
        last = self.events_until(Disconnected)[-1]
        self.assertEqual((last.reason, last.detail), ("WebRtcFailure", "signalling failed: HTTP 403"))

    def test_notifications_stream_ending_reconnects_and_stop_ends_a_session(self):
        self.start([bytes([103])], reconnect_delays=(0.01,))
        self.service.join_active()
        self.events_until(ServerHello)
        self.notifications.end_first = 1                                        # next connection ends
        self.service.loop.call_soon_threadsafe(lambda: None)
        self.service.stop()                                                     # with a session running
        self.assertIsNone(self.service.session)

    def test_wrong_dtls_fingerprint_fails_fast(self):
        self.start(tamper_fingerprint=True)
        started = time.monotonic()
        with self.assertLogs("rm.liveview.dtls", "WARNING"):
            self.service.join_active()
            last = self.events_until(Disconnected)[-1]
        self.assertEqual((last.reason, last.detail), ("WebRtcFailure", "the secure (DTLS) handshake failed"))
        self.assertLess(time.monotonic() - started, 10)                         # not the 30 s timeout

    def test_background_errors_end_the_session(self):
        self.start([bytes([103])])
        self.service.join_active()
        self.events_until(ServerHello)
        with self.assertLogs("rm.liveview.session", "ERROR"):
            self.service.loop.call_soon_threadsafe(self.service.loop.call_exception_handler,
                                                   {"message": "Task exception was never retrieved",
                                                    "exception": RuntimeError("boom")})
            last = self.events_until(Disconnected)[-1]
        self.assertEqual((last.reason, last.detail), ("WebRtcFailure", "RuntimeError: boom"))
        self.service.loop.call_soon_threadsafe(self.service.loop.call_exception_handler, {"message": "idle"})

    def test_requests_need_a_running_service(self):
        service = LiveViewService(None, print, rooms=FakeRooms(None), notifications=object(), device_id="")
        with self.assertRaisesRegex(RuntimeError, "not running"):
            service.join_active()
        service.disconnect()                                                    # harmless
        service.stop()


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        from ui.liveview import LiveViewWindow
        window = LiveViewWindow()
        window.resize(400, 300)
        self.addCleanup(window.deleteLater)
        return window

    def test_connecting_text_then_frames_rotation_and_cursor(self):
        from PySide6.QtCore import QPointF
        window = self.window()
        blank = window.grab().toImage()
        self.assertIsNone(window.image)
        self.assertGreater(len({blank.pixelColor(x, 150).name() for x in range(100, 300)}), 1)   # the text
        window.on_event(ServerHello(2, 40, 30))
        window.on_event(FramebufferUpdate(((Rect(0, 0, 20, 30), RED * 600),)))   # left half red
        window.on_event(Ping())                                                  # ignored
        transform = window.image_transform()
        self.assertEqual(transform.map(QPointF(0, 0)), QPointF(0, 0))           # 40x30 fills 400x300
        shot = window.grab().toImage()
        self.assertEqual(shot.pixelColor(50, 150).name(), "#ff0000")
        self.assertEqual(shot.pixelColor(350, 150).name(), "#ffffff")
        window.on_event(Rotated(90))                                             # turned: 30x40 in 400x300
        top_left = window.image_transform().map(QPointF(0, 0))
        self.assertAlmostEqual(top_left.x(), 87.5)                               # rotated by -90°
        self.assertAlmostEqual(top_left.y(), 300)
        window.on_event(CursorMoved(35, 5))
        centre = window.image_transform().map(QPointF(35, 5))
        dot = window.grab().toImage().pixelColor(round(centre.x()), round(centre.y()))
        self.assertGreater(dot.red(), 200)
        self.assertLess(dot.green(), 100)                                        # #CCF41515 over white
        window.on_event(CursorMoved(999, 5))                                     # off screen: hidden
        window.grab()
        with self.assertLogs("rm.liveview.rfb", "WARNING"):                     # outside: ignored
            window.on_event(FramebufferUpdate(((Rect(39, 0, 2, 1), RED * 2),)))

    def test_keys_and_close(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        window = self.window()
        closed = []
        window.closed.connect(lambda: closed.append(1))
        window.show()
        QTest.keyClick(window, Qt.Key.Key_F11)
        self.assertTrue(window.isFullScreen())
        window.grab()                                                            # dark background
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertFalse(window.isFullScreen())
        QTest.keyClick(window, Qt.Key.Key_F11)
        QTest.keyClick(window, Qt.Key.Key_F11)
        self.assertFalse(window.isFullScreen())
        QTest.keyClick(window, Qt.Key.Key_A)
        window.close()
        self.assertEqual(closed, [1])


class MainWindowScreenShareTests(unittest.TestCase):
    """The whole path: notification → accept dialog → session → window → tablet stops."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from fakecloud import FakeCloud, paired_auth
        from core.sync import SyncClient
        from ui.library import LibrarySource
        from ui.liveview import LiveViewController
        from ui.main_window import MainWindow
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.notifications = FakeNotifications()
        self.tablet = FakeTablet(self.notifications, [rect_update([(0, 0, 8, 6, RED * 48)]), bytes([103])])
        self.rooms = FakeRooms(self.tablet)
        self.controller = LiveViewController(lambda emit: LiveViewService(
            None, emit, rooms=self.rooms, notifications=self.notifications, device_id=DESKTOP))
        source = LibrarySource(SyncClient(paired_auth(base / "config", FakeCloud()), base / "cache"))
        self.window = MainWindow(source, base / "cache", liveview=self.controller)
        self.asked, self.ended = [], []
        self.window.ask_accept_screen_share = lambda: self.asked.append(1) or True
        self.window.show_screen_share_ended = self.ended.append
        self.window.start(sync=False)
        self.controller.start()
        self.rooms.loop = self.controller.service.loop

    def tearDown(self):
        import asyncio
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication
        asyncio.run_coroutine_threadsafe(self.tablet.close(), self.controller.service.loop).result(5)
        self.window.close()
        self.window.deleteLater()       # delete now: PySide's exit-time clean-up of live windows can crash
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    def wait_until(self, condition, timeout=15.0):
        from PySide6.QtTest import QTest
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("timed out")
            QTest.qWait(20)

    def push(self, raw):
        self.controller.service.loop.call_soon_threadsafe(self.notifications.push, raw)

    def test_offered_session_shows_the_tablet_until_it_stops(self):
        actions = self.window.navigator.actions
        self.assertTrue(actions.screen_share.isVisibleTo(self.window))
        self.push(self.rooms.room_created("r5"))
        self.wait_until(lambda: self.window.liveview_window is not None and self.window.liveview_window.image is not None)
        self.assertEqual(self.asked, [1])
        self.assertFalse(actions.screen_share.isEnabled())                      # busy
        live = self.window.liveview_window
        self.wait_until(lambda: live.grab().toImage().pixelColor(200, 300).name() == "#ff0000")
        self.assertEqual(self.window.status.text(), "Screen Share connected")
        self.controller.service.loop.call_soon_threadsafe(self.tablet.channel.send, bytes([101]))
        self.wait_until(lambda: self.window.liveview_window is None)
        self.assertEqual(self.ended, ["Screen Share ended: sharing was stopped on the tablet"])
        self.assertTrue(actions.screen_share.isEnabled())

    def test_default_dialogs(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from ui.main_window import MainWindow
        QTimer.singleShot(0, lambda: next(b for b in QApplication.activeModalWidget().buttons()
                                          if b.text() == "Accept").click())
        self.assertTrue(MainWindow.ask_accept_screen_share(self.window))
        QTimer.singleShot(0, lambda: next(b for b in QApplication.activeModalWidget().buttons()
                                          if b.text() == "Decline").click())
        self.assertFalse(MainWindow.ask_accept_screen_share(self.window))
        MainWindow.show_screen_share_ended(self.window, "Screen Share ended")
        from PySide6.QtWidgets import QMessageBox
        box = next(w for w in self.window.findChildren(QMessageBox) if w.isVisible())
        self.assertEqual((box.windowTitle(), box.text()), ("Screen Share is no longer active", "Screen Share ended"))
        box.buttons()[0].click()

    def test_join_button_no_session_and_closing_the_window(self):
        self.rooms.active = False
        self.window.navigator.actions.screen_share.click()
        self.wait_until(lambda: self.window.status.text() == "No active Screen Share sessions found")
        self.assertIsNone(self.window.liveview_window)
        self.rooms.active = True
        self.window.join_screen_share()
        self.wait_until(lambda: self.window.liveview_window is not None and self.window.liveview_window.image is not None)
        self.window.liveview_window.close()                                      # the user closes it
        self.wait_until(lambda: self.window.status.text() == "Screen Share ended")
        self.assertEqual(self.ended, [])                                         # no dialog for our own close
        self.window.ask_accept_screen_share = lambda: False                     # declined: nothing happens
        self.push(self.rooms.room_created("r6"))
        from PySide6.QtTest import QTest
        QTest.qWait(200)
        self.assertIsNone(self.window.liveview_window)


if __name__ == "__main__":
    unittest.main()
