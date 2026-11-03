"""Offline unit tests for core.config and core.auth (stdlib unittest, fake transport).

Run: python3 -m unittest discover -s client/tests -p 'test_*offline.py'
"""

from __future__ import annotations

import base64
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import auth  # noqa: E402
from core.auth import (  # noqa: E402
    AuthClient, DeviceRevokedError, HttpResponse, PairingError, decode_jwt_claims)
from core.config import AuthStore, Settings  # noqa: E402


def make_jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeTransport:
    """Routes requests by (method, url) to queued responses and records calls."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def add(self, method: str, url: str, *responses) -> None:
        self.routes.setdefault((method, url), []).extend(responses)

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        queue = self.routes.get((method, url))
        if not queue:
            raise OSError(f"connection refused: {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def resp(status: int, body: str | dict = "", headers: dict | None = None) -> HttpResponse:
    data = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    return HttpResponse(status, headers or {}, data)


GW = "https://internal.cloud.remarkable.com"
WEBAPP = "https://webapp.example"


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "remarkable-linux"
        self.settings = Settings()
        self.store = AuthStore(self.dir)
        self.t = FakeTransport()
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def client(self) -> AuthClient:
        return AuthClient(self.settings, self.store, self.t,
                          sleep=self.slept.append, clock=lambda: self.now)

    def test_pair_via_discovery_and_fetch_user_token(self):
        self.t.add("GET", GW + auth.DISCOVERY_PATH, resp(200, {"webapp": "webapp.example"}))
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(200, "device-jwt"))
        user = make_jwt({"exp": self.now + 3600, "tectonic": "eu"})  # live token shape
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH, resp(200, user))

        c = self.client()
        c.pair("  abcdefgh \n")
        sent = json.loads(self.t.calls[1][3])
        self.assertEqual(sent["code"], "abcdefgh")
        self.assertEqual(sent["deviceDesc"], "desktop-windows")
        self.assertEqual(sent["deviceID"], c.state.device_id)

        self.assertEqual(c.get_user_token(), user)
        self.assertEqual(self.t.calls[-1][2]["Authorization"], "Bearer device-jwt")
        self.assertEqual(c.gateway(), "eu.tectonic.remarkable.com")

        # Persisted with private permissions and reloadable.
        path = self.dir / "auth.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        reloaded = AuthStore(self.dir).load()
        self.assertEqual((reloaded.device_token, reloaded.user_token, reloaded.auth_host),
                         ("device-jwt", user, "webapp.example"))

    def test_pair_falls_back_when_discovery_unavailable(self):
        fallback = "https://" + auth.FALLBACK_AUTH_HOSTS[0]
        self.t.add("POST", fallback + auth.DEVICE_NEW_PATH, resp(200, "dev"))
        self.client().pair("code1234")
        self.assertEqual(self.store.load().auth_host, auth.FALLBACK_AUTH_HOSTS[0])

    def test_rejected_code_is_not_retried_on_other_hosts(self):
        self.t.add("GET", GW + auth.DISCOVERY_PATH, resp(200, {"webapp": "webapp.example"}))
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(400, "invalid one-time-code"))
        with self.assertRaises(PairingError):
            self.client().pair("badcode0")
        posts = [c for c in self.t.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_renewal_on_expiry_ttl_hint_and_401(self):
        self.settings.auth_host = "webapp.example"
        first = make_jwt({"exp": self.now + 3600})
        second = make_jwt({"exp": self.now + 7200})
        third = make_jwt({"exp": self.now + 9000})
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(200, "dev"))
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH, resp(200, first), resp(200, second),
                   resp(200, third))
        c = self.client()
        c.pair("code1234")
        self.assertEqual(c.get_user_token(), first)
        self.assertEqual(c.get_user_token(), first)  # cached

        self.now += 3600 - 100  # inside the 300s refresh margin
        self.assertEqual(c.get_user_token(), second)

        c.note_response_headers({"RM-Token-TTL-Hint": "30"})
        self.assertEqual(c.get_user_token(), third)

        # authorized_request retries once with a forced refresh after a 401.
        url = "https://internal.cloud.remarkable.com/sync/v4/root"
        self.t.add("GET", url, resp(401), resp(200, {"hash": "x"}))
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH, resp(200, third))
        self.assertEqual(c.authorized_request("GET", "/sync/v4/root").status, 200)

    def test_user_token_retries_then_revoked(self):
        self.settings.auth_host = "webapp.example"
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(200, "dev"))
        good = make_jwt({"exp": self.now + 3600})
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH,
                   resp(503), OSError("reset"), resp(429, "", {"Retry-After": "7"}), resp(200, good))
        c = self.client()
        c.pair("code1234")
        self.assertEqual(c.get_user_token(), good)
        self.assertEqual(self.slept, [1.0, 2.0, 7.0])

        self.t.routes[("POST", WEBAPP + auth.USER_NEW_PATH)] = [resp(401)]
        with self.assertRaises(DeviceRevokedError):
            c.get_user_token(force=True)

    def test_gateway_from_region_claim(self):
        default = self.settings.default_gateway
        for claims, expected in (
                ({"tectonic": "eu"}, "eu.tectonic.remarkable.com"),
                ({"https://auth.remarkable.com/tectonic": "us"}, "us.tectonic.remarkable.com"),
                ({"tectonic": ""}, default),
                ({}, default)):
            self.assertEqual(auth.gateway_for_token(make_jwt(claims), default), expected, claims)
        self.assertEqual(auth.gateway_for_token("not-a-jwt", default), default)

    def test_jwt_validation(self):
        with self.assertRaises(ValueError):
            decode_jwt_claims("not-a-jwt")
        self.assertEqual(decode_jwt_claims(make_jwt({"a": 1}))["a"], 1)

    def test_loose_permissions_are_tightened(self):
        self.store.save(self.store.load())
        (self.dir / "auth.json").chmod(0o644)
        self.store.load()
        self.assertEqual(stat.S_IMODE((self.dir / "auth.json").stat().st_mode), 0o600)

    # -- logging in and out (the app's log-in screen) --

    def test_login_pairs_and_fetches_the_first_user_token(self):
        self.settings.auth_host = "webapp.example"
        user = make_jwt({"exp": self.now + 3600})
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(200, "dev"))
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH, resp(200, user))
        c = self.client()
        self.assertEqual(c.login("abcdefgh"), user)
        saved = AuthStore(self.dir).load()
        self.assertEqual((saved.device_token, saved.user_token), ("dev", user))
        self.assertTrue(AuthClient(self.settings, AuthStore(self.dir), self.t).is_paired)

    def test_failed_login_leaves_no_half_paired_client(self):
        # The code buys a device token, but the account gives no user token for it.
        self.settings.auth_host = "webapp.example"
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(200, "dev"))
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH, resp(400, "You must connect a rM device"))
        c = self.client()
        device_id = c.state.device_id
        with self.assertRaises(auth.AuthError) as caught:
            c.login("abcdefgh")
        self.assertIn("You must connect a rM device", str(caught.exception))
        saved = AuthStore(self.dir).load()
        self.assertFalse(c.is_paired or saved.is_paired)
        self.assertEqual(saved.device_id, device_id)         # the installation keeps its ID
        # A rejected code: nothing was stored in the first place.
        self.t.routes[("POST", WEBAPP + auth.DEVICE_NEW_PATH)] = [resp(400, "invalid one-time-code")]
        with self.assertRaises(PairingError) as caught:
            c.login("wrongcod")
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse(AuthStore(self.dir).load().is_paired)

    def test_network_failure_keeps_its_cause(self):
        import ssl
        import urllib.error
        self.settings.auth_host = "webapp.example"
        expired = ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, urllib.error.URLError(expired))
        with self.assertRaises(auth.AuthNetworkError) as caught:
            self.client().pair("abcdefgh")
        self.assertIs(caught.exception.__cause__.reason, expired)

    def test_unpair_and_forget(self):
        self.settings.auth_host = "webapp.example"
        self.t.add("POST", WEBAPP + auth.DEVICE_NEW_PATH, resp(200, "dev"))
        self.t.add("POST", WEBAPP + auth.USER_NEW_PATH, resp(200, make_jwt({"exp": self.now + 3600})))
        self.t.add("POST", WEBAPP + auth.DEVICE_DELETE_PATH, resp(200))
        c = self.client()
        c.login("abcdefgh")
        calls = len(self.t.calls)
        c.unpair(remote=False)                               # offline: no request at all
        self.assertEqual(len(self.t.calls), calls)
        self.assertFalse(c.is_paired or AuthStore(self.dir).load().is_paired)
        c.login("abcdefgh")
        c.unpair()
        self.assertEqual(self.t.calls[-1][1], WEBAPP + auth.DEVICE_DELETE_PATH)
        self.assertFalse(AuthStore(self.dir).load().is_paired)

    def test_unreadable_auth_file_means_not_paired(self):
        self.dir.mkdir(parents=True)
        for content in ("{not json", "[1, 2]", "\udcff", '{"device_token": 7}'):
            (self.dir / "auth.json").write_text(content, errors="surrogateescape")
            with self.subTest(content=content):
                if content.startswith('{"'):                 # valid JSON, wrong type: skipped quietly
                    state = AuthStore(self.dir).load()
                else:
                    with self.assertLogs("rm.config", "WARNING"):
                        state = AuthStore(self.dir).load()
                self.assertFalse(state.is_paired)
                self.assertTrue(state.device_id)


if __name__ == "__main__":
    unittest.main()
