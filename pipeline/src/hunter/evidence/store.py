"""Evidence Store (TASK-007).

Persists Evidence atomically in stable ``evidence_id`` order. Identity derives
from canonical URL plus content fingerprint; re-fetching identical content does
not duplicate evidence. New evidence starts ``UNCONFIRMED``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .models import (
    Evidence,
    Officiality,
    canonicalize_evidence_url,
    _new_evidence_id,
)


class EvidenceStore:
    """Atomic snapshot store at ``data/evidence.json``."""

    def __init__(self, path: Path):
        self.path = path
        self._items: Dict[str, Evidence] = {}
        # The pipeline fetches/upserts evidence concurrently. The persisted
        # record set must stay byte-deterministic, so the read-modify-write
        # inside upsert is serialized.
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for raw in items:
                ev = Evidence.model_validate(raw)
                self._items[ev.evidence_id] = ev
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load evidence store {self.path}: {exc}") from exc

    def list(self) -> List[Evidence]:
        return sorted(self._items.values(), key=lambda e: e.evidence_id)

    def get(self, evidence_id: str) -> Optional[Evidence]:
        return self._items.get(evidence_id)

    def remove_exact_ids(self, evidence_ids: Sequence[str]) -> List[str]:
        """Remove only the explicitly named evidence records.

        This narrow operation is reserved for audited data repair.  It never
        selects records by provider, URL, source type, or other broad
        predicates.
        """
        targets = set(evidence_ids)
        removed = sorted(evidence_id for evidence_id in self._items if evidence_id in targets)
        if not removed:
            return []
        for evidence_id in removed:
            del self._items[evidence_id]
        self.save()
        return removed

    def find_by_url_fingerprint(self, url: str, fingerprint: str) -> Optional[Evidence]:
        canonical = canonicalize_evidence_url(url)
        for ev in self._items.values():
            if ev.url == canonical and ev.content_fingerprint == fingerprint:
                return ev
        return None

    def upsert(self, evidence: Evidence) -> bool:
        """Insert or update. Returns True if a new item was added.

        Re-fetching identical content (same canonical URL + fingerprint) does
        not duplicate evidence; the incoming item's officiality, validation
        notes, dates, and content fields are merged onto the stored record. A
        merge that actually changes the stored record is persisted atomically;
        a merge that changes nothing does not rewrite the file.
        """
        with self._lock:
            return self._upsert_locked(evidence)

    def _upsert_locked(self, evidence: Evidence) -> bool:
        ev = evidence.finalize() if not evidence.content_fingerprint else evidence
        existing = self.find_by_url_fingerprint(ev.url, ev.content_fingerprint)
        if existing is not None:
            # A re-fetch of identical content never silently downgrades an
            # officiality decision the validator already made back to
            # UNCONFIRMED; trust decisions are the validator's alone.
            incoming_officiality = ev.officiality
            if (
                incoming_officiality is Officiality.UNCONFIRMED
                and existing.officiality is not Officiality.UNCONFIRMED
            ):
                incoming_officiality = existing.officiality
            updated = existing.model_copy(
                update={
                    "retrieved_at": ev.retrieved_at,
                    "officiality": incoming_officiality,
                    "validation_notes": ev.validation_notes or existing.validation_notes,
                    "effective_at": ev.effective_at or existing.effective_at,
                    "published_at": ev.published_at or existing.published_at,
                    "claim": ev.claim or existing.claim,
                    "content_excerpt": ev.content_excerpt or existing.content_excerpt,
                    "title": ev.title or existing.title,
                    "provider_id": ev.provider_id or existing.provider_id,
                }
            )
            if updated.model_dump(mode="json") == existing.model_dump(mode="json"):
                return False  # no-op merge: nothing to write
            self._items[updated.evidence_id] = updated
            self.save()
            return False
        if not ev.evidence_id:
            ev = ev.model_copy(update={"evidence_id": _new_evidence_id(ev)})
        self._items[ev.evidence_id] = ev
        self.save()
        return True

    def save(self) -> None:
        items = [e.model_dump(mode="json") for e in self.list()]
        payload = {"items": items}
        content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".evidence-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp_name, self.path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
