from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hunter import cli
from hunter.llm.client import LlmRequest


def _request() -> LlmRequest:
    return LlmRequest(
        system="system",
        user="extract only grounded facts",
        schema={"type": "object"},
        evidence_id="ev-1",
        evidence_text="Free API quota: 100 requests per day.",
    )


def test_openai_compatible_client_sends_toolless_json_without_secret_in_body() -> None:
    from hunter.llm.client import OpenAICompatibleStructuredClient

    captured = {}

    def transport(request, *, timeout: float, max_response_bytes: int) -> bytes:
        captured["request"] = request
        captured["timeout"] = timeout
        captured["max_response_bytes"] = max_response_bytes
        return json.dumps(
            {"choices": [{"message": {"content": '{"offer_kind": null}'}}]}
        ).encode("utf-8")

    client = OpenAICompatibleStructuredClient(
        base_url="https://llm.example/v1",
        api_key="super-secret",
        model="extractor-model",
        transport=transport,
    )

    response = client.complete(_request())

    outgoing = captured["request"]
    body = outgoing.data.decode("utf-8")
    payload = json.loads(body)
    assert outgoing.full_url == "https://llm.example/v1/chat/completions"
    assert outgoing.get_header("Authorization") == "Bearer super-secret"
    assert "super-secret" not in body
    assert "tools" not in payload
    assert payload["model"] == "extractor-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert response.text == '{"offer_kind": null}'
    assert response.raw == {}


@pytest.mark.parametrize(
    "base_url",
    [
        "http://llm.example/v1",
        "ftp://llm.example/v1",
        "https://user:password@llm.example/v1",
    ],
)
def test_openai_compatible_client_rejects_unsafe_remote_base_urls(base_url: str) -> None:
    from hunter.llm.client import LlmConfigurationError, OpenAICompatibleStructuredClient

    with pytest.raises(LlmConfigurationError):
        OpenAICompatibleStructuredClient(
            base_url=base_url,
            api_key="secret",
            model="extractor-model",
        )


def test_default_llm_transport_rejects_redirect_without_forwarding_authorization() -> None:
    from urllib.request import Request

    from hunter.llm.client import LlmClientError, _default_http_transport

    observed = {"sink_hits": 0, "sink_authorization": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(302)
            self.send_header("Location", "/sink")
            self.end_headers()

        def do_GET(self):  # noqa: N802 - stdlib callback name
            observed["sink_hits"] += 1
            observed["sink_authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices": []}')

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/start",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer super-secret"},
        )
        with pytest.raises(LlmClientError, match="redirect"):
            _default_http_transport(
                request,
                timeout=2,
                max_response_bytes=1024,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert observed == {"sink_hits": 0, "sink_authorization": None}


def test_default_llm_transport_sanitizes_socket_failures(monkeypatch) -> None:
    from urllib.request import Request

    import hunter.llm.client as client_module
    from hunter.llm.client import LlmClientError, _default_http_transport

    class BrokenOpener:
        def open(self, *_args, **_kwargs):
            raise ConnectionError("sensitive resolver and proxy details")

    monkeypatch.setattr(client_module, "build_opener", lambda *_args: BrokenOpener())
    request = Request("https://llm.example/v1/chat/completions", data=b"{}", method="POST")

    with pytest.raises(LlmClientError) as exc_info:
        _default_http_transport(request, timeout=2, max_response_bytes=1024)

    assert "sensitive resolver" not in str(exc_info.value)


def test_runtime_extractor_factory_is_disabled_when_no_llm_variables_exist() -> None:
    from hunter.llm.runtime import build_grounded_extractor_from_env

    assert build_grounded_extractor_from_env({}) is None


def test_runtime_extractor_factory_rejects_partial_configuration_without_leaking_values() -> None:
    from hunter.llm.client import LlmConfigurationError
    from hunter.llm.runtime import build_grounded_extractor_from_env

    with pytest.raises(LlmConfigurationError) as exc_info:
        build_grounded_extractor_from_env(
            {
                "HUNTER_LLM_BASE_URL": "https://llm.example/v1",
                "HUNTER_LLM_API_KEY": "super-secret",
            }
        )

    message = str(exc_info.value)
    assert "HUNTER_LLM_MODEL" in message
    assert "super-secret" not in message


def test_run_stage_one_wires_configured_runtime_extractor(monkeypatch, tmp_path: Path) -> None:
    import hunter.llm.runtime as llm_runtime
    import hunter.pipeline as pipeline_module

    sentinel = object()
    observed = {}

    class FakePipeline:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def run(self):
            return {}

    monkeypatch.setattr(
        llm_runtime,
        "build_grounded_extractor_from_env",
        lambda: sentinel,
    )
    monkeypatch.setattr(pipeline_module, "Pipeline", FakePipeline)

    assert cli.main(["run-stage-one", "--data-dir", str(tmp_path)]) == 0
    assert observed["extractor"] is sentinel


def test_run_stage_one_prints_sanitized_candidate_outcomes(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import hunter.llm.runtime as llm_runtime
    import hunter.pipeline as pipeline_module

    class FakePipeline:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return {
                "detail": {
                    "candidate_outcomes": [
                        {
                            "candidate_id": "acme",
                            "outcome": "extraction_failed",
                            "reason": "grounding mismatch",
                        }
                    ]
                }
            }

    monkeypatch.setattr(llm_runtime, "build_grounded_extractor_from_env", lambda: object())
    monkeypatch.setattr(pipeline_module, "Pipeline", FakePipeline)

    assert cli.main(["run-stage-one", "--data-dir", str(tmp_path)]) == 0

    line = next(
        item
        for item in capsys.readouterr().out.splitlines()
        if item.startswith("candidate_outcome=")
    )
    assert json.loads(line.removeprefix("candidate_outcome=")) == {
        "candidate_id": "acme",
        "outcome": "extraction_failed",
        "reason": "grounding mismatch",
    }


def test_run_stage_one_reports_invalid_llm_configuration(monkeypatch, tmp_path: Path, capsys) -> None:
    import hunter.llm.runtime as llm_runtime
    from hunter.llm.client import LlmConfigurationError

    monkeypatch.setattr(
        llm_runtime,
        "build_grounded_extractor_from_env",
        lambda: (_ for _ in ()).throw(
            LlmConfigurationError("missing HUNTER_LLM_MODEL")
        ),
    )

    assert cli.main(["run-stage-one", "--data-dir", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "missing HUNTER_LLM_MODEL" in captured.err


def test_run_stage_one_require_llm_fails_when_runtime_extractor_is_disabled(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import hunter.llm.runtime as llm_runtime

    monkeypatch.setattr(
        llm_runtime,
        "build_grounded_extractor_from_env",
        lambda: None,
    )

    assert (
        cli.main(
            [
                "run-stage-one",
                "--data-dir",
                str(tmp_path),
                "--require-llm",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "real grounded LLM extractor is required" in captured.err
