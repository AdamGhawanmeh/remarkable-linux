"""Device pairing and token management for the reMarkable cloud.

Flow (ARCHITECTURE.md §3.2, mirrors xofm/libs/network/src/authentication.cpp):

    one-time code ──POST /token/json/2/device/new──▶ device token   (long-lived, persisted)
    device token  ──POST /token/json/2/user/new ───▶ user token JWT (short-lived, renewed)

The user token's `https://auth.remarkable.com/tectonic` claim selects the regional
API gateway `{tectonic}.tectonic.remarkable.com`; without it the client falls back
to `internal.cloud.remarkable.com`, as the Windows app does.

Only the standard library is used so this module has no runtime dependencies.
"""

from __future__ import annotations

import base64
import json
import logging
import platform
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .config import AuthState, AuthStore, Settings

__version__ = "0.1.0"

log = logging.getLogger("rm.auth")

DEVICE_NEW_PATH = "/token/json/2/device/new"
USER_NEW_PATH = "/token/json/2/user/new"
DEVICE_DELETE_PATH = "/token/json/3/device/delete"
DISCOVERY_PATH = "/discovery/v1/endpoints"

AUTH_SERVICE = "webapp"
# Region claim. Live user tokens carry plain "tectonic"; the namespaced form is
# the literal in reMarkable.exe's usertoken.cpp. Both are accepted.
TECTONIC_CLAIMS = ("tectonic", "https://auth.remarkable.com/tectonic")
TTL_HINT_HEADER = "rm-token-ttl-hint"
MIGRATE_AUTH_V2_HEADER = "rm-migrate-auth-v2"

# Tried in order when discovery yields nothing. Not a literal in reMarkable.exe;
# it is what /discovery/v1/endpoints returned for "webapp" on 2026-11-01.
FALLBACK_AUTH_HOSTS = ("webapp-prod.cloud.remarkable.engineering",)

PAIRING_URL = "https://my.remarkable.com/device/desktop/connect"

USER_TOKEN_RETRIES = 4


class AuthError(Exception):
    """Base class for authentication failures."""


class AuthNetworkError(AuthError):
    """No usable response from any candidate host."""


class PairingError(AuthError):
    """The server rejected the one-time code (`status`: its HTTP status, if it sent one)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotPairedError(AuthError):
    """An operation needs a device token but none is stored."""


class DeviceRevokedError(AuthError):
    """The server no longer accepts the stored device token; re-pair is required."""


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace").strip()

    def header(self, name: str) -> str | None:
        name = name.lower()
        for key, value in self.headers.items():
            if key.lower() == name:
                return value
        return None


# A transport takes (method, url, headers, body, timeout) and returns an HttpResponse.
# It raises OSError (incl. urllib.error.URLError) for connection-level failures.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], HttpResponse]


def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: bytes | None, timeout: float) -> HttpResponse:
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, dict(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as err:
        return HttpResponse(err.code, dict(err.headers.items()), err.read())


# http::Error of the official app's HTTP library (xofm/libs/http), as its name lookup
# (0x140309410) prints them; the sync check shows them.
HTTP_ERRORS = ("Operation cancelled (abort)", "Partial download error", "Host not found error", "Redirection error",
               "Token failure", "Error during response receipt", "SSL certificate time error",
               "SSL handshake failed", "Connecting timeout", "Send failed", "Unspecified network error")
HTTP_ERROR_UNKNOWN = "Unknown error!"
HOST_NOT_FOUND, RESPONSE_ERROR, CERTIFICATE_TIME_ERROR, HANDSHAKE_FAILED, UNSPECIFIED_NETWORK_ERROR = 2, 5, 6, 7, 10
# OpenSSL's X509_V_ERR_CERT_NOT_YET_VALID and X509_V_ERR_CERT_HAS_EXPIRED: a wrong clock.
CERTIFICATE_TIME_ERRORS = (9, 10)


def http_error_name(code: int) -> str:
    return HTTP_ERRORS[code] if 0 <= code < len(HTTP_ERRORS) else HTTP_ERROR_UNKNOWN


def http_error_code(error: BaseException) -> int:
    """The http::Error the official app gives a request that failed without an HTTP status
    to go by, per its curl backend (0x140344970; the Qt one, 0x140333fc0, agrees): an
    unresolved host is 2, a failure once the response had begun 5, a certificate not yet
    valid or expired 6, any other TLS failure 7, and everything else (refused, timed out,
    unreachable) 10."""
    import http.client
    import socket
    import ssl
    reason = getattr(error, "reason", error)        # urllib wraps socket and TLS errors
    if isinstance(reason, socket.gaierror):
        return HOST_NOT_FOUND
    if isinstance(reason, http.client.IncompleteRead):
        return RESPONSE_ERROR
    if isinstance(reason, ssl.SSLCertVerificationError):
        return CERTIFICATE_TIME_ERROR if reason.verify_code in CERTIFICATE_TIME_ERRORS else HANDSHAKE_FAILED
    if isinstance(reason, ssl.SSLError):
        return HANDSHAKE_FAILED
    return UNSPECIFIED_NETWORK_ERROR


def user_agent() -> str:
    # Same shape as the Windows app's "%1/%2 (%3 %4)".
    return f"remarkable-linux/{__version__} (linux {platform.release()})"


def _host_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("https://", "http://")) else f"https://{host}"


def _mask(token: str) -> str:
    return f"{token[:8]}…({len(token)} chars)" if token else "<empty>"


# --------------------------------------------------------------------------- JWT

def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying its signature.

    The signing key is server-side; the client only needs the claims for expiry
    and region. Raises ValueError for anything that is not a well-formed JWT.
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(parts[:2]):
        raise ValueError("token is not a JWT (expected header.payload.signature)")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as err:
        raise ValueError(f"JWT payload is not valid base64url JSON: {err}") from err
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not a JSON object")
    return claims


def token_expiry(token: str) -> float | None:
    try:
        exp = decode_jwt_claims(token).get("exp")
    except ValueError:
        return None
    return float(exp) if isinstance(exp, (int, float)) else None


def tectonic_region(token: str) -> str | None:
    try:
        claims = decode_jwt_claims(token)
    except ValueError:
        return None
    for key in TECTONIC_CLAIMS:
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def gateway_for_token(token: str, default_gateway: str) -> str:
    region = tectonic_region(token)
    return f"{region}.tectonic.remarkable.com" if region else default_gateway


# ---------------------------------------------------------------- host discovery

class HostResolver:
    """Resolves a service name (e.g. "webapp") to candidate hosts.

    Order: explicit settings override → /discovery/v1/endpoints on the gateway →
    FALLBACK_AUTH_HOSTS. The discovery response schema is not confirmed
    (ARCHITECTURE.md §3.2), so several plausible shapes are accepted.
    """

    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings = settings
        self.transport = transport
        self._discovered: dict[str, Any] | None = None

    def discovery_url(self) -> str:
        return _host_url(self.settings.default_gateway) + DISCOVERY_PATH

    def discover(self) -> dict[str, Any]:
        if self._discovered is None:
            url = self.discovery_url()
            try:
                resp = self.transport("GET", url, {"User-Agent": user_agent()},
                                      None, self.settings.http_timeout_s)
                data = json.loads(resp.body) if resp.status == 200 else {}
                self._discovered = data if isinstance(data, dict) else {}
            except (OSError, ValueError) as err:
                log.info("discovery unavailable (%s)", err)
                self._discovered = {}
            log.debug("discovery response keys: %s", sorted(self._discovered))
        return self._discovered

    @staticmethod
    def _extract_host(entry: Any) -> str | None:
        if isinstance(entry, str) and entry:
            return entry
        if isinstance(entry, dict):
            for key in ("host", "Host", "url", "URL", "hostname"):
                if isinstance(entry.get(key), str) and entry[key]:
                    return entry[key]
        return None

    def lookup(self, service: str) -> str | None:
        data = self.discover()
        # Shape A: {"webapp": "host"} / {"webapp": {"host": ...}}
        for key, value in data.items():
            if key.lower() == service.lower():
                return self._extract_host(value)
        # Shape B: {"endpoints": {...}} or {"services": {...}}
        for container in ("endpoints", "services"):
            nested = data.get(container)
            if isinstance(nested, dict):
                for key, value in nested.items():
                    if key.lower() == service.lower():
                        return self._extract_host(value)
        # Shape C: legacy service-manager {"Status": "OK", "Host": "..."}
        if data.get("Status") == "OK":
            return self._extract_host(data)
        return None

    def candidates(self, service: str = AUTH_SERVICE) -> list[str]:
        if self.settings.auth_host:
            return [self.settings.auth_host]
        hosts: list[str] = []
        found = self.lookup(service)
        if found:
            hosts.append(found)
        hosts.extend(FALLBACK_AUTH_HOSTS)
        return list(dict.fromkeys(hosts))


# ------------------------------------------------------------------ auth client

class AuthClient:
    """Owns the device/user token lifecycle.

    Thread-safe: `get_user_token()` may be called from any thread; at most one
    refresh is in flight at a time.
    """

    def __init__(self, settings: Settings | None = None, store: AuthStore | None = None,
                 transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings or Settings.load()
        self.store = store or AuthStore()
        self.transport = transport
        self.resolver = HostResolver(self.settings, transport)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
        self._state = self.store.load()
        self._stale = False
        self._timer: threading.Timer | None = None

    # -- state --------------------------------------------------------------

    @property
    def state(self) -> AuthState:
        return self._state

    @property
    def is_paired(self) -> bool:
        return self._state.is_paired

    def _save(self) -> None:
        self.store.save(self._state)

    def _post(self, host: str, path: str, *, json_body: dict | None = None,
              bearer: str | None = None) -> HttpResponse:
        headers = {"User-Agent": user_agent(), "Accept": "*/*"}
        body = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body).encode()
        else:
            body = b""
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        url = _host_url(host) + path
        log.debug("POST %s", url)
        return self.transport("POST", url, headers, body, self.settings.http_timeout_s)

    # -- pairing ------------------------------------------------------------

    def pair(self, one_time_code: str) -> str:
        """Exchange a one-time code for a device token and persist it."""
        code = one_time_code.strip()
        if not code:
            raise PairingError("one-time code is empty")
        body = {
            "code": code,
            "deviceDesc": self.settings.device_desc,
            "deviceID": self._state.device_id,
        }
        errors: list[str] = []
        last_error: OSError | None = None
        for host in self.resolver.candidates():
            try:
                resp = self._post(host, DEVICE_NEW_PATH, json_body=body)
            except OSError as err:
                errors.append(f"{host}: {err}")
                last_error = err
                continue
            if resp.status == 404:
                # Wrong host for this service; the code has not been consumed.
                errors.append(f"{host}: HTTP 404")
                continue
            if resp.status >= 400:
                # Stop here: retrying a rejected code elsewhere could burn it.
                raise PairingError(
                    f"{host} rejected the one-time code (HTTP {resp.status}): {resp.text[:200]}",
                    resp.status)
            token = resp.text
            if not token:
                raise PairingError(f"{host} returned an empty device token")
            with self._lock:
                self._state.device_token = token
                self._state.auth_host = host
                self._state.user_token = ""
                self._save()
            log.info("paired via %s, device token %s", host, _mask(token))
            return token
        # The cause keeps the socket/TLS error, so callers can tell e.g. a bad clock apart.
        raise AuthNetworkError("could not reach any auth host: " + "; ".join(errors)) from last_error

    def login(self, one_time_code: str) -> str:
        """Pair with a one-time code, then fetch the first user token; returns it.

        This is the app's log-in: it succeeds only once the account is usable. If the new
        device token yields no user token, the device token is forgotten again, so a failed
        log-in never leaves a half-paired auth.json behind (the next attempt needs a new
        code anyway).
        """
        self.pair(one_time_code)
        try:
            return self.fetch_user_token()
        except ValueError as err:          # not a JWT
            self.forget()
            raise AuthError(f"the server sent an invalid user token: {err}") from err
        except AuthError:
            self.forget()
            raise

    # -- user token ---------------------------------------------------------

    def _auth_host(self) -> str:
        if self._state.auth_host:
            return self._state.auth_host
        return self.resolver.candidates()[0]

    def fetch_user_token(self) -> str:
        """Request a fresh user token from the device token (with retry/backoff)."""
        with self._lock:
            if not self._state.device_token:
                raise NotPairedError("no device token; pair this client first")
            host = self._auth_host()
            delay = 1.0
            last_error = ""
            last_exception: OSError | None = None
            for attempt in range(USER_TOKEN_RETRIES + 1):
                if attempt:
                    log.info("retrying user token in %.0fs (attempt %d)", delay, attempt)
                    self._sleep(delay)
                    delay = min(delay * 2, 30.0)
                try:
                    resp = self._post(host, USER_NEW_PATH, bearer=self._state.device_token)
                except OSError as err:
                    last_error = str(err)
                    last_exception = err
                    continue
                if resp.header(MIGRATE_AUTH_V2_HEADER) is not None:
                    log.info("server signalled auth v2 migration (%s)", MIGRATE_AUTH_V2_HEADER)
                if resp.status in (401, 403):
                    raise DeviceRevokedError(
                        f"device token rejected (HTTP {resp.status}); re-pair this client")
                if resp.status == 429 or resp.status >= 500:
                    last_error = f"HTTP {resp.status}"
                    retry_after = resp.header("retry-after")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                    continue
                if resp.status >= 400:
                    raise AuthError(f"user token request failed (HTTP {resp.status}): "
                                    f"{resp.text[:200]}")
                token = resp.text
                decode_jwt_claims(token)  # raises ValueError if malformed
                self._state.user_token = token
                self._stale = False
                self._save()
                log.info("new user token %s, expires %s", _mask(token),
                         time.strftime("%F %T", time.localtime(token_expiry(token) or 0)))
                self._reschedule()
                return token
            raise AuthNetworkError(f"exhausted retries fetching user token: {last_error}") from last_exception

    def needs_refresh(self) -> bool:
        token = self._state.user_token
        if not token or self._stale:
            return True
        exp = token_expiry(token)
        if exp is None:
            return True
        return exp - self.settings.refresh_margin_s <= self._clock()

    def get_user_token(self, force: bool = False) -> str:
        """Return a valid user token, renewing it first if it is expired or about to."""
        with self._lock:
            if force or self.needs_refresh():
                return self.fetch_user_token()
            return self._state.user_token

    def note_response_headers(self, headers: Mapping[str, str]) -> None:
        """Feed API response headers back so the server can force an early refresh."""
        hint = next((v for k, v in headers.items() if k.lower() == TTL_HINT_HEADER), None)
        if hint is None:
            return
        try:
            ttl = int(hint)
        except ValueError:
            log.warning("unable to convert token TTL hint to integer: %r", hint)
            return
        if ttl <= self.settings.refresh_margin_s:
            log.info("got token TTL hint of %ss, marking user token stale", ttl)
            with self._lock:
                self._stale = True

    def gateway(self) -> str:
        """API gateway host for this user (regional tectonic host when known)."""
        return gateway_for_token(self._state.user_token, self.settings.default_gateway)

    def authorized_request(self, method: str, path_or_url: str, *,
                           headers: Mapping[str, str] | None = None,
                           body: bytes | None = None) -> HttpResponse:
        """Send a request to the gateway with the user token; re-auth once on 401."""
        url = path_or_url if "://" in path_or_url else _host_url(self.gateway()) + path_or_url
        for attempt in range(2):
            token = self.get_user_token(force=attempt > 0)
            hdrs = {"User-Agent": user_agent(), **(headers or {}),
                    "Authorization": f"Bearer {token}"}
            resp = self.transport(method, url, hdrs, body, self.settings.http_timeout_s)
            self.note_response_headers(resp.headers)
            if resp.status != 401:
                return resp
        return resp

    # -- background renewal -------------------------------------------------

    def _reschedule(self) -> None:
        if self._timer is None:
            return
        self._timer.cancel()
        self._schedule()

    def _schedule(self) -> None:
        exp = token_expiry(self._state.user_token)
        when = 0.0 if exp is None else max(0.0, exp - self.settings.refresh_margin_s - self._clock())
        self._timer = threading.Timer(when, self._timer_fired)
        self._timer.daemon = True
        self._timer.start()
        log.debug("scheduled token update in %.0fs", when)

    def _timer_fired(self) -> None:
        try:
            self.get_user_token()
        except AuthError as err:
            log.error("background token renewal failed: %s", err)
            with self._lock:
                if self._timer is not None:
                    # Back off and try again rather than giving up silently.
                    self._timer = threading.Timer(60.0, self._timer_fired)
                    self._timer.daemon = True
                    self._timer.start()

    def start_auto_refresh(self) -> None:
        """Renew the user token in the background shortly before it expires."""
        with self._lock:
            if self._timer is None:
                self._schedule()

    def stop_auto_refresh(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # -- logout -------------------------------------------------------------

    def unpair(self, remote: bool = True) -> None:
        """Best-effort server-side device delete (unless `remote` is False, e.g. offline),
        then forget local tokens."""
        self.stop_auto_refresh()
        with self._lock:
            if remote and self._state.device_token:
                try:
                    token = self._state.user_token or self._state.device_token
                    resp = self._post(self._auth_host(), DEVICE_DELETE_PATH, bearer=token)
                    log.info("device delete returned HTTP %d", resp.status)
                except OSError as err:
                    log.warning("device delete failed (%s); clearing local tokens anyway", err)
            self.forget()

    def forget(self) -> None:
        """Drop the device and user tokens from memory and auth.json (the device ID stays)."""
        self.stop_auto_refresh()
        with self._lock:
            self._state = self.store.clear_tokens()
            self._stale = False
