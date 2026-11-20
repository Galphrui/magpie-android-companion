"""Exercise Browser access with real local HTTPS and an isolated desktop fixture."""

import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import ssl
import tempfile
import threading
import unittest

import bridge
from test_bridge import serving


KEY = "fixture-desktop-browser-key"
SESSION = "/companion-browser/session"
PLUGIN_ASSET = "/_magpie/plugin/" + "a" * 32 + "/plugin.js"


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        if self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.server.stream_read.wait(5)
            return
        if self.path == "/api/link":
            self.send_response(303)
            self.send_header("Location", "/next")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/foreign-link":
            self.send_response(303)
            self.send_header("Location", "https://example.invalid/outside")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/missing":
            self.send_response(404)
            payload = b'{"error":"missing"}'
        else:
            self.send_response(200)
            payload = b'{"requests":17}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Magpie-Same", "fixture-revision")
        self.send_header("Set-Cookie", "desktop=" + KEY)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_HEAD = do_POST = respond


class BrowserBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.tls, _ = bridge.make_tls(folder.name)
        cls.trust = ssl.create_default_context(cafile=str(Path(folder.name) / "cert.pem"))

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        upstream = ThreadingHTTPServer((bridge.HOST, 0), FixtureHandler)
        upstream.requests = []
        upstream.stream_read = threading.Event()
        self.upstream = stack.enter_context(serving(upstream))
        self.token = secrets.token_urlsafe(32)
        adapter = bridge.Magpie(upstream.server_port, 3425, KEY, self.token)
        self.browser = stack.enter_context(serving(bridge.BrowserServer(0, self.tls, self.token, adapter)))
        self.host = f"{bridge.HOST}:{self.browser.server_port}"
        self.origin = f"https://{self.host}"
        self.bearer = {"Authorization": "Bearer " + self.token}

    def connect(self):
        return http.client.HTTPSConnection(bridge.HOST, self.browser.server_port, timeout=3, context=self.trust)

    def request(self, path, *, headers=None, method="GET", body=None):
        with contextlib.closing(self.connect()) as conn:
            conn.request(method, path, body, headers or {})
            with conn.getresponse() as response:
                return response.status, response.headers, response.read()

    def sign_in(self):
        status, headers, body = self.request(SESSION, headers=self.bearer)
        self.assertEqual((status, body), (204, b""))
        cookie = headers["Set-Cookie"]
        self.assertIsNotNone(cookie)
        self.assertNotIn(self.token, cookie)
        self.assertNotIn(KEY, cookie)
        for attribute in ("secure", "httponly", "samesite"):
            self.assertIn(attribute, cookie.lower().split("; ", 1)[1])
        return {"Cookie": cookie.split(";", 1)[0]}

    def test_bootstrap_and_proxy_use_separate_credentials(self):
        self.assertEqual(self.request(SESSION)[0], 401)
        self.assertEqual(self.request("/api/usage", headers=self.bearer)[0], 401)
        self.assertEqual(self.request("/api/usage", headers={"Cookie": f"magpie_web_{self.upstream.server_port}={KEY}"})[0], 401)
        session = self.sign_in()
        self.assertEqual(self.upstream.requests, [])
        status, headers, body = self.request("/api/usage?period=day",
                                             headers={**session, "X-Magpie-Had": "fixture-revision"})
        self.assertEqual((status, body), (200, b'{"requests":17}'))
        self.assertEqual(headers["X-Magpie-Same"], "fixture-revision")
        self.assertIsNone(headers["Set-Cookie"])
        method, target, forwarded, _ = self.upstream.requests[0]
        self.assertEqual((method, target), ("GET", "/api/usage?period=day"))
        self.assertEqual(forwarded["Cookie"], f"magpie_web_{self.upstream.server_port}={KEY}")
        self.assertEqual(forwarded["X-Magpie-Had"], "fixture-revision")
        self.assertNotIn("Authorization", forwarded)

    def test_foreign_origin_and_host_cannot_use_authenticated_session(self):
        session = self.sign_in()
        body = b'{"field":"model","value":"preset/example"}'
        for extra in ({}, {"Host": "example.invalid"}, {"Origin": "https://example.invalid"},
                      {"Origin": "https://127.0.0.1:65432", "Sec-Fetch-Site": "same-site"},
                      {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(extra=extra):
                status, _, _ = self.request("/api/set", method="POST", body=body, headers={**session, **extra})
                self.assertGreaterEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])
        status, _, _ = self.request("/api/set", method="POST", body=body,
                                    headers={**session, "Origin": self.origin,
                                             "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        method, target, forwarded, recorded_body = self.upstream.requests[0]
        self.assertEqual((method, target, recorded_body), ("POST", "/api/set", body))
        self.assertEqual(forwarded["Content-Type"], "application/json")

    def test_proxy_does_not_offer_gateway_or_desktop_sign_in(self):
        session = self.sign_in()
        for path in ("/v1/models", "/v1/models.json", "/_magpie/sign-in", f"/?k={KEY}",
                     "http://example.invalid/api/usage", "/api/../v1/models"):
            with self.subTest(path=path):
                status, _, _ = self.request(path, headers=session)
                self.assertGreaterEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.request("/api/missing", headers=session)[0], 404)
        status, headers, _ = self.request("/api/link", headers=session)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/next")
        _, headers, _ = self.request("/api/foreign-link", headers=session)
        self.assertIsNone(headers["Location"])
        self.assertEqual([r[1] for r in self.upstream.requests], ["/api/missing", "/api/link", "/api/foreign-link"])

    def test_plugin_assets_use_upstream_capability_path(self):
        status, _, _ = self.request(PLUGIN_ASSET, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[0][1], PLUGIN_ASSET)
        self.assertNotIn("Cookie", self.upstream.requests[0][2])
        status, _, _ = self.request(PLUGIN_ASSET, method="POST", body=b"{}", headers={"Origin": "null"})
        self.assertGreaterEqual(status, 400)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_stream_frames_arrive_before_upstream_closes(self):
        session = self.sign_in()
        try:
            with contextlib.closing(self.connect()) as conn:
                conn.request("GET", "/api/stream", headers=session)
                with conn.getresponse() as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Content-Type"], "text/event-stream")
                    self.assertEqual(response.readline(), b"data: first\n")
        finally:
            self.upstream.stream_read.set()
