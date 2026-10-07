import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    def test_personal_alert_buttons_have_worker_routable_ids(self):
        view = discord_bot.PersonalAlertView(123, "87C003")
        custom_ids = {item.label: item.custom_id for item in view.children if item.custom_id}
        self.assertEqual(
            custom_ids["この機体の通知を停止"],
            "personal_alert|stop|123|87c003|",
        )
        self.assertEqual(
            custom_ids["1時間休止"],
            "personal_alert|mute|123|1h",
        )
        self.assertEqual(custom_ids["6時間休止"], "personal_alert|mute|123|6h")
        self.assertEqual(
            custom_ids["翌朝7時まで休止"], "personal_alert|mute|123|morning"
        )
        self.assertEqual(custom_ids["24時間休止"], "personal_alert|mute|123|24h")

    def test_personal_alert_bridge_actions_update_settings(self):
        self.store = {
            "123": {
                "aircraft": {"87c003": {"label": "80-1112"}},
                "airports": {"CTS": {"radius_km": 50}},
            }
        }
        result = discord_bot.apply_personal_alert_action(123, "alert_stop", "87c003")
        self.assertIn("停止しました", result)
        self.assertEqual(self.store["123"]["aircraft"], {})
        result = discord_bot.apply_personal_alert_action(123, "alert_mute")
        self.assertIn("24時間休止", result)
        self.assertGreater(self.store["123"]["muted_until"], 0)

    def test_personal_alert_queue_marker_is_parsed(self):
        queued = discord_bot.parse_personal_alert_bridge(
            "⏳ 通知設定の変更を受け付けました。\n"
            "||__PERSONAL_ALERT__|123|alert_stop|456789|87c003|CTS||"
        )
        self.assertEqual(queued["user_id"], 123)
        self.assertEqual(queued["operation"], "alert_stop")
        self.assertEqual(queued["action_id"], "456789")
        self.assertEqual(queued["args"], ["87c003", "CTS"])

    def test_mute_replay_keeps_the_same_deadline(self):
        self.store = {"123": {"aircraft": {}}}
        clicked_at = 1_700_000_000
        action_id = str(((clicked_at * 1000) - 1420070400000) << 22)
        discord_bot.apply_personal_alert_action(123, "alert_mute", action_id=action_id)
        first_deadline = self.store["123"]["muted_until"]
        discord_bot.apply_personal_alert_action(123, "alert_mute", action_id=action_id)
        self.assertEqual(self.store["123"]["muted_until"], first_deadline)
        self.assertEqual(first_deadline, clicked_at + 24 * 3600)

    def test_all_personal_mute_durations(self):
        clicked_at = 1_700_000_000
        action_id = str(((clicked_at * 1000) - 1420070400000) << 22)
        for duration, seconds, text in (
            ("1h", 3600, "1時間"),
            ("6h", 6 * 3600, "6時間"),
            ("24h", 24 * 3600, "24時間"),
        ):
            result = discord_bot.apply_personal_alert_action(
                123, "alert_mute", duration, action_id=action_id
            )
            self.assertEqual(self.store["123"]["muted_until"], clicked_at + seconds)
            self.assertIn(text, result)

    def test_mute_until_next_morning_uses_japan_time(self):
        jst = timezone(timedelta(hours=9))
        clicked = datetime(2026, 10, 6, 20, 0, tzinfo=jst).timestamp()
        deadline, label = discord_bot.personal_mute_deadline("morning", clicked)
        self.assertEqual(
            datetime.fromtimestamp(deadline, tz=jst),
            datetime(2026, 10, 7, 7, 0, tzinfo=jst),
        )
        self.assertEqual(label, "翌朝7時まで")

    async def test_flight_command_explains_adsb_outage(self):
        with patch.object(
            discord_bot, "find_live_aircraft", side_effect=discord_bot.AdsbUnavailableError
        ):
            await discord_bot.flight_lookup.callback(self.ctx, query="JL123")
        self.assertIn("現在一時的に利用できません", self.ctx.messages[-1])

    def test_registration_lookup_uses_current_hexdb_endpoint(self):
        response = Mock(status_code=200, text="4010EE")
        with patch.object(discord_bot.requests, "get", return_value=response) as get:
            self.assertEqual(discord_bot.lookup_icao24("g-ezbz"), "4010ee")
        get.assert_called_once_with(
            "https://hexdb.io/reg-hex", params={"reg": "G-EZBZ"}, timeout=5
        )

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

    def test_data_quality_uses_shared_watchlist(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "3c4a15": {"label": "D-ABPU", "type": "BOEING 787-9 Dreamliner (B789)"}
        }
        with patch.object(discord_bot.requests, "get", return_value=response) as get:
            watchlist = discord_bot.load_shared_watchlist_for_quality_check()
        self.assertEqual(watchlist["3c4a15"]["type"], "BOEING 787-9 Dreamliner (B789)")
        get.assert_called_once_with(
            discord_bot.SHARED_WATCHLIST_URL,
            headers={
                "Accept": "application/vnd.github.raw+json",
                "User-Agent": "aircraft-alert-bot/1.0",
            },
            timeout=8,
        )

    def test_data_quality_skips_when_shared_watchlist_is_unavailable(self):
        with patch.object(
            discord_bot.requests,
            "get",
            side_effect=discord_bot.requests.RequestException("offline"),
        ):
            self.assertIsNone(discord_bot.load_shared_watchlist_for_quality_check())

    async def test_personal_channel_uses_second_category_when_first_is_full(self):
        first = SimpleNamespace(name="🔒｜個人設定", channels=[object()] * 50)
        second = SimpleNamespace(name="🔒｜個人設定2", channels=[])
        panel = SimpleNamespace(pin=AsyncMock())
        created_channel = SimpleNamespace(send=AsyncMock(return_value=panel))
        guild = SimpleNamespace(
            text_channels=[], categories=[first, second], default_role=Mock(), me=Mock(),
            create_category=AsyncMock(), create_text_channel=AsyncMock(return_value=created_channel),
        )
        member = Mock(id=456, display_name="User")

        channel, created = await discord_bot.create_personal_settings_channel(guild, member)

        self.assertIs(channel, created_channel)
        self.assertTrue(created)
        self.assertIs(guild.create_text_channel.await_args.kwargs["category"], second)
        guild.create_category.assert_not_awaited()

    async def test_personal_channel_stops_at_150_users(self):
        channels = [SimpleNamespace(topic=f"aircraft-personal-panel:{index}") for index in range(150)]
        guild = SimpleNamespace(text_channels=channels, categories=[])
        member = SimpleNamespace(id=999, display_name="User")

        channel, created = await discord_bot.create_personal_settings_channel(guild, member)

        self.assertIsNone(channel)
        self.assertFalse(created)

    async def test_personal_channel_limit_notifies_owner_once_per_hour(self):
        owner = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=10, name="Test Server")
        member = SimpleNamespace(id=999, display_name="Limit User")
        discord_bot._personal_limit_notified_at.clear()
        with (
            patch.object(discord_bot.bot, "is_ready", return_value=True),
            patch.object(discord_bot.bot, "is_closed", return_value=False),
            patch.object(discord_bot.bot, "fetch_user", AsyncMock(return_value=owner)) as fetch_user,
        ):
            await discord_bot.notify_personal_channel_limit(guild, member, 150)
            await discord_bot.notify_personal_channel_limit(guild, member, 150)

        fetch_user.assert_awaited_once_with(discord_bot.FEEDBACK_OWNER_ID)
        owner.send.assert_awaited_once()


class PersonalDataDeletionTests(unittest.TestCase):
    def test_delete_personal_notification_data_keeps_other_users_and_server_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = {
                "PERSONAL_SPECIALS_PATH": os.path.join(temp_dir, "personal.json"),
                "PERSONAL_SPECIAL_EVENTS_PATH": os.path.join(temp_dir, "events.json"),
                "PERSONAL_SPECIAL_NOTIFIED_PATH": os.path.join(temp_dir, "notified.json"),
                "DESTINATION_ALERTS_PATH": os.path.join(temp_dir, "destinations.json"),
                "EQUIPMENT_ALERTS_PATH": os.path.join(temp_dir, "equipment.json"),
            }
            fixtures = {
                "PERSONAL_SPECIALS_PATH": {"123": {"aircraft": {"abc123": {}}}, "456": {"aircraft": {"def456": {}}}},
                "PERSONAL_SPECIAL_EVENTS_PATH": [{"user_id": "123"}, {"user_id": "456"}],
                "PERSONAL_SPECIAL_NOTIFIED_PATH": {"123:abc123": 1, "456:def456": 2},
                "DESTINATION_ALERTS_PATH": {"next_id": 4, "rules": [
                    {"id": 1, "scope": "personal", "owner_id": "123"},
                    {"id": 2, "scope": "personal", "owner_id": "456"},
                    {"id": 3, "scope": "server", "owner_id": "0"},
                ], "notified": {"opaque": 1}},
                "EQUIPMENT_ALERTS_PATH": {"next_id": 4, "rules": [
                    {"id": 1, "scope": "personal", "owner_id": "123"},
                    {"id": 2, "scope": "personal", "owner_id": "456"},
                    {"id": 3, "scope": "server", "owner_id": "0"},
                ], "notified": {"1:today": 1, "2:today": 2, "3:today": 3}},
            }
            for name, value in fixtures.items():
                with open(paths[name], "w", encoding="utf-8") as handle:
                    json.dump(value, handle)
            with patch.multiple(discord_bot, **paths):
                removed = discord_bot.delete_personal_notification_data(123)
            self.assertEqual(removed["settings"], 1)
            self.assertEqual(removed["destinations"], 1)
            self.assertEqual(removed["equipment"], 1)
            with open(paths["PERSONAL_SPECIALS_PATH"], encoding="utf-8") as handle:
                self.assertEqual(set(json.load(handle)), {"456"})
            with open(paths["PERSONAL_SPECIAL_EVENTS_PATH"], encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), [{"user_id": "456"}])
            with open(paths["PERSONAL_SPECIAL_NOTIFIED_PATH"], encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"456:def456": 2})
            with open(paths["DESTINATION_ALERTS_PATH"], encoding="utf-8") as handle:
                self.assertEqual([rule["id"] for rule in json.load(handle)["rules"]], [2, 3])
            with open(paths["EQUIPMENT_ALERTS_PATH"], encoding="utf-8") as handle:
                equipment = json.load(handle)
            self.assertEqual([rule["id"] for rule in equipment["rules"]], [2, 3])
            self.assertEqual(set(equipment["notified"]), {"2:today", "3:today"})


if __name__ == "__main__":
    unittest.main()
