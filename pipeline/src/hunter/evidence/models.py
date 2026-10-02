"""Evidence model and store (AGENTS.md 8.1).

New evidence starts ``UNCONFIRMED``. The resolver/fetcher never mark evidence
``OFFICIAL``. Identity derives from canonical URL plus content fingerprint.

Provenance (FIX-001): created only by SafeFetcher; imported/constructed
evidence carries no provenance and cannot become OFFICIAL without it.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..discovery.models import _require_aware_iso


class _VisibleTextParser(HTMLParser):
    """Collect visible HTML text while excluding executable and style data."""

    _HIDDEN_TAGS = {"script", "style", "noscript", "template"}
    _CHROME_TAGS = {"header", "nav", "footer", "aside"}
    _MAIN_TAGS = {"main", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._chrome_depth = 0
        self._main_depth = 0
        self.metadata_parts: List[str] = []
        self.main_parts: List[str] = []
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "meta":
            attributes = {key.lower(): value for key, value in attrs if value is not None}
            descriptor = (attributes.get("name") or attributes.get("property") or "").lower()
            content = attributes.get("content", "").strip()
            if descriptor in {"description", "og:description", "twitter:description"} and content:
                if content not in self.metadata_parts:
                    self.metadata_parts.append(content)
        if normalized_tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        if normalized_tag in self._CHROME_TAGS:
            self._chrome_depth += 1
        if normalized_tag in self._MAIN_TAGS:
            self._main_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1
        if normalized_tag in self._CHROME_TAGS and self._chrome_depth:
            self._chrome_depth -= 1
        if normalized_tag in self._MAIN_TAGS and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth or self._chrome_depth or not data.strip():
            return
        if self._main_depth:
            self.main_parts.append(data)
        else:
            self.parts.append(data)


def _evidence_text(body_text: str, headers: Any) -> str:
    """Return normalized visible text for HTML, otherwise decoded body text."""
    content_type = ""
    if hasattr(headers, "items"):
        content_type = next(
            (str(value) for key, value in headers.items() if str(key).lower() == "content-type"),
            "",
        ).lower()
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        return body_text

    parser = _VisibleTextParser()
    parser.feed(body_text)
    parser.close()
    ordered_parts = parser.metadata_parts + parser.main_parts + parser.parts
    return " ".join(" ".join(ordered_parts).split())


_FREE_OFFER_MARKERS = re.compile(
    r"(?i)\bfree\s+(?:tier|credit|credits|account|accounts)\b"
    r"|\bfor\s+free\b|\bno[- ]cost\b|\bnot\s+free\b|\btrial\b"
    r"|[$€£]\s?\d+(?:\.\d+)?"
)


def _bounded_evidence_excerpt(text: str, excerpt_length: int) -> str:
    """Keep explicit free-offer sentences when the normalized page is long."""
    limit = max(0, int(excerpt_length))
    if len(text) <= limit:
        return text
    if limit == 0:
        return ""

    spans: List[tuple[int, int]] = []
    for match in _FREE_OFFER_MARKERS.finditer(text):
        start = max(text.rfind(".", 0, match.start()), text.rfind("!", 0, match.start()), text.rfind("?", 0, match.start())) + 1
        while start < len(text) and text[start].isspace():
            start += 1
        endings = [pos for pos in (text.find(".", match.end()), text.find("!", match.end()), text.find("?", match.end())) if pos >= 0]
        end = (min(endings) + 1) if endings else min(len(text), match.end() + 240)
        if not spans or (start, end) != spans[-1]:
            spans.append((start, end))

    if not spans:
        return text[:limit]
    relevant = " ".join(text[start:end].strip() for start, end in spans if end > start)
    return relevant[:limit]


class Officiality(str, enum.Enum):
    """Officiality of an evidence item (AGENTS.md 8.1)."""

    OFFICIAL = "OFFICIAL"
    LIKELY_OFFICIAL = "LIKELY_OFFICIAL"
    UNCONFIRMED = "UNCONFIRMED"
    THIRD_PARTY = "THIRD_PARTY"
    REJECTED = "REJECTED"


class EvidenceProvenance(BaseModel):
    """Provenance metadata created only by SafeFetcher (FIX-001).

    Only evidence with ``retrieved_from_origin=true`` provenance is eligible
    for OFFICIAL. Imported/constructed evidence must NOT carry provenance.
    """

    model_config = ConfigDict(extra="forbid")

    retrieval_method: str = "safe_fetch"
    original_url: str
    final_url: str
    redirect_chain: List[str] = Field(default_factory=list)
    http_status: int = 200
    content_sha256: str = ""
    retrieved_from_origin: bool = True
    retrieved_at: datetime


class Evidence(BaseModel):
    """One piece of resolved evidence (AGENTS.md 8.1)."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = ""
    candidate_id: Optional[str] = None
    provider_id: Optional[str] = None
    url: str
    normalized_domain: Optional[str] = None
    source_type: str = "page"
    officiality: Officiality = Officiality.UNCONFIRMED
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    title: Optional[str] = None
    claim: Optional[str] = None
    supported_fields: List[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    validation_notes: List[str] = Field(default_factory=list)
    content_fingerprint: str = ""
    content_excerpt: Optional[str] = None
    provenance: Optional[EvidenceProvenance] = None

    @field_validator("evidence_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not v:
            return ""
        if not re.match(r"^[a-z0-9][a-z0-9._-]*$", v):
            raise ValueError("evidence_id must be a lowercase URL-safe string")
        return v

    @model_validator(mode="after")
    def _fill_derived(self) -> "Evidence":
        """Derive canonical url, domain, and content fingerprint at construction."""
        if not self.url:
            return self
        canonical = canonicalize_evidence_url(self.url)
        fingerprint = content_fingerprint(canonical, self.content_excerpt or "")
        domain = normalize_domain(canonical)
        self.url = canonical
        self.normalized_domain = self.normalized_domain or domain
        self.content_fingerprint = self.content_fingerprint or fingerprint
        return self

    @field_validator("retrieved_at", "published_at", "effective_at")
    @classmethod
    def _times(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            return _require_aware_iso(v, "timestamp")
        return v

    def finalize(self) -> "Evidence":
        """Fill derived identity fields (url, domain, fingerprint)."""
        canonical = canonicalize_evidence_url(self.url)
        fingerprint = content_fingerprint(canonical, self.content_excerpt or "")
        domain = normalize_domain(canonical)
        return self.model_copy(
            update={
                "url": canonical,
                "normalized_domain": domain,
                "content_fingerprint": fingerprint,
            }
        )

    @classmethod
    def from_fetch(
        cls,
        fetch_result: Any,
        *,
        provider_id: Optional[str] = None,
        source_type: str = "page",
        claim: Optional[str] = None,
        excerpt_length: int = 2000,
        retrieved_at: Optional[datetime] = None,
        candidate_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> "Evidence":
        """Build evidence from a SafeFetcher FetchResult, populating provenance.

        Only this factory creates Evidence with provenance. The content is
        BOUND to the actually-fetched body (review round 2):

        - the fetch must have returned a 2xx (non-2xx fails closed);
        - ``content_excerpt`` is derived from the decoded body; HTML is reduced
          to normalized visible text before truncation so CSS/script shells do
          not displace the actual evidence;
        - ``claim``, when provided, must be a substring of the fetched body
          so a fabricated claim cannot ride on a real fetch;
        - ``provenance.content_sha256`` is recomputed from the body bytes,
          not taken from the caller.
        """
        status = int(getattr(fetch_result, "status", 0) or 0)
        if not (200 <= status < 300):
            raise ValueError(
                f"refusing to create evidence from non-2xx fetch (status={status})"
            )
        body: bytes = fetch_result.body
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise ValueError("fetch result body is not bytes")
        body_text = bytes(body).decode("utf-8", errors="replace")
        if claim is not None and claim not in body_text:
            raise ValueError("claim is not present in the fetched content")
        sha256 = hashlib.sha256(bytes(body)).hexdigest()
        evidence_text = _evidence_text(body_text, getattr(fetch_result, "headers", {}))
        excerpt = _bounded_evidence_excerpt(evidence_text, excerpt_length)
        final_url = fetch_result.final_url or fetch_result.original_url
        provenance = EvidenceProvenance(
            retrieval_method="safe_fetch",
            original_url=fetch_result.original_url or final_url,
            final_url=final_url,
            redirect_chain=list(fetch_result.redirect_chain or []),
            http_status=status,
            content_sha256=sha256,
            retrieved_from_origin=True,
            retrieved_at=retrieved_at or fetch_result.retrieved_at or datetime.now(timezone.utc),
        )
        return cls(
            evidence_id="",
            candidate_id=candidate_id or provider_id,
            provider_id=provider_id or candidate_id,
            url=final_url,
            source_type=source_type,
            officiality=Officiality.UNCONFIRMED,
            retrieved_at=provenance.retrieved_at,
            title=title or final_url or fetch_result.original_url,
            claim=claim or excerpt[:300],
            content_excerpt=excerpt,
            provenance=provenance,
        )


def normalize_domain(url: str) -> Optional[str]:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def canonicalize_evidence_url(url: str) -> str:
    """Normalize scheme/host/port/path for identity (AGENTS.md 8.2 + 4.2)."""
    stripped = url.strip()
    try:
        parsed = urlparse(stripped)
    except ValueError:
        return stripped
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return stripped
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    port = parsed.port
    if port in (80, 443):
        port = None
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    # strip trailing slash except root
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = ("?" + parsed.query) if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def content_fingerprint(canonical_url: str, excerpt: str) -> str:
    material = f"{canonical_url}\n{excerpt}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:40]


def _new_evidence_id(evidence: Evidence) -> str:
    material = f"{evidence.url}|{evidence.content_fingerprint}"
    return "ev-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "Officiality",
    "Evidence",
    "normalize_domain",
    "canonicalize_evidence_url",
    "content_fingerprint",
    "_new_evidence_id",
]
