"""Free Score V1 (TASK-010; AGENTS.md 11.2).

Expired offers always score 0. Unknown values contribute 0; nothing is inferred
from missing evidence. Deterministic and clock-free: callers pass an explicit
timezone-aware ``as_of``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Sequence

from ..registry.schema import FreeOffer, OfferStatus
from .config import ScoreConfig


def _free_config(config: ScoreConfig) -> Dict[str, Any]:
    return config.free_score or {}


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def free_score(
    offer: FreeOffer,
    requirements: Any,
    api: Any,
    models: Sequence[str],
    has_documented_limits: bool,
    _config: ScoreConfig,
    as_of: Any,
) -> int:
    """Free Score V1, clamped to 0..100."""
    cfg = _free_config(_config)
    if offer.offer_status is OfferStatus.expired:
        return 0

    total = 0
    buckets = cfg.get("time_buckets", {})

    if offer.expires_at is None:
        total += int(buckets.get("ongoing", 0) or 0)
    else:
        days = (offer.expires_at - _as_datetime(as_of)).days
        if days > 90:
            total += int(buckets.get("expiry_gt_90_days", 0) or 0)
        elif days >= 31:
            total += int(buckets.get("expiry_31_to_90_days", 0) or 0)
        elif days >= 8:
            total += int(buckets.get("expiry_8_to_30_days", 0) or 0)
        elif days >= 0:
            total += int(buckets.get("expiry_0_to_7_days", 0) or 0)

    quota = offer.quota_mode.value if offer.quota_mode else ""
    total += int(cfg.get("quota_modes", {}).get(quota, 0) or 0)

    access = offer.access_method.value if offer.access_method else ""
    total += int(cfg.get("access_methods", {}).get(access, 0) or 0)

    requirements_cfg = cfg.get("requirements", {})
    if requirements is not None:
        if requirements.card_required is True:
            total += int(requirements_cfg.get("card_required", 0) or 0)
        elif requirements.card_required is False:
            total += int(requirements_cfg.get("no_card", 0) or 0)
        if requirements.phone_required is True:
            total += int(requirements_cfg.get("phone_required", 0) or 0)
        elif requirements.phone_required is False:
            total += int(requirements_cfg.get("no_phone", 0) or 0)
        if requirements.regional_restrictions:
            total += int(cfg.get("regional", {}).get("material_restriction", 0) or 0)

    if api is not None and api.openai_compatible is True:
        total += int(cfg.get("api", {}).get("openai_compatible", 0) or 0)

    evidence_cfg = cfg.get("evidence", {})
    if models:
        total += int(evidence_cfg.get("current_model_list", 0) or 0)
    if has_documented_limits:
        total += int(evidence_cfg.get("documented_limits", 0) or 0)

    commercial_cfg = cfg.get("commercial", {})
    if requirements is not None and requirements.commercial_use_allowed is True:
        total += int(commercial_cfg.get("allowed", 0) or 0)
    elif requirements is not None and requirements.commercial_use_allowed is False:
        total += int(commercial_cfg.get("forbidden", 0) or 0)

    limits = cfg.get("clamp") or [0, 100]
    lo, hi = limits
    return max(int(lo), min(int(hi), total))


__all__ = ["free_score"]
