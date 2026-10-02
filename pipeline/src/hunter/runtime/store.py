"""Runtime Provider store with crash recovery (Stage 2).

Mirrors the Registry's journaled pattern (AGENTS.md §12):
1. Write journal (.runtime_txn.json)
2. Atomic replace runtime_providers.json
3. Append history event (idempotent by event_id)
4. Remove journal

Recovery same logic: new revision present → append missing event;
old revision still present → discard; anything else → fail closed.

Review round 2 hardening:
- strict top-level structure validation ({"items": [...]});
- duplicate provider IDs fail closed;
- corrupt history lines fail closed (never silently skipped);
- each record's last_event_id is re-verified against its revision+content
  so a tampered snapshot cannot load;
- approval_binding can be cleared by passing None;
- a single-instance process lock guards the store;
- writes fsync before atomic replace.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .locks import ProcessFileLock
from .models import (
    ActualPoolStatus,
    ApprovalBinding,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
)


class RuntimeError(Exception):
    """Raised on invalid records or unrecoverable runtime/history state."""


def make_runtime_event_id(provider_id: str, revision: int, change_digest: str) -> str:
    """Deterministic event ID from provider_id, revision, and change digest."""
    material = "|".join([provider_id, str(revision), change_digest])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _change_digest(data: Dict[str, Any]) -> str:
    """Deterministic digest of a semantic-change payload."""
    material = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _serialize(obj: Any) -> Any:
    """JSON-serialization helper for dataclasses with enum fields."""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _deserialize_rp(raw: Dict[str, Any]) -> RuntimeProvider:
    """Deserialize a RuntimeProvider from a JSON dict."""
    raw = dict(raw)
    # Convert datetime strings back to datetimes
    for field_name in ("health_checked_at", "last_synced_at"):
        val = raw.get(field_name)
        if isinstance(val, str):
            raw[field_name] = datetime.fromisoformat(val)
    # Deserialize nested ProtocolResult
    pr = raw.get("protocol_result")
    if pr and isinstance(pr, dict):
        checked_at = pr.get("checked_at")
        if isinstance(checked_at, str):
            pr["checked_at"] = datetime.fromisoformat(checked_at)
        raw["protocol_result"] = ProtocolResult(**pr)
    # Deserialize nested ApprovalBinding
    ab = raw.get("approval_binding")
    if ab and isinstance(ab, dict):
        approved_at = ab.get("approved_at")
        if isinstance(approved_at, str):
            ab["approved_at"] = datetime.fromisoformat(approved_at)
        raw["approval_binding"] = ApprovalBinding(**ab)
    # Convert enums
    for enum_field, enum_cls in (
        ("credential_status", CredentialStatus),
        ("health_status", HealthStatus),
        ("expected_pool_status", ExpectedPoolStatus),
        ("actual_pool_status", ActualPoolStatus),
        ("approval_status", ApprovalStatus),
    ):
        val = raw.get(enum_field)
        if isinstance(val, str):
            raw[enum_field] = enum_cls(val)
    raw["protocol_result"] = raw.get("protocol_result") or ProtocolResult()
    return RuntimeProvider(**raw)


def _expected_event_id(rp: RuntimeProvider) -> str:
    """Re-derive the event_id bound to this snapshot content + revision."""
    raw = _serialize(rp)
    raw["last_event_id"] = ""
    digest = _change_digest(raw)
    return make_runtime_event_id(rp.provider_id, rp.revision, digest)


class RuntimeStore:
    """Journaled runtime store at data/runtime_providers.json."""

    JOURNAL_NAME = ".runtime_txn.json"
    LOCK_NAME = ".runtime.lock"

    def __init__(self, providers_path: Path, history_path: Path, lock: bool = True):
        self.providers_path = providers_path
        self._history_path = history_path
        self._providers: Dict[str, RuntimeProvider] = {}
        self._lock = ProcessFileLock(providers_path.parent / self.LOCK_NAME) if lock else None
        if self._lock is not None:
            self._lock.acquire()
        try:
            self._load()
            # History corruption must fail closed at open time (review 六.2),
            # not silently surface later.
            self._history_event_ids()
            self.recover_pending_transaction()
        except BaseException:
            if self._lock is not None:
                self._lock.release()
            raise

    # --- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self.providers_path.is_file():
            return
        try:
            payload = json.loads(self.providers_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot load runtime store {self.providers_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"runtime store {self.providers_path}: top-level must be an object"
            )
        if set(payload.keys()) != {"items"}:
            raise RuntimeError(
                f"runtime store {self.providers_path}: top-level must contain exactly 'items'"
            )
        items = payload["items"]
        if not isinstance(items, list):
            raise RuntimeError(
                f"runtime store {self.providers_path}: 'items' must be a list"
            )
        for raw in items:
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"runtime store {self.providers_path}: item is not an object"
                )
            try:
                rp = _deserialize_rp(raw)
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError(
                    f"runtime store {self.providers_path}: invalid record: {exc}"
                ) from exc
            if rp.provider_id in self._providers:
                raise RuntimeError(
                    f"runtime store {self.providers_path}: duplicate provider id "
                    f"{rp.provider_id!r}"
                )
            # event/snapshot binding: the stored event_id must match the
            # deterministic derivation over this content at this revision.
            if rp.revision > 0 and rp.last_event_id:
                if rp.last_event_id != _expected_event_id(rp):
                    raise RuntimeError(
                        f"runtime store {self.providers_path}: event/snapshot binding "
                        f"mismatch for {rp.provider_id!r} (revision {rp.revision})"
                    )
            elif rp.revision > 0 and not rp.last_event_id:
                raise RuntimeError(
                    f"runtime store {self.providers_path}: record {rp.provider_id!r} "
                    f"has revision {rp.revision} but no event id"
                )
            self._providers[rp.provider_id] = rp

    def _serialize(self) -> bytes:
        raw_items = [_serialize(rp) for rp in self._providers.values()]
        items = sorted(raw_items, key=lambda d: d.get("provider_id", ""))
        payload = {"items": items}
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")

    def _atomic_replace(self, content: bytes) -> None:
        self.providers_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.providers_path.parent), prefix=".runtime-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.providers_path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save(self) -> None:
        """Force-write the current runtime snapshot atomically."""
        self._atomic_replace(self._serialize())

    # --- queries -------------------------------------------------------------

    def list_providers(self) -> List[RuntimeProvider]:
        return sorted(self._providers.values(), key=lambda r: r.provider_id)

    def get_provider(self, provider_id: str) -> Optional[RuntimeProvider]:
        return self._providers.get(provider_id)

    # --- mutations -----------------------------------------------------------

    def upsert(
        self,
        provider: RuntimeProvider,
        changed_fields: Optional[List[str]] = None,
        reason: str = "",
        source_metadata: Optional[Dict[str, Any]] = None,
    ) -> RuntimeProvider:
        """Insert or update a RuntimeProvider with journaled commit.

        Returns the stored provider (possibly with updated revision/event_id).
        A no-op (identical semantic content) returns the existing record unchanged.
        """
        existing = self._providers.get(provider.provider_id)
        if existing is not None:
            # Compute semantic diff (everything except revision/event_id/notified)
            existing_raw = _serialize(existing)
            incoming_raw = _serialize(provider)
            # Exclude ephemeral fields from identity
            for key in ("revision", "last_event_id", "notified_events"):
                existing_raw.pop(key, None)
                incoming_raw.pop(key, None)
            if existing_raw == incoming_raw:
                return existing  # no-op

            new_rev = existing.revision + 1
            provider = RuntimeProvider(
                provider_id=provider.provider_id,
                provider_name=provider.provider_name or existing.provider_name,
                evidence_status=provider.evidence_status or existing.evidence_status,
                credential_status=provider.credential_status,
                health_status=provider.health_status,
                health_checked_at=provider.health_checked_at or existing.health_checked_at,
                protocol_result=provider.protocol_result or existing.protocol_result,
                expected_pool_status=provider.expected_pool_status,
                actual_pool_status=provider.actual_pool_status,
                approval_status=provider.approval_status,
                # Review 六.3: approval_binding must be clearable by passing
                # None — take the incoming value verbatim.
                approval_binding=provider.approval_binding,
                revision=new_rev,
                last_event_id="",
                last_synced_at=provider.last_synced_at or existing.last_synced_at,
                notified_events=list(existing.notified_events),
            )
            event_type = "runtime.provider_updated"
        else:
            new_rev = 1
            event_type = "runtime.provider_created"
            provider = RuntimeProvider(
                provider_id=provider.provider_id,
                provider_name=provider.provider_name,
                evidence_status=provider.evidence_status,
                credential_status=provider.credential_status,
                health_status=provider.health_status,
                health_checked_at=provider.health_checked_at,
                protocol_result=provider.protocol_result or ProtocolResult(),
                expected_pool_status=provider.expected_pool_status,
                actual_pool_status=provider.actual_pool_status,
                approval_status=provider.approval_status,
                approval_binding=provider.approval_binding,
                revision=new_rev,
                last_event_id="",
                last_synced_at=provider.last_synced_at,
                notified_events=list(provider.notified_events),
            )

        change_digest = _change_digest(_serialize(provider))
        event_id = make_runtime_event_id(provider.provider_id, new_rev, change_digest)
        provider = RuntimeProvider(
            provider_id=provider.provider_id,
            provider_name=provider.provider_name,
            evidence_status=provider.evidence_status,
            credential_status=provider.credential_status,
            health_status=provider.health_status,
            health_checked_at=provider.health_checked_at,
            protocol_result=provider.protocol_result,
            expected_pool_status=provider.expected_pool_status,
            actual_pool_status=provider.actual_pool_status,
            approval_status=provider.approval_status,
            approval_binding=provider.approval_binding,
            revision=provider.revision,
            last_event_id=event_id,
            last_synced_at=provider.last_synced_at,
            notified_events=list(provider.notified_events),
        )

        changed = changed_fields or ["state"]
        event = {
            "event_id": event_id,
            "provider_id": provider.provider_id,
            "revision": new_rev,
            "event_type": event_type,
            "changed_fields": changed,
            "reason": reason or "runtime upsert",
            "source_metadata": source_metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": _serialize(provider),
        }

        journal = self.providers_path.parent / self.JOURNAL_NAME
        old_revision = existing.revision if existing else 0
        self._write_journal(journal, old_revision, new_rev, event)
        self._providers[provider.provider_id] = provider
        self.save()
        self._append_history(event)
        self._remove_journal(journal)
        return provider

    def remove(self, provider_id: str) -> bool:
        """Remove a runtime provider record. Returns True if removed."""
        if provider_id not in self._providers:
            return False
        del self._providers[provider_id]
        self.save()
        return True

    # --- journal / history ---------------------------------------------------

    def _write_journal(self, journal: Path, old_revision: int, new_revision: int, event: Dict) -> None:
        payload = {
            "old_revision": old_revision,
            "new_revision": new_revision,
            "event": event,
        }
        journal.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(journal.parent), prefix=".runtime-journal-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, journal)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _remove_journal(journal: Path) -> None:
        try:
            journal.unlink()
        except FileNotFoundError:
            pass

    def _append_history(self, event: Dict) -> bool:
        """Append event to history.jsonl idempotently by event_id."""
        if not self.history_path.parent.exists():
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
        existing_ids = self._history_event_ids()
        if event.get("event_id") in existing_ids:
            return False
        line = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True

    def _history_event_ids(self) -> set:
        """Read history event ids; CORRUPT LINES FAIL CLOSED (review 六.2)."""
        if not self.history_path.is_file():
            return set()
        ids = set()
        for line_no, line in enumerate(
            self.history_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"corrupt runtime history {self.history_path} at line {line_no}; "
                    "manual inspection required"
                ) from exc
            if not isinstance(event, dict) or not event.get("event_id"):
                raise RuntimeError(
                    f"corrupt runtime history {self.history_path} at line {line_no}; "
                    "manual inspection required"
                )
            ids.add(event["event_id"])
        return ids

    def recover_pending_transaction(self) -> None:
        """Reconcile a leftover transaction journal (crash recovery)."""
        journal = self.providers_path.parent / self.JOURNAL_NAME
        if not journal.is_file():
            return
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            old_rev = int(payload["old_revision"])
            new_rev = int(payload["new_revision"])
            event = payload["event"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"corrupt transaction journal {journal}; manual inspection required: {exc}"
            ) from exc

        provider_id = event.get("provider_id")
        provider = self._providers.get(provider_id)
        provider_rev = provider.revision if provider is not None else 0

        if provider is not None and provider_rev == new_rev:
            # New state is in place; append the missing event, drop the journal.
            self._append_history(event)
            self._remove_journal(journal)
            return
        if provider is None and old_rev == 0:
            self._remove_journal(journal)
            return
        if provider is not None and provider_rev == old_rev:
            self._remove_journal(journal)
            return

        raise RuntimeError(
            f"runtime transaction journal {journal} does not match store state "
            f"(expected revision {old_rev} or {new_rev}, found {provider_rev}); "
            "manual inspection required"
        )

    @property
    def history_path(self) -> Path:
        return self._history_path

    @history_path.setter
    def history_path(self, value: Path) -> None:
        self._history_path = value

    def close(self) -> None:
        """Release the single-instance lock (idempotent)."""
        if self._lock is not None:
            self._lock.release()
