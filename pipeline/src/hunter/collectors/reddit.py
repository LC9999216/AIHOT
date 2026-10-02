"""Reddit discovery adapter (TASK: remaining-work T5, 2026-09-21).

Discovery-only: uses Reddit's public, credential-free JSON search endpoint
(`https://www.reddit.com/search.json`) and produces CandidateObservations.
It never verifies providers, never sets trust, and never fetches
authenticated content. Network is fully injectable so tests stay offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from ..discovery.models import CandidateObservation, SourceType


class RedditCollector:
    """Collect observations from Reddit public search JSON results."""

    def __init__(
        self,
        transport: Any,
        base_url: str = "https://www.reddit.com/search.json",
        max_results: int = 25,
        subreddit: Optional[str] = None,
        enabled: bool = True,
    ):
        self.transport = transport
        self.base_url = base_url
        self.max_results = max_results
        self.subreddit = subreddit
        self._enabled = enabled

    def enabled(self) -> bool:
        """Public JSON endpoint needs no credential; config may still disable it."""
        return self._enabled

    def collect(self, query: str, run_context) -> List[CandidateObservation]:
        if not self._enabled:
            return []
        params: Dict[str, Any] = {
            "q": query,
            "limit": self.max_results,
            "sort": "new",
            "restrict_sr": 1 if self.subreddit else 0,
        }
        url = self.base_url
        if self.subreddit:
            url = f"https://www.reddit.com/r/{self.subreddit}/search.json"
        payload = self.transport.get(url, params=params)
        children = (
            payload.get("data", {}).get("children", [])
            if isinstance(payload, dict)
            else []
        )
        observations: List[CandidateObservation] = []
        for child in children[: self.max_results]:
            if not isinstance(child, dict):
                continue
            post = child.get("data", {})
            if not isinstance(post, dict):
                continue
            post_id = post.get("id")
            permalink = post.get("permalink")
            if not post_id or not permalink:
                continue
            title = (post.get("title") or "Reddit post").strip()
            selftext = (post.get("selftext") or "").strip()
            claim = " ".join(f"{title} {selftext}".split()) if selftext else title
            observations.append(
                CandidateObservation(
                    observation_id=f"reddit-{post_id}",
                    source_type=SourceType.reddit,
                    source_url=f"https://www.reddit.com{permalink}",
                    source_title=title,
                    claim=claim or title,
                    matched_query=query,
                    discovered_at=_run_ts(run_context),
                    raw_metadata={
                        "reddit_subreddit": post.get("subreddit"),
                        "reddit_author": post.get("author"),
                        "reddit_score": post.get("score"),
                        "reddit_num_comments": post.get("num_comments"),
                        "reddit_created_utc": post.get("created_utc"),
                        "reddit_link": post.get("url_overridden_by_dest"),
                    },
                )
            )
        return observations


def _run_ts(run_context) -> datetime:
    try:
        return datetime.fromisoformat(run_context.run_timestamp)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def reddit_search_url(query: str) -> str:
    """Human-auditable search URL for a query (metadata only)."""
    return f"https://www.reddit.com/search.json?q={quote_plus(query)}"
