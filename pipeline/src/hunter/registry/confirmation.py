"""Provider confirmation (TASK-010).

``confirm_provider()`` is the ONLY entry into FREE_CONFIRMED. The generic
transition API rejects FREE_CONFIRMED destinations; this module applies the
hard gates from AGENTS.md section 6:
  1. stable Provider identity
  2. >= 1 OFFICIAL evidence supporting a current free programmatic API offer
  3. grounded structured extraction
  4. normalized offer fields
  5. no unresolved higher-priority contradiction
  6. offer_status = active (code-decided from explicit as_of)
  7. known expiry for promotions
  8. Verification Confidence >= 80
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..discovery.models import _require_aware_iso
from ..evidence.models import Evidence, Officiality
from ..evidence.validator import OfficialEvidenceValidator
from ..llm.models import ExtractionResult
from ..llm.structured_extractor import validate_grounding
from ..registry.schema import (
    AccessMethod,
    FreeOffer,
    OfferKind,
    OfferStatus,
    Provider,
    ProviderApi,
    ProviderLimits,
    ProviderRequirements,
    ProviderStatus,
    QuotaMode,
    RenewalPeriod,
    ScoreMetadata,
)
from ..scoring.config import load_scoring_config
from ..scoring.scores import free_score
from .store import ProviderRegistry

CONFIRMATION_THRESHOLD = 80

# Wording that establishes programmatic API applicability.
API_SCOPE_MARKERS = (
    "api",
    "endpoint",
    "programmatic",
    "developer",
    "sdk",
    "openai-compatible",
    "openai compatible",
    "rest",
    "graphql",
)


def _has_programmatic_api_scope(evidence: "Evidence", extraction: "ExtractionResult") -> bool:
    """True when official evidence documents programmatic API applicability.

    A free consumer chat UI is not a free API offer. Scope comes from the
    evidence text or from grounded API fields -- never from the model's opinion.
    """
    if extraction.base_url or extraction.openai_compatible is True:
        return True
    if extraction.access_method and extraction.access_method not in ("unknown",):
        return True
    text = " ".join(
        part
        for part in (
            evidence.claim or "",
            evidence.content_excerpt or "",
            extraction.quota_text or "",
            extraction.description or "",
        )
        if part
    ).lower()
    return any(marker in text for marker in API_SCOPE_MARKERS)


def _extraction_grounding_errors(
    extraction: ExtractionResult, evidence: Evidence
) -> List[str]:
    """Defensively require citations for every non-null extracted field."""
    try:
        errors = validate_grounding(extraction.grounded_fields, [evidence])
    except Exception as exc:  # noqa: BLE001 - malformed citations fail closed
        return [f"invalid grounding structure: {exc}"]

    scalar_fields = (
        "offer_kind",
        "quota_mode",
        "renewal_period",
        "access_method",
        "description",
        "quota_text",
        "expires_at",
        "card_required",
        "phone_required",
        "commercial_use_allowed",
        "signup_required",
        "openai_compatible",
        "base_url",
    )
    refs_by_field: Dict[str, List[Any]] = {}
    for field in extraction.grounded_fields:
        refs_by_field.setdefault(field.field, []).append(field)
    for field_name in scalar_fields:
        value = getattr(extraction, field_name)
        if value is None:
            continue
        refs = refs_by_field.get(field_name, [])
        if not refs:
            errors.append(f"non-null field {field_name!r} has no citation")
            continue
        if field_name in {
            "offer_kind",
            "quota_mode",
            "renewal_period",
            "access_method",
        } and refs[0].value != value:
            errors.append(f"enum field {field_name!r} citation value does not match extraction")

    if extraction.models:
        refs = refs_by_field.get("models", [])
        if len(refs) != len(extraction.models):
            errors.append("non-null field 'models' does not have one citation per model")
    return errors


class ConfirmationError(Exception):
    """Raised when a provider fails a confirmation hard gate."""


@dataclass
class ConfirmationInput:
    provider_name: str
    canonical_domain: str
    evidence: Evidence
    extraction: ExtractionResult
    verification_confidence: int
    free_score: int
    as_of: datetime
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    models: List[str] = field(default_factory=list)
    has_documented_limits: bool = False
    official_docs: List[str] = field(default_factory=list)
    # Full evidence-id snapshot recorded with the confirmation so a later
    # pipeline rerun can detect "evidence unchanged since last commit" and
    # skip redundant re-extraction. Optional; other callers are unaffected.
    evidence_ids_snapshot: Optional[List[str]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            self.as_of = datetime.fromisoformat(str(self.as_of))
        self.as_of = _require_aware_iso(self.as_of, "as_of")


def confirm_provider(
    inp: ConfirmationInput,
    registry: ProviderRegistry,
    validator: OfficialEvidenceValidator,
) -> Provider:
    """Confirm a provider into FREE_CONFIRMED (exclusive entry point)."""
    as_of = inp.as_of
    evidence = validator.validate(inp.evidence)

    # Gate 2: OFFICIAL evidence supporting a current free offer
    if evidence.officiality is not Officiality.OFFICIAL:
        raise ConfirmationError(
            f"confirmation requires OFFICIAL evidence, got {evidence.officiality.value}"
        )
    if not inp.extraction.ok:
        raise ConfirmationError(
            f"confirmation requires grounded extraction: {inp.extraction.failure_reason or 'failed'}"
        )
    grounding_errors = _extraction_grounding_errors(inp.extraction, evidence)
    if grounding_errors:
        raise ConfirmationError(
            "confirmation requires grounded extraction: " + "; ".join(grounding_errors)
        )

    # Gate 2b: the offer must be a programmatic API offer, not a consumer chat UI.
    if not _has_programmatic_api_scope(evidence, inp.extraction):
        raise ConfirmationError(
            "confirmation requires explicit programmatic API scope in official evidence"
        )

    # Gate 5: unresolved contradiction blocks
    notes = " ".join(evidence.validation_notes or []).lower()
    if "contradiction" in notes:
        raise ConfirmationError("unresolved contradiction blocks confirmation")

    # Gate 8: verification confidence threshold
    if inp.verification_confidence < CONFIRMATION_THRESHOLD:
        raise ConfirmationError(
            f"verification confidence {inp.verification_confidence} below threshold {CONFIRMATION_THRESHOLD}"
        )

    # Normalize offer fields (code decides offer_status from as_of)
    offer = _normalize_offer(inp.extraction, as_of)
    if offer.offer_kind is OfferKind.unknown:
        raise ConfirmationError("normalized offer_kind is unknown; cannot confirm")
    if offer.offer_status is not OfferStatus.active:
        raise ConfirmationError("offer_status is not active as of as_of; cannot confirm")

    # Gate 7: promotions require a known expiry
    if offer.offer_kind is OfferKind.promotion and offer.expires_at is None:
        raise ConfirmationError("promotions require a known expiry to confirm")

    requirements = ProviderRequirements(
        signup_required=inp.extraction.signup_required,
        phone_required=inp.extraction.phone_required,
        card_required=inp.extraction.card_required,
        commercial_use_allowed=inp.extraction.commercial_use_allowed,
        regional_restrictions=_regional(inp.extraction),
    )
    api = ProviderApi(
        base_url=inp.extraction.base_url,
        openai_compatible=inp.extraction.openai_compatible,
        api_documentation_url=evidence.url,
    )

    from ..config import DEFAULT_CONFIG_DIR

    config = load_scoring_config(DEFAULT_CONFIG_DIR / "scoring.yaml")
    free_score_value = inp.free_score
    score_metadata = ScoreMetadata(
        score=free_score_value,
        score_version="v1",
        as_of=as_of,
        config_digest=config.config_digest,
        breakdown={"verification_confidence": inp.verification_confidence},
    )

    provider_metadata: Dict[str, Any] = {
        "confirmed_by": "confirm_provider",
        "confirmation_as_of": inp.as_of.isoformat(),
    }
    if inp.evidence_ids_snapshot is not None:
        provider_metadata["pipeline_evidence_ids"] = sorted(set(inp.evidence_ids_snapshot))

    provider = _build_provider(
        inp=inp,
        evidence=evidence,
        offer=offer,
        requirements=requirements,
        api=api,
        score_metadata=score_metadata,
        metadata=provider_metadata,
    )

    # Gate 1: stable identity — reuse an existing provider by canonical domain.
    existing = registry.find_by_domain(provider.canonical_domain)
    if existing is not None:
        provider = provider.model_copy(
            update={
                "id": existing.id,
                "provider": existing.provider or provider.provider,
                "first_discovered": existing.first_discovered,
            }
        )

    return registry.upsert_provider(
        provider,
        reason=inp.reason or "confirmed free programmatic API offer from official evidence",
        source_metadata=inp.source_metadata,
        event_type="provider.confirmed",
    )


def _build_provider(
    inp: ConfirmationInput,
    evidence: Evidence,
    offer: FreeOffer,
    requirements: ProviderRequirements,
    api: ProviderApi,
    score_metadata: ScoreMetadata,
    metadata: Optional[Dict[str, Any]] = None,
) -> Provider:
    provider_id = _slug(inp.provider_name)
    return Provider(
        id=provider_id,
        provider=inp.provider_name,
        canonical_domain=_normalize_domain(inp.canonical_domain),
        status=ProviderStatus.FREE_CONFIRMED,
        free_offer=offer,
        models=inp.models,
        api=api,
        requirements=requirements,
        evidence_ids=[evidence.evidence_id] if evidence.evidence_id else [],
        official_docs=[evidence.url] if evidence.url else [],
        first_discovered=inp.as_of,
        last_verified=inp.as_of,
        verification_confidence=inp.verification_confidence,
        free_score=score_metadata.score,
        score_metadata=score_metadata,
        revision=0,
        metadata=metadata
        if metadata is not None
        else {
            "confirmed_by": "confirm_provider",
            "confirmation_as_of": inp.as_of.isoformat(),
        },
    )


def _normalize_offer(extraction: ExtractionResult, as_of: datetime) -> FreeOffer:
    """Derive a normalized FreeOffer; offer_status is code-decided."""
    expires_at = _parse_dt(extraction.expires_at)
    offer = FreeOffer(
        offer_kind=_kind(extraction.offer_kind),
        quota_mode=_quota(extraction.quota_mode),
        renewal_period=_renewal(extraction.renewal_period),
        access_method=_access(extraction.access_method),
        offer_status=OfferStatus.unknown,
        description=extraction.description,
        quota_text=extraction.quota_text,
        expires_at=expires_at,
    )
    # code-decided offer_status from explicit as_of
    if expires_at is not None and expires_at <= as_of:
        offer.offer_status = OfferStatus.expired
    else:
        offer.offer_status = OfferStatus.active
    return offer


def _kind(value: Optional[str]) -> OfferKind:
    if not value:
        return OfferKind.unknown
    try:
        return OfferKind(value)
    except ValueError:
        return OfferKind.unknown


def _quota(value: Optional[str]) -> QuotaMode:
    if not value:
        return QuotaMode.unknown
    try:
        return QuotaMode(value)
    except ValueError:
        return QuotaMode.unknown


def _renewal(value: Optional[str]) -> Optional[RenewalPeriod]:
    if not value:
        return None
    try:
        return RenewalPeriod(value)
    except ValueError:
        return None


def _access(value: Optional[str]) -> AccessMethod:
    if not value:
        return AccessMethod.unknown
    try:
        return AccessMethod(value)
    except ValueError:
        return AccessMethod.unknown


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _require_aware_iso(dt, "expires_at")


def _regional(extraction: ExtractionResult) -> Optional[str]:
    raw = extraction.raw_model_output or {}
    return raw.get("regional_restrictions") if isinstance(raw, dict) else None


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "provider"


def _normalize_domain(domain: str) -> str:
    return (domain or "").strip().lower().rstrip(".")
