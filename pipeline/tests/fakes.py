from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Union

from hunter.evidence.fetcher import FetchResult, FetcherError
from hunter.llm.models import ExtractionResult, GroundedField


FREE_BODY = "Official free tier programmatic API offer."


class FakeFetcher:
    def __init__(
        self,
        bodies: Dict[str, Union[str, bytes]] | None = None,
        max_pages_per_provider: int = 6,
    ):
        self.bodies = bodies or {}
        self.default_body: Union[str, bytes] = FREE_BODY
        self.calls: List[tuple[str, str]] = []
        self.max_pages_per_provider = max_pages_per_provider
        self._page_counts: Dict[str, int] = {}

    def fetch(self, url: str, provider_id: str) -> FetchResult:
        count = self._page_counts.get(provider_id, 0)
        if count >= self.max_pages_per_provider:
            raise FetcherError(
                f"provider {provider_id!r} exceeded page limit {self.max_pages_per_provider}"
            )
        self._page_counts[provider_id] = count + 1
        self.calls.append((url, provider_id))
        body = self.bodies.get(url, self.default_body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        now_dt = datetime.now(timezone.utc)
        return FetchResult(
            status=200,
            headers={"content-type": "text/plain"},
            body=body,
            final_url=url,
            original_url=url,
            redirect_chain=[],
            content_sha256=hashlib.sha256(body).hexdigest(),
            retrieved_from_origin=True,
            retrieval_method="safe_fetch",
            retrieved_at=now_dt,
            connected_ip="93.184.216.34",
        )


class FakeGroundedExtractor:
    def __init__(self):
        self.calls = []

    def extract(self, evidence, as_of: str) -> ExtractionResult:
        self.calls.append((evidence.evidence_id, evidence.content_excerpt, as_of))
        text = evidence.content_excerpt or ""
        phrase = "free tier"
        if phrase not in text.lower():
            return ExtractionResult(ok=True)
        start = text.lower().index(phrase)
        quote = text[start : start + len(phrase)]
        fields = [
            GroundedField(
                field="offer_kind",
                value="free_tier",
                evidence_id=evidence.evidence_id,
                quote=quote,
                start_offset=start,
                end_offset=start + len(quote),
            ),
            GroundedField(
                field="quota_mode",
                value="renewing",
                evidence_id=evidence.evidence_id,
                quote=quote,
                start_offset=start,
                end_offset=start + len(quote),
            ),
            GroundedField(
                field="access_method",
                value="api_key",
                evidence_id=evidence.evidence_id,
                quote=quote,
                start_offset=start,
                end_offset=start + len(quote),
            ),
        ]
        return ExtractionResult(
            ok=True,
            offer_kind="free_tier",
            quota_mode="renewing",
            access_method="api_key",
            grounded_fields=fields,
        )
