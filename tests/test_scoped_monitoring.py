from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.poller import FeedPoller
from test_category_only_monitoring import FakeBot, build_entry, build_settings


class ScopedMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.db = Database(self.path)
        await self.db.init()
        await self.db.ensure_user_profile(1, 1001, None, "Test", "Test", "private")
        await self.db.set_user_categories(1, ["tech", "trade", "daily"])
        self.poller = FeedPoller(
            build_settings(self.path, mark_as_read_on_first_poll=False), self.db
        )
        self.bot = FakeBot()

    async def poll(self, entries) -> None:
        for user in await self.db.get_polling_users():
            await self.poller._handle_user(self.bot, user, entries)

    async def test_each_category_uses_only_its_own_rules_and_empty_category_is_full(self) -> None:
        await self.db.add_keyword(1, "oracle", category_slug="tech")
        await self.db.add_keyword(1, "dmit", category_slug="trade")
        entries = [
            build_entry("1", "tech", "技术命中", "oracle"),
            build_entry("2", "tech", "错误的交易关键词", "dmit"),
            build_entry("3", "trade", "交易命中", "dmit"),
            build_entry("4", "trade", "错误的技术关键词", "oracle"),
            build_entry("5", "daily", "日常全量", "普通内容"),
            build_entry("6", "info", "未选情报", "oracle dmit"),
        ]
        await self.poll(entries)
        await self.poll(entries)
        self.assertEqual(3, len(self.bot.messages))
        history = await self.db.list_history_by_tg_user(1, 10)
        self.assertEqual({"1", "3", "5"}, {item.item_key for item in history})

    async def test_same_word_can_have_separate_ids_switches_and_hits(self) -> None:
        added, tech = await self.db.add_keyword(1, "oracle", category_slug="tech")
        self.assertTrue(added)
        added, trade = await self.db.add_keyword(1, "oracle", category_slug="trade")
        self.assertTrue(added)
        self.assertNotEqual(tech.id, trade.id)
        duplicate, _ = await self.db.add_keyword(1, "ORACLE", category_slug="tech")
        self.assertFalse(duplicate)
        await self.poll([build_entry("1", "tech", "技术", "oracle")])
        rules = {rule.id: rule for rule in await self.db.list_keywords_by_tg_user(1)}
        self.assertEqual(1, rules[tech.id].hit_count)
        self.assertEqual(0, rules[trade.id].hit_count)
        await self.db.set_keyword_enabled(1, tech.id, False)
        await self.poll([
            build_entry("2", "tech", "关闭技术", "oracle"),
            build_entry("3", "trade", "交易", "oracle"),
            build_entry("4", "daily", "仍全量", "普通"),
        ])
        self.assertEqual(3, len(self.bot.messages))

    async def test_disabled_rules_do_not_disable_other_category_full_monitoring(self) -> None:
        _, rule = await self.db.add_keyword(1, "oracle", category_slug="tech")
        await self.db.set_keyword_enabled(1, rule.id, False)
        self.assertEqual(1, len(await self.db.get_polling_users()))
        await self.db.set_user_categories(1, ["tech"])
        self.assertEqual([], await self.db.get_polling_users())
        await self.db.delete_keyword(1, rule.id)
        await self.poll([build_entry("1", "tech", "删光后全量", "普通")])
        self.assertEqual(1, len(self.bot.messages))

    async def test_unselected_rules_do_not_affect_selected_categories(self) -> None:
        await self.db.add_keyword(1, "oracle", category_slug="tech")
        await self.db.set_user_categories(1, ["daily"])
        await self.poll([
            build_entry("1", "tech", "未选", "oracle"),
            build_entry("2", "daily", "已选全量", "普通"),
        ])
        self.assertEqual(1, len(self.bot.messages))
        self.assertIn("已选全量", self.bot.messages[0]["text"])

    async def test_combo_and_blocks_apply_to_full_content_in_the_right_category(self) -> None:
        await self.db.add_keyword(
            1, "dmit + corona", required_terms=["dmit", "corona"], category_slug="trade"
        )
        await self.db.add_block_keyword(1, "spam")
        await self.poll([
            build_entry("1", "trade", "缺词", "dmit"),
            build_entry("2", "trade", "组合", "DMIT corona"),
            build_entry("3", "trade", "屏蔽组合", "dmit corona spam"),
            build_entry("4", "daily", "屏蔽全量", "spam"),
        ])
        self.assertEqual(1, len(self.bot.messages))

    async def test_legacy_global_rules_keep_filtering_until_rebound(self) -> None:
        _, legacy = await self.db.add_keyword(1, "oracle")
        await self.poll([build_entry("1", "daily", "旧规则未命中", "普通")])
        self.assertEqual([], self.bot.messages)
        self.assertTrue(await self.db.set_keyword_category(1, legacy.id, "tech"))
        await self.poll([build_entry("2", "daily", "改绑后全量", "普通")])
        self.assertEqual(1, len(self.bot.messages))

    async def test_first_poll_pause_and_restart_keep_existing_behavior(self) -> None:
        await self.db.add_keyword(1, "oracle", category_slug="tech")
        self.poller = FeedPoller(
            build_settings(self.path, mark_as_read_on_first_poll=True), self.db
        )
        old = build_entry("1", "tech", "旧帖", "oracle")
        await self.poll([old])
        self.assertEqual([], self.bot.messages)
        await self.db.set_user_enabled(1, False)
        await self.poll([build_entry("2", "tech", "暂停", "oracle")])
        self.assertEqual([], self.bot.messages)
        await self.db.set_user_enabled(1, True)
        self.db = Database(self.path)
        await self.db.init()
        self.poller.db = self.db
        await self.poll([old, build_entry("3", "tech", "新帖", "oracle")])
        self.assertEqual(1, len(self.bot.messages))

    async def test_rebinding_is_user_scoped_and_conflicts_preserve_both_rules(self) -> None:
        _, tech = await self.db.add_keyword(1, "oracle", category_slug="tech")
        _, trade = await self.db.add_keyword(1, "oracle", category_slug="trade")
        self.assertFalse(await self.db.set_keyword_category(2, tech.id, "daily"))
        self.assertFalse(await self.db.set_keyword_category(1, tech.id, "trade"))
        rules = {rule.id: rule for rule in await self.db.list_keywords_by_tg_user(1)}
        self.assertEqual("tech", rules[tech.id].category_slug)
        self.assertEqual("trade", rules[trade.id].category_slug)

    async def test_deselecting_every_category_stops_scoped_rules(self) -> None:
        await self.db.add_keyword(1, "oracle", category_slug="tech")
        await self.db.set_user_categories(1, [])
        self.assertEqual([], await self.db.get_polling_users())
        await self.db.add_keyword(1, "dmit")
        await self.poll([
            build_entry("1", "tech", "关闭的独立规则", "oracle"),
            build_entry("2", "daily", "兼容旧通用规则", "dmit"),
        ])
        self.assertEqual(1, len(self.bot.messages))
        self.assertIn("兼容旧通用规则", self.bot.messages[0]["text"])


if __name__ == "__main__":
    unittest.main()
