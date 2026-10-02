# src/hunter/collectors/social_time.py
# WHY: B-2 — collectors 需把平台 ISO 时间戳统一转 epoch 用于 freshness 分桶
"""Unified published_at extraction (epoch seconds) for social collectors (B-2).

X v2 recent-search returns ISO-8601 created_at (tweet.fields=created_at).
Reddit JSON returns created_utc (already epoch). This helper normalizes both.
"""
import datetime
from typing import Any, Optional


def to_epoch(value: Any) -> float:
    """Convert ISO-8601 or epoch-seconds values to epoch seconds; 0.0 on failure."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return 0.0
    return 0.0


def published_at_from_x(post: dict) -> float:
    return to_epoch((post or {}).get("created_at"))


def published_at_from_reddit(post: dict) -> float:
    # Reddit jq `created_utc` is epoch seconds; `created` is epoch too
    v = (post or {}).get("created_utc") or (post or {}).get("created") or 0
    return to_epoch(v)
