import unittest
from datetime import datetime, timezone

import discord_bot


class PhotoPostTests(unittest.TestCase):
    def test_parses_short_three_line_post(self):
        result = discord_bot.parse_photo_post(
            "JA784A\n成田空港\n夕日がきれいでした！",
            datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result["registration"], "JA784A")
        self.assertEqual(result["location"], "成田空港")
        self.assertEqual(result["comment"], "夕日がきれいでした！")
        self.assertEqual(result["date"], "2026年9月24日")

    def test_parses_labeled_post(self):
        result = discord_bot.parse_photo_post(
            "機体番号：JA08XJ\n撮影場所：羽田空港\n撮影日：2026年9月23日\n感想：近くで見られました"
        )
        self.assertEqual(result["registration"], "JA08XJ")
        self.assertEqual(result["location"], "羽田空港")
        self.assertEqual(result["date"], "2026年9月23日")
        self.assertEqual(result["comment"], "近くで見られました")

    def test_missing_registration_is_reported(self):
        result = discord_bot.parse_photo_post("成田空港\nきれいでした")
        self.assertIsNone(result["registration"])

    def test_parses_military_serial(self):
        result = discord_bot.parse_photo_post("92-9000\n羽田空港\n迫力がありました")
        self.assertEqual(result["registration"], "92-9000")
        self.assertEqual(result["location"], "羽田空港")
        self.assertEqual(result["comment"], "迫力がありました")

    def test_parses_us_n_number(self):
        result = discord_bot.parse_photo_post("N123AB\n成田空港\n初めて見ました")
        self.assertEqual(result["registration"], "N123AB")


if __name__ == "__main__":
    unittest.main()
