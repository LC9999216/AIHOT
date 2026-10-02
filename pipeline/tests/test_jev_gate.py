# tests/test_jev_gate.py — Jev gate unit tests (network mocked)
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from src.hunter.discovery import jev_gate


@pytest.fixture(autouse=True)
def _jev_key(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "test-key")


class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _mock_jev(monkeypatch, answers, usage=None):
    def fake_urlopen(req, timeout):
        return _FakeResp({"model": "jev-1.13.0", "answers": answers, "usage": usage or {"input_tokens": 400}})
    monkeypatch.setattr(jev_gate.urllib.request, "urlopen", fake_urlopen)


def _answers(bucket="free_tier_legit", noul=0.9):
    return {
        "bucket": {"type": "choice", "choice": bucket},
        "trust_domain": {"type": "noul", "noul": noul},
    }


def test_passes_free_tier_legit(monkeypatch):
    _mock_jev(monkeypatch, _answers())
    post = {"claim": "Cohere free tier gives 1000 req/day", "quote": "free tier", "source": "x"}
    assert jev_gate.gate_post(post) == "free_tier_legit"


def test_rejects_scam(monkeypatch):
    _mock_jev(monkeypatch, _answers(bucket="spam_scam"))
    assert jev_gate.gate_post({"claim": "free unlimited GPT at prov-ai.net"}) is None


def test_rejects_counterfeit_domain(monkeypatch):
    _mock_jev(monkeypatch, _answers(bucket="free_tier_legit", noul=0.08))
    assert jev_gate.gate_post({"claim": "free API at prov-ai.net"}) is None


def test_fail_open_on_api_error(monkeypatch):
    def boom(req, timeout):
        raise RuntimeError("429")
    monkeypatch.setattr(jev_gate.urllib.request, "urlopen", boom)
    assert jev_gate.gate_post({"claim": "some claim"}) is None


def test_empty_claim_dropped():
    assert jev_gate.gate_post({"claim": ""}) is None


def test_gate_posts_batch(monkeypatch):
    _mock_jev(monkeypatch, _answers())
    kept = jev_gate.gate_posts([{"claim": "free tier at cohere.com"}, {"claim": ""}])
    assert len(kept) == 1 and kept[0]["jev_bucket"] == "free_tier_legit"
