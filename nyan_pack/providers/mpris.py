"""Album art from any player exposed through Linux MPRIS/playerctl."""

import subprocess

from ..plugin_api import Frame


def album_art(player="auto"):
    selector = ["--all-players"] if player == "auto" else ["--player", player]
    result = subprocess.run(
        ["playerctl", *selector, "metadata", "--format", "{{playerName}}\t{{status}}\t{{mpris:artUrl}}\t{{xesam:title}}"],
        capture_output=True, text=True, timeout=5,
    )
    records = [line.split("\t", 3) for line in result.stdout.splitlines()] if result.returncode == 0 else []
    records = [record for record in records if len(record) == 4]
    records.sort(key=lambda record: record[1] != "Playing")
    for name, status, url, title in records:
        if url.startswith(("https://", "http://", "file://")):
            return Frame(f"{name}:{url}", url, title or name)
    return None
