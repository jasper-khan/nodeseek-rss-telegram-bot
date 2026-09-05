from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.categories import CATEGORY_ORDER
from app.utils import normalize_keywords, utc_now_iso


@dataclass(slots=True)
class UserRecord:
    id: int
    tg_user_id: int
    chat_id: int
    username: str | None
    first_name: str | None


@dataclass(slots=True)
class UserSettingsRecord:
    user_id: int
    category_slugs: str
    enabled: bool
    initialized: bool


@dataclass(slots=True)
class KeywordRecord:
    id: int
    user_id: int
    keyword: str
    normalized_keyword: str
    required_keywords: str
    enabled: bool
    hit_count: int
    last_hit_at: str | None
    category_slug: str = ""


@dataclass(slots=True)
class BlockKeywordRecord:
    id: int
    user_id: int
    keyword: str
    normalized_keyword: str
    hit_count: int
    last_hit_at: str | None


@dataclass(slots=True)
class TargetRecord:
    id: int
    user_id: int
    chat_id: int
    chat_title: str | None
    chat_type: str | None
    enabled: bool


@dataclass(slots=True)
class DeliveryRecord:
    id: int
    user_id: int
    item_key: str
    title: str
    link: str
    category_slug: str | None
    matched_keywords: str
    delivered_at: str


@dataclass(slots=True)
class PendingTargetDeliveryRecord:
    item_key: str
    target_id: int
    chat_id: int
    message_text: str
    link: str
    scope_category_slug: str


@dataclass(slots=True)
class PollingUserRecord:
    user: UserRecord
    settings: UserSettingsRecord
    keywords: list[KeywordRecord]
    block_keywords: list[BlockKeywordRecord]
    targets: list[TargetRecord]

    def rules_for_category(self, category_slug: str | None) -> list[KeywordRecord]:
        return [
            rule for rule in self.keywords
            if not rule.category_slug
            or (self.settings.category_slugs and rule.category_slug == category_slug)
        ]

    def has_active_monitoring(self) -> bool:
        categories = [slug for slug in self.settings.category_slugs.split(",") if slug]
        if not categories:
            return any(rule.enabled and not rule.category_slug for rule in self.keywords)
        for slug in categories:
            rules = self.rules_for_category(slug)
            if not rules or any(rule.enabled for rule in rules):
                return True
        return False


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute("PRAGMA busy_timeout = 5000;")
            yield db

    async def init(self) -> None:
        async with self._connect() as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    category_slugs TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    normalized_keyword TEXT NOT NULL,
                    required_keywords TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    last_hit_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    category_slug TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, category_slug, normalized_keyword),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS block_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    normalized_keyword TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    last_hit_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, normalized_keyword),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT,
                    chat_type TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, chat_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS delivery_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    item_key TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '',
                    category_slug TEXT,
                    matched_keywords TEXT NOT NULL DEFAULT '',
                    matched_keyword_ids TEXT NOT NULL DEFAULT '',
                    scope_category_slug TEXT NOT NULL DEFAULT '',
                    delivery_status TEXT NOT NULL DEFAULT 'sent',
                    delivered_at TEXT NOT NULL,
                    UNIQUE(user_id, item_key),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS target_delivery_history (
                    user_id INTEGER NOT NULL,
                    item_key TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(delivery_status IN ('pending', 'sent', 'disabled')),
                    message_text TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, item_key, target_id),
                    FOREIGN KEY(user_id, item_key)
                        REFERENCES delivery_history(user_id, item_key) ON DELETE CASCADE,
                    FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_keywords_user_id ON keywords(user_id);
                CREATE INDEX IF NOT EXISTS idx_block_keywords_user_id ON block_keywords(user_id);
                CREATE INDEX IF NOT EXISTS idx_targets_user_id ON targets(user_id);
                CREATE INDEX IF NOT EXISTS idx_delivery_history_user_id ON delivery_history(user_id);
                CREATE INDEX IF NOT EXISTS idx_target_delivery_status
                    ON target_delivery_history(user_id, item_key, delivery_status);
                """
            )
            await db.execute("BEGIN")
            await self._ensure_current_schema(db)
            await self._migrate_legacy_schema(db)
            await db.commit()

    async def _ensure_current_schema(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(keywords)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "required_keywords" not in columns:
            await db.execute(
                "ALTER TABLE keywords ADD COLUMN required_keywords TEXT NOT NULL DEFAULT ''"
            )
        if "category_slug" not in columns:
            # Rebuild to replace the old user-wide UNIQUE constraint, preserving rule IDs.
            cursor = await db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'keywords'")
            sequence = await cursor.fetchone()
            await db.execute(
                """
                CREATE TABLE keywords_scoped (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    normalized_keyword TEXT NOT NULL,
                    required_keywords TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    last_hit_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    category_slug TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, category_slug, normalized_keyword),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                INSERT INTO keywords_scoped (
                    id, user_id, keyword, normalized_keyword, required_keywords,
                    enabled, hit_count, last_hit_at, created_at, updated_at
                )
                SELECT id, user_id, keyword, normalized_keyword, required_keywords,
                       enabled, hit_count, last_hit_at, created_at, updated_at
                FROM keywords
                """
            )
            await db.execute("DROP TABLE keywords")
            await db.execute("ALTER TABLE keywords_scoped RENAME TO keywords")
            await db.execute("CREATE INDEX idx_keywords_user_id ON keywords(user_id)")
            if sequence:
                await db.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'keywords'",
                    (sequence[0],),
                )
        cursor = await db.execute("PRAGMA table_info(delivery_history)")
        delivery_columns = {row[1] for row in await cursor.fetchall()}
        if "delivery_status" not in delivery_columns:
            await db.execute(
                "ALTER TABLE delivery_history ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'sent'"
            )
        if "matched_keyword_ids" not in delivery_columns:
            await db.execute(
                "ALTER TABLE delivery_history ADD COLUMN matched_keyword_ids TEXT NOT NULL DEFAULT ''"
            )
        if "scope_category_slug" not in delivery_columns:
            await db.execute(
                "ALTER TABLE delivery_history ADD COLUMN scope_category_slug TEXT NOT NULL DEFAULT ''"
            )

    async def _migrate_legacy_schema(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute(
            "SELECT value FROM schema_meta WHERE key = 'legacy_subscriptions_migrated'"
        )
        row = await cursor.fetchone()
        if row is not None:
            return

        cursor = await db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'subscriptions'
            """
        )
        if await cursor.fetchone() is None:
            await db.execute(
                """
                INSERT OR REPLACE INTO schema_meta (key, value)
                VALUES ('legacy_subscriptions_migrated', '1')
                """
            )
            return

        cursor = await db.execute(
            """
            SELECT id, user_id, keywords, target_chat_id, enabled, initialized, created_at
            FROM subscriptions
            ORDER BY id ASC
            """
        )
        subscriptions = await cursor.fetchall()
        if not subscriptions:
            await db.execute(
                """
                INSERT OR REPLACE INTO schema_meta (key, value)
                VALUES ('legacy_subscriptions_migrated', '1')
                """
            )
            return

        user_agg: dict[int, dict[str, object]] = {}
        for _, user_id, keywords, target_chat_id, enabled, initialized, created_at in subscriptions:
            aggregate = user_agg.setdefault(
                user_id,
                {
                    "targets": set(),
                    "keywords": set(),
                    "enabled": False,
                    "initialized": False,
                    "created_at": created_at,
                },
            )
            aggregate["targets"].add(target_chat_id)
            aggregate["enabled"] = bool(aggregate["enabled"]) or bool(enabled)
            aggregate["initialized"] = bool(aggregate["initialized"]) or bool(initialized)
            for keyword in normalize_keywords(keywords):
                aggregate["keywords"].add(keyword)

        for user_id, aggregate in user_agg.items():
            created_at = str(aggregate["created_at"])
            await db.execute(
                """
                INSERT OR IGNORE INTO user_settings (
                    user_id, category_slugs, enabled, initialized, created_at, updated_at
                )
                VALUES (?, '', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    1 if aggregate["enabled"] else 0,
                    1 if aggregate["initialized"] else 0,
                    created_at,
                    created_at,
                ),
            )
            for chat_id in aggregate["targets"]:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO targets (
                        user_id, chat_id, chat_title, chat_type, enabled, created_at, updated_at
                    )
                    VALUES (?, ?, NULL, NULL, 1, ?, ?)
                    """,
                    (user_id, int(chat_id), created_at, created_at),
                )
            for keyword in aggregate["keywords"]:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO keywords (
                        user_id, keyword, normalized_keyword, enabled, hit_count, last_hit_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 1, 0, NULL, ?, ?)
                    """,
                    (user_id, keyword, keyword.lower(), created_at, created_at),
                )

        cursor = await db.execute(
            """
            SELECT s.user_id, d.item_key, d.delivered_at
            FROM deliveries d
            JOIN subscriptions s ON s.id = d.subscription_id
            """
        )
        for user_id, item_key, delivered_at in await cursor.fetchall():
            await db.execute(
                """
                INSERT OR IGNORE INTO delivery_history (
                    user_id, item_key, title, link, category_slug, matched_keywords, delivered_at
                )
                VALUES (?, ?, '', '', NULL, '', ?)
                """,
                (user_id, item_key, delivered_at),
            )

        await db.execute(
            """
            INSERT OR REPLACE INTO schema_meta (key, value)
            VALUES ('legacy_subscriptions_migrated', '1')
            """
        )

    async def upsert_user(
        self,
        tg_user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
    ) -> UserRecord:
        now = utc_now_iso()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO users (tg_user_id, chat_id, username, first_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (tg_user_id, chat_id, username, first_name, now, now),
            )
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id, tg_user_id, chat_id, username, first_name
                FROM users
                WHERE tg_user_id = ?
                """,
                (tg_user_id,),
            )
            row = await cursor.fetchone()
        return UserRecord(*row)

    async def ensure_user_profile(
        self,
        tg_user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        chat_title: str | None,
        chat_type: str | None,
    ) -> UserRecord:
        user = await self.upsert_user(tg_user_id, chat_id, username, first_name)
        await self.ensure_user_settings(user.id)
        if await self.count_targets_by_user_id(user.id) == 0:
            await self.add_target(
                user.id,
                chat_id,
                chat_title=chat_title,
                chat_type=chat_type,
            )
        return user

    async def ensure_user_settings(self, user_id: int) -> None:
        now = utc_now_iso()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO user_settings (
                    user_id, category_slugs, enabled, initialized, created_at, updated_at
                )
                VALUES (?, '', 1, 0, ?, ?)
                """,
                (user_id, now, now),
            )
            await db.commit()

    async def get_user_settings_by_tg_user(self, tg_user_id: int) -> UserSettingsRecord | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT us.user_id, us.category_slugs, us.enabled, us.initialized
                FROM user_settings us
                JOIN users u ON u.id = us.user_id
                WHERE u.tg_user_id = ?
                """,
                (tg_user_id,),
            )
            row = await cursor.fetchone()
        return UserSettingsRecord(*row) if row else None

    async def set_user_enabled(self, tg_user_id: int, enabled: bool) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE user_settings
                SET enabled = ?, updated_at = ?
                WHERE user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (1 if enabled else 0, utc_now_iso(), tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_user_initialized(self, user_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE user_settings
                SET initialized = 1, updated_at = ?
                WHERE user_id = ?
                """,
                (utc_now_iso(), user_id),
            )
            await db.commit()

    async def set_user_categories(self, tg_user_id: int, slugs: list[str]) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE user_settings
                SET category_slugs = ?, updated_at = ?
                WHERE user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (",".join(slugs), utc_now_iso(), tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def count_keywords_by_tg_user(self, tg_user_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM keywords k
                JOIN users u ON u.id = k.user_id
                WHERE u.tg_user_id = ?
                """,
                (tg_user_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def add_keyword(
        self,
        tg_user_id: int,
        keyword: str,
        required_terms: list[str] | None = None,
        *,
        category_slug: str = "",
    ) -> tuple[bool, KeywordRecord | None]:
        if category_slug and category_slug not in CATEGORY_ORDER:
            raise ValueError("Unknown keyword category")
        terms = [
            item.strip().lower()
            for item in (required_terms or [keyword])
            if item.strip()
        ]
        if not terms:
            return False, None
        seen_terms: set[str] = set()
        normalized_terms = []
        for term in terms:
            if term not in seen_terms:
                seen_terms.add(term)
                normalized_terms.append(term)
        normalized = (
            " + ".join(sorted(normalized_terms))
            if len(normalized_terms) > 1
            else normalized_terms[0]
        )
        required_keywords = ",".join(normalized_terms)
        now = utc_now_iso()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT id
                FROM users
                WHERE tg_user_id = ?
                """,
                (tg_user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False, None
            user_id = int(row[0])
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO keywords (
                    user_id, keyword, normalized_keyword, required_keywords,
                    enabled, hit_count, last_hit_at, created_at, updated_at, category_slug
                )
                VALUES (?, ?, ?, ?, 1, 0, NULL, ?, ?, ?)
                """,
                (user_id, keyword.strip(), normalized, required_keywords, now, now, category_slug),
            )
            inserted = cursor.rowcount > 0
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id, user_id, keyword, normalized_keyword, required_keywords,
                       enabled, hit_count, last_hit_at, category_slug
                FROM keywords
                WHERE user_id = ? AND normalized_keyword = ? AND category_slug = ?
                """,
                (user_id, normalized, category_slug),
            )
            keyword_row = await cursor.fetchone()
        return inserted, KeywordRecord(*keyword_row) if keyword_row else None

    async def list_keywords_by_tg_user(self, tg_user_id: int) -> list[KeywordRecord]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT k.id, k.user_id, k.keyword, k.normalized_keyword,
                       k.required_keywords, k.enabled, k.hit_count, k.last_hit_at, k.category_slug
                FROM keywords k
                JOIN users u ON u.id = k.user_id
                WHERE u.tg_user_id = ?
                ORDER BY k.id ASC
                """,
                (tg_user_id,),
            )
            rows = await cursor.fetchall()
        return [KeywordRecord(*row) for row in rows]

    async def set_keyword_category(self, tg_user_id: int, keyword_id: int, category_slug: str) -> bool:
        if category_slug not in CATEGORY_ORDER:
            raise ValueError("Unknown keyword category")
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE OR IGNORE keywords
                SET category_slug = ?, updated_at = ?
                WHERE id = ? AND user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (category_slug, utc_now_iso(), keyword_id, tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_keyword_enabled(self, tg_user_id: int, keyword_id: int, enabled: bool) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE keywords
                SET enabled = ?, updated_at = ?
                WHERE id = ?
                  AND user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (1 if enabled else 0, utc_now_iso(), keyword_id, tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_keyword(self, tg_user_id: int, keyword_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                DELETE FROM keywords
                WHERE id = ?
                  AND user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (keyword_id, tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def add_block_keyword(
        self,
        tg_user_id: int,
        keyword: str,
    ) -> tuple[bool, BlockKeywordRecord | None]:
        normalized = keyword.strip().lower()
        if not normalized:
            return False, None
        now = utc_now_iso()
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id FROM users WHERE tg_user_id = ?",
                (tg_user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False, None
            user_id = int(row[0])
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO block_keywords (
                    user_id, keyword, normalized_keyword, hit_count,
                    last_hit_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 0, NULL, ?, ?)
                """,
                (user_id, keyword.strip(), normalized, now, now),
            )
            inserted = cursor.rowcount > 0
            await db.commit()
            cursor = await db.execute(
                """
                SELECT id, user_id, keyword, normalized_keyword, hit_count, last_hit_at
                FROM block_keywords
                WHERE user_id = ? AND normalized_keyword = ?
                """,
                (user_id, normalized),
            )
            block_row = await cursor.fetchone()
        return inserted, BlockKeywordRecord(*block_row) if block_row else None

    async def list_block_keywords_by_tg_user(self, tg_user_id: int) -> list[BlockKeywordRecord]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT b.id, b.user_id, b.keyword, b.normalized_keyword,
                       b.hit_count, b.last_hit_at
                FROM block_keywords b
                JOIN users u ON u.id = b.user_id
                WHERE u.tg_user_id = ?
                ORDER BY b.id ASC
                """,
                (tg_user_id,),
            )
            rows = await cursor.fetchall()
        return [BlockKeywordRecord(*row) for row in rows]

    async def delete_block_keyword(self, tg_user_id: int, block_keyword_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                DELETE FROM block_keywords
                WHERE id = ?
                  AND user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (block_keyword_id, tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def bump_keyword_hits(self, user_id: int, keyword_ids: list[int]) -> None:
        if not keyword_ids:
            return
        async with self._connect() as db:
            for keyword_id in keyword_ids:
                await db.execute(
                    """
                    UPDATE keywords
                    SET hit_count = hit_count + 1,
                        last_hit_at = ?,
                        updated_at = ?
                    WHERE user_id = ? AND id = ?
                    """,
                    (utc_now_iso(), utc_now_iso(), user_id, keyword_id),
                )
            await db.commit()

    async def bump_block_keyword_hits(self, user_id: int, normalized_keywords: list[str]) -> None:
        if not normalized_keywords:
            return
        async with self._connect() as db:
            for keyword in normalized_keywords:
                await db.execute(
                    """
                    UPDATE block_keywords
                    SET hit_count = hit_count + 1,
                        last_hit_at = ?,
                        updated_at = ?
                    WHERE user_id = ? AND normalized_keyword = ?
                    """,
                    (utc_now_iso(), utc_now_iso(), user_id, keyword.lower()),
                )
            await db.commit()

    async def count_targets_by_user_id(self, user_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM targets WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_targets_by_tg_user(self, tg_user_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM targets t
                JOIN users u ON u.id = t.user_id
                WHERE u.tg_user_id = ?
                """,
                (tg_user_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def add_target(
        self,
        user_id: int,
        chat_id: int,
        *,
        chat_title: str | None,
        chat_type: str | None,
    ) -> bool:
        now = utc_now_iso()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO targets (
                    user_id, chat_id, chat_title, chat_type, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (user_id, chat_id, chat_title, chat_type, now, now),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def add_target_by_tg_user(
        self,
        tg_user_id: int,
        chat_id: int,
        *,
        chat_title: str | None,
        chat_type: str | None,
    ) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id FROM users WHERE tg_user_id = ?",
                (tg_user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            user_id = int(row[0])
        return await self.add_target(
            user_id,
            chat_id,
            chat_title=chat_title,
            chat_type=chat_type,
        )

    async def list_targets_by_tg_user(self, tg_user_id: int) -> list[TargetRecord]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT t.id, t.user_id, t.chat_id, t.chat_title, t.chat_type, t.enabled
                FROM targets t
                JOIN users u ON u.id = t.user_id
                WHERE u.tg_user_id = ?
                ORDER BY t.id ASC
                """,
                (tg_user_id,),
            )
            rows = await cursor.fetchall()
        return [TargetRecord(*row) for row in rows]

    async def delete_target(self, tg_user_id: int, target_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                DELETE FROM targets
                WHERE id = ?
                  AND user_id = (SELECT id FROM users WHERE tg_user_id = ?)
                """,
                (target_id, tg_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_target_enabled(self, user_id: int, target_id: int, enabled: bool) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE targets
                SET enabled = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (1 if enabled else 0, utc_now_iso(), target_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_history_by_tg_user(self, tg_user_id: int, limit: int) -> list[DeliveryRecord]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT d.id, d.user_id, d.item_key, d.title, d.link,
                       d.category_slug, d.matched_keywords, d.delivered_at
                FROM delivery_history d
                JOIN users u ON u.id = d.user_id
                WHERE u.tg_user_id = ?
                  AND d.delivery_status = 'sent'
                ORDER BY d.id DESC
                LIMIT ?
                """,
                (tg_user_id, limit),
            )
            rows = await cursor.fetchall()
        return [DeliveryRecord(*row) for row in rows]

    async def get_polling_users(self) -> list[PollingUserRecord]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT u.id, u.tg_user_id, u.chat_id, u.username, u.first_name,
                       us.category_slugs, us.enabled, us.initialized
                FROM users u
                JOIN user_settings us ON us.user_id = u.id
                WHERE us.enabled = 1
                ORDER BY u.id ASC
                """
            )
            user_rows = await cursor.fetchall()

            results: list[PollingUserRecord] = []
            for row in user_rows:
                user = UserRecord(*row[:5])
                settings = UserSettingsRecord(user.id, row[5], row[6], row[7])

                cursor = await db.execute(
                    """
                    SELECT id, user_id, keyword, normalized_keyword, required_keywords,
                           enabled, hit_count, last_hit_at, category_slug
                    FROM keywords
                    WHERE user_id = ?
                    ORDER BY id ASC
                    """,
                    (user.id,),
                )
                keyword_rows = await cursor.fetchall()
                user_record = PollingUserRecord(
                    user=user,
                    settings=settings,
                    keywords=[KeywordRecord(*item) for item in keyword_rows],
                    block_keywords=[],
                    targets=[],
                )
                if not user_record.has_active_monitoring():
                    continue

                cursor = await db.execute(
                    """
                    SELECT id, user_id, keyword, normalized_keyword,
                           hit_count, last_hit_at
                    FROM block_keywords
                    WHERE user_id = ?
                    ORDER BY id ASC
                    """,
                    (user.id,),
                )
                block_keyword_rows = await cursor.fetchall()

                cursor = await db.execute(
                    """
                    SELECT id, user_id, chat_id, chat_title, chat_type, enabled
                    FROM targets
                    WHERE user_id = ? AND enabled = 1
                    ORDER BY id ASC
                    """,
                    (user.id,),
                )
                target_rows = await cursor.fetchall()
                if not target_rows:
                    continue

                user_record.block_keywords = [BlockKeywordRecord(*item) for item in block_keyword_rows]
                user_record.targets = [TargetRecord(*item) for item in target_rows]
                results.append(user_record)

        return results

    async def is_delivered(self, user_id: int, item_key: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM delivery_history
                WHERE user_id = ? AND item_key = ?
                """,
                (user_id, item_key),
            )
            row = await cursor.fetchone()
        return row is not None

    async def get_target_delivery_states(
        self,
        user_id: int,
        item_key: str,
    ) -> dict[int, str]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT target_id, delivery_status
                FROM target_delivery_history
                WHERE user_id = ? AND item_key = ?
                ORDER BY target_id ASC
                """,
                (user_id, item_key),
            )
            rows = await cursor.fetchall()
        return {int(target_id): str(status) for target_id, status in rows}

    async def list_pending_target_deliveries(
        self,
        user_id: int,
    ) -> list[PendingTargetDeliveryRecord]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT td.item_key, td.target_id, t.chat_id, td.message_text,
                       td.link, d.scope_category_slug
                FROM target_delivery_history td
                JOIN targets t ON t.id = td.target_id AND t.user_id = td.user_id
                JOIN delivery_history d
                  ON d.user_id = td.user_id AND d.item_key = td.item_key
                WHERE td.user_id = ?
                  AND td.delivery_status = 'pending'
                  AND t.enabled = 1
                ORDER BY td.created_at ASC, td.target_id ASC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [PendingTargetDeliveryRecord(*row) for row in rows]

    async def prepare_target_delivery(
        self,
        user_id: int,
        item_key: str,
        target_ids: list[int],
        *,
        title: str,
        link: str,
        category_slug: str | None,
        matched_keywords: list[str],
        matched_keyword_ids: list[int],
        scope_category_slug: str,
        message_text: str,
    ) -> None:
        now = utc_now_iso()
        async with self._connect() as db:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT OR IGNORE INTO delivery_history (
                    user_id, item_key, title, link, category_slug,
                    matched_keywords, matched_keyword_ids, scope_category_slug,
                    delivery_status, delivered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    user_id,
                    item_key,
                    title,
                    link,
                    category_slug,
                    ",".join(matched_keywords),
                    ",".join(str(keyword_id) for keyword_id in matched_keyword_ids),
                    scope_category_slug,
                    now,
                ),
            )
            cursor = await db.execute(
                """
                SELECT delivery_status
                FROM delivery_history
                WHERE user_id = ? AND item_key = ?
                """,
                (user_id, item_key),
            )
            row = await cursor.fetchone()
            if row and row[0] == "pending":
                for target_id in target_ids:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO target_delivery_history (
                            user_id, item_key, target_id, delivery_status,
                            message_text, link, delivered_at, created_at, updated_at
                        )
                        SELECT ?, ?, id, 'pending', ?, ?, NULL, ?, ?
                        FROM targets
                        WHERE id = ? AND user_id = ? AND enabled = 1
                        """,
                        (
                            user_id,
                            item_key,
                            message_text,
                            link,
                            now,
                            now,
                            target_id,
                            user_id,
                        ),
                    )
            await db.commit()

    async def set_target_delivery_status(
        self,
        user_id: int,
        item_key: str,
        target_id: int,
        status: str,
    ) -> bool:
        if status not in {"sent", "disabled"}:
            raise ValueError("Invalid target delivery status")
        now = utc_now_iso()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE target_delivery_history
                SET delivery_status = ?,
                    delivered_at = CASE WHEN ? = 'sent' THEN ? ELSE delivered_at END,
                    message_text = '',
                    link = '',
                    updated_at = ?
                WHERE user_id = ? AND item_key = ? AND target_id = ?
                  AND delivery_status = 'pending'
                """,
                (status, status, now, now, user_id, item_key, target_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def finalize_target_delivery(self, user_id: int, item_key: str) -> list[int]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT matched_keyword_ids, delivery_status
                FROM delivery_history
                WHERE user_id = ? AND item_key = ?
                """,
                (user_id, item_key),
            )
            row = await cursor.fetchone()
            if row is None or row[1] != "pending":
                return []
            cursor = await db.execute(
                """
                SELECT
                    SUM(CASE WHEN delivery_status = 'sent' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN delivery_status = 'pending' THEN 1 ELSE 0 END)
                FROM target_delivery_history
                WHERE user_id = ? AND item_key = ?
                """,
                (user_id, item_key),
            )
            sent_count, pending_count = await cursor.fetchone()
            if not sent_count and pending_count:
                return []
            final_status = "sent" if sent_count else "failed"
            await db.execute(
                """
                UPDATE delivery_history
                SET delivery_status = ?, delivered_at = ?
                WHERE user_id = ? AND item_key = ? AND delivery_status = 'pending'
                """,
                (final_status, utc_now_iso(), user_id, item_key),
            )
            await db.commit()
        if final_status != "sent":
            return []
        return [int(value) for value in row[0].split(",") if value]

    async def mark_delivered(
        self,
        user_id: int,
        item_key: str,
        *,
        title: str,
        link: str,
        category_slug: str | None,
        matched_keywords: list[str],
        matched_keyword_ids: list[int] | None = None,
        delivery_status: str = "sent",
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO delivery_history (
                    user_id, item_key, title, link, category_slug,
                    matched_keywords, matched_keyword_ids, scope_category_slug,
                    delivery_status, delivered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    user_id,
                    item_key,
                    title,
                    link,
                    category_slug,
                    ",".join(matched_keywords),
                    ",".join(str(keyword_id) for keyword_id in (matched_keyword_ids or [])),
                    delivery_status,
                    utc_now_iso(),
                ),
            )
            await db.commit()
