import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from nyan_pack.cli import request
from nyan_pack.daemon import Watcher
from nyan_pack.rendering import encode
from nyan_pack.devices.loy import UploadResult
from nyan_pack.settings import Paths, defaults, load_config, save_config
from test_plugins import manifest


class FakeDriver:
    is_connected = False
    address = "AA:BB:CC:DD:EE:FF"

    def __init__(self):
        self.uploads = []
        self.block = None
        self.fail_once = False

    async def upload(self, data):
        self.is_connected = True
        self.uploads.append(data)
        if self.fail_once:
            self.fail_once = False
            self.is_connected = False
            raise ConnectionError("link lost")
        try:
            if self.block is not None:
                await self.block.wait()
            return UploadResult(len(data), (len(data)+195)//196, 0)
        except asyncio.CancelledError:
            self.is_connected = False
            raise

    async def close(self):
        self.is_connected = False


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = Paths(root/"config", root/"cache", root/"runtime")
        self.plugins = self.paths.config / "plugins"
        self.plugins.mkdir(parents=True)
        self.write_plugin("one", "red")
        self.write_plugin("two", "blue")
        config = defaults()
        config["device"]["calibrated"] = True
        config["active_plugin"] = "one"
        config["retry"] = 10
        save_config(self.paths, config)
        self.driver = FakeDriver()
        self.watcher = Watcher(self.paths, driver=self.driver)
        self.task = None

    def write_plugin(self, identifier, color):
        code = f'''from PIL import Image
def poll(ctx):
    return ctx.image(Image.new("RGB", (ctx.width, ctx.height), "{color}"), "{color}", "{identifier}:{color}")
'''
        (self.plugins/f"{identifier}.toml").write_text(manifest(identifier, code))

    async def start(self):
        self.task = asyncio.create_task(self.watcher.run())
        self.addAsyncCleanup(self.finish)
        await self.until(lambda: self.paths.socket.exists())

    async def finish(self):
        self.watcher.stop.set()
        await asyncio.wait_for(self.task, 5)

    async def until(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    async def test_socket_plugin_switch_and_hot_reload(self):
        await self.start()
        await self.until(lambda: self.watcher.phase == "displaying")
        status = await request(self.paths, {"command": "use", "plugin": "two"})
        self.assertEqual(status["plugin"], "two")
        await self.until(lambda: self.watcher.last_upload["label"] == "two:blue")
        self.write_plugin("two", "green")
        await self.until(lambda: self.watcher.last_upload["label"] == "two:green")
        self.assertEqual(load_config(self.paths)["active_plugin"], "two")

    async def test_switch_cancels_old_transfer_and_control_stays_responsive(self):
        self.driver.block = asyncio.Event()
        await self.start()
        await self.until(lambda: self.watcher.phase == "uploading")
        status = await asyncio.wait_for(request(self.paths, {"command": "status"}), 0.5)
        self.assertEqual(status["phase"], "uploading")
        await request(self.paths, {"command": "use", "plugin": "two"})
        self.driver.block.set()
        await self.until(lambda: self.watcher.last_upload is not None and self.watcher.last_upload["label"] == "two:blue")

    async def test_new_plugin_bypasses_failed_image_retry_delay(self):
        self.driver.fail_once = True
        await self.start()
        await self.until(lambda: self.watcher.phase == "upload_error")
        await request(self.paths, {"command": "use", "plugin": "two"})
        await self.until(lambda: self.watcher.last_upload is not None and self.watcher.last_upload["label"] == "two:blue", timeout=3)

    async def test_returning_to_same_plugin_frame_reuses_encoded_image(self):
        with patch("nyan_pack.rendering.encode", wraps=encode) as render:
            await self.start()
            await self.until(lambda: self.watcher.last_upload is not None)
            await request(self.paths, {"command": "use", "plugin": "two"})
            await self.until(lambda: self.watcher.last_upload["label"] == "two:blue")
            await request(self.paths, {"command": "use", "plugin": "one"})
            await self.until(lambda: self.watcher.last_upload["label"] == "one:red")
            self.assertEqual(render.call_count, 2)

    async def test_switch_to_identical_content_finishes_without_duplicate_upload(self):
        self.write_plugin("two", "red")
        await self.start()
        await self.until(lambda: self.watcher.phase == "displaying")
        uploads = len(self.driver.uploads)
        await request(self.paths, {"command": "use", "plugin": "two"})
        await self.until(lambda: self.watcher.phase == "displaying")
        self.assertEqual(len(self.driver.uploads), uploads)

    async def test_expired_live_scene_is_resampled_even_when_content_key_is_unchanged(self):
        code = '''import time
from PIL import Image
from nyan_pack.plugin_api import Scene, Region
counter = 0
def poll(ctx):
    global counter
    counter += 1
    background = ctx.image(Image.new("RGB", (96,104), "red"), "background", name="background")
    strip = ctx.image(Image.new("RGB", (96,24), "blue"), "strip", name="strip")
    return Scene("same-key", background, [Region(1,0,104,96,24,strip)], "live",
                 created_at=time.monotonic() - (100 if counter == 1 else 0), background_size=(96,104))
'''
        text = manifest("one", code).replace("timeout = 2", "timeout = 2\nmax_age = 1")
        (self.plugins/"one.toml").write_text(text)
        await self.start()
        await self.until(lambda: self.watcher.phase == "displaying" and self.watcher.last_upload is not None)
        self.assertEqual(self.watcher.dropped_frames, 1)
        self.assertEqual(len(self.driver.uploads), 1)

    async def test_missing_source_retains_last_display_and_idle_disconnect_restores_it(self):
        await self.start()
        await self.until(lambda: self.watcher.phase == "displaying")
        first_count = len(self.driver.uploads)
        self.driver.is_connected = False
        await self.until(lambda: len(self.driver.uploads) > first_count)
        (self.plugins/"empty.toml").write_text(manifest("empty"))
        await request(self.paths, {"command": "use", "plugin": "empty"})
        await self.until(lambda: self.watcher.phase == "waiting_for_source")
        self.assertEqual(self.watcher.last_upload["label"], "one:red")

    async def test_calibration_requires_visual_confirmation_then_restarts_plugin(self):
        await self.start()
        with self.assertRaises(ValueError):
            await request(self.paths, {"command": "confirm_calibration"})
        await request(self.paths, {"command": "calibrate", "dimensions": [24, 32]})
        await self.until(lambda: self.watcher.phase == "calibration")
        with Image.open(BytesIO(self.driver.uploads[-1])) as image:
            self.assertEqual(image.size, (24, 32))
        await request(self.paths, {"command": "confirm_calibration"})
        self.assertEqual(load_config(self.paths)["device"]["width"], 24)
        await self.until(lambda: self.watcher.phase == "displaying")
