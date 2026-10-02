"""Stage 2 runner + CLI end-to-end offline tests (review round 2, 六.8).

Covers the plan §8 run order with fully injected fakes:
- lock → skip when a previous run is active;
- reconcile registry → suspend invalid production providers FIRST;
- import new FREE_CONFIRMED providers;
- health/protocol checks through the injected pool control;
- eligibility recompute; promote only currently-approved providers;
- pool-vs-runtime verification (split detection halts production);
- `hunter pool approve` revision guard; `hunter runtime status` sanitized
  output; `hunter run-stage-two` end-to-end via main().
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hunter.auto_suspend import AutoSuspendPolicy
from hunter.cli import main as cli_main
from hunter.evidence.models import Evidence
from hunter.evidence.store import EvidenceStore
from hunter.pool_api import PoolControlServer
from hunter.pool_control import PoolControl
from hunter.registry.schema import FreeOffer, Provider, ProviderStatus
from hunter.registry.store import ProviderRegistry
from hunter.runtime.locks import LockHeldError, ProcessFileLock
from hunter.runtime.models import (
    ActualPoolStatus,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
)
from hunter.runtime.stage2 import Stage2Runner, Stage2Summary, approve_provider, runtime_status
from hunter.runtime.outbox import OutboxStore
from hunter.runtime.store import RuntimeStore

AS_OF = datetime.fromisoformat("2026-09-12T00:00:00+00:00")
SECRET = "sk-test-SECRET-0123456789"


class StaticSecretReader:
    def __call__(self, prompt: str) -> str:
        return SECRET


class FakeProbeRunner:
    def __init__(self, results=None):
        self.results = results or {}

    def run(self, provider_record, key_env, config_file, features=("chat",), timeout=20.0):
        return {
            feature: dict(
                self.results.get(
                    feature, {"status": "pass", "classification": "verified"}
                )
            )
            for feature in features
        }


def _registry_provider(pid="acme", **kw) -> Provider:
    return Provider(
        id=pid,
        provider=kw.get("provider", f"{pid.capitalize()} AI"),
        canonical_domain=kw.get("domain", f"{pid}.ai"),
        status=kw.get("status", ProviderStatus.FREE_CONFIRMED),
        evidence_ids=kw.get("evidence_ids", [f"ev-{pid}"]),
        verification_confidence=kw.get("verification_confidence", 85),
        score_metadata=kw.get("score_metadata", {"score": 90, "as_of": AS_OF.isoformat()}),
        free_score=kw.get("free_score", 90),
        free_offer=kw.get("free_offer", FreeOffer(offer_status="active")),
    )


def _seed(tmp_path: Path, providers=None, **pool_kw):
    """Build data dir with registry, evidence, runtime, pool control.

    Pass ``hunter_root=None`` to ``pool_kw`` for tests that need the pool
    directory under the repo (isolated tests use a path outside the repo).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    registry = ProviderRegistry(data_dir / "providers.json", data_dir / "history.jsonl")
    evidence_store = EvidenceStore(data_dir / "evidence.json")
    for provider in providers or [_registry_provider()]:
        registry.upsert_provider(provider, reason="seed", source_metadata={"t": True})
        evidence_store.upsert(
            Evidence(
                evidence_id=f"ev-{provider.id}",
                provider_id=provider.id,
                url=f"https://{provider.canonical_domain}/pricing",
                officiality="OFFICIAL",
                source_type="pricing",
                content_fingerprint=f"fp-{provider.id}",
            )
        )
    pool = PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=StaticSecretReader(),
        probe_runner=FakeProbeRunner(),
        **pool_kw,
    )
    return data_dir, registry, evidence_store, pool


def _approved_runtime_record(pid="acme", production=False) -> RuntimeProvider:
    return RuntimeProvider(
        provider_id=pid,
        provider_name=f"{pid.capitalize()} AI",
        evidence_status="FREE_CONFIRMED",
        credential_status=CredentialStatus.CONFIGURED,
        health_status=HealthStatus.HEALTHY,
        protocol_result=ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass"),
        expected_pool_status=(
            ExpectedPoolStatus.PRODUCTION if production else ExpectedPoolStatus.STAGING
        ),
        actual_pool_status=(
            ActualPoolStatus.PRODUCTION if production else ActualPoolStatus.STAGING
        ),
        approval_status=ApprovalStatus.APPROVED,
    )


def _drive_to_approval(data_dir, pool, pid="acme") -> RuntimeProvider:
    """Register key, health, protocol, then approve via the worker."""
    pool.register_provider(pid, base_url=f"https://{pid}.ai/v1")
    pool.enter_key(pid)
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    store.upsert(_approved_runtime_record(pid), reason="setup")
    from hunter.runtime.orchestrator import (
        run_health_check,
        run_protocol_check,
        request_approval,
    )
    registry = ProviderRegistry(data_dir / "providers.json", data_dir / "history.jsonl")
    evidence_store = EvidenceStore(data_dir / "evidence.json")
    run_health_check(pid, store, pool)
    run_protocol_check(pid, store, pool)
    result = request_approval(pid, store, registry, evidence_store)
    assert result.success, result.error
    return store.get_provider(pid)


# =============================================================================
# Runner order tests
# =============================================================================


def test_runner_imports_new_free_confirmed(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.skipped_locked is False
    assert summary.imported == ["acme"]
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    assert rp is not None
    assert rp.credential_status == CredentialStatus.NOT_CONFIGURED
    assert rp.actual_pool_status == ActualPoolStatus.UNKNOWN


def test_runner_promotes_approved_provider(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.promoted == ["acme"]
    assert pool.list_production() == ["acme"]


def test_runner_suspends_registry_downgrade_first(tmp_path: Path) -> None:
    """Production provider whose registry status degrades is suspended
    BEFORE any import/promotion happens (plan §8 step 4)."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.PRODUCTION}),
        changed_fields=["actual_pool_status"],
        reason="in production",
    )
    # registry downgrades the provider to UNCERTAIN
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.UNCERTAIN),
        reason="evidence weakened",
        source_metadata={"t": True},
    )
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.suspended == ["acme"]
    assert pool.list_production() == []
    # and it is NOT re-promoted (approval binding now invalid)
    assert summary.promoted == []


def test_runner_skips_when_locked(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    # hold a separate lock so the runner cannot acquire
    held_lock = ProcessFileLock(data_dir / ".stage2.lock")
    held_lock.acquire()
    try:
        runner = Stage2Runner(
            data_dir=data_dir,
            pool_control=pool,
            registry=registry,
            evidence_store=evidence_store,
            stage_lock=held_lock,  # already held by this process
        )
        summary = runner.run()
        assert summary.skipped_locked is True
    finally:
        held_lock.release()


def test_runner_detects_pool_runtime_split(tmp_path: Path) -> None:
    """A provider serving in the pool but SUSPENDED in runtime → split → halt."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")  # pool now serves acme
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.SUSPENDED}),
        changed_fields=["actual_pool_status"],
        reason="suspended in runtime only",
    )
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert "acme" in summary.pool_runtime_split
    assert summary.production_halted is True


def test_runner_suspend_failure_halts_production(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.PRODUCTION}),
        changed_fields=["actual_pool_status"],
        reason="in production",
    )
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.NOT_FREE),
        reason="offer withdrawn",
        source_metadata={"t": True},
    )

    def failing_suspend(provider_id):
        return {"suspended": False, "error": "production_write_failed"}

    pool.suspend = failing_suspend  # type: ignore[assignment]
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=pool, registry=registry, evidence_store=evidence_store
    )
    summary = runner.run()
    assert summary.suspend_failed == ["acme"]
    assert summary.production_halted is True


# =============================================================================
# approve worker revision guard
# =============================================================================


def test_approve_worker_revision_guard(tmp_path: Path) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    provider = registry.get_provider("acme")
    wrong = approve_provider(
        "acme", expected_revision=provider.revision + 5, data_dir=data_dir
    )
    assert wrong["ok"] is False
    assert "registry_revision_mismatch" in wrong["error"]
    right = approve_provider(
        "acme", expected_revision=provider.revision, data_dir=data_dir
    )
    assert right["ok"] is True


def test_approve_worker_missing_provider(tmp_path: Path) -> None:
    data_dir, _, _, _ = _seed(tmp_path)
    result = approve_provider("ghost", expected_revision=1, data_dir=data_dir)
    assert result["ok"] is False


# =============================================================================
# runtime status worker
# =============================================================================


def test_runtime_status_sanitized(tmp_path: Path) -> None:
    data_dir, _, _, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    rows = runtime_status(data_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider_id"] == "acme"
    dumped = json.dumps(rows)
    assert SECRET not in dumped
    assert "api_key" not in dumped


# =============================================================================
# CLI end-to-end (offline, monkeypatched pool root)
# =============================================================================


def test_cli_runtime_status_offline(tmp_path: Path, capsys, monkeypatch) -> None:
    data_dir, _, _, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    code = cli_main(["runtime", "status", "--data-dir", str(data_dir)])
    assert code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed[0]["provider_id"] == "acme"
    assert "sk-" not in out


def test_cli_pool_approve_offline(tmp_path: Path, capsys) -> None:
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    provider = registry.get_provider("acme")
    code = cli_main(
        [
            "pool", "approve",
            "--provider-id", "acme",
            "--expected-revision", str(provider.revision),
            "--data-dir", str(data_dir),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is True


def test_cli_pool_approve_rejects_wrong_revision(tmp_path: Path, capsys) -> None:
    data_dir, _, _, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    code = cli_main(
        [
            "pool", "approve",
            "--provider-id", "acme",
            "--expected-revision", "999",
            "--data-dir", str(data_dir),
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "registry_revision_mismatch" in out


def test_cli_run_stage_two_offline(
    tmp_path: Path,
    outside_repo_tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """Full CLI e2e: approved provider gets promoted through run-stage-two."""
    data_dir, registry, evidence_store, pool = _seed(
        outside_repo_tmp_path / "data", hunter_root=None
    )
    _drive_to_approval(data_dir, pool)
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["run-stage-two", "--data-dir", str(data_dir)])
        out = capsys.readouterr().out
        summary = json.loads(out)
        assert code == 0
        assert summary["providers_read"] >= 1
        assert "acme" in summary["promoted"]
    finally:
        server.shutdown()


def test_run_stage_two_cli_uses_pool_client_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, url: str, token: str):
            captured["url"] = url
            captured["token"] = token

    class FakeRunner:
        def __init__(self, *, data_dir: Path, pool_control: object, notification_adapter=None, **kw):
            captured["data_dir"] = data_dir
            captured["pool_control"] = pool_control
            captured["notification_adapter"] = notification_adapter

        def run(self):
            return SimpleNamespace(
                to_dict=lambda: {"skipped_locked": False},
                skipped_locked=False,
                suspend_failed=[],
                pool_runtime_split=[],
            )

    monkeypatch.setattr("hunter.pool_api.PoolControlClient", FakeClient)
    monkeypatch.setattr("hunter.runtime.stage2.Stage2Runner", FakeRunner)
    assert cli_main(["run-stage-two", "--data-dir", str(tmp_path)]) == 0
    assert captured["url"] == "http://127.0.0.1:8091"
    assert isinstance(captured["pool_control"], FakeClient)


# =============================================================================
# Credential reconciliation (Problem 1.4.2B)
# =============================================================================


class FakePoolStatus:
    """Simulates PoolControlClient.status() responses."""

    def __init__(self, **fields):
        self._fields = fields

    def __call__(self):
        return self


def test_credential_reconciliation_maps_key_configured(tmp_path: Path) -> None:
    """key_configured true -> CONFIGURED; false -> NOT_CONFIGURED."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    store.upsert(
        RuntimeProvider(
            provider_id="acme",
            credential_status=CredentialStatus.NOT_CONFIGURED,
        ),
        reason="setup",
    )
    # Fake pool_control that returns configured=true, in_staging=true
    class FakeControl:
        def status(self, pid: str):
            return {"key_configured": True, "in_staging": True, "in_production": False}
        def list_production(self):
            return []
        def probe(self, *a, **kw):
            return {}
        def promote(self, *a):
            return {"promoted": False, "error": "no-op"}
        def suspend(self, *a):
            return {"suspended": False, "error": "no-op"}
        def stop_production(self):
            return {}

    runner = Stage2Runner(
        data_dir=data_dir,
        pool_control=FakeControl(),
        registry=registry,
        evidence_store=evidence_store,
    )
    runner._reconcile_credential_state(store, Stage2Summary())
    rp = store.get_provider("acme")
    assert rp is not None
    assert rp.credential_status == CredentialStatus.CONFIGURED


def test_credential_reconciliation_maps_pool_state(tmp_path: Path) -> None:
    """in_staging -> STAGING, in_production -> PRODUCTION, none -> UNKNOWN."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    store.upsert(RuntimeProvider(provider_id="acme"), reason="setup")

    class FakeControl:
        def __init__(self, in_staging, in_production):
            self._s = in_staging
            self._p = in_production
        def status(self, pid: str):
            return {"key_configured": False, "in_staging": self._s, "in_production": self._p}
        def list_production(self):
            return []
        def probe(self, *a, **kw):
            return {}
        def promote(self, *a):
            return {"promoted": False, "error": "no-op"}
        def suspend(self, *a):
            return {"suspended": False, "error": "no-op"}
        def stop_production(self):
            return {}

    # in_production=true
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=FakeControl(False, True), registry=registry, evidence_store=evidence_store
    )
    runner._reconcile_credential_state(store, Stage2Summary())
    assert store.get_provider("acme").actual_pool_status == ActualPoolStatus.PRODUCTION

    # in_staging=true (not production)
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=FakeControl(True, False), registry=registry, evidence_store=evidence_store
    )
    runner._reconcile_credential_state(store, Stage2Summary())
    assert store.get_provider("acme").actual_pool_status == ActualPoolStatus.STAGING

    # neither
    runner = Stage2Runner(
        data_dir=data_dir, pool_control=FakeControl(False, False), registry=registry, evidence_store=evidence_store
    )
    runner._reconcile_credential_state(store, Stage2Summary())
    assert store.get_provider("acme").actual_pool_status == ActualPoolStatus.UNKNOWN


def test_credential_reconciliation_unreachable_fails_closed(tmp_path: Path) -> None:
    """An unreachable PoolControl must not mark anything configured."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path, hunter_root=None)
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    store.upsert(
        RuntimeProvider(
            provider_id="acme",
            credential_status=CredentialStatus.NOT_CONFIGURED,
        ),
        reason="setup",
    )

    class FaultyControl:
        def status(self, pid: str):
            raise ConnectionError("refused")
        def list_production(self):
            return []
        def probe(self, *a, **kw):
            return {}
        def promote(self, *a):
            return {"promoted": False, "error": "no-op"}
        def suspend(self, *a):
            return {"suspended": False, "error": "no-op"}
        def stop_production(self):
            return {}

    runner = Stage2Runner(
        data_dir=data_dir,
        pool_control=FaultyControl(),
        registry=registry,
        evidence_store=evidence_store,
    )
    runner._reconcile_credential_state(store, Stage2Summary())
    rp = store.get_provider("acme")
    assert rp is not None
    assert rp.credential_status == CredentialStatus.NOT_CONFIGURED


def test_credential_reconciliation_reconciles_before_suspend_first(
    tmp_path: Path, outside_repo_tmp_path: Path
) -> None:
    """Runner calls _reconcile_credential_state before suspend-first."""
    data_dir, registry, evidence_store, pool = _seed(outside_repo_tmp_path / "data", hunter_root=None)
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    # Set actual_pool=PRODUCTION but force credential back to NOT_CONFIGURED
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "credential_status": CredentialStatus.NOT_CONFIGURED, "actual_pool_status": ActualPoolStatus.PRODUCTION}),
        changed_fields=["credential_status", "actual_pool_status"],
        reason="simulate stale",
    )
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.NOT_FREE),
        reason="weakened",
        source_metadata={"t": True},
    )
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    try:
        runner = Stage2Runner(
            data_dir=data_dir,
            pool_control=pool,
            registry=registry,
            evidence_store=evidence_store,
        )
        summary = runner.run()
        # The credential should have been reconciled from pool before suspend.
        assert summary.providers_read >= 1
    finally:
        server.shutdown()


def test_suspend_first_uses_reconciled_pool_state_same_round(
    tmp_path: Path, outside_repo_tmp_path: Path
) -> None:
    """A provider the Pool reports as live PRODUCTION is suspended in the SAME
    run, even when the persisted Runtime snapshot still says UNKNOWN.

    Regression: _suspend_invalid_first must operate on the RECONCILED state
    (re-read from RuntimeStore after _reconcile_credential_state), not on the
    stale pre-reconcile snapshot list.
    """
    from hunter.pool_api import PoolControlClient, PoolControlServer

    data_dir, registry, evidence_store, pool = _seed(
        outside_repo_tmp_path / "data", hunter_root=None
    )
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    assert pool.list_production() == ["acme"]
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    # Stale snapshot: runtime says UNKNOWN although the pool is serving it.
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.UNKNOWN}),
        changed_fields=["actual_pool_status"],
        reason="simulate stale snapshot",
    )
    # Registry downgrades to NOT_FREE — provider must leave production.
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.NOT_FREE),
        reason="offer withdrawn",
        source_metadata={"t": True},
    )
    # Real server + client (plan stage-1 requirement: no fake Client).
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    try:
        client = PoolControlClient(server.url, "test-control-token")
        runner = Stage2Runner(
            data_dir=data_dir,
            pool_control=client,
            registry=registry,
            evidence_store=evidence_store,
        )
        summary = runner.run()
    finally:
        server.shutdown()
    assert summary.suspended == ["acme"]
    assert pool.list_production() == []
    # Runtime persisted non-production state
    persisted = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    ).get_provider("acme")
    assert persisted.actual_pool_status != ActualPoolStatus.PRODUCTION
    # No re-promotion in the same run
    assert summary.promoted == []


@pytest.mark.parametrize("downgrade", [ProviderStatus.UNCERTAIN, ProviderStatus.EXPIRED])
def test_suspend_first_same_round_parametrized_statuses(
    tmp_path: Path, outside_repo_tmp_path: Path, downgrade: ProviderStatus
) -> None:
    """UNCERTAIN / EXPIRED downgrades also suspend in the same round."""
    from hunter.pool_api import PoolControlClient, PoolControlServer

    data_dir, registry, evidence_store, pool = _seed(
        outside_repo_tmp_path / "data", hunter_root=None
    )
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.UNKNOWN}),
        changed_fields=["actual_pool_status"],
        reason="simulate stale snapshot",
    )
    registry.upsert_provider(
        _registry_provider(status=downgrade),
        reason="downgrade",
        source_metadata={"t": True},
    )
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    try:
        client = PoolControlClient(server.url, "test-control-token")
        runner = Stage2Runner(
            data_dir=data_dir,
            pool_control=client,
            registry=registry,
            evidence_store=evidence_store,
        )
        summary = runner.run()
    finally:
        server.shutdown()
    assert summary.suspended == ["acme"]
    assert pool.list_production() == []


# =============================================================================
# HIGH remediation: suspension containment must not depend on outbox lock
# =============================================================================


def test_suspend_survives_outbox_lock_contention(
    tmp_path: Path, outside_repo_tmp_path: Path, monkeypatch
) -> None:
    """A held .outbox.lock must never prevent same-round suspension.

    Regression: _suspend_invalid_first evaluated _open_outbox() as a call
    argument BEFORE suspend_from_production ran, so LockHeldError aborted the
    run and the invalid provider kept serving in production.
    """
    from hunter.runtime.locks import LockHeldError

    data_dir, registry, evidence_store, pool = _seed(
        outside_repo_tmp_path / "data", hunter_root=None
    )
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.PRODUCTION}),
        changed_fields=["actual_pool_status"],
        reason="in production",
    )
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.NOT_FREE),
        reason="offer withdrawn",
        source_metadata={"t": True},
    )

    def locked_outbox(*args, **kwargs):
        raise LockHeldError("another process holds the outbox lock")

    monkeypatch.setattr("hunter.runtime.stage2.OutboxStore", locked_outbox)
    runner = Stage2Runner(
        data_dir=data_dir,
        pool_control=pool,
        registry=registry,
        evidence_store=evidence_store,
    )
    summary = runner.run()
    # Containment happened despite the outbox contention
    assert summary.suspended == ["acme"]
    assert pool.list_production() == []
    assert any("outbox" in note.lower() for note in summary.notes)
    intent_path = data_dir / "suspension_alert_intents.json"
    assert "acme" in json.loads(intent_path.read_text(encoding="utf-8"))
    monkeypatch.undo()
    runner.run()
    assert json.loads(intent_path.read_text(encoding="utf-8")) == {}
    outbox = OutboxStore(data_dir / "notification_outbox.json")
    try:
        assert any(m.event_type == "PRODUCTION_BLOCKED" for m in outbox.pending())
    finally:
        outbox.close()


def test_second_reconciliation_cannot_restore_invalid_production(
    tmp_path: Path, outside_repo_tmp_path: Path
) -> None:
    """If the first reconciliation fails and the retry later reports the
    provider in production, the same round must still suspend it — the
    invalid provider must not survive because checks ran before the retry.

    Regression (pre-existing): first status raise left the provider UNKNOWN
    (skipped by suspend-first); the second reconciliation then wrote
    PRODUCTION for a NOT_FREE provider and final verification saw matching
    states, so nothing halted.
    """
    data_dir, registry, evidence_store, pool = _seed(
        outside_repo_tmp_path / "data", hunter_root=None
    )
    _drive_to_approval(data_dir, pool)
    pool.promote("acme")
    store = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    )
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "actual_pool_status": ActualPoolStatus.UNKNOWN}),
        changed_fields=["actual_pool_status"],
        reason="stale snapshot",
    )
    registry.upsert_provider(
        _registry_provider(status=ProviderStatus.NOT_FREE),
        reason="offer withdrawn",
        source_metadata={"t": True},
    )

    real_status = pool.status
    calls = {"n": 0}

    def flaky_status(provider_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient first reconciliation failure")
        from dataclasses import asdict
        return asdict(real_status(provider_id))

    pool.status = flaky_status  # type: ignore[method-assign]
    runner = Stage2Runner(
        data_dir=data_dir,
        pool_control=pool,
        registry=registry,
        evidence_store=evidence_store,
    )
    summary = runner.run()
    # The provider must not survive the round in production
    assert pool.list_production() == []
    assert summary.suspended == ["acme"]
    persisted = RuntimeStore(
        data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl"
    ).get_provider("acme")
    assert persisted.actual_pool_status != ActualPoolStatus.PRODUCTION


def test_runtime_status_has_distinct_revisions(tmp_path: Path, capsys) -> None:
    """runtime status returns runtime_revision and registry_revision separately."""
    data_dir, registry, evidence_store, pool = _seed(tmp_path)
    _drive_to_approval(data_dir, pool)
    rows = runtime_status(data_dir)
    assert len(rows) == 1
    row = rows[0]
    assert "runtime_revision" in row
    assert "registry_revision" in row
    # runtime revision should be >= 1 (has been upserted multiple times)
    assert row["runtime_revision"] >= 1
    # registry revision should be >= 1
    assert row["registry_revision"] >= 1


# =============================================================================
# Safe operator commands (Problem 1.4.2C)
# =============================================================================


def test_cli_pool_control_status_offline(tmp_path: Path, capsys, monkeypatch) -> None:
    """hunter pool control-status returns sanitized pool state."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    from hunter.pool_api import PoolControlServer
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "control-status"])
        out = capsys.readouterr().out
        assert code == 0
        import json
        parsed = json.loads(out)
        assert "production_ids" in parsed
        assert "production_halted" in parsed
        assert "sk-" not in out
    finally:
        server.shutdown()


def test_cli_pool_suspend_via_client(tmp_path: Path, capsys, monkeypatch) -> None:
    """hunter pool suspend calls PoolControlClient.suspend()."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    from hunter.pool_api import PoolControlServer
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "suspend", "--provider-id", "ghost", "--confirm", "SUSPEND"])
        out = capsys.readouterr().out
        # ghost is not in production, so suspend is idempotent (returns suspended=true)
        assert code == 0
        assert "sk-" not in out
    finally:
        server.shutdown()


def test_cli_pool_stop_via_client(tmp_path: Path, capsys, monkeypatch) -> None:
    """hunter pool stop calls PoolControlClient.stop_production()."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    from hunter.pool_api import PoolControlServer
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "stop", "--confirm", "STOP"])
        out = capsys.readouterr().out
        assert code == 0
        assert "halted" in out or "true" in out
        assert "sk-" not in out
    finally:
        server.shutdown()


def test_cli_pool_control_status_requires_connection(tmp_path: Path) -> None:
    """control-status fails closed without pool control env vars."""
    with pytest.raises(RuntimeError, match="pool control URL and token are required"):
        cli_main(["pool", "control-status", "--data-dir", str(tmp_path)])


def test_cli_pool_suspend_refuses_without_confirm(tmp_path: Path) -> None:
    """hunter pool suspend requires --confirm SUSPEND."""
    import sys
    from hunter.cli import main as cli_entry
    # argparse exits on missing required argument, so catch sys.exit
    with pytest.raises((SystemExit, RuntimeError)):
        cli_main(["pool", "suspend", "--provider-id", "acme"])


# =============================================================================
# Operator confirm word + exit code enforcement (remediation plan stage 2)
# =============================================================================


class _CallCountingServer:
    """Wraps PoolControlServer handlers to count client-visible calls."""

    def __init__(self, pool, bearer_token: str):
        from hunter.pool_api import PoolControlServer
        self.suspend_calls = 0
        self.stop_calls = 0
        server = self

        original_suspend = pool.suspend
        original_stop = pool.stop_production

        def counting_suspend(provider_id):
            server.suspend_calls += 1
            return original_suspend(provider_id)

        def counting_stop(reason="manual_stop"):
            server.stop_calls += 1
            return original_stop(reason)

        pool.suspend = counting_suspend  # type: ignore[method-assign]
        pool.stop_production = counting_stop  # type: ignore[method-assign]
        self.server = PoolControlServer(pool, bearer_token=bearer_token)

    @property
    def url(self):
        return self.server.url

    def start_background(self):
        self.server.start_background()

    def shutdown(self):
        self.server.shutdown()


def test_pool_suspend_wrong_confirm_word_makes_no_api_call(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """--confirm WRONG must be rejected BEFORE any env read or API call."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    counting = _CallCountingServer(pool, "test-control-token")
    counting.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", counting.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(
            ["pool", "suspend", "--provider-id", "acme", "--confirm", "WRONG"]
        )
    finally:
        counting.shutdown()
    out = capsys.readouterr().out
    assert code != 0
    assert counting.suspend_calls == 0
    assert "sk-" not in out
    assert "test-control-token" not in out


def test_pool_stop_wrong_confirm_word_makes_no_api_call(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """--confirm WRONG must be rejected BEFORE any env read or API call."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    counting = _CallCountingServer(pool, "test-control-token")
    counting.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", counting.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "stop", "--confirm", "WRONG"])
    finally:
        counting.shutdown()
    out = capsys.readouterr().out
    assert code != 0
    assert counting.stop_calls == 0
    assert "test-control-token" not in out


def test_pool_stop_halt_false_returns_nonzero(tmp_path: Path, capsys, monkeypatch) -> None:
    """stop reports halted=false → exit 1 (truthful exit codes)."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    from hunter.pool_api import PoolControlServer

    def failing_stop(reason="manual_stop"):
        return {"halted": False, "error": "control_state_write_failed"}

    pool.stop_production = failing_stop  # type: ignore[method-assign]
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "stop", "--confirm", "STOP"])
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 1
    assert '"halted": false' in out


def test_pool_stop_proxy_halt_failed_returns_nonzero(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """halted=true but error=proxy_halt_failed → exit 1, error preserved."""
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    from hunter.pool_api import PoolControlServer

    def partial_stop(reason="manual_stop"):
        return {"halted": True, "error": "proxy_halt_failed"}

    pool.stop_production = partial_stop  # type: ignore[method-assign]
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "stop", "--confirm", "STOP"])
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 1
    assert "proxy_halt_failed" in out


def test_pool_stop_success_exits_zero_and_sanitized(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    data_dir, _, _, pool = _seed(tmp_path, hunter_root=None)
    from hunter.pool_api import PoolControlServer
    server = PoolControlServer(pool, bearer_token="test-control-token")
    server.start_background()
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", server.url)
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    try:
        code = cli_main(["pool", "stop", "--confirm", "STOP"])
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 0
    assert "test-control-token" not in out
    assert "sk-" not in out


# =============================================================================
# Feishu webhook wiring and outbox drain (Problem 1.4.2D)
# =============================================================================


def test_notifications_drain_command(tmp_path: Path, capsys, monkeypatch) -> None:
    """Configured empty outbox does not attempt network delivery."""
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://example.invalid/offline-webhook")
    monkeypatch.setattr("hunter.runtime.notify.urllib.request.urlopen", lambda *a, **kw: pytest.fail("unexpected network"))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    code = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert parsed["sent"] == []
    assert parsed["failed"] == []
    assert OutboxStore(data_dir / "notification_outbox.json").pending() == []


# =============================================================================
# notifications drain = real delivery before mark-sent (remediation plan stage 3)
# =============================================================================


def _seed_pending_outbox(data_dir: Path, event_id: str = "test-ev-001"):
    from hunter.runtime.outbox import OutboxMessage, OutboxStore
    outbox = OutboxStore(data_dir / "notification_outbox.json")
    outbox.enqueue(
        OutboxMessage(
            event_id=event_id,
            provider_id="acme",
            provider_name="Acme AI",
            event_type="NEW_HIGH_VALUE",
            title="Test notification",
            body="Test body",
        )
    )
    outbox.close()
    return event_id


class _RecordingAdapter:
    """Offline adapter recording send() calls; configurable failure."""

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def send(self, message) -> None:
        self.calls.append(message.event_id)
        if self.fail:
            from hunter.runtime.notify import NotificationError
            raise NotificationError("feishu_unreachable")


def test_drain_without_webhook_env_fails_closed_pending_unchanged(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """No HUNTER_FEISHU_WEBHOOK_URL → config error, pending NOT marked sent."""
    monkeypatch.delenv("HUNTER_FEISHU_WEBHOOK_URL", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    eid = _seed_pending_outbox(data_dir)
    code = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 2
    reopened = OutboxStore(data_dir / "notification_outbox.json")
    pending_ids = [m.event_id for m in reopened.pending()]
    assert pending_ids == [eid]


def test_drain_marks_sent_only_after_adapter_success(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Adapter success happens BEFORE mark-sent; second drain does not resend."""
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://example.invalid/offline-webhook")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    eid = _seed_pending_outbox(data_dir)
    from hunter.runtime.notify import WebhookFeishuAdapter
    from hunter.runtime.outbox import OutboxStore

    recorded: dict[str, object] = {}

    class RecordingAdapter(WebhookFeishuAdapter):
        def __init__(self, webhook_url: str):
            super().__init__(webhook_url)
            self.calls: list[str] = []

        def send(self, message) -> None:
            recorded.setdefault("order", []).append(("send", message.event_id))
            self.calls = getattr(self, "calls", [])
            self.calls.append(message.event_id)

    monkeypatch.setattr(
        "hunter.runtime.notify.WebhookFeishuAdapter", RecordingAdapter, raising=False
    )
    code = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert parsed["sent"] == [eid]
    assert recorded["order"] == [("send", eid)]
    reopened = OutboxStore(data_dir / "notification_outbox.json")
    assert [m.event_id for m in reopened.sent()] == [eid]
    assert reopened.pending() == []
    # Second drain: nothing pending, adapter not called again
    code2 = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    out2 = capsys.readouterr().out
    assert code2 == 0
    assert json.loads(out2)["sent"] == []


def test_drain_adapter_failure_keeps_pending_and_nonzero(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Adapter failure → pending preserved, attempts/next_retry updated, exit 1."""
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://example.invalid/offline-webhook")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    eid = _seed_pending_outbox(data_dir)
    from hunter.runtime.notify import WebhookFeishuAdapter

    class FailingAdapter(WebhookFeishuAdapter):
        def send(self, message) -> None:
            from hunter.runtime.notify import NotificationError
            raise NotificationError("feishu_unreachable")

    monkeypatch.setattr(
        "hunter.runtime.notify.WebhookFeishuAdapter", FailingAdapter, raising=False
    )
    code = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 1
    reopened = OutboxStore(data_dir / "notification_outbox.json")
    pending = reopened.pending()
    assert [m.event_id for m in pending] == [eid]
    assert pending[0].attempts == 1
    assert pending[0].last_error_code == "feishu_unreachable"
    assert pending[0].next_retry_at is not None
    assert parsed_failure_guard(out)


def parsed_failure_guard(out: str) -> bool:
    """Failure output names the failed event, never secrets."""
    parsed = json.loads(out)
    return parsed["failed"] == ["test-ev-001"]


def test_drain_http_200_but_app_level_failure_not_marked_sent(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """HTTP 200 with Feishu application-layer error code must NOT mark-sent."""
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://example.invalid/offline-webhook")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    eid = _seed_pending_outbox(data_dir)
    from hunter.runtime.notify import WebhookFeishuAdapter
    from hunter.runtime.outbox import OutboxStore

    class AppFailureAdapter(WebhookFeishuAdapter):
        def send(self, message) -> None:
            from hunter.runtime.notify import NotificationError
            raise NotificationError("feishu_app_error")

    monkeypatch.setattr(
        "hunter.runtime.notify.WebhookFeishuAdapter", AppFailureAdapter, raising=False
    )
    code = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    reopened = OutboxStore(data_dir / "notification_outbox.json")
    assert code == 1
    assert [m.event_id for m in reopened.pending()] == [eid]
    assert reopened.sent() == []


def test_drain_retry_not_due_and_max_attempts_reported_skipped(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Retry-not-due and over-max-attempts messages are skipped, not faked sent."""
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://example.invalid/offline-webhook")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    from datetime import datetime, timedelta, timezone
    from hunter.runtime.outbox import OutboxMessage, OutboxStore

    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    outbox = OutboxStore(data_dir / "notification_outbox.json")
    outbox.enqueue(
        OutboxMessage(
            event_id="evt-retry-wait",
            provider_id="acme",
            provider_name="Acme AI",
            event_type="NEW_HIGH_VALUE",
            title="t",
            body="b",
            timestamp=now,
            next_retry_at=now + timedelta(seconds=120),
        )
    )
    outbox.enqueue(
        OutboxMessage(
            event_id="evt-dead",
            provider_id="acme",
            provider_name="Acme AI",
            event_type="NEW_HIGH_VALUE",
            title="t",
            body="b",
            timestamp=now,
            attempts=10,
        )
    )
    outbox.close()

    from hunter.runtime.notify import WebhookFeishuAdapter

    class NeverAdapter(WebhookFeishuAdapter):
        def __init__(self, webhook_url: str):
            super().__init__(webhook_url)
            self.calls: list[str] = []

        def send(self, message) -> None:
            self.calls.append(message.event_id)

    monkeypatch.setattr(
        "hunter.runtime.notify.WebhookFeishuAdapter", NeverAdapter, raising=False
    )

    from hunter.runtime.notify import OutboxConsumer

    def _consumer_with_clock(outbox_store, adapter):
        return OutboxConsumer(outbox_store, adapter, clock=lambda: now)

    monkeypatch.setattr(
        "hunter.runtime.notify.OutboxConsumer", _consumer_with_clock
    )
    code = cli_main(["notifications", "drain", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert parsed["sent"] == []
    assert sorted(parsed["skipped_retry"]) == ["evt-dead", "evt-retry-wait"]
    reopened = OutboxStore(data_dir / "notification_outbox.json")
    assert len(reopened.pending()) == 2


def test_run_stage_two_wires_feishu_adapter(monkeypatch, tmp_path) -> None:
    """run-stage-two wires WebhookFeishuAdapter when env var is set."""
    monkeypatch.setenv("HUNTER_POOL_CONTROL_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("HUNTER_POOL_CONTROL_TOKEN", "test-control-token")
    monkeypatch.setenv("HUNTER_FEISHU_WEBHOOK_URL", "https://hooks.feishu.cn/custom/test")
    captured_adapter = {"value": None}
    captured_runner = {"kwargs": None}

    class FakeClient:
        def __init__(self, url, token):
            pass

    class FakeRunner:
        def __init__(self, *, data_dir, pool_control, notification_adapter, **kw):
            captured_runner["kwargs"] = {"pool_control": pool_control, "notification_adapter": notification_adapter}
        def run(self):
            from types import SimpleNamespace
            return SimpleNamespace(to_dict=lambda: {"skipped_locked": False}, skipped_locked=False, suspend_failed=[], pool_runtime_split=[])

    monkeypatch.setattr("hunter.pool_api.PoolControlClient", FakeClient)
    monkeypatch.setattr("hunter.runtime.stage2.Stage2Runner", FakeRunner)
    from hunter.cli import main as cli_entry
    code = cli_entry(["run-stage-two", "--data-dir", str(tmp_path)])
    assert code == 0
    assert captured_runner["kwargs"]["notification_adapter"] is not None
    assert "Webhook" in type(captured_runner["kwargs"]["notification_adapter"]).__name__


# =============================================================================
# Client-config CLI command (Problem 1.4.2E)
# =============================================================================


def test_cli_client_config_write_offline(tmp_path: Path, capsys, monkeypatch) -> None:
    """Runtime state alone must not generate production client configuration."""
    monkeypatch.delenv("HUNTER_POOL_CONTROL_URL", raising=False)
    monkeypatch.delenv("HUNTER_POOL_CONTROL_TOKEN", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    from hunter.runtime.store import RuntimeStore
    store = RuntimeStore(data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl")
    store.upsert(
        RuntimeProvider(
            provider_id="acme",
            provider_name="Acme AI",
            evidence_status="FREE_CONFIRMED",
            credential_status=CredentialStatus.CONFIGURED,
            health_status=HealthStatus.HEALTHY,
            protocol_result=ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass"),
            expected_pool_status=ExpectedPoolStatus.PRODUCTION,
            actual_pool_status=ActualPoolStatus.PRODUCTION,
            approval_status=ApprovalStatus.APPROVED,
        ),
        reason="setup",
    )
    output_dir = tmp_path / "clients"
    code = cli_main([
        "client-config", "write",
        "--data-dir", str(data_dir),
        "--output-dir", str(output_dir),
    ])
    assert code == 2
    out = capsys.readouterr().out
    assert "pool_control_not_configured" in out
    assert not output_dir.exists()
    assert "sk-" not in out
    assert "Bearer " not in out
