"""Exercise real packet/ACK logic against a simulated BLE peripheral."""

import asyncio
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from bleak.exc import BleakError

from nyan_pack.devices import loy as backpack


def ack(echo, status=0):
    data = bytes.fromhex("aa55ffff07") + echo.to_bytes(2, "big") + bytes.fromhex("008182") + bytes([status])
    return data + (sum(data) & 0xFFFF).to_bytes(2, "little")


def gif(color="blue"):
    buffer = BytesIO()
    Image.new("RGB", (96, 128), color).save(buffer, format="GIF")
    return buffer.getvalue()


class FakeClient:
    def __init__(self, device, disconnected_callback):
        self.is_connected = False
        self.disconnected_callback = disconnected_callback
        self.packets = []
        self.services = SimpleNamespace(get_characteristic=lambda uuid: object())
        self.start_notify = AsyncMock(side_effect=self.subscribe)
        self.disconnect = AsyncMock(side_effect=self.drop)
        self.on_write = None

    async def connect(self):
        self.is_connected = True

    async def subscribe(self, uuid, callback):
        self.callback = callback

    def drop(self):
        self.is_connected = False
        self.disconnected_callback(self)

    async def write_gatt_char(self, uuid, packet, response):
        self.packets.append(packet)
        if self.on_write:
            self.on_write(self, packet)
        else:
            self.callback(None, ack(int.from_bytes(packet[5:7], "big")))


class AckTests(unittest.TestCase):
    def test_observed_hardware_ack(self):
        self.assertEqual(backpack.parse_ack(bytes.fromhex("aa55ffff070005008182000c04")), (5, 0))

    def test_corrupt_and_unrelated_notifications_are_rejected(self):
        valid = ack(9)
        for data in [b"", valid[:-1], valid + b"x", valid[:-1] + b"\xff", b"\x00\x00" + valid[2:]]:
            with self.subTest(data=data):
                self.assertIsNone(backpack.parse_ack(data))


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []

        def make_client(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            self.clients.append(client)
            return client

        self.device = SimpleNamespace(name="YS-test", address="AA:BB:CC:DD:EE:FF")
        self.scanner = self.enterContext(patch.object(backpack.BleakScanner, "find_device_by_filter", new_callable=AsyncMock, return_value=self.device))
        self.enterContext(patch.object(backpack, "BleakClient", side_effect=make_client))
        self.connection = backpack.BackpackConnection(chunk_delay=0, ack_timeout=0.01)
        self.addAsyncCleanup(self.connection.close)

    async def test_two_uploads_share_one_connection_and_mask_each_transfer(self):
        data = gif()
        for _ in range(2):
            result = await self.connection.upload(data)
            self.assertEqual(result.byte_count, len(data))
        self.assertEqual(len(self.clients), 1)
        self.scanner.assert_awaited_once()
        self.clients[0].start_notify.assert_awaited_once()
        packets = self.clients[0].packets
        for start in [0, len(packets) // 2]:
            end = start + len(packets) // 2
            self.assertEqual(packets[start:start + 2], [backpack.SCREEN_OFF, backpack.READY])
            self.assertEqual(packets[end - 2:end], [backpack.FINALIZE, backpack.SCREEN_ON])

    async def test_idle_disconnect_reconnects_to_the_same_device(self):
        await self.connection.upload(gif())
        self.clients[0].drop()
        self.assertFalse(self.connection.is_connected)
        await self.connection.upload(gif())
        self.assertTrue(self.connection.is_connected)
        self.assertEqual(len(self.clients), 2)
        self.scanner.assert_awaited_once()
        self.assertEqual(self.connection.address, self.device.address)

    async def test_wrong_echo_and_bad_checksum_do_not_complete_packet(self):
        await self.connection.connect()
        self.clients[0].on_write = lambda client, packet: (
            client.callback(None, ack(123)),
            client.callback(None, ack(9)[:-1] + b"\xff"),
        )
        with self.assertRaisesRegex(backpack.UploadError, "no matching ACK"):
            await self.connection._send(backpack.READY, "READY")
        self.assertIsNone(self.connection._pending)

    async def test_stray_ack_is_ignored_before_correct_ack(self):
        await self.connection.connect()

        def receive(client, packet):
            client.callback(None, ack(123))
            client.callback(None, b"malformed")
            client.callback(None, ack(int.from_bytes(packet[5:7], "big")))

        self.clients[0].on_write = receive
        await self.connection._send(backpack.READY, "READY")

    async def test_old_connection_notification_cannot_ack_new_connection(self):
        await self.connection.connect()
        old_client = self.clients[0]
        old_client.drop()
        await self.connection.connect()

        def receive(client, packet):
            old_client.callback(None, ack(9, 3))
            client.callback(None, ack(9))

        self.clients[1].on_write = receive
        await self.connection._send(backpack.READY, "READY")

    async def test_failed_chunk_never_finalizes_or_unmasks_and_slows_retry(self):
        await self.connection.connect()

        def receive(client, packet):
            echo = int.from_bytes(packet[5:7], "big")
            client.callback(None, ack(echo, 3 if packet not in [backpack.SCREEN_OFF, backpack.READY] else 0))

        client = self.clients[0]
        client.on_write = receive
        with self.assertRaisesRegex(backpack.UploadError, "rejected packet"):
            await self.connection.upload(gif())
        self.assertNotIn(backpack.FINALIZE, client.packets)
        self.assertNotIn(backpack.SCREEN_ON, client.packets)
        self.assertEqual(client.packets[-1], backpack.SCREEN_OFF)
        self.assertEqual(self.connection.chunk_delay, 0.3)
        self.assertFalse(self.connection.is_connected)

    async def test_failed_unmask_is_followed_by_screen_off(self):
        await self.connection.connect()

        def receive(client, packet):
            echo = int.from_bytes(packet[5:7], "big")
            client.callback(None, ack(echo, 3 if packet == backpack.SCREEN_ON else 0))

        client = self.clients[0]
        client.on_write = receive
        with self.assertRaisesRegex(backpack.UploadError, "SCREEN_ON"):
            await self.connection.upload(gif())
        self.assertEqual(client.packets[-2:], [backpack.SCREEN_ON, backpack.SCREEN_OFF])
        self.assertFalse(self.connection.is_connected)

    async def test_disconnect_during_packet_fails_immediately(self):
        await self.connection.connect()
        self.clients[0].on_write = lambda client, packet: client.drop()
        with self.assertRaisesRegex(ConnectionError, "disconnected during upload"):
            await self.connection.upload(gif())
        self.assertFalse(self.connection.is_connected)

    async def test_failed_connect_discards_cached_discovery(self):
        with patch.object(FakeClient, "connect", new_callable=AsyncMock, side_effect=BleakError("Connection failed")):
            with self.assertRaises(BleakError):
                await self.connection.connect()
        self.assertIsNone(self.connection._device)
        self.assertFalse(self.connection.is_connected)
        with patch.object(backpack.BleakScanner, "find_device_by_address", new_callable=AsyncMock, return_value=self.device) as rescan:
            await self.connection.connect()
        rescan.assert_awaited_once_with(self.device.address, timeout=10)

    async def test_cancelled_upload_disconnects_and_does_not_unmask(self):
        await self.connection.connect()
        written = asyncio.Event()

        def receive(client, packet):
            written.set()

        self.clients[0].on_write = receive
        task = asyncio.create_task(self.connection.upload(gif()))
        await written.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.connection.is_connected)
        self.assertNotIn(backpack.SCREEN_ON, self.clients[0].packets)

    async def test_unsupported_or_oversized_payload_does_not_touch_hardware(self):
        for data in [b"not a gif", b"GIF89a" + b"x" * backpack.MAX_GIF_BYTES]:
            with self.assertRaises(ValueError):
                await self.connection.upload(data)
        self.assertEqual(self.clients, [])

    async def test_staged_upload_retains_active_program_and_cached_switch_sends_one_packet(self):
        self.connection.staged_upload = True
        await self.connection.upload(gif("blue"))
        client = self.clients[0]
        first = len(client.packets)
        self.assertEqual(client.packets[0], backpack.SCREEN_OFF)
        self.assertEqual(client.packets[-2:], [backpack.program_select(1), backpack.SCREEN_ON])
        self.assertEqual(client.packets[1], backpack.program_header(1, 96, 128))
        await self.connection.upload(gif("green"))
        second = len(client.packets)
        staged = client.packets[first:second]
        self.assertEqual(staged[0], backpack.program_header(2, 96, 128))
        self.assertEqual(staged[-1], backpack.program_select(2))
        self.assertNotIn(backpack.SCREEN_OFF, staged)
        self.assertNotIn(backpack.SCREEN_ON, staged)
        self.assertNotIn(backpack.READY, staged)
        result = await self.connection.upload(gif("blue"))
        self.assertEqual(client.packets[second:], [backpack.program_select(1)])
        self.assertEqual(result.chunks, 0)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual([p["slot"] for p in self.connection.programs if p["active"]], [1])

    async def test_failed_inactive_program_is_never_selected_and_old_display_is_not_blanked(self):
        self.connection.staged_upload = True
        await self.connection.upload(gif())
        client = self.clients[0]
        start = len(client.packets)
        client.on_write = lambda client, packet: client.callback(None, ack(int.from_bytes(packet[6:8], "little"), 3))
        with self.assertRaises(backpack.UploadError):
            await self.connection.upload(gif("green"))
        self.assertNotIn(backpack.program_select(2), client.packets[start:])
        self.assertNotIn(backpack.SCREEN_OFF, client.packets[start:])
        self.assertFalse(self.connection.is_connected)

    async def test_reconnect_discards_cached_programs_and_masks_startup(self):
        self.connection.staged_upload = True
        await self.connection.upload(gif())
        self.clients[0].drop()
        result = await self.connection.upload(gif())
        self.assertGreater(result.chunks, 0)
        self.assertEqual(self.clients[1].packets[0], backpack.SCREEN_OFF)

    async def test_large_logical_packets_are_fragmented_and_acknowledged_after_reassembly(self):
        self.connection.staged_upload = True
        self.connection.stream_chunk = 960
        await self.connection.connect()
        client = self.clients[0]
        buffer = bytearray()
        logical = []

        def receive(client, part):
            buffer.extend(part)
            if len(buffer) < 6:
                return
            expected = int.from_bytes(buffer[4:6], "little") + 6
            if len(buffer) == expected:
                packet = bytes(buffer)
                buffer.clear()
                self.assertEqual(sum(packet[:-2]) & 65535, int.from_bytes(packet[-2:], "little"))
                logical.append(packet)
                client.callback(None, ack(int.from_bytes(packet[6:8], "little")))

        client.on_write = receive
        image = Image.new("RGB", (96,128))
        image.putdata([(x*3 % 256, y*2 % 256, (x+y)*2 % 256) for y in range(128) for x in range(96)])
        output = BytesIO()
        image.save(output, format="GIF")
        data = output.getvalue()
        result = await self.connection.upload(data)
        self.assertEqual(result.chunks, (len(data)+959)//960)
        self.assertGreater(len(client.packets), len(logical))
        self.assertTrue(all(len(part) <= 243 for part in client.packets))
        self.assertEqual(logical[-2:], [backpack.program_select(1), backpack.SCREEN_ON])
        self.assertEqual(buffer, b"")

    async def test_large_stream_failure_falls_back_to_original_format(self):
        self.connection.staged_upload = True
        self.connection.stream_chunk = 960
        await self.connection.connect()
        self.clients[0].on_write = lambda client, packet: client.callback(None, ack(int.from_bytes(packet[6:8], "little"), 3))
        with self.assertRaises(backpack.UploadError):
            await self.connection.upload(gif())
        self.assertEqual(self.connection.stream_chunk, 196)
        self.assertEqual(self.connection.chunk_delay, 0.3)

    async def test_deliberate_cancel_does_not_disable_fast_transport(self):
        self.connection.staged_upload = True
        self.connection.stream_chunk = 960
        await self.connection.connect()
        written = asyncio.Event()
        self.clients[0].on_write = lambda client, packet: written.set()
        task = asyncio.create_task(self.connection.upload(gif()))
        await written.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.connection.stream_chunk, 960)
        self.assertEqual(self.connection.chunk_delay, 0)


if __name__ == "__main__":
    unittest.main()
