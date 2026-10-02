"""Auto-suspend — periodic health re-check with consecutive-failure threshold (Stage 5).

When a provider in production fails N consecutive health checks, it is
automatically suspended.

Review round 2 hardening (六):
- ``check_interval_seconds`` is validated (>= 1 second) at policy
  construction; nonsense intervals fail closed with ValueError.
- Failure counters are PERSISTED (atomic JSON state file) so a restart does
  not reset the failure history mid-incident.
- The counter for a provider resets ONLY after a CONFIRMED suspend (the pool
  actually removed the provider). A failed suspend keeps the count — and the
  suspend failure path in the orchestrator already halts production and
  alerts, so the next cycle re-attempts instead of silently forgetting.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from hunter.pool_control import PoolControl
from hunter.runtime.models import ActualPoolStatus, HealthStatus
from hunter.runtime.store import RuntimeStore
from hunter.runtime.orchestrator import (
    LifecycleResult,
    run_health_check,
    suspend_from_production,
)

STATE_FILE_NAME = "auto_suspend_state.json"


@dataclass
class AutoSuspendPolicy:
    """Policy governing automatic suspension of unhealthy production providers."""

    max_consecutive_failures: int = 3
    check_interval_seconds: int = 300
    excluded_providers: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        # Review 六.5: reject invalid intervals (< 1 second) at construction.
        if int(self.check_interval_seconds) < 1:
            raise ValueError("check_interval_seconds must be >= 1")
        if not isinstance(self.check_interval_seconds, int):
            raise ValueError("check_interval_seconds must be an integer")

    def is_excluded(self, provider_id: str) -> bool:
        return provider_id in self.excluded_providers


class AutoSuspendTracker:
    """Tracks consecutive health failures per provider, persisted to disk.

    The state file is written atomically (temp + fsync + replace) on every
    mutation so a crash or restart cannot lose the failure history.
    """

    def __init__(self, policy: AutoSuspendPolicy, state_path: Optional[Path] = None):
        self.policy = policy
        self._state_path = Path(state_path) if state_path is not None else None
        self._failures: Dict[str, int] = {}
        if self._state_path is not None:
            self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt state fails CLOSED: keep whatever we have in memory
            # (empty) — a lost counter only delays suspension by one cycle,
            # it never fakes health.
            return
        if not isinstance(data, dict):
            return
        counts = data.get("consecutive_failures", {})
        if isinstance(counts, dict):
            self._failures = {
                str(k): int(v) for k, v in counts.items() if isinstance(v, int)
            }

    def _persist(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"consecutive_failures": dict(self._failures)}
        fd, tmp = tempfile.mkstemp(
            dir=str(self._state_path.parent),
            prefix=f".{self._state_path.name}-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps(payload, indent=2).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- tracking ------------------------------------------------------------

    def record_outcome(self, provider_id: str, healthy: bool) -> None:
        """Record a health check outcome; healthy clears the failure count."""
        if healthy:
            if provider_id in self._failures:
                self._failures.pop(provider_id, None)
                self._persist()
        else:
            self._failures[provider_id] = self._failures.get(provider_id, 0) + 1
            self._persist()

    def should_suspend(self, provider_id: str) -> bool:
        if self.policy.is_excluded(provider_id):
            return False
        return self._failures.get(provider_id, 0) >= self.policy.max_consecutive_failures

    def consecutive_failures(self, provider_id: str) -> int:
        return self._failures.get(provider_id, 0)

    def reset(self, provider_id: str) -> None:
        """Clear the counter (persisted)."""
        if provider_id in self._failures:
            self._failures.pop(provider_id, None)
            self._persist()


def run_auto_suspend_cycle(
    runtime_store: RuntimeStore,
    pool_control: PoolControl,
    tracker: AutoSuspendTracker,
    outbox_store=None,
) -> List[LifecycleResult]:
    """Run one auto-suspend cycle: health check all production providers,
    track failures, suspend any that hit the threshold.

    Review 六.5: the failure counter resets ONLY after a CONFIRMED suspend
    (``suspend_result.success`` and the provider actually leaving the
    production catalog). A failed suspend keeps the count so the next cycle
    re-attempts — production is already halted by the fail-closed suspend
    path, so no unknown provider keeps serving.
    """
    results: List[LifecycleResult] = []
    for rp in runtime_store.list_providers():
        pid = rp.provider_id
        if rp.actual_pool_status != ActualPoolStatus.PRODUCTION:
            continue

        hc_result = run_health_check(pid, runtime_store, pool_control)
        checked = runtime_store.get_provider(pid)
        healthy = hc_result.success and checked is not None and checked.health_status == HealthStatus.HEALTHY

        tracker.record_outcome(pid, healthy)

        if tracker.should_suspend(pid):
            suspend_result = suspend_from_production(
                pid,
                runtime_store,
                pool_control,
                reason="auto_suspend_consecutive_failures",
                outbox_store=outbox_store,
            )
            if suspend_result.success and pid not in pool_control.list_production():
                tracker.reset(pid)
            results.append(suspend_result)
        else:
            results.append(hc_result)

    return results
