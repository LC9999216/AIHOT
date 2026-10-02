# src/hunter/runtime/auto_promote.py
# WHY: B-8 自动 promote — 删除人工 approve 闸, binding 自动计算并写入(保留审计)
"""Automatic approval + promotion for lean pipeline (B-8).

User decision 2026-09-22: 完全删除手工审核。
- No human `approve` call. The binding is derived by the system from
  authoritative stores at promote-time (same computation as manual approve),
  then promote runs immediately.
- Hard gates: credential CONFIGURED, health HEALTHY, registry FREE_CONFIRMED,
  lean verdict VERIFIED_FREE, probe PASSED. Any drift fails closed.
- Binding remains computed-not-accepted → audit replay intact.
"""
from typing import Optional, Any
from .orchestrator import (
    LifecycleResult, promote_to_production, request_approval,
    check_credential_gate, check_health_gate, GateResult,
)
from ..verification.models import VerificationVerdict


def promote_lean(
    provider_id: str,
    runtime_store,
    pool_control,
    registry,
    evidence_store,
    *,
    lean_verdict: str = "VERIFIED_FREE",
    probe_status: str = "PASSED",
    outbox_store=None,
    clock: Optional[Any] = None,
) -> LifecycleResult:
    """Auto-approve + auto-promote in one call (no human step)."""
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(provider_id=provider_id, success=False,
                               error="no_runtime_record")

    pre = [check_credential_gate(rp), check_health_gate(rp)]
    if not all(g.passed for g in pre):
        return LifecycleResult(provider_id=provider_id, success=False,
                               gates=pre, error="lean_autoprove_gates_failed")

    if lean_verdict != "VERIFIED_FREE":
        return LifecycleResult(provider_id=provider_id, success=False,
                               error=f"lean_verdict_not_verified_free:{lean_verdict}")
    if probe_status != "PASSED":
        return LifecycleResult(provider_id=provider_id, success=False,
                               error=f"probe_{probe_status.lower()}_blocks_auto_promote")

    # 1) 自动写入 approval binding(与人工 approve 相同计算路径, 审计等价)
    approval = request_approval(provider_id, runtime_store, registry,
                                evidence_store, approved_by="system:auto-revised",
                                clock=clock)
    if not approval.success:
        return approval

    # 2) 直接 promote(binding 直通, 无需人工)
    return promote_to_production(
        provider_id, runtime_store, pool_control,
        registry=registry, evidence_store=evidence_store,
        outbox_store=outbox_store, clock=clock,
    )
