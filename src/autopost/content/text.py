"""Platform texts: templates, length limits and trimming at the last full sentence.

Lengths are measured conservatively in UTF-16 code units (an emoji counts as 2),
except YouTube descriptions, whose 5000 limit is in UTF-8 bytes (an Arabic letter = 2 bytes).
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from ..config import TEMPLATE_FIELDS, TemplateConfig

log = logging.getLogger(__name__)

# platform -> field -> limit
DEFAULT_LIMITS: dict[str, dict[str, int]] = {
    "facebook": {"text": 5000},
    "instagram": {"text": 2200},
    "tiktok": {"text": 2200},
    "youtube": {"title": 100, "description": 5000},
    "pinterest": {"title": 100, "description": 800},
}
BYTE_LIMITED = {("youtube", "description")}
TITLE_FIELDS = {"title"}

# A sentence ends with one of these (plus any closing quotes/brackets), followed by whitespace or the end.
_SENTENCE_END = re.compile(r"[.!?؟…۔۝]+[\"'”’»)\]﴾﴿]*(?=\s|$)|\n")
_TRAILING_JUNK = "،,؛;:-–— \n\t"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_to(text: str, limit: int, measure: Callable[[str], int]) -> str:
    """Longest prefix of `text` whose measure is <= limit."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _balanced(text: str) -> bool:
    # Never cut inside a Quranic verse ﴿ … ﴾ or parentheses.
    return text.count("﴿") == text.count("﴾") and text.count("(") == text.count(")")


def cut_words(text: str, limit: int, measure: Callable[[str], int] = utf16_len, suffix: str = "") -> str:
    """Cut at a word boundary so that result + suffix fits; hard cut if no reasonable boundary."""
    text = text.strip()
    if measure(text) <= limit:
        return text
    room = limit - measure(suffix)
    prefix = _cut_to(text, room, measure)
    space = max(prefix.rfind(" "), prefix.rfind("\n"))
    if space >= len(prefix) * 0.6:
        prefix = prefix[:space]
    return prefix.rstrip(_TRAILING_JUNK) + suffix


def trim_sentences(text: str, limit: int, measure: Callable[[str], int] = utf16_len) -> str:
    """Trim to `limit` at the last full sentence; fall back to a word boundary + "…"."""
    text = text.strip()
    if measure(text) <= limit:
        return text
    best = None
    for m in _SENTENCE_END.finditer(text):
        candidate = text[: m.end()].rstrip()
        if measure(candidate) > limit:
            break
        if _balanced(candidate):
            best = candidate
    if best and measure(best) >= limit * 0.3:
        return best
    cut = cut_words(text, limit, measure, suffix="…")
    if _balanced(cut):
        return cut
    if best:
        return best
    # cut just before the verse/parenthesis that would be left open
    body = cut[:-1]
    for opener, closer in (("﴿", "﴾"), ("﴾", "﴿"), ("(", ")")):
        if body.count(opener) > body.count(closer):
            body = body[: body.rfind(opener)]
    return body.rstrip(_TRAILING_JUNK) + "…"


def clean_for(platform: str, field: str, value: str) -> str:
    if platform == "youtube":
        value = value.replace("<", "").replace(">", "")     # rejected by the YouTube API
    if field in TITLE_FIELDS:
        value = " ".join(value.split())                     # titles are single-line
    return value.strip()


def limit_field(platform: str, field: str, value: str, limits: dict[str, dict[str, int]] | None = None) -> str:
    limit = ((limits or {}).get(platform) or {}).get(field) or DEFAULT_LIMITS.get(platform, {}).get(field)
    value = clean_for(platform, field, value)
    if not limit:
        return value
    measure = utf8_len if (platform, field) in BYTE_LIMITED else utf16_len
    if measure(value) <= limit:
        return value
    out = cut_words(value, limit, measure) if field in TITLE_FIELDS else trim_sentences(value, limit, measure)
    log.info("%s %s trimmed from %d to %d", platform, field, measure(value), measure(out))
    return out


def render(template: TemplateConfig, platform: str, variables: dict[str, str],
           limits: dict[str, dict[str, int]] | None = None) -> dict[str, str]:
    out = {}
    for field in TEMPLATE_FIELDS[platform]:
        raw = getattr(template, field).format_map(variables)
        out[field] = limit_field(platform, field, raw, limits)
    return out


_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def title_from_filename(name: str, strip_prefixes: list[str] | None = None,
                        replacements: dict[str, str] | None = None) -> str:
    """File name without extension (any extension, any case), with optional clean-ups."""
    title = _EXTENSION.sub("", name.strip())
    for prefix in strip_prefixes or []:
        if title.startswith(prefix):
            title = title[len(prefix):]
            break
    for old, new in (replacements or {}).items():
        title = title.replace(old, new)
    return " ".join(title.split())
