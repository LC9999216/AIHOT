"""Evidence collection worker (review round 2, item 五.4).

The CLI must build evidence through :meth:`Evidence.from_fetch` so every
stored record carries verified SafeFetcher provenance bound to the final URL
(after redirects) and the actually-fetched content. Non-2xx fetches are
rejected outright — they can never become evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..discovery.store import CandidateStore
from .fetcher import SafeFetcher
from .models import Evidence
from .store import EvidenceStore


@dataclass
class CollectReport:
    """Structured, secret-free summary of one collect run."""

    fetched: int = 0
    stored: int = 0
    rejected_non_2xx: int = 0
    errors: int = 0
    rejected_urls: List[str] = field(default_factory=list)

    def as_lines(self) -> List[str]:
        lines = [
            f"fetched={self.fetched}",
            f"stored={self.stored}",
            f"rejected_non_2xx={self.rejected_non_2xx}",
            f"errors={self.errors}",
        ]
        for url in self.rejected_urls:
            lines.append(f"rejected: {url}")
        return lines


def _guess_source_type(url: str) -> str:
    lowered = (url or "").lower()
    if "pricing" in lowered or "plan" in lowered:
        return "pricing"
    if "api" in lowered or "developer" in lowered:
        return "api-docs"
    if "blog" in lowered or "news" in lowered:
        return "blog"
    if "docs" in lowered or "documentation" in lowered:
        return "docs"
    return "page"


def collect_evidence(
    candidate_store: CandidateStore,
    evidence_store: EvidenceStore,
    fetcher: SafeFetcher,
    *,
    candidate_id: Optional[str] = None,
    limit: int = 50,
) -> CollectReport:
    """Fetch proposed URLs and store provenance-bound evidence.

    - Only 2xx fetch results are stored (fail closed otherwise).
    - Evidence identity uses the FINAL url after redirects.
    - Provenance comes from the fetch itself via Evidence.from_fetch.
    """
    from .resolver import EvidenceResolver

    report = CollectReport()
    resolver = EvidenceResolver()
    candidates = candidate_store.list_candidates()
    if candidate_id:
        candidates = [c for c in candidates if c.candidate_id == candidate_id]
    for candidate in candidates:
        if report.fetched >= limit:
            break
        observations = [o.model_dump(mode="json") for o in candidate.observations]
        urls = resolver.propose_urls(
            candidate_domain=candidate.canonical_domain_hint,
            observations=observations,
        )
        for url in urls:
            if report.fetched >= limit:
                break
            try:
                result = fetcher.fetch(url, provider_id=candidate.candidate_id)
            except Exception:  # noqa: BLE001 - per-URL failures are counted, not fatal
                report.errors += 1
                continue
            report.fetched += 1
            if not 200 <= result.status < 300:
                report.rejected_non_2xx += 1
                report.rejected_urls.append(result.final_url or url)
                continue
            try:
                evidence = Evidence.from_fetch(
                    result,
                    provider_id=candidate.candidate_id,
                    candidate_id=candidate.candidate_id,
                    source_type=_guess_source_type(result.final_url or url),
                    title=result.final_url or url,
                )
            except ValueError:
                # from_fetch fails closed (binding violations); never store.
                report.rejected_non_2xx += 1
                report.rejected_urls.append(result.final_url or url)
                continue
            if evidence_store.upsert(evidence):
                report.stored += 1
    return report


__all__ = ["CollectReport", "collect_evidence"]
