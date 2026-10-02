"""Offline tests for init_live_state.py."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _seed_source(tmp_path):
    """Create a minimal data/ directory in tmp_path for the source worktree."""
    (tmp_path / "data").mkdir()
    for name in ("providers.json", "candidates.json", "evidence.json", "history.jsonl"):
        (tmp_path / "data" / name).write_text(f"source_{name}")
    return tmp_path


def test_create_missing_directory(tmp_path):
    from scripts.init_live_state import init
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    result = init(worktree, state, mode="create")
    assert (state / "data").is_dir()
    assert (state / "pool/staging").is_dir()
    assert (state / "pool/production").is_dir()
    assert (state / "clients").is_dir()
    for name in ("providers.json", "candidates.json", "evidence.json", "history.jsonl"):
        assert (state / "data" / name).is_file()
    assert json.loads(result)["copied"] == [name for name in ("providers.json", "candidates.json", "evidence.json", "history.jsonl")]


def test_create_quoted_json_copied_is_array(tmp_path):
    from scripts.init_live_state import init
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    result = json.loads(init(worktree, state, mode="create"))
    assert isinstance(result["copied"], list)
    assert len(result["copied"]) == 4


def test_existing_hash_match_is_noop(tmp_path):
    from scripts.init_live_state import init
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    init(worktree, state, mode="create")
    before_mtime = (state / "data/providers.json").stat().st_mtime_ns
    import time
    time.sleep(0.01)
    result = json.loads(init(worktree, state, mode="create"))
    after_mtime = (state / "data/providers.json").stat().st_mtime_ns
    assert result["copied"] == []
    assert before_mtime == after_mtime


def test_hash_mismatch_does_not_overwrite(tmp_path):
    from scripts.init_live_state import init
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    init(worktree, state, mode="create")
    (state / "data/providers.json").write_text("DIFFERENT")
    result = json.loads(init(worktree, state, mode="create"))
    assert (state / "data/providers.json").read_text() == "DIFFERENT"
    assert "copied" in result
    assert "providers.json" not in [str(c) for c in result["copied"]]


def test_history_append_not_truncated(tmp_path):
    from scripts.init_live_state import init
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    init(worktree, state, mode="create")
    (state / "data/history.jsonl").write_text("LIVE ENTRY\n")
    import time
    time.sleep(0.02)
    (worktree / "data/history.jsonl").write_text("NEW REPO ENTRY\n")
    result = json.loads(init(worktree, state, mode="create"))
    assert (state / "data/history.jsonl").read_text() == "LIVE ENTRY\n"


def test_interrupted_copy_does_not_leave_partial_file(tmp_path, monkeypatch):
    import scripts.init_live_state as ils
    def broken_copy(src, dst):
        raise OSError("fixture: transfer interrupted")
    monkeypatch.setattr(ils, "_copy_file", broken_copy)
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    result = json.loads(ils.init(worktree, state, mode="create"))
    assert any("copy_failed" in e for e in result.get("errors", []))
    # The partial destination file must not exist (init cleans up).
    assert not (state / "data/providers.json").is_file()


def test_verify_mode_does_not_create(tmp_path):
    from scripts.init_live_state import init
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    state.mkdir()
    result = init(worktree, state, mode="verify")
    data = json.loads(result)
    assert data["copied"] == []
    assert data["missing"] == ["providers.json", "candidates.json", "evidence.json", "history.jsonl"]


def test_secure_permissions_applied_to_new_directories(tmp_path, monkeypatch):
    from scripts import init_live_state
    import src.hunter.pool_permissions as perm_mod
    calls = []
    monkeypatch.setattr("scripts.init_live_state.secure_new_secret_directory", lambda p: calls.append(p))
    monkeypatch.setattr("scripts.init_live_state.verify_secret_directory", lambda p: True)
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    init_live_state.init(worktree, state, mode="create")
    dir_calls = [p for p in calls if str(p).endswith("production")]
    assert len(dir_calls) == 1, f"production dir should be hardened, got {calls}"


def test_existing_insecure_directory_fails_verify(tmp_path, monkeypatch):
    from scripts import init_live_state
    from src.hunter.pool_permissions import verify_secret_directory, secure_new_secret_directory
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
def test_existing_insecure_directory_fails_verify(tmp_path, monkeypatch):
    import scripts.init_live_state as ils
    worktree = _seed_source(tmp_path)
    state = tmp_path / "live-state"
    ils.init(worktree, state, mode="create")
    monkeypatch.setattr(ils, "verify_secret_directory", lambda p: False)
    result = json.loads(ils.init(worktree, state, mode="verify"))
    assert "permissions_unsafe" in str(result.get("errors", ""))
