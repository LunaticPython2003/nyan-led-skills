from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from nyan_pack import rendering

SETTINGS = {"width": 96, "height": 128, "fit": "cover", "quality": "natural"}


class RenderingTests(unittest.TestCase):
    def test_rectangle_and_exact_encoded_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.png"
            Image.new("RGB", (640, 640), "red").save(path)
            for quality in ("natural", "detail"):
                encoded = rendering.encode({"source": str(path)}, SETTINGS | {"quality": quality}, Path(tmp)/"output")
                self.assertEqual((Path(tmp)/"output/output.gif").read_bytes(), encoded.data)
                with Image.open(BytesIO(encoded.data)) as image:
                    self.assertEqual(image.size, (96, 128))

    def test_animation_frames_timing_and_loop_survive_resizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.gif"
            red = Image.new("RGB", (32, 32), "red")
            blue = Image.new("RGB", (32, 32), "blue")
            red.save(path, save_all=True, append_images=[blue], duration=[120, 240], loop=2)
            encoded = rendering.encode({"source": str(path)}, SETTINGS, Path(tmp)/"output")
            with Image.open(BytesIO(encoded.data)) as image:
                self.assertEqual(image.n_frames, 2)
                self.assertEqual(image.size, (96, 128))
                self.assertEqual(image.info["loop"], 2)
                self.assertEqual(image.info["duration"], 120)
                image.seek(1)
                self.assertEqual(image.info["duration"], 240)
                self.assertEqual(image.convert("RGB").getpixel((10, 10)), (0, 0, 255))

    def test_native_natural_gif_is_not_reencoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.gif"
            Image.new("RGB", (96, 128), "blue").save(path)
            self.assertEqual(rendering.encode({"source": str(path)}, SETTINGS, Path(tmp)/"out").data, path.read_bytes())

    def test_oversized_gif_or_source_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.png"
            Image.new("RGB", (640, 640), "red").save(path)
            with patch.object(rendering, "MAX_GIF_BYTES", 1):
                with self.assertRaises(ValueError):
                    rendering.encode({"source": str(path)}, SETTINGS, Path(tmp)/"out")
            self.assertFalse((Path(tmp)/"out/output.gif").exists())
            with patch.object(rendering, "MAX_SOURCE_BYTES", 1):
                with self.assertRaises(ValueError):
                    rendering.source_bytes(str(path))
