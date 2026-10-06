"""docs/before_after.png: the samples folder before and after a real run (HEIC -> JPG on).

    python docs/make_before_after.py

The trees are listed from the disk before and after running main.py on a copy;
nothing is drawn by hand.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "before_after.png")
FONT = r"C:\Windows\Fonts\malgun.ttf"
FONT_BOLD = r"C:\Windows\Fonts\malgunbd.ttf"
BG, TEXT, MUTED, ACCENT = (250, 250, 250), (31, 35, 40), (107, 114, 128), (37, 99, 235)
SCALE = 2


def tree(root: str) -> list[tuple[int, str, bool]]:
    """(depth, name, is_folder), folders first, the log left out."""
    out = []

    def walk(d: str, depth: int) -> None:
        names = sorted(os.listdir(d), key=lambda n: (not os.path.isdir(os.path.join(d, n)), n.lower()))
        for n in names:
            if n.startswith("organize_log.json"):
                continue
            p = os.path.join(d, n)
            out.append((depth, n, os.path.isdir(p)))
            if os.path.isdir(p):
                walk(p, depth + 1)

    walk(root, 0)
    return out


def main() -> None:
    work = tempfile.mkdtemp()
    try:
        folder = os.path.join(work, "Photos")
        os.makedirs(folder)
        for n in os.listdir(os.path.join(ROOT, "samples")):
            if n != "make_samples.py" and not n.endswith(".AAE"):
                shutil.copy2(os.path.join(ROOT, "samples", n), os.path.join(folder, n))
        before = tree(folder)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PHOTO_ORGANIZER_SETTINGS=os.path.join(work, "s.json"))
        subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), folder, "--convert-heic", "--lang", "ko"],
                       check=True, env=env, capture_output=True, stdin=subprocess.DEVNULL)
        after = tree(folder)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    draw(before, after)


def draw(before, after) -> None:
    s = SCALE
    font = ImageFont.truetype(FONT, 15 * s)
    bold = ImageFont.truetype(FONT_BOLD, 17 * s)
    line, col, pad = 24 * s, 430 * s, 24 * s
    rows = max(len(before), len(after))
    im = Image.new("RGB", (pad * 2 + col * 2, pad * 2 + 40 * s + rows * line), BG)
    d = ImageDraw.Draw(im)
    for x, title, items in ((pad, "Before", before), (pad + col, "After", after)):
        d.text((x, pad), "Photos/  —  " + title, font=bold, fill=ACCENT if title == "After" else TEXT)
        for i, (depth, name, is_dir) in enumerate(items):
            y = pad + 40 * s + i * line
            d.text((x + depth * 22 * s, y), name + ("/" if is_dir else ""), font=font, fill=TEXT if is_dir else MUTED)
    im = im.resize((im.width // s, im.height // s), Image.Resampling.LANCZOS)
    im.save(OUT)
    print(OUT, im.size)


if __name__ == "__main__":
    main()
