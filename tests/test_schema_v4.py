from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_cs.store import SCHEMA_VERSION, initialize_run, initialize_schema, open_store


SECRET = "schema-v4-fixture-secret-with-at-least-32-characters"


class SchemaV4Tests(unittest.TestCase):
    def test_schema_v4_adds_order_aux_and_sales_profile_tables(self) -> None:
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
                    "customer_aux_facts",
                    "sales_profile_runs",
                    "sales_profile_subjects",
                    "sales_profile_events",
                    "sales_profiles",
                    "sales_profile_reviews",
                }.issubset(names)
            )
            order_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)")
            }
            self.assertTrue(
                {"ordered_at", "paid_at", "order_note"}.issubset(order_columns)
            )
            subject_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(sales_profile_subjects)"
                )
            }
            self.assertTrue(
                {"feature_payload_json", "feature_freshness_json"}.issubset(
                    subject_columns
                )
            )
            self.assertEqual(SCHEMA_VERSION, 4)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=4"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.close()

    def test_v4_initialization_is_idempotent_and_preserves_existing_human_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = open_store(str(Path(temp_dir) / "m0.sqlite3"))
            initialize_schema(connection)
            initialize_run(connection, run_id="run-v3", secret=SECRET)
            quality = {
                "acceptance_gates": {
                    "m0_a": True,
                    "m0_b": True,
                    "m0_c": False,
                }
            }
            connection.execute(
                "UPDATE pipeline_runs SET quality_json=? WHERE run_id='run-v3'",
                (json.dumps(quality, sort_keys=True),),
            )
            connection.execute(
                "INSERT INTO customers(customer_key,display_name,last_active_at,"
                "opportunity_score,opportunity_level,summary,reasons_json,evidence_json,"
                "memory_json,source_file) VALUES('customer-v4','客户','2026-07-13T12:00:00+08:00',"
                "50,'medium','摘要','[]','[]','{}','fixture')"
            )
            connection.execute(
                "INSERT INTO drafts(draft_id,customer_key,request_text,draft_text,intent,created_at) "
                "VALUES('draft-v4','customer-v4','请求','草稿','general','2026-07-13T12:00:00+08:00')"
            )
            connection.execute(
                "INSERT INTO feedback(feedback_id,draft_id,customer_key,outcome,final_text,created_at) "
                "VALUES('feedback-v4','draft-v4','customer-v4','edited','人工最终文本',"
                "'2026-07-13T12:01:00+08:00')"
            )
            connection.commit()

            initialize_schema(connection)
            initialize_schema(connection)

            feedback = connection.execute(
                "SELECT outcome,final_text FROM feedback WHERE feedback_id='feedback-v4'"
            ).fetchone()
            self.assertEqual((feedback["outcome"], feedback["final_text"]), ("edited", "人工最终文本"))
            stored_quality = json.loads(
                connection.execute(
                    "SELECT quality_json FROM pipeline_runs WHERE run_id='run-v3'"
                ).fetchone()[0]
            )
            self.assertEqual(stored_quality, quality)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=4"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            connection.close()

    def test_sales_profile_review_enforces_one_review_per_operator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = open_store(str(Path(temp_dir) / "m0.sqlite3"))
            initialize_schema(connection)
            review_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='sales_profile_reviews'"
            ).fetchone()[0]
            self.assertIn("UNIQUE(sales_profile_id, reviewer)", review_sql)
            for score in (
                "fact_accuracy",
                "insight_usefulness",
                "sales_realism",
                "timing_quality",
                "evidence_quality",
            ):
                self.assertIn(f"{score} BETWEEN 1 AND 5", review_sql)
            connection.close()


if __name__ == "__main__":
    unittest.main()
