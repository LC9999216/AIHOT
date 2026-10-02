"""Hacker News discovery (TASK-006).

Uses the bounded public Algolia search API and remains discovery-only.
Network is fully injectable so tests stay offline.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..discovery.models import CandidateObservation, SourceType


class HackerNewsCollector:
    """Collect observations from Hacker News search results."""

    def __init__(
        self,
        transport: Any,
        base_url: str = "https://hn.algolia.com/api/v1/search",
        max_results: int = 20,
    ):
        self.transport = transport
        self.base_url = base_url
        self.max_results = max_results

    def collect(self, query: str, run_context) -> List[CandidateObservation]:
        payload = self.transport.get(
            self.base_url,
            params={"query": query, "tags": "story", "hitsPerPage": self.max_results},
        )
        hits = payload.get("hits", []) if isinstance(payload, dict) else []
        observations = []
        for hit in hits[: self.max_results]:
            if not isinstance(hit, dict):
                continue
            object_id = hit.get("objectID")
            title = hit.get("title") or "Hacker News post"
            item_url = f"https://news.ycombinator.com/item?id={object_id}"
            observations.append(
                CandidateObservation(
                    observation_id=f"hn-{object_id}",
                    source_type=SourceType.hackernews,
                    source_url=item_url,
                    source_title=title,
                    claim=title,
                    matched_query=query,
                    discovered_at=_run_ts(run_context),
                    raw_metadata={
                        "hn_points": hit.get("points"),
                        "hn_num_comments": hit.get("num_comments"),
                        "hn_author": hit.get("author"),
                        "hn_created_at": hit.get("created_at"),
                        "hn_link": hit.get("url"),
                    },
                )
            )
        return observations


def _run_ts(run_context) -> datetime:
    try:
        return datetime.fromisoformat(run_context.run_timestamp)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
