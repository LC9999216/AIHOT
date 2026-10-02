# tests/test_social_time.py
import os, sys, time, unittest
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from src.hunter.collectors.social_time import (
    to_epoch, published_at_from_x, published_at_from_reddit,
)

class TestSocialTime(unittest.TestCase):
    def test_x_iso_to_epoch(self):
        # X v2 ISO-8601 with Z
        e = published_at_from_x({"created_at": "2026-09-22T07:00:00.000Z"})
        self.assertGreater(e, 1_700_000_000)

    def test_reddit_epoch_passthrough(self):
        e = published_at_from_reddit({"created_utc": 1_700_000_000})
        self.assertEqual(e, 1_700_000_000)

    def test_garbage_returns_zero(self):
        self.assertEqual(to_epoch("not-a-date"), 0.0)
        self.assertEqual(to_epoch(None), 0.0)

    def test_freshness_integration(self):
        from src.hunter.verification.models import freshness_bucket
        now = time.time()
        e = published_at_from_reddit({"created_utc": now - 3600})
        self.assertEqual(freshness_bucket(e, now), "HOT")

if __name__ == "__main__":
    unittest.main()
