"""TASK-005 tests: GitHub candidate discovery, deduplicator, orchestrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter.collectors.github import GitHubCollector, GitHubHttpError
from hunter.discovery.deduplicator import deduplicate_observations
from hunter.discovery.models import CandidateObservation, SourceType, make_fingerprint
from hunter.discovery.orchestrator import DiscoveryOrchestrator, RunContext
from hunter.discovery.store import CandidateStore


def _gh_result(repo_id: int, full_name: str, html_url: str, description: str | None) -> dict:
    return {
        "id": repo_id,
        "full_name": full_name,
        "html_url": html_url,
        "description": description,
        "stargazers_count": 10,
        "fork": False,
        "language": "Python",
        "updated_at": "2026-01-01T00:00:00Z",
    }


class FakeGitHubHttp:
    """Mocked GitHub HTTP transport (boundary fake, not business logic)."""

    def __init__(self, pages_by_query=None, statuses=None):
        self.pages_by_query = pages_by_query or {}
        self.statuses = statuses or {}
        self.calls = []

    def get_json(self, url: str, params: dict) -> dict:
        self.calls.append((url, params))
        query = params.get("q")
        page = int(params.get("page", 1))
        status = self.statuses.get(url)
        if status:
            raise GitHubHttpError(f"http {status}", status)
        pages = self.pages_by_query.get(query, [])
        if page > len(pages):
            return {"total_count": 0, "items": [], "incomplete_results": False}
        return pages[page - 1]


def _collector(http=None, token=None) -> GitHubCollector:
    return GitHubCollector(
        http=http or FakeGitHubHttp(),
        token=token,
        queries=["free llm api"],
        per_page=10,
        max_total=50,
    )


def _run_context() -> RunContext:
    return RunContext(
        run_timestamp="2026-09-01T00:00:00+00:00",
        max_results_per_collector=50,
    )


# --- GitHub collector -------------------------------------------------------


def test_normalizes_repository_results() -> None:
    http = FakeGitHubHttp(
        pages_by_query={
            "free llm api": [
                {
                    "total_count": 1,
                    "items": [_gh_result(1, "acme/free-llm", "https://github.com/acme/free-llm", "Free LLM API")],
                    "incomplete_results": False,
                }
            ]
        }
    )
    collector = _collector(http=http)
    observations = collector.collect("free llm api", _run_context())
    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_type == SourceType.github
    assert obs.source_url == "https://github.com/acme/free-llm"
    assert obs.source_title == "acme/free-llm"
    assert obs.claim == "Free LLM API"
    assert obs.matched_query == "free llm api"
    assert obs.raw_metadata["github_full_name"] == "acme/free-llm"
    assert obs.raw_metadata["stargazers_count"] == 10


def test_empty_results() -> None:
    http = FakeGitHubHttp(
        pages_by_query={"free llm api": [{"total_count": 0, "items": [], "incomplete_results": False}]}
    )
    collector = _collector(http=http)
    assert collector.collect("free llm api", _run_context()) == []


def test_duplicate_suppression_via_fingerprint() -> None:
    http = FakeGitHubHttp(
        pages_by_query={
            "q": [
                {
                    "total_count": 2,
                    "items": [
                        _gh_result(1, "a/x", "https://github.com/a/x", "Free API"),
                        _gh_result(1, "a/x", "https://github.com/a/x", "Free API"),
                    ],
                    "incomplete_results": False,
                }
            ]
        }
    )
    collector = _collector(http=http)
    observations = collector.collect("q", _run_context())
    assert len(observations) == 1


def test_provenance_retention_in_metadata() -> None:
    http = FakeGitHubHttp(
        pages_by_query={
            "q": [
                {
                    "total_count": 1,
                    "items": [_gh_result(1, "o/r", "https://github.com/o/r", "desc")],
                    "incomplete_results": False,
                }
            ]
        }
    )
    collector = _collector(http=http)
    obs = collector.collect("q", _run_context())[0]
    meta = obs.raw_metadata
    assert meta["github_full_name"] == "o/r"
    assert meta["fork"] is False
    assert meta["stargazers_count"] == 10


def test_malformed_result_skipped() -> None:
    http = FakeGitHubHttp(
        pages_by_query={
            "q": [
                {
                    "total_count": 2,
                    "items": [
                        {"id": 1},  # missing url/name
                        _gh_result(2, "ok/repo", "https://github.com/ok/repo", "good"),
                    ],
                    "incomplete_results": False,
                }
            ]
        }
    )
    collector = _collector(http=http)
    observations = collector.collect("q", _run_context())
    assert len(observations) == 1
    assert observations[0].source_title == "ok/repo"


def test_pagination_bounds() -> None:
    pages = []
    for page in range(1, 8):  # 70 results over 7 pages, per_page=10
        items = [
            _gh_result(page * 100 + i, f"o/r{page}-{i}", f"https://github.com/o/r{page}-{i}", f"desc {page}-{i}")
            for i in range(10)
        ]
        pages.append({"total_count": 70, "items": items, "incomplete_results": False})
    http = FakeGitHubHttp(pages_by_query={"q": pages})
    collector = _collector(http=http)
    collector.max_total = 25
    observations = collector.collect("q", _run_context())
    # bounded by max_total
    assert len(observations) <= 25
    assert len(observations) == 25


def test_rate_limit_raises_typed_error() -> None:
    http = FakeGitHubHttp(pages_by_query={"q": []})
    http.statuses = {"https://api.github.com/search/repositories": 403}
    collector = _collector(http=http)
    with pytest.raises(GitHubHttpError):
        collector.collect("q", _run_context())


def test_transient_error_raises() -> None:
    class FlakyHttp:
        def get_json(self, url, params):
            raise GitHubHttpError("boom", 500)

    collector = _collector(http=FlakyHttp())
    with pytest.raises(GitHubHttpError):
        collector.collect("q", _run_context())


def test_deterministic_fingerprints() -> None:
    http = FakeGitHubHttp(
        pages_by_query={"q": [{"total_count": 1, "items": [_gh_result(1, "o/r", "https://github.com/o/r", "Free API")], "incomplete_results": False}]}
    )
    collector = _collector(http=http)
    a = collector.collect("q", _run_context())[0]
    b = collector.collect("q", _run_context())[0]
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint == make_fingerprint(SourceType.github, "https://github.com/o/r", "Free API")


def test_collector_requires_query() -> None:
    collector = _collector()
    with pytest.raises(ValueError):
        collector.collect("", _run_context())


# --- deduplicator -----------------------------------------------------------


def test_deduplicate_exact_fingerprints() -> None:
    obs = [_obs("1", "https://a.com", "Free"), _obs("2", "https://a.com", "Free")]
    unique, duplicates = deduplicate_observations(obs)
    assert len(unique) == 1
    assert len(duplicates) == 1
    assert duplicates[0].observation_id == "2"


def test_deduplicate_preserves_distinct() -> None:
    obs = [
        _obs("1", "https://a.com", "Free"),
        _obs("2", "https://b.com", "Free"),
        _obs("3", "https://a.com", "Different claim"),
    ]
    unique, duplicates = deduplicate_observations(obs)
    assert len(unique) == 3
    assert duplicates == []


# --- orchestrator -----------------------------------------------------------


def test_orchestrator_aggregates_and_persists_once(tmp_path: Path) -> None:
    http = FakeGitHubHttp(
        pages_by_query={
            "q": [
                {
                    "total_count": 1,
                    "items": [_gh_result(1, "o/r", "https://github.com/o/r", "desc")],
                    "incomplete_results": False,
                }
            ]
        }
    )
    collector = GitHubCollector(http=http, queries=["q"], per_page=10, max_total=50)
    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(store=store, collectors={"github": collector})
    summary = orch.run(_run_context())
    assert summary["total_observations"] == 1
    assert summary["collectors"]["github"]["observations"] == 1
    assert store.count() == 1
    # candidate is DISCOVERED only; no Provider created
    cand = store.list_candidates()[0]
    assert cand.observations[0].source_type == SourceType.github


def test_unrelated_github_repositories_get_distinct_candidates_without_github_domain_hint(
    tmp_path: Path,
) -> None:
    http = FakeGitHubHttp(
        pages_by_query={
            "q": [
                {
                    "total_count": 2,
                    "items": [
                        _gh_result(101, "one/free-api", "https://github.com/one/free-api", "one"),
                        _gh_result(202, "two/free-api", "https://github.com/two/free-api", "two"),
                    ],
                    "incomplete_results": False,
                }
            ]
        }
    )
    collector = GitHubCollector(http=http, queries=["q"], per_page=10, max_total=50)
    store = CandidateStore(tmp_path / "candidates.json")
    summary = DiscoveryOrchestrator(store=store, collectors={"github": collector}).run(_run_context())

    assert summary["store"]["candidates_created"] == 2
    candidates = store.list_candidates()
    assert len(candidates) == 2
    assert {candidate.candidate_id for candidate in candidates} == {"github-101", "github-202"}
    assert all(candidate.canonical_domain_hint is None for candidate in candidates)


def test_one_collector_failure_others_succeed(tmp_path: Path) -> None:
    class Boom:
        def collect(self, query, ctx):
            raise RuntimeError("boom")

    class Fine:
        def collect(self, query, ctx):
            return [_obs("1", "https://a.com", "Free")]

    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(store=store, collectors={"bad": Boom(), "good": Fine()})
    summary = orch.run(_run_context())
    assert summary["collectors"]["bad"]["errors"] == 1
    assert summary["collectors"]["good"]["observations"] == 1
    assert summary["total_observations"] == 1
    assert store.count() == 1


def test_disabled_collector_not_run(tmp_path: Path) -> None:
    calls = []

    class Never:
        def collect(self, query, ctx):
            calls.append(query)
            return []

    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(store=store, collectors={"never": Never()}, enabled={"github"})
    orch.run(_run_context())
    assert calls == []


# --- TASK-006: curated, Hacker News, web search -----------------------------


def _obs(obs_id: str, url: str, claim: str, source: SourceType = SourceType.github) -> CandidateObservation:
    return CandidateObservation(
        observation_id=obs_id,
        source_type=source,
        source_url=url,
        claim=claim,
    )


class FakeTransport:
    """Maps URL -> (payload or text)."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises or {}
        self.calls = []

    def get(self, url: str, params=None) -> dict:
        self.calls.append((url, params))
        if url in self.raises:
            raise self.raises[url]
        return self.responses[url]

    def get_text(self, url: str) -> str:
        self.calls.append((url, None))
        if url in self.raises:
            raise self.raises[url]
        return self.responses[url]


def _structured_curated_payload() -> dict:
    return {
        "version": "2.9.0",
        "providers": [
            {
                "slug": "curated-one",
                "name": "Curated One",
                "free_type": "renewing-quota",
                "free_tier": "Free tier",
                "docs_url": "https://curated.one",
                "verified": True,
                "last_verified": "2026-08-01",
            }
        ],
    }


def test_curated_structured_source(tmp_path: Path) -> None:
    from hunter.collectors.curated_repos import CuratedRepoCollector

    url = "https://example.com/data.json"
    transport = FakeTransport(responses={url: _structured_curated_payload()})
    collector = CuratedRepoCollector(
        transport=transport,
        sources=[{"name": "hub", "url": url, "kind": "structured"}],
    )
    observations = collector.collect("q", _run_context())
    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_type == SourceType.curated_repo
    assert obs.raw_metadata["upstream_slug"] == "curated-one"
    # upstream verified/last_verified preserved as assertions only
    assert obs.raw_metadata["asserted_verified"] is True


def test_curated_readme_source_lines(tmp_path: Path) -> None:
    from hunter.collectors.curated_repos import CuratedRepoCollector

    url = "https://example.com/README.md"
    readme = (
        "# Awesome Free AI\n"
        "## Free APIs\n"
        "- [Acme](https://acme.ai) - Free LLM API with 100k tokens/month\n"
        "- [Beta](https://beta.dev) - free API for developers\n"
    )
    transport = FakeTransport(responses={url: readme})
    collector = CuratedRepoCollector(
        transport=transport,
        sources=[{"name": "awesome", "url": url, "kind": "readme"}],
    )
    observations = collector.collect("q", _run_context())
    assert len(observations) >= 1
    for obs in observations:
        assert obs.source_type == SourceType.curated_repo
        assert obs.raw_metadata["curated_source"] == "awesome"


def test_curated_source_failure_isolated(tmp_path: Path) -> None:
    from hunter.collectors.curated_repos import CuratedRepoCollector

    url = "https://example.com/README.md"
    transport = FakeTransport(
        responses={url: "- [Good](https://good.ai) - free API\n"},
        raises={"https://example.com/broken.json": RuntimeError("boom")},
    )
    collector = CuratedRepoCollector(
        transport=transport,
        sources=[
            {"name": "good", "url": url, "kind": "readme"},
            {"name": "bad", "url": "https://example.com/broken.json", "kind": "structured"},
        ],
    )
    observations = collector.collect("q", _run_context())
    assert len(observations) >= 1  # good source still contributed


def test_hackernews_search(tmp_path: Path) -> None:
    from hunter.collectors.hackernews import HackerNewsCollector

    base = "https://hn.algolia.com/api/v1/search"
    transport = FakeTransport(
        responses={
            base: {
                "hits": [
                    {
                        "objectID": "1",
                        "title": "Show HN: free LLM API",
                        "url": "https://news.ycombinator.com/item?id=1",
                        "points": 50,
                    }
                ]
            }
        }
    )
    collector = HackerNewsCollector(transport=transport, base_url=base, max_results=20)
    observations = collector.collect("free llm api", _run_context())
    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_type == SourceType.hackernews
    assert obs.matched_query == "free llm api"
    assert obs.raw_metadata["hn_points"] == 50


def test_hackernews_empty(tmp_path: Path) -> None:
    from hunter.collectors.hackernews import HackerNewsCollector

    base = "https://hn.algolia.com/api/v1/search"
    transport = FakeTransport(responses={base: {"hits": []}})
    collector = HackerNewsCollector(transport=transport, base_url=base, max_results=20)
    assert collector.collect("q", _run_context()) == []


def test_web_search_disabled_without_credentials(tmp_path: Path) -> None:
    from hunter.collectors.web_search import WebSearchCollector

    collector = WebSearchCollector(
        transport=FakeTransport(), api_key=None, base_url="https://search.example"
    )
    assert collector.enabled() is False
    # disabled collector must not be executed by the orchestrator
    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(
        store=store, collectors={"web": collector}, enabled={"web"}
    )
    summary = orch.run(_run_context())
    assert summary["collectors"]["web"]["disabled"] is True
    assert summary["collectors"]["web"]["errors"] == 0


def test_web_search_enabled_with_credentials(tmp_path: Path) -> None:
    from hunter.collectors.web_search import WebSearchCollector

    collector = WebSearchCollector(
        transport=FakeTransport(), api_key="test-key", base_url="https://search.example"
    )
    assert collector.enabled() is True


def test_cross_source_aggregation_preserves_all_observations(tmp_path: Path) -> None:
    from hunter.collectors.curated_repos import CuratedRepoCollector
    from hunter.collectors.hackernews import HackerNewsCollector

    gh_url = "https://example.com/data.json"
    hn_base = "https://hn.algolia.com/api/v1/search"
    transport = FakeTransport(
        responses={
            gh_url: _structured_curated_payload(),
            hn_base: {
                "hits": [
                    {
                        "objectID": "2",
                        "title": "Curated One free tier!",
                        "url": "https://curated.one/free",
                    }
                ]
            },
        }
    )
    curated = CuratedRepoCollector(
        transport=transport,
        sources=[{"name": "hub", "url": gh_url, "kind": "structured"}],
    )
    hn = HackerNewsCollector(transport=transport, base_url=hn_base, max_results=20)
    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(store=store, collectors={"curated": curated, "hn": hn})
    summary = orch.run(_run_context())
    assert summary["collectors"]["curated"]["observations"] == 1
    assert summary["collectors"]["hn"]["observations"] == 1
    # both observations preserved (different sources/URLs -> different fingerprints)
    assert store.count() == 1
    cand = store.list_candidates()[0]
    assert len(cand.observations) == 2
    sources = {o.source_type for o in cand.observations}
    assert sources == {SourceType.curated_repo, SourceType.hackernews}


def test_same_url_different_claim_both_kept(tmp_path: Path) -> None:
    from hunter.collectors.hackernews import HackerNewsCollector

    base = "https://hn.algolia.com/api/v1/search"
    transport = FakeTransport(
        responses={
            base: {
                "hits": [
                    {
                        "objectID": "1",
                        "title": "free API A",
                        "url": "https://example.com/post",
                    },
                    {
                        "objectID": "2",
                        "title": "free API B different claim",
                        "url": "https://example.com/post",
                    },
                ]
            }
        }
    )
    collector = HackerNewsCollector(transport=transport, base_url=base, max_results=20)
    observations = collector.collect("q", _run_context())
    # same URL but different claims -> different fingerprints -> both retained
    assert len(observations) == 2


def test_same_claim_different_url_both_kept(tmp_path: Path) -> None:
    from hunter.collectors.hackernews import HackerNewsCollector

    base = "https://hn.algolia.com/api/v1/search"
    transport = FakeTransport(
        responses={
            base: {
                "hits": [
                    {
                        "objectID": "1",
                        "title": "same claim",
                        "url": "https://a.example/post",
                    },
                    {
                        "objectID": "2",
                        "title": "same claim",
                        "url": "https://b.example/post",
                    },
                ]
            }
        }
    )
    collector = HackerNewsCollector(transport=transport, base_url=base, max_results=20)
    observations = collector.collect("q", _run_context())
    assert len(observations) == 2


def test_bounded_calls(tmp_path: Path) -> None:
    from hunter.collectors.hackernews import HackerNewsCollector

    base = "https://hn.algolia.com/api/v1/search"
    transport = FakeTransport(responses={base: {"hits": []}})
    collector = HackerNewsCollector(transport=transport, base_url=base, max_results=5)
    collector.collect("q", _run_context())
    assert len(transport.calls) == 1


def test_deterministic_output_curated(tmp_path: Path) -> None:
    from hunter.collectors.curated_repos import CuratedRepoCollector

    url = "https://example.com/data.json"
    transport = FakeTransport(responses={url: _structured_curated_payload()})
    collector = CuratedRepoCollector(
        transport=transport,
        sources=[{"name": "hub", "url": url, "kind": "structured"}],
    )
    a = collector.collect("q", _run_context())
    b = collector.collect("q", _run_context())
    assert [o.fingerprint for o in a] == [o.fingerprint for o in b]
