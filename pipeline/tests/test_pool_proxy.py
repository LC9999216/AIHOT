"""Offline regression tests for the Pool Control live-proxy boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hunter.pool_control import PoolControl, PoolControlError
from hunter.pool_proxy import FreellmpoolProxySupervisor


class StaticSecretReader:
    def __call__(self, prompt: str) -> str:
        return "test-provider-key"


class FakeProxySupervisor:
    def __init__(self, provider_ids: list[str], *, fail_reload: bool = False):
        self._provider_ids = set(provider_ids)
        self.fail_reload = fail_reload
        self.reload_calls = 0
        self.halt_calls = 0

    def reload(self) -> None:
        self.reload_calls += 1
        if self.fail_reload:
            raise RuntimeError("synthetic reload failure without secret material")

    def halt(self) -> None:
        self.halt_calls += 1
        self._provider_ids.clear()

    def running_provider_ids(self) -> set[str]:
        return set(self._provider_ids)

    def force_loaded(self, provider_ids: list[str]) -> None:
        self._provider_ids = set(provider_ids)


class RaisingProxySupervisor(FakeProxySupervisor):
    def reload(self) -> None:
        self.reload_calls += 1
        raise RuntimeError("synthetic reload failure without secret material")


class FakeServer:
    def __init__(self, providers: list[str]):
        self.pool = SimpleNamespace(
            providers=[SimpleNamespace(id=provider_id) for provider_id in providers]
        )
        self.serve_calls = 0
        self.shutdown_calls = 0
        self.close_calls = 0

    def serve_forever(self) -> None:
        self.serve_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def server_close(self) -> None:
        self.close_calls += 1


def _control(tmp_path: Path, proxy_supervisor: FakeProxySupervisor) -> PoolControl:
    return PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=StaticSecretReader(),
        proxy_supervisor=proxy_supervisor,
    )


def _registered_with_key(control: PoolControl) -> None:
    control.register_provider("acme", base_url="https://api.acme.example/v1")
    assert control.enter_key("acme")["configured"] is True


def test_halt_survives_reconstruction(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    assert control.stop_production()["halted"] is True
    rebuilt = _control(tmp_path, proxy_supervisor=FakeProxySupervisor(["acme"]))
    assert rebuilt.production_halted is True
    assert rebuilt.promote("acme")["error"] == "production_halted"


def test_malformed_halt_state_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "production" / "control_state.json"
    state_path.parent.mkdir()
    state_path.write_text('{"production_halted":"yes"}', encoding="utf-8")
    with pytest.raises(PoolControlError, match="invalid_control_state"):
        _control(tmp_path, proxy_supervisor=FakeProxySupervisor([]))


def test_promote_requires_live_proxy_readback(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result == {
        "provider_id": "acme",
        "promoted": False,
        "error": "live_proxy_readback_failed",
    }
    assert control.production_halted is True
    assert supervisor.halt_calls == 1


def test_live_failure_does_not_stop_proxy_when_halt_latch_cannot_persist(
    tmp_path: Path,
) -> None:
    supervisor = FakeProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)

    def fail_persist(reason: str) -> None:
        raise PoolControlError("control_state_write_failed")

    control._persist_halt = fail_persist
    result = control.promote("acme")
    assert result["error"] == "control_state_write_failed"
    assert supervisor.halt_calls == 0


def test_suspend_requires_provider_absent_from_live_proxy(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    assert control.promote("acme")["promoted"] is True
    supervisor.force_loaded(["acme"])
    result = control.suspend("acme")
    assert result["suspended"] is False
    assert result["error"] == "live_proxy_readback_failed"
    assert control.list_production() == []
    assert control.production_halted is True
    assert supervisor.halt_calls == 1


def test_list_production_reads_live_proxy_not_toml(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    assert control.promote("acme")["promoted"] is True
    supervisor.force_loaded([])
    assert control.list_production() == []


def test_proxy_supervisor_reload_replaces_server_and_reads_loaded_ids(
    tmp_path: Path,
) -> None:
    first = FakeServer(["acme"])
    second = FakeServer(["beta"])
    servers = iter([first, second])
    supervisor = FreellmpoolProxySupervisor(
        tmp_path / "providers.toml",
        tmp_path / "config.toml",
        pool_factory=lambda: object(),
        server_factory=lambda pool: next(servers),
    )
    supervisor.reload()
    assert supervisor.running_provider_ids() == {"acme"}
    supervisor.reload()
    assert first.shutdown_calls == 1
    assert first.close_calls == 1
    assert supervisor.running_provider_ids() == {"beta"}
    supervisor.halt()
    assert second.shutdown_calls == 1
    assert second.close_calls == 1
    assert supervisor.running_provider_ids() == set()


def test_proxy_supervisor_rejects_noncanonical_loopback(tmp_path: Path) -> None:
    with pytest.raises(PoolControlError, match="proxy_loopback_host_required"):
        FreellmpoolProxySupervisor(
            tmp_path / "providers.toml",
            tmp_path / "config.toml",
            host="localhost",
        )


def test_promotion_reload_failure_restores_config_and_persists_halt(
    tmp_path: Path,
) -> None:
    supervisor = RaisingProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result["promoted"] is False
    assert result["error"] == "live_proxy_reload_failed"
    assert control.list_production() == []
    assert supervisor.running_provider_ids() == set()
    rebuilt = _control(tmp_path, proxy_supervisor=FakeProxySupervisor([]))
    assert rebuilt.production_halted is True


def test_promotion_readback_failure_halts_and_removes_live_provider(
    tmp_path: Path,
) -> None:
    supervisor = FakeProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    assert result["error"] == "live_proxy_readback_failed"
    assert control.list_production() == []
    assert control.production_halted is True


def test_suspension_reload_failure_never_reports_suspended(tmp_path: Path) -> None:
    supervisor = FakeProxySupervisor(["acme"])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    assert control.promote("acme")["promoted"] is True
    supervisor.fail_reload = True
    result = control.suspend("acme")
    assert result["suspended"] is False
    assert result["error"] == "live_proxy_reload_failed"
    assert control.production_halted is True


def test_failure_artifacts_never_contain_secret(tmp_path: Path, caplog) -> None:
    supervisor = RaisingProxySupervisor([])
    control = _control(tmp_path, proxy_supervisor=supervisor)
    _registered_with_key(control)
    result = control.promote("acme")
    artifacts = json.dumps(result) + caplog.text
    artifacts += control.production_providers_path.read_text(encoding="utf-8")
    artifacts += (control.production_dir / "control_state.json").read_text(
        encoding="utf-8"
    )
    assert "test-provider-key" not in artifacts
