"""Curated repository discovery (TASK-006).

Prefers structured data over README text for configured immutable source
references. Network is fully injectable so tests stay offline.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..discovery.models import CandidateObservation, SourceType

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_FREE_HINT_RE = re.compile(r"\bfree\b", re.IGNORECASE)


class CuratedRepoCollector:
    """Collect observations from configured immutable curated sources."""

    def __init__(
        self,
        transport: Any,
        sources: List[Dict[str, Any]],
        max_lines: int = 200,
    ):
        self.transport = transport
        self.sources = sources
        self.max_lines = max_lines

    def collect(self, query: str, run_context) -> List[CandidateObservation]:
        observations: List[CandidateObservation] = []
        for source in self.sources:
            try:
                kind = source.get("kind", "readme")
                url = source["url"]
                name = source["name"]
                if kind == "structured":
                    observations.extend(self._from_structured(url, name, run_context))
                else:
                    observations.extend(self._from_readme(url, name, run_context))
            except Exception:  # noqa: BLE001 - one source must not break others
                continue
        return observations

    def _from_structured(self, url: str, name: str, run_context) -> List[CandidateObservation]:
        payload = self.transport.get(url)
        providers = payload.get("providers", []) if isinstance(payload, dict) else []
        observations = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            slug = provider.get("slug")
            pname = provider.get("name")
            if not slug:
                continue
            claim = provider.get("free_tier") or provider.get("notes") or f"{pname or slug} free offer"
            observations.append(
                CandidateObservation(
                    observation_id=f"curated-{name}-{slug}",
                    source_type=SourceType.curated_repo,
                    source_url=url,
                    source_title=f"{name} / {pname or slug}",
                    claim=str(claim),
                    matched_query=None,
                    discovered_at=_run_ts(run_context),
                    raw_metadata={
                        "curated_source": name,
                        "upstream_slug": slug,
                        "asserted_verified": provider.get("verified"),
                        "asserted_last_verified": provider.get("last_verified"),
                        "asserted_docs_url": provider.get("docs_url"),
                    },
                )
            )
        return observations

    def _from_readme(self, url: str, name: str, run_context) -> List[CandidateObservation]:
        text = self.transport.get_text(url)
        observations = []
        seen: set = set()
        for line in text.splitlines()[: self.max_lines]:
            if not _FREE_HINT_RE.search(line):
                continue
            match = _LINK_RE.search(line)
            if not match:
                continue
            title, link = match.group(1), match.group(2)
            obs = CandidateObservation(
                observation_id=f"curated-{name}-{abs(hash((title, link)))}",
                source_type=SourceType.curated_repo,
                source_url=link,
                source_title=title,
                claim=line.strip(),
                matched_query=None,
                discovered_at=_run_ts(run_context),
                raw_metadata={"curated_source": name},
            )
            if obs.fingerprint in seen:
                continue
            seen.add(obs.fingerprint)
            observations.append(obs)
        return observations


def _run_ts(run_context) -> datetime:
    try:
        return datetime.fromisoformat(run_context.run_timestamp)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
