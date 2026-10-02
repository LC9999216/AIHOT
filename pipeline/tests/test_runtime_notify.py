"""Offline HTTP-contract tests, not live Feishu validation.

Contract: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot.md
Only the documented integer code=0 response acknowledges delivery.
"""
import io
import json

import pytest

from hunter.cli import main
from hunter.runtime.notify import WebhookFeishuAdapter, build_feishu_payload
from hunter.runtime.outbox import OutboxMessage, OutboxStore


@pytest.mark.parametrize("body,success", [
    (b'{"code":0,"msg":"success","data":{}}', True),
    (b'{"code":19024,"msg":"Key Words Not Found"}', False),
    (b'{"msg":"success"}', False),
    (b'{"code":false}', False),
    (b'{"code":"0"}', False),
    (b'{}', False),
    (b'[]', False),
    (b'not-json', False),
])
def test_real_adapter_acknowledgement_before_mark_sent(tmp_path, monkeypatch, capsys, body, success):
    path = tmp_path / "notification_outbox.json"
    outbox = OutboxStore(path)
    message = OutboxMessage("evt-ack", "acme", "Acme", "POOL_SUSPENDED", "Suspended", "Public notice")
    outbox.enqueue(message)
    outbox.close()
    calls = []

    class Response(io.BytesIO):
        status = 200

    def transport(request, timeout):
        # On-disk message must still be pending when transport is called.
        state = json.loads(path.read_text(encoding="utf-8"))
        assert "evt-ack" in state["pending"]
        assert "evt-ack" not in state["sent"]
        calls.append(json.loads(request.data))
        return Response(body)

    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://example.invalid/offline")
    monkeypatch.setattr("hunter.runtime.notify.urllib.request.urlopen", transport)
    code = main(["notifications", "drain", "--data-dir", str(tmp_path)])
    assert code == (0 if success else 1)
    assert len(calls) == 1
    report = json.loads(capsys.readouterr().out)
    for key in ("sent", "failed", "skipped_retry"):
        assert report[key + "_count"] == len(report[key])
    reopened = OutboxStore(path)
    try:
        assert bool(reopened.sent()) is success
        assert bool(reopened.pending()) is not success
        if not success:
            assert reopened.pending()[0].attempts == 1
    finally:
        reopened.close()


def test_webhook_payload_uses_only_documented_fields():
    message = OutboxMessage("evt-payload", "acme", "Acme", "POOL_SUSPENDED", "Suspended", "Public notice")
    payload = build_feishu_payload(message)
    assert set(payload) == {"msg_type", "content"}
    assert payload["msg_type"] == "text"
    assert "POOL_SUSPENDED" in payload["content"]["text"]


def test_invalid_webhook_configuration_returns_two_without_touching_outbox(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "invalid-offline-url")
    assert main(["notifications", "drain", "--data-dir", str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "feishu_webhook_not_configured"
    for key in ("sent", "failed", "skipped_retry"):
        assert report[key + "_count"] == 0
    assert not (tmp_path / ".outbox.lock").exists()
