import gzip
import os
import tempfile
import unittest
from datetime import date

from lifecycle_review import (
    expired_liveries,
    registrations_in_tar1090,
    update_retirement_observations,
)


class LifecycleReviewTests(unittest.TestCase):
    def test_only_expired_livery_is_returned(self):
        data = {
            "JA0001": {"name": "Ended", "valid_until": "2026-10-10"},
            "JA0002": {"name": "Current", "valid_until": "2026-10-11"},
            "JA0003": {"name": "No date"},
        }
        result = expired_liveries(data, date(2026, 10, 11))
        self.assertEqual([item["registration"] for item in result], ["JA0001"])

    def test_tar1090_scan_normalizes_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "aircraft.csv.gz")
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.write("abc123;JA-0001;B738;;Boeing 737-800\n")
            self.assertEqual(registrations_in_tar1090(path, ["ja0001", "JA9999"]), {"JA0001"})

    def test_never_seen_aircraft_is_not_called_retired(self):
        watchlist = {"abc123": {"label": "JA0001", "type": "TEST"}}
        candidates, state = update_retirement_observations(
            watchlist, set(), {}, today=date(2026, 10, 11), threshold_days=30,
        )
        self.assertEqual(candidates, [])
        self.assertFalse(state["retirement"]["abc123"].get("ever_seen", False))

    def test_seen_then_missing_for_30_days_becomes_candidate_once(self):
        watchlist = {"abc123": {"label": "JA0001", "type": "TEST"}}
        _, state = update_retirement_observations(
            watchlist, {"JA0001"}, {}, today=date(2026, 9, 1), threshold_days=30,
        )
        _, state = update_retirement_observations(
            watchlist, set(), state, today=date(2026, 9, 2), threshold_days=30,
        )
        candidates, state = update_retirement_observations(
            watchlist, set(), state, today=date(2026, 10, 2), threshold_days=30,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["icao24"], "abc123")
        again, _ = update_retirement_observations(
            watchlist, set(), state, today=date(2026, 10, 3), threshold_days=30,
        )
        self.assertEqual(again, [])

    def test_keep_snooze_can_trigger_a_later_review(self):
        watchlist = {"abc123": {"label": "JA0001", "type": "TEST"}}
        state = {"retirement": {"abc123": {
            "registration": "JA0001", "ever_seen": True,
            "missing_since": "2026-01-01", "notified_for": "2026-01-01",
            "next_review_on": "2026-10-11",
        }}}
        candidates, _ = update_retirement_observations(
            watchlist, set(), state, today=date(2026, 10, 11), threshold_days=30,
        )
        self.assertEqual(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
