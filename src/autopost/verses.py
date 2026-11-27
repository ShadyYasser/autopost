"""Bible quotations read out in a reel, found in Whisper's transcript and checked against the real text.

Nothing here is written by a language model: a card shows a verse only when the speaker's words match the
Smith-Van Dyck text (public domain, 1865) closely, and the card shows that text and its reference exactly as
the translation has them. A paraphrase, a loose quote or another translation simply gets no card.

Two sources of cards, the first one preferred:
- on screen: the creator's own verse card, read by OCR (`screen_text.py`): its reference, e.g. "(يو 13: 25)", says
  which verse he means; the card is used when the Van Dyck text of that reference matches the words on screen or
  the words spoken around it.
- spoken: a verse read out without being shown, matched word for word as below; the language model then checks
  that the speaker really quotes it (it can only keep or drop a candidate, never write or change text).

How a spoken quote is found (plain string matching):
1. Both texts are normalised (no diacritics, one form of alef/ya/ta marbuta, no punctuation).
2. Runs of three words of the transcript are looked up in an index of the Bible's three-word runs; verses
   that share several runs with a stretch of speech are candidates.
3. A candidate is kept only when a long stretch of its words is said in order (difflib) - see `CardRules`.
   When two different verses fit equally well (parallel passages), no card is made: the reference would be a guess.
"""

from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests

from .edits import Segment

log = logging.getLogger(__name__)

# Pinned downloads (checked by SHA-256): the Bible text and the Amiri font used on the card.
BIBLE_URL = ("https://raw.githubusercontent.com/midvash/bible-data/9586bd8228a747962df1ee8692730422fb7eb2ec/"
             "versions/ar/svd/svd.json")
BIBLE_SHA256 = "cf3eba11d6e57feb0a6f4fc9d4ed41505e9f74067beac96f64b25b3b1e2171f7"
FONT_URL = ("https://raw.githubusercontent.com/google/fonts/5026800714d07231937d92eb4cceabf06e05d74e/"
            "ofl/amiri/Amiri-Regular.ttf")
FONT_SHA256 = "ab391c4147d054c48976e98322ad0eefe1427aa0e0502a12a4c75d80a70cfcd7"
FONT_BOLD_URL = FONT_URL.replace("Amiri-Regular", "Amiri-Bold")
FONT_BOLD_SHA256 = "cfccb794268e7d573d857e6d6a67f89cf8a053e8ffd85dfa0c8ec1bb36fc4827"

# Book names as printed in Arabic Bibles, in canonical order (book id 1..66).
BOOKS_AR = [
    "التكوين", "الخروج", "اللاويين", "العدد", "التثنية", "يشوع", "القضاة", "راعوث", "صموئيل الأول",
    "صموئيل الثاني", "الملوك الأول", "الملوك الثاني", "أخبار الأيام الأول", "أخبار الأيام الثاني", "عزرا",
    "نحميا", "أستير", "أيوب", "المزامير", "الأمثال", "الجامعة", "نشيد الأنشاد", "إشعياء", "إرميا",
    "مراثي إرميا", "حزقيال", "دانيال", "هوشع", "يوئيل", "عاموس", "عوبديا", "يونان", "ميخا", "ناحوم",
    "حبقوق", "صفنيا", "حجي", "زكريا", "ملاخي", "متى", "مرقس", "لوقا", "يوحنا", "أعمال الرسل", "رومية",
    "كورنثوس الأولى", "كورنثوس الثانية", "غلاطية", "أفسس", "فيلبي", "كولوسي", "تسالونيكي الأولى",
    "تسالونيكي الثانية", "تيموثاوس الأولى", "تيموثاوس الثانية", "تيطس", "فليمون", "العبرانيين", "يعقوب",
    "بطرس الأولى", "بطرس الثانية", "يوحنا الأولى", "يوحنا الثانية", "يوحنا الثالثة", "يهوذا", "رؤيا يوحنا",
]
_SINGULAR = {"المزامير": "المزمور"}     # "المزمور ٢٣: ١", not "المزامير ٢٣: ١"

_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_NON_WORD = re.compile(r"[^\u0621-\u064a0-9\s]")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def normalize(text: str) -> list[str]:
    """Words of `text` with diacritics, tatweel and punctuation removed and letter variants unified."""
    text = _DIACRITICS.sub("", text.replace("ٱ", "ا")).translate(_ALEF)
    return _NON_WORD.sub(" ", text).split()


@dataclass(frozen=True)
class Verse:
    book: int
    chapter: int
    number: int
    text: str                       # as printed (with diacritics)
    words: tuple[str, ...]          # normalised


@dataclass(frozen=True)
class CardRules:
    min_matched_words: int = 7      # words of the verse said in order, at least
    min_coverage: float = 0.6       # ... and at least this share of the verse
    min_coverage_long: float = 0.35  # a long verse read in part: allowed when this many words match ...
    long_matched_words: int = 14     # ... in a row of at least this length
    min_density: float = 0.7        # matched words / spoken words over the matched stretch
    max_card_words: int = 60        # longer passages are not shown (the card would be unreadable)
    min_seconds: float = 2.5
    pad_before: float = 0.2
    pad_after: float = 0.6
    ambiguity_margin: float = 0.02  # two different verses within this score of each other: no card


@dataclass(frozen=True)
class VerseCard:
    start: float                    # seconds in the source video
    end: float
    text: str                       # verse text as printed (consecutive verses joined)
    reference: str                  # e.g. "متى ٢٦: ٢٦" or "يوحنا ٣: ١٦-١٧"
    score: float = 0.0
    source: str = "speech"          # "screen" (the creator's own card) or "speech"

    def to_json(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "text": self.text,
                "reference": self.reference, "score": round(self.score, 3), "source": self.source}

    @classmethod
    def from_json(cls, d: dict) -> "VerseCard":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]), str(d["reference"]),
                   float(d.get("score", 0.0)), str(d.get("source", "speech")))


def reference(book: int, chapter: int, first: int, last: int | None = None) -> str:
    name = BOOKS_AR[book - 1]
    name = _SINGULAR.get(name, name)
    verses = str(first) if not last or last == first else f"{first}-{last}"
    return f"{name} {str(chapter).translate(_ARABIC_DIGITS)}: {verses.translate(_ARABIC_DIGITS)}"


# ---------------------------------------------------------------- downloads


def cache_dir() -> Path:
    base = Path(os.environ.get("AUTOPOST_CACHE", Path.home() / ".cache" / "autopost"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def fetch_pinned(url: str, sha256: str, name: str, timeout: int = 120) -> Path:
    """Download `url` once into the cache and check its SHA-256 (a changed file is never used)."""
    path = cache_dir() / name
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
        return path
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    if hashlib.sha256(resp.content).hexdigest() != sha256:
        raise ValueError(f"{name}: downloaded file does not match its pinned SHA-256")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- the Bible and its index


class Bible:
    """Every verse, plus an index of three-word runs → verse positions."""

    def __init__(self, verses: list[Verse]):
        self.verses = verses
        self.pos = {(v.book, v.chapter, v.number): i for i, v in enumerate(verses)}
        self.index: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for i, v in enumerate(verses):
            for j in range(len(v.words) - 2):
                self.index[v.words[j:j + 3]].append(i)

    @classmethod
    def from_json_file(cls, path: Path) -> "Bible":
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        verses = []
        for b in data["books"]:
            for ch in b["chapters"]:
                for v in ch["verses"]:
                    text = " ".join(str(v["text"]).split())
                    verses.append(Verse(int(b["bookId"]), int(ch["chapter"]), int(v["number"]), text,
                                        tuple(normalize(text))))
        return cls(verses)

    def next_verse(self, i: int) -> int | None:
        if i + 1 < len(self.verses):
            a, b = self.verses[i], self.verses[i + 1]
            if (a.book, a.chapter) == (b.book, b.chapter):
                return i + 1
        return None


_BIBLE: Bible | None = None


def load_bible() -> Bible:
    global _BIBLE
    if _BIBLE is None:
        path = fetch_pinned(BIBLE_URL, BIBLE_SHA256, "bible-svd.json")
        _BIBLE = Bible.from_json_file(path)
        log.info("bible: %d verses indexed", len(_BIBLE.verses))
    return _BIBLE


# ---------------------------------------------------------------- matching


def _timed_words(segments: list[Segment]) -> tuple[list[str], list[float], list[float]]:
    """Normalised words of the transcript with an estimated start/end time each (spread over the segment)."""
    words, starts, ends = [], [], []
    for s in segments:
        ws = normalize(s.text)
        if not ws:
            continue
        step = (s.end - s.start) / len(ws)
        for k, w in enumerate(ws):
            words.append(w)
            starts.append(s.start + k * step)
            ends.append(s.start + (k + 1) * step)
    return words, starts, ends


@dataclass
class _Match:
    verse: int
    score: float
    matched: int
    first: int          # transcript word index of the first matched word
    last: int           # ... of the last matched word


def _align(spoken: list[str], offset: int, verse: Verse, rules: CardRules) -> _Match | None:
    sm = difflib.SequenceMatcher(None, verse.words, spoken, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
    if not blocks:
        return None
    matched = sum(b.size for b in blocks)
    first, last = blocks[0].b, blocks[-1].b + blocks[-1].size - 1
    span = last - first + 1
    coverage = matched / len(verse.words)
    density = matched / span
    longest = max(b.size for b in blocks)
    ok = matched >= rules.min_matched_words and density >= rules.min_density and (
        coverage >= rules.min_coverage or (coverage >= rules.min_coverage_long and longest >= rules.long_matched_words))
    if not ok:
        return None
    return _Match(-1, coverage * density, matched, offset + first, offset + last)


def find_quotes(segments: list[Segment], bible: Bible, rules: CardRules | None = None) -> list[VerseCard]:
    """Cards for the verses read out in `segments`, in the order they are said (never overlapping)."""
    rules = rules or CardRules()
    words, starts, ends = _timed_words(segments)
    if len(words) < 3:
        return []
    hits: dict[int, list[int]] = defaultdict(list)          # verse -> transcript positions of shared runs
    for j in range(len(words) - 2):
        for vi in bible.index.get(tuple(words[j:j + 3]), ()):
            hits[vi].append(j)
    found: list[_Match] = []
    for vi, positions in hits.items():
        verse = bible.verses[vi]
        if len(positions) < 2 or len(verse.words) < 5:
            continue
        # look at the stretch of speech around the shared runs, a little wider than the verse itself
        lo = max(0, min(positions) - 3)
        hi = min(len(words), max(positions) + 3 + len(verse.words))
        if hi - lo > 3 * len(verse.words) + 10:                # runs scattered over the reel: take the densest
            centre = sorted(positions)[len(positions) // 2]
            lo = max(0, centre - len(verse.words) - 3)
            hi = min(len(words), centre + len(verse.words) + 3)
        m = _align(words[lo:hi], lo, verse, rules)
        if m is not None:
            m.verse = vi
            found.append(m)
    # one verse per stretch of speech: the best; a near-tie between different texts means "unsure"
    found.sort(key=lambda m: (-m.score, m.first))
    chosen: list[_Match] = []
    for m in found:
        overlapping = [c for c in chosen if not (m.last < c.first or m.first > c.last)]
        if overlapping:
            continue
        rivals = [o for o in found if o is not m and not (o.last < m.first or o.first > m.last)
                  and bible.verses[o.verse].words != bible.verses[m.verse].words]
        if rivals and max(r.score for r in rivals) >= m.score - rules.ambiguity_margin:
            log.info("verse card skipped: %s and %s fit equally well", _ref_of(bible, m.verse),
                     _ref_of(bible, max(rivals, key=lambda r: r.score).verse))
            continue
        same = [o for o in found if o is not m and bible.verses[o.verse].words == bible.verses[m.verse].words
                and not (o.last < m.first or o.first > m.last)]
        if same:                                             # identical wording in two places: reference unknown
            log.info("verse card skipped: %s has identical twins", _ref_of(bible, m.verse))
            continue
        chosen.append(m)
    chosen.sort(key=lambda m: m.first)
    return _cards(chosen, bible, starts, ends, rules)


def _ref_of(bible: Bible, vi: int) -> str:
    v = bible.verses[vi]
    return reference(v.book, v.chapter, v.number)


def _cards(matches: list[_Match], bible: Bible, starts: list[float], ends: list[float],
           rules: CardRules) -> list[VerseCard]:
    # consecutive verses read back to back become one card
    groups: list[list[_Match]] = []
    for m in matches:
        if groups and bible.next_verse(groups[-1][-1].verse) == m.verse and m.first - groups[-1][-1].last <= 4:
            groups[-1].append(m)
        else:
            groups.append([m])
    cards = []
    for g in groups:
        vs = [bible.verses[m.verse] for m in g]
        text = " ".join(v.text for v in vs)
        if len(text.split()) > rules.max_card_words:
            log.info("verse card skipped: %s is too long for a card", _ref_of(bible, g[0].verse))
            continue
        start = max(0.0, starts[g[0].first] - rules.pad_before)
        end = ends[g[-1].last] + rules.pad_after
        if end - start < rules.min_seconds:
            end = start + rules.min_seconds
        ref = reference(vs[0].book, vs[0].chapter, vs[0].number, vs[-1].number)
        cards.append(VerseCard(start, end, text, ref, min(m.score for m in g), "speech"))
    # never overlapping, and a little air between two cards
    out: list[VerseCard] = []
    for c in cards:
        if out and c.start < out[-1].end + 0.3:
            prev = out[-1]
            out[-1] = VerseCard(prev.start, max(prev.start + 1.5, c.start - 0.3), prev.text, prev.reference,
                                prev.score, prev.source)
        out.append(c)
    return out


# ---------------------------------------------------------------- references ("(يو 13: 25)", "إنجيل يوحنا 13 23")

# Abbreviations printed in Arabic Bibles' references, in book order (1..66).
ABBR = ["تك", "خر", "لا", "عد", "تث", "يش", "قض", "را", "1صم", "2صم", "1مل", "2مل", "1اخ", "2اخ", "عز", "نح",
        "اس", "اي", "مز", "ام", "جا", "نش", "اش", "ار", "مرا", "حز", "دا", "هو", "يؤ", "عا", "عو", "يون", "مي",
        "نا", "حب", "صف", "حج", "زك", "ملا", "مت", "مر", "لو", "يو", "اع", "رو", "1كو", "2كو", "غل", "اف", "في",
        "كو", "1تس", "2تس", "1تي", "2تي", "تي", "فل", "عب", "يع", "1بط", "2بط", "1يو", "2يو", "3يو", "يه", "رؤ"]
# Spoken / written names (normalised) → book, including common speech-to-text spellings.
NAME_ALIASES = {
    "متي": 40, "مرقس": 41, "لوقا": 42, "يوحنا": 43, "يحنا": 43, "يحني": 43, "يوحني": 43, "اعمال": 44,
    "روميه": 45, "غلاطيه": 48, "افسس": 49, "فيلبي": 50, "كولوسي": 51, "العبرانيين": 58, "عبرانيين": 58,
    "يعقوب": 59, "يهوذا": 65, "رؤيا": 66, "الرؤيا": 66, "التكوين": 1, "تكوين": 1, "الخروج": 2, "خروج": 2,
    "اللاويين": 3, "لاويين": 3, "العدد": 4, "التثنيه": 5, "تثنيه": 5, "يشوع": 6, "القضاه": 7, "راعوث": 8,
    "عزرا": 15, "نحميا": 16, "استير": 17, "ايوب": 18, "مزمور": 19, "المزمور": 19, "المزامير": 19, "مزامير": 19,
    "الامثال": 20, "امثال": 20, "الجامعه": 21, "جامعه": 21, "النشيد": 22, "اشعياء": 23, "اشعيا": 23,
    "ارميا": 24, "ارمياء": 24, "حزقيال": 26, "دانيال": 27, "هوشع": 28, "يوئيل": 29, "عاموس": 30, "يونان": 32,
    "ميخا": 33, "زكريا": 38, "ملاخي": 39,
}
_ABBR_INDEX = {normalize(a)[0] if normalize(a) else a: i + 1 for i, a in enumerate(ABBR)}
_TO_WESTERN = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SCREEN_REF = re.compile(r"([1-3١-٣]?\s?[\u0621-\u064a]{1,4})\.?\s*(\d{1,3})\s*[:：؛]\s*(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?")


def _edit1(a: str, b: str) -> bool:
    """Same word, or one letter different / missing (OCR slips on short abbreviations)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def book_candidates(token: str) -> list[int]:
    """Books an abbreviation read off the screen may stand for: the exact match, else every one-letter slip."""
    t = token.translate(_TO_WESTERN).replace(" ", "")
    t = _DIACRITICS.sub("", t).translate(_ALEF)
    if t in _ABBR_INDEX:
        return [_ABBR_INDEX[t]]
    if t in NAME_ALIASES:
        return [NAME_ALIASES[t]]
    return sorted({b for a, b in _ABBR_INDEX.items() if len(t) >= 2 and _edit1(a, t)})


@dataclass(frozen=True)
class RefHit:
    books: tuple[int, ...]
    chapter: int
    first: int
    last: int


def screen_refs(text: str) -> list[RefHit]:
    """References printed in a frame's text. Digits may come out swapped by the right-to-left layout
    ("(مر21:3)" for 3:21), so both orders are offered; the verse text decides."""
    out = []
    for m in _SCREEN_REF.finditer(text.translate(_TO_WESTERN)):
        books = tuple(book_candidates(m.group(1)))
        if not books:
            continue
        a, b = int(m.group(2)), int(m.group(3))
        last = int(m.group(4)) if m.group(4) else None
        out.append(RefHit(books, a, b, last or b))
        if not last and a != b:
            out.append(RefHit(books, b, a, a))
    return out


def _verse_range(bible: Bible, book: int, chapter: int, first: int, last: int) -> list[int]:
    if last < first or last - first > 4:
        return []
    idx = [bible.pos.get((book, chapter, n)) for n in range(first, last + 1)]
    return [i for i in idx if i is not None] if all(i is not None for i in idx) else []


def _support(bible: Bible, verses_idx: list[int], evidence: set[str]) -> tuple[float, int]:
    """(share, count) of the verse's meaningful words found in `evidence`."""
    words = {w for i in verses_idx for w in bible.verses[i].words if len(w) >= 3}
    if not words:
        return 0.0, 0
    hit = sum(1 for w in words if w in evidence)
    return hit / len(words), hit


def _words_between(segments: list[Segment], start: float, end: float) -> set[str]:
    return {w for s in segments if s.end >= start and s.start <= end for w in normalize(s.text)}


def cards_from_screen(frames: list[tuple[float, str]], segments: list[Segment], bible: Bible,
                      rules: CardRules | None = None, min_support: float = 0.4) -> list[VerseCard]:
    """Cards for the verse cards the creator shows on screen. A reference seen in at least two frames is kept
    when the Van Dyck text of it matches the words on screen or the words spoken around it."""
    rules = rules or CardRules()
    seen: dict[tuple, list[float]] = defaultdict(list)         # (book, chapter, first, last) -> seconds
    words_at: dict[float, set[str]] = {}
    for sec, text in frames:
        words_at[sec] = set(normalize(text))
        for hit in screen_refs(text):
            for book in hit.books:
                if _verse_range(bible, book, hit.chapter, hit.first, hit.last):
                    seen[(book, hit.chapter, hit.first, hit.last)].append(sec)
    found = []
    for key, secs in seen.items():
        secs = sorted(set(secs))
        # split into separate showings (the same card may come back later)
        runs, cur = [], [secs[0]]
        for t in secs[1:]:
            if t - cur[-1] <= 3:
                cur.append(t)
            else:
                runs.append(cur)
                cur = [t]
        runs.append(cur)
        book, chapter, first, last = key
        idx = _verse_range(bible, book, chapter, first, last)
        for run in runs:
            if len(run) < 2:
                continue
            start, end = run[0], run[-1] + 1.0
            evidence = set().union(*(words_at.get(t, set()) for t in run))
            evidence |= _words_between(segments, start - 8, end + 8)
            share, hits = _support(bible, idx, evidence)
            if share >= min_support and hits >= 3:
                found.append((share, len(run), key, start, end))
    # one card per showing: when readings of the same frames disagree (OCR slip, swapped digits), the best
    # supported one wins; a close second that is a different verse means "unsure" → no card
    found.sort(key=lambda f: (-f[0], -f[1]))
    chosen = []
    for f in found:
        share, n, key, start, end = f
        overlap = [c for c in chosen if not (end <= c[3] or start >= c[4])]
        if overlap:
            continue
        rivals = [g for g in found if g[2] != key and not (end <= g[3] or start >= g[4])]
        if rivals and max(r[0] for r in rivals) >= share - 0.05 and \
                _verse_text(bible, rivals[0][2]) != _verse_text(bible, key):
            log.info("screen card skipped: %s and %s both fit", key, rivals[0][2])
            continue
        chosen.append(f)
    cards = []
    for share, n, (book, chapter, first, last), start, end in sorted(chosen, key=lambda f: f[3]):
        idx = _verse_range(bible, book, chapter, first, last)
        text = " ".join(bible.verses[i].text for i in idx)
        if len(text.split()) > rules.max_card_words:
            continue
        cards.append(VerseCard(start, max(end, start + rules.min_seconds), text,
                               reference(book, chapter, first, last), share, "screen"))
    return cards


def _verse_text(bible: Bible, key: tuple) -> str:
    book, chapter, first, last = key
    return " ".join(bible.verses[i].text for i in _verse_range(bible, book, chapter, first, last))


_SPOKEN_REF = re.compile(r"([\u0621-\u064a]{3,10})\s+(?:الاصحاح\s+|اصحاح\s+)?(\d{1,3})\s*(?:[-:/،,]|\s|العدد|عدد|الايه|ايه)+\s*(\d{1,3})")


def spoken_refs(segments: list[Segment], bible: Bible) -> list[tuple[float, RefHit]]:
    """References said out loud ("في إنجيل يحنى 13 23", "مزمور 22-6") with the time they are said."""
    out = []
    for s in segments:
        text = " ".join(normalize(s.text.translate(_TO_WESTERN)))
        for m in _SPOKEN_REF.finditer(text):
            book = NAME_ALIASES.get(m.group(1)) or NAME_ALIASES.get(m.group(1).removeprefix("ال"))
            if book:
                ch, v = int(m.group(2)), int(m.group(3))
                if bible.pos.get((book, ch, v)) is not None:
                    out.append((s.start, RefHit((book,), ch, v, v)))
    return out


def speech_candidates(segments: list[Segment], bible: Bible, rules: CardRules | None = None) -> list[VerseCard]:
    """Verses read out (word-for-word match), plus verses whose reference is said out loud and whose words are
    (at least partly) said within a few seconds. The model still has to confirm each of them."""
    rules = rules or CardRules()
    cards = find_quotes(segments, bible, rules)
    have = {c.reference for c in cards}
    for t, hit in spoken_refs(segments, bible):
        ref = reference(hit.books[0], hit.chapter, hit.first)
        if ref in have:
            continue
        idx = _verse_range(bible, hit.books[0], hit.chapter, hit.first, hit.last)
        near = [s for s in segments if t - 4 <= s.start <= t + 20]
        share, hits = _support(bible, idx, {w for s in near for w in normalize(s.text)})
        if share >= 0.4 and hits >= 3:
            words = set(bible.verses[idx[0]].words)
            said = [s for s in near if len(words & set(normalize(s.text))) >= 2] or near[:1]
            start, end = said[0].start - rules.pad_before, said[-1].end + rules.pad_after
            text = bible.verses[idx[0]].text
            if len(text.split()) <= rules.max_card_words:
                cards.append(VerseCard(max(0.0, start), max(end, start + rules.min_seconds), text, ref, share,
                                       "speech"))
                have.add(ref)
    return sorted(cards, key=lambda c: c.start)


def combine(screen: list[VerseCard], speech: list[VerseCard]) -> list[VerseCard]:
    """On-screen cards first; a spoken card is added only where no on-screen card is (and not the same verse
    again within a few seconds of it). Never overlapping."""
    out = list(screen)
    for c in speech:
        if any(not (c.end <= s.start - 0.3 or c.start >= s.end + 0.3) for s in out):
            continue
        if any(s.reference == c.reference and abs(s.start - c.start) < 20 for s in screen):
            continue
        out.append(c)
    return sorted(out, key=lambda c: c.start)
