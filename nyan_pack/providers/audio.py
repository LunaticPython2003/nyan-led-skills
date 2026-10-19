"""Desktop playback capture and an eight-band spectrum; no Spotify dependency."""

from array import array
import atexit
import cmath
import hashlib
from io import BytesIO
import math
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageOps

from ..plugin_api import Region, Scene
from .mpris import album_art
from ..rendering import enhance, source_bytes

RATE = 8000
WINDOW = 512


def spectrum(samples, gain=1.0):
    """Hann-window FFT into eight log-spaced frequency bands (0..1)."""
    if len(samples) != WINDOW or not math.isfinite(gain) or not 0 < gain <= 100:
        raise ValueError("Spectrum requires 512 samples and gain between zero and 100")
    values = [complex(sample / 32768 * (0.5 - 0.5 * math.cos(2 * math.pi * index / (WINDOW - 1))))
              for index, sample in enumerate(samples)]
    # Iterative radix-two FFT, keeping the plugin dependency-free.
    target = 0
    for index in range(1, WINDOW):
        bit = WINDOW >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if index < target:
            values[index], values[target] = values[target], values[index]
    size = 2
    while size <= WINDOW:
        step = cmath.exp(-2j * math.pi / size)
        for start in range(0, WINDOW, size):
            factor = 1
            for index in range(start, start + size // 2):
                even, odd = values[index], factor * values[index + size // 2]
                values[index], values[index + size // 2] = even + odd, even - odd
                factor *= step
        size *= 2
    edges = [40, 80, 160, 315, 630, 1250, 2000, 3000, 4000]
    bands = []
    for low, high in zip(edges, edges[1:]):
        first, last = max(1, int(low * WINDOW / RATE)), min(WINDOW // 2, int(high * WINDOW / RATE))
        amplitude = max(abs(values[index]) for index in range(first, max(first + 1, last))) * 4 / WINDOW * gain
        decibels = 20 * math.log10(max(amplitude, 1e-9))
        bands.append(max(0, min(1, (decibels + 55) / 55)))
    return bands


class PlaybackCapture:
    def __init__(self, source="@DEFAULT_MONITOR@"):
        if source != "@DEFAULT_MONITOR@" and not source.endswith(".monitor"):
            raise ValueError("Use a playback monitor source (.monitor), not a microphone")
        self.process = subprocess.Popen(
            ["parec", "--raw", f"--device={source}", "--format=s16le", f"--rate={RATE}",
             "--channels=1", "--latency-msec=50", "--client-name=nyan-visualizer"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.latest = None
        self.timestamp = 0
        threading.Thread(target=self._read, daemon=True).start()
        atexit.register(self.close)

    def _read(self):
        while True:
            data = self.process.stdout.read(WINDOW * 2)
            if len(data) != WINDOW * 2:
                self.ready.set()
                return
            with self.lock:
                self.latest, self.timestamp = data, time.monotonic()
            self.ready.set()

    def sample(self):
        self.ready.wait(1)
        with self.lock:
            data, timestamp = self.latest, self.timestamp
        if data is None or time.monotonic() - timestamp > 2:
            raise RuntimeError("No desktop playback samples from parec; check the monitor source")
        samples = array("h", data)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples, timestamp

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process.stdout is not None:
            self.process.stdout.close()


class AudioVisualizer:
    def __init__(self):
        self.capture = None
        self.background = None
        self.queried_at = 0
        self.levels = [0] * 8
        self.artwork = None
        self.cover_key = None

    def poll(self, ctx):
        now = time.monotonic()
        if now - self.queried_at >= 2:
            self.background = album_art(ctx.inputs["player"])
            self.queried_at = now
        if self.background is None:
            return None
        height = ctx.inputs["height"]
        if not 8 <= height <= ctx.height // 2:
            raise ValueError("Visualizer height must be 8 pixels to half the panel height")
        configuration = (self.background.key, ctx.width, ctx.height, height, ctx.inputs["color"])
        if configuration != self.cover_key:
            with Image.open(BytesIO(source_bytes(self.background.source))) as source:
                original = enhance(ImageOps.fit(source.convert("RGB"), (ctx.width, ctx.height - height)), "detail")
            self.artwork = ctx.image(original, self.background.key, self.background.label, name="background")
            self.cover_key = configuration
        if self.capture is None:
            self.capture = PlaybackCapture(ctx.inputs["source"])
        samples, timestamp = self.capture.sample()
        levels = spectrum(samples, ctx.inputs["gain"])
        self.levels = [max(level, previous * 0.65) for level, previous in zip(levels, self.levels)]
        heights = [round(level * (height - 2)) for level in self.levels]
        image = Image.new("RGB", (ctx.width, height), "black")
        draw = ImageDraw.Draw(image)
        for index, value in enumerate(heights):
            left, right = index * ctx.width // 8, (index + 1) * ctx.width // 8
            if value:
                draw.rectangle((left + 1, height - value, right - 2, height - 1), fill=ctx.inputs["color"])
        key = hashlib.sha256(bytes(heights)).hexdigest()
        bars = ctx.image(image, key, "Audio spectrum", name="spectrum")
        return Scene(f"{self.background.key}:{key}", self.artwork,
                     [Region(1, 0, ctx.height - height, ctx.width, height, bars)],
                     f"{self.background.label} + live audio", created_at=timestamp,
                     background_size=(ctx.width, ctx.height - height))
