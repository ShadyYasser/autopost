"""Text burned into a reel's picture, read with Tesseract OCR (Arabic), one frame per second.

Used to find the verse cards the creator already put on screen ("(يو 13: 25)" and the verse above it), so our
own card follows exactly the verse he meant, at the moment he shows it. Tesseract is installed on demand
(apt, like ffmpeg); if that fails, nothing is read and no on-screen card is found.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .media import _sudo

log = logging.getLogger(__name__)


class ScreenTextError(Exception):
    pass


def tesseract_ready() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "ara" in (out.stdout + out.stderr).split()


def ensure_tesseract() -> None:
    if tesseract_ready():
        return
    if not (shutil.which("sudo") and shutil.which("apt-get")):
        raise ScreenTextError("tesseract (Arabic) is missing and cannot be installed on this machine")
    _sudo(["apt-get", "update", "-qq"])
    result = _sudo(["apt-get", "install", "-y", "-qq", "--no-install-recommends", "tesseract-ocr",
                    "tesseract-ocr-ara"])
    if result.returncode != 0 or not tesseract_ready():
        raise ScreenTextError(f"apt install of tesseract failed: {(result.stderr or result.stdout).strip()[-300:]}")
    log.info("tesseract (Arabic) installed")


def read_screen(video: Path, max_seconds: int = 180, fps: float = 1.0, timeout: int = 900) -> list[tuple[float, str]]:
    """[(second, text)] for one frame per second of the first `max_seconds` (frames with no text left out)."""
    ensure_tesseract()
    out: list[tuple[float, str]] = []
    with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
        frames = Path(tmp)
        cmd = ["ffmpeg", "-v", "error", "-y", "-t", str(max_seconds), "-i", str(video),
               "-vf", f"fps={fps}", "-q:v", "3", str(frames / "%05d.jpg")]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ScreenTextError("frame extraction timed out") from exc
        if res.returncode != 0:
            raise ScreenTextError(f"frame extraction failed: {res.stderr.strip()[-300:]}")
        for i, frame in enumerate(sorted(frames.glob("*.jpg"))):
            try:
                r = subprocess.run(["tesseract", str(frame), "-", "-l", "ara", "--psm", "6"],
                                   capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                continue
            text = " ".join(r.stdout.split())
            if text:
                out.append((round(i / fps, 2), text))
    log.info("screen text of %s: %d frame(s) with text", video.name, len(out))
    return out
