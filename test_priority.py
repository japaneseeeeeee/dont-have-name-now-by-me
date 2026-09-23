import unittest

import monitor


class PriorityTests(unittest.TestCase):
    def test_old_entries_default_to_normal(self):
        self.assertEqual(monitor.effective_priority({"label": "JA0001"}, now=100), "NORMAL")

    def test_active_temporary_special(self):
        entry = {"priority": "SPECIAL", "special_until": 200, "priority_after_special": "WATCH"}
        self.assertEqual(monitor.effective_priority(entry, now=100), "SPECIAL")

    def test_expired_temporary_special_restores_previous_priority(self):
        entry = {"priority": "SPECIAL", "special_until": 100, "priority_after_special": "WATCH"}
        self.assertEqual(monitor.effective_priority(entry, now=200), "WATCH")

    def test_invalid_priority_is_normal(self):
        self.assertEqual(monitor.effective_priority({"priority": "urgent"}, now=100), "NORMAL")

    def test_special_embed_is_red_and_has_reason(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.0, 1000, False, 100, 90, 0]
        embed = monitor.build_embed(
            "abc123",
            {"label": "JA0001", "type": "TEST", "priority": "SPECIAL"},
            aircraft,
        )
        self.assertEqual(embed["color"], monitor.COLOR_SPECIAL)
        self.assertIn("SPECIAL", embed["title"])
        self.assertTrue(any(field["name"] == "検出理由" for field in embed["fields"]))


if __name__ == "__main__":
    unittest.main()
