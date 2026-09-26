import unittest
from datetime import date

from livery import lookup_livery


class LiveryTests(unittest.TestCase):
    def test_matches_registration_without_hyphen_or_case(self):
        data = {"JA339J": {"name": "JAL Jubilee Express"}}
        self.assertEqual(
            lookup_livery("ja-339j", data=data),
            "JAL Jubilee Express",
        )

    def test_unknown_aircraft_has_no_livery_field(self):
        self.assertIsNone(lookup_livery("JA000A", data={}))

    def test_expired_livery_is_hidden(self):
        data = {
            "JA339J": {
                "name": "JAL Jubilee Express",
                "valid_until": "2027-04-30",
            }
        }
        self.assertIsNone(
            lookup_livery("JA339J", today=date(2027, 5, 1), data=data)
        )


if __name__ == "__main__":
    unittest.main()
