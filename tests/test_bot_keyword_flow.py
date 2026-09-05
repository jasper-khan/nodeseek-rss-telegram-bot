from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import BotHandlers, build_application
from app.categories import CATEGORY_ORDER
from app.db import Database
from app.poller import FeedPoller
from app.rss import FeedFetchResult
from test_category_only_monitoring import FakeBot, build_entry, build_settings


def make_update(text: str = "", *, user_id: int = 1, chat_id: int = 1001, message_id: int = 1,
                callback: str | None = None):
    message = SimpleNamespace(
        text=text, message_id=message_id,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=message_id + 100)),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=None, first_name="Test", full_name="Test"),
        effective_chat=SimpleNamespace(id=chat_id, type="private", title="Test"),
        effective_message=message,
        callback_query=SimpleNamespace(
            data=callback, message=message, answer=AsyncMock(), edit_message_text=AsyncMock()
        ) if callback else None,
    )


class BotKeywordFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.db = Database(self.path)
        await self.db.init()
        self.settings = build_settings(self.path, mark_as_read_on_first_poll=False)
        self.handlers = BotHandlers(self.settings, self.db)
        self.context = SimpleNamespace(user_data={})
        await self.handlers.scope(make_update("/scope tech,trade,daily"), self.context)

    async def choose(self, slug: str):
        draft = self.context.user_data["keyword_draft"]
        update = make_update(callback=f"kwcategory:1:{slug}", message_id=draft["message_id"])
        await self.handlers.keyword_callback(update, self.context)
        return update

    async def test_menu_choose_category_then_type_saves_only_that_category(self) -> None:
        update = make_update("新建关键词")
        await self.handlers.handle_text_message(update, self.context)
        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        self.assertEqual({"技术", "交易", "日常"}, {row[0].text for row in markup.inline_keyboard})
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))
        await self.choose("tech")
        await self.handlers.handle_text_message(make_update("oracle,免费鸡"), self.context)
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual(["tech", "tech"], [rule.category_slug for rule in rules])
        self.assertEqual(["oracle", "免费鸡"], [rule.keyword for rule in rules])
        self.assertNotIn("keyword_draft", self.context.user_data)

    async def test_old_command_syntax_prompts_instead_of_adding_global_rules(self) -> None:
        await self.handlers.keywords(make_update("/keywords oracle"), self.context)
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))
        await self.choose("trade")
        rule = (await self.db.list_keywords_by_tg_user(1))[0]
        self.assertEqual(("oracle", "trade"), (rule.keyword, rule.category_slug))

    async def test_direct_commands_and_combinations_accept_category_aliases(self) -> None:
        await self.handlers.keywords(make_update("/keywords 技术 oracle"), self.context)
        await self.handlers.addkw(make_update("/addkw trade oracle"), self.context)
        await self.handlers.combo(make_update("/combo trade DMIT,corona"), self.context)
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual(["tech", "trade", "trade"], [rule.category_slug for rule in rules])
        self.assertEqual("dmit,corona", rules[-1].required_keywords)

    async def test_combo_without_category_keeps_all_terms_in_the_picker_flow(self) -> None:
        await self.handlers.combo(make_update("/combo dmit,corona"), self.context)
        await self.choose("trade")
        rule = (await self.db.list_keywords_by_tg_user(1))[0]
        self.assertEqual(("trade", "dmit,corona"), (rule.category_slug, rule.required_keywords))

    async def test_unselected_category_and_cancel_do_not_write_rules(self) -> None:
        await self.handlers.keywords(make_update("/keywords info oracle"), self.context)
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))
        await self.handlers.handle_text_message(make_update("新建关键词"), self.context)
        stale_id = self.context.user_data["keyword_draft"]["message_id"]
        await self.handlers.cancel(make_update("/cancel"), self.context)
        stale = make_update(callback="kwcategory:1:tech", message_id=stale_id)
        await self.handlers.keyword_callback(stale, self.context)
        self.assertTrue(stale.callback_query.answer.call_args.kwargs["show_alert"])
        await self.handlers.handle_text_message(make_update("oracle"), self.context)
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))

    async def test_category_removed_while_typing_cannot_receive_a_new_rule(self) -> None:
        await self.handlers.handle_text_message(make_update("新建关键词"), self.context)
        await self.choose("tech")
        await self.db.set_user_categories(1, ["daily"])
        await self.handlers.handle_text_message(make_update("oracle"), self.context)
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))

    async def test_old_category_button_cannot_change_a_draft_after_selection(self) -> None:
        await self.handlers.handle_text_message(make_update("新建关键词"), self.context)
        await self.choose("tech")
        stale = make_update(
            callback="kwcategory:1:trade", message_id=self.context.user_data["keyword_draft"]["message_id"]
        )
        await self.handlers.keyword_callback(stale, self.context)
        self.assertEqual("tech", self.context.user_data["keyword_draft"]["category_slug"])
        self.assertTrue(stale.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_other_user_and_stale_menu_cannot_consume_current_draft(self) -> None:
        await self.handlers.keywords(make_update("/keywords oracle"), self.context)
        draft = self.context.user_data["keyword_draft"].copy()
        other = make_update(user_id=2, callback="kwcategory:1:trade", message_id=draft["message_id"])
        await self.handlers.keyword_callback(other, self.context)
        stale = make_update(callback="kwcategory:1:trade", message_id=draft["message_id"] + 1)
        await self.handlers.keyword_callback(stale, self.context)
        self.assertEqual(draft, self.context.user_data["keyword_draft"])
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))
        await self.choose("tech")
        self.assertEqual("tech", (await self.db.list_keywords_by_tg_user(1))[0].category_slug)

    async def test_text_from_another_chat_does_not_become_a_keyword(self) -> None:
        await self.handlers.handle_text_message(make_update("新建关键词"), self.context)
        await self.choose("tech")
        await self.handlers.handle_text_message(make_update("unrelated", chat_id=2002), self.context)
        self.assertEqual([], await self.db.list_keywords_by_tg_user(1))
        await self.handlers.handle_text_message(make_update("oracle"), self.context)
        self.assertEqual("oracle", (await self.db.list_keywords_by_tg_user(1))[0].keyword)

    async def test_legacy_rule_can_be_rebound_from_list_button_or_command(self) -> None:
        _, rule = await self.db.add_keyword(1, "oracle")
        listing = make_update("/keywords")
        await self.handlers.keywords(listing, self.context)
        self.assertIn("旧版通用", listing.effective_message.reply_text.call_args.args[0])
        callback = listing.effective_message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        await self.handlers.keyword_callback(make_update(callback=callback), self.context)
        await self.choose("tech")
        self.assertEqual("tech", (await self.db.list_keywords_by_tg_user(1))[0].category_slug)
        await self.handlers.kwscope(make_update(f"/kwscope {rule.id} trade"), self.context)
        self.assertEqual("trade", (await self.db.list_keywords_by_tg_user(1))[0].category_slug)

    async def test_status_explains_filtered_full_and_disabled_categories(self) -> None:
        await self.db.add_keyword(1, "oracle", category_slug="tech")
        _, trade = await self.db.add_keyword(1, "dmit", category_slug="trade")
        await self.db.set_keyword_enabled(1, trade.id, False)
        update = make_update("/status")
        await self.handlers.status(update, self.context)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("技术：关键词筛选（启用 1 条）", text)
        self.assertIn("日常：全量推送", text)
        self.assertIn("交易：规则全部关闭，不推送", text)

    async def test_explicit_all_selects_every_category_without_keywords(self) -> None:
        await self.handlers.scope(make_update("/scope all"), self.context)
        users = await self.db.get_polling_users()
        self.assertEqual(set(CATEGORY_ORDER), set(users[0].settings.category_slugs.split(",")))

    async def test_bot_configuration_to_polling_renders_content_and_deduplicates(self) -> None:
        await self.handlers.keywords(make_update("/keywords tech oracle"), self.context)
        await self.handlers.keywords(make_update("/keywords trade dmit"), self.context)
        tech = build_entry("1", "tech", "技术帖子", "oracle")
        tech.summary = "来自 RSS 的帖子内容"
        daily = build_entry("3", "daily", "日常帖子", "普通正文")
        daily.summary = "日常正文"
        wrong = build_entry("2", "trade", "不能跨板块命中", "oracle")
        poller = FeedPoller(self.settings, self.db)
        poller.feed_client.fetch = AsyncMock(return_value=FeedFetchResult("NodeSeek", [daily, wrong, tech]))
        bot = FakeBot()
        await poller.run_once(bot)
        await poller.run_once(bot)
        self.assertEqual(2, len(bot.messages))
        self.assertEqual(
            'NodeSeek 新帖提醒\n标题：<a href="https://www.nodeseek.com/post-1-1"><b>技术帖子</b></a>\n'
            '摘要：来自 RSS 的帖子内容', bot.messages[0]["text"]
        )
        self.assertEqual("HTML", bot.messages[0]["parse_mode"])
        self.assertIn("摘要：日常正文", bot.messages[1]["text"])

    async def test_application_registers_new_commands_and_callbacks(self) -> None:
        from dataclasses import replace
        app = build_application(replace(self.settings, bot_token="123456:fake-test-token"), self.db)
        commands = set().union(*(getattr(handler, "commands", set()) for handler in app.handlers[0]))
        self.assertTrue({"kwscope", "cancel", "keywords", "combo"}.issubset(commands))
        callback = make_update(callback="kwcategory:1:tech")
        self.assertTrue(any(
            getattr(handler, "pattern", None) and handler.pattern.match(callback.callback_query.data)
            for handler in app.handlers[0]
        ))
