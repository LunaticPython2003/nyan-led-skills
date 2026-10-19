"""Production calibration: light every pixel and identify all four panel edges."""

from PIL import Image, ImageDraw


def bounds(dimensions):
    width, height = dimensions
    image = Image.new("RGB", dimensions, (24, 24, 24))
    draw = ImageDraw.Draw(image)
    for x in range(8, width - 1, 8):
        draw.line((x, 1, x, height - 2), fill=(80, 80, 80))
    for y in range(8, height - 1, 8):
        draw.line((1, y, width - 2, y), fill=(80, 80, 80))
    draw.line((0, 0, width - 1, 0), fill="green")
    draw.line((0, 0, 0, height - 1), fill="blue")
    draw.line((width - 1, 0, width - 1, height - 1), fill="yellow")
    draw.line((0, height - 1, width - 1, height - 1), fill="red")
    return image
