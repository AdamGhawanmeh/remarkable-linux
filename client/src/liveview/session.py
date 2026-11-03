"""Screen Share sessions: the notifications socket, room joins and the WebRTC peer.

Everything network-facing runs on one asyncio loop in a background thread (aiortc is
asyncio-only); the UI talks to it through thread-safe methods and receives events
through a callback, which is called on that thread.

Like the app (screenshare/client/src/client.cpp), the service listens on the
notifications socket all the time, so a tablet that starts sharing is offered as soon
as it creates its room. A session then:
  1. joins the room (or the active one) → room id and ICE servers
  2. broadcasts {"type": "request-offer"} to the room
  3. receives the tablet's offer and trickled candidates (ScreenshareMessage "webtrc")
  4. answers by direct message to the tablet; aiortc puts its own candidates in the
     answer, which libdatachannel accepts
  5. on the "screenshare" data channel: writes the handshake, then decodes the stream

It ends with one of the app's disconnect reasons (DisconnectReason in clientfacade.cpp).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from core.auth import AuthClient, AuthError, user_agent

from . import dtls
from .rfb import (FramebufferUpdate, Ping, ProtocolError, Shutdown, StreamDecoder, client_handshake,
                  PING_TIMEOUT_S)
from .signaling import (REQUEST_OFFER, ROOM_CREATED, ROOM_MESSAGE, WEBRTC, Room, RoomClient,
                        RoomNotFound, SignalingError, notifications_url, parse_notification)

log = logging.getLogger("rm.liveview.session")

dtls.install()          # the tablet's DTLS certificate is X.509 v2 (see dtls.py)

CHANNEL_LABEL = "screenshare"
OFFER_TIMEOUT_S = 10.0          # the app's negotiation timer, until the tablet's offer
CONNECT_TIMEOUT_S = 30.0        # until the data channel opens (ICE through TURN can be slow)
RECONNECT_DELAYS_S = (1, 2, 5, 10, 30)

# DisconnectReason values from the app
PING_TIMEOUT = "PingTimeout"
SERVER_SHUTDOWN = "ServerShutdown"
USER_INITIATED = "UserInitiated"
WEBRTC_FAILURE = "WebRtcFailure"
PROTOCOL_ERROR = "ProtocolError"
NEGOTIATION_TIMEOUT = "NegotiationTimeout"


# -- events (delivered on the service thread) ------------------------------------------

@dataclass(frozen=True)
class RoomOffered:
    room_id: str
    source: str


@dataclass(frozen=True)
class Connecting:
    pass


@dataclass(frozen=True)
class Connected:
    pass


@dataclass(frozen=True)
class NoActiveSession:
    pass


@dataclass(frozen=True)
class Disconnected:
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class NotificationsState:
    connected: bool


# -- notifications ----------------------------------------------------------------------

class WebSocketNotifications:
    """wss://<notifications host>/notifications/ws/json/1 with the user token.

    messages() yields None once connected, then each message as it arrives.
    """

    def __init__(self, auth: AuthClient) -> None:
        self.auth = auth
        self._refresh_token = False

    async def messages(self) -> AsyncIterator[str | bytes]:
        import websockets
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(None, notifications_url, self.auth)
        force, self._refresh_token = self._refresh_token, False
        token = await loop.run_in_executor(None, lambda: self.auth.get_user_token(force=force))
        try:
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}",
                                                                   "User-Agent": user_agent()}) as socket:
                log.info("connected to %s", url)
                yield None
                async for message in socket:
                    yield message
        except websockets.InvalidStatus as err:
            if err.response.status_code == 401:
                self._refresh_token = True
            raise


# -- one session ------------------------------------------------------------------------

def _mid(sdp: str) -> str | None:
    """The mid of the data channel's m-line in an SDP."""
    for section in re.split(r"\r?\n(?=m=)", sdp):
        if section.startswith("m=application"):
            m = re.search(r"^a=mid:(\S+)", section, re.MULTILINE)
            return m.group(1) if m else None
    return None


class Session:
    def __init__(self, service: "LiveViewService", room: Room) -> None:
        self.service = service
        self.room = room
        servers = [RTCIceServer(urls=s.url, username=s.username or None, credential=s.credential or None)
                   for s in room.ice_servers]
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self.decoder = StreamDecoder()
        self.tablet: str | None = None
        self.mid: str | None = None
        self.stashed: list[str] = []
        self.channel = None
        self.opened = asyncio.Event()
        self.offered = asyncio.Event()
        self.last_ping: float | None = None
        self.ended: asyncio.Future[Disconnected] = asyncio.get_running_loop().create_future()

    def finish(self, reason: str, detail: str = "") -> None:
        if not self.ended.done():
            log.info("session ends: %s %s", reason, detail)
            self.ended.set_result(Disconnected(reason, detail))

    async def run(self) -> Disconnected:
        pc = self.pc

        @pc.on("datachannel")
        def on_datachannel(channel) -> None:
            if channel.label != CHANNEL_LABEL:
                log.info("ignoring data channel %r", channel.label)
                return
            self.channel = channel
            channel.on("message", self.on_data)
            channel.on("close", lambda: self.finish(WEBRTC_FAILURE, "the data channel closed"))
            if channel.readyState == "open":
                self.on_open()
            else:
                channel.on("open", self.on_open)

        @pc.on("connectionstatechange")
        def on_state() -> None:
            if pc.connectionState == "failed":
                dtls_failed = pc.sctp is not None and pc.sctp.transport.state == "failed"
                self.finish(WEBRTC_FAILURE, "the secure (DTLS) handshake failed" if dtls_failed
                            else "the WebRTC connection failed")

        watchdog = asyncio.create_task(self.watch())
        try:
            await self.service.call(self.service.rooms.request_offer, self.room.id)
            return await self.ended
        except (SignalingError, AuthError, OSError) as err:
            return Disconnected(NEGOTIATION_TIMEOUT, f"cannot request an offer: {err}")
        finally:
            watchdog.cancel()
            await pc.close()

    async def watch(self) -> None:
        """Negotiation and ping timeouts."""
        service = self.service
        try:
            await asyncio.wait_for(self.offered.wait(), service.offer_timeout)
            await asyncio.wait_for(self.opened.wait(), service.connect_timeout)
        except asyncio.TimeoutError:
            self.finish(NEGOTIATION_TIMEOUT, "the tablet did not connect")
            return
        while not self.ended.done():
            await asyncio.sleep(min(1.0, service.ping_timeout / 4))
            if self.last_ping is not None and service.clock() - self.last_ping > service.ping_timeout:
                self.finish(PING_TIMEOUT, f"no ping for {service.ping_timeout:.0f} s")

    async def on_signal(self, source: str, signal: dict[str, Any]) -> None:
        kind = signal.get("type")
        if kind == "offer":
            if self.tablet is not None and source != self.tablet:
                log.info("ignoring an offer from %s: talking to %s", source, self.tablet)
                return
            self.tablet = source
            sdp = str(signal.get("description") or "")
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            self.mid = _mid(sdp)
            if self.pc.sctp is not None:
                transport = self.pc.sctp.transport

                @transport.on("statechange")
                def on_dtls_state() -> None:
                    if transport.state == "failed":
                        self.finish(WEBRTC_FAILURE, "the secure (DTLS) handshake failed")
            self.offered.set()
            for candidate in self.stashed:
                await self.add_candidate(candidate)
            self.stashed.clear()
            await self.pc.setLocalDescription(await self.pc.createAnswer())
            answer = {"type": "answer", "description": self.pc.localDescription.sdp}
            await self.service.call(self.service.rooms.send_signal, self.room.id, source, answer)
        elif kind == "candidate":
            candidate = str(signal.get("candidate") or "")
            if self.pc.remoteDescription is None:
                self.stashed.append(candidate)        # arrived before the offer, as in the app
            else:
                await self.add_candidate(candidate)
        else:
            log.info("unknown signal %r from %s", kind, source)

    async def add_candidate(self, text: str) -> None:
        text = text.strip().removeprefix("a=")
        if not text.startswith("candidate:"):
            return                                       # end of candidates
        try:
            candidate = candidate_from_sdp(text.removeprefix("candidate:"))
            candidate.sdpMid, candidate.sdpMLineIndex = self.mid, 0
            await self.pc.addIceCandidate(candidate)
        except (ValueError, AssertionError) as err:
            log.warning("ignoring candidate %r: %s", text, err)

    def on_open(self) -> None:
        log.info("screenshare channel open; sending the handshake")
        self.channel.send(client_handshake())
        self.opened.set()
        self.service.emit(Connected())

    def on_data(self, data: bytes | str) -> None:
        try:
            events = self.decoder.feed(data.encode() if isinstance(data, str) else data)
        except ProtocolError as err:
            self.finish(PROTOCOL_ERROR, str(err))
            return
        for event in events:
            if not isinstance(event, (Ping, FramebufferUpdate)):
                log.info("stream: %s", event)
            if isinstance(event, Ping):
                self.last_ping = self.service.clock()
            elif isinstance(event, Shutdown):
                self.finish(SERVER_SHUTDOWN, "Server is shutting down")
                return
            else:
                self.service.emit(event)


# -- the service ------------------------------------------------------------------------

class LiveViewService:
    """Background thread with the notifications socket and at most one session."""

    def __init__(self, auth: AuthClient, emit: Callable[[object], None], *,
                 rooms: RoomClient | None = None, notifications: WebSocketNotifications | None = None,
                 device_id: str | None = None, clock: Callable[[], float] = time.monotonic,
                 offer_timeout: float = OFFER_TIMEOUT_S, connect_timeout: float = CONNECT_TIMEOUT_S,
                 ping_timeout: float = PING_TIMEOUT_S,
                 reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS_S) -> None:
        self.auth = auth
        self.emit = emit
        self.rooms = rooms or RoomClient(auth)
        self.notifications = notifications or WebSocketNotifications(auth)
        self.device_id = device_id if device_id is not None else auth.store.load().device_id
        self.clock = clock
        self.offer_timeout, self.connect_timeout, self.ping_timeout = offer_timeout, connect_timeout, ping_timeout
        self.reconnect_delays = reconnect_delays
        self.session: Session | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._listener: asyncio.Task | None = None
        self._ready = threading.Event()

    # -- thread ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="liveview", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._loop_error)
        self._listener = self.loop.create_task(self._listen())
        self.loop.call_soon(self._ready.set)
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def _loop_error(self, loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """An exception nobody awaited (e.g. inside aiortc's connect task): end the
        session with it now, rather than at the negotiation timeout."""
        error = context.get("exception")
        log.error("background error: %s", context.get("message"), exc_info=error)
        if self.session is not None:
            self.session.finish(WEBRTC_FAILURE, f"{type(error).__name__}: {error}" if error
                                else str(context.get("message", "")))

    def stop(self, timeout: float = 10.0) -> None:
        if self.loop is None or self._thread is None:
            return
        asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)
        self._thread = None

    async def _shutdown(self) -> None:
        if self.session is not None:
            self.session.finish(USER_INITIATED)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start_session(self, join: Callable, *args) -> None:
        if self.loop is None:
            raise RuntimeError("the LiveView service is not running")
        asyncio.run_coroutine_threadsafe(self._session(join, *args), self.loop)

    async def call(self, fn: Callable, *args) -> Any:
        """Run a blocking room request off the loop."""
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # -- requests (any thread) -----------------------------------------------------

    def join_active(self) -> None:
        self._start_session(self.rooms.join_active)

    def accept(self, room_id: str) -> None:
        self._start_session(self.rooms.join, room_id)

    def disconnect(self) -> None:
        def stop():
            if self.session is not None:
                self.session.finish(USER_INITIATED)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(stop)

    # -- on the loop -----------------------------------------------------------------

    async def _session(self, join: Callable, *args) -> None:
        if self.session is not None:
            log.info("a session is already running")
            return
        self.emit(Connecting())
        try:
            room = await self.call(join, *args)
        except RoomNotFound:
            self.emit(NoActiveSession())
            return
        except (SignalingError, AuthError, OSError) as err:
            self.emit(Disconnected(NEGOTIATION_TIMEOUT, f"cannot join: {err}"))
            return
        log.info("joined room %s (%d ICE servers)", room.id, len(room.ice_servers))
        self.session = Session(self, room)
        try:
            result = await self.session.run()
        finally:
            self.session = None
        self.emit(result)

    async def _listen(self) -> None:
        failures = 0
        while True:
            try:
                async for raw in self.notifications.messages():
                    if raw is None:                       # connected
                        failures = 0
                        self.emit(NotificationsState(True))
                    else:
                        await self._on_notification(raw)
                failures += 1
            except asyncio.CancelledError:
                raise
            except Exception as err:          # socket, TLS and HTTP errors: reconnect later
                log.info("notifications socket: %s: %s", type(err).__name__, err)
                failures += 1
            self.emit(NotificationsState(False))
            await asyncio.sleep(self.reconnect_delays[min(failures, len(self.reconnect_delays)) - 1])

    async def _on_notification(self, raw: str | bytes) -> None:
        note = parse_notification(raw)
        if note is None or (self.device_id and note.source == self.device_id):
            return                                               # not ours, or from ourselves
        if note.event == ROOM_CREATED:
            room_id = str(note.attributes.get("roomId") or "")
            if self.session is not None:
                log.info("ongoing session: not offering room %s", room_id)
            elif room_id:
                self.emit(RoomOffered(room_id, note.source))
        elif note.event == ROOM_MESSAGE and note.data:
            kind = note.data.get("type")
            if kind == WEBRTC and isinstance(note.data.get("payload"), dict) and self.session is not None:
                try:
                    await self.session.on_signal(note.source, note.data["payload"])
                except (SignalingError, AuthError, OSError, ValueError) as err:
                    self.session.finish(WEBRTC_FAILURE, f"signalling failed: {err}")
            elif kind != REQUEST_OFFER:
                log.info("unhandled Screen Share message %r", kind)
