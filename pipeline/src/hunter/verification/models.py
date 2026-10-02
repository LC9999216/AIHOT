# src/hunter/verification/models.py
# WHY: lean重构的数据模型层 - 三元断言gate替代评分gate, probe独立
"""Lean pipeline data models (B-1). Keep legacy Candidate compatible; no Officiality enum."""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, List
import time, uuid

class FreeClaimType(str, Enum):
    FREE_API = "FREE_API"
    FREE_TOKEN_CREDIT = "FREE_TOKEN_CREDIT"
    FREE_TIER = "FREE_TIER"
    FREE_TRIAL = "FREE_TRIAL"
    FREE_MODEL_WEIGHTS = "FREE_MODEL_WEIGHTS"
    FREE_CHAT_ONLY = "FREE_CHAT_ONLY"
    GIVEAWAY = "GIVEAWAY"
    UNKNOWN = "UNKNOWN"

ENTERABLE = {FreeClaimType.FREE_API, FreeClaimType.FREE_TOKEN_CREDIT,
             FreeClaimType.FREE_TIER, FreeClaimType.FREE_TRIAL}

class VerificationVerdict(str, Enum):
    VERIFIED_FREE = "VERIFIED_FREE"
    NOT_FREE = "NOT_FREE"
    UNKNOWN = "UNKNOWN"

class ProbeStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"

@dataclass
class Signal:
    source: str            # x | reddit | github | hn
    url: str
    published_at: float    # epoch seconds
    claim: str
    evidence_quote: str = ""   # iron rule: must be substring of post text

@dataclass
class LeanCandidate:
    candidate_id: str
    provider_name: str
    canonical_domain: str
    signals: List[Signal] = field(default_factory=list)
    discovered_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_legacy(cand: dict) -> "LeanCandidate":
        return LeanCandidate(
            candidate_id=cand.get("candidate_id") or cand.get("id") or str(uuid.uuid4()),
            provider_name=cand.get("provider_name") or cand.get("name", "unknown"),
            canonical_domain=cand.get("canonical_domain") or "",
            signals=[],
        )

@dataclass
class OfficialVerification:
    provider_id: str
    verdict: VerificationVerdict
    free_type: FreeClaimType = FreeClaimType.UNKNOWN
    quota: str = ""
    renewal: str = ""
    models: List[str] = field(default_factory=list)
    official_urls: List[str] = field(default_factory=list)
    content_hashes: List[str] = field(default_factory=list)
    summary: str = ""
    verified_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["free_type"] = self.free_type.value
        return d

def freshness_bucket(published_at: float, now: Optional[float] = None) -> str:
    now = now if now is not None else time.time()
    h = (now - published_at) / 3600.0
    if h < 24: return "HOT"
    if h < 72: return "RECENT"
    if h < 168: return "WEEK"
    return "OLD"
