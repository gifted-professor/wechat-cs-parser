from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_cs.store import SCHEMA_VERSION, initialize_schema, open_store


class SchemaV3Tests(unittest.TestCase):
    def test_schema_v3_creates_action_queue_and_feature_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = open_store(str(Path(temp_dir) / "m0.sqlite3"))
            initialize_schema(connection)
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "customer_value_snapshots",
                    "card_feature_snapshots",
                    "action_annotations",
                    "card_annotations",
                    "strategy_catalog",
                    "action_queue_runs",
                    "action_queue_items",
                    "action_queue_feedback",
                    "contact_suppressions",
                }.issubset(names)
            )
            self.assertEqual(SCHEMA_VERSION, 3)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            order_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)")
            }
            self.assertTrue(
                {"sku_name", "factory", "category", "color", "size"}.issubset(order_columns)
            )
            queue_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(action_queue_items)")
            }
            self.assertTrue({"signals_json", "missing_facts_json"}.issubset(queue_columns))
            migrations = {
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            self.assertIn(3, migrations)
            connection.close()

    def test_v3_initialization_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = open_store(str(Path(temp_dir) / "m0.sqlite3"))
            initialize_schema(connection)
            initialize_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=3"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            connection.close()


if __name__ == "__main__":
    unittest.main()
