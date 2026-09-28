import gzip
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GITHUB_REPOSITORY", "example/example")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test")

from lookup_action import search_tar1090


SAMPLE_DATABASE = """\
aa0001;N100AA;B77W;;BOEING 777-300ER;
aa0002;N200AA;B77W;;BOEING 777-300ER;
780001;B-1001;B77W;;BOEING 777-300ER;
840001;JA731A;B77W;;BOEING 777-300ER;
840002;JA732A;B77W;;BOEING 777-300ER;
840003;JA733A;B77W;;BOEING 777-300ER;
840004;JA734A;B77W;;BOEING 777-300ER;
840005;JA735A;B77W;;BOEING 777-300ER;
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


if __name__ == "__main__":
    unittest.main()
