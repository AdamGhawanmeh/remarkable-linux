"""Screen Share signalling: the cloud room API and the notifications socket messages.

Reconstructed from reMarkable.exe (screenshare/core/src/{restbroker,roombroker}.cpp,
notifications/src/notifications.cpp, decompiled; ARCHITECTURE.md §4). The desktop app
is the viewer; the tablet owns the room and makes the WebRTC offer.

Room API on the account's gateway, under /screenshare/v1 (all POST, JSON):
    /rooms/{id}/join            accept a room the tablet created
    /rooms/join-active          join whichever room is active ("Join active session")
        → {"roomId": ..., "iceServers": [{"url", "username", "credential"}, ...]}
    /rooms/{id}/messages/broadcast  {"payload": {...}}                      to the room
    /rooms/{id}/messages/direct     {"payload": {...}, "targetClientId": id}  to one device

Notifications: wss://<discovery "notifications" host>/notifications/ws/json/1, with the
user token as a Bearer header. Each message is
    {"message": {"attributes": {"event": ..., "sourceDeviceID": ..., ...},
                 "data": base64(JSON)}}
Screen Share events:
    ScreenshareRoomCreated  attributes.roomId: the tablet started sharing
    ScreenshareMessage      data {"type": "request-offer"} or
                            data {"type": "webtrc", "payload": <WebRTC signal>}  ("webtrc" sic)
WebRTC signals (libdatachannel): {"type": "offer"|"answer", "description": <SDP>},
{"type": "candidate", "candidate": "candidate:..."}.

Flow: join the room, broadcast {"type": "request-offer"}, receive the tablet's offer as
a ScreenshareMessage, answer it by direct message to the tablet's sourceDeviceID.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from core.auth import AuthClient

log = logging.getLogger("rm.liveview.signaling")

BASE_PATH = "/screenshare/v1"
NOTIFICATIONS_PATH = "/notifications/ws/json/1"
ROOM_CREATED = "ScreenshareRoomCreated"
ROOM_MESSAGE = "ScreenshareMessage"
REQUEST_OFFER = "request-offer"
WEBRTC = "webtrc"                    # sic: the app's own spelling of the message type


class SignalingError(Exception):
    """A room request failed."""


class RoomNotFound(SignalingError):
    """There is no (such) active Screen Share room."""


@dataclass(frozen=True)
class IceServer:
    url: str
    username: str = ""
    credential: str = ""


@dataclass(frozen=True)
class Room:
    id: str
    ice_servers: tuple[IceServer, ...] = ()

    @classmethod
    def parse(cls, reply: dict[str, Any]) -> "Room":
        room_id = reply.get("roomId")
        if not isinstance(room_id, str) or not room_id:
            raise SignalingError(f"room reply without a roomId: {reply!r:.200}")
        servers = []
        for entry in reply.get("iceServers") or []:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                servers.append(IceServer(entry["url"], str(entry.get("username") or ""),
                                         str(entry.get("credential") or "")))
        return cls(room_id, tuple(servers))


@dataclass(frozen=True)
class Notification:
    event: str
    source: str                                   # sourceDeviceID
    attributes: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] | None = None            # the decoded base64 JSON, if any


def parse_notification(raw: str | bytes) -> Notification | None:
    """A notifications-socket message, or None if it is not one."""
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    message = doc.get("message") if isinstance(doc, dict) else None
    attributes = message.get("attributes") if isinstance(message, dict) else None
    if not isinstance(attributes, dict) or not attributes:
        return None
    data = None
    encoded = message.get("data")
    if isinstance(encoded, str) and encoded:
        try:
            decoded = json.loads(base64.b64decode(encoded))
            data = decoded if isinstance(decoded, dict) else None
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
            log.warning("notification data is not base64 JSON")
    return Notification(str(attributes.get("event", "")), str(attributes.get("sourceDeviceID", "")),
                        attributes, data)


def notifications_url(auth: AuthClient) -> str:
    host = auth.resolver.lookup("notifications") or auth.gateway()
    return f"wss://{host}{NOTIFICATIONS_PATH}"


class RoomClient:
    """The /screenshare/v1 room API (blocking; run it off the event loop)."""

    def __init__(self, auth: AuthClient) -> None:
        self.auth = auth

    def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.auth.authorized_request(
            "POST", BASE_PATH + path, headers={"Content-Type": "application/json"},
            body=json.dumps(body or {}).encode())
        if resp.status == 404:
            raise RoomNotFound(f"POST {path}: room not found")
        if not 200 <= resp.status < 300:
            raise SignalingError(f"POST {path} failed: HTTP {resp.status} {resp.text[:200]}")
        if not resp.body.strip():
            return {}
        try:
            reply = json.loads(resp.body)
        except json.JSONDecodeError as err:
            raise SignalingError(f"POST {path}: reply is not JSON") from err
        return reply if isinstance(reply, dict) else {}

    def join(self, room_id: str) -> Room:
        return Room.parse(self._post(f"/rooms/{room_id}/join"))

    def join_active(self) -> Room:
        return Room.parse(self._post("/rooms/join-active"))

    def broadcast(self, room_id: str, payload: dict[str, Any]) -> None:
        self._post(f"/rooms/{room_id}/messages/broadcast", {"payload": payload})

    def direct(self, room_id: str, target: str, payload: dict[str, Any]) -> None:
        self._post(f"/rooms/{room_id}/messages/direct", {"payload": payload, "targetClientId": target})

    def request_offer(self, room_id: str) -> None:
        self.broadcast(room_id, {"type": REQUEST_OFFER})

    def send_signal(self, room_id: str, target: str, signal: dict[str, Any]) -> None:
        self.direct(room_id, target, {"type": WEBRTC, "payload": signal})
