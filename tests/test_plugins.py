import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from nyan_pack.plugins import PluginWorker, discover, read_plugin
from nyan_pack.settings import Paths


def manifest(identifier="custom", code="def poll(ctx): return None", timeout=2):
    return f'''schema = 1
id = "{identifier}"
[inputs.color]
type = "string"
default = "red"
[requires]
binaries = []
[output]
type = "image"
formats = ["png"]
[operation]
interval = 0.5
settle = 0
timeout = {timeout}
[python]
code = {json.dumps(code)}
'''


class ManifestTests(unittest.TestCase):
    def test_typed_inputs_and_unknown_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.toml"
            path.write_text(manifest())
            plugin = read_plugin(path)
            self.assertEqual(plugin.inputs({}), {"color": "red"})
            for supplied in [{"color": 123}, {"unknown": "value"}]:
                with self.assertRaises(ValueError):
                    plugin.inputs(supplied)

    def test_invalid_plugin_does_not_hide_other_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp), Path(tmp)/"cache", Path(tmp)/"runtime")
            directory = paths.config / "plugins"
            directory.mkdir()
            (directory / "broken.toml").write_text("this is not toml")
            (directory / "custom.toml").write_text(manifest())
            plugins, errors = discover(paths)
            self.assertIn("custom", plugins)
            self.assertIn("spotify", plugins)
            self.assertIn(str(directory / "broken.toml"), errors)

    def test_unsafe_polling_intervals_and_syntax_errors_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.toml"
            for content in [manifest().replace("interval = 0.5", "interval = nan"), manifest(code="def broken(:")]:
                path.write_text(content)
                with self.assertRaises((ValueError, SyntaxError)):
                    read_plugin(path)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_printing_plugin_and_cached_state_do_not_corrupt_ipc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.toml"
            code = '''from nyan_pack.plugin_api import Frame
counter = 0
def poll(ctx):
    global counter
    counter += 1
    print("plugin log")
    return Frame(str(counter), "/tmp/example.png", ctx.inputs["color"])
'''
            path.write_text(manifest(code=code))
            worker = PluginWorker(read_plugin(path), {}, Path(tmp), (96, 128))
            try:
                self.assertEqual((await worker.poll())["key"], "1")
                self.assertEqual((await worker.poll())["key"], "2")
            finally:
                await worker.close()

    async def test_hung_plugin_is_terminated_and_can_be_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.toml"
            path.write_text(manifest(code="import time\ndef poll(ctx): time.sleep(60)", timeout=0.2))
            worker = PluginWorker(read_plugin(path), {}, Path(tmp), (96, 128))
            with self.assertRaises(TimeoutError):
                await worker.poll()
            self.assertIsNone(worker.process)
            path.write_text(manifest())
            worker = PluginWorker(read_plugin(path), {}, Path(tmp), (96, 128))
            try:
                self.assertIsNone(await worker.poll())
            finally:
                await worker.close()
