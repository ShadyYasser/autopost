"""Regressions from 2026-11-26: a state.db swapped by commit_state.sh during a run, and a model reply without "risk"."""
import shutil
import sqlite3

import pytest

from autopost.state import State
from autopost.title_guard import GuardError, TitleGuard, parse_audio_reply


def _swap_file(path):
    """What scripts/commit_state.sh does on a push conflict: the file at `path` becomes a new file (new inode)."""
    copy = path.with_suffix(".merged")
    shutil.copy(path, copy)
    path.unlink()
    copy.rename(path)


def test_connection_on_a_swapped_file_is_readonly_and_reopen_fixes_it(tmp_path):
    db = tmp_path / "state.db"
    state = State(db)
    state.kv_set("a", "1")
    _swap_file(db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        state.kv_set("b", "2")
    state.reopen()
    state.kv_set("b", "2")
    assert state.kv_get("a") == "1" and state.kv_get("b") == "2"
    state.close()


def test_risk_key_is_found_whatever_its_case_or_name():
    assert parse_audio_reply('{"Risk": "Medium ", "reasons": []}', "some words here").risk == "medium"
    assert parse_audio_reply('{"risk_level": "low"}', "some words here").risk == "low"
    with pytest.raises(GuardError):
        parse_audio_reply('{"reasons": ["x"]}', "some words here")


class _Backend:
    def __init__(self, replies):
        self.replies, self.users = list(replies), []

    def complete(self, system, user, max_tokens=500):
        self.users.append(user)
        return self.replies.pop(0)


def test_unusable_reply_is_asked_again_with_a_reminder():
    backend = _Backend(['{"reasons": ["no risk key"]}', '{"risk": "low", "title": "عنوان", "reasons": []}'])
    guard = TitleGuard(context="", sleep=lambda s: None, backend=backend)
    assert guard.judge("عنوان").risk == "low"
    assert len(backend.users) == 2 and '"risk"' in backend.users[1]
