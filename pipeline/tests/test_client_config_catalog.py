"""Phase 4 OFFLINE tests: synthetic loaded pools and loopback fixture servers only."""
import json
import os
import tomllib
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from hunter import client_config as cc
from hunter.cli_imports import _handle_client_config_write
from hunter.pool_api import PoolApiError, PoolControlClient, PoolControlServer
from hunter.pool_control import PoolControl, PoolControlError
from hunter.pool_proxy import FreellmpoolProxySupervisor
from hunter.runtime.models import ActualPoolStatus, ProtocolResult, RuntimeProvider


def catalog():
    return {
        "freellmpool_version": "0.13.0",
        "proxy_base_url": "http://127.0.0.1:8080/v1",
        "proxy_auth_env": "FREELLMPOOL_PROXY_KEY",
        "providers": [{"provider_id": "acme", "model_ids": ["acme/org/model", "acme/model-two"]}],
    }


def runtime():
    return [RuntimeProvider(provider_id=p, actual_pool_status=ActualPoolStatus.PRODUCTION,
                            protocol_result=ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass"))
            for p in ("acme", "ghost")]


def test_configs_use_single_proxy_and_actual_pins(tmp_path):
    paths = cc.write_client_configs(tmp_path / "clients", runtime(), catalog())
    codex = tomllib.loads(paths["codex"].read_text())
    provider = codex["model_providers"]["freellmpool"]
    assert provider == {"name": "FreeLLMPool", "base_url": catalog()["proxy_base_url"],
                        "env_key": "FREELLMPOOL_PROXY_KEY", "wire_api": "responses"}
    assert codex["model"] == "acme/model-two"
    assert {p["model"] for p in codex["profiles"].values()} == set(catalog()["providers"][0]["model_ids"])
    opencode = json.loads(paths["opencode"].read_text())
    entry = opencode["provider"]["freellmpool"]
    assert entry["options"] == {"baseURL": catalog()["proxy_base_url"], "apiKey": "{env:FREELLMPOOL_PROXY_KEY}"}
    assert set(entry["models"]) == set(catalog()["providers"][0]["model_ids"])
    agent = yaml.safe_load(paths["agent"].read_text())
    assert agent["providers"][0]["endpoint"] == catalog()["proxy_base_url"]
    assert agent["providers"][0]["auth_env"] == "FREELLMPOOL_PROXY_KEY"
    assert agent["providers"][0]["models"] == sorted(catalog()["providers"][0]["model_ids"])
    assert all("ghost" not in path.read_text() and "-default" not in path.read_text() for path in paths.values())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("missing", ["HUNTER_POOL_CONTROL_URL", "HUNTER_POOL_CONTROL_TOKEN", "both"])
def test_cli_missing_readback_config_preserves_outputs(tmp_path, monkeypatch, missing, existing):
    for name in ("HUNTER_POOL_CONTROL_URL", "HUNTER_POOL_CONTROL_TOKEN"):
        monkeypatch.setenv(name, "offline-fixture")
    for name in ((missing,) if missing != "both" else ("HUNTER_POOL_CONTROL_URL", "HUNTER_POOL_CONTROL_TOKEN")):
        monkeypatch.delenv(name)
    output = tmp_path / "clients"
    names = ("codex-providers.toml", "opencode.json", "agent-providers.yaml")
    if existing:
        output.mkdir()
        for name in names:
            (output / name).write_text("old")
    args = Namespace(data_dir=str(tmp_path), output_dir=str(output))
    assert _handle_client_config_write(args) != 0
    if existing:
        assert all((output / name).read_text() == "old" for name in names)
    else:
        assert not output.exists()


@pytest.mark.parametrize("bad", [None, {}, [], {"freellmpool_version": "0.12.0"}])
def test_bad_catalog_writes_nothing(tmp_path, bad):
    with pytest.raises(cc.ClientConfigError):
        cc.write_client_configs(tmp_path / "clients", runtime(), bad)
    assert not (tmp_path / "clients").exists()


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(freellmpool_version="0.14.0"),
    lambda c: c.update(proxy_base_url="http://127.0.0.1:8080/v1/acme/responses"),
    lambda c: c.update(proxy_auth_env="UPSTREAM_SECRET"),
    lambda c: c["providers"][0].update(model_ids=["ghost/model"]),
    lambda c: c["providers"][0].update(model_ids=["acme/sk-" + "a" * 20]),
    lambda c: c["providers"][0].update(headers={"anything": "not-public"}),
])
def test_invalid_catalog_preserves_existing_outputs(tmp_path, mutate):
    names = ("codex-providers.toml", "opencode.json", "agent-providers.yaml")
    for name in names:
        (tmp_path / name).write_text("old")
    bad = catalog()
    mutate(bad)
    with pytest.raises(cc.ClientConfigError):
        cc.write_client_configs(tmp_path, runtime(), bad)
    assert [(tmp_path / n).read_text() for n in names] == ["old"] * 3
    assert set(p.name for p in tmp_path.iterdir()) == set(names)


def test_all_serializers_validated_before_output_directory(tmp_path, monkeypatch):
    def fail(*args):
        raise cc.ClientConfigError("fixture_invalid_document")
    monkeypatch.setattr(cc, "generate_agent_config", fail)
    with pytest.raises(cc.ClientConfigError):
        cc.write_client_configs(tmp_path / "clients", runtime(), catalog())
    assert not (tmp_path / "clients").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_replacement_failure_rolls_back_entire_set(tmp_path, monkeypatch, existing):
    names = ("codex-providers.toml", "opencode.json", "agent-providers.yaml")
    if existing:
        for name in names:
            (tmp_path / name).write_text("old")
    original = os.replace
    calls = 0
    def fail_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("offline injected replacement failure")
        return original(src, dst)
    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(OSError):
        cc.write_client_configs(tmp_path, runtime(), catalog())
    assert {p.name for p in tmp_path.iterdir()} == (set(names) if existing else set())
    if existing:
        assert all((tmp_path / n).read_text() == "old" for n in names)


def test_rollback_failure_preserves_backup_and_restores_other_files(tmp_path, monkeypatch):
    names = ("codex-providers.toml", "opencode.json", "agent-providers.yaml")
    for name in names:
        (tmp_path / name).write_text("old-" + name)
    original = os.replace
    calls = 0

    def failing_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls in (3, 4):
            raise OSError("fixture replacement and rollback failure")
        return original(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        cc.write_client_configs(tmp_path, runtime(), catalog())
    assert (tmp_path / names[0]).read_text() == "old-" + names[0]
    assert (tmp_path / names[2]).read_text() == "old-" + names[2]
    backups = list(tmp_path.glob(".opencode.json-*.tmp"))
    assert len(backups) == 1
    assert backups[0].read_text() == "old-opencode.json"


def test_control_catalog_refuses_disk_only_and_halted(tmp_path):
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    with pytest.raises(PoolControlError):
        pc.production_catalog()
    pc._proxy_supervisor = SimpleNamespace(production_catalog=lambda: catalog())
    pc._production_halted = True
    with pytest.raises(PoolControlError):
        pc.production_catalog()


def test_catalog_http_roundtrip_and_cli_failure(tmp_path, monkeypatch):
    pc = PoolControl(tmp_path / "staging", tmp_path / "production",
                     proxy_supervisor=SimpleNamespace(production_catalog=lambda: catalog()))
    server = PoolControlServer(pc, "offline-control-fixture")
    server.start_background()
    try:
        client = PoolControlClient(server.url, "offline-control-fixture")
        assert client.production_catalog() == cc.validate_production_catalog(catalog())
        monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
        monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "offline-control-fixture")
        pc._production_halted = True
        assert _handle_client_config_write(Namespace(data_dir=str(tmp_path), output_dir=str(tmp_path / "clients"))) != 0
        assert not (tmp_path / "clients").exists()
    finally:
        server.shutdown()


def test_supervisor_catalog_reads_loaded_enabled_models_only(tmp_path):
    from freellmpool.models import Model, Provider
    from freellmpool.proxy import _model_ids, _parse_model
    pool = SimpleNamespace(providers=[Provider(id="acme", label="Acme", adapter="openai", base_url="http://unused.invalid",
        models=[Model(name="org/model"), Model(name="disabled", enabled=False)])])
    supervisor = FreellmpoolProxySupervisor(tmp_path / "providers.toml", tmp_path / "config.toml", proxy_key="offline-proxy-fixture")
    supervisor._server = SimpleNamespace(pool=pool, server_address=("127.0.0.1", 8080))
    supervisor._thread = SimpleNamespace(is_alive=lambda: True)
    result = supervisor.production_catalog()
    assert result["providers"] == [{"provider_id": "acme", "model_ids": ["acme/org/model"]}]
    assert result["providers"][0]["model_ids"][0] in _model_ids(pool)
    assert _parse_model("acme/org/model", {"acme"}) == (["acme"], "org/model")
    assert "unused.invalid" not in json.dumps(result)
    supervisor.proxy_key = None
    with pytest.raises(PoolControlError):
        supervisor.production_catalog()


def test_generated_routes_hit_installed_proxy_matcher_without_upstream(tmp_path):
    import http.client
    import threading
    from urllib.parse import urlsplit
    from freellmpool.proxy import serve
    # Invalid/no auth stops before body parsing or any upstream call; 401 proves route match.
    server = serve(SimpleNamespace(flush=lambda: None), host="127.0.0.1", port=0, api_key="offline-proxy-fixture")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = catalog()
        c["proxy_base_url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
        paths = cc.write_client_configs(tmp_path, runtime(), c)
        base = tomllib.loads(paths["codex"].read_text())["model_providers"]["freellmpool"]["base_url"]
        for suffix in ("/responses", "/chat/completions"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
            try:
                conn.request("POST", urlsplit(base).path + suffix, body=b"{}")
                assert conn.getresponse().status == 401
            finally:
                conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
