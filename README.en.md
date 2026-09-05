# NodeSeek Keyword Monitor Bot

Monitor NodeSeek keywords and push matched new posts to Telegram. Supports multi-user shared deployment, keyword combinations, block keywords, multi-category filtering, delivery history, and deduplicated notifications.

## Features

- Per-category keyword rules with independent IDs, switches and hit counts
- Keyword combinations, for example `dmit + corona` only matches when all terms appear
- Block keywords, so matched blocked terms suppress notifications
- Multi-select category filtering
- Each selected category without applicable rules delivers all its new posts
- Notifications contain a reminder header, a linked post title and post content, with no separate time or URL line
- Multiple delivery targets, up to 10 in total across user chats and communities
- Per-target deduplication and durable retry: only failed targets retry, including after restart or after a post leaves the current RSS window
- Delivery history
- Deduplicated notifications with persisted state
- Multi-user shared deployment
- Polls RSS every 10 seconds by default, configurable in `.env`

Common commands:

- `/keywords`: show your keywords
- `/scope`: choose monitored categories using buttons
- `/scope tech,trade`: select categories (Chinese category labels also work)
- `/scope all`: explicitly select every known category, including full monitoring where no rules apply
- `/keywords <category> <kw1,kw2>`: add keywords to one selected category
- `/keywords <kw1,kw2>`: choose a category using buttons before saving
- `/combo <category> <kw1,kw2>`: add a combination requiring all terms within that category
- `/kwscope <keyword_id> <category>`: rebind an existing rule to a selected category
- `/cancel`: cancel keyword setup
- `/on <keyword_id>`: enable a keyword
- `/off <keyword_id>`: disable a keyword
- `/delkw <keyword_id>`: delete a keyword
- `/block <kw1,kw2>`: add block keywords
- `/blocks`: show block keywords
- `/delblock <block_keyword_id>`: delete a block keyword
- `/addtarget`: add the current chat as a target
- `/addtarget <chat_id>`: bind a group or channel from private chat
- `/targets`: show delivery targets
- `/deltarget <target_id>`: remove a target
- `/history`: show recent matched posts
- `/status`: show current settings
- `/pause`: pause notifications
- `/resume`: resume notifications

### Category-specific setup

In the Bot, choose categories through “版块设置”, then click “新建关键词”, select one category and enter keywords. “我的关键词” lists scopes, IDs, switches and hit counts, with a button to rebind each rule. `/status` shows the monitoring mode for each selected category.

For example, after rebinding any legacy global rules:

```text
/scope tech,trade,daily
/keywords tech oracle,free
/combo trade dmit,corona
```

Technology matches `oracle` OR `free`, trading matches `dmit` AND `corona`, and daily delivers every new post. Block keywords apply across categories. Matching is case-insensitive and uses the RSS title, full available content and category tags.

Disabling every applicable rule pauses a category; deleting its last applicable rule restores full monitoring. Deselecting a category stops its scoped rules without deleting them. `/pause` pauses all notifications for the user.

### Existing databases

Startup migrates the keyword table automatically and preserves rule IDs, enabled states, hit counts and timestamps. Existing targets and post-level history remain intact, while a new per-target delivery table tracks pending and completed sends. If one target succeeds and another fails temporarily, only the failed target retries with the persisted message. New targets do not receive older posts that were already prepared, and a target that blocks the Bot is disabled. Stop the Bot and back up the database before upgrading; restore the matching backup if rolling back the code.

Existing rules are labeled “旧版通用” (legacy global) and retain their original scope. They combine with category-specific rules using OR. Rebind them through the keyword list or `/kwscope <ID> <category>` to make them category-specific. Add separate rules when the same keyword should apply to multiple categories.

Without selected categories, scoped rules stop and full monitoring stays off; legacy global rules still match across categories. `/scope all` explicitly selects all known categories and differs from an unconfigured new account.

### Notification content

Notifications show “NodeSeek 新帖提醒”, “标题：” followed by a linked title, and “摘要：” followed by post content. RSS `content` is preferred, with `summary/description` as fallback. The Bot does not fetch authenticated post pages, so content absent from RSS cannot be recovered.

Keyword matching uses content before truncation. Outgoing messages are capped to Telegram's text limit, with an ellipsis for long content and a 512 UTF-16-unit title cap. All HTML is escaped. Set `DISABLE_WEB_PAGE_PREVIEW=true` to suppress Telegram's optional link preview.

### Local verification

```bash
python -B -m unittest discover -s tests -v
```

Tests use temporary SQLite databases and mocked Telegram/RSS responses without live credentials or messages.

Notes:

- Private chats work by default, and you do not need to run `/addtarget` manually
- In groups, you can run `/addtarget` directly
- For channels, use `/addtarget <chat_id>` in private chat
- The operator must be an admin of the target group or channel
- If `ALLOWED_USER_IDS` is enabled, only allowlisted users can use the bot

## Try My Bot First

[https://t.me/NodeSeekKey_bot](https://t.me/NodeSeekKey_bot)

## Personal Deployment Guide

### 1. Prepare Your VPS

Install Docker and Git on your VPS:

```bash
apt update
apt install -y docker.io docker-compose-plugin git
```

### 2. Create a Telegram Bot

Open Telegram, talk to `@BotFather`, create a new bot, and keep the `BOT_TOKEN` it gives you.

### 3. Clone the Project

Run this on your VPS:

```bash
git clone https://github.com/<your-username>/nodeseek-rss-telegram-bot.git
cd nodeseek-rss-telegram-bot
```

### 4. Configure Environment Variables

Copy the config template:

```bash
cp .env.example .env
nano .env
```

Replace `BOT_TOKEN` in `.env` with your own token.

If you want allowlist mode, you can also add:

```text
ALLOWED_USER_IDS=<user_id_1>,<user_id_2>
```

### 5. Start the Bot

```bash
docker compose up -d --build
```

### 6. Check Logs

```bash
docker compose logs -f
```

If you see `Application started`, the bot is running.

Press `Ctrl + C` to exit log viewing. This will not stop the bot.

### 7. Update the Project

For normal updates, run:

```bash
git pull
docker compose down
docker compose up -d --build
```

If your VPS reports a Git branch conflict, force it to match GitHub:

```bash
git fetch origin
git reset --hard origin/main
docker compose down
docker compose up -d --build
```

## Privacy

- This project stores Telegram user IDs, chat IDs, keywords, category settings, delivery targets, and delivery history only for notifications. Messages pending for temporarily failed targets are stored in SQLite; their message body is cleared after delivery or permanent disablement.
- Data is stored in the deployer's own SQLite database and is not uploaded to GitHub.
- Do not expose `.env` or the `data/` directory. If your `BOT_TOKEN` leaks, reset it in BotFather immediately.
