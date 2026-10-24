"""Quota tracking: Pacific-day boundaries, counting, and limits learned from Google's 429 messages."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.usage import QuotaTracker, next_reset, quota_day  # noqa: E402


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


GOOGLE_429 = ("[429]: You exceeded your current quota, please check your plan and billing details. "
              "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
              "limit: 20, model: gemini-3.6-flash")


class PacificDayTests(unittest.TestCase):
    def test_day_follows_pacific_time(self) -> None:
        # 2026-10-24 06:30 UTC is still 23:30 on the 23rd in California (PDT, UTC-7)
        self.assertEqual(quota_day(ts("2026-10-24T06:30:00")), "2026-10-23")
        self.assertEqual(quota_day(ts("2026-10-24T07:30:00")), "2026-10-24")

    def test_reset_is_next_pacific_midnight(self) -> None:
        self.assertEqual(next_reset(ts("2026-10-24T18:00:00")), ts("2026-10-25T07:00:00"))  # PDT
        self.assertEqual(next_reset(ts("2026-12-01T18:00:00")), ts("2026-12-02T08:00:00"))  # PST
        # the night the clocks go back (2026-11-01), midnight is already PST
        self.assertEqual(next_reset(ts("2026-10-31T18:00:00")), ts("2026-11-01T07:00:00"))
        self.assertEqual(next_reset(ts("2026-11-01T18:00:00")), ts("2026-11-02T08:00:00"))


class TrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.q = QuotaTracker(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_gateway_and_direct_names_share_one_count(self) -> None:
        now = ts("2026-10-24T18:00:00")
        self.q.record("gemini/gemini-3.6-flash", now)
        self.q.record("gemini-3.6-flash", now)
        row = self.q.snapshot(["gemini/gemini-3.6-flash"], now)["models"][0]
        self.assertEqual((row["model"], row["used"], row["state"]), ("gemini-3.6-flash", 2, "ready"))

    def test_quota_error_teaches_limit_and_blocks_until_reset(self) -> None:
        now = ts("2026-10-24T18:00:00")
        self.q.rejected("gemini/gemini-3.6-flash", GOOGLE_429, retry_after=1800, now=now)
        row = self.q.snapshot(["gemini-3.6-flash"], now)["models"][0]
        self.assertEqual(row["limit"], 20)
        self.assertEqual(row["state"], "out")
        self.assertEqual(row["until"], ts("2026-10-25T07:00:00"))
        # a new quota day frees it again, and the learned limit is kept
        later = self.q.snapshot(["gemini-3.6-flash"], ts("2026-10-25T07:00:01"))["models"][0]
        self.assertEqual((later["state"], later["limit"], later["used"]), ("ready", 20, 0))

    def test_success_clears_a_block(self) -> None:
        now = ts("2026-10-24T18:00:00")
        self.q.rejected("gemini-3.5-flash", "429 Resource exhausted, retry later", retry_after=30, now=now)
        self.assertEqual(self.q.snapshot(["gemini-3.5-flash"], now)["models"][0]["state"], "out")
        self.q.record("gemini-3.5-flash", now + 5)
        self.assertEqual(self.q.snapshot(["gemini-3.5-flash"], now + 5)["models"][0]["state"], "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
