# src/hunter/discovery/deduplicator.py
# WHY: 域名+产品线复合key, 防同公司不同产品被胶水化 (B-5)
from typing import Dict, List
from ..verification.models import LeanCandidate, Signal

def dedup_key(domain: str, product_hint: str = "") -> str:
    d = (domain or "").lower().strip()
    p = (product_hint or "").lower().strip().replace(" ", "-")
    return f"{d}::{p}" if p else d

def merge_signals(candidates: Dict[str, LeanCandidate], signalspace: List[tuple]) -> Dict[str, LeanCandidate]:
    """signalspace: list of (domain, product_hint, Signal)"""
    for domain, product_hint, sig in signalspace:
        k = dedup_key(domain, product_hint)
        if k not in candidates:
            candidates[k] = LeanCandidate(candidate_id=k, provider_name=domain, canonical_domain=domain)
        candidates[k].signals.append(sig)
    return candidates

def bucketize(signals: List[Signal], now=None) -> Dict[str, List[Signal]]:
    from ..verification.models import freshness_bucket
    out = {"signals_24h": [], "signals_72h": [], "signals_7d": []}
    for s in signals:
        b = freshness_bucket(s.published_at, now)
        if b == "HOT": out["signals_24h"].append(s)
        elif b == "RECENT": out["signals_72h"].append(s)
        elif b == "WEEK": out["signals_7d"].append(s)
    return out
