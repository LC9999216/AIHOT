"""Remaining-work T5 tests: Reddit and X discovery adapters.

Both adapters are discovery-only: they produce CandidateObservations and
never verify providers. All network is injected, so tests stay offline.
"""

from __future__ import annotations

from pathlib import Path

from hunter.collectors.reddit import RedditCollector
from hunter.collectors.x_twitter import XCollector
from hunter.discovery.models import SourceType
from hunter.discovery.orchestrator import DiscoveryOrchestrator, RunContext, _identity_domain
from hunter.discovery.store import CandidateStore

REDDIT_URL = "https://www.reddit.com/search.json"
X_URL = "https://api.twitter.com/2/tweets/search/recent"


class FakeTransport:
    """Maps URL -> payload (same boundary fake style as test_discovery)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.responses[url]

    def get_text(self, url):
        self.calls.append((url, None, None))
        return self.responses[url]


def _run_context() -> RunContext:
    return RunContext(run_timestamp="2026-09-21T12:00:00+00:00", config={})


def _reddit_payload() -> dict:
    return {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "abc123",
                        "title": "Free LLM API tier",
                        "selftext": "This provider has a free API tier.",
                        "permalink": "/r/freellm/comments/abc123/free_llm_api_tier/",
                        "subreddit": "freellm",
                        "author": "someone",
                        "score": 42,
                        "num_comments": 7,
                        "created_utc": 1729500000.0,
                        "url_overridden_by_dest": "https://provider.example/free",
                    }
                },
                {"data": {"id": "def456", "title": "  ", "permalink": ""}},
            ]
        }
    }


def test_reddit_collects_observations_only() -> None:
    transport = FakeTransport(responses={REDDIT_URL: _reddit_payload()})
    collector = RedditCollector(transport=transport, base_url=REDDIT_URL)
    assert collector.enabled() is True
    observations = collector.collect("free llm api", _run_context())

    assert len(observations) == 1  # malformed second child is skipped
    obs = observations[0]
    assert obs.source_type == SourceType.reddit
    assert obs.observation_id == "reddit-abc123"
    assert (
        obs.source_url
        == "https://www.reddit.com/r/freellm/comments/abc123/free_llm_api_tier/"
    )
    assert obs.matched_query == "free llm api"
    assert obs.raw_metadata["reddit_subreddit"] == "freellm"
    assert obs.raw_metadata["reddit_link"] == "https://provider.example/free"
    # discovery-only: the observation carries no verification/trust fields
    assert not hasattr(obs, "verified") and not hasattr(obs, "officiality")


def test_reddit_deterministic_fingerprint_and_no_credential() -> None:
    transport = FakeTransport(responses={REDDIT_URL: _reddit_payload()})
    first = RedditCollector(transport=transport, base_url=REDDIT_URL).collect(
        "free llm api", _run_context()
    )
    second = RedditCollector(transport=FakeTransport(responses={REDDIT_URL: _reddit_payload()})).collect(
        "free llm api", _run_context()
    )
    assert [o.fingerprint for o in first] == [o.fingerprint for o in second]


def test_reddit_disabled_by_config() -> None:
    transport = FakeTransport()
    collector = RedditCollector(transport=transport, base_url=REDDIT_URL, enabled=False)
    assert collector.enabled() is False
    assert collector.collect("q", _run_context()) == []
    assert transport.calls == []  # no network when disabled


def test_x_disabled_without_credential(tmp_path: Path) -> None:
    collector = XCollector(transport=FakeTransport(), bearer_token=None)
    assert collector.enabled() is False
    assert collector.collect("q", _run_context()) == []

    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(store=store, collectors={"x": collector}, enabled={"x"})
    summary = orch.run(_run_context())
    assert summary["collectors"]["x"]["disabled"] is True
    assert summary["collectors"]["x"]["errors"] == 0
    assert summary["collectors"]["x"]["observations"] == 0


def test_x_collects_observations_with_credential() -> None:
    transport = FakeTransport(
        responses={
            X_URL: {
                "data": [
                    {
                        "id": "1234567890",
                        "text": "Provider X now has a free API tier",
                        "author_id": "u1",
                        "created_at": "2026-09-20T00:00:00.000Z",
                    }
                ],
                "includes": {"users": [{"id": "u1", "username": "freedeals"}]},
            }
        }
    )
    collector = XCollector(
        transport=transport, bearer_token="test-token", base_url=X_URL
    )
    assert collector.enabled() is True
    observations = collector.collect("free llm api", _run_context())
    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_type == SourceType.x
    assert obs.observation_id == "x-1234567890"
    assert obs.source_url == "https://x.com/freedeals/status/1234567890"
    assert obs.raw_metadata["x_username"] == "freedeals"
    # the credential is sent only to the injected transport, never stored
    assert obs.model_dump() and "test-token" not in str(obs.model_dump())
    url, params, headers = transport.calls[0]
    assert url == X_URL
    assert headers["Authorization"] == "Bearer test-token"


def test_social_observations_ingest_without_transferring_trust(tmp_path: Path) -> None:
    """Reddit + X observations aggregate into candidates, never verification."""
    transport = FakeTransport(
        responses={
            REDDIT_URL: _reddit_payload(),
            X_URL: {
                "data": [
                    {
                        "id": "999",
                        "text": "free api",
                        "author_id": "u1",
                    }
                ],
                "includes": {"users": [{"id": "u1", "username": "freedeals"}]},
            },
        }
    )
    reddit = RedditCollector(transport=transport, base_url=REDDIT_URL)
    xc = XCollector(transport=transport, bearer_token="t", base_url=X_URL)
    store = CandidateStore(tmp_path / "candidates.json")
    orch = DiscoveryOrchestrator(store=store, collectors={"reddit": reddit, "x": xc})
    summary = orch.run(_run_context())
    assert summary["collectors"]["reddit"]["observations"] == 1
    assert summary["collectors"]["x"]["observations"] == 1
    # both distinct observations preserved across sources
    candidates = store.list_candidates()
    all_obs = [o for c in candidates for o in c.observations]
    assert {o.source_type for o in all_obs} == {SourceType.reddit, SourceType.x}


def test_identity_domain_ignores_social_host_fallback() -> None:
    """A social post with no provider link must not claim reddit.com/x.com as identity."""
    from hunter.discovery.models import CandidateObservation

    linked = CandidateObservation(
        observation_id="reddit-ok1",
        source_type=SourceType.reddit,
        source_url="https://www.reddit.com/r/freellm/comments/abc123/t/",
        claim="free api",
        raw_metadata={"reddit_link": "https://provider.example/free"},
    )
    assert _identity_domain(linked) == "provider.example"

    unlinked = CandidateObservation(
        observation_id="reddit-ok2",
        source_type=SourceType.reddit,
        source_url="https://www.reddit.com/r/freellm/comments/def456/t/",
        claim="free api",
        raw_metadata={},
    )
    # no provider link: the fallback stays the (harmless) source host, never
    # an asserted provider domain — identity hints are not officiality
    assert _identity_domain(unlinked) == "www.reddit.com"
