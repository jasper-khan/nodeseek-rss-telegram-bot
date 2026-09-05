from __future__ import annotations

import asyncio
import logging

from telegram import Bot, LinkPreviewOptions
from telegram.error import Forbidden

from app.config import Settings
from app.db import Database, PendingTargetDeliveryRecord, PollingUserRecord
from app.formatter import MessageFormatter
from app.rss import FeedClient, match_keyword_rules, match_keywords

logger = logging.getLogger(__name__)


class FeedPoller:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.feed_client = FeedClient(
            timeout_seconds=settings.http_timeout_seconds,
            max_entries_per_feed=settings.max_entries_per_feed,
        )
        self.formatter = MessageFormatter()
        self._stopped = asyncio.Event()

    async def run_forever(self, bot: Bot) -> None:
        logger.info("Feed poller started with %s second interval", self.settings.poll_interval_seconds)
        while not self._stopped.is_set():
            try:
                await self.run_once(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error while polling feeds")

            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stopped.set()

    async def run_once(self, bot: Bot) -> None:
        users = await self.db.get_polling_users()
        if not users:
            return

        try:
            result = await self.feed_client.fetch(self.settings.rss_url)
        except Exception:
            logger.exception("Failed to fetch NodeSeek RSS")
            return

        entries = list(reversed(result.entries))
        semaphore = asyncio.Semaphore(8)

        async def handle_with_limit(user_record: PollingUserRecord) -> None:
            async with semaphore:
                await self._handle_user(bot, user_record, entries)

        await asyncio.gather(*(handle_with_limit(user_record) for user_record in users))

    async def _handle_user(self, bot: Bot, user_record: PollingUserRecord, entries) -> None:
        category_filter = {
            slug.strip()
            for slug in user_record.settings.category_slugs.split(",")
            if slug.strip()
        }
        if not user_record.has_active_monitoring():
            return

        block_keywords = [item.keyword for item in user_record.block_keywords]

        if not user_record.settings.initialized and self.settings.mark_as_read_on_first_poll:
            for entry in entries:
                await self.db.mark_delivered(
                    user_record.user.id,
                    entry.item_key,
                    title=entry.title,
                    link=entry.link,
                    category_slug=entry.category_slug,
                    matched_keywords=[],
                    delivery_status="read",
                )
            await self.db.set_user_initialized(user_record.user.id)
            logger.info("User %s initialized without backfill", user_record.user.tg_user_id)
            return

        disabled_target_ids: set[int] = set()
        pending_deliveries = await self.db.list_pending_target_deliveries(
            user_record.user.id
        )
        for pending in pending_deliveries:
            if (
                pending.scope_category_slug
                and pending.scope_category_slug not in category_filter
            ):
                continue
            await self._send_prepared_target(
                bot,
                user_record,
                pending,
                disabled_target_ids=disabled_target_ids,
            )

        for entry in entries:
            if category_filter and entry.category_slug not in category_filter:
                continue
            target_states = await self.db.get_target_delivery_states(
                user_record.user.id,
                entry.item_key,
            )
            if target_states:
                continue
            else:
                if await self.db.is_delivered(user_record.user.id, entry.item_key):
                    continue

                matched_blocks = match_keywords(entry.source_text, block_keywords)
                if matched_blocks:
                    await self.db.mark_delivered(
                        user_record.user.id,
                        entry.item_key,
                        title=entry.title,
                        link=entry.link,
                        category_slug=entry.category_slug,
                        matched_keywords=matched_blocks,
                        delivery_status="blocked",
                    )
                    await self.db.bump_block_keyword_hits(
                        user_record.user.id,
                        [keyword.lower() for keyword in matched_blocks],
                    )
                    continue

                rules = user_record.rules_for_category(entry.category_slug)
                if not rules:
                    if not category_filter:
                        continue
                    matched_keywords = ["板块全量"]
                    matched_keyword_ids = []
                    scope_category_slug = entry.category_slug or ""
                else:
                    enabled_keywords = [rule for rule in rules if rule.enabled]
                    matched_rules = match_keyword_rules(entry.source_text, enabled_keywords)
                    if not matched_rules:
                        continue
                    matched_keywords = [rule.keyword for rule in matched_rules]
                    matched_keyword_ids = [rule.id for rule in matched_rules]
                    scope_category_slug = (
                        ""
                        if any(not rule.category_slug for rule in matched_rules)
                        else entry.category_slug or ""
                    )

                targets = list(user_record.targets)
                message = self.formatter.render(
                    title=entry.title,
                    link=entry.link,
                    summary=entry.summary,
                )
                await self.db.prepare_target_delivery(
                    user_record.user.id,
                    entry.item_key,
                    [target.id for target in targets],
                    title=entry.title,
                    link=entry.link,
                    category_slug=entry.category_slug,
                    matched_keywords=matched_keywords,
                    matched_keyword_ids=matched_keyword_ids,
                    scope_category_slug=scope_category_slug,
                    message_text=message,
                )

            for target in targets:
                await self._send_prepared_target(
                    bot,
                    user_record,
                    PendingTargetDeliveryRecord(
                        item_key=entry.item_key,
                        target_id=target.id,
                        chat_id=target.chat_id,
                        message_text=message,
                        link=entry.link,
                        scope_category_slug=scope_category_slug,
                    ),
                    disabled_target_ids=disabled_target_ids,
                )

        if not user_record.settings.initialized:
            await self.db.set_user_initialized(user_record.user.id)

    async def _send_prepared_target(
        self,
        bot: Bot,
        user_record: PollingUserRecord,
        delivery: PendingTargetDeliveryRecord,
        *,
        disabled_target_ids: set[int],
    ) -> None:
        if delivery.target_id in disabled_target_ids:
            return
        try:
            await bot.send_message(
                chat_id=delivery.chat_id,
                text=delivery.message_text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(
                    is_disabled=self.settings.disable_web_page_preview,
                    url=delivery.link or None,
                ),
            )
            await self.db.set_target_delivery_status(
                user_record.user.id,
                delivery.item_key,
                delivery.target_id,
                "sent",
            )
        except Forbidden as error:
            disabled_target_ids.add(delivery.target_id)
            await self.db.set_target_delivery_status(
                user_record.user.id,
                delivery.item_key,
                delivery.target_id,
                "disabled",
            )
            await self.db.set_target_enabled(
                user_record.user.id,
                delivery.target_id,
                False,
            )
            logger.warning(
                "Disabled target %s for user %s: %s",
                delivery.target_id,
                user_record.user.tg_user_id,
                error.message,
            )
        except Exception:
            logger.exception(
                "Failed to send item %s to user %s target %s",
                delivery.item_key,
                user_record.user.tg_user_id,
                delivery.chat_id,
            )
            return

        newly_delivered_keyword_ids = await self.db.finalize_target_delivery(
            user_record.user.id,
            delivery.item_key,
        )
        await self.db.bump_keyword_hits(
            user_record.user.id,
            newly_delivered_keyword_ids,
        )
