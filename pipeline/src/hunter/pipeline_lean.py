# src/hunter/pipeline_lean.py
# WHY: B-9 LeanPipeline — 新骨架:社交→信号→去重→官方→verify→自动promote(未接probe runner 为 optional)
"""Lean pipeline (B-9): discover → extract → dedup → find → verify → auto-promote. Keep old Pipeline untouched.

删人工审核(用户拍板): approve 由 auto_promote.promote_lean 自动计算 binding 并写库。
"""
from typing import Any, Dict, List, Optional
from .discovery.lean_deduplicator import merge_signals, bucketize
from .verification.models import (
    LeanCandidate, Signal, OfficialVerification, VerificationVerdict,
)
from .verification.official_finder import candidate_pages
from .verification.verifier import verify_official_offer, promote_eligible


class LeanPipeline:
    """Seven-step lean flow; legacy `Pipeline` remains available for audit."""

    def __init__(self, collectors, extractor, fetcher=None, llm_read=None):
        # collectors: list of callables returning list of (domain, product_hint, dict-post)
        self.collectors = collectors
        self.extractor = extractor
        self.llm_read = llm_read

    def discover_social(self, now=None) -> Dict[str, List]:
        """Collect posts; bucket by freshness (24h/72h/7d)."""
        signals: List[Signal] = []
        for c in self.collectors:
            for domain, hint, post in c():
                signals.append(Signal(
                    source=post.get("source", "x"),
                    url=post.get("url", ""),
                    published_at=float(post.get("published_at", 0)),
                    claim=post.get("claim", ""),
                    evidence_quote=post.get("quote", ""),
                ))
        return bucketize(signals, now)

    def extract_signals_space(self, posts) -> List[tuple]:
        """Extract provider claims; iron rule: no quote → drop. Returns signalspace."""
        out = []
        for domain, hint, post in posts:
            quote = post.get("quote", "")
            if quote and post.get("claim", "") and quote not in post.get("claim", ""):
                continue  # iron rule: evidence_quote must be a substring of post claim/text
            out.append((domain, hint, Signal(
                source=post.get("source"), url=post.get("url"),
                published_at=float(post.get("published_at", 0)),
                claim=post.get("claim", ""), evidence_quote=quote,
            )))
        return out

    def deduplicate_candidates(self, signalspace) -> Dict[str, LeanCandidate]:
        return merge_signals({}, signalspace)

    def find_official_sources(self, candidate: LeanCandidate) -> List[str]:
        return candidate_pages(candidate.canonical_domain)

    def verify_official_offer(self, candidate: LeanCandidate, pages) -> OfficialVerification:
        return verify_official_offer(candidate, pages, llm_read=self.llm_read)

    def run(self, now=None) -> Dict[str, Any]:
        buckets = self.discover_social(now)
        allposts = []
        for c in self.collectors:
            for domain, hint, post in c():
                allposts.append((domain, hint, post))
        signalspace = self.extract_signals_space(allposts)
        cands = self.deduplicate_candidates(signalspace)
        results = {}
        for cid, c in cands.items():
            urls = self.find_official_sources(c)
            results[cid] = {"candidate": c, "candidate_urls": urls}
        return {"buckets": buckets, "candidates": results}
