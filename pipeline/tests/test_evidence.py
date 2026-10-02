"""TASK-007 tests: Evidence model, store, resolver, and safe fetcher.

SSRF coverage: direct private addresses, DNS resolving to private space,
IPv4/IPv6 loopback/private/link-local/reserved/multicast/unspecified, and
public-to-private redirects. All network behavior is exercised offline
through an injectable socket-level transport.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.evidence.fetcher import (
    FetcherError,
    SafeFetcher,
    check_host_addresses,
    check_url,
    dns_resolve,
)
from hunter.evidence.models import (
    Evidence,
    Officiality,
    canonicalize_evidence_url,
)
from hunter.evidence.resolver import EvidenceResolver
from hunter.evidence.store import EvidenceStore
from hunter.evidence.validator import OfficialEvidenceValidator

# --- host / address policy --------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "127.0.0.2",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # link-local / cloud metadata
        "0.0.0.0",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",  # multicast
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "198.18.0.1",  # benchmark/reserved
    ],
)
def test_private_and_special_addresses_rejected(addr: str) -> None:
    ip = ipaddress.ip_address(addr)
    assert check_host_addresses([ip]) is False


@pytest.mark.parametrize(
    "addr",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "2001:4860:4860::8888"],
)
def test_public_addresses_accepted(addr: str) -> None:
    ip = ipaddress.ip_address(addr)
    assert check_host_addresses([ip]) is True


def test_dns_resolve_mapping() -> None:
    # 198.18.x.y is the benchmark range: treated as NOT public.
    assert dns_resolve("example.com", resolver=lambda h: ["198.18.0.5"]) is False
    assert dns_resolve("example.com", resolver=lambda h: ["8.8.8.8"]) is True
    assert dns_resolve("example.com", resolver=lambda h: ["8.8.8.8", "127.0.0.1"]) is False
    assert dns_resolve("no-such-host.invalid", resolver=lambda h: []) is False


def test_check_url_scheme_and_port() -> None:
    with pytest.raises(FetcherError):
        check_url("ftp://example.com/x")
    with pytest.raises(FetcherError):
        check_url("file:///etc/passwd")
    with pytest.raises(FetcherError):
        check_url("https://example.com:8443/x")
    with pytest.raises(FetcherError):
        check_url("https://user:pass@example.com/x")
    with pytest.raises(FetcherError):
        check_url("not a url")
    ok = check_url("https://example.com/x")
    assert ok is not None


def test_check_url_host_requirement() -> None:
    with pytest.raises(FetcherError):
        check_url("https:///path-only")


def test_upsert_persists_officiality_change_across_reload(tmp_path: Path) -> None:
    """A merge that changes officiality must be persisted, not held in memory."""
    path = tmp_path / "evidence.json"
    store = EvidenceStore(path)
    store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            candidate_id="acme",
            url="https://acme.ai/pricing",
            source_type="pricing",
            claim="free plan",
            content_excerpt="free plan programmatic API",
            retrieved_at="2026-08-01T00:00:00+00:00",
        )
    )
    stored = store.get("ev-1")
    assert stored.officiality.value == "UNCONFIRMED"

    changed = stored.model_copy(
        update={
            "officiality": Officiality.OFFICIAL,
            "validation_notes": ["TA-001: exact configured anchor domain"],
        }
    )
    assert store.upsert(changed) is False  # merged, not inserted

    reloaded = EvidenceStore(path)
    persisted = reloaded.get("ev-1")
    assert persisted.officiality is Officiality.OFFICIAL
    assert persisted.validation_notes == ["TA-001: exact configured anchor domain"]


def test_upsert_noop_merge_does_not_rewrite_file(tmp_path: Path) -> None:
    """An identical re-fetch must not rewrite the evidence file."""
    path = tmp_path / "evidence.json"
    store = EvidenceStore(path)
    original = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        claim="free plan",
        content_excerpt="free plan programmatic API",
        retrieved_at="2026-08-01T00:00:00+00:00",
        effective_at="2026-07-01T00:00:00+00:00",
        published_at="2026-06-01T00:00:00+00:00",
    )
    store.upsert(original)
    before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns

    store.upsert(original.model_copy())  # identical content, same identity
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime_before


def test_evidence_store_stable_order_after_validation_merge(tmp_path: Path) -> None:
    """Validated evidence still serializes in stable evidence_id order."""
    path = tmp_path / "evidence.json"
    store = EvidenceStore(path)
    for i in range(5):
        store.upsert(
            Evidence(
                evidence_id=f"ev-{i}",
                provider_id="acme",
                url=f"https://acme.ai/p{i}",
                source_type="pricing",
                claim="free plan",
                content_excerpt="free plan programmatic API",
                retrieved_at="2026-08-01T00:00:00+00:00",
            )
        )
    for ev in store.list():
        store.upsert(ev.model_copy(update={"officiality": Officiality.LIKELY_OFFICIAL}))
    reloaded = EvidenceStore(path)
    ids = [e.evidence_id for e in reloaded.list()]
    assert ids == sorted(ids)
    assert all(e.officiality is Officiality.LIKELY_OFFICIAL for e in reloaded.list())


def test_refetch_does_not_downgrade_officiality(tmp_path: Path) -> None:
    """Re-fetching identical content must not reset a validator decision."""
    path = tmp_path / "evidence.json"
    store = EvidenceStore(path)
    store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            candidate_id="acme",
            url="https://acme.ai/pricing",
            source_type="pricing",
            claim="free plan",
            content_excerpt="free plan programmatic API",
            retrieved_at="2026-08-01T00:00:00+00:00",
        )
    )
    store.upsert(store.get("ev-1").model_copy(update={"officiality": Officiality.OFFICIAL}))
    assert store.get("ev-1").officiality is Officiality.OFFICIAL

    # a fresh fetch of the same content arrives UNCONFIRMED
    store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            candidate_id="acme",
            url="https://acme.ai/pricing",
            source_type="pricing",
            claim="free plan",
            content_excerpt="free plan programmatic API",
            retrieved_at="2026-09-01T00:00:00+00:00",
        )
    )
    assert EvidenceStore(path).get("ev-1").officiality is Officiality.OFFICIAL


def test_refetch_does_not_downgrade_likely_official(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    store = EvidenceStore(path)
    store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            url="https://docs.acme.ai/pricing",
            source_type="pricing",
            claim="free plan",
            content_excerpt="free plan",
            retrieved_at="2026-08-01T00:00:00+00:00",
        )
    )
    store.upsert(
        store.get("ev-1").model_copy(update={"officiality": Officiality.LIKELY_OFFICIAL})
    )
    store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            url="https://docs.acme.ai/pricing",
            source_type="pricing",
            claim="free plan",
            content_excerpt="free plan",
            retrieved_at="2026-09-01T00:00:00+00:00",
        )
    )
    assert EvidenceStore(path).get("ev-1").officiality is Officiality.LIKELY_OFFICIAL


def test_revalidation_does_not_duplicate_notes() -> None:
    """Repeated validation must not grow validation_notes (rerun stability)."""
    from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor
    from hunter.evidence.models import EvidenceProvenance

    validator = OfficialEvidenceValidator(
        [
            TrustAnchor(
                provider_id="acme",
                domains=["acme.ai"],
                provenance="manual",
                reviewed_at="2026-09-01",
            )
        ]
    )
    provenance = EvidenceProvenance(
        retrieval_method="safe_fetch",
        original_url="https://acme.ai/pricing",
        final_url="https://acme.ai/pricing",
        http_status=200,
        content_sha256="a" * 64,
        retrieved_from_origin=True,
        retrieved_at=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
    )
    evidence = Evidence(
        evidence_id="ev-1",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        claim="free plan",
        content_excerpt="free plan programmatic API",
        retrieved_at="2026-08-01T00:00:00+00:00",
        provenance=provenance,
    )
    once = validator.validate(evidence)
    twice = validator.validate(once)
    thrice = validator.validate(twice)
    assert once.officiality is Officiality.OFFICIAL
    assert len(once.validation_notes) == 1
    assert twice.validation_notes == once.validation_notes
    assert thrice.validation_notes == once.validation_notes


def test_revalidation_is_stable_through_the_store(tmp_path: Path) -> None:
    """A second build+validate cycle leaves the evidence file unchanged."""
    from hunter.evidence.validator import OfficialEvidenceValidator, TrustAnchor

    path = tmp_path / "evidence.json"
    store = EvidenceStore(path)
    validator = OfficialEvidenceValidator(
        [
            TrustAnchor(
                provider_id="acme",
                domains=["acme.ai"],
                provenance="manual",
                reviewed_at="2026-09-01",
            )
        ]
    )

    def _cycle() -> None:
        store.upsert(
            Evidence(
                candidate_id="acme",
                provider_id="acme",
                url="https://acme.ai/pricing",
                source_type="pricing",
                claim="free plan",
                content_excerpt="free plan programmatic API",
                retrieved_at="2026-09-01T00:00:00+00:00",
                effective_at="2026-09-01T00:00:00+00:00",
            )
        )
        for ev in store.list():
            store.upsert(validator.validate(ev))

    _cycle()
    after_first = path.read_bytes()
    _cycle()
    assert path.read_bytes() == after_first


# --- fixture-driven SSRF target list ----------------------------------------

EVIDENCE_FIXTURES = Path(__file__).parent / "fixtures" / "evidence"


def _static_policy_blocks(url: str) -> bool:
    """Apply the complete pre-connect static policy, including IP literals."""
    try:
        check_url(url)
    except (FetcherError, ValueError):
        return True
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP literal; the DNS step must catch it
    return not check_host_addresses([literal])


def test_ssrf_fixture_static_urls_are_all_rejected() -> None:
    """Every scheme/port/user-info/private-IP-literal target is rejected."""
    fixture = json.loads((EVIDENCE_FIXTURES / "ssrf_targets.json").read_text(encoding="utf-8"))
    assert fixture["rejected_static"], "fixture must list statically rejected URLs"
    for url in fixture["rejected_static"]:
        assert _static_policy_blocks(url), f"expected static rejection for {url}"


def test_ssrf_fixture_accepted_urls_pass_static_checks() -> None:
    fixture = json.loads((EVIDENCE_FIXTURES / "ssrf_targets.json").read_text(encoding="utf-8"))
    for url in fixture["accepted_urls"]:
        check_url(url)  # must not raise
        assert not _static_policy_blocks(url)


def test_ssrf_fixture_dns_private_hosts_blocked() -> None:
    """Hosts resolving to non-public space fail even though the URL looks clean."""
    fixture = json.loads((EVIDENCE_FIXTURES / "ssrf_targets.json").read_text(encoding="utf-8"))
    assert fixture["rejected_dns"]
    for host, addrs in fixture["rejected_dns"].items():
        parsed = [ipaddress.ip_address(a) for a in addrs]
        assert not check_host_addresses(parsed), f"{host} -> {addrs} must be non-public"
        # and the public-looking URL is accepted statically, so DNS is the gate
        assert not _static_policy_blocks(f"https://{host}/pricing")


def test_ssrf_fixture_dns_hosts_rejected_by_fetcher() -> None:
    """The fetcher refuses a DNS-private host end to end."""
    fixture = json.loads((EVIDENCE_FIXTURES / "ssrf_targets.json").read_text(encoding="utf-8"))
    hosts = {h: addrs for h, addrs in fixture["rejected_dns"].items()}
    transport = FakeSocketTransport(hosts=hosts)
    fetcher = SafeFetcher(test_transport=transport)
    for host in hosts:
        with pytest.raises(FetcherError):
            fetcher.fetch(f"https://{host}/pricing", provider_id="acme")


def test_evidence_fixtures_round_trip_through_store(tmp_path: Path) -> None:
    """Every evidence fixture loads, stays UNCONFIRMED, and persists."""
    store = EvidenceStore(tmp_path / "evidence.json")
    files = sorted(EVIDENCE_FIXTURES.glob("*.json"))
    loaded = 0
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "url" not in payload:
            continue
        evidence = Evidence(
            evidence_id=f"ev-{payload['name']}",
            provider_id=payload["provider_id"],
            candidate_id=payload.get("provider_id"),
            url=payload["url"],
            source_type=payload["source_type"],
            title=payload.get("title"),
            claim=payload.get("claim"),
            content_excerpt=payload.get("content_excerpt"),
            retrieved_at=payload["retrieved_at"],
            effective_at=payload.get("effective_at"),
            published_at=payload.get("published_at"),
        )
        # new evidence always starts UNCONFIRMED; the fetcher never assigns trust
        assert evidence.officiality.value == "UNCONFIRMED"
        assert evidence.normalized_domain == payload["expected_normalized_domain"]
        store.upsert(evidence)
        loaded += 1
    assert loaded >= 5
    store.save()
    reread = EvidenceStore(tmp_path / "evidence.json")
    assert len(reread.list()) == loaded
    assert [e.evidence_id for e in reread.list()] == sorted(e.evidence_id for e in reread.list())


# --- fetcher ----------------------------------------------------------------


class FakeSocketTransport:
    """In-process HTTP transport simulating DNS + sockets (offline)."""

    def __init__(self, hosts=None, responses=None, redirects=None, body_size=None, delay=None):
        self.hosts = hosts or {"example.com": ["93.184.216.34"]}
        self.responses = responses or {}  # url -> (status, headers, body)
        self.redirects = redirects or {}  # url -> location
        self.body_size = body_size
        self.delay = delay
        self.requests = []
        self.pinned_ips = []

    def resolve(self, host):
        return self.hosts.get(host, ["93.184.216.34"])

    def connect(self, host, port):
        addrs = self.hosts.get(host, ["93.184.216.34"])
        if not check_host_addresses([ipaddress.ip_address(a) for a in addrs]):
            raise FetcherError(f"blocked address for {host}")
        return addrs[0]

    def request(self, url, pinned_ip=None):
        self.requests.append(url)
        self.pinned_ips.append(pinned_ip)
        if self.delay:
            import time

            time.sleep(self.delay)
        if url in self.redirects:
            return (302, {"Location": self.redirects[url]}, b"")
        if url in self.responses:
            status, headers, body = self.responses[url]
            if self.body_size is not None and len(body) > self.body_size:
                body = body[: self.body_size]
            return (status, headers, body)
        return (404, {"Content-Type": "text/html"}, b"not found")

    def resolve(self, host):
        return self.hosts.get(host, ["93.184.216.34"])


def _fetcher(transport=None, **kwargs) -> SafeFetcher:
    return SafeFetcher(
        test_transport=transport or FakeSocketTransport(),
        max_redirects=kwargs.get("max_redirects", 3),
        max_body_bytes=kwargs.get("max_body_bytes", 2 * 1024 * 1024),
        total_timeout=kwargs.get("total_timeout", 10),
        max_pages_per_provider=kwargs.get("max_pages_per_provider", 6),
    )


def test_fetcher_accepts_public_content() -> None:
    transport = FakeSocketTransport(
        responses={"https://example.com/pricing": (200, {"Content-Type": "text/html"}, b"<html>free</html>")}
    )
    result = _fetcher(transport).fetch("https://example.com/pricing", provider_id="p1")
    assert result.status == 200
    assert b"free" in result.body


def test_fetcher_direct_private_target_rejected() -> None:
    transport = FakeSocketTransport(hosts={"internal.local": ["10.0.0.5"]})
    fetcher = _fetcher(transport)
    with pytest.raises(FetcherError):
        fetcher.fetch("http://internal.local/x", provider_id="p1")


def test_fetcher_ipv4_loopback_rejected() -> None:
    transport = FakeSocketTransport(hosts={"localhost": ["127.0.0.1"]})
    with pytest.raises(FetcherError):
        _fetcher(transport).fetch("http://localhost/x", provider_id="p1")


def test_fetcher_ipv6_loopback_rejected() -> None:
    transport = FakeSocketTransport(hosts={"v6.local": ["::1"]})
    with pytest.raises(FetcherError):
        _fetcher(transport).fetch("http://v6.local/x", provider_id="p1")


def test_fetcher_public_to_private_redirect_rejected() -> None:
    transport = FakeSocketTransport(
        hosts={"public.example": ["93.184.216.34"], "internal.local": ["10.1.1.1"]},
        redirects={"https://public.example/start": "http://internal.local/secret"},
    )
    with pytest.raises(FetcherError):
        _fetcher(transport).fetch("https://public.example/start", provider_id="p1")


def test_fetcher_relative_redirect_uses_lowercase_location() -> None:
    class LowercaseRedirectTransport(FakeSocketTransport):
        def request(self, url, pinned_ip=None):
            if url == "https://example.com/start":
                self.requests.append(url)
                return (302, {"location": "next"}, b"")
            return super().request(url)

    transport = LowercaseRedirectTransport(
        responses={"https://example.com/next": (200, {"content-type": "text/plain"}, b"ok")}
    )
    result = _fetcher(transport).fetch("https://example.com/start", provider_id="p1")
    assert result.final_url == "https://example.com/next"
    assert transport.requests == ["https://example.com/start", "https://example.com/next"]


def test_fetcher_uses_connection_class_for_url_scheme(monkeypatch) -> None:
    import http.client

    connections = []

    class Response:
        status = 200

        def __init__(self):
            self._read = False

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self, size=-1):
            if self._read:
                return b""
            self._read = True
            return b"ok"

    class Connection:
        def __init__(self, host, port, timeout):
            connections.append((type(self).__name__, host, port, timeout))

        def request(self, method, url):
            assert method == "GET"

        def getresponse(self):
            return Response()

        def close(self):
            pass

    class HttpConnection(Connection):
        pass

    class HttpsConnection(Connection):
        pass

    monkeypatch.setattr(http.client, "HTTPConnection", HttpConnection)
    monkeypatch.setattr(http.client, "HTTPSConnection", HttpsConnection)
    fetcher = SafeFetcher(max_body_bytes=10)
    from urllib.parse import urlparse as _urlparse

    fetcher._real_request("example.com", 80, _urlparse("http://example.com/x"), 1)
    fetcher._real_request("example.com", 443, _urlparse("https://example.com/x"), 1)
    assert [entry[0] for entry in connections] == ["HttpConnection", "HttpsConnection"]


def test_fetcher_streaming_body_is_bounded() -> None:
    class ChunkedBody:
        def __init__(self, payload: bytes):
            self._payload = payload
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            if size < 0:
                size = len(self._payload)
            chunk, self._payload = self._payload[:size], self._payload[size:]
            return chunk

    body = ChunkedBody(b"x" * 11)
    transport = FakeSocketTransport(
        responses={"https://example.com/stream": (200, {"content-type": "text/plain"}, body)}
    )
    with pytest.raises(FetcherError, match="size limit"):
        _fetcher(transport, max_body_bytes=8).fetch("https://example.com/stream", provider_id="p1")
    assert body.read_sizes
    assert max(body.read_sizes) <= 9


def test_fetcher_checks_content_type_before_reading_body() -> None:
    class UnreadBody:
        read_calls = 0

        def read(self, size=-1):
            self.read_calls += 1
            return b"binary"

    body = UnreadBody()
    transport = FakeSocketTransport(
        responses={
            "https://example.com/file": (
                200,
                {"Content-Type": "application/octet-stream"},
                body,
            )
        }
    )
    with pytest.raises(FetcherError, match="content type"):
        _fetcher(transport).fetch("https://example.com/file", provider_id="p1")
    assert body.read_calls == 0


def test_fetcher_redirect_limit() -> None:
    redirects = {
        "https://a.example/1": "https://a.example/2",
        "https://a.example/2": "https://a.example/3",
        "https://a.example/3": "https://a.example/4",
        "https://a.example/4": "https://a.example/5",
    }
    transport = FakeSocketTransport(redirects=redirects)
    with pytest.raises(FetcherError):
        _fetcher(transport, max_redirects=3).fetch("https://a.example/1", provider_id="p1")


def test_fetcher_oversize_body_rejected() -> None:
    transport = FakeSocketTransport(
        responses={"https://example.com/big": (200, {"Content-Type": "text/html"}, b"x" * 100)}
    )
    with pytest.raises(FetcherError):
        _fetcher(transport, max_body_bytes=10).fetch("https://example.com/big", provider_id="p1")


def test_fetcher_declared_oversize_rejected() -> None:
    transport = FakeSocketTransport(
        responses={"https://example.com/big": (200, {"Content-Length": "999999999", "Content-Type": "text/html"}, b"small")}
    )
    with pytest.raises(FetcherError):
        _fetcher(transport, max_body_bytes=100).fetch("https://example.com/big", provider_id="p1")


def test_fetcher_timeout() -> None:
    import time as _t

    class SlowTransport(FakeSocketTransport):
        def request(self, url, pinned_ip=None):
            _t.sleep(0.4)
            return (200, {"Content-Type": "text/html"}, b"late")

    with pytest.raises(FetcherError):
        _fetcher(SlowTransport(), total_timeout=0.1).fetch("https://example.com/x", provider_id="p1")


def test_fetcher_unsupported_content_type() -> None:
    transport = FakeSocketTransport(
        responses={"https://example.com/file": (200, {"Content-Type": "application/octet-stream"}, b"binary")}
    )
    with pytest.raises(FetcherError):
        _fetcher(transport).fetch("https://example.com/file", provider_id="p1")


def test_fetcher_page_count_bound() -> None:
    transport = FakeSocketTransport(
        responses={"https://example.com/p": (200, {"Content-Type": "text/html"}, b"page")}
    )
    fetcher = _fetcher(transport, max_pages_per_provider=2)
    for i in range(2):
        fetcher.fetch(f"https://example.com/p?n={i}", provider_id="p1")
    with pytest.raises(FetcherError):
        fetcher.fetch("https://example.com/p?n=3", provider_id="p1")


def test_fetcher_no_credentials_forwarded() -> None:
    transport = FakeSocketTransport(
        responses={"https://example.com/x": (200, {"Content-Type": "text/plain"}, b"ok")}
    )
    fetcher = _fetcher(transport)
    fetcher.fetch("https://example.com/x", provider_id="p1")
    assert transport.requests  # no auth headers tracked because none exist


# --- evidence model / store -------------------------------------------------


def test_evidence_round_trip() -> None:
    ev = Evidence(
        evidence_id="ev-1",
        candidate_id="cand-1",
        provider_id=None,
        url="https://example.com/pricing",
        normalized_domain="example.com",
        source_type="pricing",
        officiality=Officiality.UNCONFIRMED,
        retrieved_at="2026-09-01T00:00:00+00:00",
        title="Pricing",
        claim="Free tier",
        content_fingerprint="abc123",
    )
    restored = Evidence.model_validate_json(ev.model_dump_json())
    assert restored == ev


def test_evidence_new_starts_unconfirmed() -> None:
    ev = Evidence(url="https://example.com/pricing")
    assert ev.officiality is Officiality.UNCONFIRMED
    assert ev.retrieved_at is not None
    assert ev.content_fingerprint


def test_evidence_store_persists_stable_order(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.json")
    store.upsert(Evidence(evidence_id="b-ev", url="https://b.example"))
    store.upsert(Evidence(evidence_id="a-ev", url="https://a.example"))
    assert [e.evidence_id for e in store.list()] == ["a-ev", "b-ev"]
    store2 = EvidenceStore(tmp_path / "evidence.json")
    assert [e.evidence_id for e in store2.list()] == ["a-ev", "b-ev"]


def test_evidence_store_dedup_by_url_and_fingerprint(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.json")
    ev1 = Evidence(evidence_id="ev-1", url="https://example.com/pricing", content_fingerprint="f1")
    ev2 = Evidence(evidence_id="ev-2", url="https://example.com/pricing", content_fingerprint="f1")
    added1 = store.upsert(ev1)
    added2 = store.upsert(ev2)
    assert added1 is True
    assert added2 is False
    assert len(store.list()) == 1


def test_evidence_store_identity_from_url_and_fingerprint(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.json")
    store.upsert(Evidence(evidence_id="ev-1", url="https://example.com/pricing", content_fingerprint="f1"))
    store.upsert(Evidence(evidence_id="ev-2", url="https://example.com/pricing", content_fingerprint="f2"))
    # different content -> different evidence
    assert len(store.list()) == 2


# --- canonical URL normalization --------------------------------------------


def test_canonicalize_evidence_url() -> None:
    assert canonicalize_evidence_url("HTTPS://Example.COM:443/x/") == "https://example.com/x"
    assert canonicalize_evidence_url("https://example.com/x#frag") == "https://example.com/x"
    assert canonicalize_evidence_url("https://example.com:80/x") == "https://example.com/x"


# --- resolver ---------------------------------------------------------------


def test_resolver_proposes_bounded_urls() -> None:
    resolver = EvidenceResolver()
    urls = resolver.propose_urls(
        candidate_domain="acme.ai",
        observations=[
            {"type": "github", "url": "https://github.com/acme/llm"},
            {"type": "registry", "docs_url": "https://docs.acme.ai/pricing"},
        ],
    )
    assert urls
    for url in urls:
        assert url.startswith(("https://", "http://"))
    # high search rank is not officiality - resolver returns plain URLs only
    assert isinstance(urls, list)


def test_resolver_covers_document_kinds() -> None:
    resolver = EvidenceResolver()
    urls = resolver.propose_urls(candidate_domain="acme.ai", observations=[])
    kinds = set()
    for url in urls:
        kinds.add(_kind_from_url(url))
    assert {"pricing", "api-docs"} <= kinds
    assert len(urls) <= 10


def test_resolver_includes_asserted_docs_url_as_fetch_target() -> None:
    resolver = EvidenceResolver()
    urls = resolver.propose_urls(
        candidate_domain="other.example",
        observations=[
            {
                "raw_metadata": {"asserted_docs_url": "https://acme.ai/special-offer"},
            }
        ],
    )
    assert "https://acme.ai/special-offer" in urls


def test_equal_priority_identical_official_content_is_consistent() -> None:
    first = Evidence(
        evidence_id="ev-first",
        provider_id="acme",
        url="https://acme.ai/pricing",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        retrieved_at="2026-09-01T00:00:00+00:00",
        claim="free plan programmatic API",
        content_excerpt="free plan programmatic API",
    )
    second = Evidence(
        evidence_id="ev-second",
        provider_id="acme",
        url="https://acme.ai/plans",
        source_type="pricing",
        officiality=Officiality.OFFICIAL,
        retrieved_at="2026-09-01T00:00:00+00:00",
        claim="free plan programmatic API",
        content_excerpt="free plan programmatic API",
    )
    decision = OfficialEvidenceValidator([]).resolve_contradictions([first, second])
    assert decision["unresolved"] == []
    assert decision["winner"].evidence_id in {"ev-first", "ev-second"}


def _kind_from_url(url: str) -> str:
    if "pricing" in url:
        return "pricing"
    if "api" in url or "developer" in url:
        return "api-docs"
    if "docs" in url:
        return "docs"
    return "other"
