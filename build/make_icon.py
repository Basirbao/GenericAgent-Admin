"""Generate build/icon.ico — multi-resolution Windows app icon.

Run once, commit the .ico into the repo. Re-run only if you want to
change the design.

  python build/make_icon.py

Design mirrors launch_webui.pyw:_try_start_windows_tray so the system
tray icon and the desktop / taskbar icon stay visually consistent:
blue rounded square (#5B9BFF), white "GA" text centred. Output is a
multi-resolution .ico (16/32/48/64/128/256) — Windows picks the
right size from the same file depending on context (taskbar = 16/32,
explorer = 48, jump-list large = 256).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "icon.ico"
SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

# Try common bold-sans fonts. PIL's load_default() is a pixel bitmap
# font and looks awful upscaled to 256px, so we'd rather fall back to
# whatever the host has.
FONT_CANDIDATES = [
    "/System/Library/Fonts/SFCompactRounded-Bold.otf",  # macOS Big Sur+
    "/System/Library/Fonts/Helvetica.ttc",              # macOS classic
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arial.ttf",                                         # PIL search-path fallback
]


def _pick_font_path() -> str | None:
    for p in FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return "arial.ttf"  # let PIL try its own search path


def render(size: int, font_path: str | None) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = max(2, size // 5)
    d.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=radius, fill=(91, 155, 255, 255)
    )

    fsize = int(size * 0.55)
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype(font_path, fsize) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    bbox = d.textbbox((0, 0), "GA", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1]),
        "GA",
        font=font,
        fill=(255, 255, 255, 255),
    )
    return img


def main() -> None:
    font_path = _pick_font_path()
    print(f"using font: {font_path}")
    base = render(256, font_path)
    # PIL's ICO writer auto-generates the requested sub-resolutions by
    # downscaling the largest provided image. That's fine for our flat
    # design (no fine details that need pixel-grid alignment at 16px).
    base.save(OUT, format="ICO", sizes=SIZES)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(SIZES)} resolutions)")


if __name__ == "__main__":
    main()
