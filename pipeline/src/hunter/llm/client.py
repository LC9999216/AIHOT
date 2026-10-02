"""Replaceable structured LLM client interface (TASK-009).

The client receives messages, a JSON schema, and evidence text only. It gets no
tools, no environment variables, no files, and no arbitrary network access.
Implementations are injected so all default tests stay offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


@dataclass(frozen=True)
class LlmRequest:
    """A single tool-less structured completion request."""

    system: str
    user: str
    schema: Dict[str, Any]
    evidence_id: str
    evidence_text: str
    tools: Optional[Sequence[Any]] = None  # must stay empty; tools are forbidden
    max_repairs: int = 1

    def __post_init__(self) -> None:
        if self.tools:
            raise ValueError("the structured LLM boundary never passes tools")


@dataclass(frozen=True)
class LlmResponse:
    text: str
    raw: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class StructuredLlmClient(Protocol):
    """Anything that can complete a tool-less structured request."""

    def complete(self, request: LlmRequest) -> LlmResponse: ...


class LlmConfigurationError(ValueError):
    """Raised when the production LLM boundary is configured unsafely."""


class LlmClientError(RuntimeError):
    """Sanitized runtime failure from the production LLM boundary."""


LlmHttpTransport = Callable[..., bytes]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_http_transport(
    request: Request,
    *,
    timeout: float,
    max_response_bytes: int,
) -> bytes:
    """Execute one bounded JSON request without returning upstream bodies in errors."""
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise LlmClientError("LLM response content type is not application/json")
            body = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise LlmClientError("LLM redirects are not allowed") from None
        raise LlmClientError(f"LLM request failed with HTTP status {exc.code}") from None
    except (URLError, OSError):
        raise LlmClientError("LLM request failed before a response was received") from None
    if len(body) > max_response_bytes:
        raise LlmClientError("LLM response exceeded the configured size limit")
    return body


class OpenAICompatibleStructuredClient:
    """Minimal tool-less Chat Completions client for grounded extraction.

    The API key is carried only in the Authorization header. It is never placed
    in the model messages, response object, exception text, or serialized data.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        transport: Optional[LlmHttpTransport] = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise LlmConfigurationError("HUNTER_LLM_BASE_URL must be an HTTPS URL")
        if parsed.username or parsed.password:
            raise LlmConfigurationError("HUNTER_LLM_BASE_URL must not contain user-info")
        if parsed.query or parsed.fragment:
            raise LlmConfigurationError("HUNTER_LLM_BASE_URL must not contain query or fragment")
        if not api_key:
            raise LlmConfigurationError("HUNTER_LLM_API_KEY must not be empty")
        if not model:
            raise LlmConfigurationError("HUNTER_LLM_MODEL must not be empty")
        if timeout <= 0 or max_response_bytes <= 0:
            raise LlmConfigurationError("LLM timeout and response limit must be positive")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._transport = transport or _default_http_transport

    def complete(self, request: LlmRequest) -> LlmResponse:
        assert_no_tools(request)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        http_request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        raw = self._transport(
            http_request,
            timeout=self.timeout,
            max_response_bytes=self.max_response_bytes,
        )
        try:
            parsed = json.loads(raw.decode("utf-8"))
            content = parsed["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise LlmClientError("LLM response did not contain a valid message") from None
        if not isinstance(content, str):
            raise LlmClientError("LLM response message content was not text")
        return LlmResponse(text=content)


class FakeStructuredClient:
    """In-memory client for tests and offline pipelines.

    Accepts a queue of raw text outputs; never touches the network.
    """

    def __init__(self, responses: Optional[Sequence[str]] = None):
        self.responses: List[str] = list(responses or [])
        self.requests: List[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if self.responses:
            return LlmResponse(text=self.responses.pop(0))
        return LlmResponse(text="{}")


def assert_no_secrets(payload: str, secret_values: Sequence[str]) -> None:
    """Raise if any known secret value appears in outbound model payload."""
    for secret in secret_values:
        if secret and secret in payload:
            raise ValueError("refusing to send secret material to the LLM boundary")


def assert_no_tools(request: LlmRequest) -> None:
    """Raise if a request carries tools.

    Only the request's own structure is inspected: untrusted page text may
    legitimately contain strings like ``api_key=`` and must not be mistaken for
    outbound credential material. Real secrets are guarded separately by
    :func:`assert_no_secrets` against known secret values.
    """
    if request.tools:
        raise ValueError("refusing to send tools to the LLM boundary")
