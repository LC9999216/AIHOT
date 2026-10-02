"""Stage 5 tests: auto-suspend, client config, orchestration, CLI.

Updated for the hardened contracts (review round 2, 六/七):
- auto-suspend persists failure counts and validates intervals;
- client configs use REAL formats (Codex TOML / OpenCode JSON / Agent YAML),
  are re-parsed after generation, honor pool readback, and never contain
  credential material.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hunter.auto_suspend import AutoSuspendPolicy, AutoSuspendTracker, run_auto_suspend_cycle
from hunter.client_config import (
    ClientConfigError,
    generate_agent_config,
    generate_codex_config,
    generate_opencode_config,
    write_client_configs,
)
from hunter.pool_control import PoolControl
from hunter.runtime.models import (
    ActualPoolStatus,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
)
from hunter.runtime.store import RuntimeStore


# =============================================================================
# Auto-suspend policy validation (review 六.5)
# =============================================================================


def test_policy_rejects_interval_below_one_second() -> None:
    with pytest.raises(ValueError):
        AutoSuspendPolicy(check_interval_seconds=0)
    with pytest.raises(ValueError):
        AutoSuspendPolicy(check_interval_seconds=-5)


def test_policy_accepts_valid_intervals() -> None:
    AutoSuspendPolicy(check_interval_seconds=1)
    AutoSuspendPolicy(check_interval_seconds=300)


def test_tracker_records_failure() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy)
    tracker.record_outcome("acme", healthy=False)
    assert tracker.consecutive_failures("acme") == 1
    assert tracker.should_suspend("acme") is False


def test_tracker_resets_on_healthy() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy)
    tracker.record_outcome("acme", healthy=False)
    tracker.record_outcome("acme", healthy=False)
    tracker.record_outcome("acme", healthy=True)
    assert tracker.consecutive_failures("acme") == 0


def test_tracker_triggers_suspend() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy)
    for _ in range(3):
        tracker.record_outcome("acme", healthy=False)
    assert tracker.should_suspend("acme") is True


def test_tracker_excluded_providers() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=2, excluded_providers={"acme"})
    tracker = AutoSuspendTracker(policy)
    for _ in range(5):
        tracker.record_outcome("acme", healthy=False)
    assert tracker.should_suspend("acme") is False


def test_tracker_reset_clears() -> None:
    policy = AutoSuspendPolicy(max_consecutive_failures=2)
    tracker = AutoSuspendTracker(policy)
    tracker.record_outcome("acme", healthy=False)
    tracker.reset("acme")
    assert tracker.consecutive_failures("acme") == 0


# =============================================================================
# Auto-suspend persistence (review 六.5)
# =============================================================================


def test_failure_counts_persist_across_restart(tmp_path: Path) -> None:
    state = tmp_path / "auto_suspend_state.json"
    policy = AutoSuspendPolicy(max_consecutive_failures=3)
    tracker = AutoSuspendTracker(policy, state_path=state)
    tracker.record_outcome("acme", healthy=False)
    tracker.record_outcome("acme", healthy=False)

    # "restart": a fresh tracker on the same state file keeps the counts
    tracker2 = AutoSuspendTracker(policy, state_path=state)
    assert tracker2.consecutive_failures("acme") == 2


def test_healthy_outcome_persists_clear(tmp_path: Path) -> None:
    state = tmp_path / "auto_suspend_state.json"
    policy = AutoSuspendPolicy()
    tracker = AutoSuspendTracker(policy, state_path=state)
    tracker.record_outcome("acme", healthy=False)
    tracker.record_outcome("acme", healthy=True)
    assert AutoSuspendTracker(policy, state_path=state).consecutive_failures("acme") == 0


def test_corrupt_state_file_fails_safe(tmp_path: Path) -> None:
    state = tmp_path / "auto_suspend_state.json"
    state.write_text("{not json", encoding="utf-8")
    tracker = AutoSuspendTracker(AutoSuspendPolicy(), state_path=state)
    assert tracker.consecutive_failures("acme") == 0  # starts empty, no crash


def test_auto_suspend_cycle_does_not_affect_non_production(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.json", tmp_path / "history.jsonl")
    pc = PoolControl(tmp_path / "staging", tmp_path / "production")
    rp = RuntimeProvider(
        provider_id="acme",
        credential_status=CredentialStatus.CONFIGURED,
        health_status=HealthStatus.HEALTHY,
        actual_pool_status=ActualPoolStatus.STAGING,  # not production
        approval_status=ApprovalStatus.APPROVED,
    )
    store.upsert(rp, reason="setup")
    policy = AutoSuspendPolicy(max_consecutive_failures=2)
    tracker = AutoSuspendTracker(policy, state_path=tmp_path / "state.json")
    results = run_auto_suspend_cycle(store, pc, tracker)
    assert all(r.success for r in results)


# =============================================================================
# Client config generation (review 七)
# =============================================================================


def _production_rp(provider_id: str = "acme", **kw) -> RuntimeProvider:
    return RuntimeProvider(
        provider_id=provider_id,
        provider_name=kw.get("provider_name", f"{provider_id.capitalize()} AI"),
        credential_status=kw.get("credential_status", CredentialStatus.CONFIGURED),
        health_status=kw.get("health_status", HealthStatus.HEALTHY),
        protocol_result=kw.get(
            "protocol_result",
            ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass"),
        ),
        actual_pool_status=kw.get("actual_pool_status", ActualPoolStatus.PRODUCTION),
        expected_pool_status=kw.get("expected_pool_status", ExpectedPoolStatus.PRODUCTION),
        approval_status=kw.get("approval_status", ApprovalStatus.APPROVED),
    )


def _catalog(*ids):
    """Explicit OFFLINE loaded-catalog fixture; never a runtime-only fallback."""
    return {"freellmpool_version": "0.13.0", "proxy_base_url": "http://127.0.0.1:8080/v1",
            "proxy_auth_env": "FREELLMPOOL_PROXY_KEY",
            "providers": [{"provider_id": pid, "model_ids": [f"{pid}/fixture-model"]} for pid in ids]}


# --- Codex (TOML, responses wire API) ---------------------------------------


def test_codex_config_real_toml_format() -> None:
    text = generate_codex_config([_production_rp("acme")], _catalog("acme"))
    import tomllib

    parsed = tomllib.loads(text)
    entry = parsed["model_providers"]["freellmpool"]
    assert entry["wire_api"] == "responses"
    assert entry["base_url"] == "http://127.0.0.1:8080/v1"
    assert entry["env_key"]  # env var NAME, not a value
    assert not entry["env_key"].startswith("sk-")


def test_codex_requires_responses_streaming_tools() -> None:
    """Plan §7.3: Codex admission requires Responses + Streaming + Tools."""
    pr = ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="fail")
    text = generate_codex_config([_production_rp("acme", protocol_result=pr)], _catalog("acme"))
    import tomllib

    assert tomllib.loads(text).get("model_providers", {}) == {}


def test_codex_provider_name_injection_is_escaped() -> None:
    evil = 'Acme"]\n[injected]\nwire_api = "chat"'
    text = generate_codex_config([_production_rp("acme", provider_name=evil)], _catalog("acme"))
    import tomllib

    parsed = tomllib.loads(text)
    assert list(parsed["model_providers"].keys()) == ["freellmpool"]
    assert "injected" not in parsed


# --- OpenCode (JSON) ----------------------------------------------------------


def test_opencode_config_real_json_format() -> None:
    text = generate_opencode_config([_production_rp("acme"), _production_rp("beta")], _catalog("acme", "beta"))
    parsed = json.loads(text)
    assert set(parsed["provider"]["freellmpool"]["models"]) == {"acme/fixture-model", "beta/fixture-model"}
    assert parsed["provider"]["freellmpool"]["options"]["baseURL"] == "http://127.0.0.1:8080/v1"


def test_opencode_config_skips_non_production() -> None:
    staging = RuntimeProvider(
        provider_id="staging-only", actual_pool_status=ActualPoolStatus.STAGING
    )
    text = generate_opencode_config([_production_rp("prod"), staging], _catalog("prod", "staging-only"))
    parsed = json.loads(text)
    assert set(parsed["provider"]["freellmpool"]["models"]) == {"prod/fixture-model"}
    assert "staging-only" not in text


def test_opencode_requires_chat() -> None:
    pr = ProtocolResult(chat="fail", responses="pass", streaming="pass", tools="pass")
    text = generate_opencode_config([_production_rp("acme", protocol_result=pr)], _catalog("acme"))
    assert "acme" not in json.loads(text)["provider"]


def test_opencode_respects_pool_readback() -> None:
    """Only providers confirmed in the production pool readback are emitted."""
    text = generate_opencode_config(
        [_production_rp("acme"), _production_rp("ghost")],
        production_catalog=_catalog("acme"),
    )
    models = json.loads(text)["provider"]["freellmpool"]["models"]
    assert set(models) == {"acme/fixture-model"}
    assert "ghost" not in text


def test_codex_respects_pool_readback() -> None:
    import tomllib

    text = generate_codex_config(
        [_production_rp("acme"), _production_rp("ghost")],
        production_catalog=_catalog(),
    )
    assert tomllib.loads(text).get("model_providers", {}) == {}


# --- Agent (YAML via safe_dump) -------------------------------------------------


def test_agent_config_yaml_round_trips() -> None:
    text = generate_agent_config([_production_rp("acme")], _catalog("acme"))
    parsed = yaml.safe_load(text)
    entry = parsed["providers"][0]
    assert entry["endpoint"] == "http://127.0.0.1:8080/v1"
    assert "auth_env" in entry  # NAME only


def test_agent_chat_admission_does_not_invent_model_capabilities() -> None:
    pr = ProtocolResult(chat="pass", responses="pass", streaming="unchecked", tools="pass")
    text = generate_agent_config([_production_rp("acme", protocol_result=pr)], _catalog("acme"))
    entry = yaml.safe_load(text)["providers"][0]
    assert entry["models"] == ["acme/fixture-model"]
    assert "capabilities" not in entry


def test_agent_name_with_yaml_metacharacters_is_safe() -> None:
    evil = 'Acme: [inject]\n  key: value'
    text = generate_agent_config([_production_rp("acme", provider_name=evil)], _catalog("acme"))
    parsed = yaml.safe_load(text)
    assert parsed["providers"][0]["name"] == "FreeLLMPool"
    assert evil not in text  # upstream display strings are not copied into proxy configuration


# --- secrets & writer -----------------------------------------------------------


def test_config_no_secret_key_in_output() -> None:
    for generator in (generate_opencode_config, generate_codex_config, generate_agent_config):
        text = generator([_production_rp("acme")], _catalog("acme"))
        assert "api_key" not in text
        assert "sk-" not in text
        assert "Bearer " not in text


def test_write_client_configs_creates_validated_files(tmp_path: Path) -> None:
    outputs = write_client_configs(
        tmp_path / "clients", [_production_rp("acme")], production_catalog=_catalog("acme")
    )
    assert outputs["codex"].is_file()
    assert outputs["opencode"].is_file()
    assert outputs["agent"].is_file()
    json.loads(outputs["opencode"].read_text(encoding="utf-8"))
    yaml.safe_load(outputs["agent"].read_text(encoding="utf-8"))


def test_empty_list_produces_valid_configs() -> None:
    assert generate_codex_config([], _catalog()).strip() != ""
    assert generate_opencode_config([], _catalog()).strip() != ""
    assert generate_agent_config([], _catalog()).strip() != ""
