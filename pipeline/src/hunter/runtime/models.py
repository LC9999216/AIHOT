"""Runtime provider model (Stage 2, AGENTS_STAGE2.md §8).

Six separated state planes:
- credential_status:  NOT_CONFIGURED | SETUP_REQUIRED | CONFIGURED
- health_status:      UNKNOWN | HEALTHY | RATE_LIMITED | EXHAUSTED | INVALID_KEY | DOWN
- protocol_status:    per-protocol classification (chat, responses, streaming, tools)
- expected_pool_status: DISABLED | STAGING | PROMOTION_PENDING | PRODUCTION | SUSPENDED
- actual_pool_status: UNKNOWN | STAGING | PRODUCTION | SUSPENDED
- approval_status:    PENDING | APPROVED | REJECTED | EXPIRED

Never store raw keys, authorization headers, cookies, or full upstream responses.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..registry.schema import ProviderStatus


class CredentialStatus(str, enum.Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    CONFIGURED = "CONFIGURED"


class HealthStatus(str, enum.Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    RATE_LIMITED = "RATE_LIMITED"
    EXHAUSTED = "EXHAUSTED"
    INVALID_KEY = "INVALID_KEY"
    DOWN = "DOWN"


@dataclass(frozen=True)
class ProtocolResult:
    """Result of one protocol conformance test."""
    chat: str = "unchecked"            # pass | fail | unchecked
    responses: str = "unchecked"
    streaming: str = "unchecked"
    tools: str = "unchecked"
    checked_at: Optional[datetime] = None

    def all_pass(self) -> bool:
        return (
            self.chat == "pass"
            and self.responses == "pass"
            and self.streaming == "pass"
            and self.tools == "pass"
        )


class ExpectedPoolStatus(str, enum.Enum):
    DISABLED = "DISABLED"
    STAGING = "STAGING"
    PROMOTION_PENDING = "PROMOTION_PENDING"
    PRODUCTION = "PRODUCTION"
    SUSPENDED = "SUSPENDED"


class ActualPoolStatus(str, enum.Enum):
    UNKNOWN = "UNKNOWN"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"
    SUSPENDED = "SUSPENDED"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class ApprovalBinding:
    """Binding that a previous approval was based on (changes invalidate it)."""
    provider_id: str
    registry_revision: int
    evidence_digest: str           # sha256 of evidence_ids + content_fingerprints
    free_score_snapshot: int
    eligibility_policy_version: str = "v1"
    health_result: str = ""
    protocol_result: str = ""
    approved_by: str = ""
    approved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RuntimeProvider:
    """Runtime record for one Provider, stored in data/runtime_providers.json.

    This is NOT a Provider; it is the runtime projection of one Provider's
    operational state. The Provider's evidence-state lives in providers.json.
    """

    provider_id: str
    provider_name: str = ""
    evidence_status: Optional[str] = None           # mirrored from Registry for convenience
    credential_status: CredentialStatus = CredentialStatus.NOT_CONFIGURED
    health_status: HealthStatus = HealthStatus.UNKNOWN
    health_checked_at: Optional[datetime] = None
    protocol_result: ProtocolResult = field(default_factory=ProtocolResult)
    expected_pool_status: ExpectedPoolStatus = ExpectedPoolStatus.DISABLED
    actual_pool_status: ActualPoolStatus = ActualPoolStatus.UNKNOWN
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    approval_binding: Optional[ApprovalBinding] = None
    revision: int = 0
    last_event_id: str = ""
    last_synced_at: Optional[datetime] = None
    notified_events: List[str] = field(default_factory=list)   # event_ids already notified


__all__ = [
    "CredentialStatus",
    "HealthStatus",
    "ProtocolResult",
    "ExpectedPoolStatus",
    "ActualPoolStatus",
    "ApprovalStatus",
    "ApprovalBinding",
    "RuntimeProvider",
]
