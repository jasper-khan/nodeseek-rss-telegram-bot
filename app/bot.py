from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.categories import CATEGORY_ORDER, category_label, normalize_category_slug
from app.config import Settings
from app.db import Database, UserRecord
from app.keyboard import build_category_keyboard, build_keyword_category_keyboard, build_main_menu
from app.utils import truncate_text

MAIN_MENU_TEXT = (
    "欢迎使用 NodeSeek 关键词监控 Bot。\n\n"
    "先选择监控版块，再为各版块单独添加关键词；未设置规则的已选版块会推送全部新帖。"
    "你还可以设置多个推送目标，并通过 /history 回看命中记录。"
)

HELP_TEXT = """
可用命令：
/start - 打开主菜单
/help - 查看帮助
/keywords - 查看我的关键词
/keywords <版块> <关键词1,关键词2> - 为一个已选版块添加关键词
/keywords <关键词1,关键词2> - 先通过按钮选择所属版块，再添加关键词
/addkw <版块> <关键词> - 添加单个关键词
/combo <版块> <关键词1,关键词2> - 添加组合规则，所有词都命中才提醒
/kwscope <关键词ID> <版块> - 修改关键词所属版块（列表里也有按钮）
/cancel - 取消当前关键词设置
/on <关键词ID> - 开启一个关键词
/off <关键词ID> - 关闭一个关键词
/delkw <关键词ID> - 删除一个关键词
/block <屏蔽词1,屏蔽词2> - 添加屏蔽词，命中后不推送
/blocks - 查看我的屏蔽词
/delblock <屏蔽词ID> - 删除一个屏蔽词
/scope - 通过按钮选择监控版块（支持多选）
/scope all - 监控全部版块
/scope <slug1,slug2> - 用 slug 设置版块，例如：/scope trade,inner
/targets - 查看当前推送目标
/addtarget - 将当前聊天加入推送目标；私聊里也可用 /addtarget <chat_id> 绑定群组或频道
/target <chat_id> - 已停用，请改用 /addtarget
/deltarget <目标ID> - 删除一个推送目标
/history - 查看最近命中的帖子
/status - 查看当前配置
/pause - 暂停提醒
/resume - 恢复提醒
/chatid - 查看当前聊天 ID

版块 slug：
daily, tech, info, review, trade, carpool, promo, life, dev, photo-share, expose, inner, sandbox

说明：
1. 普通关键词命中任意一个就提醒，组合规则需要所有词同时命中。
2. 默认第一次使用会跳过旧帖，只从后续新帖开始通知。
3. 默认会把你第一次对话的当前聊天加入推送目标。
4. 每个用户最多可配置多个推送目标，默认上限是 10 个。
5. 如启用白名单，只有服务端配置过的用户才能使用。
6. 每个已选版块独立匹配；没有适用规则时推送全部新帖，规则全部关闭时该版块不推送。屏蔽词仍对全部版块生效。
7. 旧版通用规则保留原范围，可在“我的关键词”中改绑板块。新建关键词必须选择一个板块。
8. 例如：/scope tech,trade,daily，然后 /keywords tech oracle、/keywords trade dmit；日常版块不加规则即全量推送。
""".strip()


def _split_keywords(payload: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for part in payload.split(","):
        keyword = part.strip()
        normalized = keyword.lower()
        if not keyword or normalized in seen:
            continue
        seen.add(normalized)
        result.append(keyword)
    return result


def _split_combo_terms(payload: str) -> list[str]:
    if "," in payload:
        parts = payload.split(",")
    else:
        parts = payload.split(" + ")

    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        keyword = part.strip()
        normalized = keyword.lower()
        if not keyword or normalized in seen:
            continue
        seen.add(normalized)
        result.append(keyword)
    return result


def _keyword_kind(required_keywords: str) -> str:
    terms = [item.strip() for item in required_keywords.split(",") if item.strip()]
    return "组合" if len(terms) > 1 else "关键词"


def _chat_display_name(chat_title: str | None, chat_type: str | None, chat_id: int) -> str:
    if chat_title:
        return chat_title
    if chat_type == "private":
        return f"私聊 {chat_id}"
    return str(chat_id)


@dataclass(slots=True)
class BotHandlers:
    settings: Settings
    db: Database

    async def _ensure_chat_admin(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        if not update.effective_chat or not update.effective_user or not update.effective_message:
            return False
        if update.effective_chat.type == "private":
            return True

        try:
            member = await context.bot.get_chat_member(
                chat_id=update.effective_chat.id,
                user_id=update.effective_user.id,
            )
        except Exception:
            await update.effective_message.reply_text("暂时无法校验你的管理员身份，请稍后再试。")
            return False

        if getattr(member, "status", "") in {"administrator", "creator", "owner"}:
            return True

        await update.effective_message.reply_text(
            "当前聊天是群组或频道，只有管理员才能把这里加入推送目标。"
        )
        return False

    async def _resolve_target_chat(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        requested_chat_id: int | None,
    ) -> tuple[int, str | None, str] | None:
        if not update.effective_message or not update.effective_chat:
            return None

        if requested_chat_id is None:
            if not await self._ensure_chat_admin(update, context):
                return None

            chat = update.effective_chat
            if chat.type == "private":
                chat_title = update.effective_user.full_name if update.effective_user else "私聊"
            else:
                chat_title = chat.title
            return chat.id, chat_title, chat.type

        if not update.effective_user:
            await update.effective_message.reply_text(
                "私聊绑定频道或群组时，需要由你的个人账号来操作。"
            )
            return None

        try:
            target_chat = await context.bot.get_chat(requested_chat_id)
        except Exception:
            await update.effective_message.reply_text(
                "无法访问这个目标 chat_id。\n"
                "请确认 chat_id 正确，并且 Bot 已经加入对应的群组或频道。"
            )
            return None

        try:
            requester_member = await context.bot.get_chat_member(
                chat_id=requested_chat_id,
                user_id=update.effective_user.id,
            )
        except Exception:
            await update.effective_message.reply_text(
                "暂时无法校验你在目标群组或频道里的管理员身份。"
            )
            return None

        if getattr(requester_member, "status", "") not in {"administrator", "creator", "owner"}:
            await update.effective_message.reply_text(
                "你不是这个群组或频道的管理员，不能把它加入推送目标。"
            )
            return None

        try:
            me = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(
                chat_id=requested_chat_id,
                user_id=me.id,
            )
        except Exception:
            await update.effective_message.reply_text(
                "Bot 还没有加入这个群组或频道，或当前权限不足。"
            )
            return None

        bot_status = getattr(bot_member, "status", "")
        if target_chat.type == "channel":
            allowed_statuses = {"administrator", "creator", "owner"}
            if bot_status not in allowed_statuses:
                await update.effective_message.reply_text(
                    "频道场景下，请先把 Bot 加为频道管理员，然后再执行：\n"
                    f"/addtarget {requested_chat_id}"
                )
                return None
        else:
            allowed_statuses = {"administrator", "creator", "owner", "member"}
            if bot_status not in allowed_statuses:
                await update.effective_message.reply_text(
                    "Bot 还没有加入这个群组，或当前权限不足。"
                )
                return None

        chat_title = getattr(target_chat, "title", None) or getattr(target_chat, "full_name", None)
        return requested_chat_id, chat_title, target_chat.type

    async def ensure_user(self, update: Update) -> UserRecord | None:
        if not update.effective_user or not update.effective_chat:
            raise RuntimeError("当前更新没有用户信息")
        if self.settings.allowed_user_ids and update.effective_user.id not in self.settings.allowed_user_ids:
            if update.effective_message:
                await update.effective_message.reply_text("这个 Bot 当前未开放给你使用。")
            return None

        chat = update.effective_chat
        if chat.type == "private":
            chat_title = update.effective_user.full_name
        else:
            chat_title = chat.title

        return await self.db.ensure_user_profile(
            tg_user_id=update.effective_user.id,
            chat_id=chat.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
            chat_title=chat_title,
            chat_type=chat.type,
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        context.user_data.pop("keyword_draft", None)
        await update.effective_message.reply_text(
            MAIN_MENU_TEXT,
            reply_markup=build_main_menu(),
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        await update.effective_message.reply_text(HELP_TEXT, reply_markup=build_main_menu())

    async def chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        await update.effective_message.reply_text(f"当前聊天 ID：{update.effective_chat.id}")

    async def addkw(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        if not payload:
            await self._send_keyword_category_selector(update, context)
            return
        await self._begin_keyword_input(update, context, payload)

    async def keywords(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return

        payload = update.effective_message.text.partition(" ")[2].strip()
        if payload:
            await self._begin_keyword_input(update, context, payload)
            return

        keywords = await self.db.list_keywords_by_tg_user(update.effective_user.id)
        if not keywords:
            await update.effective_message.reply_text(
                "你还没有关键词，已选版块会全量推送。\n"
                "可点击“新建关键词”按版块设置，或发送：/keywords tech oracle,免费鸡"
            )
            return

        for offset in range(0, len(keywords), 10):
            lines = ["我的关键词（旧版通用规则仍适用于所有已选版块）："]
            buttons = []
            for keyword in keywords[offset : offset + 10]:
                status = "开启" if keyword.enabled else "关闭"
                kind = _keyword_kind(keyword.required_keywords)
                scope = category_label(keyword.category_slug) if keyword.category_slug else "旧版通用"
                lines.append(
                    f"#{keyword.id} [{scope}] {truncate_text(keyword.keyword, 80)}"
                    f"（{kind}，{status}）命中 {keyword.hit_count} 次"
                )
                buttons.append([InlineKeyboardButton(
                    f"修改 #{keyword.id} 的板块",
                    callback_data=f"kwbind:{update.effective_user.id}:{keyword.id}",
                )])
            await update.effective_message.reply_text(
                "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
            )

    async def _begin_keyword_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str, *, combo: bool = False
    ) -> None:
        context.user_data.pop("keyword_draft", None)
        parts = payload.split(maxsplit=1)
        slug = normalize_category_slug(parts[0]) if len(parts) == 2 else None
        if slug:
            await self._add_keywords_from_text(
                update, update.effective_user.id, parts[1], slug, combo=combo
            )
        else:
            await self._send_keyword_category_selector(update, context, payload=payload, combo=combo)

    async def _send_keyword_category_selector(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *,
        payload: str = "", combo: bool = False, keyword_id: int | None = None,
    ) -> None:
        context.user_data.pop("keyword_draft", None)
        settings = await self.db.get_user_settings_by_tg_user(update.effective_user.id)
        selected = set(settings.category_slugs.split(",")) if settings else set()
        selected.intersection_update(CATEGORY_ORDER)
        if not selected:
            await update.effective_message.reply_text(
                "请先在“版块设置”选择要监控的版块，再设置关键词。也可发送 /scope tech,trade。"
            )
            return
        message = await update.effective_message.reply_text(
            "请选择关键词所属的一个版块（/cancel 取消）：",
            reply_markup=build_keyword_category_keyboard(selected, update.effective_user.id),
        )
        context.user_data["keyword_draft"] = {
            "payload": payload, "combo": combo, "keyword_id": keyword_id,
            "chat_id": update.effective_chat.id, "message_id": message.message_id,
        }

    async def keyword_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.message or not update.effective_user:
            return
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[1] != str(update.effective_user.id):
            await query.answer("请使用你自己的关键词菜单。", show_alert=True)
            return
        if await self.ensure_user(update) is None:
            await query.answer()
            return
        if parts[0] == "kwbind" and parts[2].isdigit():
            keyword_id = int(parts[2])
            rules = await self.db.list_keywords_by_tg_user(update.effective_user.id)
            if not any(rule.id == keyword_id for rule in rules):
                await query.answer("这个关键词已不存在。", show_alert=True)
                return
            await query.answer()
            await self._send_keyword_category_selector(update, context, keyword_id=keyword_id)
            return

        draft = context.user_data.get("keyword_draft")
        if (
            parts[0] != "kwcategory" or parts[2] not in CATEGORY_ORDER or not draft
            or draft.get("category_slug")
            or draft["chat_id"] != update.effective_chat.id
            or draft["message_id"] != query.message.message_id
        ):
            await query.answer("此菜单已失效，请重新设置关键词。", show_alert=True)
            return
        slug = parts[2]
        await query.answer()
        await query.edit_message_text(f"关键词所属版块：{category_label(slug)}")
        if draft["keyword_id"] is not None:
            context.user_data.pop("keyword_draft", None)
            await self._set_keyword_category(update, draft["keyword_id"], slug)
        elif draft["payload"]:
            context.user_data.pop("keyword_draft", None)
            await self._add_keywords_from_text(
                update, update.effective_user.id, draft["payload"], slug, combo=draft["combo"]
            )
        else:
            draft["category_slug"] = slug
            await update.effective_message.reply_text(
                "请输入组合规则的词，用英文逗号分隔，全部命中才提醒；/cancel 取消。"
                if draft["combo"] else "请输入关键词，多个词用英文逗号分隔；/cancel 取消。"
            )

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        context.user_data.pop("keyword_draft", None)
        await update.effective_message.reply_text("已取消关键词设置。")

    async def kwscope(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        parts = (update.effective_message.text or "").split()
        slug = normalize_category_slug(parts[2]) if len(parts) == 3 else None
        if not slug or not parts[1].isdigit():
            await update.effective_message.reply_text("请用：/kwscope <关键词ID> <版块>，例如 /kwscope 1 tech")
            return
        await self._set_keyword_category(update, int(parts[1]), slug)

    async def _set_keyword_category(self, update: Update, keyword_id: int, slug: str) -> None:
        if not await self._check_keyword_category(update, slug):
            return
        changed = await self.db.set_keyword_category(update.effective_user.id, keyword_id, slug)
        await update.effective_message.reply_text(
            f"已将 #{keyword_id} 改绑到{category_label(slug)}。原板块没有其他规则时会恢复全量推送。"
            if changed else "未改绑：关键词不存在，或该板块已有相同规则。"
        )

    async def _check_keyword_category(self, update: Update, slug: str) -> bool:
        settings = await self.db.get_user_settings_by_tg_user(update.effective_user.id)
        if not settings or slug not in settings.category_slugs.split(","):
            await update.effective_message.reply_text(
                f"{category_label(slug)}尚未选为监控版块，请先通过 /scope 选择，再设置关键词。"
            )
            return False
        return True

    async def _add_keywords_from_text(
        self,
        update: Update,
        tg_user_id: int,
        payload: str,
        category_slug: str,
        *,
        combo: bool = False,
    ) -> None:
        if not update.effective_message:
            return
        if not await self._check_keyword_category(update, category_slug):
            return
        keywords = _split_combo_terms(payload) if combo else _split_keywords(payload)
        if combo and len(keywords) < 2:
            await update.effective_message.reply_text("组合规则至少需要 2 个词，例如 /combo trade dmit,corona")
            return
        if not keywords:
            await update.effective_message.reply_text("没有识别到有效关键词。")
            return

        current_count = await self.db.count_keywords_by_tg_user(tg_user_id)
        if current_count >= self.settings.max_keywords_per_user:
            await update.effective_message.reply_text(
                "你已经达到关键词数量上限。\n"
                f"当前上限：{self.settings.max_keywords_per_user} 个"
            )
            return

        added: list[str] = []
        duplicates: list[str] = []
        rules = [keywords] if combo else [[keyword] for keyword in keywords]
        for terms in rules:
            if current_count >= self.settings.max_keywords_per_user:
                break
            keyword = " + ".join(terms)
            success, record = await self.db.add_keyword(
                tg_user_id, keyword, required_terms=terms, category_slug=category_slug
            )
            if not success or record is None:
                duplicates.append(keyword)
                continue
            if record.keyword.lower() != keyword.strip().lower():
                duplicates.append(keyword)
                continue
            current_count += 1
            added.append(record.keyword)

        if added:
            kind = "组合规则（全部词同时命中）" if combo else "关键词（任一词命中）"
            text = f"已为【{category_label(category_slug)}】添加{kind}：\n" + "\n".join(f"- {item}" for item in added)
            if duplicates:
                text += "\n\n以下关键词已存在，已跳过：\n" + "\n".join(f"- {item}" for item in duplicates)
            await update.effective_message.reply_text(text)
            return

        await update.effective_message.reply_text("这些关键词都已经存在了，无需重复添加。")

    async def combo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        await self._begin_keyword_input(update, context, payload, combo=True)

    async def block(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        keywords = _split_keywords(payload)
        if not keywords:
            await update.effective_message.reply_text(
                "请用：/block <屏蔽词1,屏蔽词2>\n例如：/block 求购,收"
            )
            return

        added: list[str] = []
        duplicates: list[str] = []
        for keyword in keywords:
            success, record = await self.db.add_block_keyword(update.effective_user.id, keyword)
            if not success or record is None:
                duplicates.append(keyword)
                continue
            added.append(record.keyword)

        if added:
            text = "已添加屏蔽词：\n" + "\n".join(f"- {item}" for item in added)
            if duplicates:
                text += "\n\n以下屏蔽词已存在，已跳过：\n" + "\n".join(
                    f"- {item}" for item in duplicates
                )
            await update.effective_message.reply_text(text)
            return

        await update.effective_message.reply_text("这些屏蔽词都已经存在了，无需重复添加。")

    async def blocks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        blocks = await self.db.list_block_keywords_by_tg_user(update.effective_user.id)
        if not blocks:
            await update.effective_message.reply_text(
                "你还没有屏蔽词。\n可以直接发：/block 求购,收"
            )
            return
        lines = ["我的屏蔽词："]
        for index, keyword in enumerate(blocks, start=1):
            lines.append(
                f"{index}. {keyword.keyword}（ID: {keyword.id}）命中 {keyword.hit_count} 次"
            )
        await update.effective_message.reply_text("\n".join(lines))

    async def delblock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        if not payload or not payload.isdigit():
            await update.effective_message.reply_text("请用：/delblock <屏蔽词ID>")
            return
        deleted = await self.db.delete_block_keyword(update.effective_user.id, int(payload))
        await update.effective_message.reply_text("已删除。" if deleted else "没有找到这个屏蔽词 ID。")

    async def set_keyword_state(
        self,
        update: Update,
        enabled: bool,
    ) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        if not payload or not payload.isdigit():
            command = "/on 1" if enabled else "/off 1"
            await update.effective_message.reply_text(f"请用：{command}")
            return

        changed = await self.db.set_keyword_enabled(
            update.effective_user.id,
            int(payload),
            enabled,
        )
        if not changed:
            await update.effective_message.reply_text("没有找到这个关键词 ID。")
            return
        await update.effective_message.reply_text("已开启。" if enabled else "已关闭。")

    async def on(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.set_keyword_state(update, True)

    async def off(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.set_keyword_state(update, False)

    async def delkw(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        if not payload or not payload.isdigit():
            await update.effective_message.reply_text("请用：/delkw <关键词ID>\n例如：/delkw 1")
            return
        deleted = await self.db.delete_keyword(update.effective_user.id, int(payload))
        await update.effective_message.reply_text("已删除。" if deleted else "没有找到这个关键词 ID。")

    async def scope(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        if not payload:
            await self._send_scope_selector(update.effective_message, update.effective_user.id)
            return

        if payload.lower() == "all":
            await self.db.set_user_categories(update.effective_user.id, list(CATEGORY_ORDER))
            await update.effective_message.reply_text("已设置为监控全部版块。")
            return

        slugs: list[str] = []
        invalid: list[str] = []
        for part in payload.split(","):
            slug = normalize_category_slug(part)
            if slug is None:
                invalid.append(part.strip())
            elif slug not in slugs:
                slugs.append(slug)

        if invalid:
            await update.effective_message.reply_text(
                "以下版块无法识别：\n" + "\n".join(f"- {item}" for item in invalid)
            )
            return

        await self.db.set_user_categories(update.effective_user.id, slugs)
        names = "、".join(category_label(item) for item in slugs)
        await update.effective_message.reply_text(f"已设置监控版块：{names}")

    async def _send_scope_selector(self, message, tg_user_id: int) -> None:
        settings = await self.db.get_user_settings_by_tg_user(tg_user_id)
        selected = {
            slug.strip()
            for slug in (settings.category_slugs if settings else "").split(",")
            if slug.strip()
        }
        await message.reply_text(
            "请选择要监控的版块（支持多选）。没有关键词的已选版块会全量推送；"
            "不选版块时仅保留旧版通用规则匹配，不启动全量监控：",
            reply_markup=build_category_keyboard(selected),
        )

    async def scope_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not update.effective_user:
            return
        if await self.ensure_user(update) is None:
            return

        data = query.data or ""
        settings = await self.db.get_user_settings_by_tg_user(update.effective_user.id)
        selected = {
            slug.strip()
            for slug in (settings.category_slugs if settings else "").split(",")
            if slug.strip()
        }

        if data == "scope:all":
            selected = set(CATEGORY_ORDER)
            await self.db.set_user_categories(update.effective_user.id, list(CATEGORY_ORDER))
            await query.answer("已切换为全部版块")
        elif data == "scope:done":
            if selected:
                names = "、".join(category_label(item) for item in CATEGORY_ORDER if item in selected)
                text = f"已保存版块设置：{names}"
            else:
                text = "未选择版块：仅保留旧版通用规则匹配，不启动全量监控。"
            await query.edit_message_text(text)
            await query.answer()
            return
        elif data.startswith("scope:toggle:"):
            slug = data.split(":")[-1]
            if slug not in CATEGORY_ORDER:
                await query.answer("无法识别这个版块。")
                return
            if slug in selected:
                selected.remove(slug)
            else:
                selected.add(slug)
            await self.db.set_user_categories(
                update.effective_user.id,
                [item for item in CATEGORY_ORDER if item in selected],
            )
            await query.answer(category_label(slug))
        else:
            await query.answer()
            return

        await query.edit_message_reply_markup(reply_markup=build_category_keyboard(selected))

    async def targets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        targets = await self.db.list_targets_by_tg_user(update.effective_user.id)
        if not targets:
            await update.effective_message.reply_text("你还没有推送目标，可以先在当前聊天发送 /addtarget")
            return
        lines = ["当前推送目标："]
        for target in targets:
            lines.append(
                f"{target.id}. {_chat_display_name(target.chat_title, target.chat_type, target.chat_id)} ({target.chat_id})"
            )
        await update.effective_message.reply_text("\n".join(lines))

    async def addtarget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self.ensure_user(update)
        if user is None or not update.effective_message or not update.effective_chat:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        requested_chat_id: int | None = None
        if payload:
            if not payload.lstrip("-").isdigit():
                await update.effective_message.reply_text(
                    "请用：/addtarget 或 /addtarget <chat_id>"
                )
                return
            requested_chat_id = int(payload)

        target_chat = await self._resolve_target_chat(update, context, requested_chat_id)
        if target_chat is None:
            return

        target_chat_id, chat_title, chat_type = target_chat

        existing_targets = await self.db.list_targets_by_tg_user(user.tg_user_id)
        if any(item.chat_id == target_chat_id for item in existing_targets):
            await update.effective_message.reply_text("这个目标已经在推送目标列表里了。")
            return

        current_count = await self.db.count_targets_by_tg_user(user.tg_user_id)
        if current_count >= self.settings.max_targets_per_user:
            await update.effective_message.reply_text(
                "你已经达到推送目标上限。\n"
                f"当前上限：{self.settings.max_targets_per_user} 个"
            )
            return

        created = await self.db.add_target_by_tg_user(
            user.tg_user_id,
            target_chat_id,
            chat_title=chat_title,
            chat_type=chat_type,
        )
        if not created:
            await update.effective_message.reply_text("这个目标已经在推送目标列表里了。")
            return
        await update.effective_message.reply_text(
            f"已添加推送目标：{_chat_display_name(chat_title, chat_type, target_chat_id)}"
        )

    async def target(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None or not update.effective_message:
            return
        await update.effective_message.reply_text(
            "请改用：/addtarget 或 /addtarget <chat_id>\n"
            "例如私聊绑定频道：/addtarget -1001234567890"
        )

    async def deltarget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        payload = update.effective_message.text.partition(" ")[2].strip()
        if not payload or not payload.isdigit():
            await update.effective_message.reply_text("请用：/deltarget <目标ID>")
            return
        deleted = await self.db.delete_target(update.effective_user.id, int(payload))
        await update.effective_message.reply_text("已删除。" if deleted else "没有找到这个目标 ID。")

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        limit = self.settings.history_limit
        payload = update.effective_message.text.partition(" ")[2].strip()
        if payload.isdigit():
            limit = max(1, min(int(payload), 30))
        records = await self.db.list_history_by_tg_user(update.effective_user.id, limit)
        if not records:
            await update.effective_message.reply_text("还没有命中历史。")
            return

        lines = ["最近推送历史："]
        for index, record in enumerate(records, start=1):
            keywords = record.matched_keywords or "未知"
            category = category_label(record.category_slug)
            title = record.title or "历史记录"
            link = record.link or "(历史迁移记录，原链接不可用)"
            lines.append(
                f"{index}. {title}\n"
                f"⚡️ {keywords}\n"
                f"🏷️ {category}\n"
                f"🕒 {record.delivered_at}\n"
                f"🔗 {link}"
            )
        await update.effective_message.reply_text("\n\n".join(lines))

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return
        settings = await self.db.get_user_settings_by_tg_user(update.effective_user.id)
        keywords = await self.db.list_keywords_by_tg_user(update.effective_user.id)
        block_keywords = await self.db.list_block_keywords_by_tg_user(update.effective_user.id)
        targets = await self.db.list_targets_by_tg_user(update.effective_user.id)

        active_keywords = sum(1 for item in keywords if item.enabled)
        combo_count = sum(
            1 for item in keywords if _keyword_kind(item.required_keywords) == "组合"
        )
        category_text = "未选择"
        if settings and settings.category_slugs:
            selected = [slug for slug in CATEGORY_ORDER if slug in settings.category_slugs.split(",")]
            category_text = "、".join(category_label(slug) for slug in selected) if selected else "未选择"

        lines = [
            "当前配置：",
            f"状态：{'启用' if settings and settings.enabled else '暂停'}",
            f"关键词规则：{len(keywords)} 条（启用 {active_keywords} 条，组合 {combo_count} 条）",
            f"屏蔽词：{len(block_keywords)} 个",
            f"版块：{category_text}",
            f"推送目标：{len(targets)} 个",
        ]
        if settings and settings.category_slugs:
            lines.append("各版块规则：")
            for slug in CATEGORY_ORDER:
                if slug not in settings.category_slugs.split(","):
                    continue
                applicable = [rule for rule in keywords if not rule.category_slug or rule.category_slug == slug]
                active = sum(1 for rule in applicable if rule.enabled)
                mode = f"关键词筛选（启用 {active} 条）" if active else "规则全部关闭，不推送"
                if not applicable:
                    mode = "全量推送"
                lines.append(f"- {category_label(slug)}：{mode}")
        else:
            lines.append("未选版块：仅旧版通用规则在全部版块匹配，不启动全量监控。")
        await update.effective_message.reply_text("\n".join(lines))

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_user or not update.effective_message:
            return
        changed = await self.db.set_user_enabled(update.effective_user.id, False)
        await update.effective_message.reply_text("已暂停提醒。" if changed else "暂停失败。")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_user or not update.effective_message:
            return
        changed = await self.db.set_user_enabled(update.effective_user.id, True)
        await update.effective_message.reply_text("已恢复提醒。" if changed else "恢复失败。")

    async def legacy_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.status(update, context)

    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.ensure_user(update) is None:
            return
        if not update.effective_message or not update.effective_user:
            return

        text = (update.effective_message.text or "").strip()
        if text in {"新建关键词", "我的关键词", "版块设置", "推送历史", "添加当前聊天", "帮助"}:
            context.user_data.pop("keyword_draft", None)
        if text == "新建关键词":
            await self._send_keyword_category_selector(update, context)
            return
        if text == "我的关键词":
            await self.keywords(update, context)
            return
        if text == "版块设置":
            await self._send_scope_selector(update.effective_message, update.effective_user.id)
            return
        if text == "推送历史":
            await self.history(update, context)
            return
        if text == "添加当前聊天":
            await self.addtarget(update, context)
            return
        if text == "帮助":
            await self.help(update, context)
            return

        draft = context.user_data.get("keyword_draft")
        if draft and draft.get("category_slug"):
            if draft["chat_id"] != update.effective_chat.id:
                await update.effective_message.reply_text("请回到发起设置的聊天输入关键词，或 /cancel 取消。")
                return
            context.user_data.pop("keyword_draft", None)
            await self._add_keywords_from_text(
                update, update.effective_user.id, text, draft["category_slug"], combo=draft["combo"]
            )


def build_application(settings: Settings, db: Database) -> Application:
    handlers = BotHandlers(settings=settings, db=db)
    application = Application.builder().token(settings.bot_token).build()
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(CommandHandler("chatid", handlers.chatid))
    application.add_handler(CommandHandler("keywords", handlers.keywords))
    application.add_handler(CommandHandler("addkw", handlers.addkw))
    application.add_handler(CommandHandler("combo", handlers.combo))
    application.add_handler(CommandHandler("kwscope", handlers.kwscope))
    application.add_handler(CommandHandler("cancel", handlers.cancel))
    application.add_handler(CommandHandler("on", handlers.on))
    application.add_handler(CommandHandler("off", handlers.off))
    application.add_handler(CommandHandler("delkw", handlers.delkw))
    application.add_handler(CommandHandler("block", handlers.block))
    application.add_handler(CommandHandler("blocks", handlers.blocks))
    application.add_handler(CommandHandler("delblock", handlers.delblock))
    application.add_handler(CommandHandler("scope", handlers.scope))
    application.add_handler(CommandHandler("targets", handlers.targets))
    application.add_handler(CommandHandler("addtarget", handlers.addtarget))
    application.add_handler(CommandHandler("target", handlers.target))
    application.add_handler(CommandHandler("deltarget", handlers.deltarget))
    application.add_handler(CommandHandler("history", handlers.history))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(CommandHandler("pause", handlers.pause))
    application.add_handler(CommandHandler("resume", handlers.resume))
    application.add_handler(CommandHandler("list", handlers.legacy_list))
    application.add_handler(CallbackQueryHandler(handlers.scope_callback, pattern=r"^scope:"))
    application.add_handler(CallbackQueryHandler(handlers.keyword_callback, pattern=r"^kw(category|bind):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text_message))
    return application
