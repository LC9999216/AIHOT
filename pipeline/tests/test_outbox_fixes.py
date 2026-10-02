"""Outbox consumer / retry / idempotency regressions (review round 2, 六.6/六.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hunter.runtime.notify import (
    FeishuAdapter,
    NotificationError,
    OutboxConsumer,
    build_feishu_payload,
)
from hunter.runtime.outbox import OutboxMessage, OutboxStore


def _msg(event_id: str = "evt-1", **kw) -> OutboxMessage:
    return OutboxMessage(
        event_id=event_id,
        provider_id=kw.get("provider_id", "acme"),
        provider_name=kw.get("provider_name", "Acme AI"),
        event_type=kw.get("event_type", "POOL_SUSPENDED"),
        title=kw.get("title", "Acme suspended"),
        body=kw.get("body", "Provider Acme AI was suspended from production."),
        timestamp=kw.get("timestamp", datetime(2026, 9, 12, tzinfo=timezone.utc)),
    )


# --- structured, secret-free payloads -----------------------------------------


def test_secrets_rejected_in_outbox_messages(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "outbox.json")
    with pytest.raises(ValueError):
        outbox.enqueue(_msg(body="key sk-abc123secretvalue leaked"))
    with pytest.raises(ValueError):
        outbox.enqueue(_msg(title="Bearer ghp_supersecret"))
    with pytest.raises(ValueError):
        outbox.enqueue(_msg(body="Authorization: Bearer xyz"))
    assert outbox.pending() == []


def test_unknown_event_type_rejected(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "outbox.json")
    with pytest.raises(ValueError):
        outbox.enqueue(_msg(event_type="TOTALLY_ARBITRARY"))
    assert outbox.pending() == []


def test_feishu_payload_built_from_structured_fields_only() -> None:
    msg = _msg()
    payload = build_feishu_payload(msg)
    # Documented request fields only; context is embedded in the text body.
    assert set(payload) == {"msg_type", "content"}
    assert payload["msg_type"] == "text"
    assert msg.event_type in payload["content"]["text"]
    assert msg.provider_id in payload["content"]["text"]


# --- consumer: retry + idempotency --------------------------------------------


class FakeFeishuAdapter(FeishuAdapter):
    """Fully mocked Feishu delivery adapter."""

    def __init__(self, failures_before_success: int = 0):
        self.failures_before_success = failures_before_success
        self.calls: list[str] = []

    def send(self, message: OutboxMessage) -> None:
        self.calls.append(message.event_id)
        if len(self.calls) <= self.failures_before_success:
            raise NotificationError("feishu_unavailable")


def test_consumer_marks_sent_after_success(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "outbox.json")
    outbox.enqueue(_msg("evt-1"))
    adapter = FakeFeishuAdapter()
    consumer = OutboxConsumer(outbox, adapter, clock=lambda: datetime(2026, 9, 12, tzinfo=timezone.utc))
    result = consumer.process_once()
    assert result.sent == ["evt-1"]
    assert result.failed == []
    assert [m.event_id for m in outbox.sent()] == ["evt-1"]
    assert outbox.pending() == []


def test_consumer_retries_failed_send_with_backoff(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "outbox.json")
    outbox.enqueue(_msg("evt-1"))
    adapter = FakeFeishuAdapter(failures_before_success=1)
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    consumer = OutboxConsumer(outbox, adapter, clock=lambda: now)
    first = consumer.process_once()
    assert first.failed == ["evt-1"]
    assert outbox.sent() == []
    msg = outbox.pending()[0]
    assert msg.attempts == 1
    # not retryable until next_retry_at
    second = consumer.process_once()
    assert second.sent == []
    assert second.skipped_retry == ["evt-1"]
    # after the backoff elapses it is retried and succeeds
    later = now + timedelta(seconds=120)
    consumer = OutboxConsumer(outbox, adapter, clock=lambda: later)
    third = consumer.process_once()
    assert third.sent == ["evt-1"]
    assert adapter.calls == ["evt-1", "evt-1"]


def test_consumer_never_resends_sent_event(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "outbox.json")
    outbox.enqueue(_msg("evt-1"))
    adapter = FakeFeishuAdapter()
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    OutboxConsumer(outbox, adapter, clock=lambda: now).process_once()
    result = OutboxConsumer(outbox, adapter, clock=lambda: now).process_once()
    assert result.sent == []
    assert adapter.calls == ["evt-1"]  # delivered exactly once


def test_retry_counter_persists_across_restart(tmp_path: Path) -> None:
    outbox = OutboxStore(tmp_path / "outbox.json")
    outbox.enqueue(_msg("evt-1"))
    adapter = FakeFeishuAdapter(failures_before_success=99)
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    OutboxConsumer(outbox, adapter, clock=lambda: now).process_once()
    OutboxConsumer(outbox, adapter, clock=lambda: now).process_once()  # same tick: skipped
    reopened = OutboxStore(tmp_path / "outbox.json")
    msg = reopened.pending()[0]
    assert msg.attempts >= 1
