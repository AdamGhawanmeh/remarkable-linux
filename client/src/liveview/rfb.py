"""The Screen Share display stream: reMarkable's RFB-like protocol on the WebRTC data
channel labelled "screenshare".

Reconstructed from reMarkable.exe, screenshare/core/src/rfb/clientprotocol.cpp
(decompiled with Ghidra, ARCHITECTURE.md §4). Integers are big-endian (QDataStream).

Client → server, once the channel is open:
    "reMarkable" (10 raw bytes) + u16 protocol version (the app sends 2)

Server → client, a u8 message type followed by:
    104 Handshake         u16 version (0 means 1), u16 width, u16 height: a white
                          width × height framebuffer of RGB565 pixels (QImage::Format_RGB16)
      0 FramebufferUpdate u16 rectangle count, u32 length, then `length` bytes: a new zlib
                          stream that the tablet ends with a flush, not a final block (seen
                          live; the app inflates whatever is there), which contains per
                          rectangle:
                          u16 x, y, w, h, u32 size, then w × h RGB565 pixels as stored in
                          the QImage (little-endian), copied row by row
    100 CursorUpdate      u16 x, u16 y
    101 (no payload)      the server is shutting down
    102 RotationUpdate    version 1: u8, 1 → 0°, anything else → 270°;
                          version 2: i32 degrees
    103 Ping              no payload; the client disconnects after 20 s without one

Messages arrive in arbitrary pieces: an incomplete one waits for more data. An unknown
type is a protocol error (the app disconnects). An update that does not inflate, or a
malformed rectangle, is dropped with a warning, as the app does.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

log = logging.getLogger("rm.liveview.rfb")

MAGIC = b"reMarkable"
CLIENT_VERSION = 2
PING_TIMEOUT_S = 20.0
BYTES_PER_PIXEL = 2                  # RGB565
WHITE = b"\xff\xff"


def client_handshake(version: int = CLIENT_VERSION) -> bytes:
    return MAGIC + struct.pack(">H", version)


class MessageType(IntEnum):
    FRAMEBUFFER_UPDATE = 0
    CURSOR = 100
    SHUTDOWN = 101
    ROTATION = 102
    PING = 103
    HANDSHAKE = 104


class ProtocolError(ValueError):
    """The server sent something this protocol does not allow."""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def united(self, other: "Rect | None") -> "Rect":
        if other is None:
            return self
        x0, y0 = min(self.x, other.x), min(self.y, other.y)
        x1, y1 = max(self.x + self.w, other.x + other.w), max(self.y + self.h, other.y + other.h)
        return Rect(x0, y0, x1 - x0, y1 - y0)


# -- events --------------------------------------------------------------------------

@dataclass(frozen=True)
class ServerHello:
    version: int
    width: int
    height: int


@dataclass(frozen=True)
class FramebufferUpdate:
    rects: tuple[tuple[Rect, bytes], ...]    # rectangle, its RGB565 pixels


@dataclass(frozen=True)
class CursorMoved:
    x: int
    y: int


@dataclass(frozen=True)
class Rotated:
    degrees: int


@dataclass(frozen=True)
class Ping:
    pass


@dataclass(frozen=True)
class Shutdown:
    pass


Event = ServerHello | FramebufferUpdate | CursorMoved | Rotated | Ping | Shutdown


class StreamDecoder:
    """Turns data-channel bytes into events, keeping incomplete messages buffered."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.version = 1                     # until the server's handshake says otherwise

    def feed(self, data: bytes) -> list[Event]:
        self.buffer += data
        events: list[Event] = []
        while self.buffer:
            parsed = self._parse_one()
            if parsed is None:
                break
            event, used = parsed
            del self.buffer[:used]
            if event is not None:
                events.append(event)
        return events

    def _parse_one(self) -> tuple[Event | None, int] | None:
        buf, kind = self.buffer, self.buffer[0]
        if kind == MessageType.HANDSHAKE:
            if len(buf) < 7:
                return None
            version, width, height = struct.unpack_from(">HHH", buf, 1)
            self.version = version or 1
            return ServerHello(self.version, width, height), 7
        if kind == MessageType.FRAMEBUFFER_UPDATE:
            if len(buf) < 7:
                return None
            count, length = struct.unpack_from(">HI", buf, 1)
            if len(buf) < 7 + length:
                return None
            return _framebuffer_update(count, bytes(buf[7:7 + length])), 7 + length
        if kind == MessageType.CURSOR:
            if len(buf) < 5:
                return None
            return CursorMoved(*struct.unpack_from(">HH", buf, 1)), 5
        if kind == MessageType.ROTATION:
            if self.version == 1:
                if len(buf) < 2:
                    return None
                return Rotated(0 if buf[1] == 1 else 270), 2
            if len(buf) < 5:
                return None
            return Rotated(struct.unpack_from(">i", buf, 1)[0]), 5
        if kind == MessageType.PING:
            return Ping(), 1
        if kind == MessageType.SHUTDOWN:
            return Shutdown(), 1
        raise ProtocolError(f"unknown message type {kind}")


def _framebuffer_update(count: int, compressed: bytes) -> FramebufferUpdate | None:
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(compressed) + inflater.flush()
    except zlib.error as err:
        log.warning("failed to read zstream: %s", err)
        return None
    rects, offset = [], 0
    for index in range(count):
        if len(data) < offset + 12:
            log.warning("update rect %d: missing header (%d bytes left)", index, len(data) - offset)
            return None
        x, y, w, h, size = struct.unpack_from(">HHHHI", data, offset)
        offset += 12
        if len(data) < offset + size:
            log.warning("update rect %d: missing data (%d < %d)", index, len(data) - offset, size)
            return None
        rects.append((Rect(x, y, w, h), data[offset:offset + size]))
        offset += size
    if log.isEnabledFor(logging.DEBUG):
        log.debug("update: %d rect(s) %s, %d bytes inflated from %d", count,
                  ", ".join(f"{r.w}x{r.h}@{r.x},{r.y}:{len(px)}B" for r, px in rects[:4]), len(data), len(compressed))
    return FramebufferUpdate(tuple(rects))


class Framebuffer:
    """The screen as RGB565 pixels in a writable buffer (e.g. a QImage's bits())."""

    def __init__(self, width: int, height: int, pixels: bytearray | memoryview | None = None,
                 stride: int | None = None) -> None:
        self.width, self.height = width, height
        self.stride = stride or width * BYTES_PER_PIXEL
        self.pixels = pixels if pixels is not None else bytearray(WHITE * (width * height))

    def apply(self, update: FramebufferUpdate) -> Rect | None:
        """Copy the update's rectangles in; returns the area that changed."""
        changed = None
        for rect, data in update.rects:
            if rect.x + rect.w > self.width or rect.y + rect.h > self.height:
                log.warning("update rect %s outside the %dx%d framebuffer", rect, self.width, self.height)
                continue
            row = rect.w * BYTES_PER_PIXEL
            if len(data) < row * rect.h:
                log.warning("update rect %s: %d bytes for %d", rect, len(data), row * rect.h)
                continue
            for line in range(rect.h):
                start = (rect.y + line) * self.stride + rect.x * BYTES_PER_PIXEL
                self.pixels[start:start + row] = data[line * row:(line + 1) * row]
            changed = rect.united(changed)
        return changed
