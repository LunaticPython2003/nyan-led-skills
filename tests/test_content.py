from dataclasses import asdict
from io import BytesIO
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from nyan_pack.plugin_api import Context, Frame, Region, Scene
from nyan_pack.providers.audio import AudioVisualizer, RATE, WINDOW, spectrum
from nyan_pack.providers.cards import news, news_text, weather
from nyan_pack.rendering import ContentEncoder


class ContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.ctx = Context({}, self.directory, 96, 128)

    def test_context_animation_publishes_loop_and_per_frame_durations(self):
        result = self.ctx.animation([Image.new("RGB", (96, 128), color) for color in ("red", "blue")],
                                    "animation", duration=[120, 300], loop=3)
        with Image.open(result.source) as image:
            self.assertEqual(image.n_frames, 2)
            self.assertEqual(image.info["loop"], 3)
            image.seek(1)
            self.assertEqual(image.info["duration"], 300)

    def test_scene_caches_background_but_reads_changed_region(self):
        background = self.ctx.image(Image.new("RGB", (96, 104), "red"), "cover", name="background")
        bars = self.ctx.image(Image.new("RGB", (96, 24), "blue"), "one", name="bars")
        scene = Scene("one", background, [Region(1, 0, 104, 96, 24, bars)], background_size=(96, 104))
        encoder = ContentEncoder({"width":96,"height":128,"quality":"natural","fit":"cover"}, self.directory/"encoded", {}, {"max_age":5})
        first = encoder.encode(asdict(scene))
        Path(background.source).unlink()  # Region refresh must not re-fetch static content.
        scene.regions[0].frame = self.ctx.image(Image.new("RGB", (96, 24), "green"), "two", name="bars")
        second = encoder.encode(asdict(scene))
        with Image.open(BytesIO(first.data)) as one, Image.open(BytesIO(second.data)) as two:
            self.assertEqual(one.convert("RGB").getpixel((20,20)), two.convert("RGB").getpixel((20,20)))
            self.assertNotEqual(one.convert("RGB").getpixel((20,120)), two.convert("RGB").getpixel((20,120)))
        self.assertEqual(second.max_age, 5)

    def test_weather_card_without_media_metadata(self):
        self.ctx.inputs = {"latitude":52.52,"longitude":13.41,"label":"Weather","units":"celsius"}
        raw = json.dumps({"current":{"temperature_2m":23,"relative_humidity_2m":61,"weather_code":0,"time":"2026-10-17T12:00"}}).encode()
        with patch("nyan_pack.providers.cards.fetch", return_value=raw):
            first, second = weather(self.ctx), weather(self.ctx)
        self.assertEqual(first.key, second.key)
        with Image.open(first.source) as image:
            self.assertEqual(image.size, (96,128))

    def test_rss_and_atom_news_render_to_paged_animation(self):
        rss = b'<rss><channel><item><title>Headline &amp; detail</title><description>Some useful text for a display.</description></item></channel></rss>'
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Headline &amp; detail</title><summary>Some useful text for a display.</summary></entry></feed>'
        self.assertEqual(news_text(rss), news_text(atom))
        self.ctx.inputs = {"feed":"https://example.test/feed","label":"News","page_seconds":4}
        with patch("nyan_pack.providers.cards.fetch", return_value=rss):
            result = news(self.ctx)
        with Image.open(result.source) as image:
            self.assertEqual(image.size, (96,128))
            self.assertEqual(image.info["loop"], 0)

    def test_fft_reacts_to_real_frequency_content_and_silence(self):
        self.assertEqual(spectrum([0]*WINDOW), [0]*8)
        for frequency, expected in [(120,1),(500,3),(1800,5)]:
            samples = [int(16000*math.sin(2*math.pi*frequency*index/RATE)) for index in range(WINDOW)]
            bands = spectrum(samples)
            self.assertEqual(max(range(8), key=bands.__getitem__), expected)

    def test_visualizer_keeps_native_cover_detail_without_palette_or_grid_reduction(self):
        image = Image.new("RGB", (320, 320))
        image.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(320) for x in range(320)])
        output = BytesIO()
        image.save(output, format="PNG")
        self.ctx.inputs = {"player":"spotify","source":"@DEFAULT_MONITOR@","height":24,"color":"cyan","gain":1.0}
        capture = type("Capture", (), {"sample": lambda self: ([0]*WINDOW, time.monotonic())})()
        visualizer = AudioVisualizer()
        visualizer.capture = capture
        with patch("nyan_pack.providers.audio.album_art", return_value=Frame("cover","https://example.test/cover","Cover")), patch("nyan_pack.providers.audio.source_bytes", return_value=output.getvalue()):
            result = visualizer.poll(self.ctx)
        with Image.open(result.background.source) as background:
            self.assertEqual(background.size, (96,104))
            self.assertGreater(len(background.getcolors(100000)), 256)

