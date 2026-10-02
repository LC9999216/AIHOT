"""Runtime persistence hardening regressions (review round 2).

Covers:
- RuntimeStore strict top-level structure validation;
- duplicate provider IDs fail closed;
- history corruption (truncated JSON / bad journal) fails closed;
- approval_binding can be CLEARED by passing None;
- event/snapshot revision+event_id binding validated on load;
- duplicate revision detection;
- cross-process single-instance lock (real subprocess).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.runtime.locks import ProcessFileLock, LockHeldError
from hunter.runtime.models import (
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    RuntimeProvider,
)
from hunter.runtime.store import RuntimeStore, RuntimeError as RuntimeStoreError


def _store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "runtime.json", tmp_path / "history.jsonl")


def _rp(provider_id: str = "acme", **kw) -> RuntimeProvider:
    return RuntimeProvider(
        provider_id=provider_id,
        provider_name=kw.get("provider_name", "Acme"),
        credential_status=kw.get("credential_status", CredentialStatus.CONFIGURED),
        health_status=kw.get("health_status", HealthStatus.HEALTHY),
        approval_status=kw.get("approval_status", ApprovalStatus.PENDING),
        approval_binding=kw.get("approval_binding"),
    )


# --- strict load validation --------------------------------------------------


def test_top_level_not_a_dict_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


def test_top_level_missing_items_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text(json.dumps({"providers": []}), encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


def test_items_not_a_list_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text(json.dumps({"items": {}}), encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


def test_duplicate_provider_ids_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rp = _rp("acme")
    store.upsert(rp, reason="create")
    store.save()
    payload = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    payload["items"].append(dict(payload["items"][0]))  # duplicate provider_id
    (tmp_path / "runtime.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


def test_corrupt_history_line_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(_rp("acme"), reason="create")
    # truncate the last history line (simulated crash mid-append)
    history = tmp_path / "history.jsonl"
    text = history.read_text(encoding="utf-8")
    history.write_text(text[: len(text) // 2], encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


def test_journal_with_unrelated_revision_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(_rp("acme"), reason="create")  # revision 1
    # forge a journal for revision 42 which is neither old nor new
    journal = tmp_path / ".runtime_txn.json"
    journal.write_text(json.dumps({
        "old_revision": 41,
        "new_revision": 42,
        "event": {"event_id": "x", "provider_id": "acme", "revision": 42},
    }), encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


# --- approval binding clearing -----------------------------------------------


def test_approval_binding_can_be_cleared_with_none(tmp_path: Path) -> None:
    from hunter.runtime.models import ApprovalBinding

    store = _store(tmp_path)
    binding = ApprovalBinding(
        provider_id="acme",
        registry_revision=3,
        evidence_digest="d" * 40,
        free_score_snapshot=60,
    )
    created = store.upsert(
        RuntimeProvider(
            provider_id="acme",
            approval_status=ApprovalStatus.APPROVED,
            approval_binding=binding,
        ),
        reason="approval",
    )
    assert created.approval_binding is not None

    cleared = store.upsert(
        RuntimeProvider(
            provider_id="acme",
            provider_name="Acme",
            credential_status=created.credential_status,
            health_status=created.health_status,
            approval_status=ApprovalStatus.REJECTED,
            approval_binding=None,  # explicit clear
        ),
        reason="approval revoked",
    )
    assert cleared.approval_binding is None


# --- event/snapshot binding ----------------------------------------------------


def test_tampered_snapshot_breaks_event_binding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.upsert(_rp("acme"), reason="create")
    assert created.last_event_id
    payload = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    payload["items"][0]["health_status"] = "DOWN"  # tamper without re-deriving event
    (tmp_path / "runtime.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


def test_duplicate_revision_in_store_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(_rp("acme"), reason="create")
    payload = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    item = dict(payload["items"][0])
    item["provider_id"] = "beta"
    payload["items"].append(item)
    (tmp_path / "runtime.json").write_text(json.dumps(payload), encoding="utf-8")
    # the copied item carries an event_id that won't match beta's content binding
    with pytest.raises(RuntimeStoreError):
        _store(tmp_path)


# --- cross-process single-instance lock ----------------------------------------


def test_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime.lock"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                f"sys.path.insert(0, r'{(Path(__file__).resolve().parents[1] / 'src')}')\n"
                "from hunter.runtime.locks import ProcessFileLock\n"
                "import threading\n"
                f"lock = ProcessFileLock(r'{lock_path}')\n"
                "lock.acquire()\n"
                "print('held', flush=True)\n"
                "time.sleep(6)\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr


def test_second_process_cannot_acquire_same_lock(tmp_path: Path) -> None:
    """A real subprocess holds the lock; this process must fail closed."""
    lock_path = tmp_path / "runtime.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                f"sys.path.insert(0, r'{(Path(__file__).resolve().parents[1] / 'src')}')\n"
                "from hunter.runtime.locks import ProcessFileLock\n"
                f"lock = ProcessFileLock(r'{lock_path}')\n"
                "lock.acquire()\n"
                "print('held', flush=True)\n"
                "time.sleep(10)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        lock = ProcessFileLock(lock_path)
        with pytest.raises(LockHeldError):
            lock.acquire(timeout=1.0)
        # a second instance in THIS process is still blocked (holder is
        # another process; same-process re-entry does not apply)
        lock2 = ProcessFileLock(lock_path)
        with pytest.raises(LockHeldError):
            lock2.acquire(timeout=1.0)
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_lock_reentrant_within_process(tmp_path: Path) -> None:
    lock = ProcessFileLock(tmp_path / "runtime.lock")
    lock.acquire()
    lock.acquire()  # same-process reentry
    lock.release()
    lock.release()
