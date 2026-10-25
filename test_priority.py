import unittest
import json
import os
import tempfile
from unittest.mock import Mock, patch
from datetime import date, datetime, timedelta, timezone

import monitor
from route_corrections import correct_route


class PriorityTests(unittest.TestCase):
    def setUp(self):
        monitor._callsign_cache.clear()
        monitor._route_cache.clear()
        monitor._photo_cache = None

    def test_photo_request_has_required_contact_and_is_cached(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "photos": [{
                "thumbnail_large": {"src": "https://example.com/ja755a.jpg"},
                "link": "https://example.com/photo",
                "photographer": "Test Photographer",
            }]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "photo_cache.json")
            with (
                patch.object(monitor, "PHOTO_CACHE_PATH", cache_path),
                patch.object(monitor.requests, "get", return_value=response) as request,
            ):
                first = monitor.fetch_photo("868710", now=100)
                second = monitor.fetch_photo("868710", now=101)

        self.assertEqual(first, second)
        self.assertEqual(first["src"], "https://example.com/ja755a.jpg")
        self.assertEqual(request.call_count, 1)
        user_agent = request.call_args.kwargs["headers"]["User-Agent"]
        self.assertIn("https://", user_agent)

    def test_photo_uses_stale_cache_when_api_is_temporarily_unavailable(self):
        cached_photo = {
            "src": "https://example.com/cached.jpg",
            "link": None,
            "photographer": "Cached Photographer",
        }
        monitor._photo_cache = {
            "868710": {"photo": cached_photo, "cached_at": 1},
        }
        response = Mock(status_code=503)
        with (
            patch.object(monitor.requests, "get", return_value=response) as request,
            patch.object(monitor.time, "sleep"),
        ):
            result = monitor.fetch_photo(
                "868710", now=monitor.PHOTO_CACHE_SECONDS + 10,
            )

        self.assertEqual(result, cached_photo)
        self.assertEqual(request.call_count, monitor.PHOTO_MAX_ATTEMPTS)

    def test_old_entries_default_to_normal(self):
        self.assertEqual(monitor.effective_priority({"label": "JA0001"}, now=100), "NORMAL")

    def test_shared_watchlist_refreshes_local_fallback_copy(self):
        remote = {"8691aa": {"label": "JA784A", "type": "B77W"}}
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = remote
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "watchlist.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"old": {"label": "OLD", "type": "不明"}}, stream)
            with (
                patch.object(monitor, "WATCHLIST_PATH", path),
                patch.object(monitor.requests, "get", return_value=response),
            ):
                loaded = monitor.load_watchlist()
            with open(path, encoding="utf-8") as stream:
                cached = json.load(stream)
        self.assertEqual(cached, remote)
        self.assertEqual(loaded["8691aa"]["label"], "JA784A")

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

    def test_known_special_livery_is_in_notification(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.0, 1000, False, 100, 90, 0]
        with patch("monitor.lookup_livery", return_value="記念特別塗装"):
            embed = monitor.build_embed(
                "abc123",
                {"label": "JA0001", "type": "TEST", "priority": "NORMAL"},
                aircraft,
            )
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["🎨 特別塗装"], "記念特別塗装")

    def test_special_livery_is_in_mobile_notification_content(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.0, 1000, False, 100, 90, 0]
        response = Mock(status_code=204)
        with (
            patch("monitor.lookup_livery", return_value="記念特別塗装"),
            patch("monitor.fetch_photo", return_value=None),
            patch("monitor.resolve_flight_context", return_value=("TEST1", None)),
            patch("monitor.request_with_retry", return_value=response) as request,
        ):
            monitor.notify_discord(
                "abc123", {"label": "JA0001", "type": "TEST"}, aircraft,
                "https://example.invalid/webhook", "関東",
            )
        payload = request.call_args.kwargs["json"]
        self.assertIn("特別塗装：記念特別塗装", payload["content"])

    def test_recovery_notice_waits_for_successful_monitor_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            pending_path = os.path.join(directory, "recovery.json")
            events_path = os.path.join(directory, "events.json")
            state_path = os.path.join(directory, "state.json")
            with open(pending_path, "w", encoding="utf-8") as stream:
                json.dump({
                    "last_seen": 1000,
                    "recovered_at": 1600,
                    "duration_seconds": 600,
                }, stream)
            with (
                patch.object(monitor, "RECOVERY_PENDING_PATH", pending_path),
                patch.object(monitor, "SYSTEM_ALERT_EVENTS_PATH", events_path),
                patch.object(monitor, "SYSTEM_ALERT_STATE_PATH", state_path),
            ):
                self.assertTrue(monitor.complete_recovery_notice(now=1700))
                with open(events_path, encoding="utf-8") as stream:
                    events = json.load(stream)
                with open(pending_path, encoding="utf-8") as stream:
                    pending = json.load(stream)
        self.assertEqual(events[0]["severity"], "recovery")
        self.assertIn("最新ADS-Bデータ", events[0]["detail"])
        self.assertEqual(pending, {})

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

    def test_departing_aircraft_rejects_route_from_distant_origin(self):
        route = {
            "origin": {
                "iata_code": "KIX", "latitude": 34.4273, "longitude": 135.244,
            },
            "destination": {
                "iata_code": "GUM", "latitude": 13.4834, "longitude": 144.796,
            },
            "flight_iata": "DL294",
        }
        self.assertFalse(monitor.route_matches_position(
            route,
            35.739,
            139.828,
            track=324,
            altitude=1935,
            vertical_rate=13,
        ))

    def test_departing_aircraft_accepts_route_from_nearby_origin(self):
        route = {
            "origin": {
                "iata_code": "HND", "latitude": 35.5494, "longitude": 139.7798,
            },
            "destination": {
                "iata_code": "MSP", "latitude": 44.8848, "longitude": -93.2223,
            },
            "flight_iata": "DL120",
        }
        self.assertTrue(monitor.route_matches_position(
            route,
            35.739,
            139.828,
            track=324,
            altitude=1935,
            vertical_rate=13,
        ))

    def test_mismatched_primary_callsign_can_use_verified_fallback(self):
        wrong_route = {
            "origin": {"iata_code": "KIX", "latitude": 34.4273, "longitude": 135.244},
            "destination": {"iata_code": "GUM", "latitude": 13.4834, "longitude": 144.796},
        }
        correct_route = {
            "origin": {"iata_code": "HND", "latitude": 35.5494, "longitude": 139.7798},
            "destination": {"iata_code": "MSP", "latitude": 44.8848, "longitude": -93.2223},
            "flight_iata": "DL120",
        }

        def route_for(callsign, _icao24=None):
            return {"DAL294": wrong_route, "DAL120": correct_route}.get(callsign)

        with (
            patch("monitor.fetch_fallback_callsign", return_value="DAL120"),
            patch("monitor.fetch_route_cached", side_effect=route_for),
        ):
            callsign, route = monitor.resolve_flight_context(
                "a6b47c", "DAL294", 35.739, 139.828,
                track=324, altitude=1935, vertical_rate=13,
            )
        self.assertEqual(callsign, "DAL120")
        self.assertEqual(route["flight_iata"], "DL120")
        self.assertEqual(route["_confidence"], "estimated")

    @staticmethod
    def observed_aircraft(
        callsign="ANA1", lat=35.0, lon=139.0, altitude=1000,
        on_ground=False, track=45, vertical_rate=5, last_contact=1000,
    ):
        return [
            "abc123", callsign, "Japan", last_contact, last_contact,
            lon, lat, altitude, on_ground, 200, track, vertical_rate,
        ]

    def test_callsign_becomes_stable_after_three_observations(self):
        state = {}
        for index, now in enumerate((1000, 1087, 1174), start=1):
            observations, state = monitor.update_flight_context_state(
                [self.observed_aircraft(last_contact=now)], {"abc123"}, state, now,
            )
            self.assertEqual(observations["abc123"]["consecutive"], index)
        self.assertTrue(observations["abc123"]["callsign_stable"])

    def test_airborne_callsign_change_requires_reconfirmation(self):
        previous = {
            "abc123": {
                "callsign": "ANA1", "consecutive": 5, "callsign_abrupt": False,
                "latitude": 35.0, "longitude": 139.0, "altitude": 1000,
                "on_ground": False, "last_seen": 1000,
            },
        }
        observations, _ = monitor.update_flight_context_state(
            [self.observed_aircraft(callsign="ANA2", last_contact=1087)],
            {"abc123"}, previous, 1087,
        )
        self.assertTrue(observations["abc123"]["callsign_abrupt"])
        self.assertFalse(observations["abc123"]["callsign_stable"])
        self.assertEqual(observations["abc123"]["previous_callsign"], "ANA1")

    def test_ground_callsign_change_is_allowed(self):
        previous = {
            "abc123": {
                "callsign": "ANA1", "consecutive": 5, "callsign_abrupt": False,
                "latitude": 35.0, "longitude": 139.0, "altitude": 0,
                "on_ground": True, "last_seen": 1000,
            },
        }
        observations, _ = monitor.update_flight_context_state(
            [self.observed_aircraft(
                callsign="ANA2", altitude=0, on_ground=True,
                vertical_rate=0, last_contact=1087,
            )],
            {"abc123"}, previous, 1087,
        )
        self.assertFalse(observations["abc123"]["callsign_abrupt"])
        self.assertTrue(observations["abc123"]["callsign_stable"])

    def test_impossible_position_jump_is_rejected(self):
        previous = {
            "abc123": {
                "callsign": "ANA1", "consecutive": 5, "callsign_abrupt": False,
                "latitude": 35.0, "longitude": 139.0, "altitude": 1000,
                "on_ground": False, "last_seen": 1000,
            },
        }
        observations, state = monitor.update_flight_context_state(
            [self.observed_aircraft(
                lat=45.0, lon=145.0, altitude=10000, last_contact=1087,
            )],
            {"abc123"}, previous, 1087,
        )
        self.assertFalse(observations["abc123"]["position_consistent"])
        self.assertEqual(state["abc123"]["latitude"], 35.0)

    def test_stale_aircraft_data_is_marked_unreliable(self):
        observations, _ = monitor.update_flight_context_state(
            [self.observed_aircraft(last_contact=700)], {"abc123"}, {}, 1000,
        )
        self.assertFalse(observations["abc123"]["data_fresh"])

    def test_missing_callsign_does_not_erase_previous_callsign(self):
        previous = {
            "abc123": {
                "callsign": "ANA1", "consecutive": 5, "callsign_abrupt": False,
                "latitude": 35.0, "longitude": 139.0, "altitude": 1000,
                "on_ground": False, "last_seen": 1000,
            },
        }
        _, state = monitor.update_flight_context_state(
            [self.observed_aircraft(callsign="", last_contact=1087)],
            {"abc123"}, previous, 1087,
        )
        self.assertEqual(state["abc123"]["callsign"], "ANA1")

    def test_unconfirmed_abrupt_callsign_hides_route(self):
        route = {
            "origin": {"iata_code": "HND", "latitude": 35.5494, "longitude": 139.7798},
            "destination": {"iata_code": "MSP", "latitude": 44.8848, "longitude": -93.2223},
            "flight_iata": "DL120",
        }
        observation = {
            "callsign_abrupt": True, "previous_callsign": "DAL121",
            "callsign_stable": False, "position_consistent": True, "data_fresh": True,
        }
        with (
            patch("monitor.fetch_route_cached", return_value=route),
            patch("monitor.fetch_fallback_callsign", return_value=""),
        ):
            callsign, verified = monitor.resolve_flight_context(
                "a6b47c", "DAL294", 35.739, 139.828,
                track=324, altitude=1935, vertical_rate=13,
                observation=observation,
            )
        self.assertEqual(callsign, "DAL294")
        self.assertIsNone(verified)

    def test_agreeing_sources_mark_route_high_confidence(self):
        route = {
            "origin": {"iata_code": "HND", "latitude": 35.5494, "longitude": 139.7798},
            "destination": {"iata_code": "MSP", "latitude": 44.8848, "longitude": -93.2223},
            "flight_iata": "DL120",
        }
        with (
            patch("monitor.fetch_route_cached", return_value=route),
            patch("monitor.fetch_fallback_callsign", return_value="DAL120"),
        ):
            _, verified = monitor.resolve_flight_context(
                "a6b47c", "DAL120", 35.739, 139.828,
                track=324, altitude=1935, vertical_rate=13,
            )
        self.assertEqual(verified["_confidence"], "high")

    def test_haneda_departure_corrects_stale_narita_origin(self):
        route = {
            "origin": {
                "iata_code": "NRT", "name": "Narita International Airport",
                "latitude": 35.7720, "longitude": 140.3929,
            },
            "destination": {
                "iata_code": "DFW", "name": "Dallas Fort Worth International Airport",
                "latitude": 32.8998, "longitude": -97.0403,
            },
            "flight_iata": "AA176",
        }
        with (
            patch("monitor.fetch_route_cached", return_value=route),
            patch("monitor.fetch_fallback_callsign", return_value="AAL176"),
        ):
            _, corrected = monitor.resolve_flight_context(
                "a9ddb0", "AAL176", 35.591, 139.774,
                track=355, altitude=549, vertical_rate=11.4,
            )
        self.assertEqual(corrected["origin"]["iata_code"], "HND")
        self.assertEqual(corrected["destination"]["iata_code"], "DFW")
        self.assertEqual(corrected["_scheduled_origin"]["iata_code"], "NRT")
        self.assertEqual(corrected["_confidence"], "corrected")

        aircraft = ["a9ddb0", "AAL176", None, None, None, 139.774, 35.591, 549, False, 103, 355, 11.4]
        embed = monitor.build_embed(
            "a9ddb0", {"label": "N735AT", "type": "B77W"}, aircraft, route=corrected,
        )
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertIn("区間(実測補正)", fields)
        self.assertIn("Haneda (HND) → Dallas Fort Worth International Airport (DFW)", fields["区間(実測補正)"])
        self.assertIn("予定情報は Narita (NRT) 発", fields["区間(実測補正)"])

    def test_cruising_aircraft_does_not_override_scheduled_origin(self):
        route = {
            "origin": {"iata_code": "NRT", "latitude": 35.7720, "longitude": 140.3929},
            "destination": {"iata_code": "DFW", "latitude": 32.8998, "longitude": -97.0403},
        }
        unchanged = monitor.reconcile_route_departure(
            route, 35.591, 139.774, track=355, altitude=10000, vertical_rate=5,
        )
        self.assertIs(unchanged, route)

    def test_go_around_does_not_turn_destination_into_origin(self):
        route = {
            "origin": {"iata_code": "ITM", "latitude": 34.7855, "longitude": 135.4382},
            "destination": {"iata_code": "HND", "latitude": 35.5494, "longitude": 139.7798},
        }
        unchanged = monitor.reconcile_route_departure(
            route, 35.591, 139.774, track=355, altitude=549, vertical_rate=11.4,
        )
        self.assertIs(unchanged, route)

    def test_route_cache_is_separated_by_aircraft_and_japan_date(self):
        route = {
            "origin": {"iata_code": "HND"},
            "destination": {"iata_code": "DFW"},
        }
        first_day = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc).timestamp()
        next_day = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc).timestamp()
        with patch("monitor.fetch_route", return_value=route) as mocked:
            self.assertIs(monitor.fetch_route_cached("AAL176", "a9ddb0", first_day), route)
            self.assertIs(monitor.fetch_route_cached("AAL176", "a9ddb0", first_day), route)
            self.assertIs(monitor.fetch_route_cached("AAL176", "abcdef", first_day), route)
            self.assertIs(monitor.fetch_route_cached("AAL176", "a9ddb0", next_day), route)
        self.assertEqual(mocked.call_count, 3)

    def test_missing_opensky_callsign_is_filled_from_secondary_adsb(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "ac": [{"hex": "8691aa", "flight": "ANA111  ", "r": "JA784A"}],
        }
        with patch("monitor.requests.get", return_value=response) as mocked:
            self.assertEqual(monitor.fetch_fallback_callsign("8691AA"), "ANA111")
            self.assertEqual(monitor.fetch_fallback_callsign("8691AA"), "ANA111")
        self.assertEqual(mocked.call_count, 1)
        self.assertIn("/v2/hex/8691aa", mocked.call_args.args[0])

    def test_resolved_callsign_adds_verified_route_to_notification(self):
        route = {
            "origin": {
                "iata_code": "ORD", "name": "Chicago O'Hare",
                "latitude": 41.9786, "longitude": -87.9048,
            },
            "destination": {
                "iata_code": "HND", "name": "Haneda",
                "latitude": 35.5494, "longitude": 139.7798,
            },
            "flight_iata": "NH111",
        }
        with (
            patch("monitor.fetch_fallback_callsign", return_value="ANA111"),
            patch("monitor.fetch_route_cached", return_value=route),
        ):
            callsign, verified = monitor.resolve_flight_context(
                "8691aa", "", 35.946, 141.438,
            )
        self.assertEqual(callsign, "ANA111")
        self.assertEqual(verified["flight_iata"], "NH111")

        aircraft = ["8691aa", callsign, None, None, None, 141.438, 35.946, 5227, False, 187, 270, -5]
        embed = monitor.build_embed(
            "8691aa", {"label": "JA784A", "type": "B77W"}, aircraft, route=verified,
        )
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["コールサイン"], "`ANA111`")
        self.assertIn("NH111", fields["区間(推定)"])
        self.assertIn("ORD", fields["区間(推定)"])
        self.assertIn("HND", fields["区間(推定)"])

    def test_personal_event_receives_same_flight_context(self):
        route = {
            "origin": {"iata_code": "ORD", "latitude": 41.9786, "longitude": -87.9048},
            "destination": {"iata_code": "HND", "latitude": 35.5494, "longitude": 139.7798},
            "flight_iata": "NH111",
        }
        events = [{
            "icao24": "8691aa", "callsign": "", "latitude": 35.946, "longitude": 141.438,
        }]
        with patch("monitor.resolve_flight_context", return_value=("ANA111", route)):
            monitor.enrich_personal_event_flight_context(events)
        self.assertEqual(events[0]["callsign"], "ANA111")
        self.assertEqual(events[0]["route"]["flight_iata"], "NH111")

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

    def test_personal_subscription_index_groups_same_aircraft_users(self):
        settings = {
            "100": {"aircraft": {"ABC123": {"label": "A"}}},
            "200": {"aircraft": {"abc123": {"label": "B"}}},
            "300": {"enabled": False, "aircraft": {"abc123": {"label": "C"}}},
        }
        subscriptions, configs = monitor.build_personal_subscriptions(settings)
        self.assertEqual([item[0] for item in subscriptions["abc123"]], ["100", "200"])
        self.assertNotIn("300", configs)

    def test_personal_aircraft_can_override_legacy_global_off(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        settings = {"123": {"enabled": False, "aircraft": {
            "abc123": {"label": "JA0001", "enabled": True},
            "def456": {"label": "JA0002"},
        }}}
        events, _ = monitor.find_personal_special_events([aircraft], settings, {}, 100)
        self.assertEqual([event["label"] for event in events], ["JA0001"])

    def test_personal_region_and_airport_items_can_be_disabled_individually(self):
        kyushu = ["abc123", "ANA101", None, None, None, 130.4, 33.59, 1000, False, 100, 90, 0]
        settings = {"123": {
            "aircraft": {},
            "region_alerts": {"abc123": {
                "label": "JA738B", "regions": ["kyushu"],
                "region_enabled": {"kyushu": False},
            }},
            "airports": {"FUK": {"radius_km": 200, "enabled": False}},
        }}
        events, _ = monitor.find_personal_special_events(
            [kyushu], settings, {}, 100,
            {"abc123": {"label": "JA738B", "type": "B738"}},
        )
        self.assertEqual(events, [])

    def test_same_aircraft_position_is_reused_for_multiple_subscribers(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        settings = {
            "100": {"aircraft": {"abc123": {"label": "A"}}},
            "200": {"aircraft": {"abc123": {"label": "B"}}},
        }
        events, _ = monitor.find_personal_special_events([aircraft], settings, {}, 100)
        self.assertEqual({event["user_id"] for event in events}, {"100", "200"})
        self.assertEqual({event["event_key"] for event in events}, {"100:abc123", "200:abc123"})

    def test_personal_aircraft_region_rule_only_matches_selected_region(self):
        kyushu = ["abc123", "ANA101", None, None, None, 130.4, 33.59, 1000, False, 100, 90, 0]
        kanto = ["abc123", "ANA101", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        settings = {"123": {
            "enabled": True,
            "aircraft": {},
            "region_alerts": {
                "abc123": {"label": "JA738B", "type": "B738", "regions": ["kyushu"]},
            },
        }}
        events, notified = monitor.find_personal_special_events([kyushu], settings, {}, 100)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["alert_kind"], "region")
        self.assertEqual(events[0]["target_region"], "kyushu")
        self.assertEqual(events[0]["region"], "九州")
        self.assertIn("region:123:abc123:kyushu", notified)
        events, _ = monitor.find_personal_special_events(
            [kyushu], settings, notified, 100 + monitor.RENOTIFY_SECONDS + 1
        )
        self.assertEqual(events, [])
        events, _ = monitor.find_personal_special_events([kanto], settings, {}, 100)
        self.assertEqual(events, [])

    def test_personal_region_rule_does_not_duplicate_regular_aircraft_alert(self):
        kyushu = ["abc123", "ANA101", None, None, None, 130.4, 33.59, 1000, False, 100, 90, 0]
        settings = {"123": {
            "enabled": True,
            "aircraft": {"abc123": {"label": "JA738B", "type": "B738"}},
            "region_alerts": {
                "abc123": {"label": "JA738B", "type": "B738", "regions": ["kyushu"]},
            },
        }}
        events, _ = monitor.find_personal_special_events([kyushu], settings, {}, 100)
        self.assertEqual(len(events), 1)
        self.assertNotEqual(events[0].get("alert_kind"), "region")

    def test_disabled_regular_aircraft_does_not_block_enabled_region_rule(self):
        kyushu = ["abc123", "ANA101", None, None, None, 130.4, 33.59, 1000, False, 100, 90, 0]
        settings = {"123": {
            "aircraft": {"abc123": {"label": "JA738B", "enabled": False}},
            "region_alerts": {"abc123": {
                "label": "JA738B", "regions": ["kyushu"],
                "region_enabled": {"kyushu": True},
            }},
        }}
        events, _ = monitor.find_personal_special_events([kyushu], settings, {}, 100)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("alert_kind"), "region")

    def test_tracked_icaos_include_region_only_aircraft(self):
        tracked = monitor._tracked_icaos({}, {
            "123": {"aircraft": {}, "region_alerts": {"abc123": {"regions": ["kyushu"]}}},
        })
        self.assertEqual(tracked, {"abc123"})

    def test_personal_event_queue_deduplicates_event_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "events.json")
            old_path = monitor.PERSONAL_SPECIAL_EVENTS_PATH
            monitor.PERSONAL_SPECIAL_EVENTS_PATH = path
            try:
                event = {"event_key": "100:abc123", "user_id": "100", "icao24": "abc123"}
                monitor.append_personal_special_events([event])
                monitor.append_personal_special_events([event])
                with open(path, "r", encoding="utf-8") as data_file:
                    self.assertEqual(json.load(data_file), [event])
            finally:
                monitor.PERSONAL_SPECIAL_EVENTS_PATH = old_path

    def test_personal_detection_history_is_bounded_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = monitor.PERSONAL_DETECTION_HISTORY_PATH
            old_limit = monitor.PERSONAL_HISTORY_LIMIT_PER_USER
            monitor.PERSONAL_DETECTION_HISTORY_PATH = os.path.join(directory, "history.json")
            monitor.PERSONAL_HISTORY_LIMIT_PER_USER = 2
            try:
                base = {"user_id": "100", "icao24": "abc123", "label": "JA0001", "region": "関東"}
                monitor.append_personal_detection_history([{**base, "detected_at": 100}], 100)
                monitor.append_personal_detection_history([{**base, "detected_at": 120}], 120)
                monitor.append_personal_detection_history([{**base, "detected_at": 200}], 200)
                monitor.append_personal_detection_history([{**base, "detected_at": 300}], 300)
                with open(monitor.PERSONAL_DETECTION_HISTORY_PATH, "r", encoding="utf-8") as data_file:
                    history = json.load(data_file)
                self.assertEqual(len(history["100"]), 2)
                self.assertEqual([item["detected_at"] for item in history["100"]], [200.0, 300.0])
            finally:
                monitor.PERSONAL_DETECTION_HISTORY_PATH = old_path
                monitor.PERSONAL_HISTORY_LIMIT_PER_USER = old_limit

    def test_system_alert_queue_has_six_hour_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            old_events = monitor.SYSTEM_ALERT_EVENTS_PATH
            old_state = monitor.SYSTEM_ALERT_STATE_PATH
            monitor.SYSTEM_ALERT_EVENTS_PATH = os.path.join(directory, "events.json")
            monitor.SYSTEM_ALERT_STATE_PATH = os.path.join(directory, "state.json")
            try:
                self.assertTrue(monitor.queue_system_alert("OpenSky", "利用制限", status_code=429, now=100))
                self.assertFalse(monitor.queue_system_alert("OpenSky", "利用制限", status_code=429, now=200))
                self.assertTrue(monitor.queue_system_alert("OpenSky", "利用制限", status_code=429, now=100 + 6 * 3600))
                with open(monitor.SYSTEM_ALERT_EVENTS_PATH, "r", encoding="utf-8") as data_file:
                    self.assertEqual(len(json.load(data_file)), 1)
            finally:
                monitor.SYSTEM_ALERT_EVENTS_PATH = old_events
                monitor.SYSTEM_ALERT_STATE_PATH = old_state

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

    def test_personal_watchlist_respects_selected_regions(self):
        kanto = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        settings = {"123": {
            "enabled": True,
            "regions": ["kyushu"],
            "aircraft": {"abc123": {"label": "JA0001", "priority": "NORMAL"}},
        }}
        events, _ = monitor.find_personal_special_events([kanto], settings, {}, 100)
        self.assertEqual(events, [])

    def test_quiet_hours_suppress_normal_but_not_special(self):
        aircraft = ["abc123", "TEST1", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        quiet_time = datetime(2026, 9, 28, 23, 30, tzinfo=timezone(timedelta(hours=9))).timestamp()
        base = {"enabled": True, "quiet_hours": {"start": "23:00", "end": "07:00"}}
        normal = {"123": {**base, "aircraft": {"abc123": {"label": "JA0001", "priority": "NORMAL"}}}}
        special = {"123": {**base, "aircraft": {"abc123": {"label": "JA0001", "priority": "SPECIAL"}}}}
        normal_events, _ = monitor.find_personal_special_events([aircraft], normal, {}, quiet_time)
        special_events, _ = monitor.find_personal_special_events([aircraft], special, {}, quiet_time)
        self.assertEqual(normal_events, [])
        self.assertEqual(len(special_events), 1)
        self.assertEqual(special_events[0]["priority"], "SPECIAL")

    def test_personal_filters_match_status_airline_and_type(self):
        airborne = ["abc123", "ANA101", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        ground = ["abc123", "ANA101", None, None, None, 139.0, 35.5, 0, True, 0, 0, 0]
        config = {
            "enabled": True,
            "filters": {"status": "airborne", "airlines": ["NH"], "types": ["77W"]},
            "aircraft": {"abc123": {"label": "JA0001", "type": "BOEING 777-300ER (B77W)", "priority": "NORMAL"}},
        }
        events, _ = monitor.find_personal_special_events([airborne], {"123": config}, {}, 100)
        self.assertEqual(len(events), 1)
        events, _ = monitor.find_personal_special_events([ground], {"123": config}, {}, 100)
        self.assertEqual(events, [])

    def test_personal_airline_filter_rejects_other_airline(self):
        aircraft = ["abc123", "JAL101", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        config = {
            "enabled": True,
            "filters": {"airlines": ["NH"]},
            "aircraft": {"abc123": {"label": "JA0001", "type": "B77W", "priority": "NORMAL"}},
        }
        events, _ = monitor.find_personal_special_events([aircraft], {"123": config}, {}, 100)
        self.assertEqual(events, [])

    def test_airport_watch_detects_server_watchlist_aircraft_near_airport(self):
        near_haneda = ["abc123", "ANA101", None, None, None, 139.80, 35.56, 1000, False, 100, 90, 0]
        settings = {"123": {"enabled": True, "aircraft": {}, "airports": {"HND": {"radius_km": 50}}}}
        watchlist = {"abc123": {"label": "JA0001", "type": "B77W", "priority": "WATCH"}}
        events, notified = monitor.find_personal_special_events(
            [near_haneda], settings, {}, 100, watchlist
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["airport"], "HND")
        self.assertIn("airport:123:HND:abc123", notified)

    def test_personal_mute_suppresses_notifications(self):
        aircraft = ["abc123", "ANA101", None, None, None, 139.0, 35.5, 1000, False, 100, 90, 0]
        settings = {"123": {"enabled": True, "muted_until": 200, "aircraft": {"abc123": {"label": "JA0001"}}}}
        events, _ = monitor.find_personal_special_events([aircraft], settings, {}, 100)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
