"""Иконка приложения: буква «B» на градиентной плитке в стиле Windows 11.

    python tools/make_icon.py

Пишет studio/assets/icon.ico (16…256 px) и icon.png (512 px). Иконку берут окно
программы, лаунчер BegoFrame.exe и установщик — перерисовывать нужно только при смене дизайна.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "studio" / "assets"
FONT = ASSETS / "fonts" / "Inter-Bold.ttf"

S = 1024                       # рисуем крупно и уменьшаем — так края чище на любом размере
TOP, BOTTOM = (139, 140, 255), (79, 70, 229)   # те же цвета, что у значка в окне программы


def _gradient(size: int) -> Image.Image:
    """Диагональный градиент от светлого индиго к насыщенному."""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
    return grad


def render(size: int = S) -> Image.Image:
    small = 64
    grad = _gradient(small).resize((size, size), Image.BICUBIC)

    radius = int(size * 0.23)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)

    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)

    # мягкий блик сверху
    gloss = Image.new("L", (size, size), 0)
    ImageDraw.Draw(gloss).ellipse((-size * 0.3, -size * 0.75, size * 1.3, size * 0.45), fill=38)
    gloss = ImageChops.multiply(gloss.filter(ImageFilter.GaussianBlur(size * 0.04)), mask)
    tile = Image.alpha_composite(tile, Image.merge("RGBA", (*[Image.new("L", (size, size), 255)] * 3, gloss)))

    # буква B
    font = ImageFont.truetype(str(FONT), int(size * 0.66))
    glyph = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(glyph)
    l, t, r, b = d.textbbox((0, 0), "B", font=font)
    x = (size - (r - l)) / 2 - l + size * 0.01
    y = (size - (b - t)) / 2 - t
    d.text((x, y), "B", font=font, fill=255)

    shadow = glyph.filter(ImageFilter.GaussianBlur(size * 0.025))
    shadow = shadow.point(lambda v: int(v * 0.35))
    shadow_layer = Image.new("RGBA", (size, size), (30, 20, 90, 0))
    shadow_layer.putalpha(ImageChops.offset(shadow, 0, int(size * 0.02)))
    tile = Image.alpha_composite(tile, shadow_layer)

    letter = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    letter.putalpha(glyph)
    return Image.alpha_composite(tile, letter)


def main() -> None:
    big = render()
    ASSETS.mkdir(parents=True, exist_ok=True)
    big.resize((512, 512), Image.LANCZOS).save(ASSETS / "icon.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [big.resize((n, n), Image.LANCZOS) for n in sizes]
    frames[-1].save(ASSETS / "icon.ico", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    print("icon:", ASSETS / "icon.ico")


if __name__ == "__main__":
    main()
