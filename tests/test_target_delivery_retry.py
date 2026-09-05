from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from telegram.error import Forbidden

from app.db import Database
from app.poller import FeedPoller
from test_category_only_monitoring import build_entry, build_settings


class FailingBot:
    def __init__(self, failures: dict[int, list[Exception]]) -> None:
        self.failures = failures
        self.attempts: list[int] = []

    async def send_message(self, **kwargs) -> None:
        chat_id = kwargs["chat_id"]
        self.attempts.append(chat_id)
        failures = self.failures.get(chat_id, [])
        if failures:
            raise failures.pop(0)


class TargetDeliveryRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        previous_logging_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_logging_level)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.db = Database(self.path)
        await self.db.init()
        await self.db.ensure_user_profile(1, 1001, None, "Test", "Primary", "private")
        await self.db.add_target_by_tg_user(
            1, 1002, chat_title="Secondary", chat_type="private"
        )
        self.target_ids = {
            target.chat_id: target.id for target in await self.db.list_targets_by_tg_user(1)
        }
        await self.db.set_user_categories(1, ["tech"])
        _, self.rule = await self.db.add_keyword(1, "oracle", category_slug="tech")
        self.entry = build_entry("retry", "tech", "Oracle post", "oracle content")
        self.poller = FeedPoller(
            build_settings(self.path, mark_as_read_on_first_poll=False), self.db
        )

    async def poll(self, bot, entries=None) -> None:
        users = await self.db.get_polling_users()
        self.assertEqual(1, len(users))
        await self.poller._handle_user(
            bot,
            users[0],
            [self.entry] if entries is None else entries,
        )

    async def test_partial_failure_retries_only_failed_target_after_restart(self) -> None:
        bot = FailingBot({1002: [TimeoutError("temporary")]})

        await self.poll(bot)
        self.assertEqual([1001, 1002], bot.attempts)
        history = await self.db.list_history_by_tg_user(1, 10)
        self.assertEqual(["retry"], [item.item_key for item in history])
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual(1, rules[0].hit_count)

        self.db = Database(self.path)
        await self.db.init()
        self.poller.db = self.db
        await self.poll(bot, [])
        self.assertEqual([1001, 1002, 1002], bot.attempts)
        await self.poll(bot, [])
        self.assertEqual([1001, 1002, 1002], bot.attempts)
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual(1, rules[0].hit_count)
        self.assertEqual(
            {self.target_ids[1001]: "sent", self.target_ids[1002]: "sent"},
            await self.db.get_target_delivery_states(1, "retry"),
        )

    async def test_all_temporary_failures_retry_and_create_one_history_record(self) -> None:
        bot = FailingBot(
            {1001: [TimeoutError("temporary")], 1002: [TimeoutError("temporary")]}
        )

        await self.poll(bot)
        self.assertEqual([], await self.db.list_history_by_tg_user(1, 10))
        self.assertTrue(await self.db.is_delivered(1, "retry"))
        self.assertEqual(
            {self.target_ids[1001]: "pending", self.target_ids[1002]: "pending"},
            await self.db.get_target_delivery_states(1, "retry"),
        )

        await self.poll(bot, [])
        self.assertEqual([1001, 1002, 1001, 1002], bot.attempts)
        self.assertEqual(1, len(await self.db.list_history_by_tg_user(1, 10)))
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual(1, rules[0].hit_count)

    async def test_forbidden_target_is_disabled_and_not_retried(self) -> None:
        bot = FailingBot({1002: [Forbidden("bot was blocked")]})

        await self.poll(bot)
        await self.poll(bot, [])

        self.assertEqual([1001, 1002], bot.attempts)
        targets = {target.chat_id: target for target in await self.db.list_targets_by_tg_user(1)}
        self.assertTrue(targets[1001].enabled)
        self.assertFalse(targets[1002].enabled)
        self.assertEqual(
            {self.target_ids[1001]: "sent", self.target_ids[1002]: "disabled"},
            await self.db.get_target_delivery_states(1, "retry"),
        )

    async def test_target_added_after_delivery_does_not_receive_old_post(self) -> None:
        targets = await self.db.list_targets_by_tg_user(1)
        secondary = next(target for target in targets if target.chat_id == 1002)
        await self.db.delete_target(1, secondary.id)
        bot = FailingBot({})

        await self.poll(bot)
        await self.db.add_target_by_tg_user(1, 1003, chat_title="New", chat_type="private")
        await self.poll(bot)

        self.assertEqual([1001], bot.attempts)
        self.assertEqual(
            {self.target_ids[1001]: "sent"},
            await self.db.get_target_delivery_states(1, "retry"),
        )

    async def test_pending_delivery_stops_while_category_is_deselected(self) -> None:
        bot = FailingBot({1002: [TimeoutError("temporary")]})
        await self.poll(bot)
        await self.db.set_user_categories(1, [])

        self.assertEqual([], await self.db.get_polling_users())
        await self.db.set_user_categories(1, ["tech"])
        await self.poll(bot)
        self.assertEqual([1001, 1002, 1002], bot.attempts)


if __name__ == "__main__":
    unittest.main()
