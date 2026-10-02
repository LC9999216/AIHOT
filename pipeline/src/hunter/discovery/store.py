"""Candidate Store: complete atomic snapshot persistence for Candidates.

Persists ``data/candidates.json`` as a deterministic snapshot: stable
Candidate ordering by ``candidate_id``, stable observation ordering by
fingerprint, exact fingerprint deduplication, and merge only through stable ID,
exact normalized domain hint, explicit alias, or unambiguous exact normalized
name. Unchanged input produces byte-identical output.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .models import Candidate, CandidateObservation, SourceType, canonicalize_url, normalize_claim

_URLSAFE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CandidateStoreError(Exception):
    """Raised when the candidate store cannot load or persist."""


@dataclass
class ObservationWithCandidate:
    """An observation plus the identity hints used to place it into a Candidate."""

    observation: CandidateObservation
    candidate_id: Optional[str] = None
    provider_name: Optional[str] = None
    canonical_domain_hint: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    provider: Optional["object"] = None  # never set by discovery/import stages


@dataclass
class ImportCounts:
    candidates_created: int = 0
    candidates_merged: int = 0
    observations_added: int = 0
    unchanged: int = 0
    rejected: int = 0
    errors: int = 0


def slugify(text: str, fallback: str = "candidate") -> str:
    """Deterministic lowercase URL-safe id from a display name."""
    lowered = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return lowered or fallback


def normalize_domain(domain: Optional[str]) -> Optional[str]:
    """Lowercase, strip trailing dot, IDNA-encode a domain for exact matching."""
    if domain is None:
        return None
    host = str(domain).strip().lower().rstrip(".")
    if not host:
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def normalize_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    return " ".join(str(name).split()).lower()


class CandidateStore:
    """Snapshot store at ``data/candidates.json``."""

    def __init__(self, path: Path):
        self.path = path
        self._candidates: Dict[str, Candidate] = {}
        self._load()

    # --- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for raw in items:
                cand = Candidate.model_validate(raw)
                self._candidates[cand.candidate_id] = cand
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateStoreError(f"cannot load candidate store {self.path}: {exc}") from exc

    def serialize(self) -> bytes:
        items = [c.model_dump(mode="json") for c in self.list_candidates()]
        payload = {"items": items}
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")

    def save(self) -> bool:
        """Atomically replace the snapshot only if content changed.

        Returns True when a write happened (content differed).
        """
        content = self.serialize()
        if self.path.is_file() and self.path.read_bytes() == content:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".candidates-", suffix=".tmp"
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
        return True

    # --- queries ------------------------------------------------------------

    def list_candidates(self) -> List[Candidate]:
        return sorted(self._candidates.values(), key=lambda c: c.candidate_id)

    def get(self, candidate_id: str) -> Optional[Candidate]:
        return self._candidates.get(candidate_id)

    def count(self) -> int:
        return len(self._candidates)

    # --- ingestion ----------------------------------------------------------

    def ingest(self, entries: List[ObservationWithCandidate]) -> ImportCounts:
        counts = ImportCounts()
        for entry in entries:
            try:
                target = self._find_target(entry)
                if target is None:
                    target = self._create(entry)
                    counts.candidates_created += 1
                    counts.observations_added += 1
                else:
                    counts.candidates_merged += 1
                    if target.add_observation(entry.observation):
                        counts.observations_added += 1
                        target.last_seen_at = entry.observation.discovered_at
                    else:
                        counts.unchanged += 1
            except Exception:  # noqa: BLE001 - isolated per-entry failure
                counts.errors += 1
        for cand in self._candidates.values():
            cand.observations.sort(key=lambda o: o.fingerprint)
        self.save()
        return counts

    def _find_target(self, entry: ObservationWithCandidate) -> Optional[Candidate]:
        # 1. existing stable candidate ID. A provided ID that does not exist
        #    creates a new Candidate with that identity; fallbacks are only
        #    consulted when no stable ID is carried by the entry.
        if entry.candidate_id:
            return self._candidates.get(entry.candidate_id)

        # 2. exact normalized domain hint
        domain = normalize_domain(entry.canonical_domain_hint)
        if domain:
            for cand in self._candidates.values():
                if normalize_domain(cand.canonical_domain_hint) == domain:
                    return cand

        # 3. explicit alias (matches candidate_id or provider_name)
        for alias in entry.aliases:
            if alias in self._candidates:
                return self._candidates[alias]
            for cand in self._candidates.values():
                if normalize_name(cand.provider_name) == normalize_name(alias):
                    return cand

        # 4. unambiguous exact normalized provider name
        name = normalize_name(entry.provider_name)
        if name:
            matches = [
                cand
                for cand in self._candidates.values()
                if normalize_name(cand.provider_name) == name
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def _create(self, entry: ObservationWithCandidate) -> Candidate:
        candidate_id = entry.candidate_id or slugify(
            entry.provider_name or "candidate"
        )
        # ensure uniqueness of generated ids
        base, suffix = candidate_id, 2
        while candidate_id in self._candidates:
            candidate_id = f"{base}-{suffix}"
            suffix += 1
        cand = Candidate(
            candidate_id=candidate_id,
            provider_name=entry.provider_name or "Unknown provider",
            canonical_domain_hint=entry.canonical_domain_hint,
            first_discovered_at=entry.observation.discovered_at,
            last_seen_at=entry.observation.discovered_at,
            observations=[entry.observation],
        )
        self._candidates[candidate_id] = cand
        return cand
