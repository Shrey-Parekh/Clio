"""Builds Clio's icon set from his picture.

Big sizes (desktop, Start menu, installer) are the picture itself, cropped to
its rounded tile with the corners made transparent. Small sizes (tray, taskbar)
are a simplified mark in the same style - the ring and the reticle - because
at 16 to 48 pixels the word CLIO and the fine arcs turn to mush. His choice,
2026-10-01.

Run from the repo root:  .venv/Scripts/python.exe frontend/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ICONS = Path(__file__).resolve().parent / "src-tauri" / "icons"
SOURCE = ICONS / "source.webp"

# Where the tile sits in the 1254x1254 source, measured from the picture.
TILE = (136, 117, 136 + 980, 117 + 980)
TILE_RADIUS = 0.205        # of the tile's width, matching the picture's corners

MINT = (155, 224, 202)
WHITE = (227, 238, 243)
GREY = (96, 108, 116)
DARK = (17, 21, 25)
EDGE = (52, 62, 70)

SMALL = (16, 24, 32, 48)   # the simple mark
LARGE = (64, 128, 256)     # the picture


def rounded_mask(size: int, radius: float) -> Image.Image:
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1), radius=int(size * scale * radius), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def tile(size: int) -> Image.Image:
    """His picture, cropped to the tile, corners transparent."""
    picture = Image.open(SOURCE).convert("RGB").crop(TILE).resize((size, size), Image.LANCZOS)
    out = picture.convert("RGBA")
    out.putalpha(rounded_mask(size, TILE_RADIUS))
    return out


def mark(size: int) -> Image.Image:
    """The same idea with nothing that can smear: tile, ring, reticle."""
    s = 1024                                   # drawn large, then reduced
    art = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(art)
    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=DARK + (255,),
                           outline=EDGE + (255,), width=int(s * 0.03))

    def arc(radius, width, start, end, colour):
        box = (s / 2 - s * radius, s / 2 - s * radius, s / 2 + s * radius, s / 2 + s * radius)
        draw.arc(box, start=start, end=end, fill=colour + (255,), width=int(s * width))

    # The picture's sweep: mint up the left, grey answering on the right.
    arc(0.37, 0.085, 115, 300, MINT)
    arc(0.37, 0.085, 325, 80, GREY)
    # The reticle from the wordmark's O: a white half, a mint half, a dark eye.
    core = (s * 0.5 - s * 0.17, s * 0.5 - s * 0.17, s * 0.5 + s * 0.17, s * 0.5 + s * 0.17)
    draw.pieslice(core, start=90, end=270, fill=WHITE + (255,))
    draw.pieslice(core, start=270, end=90, fill=MINT + (255,))
    eye = s * 0.07
    draw.ellipse((s / 2 - eye, s / 2 - eye, s / 2 + eye, s / 2 + eye), fill=DARK + (255,))
    return art.resize((size, size), Image.LANCZOS)


def main() -> None:
    by_size = {n: mark(n) for n in SMALL} | {n: tile(n) for n in LARGE}
    by_size[32].save(ICONS / "32x32.png")
    tile(128).save(ICONS / "128x128.png")
    tile(256).save(ICONS / "128x128@2x.png")
    tile(512).save(ICONS / "512x512.png")
    tile(512).save(ICONS / "icon.png")
    # Windows picks the nearest size from the .ico, so each one is its own image.
    sizes = sorted(by_size)
    by_size[256].save(ICONS / "icon.ico", format="ICO", sizes=[(n, n) for n in sizes],
                      append_images=[by_size[n] for n in sizes if n != 256])
    print("wrote", ", ".join(p.name for p in sorted(ICONS.glob("*")) if p.suffix in (".png", ".ico")))


if __name__ == "__main__":
    main()
