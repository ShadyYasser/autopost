from pathlib import Path

import pytest

from autopost.config import load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    return load_config(ROOT / "config")
