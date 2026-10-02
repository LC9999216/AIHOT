"""Notification delivery: outbox consumer + Feishu adapter (Stage 2).

The consumer drains the persistent outbox:

- every message is delivered at most once (event_id idempotency);
- failed deliveries are retried with linear backoff, recording only a
  structured error code (never exception text, keys, or headers);
- the Feishu adapter is an injectable interface so tests stay fully offline
  and mockable. The real HTTP adapter is only used when the operator
  configures credentials; without them delivery fails closed.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from .outbox import OutboxMessage, OutboxStore


class NotificationError(Exception):
    """Delivery failed; carries a structured code, never payload secrets."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class FeishuAdapter:
    """Delivery interface. Mock this in tests; subclass for real transports."""

    def send(self, message: OutboxMessage) -> None:
        raise NotImplementedError


def build_feishu_payload(message: OutboxMessage) -> dict:
    """Build a Feishu custom-bot text payload.

    Only the documented request fields (msg_type/content) are emitted;
    structured context goes into the text itself. No exception text, no
    reasons, no keys, no Authorization content ever enters a payload.
    """
    text = f"[{message.event_type}] {message.provider_id}: {message.title}\n{message.body}"
    return {
        "msg_type": "text",
        "content": {
            "text": text[:2000],
        },
    }


class WebhookFeishuAdapter(FeishuAdapter):
    """Feishu custom-bot webhook adapter (optional live integration).

    Used ONLY when the operator provides a webhook URL at runtime; the URL is
    read at send time and never persisted or logged.
    """

    def __init__(self, webhook_url: str, timeout: float = 10.0):
        if not webhook_url or not webhook_url.startswith("https://"):
            raise NotificationError("feishu_webhook_not_configured")
        self._webhook_url = webhook_url
        self._timeout = timeout

    def send(self, message: OutboxMessage) -> None:
        payload = json.dumps(build_feishu_payload(message)).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                body = response.read(4096)
        except Exception:  # noqa: BLE001 - classified, message never surfaces
            raise NotificationError("feishu_unreachable") from None
        if status != 200:
            raise NotificationError("feishu_rejected")
        # Feishu custom-bot contract: HTTP 200 alone is NOT success. The
        # application layer must report code == 0 ("msg": "success");
        # e.g. code 19024 keyword mismatch arrives with HTTP 200.
        try:
            decoded = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise NotificationError("feishu_app_error") from None
        code = decoded.get("code") if isinstance(decoded, dict) else None
        if type(code) is not int or code != 0:
            raise NotificationError("feishu_app_error")


@dataclass
class ConsumeResult:
    sent: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped_retry: List[str] = field(default_factory=list)


class OutboxConsumer:
    """Drains pending outbox messages through an adapter with backoff retry."""

    def __init__(
        self,
        outbox: OutboxStore,
        adapter: FeishuAdapter,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        base_backoff_seconds: float = 60.0,
        max_attempts: int = 10,
    ):
        self.outbox = outbox
        self.adapter = adapter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.base_backoff_seconds = base_backoff_seconds
        self.max_attempts = max_attempts

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    def process_once(self) -> ConsumeResult:
        result = ConsumeResult()
        now = self._now()
        for message in self.outbox.pending():
            if message.next_retry_at is not None and message.next_retry_at > now:
                result.skipped_retry.append(message.event_id)
                continue
            if message.attempts >= self.max_attempts:
                # Dead-letter: keep it pending but never retry within a run;
                # a high-priority alert stays visible to the operator.
                result.skipped_retry.append(message.event_id)
                continue
            try:
                self.adapter.send(message)
            except NotificationError as exc:
                backoff = self.base_backoff_seconds * (message.attempts + 1)
                self.outbox.mark_failed(
                    message.event_id, exc.code, now + timedelta(seconds=backoff)
                )
                result.failed.append(message.event_id)
                continue
            except Exception:  # noqa: BLE001 - unknown failures get a code
                backoff = self.base_backoff_seconds * (message.attempts + 1)
                self.outbox.mark_failed(
                    message.event_id, "delivery_error", now + timedelta(seconds=backoff)
                )
                result.failed.append(message.event_id)
                continue
            self.outbox.mark_sent(message.event_id)
            result.sent.append(message.event_id)
        return result


__all__ = [
    "NotificationError",
    "FeishuAdapter",
    "WebhookFeishuAdapter",
    "OutboxConsumer",
    "ConsumeResult",
    "build_feishu_payload",
]
