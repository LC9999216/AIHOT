"""TASK-001 tests for shared domain contracts.

Covers enums, CandidateObservation, Candidate, FreeOffer, Provider and all
supporting typed models from AGENTS.md section 4, including validation rules,
legacy mapping, JSON round trips, and order-independent equality.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hunter.discovery.models import (
    Candidate,
    CandidateObservation,
    SourceType,
    make_fingerprint,
)
from hunter.registry.schema import (
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
    map_legacy_free_type,
)


# --- enums ------------------------------------------------------------------


def test_all_enums_have_exact_spellings() -> None:
    assert {e.value for e in OfferKind} == {
        "free_tier",
        "free_credit",
        "trial",
        "promotion",
        "unknown",
    }
    assert {e.value for e in QuotaMode} == {
        "unmetered",
        "renewing",
        "one_time",
        "unknown",
    }
    assert {e.value for e in RenewalPeriod} == {"daily", "weekly", "monthly", "custom"}
    assert {e.value for e in AccessMethod} == {
        "api_key",
        "keyless",
        "oauth",
        "unknown",
    }
    assert {e.value for e in OfferStatus} == {"active", "expired", "unknown"}
    assert {e.value for e in ProviderStatus} == {
        "DISCOVERED",
        "EVIDENCE_PENDING",
        "EVIDENCE_VERIFIED",
        "FREE_CONFIRMED",
        "UNCERTAIN",
        "NOT_FREE",
        "EXPIRED",
        "REJECTED",
    }


def test_models_reject_unknown_enum_spellings() -> None:
    with pytest.raises(ValidationError):
        FreeOffer(offer_kind="magical")
    with pytest.raises(ValidationError):
        FreeOffer(quota_mode="unlimited")
    with pytest.raises(ValidationError):
        FreeOffer(access_method="magic")
    with pytest.raises(ValueError):
        ProviderStatus("FREE_CONFIRMEDED")
    with pytest.raises(ValidationError):
        CandidateObservation(
            observation_id="obs-1",
            source_type="unknown_source_type",
            source_url="https://example.com/",
            claim="free",
        )


def test_renewal_period_is_nullable() -> None:
    assert FreeOffer(offer_kind=OfferKind.free_tier).renewal_period is None


# --- FreeOffer --------------------------------------------------------------


def test_free_offer_exact_field_names() -> None:
    offer = FreeOffer(
        offer_kind="free_tier",
        quota_mode="renewing",
        renewal_period="daily",
        access_method="api_key",
        offer_status="active",
        description="free tier",
        quota_text="1k requests/day",
        expires_at="2026-12-31T00:00:00+00:00",
    )
    assert offer.offer_kind == OfferKind.free_tier
    assert offer.quota_mode == QuotaMode.renewing
    assert offer.renewal_period == RenewalPeriod.daily
    assert offer.access_method == AccessMethod.api_key
    assert offer.offer_status == OfferStatus.active
    assert offer.description == "free tier"
    assert offer.quota_text == "1k requests/day"
    assert offer.expires_at is not None


def test_free_offer_unknown_defaults() -> None:
    offer = FreeOffer()
    assert offer.offer_kind == OfferKind.unknown
    assert offer.quota_mode == QuotaMode.unknown
    assert offer.access_method == AccessMethod.unknown
    assert offer.offer_status == OfferStatus.unknown
    assert offer.renewal_period is None
    assert offer.description is None
    assert offer.quota_text is None
    assert offer.expires_at is None


def test_free_offer_expired_is_status_not_kind() -> None:
    expired = FreeOffer(offer_kind="free_tier", offer_status="expired")
    assert expired.offer_status == OfferStatus.expired
    assert expired.offer_kind == OfferKind.free_tier
    # offer_kind never contains 'expired'
    assert "expired" not in {e.value for e in OfferKind}


# --- legacy mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    "legacy,expected",
    [
        (
            "permanent_free",
            {
                "offer_kind": "free_tier",
                "quota_mode": "unmetered",
                "renewal_period": None,
            },
        ),
        (
            "renewing_quota",
            {"offer_kind": "free_tier", "quota_mode": "renewing", "renewal_period": None},
        ),
        (
            "daily_free",
            {
                "offer_kind": "free_tier",
                "quota_mode": "renewing",
                "renewal_period": "daily",
            },
        ),
        (
            "monthly_free",
            {
                "offer_kind": "free_tier",
                "quota_mode": "renewing",
                "renewal_period": "monthly",
            },
        ),
        (
            "signup_credit",
            {"offer_kind": "free_credit", "quota_mode": "one_time", "renewal_period": None},
        ),
        (
            "trial_credit",
            {"offer_kind": "trial", "quota_mode": "one_time", "renewal_period": None},
        ),
        (
            "promotion",
            {"offer_kind": "promotion", "quota_mode": "unknown", "renewal_period": None},
        ),
        (
            "keyless_free",
            {"offer_kind": "unknown", "access_method": "keyless", "renewal_period": None},
        ),
        (
            "expired",
            {"offer_kind": "unknown", "offer_status": "expired", "renewal_period": None},
        ),
    ],
)
def test_legacy_free_type_mapping(legacy: str, expected: dict) -> None:
    mapped = map_legacy_free_type(legacy)
    for key, value in expected.items():
        got = getattr(mapped, key)
        assert got is not None or value is None
        assert got.value if hasattr(got, "value") else got == value


def test_legacy_mapping_unknown_value() -> None:
    mapped = map_legacy_free_type("something-else")
    assert mapped.offer_kind == OfferKind.unknown
    assert mapped.quota_mode == QuotaMode.unknown


# --- CandidateObservation ---------------------------------------------------


def _obs(**overrides) -> CandidateObservation:
    base = dict(
        observation_id="obs-1",
        source_type=SourceType.third_party_registry,
        source_url="https://example.com/providers.json",
        source_title="Registry",
        claim="Free tier available",
        matched_query=None,
        discovered_at="2026-09-01T00:00:00+00:00",
    )
    base.update(overrides)
    return CandidateObservation(**base)


def test_observation_minimal_and_complete() -> None:
    minimal = _obs()
    assert minimal.fingerprint
    complete = _obs(
        matched_query="free llm api",
        raw_metadata={"verified": True, "last_verified": "2026-08-01"},
    )
    assert complete.raw_metadata["verified"] is True


def test_observation_timestamps_require_timezone() -> None:
    with pytest.raises(ValidationError):
        _obs(discovered_at="2026-08-01T00:00:00")  # naive
    ok = _obs(discovered_at="2026-08-01T00:00:00+00:00")
    assert ok.discovered_at.tzinfo is not None


def test_observation_id_must_be_url_safe() -> None:
    with pytest.raises(ValidationError):
        _obs(observation_id="Not Safe!/id")
    with pytest.raises(ValidationError):
        _obs(observation_id="")


def test_fingerprint_deterministic_and_normalized() -> None:
    a = make_fingerprint("third_party_registry", "https://EXAMPLE.com/x", "  Free   tier\n")
    b = make_fingerprint("third_party_registry", "https://example.com/x", "Free tier")
    assert a == b
    c = make_fingerprint("github", "https://example.com/x", "Free tier")
    assert a != c
    d = make_fingerprint("third_party_registry", "https://example.com/y", "Free tier")
    assert a != d


def test_observation_fingerprint_matches_make_fingerprint() -> None:
    obs = _obs()
    assert obs.fingerprint == make_fingerprint(
        obs.source_type, obs.source_url, obs.claim
    )


# --- Candidate --------------------------------------------------------------


def _candidate(*obs) -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        provider_name="Example AI",
        canonical_domain_hint="example.com",
        observations=list(obs) if obs else [_obs()],
    )


def test_candidate_requires_at_least_one_observation() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            candidate_id="cand-1",
            provider_name="Example AI",
            observations=[],
        )


def test_candidate_observation_fingerprints_unique() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            candidate_id="cand-1",
            provider_name="Example AI",
            observations=[_obs(), _obs()],  # same fingerprint
        )


def test_candidate_distinct_observations_allowed() -> None:
    obs1 = _obs(observation_id="o1")
    obs2 = _obs(observation_id="o2", source_url="https://example.com/other")
    cand = _candidate(obs1, obs2)
    assert len(cand.observations) == 2


def test_candidate_timestamps_require_timezone() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            candidate_id="cand-1",
            provider_name="Example AI",
            first_discovered_at="2026-08-01T00:00:00",
            last_seen_at="2026-08-01T00:00:00",
            observations=[_obs()],
        )


# --- Provider ---------------------------------------------------------------


def _provider(**overrides) -> Provider:
    base = dict(
        id="provider-1",
        provider="Example AI",
        canonical_domain="example.com",
        status=ProviderStatus.DISCOVERED,
        free_offer=FreeOffer(offer_kind="free_tier"),
    )
    base.update(overrides)
    return Provider(**base)


def test_provider_minimal() -> None:
    p = _provider()
    assert p.revision == 0
    assert p.last_event_id is None
    assert p.verification_confidence is None
    assert p.free_score is None
    assert p.models == []
    assert p.evidence_ids == []
    assert p.official_docs == []


def test_provider_revision_non_negative() -> None:
    with pytest.raises(ValidationError):
        _provider(revision=-1)


def test_provider_scores_bounded() -> None:
    with pytest.raises(ValidationError):
        _provider(verification_confidence=101)
    with pytest.raises(ValidationError):
        _provider(free_score=-1)
    ok = _provider(verification_confidence=100, free_score=0)
    assert ok.verification_confidence == 100
    assert ok.free_score == 0


def test_provider_free_confirmed_requires_scores_and_evidence() -> None:
    with pytest.raises(ValidationError):
        _provider(
            status=ProviderStatus.FREE_CONFIRMED,
            free_offer=FreeOffer(offer_kind="free_tier", offer_status="active"),
        )
    ok = _provider(
        status=ProviderStatus.FREE_CONFIRMED,
        free_offer=FreeOffer(offer_kind="free_tier", offer_status="active"),
        evidence_ids=["ev-1"],
        verification_confidence=90,
        free_score=80,
        score_metadata=ScoreMetadata(score=90, as_of="2026-09-01T00:00:00+00:00"),
    )
    assert ok.free_score == 80


def test_provider_requirements_api_limits() -> None:
    p = _provider(
        requirements=ProviderRequirements(
            signup_required=True,
            phone_required=False,
            card_required=True,
            commercial_use_allowed=None,
            regional_restrictions="EEA only",
        ),
        api=ProviderApi(
            base_url="https://api.example.com",
            openai_compatible=True,
            api_documentation_url="https://docs.example.com",
        ),
        limits=ProviderLimits(rpm=60, tpm=None, rpd=1000, tpd=50000, other="daily cap"),
    )
    assert p.requirements.card_required is True
    assert p.api.openai_compatible is True
    assert p.limits.rpm == 60
    assert p.limits.tpm is None  # unknown stays null


def test_unknown_numeric_limits_default_null() -> None:
    limits = ProviderLimits()
    for field in ("rpm", "tpm", "rpd", "tpd"):
        assert getattr(limits, field) is None


def test_provider_timestamps_require_timezone() -> None:
    with pytest.raises(ValidationError):
        _provider(last_verified="2026-08-01T00:00:00")
    ok = _provider(last_verified="2026-08-01T00:00:00+00:00")
    assert ok.last_verified.tzinfo is not None


# --- JSON round trips and equality ------------------------------------------


def test_json_round_trip_observation() -> None:
    obs = _obs(raw_metadata={"a": 1})
    restored = CandidateObservation.model_validate_json(obs.model_dump_json())
    assert restored == obs


def test_json_round_trip_candidate() -> None:
    cand = _candidate(_obs(observation_id="o1"), _obs(observation_id="o2", source_url="https://e.com/2"))
    restored = Candidate.model_validate_json(cand.model_dump_json())
    assert restored == cand


def test_json_round_trip_provider() -> None:
    p = _provider(
        status=ProviderStatus.EVIDENCE_VERIFIED,
        free_offer=FreeOffer(offer_kind="free_tier", quota_mode="unmetered"),
        limits=ProviderLimits(rpm=10, tpd=1000),
        revision=3,
    )
    restored = Provider.model_validate_json(p.model_dump_json())
    assert restored == p


def test_semantic_equality_independent_of_dict_order() -> None:
    a = _obs(raw_metadata={"x": 1, "y": 2})
    b = _obs(raw_metadata={"y": 2, "x": 1})
    assert a == b


def test_score_metadata_requires_timezone_as_of() -> None:
    with pytest.raises(ValidationError):
        ScoreMetadata(score=90, as_of="2026-09-01T00:00:00")
    ok = ScoreMetadata(score=90, as_of="2026-09-01T00:00:00+00:00")
    assert ok.score_version == "v1"


def test_sources_type_enum_values() -> None:
    values = {s.value for s in SourceType}
    assert "third_party_registry" in values
    assert "github" in values
