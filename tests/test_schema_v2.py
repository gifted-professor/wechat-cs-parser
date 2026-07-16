from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_cs.source_snapshot import (
    assert_project_output,
    hmac_key_fingerprint,
    read_stable_bytes,
)
from wechat_cs.store import (
    SCHEMA_VERSION,
    initialize_m0_run,
    initialize_run,
    initialize_schema,
    open_store,
    publish_m0_database,
    validate_m0_database,
)


FIXTURE_V1 = Path(__file__).parent / "fixtures" / "schema" / "v1.sql"
SECRET_A = "first-test-secret-with-at-least-32-characters"
SECRET_B = "different-test-secret-with-at-least-32-characters"


class SchemaV2Tests(unittest.TestCase):
    def test_schema_v2_creates_m0_truth_tables_and_constraints(self) -> None:
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
                    "schema_migrations",
                    "pipeline_runs",
                    "source_snapshots",
                    "profile_observations",
                    "account_registry",
                "conversation_refs",
                "conversation_order_eligibility",
                "conversation_links",
                    "order_snapshots",
                    "orders",
                    "decision_cards",
                    "card_outcomes",
                }.issubset(names)
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(SCHEMA_VERSION, 3)

            role_columns = {
                row[1]: row for row in connection.execute("PRAGMA table_info(role_calibration)")
            }
            self.assertEqual(role_columns["source_status"][3], 0)
            self.assertIn("source_role_evidence_json", role_columns)

            outcome_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='card_outcomes'"
            ).fetchone()[0]
            self.assertIn("paid_1d IN (0,1)", outcome_sql)
            connection.close()

    def test_real_v1_migration_is_idempotent_and_preserves_human_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "v1.sqlite3"
            connection = sqlite3.connect(str(db_path))
            connection.executescript(FIXTURE_V1.read_text(encoding="utf-8"))
            connection.close()

            connection = open_store(str(db_path))
            initialize_schema(connection)
            initialize_schema(connection)
            calibration = connection.execute(
                "SELECT source_status,source_role_evidence_json,reviewer_role "
                "FROM role_calibration WHERE calibration_id='calibration_fixture'"
            ).fetchone()
            self.assertEqual(calibration["source_status"], 3)
            self.assertEqual(calibration["reviewer_role"], "customer")
            self.assertIn("export", calibration["source_role_evidence_json"])
            pair = connection.execute(
                "SELECT review_status,review_reasons_json FROM style_pairs "
                "WHERE pair_id='pair_fixture'"
            ).fetchone()
            self.assertEqual(pair["review_status"], "approved")
            self.assertIn("human-reviewed", pair["review_reasons_json"])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=2").fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            connection.close()

    def test_existing_database_rejects_different_hmac_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = open_store(str(Path(temp_dir) / "m0.sqlite3"))
            initialize_schema(connection)
            initialize_run(connection, run_id="run-a", secret=SECRET_A)
            with self.assertRaisesRegex(RuntimeError, "HMAC key fingerprint mismatch"):
                initialize_run(connection, run_id="run-b", secret=SECRET_B)
            connection.close()

    def test_output_guard_and_stable_read_are_local_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            allowed = root / ".wechat-cs" / "runs" / "run-a" / "m0.sqlite3"
            assert_project_output(allowed, root)
            with self.assertRaisesRegex(ValueError, r"\.wechat-cs"):
                assert_project_output(root / "outside.sqlite3", root)

            source = root / "source.jsonl"
            source.write_bytes(b'{"fixture":true}\n')
            before = source.stat()
            stable = read_stable_bytes(source)
            after = source.stat()
            self.assertEqual(stable.data, b'{"fixture":true}\n')
            self.assertEqual(stable.sha256, hashlib.sha256(stable.data).hexdigest())
            self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_run_validation_and_publish_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            runs_dir = root / ".wechat-cs" / "runs"
            created = initialize_m0_run(
                runs_dir=runs_dir,
                secret=SECRET_A,
                project_root=root,
                run_id="fixture-run",
            )
            db_path = Path(created["db"])
            with self.assertRaisesRegex(RuntimeError, "acceptance gates are incomplete"):
                validate_m0_database(db_path)
            connection = open_store(str(db_path))
            connection.execute(
                "UPDATE pipeline_runs SET quality_json=? WHERE run_id='fixture-run'",
                (
                    json.dumps(
                        {
                            "acceptance_gates": {
                                "m0_a": True,
                                "m0_b": True,
                                "m0_c": True,
                                "m0_d": True,
                                "integration": True,
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
            connection.commit()
            connection.close()
            validation = validate_m0_database(db_path)
            self.assertEqual(validation["integrity_check"], "ok")
            self.assertEqual(validation["foreign_key_errors"], 0)
            self.assertEqual(validation["state"], "complete")

            output = root / ".wechat-cs" / "data" / "wechat_cs_m0.sqlite3"
            published = publish_m0_database(db_path, output, project_root=root)
            self.assertEqual(Path(published["output"]), output)
            self.assertTrue(output.is_file())
            published_bytes = output.read_bytes()

            invalid = root / ".wechat-cs" / "runs" / "invalid.sqlite3"
            invalid.parent.mkdir(parents=True, exist_ok=True)
            invalid.write_bytes(b"not a sqlite database")
            with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
                publish_m0_database(invalid, output, project_root=root)
            self.assertEqual(output.read_bytes(), published_bytes)

    def test_hmac_fingerprint_is_non_secret_and_stable(self) -> None:
        fingerprint = hmac_key_fingerprint(SECRET_A)
        self.assertEqual(fingerprint, hmac_key_fingerprint(SECRET_A))
        self.assertNotEqual(fingerprint, hmac_key_fingerprint(SECRET_B))
        self.assertNotIn(SECRET_A, fingerprint)


if __name__ == "__main__":
    unittest.main()
