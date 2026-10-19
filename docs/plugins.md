# Content plugin format (schema 1)

Put a `.toml` file in `~/.config/nyan-pack/plugins/`. Its `id` is the terminal selector. Built-ins live in `nyan_pack/plugins/`; a user plugin with the same id overrides the built-in. New plugins appear automatically; editing the active plugin restarts its worker. Invalid files are reported by `nyan plugins list` and `nyan status --json`. A broken active plugin keeps the last display and can be replaced from the terminal.

Content can come from an HTTP API, RSS feed, local file, sensor, binary, library, or media player. The core accepts images, animated GIFs, and composed scenes; it has no album-art requirement.

## Complete example

```toml
schema = 1
id = "my_clock"
name = "My clock"
description = "Render local time, with a configurable color."

[inputs.color]
type = "string"
default = "cyan"
choices = ["cyan", "white", "yellow"]

[requires]
binaries = []
libraries = ["PIL"]
environment = []

[output]
type = "image"
formats = ["png"]

[operation]
interval = 1
settle = 0
timeout = 5

[python]
code = '''
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

def poll(ctx):
    text = datetime.now().strftime("%H:%M")
    image = Image.new("RGB", (ctx.width, ctx.height), "black")
    font = ImageFont.load_default(size=max(8, ctx.width // 4))
    ImageDraw.Draw(image).text(
        (ctx.width // 2, ctx.height // 2), text,
        font=font, fill=ctx.inputs["color"], anchor="mm",
    )
    return ctx.image(image, key=text, label=text)
'''
```

```bash
nyan plugins validate ~/.config/nyan-pack/plugins/my_clock.toml
nyan use my_clock --set color=yellow
```

## Sections

- `schema`: currently `1`. `id`: lowercase letter followed by letters, digits, `_`, or `-`, at most 64 characters. `name` and `description` describe the plugin.
- `inputs.<name>`: `type` is `string`, `integer`, `number`, or `boolean`. Supports `default`, `required = true`, and `choices`. Unknown input names and wrong types are rejected. `--set name=value` accepts a JSON literal or a plain string; values are saved per plugin.
- `requires.binaries`: executable names found on PATH. `requires.libraries`: Python **import names**, such as `PIL`, available in the watcher's virtual environment. `requires.environment`: required nonempty environment variables. These declarations check availability; they do not install packages or fetch secrets. Install Python dependencies with `uv add <package>` and restart the service; system tools need to be available on the service's PATH.
- `output.type`: `image` for static images/animated GIFs, or `scene` for a composed background and regions. `formats` lists acceptable Frame **source** encodings, such as `png`, `jpeg`, `gif`, `webp`, `bmp`; omit it to accept any Pillow-readable encoding. The renderer converts content to the device GIF format. Optional `quality` (`natural`/`detail`) and `fit` (`cover`/`contain`/`stretch`) override the device preset for this plugin. Optional `colors` (2–256) explicitly reduces the palette; it is never applied automatically. `max_bytes` (64–49980) rejects oversized encoded output before BLE, without silently reducing image quality.
- `operation.interval`: polling interval, 0.5–3600 seconds (default 2). `settle`: require subsequent changed keys to remain stable for 0–60 seconds (default 2); the first valid frame after selecting a plugin runs immediately. `timeout`: worker poll deadline, 0.1–120 seconds (default 10). On failure, the watcher uses the configured retry interval (default 20 seconds).
- `operation.max_age`: optional 0.1–120 seconds for live `Scene` samples. An expired pending scene is dropped before upload. The producer keeps sampling while BLE is busy. This limits stale queue entries, not total radio time. Live scenes bypass the general encoded-frame cache; the static background is still cached independently.
- `python.code`: normal synchronous Python defining `poll(ctx)`. Imports and module state survive between polls until a switch, edit, worker timeout, or restart.

## Python API

`ctx.inputs` contains validated input values. `ctx.width` and `ctx.height` come from the calibrated device. `ctx.cache_dir` is a writable per-plugin cache directory.

Return `Frame(key, source, label="")` from `nyan_pack.plugin_api`:

```python
from nyan_pack.plugin_api import Frame

def poll(ctx):
    result = ctx.run(["some-tool", "--image-path"], timeout=3)
    path = result.stdout.strip()
    return Frame(key=path, source=path, label="External artwork")
```

`source` can be a local path, `file://` URL, or HTTP(S) URL. A `key` must change when the image content changes; use modification time, an artwork URL, or a content digest. The watcher deduplicates unchanged frames, caches up to eight encoded outputs in memory, and retains only the newest pending frame while BLE is busy. After a source becomes available, switching back to a cached program can use just the board's selection command if its GIF is still resident in one of the two device slots. More distinct images evict those slots.

`ctx.image(pillow_image, key, label="", name="frame")` publishes an image atomically and returns its `Frame`. Use different names for separately published images in one scene. `ctx.animation(frames, key, label="", duration=100, loop=0)` publishes an actual animated GIF; `duration` is milliseconds or a per-frame list, and `loop=0` means continuous playback. You can also return an existing GIF path or URL directly. Native-size GIFs under the `natural` quality setting are transmitted byte-for-byte; other sources are resized, preserving animation durations and loop settings. Encoded GIFs exceeding 49,980 bytes are rejected before BLE activity.

Return `None` when there is no content yet: the previous display remains. Raise an exception for a query failure: it appears in status/logs and is retried. `ctx.run(argv, timeout=5)` invokes a binary without a shell and returns a checked `subprocess.CompletedProcess`; failures raise. A plugin must declare any binary it uses.

`print()` goes to the service journal. Plugin code runs as your user with normal filesystem/network access; process isolation provides timeout and failure handling, not a security sandbox. Plugins supply content; the watcher owns BLE and display state. Keep Bluetooth operations in device drivers.

## Composed scenes

A `Scene` combines a static `Frame` background with one or more `Region` images. For example, a weather map can have a sensor strip or a photo can have a clock. Sources remain ordinary files, URLs, or Pillow images; none needs Spotify.

```python
from nyan_pack.plugin_api import Frame, Region, Scene

def poll(ctx):
    # Publish your data image with a stable key that changes with its content.
    background = Frame("map-v1", "/path/to/map.png", "Weather map")
    strip = make_sensor_image(ctx.width, 24)  # Your own Pillow rendering function.
    overlay = ctx.image(strip, sensor_revision(), name="sensor")
    return Scene(
        key=f"{background.key}:{overlay.key}",
        background=background,
        regions=[Region(1, 0, ctx.height - 24, ctx.width, 24, overlay)],
        background_size=(ctx.width, ctx.height - 24),
        label="Weather and sensor",
    )
```

Region ids must be unique integers 1–15; coordinates and dimensions must fit the calibrated panel. `background_size` defaults to the whole display; a smaller size reserves space for a strip. Live scene sources are static images; return a plain `Frame` or `ctx.animation(...)` for animated GIF playback. Scenes are **composited on the host and sent as complete GIFs**, using the stable uploader. They do not enable the firmware's experimental partial-region updates. Background images are cached independently of changing regions. `Scene.created_at` defaults to the current monotonic time; capture plugins can supply the actual sample timestamp.

## Built-ins

| Plugin | Input | Behavior |
| --- | --- | --- |
| `spotify` | `player=spotify` | Spotify preset using the shared MPRIS provider |
| `mpris` | `player=auto` | Any player with artwork; prefer `Playing`; explicit player names supported |
| `image` | required `path` | Watch an image/GIF file using path, modification time, and size |
| `clock` | `color=white` | Render local HH:MM once per minute |
| `weather` | required `latitude`, `longitude`; `label`, `units` | Query Open-Meteo every 600 seconds; render a weather card |
| `news` | required `feed`; `label`, `page_seconds=4` | Poll RSS/Atom every 60 seconds; loop paged headline/summary text |
| `spotify_visualizer` | `player=spotify`, `source=@DEFAULT_MONITOR@`, `height=24`, `color=cyan`, `gain=1.0` | Compose full-detail media artwork and actual desktop playback spectrum |

For MPRIS, the provider uses one `playerctl` query, supports HTTPS/HTTP/file artwork URLs, and works for paused Spotify metadata too. A media client must expose album artwork; this cannot add art to metadata that does not contain it.

Weather uses the [Open-Meteo forecast API](https://open-meteo.com/en/docs) and explicit coordinates; no location is inferred. RSS and Atom are parsed with the Python standard library. News shows a readable headline/summary, up to four pages, rather than embedding a web browser. Editing a plugin's `operation.interval` controls refresh cadence independently of content type. Source errors preserve the previous display.

The audio plugin requires `parec` (`pulseaudio-utils` on Ubuntu/Zorin) and a PulseAudio-compatible playback monitor, including PipeWire's PulseAudio server. It reads the current monitor continuously, retaining the latest 512 mono samples at 8 kHz; it does not accumulate an audio backlog or capture the microphone. Frequency bands come from a Hann-window FFT, and every displayed shape reflects sampled audio. It accepts any MPRIS player through its `player` input. GIF upload latency makes the result a stepped, delayed visualization; a five-second buffer does not create more device bandwidth. No automatic cover downsampling or tiny palette is used.

## Watcher and service

`nyan use <id>` saves the selection and switches the active worker. A switch cancels obsolete work; a source change within a plugin settles before being queued. The watcher holds one BLE connection, matches ACKs, handles disconnects, retries whole transfers, and refuses a second watcher using an instance lock. Worker hangs are terminated independently. The local control socket is restricted to the user.

`nyan reload` rereads configuration and restarts the device connection. `nyan service restart` also loads Python application code changes. `nyan service logs` shows recent journal entries. User plugins that need extra environment variables can use a systemd user service override (`systemctl --user edit nyan-pack.service`) followed by a restart.
