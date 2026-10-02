"""Append-only history of meaningful registry updates (AGENTS.md section 12).

History contains event ID, timestamp, Provider ID, revision, event type,
changed fields, and reason/source metadata. It never contains secrets or page
bodies. Appends are idempotent by ``event_id``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class HistoryError(Exception):
    """Raised when the history journal cannot be read or appended."""


def make_event_id(provider_id: str, revision: int, change_digest: str) -> str:
    """Deterministic event ID from Provider ID, revision, and change digest."""
    material = "|".join([provider_id, str(revision), change_digest])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _change_digest(changed_fields: List[str], reason: str, content: Dict[str, Any]) -> str:
    material = json.dumps(
        {"fields": changed_fields, "reason": reason, "content": content},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


class History:
    """Append-only JSONL history with event_id idempotency."""

    def __init__(self, path: Path):
        self.path = path

    def _lines(self) -> List[str]:
        if not self.path.is_file():
            return []
        text = self.path.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]

    def event_ids(self) -> set:
        ids = set()
        for line in self._lines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HistoryError(f"corrupt history line in {self.path}: {exc}") from exc
            ids.add(event.get("event_id"))
        return ids

    def append(self, event: Dict[str, Any]) -> bool:
        """Append an event only if its event_id is absent. Returns True if appended."""
        if event.get("event_id") in self.event_ids():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True

    def build_event(
        self,
        provider_id: str,
        revision: int,
        event_type: str,
        changed_fields: List[str],
        reason: str,
        source_metadata: Dict[str, Any],
        content: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Build a deterministic event dict without appending it."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        digest = _change_digest(changed_fields, reason, content)
        return {
            "event_id": make_event_id(provider_id, revision, digest),
            "timestamp": timestamp.isoformat(),
            "provider_id": provider_id,
            "revision": revision,
            "event_type": event_type,
            "changed_fields": sorted(changed_fields),
            "reason": reason,
            "source_metadata": source_metadata,
        }

    def append_event(
        self,
        provider_id: str,
        revision: int,
        event_type: str,
        changed_fields: List[str],
        reason: str,
        source_metadata: Dict[str, Any],
        content: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Build and append an event; returns the event dict."""
        event = self.build_event(
            provider_id,
            revision,
            event_type,
            changed_fields,
            reason,
            source_metadata,
            content,
            timestamp,
        )
        self.append(event)
        return event

    def _append_replay_event(
        self, provider_id: str, revision: int, event_id: str
    ) -> bool:
        """Test helper: replay a synthetic event (idempotent by event_id)."""
        return self.append(
            {
                "event_id": event_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "provider_id": provider_id,
                "revision": revision,
                "event_type": "provider.replay",
                "changed_fields": [],
                "reason": "replay",
                "source_metadata": {},
            }
        )
