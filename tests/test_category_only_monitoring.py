from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.categories import category_label
from app.config import Settings
from app.db import Database
from app.poller import FeedPoller
from app.rss import FeedEntry


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


def build_settings(database_path: Path, *, mark_as_read_on_first_poll: bool) -> Settings:
    return Settings(
        bot_token="test-token",
        database_path=database_path,
        rss_url="https://rss.nodeseek.com/",
        max_keywords_per_user=50,
        max_targets_per_user=10,
        history_limit=10,
        poll_interval_seconds=10,
        http_timeout_seconds=20,
        max_entries_per_feed=15,
        mark_as_read_on_first_poll=mark_as_read_on_first_poll,
        disable_web_page_preview=True,
        log_level="INFO",
        allowed_user_ids=(),
    )


def build_entry(item_key: str, category_slug: str, title: str, source_text: str) -> FeedEntry:
    return FeedEntry(
        item_key=item_key,
        title=title,
        link=f"https://www.nodeseek.com/post-{item_key}-1",
        summary="",
        published_at="",
        source_text=source_text.lower(),
        category_slug=category_slug,
        category_name=category_label(category_slug),
    )


class CategoryOnlyMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "bot.db"
        self.db = Database(self.database_path)
        await self.db.init()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def create_user(self, tg_user_id: int, categories: list[str]) -> None:
        await self.db.ensure_user_profile(
            tg_user_id=tg_user_id,
            chat_id=tg_user_id + 1000,
            username=None,
            first_name="Test",
            chat_title="Test chat",
            chat_type="private",
        )
        await self.db.set_user_categories(tg_user_id, categories)

    async def test_category_scope_without_keywords_is_polled(self) -> None:
        await self.create_user(1, ["tech"])
        await self.create_user(2, [])

        users = await self.db.get_polling_users()

        self.assertEqual([1], [item.user.tg_user_id for item in users])
        self.assertEqual([], users[0].keywords)

    async def test_category_only_sends_all_in_scope_and_applies_blocks(self) -> None:
        await self.create_user(1, ["tech"])
        added, _ = await self.db.add_block_keyword(1, "spam")
        self.assertTrue(added)

        user_record = (await self.db.get_polling_users())[0]
        poller = FeedPoller(
            build_settings(self.database_path, mark_as_read_on_first_poll=False),
            self.db,
        )
        bot = FakeBot()
        entries = [
            build_entry("1", "tech", "普通技术帖", "普通内容"),
            build_entry("2", "tech", "被屏蔽的技术帖", "contains spam"),
            build_entry("3", "daily", "日常帖", "普通内容"),
        ]

        await poller._handle_user(bot, user_record, entries)

        self.assertEqual(1, len(bot.messages))
        self.assertIn("普通技术帖", bot.messages[0]["text"])
        self.assertNotIn("日常帖", bot.messages[0]["text"])
        history = await self.db.list_history_by_tg_user(1, 10)
        self.assertEqual(1, len(history))
        self.assertEqual("板块全量", history[0].matched_keywords)
        self.assertTrue(await self.db.is_delivered(user_record.user.id, "2"))
        self.assertFalse(await self.db.is_delivered(user_record.user.id, "3"))

    async def test_disabled_keyword_rules_do_not_become_category_only_mode(self) -> None:
        await self.create_user(1, ["tech"])
        added, keyword = await self.db.add_keyword(1, "oracle")
        self.assertTrue(added)
        self.assertIsNotNone(keyword)
        await self.db.set_keyword_enabled(1, keyword.id, False)

        self.assertEqual([], await self.db.get_polling_users())
