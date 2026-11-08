"""Speech to text for the audio check: ffmpeg cuts the first seconds of the reel into a 16 kHz mono wav,
faster-whisper (Whisper large-v3 by default) turns it into text on the CPU of the GitHub runner.

The model is loaded once per process, on first use. Any trouble raises TranscribeError, which the audio
check treats like an API failure: the video is not posted and the slot is retried on the next run.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import mldeps
from .media import ensure_ffmpeg

log = logging.getLogger(__name__)


class TranscribeError(Exception):
    pass


@dataclass(frozen=True)
class WhisperSettings:
    model: str = "large-v3"
    compute_type: str = "int8"        # int8 keeps large-v3 at ~3 GB of RAM on a CPU
    language: str = "ar"
    max_seconds: int = 180            # only the first N seconds are transcribed
    beam_size: int = 1                # greedy: several times faster, enough to judge what is said
    timeout_seconds: int = 30 * 60    # ffmpeg + decoding budget for one video


def extract_audio(src: Path, out: Path, seconds: int, timeout: int = 300) -> Path:
    """First `seconds` of the audio of `src` as 16 kHz mono wav. Raises TranscribeError when there is none."""
    ensure_ffmpeg()
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(out)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out.unlink(missing_ok=True)
        raise TranscribeError(f"ffmpeg (audio) timed out after {timeout}s") from exc
    if result.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        out.unlink(missing_ok=True)
        raise TranscribeError(f"no usable audio track: {result.stderr.strip()[-200:] or 'empty output'}")
    return out


class Transcriber:
    def __init__(self, settings: WhisperSettings | None = None, model_factory: Callable[[WhisperSettings], object] | None = None):
        self.settings = settings or WhisperSettings()
        self._factory = model_factory or self._load
        self._model = None

    @staticmethod
    def _load(settings: WhisperSettings):
        try:
            module = mldeps.ensure("faster_whisper")
            log.info("loading Whisper %s (%s) - the first run downloads the model", settings.model,
                     settings.compute_type)
            return module.WhisperModel(settings.model, device="cpu", compute_type=settings.compute_type,
                                       cpu_threads=os.cpu_count() or 4)
        except mldeps.MLDepsError as exc:
            raise TranscribeError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - download / load problems of any kind
            raise TranscribeError(f"cannot load Whisper {settings.model}: {type(exc).__name__}: {exc}") from exc

    def transcribe(self, video: Path, work_dir: Path) -> str:
        """The spoken text of the first `max_seconds` of `video` ('' when nothing is said)."""
        s = self.settings
        work_dir.mkdir(parents=True, exist_ok=True)
        wav = work_dir / f"{video.stem}.audio.wav"
        try:
            extract_audio(video, wav, s.max_seconds)
            if self._model is None:
                self._model = self._factory(s)
            try:
                segments, _info = self._model.transcribe(
                    str(wav), language=s.language, beam_size=s.beam_size, vad_filter=True,
                    condition_on_previous_text=False)
                text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
            except TranscribeError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TranscribeError(f"Whisper failed on {video.name}: {type(exc).__name__}: {exc}") from exc
        finally:
            wav.unlink(missing_ok=True)
        text = " ".join(text.split())
        log.info("transcript of %s: %d characters", video.name, len(text))
        return text
