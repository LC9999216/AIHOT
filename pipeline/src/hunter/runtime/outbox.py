"""Notification outbox (Stage 2).

All notifications go through a persistent outbox before being sent to Feishu.
Each message carries a deterministic event_id used as the Feishu idempotency key.

The outbox is stored at data/notification_outbox.json:
- ``pending``: ready to send, keyed by event_id
- ``sent``: already delivered

Rules (review round 2, 六.6/六.7):
- Never include Provider Keys, Authorization headers, or secrets; messages
  carrying secret-like content are REJECTED at enqueue time.
- ``event_type`` must come from the fixed vocabulary below; notifications are
  generated only from structured public fields.
- Failed sends keep a structured ``last_error_code`` (never free exception
  text) plus a retry counter and next-retry timestamp for backoff.
- A single-instance lock guards concurrent writers.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .locks import ProcessFileLock


# Fixed notification vocabulary: notifications are built from structured
# public fields only, never from arbitrary exception/reason text.
EVENT_TYPES = {
    "NEW_HIGH_VALUE",
    "FREE_TIER_CHANGED",
    "PROMOTION_EXPIRING",
    "FREE_TIER_EXPIRED",
    "POOL_SUSPENDED",
    "PROMOTED_TO_PRODUCTION",
    "POOL_SUSPEND_FAILED",
    "POOL_STATE_SPLIT",
    "PRODUCTION_BLOCKED",
    "APPROVAL_GRANTED",
    "APPROVAL_INVALIDATED",
    "HEALTH_DEGRADED",
}

# High-priority events that page the user immediately.
HIGH_PRIORITY_EVENT_TYPES = {
    "POOL_SUSPEND_FAILED",
    "POOL_STATE_SPLIT",
    "PRODUCTION_BLOCKED",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization[\s=:]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"sk-[a-z0-9_\-]{16,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)[\s=:]+\S{6,}"),
)


class OutboxError(Exception):
    """Raised on outbox read/write failures."""


def contains_secret(text: str) -> bool:
    """True when text carries credential-like content (fail-closed check)."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text or ""):
            return True
    return False


@dataclass(frozen=True)
class OutboxMessage:
    """A single notification destined for Feishu (or other channels)."""

    event_id: str
    provider_id: str
    provider_name: str
    event_type: str                 # from EVENT_TYPES
    title: str
    body: str                       # Markdown body, public info only
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    channel: str = "feishu"         # routing hint
    attempts: int = 0
    last_error_code: str = ""
    next_retry_at: Optional[datetime] = None

    @property
    def high_priority(self) -> bool:
        return self.event_type in HIGH_PRIORITY_EVENT_TYPES


class OutboxStore:
    """Persistent notification outbox at data/notification_outbox.json."""

    LOCK_NAME = ".outbox.lock"

    def __init__(self, path: Path, lock: bool = True):
        self.path = path
        self._pending: Dict[str, OutboxMessage] = {}
        self._sent: Dict[str, OutboxMessage] = {}
        self._lock = ProcessFileLock(path.parent / self.LOCK_NAME) if lock else None
        if self._lock is not None:
            self._lock.acquire()
        try:
            self._load()
        except BaseException:
            if self._lock is not None:
                self._lock.release()
            raise

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OutboxError(f"cannot load outbox {self.path}: {exc}") from exc
        if not isinstance(data, dict) or set(data.keys()) - {"pending", "sent"}:
            raise OutboxError(f"outbox {self.path}: invalid top-level structure")
        for src in ("pending", "sent"):
            items = data.get(src, {})
            if not isinstance(items, dict):
                raise OutboxError(f"outbox {self.path}: '{src}' must be an object")
            target = self._pending if src == "pending" else self._sent
            for eid, raw in items.items():
                raw = dict(raw)
                raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
                if raw.get("next_retry_at"):
                    raw["next_retry_at"] = datetime.fromisoformat(raw["next_retry_at"])
                target[eid] = OutboxMessage(**raw)

    def save(self) -> None:
        def _serialize_dict(d: Dict) -> Dict:
            return {
                eid: {
                    "event_id": msg.event_id,
                    "provider_id": msg.provider_id,
                    "provider_name": msg.provider_name,
                    "event_type": msg.event_type,
                    "title": msg.title,
                    "body": msg.body,
                    "timestamp": msg.timestamp.isoformat(),
                    "channel": msg.channel,
                    "attempts": msg.attempts,
                    "last_error_code": msg.last_error_code,
                    "next_retry_at": msg.next_retry_at.isoformat() if msg.next_retry_at else None,
                }
                for eid, msg in d.items()
            }
        payload = {
            "pending": _serialize_dict(self._pending),
            "sent": _serialize_dict(self._sent),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".outbox-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def enqueue(self, message: OutboxMessage) -> bool:
        """Add a pending message if its event_id is new. Returns True if added.

        Fails closed when the message carries secret-like content or an
        event_type outside the fixed vocabulary (review 六.7).
        """
        if message.event_type not in EVENT_TYPES:
            raise ValueError(
                f"unknown outbox event_type {message.event_type!r}; "
                "notifications come from the structured vocabulary only"
            )
        for field_name in ("title", "body", "provider_name", "provider_id"):
            if contains_secret(getattr(message, field_name, "") or ""):
                raise ValueError(
                    f"outbox message {message.event_id} rejected: "
                    f"{field_name} carries secret-like content"
                )
        if message.event_id in self._sent or message.event_id in self._pending:
            return False
        self._pending[message.event_id] = message
        self.save()
        return True

    def mark_sent(self, event_id: str) -> bool:
        """Move a pending message to sent. Returns True if moved."""
        msg = self._pending.get(event_id)
        if msg is None:
            return False
        del self._pending[event_id]
        self._sent[event_id] = OutboxMessage(
            event_id=msg.event_id,
            provider_id=msg.provider_id,
            provider_name=msg.provider_name,
            event_type=msg.event_type,
            title=msg.title,
            body=msg.body,
            timestamp=msg.timestamp,
            channel=msg.channel,
            attempts=msg.attempts,
            last_error_code="",
            next_retry_at=None,
        )
        self.save()
        return True

    def mark_failed(self, event_id: str, error_code: str, next_retry_at: datetime) -> bool:
        """Record a structured failure (code only, never exception text)."""
        msg = self._pending.get(event_id)
        if msg is None:
            return False
        self._pending[event_id] = OutboxMessage(
            event_id=msg.event_id,
            provider_id=msg.provider_id,
            provider_name=msg.provider_name,
            event_type=msg.event_type,
            title=msg.title,
            body=msg.body,
            timestamp=msg.timestamp,
            channel=msg.channel,
            attempts=msg.attempts + 1,
            last_error_code=str(error_code)[:64],
            next_retry_at=next_retry_at,
        )
        self.save()
        return True

    def pending(self) -> List[OutboxMessage]:
        return sorted(self._pending.values(), key=lambda m: m.timestamp)

    def sent(self) -> List[OutboxMessage]:
        return sorted(self._sent.values(), key=lambda m: m.timestamp)

    def has_event(self, event_id: str) -> bool:
        return event_id in self._pending or event_id in self._sent
