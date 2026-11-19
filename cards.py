from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path("C:/Windows/Fonts")


def username_value(username: str) -> int:
    vowels = sum(char in "aeiouy" for char in username)
    unique = len(set(username))
    repeats = len(username) - unique
    score = 61 + unique * 3 + min(vowels, 3) * 3 - repeats * 5
    if username[0] in "aeiou":
        score += 2
    return max(45, min(96, score))


@lru_cache(maxsize=96)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size=size)


@lru_cache(maxsize=1)
def _username_background() -> Image.Image:
    """Радиальный фон строим один раз для всех пользователей."""
    width, height = 1200, 675
    image = Image.new("RGB", (width, height), "#06152d")
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            glow = max(0, 1 - ((x - 820) ** 2 + (y - 120) ** 2) ** 0.5 / 850)
            pixels[x, y] = (
                int(5 + 12 * glow),
                int(19 + 37 * glow),
                int(47 + 93 * glow),
            )
    return image


def render_username_card(username: str) -> bytes:
    image = _username_background().copy()
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((760, -210, 1310, 340), outline=(30, 212, 255, 80), width=7)
    draw.ellipse((-170, 420, 260, 850), outline=(105, 74, 255, 55), width=5)
    draw.rounded_rectangle((70, 55, 1130, 620), radius=46, fill=(7, 23, 53, 205), outline=(39, 199, 255, 100), width=3)
    draw.text((120, 105), "PUBLIC USERNAME CHECK", font=_font("seguisb.ttf", 34), fill=(67, 215, 255, 255))
    draw.text((120, 225), f"@{username}", font=_font("segoeuib.ttf", 88), fill=(245, 250, 255, 255))

    value = username_value(username)
    draw.text((120, 390), "АЛГОРИТМИЧЕСКАЯ ЦЕННОСТЬ", font=_font("seguisb.ttf", 25), fill=(151, 169, 201, 255))
    draw.rounded_rectangle((120, 445, 900, 485), radius=20, fill=(26, 48, 83, 255))
    draw.rounded_rectangle((120, 445, 120 + int(780 * value / 100), 485), radius=20, fill=(37, 206, 255, 255))
    draw.text((940, 425), f"{value}/100", font=_font("segoeuib.ttf", 38), fill=(255, 255, 255, 255))
    draw.text((120, 535), "Публичная проверка t.me, регистрация не гарантируется", font=_font("segoeui.ttf", 24), fill=(109, 133, 173, 255))

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()




def render_valuation_card(result: "Valuation") -> bytes:
    """PNG 1200x675: оценка стоимости по Fragment + алгоритмическая ценность."""
    from valuation import Valuation

    if not isinstance(result, Valuation):
        raise TypeError("Expected Valuation")

    width, height = 1200, 675
    image = Image.new("RGB", (width, height), "#0b1221")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        shade = int(11 + 15 * (1 - y / height))
        draw.line((0, y, width, y), fill=(shade, shade + 7, shade + 20))

    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((840, -260, 1390, 290), outline=(70, 180, 230, 50), width=4)
    draw.ellipse((-240, 410, 310, 930), outline=(56, 112, 184, 45), width=4)
    draw.rounded_rectangle((55, 48, 1145, 625), radius=36,
                           fill=(16, 27, 47, 235), outline=(60, 89, 127, 125), width=2)

    small = _font("seguisb.ttf", 25)
    body = _font("segoeui.ttf", 23)
    bold = _font("seguisb.ttf", 31)
    number = _font("segoeuib.ttf", 70)
    draw.text((92, 80), "USERNAME / VALUE REPORT", font=small, fill=(80, 208, 241, 255))

    username = "@" + result.username
    name_size = 78
    while name_size > 30:
        name_font = _font("segoeuib.ttf", name_size)
        if draw.textbbox((0, 0), username, font=name_font)[2] <= 940:
            break
        name_size -= 3
    draw.text((92, 145), username, font=name_font, fill=(245, 250, 255, 255))

    draw.rounded_rectangle((89, 267, 760, 468), radius=23,
                           fill=(23, 39, 65, 255))
    draw.rounded_rectangle((781, 267, 1108, 468), radius=23,
                           fill=(23, 39, 65, 255))
    draw.text((116, 295), "ОРИЕНТИР ПО СДЕЛКАМ FRAGMENT", font=small, fill=(137, 162, 193, 255))
    if result.has_price:
        price_text = f"{result.low_ton:,}–{result.high_ton:,} TON".replace(",", " ")
        price_size = 57
        while price_size > 34:
            price_font = _font("segoeuib.ttf", price_size)
            if draw.textbbox((0, 0), price_text, font=price_font)[2] <= 618:
                break
            price_size -= 2
        draw.text((115, 355), price_text, font=price_font, fill=(250, 253, 255, 255))
    else:
        draw.text((116, 363), "НЕТ ДАННЫХ О ЦЕНЕ", font=bold, fill=(250, 253, 255, 255))

    draw.text((808, 295), "ЦЕННОСТЬ НИКА", font=small, fill=(137, 162, 193, 255))
    draw.text((808, 337), f"{result.score}/100", font=number, fill=(92, 220, 245, 255))
    draw.rounded_rectangle((809, 428, 1078, 443), radius=7, fill=(47, 68, 95, 255))
    draw.rounded_rectangle((809, 428, 809 + int(269 * result.score / 100), 443),
                           radius=7, fill=(57, 207, 238, 255))

    draw.text((92, 496), "ПОХОЖИЕ ПРОДАЖИ", font=small, fill=(137, 162, 193, 255))
    if result.sales:
        examples = "  |  ".join(
            f"@{sale.username}  {sale.price_ton:g} TON"
            for sale in result.sales[:3]
        )
        example_size = 25
        while example_size > 16:
            font = _font("seguisb.ttf", example_size)
            if draw.textbbox((0, 0), examples, font=font)[2] < 1040:
                break
            example_size -= 1
        draw.text((92, 542), examples, font=font, fill=(243, 248, 255, 255))
    else:
        draw.text((92, 542), "Сопоставимых завершённых продаж не найдено",
                  font=body, fill=(243, 248, 255, 255))

    timestamp = result.checked_at.strftime("%d.%m.%Y %H:%M UTC")
    draw.text((92, 594), f"Fragment · {timestamp} · оценка, не гарантия сделки",
              font=_font("segoeui.ttf", 19), fill=(142, 163, 190, 255))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
