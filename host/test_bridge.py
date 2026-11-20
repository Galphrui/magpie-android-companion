"""Live loopback TLS/HTTP integration, with fixture services and a fake local CLI."""
import contextlib
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import bridge

KEY = "test-browser-key-not-real"
MARKERS = ("PRIVATE_PROVIDER_KEY", "RAW_PROMPT_BODY", "/private/config/location", "PRIVATE_TRACE_LOG")


@contextlib.contextmanager
def serving(server):
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


class MockMagpie(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fixture, is_browser):
        self.fixture, self.is_browser = fixture, is_browser
        super().__init__((bridge.HOST, 0), MockHandler)


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        f = self.server.fixture
        cookie = self.headers.get("Cookie")
        f.requests.append((self.server.is_browser, self.path, cookie))
        if self.server.is_browser:
            if self.path not in (bridge.STATE, bridge.SETTINGS):
                self.reply(404, {"error": MARKERS[0]})
                return
            if cookie != f"magpie_web_{self.server.server_port}={KEY}" or f.mode == "denied":
                self.reply(401, {"error": KEY})
                return
            if f.mode == "redirect":
                self.send_response(303)
                self.send_header("Location", f"http://{bridge.HOST}:{f.gateway.server_port}/private")
                self.end_headers()
                return
            if f.mode == "big":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(bridge.MAX_STATE + 1))
                self.end_headers()
                return
            if f.mode == "uncached":
                self.reply(200, None)
                return
            if self.path == bridge.SETTINGS:
                self.reply(200, f.load_settings())
                return
            self.reply(200, {
                "last": True,
                "agents": [{"id": "codex", "name": "Codex", "path": MARKERS[2],
                            "fields": [{"key": "apiKey", "value": MARKERS[0]}, {"key": "model", "value": f.model}],
                            "own": [{"key": "model", "value": f.token}]},
                           {"id": "claude", "name": KEY, "fields": [{"key": "model", "value": "sk-fake-provider-credential"}]}],
                "settings": {"currency": "usd", "lanKey": KEY, "archive": MARKERS[1]},
                "options": [{"ref": "group/coding", "body": MARKERS[1]}],
                "logs": MARKERS[3],
            })
        elif self.path == bridge.CONCURRENCY:
            self.reply(200, {"object": "concurrency", "requests": f.in_flight,
                             "active": [{"prompt": MARKERS[1]}], "data": {KEY: {"busy": 98}}})
        elif self.path == bridge.TRACE and f.trace_ok:
            self.reply(200, {"routes": [{"model": f.model, "done": False, "prompt": MARKERS[1]},
                                        {"model": "group/coding", "done": True, "status": 500,
                                         "error": MARKERS[3], "sessionTitle": MARKERS[1]},
                                        {"model": MARKERS[0], "done": True, "status": 200}]})
        else:
            self.reply(404, {"error": MARKERS[0]})

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# Writes only the separate test fixture; no real Magpie process or files are used.
FAKE_CLI = '''
import json, pathlib, sys
path = pathlib.Path(__file__).with_name("settings.json")
record = pathlib.Path(__file__).with_name("invocations")
args = sys.argv[1:]
with record.open("a") as f:
    f.write(json.dumps(args) + "\\n")
settings = json.loads(path.read_text())
if args == ["settings", "get", "currency", "--json"]:
    if pathlib.Path(__file__).with_name("different-profile").exists():
        print(json.dumps("provider"))
    else:
        print(json.dumps(settings["currency"]))
elif len(args) == 4 and args[:3] == ["settings", "set", "currency"] and args[3] in {"usd", "cny", "provider"}:
    settings["currency"] = args[3]
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings))
    temp.replace(path)
else:
    raise SystemExit(2)
'''


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        holder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(holder.cleanup)
        cls.tls, cls.fingerprint = bridge.make_tls(holder.name)
        cls.cert = str(Path(holder.name) / "cert.pem")
        cls.trust = ssl.create_default_context(cafile=cls.cert)
        other = tempfile.TemporaryDirectory()
        cls.addClassCleanup(other.cleanup)
        cls.other_tls, cls.other_fingerprint = bridge.make_tls(other.name)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = self.root / "settings.json"
        self.config.write_text(json.dumps({"currency": "usd", "theme": "dark", "nonCurrency": {"secret": MARKERS[0]}}))
        self.cli = self.root / "fake-magpie"
        self.cli.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
        self.cli.chmod(0o700)
        self.requests = []
        self.mode, self.model, self.in_flight, self.trace_ok = "normal", "test/model-v1", 2, True
        self.web = self.stack.enter_context(serving(MockMagpie(self, True)))
        self.gateway = self.stack.enter_context(serving(MockMagpie(self, False)))

    def start_bridge(self, write=False):
        self.token = secrets.token_urlsafe(32)
        adapter = bridge.Magpie(self.web.server_port, self.gateway.server_port, KEY, self.token,
                                str(self.cli) if write else None)
        self.server = self.stack.enter_context(serving(bridge.BridgeServer(
            0, self.tls, self.token, adapter, interval=0.2, heartbeat=0.15)))
        self.auth = {"Authorization": "Bearer " + self.token}
        return self.server

    def load_settings(self):
        return json.loads(self.config.read_text())

    def desktop_change(self, **updates):
        other = self.config.with_suffix(".new")
        other.write_text(json.dumps({**self.load_settings(), **updates}))
        other.replace(self.config)

    def writes(self):
        calls = self.root / "invocations"
        return [x for x in map(json.loads, calls.read_text().splitlines()) if x[1] == "set"] if calls.exists() else []

    def conn(self, trust=None):
        return http.client.HTTPSConnection(bridge.HOST, self.server.server_port, timeout=4,
                                           context=self.trust if trust is None else trust)

    def stream(self):
        conn = self.conn()
        conn.request("GET", bridge.EVENTS, headers=self.auth)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(resp.readline(), b": connected\n")
        self.assertEqual(resp.readline(), b"\n")
        return conn, resp

    def event(self, resp, wanted=lambda _: True):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            line = resp.readline()
            self.assertTrue(line, "stream closed unexpectedly")
            if line.startswith(b"data: "):
                obj = json.loads(line[6:])
                self.assertEqual(resp.readline(), b"\n")
                if wanted(obj):
                    return obj
        self.fail("snapshot did not arrive")

    def request(self, method="GET", path=bridge.EVENTS, *, body=None, headers=None):
        with contextlib.closing(self.conn()) as conn:
            conn.request(method, path, body=body, headers=self.auth if headers is None else headers)
            resp = conn.getresponse()
            return resp.status, resp.read()

    def command(self, revision, value="cny", **extra):
        body = {"action": "setCurrency", "value": value, "expectedRevision": revision, **extra}
        return self.request("POST", bridge.COMMANDS, body=json.dumps(body),
                            headers={**self.auth, "Content-Type": "application/json"})

    def until(self, condition):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        self.fail("condition not met")

    def test_real_tls_pin_and_auth(self):
        self.start_bridge()
        self.assertEqual(len(self.fingerprint), 64)
        self.assertNotEqual(self.fingerprint, self.other_fingerprint)
        with contextlib.closing(self.conn()) as conn:
            conn.connect()
            self.assertEqual(hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest(), self.fingerprint)
            self.assertIn(conn.sock.version(), ("TLSv1.2", "TLSv1.3"))
        with contextlib.closing(self.conn(ssl.create_default_context())) as conn:
            with self.assertRaises(ssl.SSLCertVerificationError):
                conn.connect()
        with socket.create_connection(self.server.server_address, timeout=2) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            try:
                reply = sock.recv(128)
            except ConnectionResetError:
                reply = b""
            self.assertNotIn(b"HTTP/1.1", reply)
        for method, path in (("GET", bridge.EVENTS), ("POST", bridge.COMMANDS)):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, path, headers={})[0], 401)
                self.assertEqual(self.request(method, path, headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request(path="/api/state")[0], 404)
        self.assertEqual(self.request(path=bridge.EVENTS + "?secret=1")[0], 404)
        self.assertEqual(self.request(headers={**self.auth, "Origin": "null"})[0], 400)
        self.assertEqual(self.requests, [])

    def test_sse_freshness_heartbeat_and_secret_omission(self):
        self.start_bridge()
        conn, resp = self.stream()
        with contextlib.closing(conn), resp:
            snap = self.event(resp)
            self.assertEqual(snap["version"], 1)
            self.assertEqual(snap["gateway"], {"available": True, "inFlight": 2})
            self.assertEqual(snap["agents"], [{"id": "codex", "name": "Codex", "model": "test/model-v1"},
                                              {"id": "claude", "name": "claude", "model": ""}])
            self.assertEqual(snap["settings"]["currency"], "usd")
            self.assertRegex(snap["settings"]["revision"], "^[a-f0-9]{32}$")
            self.assertEqual(snap["routing"], [{"model": "group/coding", "outcome": "error"},
                                                {"model": "test/model-v1", "outcome": "inFlight"}])
            self.assertTrue(snap["upstreamAvailable"])
            self.assertFalse(snap["canWrite"])
            for secret in (*MARKERS, KEY, self.token, "sk-fake-provider-credential"):
                self.assertNotIn(secret, json.dumps(snap))
            self.assertEqual(resp.readline(), b": heartbeat\n")
            self.assertEqual(resp.readline(), b"\n")
            self.desktop_change(currency="cny")
            self.model, self.in_flight = "test/model-v2", 0
            updated = self.event(resp, lambda x: x["settings"]["currency"] == "cny" and x["gateway"]["inFlight"] == 0)
            self.assertEqual(updated["agents"][0]["model"], "test/model-v2")
            self.assertNotEqual(updated["settings"]["revision"], snap["settings"]["revision"])
            self.assertNotIn("test/model-v1", json.dumps(updated))
            self.assertEqual(self.command(updated["settings"]["revision"])[0], 403)
        for is_browser, path, cookie in self.requests:
            if is_browser:
                self.assertIn(path, (bridge.STATE, bridge.SETTINGS))
                self.assertEqual(cookie, f"magpie_web_{self.web.server_port}={KEY}")
            else:
                self.assertIn(path, (bridge.CONCURRENCY, bridge.TRACE))
                self.assertIsNone(cookie)

    def test_polling_stops_and_restarts_fresh(self):
        self.start_bridge()
        time.sleep(0.3)
        self.assertEqual(self.requests, [])
        first, stream1 = self.stream()
        second, stream2 = self.stream()
        with contextlib.closing(first), stream1, contextlib.closing(second), stream2:
            self.assertEqual(self.event(stream1), self.event(stream2))
        self.until(lambda: self.server.snapshots.clients == 0)
        # One in-progress pass may close out, but no later pass should start.
        time.sleep(0.25)
        count = len(self.requests)
        time.sleep(0.5)
        self.assertEqual(count, len(self.requests))
        self.desktop_change(currency="provider")
        conn, resp = self.stream()
        with contextlib.closing(conn), resp:
            self.assertEqual(self.event(resp)["settings"]["currency"], "provider")

    def test_opt_in_writes_recheck_fresh_value_without_overwriting(self):
        self.start_bridge(write=True)
        conn, resp = self.stream()
        with contextlib.closing(conn), resp:
            snap = self.event(resp)
            rev = snap["settings"]["revision"]
            self.assertTrue(snap["canWrite"])
            self.assertEqual(self.command(rev, value="--help")[0], 400)
            self.assertEqual(self.command(rev, value="usd", path="/tmp/bad")[0], 400)
            self.assertEqual(self.command(rev, action="setModel", agent="codex")[0], 400)
            # Desktop edit after SSE and before POST; no invocation of the setter.
            self.desktop_change(currency="provider", theme="light")
            self.assertEqual(self.command(rev)[0], 409)
            self.assertEqual(self.writes(), [])
            updated = self.event(resp, lambda x: x["settings"]["currency"] == "provider")
            latest = updated["settings"]["revision"]
            self.assertEqual(self.command(latest, value="provider")[0], 200)
            self.assertEqual(self.writes(), [])
            self.assertEqual(self.command(latest, value=" CNY ")[0], 200)
            self.assertEqual(self.command(latest, value="usd")[0], 409)
            self.assertEqual(self.writes(), [["settings", "set", "currency", "cny"]])
            self.assertEqual(self.load_settings(), {"currency": "cny", "theme": "light", "nonCurrency": {"secret": MARKERS[0]}})
            self.assertNotEqual(self.event(resp, lambda x: x["settings"]["currency"] == "cny")["settings"]["revision"], latest)

    def test_strict_command_encoding_and_payload_cap(self):
        self.start_bridge(write=True)
        headers = {**self.auth, "Content-Type": "application/json"}
        for raw in (b'{"action":"setCurrency","action":"setCurrency","value":"usd","expectedRevision":"' + b'0' * 32 + b'"}',
                    b'{"action":"setCurrency","value":"usd","expectedRevision":"bad"}',
                    b'{"action":"setCurrency","value":NaN,"expectedRevision":"' + b'0' * 32 + b'"}',
                    b'[]', b'{}{}', b'\xff'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", bridge.COMMANDS, body=raw, headers=headers)[0], 400)
        self.assertEqual(self.request("POST", bridge.COMMANDS, body=b"x" * (bridge.MAX_COMMAND + 1), headers=headers)[0], 413)
        self.assertEqual(self.request("POST", bridge.COMMANDS, body="{}", headers=self.auth)[0], 415)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.requests, [])

    def test_mismatched_cli_profile_is_not_written(self):
        self.start_bridge(write=True)
        (self.root / "different-profile").touch()
        conn, resp = self.stream()
        with contextlib.closing(conn), resp:
            rev = self.event(resp)["settings"]["revision"]
            self.assertEqual(self.command(rev)[0], 409)
            self.assertEqual(self.writes(), [])

    def test_unavailable_clears_snapshot_and_never_follows_redirect(self):
        self.start_bridge(write=True)
        conn, resp = self.stream()
        with contextlib.closing(conn), resp:
            rev = self.event(resp)["settings"]["revision"]
            self.mode = "redirect"
            down = self.event(resp, lambda x: not x["upstreamAvailable"])
            self.assertEqual(down["gateway"], {"available": False, "inFlight": None})
            self.assertEqual(down["settings"], {"currency": "", "revision": ""})
            self.assertEqual(down["agents"], [])
            self.assertNotIn("routing", down)
            self.assertEqual(self.command(rev)[0], 503)
            self.assertNotIn("/private", [r[1] for r in self.requests])
            self.mode = "normal"
            recovery = self.event(resp, lambda x: x["upstreamAvailable"])
            self.assertNotEqual(recovery["settings"]["revision"], rev)
            self.mode = "big"
            self.event(resp, lambda x: not x["upstreamAvailable"])

    def test_uncached_and_unknown_routing(self):
        self.mode, self.trace_ok = "uncached", False
        self.start_bridge()
        conn, resp = self.stream()
        with contextlib.closing(conn), resp:
            cold = self.event(resp)
            self.assertFalse(cold["upstreamAvailable"])
            self.assertTrue(cold["gateway"]["available"])
            self.assertNotIn("routing", cold)
            self.mode = "normal"
            warm = self.event(resp, lambda x: x["upstreamAvailable"])
            self.assertNotIn("routing", warm)

    def test_large_snapshot_is_valid_and_bounded(self):
        agents = [{"id": f"agent-{i}", "name": "龘" * 128, "model": "known/model"} for i in range(160)]
        frame = bridge.sse_frame({"agents": agents, "version": 1})
        self.assertLessEqual(len(frame), bridge.MAX_EVENT)
        self.assertTrue(frame.endswith(b"\n\n"))
        kept = json.loads(frame[6:])
        self.assertEqual(kept["version"], 1)
        self.assertGreater(len(kept["agents"]), 0)
        self.assertLess(len(kept["agents"]), len(agents))
        self.assertEqual(kept["agents"], agents[:len(kept["agents"])])

    def test_usb_pairing_file_is_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pairing.json"
            bridge.write_pairing_file(path, 43425, "a" * 64, "b" * 43)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            body = json.loads(path.read_text())
            self.assertEqual(body["transport"], "usb")
            self.assertEqual(body["origin"], "https://127.0.0.1:43425")
            self.assertEqual(body["pairing"], "a" * 64 + ":" + "b" * 43)

    def test_companion_port_accepts_default_and_custom_values(self):
        self.assertEqual(bridge.companion_port("43425"), 43425)
        self.assertEqual(bridge.companion_port("44444"), 44444)
        for value in ("1023", "65536", "no", "0"):
            with self.subTest(value=value), self.assertRaises(Exception):
                bridge.companion_port(value)

    def test_cannot_select_nonlocal_origin(self):
        self.assertEqual(bridge.browser_port("http://localhost:3430/"), 3430)
        for url in ("https://127.0.0.1:3430", "http://192.168.1.2:3430", "http://2130706433:3430",
                    "http://127.0.0.1:3430/?k=SECRET", "http://localhost.evil:3430",
                    "http://someone@127.0.0.1:3430", "http://127.0.0.1:43425", "http://127.0.0.1:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                bridge.browser_port(url)
        for value in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "192.168.1.999", "example.com", "2001:db8::1"):
            with self.subTest(value=value), self.assertRaises(Exception):
                bridge.lan_address(value)
        for value in ("10.23.42.7", "172.16.4.3", "192.168.100.2"):
            self.assertEqual(bridge.lan_address(value), value)

        with tempfile.TemporaryDirectory() as folder:
            _ctx, fingerprint = bridge.make_tls(folder, "192.168.3.50")
            cert = ssl._ssl._test_decode_cert(str(Path(folder) / "cert.pem"))
            addresses = [value for kind, value in cert["subjectAltName"] if kind == "IP Address"]
            self.assertEqual(set(addresses), {"127.0.0.1", "192.168.3.50"})
            self.assertEqual(len(fingerprint), 64)

        self.start_bridge()
        primary = self.server
        lan = self.stack.enter_context(serving(bridge.BridgeServer(
            0, self.tls, self.token, primary.snapshots.adapter, interval=0.2,
            heartbeat=0.15, bind=bridge.HOST, primary=primary)))
        self.assertIs(lan.snapshots, primary.snapshots)
        self.assertIs(lan.slots, primary.slots)
        self.server = lan
        self.assertEqual(self.request(path=bridge.EVENTS, headers={"Authorization": "Bearer wrong"})[0], 401)
        conn = self.conn()
        try:
            conn.request("GET", bridge.EVENTS, headers=self.auth)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            response.close()
        finally:
            conn.close()
        self.server = primary
        with patch.dict(os.environ, {"HTTP_PROXY": "http://203.0.113.1:9999", "NO_PROXY": ""}):
            conn, resp = self.stream()
            with contextlib.closing(conn), resp:
                self.assertTrue(self.event(resp)["upstreamAvailable"])


if __name__ == "__main__":
    unittest.main()
