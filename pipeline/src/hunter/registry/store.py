"""Provider Registry Store: current-state source of truth + crash recovery.

``data/providers.json`` is the source of truth. Every meaningful update
increments ``revision``, derives a deterministic ``event_id``, and commits
through a transaction journal (``.registry_txn.json``) exactly as specified in
AGENTS.md section 12. No-op updates change nothing.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .history import History, make_event_id
from .schema import Provider

_URLSAFE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class RegistryError(Exception):
    """Raised on invalid records or an unrecoverable registry/history state."""


def _normalize_domain(domain: Optional[str]) -> Optional[str]:
    if domain is None:
        return None
    host = str(domain).strip().lower().rstrip(".")
    if not host:
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _normalize_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    return " ".join(str(name).split()).lower()


class ProviderRegistry:
    """Single Provider persistence boundary."""

    JOURNAL_NAME = ".registry_txn.json"

    def __init__(self, providers_path: Path, history_path: Path):
        self.providers_path = providers_path
        self.history_path = history_path
        self.history = History(history_path)
        self._providers: Dict[str, Provider] = {}
        self._aliases: Dict[str, str] = {}
        self._load()
        self.recover_pending_transaction()

    # --- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.providers_path.is_file():
            return
        try:
            payload = json.loads(self.providers_path.read_text(encoding="utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for raw in items:
                provider = Provider.model_validate(raw)
                self._providers[provider.id] = provider
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RegistryError(
                f"cannot load provider registry {self.providers_path}: {exc}"
            ) from exc

    def _serialize(self) -> bytes:
        items = [p.model_dump(mode="json") for p in self.list_providers()]
        payload = {"items": items}
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )

    def _atomic_replace(self, content: bytes) -> None:
        self.providers_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.providers_path.parent), prefix=".providers-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp_name, self.providers_path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save(self) -> None:
        """Force-write the current providers snapshot atomically."""
        self._atomic_replace(self._serialize())

    # --- queries ------------------------------------------------------------

    def list_providers(self) -> List[Provider]:
        return sorted(self._providers.values(), key=lambda p: p.id)

    def get_provider(self, provider_id: str) -> Optional[Provider]:
        return self._providers.get(provider_id)

    def find_by_domain(self, domain: str) -> Optional[Provider]:
        target = _normalize_domain(domain)
        for provider in self._providers.values():
            if _normalize_domain(provider.canonical_domain) == target:
                return provider
        return None

    def find_by_domain_or_alias(self, domain_or_alias: str) -> Optional[Provider]:
        provider = self.find_by_domain(domain_or_alias)
        if provider is not None:
            return provider
        return self._providers.get(self._aliases.get(domain_or_alias, ""))

    def find_by_name(self, name: str) -> Optional[Provider]:
        target = _normalize_name(name)
        matches = [
            p for p in self._providers.values() if _normalize_name(p.provider) == target
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _find_identity(self, provider: Provider) -> Optional[Provider]:
        """Match an incoming Provider per AGENTS.md order 1-4."""
        existing = self._providers.get(provider.id)
        if existing is not None:
            return existing
        if provider.canonical_domain:
            by_domain = self.find_by_domain(provider.canonical_domain)
            if by_domain is not None:
                return by_domain
        alias = self._aliases.get(provider.id)
        if alias and alias in self._providers:
            return self._providers[alias]
        return self.find_by_name(provider.provider)

    # --- mutation -----------------------------------------------------------

    def _semantic(self, provider: Provider) -> Dict[str, Any]:
        """Content used to decide whether an update is meaningful.

        Revision, last_event_id, and first_discovered are bookkeeping, not
        business content; they never count as a meaningful change.
        """
        dumped = provider.model_dump(mode="json")
        dumped.pop("revision", None)
        dumped.pop("last_event_id", None)
        dumped.pop("first_discovered", None)
        return dumped

    def upsert_provider(
        self,
        provider: Provider,
        reason: str,
        source_metadata: Dict[str, Any],
        event_type: str = "provider.upsert",
    ) -> Provider:
        """Create or update a Provider through the journaled transaction."""
        if not isinstance(provider, Provider):
            raise RegistryError("upsert_provider requires a Provider instance")
        try:
            provider.model_dump()  # validates
        except ValueError as exc:
            raise RegistryError(f"invalid Provider record: {exc}") from exc

        existing = self._find_identity(provider)

        if existing is None:
            new_rev = 1
            event_type = "provider.created"
            semantic_before: Dict[str, Any] = {}
        else:
            new_rev = existing.revision + 1
            # preserve first_discovered for identity stability
            provider = provider.model_copy(
                update={
                    "first_discovered": existing.first_discovered,
                    "canonical_domain": existing.canonical_domain
                    if existing.canonical_domain and not provider.canonical_domain
                    else provider.canonical_domain,
                }
            )
            semantic_before = self._semantic(existing)

        semantic_after = self._semantic(provider)
        if existing is not None and semantic_before == semantic_after:
            return existing  # no-op: nothing changed

        changed_fields = _changed_fields(semantic_before, semantic_after)
        event = self.history.build_event(
            provider_id=provider.id,
            revision=new_rev,
            event_type=event_type,
            changed_fields=changed_fields,
            reason=reason,
            source_metadata=source_metadata,
            content=semantic_after,
        )
        provider = provider.model_copy(
            update={
                "revision": new_rev,
                "last_event_id": event["event_id"],
            }
        )

        # Cross-file update sequence per AGENTS.md section 12:
        # 1) write journal (old/new revision + complete event)
        # 2) atomically replace providers.json
        # 3) append the event only if its event_id is absent
        # 4) remove the journal
        journal = self.providers_path.parent / self.JOURNAL_NAME
        self._write_journal(
            journal,
            old_revision=existing.revision if existing else 0,
            new_revision=new_rev,
            event=event,
        )
        self._providers[provider.id] = provider
        self.save()
        self.history.append(event)
        self._remove_journal(journal)
        return provider

    def _write_journal(self, journal: Path, old_revision: int, new_revision: int, event: Dict) -> None:
        payload = {
            "old_revision": old_revision,
            "new_revision": new_revision,
            "event": event,
        }
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    @staticmethod
    def _remove_journal(journal: Path) -> None:
        try:
            journal.unlink()
        except FileNotFoundError:
            pass

    # --- recovery -----------------------------------------------------------

    def recover_pending_transaction(self) -> None:
        """Reconcile a leftover transaction journal per AGENTS.md section 12."""
        journal = self.providers_path.parent / self.JOURNAL_NAME
        if not journal.is_file():
            return
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            old_rev = int(payload["old_revision"])
            new_rev = int(payload["new_revision"])
            event = payload["event"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RegistryError(
                f"corrupt transaction journal {journal}; manual inspection required: {exc}"
            ) from exc

        provider_id = event.get("provider_id")
        provider = self._providers.get(provider_id)
        provider_rev = provider.revision if provider is not None else 0

        if provider is not None and provider_rev == new_rev:
            # New state is in place; append the missing event, drop the journal.
            self.history.append(event)
            self._remove_journal(journal)
            return
        if provider is None and old_rev == 0:
            # Create never committed; discard the unapplied journal.
            self._remove_journal(journal)
            return
        if provider is not None and provider_rev == old_rev:
            # Old state still in place; the journal was unapplied. Discard.
            self._remove_journal(journal)
            return

        raise RegistryError(
            f"transaction journal {journal} does not match registry state "
            f"(expected revision {old_rev} or {new_rev}, found {provider_rev}); "
            "manual inspection required"
        )


def _changed_fields(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def _digest(content: Dict[str, Any]) -> str:
    import hashlib

    material = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]
