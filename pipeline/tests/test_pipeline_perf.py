"""Performance-refactor behavior tests for the pipeline (TASK-010 perf work).

Covers:
1. candidate-parallel evidence building (max_workers=8): per-candidate URL
   order is preserved and concurrent competing writes to the same evidence
   stay idempotent;
2. concurrent LLM extraction (max_workers=4) over the extraction candidate list;
3. skip_uptodate: a provider whose evidence id set is unchanged since the last
   commit is reused without re-extraction, while a candidate whose evidence
   changed is re-extracted.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from hunter.discovery.models import CandidateObservation, SourceType
from hunter.discovery.store import CandidateStore, ObservationWithCandidate
from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor
from hunter.pipeline import Pipeline
from tests.fakes import FakeFetcher, FakeGroundedExtractor

AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)
ACME_URL = "https://acme.ai/pricing"
ACME_FREE_BODY = "Official free tier programmatic API offer."
BETA_URL = "https://beta.dev/api"
BETA_FREE_BODY = "Beta offers a free tier for its programmatic API."


def _seed_two_candidates(data_dir: Path) -> None:
    store = CandidateStore(data_dir / "candidates.json")
    entries = []
    for candidate_id, provider_name, domain, url in (
        ("acme", "Acme", "acme.ai", ACME_URL),
        ("beta", "Beta", "beta.dev", BETA_URL),
    ):
        entries.append(
            ObservationWithCandidate(
                observation=CandidateObservation(
                    observation_id=f"obs-{candidate_id}",
                    source_type=SourceType.third_party_registry,
                    source_url="https://registry.example/" + candidate_id,
                    source_title=provider_name + " listing",
                    claim="third-party assertion, not evidence",
                    discovered_at=AS_OF,
                    raw_metadata={"asserted_docs_url": url},
                ),
                candidate_id=candidate_id,
                provider_name=provider_name,
                canonical_domain_hint=domain,
            )
        )
    store.ingest(entries)


def _write_sources(tmp_path: Path, reviewed: str) -> Path:
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(reviewed.strip(), encoding="utf-8")
    return sources_path


def _validator(pipeline: Pipeline) -> None:
    pipeline.validator = OfficialEvidenceValidator(
        [
            TrustAnchor(provider_id="acme", domains=["acme.ai"], provenance="test", reviewed_at="2026-09-01"),
            TrustAnchor(provider_id="beta", domains=["beta.dev"], provenance="test", reviewed_at="2026-09-01"),
        ]
    )


def _two_candidate_pipeline(
    tmp_path: Path, data_dir: Path, fetcher: FakeFetcher, extractor
) -> Pipeline:
    _seed_two_candidates(data_dir)
    sources_path = _write_sources(
        tmp_path,
        """
trust_anchors:
  - provider_id: acme
    domains: [acme.ai]
    allow_subdomains: true
    provenance: manual review
    reviewed_at: '2026-09-01'
  - provider_id: beta
    domains: [beta.dev]
    allow_subdomains: true
    provenance: manual review
    reviewed_at: '2026-09-01'
reviewed_evidence:
  acme:
    - url: https://acme.ai/pricing
      source_type: pricing
  beta:
    - url: https://beta.dev/api
      source_type: api-docs
""",
    )
    pipeline = Pipeline(
        data_dir=data_dir,
        as_of=AS_OF,
        fetcher=fetcher,
        extractor=extractor,
        sources_path=sources_path,
    )
    _validator(pipeline)
    return pipeline


class BarrierFetcher(FakeFetcher):
    """Fetcher that forces all provider fetches to overlap in time."""

    def __init__(self, bodies, threads: int = 2):
        super().__init__(bodies)
        self._barrier = threading.Barrier(threads, timeout=10)

    def fetch(self, url: str, provider_id: str):
        self._barrier.wait()
        return super().fetch(url, provider_id)


def test_concurrent_competing_evidence_writes_are_idempotent(tmp_path: Path) -> None:
    """Two candidates competing for the same evidence record (identical URL and
    identical fetched content) concurrently reach EvidenceStore.upsert. The
    persisted evidence must stay a single record, loadable, and stable."""
    data_dir = tmp_path / "data"
    sources_path = _write_sources(
        tmp_path,
        """
trust_anchors:
  - provider_id: acme
    domains: [acme.ai]
    provenance: manual review
    reviewed_at: '2026-09-01'
  - provider_id: beta
    domains: [beta.dev]
    provenance: manual review
    reviewed_at: '2026-09-01'
reviewed_evidence:
  acme:
    - url: https://acme.ai/pricing
      source_type: pricing
  beta:
    - url: https://acme.ai/pricing
      source_type: pricing
""",
    )
    _seed_two_candidates(data_dir)
    fetcher = BarrierFetcher({ACME_URL: ACME_FREE_BODY})
    pipeline = Pipeline(
        data_dir=data_dir,
        as_of=AS_OF,
        fetcher=fetcher,
        extractor=FakeGroundedExtractor(),
        sources_path=sources_path,
    )
    pipeline.validator = OfficialEvidenceValidator(
        [
            TrustAnchor(provider_id="acme", domains=["acme.ai"], provenance="test", reviewed_at="2026-09-01"),
        ]
    )

    created = pipeline.build_evidence()

    items = pipeline.evidence_store.list()
    assert len(items) == 1, [item.model_dump(mode="json") for item in items]
    assert items[0].url == ACME_URL
    assert created == 1
    # A second identical build adds nothing new (idempotent).
    assert pipeline.build_evidence() == 0
    assert len(pipeline.evidence_store.list()) == 1


def test_build_evidence_keeps_per_candidate_url_order(tmp_path: Path) -> None:
    """Under candidate-level concurrency, each candidate's URLs are still
    fetched in the order they were proposed."""
    data_dir = tmp_path / "data"
    store = CandidateStore(data_dir / "candidates.json")
    for tag in ("gh-a", "gh-b"):
        store.ingest(
            [
                ObservationWithCandidate(
                    observation=CandidateObservation(
                        observation_id=f"obs-{tag}",
                        source_type=SourceType.third_party_registry,
                        source_url="https://registry.example/" + tag,
                        source_title=tag,
                        claim="third-party assertion, not evidence",
                        discovered_at=AS_OF,
                        raw_metadata={},
                    ),
                    candidate_id=tag,
                    provider_name=tag,
                    canonical_domain_hint=tag + ".io",
                )
            ]
        )
    sources_path = _write_sources(
        tmp_path,
        """
trust_anchors:
  - provider_id: gh-a
    domains: [gh-a.io]
    provenance: manual review
    reviewed_at: '2026-09-01'
  - provider_id: gh-b
    domains: [gh-b.io]
    provenance: manual review
    reviewed_at: '2026-09-01'
reviewed_evidence:
  gh-a:
    - url: https://gh-a.io/z-late
      source_type: pricing
    - url: https://gh-a.io/a-early
      source_type: pricing
  gh-b:
    - url: https://gh-b.io/z-late
      source_type: pricing
    - url: https://gh-b.io/a-early
      source_type: pricing
""",
    )
    bodies = {
        "https://gh-a.io/z-late": ACME_FREE_BODY,
        "https://gh-a.io/a-early": ACME_FREE_BODY,
        "https://gh-b.io/z-late": ACME_FREE_BODY,
        "https://gh-b.io/a-early": ACME_FREE_BODY,
    }
    pipeline = Pipeline(
        data_dir=data_dir,
        as_of=AS_OF,
        fetcher=FakeFetcher(bodies),
        extractor=FakeGroundedExtractor(),
        sources_path=sources_path,
    )
    pipeline.validator = OfficialEvidenceValidator(
        [
            TrustAnchor(provider_id="gh-a", domains=["gh-a.io"], provenance="test", reviewed_at="2026-09-01"),
            TrustAnchor(provider_id="gh-b", domains=["gh-b.io"], provenance="test", reviewed_at="2026-09-01"),
        ]
    )

    pipeline.build_evidence()

    per_provider: dict[str, list[str]] = {}
    for url, provider in pipeline.fetcher.calls:  # type: ignore[attr-defined]
        per_provider.setdefault(provider, []).append(url)
    for provider in ("gh-a", "gh-b"):
        assert per_provider[provider] == [
            f"https://{provider}.io/z-late",
            f"https://{provider}.io/a-early",
        ]


def test_score_and_confirm_skips_uptodate_and_retries_changed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first_extractor = FakeGroundedExtractor()
    pipeline1 = _two_candidate_pipeline(
        tmp_path,
        data_dir,
        FakeFetcher({ACME_URL: ACME_FREE_BODY, BETA_URL: BETA_FREE_BODY}),
        first_extractor,
    )
    summary1 = pipeline1.run()
    assert summary1["free_confirmed"] == 2
    assert len(first_extractor.calls) == 2
    beta_ids = {
        e.evidence_id for e in pipeline1.evidence_store.list() if e.candidate_id == "beta"
    }
    providers_after_run1 = (data_dir / "providers.json").read_bytes()

    # Second run: nothing changed -> every candidate is skipped (no LLM calls),
    # and the registry bytes are untouched.
    second_extractor = FakeGroundedExtractor()
    pipeline2 = _two_candidate_pipeline(
        tmp_path,
        data_dir,
        FakeFetcher({ACME_URL: ACME_FREE_BODY, BETA_URL: BETA_FREE_BODY}),
        second_extractor,
    )
    summary2 = pipeline2.run()
    assert second_extractor.calls == []
    assert summary2["skip_uptodate"] == 2
    assert summary2["errors"] == 0
    # run 2 skipped everything: registry bytes are untouched.
    assert (data_dir / "providers.json").read_bytes() == providers_after_run1

    # Third run: acme's evidence content changed (new fingerprint -> new
    # evidence id), so only acme is re-extracted; beta is still skipped.
    third_extractor = FakeGroundedExtractor()
    pipeline3 = _two_candidate_pipeline(
        tmp_path,
        data_dir,
        FakeFetcher({ACME_URL: "Official free tier programmatic API offer with new promo terms.", BETA_URL: BETA_FREE_BODY}),
        third_extractor,
    )
    summary3 = pipeline3.run()
    extracted_ids = {call[0] for call in third_extractor.calls}
    assert extracted_ids, third_extractor.calls
    assert extracted_ids.isdisjoint(beta_ids)
    assert summary3["skip_uptodate"] == 1
    # The untouched provider was never rewritten during run 3.
    confirmed = {
        p.id: p for p in pipeline3.registry.list_providers() if p.status.value == "FREE_CONFIRMED"
    }
    # run 3 re-processed only acme: beta stays at its run-1 revision (skipped).
    assert confirmed["beta"].revision == 1


def _shared_pipeline(tmp_path: Path, data_dir: Path, fetcher, extractor):
    return _two_candidate_pipeline(tmp_path, data_dir, fetcher, extractor)


class ThreadRecordingExtractor:
    """Extractor stub that records the calling thread per extract."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def extract(self, evidence, as_of: str):
        self.calls.append((evidence.evidence_id, threading.get_ident()))
        return FakeGroundedExtractor().extract(evidence, as_of)


def test_extraction_runs_concurrently_across_threads(tmp_path: Path) -> None:
    """The step-2 extraction path runs on a worker thread pool, not inline."""
    data_dir = tmp_path / "data"
    extractor = ThreadRecordingExtractor()
    pipeline = _shared_pipeline(
        tmp_path,
        data_dir,
        FakeFetcher({ACME_URL: ACME_FREE_BODY, BETA_URL: BETA_FREE_BODY}),
        extractor,
    )
    summary = pipeline.run()
    assert summary["free_confirmed"] == 2
    assert len(extractor.calls) == 2
    # Both extractions were submitted to workers; a worker thread is never the
    # thread of the sequential confirm loop for concurrent submissions unless
    # the pool degenerated to inline execution, which main-thread identity
    # forbids here.
    assert threading.main_thread().ident not in {thread for _, thread in extractor.calls}


class FlakyExtractor:
    """Extractor that always reports an ungrounded result."""

    def __init__(self, record):
        self.record = record

    def extract(self, evidence, as_of: str):
        self.record.append(evidence.evidence_id)
        return ExtractionResult(ok=False, failure_reason="grounding mismatch")


def test_extraction_failed_candidates_are_always_retried(tmp_path: Path) -> None:
    """An extraction failure leaves no provider record (no skip snapshot), so
    the candidate is retried on the next run instead of being skipped."""
    from hunter.llm.models import ExtractionResult

    data_dir = tmp_path / "data"
    calls_first: list = []
    calls_second: list = []

    first_pipeline = _shared_pipeline(
        tmp_path,
        data_dir,
        FakeFetcher({ACME_URL: ACME_FREE_BODY, BETA_URL: BETA_FREE_BODY}),
        FlakyExtractor(calls_first),
    )
    summary1 = first_pipeline.run()
    assert summary1["free_confirmed"] == 0
    assert summary1["skip_uptodate"] == 0

    second_pipeline = _shared_pipeline(
        tmp_path,
        data_dir,
        FakeFetcher({ACME_URL: ACME_FREE_BODY, BETA_URL: BETA_FREE_BODY}),
        FlakyExtractor(calls_second),
    )
    summary2 = second_pipeline.run()
    # No provider was confirmed, so nothing is skippable: both candidates are
    # extracted again.
    assert second_pipeline.registry.list_providers() == []
    assert len(calls_second) == 2
    assert summary2["skip_uptodate"] == 0
