"""Device manifests are separate from content plugins and user calibration."""

import importlib
from pathlib import Path
import tomllib


def models(paths):
    result = {}
    for folder in (Path(__file__).parent, paths.config / "devices"):
        if folder.exists():
            for path in sorted(folder.glob("*.toml")):
                data = tomllib.loads(path.read_text())
                if data.get("schema") != 1 or not data.get("id") or ":" not in data.get("driver", ""):
                    raise ValueError(f"Invalid device manifest: {path}")
                result[data["id"]] = data
    return result


def device_settings(paths, config):
    saved = config["device"]
    available = models(paths)
    if saved["model"] not in available:
        raise ValueError(f"Unknown device model: {saved['model']}")
    model = available[saved["model"]]
    result = model["display"] | model["transport"] | saved
    if not all(type(result[key]) is int and 1 <= result[key] <= 256 for key in ("width", "height")):
        raise ValueError("Invalid device dimensions")
    if result["quality"] not in ("natural", "detail"):
        raise ValueError("Device quality must be natural or detail")
    if result["fit"] not in ("cover", "contain", "stretch"):
        raise ValueError("Device fit must be cover, contain, or stretch")
    if not isinstance(result["address"], str) or type(result.get("calibrated")) is not bool:
        raise ValueError("Device requires an address string and calibrated boolean")
    if type(result.get("staged_upload", False)) is not bool:
        raise ValueError("staged_upload must be boolean")
    return model, result


def create_driver(model, settings):
    module, name = model["driver"].split(":", 1)
    factory = getattr(importlib.import_module(module), name)
    arguments = {key: settings[key] for key in ("chunk_delay", "ack_timeout", "staged_upload", "stream_chunk") if key in settings}
    return factory(settings["address"] or None, **arguments)
