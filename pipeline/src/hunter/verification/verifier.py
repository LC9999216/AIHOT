# src/hunter/verification/verifier.py
# WHY: 三元断言机械gate在前,LLM阅读在后 - 防结构化幻觉污染verdict (B-7)
"""Three binary assertion gates + LLM structured read. No numeric score gates."""
from typing import List, Tuple
from .models import OfficialVerification, VerificationVerdict, FreeClaimType

def gate_official_domain(candidate, pages) -> bool:
    return any((p.get("domain") or "") == (candidate.canonical_domain or "") for p in pages)

def gate_explicit_free(pages) -> bool:
    for p in pages:
        body = (p.get("body") or "").lower()
        if "free" in body and ("api" in body or "tier" in body or "credit" in body):
            return True
    return False

def gate_content_hashes(pages) -> bool:
    return all(p.get("sha256") for p in pages)

def verify_official_offer(candidate, pages, llm_read=None) -> OfficialVerification:
    g1 = gate_official_domain(candidate, pages)
    g2 = gate_explicit_free(pages)
    g3 = gate_content_hashes(pages)
    if not (g1 and g2 and g3):
        return OfficialVerification(provider_id=candidate.candidate_id,
                                    verdict=VerificationVerdict.UNKNOWN,
                                    summary=f"gates: domain={g1} free={g2} hash={g3}")
    if llm_read is None:
        return OfficialVerification(provider_id=candidate.candidate_id,
                                    verdict=VerificationVerdict.VERIFIED_FREE,
                                    free_type=FreeClaimType.FREE_TIER,
                                    official_urls=[p.get("url","") for p in pages],
                                    content_hashes=[p.get("sha256","") for p in pages],
                                    summary="mechanical gates passed; llm_read not provided")
    return llm_read(candidate, pages)

def promote_eligible(v: OfficialVerification, probe_status: str) -> Tuple[bool, str]:
    if v.verdict != VerificationVerdict.VERIFIED_FREE:
        return False, "verdict_not_verified_free"
    if probe_status != "PASSED":
        return False, f"probe_{probe_status.lower()}_blocks_pool_entry"
    if not v.content_hashes:
        return False, "missing_content_hash"
    return True, "ok"
