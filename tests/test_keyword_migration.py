from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.db import Database


class KeywordMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "legacy.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tg_user_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL, username TEXT, first_name TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO users VALUES (1, 1, 1001, NULL, 'Test', 'before', 'before');
                CREATE TABLE keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL, normalized_keyword TEXT NOT NULL,
                    required_keywords TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                    hit_count INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(user_id, normalized_keyword),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                INSERT INTO keywords VALUES
                    (21, 1, 'Oracle', 'oracle', 'oracle', 0, 7, 'hit', 'created', 'updated'),
                    (22, 1, 'dmit + corona', 'corona + dmit', 'dmit,corona', 1, 3, 'hit', 'created', 'updated'),
                    (900, 1, 'deleted', 'deleted', 'deleted', 1, 0, NULL, 'created', 'updated');
                DELETE FROM keywords WHERE id = 900;
            """)
        self.db = Database(self.path)

    async def test_upgrade_is_idempotent_and_preserves_rules_ids_and_sequence(self) -> None:
        await self.db.init()
        await self.db.init()
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual([21, 22], [rule.id for rule in rules])
        self.assertEqual(["", ""], [rule.category_slug for rule in rules])
        self.assertFalse(rules[0].enabled)
        self.assertEqual(7, rules[0].hit_count)
        self.assertEqual("hit", rules[0].last_hit_at)
        self.assertEqual("dmit,corona", rules[1].required_keywords)
        added, tech = await self.db.add_keyword(1, "oracle", category_slug="tech")
        self.assertTrue(added)
        self.assertGreater(tech.id, 900)
        added, _ = await self.db.add_keyword(1, "oracle", category_slug="trade")
        self.assertTrue(added)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(("created", "updated"), connection.execute(
                "SELECT created_at, updated_at FROM keywords WHERE id = 21"
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'idx_keywords_user_id'"
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'target_delivery_history'"
            ).fetchone())
            delivery_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(delivery_history)")
            }
            self.assertIn("matched_keyword_ids", delivery_columns)
            self.assertIn("scope_category_slug", delivery_columns)

    async def test_failed_upgrade_rolls_back_the_original_rules(self) -> None:
        with patch.object(Database, "_migrate_legacy_schema", AsyncMock(side_effect=RuntimeError("test"))):
            with self.assertRaises(RuntimeError):
                await self.db.init()
        with closing(sqlite3.connect(self.path)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(keywords)")]
            self.assertNotIn("category_slug", columns)
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM keywords").fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'keywords_scoped'"
            ).fetchone())
        await self.db.init()
        self.assertEqual(2, len(await self.db.list_keywords_by_tg_user(1)))

    async def test_older_database_without_combo_column_can_upgrade(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE keywords DROP COLUMN required_keywords")
        await self.db.init()
        rules = await self.db.list_keywords_by_tg_user(1)
        self.assertEqual("", rules[0].required_keywords)
        self.assertEqual("oracle", rules[0].normalized_keyword)
