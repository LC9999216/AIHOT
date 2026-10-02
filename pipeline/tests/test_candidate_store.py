"""TASK-002 tests: Candidate Store (discovery/store.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter.collectors.registry_seed import RegistrySeedImporter
from hunter.discovery.models import CandidateObservation, SourceType, make_fingerprint
from hunter.discovery.store import CandidateStore, ImportCounts

FIXTURE = Path(__file__).parent / "fixtures" / "upstream" / "free-llm-api-hub-v2.9.0.json"


def _obs(obs_id: str, url: str, claim: str, source: SourceType = SourceType.third_party_registry) -> CandidateObservation:
    return CandidateObservation(
        observation_id=obs_id,
        source_type=source,
        source_url=url,
        claim=claim,
        discovered_at="2026-08-01T00:00:00+00:00",
    )


def _seed(obs: CandidateObservation, candidate_id=None, provider_name="Acme AI", domain=None, aliases=()):
    from hunter.discovery.store import ObservationWithCandidate

    return ObservationWithCandidate(
        observation=obs,
        candidate_id=candidate_id,
        provider_name=provider_name,
        canonical_domain_hint=domain,
        aliases=list(aliases),
    )


def _empty_store(tmp_path: Path) -> CandidateStore:
    return CandidateStore(tmp_path / "candidates.json")


# --- basic ingestion --------------------------------------------------------


def test_import_seed_fixture_creates_candidates(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    importer = RegistrySeedImporter()
    result = importer.load(FIXTURE)
    counts = store.ingest(result.entries)
    assert counts.candidates_created == 69
    assert counts.observations_added == 69
    assert counts.unchanged == 0
    assert store.count() == 69


def test_reimport_is_noop_and_byte_identical(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    importer = RegistrySeedImporter()
    result = importer.load(FIXTURE)
    store.ingest(result.entries)
    bytes1 = store.serialize()
    counts = store.ingest(result.entries)
    assert counts.candidates_created == 0
    assert counts.observations_added == 0
    assert counts.unchanged == 69
    assert store.serialize() == bytes1
    # file on disk also unchanged
    on_disk = store.path.read_bytes()
    assert on_disk == bytes1


def test_duplicate_observation_dedup_exact_fingerprint(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    obs1 = _obs("o1", "https://example.com/pricing", "Free tier")
    counts = store.ingest([_seed(obs1, candidate_id="acme")])
    assert counts.observations_added == 1
    counts = store.ingest([_seed(obs1, candidate_id="acme")])
    assert counts.observations_added == 0
    assert counts.unchanged == 1
    cand = store.get("acme")
    assert len(cand.observations) == 1


def test_new_source_for_same_candidate_retained(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    obs1 = _obs("o1", "https://example.com/pricing", "Free tier")
    obs2 = _obs("o2", "https://example.com/api-docs", "Free tier", source=SourceType.github)
    store.ingest([_seed(obs1, candidate_id="acme")])
    store.ingest([_seed(obs2, candidate_id="acme")])
    cand = store.get("acme")
    assert len(cand.observations) == 2


# --- merge rules ------------------------------------------------------------


def test_merge_by_stable_id(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id="acme")])
    # same stable ID, new observation
    counts = store.ingest([_seed(_obs("o2", "https://b.com", "also free"), candidate_id="acme")])
    assert counts.candidates_merged == 1
    assert store.get("acme") is not None


def test_merge_by_exact_domain_hint(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id="acme", domain="docs.acme.ai")])
    counts = store.ingest(
        [_seed(_obs("o2", "https://b.com", "also free"), candidate_id=None, domain="docs.acme.ai")]
    )
    assert counts.candidates_merged == 1
    assert counts.candidates_created == 0
    cand = store.get("acme")
    assert len(cand.observations) == 2


def test_merge_by_explicit_alias(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id="acme")])
    counts = store.ingest(
        [_seed(_obs("o2", "https://b.com", "free 2"), candidate_id=None, aliases=["acme"])]
    )
    assert counts.candidates_merged == 1


def test_merge_by_unambiguous_exact_name(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id=None, provider_name="Acme AI")])
    counts = store.ingest(
        [_seed(_obs("o2", "https://b.com", "free 2"), candidate_id=None, provider_name="Acme AI")]
    )
    assert counts.candidates_merged == 1
    assert store.count() == 1


def test_ambiguous_same_name_no_auto_merge(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    # two distinct candidates with identical name but different domains
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id="c1", provider_name="Acme AI", domain="a.com")])
    store.ingest([_seed(_obs("o2", "https://b.com", "free"), candidate_id="c2", provider_name="Acme AI", domain="b.com")])
    # a third with the same name but no ID/domain must NOT fuzzy-merge
    counts = store.ingest(
        [_seed(_obs("o3", "https://c.com", "free 3"), candidate_id=None, provider_name="Acme AI")]
    )
    assert counts.candidates_created == 1
    assert store.count() == 3


def test_no_fuzzy_name_merge_on_similar_names(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id=None, provider_name="Acme AI")])
    counts = store.ingest(
        [_seed(_obs("o2", "https://b.com", "free"), candidate_id=None, provider_name="Acme AI Inc.")]
    )
    assert counts.candidates_created == 1
    assert store.count() == 2


# --- ordering and determinism ----------------------------------------------


def test_stable_ordering_by_candidate_id(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    importer = RegistrySeedImporter()
    store.ingest(importer.load(FIXTURE).entries)
    ids = [c.candidate_id for c in store.list_candidates()]
    assert ids == sorted(ids)


def test_stable_observation_order_by_fingerprint(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest(
        [
            _seed(_obs("o2", "https://z.com", "zeta"), candidate_id="acme"),
            _seed(_obs("o1", "https://a.com", "alpha"), candidate_id="acme"),
        ]
    )
    cand = store.get("acme")
    fps = [o.fingerprint for o in cand.observations]
    assert fps == sorted(fps)


def test_unchanged_timestamps_on_noop(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    obs = _obs("o1", "https://a.com", "free", )
    store.ingest([_seed(obs, candidate_id="acme")])
    first = store.get("acme").first_discovered_at
    store.ingest([_seed(obs, candidate_id="acme")])
    again = store.get("acme").first_discovered_at
    assert first == again


def test_save_is_atomic(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    store.ingest([_seed(_obs("o1", "https://a.com", "free"), candidate_id="acme")])
    assert store.path.exists()
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert isinstance(payload["items"], list)


def test_counts_structure() -> None:
    counts = ImportCounts()
    assert counts.candidates_created == 0
    assert counts.candidates_merged == 0
    assert counts.observations_added == 0
    assert counts.unchanged == 0
    assert counts.rejected == 0
    assert counts.errors == 0
