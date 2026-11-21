"""BLE protocol and loopback integration without a Bluetooth radio."""

import asyncio
from contextlib import closing
import hashlib
import http.client
import importlib.util
import ssl
import time
import unittest
from unittest.mock import patch

import bluetooth_transport as ble


TOKEN = "A" * 43


class FakeGatt:
    mtu_size = 23

    def __init__(self):
        self.sent = asyncio.Queue()

    async def write_gatt_char(self, characteristic, frame, response):
        assert characteristic == ble.RX and response and len(frame) <= 20
        opcode, stream = ble.HEADER.unpack_from(frame)
        await self.sent.put((opcode, stream, frame[3:]))


class BluetoothRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeGatt()
        self.relay = ble.Relay(self.client, TOKEN, {})
        self.addAsyncCleanup(self.relay.close)

    def send_phone(self, opcode, stream_id=0, payload=b""):
        self.relay.enqueue(ble.HEADER.pack(opcode, stream_id) + payload)

    async def next_host(self):
        return await asyncio.wait_for(self.client.sent.get(), 2)

    async def test_handshake_rejects_invalid_phone_proof(self):
        task = asyncio.create_task(self.relay.authenticate())
        self.assertEqual((await self.next_host())[:2], (ble.HELLO, 0))
        self.send_phone(ble.PHONE_NONCE, payload=b"p" * 16)
        self.send_phone(ble.PHONE_PROOF, payload=b"invalid!!!!!!!!")
        with self.assertRaises(ble.LinkError):
            await task
        self.assertTrue(self.client.sent.empty())
        self.assertEqual(self.relay.streams, {})

    async def test_verified_phone_can_multiplex_fixed_local_streams(self):
        task = asyncio.create_task(self.relay.authenticate())
        opcode, stream_id, nonce = await self.next_host()
        self.assertEqual((opcode, stream_id), (ble.HELLO, 0))
        phone_nonce = b"p" * 16
        self.send_phone(ble.PHONE_NONCE, payload=phone_nonce)
        self.send_phone(ble.PHONE_PROOF, payload=ble.proof(TOKEN, b"phone", nonce, phone_nonce))
        self.assertEqual(await self.next_host(), (ble.HOST_PROOF, 0, ble.proof(TOKEN, b"host", nonce, phone_nonce)))
        self.send_phone(ble.READY)
        await task

        async def exchange(reader, writer):
            try:
                data = await reader.read()
                writer.write(data[::-1])
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(exchange, ble.bridge.HOST, 0)
        async with server:
            self.relay.ports = {1: server.sockets[0].getsockname()[1]}
            await self.relay.dispatch(ble.OPEN, 99, b"\x02")
            self.assertEqual(await self.next_host(), (ble.RESET, 99, b""))
            parts = {10: b"abcdefgh123456789!", 11: b"12345"}
            for stream, data in parts.items():
                await self.relay.dispatch(ble.OPEN, stream, b"\x01")
                for index in range(0, len(data), 17):
                    await self.relay.dispatch(ble.DATA, stream, data[index:index + 17])
            for stream in parts:
                await self.relay.dispatch(ble.FIN, stream, b"")
            finished = set()
            buffers = {stream: bytearray() for stream in parts}
            while len(finished) < len(parts):
                opcode, stream_id, payload = await self.next_host()
                if opcode == ble.DATA:
                    buffers[stream_id].extend(payload)
                else:
                    self.assertEqual(opcode, ble.FIN)
                    finished.add(stream_id)
            self.assertEqual(buffers, {stream: data[::-1] for stream, data in parts.items()})

    async def test_pairing_attempt_timeout_cancels_stalled_connection(self):
        session = ble.DesktopSession(3430, "placeholder-desktop-key")
        cancelled = asyncio.Event()

        async def stalled(_client, _device, _ready=None):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session.connect_phone = stalled
        with patch.object(ble, "PAIRING_TIMEOUT", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await session._connect_with_timeout(None, None)
        self.assertTrue(cancelled.is_set())

    async def test_heartbeat_disconnects_when_phone_stops_responding(self):
        with patch.object(ble, "HEARTBEAT_INTERVAL", 0), patch.object(ble, "HEARTBEAT_TIMEOUT", 0.1):
            await self.relay.heartbeat()
        self.assertEqual(await self.next_host(), (ble.PING, 0, b""))
        self.assertTrue(self.relay.stopped)


class PairingLifecycleTests(unittest.TestCase):
    def test_rotating_pairing_expires_old_qr_without_breaking_bridge_token(self):
        session = ble.DesktopSession(3430, "placeholder-desktop-key")
        seen = []
        session.pairing = seen.append
        session._update_pairing_code()
        first_code, first_service, first_token = session.pairing_code, session.service_id, session.token
        session.rotate_pairing()
        self.assertNotEqual(session.service_id, first_service)
        self.assertEqual(session.token, first_token)
        self.assertNotEqual(session.pairing_code, first_code)
        self.assertEqual(seen[-1], session.pairing_code)
        self.assertGreater(session._pairing_deadline, time.monotonic())


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "install Bluetooth requirements for portable TLS")
class PortableBluetoothTlsTest(unittest.TestCase):
    def test_session_qr_pins_tls_and_preserves_phone_browser_origin(self):
        with ble.DesktopSession(3430, "placeholder-desktop-key") as session:
            prefix, version, uuid, fingerprint, token = session.pairing_code.split(":")
            self.assertEqual((prefix, version, uuid), ("magpie-ble", "1", session.service_id))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with closing(http.client.HTTPSConnection(
                    "127.0.0.1", session.ports[2], context=context, timeout=3)) as conn:
                conn.connect()
                self.assertEqual(hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest(), fingerprint)
                conn.request("GET", ble.bridge.BROWSER_SESSION, headers={
                    "Host": f"127.0.0.1:{ble.BROWSER_PORT}", "Authorization": "Bearer " + token})
                with conn.getresponse() as response:
                    self.assertEqual(response.status, 204)
                    self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.assertEqual(session.pairing_code, "")
