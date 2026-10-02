"""Pool Control tests (rewritten for the real FreeLLMPool integration).

The previous revision asserted the FAKE behavior the review flagged:
hand-concatenated TOML, hard-coded healthy probes, inject_key. These tests now
exercise the real surface: registration, getpass-injected key entry, real
classified probes via an injectable runner, promote/suspend with readback.
"""

from __future__ import annotations

import json
from pathlib import Path

from hunter.pool_control import PoolControl, PoolProviderStatus

SECRET = "sk-test-SECRET-0123456789abcdef"


class StaticSecretReader:
    def __init__(self, secret: str = SECRET):
        self.secret = secret
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.secret


class FakeProbeRunner:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def run(self, provider_record, key_env, config_file, features=("chat",), timeout=20.0):
        self.calls.append({"id": provider_record.get("id"), "key_env": key_env})
        return {
            feature: dict(
                self.results.get(
                    feature, {"status": "pass", "classification": "verified"}
                )
            )
            for feature in features
        }


def _control(tmp_path: Path, probe_runner=None) -> PoolControl:
    return PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=StaticSecretReader(),
        probe_runner=probe_runner,
    )


def _ready(control: PoolControl, provider_id: str = "acme") -> None:
    control.register_provider(
        provider_id,
        label="Acme AI",
        base_url="https://api.acme.ai/v1",
        models=[{"name": "acme-mini"}],
    )
    assert control.enter_key(provider_id)["configured"] is True


def test_pool_control_creates_directories(tmp_path: Path) -> None:
    _control(tmp_path)
    assert (tmp_path / "staging").is_dir()
    assert (tmp_path / "production").is_dir()


def test_pool_control_empty_status(tmp_path: Path) -> None:
    control = _control(tmp_path)
    status = control.status("ghost")
    assert isinstance(status, PoolProviderStatus)
    assert status.in_staging is False
    assert status.in_production is False
    assert status.key_configured is False
    assert status.health_status == "unknown"


def test_enter_key_creates_staging_config(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control)
    keys_text = control.staging_config_path.read_text(encoding="utf-8")
    assert "ACME_API_KEY" in keys_text
    assert SECRET in keys_text  # inside Pool Control boundary only
    # providers.toml must NOT contain the key
    providers_text = control.staging_providers_path.read_text(encoding="utf-8")
    assert SECRET not in providers_text


def test_key_not_in_status_response(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control)
    control.promote("acme")
    status = control.status("acme")
    assert SECRET not in json.dumps(status.__dict__)


def test_probe_requires_key(tmp_path: Path) -> None:
    control = _control(tmp_path, probe_runner=FakeProbeRunner())
    control.register_provider("acme", base_url="https://api.acme.ai/v1")
    result = control.probe("acme")
    assert result["health_status"] == "UNKNOWN"
    assert result["error"] == "key_not_configured"


def test_probe_without_runner_fails_closed(tmp_path: Path) -> None:
    control = _control(tmp_path)  # no runner wired
    _ready(control)
    result = control.probe("acme")
    assert result["health_status"] == "UNKNOWN"
    assert result["error"] == "probe_runner_unavailable"


def test_probe_with_key_returns_classified_result(tmp_path: Path) -> None:
    control = _control(
        tmp_path,
        probe_runner=FakeProbeRunner(
            results={"chat": {"status": "pass", "classification": "verified"}}
        ),
    )
    _ready(control)
    result = control.probe_health("acme")
    assert result["health_status"] == "HEALTHY"
    assert result["classification"] == "verified"
    assert result["error"] is None


def test_probe_updates_health_status(tmp_path: Path) -> None:
    control = _control(
        tmp_path,
        probe_runner=FakeProbeRunner(
            results={"chat": {"status": "fail", "classification": "quota"}}
        ),
    )
    _ready(control)
    control.probe_health("acme")
    status = control.status("acme")
    assert status.health_status == "EXHAUSTED"


def test_promote_without_staging_fails(tmp_path: Path) -> None:
    control = _control(tmp_path)
    result = control.promote("ghost")
    assert result["promoted"] is False
    assert result["error"] == "provider_not_registered"


def test_promote_copies_config(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control)
    result = control.promote("acme")
    assert result["promoted"] is True
    prod_providers = control.production_providers_path.read_text(encoding="utf-8")
    assert 'id = "acme"' in prod_providers
    assert "api.acme.ai" in prod_providers


def test_promote_does_not_expose_key_in_result(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control)
    result = control.promote("acme")
    assert SECRET not in json.dumps(result)


def test_suspend_removes_from_production(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control)
    control.promote("acme")
    result = control.suspend("acme")
    assert result["suspended"] is True
    assert "acme" not in control.list_production()


def test_suspend_preserves_staging(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control)
    control.promote("acme")
    control.suspend("acme")
    # staging key survives for re-promotion without re-entering credentials
    assert control.status("acme").key_configured is True
    assert "acme" in control.staging_providers_path.read_text(encoding="utf-8")


def test_multiple_providers_independent(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _ready(control, "acme")
    _ready(control, "beta")
    assert control.promote("acme")["promoted"] is True
    assert control.list_production() == ["acme"]
    assert control.promote("beta")["promoted"] is True
    assert control.list_production() == ["acme", "beta"]
    assert control.suspend("acme")["suspended"] is True
    assert control.list_production() == ["beta"]


def test_probe_uses_staging_key_env(tmp_path: Path) -> None:
    runner = FakeProbeRunner()
    control = _control(tmp_path, probe_runner=runner)
    _ready(control)
    control.probe_health("acme")
    assert runner.calls[0]["key_env"] == "ACME_API_KEY"
