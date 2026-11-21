"""BLE central for the Android companion. Carries opaque TLS records to local Magpie."""

import asyncio
from contextlib import AsyncExitStack, ExitStack, suppress
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import struct
import sys
import tempfile
import threading
import time
import uuid

import bridge


RX = "3a53a032-1eef-4c97-8c9a-15ac27a9f2a1"
TX = "3a53a032-1eef-4c97-8c9a-15ac27a9f2a2"
SUMMARY_PORT, BROWSER_PORT = 43435, 43437
HELLO, PHONE_NONCE, PHONE_PROOF, HOST_PROOF, READY = range(1, 6)
PING, PONG = 6, 7
OPEN, DATA, FIN, RESET = range(16, 20)
HEADER = struct.Struct(">BH")
MAX_FRAME = 244
IO_TIMEOUT, HANDSHAKE_TIMEOUT = 15, 20
# A QR code is deliberately short lived.  This bounds the time an abandoned
# scan can keep the desktop and phone on different pairing records.
PAIRING_TTL = 60
PAIRING_TIMEOUT = 35
CONNECT_TIMEOUT = 8
HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT = 3, 5
MAX_STREAMS = 12


class LinkError(Exception):
    pass


def proof(token, role, desktop_nonce, phone_nonce):
    message = b"magpie-ble-v1/" + role + desktop_nonce + phone_nonce
    return hmac.new(token.encode("ascii"), message, hashlib.sha256).digest()[:16]


@dataclass
class Stream:
    writer: asyncio.StreamWriter
    task: asyncio.Task | None = None
    phone_finished: bool = False
    host_finished: bool = False


class Relay:
    """One authenticated BLE connection and its bounded set of TCP streams."""

    def __init__(self, client, token, ports):
        self.client, self.token, self.ports = client, token, ports
        self.frames = asyncio.Queue(256)
        self.send_lock = asyncio.Lock()
        self.pong = asyncio.Event()
        self.streams = {}
        self.stopped = False
        self.loop = asyncio.get_running_loop()
        # Every frame is a single ATT value, including on low-MTU Bluetooth adapters.
        self.payload_size = max(20, min(MAX_FRAME, client.mtu_size - 3)) - HEADER.size

    def notification(self, _characteristic, value):
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.enqueue, bytes(value))

    def enqueue(self, frame):
        if self.stopped:
            return
        if self.frames.full():
            self.disconnect()
        else:
            self.frames.put_nowait(frame)

    def disconnect(self):
        self.stopped = True
        if not self.frames.full():
            self.frames.put_nowait(None)

    async def send(self, opcode, stream_id=0, payload=b""):
        async with self.send_lock:
            if self.stopped:
                raise LinkError("disconnected")
            if len(payload) > self.payload_size:
                raise LinkError("invalid frame size")
            try:
                await asyncio.wait_for(
                    self.client.write_gatt_char(RX, HEADER.pack(opcode, stream_id) + payload, response=True),
                    IO_TIMEOUT,
                )
            except Exception as error:
                self.disconnect()
                raise LinkError("could not send BLE frame") from error

    async def receive(self):
        frame = await self.frames.get()
        if self.stopped or frame is None or not HEADER.size <= len(frame) <= MAX_FRAME:
            raise LinkError("invalid frame or disconnected link")
        return (*HEADER.unpack_from(frame), frame[HEADER.size:])

    async def authenticate(self):
        desktop_nonce = secrets.token_bytes(16)
        await self.send(HELLO, payload=desktop_nonce)
        opcode, stream_id, phone_nonce = await self.receive()
        if opcode != PHONE_NONCE or stream_id or len(phone_nonce) != 16:
            raise LinkError("invalid handshake")
        opcode, stream_id, received_proof = await self.receive()
        expected = proof(self.token, b"phone", desktop_nonce, phone_nonce)
        if opcode != PHONE_PROOF or stream_id or not hmac.compare_digest(received_proof, expected):
            raise LinkError("phone verification failed")
        await self.send(HOST_PROOF, payload=proof(self.token, b"host", desktop_nonce, phone_nonce))
        if await self.receive() != (READY, 0, b""):
            raise LinkError("phone did not complete verification")

    def remove_stream(self, stream_id):
        stream = self.streams.pop(stream_id, None)
        if stream is not None:
            stream.writer.close()
            if stream.task is not None and stream.task is not asyncio.current_task():
                stream.task.cancel()

    async def pump(self, stream_id, stream, reader):
        try:
            while data := await reader.read(self.payload_size):
                await self.send(DATA, stream_id, data)
            await self.send(FIN, stream_id)
            stream.host_finished = True
            if stream.phone_finished:
                self.remove_stream(stream_id)
        except (LinkError, OSError, asyncio.TimeoutError):
            self.remove_stream(stream_id)
            with suppress(LinkError, OSError, asyncio.TimeoutError):
                await self.send(RESET, stream_id)

    async def dispatch(self, opcode, stream_id, payload):
        if (opcode, stream_id, payload) == (PONG, 0, b""):
            self.pong.set()
            return
        if not stream_id or opcode not in (OPEN, DATA, FIN, RESET):
            raise LinkError("invalid stream frame")
        stream = self.streams.get(stream_id)
        if opcode == OPEN:
            if (stream is not None or len(payload) != 1 or payload[0] not in self.ports
                    or len(self.streams) >= MAX_STREAMS):
                await self.send(RESET, stream_id)
                return
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(bridge.HOST, self.ports[payload[0]]), IO_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                await self.send(RESET, stream_id)
                return
            stream = Stream(writer)
            self.streams[stream_id] = stream
            stream.task = asyncio.create_task(self.pump(stream_id, stream, reader))
        elif stream is not None:
            try:
                if opcode == DATA and payload and not stream.phone_finished:
                    stream.writer.write(payload)
                    await asyncio.wait_for(stream.writer.drain(), IO_TIMEOUT)
                elif opcode == FIN and not payload and not stream.phone_finished:
                    stream.phone_finished = True
                    if stream.host_finished:
                        self.remove_stream(stream_id)
                    else:
                        stream.writer.write_eof()
                else:
                    self.remove_stream(stream_id)
            except (OSError, asyncio.TimeoutError):
                self.remove_stream(stream_id)
                await self.send(RESET, stream_id)

    async def close(self):
        self.disconnect()
        streams = list(self.streams.values())
        for stream_id in list(self.streams):
            self.remove_stream(stream_id)
        await asyncio.gather(*(stream.task for stream in streams), return_exceptions=True)

    async def heartbeat(self):
        async def ping():
            await self.send(PING)
            await self.pong.wait()

        while not self.stopped:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            self.pong.clear()
            try:
                await asyncio.wait_for(ping(), HEARTBEAT_TIMEOUT)
            except (LinkError, asyncio.TimeoutError):
                self.disconnect()
                return


class DesktopSession:
    """Pair one phone and keep its local HTTPS bridge alive until stopped."""

    def __init__(self, browser_port, browser_key, gateway_port=3425, status=lambda _message: None,
                 pairing=lambda _code: None):
        self.browser_port, self.browser_key, self.gateway_port = browser_port, browser_key, gateway_port
        self.status = status
        self.pairing = pairing
        self.service_id = str(uuid.uuid4())
        self.token = secrets.token_urlsafe(32)
        self.fingerprint = ""
        self.pairing_code = ""
        self._pairing_deadline = None
        self.stack = ExitStack()

    def __enter__(self):
        try:
            folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="magpie-ble-"))
            tls, self.fingerprint = bridge.make_tls(folder, portable=True)
            adapter = bridge.Magpie(self.browser_port, self.gateway_port, self.browser_key, self.token)
            summary = self.stack.enter_context(
                bridge.BridgeServer(0, tls, self.token, adapter, client_timeout=75))
            browser = self.stack.enter_context(
                bridge.BrowserServer(0, tls, self.token, adapter, client_port=BROWSER_PORT, client_timeout=75))
            for server in (summary, browser):
                threading.Thread(target=server.serve_forever, name="magpie-ble-http", daemon=True).start()
                self.stack.callback(server.shutdown)
            self.ports = {1: summary.server_port, 2: browser.server_port}
            self._update_pairing_code()
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *_):
        self.pairing_code = ""
        self.pairing("")
        self.stack.close()

    def _update_pairing_code(self):
        self.pairing_code = f"magpie-ble:1:{self.service_id}:{self.fingerprint}:{self.token}"
        self._pairing_deadline = time.monotonic() + PAIRING_TTL
        self.pairing(self.pairing_code)

    def rotate_pairing(self):
        # The HTTPS bridge keeps this session's token, so expire scans by rotating
        # its BLE service ID while retaining that bridge credential.
        self.service_id = str(uuid.uuid4())
        self._update_pairing_code()

    async def _connect_with_timeout(self, client_class, device):
        """Wait for authentication, then keep the relay alive until disconnect."""
        ready = asyncio.Event()
        task = asyncio.create_task(self.connect_phone(client_class, device, ready))
        waiter = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait(
                (task, waiter), timeout=PAIRING_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.TimeoutError("Bluetooth pairing timed out")
            if task in done:
                await task
                return
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await task
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def run(self):
        from bleak import BleakClient
        from bleak.exc import BleakError

        while True:
            if time.monotonic() >= self._pairing_deadline:
                self.rotate_pairing()
            message = "等待手机扫描二维码"
            if sys.platform == "darwin":
                from CoreBluetooth import CBManager, CBManagerAuthorizationNotDetermined

                if CBManager.authorization() == CBManagerAuthorizationNotDetermined:
                    message = "等待 macOS 蓝牙授权，请在系统提示中选择允许"
            self.status(message)
            device = await self._find_phone(max(0, self._pairing_deadline - time.monotonic()))
            if device is None:
                continue
            try:
                await self._connect_with_timeout(BleakClient, device)
            except (BleakError, LinkError, OSError, asyncio.TimeoutError):
                self.rotate_pairing()
                self.status("蓝牙连接已断开，二维码已更新，请重新扫描")
                await asyncio.sleep(1)

    async def _find_phone(self, timeout):
        from bleak import BleakScanner

        service_id = self.service_id
        return await BleakScanner.find_device_by_filter(
            lambda _device, advertising: any(service_id == str(uuid).lower()
                                             for uuid in advertising.service_uuids),
            timeout=timeout, service_uuids=[service_id])

    async def connect_phone(self, client_class, device, ready=None):
        relay = None

        def disconnected(_client):
            if relay is not None and not relay.loop.is_closed():
                relay.loop.call_soon_threadsafe(relay.disconnect)

        async with AsyncExitStack() as stack:
            for attempt in range(2):
                self.status("正在连接手机蓝牙")
                try:
                    client = await stack.enter_async_context(client_class(
                        device, disconnected_callback=disconnected, timeout=CONNECT_TIMEOUT,
                        winrt={"use_cached_services": False}))
                    break
                except asyncio.TimeoutError:
                    if attempt == 1:
                        raise
                    self.status("蓝牙连接未完成，正在重试")
                    device = await self._find_phone(CONNECT_TIMEOUT)
                    if device is None:
                        raise
            relay = Relay(client, self.token, self.ports)
            heartbeat = None
            try:
                self.status("正在验证手机")
                await asyncio.wait_for(client.start_notify(TX, relay.notification, force_indicate=True), IO_TIMEOUT)
                await asyncio.wait_for(relay.authenticate(), HANDSHAKE_TIMEOUT)
                self.status("蓝牙已连接")
                if ready is not None:
                    ready.set()
                heartbeat = asyncio.create_task(relay.heartbeat())
                while True:
                    await relay.dispatch(*(await relay.receive()))
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                await relay.close()
