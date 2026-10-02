"""Safe evidence fetcher with SSRF controls (AGENTS.md section 9).

Every requested URL and redirect hop must pass all checks before connecting:
scheme HTTP/HTTPS, port 80/443, no user-info, hostname resolves, every
IPv4/IPv6 result is public (not loopback/private/link-local/reserved/
multicast/unspecified), redirects are revalidated from scratch, max 3
redirects, max 2 MiB body, 10-second total timeout, bounded content types,
max 6 pages per Provider per run. No scripts, no cookies, no auth headers.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2 * 1024 * 1024
TOTAL_TIMEOUT = 10
MAX_PAGES_PER_PROVIDER = 6
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
ACCEPTED_CONTENT_TYPES = ("text/html", "text/plain", "application/json", "application/xhtml+xml")


class FetcherError(Exception):
    """Raised when a fetch is blocked by a security check or fails."""


@dataclass
class FetchResult:
    status: int
    headers: Dict[str, str]
    body: bytes
    final_url: str
    original_url: str = ""
    redirect_chain: List[str] = field(default_factory=list)
    content_sha256: str = ""
    retrieved_from_origin: bool = True
    retrieval_method: str = "safe_fetch"
    retrieved_at: Optional[datetime] = None
    connected_ip: Optional[str] = None


def check_host_addresses(addresses: Sequence[ipaddress._BaseAddress]) -> bool:
    """Return True only if every address is public (not special)."""
    if not addresses:
        return False
    for addr in addresses:
        if not isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return False
        if addr.is_private:
            return False
        if addr.is_loopback:
            return False
        if addr.is_link_local:
            return False
        if addr.is_multicast:
            return False
        if addr.is_reserved:
            return False
        if addr.is_unspecified:
            return False
    return True


def dns_resolve(host: str, resolver=None) -> bool:
    """Resolve a hostname and confirm all results are public."""
    if resolver is None:
        try:
            infos = socket.getaddrinfo(host, None)
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (socket.gaierror, ValueError, OSError):
            return False
    else:
        raw = resolver(host)
        try:
            addresses = [ipaddress.ip_address(item) for item in raw]
        except ValueError:
            return False
    return check_host_addresses(addresses)


def check_url(url: str) -> urlparse.ParseResult:
    """Validate scheme, port, user-info, and parseable URL. Returns parsed."""
    if not isinstance(url, str) or not url.strip():
        raise FetcherError("empty URL")
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise FetcherError(f"unparseable URL {url!r}") from exc
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetcherError(f"disallowed scheme {parsed.scheme!r}")
    if parsed.hostname is None:
        raise FetcherError(f"URL has no host: {url!r}")
    if parsed.port is not None and parsed.port not in ALLOWED_PORTS:
        raise FetcherError(f"disallowed port {parsed.port}")
    if parsed.username or parsed.password:
        raise FetcherError("URL must not contain user-info credentials")
    return parsed


class SafeFetcher:
    """HTTP(S) fetcher enforcing all SSRF and size/timeout limits.

    Production instances must be constructed WITHOUT ``test_transport``: the
    real path resolves a verified public IP, pins the connection to it (DNS
    rebinding protection), keeps the hostname only for Host/SNI, and
    re-resolves on every redirect hop.

    ``test_transport`` is an explicit TEST-ONLY seam (review round 2): it
    replaces the socket layer in offline tests and MUST honor the pinned IP
    it is handed — the resolved, validated address is passed to every
    ``request(url, pinned_ip=...)`` call so a fake that "connects" uses the
    same address the production path would use.
    """

    def __init__(
        self,
        max_redirects: int = MAX_REDIRECTS,
        max_body_bytes: int = MAX_BODY_BYTES,
        total_timeout: float = TOTAL_TIMEOUT,
        max_pages_per_provider: int = MAX_PAGES_PER_PROVIDER,
        resolver=None,
        test_transport: Any = None,
    ):
        self.max_redirects = max_redirects
        self.max_body_bytes = max_body_bytes
        self.total_timeout = total_timeout
        self.max_pages_per_provider = max_pages_per_provider
        self._resolver = resolver
        self.transport = test_transport  # TEST-ONLY seam; None in production
        self._page_counts: Dict[str, int] = {}

    def fetch(self, url: str, provider_id: str) -> FetchResult:
        self._bump_page_count(provider_id)
        start = time.monotonic()
        current = url
        chain: List[str] = []
        for _ in range(self.max_redirects + 1):
            parsed = check_url(current)
            self._assert_public(parsed)
            pinned_ip = self._pick_public_ip(parsed)
            status, headers, body = self._transact(parsed, start, pinned_ip=pinned_ip)
            location = headers.get("location")
            if status in (301, 302, 303, 307, 308) and location:
                chain.append(current)
                current = urljoin(current, location)
                # redirect destination is revalidated from scratch
                continue
            self._check_declared_length(headers)
            self._check_content_type(headers)
            body = self._read_limited(body)
            sha256 = hashlib.sha256(body).hexdigest()
            now_dt = datetime.now(timezone.utc)
            return FetchResult(
                status=status,
                headers=headers,
                body=body,
                final_url=current,
                original_url=url,
                redirect_chain=chain,
                content_sha256=sha256,
                retrieved_from_origin=True,
                retrieval_method="safe_fetch",
                retrieved_at=now_dt,
                connected_ip=pinned_ip,
            )
        raise FetcherError("too many redirects")

    def _pick_public_ip(self, parsed: urlparse.ParseResult) -> str:
        """Resolve and return ONE verified public IP (IPv4 preferred).

        DNS rebinding protection: we pick a concrete IP *before* connecting
        and pass it to _transact so the transport connects to that exact IP,
        not to a freshly-resolved hostname.
        """
        host = parsed.hostname or ""
        if self.transport is not None and hasattr(self.transport, "resolve"):
            raw = self.transport.resolve(host)
            if not raw:
                raise FetcherError(f"cannot resolve host {host!r}")
            addresses = [ipaddress.ip_address(a) for a in raw]
        else:
            try:
                infos = socket.getaddrinfo(host, None)
                addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
            except (socket.gaierror, ValueError, OSError) as exc:
                raise FetcherError(f"cannot resolve host {host!r}: {exc}") from exc

        if not addresses:
            raise FetcherError(f"no addresses for host {host!r}")
        if not check_host_addresses(addresses):
            raise FetcherError(f"host {host!r} resolves to non-public addresses")

        # Prefer IPv4 for connection stability
        v4 = [a for a in addresses if isinstance(a, ipaddress.IPv4Address)]
        if v4:
            return str(v4[0])
        return str(addresses[0])

    def _assert_public(self, parsed: urlparse.ParseResult) -> None:
        host = parsed.hostname or ""
        if self.transport is not None and hasattr(self.transport, "resolve"):
            try:
                raw = self.transport.resolve(host)
            except Exception:  # noqa: BLE001 - resolution failure blocks fetch
                raise FetcherError(f"cannot resolve host {host!r}") from None
            if not check_host_addresses([ipaddress.ip_address(a) for a in raw]):
                raise FetcherError(f"host {host!r} resolves to non-public addresses")
            return
        if not dns_resolve(host, self._resolver):
            raise FetcherError(f"host {host!r} does not resolve to public addresses")

    def _transact(self, parsed, start, pinned_ip=None) -> tuple[int, Dict[str, str], bytes]:
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        remaining = self.total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise FetcherError("total request timeout exceeded")
        url = parsed.geturl()
        if self.transport is not None:
            # TEST-ONLY seam: the fake MUST honor the pinned IP it is given
            # (same validated address the production path connects to).
            result = self.transport.request(url, pinned_ip=pinned_ip)
            if time.monotonic() - start > self.total_timeout:
                raise FetcherError("total request timeout exceeded")
            status, headers, body = result
            normalized_headers = {
                str(key).lower(): str(value) for key, value in dict(headers).items()
            }
            return status, normalized_headers, body
        return self._real_request(host, port, parsed, remaining, pinned_ip=pinned_ip)

    def _real_request(
        self, host, port, parsed, timeout, pinned_ip=None
    ) -> tuple[int, Dict[str, str], bytes]:
        # DNS rebinding protection (FIX-003):
        # Connect to the verified public IP, keep the hostname for Host/SNI.
        import http.client
        import ssl

        url = parsed.geturl()
        scheme = parsed.scheme
        try:
            request_target = parsed.path or "/"
            if parsed.query:
                request_target += "?" + parsed.query
        except ValueError:
            request_target = "/"
        if pinned_ip:
            # Pin the socket to the validated IP; hostname stays in Host/SNI.
            raw_sock = socket.create_connection((pinned_ip, port), timeout=timeout)
            if scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(raw_sock, server_hostname=host)
                conn = http.client.HTTPSConnection(host, port, timeout=timeout)
                conn.sock = sock
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
                conn.sock = raw_sock
        else:
            connection_class = (
                http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            )
            conn = connection_class(host, port, timeout=timeout)
        try:
            # Origin-form request target (absolute-form confuses many origins).
            # http.client derives the Host header from the connection host,
            # which is the original hostname even when pinned to an IP.
            conn.request("GET", request_target)
            response = conn.getresponse()
            headers = {str(k).lower(): str(v) for k, v in response.getheaders()}
            self._check_declared_length(headers)
            self._check_content_type(headers)
            body = self._read_limited(response)
            return (response.status, headers, body)
        finally:
            conn.close()

    def _check_content_type(self, headers) -> None:
        ctype = str(headers.get("content-type") or "").lower().split(";")[0].strip()
        if ctype and ctype not in ACCEPTED_CONTENT_TYPES:
            raise FetcherError(f"unsupported content type {ctype!r}")

    def _check_declared_length(self, headers) -> None:
        raw = headers.get("content-length")
        if raw is None:
            return
        try:
            declared = int(str(raw).strip())
        except ValueError:
            return
        if declared > self.max_body_bytes:
            raise FetcherError(f"declared Content-Length {declared} exceeds size limit")

    def _read_limited(self, body) -> bytes:
        """Read at most max_body_bytes + 1 bytes so overflow fails closed."""
        if isinstance(body, (bytes, bytearray, memoryview)):
            data = bytes(body)
            if len(data) > self.max_body_bytes:
                raise FetcherError("response body exceeds size limit")
            return data

        chunks: List[bytes] = []
        total = 0
        read = getattr(body, "read", None)
        if callable(read):
            while total <= self.max_body_bytes:
                size = min(64 * 1024, self.max_body_bytes + 1 - total)
                try:
                    chunk = read(size)
                except Exception as exc:  # noqa: BLE001 - response read failure
                    raise FetcherError("response body read failed") from exc
                if not chunk:
                    break
                try:
                    chunk = bytes(chunk)
                except (TypeError, ValueError) as exc:
                    raise FetcherError("response body is not bytes") from exc
                chunks.append(chunk)
                total += len(chunk)
                if total > self.max_body_bytes:
                    raise FetcherError("response body exceeds size limit")
            return b"".join(chunks)

        try:
            iterator = iter(body)
        except TypeError as exc:
            raise FetcherError("response body is not readable") from exc
        for chunk in iterator:
            try:
                chunk = bytes(chunk)
            except (TypeError, ValueError) as exc:
                raise FetcherError("response body is not bytes") from exc
            chunks.append(chunk)
            total += len(chunk)
            if total > self.max_body_bytes:
                raise FetcherError("response body exceeds size limit")
        return b"".join(chunks)

    def _bump_page_count(self, provider_id: str) -> None:
        count = self._page_counts.get(provider_id, 0)
        if count >= self.max_pages_per_provider:
            raise FetcherError(
                f"provider {provider_id!r} exceeded page limit {self.max_pages_per_provider}"
            )
        self._page_counts[provider_id] = count + 1
