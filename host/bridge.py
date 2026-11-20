#!/usr/bin/env python3
"""Magpie's allowlisted, laptop-side HTTPS companion service (protocol v1)."""

import argparse
import getpass
import hashlib
import hmac
import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
import warnings

HOST, PORT = "127.0.0.1", 43425
BROWSER_PORT = 43427
BROWSER_SESSION = "/companion-browser/session"
BROWSER_COOKIE = "__Host-magpie_companion"
EVENTS, COMMANDS = "/companion/v1/events", "/companion/v1/commands"
STATE, SETTINGS = "/api/state?last=1", "/api/settings"
CONCURRENCY, TRACE = "/v1/magpie/concurrency", "/v1/magpie/trace"
CURRENCIES = frozenset(("usd", "cny", "provider"))
POLL, HEARTBEAT, TRACE_POLL = 5.0, 5.0, 10.0
UPSTREAM_TIMEOUT, CLIENT_TIMEOUT, CLI_TIMEOUT = 2.0, 5.0, 6.0
MAX_STATE, MAX_GATEWAY, MAX_COMMAND = 2 << 20, 1 << 20, 1024
MAX_CLIENTS, MAX_STREAMS, MAX_EVENT = 12, 4, 60 << 10
BROWSER_KEY = re.compile(r"[A-Za-z0-9._~-]{16,128}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,95}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@-]*(?:/[A-Za-z0-9][A-Za-z0-9._+@-]*)*(?::[A-Za-z0-9][A-Za-z0-9._+@-]*)?\Z")
# Extra defense for misconfigured labels; allowlists, not this pattern, are the privacy boundary.
CREDENTIAL_LIKE = re.compile(r"(?:sk-[\w-]{6,}|(?:gh[pousr]_|github_pat_|xox[baprs]-)[\w-]+|AKIA[A-Z0-9]{16}|[A-Za-z0-9_=-]{40,})", re.ASCII)


class Unavailable(Exception):
    pass


def lan_address(value):
    """Only explicit RFC1918 IPv4 listeners; never wildcard/public/hostnames."""
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("use this computer's private IPv4 address") from exc
    if not any(address in ipaddress.IPv4Network(network)
               for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise argparse.ArgumentTypeError("LAN listener must use a private RFC1918 IPv4 address")
    return str(address)


def browser_port(url):
    # Pinned desktop always advertises plain HTTP on loopback, even with LAN TLS.
    if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}/?", url):
        raise ValueError("Browser URL must be http://127.0.0.1:PORT (localhost also accepted), without a key or path")
    port = urlsplit(url).port
    if port is None or not 1024 <= port <= 65535 or port == PORT:
        raise ValueError("invalid Browser access port")
    return port


def local_port(value):
    port = int(value)
    if not 1024 <= port <= 65535 or port == PORT:
        raise argparse.ArgumentTypeError("choose a local port 1024–65535 other than 43425")
    return port


def companion_port(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("choose a local port from 1024 to 65535")
    return port


def abort(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def deadline(seconds, sock):
    timer = threading.Timer(seconds, abort, args=(sock,))
    timer.daemon = True
    timer.start()
    return timer


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def bad_constant(_):
    raise ValueError("invalid JSON constant")


def decode_json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=bad_constant)


def json_bytes(data):
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def sse_frame(data):
    # Bound the entire line independently of upstream's sampled row limits.
    # Everything but the agents list is already small (12 short route summaries).
    start, end = b"data: ", b"\n\n"
    payload = json_bytes(data)
    if len(start) + len(payload) + len(end) > MAX_EVENT:
        base = json_bytes({**data, "agents": []})
        budget = MAX_EVENT - len(base) - len(start) - len(end)
        agents = []
        for row in data["agents"]:
            budget -= len(json_bytes(row)) + bool(agents)
            if budget < 0:
                break
            agents.append(row)
        payload = json_bytes({**data, "agents": agents})
    return start + payload + end


class Magpie:
    def __init__(self, web_port, gateway_port, key, token, cli=None):
        self.web_port, self.gateway_port = web_port, gateway_port
        # Same exact cookie that pinned webGuard issues for the pasted Browser key.
        # Do not send the key in the URL or to the separate gateway port.
        self.cookie = f"magpie_web_{web_port}={key}"
        self.private = (key, token)
        self.cli = cli
        self.currency = self.revision = ""
        self.edits = threading.Lock()
        self.trace_due = 0.0
        self.routes = None

    def _read(self, path):
        if path in (STATE, SETTINGS):
            port, cap, extra = self.web_port, MAX_STATE, {"Cookie": self.cookie}
        elif path in (CONCURRENCY, TRACE):
            port, cap, extra = self.gateway_port, MAX_GATEWAY, {}
        else:
            raise ValueError("not an upstream read")
        # Literal IP, no environment proxy, no DNS, no redirects and no cookies from response headers.
        conn = http.client.HTTPConnection(HOST, port, timeout=UPSTREAM_TIMEOUT)
        timer = None
        try:
            conn.connect()
            timer = deadline(UPSTREAM_TIMEOUT, conn.sock)
            conn.request("GET", path, headers={"Accept": "application/json", "Connection": "close", **extra})
            with conn.getresponse() as reply:
                if (reply.status != 200 or reply.headers.get_content_type() != "application/json"
                        or reply.headers.get("Content-Encoding", "identity") != "identity"
                        or (reply.length is not None and reply.length > cap)):
                    raise Unavailable()
                expected = reply.length
                raw = reply.read(cap + 1)
                if len(raw) > cap or (expected is not None and len(raw) != expected):
                    raise Unavailable()
                return decode_json(raw)
        except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
            raise Unavailable() from exc
        finally:
            if timer is not None:
                timer.cancel()
            conn.close()

    def _safe(self, value):
        return (isinstance(value, str) and 0 < len(value) <= 128
                and not any(secret in value for secret in self.private)
                and not CREDENTIAL_LIKE.search(value))

    def _model(self, value):
        return value if self._safe(value) and MODEL.fullmatch(value) else ""

    def _agents(self, rows):
        result, models, seen = [], set(), set()
        for row in rows[:160]:
            if not isinstance(row, dict):
                continue
            ident, name = row.get("id"), row.get("name")
            if not self._safe(ident) or not IDENT.fullmatch(ident) or ident in seen:
                continue
            if (not self._safe(name) or not all(c.isalnum() or c in " ._()+-" for c in name)):
                name = ident
            fields = row.get("fields")
            model = ""
            if isinstance(fields, list):
                for field in fields:
                    if isinstance(field, dict) and field.get("key") == "model":
                        model = self._model(field.get("value"))
                        break
            if model:
                models.add(model)
            seen.add(ident)
            result.append({"id": ident, "name": name, "model": model})
        return result, models

    def _route_summary(self, allow_models):
        data = self._read(TRACE)
        if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
            raise Unavailable()
        result = []
        # Trace order is upstream's order. Raw model IDs may be caller-controlled;
        # only repeat IDs we already saw in the trusted desktop state/catalog.
        for row in reversed(data["routes"][-60:]):
            if not isinstance(row, dict):
                continue
            model = self._model(row.get("model"))
            if not model or model not in allow_models or type(row.get("done")) is not bool:
                continue
            if not row["done"]:
                outcome = "inFlight"
            else:
                status = row.get("status")
                if type(status) is not int and not row.get("error"):
                    continue
                outcome = "success" if (not row.get("error") and type(status) is int and 200 <= status < 300) else "error"
            result.append({"model": model, "outcome": outcome})
            if len(result) == 12:
                break
        return result

    def _observe(self, currency):
        if currency != self.currency:
            self.currency = currency
            self.revision = secrets.token_hex(16) if currency else ""

    def _read_state(self):
        state = self._read(STATE)
        if state is not None and not (isinstance(state, dict) and state.get("last") is True
                                      and isinstance(state.get("agents"), list)
                                      and isinstance(state.get("settings"), dict)):
            raise Unavailable()
        return state

    def _currency(self, state):
        if state is None:
            return ""
        # The browser's cached state can lag settings written by another process.
        try:
            settings = self._read(SETTINGS)
            value = settings.get("currency") if isinstance(settings, dict) else None
        except Unavailable:
            return ""
        return value if isinstance(value, str) and value in CURRENCIES else ""

    def snapshot(self, active):
        with self.edits:
            if not active():
                return None
            try:
                state = self._read_state()
                authenticated = True
            except Unavailable:
                state, authenticated = None, False
            currency = self._currency(state)
            self._observe(currency)
            revision = self.revision
        agents, models = self._agents(state["agents"]) if state is not None else ([], set())
        result = {
            "version": 1, "gateway": {"available": False, "inFlight": None},
            "agents": agents, "settings": {"currency": currency, "revision": revision},
            "canWrite": bool(self.cli and revision), "upstreamAvailable": bool(state is not None and currency),
        }
        if not authenticated or not active():
            self.routes, self.trace_due = None, 0.0
            return result
        try:
            gateway = self._read(CONCURRENCY)
            if not isinstance(gateway, dict) or gateway.get("object") != "concurrency":
                raise Unavailable()
            count = gateway.get("requests")
            result["gateway"] = {"available": True, "inFlight": count if type(count) is int and 0 <= count < 2**31 else None}
        except Unavailable:
            self.routes, self.trace_due = None, 0.0
            return result
        if state is not None:
            options = state.get("options")
            if isinstance(options, list):
                for opt in options:
                    if isinstance(opt, dict):
                        ref = self._model(opt.get("ref"))
                        if ref:
                            models.add(ref)
            if not models:
                self.routes, self.trace_due = None, 0.0
            elif active() and time.monotonic() >= self.trace_due:
                try:
                    self.routes = self._route_summary(models)
                except Unavailable:
                    self.routes = None
                self.trace_due = time.monotonic() + TRACE_POLL
            if self.routes is not None:
                result["routing"] = [r for r in self.routes if r["model"] in models]
        return result

    def _cli_get(self):
        # Stream into a capped buffer rather than capturing unlimited subprocess output.
        with subprocess.Popen([self.cli, "settings", "get", "currency", "--json"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            # The target host is POSIX (macOS/Linux); select() also caps wall-clock time.
            end, raw = time.monotonic() + CLI_TIMEOUT, b""
            try:
                while len(raw) <= 128:
                    remaining = end - time.monotonic()
                    if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                        raise Unavailable()
                    part = os.read(proc.stdout.fileno(), 129 - len(raw))
                    if not part:
                        if proc.wait(timeout=max(0, end - time.monotonic())) == 0:
                            return decode_json(raw)
                        raise Unavailable()
                    raw += part
                raise Unavailable()
            finally:
                if proc.poll() is None:
                    proc.kill()

    def set_currency(self, value, expected):
        if not self.cli:
            return 403, "readOnly"
        if not self.edits.acquire(timeout=2 * UPSTREAM_TIMEOUT + 0.5):
            return 503, "busy"
        try:
            # Do not reuse the SSE snapshot: the desktop may have saved since it was sent.
            current = self._currency(self._read_state())
            self._observe(current)
            if not current:
                return 503, "upstreamUnavailable"
            if not hmac.compare_digest(self.revision, expected):
                return 409, "revisionConflict"
            if self._cli_get() != current:
                self._observe("")
                return 409, "revisionConflict"
            if value == current:
                return 200, "ok"
            subprocess.run([self.cli, "settings", "set", "currency", value],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=CLI_TIMEOUT, check=True)
            # Invalidate even before the next SSE poll; do not replay an old revision.
            self._observe(value)
            return 200, "changed"
        except (Unavailable, ValueError, RecursionError, OSError, subprocess.SubprocessError):
            self._observe("")
            return 503, "upstreamUnavailable"
        finally:
            self.edits.release()


class Snapshots:
    """One poller, one bounded snapshot, no work with zero stream subscribers."""
    def __init__(self, adapter, interval=POLL):
        self.adapter, self.interval = adapter, interval
        self.changed = threading.Condition()
        self.clients, self.epoch, self.generation = 0, 0, 0
        self.frame, self.due, self.stopped = None, 0.0, False
        self.last_poll = 0.0
        self.worker = threading.Thread(target=self._poll, daemon=True)
        self.worker.start()

    def join(self):
        with self.changed:
            if self.stopped or self.clients >= MAX_STREAMS:
                return False
            self.clients += 1
            self.changed.notify_all()
            return True

    def leave(self):
        with self.changed:
            self.clients -= 1
            if not self.clients:
                self.frame, self.due = None, 0.0
                self.epoch += 1
            self.changed.notify_all()

    def refresh(self):
        with self.changed:
            self.due = min(self.due, self.last_poll + min(self.interval, 1.0))
            self.changed.notify_all()

    def _poll(self):
        while True:
            with self.changed:
                while not self.stopped:
                    delay = self.due - time.monotonic()
                    if self.clients and delay <= 0:
                        break
                    self.changed.wait(delay if self.clients else None)
                if self.stopped:
                    return
                epoch = self.epoch
                self.last_poll = time.monotonic()
                self.due = self.last_poll + self.interval

            def active():
                with self.changed:
                    return bool(self.clients and not self.stopped and epoch == self.epoch)

            try:
                data = self.adapter.snapshot(active)
            except Exception:
                # Never print upstream payloads or exception text; clear uncertain state and retry later.
                self.adapter.routes, self.adapter.trace_due = None, 0.0
                data = {"version": 1, "gateway": {"available": False, "inFlight": None},
                        "agents": [], "settings": {"currency": "", "revision": ""},
                        "canWrite": False, "upstreamAvailable": False}
            if data is None:
                continue
            frame = sse_frame(data)
            with self.changed:
                if active() and frame != self.frame:
                    self.frame = frame
                    self.generation += 1
                    self.changed.notify_all()

    def close(self):
        with self.changed:
            self.stopped = True
            self.changed.notify_all()
        self.worker.join(timeout=3 * UPSTREAM_TIMEOUT + 2 * CLI_TIMEOUT + 1)


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "MagpieCompanion/1", ""

    def log_message(self, *_):
        pass  # No request targets, headers, payloads or exceptions in logs.

    def send_error(self, code, message=None, explain=None):
        self._reply(code, "invalidRequest")

    def handle_expect_100(self):
        self._reply(417, "invalidRequest")
        return False

    def _headers(self, code, content_type, length=None):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _reply(self, code, result):
        body = json_bytes({"ok": True} if code == 200 else {"error": result})
        self._headers(code, "application/json", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get_all("Authorization", [])
        if (len(header) != 1 or len(header[0]) != len(self.server.bearer) or not header[0].isascii()
                or not hmac.compare_digest(header[0], self.server.bearer)):
            self._reply(401, "unauthorized")
            return False
        # No browser origin/CORS access and no alternate HTTP request framing.
        if self.headers.get_all("Origin") or self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Expect"):
            self._reply(400, "invalidRequest")
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        if self.path != EVENTS or self.requestline.split()[1] != EVENTS:
            self._reply(404, "notFound")
            return
        if self.headers.get_all("Content-Length", []) not in ([], ["0"]):
            self._reply(400, "invalidRequest")
            return
        hub = self.server.snapshots
        if not hub.join():
            self._reply(503, "busy")
            return
        try:
            self.connection.request_deadline.cancel()
            self._headers(200, "text/event-stream; charset=utf-8")
            self.wfile.write(b": connected\n\n")
            seen = -1
            last_sent = time.monotonic()
            while True:
                with hub.changed:
                    if not hub.stopped and (hub.generation == seen or hub.frame is None):
                        hub.changed.wait(min(0.5, self.server.heartbeat))
                    if hub.stopped:
                        return
                    frame = hub.frame if hub.generation != seen else None
                    seen = hub.generation
                if select.select([self.connection], [], [], 0)[0]:
                    # SSE takes no further client bytes; notice a peer close promptly.
                    self.connection.setblocking(False)
                    try:
                        self.connection.recv(1)
                        return
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        pass
                    finally:
                        self.connection.settimeout(CLIENT_TIMEOUT)
                now = time.monotonic()
                if frame is not None or now - last_sent >= self.server.heartbeat:
                    self.wfile.write(frame if frame is not None else b": heartbeat\n\n")
                    last_sent = now
        finally:
            hub.leave()

    def do_POST(self):
        if not self._authorized():
            return
        if self.path != COMMANDS or self.requestline.split()[1] != COMMANDS:
            self._reply(404, "notFound")
            return
        if not self.server.snapshots.adapter.cli:
            self._reply(403, "readOnly")
            return
        ctypes = self.headers.get_all("Content-Type", [])
        lengths = self.headers.get_all("Content-Length", [])
        if len(ctypes) != 1 or self.headers.get_content_type() != "application/json":
            self._reply(415, "invalidRequest")
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self._reply(400, "invalidRequest")
            return
        length = int(lengths[0])
        if length > MAX_COMMAND:
            self._reply(413, "invalidRequest")
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            cmd = decode_json(raw)
            if not isinstance(cmd, dict) or set(cmd) != {"action", "value", "expectedRevision"}:
                raise ValueError()
            value, revision = cmd["value"], cmd["expectedRevision"]
            if (cmd["action"] != "setCurrency" or not isinstance(value, str) or not value.isascii()
                    or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{32}", revision)):
                raise ValueError()
            value = value.strip().lower()
            if value not in CURRENCIES:
                raise ValueError()
        except (ValueError, RecursionError):
            self._reply(400, "invalidCommand")
            return
        # The bounded CLI operation has its own timeout; abort timer only covers client input.
        self.connection.request_deadline.cancel()
        hub = self.server.snapshots
        status, result = hub.adapter.set_currency(value, revision)
        if result == "changed":
            hub.refresh()
        self._reply(status, result)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CLIENTS

    def __init__(self, port, tls, token, adapter, *, interval=POLL, heartbeat=HEARTBEAT,
                 bind=HOST, primary=None, handler=BridgeHandler):
        super().__init__((bind, port), handler)
        self.tls, self.bearer, self.heartbeat = tls, "Bearer " + token, heartbeat
        self.owns_snapshots = primary is None
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS) if primary is None else primary.slots
        self.snapshots = Snapshots(adapter, interval) if primary is None else primary.snapshots

    def process_request(self, request, addr):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, addr)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, addr):
        tls_sock = None
        timer = None
        try:
            request.settimeout(CLIENT_TIMEOUT)
            tls_sock = self.tls.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            timer = deadline(CLIENT_TIMEOUT, tls_sock)
            tls_sock.do_handshake()
            # Pass the input deadline to the handler without global/per-peer mutable state.
            tls_sock.request_deadline = timer
            self.finish_request(tls_sock, addr)
        except Exception:
            self.handle_error(request, addr)  # deliberately silent; never log payloads or upstream errors
        finally:
            if timer is not None:
                timer.cancel()
            self.shutdown_request(tls_sock if tls_sock is not None else request)
            self.slots.release()

    def handle_error(self, *_):
        pass

    def server_close(self):
        if self.owns_snapshots:
            self.snapshots.close()
        super().server_close()


class BrowserHandler(BridgeHandler):
    """Serve the installed browser UI to the verified phone's private USB origin."""

    request_headers = (
        "Accept", "Accept-Encoding", "Accept-Language", "Content-Type", "Range",
        "If-Match", "If-None-Match", "If-Modified-Since", "If-Unmodified-Since", "If-Range",
        "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest", "User-Agent",
        "X-Magpie-Had", "X-Magpie-Reading", "X-Magpie-Session", "X-Magpie-Tag",
    )
    blocked_response_headers = frozenset((
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "set-cookie", "set-cookie2",
        "server", "date", "cache-control", "clear-site-data", "www-authenticate",
    ))
    max_upload = 32 << 20

    def _same_secret(self, supplied, expected):
        return len(supplied) == len(expected) and supplied.isascii() and hmac.compare_digest(supplied, expected)

    def _open_session(self):
        headers = self.headers.get_all("Authorization", [])
        if self.command != "GET" or len(headers) != 1 or not self._same_secret(headers[0], self.server.bearer):
            return self._reply(401, "unauthorized")
        self.send_response(204)
        cookie = f"{BROWSER_COOKIE}={self.server.browser_token}; Path=/; Secure; HttpOnly; SameSite=Strict"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _has_session(self):
        for pair in self.headers.get("Cookie", "").split(";"):
            name, _, value = pair.strip().partition("=")
            if name == BROWSER_COOKIE and self._same_secret(value, self.server.browser_token):
                return True
        return False

    def _serve_browser(self):
        self.close_connection = True
        origin = self.server.origin
        client_host = self.server.client_host
        origins = self.headers.get_all("Origin", [])
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if (self.headers.get_all("Host", []) != [client_host] or len(origins) > 1 or len(sites) > 1
                or self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Expect")
                or self.headers.get_all("Upgrade")):
            return self._reply(400, "invalidRequest")
        target = self.requestline.split()[1]
        if not target.startswith("/") or target.startswith("//") or "#" in target:
            return self._reply(400, "invalidRequest")
        url = urlsplit(target)
        path = unquote(url.path)
        if ("\\" in path or "//" in path or any(part in (".", "..") for part in path.split("/"))
                or any(ord(char) < 32 for char in path)):
            return self._reply(400, "invalidRequest")
        if "k" in parse_qs(url.query.replace(";", "&"), keep_blank_values=True):
            return self._reply(400, "invalidRequest")
        reading = self.command in ("GET", "HEAD")
        lengths = self.headers.get_all("Content-Length", [])
        if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            return self._reply(400, "invalidRequest")
        length = int(lengths[0]) if lengths else 0
        if (reading and length) or length > self.max_upload:
            return self._reply(413, "invalidRequest")
        plugin_path = re.match(r"^/_magpie/plugin/[0-9a-f]{32}/(.*)$", path)
        plugin_service = bool(plugin_path and plugin_path[1].startswith("_magpie/service/"))
        plugin_asset = reading and bool(plugin_path) and not plugin_service
        if not plugin_asset and ((origins and origins != [origin]) or (sites and sites[0] not in ("same-origin", "none"))):
            return self._reply(403, "crossOrigin")
        if self.path == BROWSER_SESSION:
            return self._open_session()
        if not plugin_asset and not self._has_session():
            return self._reply(401, "unauthorized")
        api = path.startswith("/api/")
        static = reading and (path == "/" or bool(re.fullmatch(
            r"/(?:(?:icons|plugins)/(?:[^/]+/)*)?[^/]+\.(?:html|css|m?js|json|webmanifest|svg|png|ico|webp|jpg|jpeg|woff2?|ttf|avif)",
            path, re.I)))
        if plugin_service or not (api or plugin_asset or static) or (not api and not reading):
            return self._reply(404, "notFound")
        if not reading and origins != [origin] and sites != ["same-origin"]:
            return self._reply(403, "crossOrigin")

        headers = {key: self.headers[key] for key in self.request_headers if key in self.headers}
        headers.update({"Host": client_host, "Connection": "close"})
        if not plugin_asset:
            headers["Cookie"] = self.server.adapter.cookie
            if origins:
                headers["Origin"] = origin
        if lengths:
            headers["Content-Length"] = lengths[0]
        # Finish receiving and validating the bounded request before sending it.
        body = self.rfile.read(length) if length else None
        if length and len(body) != length:
            return self._reply(400, "invalidRequest")
        self.connection.request_deadline.cancel()
        timeout = 315 if path in ("/api/plugins/add", "/api/plugins/update", "/api/plugins/upgrade") else 90
        upstream = http.client.HTTPConnection(HOST, self.server.adapter.web_port, timeout=timeout)
        try:
            upstream.request(self.command, target, body=body, headers=headers)
            response = upstream.getresponse()
        except (OSError, http.client.HTTPException, ValueError):
            upstream.close()
            return self._reply(502, "desktopUnavailable")
        with response:
            try:
                self._relay_response(response)
            finally:
                upstream.close()

    def _relay_response(self, response):
        self.send_response(response.status)
        connection_options = {
            option.strip().lower()
            for name, value in response.getheaders() if name.lower() == "connection"
            for option in value.split(",")
        }
        for name, value in response.getheaders():
            key = name.lower()
            if key in self.blocked_response_headers or key in connection_options or key.startswith("access-control-"):
                continue
            if key == "location":
                location = urlsplit(value)
                if location.netloc in (self.server.client_host, f"{HOST}:{self.server.adapter.web_port}"):
                    value = urlunsplit(("", "", location.path or "/", location.query, location.fragment))
                elif location.scheme or location.netloc:
                    continue
            if key != "content-length" or not response.chunked:
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            while part := response.read1(64 << 10):
                self.wfile.write(part)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _serve_browser


class BrowserServer(BridgeServer):
    def __init__(self, port, tls, token, adapter):
        super().__init__(port, tls, token, adapter, handler=BrowserHandler)
        self.adapter = adapter
        self.browser_token = secrets.token_urlsafe(32)
        self.client_host = f"{HOST}:{self.server_port}"
        self.origin = f"https://{self.client_host}"

    def launch(self):
        threading.Thread(target=self.serve_forever, name="companion-browser", daemon=True).start()
        return self


def make_tls(folder, lan=None):
    cert, key, config = (Path(folder) / name for name in ("cert.pem", "key.pem", "openssl.cnf"))
    sans = "IP:127.0.0.1,DNS:localhost" + (f",IP:{lan}" if lan else "")
    config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                      f"[dn]\nCN=localhost\n[ext]\nsubjectAltName={sans}\n"
                      "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                      "extendedKeyUsage=serverAuth\n", encoding="ascii")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "7",
                    "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15, check=True)
    pem = cert.read_text(encoding="ascii")
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context, fingerprint


def browser_config_candidates():
    home = Path.home()
    roots = []
    if os.environ.get("MAGPIE_CONFIG_DIR"):
        roots.append(Path(os.environ["MAGPIE_CONFIG_DIR"]))
    roots.extend((home / ".config" / "magpie", home / "Library" / "Application Support" / "magpie"))
    return tuple(root / "web-page.json" for root in roots)


def read_auto_key():
    for path in browser_config_candidates():
        try:
            raw = path.read_bytes()
            if len(raw) > 4096:
                continue
            config = decode_json(raw)
            if not isinstance(config, dict):
                continue
            key = config.get("key")
            mode = config.get("mode")
            port = config.get("port", 0)
            if type(port) is int and port == 0:
                port = 3430
            if (mode in ("local", "lan") and type(port) is int and 1024 <= port <= 65535
                    and isinstance(key, str) and BROWSER_KEY.fullmatch(key)):
                return key, port
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    raise ValueError("Magpie Browser access is not enabled or web-page.json was not found")


def read_key(stdin):
    if stdin:
        key = sys.stdin.readline(130).rstrip("\r\n")
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Magpie Browser access key (not the whole link): ")
    if not BROWSER_KEY.fullmatch(key):
        raise ValueError("invalid Browser key format")
    return key


def write_pairing_file(path, port, fingerprint, token):
    """Write a short-lived ADB handoff file; caller chooses a protected host temp path."""
    target = Path(path).expanduser().absolute()
    payload = json_bytes({"version": 1, "transport": "usb", "origin": f"https://127.0.0.1:{port}",
                          "fingerprint": fingerprint, "pairing": f"{fingerprint}:{token}"})
    fd, tmp = tempfile.mkstemp(prefix=".magpie-usb-pairing-", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-url", default="http://127.0.0.1:3430", help="desktop Browser access localhost origin")
    parser.add_argument("--gateway-port", type=local_port, default=3425, help="local gateway's own port (default: 3425)")
    parser.add_argument("--browser-key-stdin", action="store_true", help="read key on stdin instead of hidden terminal prompt")
    parser.add_argument("--auto-browser-key", action="store_true", help="read only this user's existing Magpie browser access config; never change it")
    parser.add_argument("--allow-write", action="store_true", help="enable revision-checked currency writes")
    parser.add_argument("--magpie-bin", default="magpie", help="trusted local CLI executable (only for --allow-write)")
    parser.add_argument("--lan", type=lan_address, help="also listen on this computer's explicit private IPv4 address")
    parser.add_argument("--port", type=companion_port, default=PORT, help="local companion port (default: 43425)")
    parser.add_argument("--pairing-file", help="write a one-shot file for adb push into this companion app's app-specific files directory")
    parser.add_argument("--full-browser", action="store_true", help="serve Magpie's original management UI via authorized USB only")
    args = parser.parse_args(argv)
    try:
        port = browser_port(args.browser_url)
        if port == args.gateway_port:
            raise ValueError("Browser access and the gateway must use separate ports")
        exe = shutil.which(args.magpie_bin) if args.allow_write else None
        if args.allow_write and exe is None:
            raise ValueError("--allow-write needs the local Magpie CLI on PATH or --magpie-bin")
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if args.auto_browser_key:
            if args.browser_key_stdin:
                raise ValueError("choose only one browser key source")
            key, port = read_auto_key()
            if port in (args.port, args.gateway_port):
                raise ValueError("Browser, companion and gateway ports must differ")
        else:
            key = read_key(args.browser_key_stdin)
    except (ValueError, getpass.GetPassWarning, EOFError, OSError):
        parser.error("could not read the local Browser key securely; existing Browser access must be enabled for automatic discovery")
    except KeyboardInterrupt:
        return 130
    if args.full_browser and (not args.pairing_file or args.lan or BROWSER_PORT in (args.port, port, args.gateway_port)):
        parser.error("full browser requires USB handoff and loopback port 43427")
    token = secrets.token_urlsafe(32)
    try:
        adapter = Magpie(port, args.gateway_port, key, token, os.path.abspath(exe) if exe else None)
        with tempfile.TemporaryDirectory(prefix="magpie-companion-") as folder:
            tls, fingerprint = make_tls(folder, args.lan)
            pairing = f"{fingerprint}:{token}"
            if args.pairing_file and args.lan:
                raise ValueError("--pairing-file is for the local USB handoff only; do not combine with --lan")
            with BridgeServer(args.port, tls, token, adapter) as bridge:
                lan_server = None
                browser_server = None
                try:
                    if args.full_browser:
                        browser_server = BrowserServer(BROWSER_PORT, tls, token, adapter).launch()
                        print(f"Magpie full browser available on USB loopback port {BROWSER_PORT}", file=sys.stderr)
                    if args.pairing_file:
                        write_pairing_file(args.pairing_file, args.port, fingerprint, token)
                    if args.lan:
                        lan_server = BridgeServer(args.port, tls, token, adapter, bind=args.lan, primary=bridge)
                        threading.Thread(target=lan_server.serve_forever, name="companion-lan", daemon=True).start()
                    if not args.pairing_file:
                        print(pairing, flush=True)
                    print(f"Magpie companion listening on https://{HOST}:{args.port} (Ctrl-C to stop)", file=sys.stderr)
                    if args.lan:
                        print(f"LAN available only at https://{args.lan}:{args.port} (private IPv4; trusted network only)", file=sys.stderr)
                    bridge.serve_forever()
                finally:
                    if browser_server is not None:
                        browser_server.shutdown()
                        browser_server.server_close()
                    if lan_server is not None:
                        lan_server.shutdown()
                        lan_server.server_close()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        print("Could not start Magpie companion; check openssl and companion loopback ports.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
