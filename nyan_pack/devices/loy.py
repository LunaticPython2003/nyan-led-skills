"""LOY BLE driver. Existing packet framing is preserved byte for byte."""

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import math
import time

from bleak import BleakClient, BleakScanner

DEVICE_NAME_PREFIX = "YS"

WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

CONST_SEQ = bytes.fromhex("c1020901010c01000d01000e0100140301090a11040001000a1207")

CONST2 = bytes.fromhex("c4000013")

PAYLOAD_LEN_MARKER = bytes.fromhex("81c4")

def make_packet(data: bytes) -> bytes:
    """Build aa55ffff packet with checksum."""
    pkt_len = len(data) + 1
    raw = bytes([0xAA, 0x55, 0xFF, 0xFF, pkt_len]) + data
    total = sum(raw)
    return raw + bytes([total & 0xFF, (total >> 8) & 0xFF])

def make_cmd(cmd_id: int, sub_cmd: int, args: bytes = b"") -> bytes:
    data = cmd_id.to_bytes(2, "big") + b"\x00\xc1\x02" + sub_cmd.to_bytes(2, "big") + args
    return make_packet(data)


def make_stream_packet(sequence: int, fields: bytes) -> bytes:
    """Full 16-bit framing for larger logical packets; legacy framing is intact."""
    raw = b"\xaa\x55\xff\xff" + (len(fields) + 6).to_bytes(2, "little")
    raw += sequence.to_bytes(2, "little") + b"\xc1\x02" + fields
    return raw + (sum(raw) & 0xFFFF).to_bytes(2, "little")


def build_stream_chunk(index, data, count, program, size):
    fields = bytes([9, 1, 1, 12, 1, program, 13, 1, 0, 14, 1, 0])
    fields += bytes.fromhex("140301090a 11040001000a 1207")
    fields += count.to_bytes(2, "little") + index.to_bytes(2, "little") + size.to_bytes(2, "little") + b"\0\x13"
    length = len(data)
    fields += (bytes([length]) if length < 128 else b"\x81" + bytes([length]) if length < 256 else b"\x82" + length.to_bytes(2, "little"))
    return make_stream_packet(index, fields + data)

def build_image_chunk(chunk_idx: int, gif_chunk: bytes, total_chunks: int, *, program: int = 0, stored: bool = False, rectangle: int = 0) -> bytes:
    """Build image data chunk in the protocol format."""
    # Index format: 00 <idx> 00
    idx_bytes = bytes([0x00, chunk_idx, 0x00])
    # Pad payload to 196 bytes
    padded = gif_chunk + b"\x00" * max(0, 196 - len(gif_chunk))
    fields = bytearray(CONST_SEQ)
    fields[4] = 0 if stored else 1
    fields[7] = program
    fields[10] = rectangle
    payload = (
        idx_bytes
        + bytes(fields)
        + bytes([total_chunks])  # frame_count = total number of chunks
        + idx_bytes
        + CONST2
        + PAYLOAD_LEN_MARKER
        + padded[:196]
    )
    return make_packet(payload)


def program_header(program: int, width: int, height: int) -> bytes:
    """Define a RAM program and one full-panel rectangle (no flash writes)."""
    geometry = b"\0\0\0\0" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\0"
    fields = bytes([8, 2, 0, program, 9, 1, 1, 12, 1, program])
    fields += bytes.fromhex("1c0400020300 0d0100 1d09") + geometry
    return make_packet(b"\0\x28\0\xc1\x02" + fields)


def program_item(program: int, rectangle: int = 0) -> bytes:
    fields = bytes([9, 1, 1, 12, 1, program])
    fields += bytes([13, 1, rectangle]) + bytes.fromhex("0e0100 1403010d00 11040001000a")
    return make_packet(b"\0\x29\0\xc1\x02" + fields)


def region_header(program, region):
    geometry = b"".join(value.to_bytes(2, "little") for value in (region.x, region.y, region.width, region.height)) + b"\0"
    fields = bytes([9, 1, 1, 12, 1, program, 13, 1, region.id, 29, 9]) + geometry
    return make_packet(b"\0\x32\0\xc1\x02" + fields)


def program_select(program: int) -> bytes:
    # 01 00 <id> was visually verified; 01 <id> 00 was ACKed but ineffective.
    return make_cmd(15, 0x3603, bytes([1, 0, program]))

READY = make_cmd(0x0009, 0x0802, b"\x00\x00")

FINALIZE = make_cmd(0x000f, 0x3603, b"\x01\x00\x00")

logger = logging.getLogger(__name__)
CHUNK_SIZE = 196
MAX_GIF_BYTES = 255 * CHUNK_SIZE
SCREEN_OFF = make_cmd(0x0005, 0x0402, b"\x00\x00")
SCREEN_ON = make_cmd(0x0005, 0x0402, b"\x00\x01")


class UploadError(RuntimeError):
    """The device did not acknowledge an upload step successfully."""


def parse_ack(data: bytes) -> tuple[int, int] | None:
    """Accept only complete, checksum-valid ACKs, returning (echo, status)."""
    if len(data) != 13 or data[:2] != b"\xaa\x55" or int.from_bytes(data[4:6], "little") != 7:
        return None
    if data[8:10] != b"\x81\x82":
        return None
    if int.from_bytes(data[-2:], "little") != sum(data[:-2]) & 0xFFFF:
        return None
    return int.from_bytes(data[6:8], "little"), data[10]


@dataclass(frozen=True)
class UploadResult:
    byte_count: int
    chunks: int
    seconds: float
    stale: bool = False


class BackpackConnection:
    """Own one connection and one in-flight packet; reconnect on the next upload.

    Legacy uploads mask the screen from READY through FINALIZE. Models enabling
    staged_upload alternate RAM programs 1/2, keeping the current program playing
    while writing the next. Startup masks the screen until a known program exists.
    Individual packets are never blindly retransmitted.
    """

    def __init__(self, address: str | None = None, *, chunk_delay: float = 0.03, ack_timeout: float = 5, staged_upload: bool = False, stream_chunk: int = 196):
        if not math.isfinite(chunk_delay) or chunk_delay < 0:
            raise ValueError("chunk_delay must be finite and nonnegative")
        if not math.isfinite(ack_timeout) or ack_timeout <= 0:
            raise ValueError("ack_timeout must be finite and positive")
        if type(stream_chunk) is not int or stream_chunk not in (196, 924, 960):
            raise ValueError("stream_chunk must be 196, 924, or 960; larger packets are unverified")
        self.address = address
        self.chunk_delay = chunk_delay
        self.ack_timeout = ack_timeout
        self.staged_upload = staged_upload
        self.stream_chunk = stream_chunk
        self._active_program = None
        self._programs = {}
        self._client = None
        self._device = None
        self._pending = None
        self._expected_echo = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _disconnected(self, client):
        if client is self._client and self._pending is not None and not self._pending.done():
            self._pending.set_exception(ConnectionError("Backpack disconnected during upload"))

    def _notification(self, client, data):
        if client is not self._client:
            return
        ack = parse_ack(bytes(data))
        if ack is None:
            logger.debug("Ignored malformed notification: %s", data.hex())
            return
        echo, status = ack
        if self._pending is not None and not self._pending.done() and echo == self._expected_echo:
            self._pending.set_result(status)
        else:
            logger.debug("Ignored unsolicited ACK for 0x%04x", echo)

    async def connect(self):
        if self.is_connected:
            return
        await self.close()
        # A new BLE session may follow a power cycle. Never trust stale slots.
        self._active_program = None
        self._programs.clear()
        if self._device is None:
            if self.address:
                self._device = await BleakScanner.find_device_by_address(self.address, timeout=10)
            else:
                self._device = await BleakScanner.find_device_by_filter(
                    lambda device, adv: (adv.local_name or device.name or "").startswith(DEVICE_NAME_PREFIX),
                    timeout=10,
                )
            if self._device is None:
                raise ConnectionError("Backpack not found. Check power and Bluetooth.")
            # Keep reconnects pinned to this backpack, even if another YS device appears.
            self.address = self._device.address
        client = BleakClient(self._device, disconnected_callback=self._disconnected)
        self._client = client
        try:
            async with asyncio.timeout(30):
                await client.connect()
                if client.services.get_characteristic(WRITE_UUID) is None:
                    raise UploadError(f"Backpack is missing write characteristic {WRITE_UUID}")
                await client.start_notify(NOTIFY_UUID, lambda sender, data: self._notification(client, data))
            logger.info("Connected to %s (%s)", self._device.name, self.address)
        except BaseException:
            self._device = None
            await self.close()
            raise

    async def close(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception as exc:
                logger.warning("BLE disconnect cleanup: %s", exc)

    async def _send(self, packet: bytes, label: str):
        client = self._client
        if not self.is_connected:
            raise ConnectionError("Backpack is disconnected")
        future = asyncio.get_running_loop().create_future()
        self._pending = future
        self._expected_echo = int.from_bytes(packet[6:8], "little")
        try:
            async with asyncio.timeout(self.ack_timeout):
                # BLE fragments carry one logical frame, acknowledged only after
                # reassembly. Do not confuse GATT write size with GIF chunk size.
                for offset in range(0, len(packet), 243):
                    await client.write_gatt_char(WRITE_UUID, packet[offset:offset + 243], response=False)
                status = await future
            if status != 0:
                raise UploadError(f"{label}: device rejected packet with status 0x{status:02x}")
        except TimeoutError as exc:
            raise UploadError(f"{label}: no matching ACK within {self.ack_timeout:g}s") from exc
        finally:
            self._pending = None
            self._expected_echo = None
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def upload(self, gif_data: bytes) -> UploadResult:
        if not gif_data.startswith((b"GIF87a", b"GIF89a")) or len(gif_data) < 13:
            raise ValueError("Only GIF payloads are supported by the verified upload path")
        if len(gif_data) > MAX_GIF_BYTES:
            raise ValueError(f"GIF exceeds the protocol's {MAX_GIF_BYTES}-byte / 255-chunk limit")
        if self.staged_upload:
            return await self._upload_stored(gif_data)
        chunks = (len(gif_data) + CHUNK_SIZE - 1) // CHUNK_SIZE
        async with self._lock:
            await self.connect()
            started = time.monotonic()
            try:
                async with asyncio.timeout(300):
                    await self._send(SCREEN_OFF, "SCREEN_OFF")
                    await self._send(READY, "READY")
                    for index in range(chunks):
                        chunk = gif_data[index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE]
                        await self._send(build_image_chunk(index, chunk, chunks), f"Chunk {index + 1}/{chunks}")
                        if self.chunk_delay:
                            await asyncio.sleep(self.chunk_delay)
                    await self._send(FINALIZE, "FINALIZE")
                    await self._send(SCREEN_ON, "SCREEN_ON")
            except BaseException:
                # Fail closed and preserve the original pacing for the next attempt.
                self.chunk_delay = max(self.chunk_delay, 0.3)
                if self.is_connected:
                    try:
                        await self._send(SCREEN_OFF, "SCREEN_OFF after failure")
                    except Exception as exc:
                        logger.warning("Could not confirm screen-off after failure: %s", exc)
                await self.close()
                raise
            result = UploadResult(len(gif_data), chunks, time.monotonic() - started)
            logger.info("Uploaded %s bytes in %s chunks (%.2fs)", result.byte_count, result.chunks, result.seconds)
            return result

    @property
    def programs(self):
        return [{"slot": slot, "digest": digest, "active": slot == self._active_program}
                for slot, digest in sorted(self._programs.items())]

    async def _upload_stored(self, gif_data):
        digest = hashlib.sha256(gif_data).hexdigest()
        async with self._lock:
            await self.connect()
            size = self.stream_chunk
            chunks = (len(gif_data) + size - 1) // size
            started = time.monotonic()
            first = self._active_program is None
            selecting = False
            try:
                async with asyncio.timeout(300):
                    cached = next((slot for slot, value in self._programs.items() if value == digest), None)
                    if cached is not None:
                        selecting = True
                        await self._send(program_select(cached), f"Select cached program {cached}")
                        self._active_program = cached
                        return UploadResult(len(gif_data), 0, time.monotonic() - started)
                    # Slot zero has different firmware behavior; use verified slots 1/2.
                    target = 2 if self._active_program == 1 else 1
                    self._programs.pop(target, None)
                    if first:
                        await self._send(SCREEN_OFF, "SCREEN_OFF for first program")
                    width = int.from_bytes(gif_data[6:8], "little")
                    height = int.from_bytes(gif_data[8:10], "little")
                    if size == CHUNK_SIZE:
                        await self._send(program_header(target, width, height), f"Prepare program {target}")
                        await self._send(program_item(target), f"Define program {target} image")
                    else:
                        fields = program_header(target, width, height)[10:-2] + program_item(target)[10:-2]
                        await self._send(make_stream_packet(40, fields), f"Prepare program {target} and image")
                    for index in range(chunks):
                        data = gif_data[index * size:(index + 1) * size]
                        packet = build_image_chunk(index, data, chunks, program=target) if size == CHUNK_SIZE else build_stream_chunk(index, data, chunks, target, size)
                        await self._send(packet, f"Program {target} chunk {index + 1}/{chunks}")
                        if self.chunk_delay:
                            await asyncio.sleep(self.chunk_delay)
                    selecting = True
                    await self._send(program_select(target), f"Select program {target}")
                    if first:
                        await self._send(SCREEN_ON, "SCREEN_ON")
                    self._active_program = target
                    self._programs[target] = digest
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    self.chunk_delay = max(self.chunk_delay, 0.3)
                    self.stream_chunk = CHUNK_SIZE
                # An incomplete inactive slot is never selected. Keep the old image
                # visible unless selection became uncertain or startup was masked.
                if (first or selecting) and self.is_connected:
                    try:
                        await self._send(SCREEN_OFF, "SCREEN_OFF after failure")
                    except Exception as exc:
                        logger.warning("Could not confirm screen-off: %s", exc)
                await self.close()
                raise
            return UploadResult(len(gif_data), chunks, time.monotonic() - started)

