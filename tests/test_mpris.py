import subprocess
import unittest
from unittest.mock import patch

from nyan_pack.providers.mpris import album_art


class MprisTests(unittest.TestCase):
    @patch("nyan_pack.providers.mpris.subprocess.run")
    def test_auto_prefers_playing_player_with_artwork_in_one_query(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "spotify\tPaused\thttps://cover/one\tOne\nvlc\tPlaying\tfile:///tmp/two.png\tTwo\n")
        frame = album_art("auto")
        self.assertEqual(frame.source, "file:///tmp/two.png")
        self.assertEqual(frame.label, "Two")
        self.assertIn("--all-players", run.call_args.args[0])
        run.assert_called_once()

    @patch("nyan_pack.providers.mpris.subprocess.run")
    def test_explicit_player_and_missing_artwork(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "firefox\tPlaying\t\tVideo\n")
        self.assertIsNone(album_art("firefox"))
        self.assertEqual(run.call_args.args[0][1:3], ["--player", "firefox"])

    @patch("nyan_pack.providers.mpris.subprocess.run")
    def test_missing_player_returns_no_frame(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "No players found")
        self.assertIsNone(album_art("spotify"))
