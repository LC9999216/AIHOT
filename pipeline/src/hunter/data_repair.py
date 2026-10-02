from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Set

from .discovery.models import _require_aware_iso
from .discovery.store import CandidateStore
from .evidence.models import Evidence, _new_evidence_id
from .evidence.store import EvidenceStore
from .registry.schema import ProviderStatus
from .registry.store import ProviderRegistry


@dataclass(frozen=True)
class RepairReport:
    backup_dir: Path
    migrated_providers: List[str]
    removed_evidence_ids: List[str]


def _guess_source_type(url: str) -> str:
    lowered = url.lower()
    if "pricing" in lowered or "plan" in lowered:
        return "pricing"
    if "api" in lowered or "developer" in lowered or "reference" in lowered:
        return "api-docs"
    if "docs" in lowered or "documentation" in lowered:
        return "docs"
    if "blog" in lowered or "news" in lowered:
        return "blog"
    return "page"


def legacy_pipeline_evidence_ids(candidates_path: Path, as_of: datetime) -> Set[str]:
    as_of = _require_aware_iso(as_of, "as_of")
    candidates = CandidateStore(Path(candidates_path)).list_candidates()
    evidence_ids: Set[str] = set()
    for candidate in candidates:
        for observation in candidate.observations:
            metadata = observation.raw_metadata or {}
            docs_url = metadata.get("asserted_docs_url")
            url = docs_url if isinstance(docs_url, str) and docs_url.startswith("http") else None
            if not url:
                continue
            claim = metadata.get("asserted_free_tier") or observation.claim or ""
            evidence = Evidence(
                candidate_id=candidate.candidate_id,
                provider_id=candidate.candidate_id,
                url=url,
                source_type=_guess_source_type(url),
                retrieved_at=as_of,
                effective_at=as_of,
                title=observation.source_title,
                claim=str(claim)[:300],
                content_excerpt=str(observation.claim or "")[:4000],
            ).finalize()
            evidence_ids.add(_new_evidence_id(evidence))
    return evidence_ids


def repair_legacy_pipeline_data(
    data_dir: Path,
    backup_dir: Path,
    as_of: datetime,
    expected_evidence_count: int | None = None,
):
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)
    as_of = _require_aware_iso(as_of, "as_of")
    legacy_ids = legacy_pipeline_evidence_ids(data_dir / "candidates.json", as_of)
    if expected_evidence_count is not None and len(legacy_ids) != expected_evidence_count:
        raise ValueError(
            f"expected {expected_evidence_count} legacy evidence IDs, found {len(legacy_ids)}"
        )
    if backup_dir.exists():
        raise ValueError(f"backup directory already exists: {backup_dir}")

    backup_dir.mkdir(parents=True)
    for name in ("candidates.json", "evidence.json", "providers.json", "history.jsonl"):
        source = data_dir / name
        if source.is_file():
            shutil.copy2(source, backup_dir / name)

    evidence_store = EvidenceStore(data_dir / "evidence.json")
    removed_evidence_ids = evidence_store.remove_exact_ids(sorted(legacy_ids))

    registry = ProviderRegistry(
        providers_path=data_dir / "providers.json",
        history_path=data_dir / "history.jsonl",
    )
    migrated_providers: List[str] = []
    for provider_id in ("assemblyai", "openrouter"):
        current = registry.get_provider(provider_id)
        if current is None:
            continue
        updated = current.model_copy(
            update={
                "status": ProviderStatus.UNCERTAIN,
                "evidence_ids": [],
                "last_verified": None,
                "verification_confidence": None,
                "free_score": None,
                "score_metadata": None,
            }
        )
        stored = registry.upsert_provider(
            updated,
            reason="quarantine legacy synthetic evidence before real fetch/extraction",
            source_metadata={
                "migration": "legacy-build-evidence-repair",
                "as_of": as_of.isoformat(),
                "legacy_evidence_id_count": len(legacy_ids),
                "backup_dir": str(backup_dir),
            },
            event_type="provider.quarantined",
        )
        if stored.revision > current.revision:
            migrated_providers.append(provider_id)

    return RepairReport(
        backup_dir=backup_dir,
        migrated_providers=migrated_providers,
        removed_evidence_ids=removed_evidence_ids,
    )
