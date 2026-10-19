"""Convert plugin sources into complete, bounded GIFs before touching BLE."""

from dataclasses import dataclass
import hashlib
import json
from io import BytesIO
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageFilter, ImageOps, ImageSequence

from .devices.loy import MAX_GIF_BYTES

MAX_SOURCE_BYTES = 8 * 1024 * 1024


@dataclass
class EncodedFrame:
    data: bytes
    digest: str
    label: str
    frames: int
    created_at: float | None = None
    max_age: float | None = None


class ContentEncoder:
    """Compose general content on the host; the device receives complete GIFs."""
    def __init__(self, settings, directory, output, operation):
        self.settings = settings | {key: output[key] for key in ("quality", "fit", "colors", "max_bytes") if key in output}
        self.directory, self.output, self.operation = directory, output, operation
        self.background_key = None
        self.background = None

    def encode(self, content):
        if "background" not in content:
            return encode(content, self.settings, self.directory, self.output.get("formats"))
        size = content.get("background_size") or (self.settings["width"], self.settings["height"])
        if not all(0 < value <= self.settings[key] for value, key in zip(size, ("width", "height"))):
            raise ValueError("Scene background extends outside the calibrated display")
        key = json.dumps([content["background"], size], sort_keys=True)
        if key != self.background_key:
            self.background = self.read_image(content["background"], size)
            self.background_key = key
        image = Image.new("RGB", (self.settings["width"], self.settings["height"]), "black")
        image.paste(self.background)
        for region in content["regions"]:
            x, y, width, height = (region[name] for name in ("x", "y", "width", "height"))
            if x < 0 or y < 0 or x + width > image.width or y + height > image.height:
                raise ValueError("Scene region extends outside the calibrated display")
            image.paste(self.read_image(region["frame"], (width, height)), (x, y))
        self.directory.mkdir(parents=True, exist_ok=True)
        source = self.directory / "scene.png"
        image.save(source)
        result = encode({"source": str(source), "label": content.get("label", "")}, self.settings, self.directory, ["png"])
        result.created_at = content["created_at"]
        result.max_age = self.operation.get("max_age")
        return result

    def read_image(self, frame, dimensions):
        with Image.open(BytesIO(source_bytes(frame["source"]))) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("Live scene regions must be static images; use Frame for animated GIFs")
            return resize(source, dimensions, self.settings["fit"])


def source_bytes(source):
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https", "file"):
        request = Request(source, headers={"User-Agent": "nyan-pack/0.2"})
        with urlopen(request, timeout=15) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
    elif not parsed.scheme:
        with Path(source).expanduser().open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    else:
        raise ValueError(f"Unsupported image URL scheme: {parsed.scheme}")
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Plugin image exceeds the 8 MiB source limit")
    return data


def resize(image, dimensions, fit):
    image = ImageOps.exif_transpose(image).convert("RGBA")
    background = Image.new("RGBA", image.size, "black")
    image = Image.alpha_composite(background, image).convert("RGB")
    if fit == "cover":
        return ImageOps.fit(image, dimensions, method=Image.Resampling.LANCZOS)
    if fit == "contain":
        return ImageOps.pad(image, dimensions, method=Image.Resampling.LANCZOS, color="black")
    return image.resize(dimensions, Image.Resampling.LANCZOS)


def enhance(image, quality):
    if quality == "detail":
        balanced = ImageOps.autocontrast(image, cutoff=0.5, preserve_tone=True)
        image = Image.blend(image, balanced, 0.35)
        image = image.filter(ImageFilter.UnsharpMask(radius=0.7, percent=140, threshold=2))
    return image


def encode(frame, settings, directory, formats=None):
    raw = source_bytes(frame["source"])
    dimensions = settings["width"], settings["height"]
    with Image.open(BytesIO(raw)) as source:
        format_name = source.format.lower()
        if formats and format_name not in formats:
            raise ValueError(f"Plugin declares {formats}, but returned {format_name}")
        count = getattr(source, "n_frames", 1)
        if count > 200:
            raise ValueError("Animated sources may contain at most 200 frames")
        if format_name == "gif" and source.size == dimensions and settings["quality"] == "natural" and "colors" not in settings:
            data = raw  # Preserve native GIF timing, palette, and animation exactly.
        else:
            frames, durations = [], []
            loop = source.info.get("loop", 0)
            for item in ImageSequence.Iterator(source):
                image = enhance(resize(item, dimensions, settings["fit"]), settings["quality"])
                if "colors" in settings:
                    image = image.quantize(colors=settings["colors"], dither=Image.Dither.NONE)
                frames.append(image)
                durations.append(max(20, int(item.info.get("duration", 100))))
            output = BytesIO()
            if len(frames) > 1:
                frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=loop, disposal=2, interlace=False)
            else:
                frames[0].save(output, format="GIF", interlace=False)
            data = output.getvalue()
    byte_limit = min(MAX_GIF_BYTES, settings.get("max_bytes", MAX_GIF_BYTES))
    if len(data) > byte_limit:
        raise ValueError(f"Encoded GIF is {len(data)} bytes; output limit is {byte_limit}. Reduce animation length or colors.")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".part.gif", delete=False) as stream:
        pending = Path(stream.name)
        stream.write(data)
    try:
        os.replace(pending, directory / "output.gif")
    finally:
        pending.unlink(missing_ok=True)
    return EncodedFrame(data, hashlib.sha256(data).hexdigest(), frame.get("label", ""), count)
