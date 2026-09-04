"""Build tool: turn image.png into autoRDP.ico with a transparent background.

Run by build.ps1 whenever the icon source is newer than the .ico. Not part of
the application -- the app never imports this.

The source art is a cream circle on a white square, with two orange dots that
overhang the circle. A plain circular mask would clip those dots, so the white
is keyed out by colour instead: pixels close to pure white become transparent,
with a ramp across the antialiased edge so the circle does not end up with a
hard, jagged rim.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# Distance from pure white, in RGB space. Below FULLY_CLEAR a pixel is treated
# as background; above KEEP it is artwork; between the two it fades.
FULLY_CLEAR = 8.0
KEEP = 34.0

# Windows picks whichever size it needs from the .ico, so ship the usual set:
# 16/32 for the taskbar and title bar, 256 for large icons in Explorer.
SIZES = [16, 24, 32, 48, 64, 128, 256]


def key_out_white(image: Image.Image) -> Image.Image:
    """Replace the white surround with transparency."""
    image = image.convert("RGBA")
    pixels = image.load()
    width, height = image.size

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue
            # How far this pixel is from white, and how neutral it is. The
            # saturation check protects the pale cream, which is close to white
            # in brightness but not in colour.
            gap = ((255 - r) ** 2 + (255 - g) ** 2 + (255 - b) ** 2) ** 0.5
            saturation = max(r, g, b) - min(r, g, b)
            if saturation > 12:
                continue                      # coloured artwork, keep it
            if gap <= FULLY_CLEAR:
                pixels[x, y] = (r, g, b, 0)
            elif gap < KEEP:
                ramp = (gap - FULLY_CLEAR) / (KEEP - FULLY_CLEAR)
                pixels[x, y] = (r, g, b, int(a * ramp))
    return image


def trim_and_square(image: Image.Image, margin: int = 2) -> Image.Image:
    """Crop transparent margins, then pad back to a square.

    Trimming makes the artwork fill more of the icon, which matters at 16x16.
    """
    box = image.getbbox()
    if box:
        image = image.crop(box)
    side = max(image.size) + margin * 2
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return canvas


def build(source: Path, target: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Icon source not found: {source}")

    art = trim_and_square(key_out_white(Image.open(source)))
    art.save(target, format="ICO",
             sizes=[(s, s) for s in SIZES if s <= max(art.size)])

    # A PNG next to it is handy for a README or a shortcut.
    art.resize((256, 256), Image.LANCZOS).save(
        target.with_name(target.stem + "-256.png"))

    corner = art.getpixel((0, 0))
    print(f"{target.name}: {', '.join(str(s) for s in SIZES)} px")
    print(f"  source        : {source.name} ({Image.open(source).size[0]}px)")
    print(f"  corner pixel  : alpha={corner[3]} (0 means the white is gone)")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    assets = root / "assets"
    build(assets / (sys.argv[1] if len(sys.argv) > 1 else "icon-source.png"),
          assets / "autoRDP.ico")
