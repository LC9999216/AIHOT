"""Stage 4 tests: hardened orchestration (review round 2, items 一/三/四).

Every promotion path now validates the approval binding against the
AUTHORITATIVE Registry + EvidenceStore. No approval bypass, no simulate
parameters, suspend is fail-closed, and pool/runtime splits roll back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.evidence.models import Evidence
from hunter.evidence.store import EvidenceStore
from hunter.pool_control import PoolControl
from hunter.registry.schema import FreeOffer, Provider, ProviderStatus
from hunter.registry.store import ProviderRegistry
from hunter.runtime.models import (
    ActualPoolStatus,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
)
from hunter.runtime.orchestrator import (
    check_approval_gate,
    check_authoritative_gate,
    check_credential_gate,
    check_eligibility,
    check_health_gate,
    check_protocol_gate,
    compute_evidence_digest,
    has_official_evidence,
    promote_to_production,
    request_approval,
    run_health_check,
    run_protocol_check,
    suspend_from_production,
)
from hunter.runtime.outbox import OutboxStore
from hunter.runtime.store import RuntimeStore

AS_OF = datetime.fromisoformat("2026-09-12T00:00:00+00:00")
SECRET = "sk-test-SECRET-0123456789"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


class StaticSecretReader:
    def __init__(self, secret: str = SECRET):
        self.secret = secret

    def __call__(self, prompt: str) -> str:
        return self.secret


class FakeProbeRunner:
    """Injected canary runner: deterministic per-feature results."""

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


def _make_registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(tmp_path / "providers.json", tmp_path / "history.jsonl")


def _make_evidence_store(tmp_path: Path) -> EvidenceStore:
    store = EvidenceStore(tmp_path / "evidence.json")
    store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            url="https://acme.ai/pricing",
            normalized_domain="acme.ai",
            officiality="OFFICIAL",
            source_type="pricing",
            content_fingerprint="fp-1",
        )
    )
    return store


def _registry_provider(revision_bump: int = 0, **kw) -> Provider:
    return Provider(
        id="acme",
        provider="Acme AI",
        canonical_domain="acme.ai",
        status=kw.get("status", ProviderStatus.FREE_CONFIRMED),
        evidence_ids=kw.get("evidence_ids", ["ev-1"]),
        verification_confidence=kw.get("verification_confidence", 85),
        score_metadata=kw.get(
            "score_metadata", {"score": 90, "as_of": AS_OF.isoformat()}
        ),
        free_score=kw.get("free_score", 90),
        free_offer=kw.get("free_offer", FreeOffer(offer_status="active")),
        metadata={"bump": revision_bump} if revision_bump else {},
    )


def _seed_registry(
    registry: ProviderRegistry, revision_bump: int = 0, **kw
) -> Provider:
    return registry.upsert_provider(
        _registry_provider(revision_bump, **kw),
        reason="test seed",
        source_metadata={"test": True},
    )


def _make_store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "runtime_providers.json", tmp_path / "runtime_history.jsonl")


def _make_rp(
    credential_status=CredentialStatus.CONFIGURED,
    health_status=HealthStatus.HEALTHY,
    protocol=None,
    approval_status=ApprovalStatus.PENDING,
    **kw,
) -> RuntimeProvider:
    if protocol is None:
        protocol = ProtocolResult(chat="pass", responses="pass", streaming="pass", tools="pass")
    return RuntimeProvider(
        provider_id=kw.get("provider_id", "acme"),
        provider_name=kw.get("provider_name", "Acme AI"),
        credential_status=credential_status,
        health_status=health_status,
        protocol_result=protocol,
        approval_status=approval_status,
    )


def _setup(tmp_path: Path, probe_results=None) -> tuple[RuntimeStore, PoolControl]:
    store = _make_store(tmp_path)
    pc = PoolControl(
        tmp_path / "staging",
        tmp_path / "production",
        read_secret=StaticSecretReader(),
        probe_runner=FakeProbeRunner(probe_results),
    )
    return store, pc


def _full_pipeline(
    tmp_path: Path, probe_results=None
) -> tuple[RuntimeStore, PoolControl, ProviderRegistry, EvidenceStore, OutboxStore]:
    """Seed everything and drive acme to the approved+staged state."""
    store, pc = _setup(tmp_path, probe_results)
    registry = _make_registry(tmp_path)
    evidence_store = _make_evidence_store(tmp_path)
    _seed_registry(registry)
    outbox = OutboxStore(tmp_path / "outbox.json")

    pc.register_provider("acme", label="Acme AI", base_url="https://api.acme.ai/v1")
    pc.enter_key("acme")

    rp = _make_rp(health_status=HealthStatus.UNKNOWN)
    store.upsert(rp, reason="setup")
    assert run_health_check("acme", store, pc).success is True
    assert run_protocol_check("acme", store, pc).success is True
    approval = request_approval("acme", store, registry, evidence_store)
    assert approval.success is True, approval.error
    return store, pc, registry, evidence_store, outbox


# =============================================================================
# Gate unit tests
# =============================================================================


def test_credential_gate_passes() -> None:
    g = check_credential_gate(_make_rp())
    assert g.passed is True


def test_credential_gate_fails() -> None:
    g = check_credential_gate(_make_rp(credential_status=CredentialStatus.NOT_CONFIGURED))
    assert g.passed is False
    assert "NOT_CONFIGURED" in g.detail


def test_health_gate_passes() -> None:
    assert check_health_gate(_make_rp()).passed is True


def test_health_gate_fails() -> None:
    assert check_health_gate(_make_rp(health_status=HealthStatus.DOWN)).passed is False


def test_protocol_gate_passes() -> None:
    assert check_protocol_gate(_make_rp()).passed is True


def test_protocol_gate_fails_partial() -> None:
    pr = ProtocolResult(chat="pass", responses="fail", streaming="pass", tools="pass")
    g = check_protocol_gate(_make_rp(protocol=pr))
    assert g.passed is False
    assert "fail" in g.detail


def test_approval_gate_passes() -> None:
    assert check_approval_gate(_make_rp(approval_status=ApprovalStatus.APPROVED)).passed is True


def test_approval_gate_pending() -> None:
    assert check_approval_gate(_make_rp(approval_status=ApprovalStatus.PENDING)).passed is False


def test_approval_gate_has_no_bypass() -> None:
    """The require_approval=False bypass is REMOVED (review 一)."""
    import inspect

    params = inspect.signature(check_approval_gate).parameters
    assert "require_approval" not in params


def test_protocol_check_has_no_simulate_parameter() -> None:
    import inspect

    params = inspect.signature(run_protocol_check).parameters
    assert "simulate_all_pass" not in params
    assert "simulate" not in params


# =============================================================================
# Eligibility
# =============================================================================


def test_eligibility_all_pass(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    registry = _make_registry(tmp_path)
    evidence_store = _make_evidence_store(tmp_path)
    provider = _seed_registry(registry)
    rp = _make_rp(approval_status=ApprovalStatus.PENDING)
    rp = store.upsert(rp, reason="setup")
    request_approval("acme", store, registry, evidence_store)
    result = check_eligibility("acme", store, registry, evidence_store)
    assert result.success is True
    assert all(g.passed for g in result.gates)


def test_eligibility_missing_provider(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    assert check_eligibility("nonexistent", store).success is False


# =============================================================================
# Health / protocol lifecycle (real classified probes)
# =============================================================================


def test_health_check_updates_status(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    pc.register_provider("acme", base_url="https://api.acme.ai/v1")
    pc.enter_key("acme")
    store.upsert(_make_rp(health_status=HealthStatus.UNKNOWN), reason="setup")
    result = run_health_check("acme", store, pc)
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.health_status is HealthStatus.HEALTHY
    assert updated.health_checked_at is not None


def test_health_check_missing_provider(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    assert run_health_check("nonexistent", store, pc).success is False


def test_health_check_classifies_failures(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path, probe_results={"chat": {"status": "fail", "classification": "auth"}})
    pc.register_provider("acme", base_url="https://api.acme.ai/v1")
    pc.enter_key("acme")
    store.upsert(_make_rp(health_status=HealthStatus.UNKNOWN), reason="setup")
    run_health_check("acme", store, pc)
    assert store.get_provider("acme").health_status is HealthStatus.INVALID_KEY


def test_protocol_check_runs_all_four_features(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    pc.register_provider("acme", base_url="https://api.acme.ai/v1")
    pc.enter_key("acme")
    store.upsert(_make_rp(), reason="setup")
    result = run_protocol_check("acme", store, pc)
    assert result.success is True
    pr = store.get_provider("acme").protocol_result
    assert pr.all_pass() is True


def test_protocol_check_partial_failure_fails_gate(tmp_path: Path) -> None:
    store, pc = _setup(
        tmp_path, probe_results={"tools": {"status": "fail", "classification": "unsupported"}}
    )
    pc.register_provider("acme", base_url="https://api.acme.ai/v1")
    pc.enter_key("acme")
    store.upsert(_make_rp(), reason="setup")
    run_protocol_check("acme", store, pc)
    pr = store.get_provider("acme").protocol_result
    assert pr.chat == "pass"
    assert pr.tools == "fail"
    assert pr.all_pass() is False


def test_protocol_check_unavailable_counts_as_fail(tmp_path: Path) -> None:
    store, pc = _setup(
        tmp_path, probe_results={"streaming": {"status": "unavailable", "classification": "timeout"}}
    )
    pc.register_provider("acme", base_url="https://api.acme.ai/v1")
    pc.enter_key("acme")
    store.upsert(_make_rp(), reason="setup")
    run_protocol_check("acme", store, pc)
    assert store.get_provider("acme").protocol_result.streaming == "fail"


# =============================================================================
# Approval: derived from authoritative sources only
# =============================================================================


def test_request_approval_derives_binding_from_stores(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    registry = _make_registry(tmp_path)
    evidence_store = _make_evidence_store(tmp_path)
    provider = _seed_registry(registry)
    store.upsert(_make_rp(approval_status=ApprovalStatus.PENDING), reason="setup")

    result = request_approval("acme", store, registry, evidence_store)
    assert result.success is True
    binding = store.get_provider("acme").approval_binding
    assert binding is not None
    assert binding.registry_revision == provider.revision
    assert binding.free_score_snapshot == 90
    # digest recomputed from evidence ids + fingerprints, not caller input
    assert binding.evidence_digest == compute_evidence_digest(evidence_store, ["ev-1"])
    assert binding.health_result == "HEALTHY"
    assert binding.protocol_result == "all_pass"


def test_request_approval_signature_accepts_no_forged_inputs() -> None:
    import inspect

    params = inspect.signature(request_approval).parameters
    for banned in ("registry_free_score", "evidence_digest", "registry_revision"):
        assert banned not in params


def test_request_approval_requires_free_confirmed(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    registry = _make_registry(tmp_path)
    evidence_store = _make_evidence_store(tmp_path)
    _seed_registry(registry, status=ProviderStatus.UNCERTAIN)
    store.upsert(_make_rp(), reason="setup")
    result = request_approval("acme", store, registry, evidence_store)
    assert result.success is False
    assert "free_confirmed" in (result.error or "")


def test_request_approval_requires_official_evidence(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    registry = _make_registry(tmp_path)
    evidence_store = EvidenceStore(tmp_path / "evidence.json")
    evidence_store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            url="https://blog.example.com/acme",
            officiality="THIRD_PARTY",
            source_type="blog",
            content_fingerprint="fp-1",
        )
    )
    _seed_registry(registry)
    store.upsert(_make_rp(), reason="setup")
    result = request_approval("acme", store, registry, evidence_store)
    assert result.success is False
    assert result.error == "no_official_evidence"


def test_request_approval_requires_health_and_protocol(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    registry = _make_registry(tmp_path)
    evidence_store = _make_evidence_store(tmp_path)
    _seed_registry(registry)
    store.upsert(
        _make_rp(health_status=HealthStatus.DOWN), reason="setup"
    )
    result = request_approval("acme", store, registry, evidence_store)
    assert result.success is False
    assert result.error == "health_not_confirmed"

    pr = ProtocolResult(chat="pass", responses="fail", streaming="pass", tools="pass")
    store2 = _make_store(tmp_path / "sub2")
    store2.upsert(_make_rp(protocol=pr), reason="setup")
    result2 = request_approval("acme", store2, registry, evidence_store)
    assert result2.success is False
    assert result2.error == "protocol_not_confirmed"


def test_request_approval_missing_provider(tmp_path: Path) -> None:
    store, pc = _setup(tmp_path)
    registry = _make_registry(tmp_path)
    evidence_store = _make_evidence_store(tmp_path)
    result = request_approval("nonexistent", store, registry, evidence_store)
    assert result.success is False


# =============================================================================
# Authoritative gate regressions (review 一)
# =============================================================================


def _approved_state(tmp_path: Path):
    store, pc, registry, evidence_store, outbox = _full_pipeline(tmp_path)
    return store, pc, registry, evidence_store, outbox


def test_promote_success(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is True, result.error
    updated = store.get_provider("acme")
    assert updated.expected_pool_status is ExpectedPoolStatus.PRODUCTION
    assert updated.actual_pool_status is ActualPoolStatus.PRODUCTION
    assert pc.list_production() == ["acme"]


def test_promote_enqueues_notification(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, outbox = _approved_state(tmp_path)
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store,
        outbox_store=outbox,
    )
    assert result.success is True
    assert len(outbox.pending()) == 1
    assert outbox.pending()[0].event_type == "PROMOTED_TO_PRODUCTION"


def test_promote_missing_credential_fails(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "credential_status": CredentialStatus.NOT_CONFIGURED}),
        changed_fields=["credential_status"],
        reason="credential removed",
    )
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    assert result.error == "promotion_gates_not_met"


def test_promote_not_approved_fails(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    rp = store.get_provider("acme")
    store.upsert(
        RuntimeProvider(**{**rp.__dict__, "approval_status": ApprovalStatus.PENDING}),
        changed_fields=["approval_status"],
        reason="approval withdrawn",
    )
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    assert "approval" in [g.gate for g in result.gates]


def test_promote_stale_registry_revision_rejected(tmp_path: Path) -> None:
    """Registry bumped after approval → binding is stale → fail closed."""
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    _seed_registry(registry, revision_bump=1)  # revision increments
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    gate = next(g for g in result.gates if g.gate == "authoritative")
    assert gate.passed is False
    assert "stale_registry_revision" in gate.detail
    assert pc.list_production() == []


def test_promote_evidence_changed_rejected(tmp_path: Path) -> None:
    """Evidence content changed after approval → digest mismatch → fail."""
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    evidence_store.upsert(
        Evidence(
            evidence_id="ev-1",
            provider_id="acme",
            url="https://acme.ai/pricing",
            normalized_domain="acme.ai",
            officiality="OFFICIAL",
            source_type="pricing",
            content_fingerprint="fp-CHANGED",
        )
    )
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    gate = next(g for g in result.gates if g.gate == "authoritative")
    assert gate.detail == "evidence_digest_mismatch"


def test_promote_offer_no_longer_active_rejected(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    _seed_registry(registry, free_offer=FreeOffer(offer_status="expired"))
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    gate = next(g for g in result.gates if g.gate == "authoritative")
    assert "offer_status" in gate.detail


def test_promote_registry_downgraded_rejected(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    _seed_registry(registry, status=ProviderStatus.UNCERTAIN)
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    gate = next(g for g in result.gates if g.gate == "authoritative")
    assert "registry_status" in gate.detail


def test_promote_forged_binding_rejected(tmp_path: Path) -> None:
    """A hand-crafted binding with wrong digest cannot pass the gate."""
    from hunter.runtime.models import ApprovalBinding

    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    rp = store.get_provider("acme")
    forged = ApprovalBinding(
        provider_id="acme",
        registry_revision=rp.approval_binding.registry_revision,
        evidence_digest="0" * 64,                     # forged digest
        free_score_snapshot=rp.approval_binding.free_score_snapshot,
        eligibility_policy_version="v1",
        health_result="HEALTHY",
        protocol_result="all_pass",
        approved_by="attacker",
    )
    updated = RuntimeProvider(**{**rp.__dict__, "approval_binding": forged})
    store.upsert(updated, changed_fields=["approval_binding"], reason="test forged")
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False
    gate = next(g for g in result.gates if g.gate == "authoritative")
    assert gate.detail == "evidence_digest_mismatch"


def test_promote_health_drift_since_approval_rejected(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    rp = store.get_provider("acme")
    updated = RuntimeProvider(**{**rp.__dict__, "health_status": HealthStatus.DOWN})
    store.upsert(updated, changed_fields=["health_status"], reason="health degraded")
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    assert result.success is False  # health gate alone blocks; drift also detected
    gate_details = [g.detail for g in result.gates if not g.passed]
    assert any("health" in d for d in gate_details)


def test_promote_requires_registry_and_evidence_stores(tmp_path: Path) -> None:
    """registry/evidence_store are required keyword args — no silent skip."""
    import inspect

    params = inspect.signature(promote_to_production).parameters
    assert params["registry"].default is inspect.Parameter.empty
    assert params["evidence_store"].default is inspect.Parameter.empty


# =============================================================================
# Promotion rollback (review 四.2)
# =============================================================================


def test_promote_rolls_back_pool_when_runtime_store_fails(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    original_upsert = store.upsert

    def failing_upsert(*args, **kwargs):
        raise RuntimeError("disk on fire")

    store.upsert = failing_upsert  # type: ignore[assignment]
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    store.upsert = original_upsert  # type: ignore[assignment]
    assert result.success is False
    assert result.error == "runtime_store_write_failed_rolled_back"
    # provider is NOT left untracked in production
    assert pc.list_production() == []


def test_promote_state_split_halts_production_and_alerts(tmp_path: Path) -> None:
    """Rollback failure → production halted + POOL_STATE_SPLIT alert."""
    store, pc, registry, evidence_store, outbox = _approved_state(tmp_path)

    def failing_upsert(*args, **kwargs):
        raise RuntimeError("disk on fire")

    def failing_suspend(provider_id):
        return {"suspended": False, "error": "production_readback_failed"}

    store.upsert = failing_upsert  # type: ignore[assignment]
    pc.suspend = failing_suspend  # type: ignore[assignment]
    result = promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store,
        outbox_store=outbox,
    )
    assert result.success is False
    assert result.error == "runtime_store_write_failed_split"
    assert pc.production_halted is True
    alerts = [m for m in outbox.pending() if m.event_type == "POOL_STATE_SPLIT"]
    assert len(alerts) == 1
    assert alerts[0].high_priority is True


# =============================================================================
# Suspension fail-closed (review 四.3/四.4)
# =============================================================================


def test_suspend_removes_from_production(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    result = suspend_from_production("acme", store, pc, reason="health_degradation")
    assert result.success is True
    updated = store.get_provider("acme")
    assert updated.actual_pool_status is ActualPoolStatus.SUSPENDED
    assert pc.list_production() == []


def test_suspend_enqueues_notification(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, outbox = _approved_state(tmp_path)
    promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    result = suspend_from_production(
        "acme", store, pc, reason="test", outbox_store=outbox
    )
    assert result.success is True
    assert len(outbox.pending()) == 1
    assert outbox.pending()[0].event_type == "POOL_SUSPENDED"


def test_suspend_failure_does_not_write_suspended(tmp_path: Path) -> None:
    """Suspend fails in the pool → NO SUSPENDED state, production halted,
    high-priority alert enqueued (review 四.4)."""
    store, pc, registry, evidence_store, outbox = _approved_state(tmp_path)
    promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    status_before = store.get_provider("acme").actual_pool_status

    def failing_suspend(provider_id):
        return {"suspended": False, "error": "production_write_failed"}

    pc.suspend = failing_suspend  # type: ignore[assignment]
    result = suspend_from_production(
        "acme", store, pc, reason="health_degradation", outbox_store=outbox
    )
    assert result.success is False
    assert "pool_suspension_failed" in (result.error or "")
    # record was NOT marked suspended
    assert store.get_provider("acme").actual_pool_status == status_before
    # production is halted: further promotions blocked
    assert pc.production_halted is True
    # high-priority alert enqueued
    alerts = [m for m in outbox.pending() if m.event_type == "POOL_SUSPEND_FAILED"]
    assert len(alerts) == 1
    assert alerts[0].high_priority is True


def test_suspend_preserves_staging_key(tmp_path: Path) -> None:
    store, pc, registry, evidence_store, _ = _approved_state(tmp_path)
    promote_to_production(
        "acme", store, pc, registry=registry, evidence_store=evidence_store
    )
    suspend_from_production("acme", store, pc, reason="test")
    assert pc.status("acme").in_staging is True
    assert pc._is_configured("acme") is True


# =============================================================================
# Digest helpers
# =============================================================================


def test_evidence_digest_is_deterministic_and_bound(tmp_path: Path) -> None:
    evidence_store = _make_evidence_store(tmp_path)
    d1 = compute_evidence_digest(evidence_store, ["ev-1"])
    d2 = compute_evidence_digest(evidence_store, ["ev-1"])
    assert d1 == d2 and len(d1) == 64


def test_evidence_digest_fails_when_evidence_missing(tmp_path: Path) -> None:
    evidence_store = _make_evidence_store(tmp_path)
    assert compute_evidence_digest(evidence_store, ["ev-1", "ghost"]) is None


def test_has_official_evidence(tmp_path: Path) -> None:
    evidence_store = _make_evidence_store(tmp_path)
    assert has_official_evidence(evidence_store, ["ev-1"]) is True
    assert has_official_evidence(evidence_store, ["ghost"]) is False
