from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hunter.discovery.models import CandidateObservation, SourceType
from hunter.discovery.store import CandidateStore, ObservationWithCandidate
from hunter.evidence.resolver import EvidenceResolver
from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor
from hunter.llm.models import ExtractionResult
from hunter.pipeline import Pipeline
from tests.fakes import FakeFetcher, FakeGroundedExtractor


AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)


class RecordingResolver(EvidenceResolver):
    def __init__(self):
        self.calls = []

    def propose_urls(self, candidate_domain, observations=None):
        self.calls.append((candidate_domain, observations))
        return super().propose_urls(candidate_domain, observations)


class StaticResolver(EvidenceResolver):
    def __init__(self, urls):
        self.urls = list(urls)

    def propose_urls(self, candidate_domain, observations=None):
        return list(self.urls)


class FailingGroundedExtractor:
    def extract(self, evidence, as_of: str) -> ExtractionResult:
        return ExtractionResult(ok=False, failure_reason="grounding mismatch")


class IncompleteGroundedExtractor:
    def extract(self, evidence, as_of: str) -> ExtractionResult:
        return ExtractionResult(ok=True)


class ExplodingGroundedExtractor:
    def extract(self, evidence, as_of: str) -> ExtractionResult:
        raise ValueError("invalid normalized value near sk-supersecret123456")


def _seed_candidate(
    tmp_path: Path,
    claim: str,
    body: str,
    *,
    resolver: EvidenceResolver | None = None,
    fetcher: FakeFetcher | None = None,
    sources_path: Path | None = None,
) -> tuple[Pipeline, FakeFetcher, FakeGroundedExtractor, EvidenceResolver]:
    data_dir = tmp_path / "data"
    store = CandidateStore(data_dir / "candidates.json")
    observation = CandidateObservation(
        observation_id="third-party-acme",
        source_type=SourceType.third_party_registry,
        source_url="https://registry.example/acme",
        source_title="Acme listing",
        claim=claim,
        discovered_at=AS_OF,
        raw_metadata={
            "asserted_docs_url": "https://acme.ai/pricing",
            "asserted_free_tier": claim,
        },
    )
    store.ingest(
        [
            ObservationWithCandidate(
                observation=observation,
                candidate_id="acme",
                provider_name="Acme",
                canonical_domain_hint="acme.ai",
            )
        ]
    )
    fetcher = fetcher or FakeFetcher()
    fetcher.default_body = body
    extractor = FakeGroundedExtractor()
    resolver = resolver or RecordingResolver()
    pipeline = Pipeline(
        data_dir=data_dir,
        as_of=AS_OF,
        resolver=resolver,
        fetcher=fetcher,
        extractor=extractor,
        sources_path=sources_path,
    )
    pipeline.validator = OfficialEvidenceValidator(
        [TrustAnchor(provider_id="acme", domains=["acme.ai"], provenance="test", reviewed_at="2026-09-01")]
    )
    return pipeline, fetcher, extractor, resolver


def test_pipeline_prefers_manually_reviewed_evidence_urls(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        """
trust_anchors:
  - provider_id: acme
    domains: [acme.ai]
    allow_subdomains: true
    provenance: manual review
    reviewed_at: '2026-09-18'
reviewed_evidence:
  acme:
    - url: https://acme.ai/docs/free-credits
      source_type: pricing
""".strip(),
        encoding="utf-8",
    )
    fetcher = FakeFetcher(
        {"https://acme.ai/docs/free-credits": "New accounts receive $50 in free API credits."}
    )
    pipeline, fetcher, _extractor, resolver = _seed_candidate(
        tmp_path,
        "third-party assertion",
        "unused",
        fetcher=fetcher,
        sources_path=sources_path,
    )

    pipeline.build_evidence()

    assert fetcher.calls == [("https://acme.ai/docs/free-credits", "acme")]
    assert getattr(resolver, "calls", []) == []
    evidence = pipeline.evidence_store.list()
    assert len(evidence) == 1
    assert evidence[0].source_type == "pricing"


def test_pipeline_uses_fetched_body_and_never_asserted_claim_or_as_of_as_policy_date(
    tmp_path: Path,
) -> None:
    pipeline, fetcher, extractor, resolver = _seed_candidate(
        tmp_path,
        "THIRD_PARTY_ASSERTED_FREE_BUT_NOT_EVIDENCE",
        "Official free tier programmatic API offer.",
    )

    summary = pipeline.run()

    assert resolver.calls
    proposed_observations = resolver.calls[0][1]
    assert any(
        item["raw_metadata"]["asserted_docs_url"] == "https://acme.ai/pricing"
        for item in proposed_observations
    )
    assert any(url == "https://acme.ai/pricing" for url, _ in fetcher.calls)
    evidence = pipeline.evidence_store.list()
    assert evidence
    assert all(ev.content_excerpt == "Official free tier programmatic API offer." for ev in evidence)
    assert all("THIRD_PARTY_ASSERTED" not in (ev.content_excerpt or "") for ev in evidence)
    assert all(ev.effective_at is None for ev in evidence)
    assert extractor.calls
    assert all(call[1] == "Official free tier programmatic API offer." for call in extractor.calls)
    assert summary["free_confirmed"] == 1


def test_third_party_fake_free_claim_plus_official_url_cannot_confirm(
    tmp_path: Path,
) -> None:
    pipeline, _fetcher, extractor, _resolver = _seed_candidate(
        tmp_path,
        "THIRD_PARTY_ASSERTED_FREE_PROGRAMMATIC_API",
        "Payment is required for the API plan.",
    )

    summary = pipeline.run()

    assert summary["free_confirmed"] == 0
    assert pipeline.registry.list_providers() == []
    assert extractor.calls
    assert all(call[1] == "Payment is required for the API plan." for call in extractor.calls)


def test_pipeline_without_real_extractor_fails_closed(tmp_path: Path) -> None:
    pipeline, _fetcher, _extractor, _resolver = _seed_candidate(
        tmp_path,
        "third-party free assertion",
        "Official free tier programmatic API offer.",
    )
    pipeline.extractor = None

    summary = pipeline.run()

    assert summary["free_confirmed"] == 0
    assert pipeline.registry.list_providers() == []


def test_pipeline_reports_sanitized_extraction_failure_per_candidate(
    tmp_path: Path,
) -> None:
    pipeline, _fetcher, _extractor, _resolver = _seed_candidate(
        tmp_path,
        "third-party free assertion",
        "Official free tier programmatic API offer.",
    )
    pipeline.extractor = FailingGroundedExtractor()

    summary = pipeline.run()

    assert summary["detail"]["candidate_outcomes"] == [
        {
            "candidate_id": "acme",
            "outcome": "extraction_failed",
            "reason": "grounding mismatch",
        }
    ]


def test_pipeline_reports_confirmation_rejection_per_candidate(tmp_path: Path) -> None:
    pipeline, _fetcher, _extractor, _resolver = _seed_candidate(
        tmp_path,
        "third-party free assertion",
        "Official free tier programmatic API offer.",
    )
    pipeline.extractor = IncompleteGroundedExtractor()

    summary = pipeline.run()

    assert summary["detail"]["candidate_outcomes"][0]["candidate_id"] == "acme"
    assert summary["detail"]["candidate_outcomes"][0]["outcome"] == "confirmation_rejected"
    assert "offer_kind" in summary["detail"]["candidate_outcomes"][0]["reason"]


def test_pipeline_reports_sanitized_unexpected_candidate_error(tmp_path: Path) -> None:
    pipeline, _fetcher, _extractor, _resolver = _seed_candidate(
        tmp_path,
        "third-party free assertion",
        "Official free tier programmatic API offer.",
    )
    pipeline.extractor = ExplodingGroundedExtractor()

    summary = pipeline.run()

    assert summary["errors"] == 1
    outcome = summary["detail"]["candidate_outcomes"][0]
    assert outcome["candidate_id"] == "acme"
    assert outcome["outcome"] == "processing_error"
    assert "ValueError" in outcome["reason"]
    assert "sk-supersecret123456" not in outcome["reason"]
    assert "[REDACTED]" in outcome["reason"]


def test_pipeline_resolves_priority_before_extraction(tmp_path: Path) -> None:
    resolver = StaticResolver(
        ["https://acme.ai/pricing", "https://acme.ai/blog/free-tier"]
    )
    fetcher = FakeFetcher(
        {
            "https://acme.ai/pricing": "Payment is required for the API plan.",
            "https://acme.ai/blog/free-tier": "Official free tier programmatic API offer.",
        }
    )
    pipeline, _fetcher, extractor, _resolver = _seed_candidate(
        tmp_path,
        "third-party assertions are not evidence",
        "unused",
        resolver=resolver,
        fetcher=fetcher,
    )

    summary = pipeline.run()

    assert summary["detail"]["validation"]["OFFICIAL"] == 2
    assert summary["detail"]["contradictions"] == {"resolved": 1, "unresolved": 0}
    assert pipeline._resolved_evidence["acme"].url == "https://acme.ai/pricing"
    assert extractor.calls
    assert extractor.calls[0][1:] == (
        "Payment is required for the API plan.",
        AS_OF.isoformat(),
    )
    assert summary["free_confirmed"] == 0
    assert pipeline.registry.list_providers() == []


def test_pipeline_blocks_equal_priority_contradiction_before_extraction(tmp_path: Path) -> None:
    resolver = StaticResolver(["https://acme.ai/pricing", "https://acme.ai/plans"])
    fetcher = FakeFetcher(
        {
            "https://acme.ai/pricing": "Official free tier programmatic API offer.",
            "https://acme.ai/plans": "Payment is required for the API plan.",
        }
    )
    pipeline, _fetcher, extractor, _resolver = _seed_candidate(
        tmp_path,
        "third-party assertions are not evidence",
        "unused",
        resolver=resolver,
        fetcher=fetcher,
    )

    summary = pipeline.run()

    assert summary["detail"]["validation"]["OFFICIAL"] == 2
    assert summary["detail"]["contradictions"] == {"resolved": 0, "unresolved": 1}
    assert extractor.calls == []
    assert summary["free_confirmed"] == 0
    assert pipeline.registry.list_providers() == []
    assert summary["detail"]["candidate_outcomes"][0]["candidate_id"] == "acme"
    assert summary["detail"]["candidate_outcomes"][0]["outcome"] == "unresolved_contradiction"
    assert "equal-priority conflict" in summary["detail"]["candidate_outcomes"][0]["reason"]
