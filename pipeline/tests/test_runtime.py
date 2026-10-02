"""Stage 2 tests: Runtime store, outbox, crash recovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.runtime.models import (
    ActualPoolStatus,
    ApprovalBinding,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
)
from hunter.runtime.outbox import OutboxMessage, OutboxStore
from hunter.runtime.store import RuntimeStore, RuntimeError, make_runtime_event_id


# =============================================================================
# RuntimeProvider model
# =============================================================================


def test_runtime_provider_defaults() -> None:
    rp = RuntimeProvider(provider_id="test-provider")
    assert rp.credential_status is CredentialStatus.NOT_CONFIGURED
    assert rp.health_status is HealthStatus.UNKNOWN
    assert rp.actual_pool_status is ActualPoolStatus.UNKNOWN
    assert rp.approval_status is ApprovalStatus.PENDING
    assert rp.revision == 0


def test_protocol_result_defaults() -> None:
    pr = ProtocolResult()
    assert pr.chat == "unchecked"
    assert pr.responses == "unchecked"
    assert pr.streaming == "unchecked"
    assert pr.tools == "unchecked"
    assert pr.checked_at is None
    assert pr.all_pass() is False


def test_protocol_result_all_pass() -> None:
    pr = ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass")
    assert pr.all_pass() is True


# =============================================================================
# RuntimeStore
# =============================================================================


def test_store_create_and_read(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    assert store.list_providers() == []


def test_store_upsert_creates(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    rp = RuntimeProvider(provider_id="acme", provider_name="Acme AI")
    stored = store.upsert(rp, reason="initial import")
    assert stored.revision == 1
    assert stored.last_event_id
    assert stored.provider_id == "acme"
    # verify reload
    store2 = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    loaded = store2.get_provider("acme")
    assert loaded is not None
    assert loaded.revision == 1
    assert loaded.provider_name == "Acme AI"


def test_store_upsert_noop_does_not_change_revision(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    rp = RuntimeProvider(provider_id="acme", provider_name="Acme")
    stored = store.upsert(rp, reason="create")
    assert stored.revision == 1

    stored2 = store.upsert(rp, reason="identical")
    assert stored2.revision == 1  # no revision bump


def test_store_upsert_updates_and_increments_revision(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    rp = RuntimeProvider(provider_id="acme", provider_name="Acme")
    store.upsert(rp, reason="create")
    updated = RuntimeProvider(
        provider_id="acme",
        provider_name="Acme",
        health_status=HealthStatus.HEALTHY,
    )
    stored = store.upsert(updated, reason="health check")
    assert stored.revision == 2
    assert stored.health_status is HealthStatus.HEALTHY


def test_store_history_events(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    store.upsert(RuntimeProvider(provider_id="acme", provider_name="Acme"), reason="create")
    history_lines = [l for l in store.history_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(history_lines) == 1
    event = json.loads(history_lines[0])
    assert event["event_type"] == "runtime.provider_created"
    assert event["provider_id"] == "acme"
    assert event["event_id"]


def test_store_no_duplicate_history_on_noop(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    rp = RuntimeProvider(provider_id="acme", provider_name="Acme")
    store.upsert(rp, reason="create")
    store.upsert(rp, reason="noop")
    lines = [l for l in store.history_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1  # no new event for no-op


def test_store_remove(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    store.upsert(RuntimeProvider(provider_id="acme"), reason="create")
    assert store.get_provider("acme") is not None
    assert store.remove("acme") is True
    assert store.get_provider("acme") is None
    assert store.remove("nonexistent") is False


def test_store_revision_monotonic_after_update(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    for i in range(3):
        rp = RuntimeProvider(
            provider_id="acme",
            health_status=HealthStatus.HEALTHY if i % 2 == 0 else HealthStatus.DOWN,
        )
        stored = store.upsert(rp, reason=f"update {i}")
        assert stored.revision == i + 1


# =============================================================================
# Crash recovery
# =============================================================================


def test_recover_pending_transaction_full(tmp_path: Path) -> None:
    """Journal with new revision applied; missing history event is appended."""
    providers = tmp_path / "runtime_providers.json"
    history = tmp_path / "runtime_history.jsonl"
    journal = tmp_path / ".runtime_txn.json"

    class _CrashAfterHistory(Exception):
        pass

    def _crash_append_history(event):
        # Simulate: journal written, snapshot replaced, then crash BEFORE the
        # history append would have completed -> truncate mid-append.
        with history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event)[:10])  # truncated line
        raise _CrashAfterHistory()

    store = RuntimeStore(providers, history)
    store.upsert(RuntimeProvider(provider_id="acme"), reason="create")
    assert store.get_provider("acme").revision == 1

    original_append = store._append_history
    store._append_history = _crash_append_history
    try:
        store.upsert(
            RuntimeProvider(provider_id="acme", credential_status="CONFIGURED"),
            reason="crash test",
        )
    except _CrashAfterHistory:
        pass
    finally:
        store._append_history = original_append
    # Journal must still exist (crash before its removal)
    assert journal.is_file()

    # Reopening must fail closed on the truncated history line.
    with pytest.raises(RuntimeError):
        RuntimeStore(providers, history)

    # Repair the truncated line (manual recovery) and reopen: the snapshot
    # already contains the new revision, so the journal is replayed cleanly.
    history.write_text("", encoding="utf-8")
    store2 = RuntimeStore(providers, history)
    assert store2.get_provider("acme").revision == 2
    assert not journal.exists()


def test_recover_discards_unapplied_journal(tmp_path: Path) -> None:
    """Journal for an old revision that was never applied is discarded."""
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    store.upsert(RuntimeProvider(provider_id="acme", revision=1), reason="create")
    journal = tmp_path / ".runtime_txn.json"
    event = {
        "event_id": "unapplied",
        "provider_id": "acme",
        "revision": 2,
        "event_type": "provider.updated",
        "changed_fields": ["test"],
        "reason": "unapplied",
        "source_metadata": {},
        "timestamp": "2026-09-12T00:00:00+00:00",
        "content": {"provider_id": "acme", "revision": 2},
    }
    store._write_journal(journal, old_revision=1, new_revision=2, event=event)
    store2 = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    assert store2.get_provider("acme").revision == 1
    assert not journal.exists()


def test_recover_corrupt_journal_fails_closed(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")
    journal = tmp_path / ".runtime_txn.json"
    journal.write_text("not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")


# =============================================================================
# Outbox
# =============================================================================


def test_outbox_create_and_save(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "notification_outbox.json")
    assert outbox.pending() == []
    assert outbox.sent() == []


def test_outbox_enqueue_and_mark_sent(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "notification_outbox.json")
    msg = OutboxMessage(
        event_id="evt-1",
        provider_id="acme",
        provider_name="Acme AI",
        event_type="NEW_HIGH_VALUE",
        title="New Provider",
        body="Acme AI is now available",
    )
    assert outbox.enqueue(msg) is True
    # duplicate is rejected
    assert outbox.enqueue(msg) is False
    assert len(outbox.pending()) == 1
    assert outbox.mark_sent("evt-1") is True
    assert len(outbox.pending()) == 0
    assert len(outbox.sent()) == 1


def test_outbox_persistence(tmp_path: Path) -> None:
    path = tmp_path / "notification_outbox.json"
    outbox = OutboxStore(path)
    msg = OutboxMessage(
        event_id="evt-1",
        provider_id="acme",
        provider_name="Acme",
        event_type="FREE_TIER_CHANGED",
        title="Changed",
        body="Tier changed",
    )
    outbox.enqueue(msg)
    outbox2 = OutboxStore(path)
    assert len(outbox2.pending()) == 1
    assert outbox2.has_event("evt-1") is True
    assert outbox2.has_event("nonexistent") is False


def test_outbox_no_secret_in_body(tmp_path: Path) -> None:
    """Verify the outbox body does not contain secrets (policy check)."""
    outbox = OutboxStore(tmp_path / "notification_outbox.json")
    msg = OutboxMessage(
        event_id="evt-sec",
        provider_id="acme",
        provider_name="Acme",
        event_type="NEW_HIGH_VALUE",
        title="Acme",
        body="Provider Acme AI is FREE_CONFIRMED. Signup: https://acme.ai/signup",
    )
    outbox.enqueue(msg)
    raw = outbox.path.read_text(encoding="utf-8")
    assert "api_key" not in raw.lower()
    assert "token" not in raw.lower()
    assert "sk-" not in raw
