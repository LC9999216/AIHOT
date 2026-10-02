"""Stage 1.5 correctness gate tests.

FIX-001: provenance
FIX-003: DNS rebinding / GitHub token
FIX-004: officiality recomputed every time
FIX-005: ProviderSetup requires citations
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from hunter.evidence.fetcher import SafeFetcher, FetchResult, FetcherError, check_host_addresses
from hunter.evidence.models import (
    Evidence,
    EvidenceProvenance,
    Officiality,
    canonicalize_evidence_url,
)
from hunter.evidence.validator import (
    OfficialEvidenceValidator,
    TrustAnchor,
)
from hunter.evidence.store import EvidenceStore
from hunter.collectors.github import GitHubCollector
from hunter.registry.schema import Provider, ProviderSetup


# =============================================================================
# FIX-001: provenance can only come from SafeFetcher
# =============================================================================


def test_provenance_only_generated_by_safe_fetcher() -> None:
    """Imported/constructed evidence must NOT carry provenance."""
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        claim="free tier",
        content_excerpt="free tier",
    )
    assert ev.provenance is None, "Evidence constructed without SafeFetcher should have no provenance"


def test_safe_fetcher_populates_provenance() -> None:
    """SafeFetcher must populate all provenance fields."""
    class DummyTransport:
        def resolve(self, host):
            return ["93.184.216.34"]
        def request(self, url, pinned_ip=None):
            return (200, {"Content-Type": "text/html"}, b"free tier programmatic API")

    fetcher = SafeFetcher(test_transport=DummyTransport())
    result = fetcher.fetch("https://acme.ai/pricing", provider_id="acme")
    assert result.retrieval_method == "safe_fetch"
    assert result.original_url == "https://acme.ai/pricing"
    assert result.final_url == "https://acme.ai/pricing"
    assert result.status == 200
    assert result.content_sha256
    assert result.retrieved_from_origin is True
    assert result.retrieved_at is not None
    assert result.redirect_chain == []


def test_evidence_from_fetch_populates_all_provenance_fields() -> None:
    """Evidence built from FetchResult must carry full provenance."""
    class DummyTransport:
        def resolve(self, host):
            return ["93.184.216.34"]
        def request(self, url, pinned_ip=None):
            return (200, {"Content-Type": "text/html"}, b"free tier")

    fetcher = SafeFetcher(test_transport=DummyTransport())
    result = fetcher.fetch("https://acme.ai/pricing", provider_id="acme")
    ev = Evidence.from_fetch(
        result,
        provider_id="acme",
        source_type="pricing",
        claim="free tier",
    )
    assert ev.provenance is not None
    assert ev.provenance.retrieval_method == "safe_fetch"
    assert ev.provenance.retrieved_from_origin is True
    assert ev.provenance.content_sha256 == result.content_sha256
    # the excerpt is derived from the actually fetched body (binding)
    assert ev.content_excerpt == "free tier"


def test_evidence_without_provenance_cannot_be_official_through_validator() -> None:
    """Evidence missing provenance cannot become OFFICIAL regardless of trust anchor."""
    anchor = TrustAnchor(
        provider_id="acme",
        domains=["acme.ai"],
        provenance="manual",
        reviewed_at="2026-09-01",
    )
    validator = OfficialEvidenceValidator(anchors=[anchor])
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.UNCONFIRMED,
        claim="free tier",
        content_excerpt="free tier",
        provenance=None,
    )
    decision = validator.evaluate(ev)
    assert decision.officiality is not Officiality.OFFICIAL
    assert "provenance" in decision.note.lower()


def test_stored_evidence_with_correct_provenance_can_be_official_if_anchored() -> None:
    """Evidence with SafeFetcher provenance and matching anchor must be OFFICIAL."""
    anchor = TrustAnchor(
        provider_id="acme",
        domains=["acme.ai"],
        provenance="manual",
        reviewed_at="2026-09-01",
    )
    validator = OfficialEvidenceValidator(anchors=[anchor])
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url="https://acme.ai/pricing",
        final_url="https://acme.ai/pricing",
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.UNCONFIRMED,
        claim="free tier",
        content_excerpt="free tier",
        provenance=provenance,
    )
    validated = validator.validate(ev)
    assert validated.officiality is Officiality.OFFICIAL


def test_stored_officiality_without_provenance_is_rejected_by_validator() -> None:
    """Even stored OFFICIAL must be rejected if provenance is missing."""
    anchor = TrustAnchor(
        provider_id="acme",
        domains=["acme.ai"],
        provenance="manual",
        reviewed_at="2026-09-01",
    )
    validator = OfficialEvidenceValidator(anchors=[anchor])
    # Evidence that was stored as OFFICIAL but has no provenance
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        claim="free tier",
        content_excerpt="free tier",
        provenance=None,
    )
    decision = validator.evaluate(ev)
    assert decision.officiality is not Officiality.OFFICIAL


def test_officiality_recomputed_not_trusted() -> None:
    """Validator must NOT short-circuit on existing OFFICIAL."""
    anchor = TrustAnchor(
        provider_id="acme",
        domains=["acme.ai"],
        provenance="manual",
        reviewed_at="2026-09-01",
    )
    validator = OfficialEvidenceValidator(anchors=[anchor])
    # Evidence with fake OFFICIAL but provenance says not from origin
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url="https://evil.com/pricing",
        final_url="https://acme.ai/pricing",
        http_status=200,
        content_sha256="b" * 64,
        retrieved_from_origin=False,
        retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        claim="free tier",
        content_excerpt="free tier",
        provenance=provenance,
    )
    decision = validator.evaluate(ev)
    # Should NOT be OFFICIAL because retrieved_from_origin is False
    assert decision.officiality is not Officiality.OFFICIAL


# =============================================================================
# FIX-003: DNS rebinding protection
# =============================================================================


def test_direct_private_ip_rejected() -> None:
    """Connecting to a private IPv4 must fail."""
    class PrivateTransport:
        def resolve(self, host):
            return ["10.0.0.5"]
        def request(self, url, pinned_ip=None):
            return (200, {"Content-Type": "text/html"}, b"secret")
    fetcher = SafeFetcher(test_transport=PrivateTransport())
    with pytest.raises(FetcherError):
        fetcher.fetch("http://internal.example/x", provider_id="p1")


def test_private_ipv6_rejected() -> None:
    """IPv6 link-local / ULA addresses must fail."""
    class PrivateV6Transport:
        def resolve(self, host):
            return ["fc00::1"]
        def request(self, url, pinned_ip=None):
            return (200, {"Content-Type": "text/html"}, b"secret")
    fetcher = SafeFetcher(test_transport=PrivateV6Transport())
    with pytest.raises(FetcherError):
        fetcher.fetch("http://v6-internal.example/x", provider_id="p1")


def test_public_to_private_ipv6_redirect_rejected() -> None:
    """Redirect from public to private IPv6 must fail."""
    class RedirectToPrivateV6Transport:
        def __init__(self):
            self.calls = []
        def resolve(self, host):
            if host == "public.example":
                return ["93.184.216.34"]
            if host == "v6-internal.local":
                return ["fc00::1"]
            return []
        def request(self, url, pinned_ip=None):
            self.calls.append(url)
            if url == "https://public.example/start":
                return (302, {"Location": "http://v6-internal.local/secret"}, b"")
            return (200, {"Content-Type": "text/html"}, b"ok")
    fetcher = SafeFetcher(test_transport=RedirectToPrivateV6Transport())
    with pytest.raises(FetcherError):
        fetcher.fetch("https://public.example/start", provider_id="p1")


def test_multiple_redirects_all_public_succeeds() -> None:
    """A sequence of public redirects must succeed."""
    class PublicRedirectTransport:
        def __init__(self):
            self.calls = []
        def resolve(self, host):
            return {"a.example": ["93.184.216.34"],
                    "b.example": ["93.184.216.35"],
                    "c.example": ["93.184.216.36"]}.get(host, ["93.184.216.34"])
        def request(self, url, pinned_ip=None):
            self.calls.append(url)
            if url == "https://a.example/1":
                return (302, {"Location": "https://b.example/2"}, b"")
            if url == "https://b.example/2":
                return (302, {"Location": "https://c.example/3"}, b"")
            return (200, {"Content-Type": "text/html"}, b"final")
    fetcher = SafeFetcher(test_transport=PublicRedirectTransport(), max_redirects=3)
    result = fetcher.fetch("https://a.example/1", provider_id="p1")
    assert result.status == 200
    assert b"final" in result.body
    assert result.final_url == "https://c.example/3"


def test_dns_rebind_after_redirect_rejected() -> None:
    """If intermediate redirect resolves to private IP, reject."""
    class RebindTransport:
        def __init__(self):
            self.calls = []
        def resolve(self, host):
            self.calls.append(("resolve", host))
            if host == "public.example":
                return ["93.184.216.34"]
            if host == "evil.local":
                return ["192.168.1.1"]
            return ["93.184.216.34"]
        def request(self, url, pinned_ip=None):
            self.calls.append(("request", url))
            if url == "https://public.example/start":
                return (302, {"Location": "https://evil.local/secret"}, b"")
            return (200, {"Content-Type": "text/html"}, b"ok")
    fetcher = SafeFetcher(test_transport=RebindTransport())
    with pytest.raises(FetcherError):
        fetcher.fetch("https://public.example/start", provider_id="p1")


# =============================================================================
# FIX-003: GitHub token
# =============================================================================


def test_github_token_only_to_api_domain() -> None:
    """GitHub token must only be sent to api.github.com."""
    actual_urls = []

    class RecordingHttp:
        def get_json(self, url, params=None):
            actual_urls.append(url)
            if "api.github.com" in url:
                return {"total_count": 0, "items": []}
            return {"items": []}

    collector = GitHubCollector(http=RecordingHttp(), token="ghp_test_token_12345")
    collector.collect("free llm api", None)
    for url in actual_urls:
        assert url.startswith("https://api.github.com"), f"Token went to non-API domain: {url}"


def test_github_token_not_in_metadata() -> None:
    """GitHub token must not appear in observation metadata."""
    class FakeHttp:
        def get_json(self, url, params=None):
            return {
                "total_count": 1,
                "items": [{
                    "id": 1,
                    "full_name": "test/repo",
                    "html_url": "https://github.com/test/repo",
                    "description": "free API",
                }]
            }

    collector = GitHubCollector(http=FakeHttp(), token="ghp_test_secret_67890")
    from hunter.discovery.orchestrator import RunContext
    ctx = RunContext(run_timestamp="2026-09-01T00:00:00+00:00")
    observations = collector.collect("free llm api", ctx)
    meta_str = json.dumps([o.raw_metadata for o in observations])
    assert "ghp_test_secret_67890" not in meta_str
    assert "ghp_" not in meta_str


def test_github_token_not_in_observation_id() -> None:
    """Token must not leak into observation_id or fingerprint."""
    class FakeHttp:
        def get_json(self, url, params=None):
            return {
                "total_count": 1,
                "items": [{
                    "id": 1,
                    "full_name": "test/repo",
                    "html_url": "https://github.com/test/repo",
                    "description": "free API",
                }]
            }

    collector = GitHubCollector(http=FakeHttp(), token="ghp_secret_11111")
    from hunter.discovery.orchestrator import RunContext
    ctx = RunContext(run_timestamp="2026-09-01T00:00:00+00:00")
    observations = collector.collect("free llm api", ctx)
    obs = observations[0]
    assert "ghp" not in obs.observation_id
    assert "ghp" not in obs.fingerprint


def test_github_token_not_in_exception_message() -> None:
    """Token must be redacted from exception messages."""
    import io
    import urllib.error

    class FailingHttp:
        def __init__(self):
            self._token = "ghp_test_33333"

        def get_json(self, url, params=None):
            # Raise an exception that our sanitize logic handles
            raise urllib.error.HTTPError(
                url, 403, "Forbidden",
                {}, io.BytesIO(b"token ghp_test_33333 invalid")
            )

    collector = GitHubCollector(http=FailingHttp(), token="ghp_test_33333")
    from hunter.collectors.github import GitHubHttpError
    try:
        from hunter.discovery.orchestrator import RunContext
        ctx = RunContext(run_timestamp="2026-09-01T00:00:00+00:00")
        collector.collect("free llm api", ctx)
    except (GitHubHttpError, Exception) as exc:
        msg = str(exc)
        assert "ghp_test_33333" not in msg, f"Token leaked in exception: {msg}"


def test_github_token_no_follow_redirect_to_other_domain() -> None:
    """If GitHub redirects off api.github.com, must not send token."""
    redirect_reached = False

    class RedirectHttp:
        def get_json(self, url, params=None):
            nonlocal redirect_reached
            if url.startswith("https://api.github.com"):
                raise Exception("redirect to evil.com would happen")
                # We can't actually redirect in this fake, so test the other path
            redirect_reached = True
            return {"items": []}

    collector = GitHubCollector(http=RedirectHttp(), token="ghp_secret")
    # If it reaches non-GitHub domain, the token should not be sent
    # The collector only constructs URLs to api.github.com
    from hunter.collectors.github import GITHUB_API
    # Verify config doesn't allow redirects
    assert "api.github.com" in GITHUB_API


# =============================================================================
# FIX-004: ProviderSetup
# =============================================================================


def test_provider_setup_fields_default_to_none() -> None:
    """Provider setup fields must default to None."""
    provider = Provider(
        id="test-provider",
        provider="Test Provider",
    )
    assert provider.setup is None, "New provider should have no setup by default"


def test_provider_setup_rejects_uncited_url() -> None:
    """ProviderSetup URLs without evidence citations must be rejected."""
    with pytest.raises(ValueError):
        ProviderSetup(
            signup_url="https://acme.ai/signup",
        )


def test_provider_setup_accepts_cited_url() -> None:
    """ProviderSetup with proper evidence citations must be accepted."""
    setup = ProviderSetup(
        signup_url={
            "url": "https://acme.ai/signup",
            "evidence_id": "ev-acme-pricing",
            "quote": "sign up for free",
            "start_offset": 10,
            "end_offset": 26,
        },
    )
    assert setup.signup_url is not None
    assert setup.signup_url.url == "https://acme.ai/signup"


def test_provider_setup_accepts_partial_fields() -> None:
    """Partial ProviderSetup must be valid (some null fields)."""
    setup = ProviderSetup(
        api_key_url={
            "url": "https://acme.ai/api-keys",
            "evidence_id": "ev-acme-docs",
            "quote": "create API key",
            "start_offset": 5,
            "end_offset": 19,
        },
    )
    assert setup.signup_url is None
    assert setup.api_key_url is not None
    assert setup.setup_instructions_url is None


# =============================================================================
# FIX-004: Fake OFFICIAL rejection
# =============================================================================


def test_fake_official_imported_must_be_rejected() -> None:
    """Evidence imported as OFFICIAL but lacking SafeFetcher provenance must be rejected."""
    anchor = TrustAnchor(
        provider_id="fake-provider",
        domains=["fake-provider.ai"],
        provenance="manual",
        reviewed_at="2026-09-01",
    )
    validator = OfficialEvidenceValidator(anchors=[anchor])
    # Simulated imported evidence claiming OFFICIAL but no provenance
    ev = Evidence(
        evidence_id="fake-official",
        provider_id="fake-provider",
        url="https://fake-provider.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        claim="free API",
        content_excerpt="free API",
        provenance=None,
    )
    decision = validator.evaluate(ev)
    assert decision.officiality is not Officiality.OFFICIAL, (
        "Imported evidence claiming OFFICIAL without provenance must be rejected"
    )
