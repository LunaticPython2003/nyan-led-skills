# Nyan / LOY protocol: verified subset

Primary implementations used for research: the repository's original uploader, [popledder protocol builders](https://github.com/eskibars/popledder/blob/main/led_matrix_api/commands/protocol_builders.py) and [frame parser](https://github.com/eskibars/popledder/blob/main/led_matrix_api/protocol/frame.py), [OverScore's BLE matrix research](https://overscore.media/posts/series/matthews-machinations/reverse-engineering-a-ble-led-matrix), and [screen-control notes](https://github.com/mtpiercey/ble-led-matrix-controller/blob/main/misc_notes.md). Protocol field interpretation is informed by those primary sources; compatibility claims below come from this unit's ACKs **and visual confirmation**.

## Device

- YS6247040189L, BLE E0:6E:41:FA:FE:69; advertised service `fee7`.
- Write characteristic `0000fff2-0000-1000-8000-00805f9b34fb`, notifications `0000fff1-0000-1000-8000-00805f9b34fb`.
- Confirmed full-panel input dimensions: 96×128, with four colored bounds matching physical edges. No native pixel-count capability was readable through GATT. Larger accepted GIF dimensions alone do not prove more addressable LEDs.

## Framing

```text
AA55 FFFF LENGTH16_LE SEQUENCE16_LE C1 02 FIELDS CHECKSUM16_LE
```

`LENGTH` counts sequence, flags/type, fields, and checksum; total packet length is `LENGTH + 6`. Checksum is the sum of all preceding packet bytes modulo 65536. The original `make_packet` helper encodes only lengths below 256 using a zero high-length byte carried in its data argument; it is preserved for byte compatibility. The original builder still sends 243-byte packets containing 196 GIF bytes. The verified fast path uses full 16-bit framing and 960-byte logical GIF chunks, fragmented into GATT writes of at most 243 bytes.

ACKs are 13 bytes:

```text
AA55 <destination:2> 0700 SEQUENCE16_LE 81 82 STATUS CHECKSUM16_LE
```

The driver checks the frame size, magic, length, ACK flags/type, checksum, matching sequence, and status zero. Other notifications cannot advance a transfer. It allows destination variants and validates the echoed sequence in little-endian order.

`FIELDS` are tag/length/value records. Lengths under 128 use one byte; 196 is encoded `81 c4`. Known tags in the verified path:

| Tag (hex) | Meaning / value |
| --- | --- |
| `04 02` | Screen state: `00 00` off; `00 01` on |
| `08 02` | Prepare/delete program: `00 <program>`; `00 ff` clears to default animation and is avoided |
| `09 01` | Data mode: `01` RAM path used here; `00` save-to-flash path remains unsupported |
| `0c 01` | Program id |
| `0d 01` | Rectangle id (0) |
| `0e 01` | Item id (0) |
| `1c 04` | Program/background settings: `00 02 03 00` |
| `1d 09` | Rectangle: x/y/width/height as four uint16 little-endian values, then `00` |
| `14 03` | Item animation settings |
| `11 04` | Control settings |
| `12 07` | Chunk count uint16, chunk index uint16, chunk size uint16, `00` |
| `13` | GIF bytes, with variable-length size prefix |
| `36 03` | Select program: `01 00 <program>` |

The chunk builder keeps the established fields:

```text
c1020901010c01000d01000e0100140301090a11040001000a1207
```

After the sequence/header, the program-id byte is changed for the inactive slot. Then follow total chunks (one low byte), index (`00 <idx> 00` with the preceding high chunk-count byte), `c4 00 00 13`, `81 c4`, and GIF bytes padded to 196. The fixed implementation allows at most 255 chunks / 49,980 GIF bytes. The application retains this conservative byte cap even with larger logical chunks.

## Legacy static path

```text
SCREEN_OFF -> READY(slot 0) -> acknowledged GIF chunks -> FINALIZE(slot 0) -> SCREEN_ON
```

```text
OFF: aa55ffff0a000500c10204020000d503
 ON: aa55ffff0a000500c10204020001d603
READY fields:    08 02 00 00
FINALIZE fields: 36 03 01 00 00
```

Screen masking was visually confirmed to hide “Amusing LED” during repeated uploads. No CLEAR command is needed between different images. This path remains selectable with `transport.staged_upload = false` for unverified devices.

## Inactive RAM program upload and selection

For program `p` (tested slots 1 and 2):

1. Prepare and define the full-panel rectangle in one packet (sequence 40):

```text
08 02 00 p
09 01 01
0c 01 p
1c 04 00 02 03 00
0d 01 00
1d 09 00 00 00 00 <width16_LE> <height16_LE> 00
```

2. Define its graphic item (sequence 41):

```text
09 01 01 0c 01 p 0d 01 00 0e 01 00
14 03 01 0d 00 11 04 00 01 00 0a
```

3. Send normal 196-byte GIF chunks with program `p`; await each matching ACK. No screen-state command or selection is sent during the inactive transfer.
4. Once **all** data has been acknowledged, send `36 03 01 00 p` (sequence 15). The new image appears directly.

At the first upload of each connection the screen is masked through this sequence, then turned on. Subsequent uploads alternate 1/2 without masking the active program. A digest cache allows selection of either resident GIF with one packet. On reconnect all host slot assumptions are discarded; volatile programs are not promised to survive a power cycle.

Physical observations on 2026-10-17: blue remained visible throughout staging green; green remained visible throughout staging a full 96×128 album cover (73 chunks); selecting the album switched directly with no text or black gap. The selection ACK took 180 ms on that connection. This measures command ACK time, not panel scanout latency.

Earlier unsuccessful selection attempts (`18 06 ...` and `36 03 01 p 00`) were ACKed but did not change the image. The working id is the **last** byte of `36 03 01 00 p`. Save-to-flash mode (`09 01 00`) accepted simple two-chunk colors, but rejected a large album's first chunk with status `0a`. Its meaning was not established. RAM mode (`09 01 01`) accepted the full album and gave the required behavior; no save-to-flash mode is enabled. The RAM long-packet path was subsequently verified, as described below.

## Larger logical packets and full-detail visualization

The fast path uses the same TLVs, but serializes the full 16-bit length and stream counters. Tag `12 07` contains count/index/size as uint16 little-endian values; size is 960. Tag `13` uses `82 <length16_LE>` for payloads of at least 256 bytes. The last chunk is its actual length, without 196-byte padding. A full 960-byte logical chunk packet is 1,008 bytes, sent in fragments of at most 243 bytes. The driver waits for one matching ACK after complete reassembly before starting another logical packet. No individual fragment ACK is assumed.

The full-panel header and graphic-item TLVs can be concatenated into one setup packet, sequence 40. Newly rendered GIFs are saved without interlacing; this changes byte ordering/compression without changing decoded pixels. Native-size GIFs under `natural` quality still pass through untouched.

Hardware measurements: a 11,873-byte full-detail composite took about 5.6–6.0 seconds with 960-byte chunks; a losslessly non-interlaced encoding of the same pixels used about 11,145 bytes. Combined setup plus that encoding took 4.85–5.17 seconds. The actual media/audio plugin on another cover transferred about 7,200 bytes in 3.5–3.8 seconds, with sample age around 3.9 seconds. The user confirmed restored cover quality and bars remaining visible between updates. These are content/radio-dependent measurements, not a universal five-second deadline or high-frame-rate streaming claim.

924-byte chunks were also accepted but did not materially outperform 960. A 1,896-byte logical chunk timed out; larger packets are not enabled. The 960-byte path is scoped to this calibrated model. The driver default remains 196 bytes. If a fast upload fails, it switches back to 196-byte chunks, at least 300 ms pacing, a fresh connection, and a whole-frame retry. It never selects an incomplete program.

Native rectangle experiments rendered a cover and a small spectrum separately, but active writes shifted the cover or flashed the bars. Inactive updates sometimes remained frozen; the documented `18 06` play command produced changes with flashing. Removing repeated animation controls and trying single-packet compact GIFs did not establish reliable visible updates. These experiments are not enabled. The supported visualizer renders full-detail artwork and actual desktop FFT bars into complete GIFs, keeping the previous complete program visible during upload.

## Timing and recovery

The connection and notification subscription remain open between content changes. Fast uploads add no artificial pacing beyond the matching logical-packet ACK. The conservative path retains the original packet format and delay after failure. There is no speculative ACK window and no blind fragment/chunk resend. Connect, ACK, upload, and cleanup have bounded timeouts.

On failure, stop immediately. A known active image remains visible during an inactive transfer failure. Uncertain selection/startup failures attempt screen-off. Disconnect and retry the complete image after the configured delay. Idle disconnection causes a fresh upload when the source returns content. The watcher keeps only the latest pending content; live scene samples have a configurable age limit before upload, while observed sample age is exposed in status.

## Formats and open questions

PNG/JPEG/WebP are accepted as host sources and converted to GIF. GIF is the verified wire encoding. The renderer preserves GIF animation, durations, and loop metadata; native-size GIFs in `natural` quality can pass through unchanged. An actual eight-frame moving-bar GIF was visually confirmed to animate and loop continuously on this backpack. Larger animations can exceed the current byte/chunk limit.

Total program capacity, flash-save semantics, a capability query for physical pixel count, direct raw/PNG/JPEG wire formats, and arbitrary live framebuffer streaming remain unverified. Keeping BLE open enables quick commands and background transfer, but cannot make a new full image arrive instantaneously. Playback of a preloaded GIF is local to the backpack.
