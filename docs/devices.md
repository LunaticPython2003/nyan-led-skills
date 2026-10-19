# Device plugins and calibration

Content plugins produce images. Device plugins define dimensions, rendering defaults, transport settings, capabilities, calibration, and an importable BLE driver. The built-in profile is `nyan_pack/devices/loy-ys6247040189l.toml`. It covers the tested YS6247040189L at E0:6E:41:FA:FE:69; the address belongs in user configuration, so another unit is not silently selected.

The profile's **96×128 input grid fills the panel**, verified with all four colored bounds at the physical edges. This establishes the correct full-panel artwork dimensions for this unit. BLE discovery exposed no readable native-resolution value; electrical pixel count and higher-resolution scaling have not been independently measured. A 64×64 image leaves unused LEDs, and 128×128 clips an edge. Successful ACKs at a larger size do not prove extra addressable pixels.

## Recalibrate

```bash
nyan device list
nyan device show
nyan device configure --model loy-ys6247040189l --address E0:6E:41:FA:FE:69
nyan calibrate --size 96x128
# Wait for the complete pattern, then inspect the entire panel.
nyan calibrate --confirm
```

Top = green, left = blue, right = yellow, bottom = red. All edges must coincide with the physical edges without unused LEDs outside or clipping. The background is lit so unused physical area can be distinguished from intentionally black artwork. If needed, send another rectangular size before confirming. `--confirm` is accepted only after the pattern has finished uploading. The active plugin resumes using the confirmed width/height. Changing the model or BLE address invalidates calibration. For the existing prototype installation, the previously saved calibration and quality preference migrate automatically.

## Add a model

Copy the built-in TOML into `~/.config/nyan-pack/devices/`, change `id` and `name`, and choose it with `nyan device configure --model <id>`. User models override built-ins with the same id. Display fields are `width`, `height`, `quality` (`natural` or `detail`), and `fit` (`cover`, `contain`, `stretch`). `cover` crops to fill; `contain` adds black padding; `stretch` changes aspect ratio. The `detail` preset applies mild tone-preserving autocontrast and sharpening before GIF quantization.

A custom model's `driver = "python.module:Class"` must be importable in the service's virtual environment. The factory receives `address` and the manifest's supported transport keyword arguments (`chunk_delay`, `ack_timeout`, optional `staged_upload` and `stream_chunk`). It supplies async `upload(gif_bytes) -> UploadResult`, async `close()`, `is_connected`, and `address`. `UploadResult` has `byte_count`, `chunks`, and `seconds`. BLE-specific framing and ACK logic belong here. A driver's optional `programs` property is exposed in status.

Keep `staged_upload = false` for unverified hardware: the existing acknowledged static sequence remains available, wrapping READY/chunks/FINALIZE with screen masking. On this model it is enabled after physical tests: full album art uploaded into an inactive RAM program while the previous program stayed visible; the selection command then switched directly, without firmware text or a black gap. Slots 1/2 are reserved by this watcher while connected. No contents are assumed valid after reconnecting, and a new session bootstraps under screen masking. Do not run the phone app or another BLE uploader concurrently.

`[capabilities]` reports evidence, rather than probing support from ACK success: GIF and RAM program staging/switching are verified on this unit; raw frames are not. Direct PNG/JPEG, arbitrary live streaming, flash persistence, and total available program-slot count are unverified. The watcher's two-slot cache is intentionally limited to tested behavior.

## Verified fast transport

The calibrated model enables `stream_chunk = 960` and zero extra post-ACK delay. Each logical packet is split into writes no larger than 243 bytes; its matching ACK follows complete reassembly. Preparation and item definition are combined into one setup packet. The original 196-byte builder is retained byte-for-byte and is the automatic fallback after a failed fast upload, together with 300 ms pacing. The generic driver default remains 196. Larger 1,896-byte chunks timed out on this unit and are not enabled.

This speeds full-detail images without changing resolution or reducing the palette. A detailed cover-plus-spectrum measured about five seconds per transfer; another live cover measured 3.5–3.8 seconds. Radio conditions and encoded size vary. `[capabilities].live_regions = false` records that reliable native partial updates were not established: region experiments flashed, shifted the layout, or froze. The supported visualizer composes complete images on the host and uses staged program uploads; the user confirmed restored detail and persistent bars between updates.
