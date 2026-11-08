"""Audio check (Whisper + a language model), the model backends and the prescreen command."""

import json
import shutil
import subprocess
import wave

import pytest
import requests

from autopost.config import AudioCheckConfig, TitleGuardConfig
from autopost.content.drive_source import ContentError
from autopost.mldeps import MLDepsError
from autopost.state import State, merge_states
from autopost.title_guard import (
    AudioVerdict,
    BackendError,
    GuardError,
    LocalBackend,
    OpenAICompatBackend,
    TitleGuard,
    parse_audio_reply,
)
from autopost.transcribe import TranscribeError, Transcriber, WhisperSettings, extract_audio

from test_drive_source import make
from test_title_guard import SHORT, reel

AUDIO = AudioCheckConfig(streams=["short"])
CALM = "كلام هادئ في موضوع ديني عن النص والتفسير"
INSULT = "فلان ده حيوان وكل أتباعه حثالة"


class Scripted:
    """A fake model backend: titles are low risk unless listed; a transcript containing a key of `audio` gets
    that verdict."""

    def __init__(self, titles=None, audio=None):
        self.titles, self.audio, self.calls = titles or {}, audio or {}, []

    def complete(self, system, user, max_tokens=500):
        data = json.loads(user)
        if "transcript" in data:
            self.calls.append(("audio", data["transcript"]))
            for needle, verdict in self.audio.items():
                if needle in data["transcript"]:
                    return json.dumps(verdict)
            return json.dumps({"risk": "low", "reasons": []})
        self.calls.append(("title", data["title"]))
        return json.dumps(self.titles.get(data["title"], {"title": data["title"], "risk": "low", "reasons": []}))


class FakeTranscriber:
    def __init__(self, texts=None, fail=()):
        self.texts, self.fail, self.calls = texts or {}, set(fail), []

    def transcribe(self, video, work_dir):
        assert video.exists()                         # the original was downloaded for the check
        vid = video.name.split(".")[0]
        self.calls.append(vid)
        if vid in self.fail:
            raise TranscribeError("whisper crashed")
        return self.texts.get(vid, CALM)


HIGH = {"risk": "high", "reasons": ["insult: حثالة"]}


def setup(app, tmp_path, texts=None, fail=(), audio_verdicts=None, n=3, dry_run=False, audio=AUDIO):
    backend = Scripted(audio=audio_verdicts or {"حثالة": HIGH})
    transcriber = FakeTranscriber(texts, fail)
    content, state, client = make(app, tmp_path, dry_run=dry_run, backend=backend, transcriber=transcriber,
                                  audio=audio)
    app.brands["kalima_sawaa"].streams["short"].order = "newest_first"
    client.files[SHORT] = [reel(i, f"عنوان {i}.mp4") for i in range(1, n + 1)]
    for i, v in enumerate(client.files[SHORT], 1):
        v.id = f"m{i}"
    alerts = []
    content.notify = lambda text, key: alerts.append((text, key))
    return content, state, client, backend, transcriber, alerts


# ------------------------------------------------------------------ the audio check in next_video

def test_clean_audio_lets_the_video_through_and_cleans_up(app, tmp_path):
    content, state, _, backend, transcriber, alerts = setup(app, tmp_path)
    assert content.next_video("kalima_sawaa", "short").id == "m3"
    assert transcriber.calls == ["m3"] and not alerts
    assert list((tmp_path / "media").glob("*.screen*")) == []             # the downloaded copy is deleted
    assert content.next_video("kalima_sawaa", "short").id == "m3"
    assert transcriber.calls == ["m3"]                                     # cached: not transcribed twice


def test_high_risk_speech_holds_the_video_back_and_the_next_one_is_used(app, tmp_path):
    content, _, _, backend, transcriber, alerts = setup(app, tmp_path, texts={"m3": INSULT})
    assert content.next_video("kalima_sawaa", "short").id == "m2"
    assert transcriber.calls == ["m3", "m2"]
    assert len(alerts) == 1 and alerts[0][1] == "guard-hold:kalima_sawaa:m3"
    assert "audio check" in alerts[0][0] and "حثالة" in alerts[0][0]
    assert content.remaining("kalima_sawaa", "short") == 2                  # held one is out of the queue
    content.next_video("kalima_sawaa", "short")
    assert transcriber.calls == ["m3", "m2"]


def test_a_title_held_back_is_never_downloaded(app, tmp_path):
    content, _, client, backend, transcriber, _ = setup(app, tmp_path)
    backend.titles["عنوان 3"] = {"title": "", "risk": "high", "reasons": ["insult"]}
    assert content.next_video("kalima_sawaa", "short").id == "m2"
    assert transcriber.calls == ["m2"]


def test_transcription_failure_posts_nothing_and_caches_nothing(app, tmp_path):
    content, state, _, _, transcriber, _ = setup(app, tmp_path, fail=["m3"])
    with pytest.raises(ContentError, match="audio check failed: whisper crashed"):
        content.next_video("kalima_sawaa", "short")
    assert state.kv_get("audio:kalima_sawaa:m3") is None
    assert list((tmp_path / "media").glob("*.screen*")) == []
    transcriber.fail.clear()
    assert content.next_video("kalima_sawaa", "short").id == "m3"           # the next run simply retries


def test_a_model_failure_in_the_audio_check_posts_nothing(app, tmp_path):
    content, state, _, backend, *_ = setup(app, tmp_path)
    complete = backend.complete

    def fails_on_speech(system, user, max_tokens=500):
        if "transcript" in json.loads(user):
            raise BackendError("HTTP 500", retryable=True)
        return complete(system, user, max_tokens)

    backend.complete = fails_on_speech
    content.guard_for("kalima_sawaa").sleep = lambda s: None
    with pytest.raises(ContentError, match="audio check failed: audio check failed: HTTP 500"):
        content.next_video("kalima_sawaa", "short")
    assert state.kv_get("audio:kalima_sawaa:m3") is None


def test_at_most_three_unchecked_videos_are_transcribed_per_call(app, tmp_path):
    texts = {f"m{i}": INSULT for i in range(1, 7)}
    content, _, _, _, transcriber, _ = setup(app, tmp_path, texts=texts, n=6)
    with pytest.raises(ContentError, match="3 videos in a row"):
        content.next_video("kalima_sawaa", "short")
    assert transcriber.calls == ["m6", "m5", "m4"]
    with pytest.raises(ContentError, match="all held back"):
        content.next_video("kalima_sawaa", "short")                         # continues with the following ones
    assert transcriber.calls == ["m6", "m5", "m4", "m3", "m2", "m1"]


def test_dry_runs_and_other_streams_skip_the_audio_check(app, tmp_path):
    content, _, _, _, transcriber, _ = setup(app, tmp_path, dry_run=True)
    assert content.next_video("kalima_sawaa", "short").id == "m3"
    assert transcriber.calls == []
    content, _, _, _, transcriber, _ = setup(app, tmp_path / "b", audio=AudioCheckConfig(streams=["long"]))
    assert content.next_video("kalima_sawaa", "short").id == "m3"
    assert transcriber.calls == []


def test_renaming_a_file_checks_its_audio_again(app, tmp_path):
    content, _, client, _, transcriber, _ = setup(app, tmp_path, texts={"m1": INSULT}, n=1)
    with pytest.raises(ContentError, match="held back"):
        content.next_video("kalima_sawaa", "short")
    client.files[SHORT][0].name = "اسم جديد.mp4"
    content._videos.clear()
    transcriber.texts["m1"] = CALM
    assert content.next_video("kalima_sawaa", "short").id == "m1"
    assert len(transcriber.calls) == 2


# ------------------------------------------------------------------ prescreen

def test_prescreen_checks_the_next_videos_in_order_and_stores_verdicts(app, tmp_path):
    content, state, _, backend, transcriber, alerts = setup(app, tmp_path, texts={"m2": INSULT}, n=4)
    stats = content.prescreen("kalima_sawaa", "short", limit=3)
    assert stats == {"checked": 3, "held": 1, "already": 0}
    assert transcriber.calls == ["m4", "m3", "m2"]
    assert [k for _, k in alerts] == ["guard-hold:kalima_sawaa:m2"]
    assert content.prescreen("kalima_sawaa", "short", limit=3) == {"checked": 1, "held": 0, "already": 2}
    assert transcriber.calls == ["m4", "m3", "m2", "m1"]
    before = len(backend.calls)
    assert content.next_video("kalima_sawaa", "short").id == "m4"           # the publisher only reads the cache
    assert len(backend.calls) == before and transcriber.calls.count("m4") == 1


def test_prescreen_stops_at_the_deadline_and_on_failure(app, tmp_path):
    content, *_ = setup(app, tmp_path, n=3)
    assert content.prescreen("kalima_sawaa", "short", limit=3, deadline=0)["checked"] == 0
    content, _, _, _, transcriber, _ = setup(app, tmp_path / "b", fail=["m2"], n=3)
    with pytest.raises(ContentError, match="audio check failed"):
        content.prescreen("kalima_sawaa", "short", limit=3)
    assert transcriber.calls == ["m3", "m2"]


# ------------------------------------------------------------------ judging the transcript

def test_judge_audio_needs_no_model_for_silence_and_parses_replies():
    guard = TitleGuard(backend=Scripted())
    assert guard.judge_audio("t", "  ") == AudioVerdict("low", ("no speech found",), "")
    assert guard.backend.calls == []
    v = guard.judge_audio("t", INSULT)
    assert v.risk == "low" and v.excerpt.startswith("فلان")


def test_audio_reply_parsing():
    v = parse_audio_reply('<think>hmm {"risk": "low"}</think>{"risk": "HIGH", "reasons": ["x"]}', "نص طويل " * 40)
    assert (v.risk, v.reasons) == ("high", ("x",)) and len(v.excerpt) <= 160
    for bad in ("nothing", '{"risk": "maybe"}', "[1]"):
        with pytest.raises(GuardError):
            parse_audio_reply(bad, "x")
    assert AudioVerdict.from_json(v.to_json()) == v


# ------------------------------------------------------------------ backends

class Reply:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body


def test_openai_compatible_backend_request_and_reply():
    seen = {}

    class Session:
        def post(self, url, json=None, headers=None, timeout=None):
            seen.update(url=url, body=json, headers=headers)
            return Reply({"choices": [{"message": {"content": '{"risk": "low", "reasons": []}'}}]})

    out = OpenAICompatBackend("https://api.groq.com/openai/v1/", "gsk", "llama-3.3-70b-versatile", Session()) \
        .complete("SYS", "USER", 300)
    assert out == '{"risk": "low", "reasons": []}'
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer gsk"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["temperature"] == 0
    assert seen["body"]["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]


def test_openai_compatible_backend_errors_and_retries():
    class Session:
        def __init__(self, replies):
            self.replies = list(replies)

        def post(self, *a, **kw):
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    with pytest.raises(BackendError) as e:
        OpenAICompatBackend("http://x", "k", "m", Session([Reply({}, 401)])).complete("s", "u")
    assert not e.value.retryable
    with pytest.raises(BackendError) as e:
        OpenAICompatBackend("http://x", "k", "m", Session([Reply({}, 429)])).complete("s", "u")
    assert e.value.retryable
    with pytest.raises(BackendError, match="no message"):
        OpenAICompatBackend("http://x", "k", "m", Session([Reply({"choices": []})])).complete("s", "u")
    sleeps = []
    flaky = Session([requests.ConnectionError(), Reply({"choices": [{"message": {"content": "{\"risk\":\"low\"}"}}]})])
    guard = TitleGuard(backend=OpenAICompatBackend("http://x", "k", "m", flaky), sleep=sleeps.append)
    assert guard.judge_audio("t", INSULT).risk == "low" and sleeps == [2]


def test_local_backend_asks_without_thinking_and_wraps_failures():
    asked = {}

    class Llm:
        def create_chat_completion(self, messages, max_tokens, temperature, response_format):
            asked.update(messages=messages, response_format=response_format, temperature=temperature)
            return {"choices": [{"message": {"content": "{\"risk\": \"low\"}"}}]}

    loads = []
    backend = LocalBackend("repo", "file.gguf", factory=lambda: loads.append(1) or Llm())
    assert backend.complete("SYS", "USER") == '{"risk": "low"}'
    backend.complete("SYS", "USER")
    assert loads == [1]                                                    # the model is loaded once
    assert asked["messages"][1]["content"].endswith("/no_think") and asked["temperature"] == 0.0
    assert asked["response_format"] == {"type": "json_object"}

    def no_llama():
        raise MLDepsError("python package 'llama_cpp' is not installed")

    with pytest.raises(BackendError, match="not installed") as e:
        LocalBackend("r", "f", factory=no_llama).complete("s", "u")
    assert not e.value.retryable

    def broken():
        raise OSError("disk full")

    with pytest.raises(BackendError, match="local model failed: OSError"):
        LocalBackend("r", "f", factory=broken).complete("s", "u")


def test_local_provider_needs_no_secret(app, tmp_path):
    from autopost.content.drive_source import DriveContent
    from autopost.secret_store import Secrets

    from test_drive_source import FakeClient

    cfg = app.brands["kalima_sawaa"].title_guard
    assert cfg.provider == "local" and cfg.api_key_secret is None
    assert "ANTHROPIC_API_KEY" not in app.required_secrets()
    content = DriveContent(app, State(tmp_path / "s.db"), FakeClient(), tmp_path / "m", False, secrets=Secrets({}))
    backend = content.guard_for("kalima_sawaa").backend
    assert isinstance(backend, LocalBackend) and (backend.repo_id, backend.filename) == (
        "Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf")
    assert content.guard_for("kalima_sawaa").backend is backend


# ------------------------------------------------------------------ config

def test_guard_config_validation():
    assert TitleGuardConfig().provider == "local"
    with pytest.raises(ValueError, match="needs `model` and `api_key_secret`"):
        TitleGuardConfig(provider="anthropic")
    with pytest.raises(ValueError, match="base_url"):
        TitleGuardConfig(provider="openai_compatible", model="m", api_key_secret="K")
    groq = TitleGuardConfig(provider="openai_compatible", model="m", api_key_secret="GROQ_API_KEY",
                            base_url="https://api.groq.com/openai/v1")
    assert groq.audio_for("short") is None
    cfg = TitleGuardConfig(audio=AudioCheckConfig(streams=["short"]))
    assert cfg.audio_for("short") and cfg.audio_for("long") is None
    assert TitleGuardConfig(audio=AudioCheckConfig(enabled=False)).audio_for("short") is None


def test_the_real_config_checks_the_audio_of_the_short_stream(app):
    cfg = app.brands["kalima_sawaa"].title_guard
    assert cfg.audio_for("short") is not None and cfg.audio_for("long") is None
    assert cfg.audio.whisper_model == "large-v3"


# ------------------------------------------------------------------ Whisper wrapper

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")


def tone_video(path, seconds=5):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-f", "lavfi", "-i", f"color=c=blue:s=64x64:d={seconds}", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@needs_ffmpeg
def test_extract_audio_cuts_the_first_seconds_as_16k_mono(tmp_path):
    out = extract_audio(tone_video(tmp_path / "v.mp4"), tmp_path / "a.wav", seconds=2)
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)
        assert 1.9 <= w.getnframes() / w.getframerate() <= 2.1


@needs_ffmpeg
def test_video_without_audio_is_a_transcribe_error(tmp_path):
    silent = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)], check=True)
    with pytest.raises(TranscribeError, match="no usable audio"):
        extract_audio(silent, tmp_path / "a.wav", 5)


@needs_ffmpeg
def test_transcriber_joins_segments_loads_once_and_removes_the_wav(tmp_path):
    class Seg:
        def __init__(self, text):
            self.text = text

    seen = {}

    class Model:
        def transcribe(self, path, **kw):
            seen.update(kw)
            seen["exists"] = getattr(path, "dtype", None) is not None and path.dtype.name == "float32" and len(path) > 1000
            return iter([Seg(" مرحبا  بكم "), Seg(""), Seg("في الحلقة")]), object()

    loads = []
    t = Transcriber(WhisperSettings(max_seconds=3), model_factory=lambda s: loads.append(s.model) or Model())
    video = tone_video(tmp_path / "v.mp4")
    assert t.transcribe(video, tmp_path / "w") == "مرحبا بكم في الحلقة"
    t.transcribe(video, tmp_path / "w")
    assert loads == ["large-v3"] and seen["exists"] and seen["language"] == "ar" and seen["vad_filter"] is True
    assert list((tmp_path / "w").glob("*.wav")) == []


@needs_ffmpeg
def test_transcriber_wraps_model_failures(tmp_path):
    class Model:
        def transcribe(self, *a, **kw):
            raise RuntimeError("out of memory")

    t = Transcriber(model_factory=lambda s: Model())
    with pytest.raises(TranscribeError, match="Whisper failed on v.mp4: RuntimeError"):
        t.transcribe(tone_video(tmp_path / "v.mp4"), tmp_path / "w")
    assert list((tmp_path / "w").glob("*.wav")) == []


# ------------------------------------------------------------------ state merge for the prescreen job

def test_merge_keeps_theirs_for_other_keys_when_a_prefix_is_given(tmp_path):
    for name in ("theirs", "ours"):
        s = State(tmp_path / f"{name}.db")
        s.kv_set("daily-summary", "2026-11-08" if name == "theirs" else "2026-11-06")
        s.close()
    ours = State(tmp_path / "ours.db")
    ours.kv_set("audio:kalima_sawaa:m1:abc", "{}")
    ours.kv_set("only-ours", "x")
    ours.close()

    merge_states(tmp_path / "theirs.db", tmp_path / "ours.db", tmp_path / "out.db", kv_ours_prefixes=("guard:", "audio:"))
    m = State(tmp_path / "out.db")
    assert m.kv_get("daily-summary") == "2026-11-08"          # a stale copy never wins
    assert m.kv_get("audio:kalima_sawaa:m1:abc") == "{}" and m.kv_get("only-ours") == "x"

    merge_states(tmp_path / "theirs.db", tmp_path / "ours.db", tmp_path / "out2.db")
    assert State(tmp_path / "out2.db").kv_get("daily-summary") == "2026-11-06"    # the old rule: ours wins
