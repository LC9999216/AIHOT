"""X (Twitter) discovery adapter (TASK: remaining-work T5, 2026-09-21).

X has no official free search API. Without a bearer token this adapter is
disabled by default; with a credential supplied through the named
``X_BEARER_TOKEN`` environment variable it performs one bounded recent-search
request against the official v2 endpoint and converts posts into
CandidateObservations. It never verifies providers and never stores secrets.

Network is fully injectable so tests stay offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..discovery.models import CandidateObservation, SourceType


class XCollector:
    """Collect observations from the official X v2 recent search endpoint."""

    def __init__(
        self,
        transport: Any,
        bearer_token: Optional[str],
        base_url: str = "https://api.twitter.com/2/tweets/search/recent",
        max_results: int = 10,
        enabled: Optional[bool] = None,
    ):
        self.transport = transport
        self.bearer_token = bearer_token
        self.base_url = base_url
        self.max_results = max_results
        self._enabled = enabled if enabled is not None else bool(bearer_token)

    def enabled(self) -> bool:
        """No official free search API: disabled without a bearer token."""
        return self._enabled and bool(self.bearer_token)

    def collect(self, query: str, run_context) -> List[CandidateObservation]:
        if not self.enabled():
            return []
        params: Dict[str, Any] = {
            "query": query,
            "max_results": min(max(self.max_results, 10), 100),
            "tweet.fields": "created_at,author_id",
        }
        payload = self.transport.get(
            self.base_url, params=params, headers={"Authorization": f"Bearer {self.bearer_token}"}
        )
        posts = payload.get("data", []) if isinstance(payload, dict) else []
        includes = payload.get("includes", {}).get("users", []) if isinstance(payload, dict) else []
        users_by_id = {u.get("id"): u for u in includes if isinstance(u, dict)}
        observations: List[CandidateObservation] = []
        for post in posts[: self.max_results]:
            if not isinstance(post, dict):
                continue
            post_id = post.get("id")
            author = users_by_id.get(post.get("author_id"), {})
            username = author.get("username", "unknown")
            if not post_id:
                continue
            text = (post.get("text") or "").strip()
            title = f"@{username} on X"
            observations.append(
                CandidateObservation(
                    observation_id=f"x-{post_id}",
                    source_type=SourceType.x,
                    source_url=f"https://x.com/{username}/status/{post_id}",
                    source_title=title,
                    claim=text or title,
                    matched_query=query,
                    discovered_at=_run_ts(run_context),
                    raw_metadata={
                        "x_author_id": post.get("author_id"),
                        "x_username": username,
                        "x_created_at": post.get("created_at"),
                    },
                )
            )
        return observations


def _run_ts(run_context) -> datetime:
    try:
        return datetime.fromisoformat(run_context.run_timestamp)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
