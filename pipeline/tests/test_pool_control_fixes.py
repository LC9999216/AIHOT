"""Pool Control security regressions (review round 2, items 二 and 三).

Covers:
- safe TOML serialization: newline/quote/injection in provider ids and keys;
- atomic writes with fsync; secure file permissions;
- key entry ONLY through the injected secret reader (never argv/env/body/log);
- no key material in any response, status, or serialized file name;
- real health/protocol classification via injectable probe runner
  (no hardcoded healthy, no simulate-all-pass parameter);
- promote verified by readback; suspend verified by readback and truthful
  failure; stop_production containment;
- staging/production isolation from the Hunter repository;
- loopback HTTP API: bearer auth, no keys in bodies or responses.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hunter.pool_api import PoolApiError, PoolControlClient, PoolControlServer
from hunter.pool_control import (
    FreellmpoolProbeRunner,
    PoolControl,
    PoolControlError,
)
from hunter.pool_toml import (
    escape_basic_string,
    read_config_keys,
    read_providers_toml,
    render_config_toml,
    render_providers_toml,
    table_key,
)


SECRET = "sk-live-SECRETKEY1234567890"


class StaticSecretReader:
    def __init__(self, secret: str = SECRET):
        self.secret = secret
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.secret


class FakeProbeRunner:
    """Deterministic probe fake — dependency injection, not a production path."""

    def __init__(self, results=None, raise_exc: Exception | None = None):
        self.results = results or {}
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def run(self, provider_record, key_env, config_file, features=("chat",), timeout=20.0):
        self.calls.append({"id": provider_record.get("id"), "features": list(features)})
        if self.raise_exc:
            raise self.raise_exc
        return {
            feature: dict(self.results.get(feature, {"status": "pass", "classification": "verified"}))
            for feature in features
        }


def _control(tmp_path: Path, probe_runner=None, hunter_root=None) -> PoolControl:
    return PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=StaticSecretReader(),
        probe_runner=probe_runner,
        hunter_root=hunter_root,
    )


def _registered_with_key(control: PoolControl, provider_id: str = "acme") -> None:
    control.register_provider(
        provider_id,
        label="Acme AI",
        base_url="https://api.acme.ai/v1",
        models=[{"name": "acme-mini"}],
    )
    result = control.enter_key(provider_id)
    assert result["configured"] is True


# =============================================================================
# TOML serialization safety
# =============================================================================


def test_escape_basic_string_handles_quotes_and_newlines() -> None:
    assert escape_basic_string('a "quoted" \\ value') == '"a \\"quoted\\" \\\\ value"'
    assert escape_basic_string("line1\nline2\ttab") == '"line1\\nline2\\ttab"'
    assert escape_basic_string("ctrl\x01") == '"ctrl\\u0001"'


def test_table_key_quotes_non_bare_ids() -> None:
    assert table_key("plain-id_1") == "plain-id_1"
    assert table_key('evil"]\napi_key = "x') == '"evil\\"]\\napi_key = \\"x"'


def test_provider_id_injection_cannot_break_toml(tmp_path: Path) -> None:
    control = _control(tmp_path)
    evil_id = 'acme"]\n[[provider]]\nid = "injected"'
    control.register_provider(evil_id, base_url="https://api.acme.ai/v1")
    providers = read_providers_toml(control.staging_providers_path)
    ids = [p.get("id") for p in providers]
    # parsed back EXACTLY as ONE provider — no injected second entry
    assert ids == [evil_id]
    assert len(providers) == 1


def test_key_value_injection_cannot_break_toml(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    evil_reader = StaticSecretReader('x"\nHACKED = "1\n[evil]\nkey = "v')
    control._read_secret = evil_reader
    control.enter_key("acme")
    keys = read_config_keys(control.staging_config_path)
    assert list(keys.keys()) == ["ACME_API_KEY"]
    assert keys["ACME_API_KEY"] == 'x"\nHACKED = "1\n[evil]\nkey = "v'


def test_key_value_never_in_providers_toml(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    toml_text = control.staging_providers_path.read_text(encoding="utf-8")
    assert SECRET not in toml_text
    assert "api_key =" not in toml_text


def test_render_providers_toml_rejects_key_material() -> None:
    with pytest.raises(ValueError):
        render_providers_toml([{"id": "x", "api_key": "sk-123"}])


def test_render_config_toml_round_trips() -> None:
    keys = {"A_KEY": 'va"lue\nnewline', "B": "plain"}
    text = render_config_toml(keys)
    import tomllib

    assert tomllib.loads(text)["keys"] == keys


# =============================================================================
# Isolation & permissions
# =============================================================================


def test_pool_dirs_inside_hunter_repo_rejected(tmp_path: Path) -> None:
    hunter_root = tmp_path / "repo"
    hunter_root.mkdir()
    with pytest.raises(PoolControlError):
        PoolControl(
            tmp_path / "repo" / "staging",
            tmp_path / "elsewhere" / "production",
            read_secret=StaticSecretReader(),
            hunter_root=hunter_root,
        )
    with pytest.raises(PoolControlError):
        PoolControl(
            tmp_path / "elsewhere" / "staging",
            tmp_path / "repo" / "production",
            read_secret=StaticSecretReader(),
            hunter_root=hunter_root,
        )


def test_pool_dirs_inside_hunter_subtree_rejected(tmp_path: Path) -> None:
    hunter_root = tmp_path / "repo"
    hunter_root.mkdir()
    with pytest.raises(PoolControlError):
        PoolControl(
            hunter_root / "secrets" / "staging",
            hunter_root / "secrets" / "production",
            read_secret=StaticSecretReader(),
            hunter_root=hunter_root,
        )


def test_pool_dirs_outside_hunter_repo_accepted(tmp_path: Path) -> None:
    hunter_root = tmp_path / "repo"
    hunter_root.mkdir()
    control = PoolControl(
        tmp_path / "pool-stage",
        tmp_path / "pool-prod",
        read_secret=StaticSecretReader(),
        hunter_root=hunter_root,
    )
    control.ensure_isolated()  # no raise


@pytest.mark.skipif(
    __import__("os").name != "posix",
    reason="POSIX permission bits",
)
def test_production_files_have_owner_only_permissions(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result["promoted"] is True
    assert control.verify_production_permissions() is True


# =============================================================================
# Probe classification (no hardcoded healthy, no bypass params)
# =============================================================================


def test_probe_fails_closed_without_runner(tmp_path: Path) -> None:
    control = _control(tmp_path)  # no probe runner
    _registered_with_key(control)
    result = control.probe("acme")
    assert result["health_status"] == "UNKNOWN"
    assert result["error"] == "probe_runner_unavailable"


def test_probe_classifies_auth_failure(tmp_path: Path) -> None:
    control = _control(
        tmp_path,
        probe_runner=FakeProbeRunner(results={"chat": {"status": "fail", "classification": "auth"}}),
    )
    _registered_with_key(control)
    result = control.probe_health("acme")
    assert result["health_status"] == "INVALID_KEY"
    assert result["classification"] == "auth"


def test_probe_classifies_rate_limit_quota_timeout_network_model(tmp_path: Path) -> None:
    # upstream classification -> (mapped HealthStatus, mapped detail code)
    cases = {
        "rate_limit": ("RATE_LIMITED", "rate_limit"),
        "quota": ("EXHAUSTED", "quota"),
        "timeout": ("DOWN", "timeout"),
        "transport": ("DOWN", "network"),
        "availability": ("DOWN", "availability"),
        "unsupported": ("DOWN", "model_not_found"),
        "client": ("DOWN", "client_error"),
    }
    for classification, (expected_health, expected_code) in cases.items():
        control = _control(
            tmp_path / classification,
            probe_runner=FakeProbeRunner(
                results={"chat": {"status": "fail", "classification": classification}}
            ),
        )
        _registered_with_key(control)
        result = control.probe_health("acme")
        assert result["health_status"] == expected_health, classification
        assert result["classification"] == expected_code, classification


def test_probe_protocols_checks_all_four_features(tmp_path: Path) -> None:
    runner = FakeProbeRunner(
        results={
            "chat": {"status": "pass", "classification": "verified"},
            "responses": {"status": "pass", "classification": "verified"},
            "streaming": {"status": "pass", "classification": "verified"},
            "tools": {"status": "pass", "classification": "verified"},
        }
    )
    control = _control(tmp_path, probe_runner=runner)
    _registered_with_key(control)
    result = control.probe_protocols("acme")
    assert set(result["features"].keys()) == {"chat", "responses", "streaming", "tools"}
    assert result["health_status"] == "HEALTHY"
    called = runner.calls[0]
    assert set(called["features"]) == {"chat", "responses", "streaming", "tools"}


def test_probe_protocol_partial_failure_not_all_pass(tmp_path: Path) -> None:
    runner = FakeProbeRunner(
        results={"tools": {"status": "fail", "classification": "unsupported"}}
    )
    control = _control(tmp_path, probe_runner=runner)
    _registered_with_key(control)
    result = control.probe_protocols("acme")
    assert result["features"]["tools"]["status"] == "fail"
    assert result["health_status"] == "HEALTHY"  # chat passed
    assert result["classification"] == "model_not_found"


def test_probe_runner_exception_fails_closed(tmp_path: Path) -> None:
    control = _control(tmp_path, probe_runner=FakeProbeRunner(raise_exc=RuntimeError("sk-secret")))
    _registered_with_key(control)
    result = control.probe_health("acme")
    assert result["health_status"] == "DOWN"
    assert result["classification"] == "probe_failed"
    assert "sk-secret" not in json.dumps(result)


def test_production_control_has_no_simulate_parameter() -> None:
    import inspect

    for parameter in ("simulate_all_pass", "simulate", "force_healthy", "assume_healthy"):
        params = inspect.signature(PoolControl.probe).parameters
        assert parameter not in params


# =============================================================================
# Key isolation
# =============================================================================


def test_enter_key_uses_secret_reader_and_never_returns_key(tmp_path: Path) -> None:
    reader = StaticSecretReader()
    control = PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=reader,
    )
    _registered_with_key(control)
    assert reader.prompts  # prompted via the reader, not argv/env
    status = control.status("acme")
    dumped = json.dumps(status.__dict__)
    assert SECRET not in dumped
    assert status.key_configured is True


def test_status_response_never_contains_key(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    control.promote("acme")
    status = control.status("acme")
    assert SECRET not in json.dumps(status.__dict__)


def test_enter_key_requires_registration(tmp_path: Path) -> None:
    control = _control(tmp_path)
    result = control.enter_key("ghost")
    assert result["configured"] is False
    assert result["error"] == "provider_not_registered"


def test_register_provider_rejects_bad_input(tmp_path: Path) -> None:
    control = _control(tmp_path)
    with pytest.raises(PoolControlError):
        control.register_provider("", base_url="https://x.example")
    with pytest.raises(PoolControlError):
        control.register_provider("x", base_url="ftp://not-http")


# =============================================================================
# Promote / suspend fail-closed with readback
# =============================================================================


def test_promote_requires_staging_key(tmp_path: Path) -> None:
    control = _control(tmp_path)
    control.register_provider("acme", base_url="https://api.acme.ai/v1")
    result = control.promote("acme")
    assert result["promoted"] is False
    assert result["error"] == "key_not_configured"
    assert control.list_production() == []


def test_promote_verified_by_readback(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result["promoted"] is True
    assert "acme" in control.list_production()
    # production config carries the key; the providers.toml does not
    prod_keys = read_config_keys(control.production_config_path)
    assert prod_keys.get("ACME_API_KEY") == SECRET
    prod_providers_text = control.production_providers_path.read_text(encoding="utf-8")
    assert SECRET not in prod_providers_text


def test_promote_blocked_when_production_halted(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    control.stop_production()
    result = control.promote("acme")
    assert result["promoted"] is False
    assert result["error"] == "production_halted"


def test_suspend_verified_by_readback(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    control.promote("acme")
    result = control.suspend("acme")
    assert result["suspended"] is True
    assert control.list_production() == []
    assert read_config_keys(control.production_config_path) == {}


def test_suspend_absent_provider_is_noop_success(tmp_path: Path) -> None:
    control = _control(tmp_path)
    result = control.suspend("ghost")
    assert result["suspended"] is True
    assert result.get("already_absent") is True


def test_suspend_write_failure_returns_false(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    control.promote("acme")

    def failing_write(providers, keys):
        raise OSError("disk full")

    control._write_production = failing_write
    result = control.suspend("acme")
    assert result["suspended"] is False
    assert result["error"] == "production_write_failed"


# =============================================================================
# Loopback HTTP API
# =============================================================================


def _serve(control: PoolControl) -> tuple[PoolControlServer, PoolControlClient]:
    server = PoolControlServer(control, bearer_token="test-bearer-token")
    server.start_background()
    client = PoolControlClient(server.url, "test-bearer-token")
    return server, client


def test_server_start_background_waits_until_ready(tmp_path: Path) -> None:
    control = _control(tmp_path)
    server = PoolControlServer(control, bearer_token="test-bearer-token")
    try:
        thread = server.start_background(timeout=2.0)
        assert thread.is_alive()
        connection = http.client.HTTPConnection(server.host, server.port, timeout=1.0)
        try:
            connection.request(
                "GET",
                "/providers/missing/status",
                headers={"Authorization": "Bearer test-bearer-token"},
            )
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["provider_id"] == "missing"
        finally:
            connection.close()
    finally:
        if getattr(server, "_thread", None) is not None:
            server.shutdown()
        else:
            server._httpd.server_close()


class CaptureFixture:
    def __init__(self) -> None:
        fixture = self
        self.seen_authorization: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fixture.seen_authorization.append(
                    self.headers.get("Authorization", "")
                )
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RedirectFixture:
    def __init__(self, location: str) -> None:
        fixture = self
        self.location = location
        self.seen_authorization: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fixture.seen_authorization.append(
                    self.headers.get("Authorization", "")
                )
                self.send_response(302)
                self.send_header("Location", fixture.location)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "127.0.0.2"])
def test_http_server_rejects_noncanonical_loopback(tmp_path: Path, host: str) -> None:
    control = _control(tmp_path)
    server = None
    try:
        with pytest.raises(PoolApiError, match="loopback_host_required"):
            server = PoolControlServer(control, bearer_token="test-bearer-token", host=host)
    finally:
        if server is not None:
            server._httpd.server_close()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9000",
        "http://localhost:9000",
        "http://127.0.0.2:9000",
        "http://user@127.0.0.1:9000",
        "http://127.0.0.1:9000/path",
        "http://example.com:9000",
    ],
)
def test_pool_client_rejects_noncanonical_base_url(url: str) -> None:
    with pytest.raises(PoolApiError, match="loopback_base_url_required"):
        PoolControlClient(url, "test-bearer-token")


def test_pool_client_refuses_redirect_before_forwarding_token() -> None:
    sink = CaptureFixture()
    sink.start()
    redirect = RedirectFixture(location=f"{sink.base_url}/capture")
    redirect.start()
    try:
        client = PoolControlClient(redirect.base_url, "test-bearer-token")
        with pytest.raises(PoolApiError, match="redirect_refused"):
            client.status("acme")
        assert redirect.seen_authorization == ["Bearer test-bearer-token"]
        assert sink.seen_authorization == []
    finally:
        redirect.stop()
        sink.stop()


def test_pool_client_ignores_system_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    server = PoolControlServer(_control(tmp_path), bearer_token="test-bearer-token")
    try:
        server.start_background()
        client = PoolControlClient(server.url, "test-bearer-token")
        assert client.status("missing")["provider_id"] == "missing"
    finally:
        server.shutdown()


def test_http_api_rejects_missing_or_wrong_token(tmp_path: Path) -> None:
    import urllib.request

    control = _control(tmp_path)
    _registered_with_key(control)
    server, _ = _serve(control)
    try:
        for bad_token in ("", "wrong-token"):
            client = PoolControlClient(server.url, bad_token or "x")
            if not bad_token:
                with pytest.raises(PoolApiError):
                    client.status("acme")
                continue
            with pytest.raises(PoolApiError):
                client.status("acme")
        # raw request without any Authorization header
        request = urllib.request.Request(f"{server.url}/providers/acme/status")
        try:
            urllib.request.urlopen(request, timeout=5)
            raised = False
        except Exception:
            raised = True
        assert raised
    finally:
        server.shutdown()


def test_http_api_status_has_no_key(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    server, client = _serve(control)
    try:
        status = client.status("acme")
        assert status["key_configured"] is True
        assert SECRET not in json.dumps(status)
    finally:
        server.shutdown()


def test_http_api_promote_suspend_roundtrip(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    server, client = _serve(control)
    try:
        promoted = client.promote("acme")
        assert promoted["promoted"] is True
        status = client.status("acme")
        assert status["in_production"] is True
        suspended = client.suspend("acme")
        assert suspended["suspended"] is True
        status = client.status("acme")
        assert status["in_production"] is False
    finally:
        server.shutdown()


def test_http_api_probe_classified_via_runner(tmp_path: Path) -> None:
    control = _control(
        tmp_path,
        probe_runner=FakeProbeRunner(results={"chat": {"status": "fail", "classification": "auth"}}),
    )
    _registered_with_key(control)
    server, client = _serve(control)
    try:
        result = client.probe("acme")
        assert result["health_status"] == "INVALID_KEY"
    finally:
        server.shutdown()


def test_http_api_does_not_expose_remote_resume(tmp_path: Path) -> None:
    server, client = _serve(_control(tmp_path))
    try:
        with pytest.raises(PoolApiError, match="not_found"):
            client._request("POST", "/pool/resume", body={})
    finally:
        server.shutdown()


def test_http_api_pool_status_returns_sanitized_provider_ids(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _registered_with_key(control)
    assert control.promote("acme")["promoted"] is True
    server, client = _serve(control)
    try:
        assert client.list_production() == ["acme"]
    finally:
        server.shutdown()


def test_http_api_rejects_key_material_in_body(tmp_path: Path) -> None:
    import urllib.request

    control = _control(tmp_path)
    _registered_with_key(control)
    server, client = _serve(control)
    try:
        with pytest.raises(PoolApiError):
            client._request(
                "POST",
                "/providers/acme/probe",
                body={"api_key": SECRET, "features": ["chat"]},
            )
    finally:
        server.shutdown()


def test_http_server_binds_loopback_only(tmp_path: Path) -> None:
    control = _control(tmp_path)
    server, _ = _serve(control)
    try:
        assert server.host == "127.0.0.1"
    finally:
        server.shutdown()
