"""Title and speech check before a video is used.

The title of a Kalima Sawaa post is the file name of a reel made by someone else, so nobody reads it
before it goes out on four platforms. This module asks a language model to judge each title once (the
verdict is cached in state): a neutral rewrite that keeps the topic, plus a risk level. The speech of the
reel (a Whisper transcript) is judged the same way. A video whose risk is in `block_levels` is held back
(alert on Telegram) and the next one is used. Any model trouble raises GuardError, so the video is simply
not posted (fail closed) and the slot is retried on the next run.

The model is a backend: a local open model run on the GitHub runner (default, no key), any
OpenAI-compatible API (for example Groq's free tier), or the Anthropic API.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import requests

from . import mldeps
from .content.text import cut_words

log = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504, 529)
RISKS = ("low", "medium", "high")
MAX_TRANSCRIPT = 3000       # characters of the transcript sent to the model
MAX_TITLE = 100          # YouTube's title limit, the shortest of the platforms

_ARABIC = "؀-ۿ"
# "واز.د.راء" / "يط..عن": dots (or tatweel) pushed into a word to dodge text filters
_TATWEEL = re.compile("ـ+")
_DOT_RUN = re.compile(rf"(?<=[{_ARABIC}])[.·•․]{{2,}}(?=[{_ARABIC}])")
_TOKEN = re.compile(rf"[{_ARABIC}.·•․]+")
_INNER_DOT = re.compile(rf"(?<=[{_ARABIC}])[.·•․](?=[{_ARABIC}])")

SYSTEM_PROMPT = """You prepare titles for short Arabic reels on the channel "كلمة سواء": an educational, \
interfaith-dialogue page (Islam, Christianity and comparative religion) that reposts reels made by others with \
permission. The title is taken from a file name and is used as the caption on TikTok, Instagram, Facebook and \
YouTube Shorts, so it must follow those platforms' community rules.
{context}
Judge the title and, if needed, rewrite it. Rewrite rules (write Arabic, same dialect and register as the original, \
at most 100 characters):
- Keep the topic, and any book, verse, person or term names exactly as given. Never add facts, claims, numbers or \
names that are not in the original title.
- Remove insults, nicknames, body-shaming and mockery aimed at a person or a religion, laughing/mocking emojis, \
and generalizations about a whole religious group (for example "المسيحي" or "المسلم" as a type of person): name the \
specific book, claim or speaker instead.
- Remove shock-bait wording (صدمة, فضيحة, كارثة, يفضح, ينهار ...) and extra punctuation or emoji.
- Prefer a calm descriptive form: a question, "قراءة في ...", "رد على ...", "مناقشة ...". Criticizing an idea or a \
claim is allowed; demeaning people or a group is not.
- Write words normally: no dots, dashes or elongation letters placed inside words.

Risk levels:
- low: the title was already neutral (return it unchanged).
- medium: the title needed rewriting, and the neutral version still describes the video honestly.
- high: it cannot be made safe without misrepresenting the video: its point is to insult or demean a person or \
group, it targets a named private individual, or it involves hate, violence, or self-harm content.

Reply with ONLY one JSON object, no other text:
{{"title": "<final title>", "risk": "low|medium|high", "reasons": ["<short English reason>", ...]}}"""


AUDIO_SYSTEM_PROMPT = """You review what is SAID in short Arabic reels for the channel "كلمة سواء": an educational, \
interfaith-dialogue page (Islam, Christianity and comparative religion) that reposts reels made by others with \
permission, on TikTok, Instagram, Facebook and YouTube Shorts.
{context}
You get one JSON object: {{"title": "...", "transcript": "..."}}. The transcript is automatic speech-to-text of \
spoken Arabic (often Egyptian dialect). It can contain recognition mistakes, missing or repeated words and, for music \
or silence, nothing at all: never judge a video on one odd word, and treat an empty or unclear transcript as low risk.

Allowed: quoting and discussing scripture, theology, history and comparative religion; criticizing a belief, a book \
or an argument; talking about death, the afterlife, hell or divine punishment in a religious context (that is NOT \
self-harm content).
Not allowed: insults, slurs or mockery aimed at people or at a whole religious group (not at an idea); dehumanizing \
generalizations; calls for, threats of or praise for violence; describing or encouraging suicide or self-harm; \
sexual content; harassment of a named private individual.

Risk levels:
- low: nothing that breaks those rules (also: empty or unclear transcript).
- medium: a sharp or mocking tone, but no clear violation.
- high: the transcript clearly contains a violation. Quote the exact Arabic words that break the rule in the reason.

Reply with ONLY one JSON object, no other text:
{{"risk": "low|medium|high", "reasons": ["<short English reason; for high, include the exact Arabic words>", ...]}}"""


RETITLE_SYSTEM_PROMPT = """You write titles for short Arabic reels on the channel "كلمة سواء": an educational, \
interfaith-dialogue page (Islam, Christianity and comparative religion) that reposts reels made by others with \
permission. The title is used as the caption on TikTok, Instagram, Facebook and YouTube Shorts, so it must follow \
those platforms' community rules.
{context}
The original title of this reel cannot be posted: it insults, mocks or generalizes about people or a religion, or is \
shock-bait. The speech of the reel was checked separately and is acceptable. You get one JSON object: \
{{"original_title": "...", "transcript": "..."}}; the transcript is automatic speech-to-text of spoken Arabic and can \
contain recognition mistakes.

Write a NEW title (Arabic, same dialect and register as the original, at most 100 characters):
- If the original title has a clear neutral core (the speaker's or guest's name, the book, the verse, the question), \
keep that core and drop the insult, the mockery, the shock words, the emojis and the generalizations.
- If the original title says nothing usable, take the topic from the transcript: what is actually discussed or asked.
- Prefer a calm descriptive form: a question, "قراءة في ...", "رد على ...", "مناقشة ...". Criticizing an idea or a \
claim is fine; demeaning people or a group is not.
- Use only names, books, verses and claims that appear in the original title or in the transcript. Never add facts. \
Never say what the transcript does not say.
- Write words normally: no dots, dashes or elongation letters placed inside words.

Risk: "low" when you wrote a clean title that honestly describes the reel; "high" when no honest, clean title is \
possible (the transcript is empty or unclear and the title has no usable core, or the whole point of the reel is to \
demean someone).

Reply with ONLY one JSON object, no other text:
{{"title": "<new title>", "risk": "low|high", "reasons": ["<short English note on where the title came from>", ...]}}"""


TAGS_SYSTEM_PROMPT = """You pick hashtags for short Arabic reels on the channel "كلمة سواء": an educational, \
interfaith-dialogue page (Islam, Christianity and comparative religion).
{context}
You get one JSON object: {{"title": "...", "transcript": "..."}}; the transcript is automatic speech-to-text of spoken \
Arabic and can contain recognition mistakes.

Pick the {count} hashtags that best name the SUBJECT of the reel:
- Arabic, one to three words joined by "_", for example "الثالوث" or "إنجيل_يوحنا" or "الكتاب_المقدس".
- Each word must come from the title or from what is said; never invent a topic that is not discussed.
- A topic, a book, a verse or a doctrine only: no insults, no slogans, no claims, no names of people, no names of \
sects, no generic tags like "دين" or "فيديو".

Reply with ONLY one JSON object, no other text:
{{"tags": ["<tag>", ...]}}"""

# hashtags the templates already carry, and words that never belong in a topical tag
RESERVED_TAGS = {"معاذ_عليان", "كلمة_سواء", "حوار_الأديان", "مقارنة_الأديان", "shorts"}
_TAG_CHARS = re.compile(r"[^\w]", re.UNICODE)
_DIACRITICS = re.compile("[\u064b-\u065f\u0670\u0640]")


def _fold(text: str) -> str:
    """Arabic spelling variants folded together, so a tag is matched against the speech loosely."""
    text = _DIACRITICS.sub("", text.lower())
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    return text


def clean_tags(raw, source_text: str, limit: int) -> list[str]:
    """Hashtags (without '#') that are well formed, not already in the templates and grounded in `source_text`:
    every word of the tag has to occur in the title or the transcript."""
    folded = _fold(source_text)
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        tag = "_".join(_TAG_CHARS.sub(" ", str(item).replace("#", " ").replace("_", " ")).split())
        words = tag.split("_")
        if not (3 <= len(tag) <= 30 and len(words) <= 3) or tag in RESERVED_TAGS or tag in out:
            continue
        if any(len(w) < 2 or _fold(w) not in folded for w in words):
            continue
        out.append(tag)
    return out[:limit]


class GuardError(Exception):
    pass


@dataclass(frozen=True)
class Verdict:
    title: str                 # the title to post (original, cleaned, if risk is low)
    risk: str                  # low | medium | high
    reasons: tuple[str, ...] = ()
    original: str = ""

    def to_json(self) -> str:
        return json.dumps({"title": self.title, "risk": self.risk, "reasons": list(self.reasons),
                           "original": self.original}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Verdict":
        d = json.loads(raw)
        return cls(d["title"], d["risk"], tuple(d.get("reasons") or ()), d.get("original", ""))


@dataclass(frozen=True)
class AudioVerdict:
    risk: str                  # low | medium | high
    reasons: tuple[str, ...] = ()
    excerpt: str = ""          # the first words of the transcript, for the Telegram alert (not the whole text)

    def to_json(self) -> str:
        return json.dumps({"risk": self.risk, "reasons": list(self.reasons), "excerpt": self.excerpt},
                          ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "AudioVerdict":
        d = json.loads(raw)
        return cls(d["risk"], tuple(d.get("reasons") or ()), d.get("excerpt", ""))


def clean_obfuscation(title: str) -> str:
    """Undo letter-spacing tricks: tatweel, dot runs inside a word, and words with several inner dots.

    A single inner dot (e.g. "د.محمد") is left alone, it is a normal abbreviation."""
    title = _TATWEEL.sub("", title)
    title = _DOT_RUN.sub("", title)

    def token(m: re.Match) -> str:
        word = m.group(0)
        return _INNER_DOT.sub("", word) if len(_INNER_DOT.findall(word)) >= 2 else word

    return " ".join(_TOKEN.sub(token, title).split())


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _json_object(text: str) -> dict:
    text = _THINK.sub("", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise GuardError(f"no JSON in the model reply: {text[:120]!r}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise GuardError(f"bad JSON in the model reply: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardError("the model reply is not a JSON object")
    return data


def parse_audio_reply(text: str, transcript: str) -> AudioVerdict:
    data = _json_object(text)
    risk = str(data.get("risk", "")).lower()
    if risk not in RISKS:
        raise GuardError(f"unknown risk {data.get('risk')!r}")
    reasons = tuple(str(r)[:300] for r in (data.get("reasons") or [])[:5])
    return AudioVerdict(risk, reasons, " ".join(transcript.split())[:160])


def parse_reply(text: str, original: str) -> Verdict:
    m = re.search(r"\{.*\}", _THINK.sub("", text), re.DOTALL)
    if not m:
        raise GuardError(f"no JSON in the model reply: {text[:120]!r}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise GuardError(f"bad JSON in the model reply: {exc}") from exc
    risk = str(data.get("risk", "")).lower()
    if risk not in RISKS:
        raise GuardError(f"unknown risk {data.get('risk')!r}")
    reasons = tuple(str(r)[:200] for r in (data.get("reasons") or [])[:5])
    title = " ".join(str(data.get("title") or "").split())
    if risk == "high":
        return Verdict(title or original, risk, reasons, original)
    if not title:
        raise GuardError("the model returned an empty title")
    if risk == "low":
        return Verdict(original, risk, reasons, original)          # low = unchanged, whatever it returned
    return Verdict(cut_words(title, MAX_TITLE), risk, reasons, original)


def parse_retitle_reply(text: str, original: str) -> Verdict:
    data = _json_object(text)
    risk = str(data.get("risk", "")).lower()
    if risk not in ("low", "high"):
        raise GuardError(f"unknown risk {data.get('risk')!r}")
    reasons = tuple(str(r)[:200] for r in (data.get("reasons") or [])[:5])
    title = clean_obfuscation(" ".join(str(data.get("title") or "").split()))
    if risk == "high":
        return Verdict(title or original, risk, reasons, original)
    if not title:
        raise GuardError("the model returned an empty title")
    return Verdict(cut_words(title, MAX_TITLE), risk, reasons, original)


class BackendError(Exception):
    """A model call failed. `retryable`: overload / network trouble that may pass, not a bad key or model."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Backend(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 500) -> str: ...


class AnthropicBackend:
    def __init__(self, api_key: str, model: str, session: requests.Session | None = None):
        self.api_key, self.model = api_key, model
        self.session = session or requests.Session()

    def complete(self, system: str, user: str, max_tokens: int = 500) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        try:
            resp = self.session.post(ANTHROPIC_API, json=body, timeout=60, headers={
                "x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"})
        except requests.RequestException as exc:
            raise BackendError(f"network error: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code != 200:
            raise BackendError(f"HTTP {resp.status_code}: {resp.text[:160]}", resp.status_code in RETRY_STATUS)
        try:
            return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        except ValueError as exc:
            raise BackendError("the API reply was not JSON") from exc


class OpenAICompatBackend:
    """Any /chat/completions API of the OpenAI shape (Groq, OpenRouter, a llama.cpp server ...)."""

    def __init__(self, base_url: str, api_key: str, model: str, session: requests.Session | None = None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key, self.model = api_key, model
        self.session = session or requests.Session()

    def complete(self, system: str, user: str, max_tokens: int = 500) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            resp = self.session.post(self.url, json=body, timeout=120, headers={
                "Authorization": f"Bearer {self.api_key}", "content-type": "application/json"})
        except requests.RequestException as exc:
            raise BackendError(f"network error: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code != 200:
            raise BackendError(f"HTTP {resp.status_code}: {resp.text[:160]}", resp.status_code in RETRY_STATUS)
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise BackendError("the API reply had no message") from exc


class LocalBackend:
    """An open model (GGUF) run by llama.cpp on the machine itself: no key, no quota, slower.

    The model is downloaded from Hugging Face and loaded on the first question of the run."""

    def __init__(self, repo_id: str, filename: str, n_ctx: int = 4096, threads: int | None = None,
                 factory: Callable[[], object] | None = None):
        self.repo_id, self.filename, self.n_ctx = repo_id, filename, n_ctx
        self.threads = threads or os.cpu_count() or 4
        self._factory = factory or self._load
        self._llm = None

    def _load(self):
        module = mldeps.ensure("llama_cpp")
        log.info("loading %s/%s - the first run downloads the model", self.repo_id, self.filename)
        return module.Llama.from_pretrained(repo_id=self.repo_id, filename=self.filename, n_ctx=self.n_ctx,
                                            n_threads=self.threads, verbose=False)

    def complete(self, system: str, user: str, max_tokens: int = 500) -> str:
        try:
            if self._llm is None:
                self._llm = self._factory()
            out = self._llm.create_chat_completion(
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user + "\n/no_think"}],     # Qwen3: answer without thinking
                max_tokens=max_tokens, temperature=0.0, response_format={"type": "json_object"})
            return out["choices"][0]["message"]["content"] or ""
        except mldeps.MLDepsError as exc:
            raise BackendError(str(exc)) from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError("the local model reply had no message") from exc
        except Exception as exc:  # noqa: BLE001 - download, memory, llama.cpp errors
            raise BackendError(f"local model failed: {type(exc).__name__}: {exc}") from exc


class TitleGuard:
    def __init__(self, api_key: str = "", model: str = "", context: str = "", session: requests.Session | None = None,
                 sleep=time.sleep, attempts: int = 3, backend: Backend | None = None):
        """With no `backend`, the Anthropic API is used (api_key, model, session)."""
        self.backend = backend or AnthropicBackend(api_key, model, session)
        ctx = (context.strip() + "\n") if context.strip() else ""
        self.system = SYSTEM_PROMPT.format(context=ctx)
        self.audio_system = AUDIO_SYSTEM_PROMPT.format(context=ctx)
        self.retitle_system = RETITLE_SYSTEM_PROMPT.format(context=ctx)
        self._ctx = ctx
        self.sleep, self.attempts = sleep, attempts

    def _ask(self, system: str, user: str, max_tokens: int, what: str) -> str:
        last = "unknown error"
        for attempt in range(self.attempts):
            try:
                return self.backend.complete(system, user, max_tokens)
            except BackendError as exc:
                last = str(exc)
                if not exc.retryable:
                    break                      # a 4xx (bad key, bad model) will not fix itself by retrying
            if attempt + 1 < self.attempts:
                self.sleep(2 ** attempt * 2)
        raise GuardError(f"{what} failed: {last}")

    def judge(self, title: str) -> Verdict:
        original = clean_obfuscation(title)
        text = self._ask(self.system, json.dumps({"title": original}, ensure_ascii=False), 500, "title check")
        return parse_reply(text, original)

    def retitle(self, original_title: str, transcript: str) -> Verdict:
        """A new, clean title for a reel whose own title cannot be posted: from the title's neutral core when it
        has one, otherwise from what is said. risk "high" = no honest clean title is possible."""
        original = clean_obfuscation(original_title)
        payload = json.dumps({"original_title": original, "transcript": " ".join(transcript.split())[:MAX_TRANSCRIPT]},
                             ensure_ascii=False)
        text = self._ask(self.retitle_system, payload, 500, "retitle")
        return parse_retitle_reply(text, original)

    def hashtags(self, title: str, transcript: str, count: int = 2) -> list[str]:
        """Up to `count` topical hashtags (no '#') for a reel, each grounded in its title or transcript."""
        transcript = " ".join(transcript.split())
        if count <= 0 or len(transcript) < 8:
            return []
        payload = json.dumps({"title": title, "transcript": transcript[:MAX_TRANSCRIPT]}, ensure_ascii=False)
        text = self._ask(TAGS_SYSTEM_PROMPT.format(context=self._ctx, count=count), payload, 200, "hashtags")
        return clean_tags(_json_object(text).get("tags"), f"{title} {transcript}", count)

    def judge_audio(self, title: str, transcript: str) -> AudioVerdict:
        """Risk of what is said in the reel. An empty transcript (music, silence) is low risk without a model call."""
        transcript = " ".join(transcript.split())
        if len(transcript) < 8:
            return AudioVerdict("low", ("no speech found",), transcript)
        payload = json.dumps({"title": title, "transcript": transcript[:MAX_TRANSCRIPT]}, ensure_ascii=False)
        text = self._ask(self.audio_system, payload, 400, "audio check")
        return parse_audio_reply(text, transcript)
