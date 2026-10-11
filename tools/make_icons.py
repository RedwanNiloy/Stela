"""Builds the PNG and ICO icons from static/favicon.svg.

Rasterising is done by headless Edge/Chrome (the SVG is drawn exactly as a browser draws it), then Pillow
resizes and packs the files. cairosvg is not used because it needs the native Cairo library, which is not
available everywhere (it is missing on a default Windows install).

    pip install pillow
    python tools/make_icons.py [path-to-chrome-or-edge]

The tile with rounded corners is used for the browser tab icons; the apple-touch icon and the manifest icons
are full-bleed squares (the phone adds its own rounding, and iOS paints transparent corners black).
"""
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parent.parent / "static"
SVG = (STATIC / "favicon.svg").read_text(encoding="utf-8")
MASTER = 1024

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]


def find_browser(arg):
    for c in ([arg] if arg else []) + BROWSERS:
        if Path(c).exists() or shutil.which(c):
            return c
    sys.exit("No Chrome/Edge found. Pass its path as the first argument.")


def render(browser, svg_text, tmp):
    """SVG text -> transparent MASTER x MASTER RGBA image."""
    page = tmp / "page.html"
    page.write_text(
        f"<!doctype html><meta charset=utf-8><style>html,body{{margin:0;background:transparent}}"
        f"svg{{display:block;width:{MASTER}px;height:{MASTER}px}}</style>{svg_text}", encoding="utf-8")
    out = tmp / "shot.png"
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                    f"--window-size={MASTER},{MASTER}", f"--screenshot={out}", page.as_uri()],
                   check=True, capture_output=True, timeout=60)
    return Image.open(io.BytesIO(out.read_bytes())).convert("RGBA").crop((0, 0, MASTER, MASTER))


def save(img, size, name):
    img.resize((size, size), Image.LANCZOS).save(STATIC / name, optimize=True)
    print("wrote", name, f"{size}x{size}")


def main():
    browser = find_browser(sys.argv[1] if len(sys.argv) > 1 else None)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tile = render(browser, SVG, tmp)
        bleed = render(browser, SVG.replace('rx="14"', 'rx="0"'), tmp)
        save(tile, 32, "favicon-32.png")
        save(tile, 48, "favicon-48.png")
        save(bleed, 180, "apple-touch-icon.png")
        save(bleed, 192, "icon-192.png")
        save(bleed, 512, "icon-512.png")
        # favicon.ico with 16, 32 and 48 px images, so /favicon.ico never 404s
        tile.resize((48, 48), Image.LANCZOS).save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        print("wrote favicon.ico 16/32/48")


if __name__ == "__main__":
    main()
