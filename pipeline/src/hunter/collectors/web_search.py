"""Web search discovery (TASK-006).

Disabled cleanly when credentials are absent. Business logic does not depend
on one search vendor: the vendor is only an injectable transport. Network is
fully mockable so tests stay offline.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..discovery.models import CandidateObservation, SourceType


class WebSearchCollector:
    """Collect observations from a web search API."""

    def __init__(
        self,
        transport: Any,
        api_key: Optional[str],
        base_url: str = "https://search.example/api",
    ):
        self.transport = transport
        self.api_key = api_key
        self.base_url = base_url

    def enabled(self) -> bool:
        return bool(self.api_key)

    def collect(self, query: str, run_context) -> List[CandidateObservation]:
        if not self.enabled():
            return []
        payload = self.transport.get(self.base_url, params={"q": query})
        results = payload.get("results", []) if isinstance(payload, dict) else []
        observations = []
        for result in results[:50]:
            if not isinstance(result, dict):
                continue
            link = result.get("url")
            if not link:
                continue
            title = result.get("title") or "web search result"
            snippet = result.get("snippet")
            observations.append(
                CandidateObservation(
                    observation_id=f"web-{abs(hash(link))}",
                    source_type=SourceType.web_search,
                    source_url=link,
                    source_title=title,
                    claim=(snippet or title).strip(),
                    matched_query=query,
                    discovered_at=_run_ts(run_context),
                    raw_metadata={
                        "search_snippet": snippet,
                        "search_rank": result.get("rank"),
                    },
                )
            )
        return observations


def _run_ts(run_context) -> datetime:
    try:
        return datetime.fromisoformat(run_context.run_timestamp)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
