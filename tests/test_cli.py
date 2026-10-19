from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from nyan_pack.cli import main
from nyan_pack.devices.loy import UploadResult
from nyan_pack.settings import Paths, defaults, instance_lock, save_config


class CliTests(unittest.TestCase):
    def test_legacy_static_color_upload_works_with_watcher_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root/"config", root/"cache", root/"runtime")
            config = defaults()
            config["device"]["calibrated"] = True
            save_config(paths, config)
            driver = type("Fake", (), {"upload": AsyncMock(return_value=UploadResult(100, 1, 0)), "close": AsyncMock()})()
            with patch("nyan_pack.cli.Paths.current", return_value=paths), patch("nyan_pack.cli.request", side_effect=ConnectionError("stopped")), patch("nyan_pack.devices.create_driver", return_value=driver), patch("nyan_pack.cli.instance_lock", side_effect=lambda paths: instance_lock(paths, hardware=False)), patch("nyan_pack.cli.logging.basicConfig"), redirect_stdout(StringIO()):
                self.assertEqual(main(["show", "--color", "red"]), 0)
            with Image.open(BytesIO(driver.upload.call_args.args[0])) as image:
                self.assertEqual(image.size, (96, 128))
                self.assertEqual(image.convert("RGB").getpixel((48, 64)), (255, 0, 0))
            driver.close.assert_awaited_once()
