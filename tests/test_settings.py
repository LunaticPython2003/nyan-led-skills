import json
from pathlib import Path
import tempfile
import unittest

from nyan_pack.devices import device_settings
from nyan_pack.service import unit_text
from nyan_pack.settings import Paths, defaults, instance_lock, load_config, save_config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = Paths(root/"config"/"nyan-pack", root/"cache", root/"runtime")

    def test_migrates_confirmed_device_calibration_and_detail_preset(self):
        old = self.paths.config.parent / "nyan-spotify"
        old.mkdir(parents=True)
        (old/"display_size").write_text("96x128")
        (old/"artwork_quality").write_text("detail")
        config = load_config(self.paths)
        model, settings = device_settings(self.paths, config)
        self.assertEqual((settings["width"], settings["height"]), (96, 128))
        self.assertTrue(settings["calibrated"])
        self.assertTrue(settings["staged_upload"])
        self.assertEqual(settings["address"], "E0:6E:41:FA:FE:69")

    def test_invalid_retry_and_inputs_fail_before_starting_watcher(self):
        for field, value in [("retry", -1), ("retry", "twenty"), ("plugin_inputs", {"clock": []})]:
            config = defaults() | {field: value}
            save_config(self.paths, config)
            with self.assertRaises(ValueError):
                load_config(self.paths)

    def test_only_one_watcher_owns_runtime(self):
        with instance_lock(self.paths, hardware=False):
            with self.assertRaisesRegex(RuntimeError, "already owns"):
                with instance_lock(self.paths, hardware=False):
                    self.fail("second lock acquired")

    def test_service_retains_virtualenv_path_and_escapes_systemd_specifiers(self):
        text = unit_text(Path('/tmp/venv % demo/bin/python'))
        self.assertIn('ExecStart="/tmp/venv %% demo/bin/python" -u -m nyan_pack watch', text)
        self.assertIn("Restart=on-failure", text)
