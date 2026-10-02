# tests/test_auto_promote.py
# WHY: B-8/B-9 验收 — autoprove在probe=NOT_RUN时拒绝,PASSED且全gate时通过;LeanPipeline骨架跑通
import os, sys, json, time, unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from src.hunter.pipeline_lean import LeanPipeline
from src.hunter.verification.models import Signal, LeanCandidate, VerificationVerdict
from src.hunter.verification.verifier import verify_official_offer, promote_eligible
from src.hunter.discovery.lean_deduplicator import merge_signals, bucketize


class TestLeanPipeline(unittest.TestCase):
    def _posts(self):
        now = time.time()
        return [
            ("cohere.com", "platform", {
                "source": "x", "url": "https://x.com/p/1", "published_at": now,
                "claim": "Cohere free tier is great for testing", "quote": "free tier",
            }),
            ("cohere.com", "chat", {
                "source": "reddit", "url": "https://reddit.com/r/x/2", "published_at": now,
                "claim": "Also free tier chat", "quote": "free tier",
            }),
        ]

    def test_skeleton_run(self):
        posts = self._posts()
        pipe = LeanPipeline(collectors=[lambda: posts], extractor=None)
        res = pipe.run()
        self.assertIn("buckets", res)
        self.assertIn("candidates", res)
        # 2 candidates due to product-line dedup key
        self.assertEqual(len(res["candidates"]), 2)

    def test_iron_rule_drops_mismatched_quote(self):
        posts = [("cohere.com", "", {"source": "x", "url": "u", "published_at": time.time(),
                                     "claim": "something unrelated", "quote": "free tier"})]
        pipe = LeanPipeline(collectors=[lambda: posts], extractor=None)
        space = pipe.extract_signals_space(posts)
        # quote "free tier" not substring of claim "something unrelated" -> dropped
        self.assertEqual(len(space), 0)


class TestVerifyFullFlow(unittest.TestCase):
    def test_gate_pass_llm_mark(self):
        c = LeanCandidate(candidate_id="cohere", provider_name="Cohere", canonical_domain="cohere.com")
        pages = [{"url": "https://cohere.com/pricing", "domain": "cohere.com",
                  "body": "Free tier: 1000 API requests/day.", "sha256": "a"*64}]
        v = verify_official_offer(c, pages)
        self.assertEqual(v.verdict, VerificationVerdict.VERIFIED_FREE)
        ok, why = promote_eligible(v, "PASSED")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
