import unittest
from destination_alerts import add_rule, empty_store, event_fingerprint, route_matches_destination, should_notify

class DestinationAlertTests(unittest.TestCase):
    def test_add_and_deduplicate_rule(self):
        store = empty_store()
        first, created = add_rule(store, registration="A7-BBA", icao24="06a0aa", aircraft_type="B77L", destination="nrt", owner_id=1)
        second, created_again = add_rule(store, registration="A7-BBA", icao24="06a0aa", aircraft_type="B77L", destination="NRT", owner_id=1)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])

    def test_personal_rules_are_separate_per_user(self):
        store = empty_store()
        first, _ = add_rule(store, registration="A7-BBA", icao24="06a0aa", aircraft_type="B77L", destination="NRT", owner_id=1, scope="personal")
        second, created = add_rule(store, registration="A7-BBA", icao24="06a0aa", aircraft_type="B77L", destination="NRT", owner_id=2, scope="personal")
        self.assertTrue(created)
        self.assertNotEqual(first["id"], second["id"])

    def test_matches_iata_or_icao_exactly(self):
        route = {"destination": {"iata_code": "NRT", "icao_code": "RJAA"}}
        self.assertTrue(route_matches_destination(route, "NRT"))
        self.assertTrue(route_matches_destination(route, "RJAA"))
        self.assertFalse(route_matches_destination(route, "HND"))

    def test_same_operation_is_suppressed_across_midnight(self):
        rule = {"icao24": "06a0aa"}
        aircraft = {"flight": "QTR806"}
        route = {"origin": {"iata_code": "DOH", "icao_code": "OTHH"}, "destination": {"iata_code": "NRT", "icao_code": "RJAA"}}
        key = event_fingerprint(rule, aircraft, route)
        self.assertFalse(should_notify({key: 1000}, key, now=2000))
        self.assertTrue(should_notify({key: 1000}, key, now=1000 + 20 * 3600))

if __name__ == "__main__":
    unittest.main()
