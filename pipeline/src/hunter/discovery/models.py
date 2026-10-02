"""Shared domain contracts: CandidateObservation and Candidate (AGENTS.md 4.1-4.2).

A CandidateObservation is one unverified source occurrence. A Candidate is the
aggregated identity for one provider lead, preserving every distinct
observation. Neither type carries trust decisions.
"""

from __future__ import annotations

import enum
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_URLSAFE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class SourceType(str, enum.Enum):
    """Provenance of a discovery observation."""

    third_party_registry = "third_party_registry"
    github = "github"
    curated_repo = "curated_repo"
    hackernews = "hackernews"
    reddit = "reddit"
    x = "x"
    web_search = "web_search"
    manual = "manual"


def _validate_urlsafe(value: str, field: str) -> str:
    if not isinstance(value, str) or not _URLSAFE_RE.match(value):
        raise ValueError(f"{field} must be a non-empty, lowercase, URL-safe string")
    return value.lower()


def _require_aware_iso(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


class CandidateObservation(BaseModel):
    """One unverified source occurrence. Fields per AGENTS.md 4.1."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    source_type: SourceType
    source_url: str
    source_title: Optional[str] = None
    claim: str
    matched_query: Optional[str] = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""

    @field_validator("observation_id")
    @classmethod
    def _obs_id(cls, v: str) -> str:
        return _validate_urlsafe(v, "observation_id")

    @field_validator("source_url")
    @classmethod
    def _source_url(cls, v: str) -> str:
        if not v:
            raise ValueError("source_url must not be empty")
        return v

    @field_validator("discovered_at")
    @classmethod
    def _discovered_at(cls, v: datetime) -> datetime:
        return _require_aware_iso(v, "discovered_at")

    @model_validator(mode="after")
    def _fill_fingerprint(self) -> "CandidateObservation":
        if not self.fingerprint:
            self.fingerprint = make_fingerprint(
                self.source_type, self.source_url, self.claim
            )
        return self


def normalize_claim(text: str) -> str:
    """Whitespace-normalize a claim (single spaces, stripped)."""
    return " ".join(str(text).split())


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL for fingerprinting (lowercase, strip fragment)."""
    stripped = url.strip()
    if "#" in stripped:
        stripped = stripped.split("#", 1)[0]
    scheme, sep, rest = stripped.partition("://")
    if sep and scheme.isalpha():
        rest = rest.rstrip("/")
        return f"{scheme.lower()}://{rest}"
    return stripped.rstrip("/")


def make_fingerprint(source_type, source_url: str, claim: str) -> str:
    """Deterministic fingerprint from source type, canonical URL, and claim.

    AGENTS.md 4.1: deterministic from normalized source type, canonicalized
    source URL, and whitespace-normalized claim.
    """
    src_type = str(source_type.value if hasattr(source_type, "value") else source_type)
    material = "|".join(
        [
            src_type.lower(),
            canonicalize_url(source_url).lower(),
            normalize_claim(claim).lower(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


class Candidate(BaseModel):
    """Aggregated identity for one provider lead (AGENTS.md 4.2)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    provider_name: str
    canonical_domain_hint: Optional[str] = None
    first_discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    observations: List[CandidateObservation] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id(cls, v: str) -> str:
        return _validate_urlsafe(v, "candidate_id")

    @field_validator("first_discovered_at", "last_seen_at")
    @classmethod
    def _times(cls, v: datetime) -> datetime:
        return _require_aware_iso(v, "timestamp")

    @model_validator(mode="after")
    def _require_observations(self) -> "Candidate":
        if not self.observations:
            raise ValueError("Candidate requires at least one observation")
        return self

    @model_validator(mode="after")
    def _unique_fingerprints(self) -> "Candidate":
        fingerprints = [o.fingerprint for o in self.observations]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Observation fingerprints must be unique inside a Candidate")
        return self

    def add_observation(self, observation: CandidateObservation) -> bool:
        """Add an observation if its fingerprint is new. Returns True if added."""
        if any(o.fingerprint == observation.fingerprint for o in self.observations):
            return False
        self.observations.append(observation)
        return True
