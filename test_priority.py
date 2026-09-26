import unittest
from datetime import date

import monitor
from route_corrections import correct_route


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

    def test_detection_reason_uses_natural_japanese(self):
        self.assertEqual(
            monitor.format_detection_reason("NORMAL", "関東"),
            "登録機を関東の監視範囲内で新たに検出しました。",
        )
        self.assertEqual(
            monitor.format_detection_reason("WATCH", "中部", repeat=True),
            "注目機（WATCH）を中部の監視範囲内で引き続き検出しています。",
        )
        self.assertEqual(
            monitor.format_detection_reason("SPECIAL", "日本周辺"),
            "特別注目機（SPECIAL）を日本周辺の監視範囲内で新たに検出しました。",
        )

    def test_route_is_hidden_when_position_does_not_match(self):
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
        self.assertFalse(any(name.startswith("区間") for name in names))

    def test_route_is_shown_when_position_matches(self):
        aircraft = ["abc123", "ANA1", None, None, None, 132.0, 30.0, 1000, False, 100, 30, 0]
        route = {
            "origin": {
                "iata_code": "OKA", "municipality": "Naha",
                "latitude": 26.1958, "longitude": 127.646,
            },
            "destination": {
                "iata_code": "NGO", "municipality": "Tokoname",
                "latitude": 34.8584, "longitude": 136.805,
            },
            "flight_iata": "NH304",
        }
        embed = monitor.build_embed(
            "abc123", {"label": "JA0001", "type": "TEST"}, aircraft, route=route,
        )
        names = [field["name"] for field in embed["fields"]]
        self.assertIn("区間(推定)", names)

    def test_major_airports_use_short_name_and_iata_code(self):
        self.assertEqual(
            monitor.format_airport({"iata_code": "NRT", "name": "Narita International Airport"}),
            "Narita (NRT)",
        )
        self.assertEqual(
            monitor.format_airport({"iata_code": "HND", "municipality": "Tokyo"}),
            "Haneda (HND)",
        )
        self.assertEqual(
            monitor.format_airport({"iata_code": "ICN", "name": "Incheon International Airport"}),
            "Incheon (ICN)",
        )

    def test_dl172_stale_route_is_corrected(self):
        stale = {
            "origin": {"iata_code": "MNL"},
            "destination": {"iata_code": "JFK"},
            "flight_iata": "DL172",
        }
        corrected = correct_route("DAL172", stale, today=date(2026, 9, 26))
        self.assertEqual(corrected["origin"]["iata_code"], "ICN")
        self.assertEqual(corrected["destination"]["iata_code"], "SLC")
        self.assertEqual(
            monitor.format_airport(corrected["destination"]),
            "Salt Lake City (SLC)",
        )
        self.assertTrue(monitor.route_matches_position(corrected, 36.319, 132.945))

    def test_dl172_correction_expires(self):
        stale = {
            "origin": {"iata_code": "MNL"},
            "destination": {"iata_code": "JFK"},
        }
        self.assertIs(
            correct_route("DAL172", stale, today=date(2027, 8, 25)),
            stale,
        )

    def test_known_bad_delta_routes_are_rejected(self):
        dal88 = {
            "origin": {"latitude": 33.6367, "longitude": -84.428101},
            "destination": {"latitude": 49.012798, "longitude": 2.55},
        }
        dal173 = {
            "origin": {"latitude": 45.6306, "longitude": 8.72811},
            "destination": {"latitude": 40.639801, "longitude": -73.7789},
        }
        self.assertFalse(monitor.route_matches_position(dal88, 34.123, 138.378))
        self.assertFalse(monitor.route_matches_position(dal173, 37.297, 137.255))

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

    def test_only_nationwide_flag_uses_japan_alert(self):
        aircraft = ["abc123", "TEST1", None, None, None, 130.4, 33.59, 1000, False, 100, 90, 0]
        special = {"label": "JA0001", "type": "TEST", "priority": "SPECIAL"}
        normal = {"label": "JA0001", "type": "TEST", "priority": "NORMAL"}
        nationwide = {"label": "JA0001", "type": "TEST", "nationwide_alert": True}
        self.assertTrue(monitor.should_send_japan_alert(aircraft, nationwide))
        self.assertFalse(monitor.should_send_japan_alert(aircraft, special))
        self.assertFalse(monitor.should_send_japan_alert(aircraft, normal))

    def test_registered_aircraft_outside_regions_uses_japan_outer_alert(self):
        watchlist = {"abc123": {"label": "JA0001", "type": "TEST"}}
        outer = ["abc123", "TEST1", None, None, None, 145.0, 40.0, 1000, False, 100, 90, 0]
        kanto = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        unknown = ["def456", "TEST2", None, None, None, 145.0, 40.0, 1000, False, 100, 90, 0]
        self.assertTrue(monitor.should_send_japan_outer_alert(outer, watchlist))
        self.assertFalse(monitor.should_send_japan_outer_alert(kanto, watchlist))
        self.assertFalse(monitor.should_send_japan_outer_alert(unknown, watchlist))

    def test_personal_special_creates_private_event(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        settings = {
            "123456789012345678": {
                "enabled": True,
                "aircraft": {"abc123": {"label": "JA0001", "type": "TEST"}},
            }
        }
        events, notified = monitor.find_personal_special_events(
            [aircraft], settings, {}, 100
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["user_id"], "123456789012345678")
        self.assertEqual(events[0]["label"], "JA0001")
        self.assertEqual(events[0]["region"], "関東")
        self.assertIn("123456789012345678:abc123", notified)

    def test_personal_special_respects_cooldown_and_setting(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        enabled = {
            "123": {"enabled": True, "aircraft": {"abc123": {"label": "JA0001"}}}
        }
        disabled = {
            "123": {"enabled": False, "aircraft": {"abc123": {"label": "JA0001"}}}
        }
        events, _ = monitor.find_personal_special_events(
            [aircraft], enabled, {"123:abc123": 100}, 200
        )
        self.assertEqual(events, [])
        events, _ = monitor.find_personal_special_events(
            [aircraft], disabled, {}, 2000
        )
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
