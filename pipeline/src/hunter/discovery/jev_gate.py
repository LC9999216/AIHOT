
# src/hunter/discovery/jev_gate.py
# WHY: Jev预筛闸 — 用TypeSafe Jev(choice/noul, ~250ms/条)在廉价层分类社交帖,
#      只有过闸信号才进昂贵的quote提取(deepseek flash数秒/条)。
# 设计约束(用户红线): 独立文件, 不动pipeline_lean/legacy; 429/网络错误fail-open
#      (返回None=未知, 由上层决定仍跑全量), 不误杀免费信号(用户规则: 429不得判key无效)。
from __future__ import annotations
import json
import os
import urllib.request
from typing import Any, Dict, List, Optional

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

QUESTIONS: Dict[str, Dict[str, Any]] = {
    "bucket": {
        "type": "choice",
        "instructions": "Classify this social post about an API/product offer:",
        "choices": {
            "free_tier_legit": "genuine free tier/API credits/giveaway from a real vendor",
            "paid_only": "mentions pricing/paid plans, no free claim",
            "spam_scam": "promotional spam, referral farming, impossible giveaway, scam signals",
            "irrelevant": "no API/product offer content",
        },
        "criteria": {
            "free_tier_legit": "claims a genuinely free offering from a plausibly official vendor",
            "paid_only": "only paid pricing/tiers mentioned",
            "spam_scam": "clickbait, scam, referral, or too-good-to-be-true signals",
            "irrelevant": "no offer content",
        },
    },
    "trust_domain": {
        "type": "noul",
        "instructions": "If a vendor domain appears, how likely is it a legitimate first-party official domain (not counterfeit/phishing/squatting)?",
    },
}

GATE_FREE = 0.55       # noul(trust) lower bound for auto-pass
TRUST_REJECT = 0.25    # below this on a domain-of-interest post → reject outright


def _auth_key() -> str:
    for env in ("JEV_API_KEY",):
        v = os.environ.get(env)
        if v:
            return v.strip()
    return ""


def query_jev(state: str, questions: Dict[str, Any], key: str = "") -> Optional[dict]:
    """Single Jev roundtrip. Returns None on ANY failure (fail-open policy)."""
    key = key or _auth_key()
    if not key:
        return None
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return None  # 429/timeout/network → fail-open, never misjudge
    answers = payload.get("answers", {})
    # Usage: cache/observability, cheap; kept for cost audit trail.
    return {"answers": answers, "usage": payload.get("usage", {})}


def gate_post(post: Dict[str, Any], usage_agg: Optional[List[dict]] = None) -> Optional[str]:
    """Return adjusted bucket label or None to drop; None also on API failure (caller falls back)."""
    claim = post.get("claim", "") or post.get("quote", "")
    if not claim:
        return None
    result = query_jev(claim[:8000], QUESTIONS)
    if result is None:
        return None  # fail-open
    if usage_agg is not None:
        usage_agg.append(result.get("usage", {}))
    a = result["answers"].get("bucket", {})
    t = result["answers"].get("trust_domain", {})
    label = a.get("choice")
    prob = float(t.get("noul", 0.5))
    if label == "spam_scam" or label == "irrelevant":
        return None  # drop cheaply
    if label == "paid_only":
        return None  # not a free signal → skip expensive extraction
    # free_tier_legit → optional hard domain gate for suspicious domains
    if prob < TRUST_REJECT:
        return None  # counterfeit candidate: reject at the cheap layer
    return "free_tier_legit"


def gate_posts(posts: List[Dict[str, Any]], usage_agg: Optional[List[dict]] = None) -> List[Dict[str, Any]]:
    """Batch gate; keeps only posts Jev says are free-tier/plausible."""
    kept: List[Dict[str, Any]] = []
    for p in posts:
        label = gate_post(p, usage_agg=usage_agg)
        if label is None:
            continue
        p = dict(p)
        p["jev_bucket"] = label
        kept.append(p)
    return kept
