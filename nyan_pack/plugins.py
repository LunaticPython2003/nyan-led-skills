"""TOML plugin discovery and a persistent, killable Python worker per active plugin."""

import asyncio
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tomllib


@dataclass
class Plugin:
    path: Path
    data: dict
    fingerprint: str

    @property
    def id(self):
        return self.data["id"]

    def inputs(self, supplied):
        definitions = self.data.get("inputs", {})
        unknown = set(supplied) - set(definitions)
        if unknown:
            raise ValueError(f"Unknown inputs for {self.id}: {', '.join(sorted(unknown))}")
        result = {}
        for name, field in definitions.items():
            value = supplied.get(name, field.get("default"))
            if value is None:
                if field.get("required", False):
                    raise ValueError(f"Plugin {self.id} requires --set {name}=VALUE")
                continue
            kind = field.get("type", "string")
            expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool}[kind]
            if not isinstance(value, expected) or (kind in ("integer", "number") and isinstance(value, bool)):
                raise ValueError(f"Input {name} must be {kind}")
            if kind == "number" and not math.isfinite(value):
                raise ValueError(f"Input {name} must be finite")
            if "choices" in field and value not in field["choices"]:
                raise ValueError(f"Input {name} must be one of {field['choices']}")
            result[name] = value
        return result

    def missing_dependencies(self):
        requires = self.data.get("requires", {})
        missing = [f"binary:{name}" for name in requires.get("binaries", []) if shutil.which(name) is None]
        for name in requires.get("libraries", []):
            try:
                found = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                found = None
            if found is None:
                missing.append(f"python:{name}")
        missing.extend(f"env:{name}" for name in requires.get("environment", []) if not os.environ.get(name))
        return missing


def read_plugin(path: Path) -> Plugin:
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError("Plugin manifest exceeds 256 KiB")
    data = tomllib.loads(raw.decode())
    if data.get("schema") != 1 or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", data.get("id", "")):
        raise ValueError("Plugin requires schema=1 and a lowercase id")
    for section in ("inputs", "requires", "output", "operation", "python"):
        if not isinstance(data.get(section, {}), dict):
            raise ValueError(f"Plugin {section} must be a table")
    if data.get("output", {}).get("type") not in ("image", "scene"):
        raise ValueError("Plugin output.type must be image or scene")
    output = data["output"]
    if "quality" in output and output["quality"] not in ("natural", "detail"):
        raise ValueError("output.quality must be natural or detail")
    if "fit" in output and output["fit"] not in ("cover", "contain", "stretch"):
        raise ValueError("output.fit must be cover, contain, or stretch")
    for name, low, high in [("colors", 2, 256), ("max_bytes", 64, 49980)]:
        if name in output and (type(output[name]) is not int or not low <= output[name] <= high):
            raise ValueError(f"output.{name} must be between {low} and {high}")
    for name in ("binaries", "libraries", "environment"):
        requirements = data.get("requires", {}).get(name, [])
        if not isinstance(requirements, list) or not all(isinstance(item, str) and item for item in requirements):
            raise ValueError(f"requires.{name} must be a list of names")
    formats = data["output"].get("formats", [])
    if not isinstance(formats, list) or not all(isinstance(item, str) for item in formats):
        raise ValueError("output.formats must be a list")
    for name, definition in data.get("inputs", {}).items():
        if not isinstance(definition, dict) or definition.get("type", "string") not in ("string", "integer", "number", "boolean"):
            raise ValueError(f"Invalid input definition: {name}")
    operation = data.setdefault("operation", {})
    if "max_age" in operation:
        age = operation["max_age"]
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0.1 <= age <= 120:
            raise ValueError("operation.max_age must be between 0.1 and 120 seconds")
    for name, default, minimum, maximum in [("interval", 2, 0.5, 3600), ("settle", 2, 0, 60), ("timeout", 10, 0.1, 120)]:
        value = operation.setdefault(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"operation.{name} must be between {minimum} and {maximum}")
    code = data.get("python", {}).get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("Plugin requires python.code defining poll(ctx)")
    compile(code, str(path), "exec")
    return Plugin(path, data, hashlib.sha256(raw).hexdigest())


def discover(paths):
    plugins, errors = {}, {}
    for directory in (Path(__file__).with_name("plugins"), paths.config / "plugins"):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.toml")):
            try:
                plugin = read_plugin(path)
                plugins[plugin.id] = plugin
            except (OSError, ValueError, SyntaxError, TypeError) as exc:
                errors[str(path)] = str(exc)
    return plugins, errors


class PluginWorker:
    def __init__(self, plugin, inputs, cache_dir, dimensions):
        self.plugin = plugin
        self.inputs = plugin.inputs(inputs)
        self.cache_dir = cache_dir
        self.dimensions = dimensions
        self.process = None

    async def poll(self):
        if self.process is None:
            missing = self.plugin.missing_dependencies()
            if missing:
                raise RuntimeError(f"Missing plugin dependencies: {', '.join(missing)}")
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-m", "nyan_pack.worker", str(self.plugin.path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                start_new_session=True, limit=1024 * 1024,
            )
            request = {"inputs": self.inputs, "cache_dir": str(self.cache_dir), "dimensions": self.dimensions}
        else:
            request = {}
        try:
            async with asyncio.timeout(self.plugin.data["operation"]["timeout"]):
                self.process.stdin.write(json.dumps(request).encode() + b"\n")
                await self.process.stdin.drain()
                line = await self.process.stdout.readline()
                if not line:
                    raise RuntimeError("Plugin worker exited without a result")
                response = json.loads(line)
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("frame")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
