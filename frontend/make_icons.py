"""Builds Clio's icon set from his picture.

Big sizes (desktop, Start menu, installer) are the picture itself, cropped to
its rounded tile with the corners made transparent. Small sizes (tray, taskbar)
are a simplified mark in the same style - the ring and the reticle - because
at 16 to 48 pixels the word CLIO and the fine arcs turn to mush. His choice,
2026-10-01.

Run from the repo root:  .venv/Scripts/python.exe frontend/make_icons.py
"""

import io
import struct
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


def write_ico(path: Path, by_size: dict[int, Image.Image]) -> None:
    """An .ico with the 32 px image first, written by hand so the order holds.

    Found live, 2026-10-01: the icon looked pixelated. Windows picks the best
    size from an .ico whatever the order, but Tauri does not - for the window,
    taskbar and tray it takes the *first* image in the file (tauri-codegen,
    image.rs: `icon_dir.entries()[0]`). Pillow writes smallest first, so Tauri
    was handed the 16 px image and Windows stretched it to twice its size.
    32 px is what the taskbar asks for at 100% scaling, and what Tauri's own
    icon tool puts first.
    """
    order = [32] + [n for n in sorted(by_size) if n != 32]
    blobs = []
    for n in order:
        buffer = io.BytesIO()
        by_size[n].save(buffer, format="PNG")
        blobs.append(buffer.getvalue())
    header = struct.pack("<HHH", 0, 1, len(order))
    offset = len(header) + 16 * len(order)
    entries = b""
    for n, blob in zip(order, blobs):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(header + entries + b"".join(blobs))
    first = struct.unpack("<B", path.read_bytes()[6:7])[0]
    assert first == 32, f"Tauri takes the first image; it must be 32 px, not {first}"


def main() -> None:
    by_size = {n: mark(n) for n in SMALL} | {n: tile(n) for n in LARGE}
    by_size[32].save(ICONS / "32x32.png")
    tile(128).save(ICONS / "128x128.png")
    tile(256).save(ICONS / "128x128@2x.png")
    tile(512).save(ICONS / "512x512.png")
    tile(512).save(ICONS / "icon.png")
    write_ico(ICONS / "icon.ico", by_size)
    # Found live: the next build kept the old icon inside clio.exe. Cargo only
    # re-runs the step that embeds it when build.rs changes, not when the .ico
    # does - so new icons on disk, the placeholder still in the app.
    (ICONS.parent / "build.rs").touch()
    print("wrote", ", ".join(p.name for p in sorted(ICONS.glob("*")) if p.suffix in (".png", ".ico")))


if __name__ == "__main__":
    main()
