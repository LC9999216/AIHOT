"""Provider registry schema and orthogonal free-offer contracts (AGENTS.md 4.3-4.5).

This module owns the canonical representations for Provider and FreeOffer.
There is deliberately no alternate free-offer enum anywhere else.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..discovery.models import _require_aware_iso, _validate_urlsafe


class OfferKind(str, enum.Enum):
    """Orthogonal offer kind (AGENTS.md 4.3)."""

    free_tier = "free_tier"
    free_credit = "free_credit"
    trial = "trial"
    promotion = "promotion"
    unknown = "unknown"


class QuotaMode(str, enum.Enum):
    """How the free quota behaves."""

    unmetered = "unmetered"
    renewing = "renewing"
    one_time = "one_time"
    unknown = "unknown"


class RenewalPeriod(str, enum.Enum):
    """Renewal period for renewing quotas; nullable."""

    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    custom = "custom"


class AccessMethod(str, enum.Enum):
    """How a client programmatically accesses the API."""

    api_key = "api_key"
    keyless = "keyless"
    oauth = "oauth"
    unknown = "unknown"


class OfferStatus(str, enum.Enum):
    """Determined by code from facts and an explicit as_of. Not an offer kind."""

    active = "active"
    expired = "expired"
    unknown = "unknown"


class ProviderStatus(str, enum.Enum):
    """Stage-one provider lifecycle states (AGENTS.md section 6)."""

    DISCOVERED = "DISCOVERED"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    EVIDENCE_VERIFIED = "EVIDENCE_VERIFIED"
    FREE_CONFIRMED = "FREE_CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    NOT_FREE = "NOT_FREE"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class FreeOffer(BaseModel):
    """Orthogonal free-offer properties (AGENTS.md 4.3)."""

    model_config = ConfigDict(extra="forbid")

    offer_kind: OfferKind = OfferKind.unknown
    quota_mode: QuotaMode = QuotaMode.unknown
    renewal_period: Optional[RenewalPeriod] = None
    access_method: AccessMethod = AccessMethod.unknown
    offer_status: OfferStatus = OfferStatus.unknown
    description: Optional[str] = None
    quota_text: Optional[str] = None
    expires_at: Optional[datetime] = None

    @field_validator("expires_at")
    @classmethod
    def _expires_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            return _require_aware_iso(v, "expires_at")
        return v


class ProviderRequirements(BaseModel):
    """Provider requirements (AGENTS.md 4.5)."""

    model_config = ConfigDict(extra="forbid")

    signup_required: Optional[bool] = None
    phone_required: Optional[bool] = None
    card_required: Optional[bool] = None
    commercial_use_allowed: Optional[bool] = None
    regional_restrictions: Optional[str] = None


class GroundedUrl(BaseModel):
    """A URL grounded in evidence (FIX-004).

    Every non-null ProviderSetup URL must carry an evidence citation:
    which evidence record, the exact quote, and its offsets.

    Review round 2: the URL, evidence_id, and quote must be non-empty; the
    URL scheme must be http(s); offsets must be non-negative and ordered
    (0 <= start < end); and :meth:`verify_against` checks the quote actually
    appears at those offsets in the cited evidence content.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    evidence_id: str
    quote: str
    start_offset: int = 0
    end_offset: int = 0

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("grounded url must not be empty")
        from urllib.parse import urlparse

        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"grounded url must be http(s): {v!r}")
        return v

    @field_validator("evidence_id", "quote")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("grounded citation fields must be non-empty")
        return v

    @model_validator(mode="after")
    def _ordered_offsets(self) -> "GroundedUrl":
        if self.start_offset < 0 or self.end_offset < 0:
            raise ValueError("offsets must be non-negative")
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self

    def verify_against(self, evidence: Any) -> bool:
        """True when the quote exists at exactly these offsets in evidence."""
        text = getattr(evidence, "content_excerpt", None) or ""
        if getattr(evidence, "evidence_id", None) != self.evidence_id:
            return False
        if self.end_offset > len(text):
            return False
        return text[self.start_offset : self.end_offset] == self.quote


class ProviderSetup(BaseModel):
    """Provider setup URLs grounded in evidence (FIX-004).

    All fields are optional; a URL is stored only if grounded evidence exists.
    Missing fields default to None. No third-party registry values are copied.
    """

    model_config = ConfigDict(extra="forbid")

    signup_url: Optional[GroundedUrl] = None
    api_key_url: Optional[GroundedUrl] = None
    setup_instructions_url: Optional[GroundedUrl] = None


class ProviderApi(BaseModel):
    """Provider API surface (AGENTS.md 4.5)."""

    model_config = ConfigDict(extra="forbid")

    base_url: Optional[str] = None
    openai_compatible: Optional[bool] = None
    api_documentation_url: Optional[str] = None


class ProviderLimits(BaseModel):
    """Documented rate limits; unknown numeric limits stay null (AGENTS.md 4.5)."""

    model_config = ConfigDict(extra="forbid")

    rpm: Optional[int] = None
    tpm: Optional[int] = None
    rpd: Optional[int] = None
    tpd: Optional[int] = None
    other: Optional[str] = None


class ScoreMetadata(BaseModel):
    """Score result attached to a Provider (AGENTS.md 11)."""

    model_config = ConfigDict(extra="forbid")

    score: int
    score_version: str = "v1"
    as_of: datetime
    config_digest: str = ""
    breakdown: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def _as_of_aware(cls, v: datetime) -> datetime:
        return _require_aware_iso(v, "as_of")


class Provider(BaseModel):
    """Canonical Provider record (AGENTS.md 4.4)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    canonical_domain: Optional[str] = None
    status: ProviderStatus = ProviderStatus.DISCOVERED
    free_offer: FreeOffer = Field(default_factory=FreeOffer)
    models: List[str] = Field(default_factory=list)
    api: ProviderApi = Field(default_factory=ProviderApi)
    requirements: ProviderRequirements = Field(default_factory=ProviderRequirements)
    limits: ProviderLimits = Field(default_factory=ProviderLimits)
    evidence_ids: List[str] = Field(default_factory=list)
    official_docs: List[str] = Field(default_factory=list)
    setup: Optional[ProviderSetup] = None
    first_discovered: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_verified: Optional[datetime] = None
    verification_confidence: Optional[int] = None
    free_score: Optional[int] = None
    score_metadata: Optional[ScoreMetadata] = None
    revision: int = 0
    last_event_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _validate_urlsafe(v, "id")

    @field_validator("first_discovered", "last_verified")
    @classmethod
    def _times(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            return _require_aware_iso(v, "timestamp")
        return v

    @field_validator("verification_confidence", "free_score")
    @classmethod
    def _scores(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("scores must be in 0..100")
        return v

    @field_validator("revision")
    @classmethod
    def _revision(cls, v: int) -> int:
        if v < 0:
            raise ValueError("revision must be a non-negative integer")
        return v

    @model_validator(mode="after")
    def _free_confirmed_gates(self) -> "Provider":
        if self.status is ProviderStatus.FREE_CONFIRMED:
            if not self.evidence_ids:
                raise ValueError("FREE_CONFIRMED records must contain evidence IDs")
            if self.verification_confidence is None:
                raise ValueError("FREE_CONFIRMED records must contain verification_confidence")
            if self.score_metadata is None:
                raise ValueError("FREE_CONFIRMED records must contain score metadata")
        return self


# --- legacy free-type mapping ------------------------------------------------

_LEGACY_KIND: Dict[str, OfferKind] = {
    "permanent_free": OfferKind.free_tier,
    "renewing_quota": OfferKind.free_tier,
    "daily_free": OfferKind.free_tier,
    "monthly_free": OfferKind.free_tier,
    "signup_credit": OfferKind.free_credit,
    "trial_credit": OfferKind.trial,
    "promotion": OfferKind.promotion,
    "keyless_free": OfferKind.unknown,
    "expired": OfferKind.unknown,
}

_LEGACY_QUOTA: Dict[str, QuotaMode] = {
    "permanent_free": QuotaMode.unmetered,
    "renewing_quota": QuotaMode.renewing,
    "daily_free": QuotaMode.renewing,
    "monthly_free": QuotaMode.renewing,
    "signup_credit": QuotaMode.one_time,
    "trial_credit": QuotaMode.one_time,
    "promotion": QuotaMode.unknown,
    "keyless_free": QuotaMode.unknown,
    "expired": QuotaMode.unknown,
}

_LEGACY_RENEWAL: Dict[str, Optional[RenewalPeriod]] = {
    "permanent_free": None,
    "renewing_quota": None,
    "daily_free": RenewalPeriod.daily,
    "monthly_free": RenewalPeriod.monthly,
    "signup_credit": None,
    "trial_credit": None,
    "promotion": None,
    "keyless_free": None,
    "expired": None,
}


def map_legacy_free_type(legacy: str) -> FreeOffer:
    """Map a legacy free-type value to orthogonal offer-field hints (AGENTS.md 4.3).

    Unknown values map to ``unknown`` fields and never invent values.
    """
    key = str(legacy).strip().lower()
    if key == "keyless_free":
        return FreeOffer(offer_kind=OfferKind.unknown, access_method=AccessMethod.keyless)
    if key == "expired":
        return FreeOffer(offer_kind=OfferKind.unknown, offer_status=OfferStatus.expired)
    return FreeOffer(
        offer_kind=_LEGACY_KIND.get(key, OfferKind.unknown),
        quota_mode=_LEGACY_QUOTA.get(key, QuotaMode.unknown),
        renewal_period=_LEGACY_RENEWAL.get(key),
    )
