"""Application paths, atomic preferences, and migration from the Spotify prototype."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    config: Path
    cache: Path
    runtime: Path

    @classmethod
    def current(cls):
        home = Path.home()
        cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "nyan-pack"
        return cls(
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "nyan-pack",
            cache,
            Path(os.environ["XDG_RUNTIME_DIR"]) / "nyan-pack" if os.environ.get("XDG_RUNTIME_DIR") else cache / "runtime",
        )

    @property
    def socket(self):
        return self.runtime / "control.sock"


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(pending, path)


def defaults():
    return {
        "version": 1,
        "active_plugin": "spotify",
        "plugin_inputs": {},
        "device": {"model": "loy-ys6247040189l", "address": "", "calibrated": False},
        "retry": 20.0,
    }


def load_config(paths: Paths):
    path = paths.config / "config.json"
    if path.exists():
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("Config must be a JSON object")
        if value.get("version") != 1:
            raise ValueError("Unsupported config version")
        merged = defaults()
        merged.update(value)
        if not isinstance(value.get("device", {}), dict):
            raise ValueError("Config device must be an object")
        merged["device"] = defaults()["device"] | value.get("device", {})
        retry = merged["retry"]
        if isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or not 0.5 <= retry <= 3600:
            raise ValueError("Config retry must be between 0.5 and 3600 seconds")
        if not isinstance(merged["active_plugin"], str) or not isinstance(merged["plugin_inputs"], dict):
            raise ValueError("Config requires active_plugin and plugin_inputs")
        if not all(isinstance(key, str) and isinstance(value, dict) for key, value in merged["plugin_inputs"].items()):
            raise ValueError("Each plugin's inputs must be an object")
        return merged
    config = defaults()
    # This exact unit was already calibrated during the prototype session.
    old = paths.config.parent / "nyan-spotify"
    if (old / "display_size").exists():
        width, height = parse_dimensions((old / "display_size").read_text().strip())
        config["device"].update(width=width, height=height, calibrated=True)
        if (old / "artwork_quality").exists():
            config["device"]["quality"] = (old / "artwork_quality").read_text().strip()
        if (width, height) == (96, 128):
            config["device"]["address"] = "E0:6E:41:FA:FE:69"
    return config


def save_config(paths, config):
    atomic_json(paths.config / "config.json", config)


def parse_dimensions(text):
    parts = str(text).lower().replace("×", "x").split("x")
    try:
        values = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("Use N or WIDTHxHEIGHT") from exc
    if len(values) not in (1, 2) or any(not 1 <= value <= 256 for value in values):
        raise ValueError("Dimensions must be between 1 and 256")
    return (values[0], values[0]) if len(values) == 1 else values


@contextmanager
def instance_lock(paths: Paths, *, hardware=True):
    paths.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = [paths.runtime / "watcher.lock"]
    legacy = Path.home() / ".cache/nyan-spotify/watcher.lock"
    if hardware and legacy.exists():
        files.append(legacy)
    handles = []
    try:
        for path in files:
            handle = path.open("a+")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("A watcher already owns the display; stop it before starting another") from exc
        handles[0].seek(0)
        handles[0].truncate()
        handles[0].write(str(os.getpid()))
        handles[0].flush()
        yield
    finally:
        for handle in reversed(handles):
            handle.close()
