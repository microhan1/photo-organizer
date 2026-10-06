"""Draw assets/icon.png and assets/icon.ico: a one-colour folder with a photo (mountains and
a sun) cut out of it. Drawn at 4x and scaled down for smooth edges.

    python assets/make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
BLUE = (37, 99, 235, 255)  # theme accent #2563EB
CLEAR = (0, 0, 0, 0)
SIZE = 256
K = 4  # supersampling


def draw(size: int = SIZE) -> Image.Image:
    s = size * K
    im = Image.new("RGBA", (s, s), CLEAR)
    d = ImageDraw.Draw(im)
    u = s / 32  # 32-unit grid
    # folder: tab, then body
    d.rounded_rectangle((2 * u, 5 * u, 14 * u, 11 * u), radius=1.5 * u, fill=BLUE)
    d.rounded_rectangle((2 * u, 8 * u, 30 * u, 27 * u), radius=2.5 * u, fill=BLUE)
    # the photo: a frame cut out of the body, with mountains and a sun left in blue
    d.rounded_rectangle((7 * u, 12 * u, 25 * u, 24 * u), radius=1.2 * u, fill=CLEAR)
    d.polygon([(8.5 * u, 22.5 * u), (13.5 * u, 16 * u), (16.5 * u, 19.5 * u), (19 * u, 17 * u),
               (23.5 * u, 22.5 * u)], fill=BLUE)
    d.ellipse((19 * u, 13.5 * u, 22 * u, 16.5 * u), fill=BLUE)
    return im.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    big = draw(SIZE)
    big.save(os.path.join(HERE, "icon.png"))
    big.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
                                                     (256, 256)])
    print("icon.png, icon.ico")


if __name__ == "__main__":
    main()
