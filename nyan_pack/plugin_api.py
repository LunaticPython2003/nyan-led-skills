"""The small Python API available inside content plugins."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from dataclasses import field


@dataclass
class Frame:
    key: str
    source: str
    label: str = ""

    def __post_init__(self):
        self.source = str(self.source)
        if not isinstance(self.key, str) or not self.key or not self.source:
            raise ValueError("A frame requires a nonempty string key and source")
        if len(self.source) > 65536 or len(self.key) > 8192:
            raise ValueError("Frame key or source is too long")


@dataclass
class Region:
    """An independently updated image within a device-sized scene."""
    id: int
    x: int
    y: int
    width: int
    height: int
    frame: Frame

    def __post_init__(self):
        if type(self.id) is not int or not 1 <= self.id <= 15:
            raise ValueError("Region id must be between 1 and 15; zero is the background")
        if not all(type(value) is int for value in (self.x, self.y, self.width, self.height)):
            raise ValueError("Region bounds must be integers")
        if min(self.x, self.y) < 0 or min(self.width, self.height) <= 0 or not isinstance(self.frame, Frame):
            raise ValueError("Region requires valid bounds and a Frame")


@dataclass
class Scene:
    """A background plus small live regions; independent of content providers."""
    key: str
    background: Frame
    regions: list[Region]
    label: str = ""
    created_at: float = field(default_factory=time.monotonic)
    background_size: tuple[int, int] | None = None

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key or not isinstance(self.background, Frame):
            raise ValueError("Scene requires a key and background Frame")
        if not isinstance(self.regions, list) or not 1 <= len(self.regions) <= 15 or not all(isinstance(region, Region) for region in self.regions):
            raise ValueError("Scene requires 1–15 Regions")
        if len({region.id for region in self.regions}) != len(self.regions):
            raise ValueError("Scene region ids must be unique")
        if self.background_size is not None and (len(self.background_size) != 2 or not all(type(value) is int and value > 0 for value in self.background_size)):
            raise ValueError("background_size must be positive width/height")


@dataclass
class Context:
    inputs: dict
    cache_dir: Path
    width: int
    height: int

    def image(self, image, key: str, label: str = "", *, name="frame") -> Frame:
        """Publish a Pillow image atomically; return its source frame."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not name or not all(character.isalnum() or character in "_-" for character in name):
            raise ValueError("Image name must contain letters, numbers, underscores, or hyphens")
        pending = self.cache_dir / f"{name}.part.png"
        image.save(pending, format="PNG")
        destination = self.cache_dir / f"{name}.png"
        os.replace(pending, destination)
        return Frame(key, str(destination), label)

    def run(self, argv: list[str], timeout: float = 5):
        """Query an external program without a shell, with a bounded wait."""
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)

    def animation(self, frames, key, label="", *, duration=100, loop=0):
        """Publish an animated GIF without making a plugin handle file writes."""
        frames = list(frames)
        if not 1 <= len(frames) <= 200:
            raise ValueError("Animation requires 1–200 frames")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pending = self.cache_dir / "animation.part.gif"
        frames[0].save(pending, format="GIF", save_all=True, append_images=frames[1:], duration=duration, loop=loop, disposal=2)
        destination = self.cache_dir / "animation.gif"
        os.replace(pending, destination)
        return Frame(key, str(destination), label)
