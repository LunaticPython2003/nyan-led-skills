# nyan-pack

A Linux plugin watcher for Nyan / LOY BLE LED backpacks. Spotify, other MPRIS players, local images/GIFs, and Python content plugins share one persistent Bluetooth connection. Currently the imlemenation is live for Spotify and there's no reason for it to not work with other players or services too by adding the relevant plugin. No Spotify API credentials are required.

The calibrated profile for **YS624#####** uses the whole panel at **96×128**, with the stronger `detail` artwork preset. Other devices require visual calibration. The BLE driver preserves the original GIF packet format and acknowledged static uploader.

## Setup and service

Requires Linux/BlueZ, Python 3.14+, [uv](https://docs.astral.sh/uv/), and `playerctl` for media plugins:

```bash
sudo apt install playerctl bluez
uv sync
uv run nyan service install
uv run nyan status
```

The installer enables `nyan-pack.service` for your user, starts it immediately, and creates `~/.local/bin/nyan` when that path is available. Use `uv run nyan` from this checkout if the launcher is outside your shell's PATH. The service starts when your user systemd session starts and requires your desktop session's MPRIS bus. Keep this checkout and its `.venv` in place, or reinstall the service after moving them.

Previously saved `~/.config/nyan-spotify/display_size` and `artwork_quality` preferences migrate on first start. This machine's existing 96×128 calibration is retained. A new installation waits for calibration before showing plugin content:

```bash
uv run nyan scan
uv run nyan device configure --model loy-ys6247040189l --address E0:6E:41:FA:FE:69
uv run nyan calibrate --size 96x128
# Wait for the pattern. Check the COMPLETE physical panel:
# GREEN top, BLUE left, YELLOW right, RED bottom; no unused LEDs outside.
uv run nyan calibrate --confirm
```

Try another `--size WIDTHxHEIGHT` if edges are clipped or LEDs remain outside the pattern. An upload ACK cannot establish native resolution. See [device profiles and calibration](docs/devices.md).

## Select content from the terminal

```bash
nyan plugins list
nyan use spotify
nyan use mpris                         # Any MPRIS player; prefer one playing
nyan use mpris --set player=firefox     # Or vlc, mpv, another playerctl name
nyan use clock --set color=cyan
nyan show ~/Pictures/cover.png
nyan show ~/Pictures/animation.gif
nyan show --color red
nyan show --text HELLO
nyan device configure --quality detail --fit cover
nyan status
nyan service logs
```

Players must publish `mpris:artUrl`; a player or browser that provides no cover cannot supply one. Missing artwork leaves the previous display in place. Same-artwork tracks do not trigger duplicate transfers. `playerctl --list-all` lists your media players. The `spotify` plugin is an explicit Spotify preset of the same general MPRIS provider.

`show` uses the running watcher, switching to the `image` plugin, which also notices subsequent file changes. With the watcher stopped, `show` performs a single acknowledged upload. The original commands `uv run scripts/display.py --color red`, `scripts/scan.py`, and `scripts/discover.py` remain as compatibility entry points. Stop the service before `discover`, which opens its own GATT connection.

## Upload behavior and speed

On the calibrated device, the driver alternates between RAM program slots 1 and 2. The current image/GIF keeps playing while the next complete GIF uploads into the inactive slot. Only after every chunk is acknowledged does one short command select the new program. Returning to an image still in those two slots sends only that command. This was visually confirmed with blue, green, and full-resolution album artwork; one selection command was acknowledged in **180 ms**. This is ACK latency, not an instrumented measurement of panel refresh.

A new connection starts with a short screen-off bootstrap while the first known program loads, suppressing the firmware's “Amusing LED” text. Subsequent successful transfers leave the current program visible. A failed inactive upload never selects partial data. If selection becomes uncertain, the driver masks the screen, disconnects, and retries conservatively. It never sends the default-animation CLEAR command.

Holding Bluetooth open removes repeated connection setup; it does not remove image-transfer time. A full cover currently takes roughly **15–18 seconds**, depending on compressed size and radio conditions. A GIF already on the device animates locally. Arbitrary live framebuffer streaming and direct PNG/JPEG/raw RGB transmission remain unverified. Continuously sending full images would queue slow transfers; the watcher instead retains only the newest pending frame. Cached program switches are fast, while new content still needs an upload.

The established 196-byte chunks and matching ACKs are retained, with 30 ms extra pacing after each ACK. Failures fall back to 300 ms pacing and a fresh connection. No speculative packet windowing or per-packet retries are used. Current GIF limit: 49,980 bytes / 255 chunks; source limit: 8 MiB and 200 animation frames.

## Write plugins

Content plugins are TOML files with **inputs**, **requires**, **output**, **operation**, and **python** sections. They can query a binary, import a library, generate Pillow images, or return local/file/HTTP(S) image sources. Python code runs in a persistent subprocess with a bounded polling timeout.

```bash
nyan plugins new my_display
# Edit ~/.config/nyan-pack/plugins/my_display.toml
nyan plugins validate ~/.config/nyan-pack/plugins/my_display.toml
nyan use my_display
```

The watcher discovers files automatically and reloads changes to the active plugin. [Plugin format and Python API](docs/plugins.md) includes a complete example. Device manifests and BLE drivers are separate plugins: see [device profiles](docs/devices.md). TOML uses Python's standard library; no YAML dependency is needed.

Preferences: `~/.config/nyan-pack/config.json`. User content plugins: `~/.config/nyan-pack/plugins/`. User device manifests: `~/.config/nyan-pack/devices/`. Encoded previews: `~/.cache/nyan-pack/plugins/<id>/output.gif`. XDG directory overrides are supported.

## Development

```bash
nyan service stop
uv run nyan watch --dry-run             # Real plugins/conversion, no Bluetooth
# In another terminal: uv run nyan use spotify; uv run nyan status
# Ctrl-C the foreground watcher before restarting the service.
nyan service start
uv run python -m unittest discover -s tests -v
```

Regression tests exercise worker timeouts, plugin switching/reloading, rendering and GIF animation preservation, retries, connection reuse, inactive-slot staging, cached selection, and ACK validation without hardware or network. Ad-hoc protocol probes were removed. Supported calibration and BLE inspection live in the CLI. Themed animation generators are preserved under [examples/](examples/) and send through the watcher.

[Protocol notes](docs/protocol.md) record verified bytes, experiments, and sources. This project began with reverse engineering of the [Nyan Gear](https://nyangear.com/) / LOY SPACE backpack protocol; stored-program research also used the primary implementation in [popledder](https://github.com/eskibars/popledder). Android MediaSession/BluetoothGatt support remains future work.
