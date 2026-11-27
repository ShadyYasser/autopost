"""The verse card drawn over the blurred reel while a Bible verse is read (see `verses.py` for how it is found).

A cream card with a gold border, the verse in navy (Amiri, the classic Naskh face, with the translation's own
diacritics) and its reference in gold underneath - the colours of the Kalima Sawaa frame.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .verses import FONT_BOLD_SHA256, FONT_BOLD_URL, FONT_SHA256, FONT_URL, VerseCard, fetch_pinned

log = logging.getLogger(__name__)

_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
TRANSLATION = "ترجمة فاندايك"


@dataclass(frozen=True)
class CardStyle:
    bg: str = "#fbf6ed"
    navy: str = "#092e48"
    gold: str = "#a47a3c"
    width_share: float = 0.88        # card width / window width
    max_height_share: float = 0.78   # the card never covers more than this of the window's height
    radius: int = 34
    border: int = 5
    padding: int = 46
    max_font: int = 54
    min_font: int = 30
    line_spacing: float = 1.55


def fonts() -> tuple[Path, Path]:
    return (fetch_pinned(FONT_URL, FONT_SHA256, "Amiri-Regular.ttf"),
            fetch_pinned(FONT_BOLD_URL, FONT_BOLD_SHA256, "Amiri-Bold.ttf"))


def _shaper():
    """(prepare(text), draw kwargs): libraqm shapes Arabic with its diacritics; without it the diacritics are
    dropped and the letters joined by arabic_reshaper + python-bidi (the words stay exactly the same)."""
    from PIL import features

    if features.check("raqm"):
        return (lambda t: t), {"direction": "rtl", "language": "ar"}, None
    import arabic_reshaper
    from bidi.algorithm import get_display
    from PIL import ImageFont

    def prep(t: str) -> str:
        return get_display(arabic_reshaper.reshape(_DIACRITICS.sub("", t.replace("ٱ", "ا"))))
    return prep, {}, ImageFont.Layout.BASIC


def _wrap(words: list[str], font, width: int, measure) -> list[str]:
    lines, cur = [], []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and measure(trial, font) > width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def render_card(card: VerseCard, window: tuple[int, int], style: CardStyle | None = None,
                font_paths: tuple[Path, Path] | None = None):
    """RGBA image the size of the video window: the card centred on a transparent background."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    style = style or CardStyle()
    regular, bold = font_paths or fonts()
    prep, kw, layout = _shaper()
    W, H = window
    cw = int(W * style.width_share)
    inner = cw - 2 * style.padding
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def font(path: Path, size: int):
        return ImageFont.truetype(str(path), size, layout_engine=layout) if layout is not None else \
            ImageFont.truetype(str(path), size)

    def measure(text: str, f) -> int:
        b = probe.textbbox((0, 0), prep(text), font=f, **kw)
        return b[2] - b[0]

    words = card.text.split()
    ref_text = f"{card.reference} — {TRANSLATION}"
    size = style.max_font
    while True:
        f = font(regular, size)
        lines = _wrap(words, f, inner, measure)
        line_h = int(size * style.line_spacing)
        ref_size = max(24, int(size * 0.62))
        rf = font(bold, ref_size)
        text_h = line_h * len(lines)
        ch = style.padding * 2 + text_h + int(ref_size * 2.4)
        if ch <= H * style.max_height_share or size <= style.min_font:
            break
        size -= 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x0, y0 = (W - cw) // 2, (H - ch) // 2
    # soft shadow, then the card
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x0 + 6, y0 + 12, x0 + cw + 6, y0 + ch + 12), style.radius,
                                             fill=(0, 0, 0, 110))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(18)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0, y0, x0 + cw, y0 + ch), style.radius, fill=style.bg, outline=style.gold,
                        width=style.border)
    y = y0 + style.padding
    for line in lines:
        t = prep(line)
        b = d.textbbox((0, 0), t, font=f, **kw)
        lw = b[2] - b[0]
        d.text((x0 + (cw - lw) // 2 - b[0], y + (line_h - (b[3] - b[1])) // 2 - b[1]), t, font=f,
               fill=style.navy, **kw)
        y += line_h
    # gold rule and the reference
    y += int(ref_size * 0.35)
    d.line((x0 + cw * 0.3, y, x0 + cw * 0.7, y), fill=style.gold, width=3)
    y += int(ref_size * 0.45)
    t = prep(ref_text)
    b = d.textbbox((0, 0), t, font=rf, **kw)
    d.text((x0 + (cw - (b[2] - b[0])) // 2 - b[0], y - b[1]), t, font=rf, fill=style.gold, **kw)
    return img
