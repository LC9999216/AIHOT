"""TASK-010 acceptance: end-to-end pipeline, run twice, byte-identical."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hunter.pipeline import Pipeline
from tests.fakes import FakeFetcher, FakeGroundedExtractor

FIXTURE = Path("tests/fixtures/upstream/free-llm-api-hub-v2.9.0.json")
AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(
        data_dir=tmp_path / "data",
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )


def test_pipeline_runs_end_to_end(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    summary = pipeline.run()
    assert summary["detail"]["evidence_built"] >= 1
    assert summary["detail"]["validation"]["OFFICIAL"] >= 1
    assert summary["detail"]["providers"] >= 1
    assert summary["free_confirmed"] >= 1
    confirmed = [p for p in pipeline.registry.list_providers() if p.status.value == "FREE_CONFIRMED"]
    assert len(confirmed) >= 1
    assert pipeline.registry.history_path.is_file()
    # spec summary fields are all present
    for key in (
        "candidates_processed",
        "providers_created",
        "providers_updated",
        "free_confirmed",
        "uncertain",
        "not_free",
        "expired",
        "rejected",
        "unchanged",
        "errors",
    ):
        assert key in summary


def test_pipeline_twice_is_byte_identical(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    p1 = Pipeline(
        data_dir=data_dir,
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    s1 = p1.run()
    providers1 = (data_dir / "providers.json").read_bytes()
    history1 = (data_dir / "history.jsonl").read_text(encoding="utf-8")
    history_lines1 = [l for l in history1.splitlines() if l.strip()]

    p2 = Pipeline(
        data_dir=data_dir,
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    s2 = p2.run()
    providers2 = (data_dir / "providers.json").read_bytes()
    history2 = (data_dir / "history.jsonl").read_text(encoding="utf-8")
    history_lines2 = [l for l in history2.splitlines() if l.strip()]

    # Registry byte-identical after the second run
    assert providers1 == providers2
    # No new no-op history events
    assert len(history_lines2) == len(history_lines1)
    assert history2 == history1
    # Second run creates nothing and updates nothing
    assert s2["providers_created"] == 0
    assert s2["providers_updated"] == 0
    assert s2["free_confirmed"] == s1["free_confirmed"]
    assert s2["errors"] == 0


def test_pipeline_deterministic_hashes(tmp_path: Path) -> None:
    """The same fixture+config+as_of yields identical provider content each run."""
    import hashlib

    data_dir = tmp_path / "data"
    p1 = Pipeline(
        data_dir=data_dir,
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    p1.run()
    h1 = hashlib.sha256((data_dir / "providers.json").read_bytes()).hexdigest()

    data_dir2 = tmp_path / "data2"
    p2 = Pipeline(
        data_dir=data_dir2,
        seed_path=FIXTURE,
        as_of=AS_OF,
        fetcher=FakeFetcher(),
        extractor=FakeGroundedExtractor(),
    )
    p2.run()
    h2 = hashlib.sha256((data_dir2 / "providers.json").read_bytes()).hexdigest()
    assert h1 == h2


def test_pipeline_confirms_only_anchored_providers(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    pipeline.run()
    confirmed = [p for p in pipeline.registry.list_providers() if p.status.value == "FREE_CONFIRMED"]
    assert confirmed
    for provider in confirmed:
        assert provider.evidence_ids
        assert provider.verification_confidence is not None and provider.verification_confidence >= 80
        assert provider.score_metadata is not None
        assert provider.score_metadata.config_digest
        assert provider.free_offer.offer_status.value == "active"
