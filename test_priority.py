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

    def test_route_is_labeled_as_unverified_reference(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.0, 1000, False, 100, 90, 0]
        route = {
            "origin": {"iata_code": "MXP", "municipality": "Milan"},
            "destination": {"iata_code": "JFK", "municipality": "New York"},
            "flight_iata": "DL173",
        }
        embed = monitor.build_embed(
            "abc123", {"label": "JA0001", "type": "TEST"}, aircraft, route=route,
        )
        names = [field["name"] for field in embed["fields"]]
        self.assertIn("区間(参考・一致未確認)", names)

    def test_major_cities_are_classified_into_expected_regions(self):
        cities = {
            "hokkaido": (43.06, 141.35),
            "tohoku": (38.27, 140.87),
            "kanto": (35.68, 139.77),
            "chubu": (35.18, 136.91),
            "kinki": (34.69, 135.50),
            "chugoku_shikoku": (34.39, 132.46),
            "kyushu": (33.59, 130.40),
            "okinawa": (26.21, 127.68),
        }
        for expected, (lat, lon) in cities.items():
            with self.subTest(expected=expected):
                self.assertEqual(monitor.classify_region(lat, lon), expected)

    def test_region_transition_can_notify_new_channel(self):
        watchlist = {"abc123": {"label": "JA0001", "type": "TEST", "priority": "NORMAL"}}
        kanto = ["abc123", "TEST1", None, None, None, 139.77, 35.68, 1000, False, 100, 90, 0]
        chubu = ["abc123", "TEST1", None, None, None, 136.91, 35.18, 1000, False, 100, 90, 0]
        first, notified, _ = monitor.find_new_region_detections([kanto], watchlist, {}, 100)
        second, _, _ = monitor.find_new_region_detections([chubu], watchlist, notified, 110)
        self.assertEqual(first[0][0], "kanto")
        self.assertEqual(second[0][0], "chubu")

    def test_early_warning_area_uses_independent_state(self):
        watchlist = {"abc123": {"label": "JA0001", "type": "TEST", "priority": "NORMAL"}}
        aircraft = ["abc123", "TEST1", None, None, None, 140.0, 35.5, 1000, False, 100, 90, 0]
        notified = {"kanto:abc123": 100}
        found, updated, _ = monitor.find_new_area_detections(
            [aircraft], watchlist, notified, 110, "japan"
        )
        self.assertEqual(len(found), 1)
        self.assertIn("japan:abc123", updated)
        self.assertIn("kanto:abc123", updated)


if __name__ == "__main__":
    unittest.main()
