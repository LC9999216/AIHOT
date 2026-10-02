"""Stage 1.5 security-fix regressions (review round 2).

Covers:
- GitHub token: HTTPS-only api.github.com:443, no downgrade, no Authorization
  forwarded to github.com or any other host on redirect (REAL RedirectHandler).
- EvidenceProvenance: constructed/imported self-declared provenance can never
  earn OFFICIAL; retrieval_method / 2xx status / final_url / redirect chain /
  content hash bindings are all enforced fail-closed.
- SafeFetcher: injectable transport is test-only and receives the pinned IP.
- GroundedUrl: non-empty url/evidence_id/quote, legal scheme, ordered
  non-negative offsets, quote verified against evidence content.
- collect-evidence: uses safe-fetch provenance + final_url and rejects non-2xx.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from hunter.collectors.github import HttpTransport, RedirectHandler, auth_headers_for
from hunter.evidence.fetcher import FetchResult, SafeFetcher, FetcherError
from hunter.evidence.models import (
    Evidence,
    EvidenceProvenance,
    Officiality,
)
from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor
from hunter.registry.schema import GroundedUrl


# =============================================================================
# GitHub token — real RedirectHandler regressions
# =============================================================================


def _request_with_token(url: str = "https://api.github.com/search/repositories") -> urllib.request.Request:
    """A real Request carrying the Authorization header like HttpTransport does."""
    transport = HttpTransport(token="ghp_supersecret123")
    # replicate the production header construction path
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    request.add_header("Authorization", "Bearer ghp_supersecret123")
    return request


def test_redirect_https_to_http_downgrade_refused() -> None:
    """A redirect from https://api.github.com to http://api.github.com must be refused."""
    handler = RedirectHandler()
    req = _request_with_token()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "http://api.github.com/x")


def test_redirect_to_github_com_does_not_carry_token() -> None:
    """api.github.com -> github.com redirect must not forward Authorization."""
    handler = RedirectHandler()
    req = _request_with_token()
    newreq = handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://github.com/owner/repo")
    assert newreq is not None
    auth = newreq.get_header("Authorization") or newreq.get_header("authorization")
    assert not auth, f"Authorization header leaked to github.com: {auth!r}"


def test_redirect_to_other_host_refused() -> None:
    """Redirects to any non-official domain are refused entirely."""
    handler = RedirectHandler()
    req = _request_with_token()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://evil.example/steal")


def test_redirect_api_github_https_keeps_token() -> None:
    """Redirect staying on https://api.github.com:443 may keep the token."""
    handler = RedirectHandler()
    req = _request_with_token()
    newreq = handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://api.github.com/page2")
    assert newreq is not None
    assert newreq.get_header("Authorization") == "Bearer ghp_supersecret123"


def test_redirect_api_github_nonstandard_port_refused() -> None:
    """Token must only ever go to api.github.com:443."""
    handler = RedirectHandler()
    req = _request_with_token()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://api.github.com:8443/x")


def test_token_not_sent_to_plain_http_api_domain() -> None:
    """HttpTransport must not attach Authorization to an http:// URL."""
    headers = auth_headers_for("http://api.github.com/search/repositories", "ghp_supersecret123")
    assert "Authorization" not in headers


def test_token_only_for_https_api_github_com() -> None:
    headers = auth_headers_for("https://api.github.com/search/repositories", "ghp_supersecret123")
    assert headers.get("Authorization") == "Bearer ghp_supersecret123"
    none_for_github = auth_headers_for("https://github.com/owner/repo", "ghp_supersecret123")
    assert "Authorization" not in none_for_github
    none_for_evil = auth_headers_for("https://evil.example/x", "ghp_supersecret123")
    assert "Authorization" not in none_for_evil
    none_without_token = auth_headers_for("https://api.github.com/x", None)
    assert "Authorization" not in none_without_token


# =============================================================================
# Provenance forgery — no OFFICIAL without verified SafeFetcher provenance
# =============================================================================


ANCHOR = TrustAnchor(provider_id="acme", domains=["acme.ai"], provenance="manual", reviewed_at="2026-09-01")
VALIDATOR = OfficialEvidenceValidator(anchors=[ANCHOR])


def _forged_provenance(**overrides) -> EvidenceProvenance:
    base = dict(
        retrieval_method="safe_fetch",
        original_url="https://acme.ai/pricing",
        final_url="https://acme.ai/pricing",
        redirect_chain=[],
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return EvidenceProvenance(**base)


def _evidence_with(provenance: EvidenceProvenance, url: str = "https://acme.ai/pricing") -> Evidence:
    return Evidence(
        evidence_id="ev-forged",
        provider_id="acme",
        url=url,
        source_type="pricing",
        claim="free tier",
        content_excerpt="free tier",
        provenance=provenance,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieval_method": "manual_import"},
        {"retrieval_method": "curl"},
        {"http_status": 404},
        {"http_status": 301},
        {"http_status": 500},
        {"retrieved_from_origin": False},
        {"content_sha256": "not-a-hash"},
        {"content_sha256": ""},
    ],
)
def test_forged_provenance_never_official(overrides) -> None:
    decision = VALIDATOR.evaluate(_evidence_with(_forged_provenance(**overrides)))
    assert decision.officiality is not Officiality.OFFICIAL, f"forgeable via {overrides}"


def test_forged_final_url_mismatch_never_official() -> None:
    """provenance.final_url must bind to the evidence URL host."""
    prov = _forged_provenance(final_url="https://evil.example/pricing")
    decision = VALIDATOR.evaluate(_evidence_with(prov))
    assert decision.officiality is not Officiality.OFFICIAL


def test_forged_redirect_chain_inconsistent_never_official() -> None:
    """redirect_chain must start at original_url when present."""
    prov = _forged_provenance(
        original_url="https://acme.ai/pricing",
        redirect_chain=["https://other.example/start", "https://acme.ai/hop"],
        final_url="https://acme.ai/pricing",
    )
    decision = VALIDATOR.evaluate(_evidence_with(prov))
    assert decision.officiality is not Officiality.OFFICIAL


def test_consistent_redirect_chain_stays_official() -> None:
    prov = _forged_provenance(
        redirect_chain=["https://acme.ai/pricing"],
        final_url="https://acme.ai/final",
    )
    decision = VALIDATOR.evaluate(_evidence_with(prov, url="https://acme.ai/final"))
    # chain is consistent here (original == chain[0]) -> may be official
    assert decision.officiality is Officiality.OFFICIAL


def test_valid_from_fetch_provenance_is_official() -> None:
    """The legitimate Evidence.from_fetch path still yields OFFICIAL."""
    body = b"free tier programmatic API"
    result = FetchResult(
        status=200,
        headers={"content-type": "text/html"},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        redirect_chain=[],
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_from_origin=True,
        retrieval_method="safe_fetch",
        retrieved_at=datetime.now(timezone.utc),
    )
    ev = Evidence.from_fetch(result, provider_id="acme", source_type="pricing", claim="free tier")
    decision = VALIDATOR.evaluate(ev)
    assert decision.officiality is Officiality.OFFICIAL


def test_from_fetch_binds_excerpt_to_actual_body() -> None:
    """content_excerpt is derived from the fetched body, not caller text."""
    body = b"real fetched page content saying free tier"
    result = FetchResult(
        status=200,
        headers={},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )
    ev = Evidence.from_fetch(result, provider_id="acme", source_type="pricing")
    assert ev.content_excerpt
    assert ev.content_excerpt in body.decode("utf-8")


def test_from_fetch_extracts_visible_html_before_truncating() -> None:
    """Long CSS/script shells must not displace the actual offer text."""
    body = (
        "<html><head><style>" + (".font{color:red}" * 500) + "</style>"
        "<script>ignore_me = 'not visible';</script></head>"
        "<body><h1>Free tier</h1><p>Programmatic API access is included.</p></body></html>"
    ).encode("utf-8")
    result = FetchResult(
        status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )

    ev = Evidence.from_fetch(
        result,
        provider_id="acme",
        source_type="pricing",
        excerpt_length=120,
    )

    assert ev.content_excerpt == "Free tier Programmatic API access is included."
    assert ".font" not in ev.content_excerpt
    assert "ignore_me" not in ev.content_excerpt


def test_from_fetch_prioritizes_official_html_description_metadata() -> None:
    """A bounded excerpt keeps the page's offer summary ahead of repeated nav text."""
    body = (
        '<html><head><meta name="description" '
        'content="Try our programmatic API for free."></head>'
        '<body><nav>' + ("Products Developers Resources " * 100) + '</nav>'
        '<main>Pricing details</main></body></html>'
    ).encode("utf-8")
    result = FetchResult(
        status=200,
        headers={"content-type": "text/html"},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )

    ev = Evidence.from_fetch(
        result,
        provider_id="acme",
        source_type="pricing",
        excerpt_length=120,
    )

    assert ev.content_excerpt.startswith("Try our programmatic API for free.")


def test_from_fetch_prioritizes_main_content_over_navigation() -> None:
    """Pricing facts in main content survive a bounded excerpt after large site chrome."""
    body = (
        '<html><body><header><nav>'
        + ("Products Developers Resources " * 100)
        + '</nav></header><main><h1>Free</h1>'
        '<p>New accounts receive $50 in one-time credits for API access.</p>'
        '</main><footer>' + ("Company Legal " * 100) + '</footer></body></html>'
    ).encode("utf-8")
    result = FetchResult(
        status=200,
        headers={"content-type": "text/html"},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )

    ev = Evidence.from_fetch(
        result,
        provider_id="acme",
        source_type="pricing",
        excerpt_length=120,
    )

    assert ev.content_excerpt == "Free New accounts receive $50 in one-time credits for API access."


def test_from_fetch_keeps_relevant_free_credit_section_late_in_long_page() -> None:
    """A late official free-credit section must survive the bounded excerpt."""
    body = (
        "<html><body><main><p>" + ("Account management details. " * 300) + "</p>"
        "<h2>Free tier and credits</h2>"
        "<p>New accounts receive $50 in one-time free credits for API access.</p>"
        "</main></body></html>"
    ).encode("utf-8")
    result = FetchResult(
        status=200,
        headers={"content-type": "text/html"},
        body=body,
        final_url="https://acme.ai/docs/account",
        original_url="https://acme.ai/docs/account",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )

    ev = Evidence.from_fetch(result, provider_id="acme", excerpt_length=180)

    assert len(ev.content_excerpt or "") <= 180
    assert "Free tier and credits" in (ev.content_excerpt or "")
    assert "$50 in one-time free credits for API access" in (ev.content_excerpt or "")


def test_from_fetch_rejects_forged_excerpt() -> None:
    """A caller-supplied excerpt that is not part of the body fails closed."""
    body = b"real fetched page content"
    result = FetchResult(
        status=200,
        headers={},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError):
        Evidence.from_fetch(
            result, provider_id="acme", source_type="pricing",
            claim="totally fabricated claim never fetched",
        )


def test_from_fetch_rejects_non_2xx() -> None:
    body = b"error page"
    result = FetchResult(
        status=503,
        headers={},
        body=body,
        final_url="https://acme.ai/pricing",
        original_url="https://acme.ai/pricing",
        content_sha256=__import__("hashlib").sha256(body).hexdigest(),
        retrieved_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError):
        Evidence.from_fetch(result, provider_id="acme", source_type="pricing")


# =============================================================================
# SafeFetcher — test-only transport receives the pinned IP
# =============================================================================


class RecordingTransport:
    """Test-only fake: records the pinned IP it was told to connect to."""

    def __init__(self, hosts=None, responses=None):
        self.hosts = hosts or {"acme.ai": ["93.184.216.34"]}
        self.responses = responses or {}
        self.seen_pinned_ips = []

    def resolve(self, host):
        return self.hosts.get(host, ["93.184.216.34"])

    def request(self, url, pinned_ip=None):
        self.seen_pinned_ips.append(pinned_ip)
        status, headers, body = self.responses.get(
            url, (200, {"Content-Type": "text/html"}, b"free tier")
        )
        return status, headers, body


def test_injected_transport_receives_pinned_ip() -> None:
    transport = RecordingTransport(
        responses={"https://acme.ai/pricing": (200, {"Content-Type": "text/html"}, b"free tier")}
    )
    fetcher = SafeFetcher(test_transport=transport)
    fetcher.fetch("https://acme.ai/pricing", provider_id="acme")
    assert transport.seen_pinned_ips == ["93.184.216.34"]


def test_production_constructor_has_no_transport_parameter() -> None:
    """The production signature must not expose a transport capability."""
    import inspect

    params = inspect.signature(SafeFetcher.__init__).parameters
    assert "transport" not in params, "plain `transport` param must not exist"
    assert "test_transport" in params


# =============================================================================
# GroundedUrl validation
# =============================================================================


def test_grounded_url_rejects_empty_fields() -> None:
    with pytest.raises(ValidationError):
        GroundedUrl(url="", evidence_id="ev-1", quote="free tier", start_offset=0, end_offset=9)
    with pytest.raises(ValidationError):
        GroundedUrl(url="https://acme.ai/signup", evidence_id="", quote="free tier", start_offset=0, end_offset=9)
    with pytest.raises(ValidationError):
        GroundedUrl(url="https://acme.ai/signup", evidence_id="ev-1", quote="", start_offset=0, end_offset=9)


def test_grounded_url_rejects_bad_scheme() -> None:
    with pytest.raises(ValidationError):
        GroundedUrl(url="javascript:alert(1)", evidence_id="ev-1", quote="free", start_offset=0, end_offset=4)
    with pytest.raises(ValidationError):
        GroundedUrl(url="ftp://acme.ai/signup", evidence_id="ev-1", quote="free", start_offset=0, end_offset=4)


def test_grounded_url_rejects_disordered_offsets() -> None:
    with pytest.raises(ValidationError):
        GroundedUrl(url="https://acme.ai/signup", evidence_id="ev-1", quote="free", start_offset=-1, end_offset=4)
    with pytest.raises(ValidationError):
        GroundedUrl(url="https://acme.ai/signup", evidence_id="ev-1", quote="free", start_offset=5, end_offset=4)
    with pytest.raises(ValidationError):
        GroundedUrl(url="https://acme.ai/signup", evidence_id="ev-1", quote="free", start_offset=4, end_offset=4)


def test_grounded_url_quote_verified_against_evidence() -> None:
    ev = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/signup",
        source_type="docs",
        claim="signup",
        content_excerpt="Visit our signup page at /signup to create an account.",
    )
    # "Visit our " is 10 chars, then "signup page at /signup" (22 chars).
    quote = "signup page at /signup"
    good = GroundedUrl(
        url="https://acme.ai/signup",
        evidence_id="ev-1",
        quote=quote,
        start_offset=10,
        end_offset=32,
    )
    assert good.verify_against(ev) is True
    bad = GroundedUrl(
        url="https://acme.ai/signup",
        evidence_id="ev-1",
        quote=quote,
        start_offset=0,
        end_offset=22,
    )
    assert bad.verify_against(ev) is False
    wrong_evidence = GroundedUrl(
        url="https://acme.ai/signup",
        evidence_id="ev-other",
        quote=quote,
        start_offset=10,
        end_offset=32,
    )
    assert wrong_evidence.verify_against(ev) is False


# =============================================================================
# collect-evidence — provenance-bound storage, final_url, non-2xx rejection
# =============================================================================


class FakeCandidate:
    """Minimal candidate stand-in for the collect worker."""

    def __init__(self, candidate_id: str, domain: str):
        self.candidate_id = candidate_id
        self.canonical_domain_hint = domain
        self.observations = []


class FakeCandidateStore:
    def __init__(self, candidates):
        self._candidates = candidates

    def list_candidates(self):
        return list(self._candidates)


class FakeFetchResult:
    def __init__(self, status, final_url, body, redirect_chain=None):
        import hashlib

        self.status = status
        self.headers = {"content-type": "text/html"}
        self.body = body
        self.final_url = final_url
        self.original_url = redirect_chain[0] if redirect_chain else final_url
        self.redirect_chain = list(redirect_chain or [])
        self.content_sha256 = hashlib.sha256(body).hexdigest()
        self.retrieved_from_origin = True
        self.retrieval_method = "safe_fetch"
        self.retrieved_at = datetime(2026, 9, 12, tzinfo=timezone.utc)


class FakeFetcherForCollect:
    def __init__(self, results):
        self._results = results  # url -> FakeFetchResult | Exception

    def fetch(self, url, provider_id):
        outcome = self._results.get(url)
        if outcome is None:
            return FakeFetchResult(404, url, b"missing")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_collect_evidence_stores_provenance_and_final_url(tmp_path) -> None:
    from hunter.evidence.collect import collect_evidence
    from hunter.evidence.store import EvidenceStore

    redirect = ["https://acme.ai/pricing"]
    result = FakeFetchResult(200, "https://acme.ai/pricing-final", b"free tier content", redirect_chain=redirect)
    store = EvidenceStore(tmp_path / "evidence.json")
    report = collect_evidence(
        FakeCandidateStore([FakeCandidate("acme", "acme.ai")]),
        store,
        FakeFetcherForCollect({"https://acme.ai/pricing": result}),
        limit=5,
    )
    assert report.stored == 1
    items = store.list()
    assert len(items) == 1
    ev = items[0]
    assert ev.provenance is not None
    assert ev.url == "https://acme.ai/pricing-final"  # final_url after redirect
    assert ev.provenance.final_url == "https://acme.ai/pricing-final"
    assert ev.provenance.http_status == 200
    assert ev.content_excerpt in "free tier content"


def test_collect_evidence_rejects_non_2xx(tmp_path) -> None:
    from hunter.evidence.collect import collect_evidence
    from hunter.evidence.resolver import EvidenceResolver
    from hunter.evidence.store import EvidenceStore

    # every URL the resolver will propose yields 503
    proposed = EvidenceResolver().propose_urls(candidate_domain="acme.ai", observations=[])
    results = {url: FakeFetchResult(503, url, b"unavailable") for url in proposed}
    store = EvidenceStore(tmp_path / "evidence.json")
    report = collect_evidence(
        FakeCandidateStore([FakeCandidate("acme", "acme.ai")]),
        store,
        FakeFetcherForCollect(results),
        limit=5,
    )
    assert report.stored == 0
    assert report.rejected_non_2xx == 5  # bounded by limit
    assert report.rejected_urls
    assert store.list() == []


def test_collect_evidence_counts_fetch_errors_without_storing(tmp_path) -> None:
    from hunter.evidence.collect import collect_evidence
    from hunter.evidence.store import EvidenceStore

    store = EvidenceStore(tmp_path / "evidence.json")
    report = collect_evidence(
        FakeCandidateStore([FakeCandidate("acme", "acme.ai")]),
        store,
        FakeFetcherForCollect({"https://acme.ai/pricing": RuntimeError("boom")}),
        limit=5,
    )
    assert report.errors == 1
    assert report.stored == 0
    assert store.list() == []
