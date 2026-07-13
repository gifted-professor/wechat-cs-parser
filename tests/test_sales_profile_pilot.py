from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests.test_sales_profile_sampling import rich_candidate_pool
from wechat_cs.sales_profile_pilot import _order_is_complex_at, prepare_sales_profile_pilot
from wechat_cs.store import initialize_m0_run, open_store


SECRET = "sales-profile-pilot-secret-with-at-least-32-characters"
RUN_ID = "20260713T140730+0800-833c3257"
AS_OF = "2026-07-13T20:14:37+08:00"


class PrepareSalesProfilePilotTests(unittest.TestCase):
    def test_future_refund_never_makes_a_historical_complex_candidate(self) -> None:
        cutoff = datetime.fromisoformat(AS_OF)
        self.assertFalse(
            _order_is_complex_at(
                {
                    "refund_type": "return",
                    "refund_on": "2026-08-01",
                    "quality_flags_json": '["future_refund_on","aftersale_open"]',
                },
                cutoff,
            )
        )
        self.assertTrue(
            _order_is_complex_at(
                {
                    "refund_type": "return",
                    "refund_on": "2026-07-01",
                    "quality_flags_json": "[]",
                },
                cutoff,
            )
        )

    def _database(self, root: Path) -> Path:
        created = initialize_m0_run(
            runs_dir=root / ".wechat-cs" / "runs",
            secret=SECRET,
            project_root=root,
            run_id=RUN_ID,
        )
        db_path = Path(created["db"])
        connection = open_store(str(db_path))
        try:
            quality = {
                "acceptance_gates": {"m0_a": True, "m0_b": True, "m0_c": False}
            }
            with connection:
                connection.execute(
                    "UPDATE pipeline_runs SET quality_json=? WHERE run_id=?",
                    (json.dumps(quality, sort_keys=True), RUN_ID),
                )
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                        mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                        captured_at,consistency_state,quality_json
                    ) VALUES('events-snapshot',?,'live-inbox-events','opaque',1,1,10,1,?,100,
                             '2026-01-01T00:00:00+08:00',?,?,'2026-07-13T20:14:37+08:00',
                             'consistent','{}')
                    """,
                    (RUN_ID, "e" * 64, AS_OF, AS_OF),
                )
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                        mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                        captured_at,consistency_state,quality_json
                    ) VALUES('orders-source',?,'orders_live','opaque',1,2,10,2,?,100,
                             NULL,NULL,?,'2026-07-13T20:00:00+08:00','consistent','{}')
                    """,
                    (RUN_ID, "o" * 64, AS_OF),
                )
                connection.execute(
                    """
                    INSERT INTO order_snapshots(
                        order_snapshot_id,source_snapshot_id,synced_at,record_count,state,quality_json
                    ) VALUES('orders-active','orders-source',?,0,'active','{}')
                    """,
                    (AS_OF,),
                )
        finally:
            connection.close()
        return db_path

    def test_prepare_freezes_exact_cohort_is_idempotent_and_never_calls_kimi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            pool = [replace(item, feature_snapshot_id=None) for item in rich_candidate_pool()]
            connection = open_store(str(db_path))
            try:
                with connection:
                    for item in pool:
                        connection.execute(
                            """
                            INSERT INTO customers(
                                customer_key,display_name,last_active_at,opportunity_score,
                                opportunity_level,summary,reasons_json,evidence_json,memory_json,
                                source_file
                            ) VALUES(?, 'fixture', ?, 0, 'low', 'fixture', '[]', '[]', '{}',
                                     'events.jsonl')
                            """,
                            (item.customer_key, AS_OF),
                        )
            finally:
                connection.close()
            with (
                patch(
                    "wechat_cs.sales_profile_pilot.refresh_sales_profile_features",
                    return_value={"feature_snapshots": len(pool)},
                ) as refresh,
                patch(
                    "wechat_cs.sales_profile_pilot.load_sampling_candidates",
                    return_value=tuple(pool),
                ),
                patch("wechat_cs.kimi_client.KimiJsonClient.complete_json") as kimi,
            ):
                first = prepare_sales_profile_pilot(
                    db_path,
                    as_of_at=AS_OF,
                    source_run_id=RUN_ID,
                    secret=SECRET,
                )
                second = prepare_sales_profile_pilot(
                    db_path,
                    as_of_at=AS_OF,
                    source_run_id=RUN_ID,
                    secret=SECRET,
                )

            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["sales_profile_run_id"], second["sales_profile_run_id"])
            self.assertEqual(first["subject_count"], 50)
            self.assertEqual(sum(first["stratum_counts"].values()), 50)
            self.assertEqual(set(first["profile_counts"]), {"aolai1", "aolai2", "aolai4", "service"})
            self.assertGreaterEqual(first["birthday_match_count"], 5)
            self.assertFalse(first["model_called"])
            self.assertFalse(first["send_allowed"])
            self.assertEqual(refresh.call_count, 2)
            kimi.assert_not_called()
            self.assertNotIn("phone_", json.dumps(first, sort_keys=True))

            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sales_profile_runs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sales_profile_subjects").fetchone()[0], 50)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sales_profiles").fetchone()[0], 50)
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM sales_profile_runs"
                    ).fetchone()[0],
                    "prepared",
                )
                quality = json.loads(
                    connection.execute(
                        "SELECT quality_json FROM pipeline_runs WHERE run_id=?", (RUN_ID,)
                    ).fetchone()[0]
                )
                self.assertEqual(
                    quality,
                    {"acceptance_gates": {"m0_a": True, "m0_b": True, "m0_c": False}},
                )
            finally:
                connection.close()

    def test_prepare_rejects_a_different_source_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            with self.assertRaisesRegex(RuntimeError, "source run"):
                prepare_sales_profile_pilot(
                    db_path,
                    as_of_at=AS_OF,
                    source_run_id="wrong-run",
                    secret=SECRET,
                )


if __name__ == "__main__":
    unittest.main()
