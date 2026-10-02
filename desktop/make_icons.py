"""Zeichnet die App-Icons aus denselben Formen wie app/web/assets/logo.svg.

    python desktop/make_icons.py   (braucht Pillow, nur beim Aendern noetig)

Ergebnis liegt eingecheckt in desktop/icons/: icon.png (Fenster), icon.ico
(Windows), icon.icns (macOS). Fuer macOS mit Rand, wie es Apple vorsieht.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"
S = 64  # Koordinaten aus dem SVG


def draw(size: int, margin: float) -> Image.Image:
    scale = 4
    big = size * scale
    inner = big * (1 - 2 * margin)
    off = big * margin
    k = inner / S

    def p(x: float, y: float) -> tuple[float, float]:
        return off + x * k, off + y * k

    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([p(0, 0), p(64, 64)], radius=14 * k, fill="#121b27")
    board = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(board)
    for x, y in [(10, 10), (32, 10), (21, 21), (43, 21), (10, 32), (32, 32), (21, 43), (43, 43)]:
        bd.rectangle([p(x, y), p(x + 11, y + 11)], fill=(255, 255, 255, 15))
    img = Image.alpha_composite(img, board)
    d = ImageDraw.Draw(img)
    w = 6 * k
    d.line([p(19, 48), p(19, 26), p(40, 26)], fill="#8098ff", width=round(w), joint="curve")
    for x, y, r in [(19, 26, 3), (40, 26, 3), (19, 48, 4.5)]:
        cx, cy = p(x, y)
        d.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill="#8098ff")
    for r, color in [(8, "#0a1018"), (6, "#ff7a4d")]:
        cx, cy = p(42, 26)
        d.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill=color)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    draw(256, 0.0).save(OUT / "icon.png")
    flat = draw(256, 0.0)
    flat.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw(1024, 0.1).save(OUT / "icon.icns")


if __name__ == "__main__":
    main()
