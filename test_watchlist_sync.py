import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from watchlist_sync import (
    backup_count,
    build_sync_status,
    read_json_locked,
    replace_local_watchlist,
    save_sync_status,
)


class WatchlistSyncTests(unittest.TestCase):
    def test_replace_saves_previous_copy_and_keeps_ten_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            backups = os.path.join(directory, "watchlist_backups")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"000000": {"label": "OLD"}}, stream)

            base = datetime(2026, 10, 14, tzinfo=timezone.utc)
            for index in range(12):
                data = {f"{index + 1:06x}": {"label": f"TEST-{index}"}}
                result = replace_local_watchlist(
                    path, data, backups, now=base + timedelta(seconds=index),
                )
                self.assertTrue(result["changed"])

            self.assertEqual(backup_count(backups), 10)
            self.assertEqual(
                read_json_locked(path, {}),
                {"00000c": {"label": "TEST-11"}},
            )

    def test_unchanged_copy_does_not_create_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            backups = os.path.join(directory, "watchlist_backups")
            data = {"abc123": {"label": "JA0001"}}
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            result = replace_local_watchlist(path, data, backups)
            self.assertFalse(result["changed"])
            self.assertEqual(backup_count(backups), 0)

    def test_failure_status_preserves_last_success_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist_sync_status.json")
            now = datetime(2026, 10, 14, 1, 2, 3, tzinfo=timezone.utc)
            data = {"abc123": {"label": "JA0001"}}
            success = build_sync_status(data, data, source="github", now=now)
            save_sync_status(path, success)
            failure = build_sync_status(None, data, source="local_fallback", error="offline")
            saved = save_sync_status(path, failure)
            self.assertEqual(saved["last_success_at"], success["last_success_at"])
            self.assertEqual(saved["source"], "local_fallback")
            self.assertEqual(saved["error"], "offline")


if __name__ == "__main__":
    unittest.main()
