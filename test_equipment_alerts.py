import unittest

from equipment_alerts import add_rule, aircraft_matches, empty_store, normalize_equipment


class EquipmentAlertsTest(unittest.TestCase):
    def test_b77w_aliases_match_adsb_data(self):
        self.assertEqual(normalize_equipment("77W"), "B77W")
        self.assertTrue(aircraft_matches({"t": "B77W"}, "77W"))
        self.assertTrue(aircraft_matches({"desc": "Boeing 777-300ER"}, "B77W"))
        self.assertFalse(aircraft_matches({"t": "B772"}, "B77W"))

    def test_duplicate_rule_is_not_added(self):
        store = empty_store()
        first, created = add_rule(
            store, owner_id="1", scope="personal", flight="JL12", equipment="77W"
        )
        second, created_again = add_rule(
            store, owner_id="1", scope="personal", flight="jl-12", equipment="B77W"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        self.assertEqual(len(store["rules"]), 1)


if __name__ == "__main__":
    unittest.main()
