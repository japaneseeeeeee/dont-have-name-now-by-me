import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import discord_bot


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeContext:
    def __init__(self, user_id=123):
        self.guild = None
        self.author = SimpleNamespace(id=user_id)
        self.messages = []

    async def send(self, message, **kwargs):
        self.messages.append(message)

    def typing(self):
        return _Typing()


class PersonalCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}
        self.ctx = FakeContext()
        self.load_patch = patch.object(
            discord_bot, "load_locked_json", side_effect=lambda path, default: self.store
        )
        self.save_patch = patch.object(
            discord_bot, "save_locked_json", side_effect=self._save
        )
        self.load_patch.start()
        self.save_patch.start()

    def tearDown(self):
        self.load_patch.stop()
        self.save_patch.stop()

    def _save(self, path, value):
        self.store = value

    async def test_normal_watch_add_list_and_remove(self):
        resolved = Mock(return_value=("abc123", "JA0001", "B77W"))
        with patch.object(discord_bot, "resolve_personal_aircraft", resolved):
            await discord_bot.my_watch_add.callback(self.ctx, "JA0001")
        entry = self.store["123"]["aircraft"]["abc123"]
        self.assertEqual(entry["priority"], "NORMAL")
        await discord_bot.my_watch_list.callback(self.ctx)
        self.assertIn("JA0001", self.ctx.messages[-1])
        await discord_bot.my_watch_remove.callback(self.ctx, "JA0001")
        self.assertEqual(self.store["123"]["aircraft"], {})

    async def test_special_add_keeps_special_priority(self):
        with patch.object(
            discord_bot, "resolve_personal_aircraft", return_value=("abc123", "JA0001", "B77W")
        ):
            await discord_bot.my_special_add.callback(self.ctx, "JA0001")
        self.assertEqual(self.store["123"]["aircraft"]["abc123"]["priority"], "SPECIAL")

    async def test_region_command_sets_and_clears_regions(self):
        await discord_bot.my_watch_regions.callback(self.ctx, "関東", "中部")
        self.assertEqual(self.store["123"]["regions"], ["kanto", "chubu"])
        await discord_bot.my_watch_regions.callback(self.ctx, "all")
        self.assertEqual(self.store["123"]["regions"], [])

    async def test_quiet_hours_command_sets_and_clears_period(self):
        await discord_bot.my_watch_quiet.callback(self.ctx, "23:00", "07:00")
        self.assertEqual(self.store["123"]["quiet_hours"], {"start": "23:00", "end": "07:00"})
        await discord_bot.my_watch_quiet.callback(self.ctx, "off")
        self.assertNotIn("quiet_hours", self.store["123"])

    async def test_filter_commands_set_show_and_reset(self):
        await discord_bot.my_watch_filter.callback(self.ctx, "status", "airborne")
        await discord_bot.my_watch_filter.callback(self.ctx, "airline", "ANA", "JAL")
        await discord_bot.my_watch_filter.callback(self.ctx, "type", "B77W", "A359")
        filters = self.store["123"]["filters"]
        self.assertEqual(filters["status"], "airborne")
        self.assertEqual(filters["airlines"], ["ANA", "JAL"])
        self.assertEqual(filters["types"], ["B77W", "A359"])
        await discord_bot.my_watch_filter.callback(self.ctx, "show")
        self.assertIn("ANA, JAL", self.ctx.messages[-1])
        await discord_bot.my_watch_filter.callback(self.ctx, "reset")
        self.assertNotIn("filters", self.store["123"])

    async def test_settings_command_turns_notifications_off_and_on(self):
        await discord_bot.my_special_settings.callback(self.ctx, "off")
        self.assertFalse(self.store["123"]["enabled"])
        await discord_bot.my_special_settings.callback(self.ctx, "on")
        self.assertTrue(self.store["123"]["enabled"])

    async def test_airport_watch_add_list_and_remove(self):
        await discord_bot.my_airport_add.callback(self.ctx, "HND", 75)
        self.assertEqual(self.store["123"]["airports"]["HND"]["radius_km"], 75)
        await discord_bot.my_airport_list.callback(self.ctx)
        self.assertIn("HND", self.ctx.messages[-1])
        await discord_bot.my_airport_remove.callback(self.ctx, "HND")
        self.assertEqual(self.store["123"]["airports"], {})

    def test_data_quality_finds_invalid_duplicate_and_unknown_entries(self):
        anomalies = discord_bot.find_watchlist_anomalies({
            "bad": {"label": "JA0001", "type": "不明"},
            "abc123": {"label": "JA0001", "type": "B77W"},
        })
        self.assertTrue(any("不正なICAO24" in item for item in anomalies))
        self.assertTrue(any("機種不明" in item for item in anomalies))
        self.assertTrue(any("登録記号重複" in item for item in anomalies))


if __name__ == "__main__":
    unittest.main()
