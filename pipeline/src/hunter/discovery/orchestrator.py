"""Discovery orchestrator (TASK-005/006).

Runs enabled collectors behind one bounded interface, isolates failures,
aggregates observations, persists once through the Candidate Store, and
returns per-collector and total counts. Adapters never verify Providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

from ..collectors.github import GitHubCollector
from .deduplicator import deduplicate_observations
from .models import CandidateObservation, SourceType
from .store import CandidateStore, ObservationWithCandidate


@dataclass
class RunContext:
    """Carries run timestamp, limits, and credentials into collectors."""

    run_timestamp: str
    max_results_per_collector: int = 50
    credentials: Dict[str, Optional[str]] = None
    config: Dict[str, Any] = None  # bounded adapter config snapshot


class Collector(Protocol):
    def collect(self, query: str, run_context: RunContext) -> List[CandidateObservation]: ...


class DiscoveryOrchestrator:
    """Coordinates discovery collectors and persists observations once."""

    def __init__(
        self,
        store: CandidateStore,
        collectors: Optional[Dict[str, Collector]] = None,
        enabled: Optional[Sequence[str]] = None,
    ):
        self.store = store
        self.collectors: Dict[str, Collector] = collectors or {}
        self.enabled = set(enabled) if enabled is not None else set(self.collectors)
        if not self.enabled:
            self.enabled = set(self.collectors.keys())

    def run(self, run_context: RunContext) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"collectors": {}, "total_observations": 0, "errors": 0}
        all_observations: List[CandidateObservation] = []
        for name, collector in self.collectors.items():
            if name not in self.enabled:
                summary["collectors"][name] = {
                    "observations": 0,
                    "errors": 0,
                    "disabled": True,
                }
                continue
            # Collectors may disable themselves (e.g. web search without keys).
            enabled_check = getattr(collector, "enabled", None)
            if callable(enabled_check) and not enabled_check():
                summary["collectors"][name] = {
                    "observations": 0,
                    "errors": 0,
                    "disabled": True,
                }
                continue
            try:
                if isinstance(collector, GitHubCollector):
                    observations = self._collect_github(collector, run_context)
                else:
                    observations = self._collect_protocol(collector, run_context)
                unique, _duplicates = deduplicate_observations(observations)
                all_observations.extend(unique)
                summary["collectors"][name] = {
                    "observations": len(unique),
                    "errors": 0,
                    "disabled": False,
                }
            except Exception:  # noqa: BLE001 - one collector must not break others
                summary["errors"] += 1
                summary["collectors"][name] = {"observations": 0, "errors": 1, "disabled": False}

        # Persist once through the Candidate Store.
        entries = [
            ObservationWithCandidate(
                observation=obs,
                candidate_id=_github_candidate_id(obs),
                provider_name=obs.source_title,
                canonical_domain_hint=_identity_domain(obs),
            )
            for obs in all_observations
        ]
        counts = self.store.ingest(entries)
        summary["total_observations"] = counts.observations_added + counts.unchanged
        summary["store"] = {
            "candidates_created": counts.candidates_created,
            "candidates_merged": counts.candidates_merged,
            "observations_added": counts.observations_added,
            "unchanged": counts.unchanged,
            "rejected": counts.rejected,
            "errors": counts.errors,
        }
        return summary

    @staticmethod
    def _collect_github(collector: GitHubCollector, run_context: RunContext) -> List[CandidateObservation]:
        observations: List[CandidateObservation] = []
        for query in collector.queries:
            observations.extend(collector.collect(query, run_context))
        return observations

    @staticmethod
    def _collect_protocol(collector: Collector, run_context: RunContext) -> List[CandidateObservation]:
        # Protocol collectors accept a single query; run the first configured query.
        config = run_context.config or {}
        queries = config.get("queries") or ["free ai api"]
        return collector.collect(queries[0], run_context)


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname
        return host.lower().rstrip(".") if host else None
    except ValueError:
        return None


def _identity_domain(obs: CandidateObservation) -> Optional[str]:
    """Derive a provider identity hint from observation metadata.

    Curated observations carry the asserted docs URL; Hacker News items carry
    the linked provider URL; social observations (Reddit/X) carry the linked
    provider URL and never fall back to a social host as the identity hint;
    otherwise fall back to the source URL host. The hint is never an
    officiality decision.
    """
    meta = obs.raw_metadata or {}
    if obs.source_type is SourceType.github:
        homepage = meta.get("github_homepage")
        domain = _domain_from_url(str(homepage)) if homepage else None
        if domain and domain not in {"github.com", "www.github.com"}:
            return domain
        return None
    for key in ("asserted_docs_url", "hn_link", "provider_url", "reddit_link"):
        value = meta.get(key)
        if value:
            domain = _domain_from_url(str(value))
            if domain and domain not in {"reddit.com", "www.reddit.com", "x.com", "twitter.com"}:
                return domain
    return _domain_from_url(obs.source_url)


def _github_candidate_id(obs: CandidateObservation) -> Optional[str]:
    if obs.source_type is not SourceType.github:
        return None
    candidate_id = (obs.raw_metadata or {}).get("github_candidate_id")
    return str(candidate_id) if candidate_id else None
