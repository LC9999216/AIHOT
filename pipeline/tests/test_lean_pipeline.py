# tests/test_lean_pipeline.py
import os, sys, json, time, unittest
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from src.hunter.verification.models import (
    FreeClaimType, VerificationVerdict, OfficialVerification, ProbeStatus,
    LeanCandidate, Signal, freshness_bucket, ENTERABLE
)
from src.hunter.verification.verifier import (
    gate_official_domain, gate_explicit_free, gate_content_hashes,
    verify_official_offer, promote_eligible,
)
from src.hunter.verification.official_finder import is_official_domain
from src.hunter.discovery.lean_deduplicator import dedup_key, merge_signals, bucketize

class TestModels(unittest.TestCase):
    def test_roundtrip(self):
        s = Signal(source="x", url="https://x.com/a/1", published_at=time.time(), claim="free api", evidence_quote="free api")
        c = LeanCandidate(candidate_id="cohere::", provider_name="Cohere", canonical_domain="cohere.com", signals=[s])
        d = json.loads(json.dumps(c.to_json()))
        self.assertEqual(d["canonical_domain"], "cohere.com")
        self.assertEqual(d["signals"][0]["source"], "x")

    def test_freshness_buckets(self):
        now = time.time()
        self.assertEqual(freshness_bucket(now - 3600, now), "HOT")
        self.assertEqual(freshness_bucket(now - 48*3600, now), "RECENT")
        self.assertEqual(freshness_bucket(now - 100*3600, now), "WEEK")
        self.assertEqual(freshness_bucket(now - 400*3600, now), "OLD")

class TestVerifier(unittest.TestCase):
    def _pages(self, domain):
        return [{"url": f"https://{domain}/pricing", "domain": domain,
                 "body": "Free tier: 1000 API requests/day. No credit card required.",
                 "sha256": "a"*64}]

    def test_gates_pass(self):
        c = LeanCandidate(candidate_id="x", provider_name="X", canonical_domain="prov.com")
        p = self._pages("prov.com")
        self.assertTrue(gate_official_domain(c, p))
        self.assertTrue(gate_explicit_free(p))
        self.assertTrue(gate_content_hashes(p))

    def test_paid_only_fails_gate2(self):
        c = LeanCandidate(candidate_id="x", provider_name="X", canonical_domain="prov.com")
        p = [{"url":"https://prov.com/pricing","domain":"prov.com","body":"Contact sales for API pricing.","sha256":"b"*64}]
        self.assertFalse(gate_explicit_free(p))

    def test_spoof_domain_fails_gate1(self):
        c = LeanCandidate(candidate_id="x", provider_name="X", canonical_domain="prov.com")
        p = self._pages("prov-ai.net")
        self.assertFalse(gate_official_domain(c, p))

    def test_promote_requires_probe_passed(self):
        v = OfficialVerification(provider_id="p", verdict=VerificationVerdict.VERIFIED_FREE,
                                 content_hashes=["a"*64])
        ok, why = promote_eligible(v, "NOT_RUN")
        self.assertFalse(ok)
        ok, why = promote_eligible(v, "PASSED")
        self.assertTrue(ok)

class TestOfficialFinder(unittest.TestCase):
    def test_subdomain_official(self):
        self.assertTrue(is_official_domain("cohere.com", "https://docs.cohere.com/pricing"))
        self.assertFalse(is_official_domain("cohere.com", "https://docs.cohere-ai.net/x"))
        self.assertFalse(is_official_domain("cohere.com", "https://cohere.com.evil.io/x"))

class TestDedup(unittest.TestCase):
    def test_same_domain_different_product_not_merged(self):
        now = time.time()
        sigs = [("cohere.com", "chat", Signal("x","u1",now,"c","c")),
                ("cohere.com", "platform", Signal("reddit","u2",now,"c2","c2"))]
        m = merge_signals({}, sigs)
        self.assertEqual(len(m), 2)

    def test_bucketize(self):
        now = time.time()
        sigs = [Signal("x","a",now-3600,"c","c"), Signal("x","b",now-100*3600,"c","c")]
        b = bucketize(sigs, now)
        self.assertEqual(len(b["signals_24h"]), 1)
        self.assertEqual(len(b["signals_7d"]), 1)

if __name__ == "__main__":
    unittest.main()
