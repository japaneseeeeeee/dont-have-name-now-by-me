import gzip
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GITHUB_REPOSITORY", "example/example")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test")

from lookup_action import (
    airline_matches,
    format_info_search_results,
    info_search_result_components,
    lookup_live_callsign,
    lookup_live_type,
    search_result_components,
    search_tar1090,
)


SAMPLE_DATABASE = """\
aa0001;N100AA;B77W;;BOEING 777-300ER;2012
aa0002;N200AA;B77W;;BOEING 777-300ER;2013
780001;B-1001;B77W;;BOEING 777-300ER;2014
840001;JA731A;B77W;;BOEING 777-300ER;2015
840002;JA732A;B77W;;BOEING 777-300ER;2016
840003;JA733A;B77W;;BOEING 777-300ER;2017
840004;JA734A;B77W;;BOEING 777-300ER;2018
840005;JA735A;B77W;;BOEING 777-300ER;2019
"""


class SearchTar1090Tests(unittest.TestCase):
    def search(self, query):
        compressed = gzip.compress(SAMPLE_DATABASE.encode())
        with patch("lookup_action.request", return_value=compressed):
            return search_tar1090(query)

    def test_common_type_alias_prioritizes_japanese_registrations(self):
        results = self.search("77W")
        self.assertEqual(
            [item["registration"] for item in results],
            ["JA731A", "JA732A", "JA733A", "JA734A", "JA735A"],
        )

    def test_full_type_code_prioritizes_japanese_registrations(self):
        results = self.search("B77W")
        self.assertTrue(all(item["registration"].startswith("JA") for item in results))

    def test_registration_prefix_still_has_priority(self):
        results = self.search("N100")
        self.assertEqual(results[0]["registration"], "N100AA")
        self.assertEqual(results[0]["year"], "2012")

    def test_partial_registration_is_case_and_hyphen_insensitive(self):
        self.assertEqual(self.search("ja731")[0]["registration"], "JA731A")
        self.assertEqual(self.search("b1001")[0]["registration"], "B-1001")

    def test_live_type_search_normalizes_alias_and_filters_airline(self):
        payload = gzip.compress(b"")
        response = b'{"ac":[{"hex":"840001","r":"JA731A","t":"B77W","flight":"ANA101"},{"hex":"aa0001","r":"N100AA","t":"B77W","flight":"AAL1"}]}'
        with patch("lookup_action.request", return_value=response) as mocked:
            results = lookup_live_type("77W", "NH")
        self.assertEqual([item["registration"] for item in results], ["JA731A"])
        self.assertIn("/v2/type/B77W", mocked.call_args.args[0])

    def test_airline_filter_accepts_iata_or_icao(self):
        aircraft = {"flight": "ANA101"}
        self.assertTrue(airline_matches(aircraft, "NH"))
        self.assertTrue(airline_matches(aircraft, "ANA"))
        self.assertFalse(airline_matches(aircraft, "JAL"))

    def test_results_have_compact_register_row_and_next_button(self):
        results = self.search("B77W")
        rows = search_result_components(results, "B77W", "", 5)
        self.assertEqual(len(rows[0]["components"]), 5)
        self.assertTrue(rows[1]["components"][0]["custom_id"].startswith("searchpage|5|"))

    def test_info_results_have_detail_buttons_year_and_next_page(self):
        results = self.search("B77W")
        rows = info_search_result_components(results, "B77W", 5)
        self.assertEqual(len(rows[0]["components"]), 5)
        self.assertTrue(rows[0]["components"][0]["custom_id"].startswith("inforesult|"))
        self.assertTrue(rows[1]["components"][0]["custom_id"].startswith("infopage|5|"))
        self.assertIn("2015年", format_info_search_results("B77W", results))

    def test_live_callsign_reports_api_availability(self):
        with patch("lookup_action.request", return_value=b'{"ac":[]}'):
            aircraft, available = lookup_live_callsign("JL123")
        self.assertIsNone(aircraft)
        self.assertTrue(available)

        with patch("lookup_action.request", side_effect=OSError("offline")):
            aircraft, available = lookup_live_callsign("JL123")
        self.assertIsNone(aircraft)
        self.assertFalse(available)


if __name__ == "__main__":
    unittest.main()
