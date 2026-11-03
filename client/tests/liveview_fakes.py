"""A simulated tablet for offline Screen Share tests.

FakeTablet is a real aiortc peer on loopback that behaves like the tablet as
reconstructed from reMarkable.exe: on "request-offer" it opens the "screenshare" data
channel and sends its offer without candidates, then trickles them one by one (as
libdatachannel does), all as ScreenshareMessage notifications. Once the client's
handshake arrives it speaks the display stream protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import zlib
from typing import Any

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from liveview.rfb import client_handshake
from liveview.signaling import REQUEST_OFFER, ROOM_CREATED, ROOM_MESSAGE, WEBRTC, Room, RoomNotFound

TABLET = "11111111-2222-4333-8444-555555555555"     # the tablet's device id
DESKTOP = "00000000-0000-4000-8000-000000000000"    # ours (paired_auth's device id)
ROOM = "room-1"


def notification(event: str, source: str, data: dict | None = None, **attributes) -> str:
    message: dict[str, Any] = {"attributes": {"event": event, "sourceDeviceID": source, **attributes}}
    if data is not None:
        message["data"] = base64.b64encode(json.dumps(data).encode()).decode()
    return json.dumps({"message": message})


def rect_update(rects: list[tuple[int, int, int, int, bytes]], finish: bool = False) -> bytes:
    """A FramebufferUpdate. Like the real tablet, the zlib stream ends with a sync flush,
    not a final block, unless `finish`."""
    inner = b"".join(struct.pack(">HHHHI", x, y, w, h, len(px)) + px for x, y, w, h, px in rects)
    deflater = zlib.compressobj()
    packed = deflater.compress(inner) + deflater.flush(zlib.Z_FINISH if finish else zlib.Z_SYNC_FLUSH)
    return bytes([0]) + struct.pack(">HI", len(rects), len(packed)) + packed


class FakeNotifications:
    """The notifications socket: yields None when connected, then queued messages."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue | None = None
        self.connections = 0
        self.fail_first = 0
        self.end_first = 0           # connections that end cleanly right after connecting

    async def messages(self):
        if self.queue is None:
            self.queue = asyncio.Queue()
        self.connections += 1
        if self.fail_first:
            self.fail_first -= 1
            raise OSError("connection refused")
        yield None
        if self.end_first:
            self.end_first -= 1
            return
        while True:
            yield await self.queue.get()

    def push(self, raw: str) -> None:
        self.queue.put_nowait(raw)


class FakeTablet:
    def __init__(self, notifications: FakeNotifications, script: list[bytes] | None = None,
                 version: int = 2, size: tuple[int, int] = (8, 6), answers: bool = True,
                 extra_channel: bool = False, tamper_fingerprint: bool = False) -> None:
        self.notifications = notifications
        self.script = script if script is not None else []
        self.version, self.size = version, size
        self.answers = answers          # False: never offers (negotiation timeout)
        self.extra_channel = extra_channel  # also open a channel that is not "screenshare"
        self.tamper_fingerprint = tamper_fingerprint   # offer a fingerprint that is not ours
        self.pc: RTCPeerConnection | None = None
        self.channel = None
        self.received: list[bytes] = []
        self.handshake_ok = asyncio.Event()

    def send(self, data: dict) -> None:
        self.notifications.push(notification(ROOM_MESSAGE, TABLET, data))

    async def on_request_offer(self) -> None:
        if not self.answers:
            return
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        if self.extra_channel:
            self.pc.createDataChannel("telemetry")
        self.channel = self.pc.createDataChannel("screenshare")
        self.channel.on("message", self.on_message)
        await self.pc.setLocalDescription(await self.pc.createOffer())
        sdp = self.pc.localDescription.sdp
        candidates = re.findall(r"^a=(candidate:.*?)\r?$", sdp, re.MULTILINE)
        bare = "".join(line + "\r\n" for line in sdp.splitlines()
                       if not line.startswith(("a=candidate:", "a=end-of-candidates")))
        if self.tamper_fingerprint:
            bare = re.sub(r"(a=fingerprint:sha-256 )[0-9A-F:]+", lambda m: m.group(1) + ":".join(["00"] * 32), bare)
        self.send({"type": WEBRTC, "payload": {"type": "candidate", "candidate": candidates[0]}})   # early
        self.send({"type": WEBRTC, "payload": {"type": "offer", "description": bare}})
        for candidate in candidates[1:]:
            self.send({"type": WEBRTC, "payload": {"type": "candidate", "candidate": candidate}})
        self.send({"type": WEBRTC, "payload": {"type": "candidate", "candidate": ""}})   # end of candidates

    async def on_answer(self, sdp: str) -> None:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    def on_message(self, data: bytes) -> None:
        self.received.append(data)
        if data == client_handshake():
            self.handshake_ok.set()
            self.channel.send(bytes([104]) + struct.pack(">HHH", self.version, *self.size))
            for chunk in self.script:
                self.channel.send(chunk)

    async def close(self) -> None:
        if self.pc is not None:
            await self.pc.close()


class FakeRooms:
    """The room API. Called from executor threads, like the real (blocking) client."""

    def __init__(self, tablet: FakeTablet, active: bool = True) -> None:
        self.tablet = tablet
        self.active = active
        self.loop: asyncio.AbstractEventLoop | None = None
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}      # method name -> error to raise

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def _room(self, room_id: str) -> Room:
        if not self.active:
            raise RoomNotFound("no room")
        return Room(room_id)

    def join(self, room_id: str) -> Room:
        self.calls.append(("join", room_id))
        self._check("join")
        return self._room(room_id)

    def join_active(self) -> Room:
        self.calls.append(("join_active",))
        return self._room(ROOM)

    def request_offer(self, room_id: str) -> None:
        self.calls.append(("broadcast", room_id, {"type": REQUEST_OFFER}))
        self._check("request_offer")
        asyncio.run_coroutine_threadsafe(self.tablet.on_request_offer(), self.loop)

    def send_signal(self, room_id: str, target: str, signal: dict) -> None:
        self.calls.append(("direct", room_id, target, signal["type"]))
        self._check("send_signal")
        if signal["type"] == "answer":
            asyncio.run_coroutine_threadsafe(self.tablet.on_answer(signal["description"]), self.loop)

    def room_created(self, room_id: str = ROOM, source: str = TABLET) -> str:
        return notification(ROOM_CREATED, source, roomId=room_id)
