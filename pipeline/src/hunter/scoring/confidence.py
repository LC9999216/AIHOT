"""Verification Confidence V1 (TASK-010; AGENTS.md 11.1).

Deterministic and clock-free: callers pass validated evidence, the scoring
configuration, and an explicit timezone-aware ``as_of``. Only OFFICIAL
evidence contributes. An unresolved contradiction hard-blocks (score 0).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Sequence

from ..evidence.models import Evidence, Officiality
from .config import ScoreConfig

SOURCE_TO_KEY = {
    "pricing": "official_pricing",
    "api-docs": "official_api_docs",
    "docs": "official_product_docs",
    "blog": "official_blog",
    "github": "official_github",
}
SOURCE_RANK = ["pricing", "api-docs", "docs", "blog", "github"]

_FREE_MARKERS = ("free", "no cost", "no-cost", "no charge", "$0", "zero cost")
_API_MARKERS = ("api", "endpoint", "programmatic", "developer", "sdk")
_AMBIGUOUS_MARKERS = ("might", "maybe", "possibly", "could be", "seems")


def clamp(value: int, config: ScoreConfig) -> int:
    limits = config.verification.get("clamp") if config.verification else None
    lo, hi = limits if limits else (0, 100)
    return max(int(lo), min(int(hi), int(value)))


def _factors(config: ScoreConfig) -> Dict[str, Any]:
    return config.verification.get("factors", {}) if config.verification else {}


def _base_points(config: ScoreConfig) -> Dict[str, Any]:
    return config.verification.get("base_points", {}) if config.verification else {}


def rank(source_type: str) -> int:
    try:
        return SOURCE_RANK.index(source_type)
    except ValueError:
        return len(SOURCE_RANK)


def verification_confidence(
    evidences: Sequence[Evidence],
    config: ScoreConfig,
    as_of: datetime,
) -> int:
    """Verification Confidence V1, clamped to 0..100."""
    official = [e for e in evidences if e.officiality is Officiality.OFFICIAL]
    if not official:
        return 0
    for e in official:
        if "contradiction" in " ".join(e.validation_notes or []).lower():
            return 0  # unresolved contradiction hard-block

    best = min(official, key=lambda e: rank(e.source_type))
    base = int(_base_points(config).get(SOURCE_TO_KEY.get(best.source_type, ""), 0) or 0)
    total = base

    claims = " ".join((e.claim or "") for e in official).lower()
    ambiguous = any(m in claims for m in _AMBIGUOUS_MARKERS)

    if not ambiguous and any(m in claims for m in _FREE_MARKERS):
        total += int(_factors(config).get("explicit_free_wording", 0) or 0)
    if any(m in claims for m in _API_MARKERS):
        total += int(_factors(config).get("explicit_programmatic_api", 0) or 0)
    if len(official) >= 2:
        total += int(_factors(config).get("second_consistent_source", 0) or 0)
    if any((e.content_excerpt or "").strip() for e in official):
        total += int(_factors(config).get("critical_fields_grounded", 0) or 0)

    age_days = (as_of - max(e.retrieved_at for e in official)).days
    if age_days <= 90:
        total += int(_factors(config).get("retrieval_within_90_days", 0) or 0)
    elif age_days > 180:
        total += int(_factors(config).get("evidence_older_180_days", 0) or 0)

    if best.effective_at is None and best.published_at is None:
        total += int(_factors(config).get("missing_usable_date", 0) or 0)
    if ambiguous:
        total += int(_factors(config).get("ambiguous_wording", 0) or 0)

    return clamp(total, config)


__all__ = ["verification_confidence", "clamp", "rank", "SOURCE_RANK", "SOURCE_TO_KEY"]
