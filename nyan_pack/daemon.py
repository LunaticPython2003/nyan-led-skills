"""Single BLE owner, plugin discovery, bounded workers, and terminal control."""

import asyncio
from collections import OrderedDict
from contextlib import suppress
import json
import logging
import os
import signal
import time

from .calibration import bounds
from .devices import create_driver, device_settings
from .devices.loy import UploadResult
from .plugins import PluginWorker, discover
from .rendering import ContentEncoder, encode
from .settings import atomic_json, instance_lock, load_config, save_config

logger = logging.getLogger(__name__)


class DryRunDriver:
    is_connected = False
    address = "dry-run"

    async def upload(self, data):
        self.is_connected = True
        return UploadResult(len(data), (len(data) + 195) // 196, 0)

    async def close(self):
        self.is_connected = False



class Watcher:
    def __init__(self, paths, *, dry_run=False, driver=None):
        self.paths = paths
        self.config = load_config(paths)
        self.model, self.device = device_settings(paths, self.config)
        self.driver = driver or (DryRunDriver() if dry_run else create_driver(self.model, self.device))
        self.dry_run = dry_run
        self.phase = "starting"
        self.error = None
        self.last_frame = None
        self.last_upload = None
        self.generation = 0
        self.pending = None
        self.changed = asyncio.Event()
        self.stop = asyncio.Event()
        self.producer = None
        self.producers = set()
        self.upload_task = None
        self.active_fingerprint = None
        self.mode = "plugin"
        self.calibration_pending = None
        self.retry_frame = None
        self.retry_at = 0
        self.frame_cache = OrderedDict()
        self.dropped_frames = 0
        self.refresh_requested = False

    def status(self):
        plugins, errors = discover(self.paths)
        return {
            "pid": os.getpid(), "phase": self.phase, "plugin": self.config["active_plugin"],
            "connected": bool(self.driver.is_connected), "dry_run": self.dry_run,
            "device": self.device, "capabilities": self.model.get("capabilities", {}),
            "last_frame": self.last_frame, "last_upload": self.last_upload,
            "error": self.error, "plugins": sorted(plugins), "plugin_errors": errors,
            "pending": self.pending is not None, "mode": self.mode,
            "programs": getattr(self.driver, "programs", []), "dropped_frames": self.dropped_frames,
            "transport": {key: getattr(self.driver, key) for key in ("stream_chunk", "chunk_delay") if hasattr(self.driver, key)},
        }

    def restart_producer(self):
        self.generation += 1
        self.pending = None
        self.changed.set()
        if self.producer is not None:
            self.producer.cancel()
        if self.upload_task is not None:
            self.upload_task.cancel()
        if self.mode != "plugin":
            return
        if not self.device.get("calibrated"):
            self.phase = "calibration_required"
            return
        self.phase, self.error = "waiting_for_plugin", None
        task = asyncio.create_task(self.produce(self.generation))
        self.producer = task
        self.producers.add(task)
        task.add_done_callback(self.producers.discard)

    async def produce(self, generation):
        worker = None
        candidate, candidate_since = None, 0
        last_key = None
        retry_at = 0
        try:
            plugins, errors = discover(self.paths)
            plugin = plugins.get(self.config["active_plugin"])
            self.active_fingerprint = plugin.fingerprint if plugin else None
            if plugin is None:
                raise ValueError(f"Active plugin is missing or invalid: {errors or self.config['active_plugin']}")
            inputs = self.config["plugin_inputs"].get(plugin.id, {})
            directory = self.paths.cache / "plugins" / plugin.id
            worker = PluginWorker(plugin, inputs, directory, (self.device["width"], self.device["height"]))
            operation = plugin.data["operation"]
            encoder = ContentEncoder(self.device, directory, plugin.data["output"], operation)
            while generation == self.generation:
                try:
                    frame = await worker.poll()
                    if frame is None:
                        self.phase = "waiting_for_source" if self.upload_task is None else self.phase
                        candidate = None
                    else:
                        key = frame["key"]
                        now = time.monotonic()
                        if key != candidate:
                            candidate = key
                            candidate_since = now - operation["settle"] if last_key is None else now
                            retry_at = 0
                        if now >= retry_at and now - candidate_since >= operation["settle"]:
                            if key != last_key or not self.driver.is_connected or self.refresh_requested:
                                cache_key = (plugin.id, plugin.fingerprint, json.dumps(inputs, sort_keys=True), key,
                                             self.device["width"], self.device["height"], self.device["quality"], self.device["fit"])
                                cacheable = "max_age" not in operation
                                encoded = self.frame_cache.get(cache_key) if cacheable else None
                                if encoded is None:
                                    encoded = await asyncio.to_thread(encoder.encode, frame)
                                    if cacheable:
                                        self.frame_cache[cache_key] = encoded
                                        if len(self.frame_cache) > 8:
                                            self.frame_cache.popitem(last=False)
                                else:
                                    self.frame_cache.move_to_end(cache_key)
                                if generation != self.generation:
                                    return
                                self.pending = generation, encoded, "plugin"
                                self.refresh_requested = False
                                self.changed.set()
                                last_key = key
                                self.error = None
                    await asyncio.sleep(operation["interval"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.error = str(exc)
                    self.phase = "plugin_error"
                    logger.warning("Plugin %s: %s", plugin.id, exc)
                    retry_at = time.monotonic() + self.config["retry"]
                    await asyncio.sleep(self.config["retry"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if generation == self.generation:
                self.phase, self.error = "plugin_error", str(exc)
                logger.error("%s", exc)
        finally:
            if worker is not None:
                await worker.close()

    async def upload_loop(self):
        while not self.stop.is_set():
            await self.changed.wait()
            self.changed.clear()
            if self.pending is None:
                continue
            generation, frame, mode = self.pending
            self.pending = None
            if generation != self.generation:
                continue
            if self.retry_frame == (generation, frame.digest) and time.monotonic() < self.retry_at:
                self.pending = generation, frame, mode
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), self.retry_at - time.monotonic())
                self.changed.set()
                continue
            if self.last_frame == frame.digest and self.driver.is_connected and mode == "plugin":
                self.phase, self.error = "displaying", None
                continue
            self.phase = "uploading"
            if frame.max_age is not None and time.monotonic() - frame.created_at > frame.max_age:
                self.phase = "displaying"
                self.dropped_frames += 1
                self.refresh_requested = True
                continue
            self.upload_task = asyncio.create_task(self.driver.upload(frame.data))
            try:
                result = await self.upload_task
                if generation == self.generation:
                    if not result.stale:
                        self.last_frame = frame.digest
                    self.last_upload = {"label": frame.label, "bytes": result.byte_count, "chunks": result.chunks, "seconds": result.seconds, "frames": frame.frames}
                    if frame.created_at is not None:
                        self.last_upload["sample_age"] = time.monotonic() - frame.created_at
                    self.phase = "calibration" if mode == "calibration" else "displaying"
                    self.error = None
                    self.retry_frame = None
                    logger.info("Displayed %s: %d bytes / %d chunks in %.2fs", frame.label, result.byte_count, result.chunks, result.seconds)
                    if not self.dry_run and not self.config["device"].get("address"):
                        self.config["device"]["address"] = self.driver.address
                        self.device["address"] = self.driver.address
                        save_config(self.paths, self.config)
            except asyncio.CancelledError:
                if self.stop.is_set():
                    raise
            except Exception as exc:
                self.error, self.phase = str(exc), "upload_error"
                logger.warning("Upload failed: %s", exc)
                self.retry_frame = generation, frame.digest
                self.retry_at = time.monotonic() + self.config["retry"]
                if generation == self.generation and self.pending is None:
                    self.pending = generation, frame, mode
                    self.changed.set()
            finally:
                self.upload_task = None

    async def scan_loop(self):
        while True:
            await asyncio.sleep(1)
            if self.mode != "plugin":
                continue
            plugins, _ = discover(self.paths)
            plugin = plugins.get(self.config["active_plugin"])
            fingerprint = plugin.fingerprint if plugin else None
            if fingerprint != self.active_fingerprint:
                self.active_fingerprint = fingerprint
                logger.info("Active plugin changed; reloading")
                self.restart_producer()

    async def command(self, request):
        action = request.get("command")
        if action == "status":
            return self.status()
        if action == "use":
            plugins, _ = discover(self.paths)
            selected = request["plugin"]
            if selected not in plugins:
                raise ValueError(f"Unknown plugin: {selected}")
            plugin = plugins[selected]
            supplied = self.config["plugin_inputs"].get(selected, {}) | request.get("inputs", {})
            plugin.inputs(supplied)
            missing = plugin.missing_dependencies()
            if missing:
                raise ValueError(f"Missing dependencies: {', '.join(missing)}")
            self.config["active_plugin"] = selected
            self.config["plugin_inputs"][selected] = supplied
            save_config(self.paths, self.config)
            self.mode = "plugin"
            self.restart_producer()
            return self.status()
        if action == "reload":
            config = load_config(self.paths)
            model, device = device_settings(self.paths, config)
            self.mode = "reloading"
            self.restart_producer()
            if self.upload_task is not None:
                with suppress(asyncio.CancelledError):
                    await self.upload_task
            await self.driver.close()
            self.config, self.model, self.device = config, model, device
            self.driver = DryRunDriver() if self.dry_run else create_driver(self.model, self.device)
            self.mode = "plugin"
            self.restart_producer()
            return self.status()
        if action == "calibrate":
            width, height = request["dimensions"]
            if not all(isinstance(value, int) and 8 <= value <= 256 for value in (width, height)):
                raise ValueError("Calibration dimensions must be between 8 and 256")
            self.mode = "calibration"
            self.restart_producer()
            self.calibration_pending = {"width": width, "height": height}
            path = self.paths.cache / "calibration.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(bounds((width, height)).save, path)
            settings = self.device | self.calibration_pending | {"quality": "natural"}
            frame = await asyncio.to_thread(encode, {"source": str(path), "label": f"Calibration {width}x{height}"}, settings, self.paths.cache / "calibration")
            self.pending = self.generation, frame, "calibration"
            self.changed.set()
            return self.status()
        if action == "confirm_calibration":
            if self.calibration_pending is None or self.phase != "calibration":
                raise ValueError("Display a calibration pattern and wait for completion before confirming")
            self.config["device"].update(self.calibration_pending, calibrated=True)
            save_config(self.paths, self.config)
            self.model, self.device = device_settings(self.paths, self.config)
            self.mode = "plugin"
            self.restart_producer()
            return self.status()
        if action == "stop":
            self.stop.set()
            return {"stopping": True}
        raise ValueError(f"Unknown command: {action}")

    async def client(self, reader, writer):
        try:
            async with asyncio.timeout(10):
                request = json.loads(await reader.readline())
                response = {"ok": True, "result": await self.command(request)}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write(json.dumps(response).encode() + b"\n")
        with suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    async def run(self):
        self.paths.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.socket.unlink(missing_ok=True)
        save_config(self.paths, self.config)
        server = await asyncio.start_unix_server(self.client, path=str(self.paths.socket), limit=1024 * 1024)
        self.paths.socket.chmod(0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        self.restart_producer()
        tasks = [asyncio.create_task(self.upload_loop()), asyncio.create_task(self.scan_loop())]
        try:
            async with server:
                await self.stop.wait()
        finally:
            self.stop.set()
            for task in [*tasks, *self.producers]:
                task.cancel()
            await asyncio.gather(*tasks, *self.producers, return_exceptions=True)
            await self.driver.close()
            self.paths.socket.unlink(missing_ok=True)


def run(paths, *, dry_run=False):
    with instance_lock(paths, hardware=not dry_run):
        asyncio.run(Watcher(paths, dry_run=dry_run).run())
