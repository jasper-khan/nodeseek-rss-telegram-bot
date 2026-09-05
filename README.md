# NodeSeek 关键词监控 Bot

监控 NodeSeek 关键词，命中新帖后自动推送到 Telegram。支持多用户共享、自部署、组合关键词、屏蔽词、版块多选、推送历史和去重推送。

## 功能介绍

- 按“版块 + 关键词”独立管理、开关和统计，同一个词可以用于不同版块
- 支持组合关键词，例如 `dmit + corona` 同时命中才提醒
- 支持屏蔽词，命中后不推送
- 支持版块多选
- 每个已选版块独立判断：没有适用关键词规则时，自动监控该版块的全部新帖
- 推送显示“NodeSeek 新帖提醒”、可点击的帖子标题、帖子内容摘要，不再单列时间和链接
- 支持多目标推送配置，用户+社群最多10个；
- 多目标按目标独立去重；临时失败只重试失败目标，重启或帖子退出 RSS 列表后仍可继续
- 支持推送历史
- 去重推送，重启后状态不丢失
- 多用户共享
- 默认 10 秒轮询一次 RSS，可在 `.env` 调整

常用命令：

- `/keywords`：查看我的关键词
- `/scope`：通过按钮选择监控版块
- `/scope tech,trade`：选择技术、交易版块（也支持中文名称）
- `/scope all`：明确选中所有已知版块；无关键词的版块全量推送
- `/keywords <版块> <词1,词2>`：为一个已选版块添加关键词，例如 `/keywords tech oracle,免费鸡`
- `/keywords <词1,词2>`：先弹出板块选择按钮，再保存关键词
- `/combo <版块> <词1,词2>`：为一个已选版块添加组合关键词，所有词都命中才提醒
- `/kwscope <关键词ID> <版块>`：将已有规则改绑到一个已选版块
- `/cancel`：取消当前关键词设置
- `/on <关键词ID>`：开启关键词
- `/off <关键词ID>`：关闭关键词
- `/delkw <关键词ID>`：删除关键词
- `/block <词1,词2>`：添加屏蔽词，命中后不推送
- `/blocks`：查看屏蔽词
- `/delblock <屏蔽词ID>`：删除屏蔽词
- `/addtarget`：把当前聊天加入推送目标
- `/addtarget <chat_id>`：在私聊里绑定群组或频道
- `/targets`：查看推送目标
- `/deltarget <目标ID>`：删除推送目标
- `/history`：查看最近命中的帖子
- `/status`：查看当前配置
- `/pause`：暂停提醒
- `/resume`：恢复提醒

### 在 Bot 中配置“版块 + 监控词”

1. 点击“版块设置”，勾选要监控的版块。
2. 点击“新建关键词”，从已选版块中选择一个，再输入关键词。多个普通关键词用英文逗号分隔，命中任意一个即可。
3. 点击“我的关键词”，查看每条规则的所属版块、ID、开关和命中次数；点击“修改 #ID 的板块”可以改绑。
4. 使用 `/status` 查看每个版块当前是全量推送、关键词筛选，还是规则全部关闭。

例如（没有剩余的旧版通用规则时）：

```text
/scope tech,trade,daily
/keywords tech oracle,免费鸡
/combo trade dmit,corona
```

| 版块 | 推送条件 |
| --- | --- |
| 技术 | 帖子命中 oracle 或 免费鸡 |
| 交易 | 同时命中 dmit 和 corona |
| 日常 | 未添加规则，推送全部新帖 |

屏蔽词对所有版块生效。关键词和屏蔽词匹配 RSS 提供的标题、完整内容与分类标签，不区分大小写。

关闭某个版块的全部规则不会变成全量推送，该版块会停止推送；删除该版块最后一条适用规则则恢复全量推送。取消勾选版块会停止其独立规则，规则仍保留，重新勾选后继续生效。`/pause` 暂停整个用户的推送。

### 旧配置升级

启动时自动迁移 SQLite 关键词表，保留规则 ID、开关、命中统计和时间记录，推送目标及原有去重历史不变；同时新增目标级发送状态。一个目标成功、另一个目标临时失败时，成功目标不会重复收到，失败目标会使用持久化的原消息继续重试。新增目标不会补收建立该状态前的旧帖；Bot 被目标封禁时，该目标会停用并停止重试。更新前请停止 Bot 并备份数据库，回退时应配套恢复更新前的数据库副本。

旧关键词显示为“旧版通用”，继续适用于原来的监控范围；它们与该版块的独立规则按“任一规则命中”合并判断。要彻底按版块独立配置，在“我的关键词”里把旧规则逐条改绑，或发送 `/kwscope <ID> <版块>`。同一规则需要多个版块时，在各版块分别添加。

没有选版块时不启动全量监控，独立版块规则停止；旧版通用规则仍按旧逻辑在全部版块匹配。`/scope all` 与“监控全部”按钮会明确选中全部已知版块，区别于未配置的新用户。

### 推送格式

```text
NodeSeek 新帖提醒
标题：帖子标题（点击可直达帖子）
摘要：帖子内容
```

标题使用 Telegram HTML 内嵌链接，不单列时间、链接、关键词和板块。摘要优先使用 RSS 的正文 `content`，没有时使用 `summary/description`；不额外抓取需要登录的帖子页面。RSS 若只提供节选，Bot 无法补出原帖未提供的内容。

关键词匹配在内容截断前进行。发送时按 Telegram 消息上限保留正文，超长部分以省略号结尾；标题超过 512 个 UTF-16 单元时也会截断。标题、内容和链接均转义 HTML。链接预览仍由 `DISABLE_WEB_PAGE_PREVIEW` 控制，设为 `true` 可只显示上述三行。

### 本地测试

```bash
python -B -m unittest discover -s tests -v
```

测试使用临时 SQLite 数据库及模拟 Telegram/RSS 响应，不需要真实 Bot Token，也不发送消息。

说明：

- 默认私聊可直接使用，不需要手动 `/addtarget`
- 群组里可直接发送 `/addtarget`
- 频道可在私聊里发送 `/addtarget <chat_id>` 进行绑定
- 群组或频道都要求操作者是管理员
- 如启用 `ALLOWED_USER_IDS`，只有白名单用户可以使用 Bot

## 可以先订阅我的机器人试试

https://t.me/NodeSeekKey_bot


## 个人部署教程

### 1. 准备 VPS 环境

在 VPS 上安装 Docker 和 Git：

```bash
apt update
apt install -y docker.io docker-compose-plugin git
```

### 2. 创建 Telegram Bot

在 Telegram 里找到 `@BotFather`，创建一个新的 Bot，并保存它给你的 `BOT_TOKEN`。

### 3. 下载项目

在 VPS 上执行：

```bash
git clone https://github.com/<你的用户名>/nodeseek-rss-telegram-bot.git
cd nodeseek-rss-telegram-bot
```

### 4. 配置环境变量

复制配置模板：

```bash
cp .env.example .env
nano .env
```

把 `.env` 里的 `BOT_TOKEN` 改成你自己的 Token。

如需启用白名单模式，可以额外配置：

```text
ALLOWED_USER_IDS=<用户ID1>,<用户ID2>
```

### 5. 启动 Bot

```bash
docker compose up -d --build
```

### 6. 查看运行日志

```bash
docker compose logs -f
```

看到 `Application started` 就说明启动成功了。

按 `Ctrl + C` 可以退出日志查看，不会停止 Bot。

### 7. 更新项目

如果只是普通更新，可以执行：

```bash
git pull
docker compose down
docker compose up -d --build
```

如果 VPS 提示 Git 分支冲突，可以改用强制对齐 GitHub：

```bash
git fetch origin
git reset --hard origin/main
docker compose down
docker compose up -d --build
```

## 隐私说明

- 本项目会保存 Telegram 用户 ID、chat_id、关键词、版块设置、推送目标和历史记录，仅用于提醒服务。临时失败目标的待推送消息会保存在 SQLite 中，发送成功或永久停用后清空消息正文。
- 数据默认保存在部署者自己服务器上的 SQLite 数据库，不会上传到 GitHub。
- 请勿公开 `.env` 和 `data/` 目录；如果 `BOT_TOKEN` 泄露，请立即在 BotFather 重置。
