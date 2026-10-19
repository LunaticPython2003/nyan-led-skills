"""Small display cards for arbitrary timed data sources; no media dependencies."""

import hashlib
import html
import json
import math
import re
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont


def fetch(url):
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Data source must be an HTTP(S) URL")
    with urlopen(Request(url, headers={"User-Agent": "nyan-pack/0.2"}), timeout=8) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Data source exceeds 1 MiB")
    return data


def font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def text_lines(text, draw, face, width):
    """Wrap by rendered width, including words longer than the display."""
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=face) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for character in word:
            if line and draw.textlength(line + character, font=face) > width:
                lines.append(line)
                line = ""
            line += character
    if line:
        lines.append(line)
    return lines


def weather(ctx):
    latitude, longitude = ctx.inputs["latitude"], ctx.inputs["longitude"]
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Latitude/longitude out of range")
    query = urlencode({"latitude": latitude, "longitude": longitude,
                       "current": "temperature_2m,relative_humidity_2m,weather_code", "timezone": "auto",
                       "temperature_unit": ctx.inputs["units"]})
    data = json.loads(fetch("https://api.open-meteo.com/v1/forecast?" + query))
    current = data["current"]
    temperature = float(current["temperature_2m"])
    if not math.isfinite(temperature):
        raise ValueError("Weather temperature is missing")
    code = current["weather_code"]
    condition = ("Clear" if code == 0 else "Cloudy" if code in (1, 2, 3) else
                 "Fog" if code in (45, 48) else "Snow" if code in (71, 73, 75, 77, 85, 86) else
                 "Thunder" if code >= 95 else "Rain")
    image = Image.new("RGB", (ctx.width, ctx.height), "black")
    draw = ImageDraw.Draw(image)
    draw.text((4, 4), ctx.inputs["label"][:18], fill="cyan", font=font(11))
    draw.text((4, 25), f"{temperature:.0f}°", fill="white", font=font(max(15, ctx.width // 3)))
    draw.text((4, 68), condition, fill="yellow", font=font(12))
    draw.text((4, 88), f"RH {current['relative_humidity_2m']}%", fill="white", font=font(11))
    draw.text((4, ctx.height - 12), "Open-Meteo", fill="#808080", font=font(9))
    key = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
    return ctx.image(image, key, f"{ctx.inputs['label']}: {temperature:.0f}° {condition}")


def news_text(raw):
    root = ET.fromstring(raw)
    entries = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in ("item", "entry")]
    if not entries:
        raise ValueError("Feed contains no RSS items or Atom entries")
    fields = {}
    for element in entries[0]:
        fields.setdefault(element.tag.rsplit("}", 1)[-1], " ".join(element.itertext()))
    title = fields.get("title", "News")
    summary = fields.get("description") or fields.get("summary") or fields.get("content", "")
    clean = lambda text: " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    return clean(title)[:200], clean(summary)[:700]


def news(ctx):
    title, summary = news_text(fetch(ctx.inputs["feed"]))
    text = title + (" — " + summary if summary else "")
    face = font(11)
    dummy = ImageDraw.Draw(Image.new("RGB", (ctx.width, ctx.height)))
    lines = text_lines(text, dummy, face, ctx.width - 8)
    per_page = max(1, (ctx.height - 36) // 14)
    pages = [lines[index:index + per_page] for index in range(0, min(len(lines), per_page * 4), per_page)]
    frames = []
    for index, page in enumerate(pages):
        image = Image.new("RGB", (ctx.width, ctx.height), "black")
        draw = ImageDraw.Draw(image)
        draw.text((4, 3), ctx.inputs["label"][:16], fill="cyan", font=font(11))
        for row, line in enumerate(page):
            draw.text((4, 22 + row * 14), line, fill="white", font=face)
        draw.text((4, ctx.height - 12), f"{index + 1}/{len(pages)}", fill="#808080", font=font(9))
        frames.append(image)
    return ctx.animation(frames, hashlib.sha256(text.encode()).hexdigest(), title,
                         duration=ctx.inputs["page_seconds"] * 1000)
