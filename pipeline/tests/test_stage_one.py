"""TASK-010 acceptance: stage-one pipeline, boundaries, and idempotency.

Covers the fully offline end-to-end fixture run from Candidate observations
through registry/history, a second identical run proving byte-level
idempotency, score date boundaries at 7/8/30/31/90/91/180/181 days, clamp
boundaries, confirmation thresholds 79/80, third-party/LIKELY evidence,
missing promotion expiry, missing grounding, consumer-chat-only offers,
unresolved contradictions, and current paid pricing overriding an older free
claim.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hunter.evidence.models import Evidence, EvidenceProvenance, Officiality
from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor
from hunter.llm.models import ExtractionResult, GroundedField
from hunter.pipeline import Pipeline
from hunter.registry.confirmation import (
    ConfirmationError,
    ConfirmationInput,
    confirm_provider,
)
from hunter.registry.schema import (
    FreeOffer,
    OfferStatus,
    ProviderApi,
    ProviderRequirements,
    ProviderStatus,
)
from hunter.registry.store import ProviderRegistry
from hunter.scoring.config import load_scoring_config
from hunter.scoring.free_score import free_score
from hunter.scoring.confidence import verification_confidence
from tests.fakes import FakeFetcher, FakeGroundedExtractor

FIXTURE = Path("tests/fixtures/upstream/free-llm-api-hub-v2.9.0.json")
AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)
SCORING = load_scoring_config(Path("config/scoring.yaml"))


def _pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(
        data_dir=tmp_path / "data",
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )


# --- end-to-end offline fixture run -----------------------------------------


def test_stage_one_offline_end_to_end(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    summary = pipeline.run()

    assert summary["candidates_processed"] == 69
    assert summary["errors"] == 0
    assert summary["free_confirmed"] >= 1
    assert summary["providers_created"] == summary["free_confirmed"]
    assert summary["detail"]["validation"]["OFFICIAL"] >= 1

    # registry + history persisted
    assert (tmp_path / "data" / "providers.json").is_file()
    assert (tmp_path / "data" / "history.jsonl").is_file()
    assert (tmp_path / "data" / "candidates.json").is_file()
    assert (tmp_path / "data" / "evidence.json").is_file()

    # every confirmed provider satisfies the hard gates
    for provider in pipeline.registry.list_providers():
        if provider.status is ProviderStatus.FREE_CONFIRMED:
            assert provider.evidence_ids
            assert provider.free_offer.offer_status is OfferStatus.active
            assert provider.verification_confidence >= 80
            assert provider.last_verified is not None
            assert provider.score_metadata is not None
            assert provider.score_metadata.score_version == "v1"
            assert provider.score_metadata.as_of == AS_OF
            assert provider.score_metadata.config_digest


def test_stage_one_second_run_is_byte_identical(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = Pipeline(
        data_dir=data_dir,
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    s1 = first.run()
    providers_1 = (data_dir / "providers.json").read_bytes()
    history_1 = (data_dir / "history.jsonl").read_bytes()

    second = Pipeline(
        data_dir=data_dir,
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    s2 = second.run()
    providers_2 = (data_dir / "providers.json").read_bytes()
    history_2 = (data_dir / "history.jsonl").read_bytes()

    assert hashlib.sha256(providers_1).hexdigest() == hashlib.sha256(providers_2).hexdigest()
    assert providers_1 == providers_2
    assert history_1 == history_2  # no duplicate/no-op history events
    assert s2["providers_created"] == 0
    assert s2["providers_updated"] == 0
    assert s2["free_confirmed"] == s1["free_confirmed"]
    assert s2["errors"] == 0


def test_stage_one_distinct_dirs_same_hash(tmp_path: Path) -> None:
    a = Pipeline(
        data_dir=tmp_path / "a",
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    a.run()
    b = Pipeline(
        data_dir=tmp_path / "b",
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    b.run()
    ha = hashlib.sha256((tmp_path / "a" / "providers.json").read_bytes()).hexdigest()
    hb = hashlib.sha256((tmp_path / "b" / "providers.json").read_bytes()).hexdigest()
    assert ha == hb


def test_stage_one_third_run_still_stable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    hashes = []
    for _ in range(3):
        Pipeline(
            data_dir=data_dir,
            seed_path=FIXTURE,
            as_of=AS_OF,
            fetcher=FakeFetcher(),
            extractor=FakeGroundedExtractor(),
        ).run()
        hashes.append(hashlib.sha256((data_dir / "providers.json").read_bytes()).hexdigest())
    assert len(set(hashes)) == 1


def test_one_candidate_failure_does_not_corrupt_others(tmp_path: Path, monkeypatch) -> None:
    pipeline = _pipeline(tmp_path)
    pipeline.import_seed()
    pipeline.build_evidence()
    pipeline.validate_evidence()

    original = pipeline._extraction_for

    def flaky(candidate_id: str):
        if candidate_id == "groq":
            raise RuntimeError("simulated per-candidate failure")
        return original(candidate_id)

    monkeypatch.setattr(pipeline, "_extraction_for", flaky)
    outcomes = pipeline.score_and_confirm()
    assert outcomes["errors"] >= 1
    assert outcomes["free_confirmed"] >= 1


# --- score date boundaries ---------------------------------------------------


def _offer(expires_at=None, status=OfferStatus.active, quota="one_time") -> FreeOffer:
    return FreeOffer(offer_status=status, quota_mode=quota, expires_at=expires_at)


def _requirements() -> ProviderRequirements:
    return ProviderRequirements()


def _api() -> ProviderApi:
    return ProviderApi()


@pytest.mark.parametrize(
    "days,expected",
    [
        (7, 0),  # <=7 days bucket
        (8, 5),  # 8-30
        (30, 5),
        (31, 10),  # 31-90
        (90, 10),
        (91, 15),  # >90
    ],
)
def test_free_score_expiry_boundaries(days: int, expected: int) -> None:
    expiry = AS_OF + timedelta(days=days)
    score = free_score(_offer(expiry, quota="unknown"), _requirements(), _api(), [], False, SCORING, AS_OF)
    assert score == expected


def test_free_score_unknown_quota_is_neutral() -> None:
    score = free_score(_offer(None, quota="unknown"), _requirements(), _api(), [], False, SCORING, AS_OF)
    assert score == 25  # ongoing only; unknown quota contributes 0


def test_free_score_clamp_lower_bound() -> None:
    offer = _offer(None, quota="unknown")
    req = ProviderRequirements(card_required=True, phone_required=True, commercial_use_allowed=False)
    score = free_score(offer, req, _api(), [], False, SCORING, AS_OF)
    # 25 ongoing -10 card -5 phone -15 forbidden commercial = -5, clamped to 0
    assert score == 0


def test_free_score_expired_is_zero() -> None:
    score = free_score(
        _offer(AS_OF - timedelta(days=1), status=OfferStatus.expired),
        _requirements(),
        _api(),
        [],
        False,
        SCORING,
        AS_OF,
    )
    assert score == 0


@pytest.mark.parametrize(
    "days,expect_fresh,expect_stale",
    [
        (90, True, False),
        (91, False, False),
        (180, False, False),
        (181, False, True),
    ],
)
def test_verification_confidence_retrieval_boundaries(
    days: int, expect_fresh: bool, expect_stale: bool
) -> None:
    retrieved = AS_OF - timedelta(days=days)
    base = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        retrieved_at=retrieved,
        effective_at=retrieved,
        claim="free plan programmatic API",
        content_excerpt="free plan programmatic API",
    )
    score = verification_confidence([base], SCORING, AS_OF)
    fresh = 10
    stale = -20
    expected = 45 + 20 + 15 + 5 + (fresh if expect_fresh else 0) + (stale if expect_stale else 0)
    assert score == expected


def test_verification_confidence_clamped_to_100() -> None:
    evs = [
        Evidence(
            evidence_id=f"ev-{i}",
            provider_id="acme",
            url=f"https://acme.ai/{i}",
            source_type=src,
            officiality=Officiality.OFFICIAL,
            retrieved_at=AS_OF,
            effective_at=AS_OF,
            claim="free plan programmatic API",
            content_excerpt="free plan programmatic API",
        )
        for i, src in enumerate(("pricing", "api-docs", "docs", "blog"))
    ]
    assert verification_confidence(evs, SCORING, AS_OF) == 100


# --- confirmation thresholds and gates --------------------------------------


def _anchor() -> TrustAnchor:
    return TrustAnchor(
        provider_id="acme",
        domains=["acme.ai"],
        allow_subdomains=True,
        provenance="manual",
        reviewed_at="2026-09-01",
    )


def _official_evidence() -> Evidence:
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url="https://acme.ai/pricing",
        final_url="https://acme.ai/pricing",
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=AS_OF,
    )
    return Evidence(
        evidence_id="ev-acme",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        provenance=provenance,
        retrieved_at=AS_OF,
        effective_at=AS_OF,
        claim="free plan programmatic API",
        content_excerpt="free plan programmatic API",
    )


def _extraction(**kw) -> ExtractionResult:
    quote = "free plan programmatic API"
    base = dict(
        ok=True,
        offer_kind="free_tier",
        access_method="api_key",
        grounded_fields=[
            GroundedField(
                field="offer_kind",
                value="free_tier",
                evidence_id="ev-acme",
                quote="free plan",
                start_offset=0,
                end_offset=9,
            ),
            GroundedField(
                field="access_method",
                value="api_key",
                evidence_id="ev-acme",
                quote="programmatic API",
                start_offset=10,
                end_offset=len(quote),
            ),
        ],
    )
    base.update(kw)
    return ExtractionResult(**base)


def _registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(
        providers_path=tmp_path / "providers.json",
        history_path=tmp_path / "history.jsonl",
    )


def _input(**kw) -> ConfirmationInput:
    base = dict(
        provider_name="Acme AI",
        canonical_domain="acme.ai",
        evidence=_official_evidence(),
        extraction=_extraction(),
        verification_confidence=80,
        free_score=60,
        as_of=AS_OF,
        source_metadata={"pipeline": "test"},
        reason="official pricing evidence",
    )
    base.update(kw)
    return ConfirmationInput(**base)


def test_threshold_80_confirms(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    provider = confirm_provider(_input(verification_confidence=80), registry, OfficialEvidenceValidator([_anchor()]))
    assert provider.status is ProviderStatus.FREE_CONFIRMED


def test_threshold_79_rejected_without_mutation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    with pytest.raises(ConfirmationError):
        confirm_provider(_input(verification_confidence=79), registry, validator)
    assert registry.list_providers() == []
    assert not (tmp_path / "providers.json").exists() or json.loads(
        (tmp_path / "providers.json").read_text(encoding="utf-8")
    )["items"] == []


def test_third_party_only_evidence_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    third_party = Evidence(
        evidence_id="ev-agg",
        provider_id="acme",
        url="https://free-apis.example/acme",
        source_type="pricing",
        officiality=Officiality.THIRD_PARTY,
        retrieved_at=AS_OF,
        claim="free plan",
        content_excerpt="free plan",
    )
    with pytest.raises(ConfirmationError):
        confirm_provider(_input(evidence=third_party), registry, validator)


def test_likely_official_evidence_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    likely = Evidence(
        evidence_id="ev-likely",
        provider_id="acme",
        url="https://docs.acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.UNCONFIRMED,
        retrieved_at=AS_OF,
        claim="free plan",
        content_excerpt="free plan",
    )
    # docs.acme.ai is not permitted (anchor forbids subdomains for this check)
    strict = OfficialEvidenceValidator(
        [
            TrustAnchor(
                provider_id="acme",
                domains=["acme.ai"],
                allow_subdomains=False,
                provenance="manual",
                reviewed_at="2026-09-01",
            )
        ]
    )
    with pytest.raises(ConfirmationError):
        confirm_provider(_input(evidence=likely), registry, strict)


def test_missing_promotion_expiry_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    with pytest.raises(ConfirmationError):
        confirm_provider(
            _input(extraction=_extraction(offer_kind="promotion")), registry, validator
        )


def test_missing_grounding_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    with pytest.raises(ConfirmationError):
        confirm_provider(
            _input(extraction=ExtractionResult(ok=False, failure_reason="ungrounded")),
            registry,
            validator,
        )


def test_unknown_offer_kind_never_confirms(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    with pytest.raises(ConfirmationError, match="offer_kind"):
        confirm_provider(
            _input(extraction=ExtractionResult(ok=True)), registry, validator
        )


def test_confirmation_defensively_rejects_non_null_field_without_citation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    with pytest.raises(ConfirmationError, match="grounded"):
        confirm_provider(
            _input(
                extraction=ExtractionResult(
                    ok=True,
                    offer_kind="free_tier",
                    access_method="api_key",
                )
            ),
            registry,
            validator,
        )


def test_consumer_chat_only_offer_rejected(tmp_path: Path) -> None:
    """A free consumer chat UI without programmatic API scope cannot confirm."""
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    chat_only = ExtractionResult(
        ok=True,
        offer_kind="free_tier",
        access_method="unknown",
        openai_compatible=None,
        base_url=None,
        quota_text="free web chat",
        grounded_fields=[
            GroundedField(
                field="offer_kind",
                value="free_tier",
                evidence_id="ev-chat",
                quote="free web chat",
                start_offset=0,
                end_offset=13,
            ),
            GroundedField(
                field="access_method",
                value="unknown",
                evidence_id="ev-chat",
                quote="free web chat",
                start_offset=0,
                end_offset=13,
            ),
            GroundedField(
                field="quota_text",
                evidence_id="ev-chat",
                quote="free web chat",
                start_offset=0,
                end_offset=13,
            ),
        ],
    )
    # no documented programmatic API scope in the evidence
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url="https://acme.ai/pricing",
        final_url="https://acme.ai/pricing",
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=AS_OF,
    )
    evidence = Evidence(
        evidence_id="ev-chat",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        provenance=provenance,
        retrieved_at=AS_OF,
        effective_at=AS_OF,
        claim="free web chat",
        content_excerpt="free web chat",
    )
    # The score alone can reach the threshold, but the API-scope hard gate blocks it.
    vc = verification_confidence([evidence], SCORING, AS_OF)
    assert vc >= 80
    with pytest.raises(ConfirmationError) as exc:
        confirm_provider(
            _input(evidence=evidence, extraction=chat_only, verification_confidence=vc),
            registry,
            validator,
        )
    assert "programmatic API scope" in str(exc.value)


def test_unresolved_contradiction_blocks(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    ev = _official_evidence()
    ev.validation_notes = ["contradiction: ev-a vs ev-b"]
    with pytest.raises(ConfirmationError):
        confirm_provider(_input(evidence=ev), registry, validator)


def test_current_paid_pricing_blocks_confirmation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    paid = Evidence(
        evidence_id="ev-paid",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        retrieved_at=AS_OF,
        effective_at=AS_OF,
        claim="subscription required",
        content_excerpt="subscription required",
    )
    # a paid pricing page never reaches the confidence threshold on a free claim
    vc = verification_confidence([paid], SCORING, AS_OF)
    assert vc < 80
    with pytest.raises(ConfirmationError):
        confirm_provider(
            _input(evidence=paid, verification_confidence=vc), registry, validator
        )


def test_confirm_failure_returns_typed_decision(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    try:
        confirm_provider(_input(verification_confidence=10), registry, validator)
    except ConfirmationError as exc:
        assert str(exc)
    else:
        pytest.fail("expected ConfirmationError")
    assert registry.list_providers() == []


def test_expired_offer_does_not_stay_confirmed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    validator = OfficialEvidenceValidator([_anchor()])
    expired = _extraction(offer_kind="trial", expires_at=(AS_OF - timedelta(days=1)).isoformat())
    with pytest.raises(ConfirmationError):
        confirm_provider(_input(extraction=expired), registry, validator)
