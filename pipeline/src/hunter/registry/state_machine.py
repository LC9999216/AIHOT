"""Provider State Machine (AGENTS.md section 6).

Enforces the ordinary transition graph. The generic ``transition`` API must
never accept ``FREE_CONFIRMED`` as a destination — only ``confirm_provider()``
(TASK-010) may perform ``EVIDENCE_VERIFIED -> FREE_CONFIRMED``. Every
successful transition persists through the Registry Store and creates one
history event. Illegal or failed transitions change nothing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from .schema import Provider, ProviderStatus
from .store import ProviderRegistry, RegistryError


class TransitionError(Exception):
    """Raised for illegal, insufficient, or malformed state transitions."""


def allowed_transitions() -> Dict[ProviderStatus, Set[ProviderStatus]]:
    """The ordinary transition graph from AGENTS.md section 6."""
    return {
        ProviderStatus.DISCOVERED: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.EVIDENCE_PENDING: {
            ProviderStatus.EVIDENCE_VERIFIED,
            ProviderStatus.UNCERTAIN,
            ProviderStatus.NOT_FREE,
            ProviderStatus.REJECTED,
        },
        ProviderStatus.EVIDENCE_VERIFIED: {
            ProviderStatus.UNCERTAIN,
            ProviderStatus.NOT_FREE,
        },
        ProviderStatus.FREE_CONFIRMED: {
            ProviderStatus.EXPIRED,
            ProviderStatus.UNCERTAIN,
        },
        ProviderStatus.UNCERTAIN: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.EXPIRED: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.NOT_FREE: {ProviderStatus.EVIDENCE_PENDING},
        ProviderStatus.REJECTED: set(),
    }


class StateMachine:
    """Applies and persists ordinary provider state transitions."""

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def transition(
        self,
        provider: Provider,
        new_state: ProviderStatus,
        reason: str,
        source_metadata: Optional[Dict[str, Any]],
    ) -> Provider:
        if not isinstance(provider, Provider):
            raise TransitionError("transition requires a Provider instance")

        if new_state is ProviderStatus.FREE_CONFIRMED:
            raise TransitionError(
                "FREE_CONFIRMED cannot be reached through the generic transition API; "
                "use confirm_provider()"
            )

        if not isinstance(new_state, ProviderStatus):
            raise TransitionError(f"invalid destination state {new_state!r}")

        current = provider.status
        allowed = allowed_transitions().get(current, set())
        if new_state not in allowed:
            raise TransitionError(
                f"illegal transition {current.value} -> {new_state.value}"
            )

        if not reason or not str(reason).strip():
            raise TransitionError("every transition requires a non-empty reason")
        if source_metadata is None:
            raise TransitionError("every transition requires source metadata")

        updated = provider.model_copy(update={"status": new_state})
        try:
            return self.registry.upsert_provider(
                updated,
                reason=reason,
                source_metadata=source_metadata,
                event_type="provider.transition",
            )
        except RegistryError as exc:
            raise TransitionError(f"transition failed to persist: {exc}") from exc
