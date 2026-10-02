"""TASK-010 tests: Verification Confidence + Free Score (fixed weights)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from hunter.evidence.models import Evidence, Officiality
from hunter.registry.schema import (
    FreeOffer,
    OfferKind,
    OfferStatus,
    ProviderApi,
    ProviderRequirements,
)
from hunter.scoring.models import ScoreConfig, ScoreMetadata
from hunter.scoring.scores import (
    free_score,
    verification_confidence,
)
from hunter.scoring.config import load_scoring_config

AS_OF = "2026-09-01T00:00:00+00:00"
AS_OF_DT = datetime.fromisoformat(AS_OF)


def _requirements(**kw) -> ProviderRequirements:
    return ProviderRequirements(**kw)


def _api(**kw) -> ProviderApi:
    return ProviderApi(**kw)


def _evidence(
    evidence_id: str,
    source_type: str,
    retrieved: str = "2026-08-01T00:00:00+00:00",
    official: Officiality = Officiality.OFFICIAL,
    explicit_free: bool = True,
    explicit_api: bool = True,
    effective: Optional[str] = "2026-08-01T00:00:00+00:00",
    published: Optional[str] = None,
) -> Evidence:
    claim = "free plan"
    if explicit_api:
        claim += " programmatic API"
    return Evidence(
        evidence_id=evidence_id,
        provider_id="acme",
        url=f"https://acme.ai/{evidence_id}",
        source_type=source_type,
        officiality=official,
        retrieved_at=datetime.fromisoformat(retrieved),
        claim=claim,
        effective_at=datetime.fromisoformat(effective) if effective else None,
        published_at=datetime.fromisoformat(published) if published else None,
        content_excerpt=claim,
    )


def _config() -> ScoreConfig:
    return load_scoring_config(Path("config/scoring.yaml"))


def _offer(**kw) -> FreeOffer:
    return FreeOffer(**kw)


# --- config digest ----------------------------------------------------------


def test_scoring_config_loads_with_digest() -> None:
    cfg = _config()
    assert cfg.verification_threshold == 80
    assert cfg.config_digest
    assert len(cfg.config_digest) == 40  # sha256 prefix


# --- verification confidence ------------------------------------------------


def test_base_points_pricing() -> None:
    evs = [_evidence("ev-1", "pricing")]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    assert score == 45 + 20 + 15 + 10 + 5  # pricing + explicit free + explicit api + fresh + grounded


def test_base_points_api_docs() -> None:
    evs = [_evidence("ev-1", "api-docs")]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    assert score == 40 + 20 + 15 + 10 + 5


def test_second_consistent_source_bonus() -> None:
    evs = [
        _evidence("ev-1", "pricing"),
        _evidence("ev-2", "api-docs", retrieved="2026-08-01T00:00:00+00:00"),
    ]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    # raw sum 105 clamps to 100; without the +10 bonus it would be 95
    assert score == 100


def test_ambiguous_wording_penalty() -> None:
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        retrieved_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        effective_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        claim="might be free",
        content_excerpt="might be free",
    )
    score = verification_confidence([ev], _config(), as_of=AS_OF_DT)
    assert score == 45 + 10 + 5 - 20  # fresh + grounded, ambiguous -20


def test_stale_evidence_penalty() -> None:
    evs = [_evidence("ev-1", "pricing", retrieved="2026-01-01T00:00:00+00:00")]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    # >180 days old: -20; still fresh? no (not within 90d)
    assert score == 45 + 20 + 15 + 5 - 20


def test_missing_date_penalty() -> None:
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        retrieved_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        claim="free",
        content_excerpt="free",
    )
    ev.published_at = None
    ev.effective_at = None
    # usable date missing -> -10
    score = verification_confidence([ev], _config(), as_of=AS_OF_DT)
    assert score == 45 + 20 + 10 + 5 - 10


def test_unresolved_contradiction_hard_block() -> None:
    evs = [
        _evidence("ev-1", "pricing"),
        _evidence("ev-2", "pricing"),
    ]
    evs[0].claim = "free forever"
    evs[1].claim = "no longer free"
    evs[0].validation_notes = ["contradiction: ev-1 vs ev-2"]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    assert score == 0  # hard-block clamps to 0


def test_likely_official_not_counted() -> None:
    evs = [_evidence("ev-1", "pricing", official=Officiality.LIKELY_OFFICIAL)]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    # only OFFICIAL evidence counts: no base -> 0
    assert score == 0


def test_score_clamped_to_100() -> None:
    evs = [
        _evidence("ev-1", "pricing"),
        _evidence("ev-2", "api-docs"),
        _evidence("ev-3", "docs"),
        _evidence("ev-4", "blog"),
    ]
    score = verification_confidence(evs, _config(), as_of=AS_OF_DT)
    assert 0 <= score <= 100


def test_score_metadata_round_trip() -> None:
    meta = ScoreMetadata(
        score=85,
        as_of=AS_OF_DT,
        config_digest=_config().config_digest,
        breakdown={"base": 45},
    )
    restored = ScoreMetadata.model_validate_json(meta.model_dump_json())
    assert restored == meta


def test_verification_threshold_constant() -> None:
    assert _config().verification_threshold == 80


# --- free score -------------------------------------------------------------


def test_expired_offer_scores_zero() -> None:
    offer = _offer(offer_status=OfferStatus.expired)
    score = free_score(offer, _requirements(), _api(), [], False, _config(), AS_OF_DT)
    assert score == 0


def test_free_score_ongoing_unmetered() -> None:
    offer = _offer(
        offer_status=OfferStatus.active,
        quota_mode="unmetered",
        access_method="keyless",
    )
    req = _requirements(card_required=False, phone_required=False)
    api = _api(openai_compatible=True)
    score = free_score(offer, req, api, ["m1"], True, _config(), AS_OF_DT)
    assert score == 25 + 25 + 10 + 10 + 5 + 10 + 5 + 5  # ongoing+unmetered+keyless+nocard+nophone+openai+models+limits


def test_free_score_renewing() -> None:
    offer = _offer(
        offer_status=OfferStatus.active,
        quota_mode="renewing",
    )
    req = _requirements(card_required=True, phone_required=False, commercial_use_allowed=True)
    api = _api()
    score = free_score(offer, req, api, [], False, _config(), AS_OF_DT)
    assert score == 25 + 20 - 10 + 5 + 10  # ongoing+renewing+card-10+nophone+commercial


def test_free_score_one_time() -> None:
    offer = _offer(offer_status=OfferStatus.active, quota_mode="one_time")
    score = free_score(offer, _requirements(), _api(), [], False, _config(), AS_OF_DT)
    assert score == 25 + 5  # ongoing + one_time


def test_free_score_expiry_buckets() -> None:
    base = dict(offer_status="active", quota_mode="one_time")
    far = _offer(expires_at="2027-01-01T00:00:00+00:00", **base)
    mid = _offer(expires_at="2026-10-15T00:00:00+00:00", **base)
    near = _offer(expires_at="2026-09-20T00:00:00+00:00", **base)
    # an explicit expiry excludes the "ongoing" +25; only the bucket applies
    assert free_score(far, _requirements(), _api(), [], False, _config(), AS_OF_DT) == 15 + 5  # >90d
    assert free_score(mid, _requirements(), _api(), [], False, _config(), AS_OF_DT) == 10 + 5  # 31-90d
    assert free_score(near, _requirements(), _api(), [], False, _config(), AS_OF_DT) == 5 + 5  # 8-30d


def test_free_score_regional_restriction() -> None:
    offer = _offer(offer_status=OfferStatus.active, quota_mode="one_time")
    req = _requirements(regional_restrictions="not available in the EU")
    score = free_score(offer, req, _api(), [], False, _config(), AS_OF_DT)
    assert score == 25 + 5 - 10  # ongoing + one_time - regional


def test_free_score_commercial_forbidden() -> None:
    offer = _offer(offer_status=OfferStatus.active, quota_mode="one_time")
    req = _requirements(commercial_use_allowed=False)
    score = free_score(offer, req, _api(), [], False, _config(), AS_OF_DT)
    assert score == 25 + 5 - 15  # ongoing + one_time - forbidden


def test_free_score_unknown_contributes_zero() -> None:
    offer = _offer(offer_status=OfferStatus.active)  # everything unknown
    score = free_score(offer, _requirements(), _api(), [], False, _config(), AS_OF_DT)
    assert score == 25  # ongoing only


def test_free_score_clamped() -> None:
    offer = _offer(offer_status=OfferStatus.active)
    score = free_score(offer, _requirements(), _api(), [], False, _config(), AS_OF_DT)
    assert 0 <= score <= 100
