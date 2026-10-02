"""Evidence URL resolver (TASK-007/008).

Proposes bounded candidate evidence URLs from a candidate identity. The
resolver produces *plain URLs to fetch* — it never decides officiality.
High search rank, URL text, or name similarity never create a trust anchor.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

MAX_PROPOSED_URLS = 10

# Document kinds in evidence-priority order (AGENTS.md 8.3).
_KIND_PATHS = [
    ("pricing", ["/pricing", "/plans", "/pricing-plans", "/billing"]),
    ("api-docs", ["/api", "/docs/api", "/developer", "/developers", "/reference"]),
    ("docs", ["/docs", "/documentation", "/help"]),
    ("blog", ["/blog", "/news", "/announcements"]),
    ("root", ["/"]),
]


class EvidenceResolver:
    """Proposes bounded candidate URLs for evidence fetching."""

    def propose_urls(
        self,
        candidate_domain: Optional[str],
        observations: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        urls: List[str] = []
        seen: set = set()

        def add(url: str) -> None:
            if url in seen or len(urls) >= MAX_PROPOSED_URLS:
                return
            seen.add(url)
            urls.append(url)

        # From observations: explicit doc URLs and repository homepages.
        for obs in observations or []:
            raw_metadata = obs.get("raw_metadata") if isinstance(obs, dict) else None
            candidates = [obs, raw_metadata] if isinstance(raw_metadata, dict) else [obs]
            for source in candidates:
                if not isinstance(source, dict):
                    continue
                for key in (
                    "docs_url",
                    "asserted_docs_url",
                    "upstream_docs_url",
                    "url",
                    "html_url",
                    "homepage",
                ):
                    value = source.get(key)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        add(value)

        # From domain: common document paths.
        if candidate_domain:
            base = _base_for_domain(candidate_domain)
            if base:
                for _kind, paths in _KIND_PATHS:
                    for path in paths:
                        add(urljoin(base, path))

        return urls


def _base_for_domain(domain: str) -> Optional[str]:
    host = domain.strip().lower().rstrip(".")
    if not host or "://" in host:
        try:
            parsed = urlparse(domain)
            host = parsed.netloc or parsed.path
        except ValueError:
            return None
    return f"https://{host}"
