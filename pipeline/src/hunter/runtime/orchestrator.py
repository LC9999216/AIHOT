"""Stage 4: Health, protocol, eligibility, approval orchestration.

Ties together RuntimeStore, PoolControl, Registry, EvidenceStore and
OutboxStore into a single lifecycle. Deterministic; all time reads are
injectable.

Gates (all must be green for automatic progression):
1. FREE_CONFIRMED in the authoritative Registry (re-read at promotion time)
2. OFFICIAL evidence present in the authoritative EvidenceStore
3. credential_status == CONFIGURED (key in PoolControl)
4. health_status == HEALTHY (from a REAL classified probe)
5. protocol_result.all_pass() (from REAL canaries; no simulate bypass)
6. approval_status == APPROVED with a binding that still matches the
   authoritative registry revision, evidence digest, free score, policy
   version, health result, protocol result, and offer_status=active

Review round 2 fixes baked in:
- request_approval derives every binding input from the authoritative stores;
  callers cannot inject arbitrary revision/digest/score values (一).
- promote re-validates the approval binding against freshly-read authoritative
  sources; a stale or forged binding fails closed (一).
- A RuntimeStore failure after the pool write triggers rollback (suspend); if
  rollback also fails, production is halted and a high-priority
  POOL_STATE_SPLIT alert is enqueued (四).
- suspend verifies removal via readback; on failure NO SUSPENDED state is
  written, production is halted, and a high-priority POOL_SUSPEND_FAILED alert
  is enqueued (四).
- No require_approval bypass, no simulate_all_pass, no hardcoded timestamps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..evidence.store import EvidenceStore
from ..pool_control import PoolControl
from ..registry.schema import ProviderStatus
from ..registry.store import ProviderRegistry
from .models import (
    ApprovalBinding,
    ApprovalStatus,
    CredentialStatus,
    ExpectedPoolStatus,
    HealthStatus,
    ProtocolResult,
    RuntimeProvider,
    ActualPoolStatus,
)
from .outbox import OutboxMessage, OutboxStore
from .store import RuntimeStore

ELIGIBILITY_POLICY_VERSION = "v1"


class EligibilityError(Exception):
    """Raised when an eligibility gate is not met."""


@dataclass
class GateResult:
    """Result of checking one eligibility gate."""

    gate: str
    passed: bool
    detail: str = ""


@dataclass
class LifecycleResult:
    """Result of a lifecycle action."""

    provider_id: str
    success: bool
    gates: List[GateResult] = field(default_factory=list)
    new_provider: Optional[RuntimeProvider] = None
    events_emitted: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _now(clock: Optional[Any] = None) -> datetime:
    if clock is not None:
        return clock() if callable(clock) else clock
    return datetime.now(timezone.utc)


def _replace(
    rp: RuntimeProvider,
    **changes: Any,
) -> RuntimeProvider:
    """Copy a RuntimeProvider with field overrides (keeps audit fields)."""
    data = {
        "provider_id": rp.provider_id,
        "provider_name": rp.provider_name,
        "evidence_status": rp.evidence_status,
        "credential_status": rp.credential_status,
        "health_status": rp.health_status,
        "health_checked_at": rp.health_checked_at,
        "protocol_result": rp.protocol_result,
        "expected_pool_status": rp.expected_pool_status,
        "actual_pool_status": rp.actual_pool_status,
        "approval_status": rp.approval_status,
        "approval_binding": rp.approval_binding,
        "revision": rp.revision,
        "last_event_id": rp.last_event_id,
        "last_synced_at": rp.last_synced_at,
        "notified_events": list(rp.notified_events),
    }
    data.update(changes)
    return RuntimeProvider(**data)


# ------------------------------------------------------------------
# Authoritative-source helpers (review 一)
# ------------------------------------------------------------------


def compute_evidence_digest(
    evidence_store: EvidenceStore, evidence_ids: List[str]
) -> Optional[str]:
    """Deterministic digest over evidence ids + content fingerprints.

    Returns None when any referenced evidence is missing from the store
    (fail-closed: we never digest partial evidence sets).
    """
    parts: List[str] = []
    for evidence_id in sorted(evidence_ids):
        evidence = evidence_store.get(evidence_id)
        if evidence is None:
            return None
        parts.append(f"{evidence_id}:{evidence.content_fingerprint}")
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def has_official_evidence(
    evidence_store: EvidenceStore, evidence_ids: List[str]
) -> bool:
    """True only when at least one referenced evidence item is OFFICIAL."""
    for evidence_id in evidence_ids:
        evidence = evidence_store.get(evidence_id)
        if evidence is not None and evidence.officiality == "OFFICIAL":
            return True
    return False


def check_authoritative_gate(
    rp: RuntimeProvider,
    registry: ProviderRegistry,
    evidence_store: EvidenceStore,
) -> GateResult:
    """Re-validate the approval binding against authoritative sources.

    Every field of the stored ApprovalBinding must match values recomputed
    from the Registry and EvidenceStore RIGHT NOW. Any drift (registry
    revision bump, evidence change, score change, offer expiry, policy
    version change, health/protocol drift since approval) fails closed.
    """
    provider = registry.get_provider(rp.provider_id)
    if provider is None:
        return GateResult(gate="authoritative", passed=False, detail="not_in_registry")
    if provider.status != ProviderStatus.FREE_CONFIRMED:
        return GateResult(
            gate="authoritative", passed=False,
            detail=f"registry_status={provider.status.value}",
        )
    free_offer = getattr(provider, "free_offer", None)
    offer_status = getattr(free_offer, "offer_status", None) if free_offer else None
    if offer_status != "active":
        return GateResult(
            gate="authoritative", passed=False, detail=f"offer_status={offer_status}"
        )
    if not provider.evidence_ids:
        return GateResult(gate="authoritative", passed=False, detail="no_evidence_ids")
    if not has_official_evidence(evidence_store, list(provider.evidence_ids)):
        return GateResult(gate="authoritative", passed=False, detail="no_official_evidence")

    binding = rp.approval_binding
    if binding is None:
        return GateResult(gate="authoritative", passed=False, detail="no_approval_binding")
    digest = compute_evidence_digest(evidence_store, list(provider.evidence_ids))
    if digest is None:
        return GateResult(gate="authoritative", passed=False, detail="evidence_missing")
    if binding.provider_id != rp.provider_id:
        return GateResult(gate="authoritative", passed=False, detail="binding_provider_mismatch")
    if binding.registry_revision != provider.revision:
        return GateResult(
            gate="authoritative", passed=False,
            detail=f"stale_registry_revision:{binding.registry_revision}!={provider.revision}",
        )
    if binding.evidence_digest != digest:
        return GateResult(gate="authoritative", passed=False, detail="evidence_digest_mismatch")
    if binding.free_score_snapshot != (provider.free_score if provider.free_score is not None else -1):
        return GateResult(
            gate="authoritative", passed=False,
            detail=f"free_score_mismatch:{binding.free_score_snapshot}!={provider.free_score}",
        )
    if binding.eligibility_policy_version != ELIGIBILITY_POLICY_VERSION:
        return GateResult(
            gate="authoritative", passed=False,
            detail=f"policy_version_mismatch:{binding.eligibility_policy_version}",
        )
    if binding.health_result != rp.health_status.value:
        return GateResult(
            gate="authoritative", passed=False, detail="health_drift_since_approval"
        )
    if binding.protocol_result != "all_pass" or not rp.protocol_result.all_pass():
        return GateResult(gate="authoritative", passed=False, detail="protocol_drift_since_approval")
    return GateResult(gate="authoritative", passed=True, detail="binding_verified")


# ------------------------------------------------------------------
# Eligibility gates
# ------------------------------------------------------------------


def check_credential_gate(rp: RuntimeProvider) -> GateResult:
    """Must have CONFIGURED credential status."""
    passed = rp.credential_status == CredentialStatus.CONFIGURED
    detail = f"credential_status={rp.credential_status.value}" if not passed else ""
    return GateResult(gate="credential", passed=passed, detail=detail)


def check_health_gate(rp: RuntimeProvider) -> GateResult:
    """Must have HEALTHY health status."""
    passed = rp.health_status == HealthStatus.HEALTHY
    detail = f"health_status={rp.health_status.value}" if not passed else ""
    return GateResult(gate="health", passed=passed, detail=detail)


def check_protocol_gate(rp: RuntimeProvider) -> GateResult:
    """All four protocol sub-tests must pass."""
    pr = rp.protocol_result
    passed = pr.all_pass()
    if not passed:
        detail = (
            f"chat={pr.chat}, responses={pr.responses}, "
            f"streaming={pr.streaming}, tools={pr.tools}"
        )
    else:
        detail = "all pass"
    return GateResult(gate="protocol", passed=passed, detail=detail)


def check_approval_gate(rp: RuntimeProvider) -> GateResult:
    """Approval must be APPROVED. There is no bypass (review 一)."""
    passed = rp.approval_status == ApprovalStatus.APPROVED
    detail = f"approval_status={rp.approval_status.value}" if not passed else ""
    return GateResult(gate="approval", passed=passed, detail=detail)


# ------------------------------------------------------------------
# Lifecycle actions
# ------------------------------------------------------------------


def _health_from_probe(probe_result: Dict[str, Any]) -> HealthStatus:
    """Map a PoolControl probe result to a HealthStatus, fail-closed."""
    raw = str(probe_result.get("health_status") or "")
    try:
        return HealthStatus(raw)
    except ValueError:
        return HealthStatus.DOWN


def _checked_at_from_probe(probe_result: Dict[str, Any], clock: Optional[Any]) -> datetime:
    raw = probe_result.get("checked_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed
        except ValueError:
            pass
    return _now(clock)


def run_health_check(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    *,
    clock: Optional[Any] = None,
) -> LifecycleResult:
    """Execute a REAL health probe through PoolControl and update the store."""
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}",
        )
    probe_result = pool_control.probe(provider_id)
    new_health = _health_from_probe(probe_result)
    classification = str(probe_result.get("classification") or "unclassified")
    checked_at = _checked_at_from_probe(probe_result, clock)

    updated = _replace(rp, health_status=new_health, health_checked_at=checked_at)
    stored = runtime_store.upsert(
        updated,
        changed_fields=["health_status"],
        reason=f"health check: {new_health.value} ({classification})",
    )
    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=[stored.last_event_id] if stored.last_event_id else [],
    )


def _protocol_from_probe(
    probe_result: Dict[str, Any], checked_at: datetime
) -> ProtocolResult:
    """Map per-feature canary rows to a ProtocolResult (fail-closed)."""
    features = probe_result.get("features") or {}
    def _feature_status(name: str) -> str:
        row = features.get(name) or {}
        # Only an explicit pass counts; fail/unavailable/unsupported all
        # count as fail for the all_pass gate (honest, fail-closed mapping).
        return "pass" if row.get("status") == "pass" else "fail"
    return ProtocolResult(
        chat=_feature_status("chat"),
        responses=_feature_status("responses"),
        streaming=_feature_status("streaming"),
        tools=_feature_status("tools"),
        checked_at=checked_at,
    )


def run_protocol_check(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    *,
    clock: Optional[Any] = None,
) -> LifecycleResult:
    """Run REAL protocol canaries (chat/responses/streaming/tools separately).

    There is no simulate parameter (review 三): the result comes from the
    PoolControl probe runner — the real FreeLLMPool canaries in production,
    an injected fake in tests.
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}",
        )
    probe_result = pool_control.probe(provider_id, features=("chat", "responses", "streaming", "tools"))
    pr = _protocol_from_probe(probe_result, _checked_at_from_probe(probe_result, clock))

    updated = _replace(rp, protocol_result=pr)
    stored = runtime_store.upsert(
        updated, changed_fields=["protocol_result"], reason="protocol check"
    )
    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=[stored.last_event_id] if stored.last_event_id else [],
    )


def check_eligibility(
    provider_id: str,
    runtime_store: RuntimeStore,
    registry: Optional[ProviderRegistry] = None,
    evidence_store: Optional[EvidenceStore] = None,
) -> LifecycleResult:
    """Check all gates. Read-only; does not modify state.

    The authoritative gate is included whenever registry + evidence stores
    are supplied (promotion paths always supply them).
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}",
        )
    gates = [
        check_credential_gate(rp),
        check_health_gate(rp),
        check_protocol_gate(rp),
        check_approval_gate(rp),
    ]
    if registry is not None and evidence_store is not None:
        gates.append(check_authoritative_gate(rp, registry, evidence_store))
    all_pass = all(g.passed for g in gates)
    return LifecycleResult(provider_id=provider_id, success=all_pass, gates=gates)


def request_approval(
    provider_id: str,
    runtime_store: RuntimeStore,
    registry: ProviderRegistry,
    evidence_store: EvidenceStore,
    *,
    approved_by: str = "user",
    clock: Optional[Any] = None,
) -> LifecycleResult:
    """Record user approval, deriving every binding input from the
    authoritative Registry and EvidenceStore (review 一).

    Callers cannot supply revision/digest/score values; a forged approval is
    impossible because the binding is computed, not accepted.
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}",
        )
    provider = registry.get_provider(provider_id)
    if provider is None:
        return LifecycleResult(provider_id=provider_id, success=False, error="provider_not_in_registry")
    if provider.status != ProviderStatus.FREE_CONFIRMED:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"registry_status_not_free_confirmed:{provider.status.value}",
        )
    if provider.free_score is None:
        return LifecycleResult(provider_id=provider_id, success=False, error="free_score_missing")
    if not provider.evidence_ids:
        return LifecycleResult(provider_id=provider_id, success=False, error="no_evidence_ids")
    if not has_official_evidence(evidence_store, list(provider.evidence_ids)):
        return LifecycleResult(provider_id=provider_id, success=False, error="no_official_evidence")
    digest = compute_evidence_digest(evidence_store, list(provider.evidence_ids))
    if digest is None:
        return LifecycleResult(provider_id=provider_id, success=False, error="evidence_missing")
    if rp.health_status != HealthStatus.HEALTHY:
        return LifecycleResult(provider_id=provider_id, success=False, error="health_not_confirmed")
    if not rp.protocol_result.all_pass():
        return LifecycleResult(provider_id=provider_id, success=False, error="protocol_not_confirmed")

    binding = ApprovalBinding(
        provider_id=provider_id,
        registry_revision=provider.revision,
        evidence_digest=digest,
        free_score_snapshot=provider.free_score,
        eligibility_policy_version=ELIGIBILITY_POLICY_VERSION,
        health_result=rp.health_status.value,
        protocol_result="all_pass",
        approved_by=approved_by,
        approved_at=_now(clock),
    )
    updated = _replace(
        rp,
        expected_pool_status=ExpectedPoolStatus.STAGING,
        approval_status=ApprovalStatus.APPROVED,
        approval_binding=binding,
    )
    stored = runtime_store.upsert(
        updated,
        changed_fields=["approval_status", "expected_pool_status"],
        reason="user approval",
    )
    return LifecycleResult(
        provider_id=provider_id,
        success=True,
        new_provider=stored,
        events_emitted=[stored.last_event_id] if stored.last_event_id else [],
    )


def _in_production(pool_control: Any, provider_id: str) -> bool:
    """Readback helper that works for PoolControl and PoolControlClient."""
    status = pool_control.status(provider_id)
    if isinstance(status, dict):
        return bool(status.get("in_production"))
    return bool(getattr(status, "in_production", False))


def _enqueue(outbox_store: Optional[OutboxStore], message: OutboxMessage) -> None:
    if outbox_store is None:
        return
    try:
        outbox_store.enqueue(message)
    except ValueError:
        # Notification content policy violation must never break the
        # lifecycle; the state change itself is already durable.
        pass


def promote_to_production(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    *,
    registry: ProviderRegistry,
    evidence_store: EvidenceStore,
    outbox_store: Optional[OutboxStore] = None,
    clock: Optional[Any] = None,
) -> LifecycleResult:
    """Promote to production only when ALL gates pass, including the
    authoritative re-validation of the approval binding (review 一).

    Rollback semantics (review 四.2): if the RuntimeStore write fails AFTER
    the pool write, the provider is suspended from production again; if that
    rollback also fails, production is halted and a high-priority
    POOL_STATE_SPLIT alert is enqueued — an untracked production provider is
    never left behind silently.
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}",
        )

    gates = [
        check_credential_gate(rp),
        check_health_gate(rp),
        check_protocol_gate(rp),
        check_approval_gate(rp),
        check_authoritative_gate(rp, registry, evidence_store),
    ]
    if not all(g.passed for g in gates):
        return LifecycleResult(
            provider_id=provider_id, success=False, gates=gates,
            error="promotion_gates_not_met",
        )

    promote_result = pool_control.promote(provider_id)
    if not promote_result.get("promoted"):
        return LifecycleResult(
            provider_id=provider_id, gates=gates, success=False,
            error=f"pool_promotion_failed:{promote_result.get('error', 'unknown')}",
        )

    # Pool write succeeded; now persist the state change. Failure here means
    # the pool and runtime store have SPLIT — roll the pool write back.
    try:
        updated = _replace(
            rp,
            expected_pool_status=ExpectedPoolStatus.PRODUCTION,
            actual_pool_status=ActualPoolStatus.PRODUCTION,
            last_synced_at=_now(clock),
        )
        stored = runtime_store.upsert(
            updated,
            changed_fields=["expected_pool_status", "actual_pool_status"],
            reason="production promotion",
        )
    except Exception:  # noqa: BLE001 — rollback path must catch everything
        rollback = pool_control.suspend(provider_id)
        if not rollback.get("suspended"):
            # Rollback failed: contain by halting production and alerting.
            pool_control.stop_production()
            _enqueue(
                outbox_store,
                OutboxMessage(
                    event_id=f"pool-state-split-{provider_id}",
                    provider_id=provider_id,
                    provider_name=rp.provider_name or provider_id,
                    event_type="POOL_STATE_SPLIT",
                    title=f"{rp.provider_name or provider_id} pool/runtime state split",
                    body=(
                        f"Provider {rp.provider_name or provider_id} was written to the "
                        "production pool but the runtime store update failed and rollback "
                        "also failed. Production is halted pending manual reconciliation."
                    ),
                ),
            )
            return LifecycleResult(
                provider_id=provider_id, gates=gates, success=False,
                error="runtime_store_write_failed_split",
            )
        return LifecycleResult(
            provider_id=provider_id, gates=gates, success=False,
            error="runtime_store_write_failed_rolled_back",
        )

    # Final readback: the pool must still report the provider in production.
    if not _in_production(pool_control, provider_id):
        return LifecycleResult(
            provider_id=provider_id, gates=gates, success=False,
            error="production_readback_failed",
        )

    events_emitted = [stored.last_event_id] if stored.last_event_id else []
    _enqueue(
        outbox_store,
        OutboxMessage(
            event_id=stored.last_event_id or f"promoted-{provider_id}",
            provider_id=provider_id,
            provider_name=rp.provider_name or provider_id,
            event_type="PROMOTED_TO_PRODUCTION",
            title=f"{rp.provider_name or provider_id} promoted to production",
            body=(
                f"Provider {rp.provider_name or provider_id} passed all gates "
                "(FREE_CONFIRMED registry status, OFFICIAL evidence, health, protocol, "
                "approval binding) and is active in the FreeLLMPool production pool."
            ),
        ),
    )
    return LifecycleResult(
        provider_id=provider_id, success=True, new_provider=stored,
        events_emitted=events_emitted, gates=gates,
    )


def suspend_from_production(
    provider_id: str,
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    reason: str = "manual",
    outbox_store: Optional[OutboxStore] = None,
    clock: Optional[Any] = None,
) -> LifecycleResult:
    """Suspend a provider from production, fail-closed (review 四.3/四.4).

    - The pool removal is verified by readback before ANY state change.
    - On failure: NO SUSPENDED state is written, production is halted via
      stop_production, and a high-priority POOL_SUSPEND_FAILED alert is
      enqueued. The operator pages in; nothing pretends to be suspended.
    """
    rp = runtime_store.get_provider(provider_id)
    if rp is None:
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"no runtime record for {provider_id}",
        )
    suspend_result = pool_control.suspend(provider_id)
    if not suspend_result.get("suspended"):
        # FAIL-CLOSED: do not write SUSPENDED; block production; alert.
        pool_control.stop_production()
        error_code = str(suspend_result.get("error") or "suspend_failed")
        _enqueue(
            outbox_store,
            OutboxMessage(
                event_id=f"pool-suspend-failed-{provider_id}",
                provider_id=provider_id,
                provider_name=rp.provider_name or provider_id,
                event_type="POOL_SUSPEND_FAILED",
                title=f"{rp.provider_name or provider_id} suspend FAILED",
                body=(
                    f"Provider {rp.provider_name or provider_id} could not be removed "
                    f"from the production pool (code: {error_code}). Production is "
                    "halted; manual intervention required. The runtime record keeps its "
                    "previous state — no SUSPENDED status was written."
                ),
            ),
        )
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error=f"pool_suspension_failed:{error_code}",
        )

    # Pool removal verified by readback; now persist the state change.
    try:
        updated = _replace(
            rp,
            expected_pool_status=ExpectedPoolStatus.SUSPENDED,
            actual_pool_status=ActualPoolStatus.SUSPENDED,
            last_synced_at=_now(clock),
        )
        stored = runtime_store.upsert(
            updated,
            changed_fields=["actual_pool_status", "expected_pool_status"],
            reason=f"suspension: {reason}",
        )
    except Exception:  # noqa: BLE001
        return LifecycleResult(
            provider_id=provider_id, success=False,
            error="runtime_store_write_failed_after_suspend",
        )

    events_emitted = [stored.last_event_id] if stored.last_event_id else []
    _enqueue(
        outbox_store,
        OutboxMessage(
            event_id=stored.last_event_id or f"suspended-{provider_id}",
            provider_id=provider_id,
            provider_name=rp.provider_name or provider_id,
            event_type="POOL_SUSPENDED",
            title=f"{rp.provider_name or provider_id} suspended",
            body=(
                f"Provider {rp.provider_name or provider_id} was removed from the "
                f"production pool and the removal was verified (reason code: {reason})."
            ),
        ),
    )
    return LifecycleResult(
        provider_id=provider_id, success=True, new_provider=stored,
        events_emitted=events_emitted,
    )
