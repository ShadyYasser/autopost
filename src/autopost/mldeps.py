"""Heavy optional packages (speech-to-text and the local language model).

They live in requirements-ml.txt, not requirements.txt: most scheduled runs post nothing and must stay
fast. The first time a check needs one of them in a run, it is installed here (pip's cache is restored
by the workflow, so a later run only unpacks the cached wheels).
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements-ml.txt"
_installed = False


class MLDepsError(Exception):
    pass


def ensure(module: str):
    """Import `module`, installing requirements-ml.txt first when it is missing. Returns the module."""
    global _installed
    try:
        return importlib.import_module(module)
    except ImportError:
        pass
    if _installed or not REQUIREMENTS.exists():
        raise MLDepsError(f"python package {module!r} is not installed (see requirements-ml.txt)")
    log.info("installing %s (first use in this run)", REQUIREMENTS.name)
    env = dict(os.environ)
    # a wheel built on one runner CPU is reused on another one: no CPU-specific instructions
    env.setdefault("CMAKE_ARGS", "-DGGML_NATIVE=OFF")
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)],
                                capture_output=True, text=True, timeout=45 * 60, env=env)
    except subprocess.TimeoutExpired as exc:
        raise MLDepsError("pip install of requirements-ml.txt timed out") from exc
    _installed = True
    if result.returncode != 0:
        raise MLDepsError(f"pip install of requirements-ml.txt failed: {result.stderr.strip()[-400:]}")
    importlib.invalidate_caches()
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise MLDepsError(f"python package {module!r} is still missing after installing") from exc
