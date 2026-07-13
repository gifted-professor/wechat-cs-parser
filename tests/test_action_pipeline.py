from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from wechat_cs.__main__ import main
from wechat_cs.action_pipeline import (
    _index_feature_inputs,
    _scope_feature_inputs,
    build_action_artifacts,
)
from wechat_cs.source_snapshot import hmac_key_fingerprint
from wechat_cs.store import initialize_m0_run, open_store


SECRET = "action-pipeline-fixture-secret-with-at-least-32-characters"
AS_OF = "2026-07-13T12:00:00+08:00"
CUSTOMER_ONE = "customer_111111111111111111111111"
CUSTOMER_TWO = "customer_222222222222222222222222"
PHONE_ONE = "phone_111111111111111111111111"
PHONE_TWO = "phone_222222222222222222222222"


class ActionPipelineTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        created = initialize_m0_run(
            runs_dir=root / ".wechat-cs" / "runs",
            secret=SECRET,
            project_root=root,
            run_id="action-pipeline-run",
        )
        db_path = Path(created["db"])
        connection = open_store(str(db_path))
        try:
            with connection:
                connection.execute(
                    "UPDATE pipeline_runs SET quality_json=? WHERE run_id=?",
                    (
                        json.dumps(
                            {
                                "acceptance_gates": {
                                    "m0_a": True,
                                    "m0_b": True,
                                    "m0_c": False,
                                    "m0_d": False,
                                    "integration": False,
                                }
                            },
                            sort_keys=True,
                        ),
                        "action-pipeline-run",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                        mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                        captured_at,consistency_state,quality_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "snapshot_events",
                        "action-pipeline-run",
                        "live-inbox-events",
                        "opaque-source-path",
                        1,
                        1,
                        100,
                        1,
                        "e" * 64,
                        7,
                        "2026-05-01T10:00:00+08:00",
                        "2026-07-14T09:00:00+08:00",
                        "2026-07-13T11:58:00+08:00",
                        "2026-07-13T11:58:10+08:00",
                        "consistent",
                        "{}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                        mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                        captured_at,consistency_state,quality_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "snapshot_orders",
                        "action-pipeline-run",
                        "orders_live",
                        "opaque-order-path",
                        1,
                        2,
                        100,
                        2,
                        "o" * 64,
                        3,
                        None,
                        None,
                        "2026-07-13T11:50:00+08:00",
                        "2026-07-13T11:50:10+08:00",
                        "consistent",
                        "{}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO account_registry(
                        profile_id,canonical_account_id,state,confidence,evidence_json,
                        config_hash,version
                    ) VALUES('aolai1','account-one','approved',1.0,'{}','cfg','accounts-v1')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO profile_observations(
                        snapshot_id,profile_id,observed_until,initialized,last_error_code,
                        consistency_state
                    ) VALUES('snapshot_events','aolai1','2026-07-13T11:58:00+08:00',1,NULL,'consistent')
                    """
                )
                for customer, suffix in ((CUSTOMER_ONE, "one"), (CUSTOMER_TWO, "two")):
                    connection.execute(
                        """
                        INSERT INTO customers(
                            customer_key,display_name,last_active_at,opportunity_score,
                            opportunity_level,aftersales_priority,summary,reasons_json,
                            evidence_json,memory_json,source_file
                        ) VALUES(?,?,?,0,'low',NULL,'fixture','[]','[]','{}','events.jsonl')
                        """,
                        (customer, "客户-" + suffix, "2026-07-13T11:55:00+08:00"),
                    )
                    connection.execute(
                        """
                        INSERT INTO conversation_refs(
                            customer_key,profile_id,canonical_account_id,raw_wechat_id_hash,
                            source_snapshot_id
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            customer,
                            "aolai1",
                            "account-one",
                            "raw-wechat-id-must-not-copy-" + suffix,
                            "snapshot_events",
                        ),
                    )
                messages = (
                    (
                        "message_111111111111111111111111",
                        CUSTOMER_ONE,
                        "customer",
                        "2026-07-13T11:55:00+08:00",
                        "想要这件，请问还有吗？",
                        1,
                    ),
                    (
                        "message_222222222222222222222221",
                        CUSTOMER_TWO,
                        "customer",
                        "2026-06-01T10:00:00+08:00",
                        "想看看针织衫",
                        2,
                    ),
                    (
                        "message_222222222222222222222222",
                        CUSTOMER_TWO,
                        "studio",
                        "2026-06-01T10:05:00+08:00",
                        "我先帮您核对款式。",
                        3,
                    ),
                    # This source row must not affect a July 13 build.
                    (
                        "message_111111111111111111111119",
                        CUSTOMER_ONE,
                        "customer",
                        "2026-07-14T09:00:00+08:00",
                        "FUTURE-MESSAGE-MUST-NOT-LEAK",
                        9,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO messages(
                        message_key,customer_key,role,timestamp,text,source_file,source_ordinal
                    ) VALUES(?,?,?,?,?,'events.jsonl',?)
                    """,
                    messages,
                )
                links = (
                    ("link-one", CUSTOMER_ONE, PHONE_ONE),
                    ("link-two", CUSTOMER_TWO, PHONE_TWO),
                )
                for link_id, customer, phone in links:
                    connection.execute(
                        """
                        INSERT INTO conversation_links(
                            link_id,customer_key,profile_id,raw_wechat_id_hash,phone_hmac,
                            match_method,confidence,state,source_hash,version,reviewed_at
                        ) VALUES(?,?, 'aolai1','opaque-raw-hash',?,'fixture',1.0,'approved',
                                 'fixture-source','identity-v1','2026-07-13T10:00:00+08:00')
                        """,
                        (link_id, customer, phone),
                    )
                    connection.execute(
                        """
                        INSERT INTO conversation_order_eligibility(
                            customer_key,eligibility,source_hash,version,evaluated_at
                        ) VALUES(?,'order_customer','fixture-source','eligibility-v1',
                                 '2026-07-13T10:00:00+08:00')
                        """,
                        (customer,),
                    )
                connection.execute(
                    """
                    INSERT INTO order_snapshots(
                        order_snapshot_id,source_snapshot_id,synced_at,record_count,state,
                        quality_json
                    ) VALUES('orders-active','snapshot_orders','2026-07-13T11:50:00+08:00',
                             3,'active','{}')
                    """
                )
                orders = (
                    ("order_old_one", "old-one", PHONE_TWO, "2026-05-01", 20000),
                    ("order_old_two", "old-two", PHONE_TWO, "2026-06-01", 30000),
                    ("order_future", "future", PHONE_ONE, "2026-07-14", 99900),
                )
                connection.executemany(
                    """
                    INSERT INTO orders(
                        order_line_id,order_snapshot_id,source_namespace,record_id,phone_hmac,
                        paid_on,revenue_minor,currency,platform,sku_name,factory,category,
                        color,size,refund_type,refund_reason,refund_amount_minor,refund_on,
                        return_status,source_hash,quality_flags_json
                    ) VALUES(?,'orders-active','fixture-orders',?,?,? ,?,'CNY','wechat',
                             '针织衫','fixture-factory','针织','蓝色','M',NULL,NULL,NULL,NULL,
                             NULL,'fixture-source','[]')
                    """,
                    orders,
                )
        finally:
            connection.close()
        return db_path

    def test_historical_cards_reuse_one_feature_input_index_per_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            with patch(
                "wechat_cs.action_pipeline._index_feature_inputs",
                wraps=_index_feature_inputs,
            ) as index_builder:
                result = build_action_artifacts(
                    db_path,
                    as_of_at=AS_OF,
                    collector_status="running",
                    profile_id="aolai1",
                    secret=SECRET,
                )

            self.assertGreater(result["profiles"]["aolai1"]["decision_cards"], 0)
            self.assertEqual(index_builder.call_count, 1)

    def test_feature_scope_follows_only_approved_shared_phone_links(self) -> None:
        linked_customer = "customer_333333333333333333333333"
        transitive_customer = "customer_444444444444444444444444"
        conflict_customer = "customer_555555555555555555555555"
        phone_three = "phone_333333333333333333333333"
        identity_rows = [
            {"customer_key": CUSTOMER_ONE, "phone_hmac": PHONE_ONE, "state": "approved"},
            {"customer_key": linked_customer, "phone_hmac": PHONE_ONE, "state": "approved"},
            {"customer_key": linked_customer, "phone_hmac": phone_three, "state": "approved"},
            {"customer_key": transitive_customer, "phone_hmac": phone_three, "state": "approved"},
            {"customer_key": conflict_customer, "phone_hmac": PHONE_ONE, "state": "conflict"},
        ]
        index = _index_feature_inputs(identity_rows, (), ())

        scoped_identity, scoped_orders, scoped_messages = _scope_feature_inputs(
            index, (CUSTOMER_ONE,)
        )

        self.assertEqual(
            {str(row["customer_key"]) for row in scoped_identity},
            {CUSTOMER_ONE, linked_customer, transitive_customer},
        )
        self.assertEqual(scoped_orders, [])
        self.assertEqual(scoped_messages, [])

    def test_build_persists_point_in_time_artifacts_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            connection = open_store(str(db_path))
            try:
                with connection:
                    connection.execute(
                        "UPDATE build_meta SET updated_at='stable-v3-meta' "
                        "WHERE key='schema_version'"
                    )
                source_before = {
                    table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in (
                        "source_snapshots",
                        "profile_observations",
                        "messages",
                        "orders",
                    )
                }
                quality_before = connection.execute(
                    "SELECT quality_json FROM pipeline_runs WHERE run_id='action-pipeline-run'"
                ).fetchone()[0]
                schema_meta_before = connection.execute(
                    "SELECT updated_at FROM build_meta WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                connection.close()

            first = build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            connection = open_store(str(db_path), read_only=True)
            try:
                derived_before = {
                    table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                    for table in (
                        "customer_value_snapshots",
                        "decision_cards",
                        "card_feature_snapshots",
                        "card_outcomes",
                        "action_annotations",
                        "action_queue_runs",
                        "action_queue_items",
                    )
                }
            finally:
                connection.close()
            second = build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            self.assertEqual(first, second)
            self.assertNotIn(PHONE_ONE, json.dumps(first))
            self.assertEqual(first["profiles"]["aolai1"]["queue_status"], "ready")
            self.assertEqual(first["profiles"]["aolai1"]["queue_counts"]["reply_now"], 1)

            connection = open_store(str(db_path), read_only=True)
            try:
                derived_after = {
                    table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                    for table in derived_before
                }
                self.assertEqual(derived_before, derived_after)
                source_after = {
                    table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in source_before
                }
                self.assertEqual(source_before, source_after)
                self.assertEqual(
                    connection.execute(
                        "SELECT updated_at FROM build_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    schema_meta_before,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT quality_json FROM pipeline_runs WHERE run_id='action-pipeline-run'"
                    ).fetchone()[0],
                    quality_before,
                )
                quality = json.loads(quality_before)
                self.assertFalse(quality["acceptance_gates"]["m0_c"])

                counts = {
                    table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in (
                        "customer_value_snapshots",
                        "decision_cards",
                        "card_feature_snapshots",
                        "card_outcomes",
                        "action_annotations",
                        "action_queue_runs",
                        "action_queue_items",
                    )
                }
                self.assertGreaterEqual(counts["customer_value_snapshots"], 2)
                self.assertGreater(counts["decision_cards"], 0)
                self.assertEqual(counts["card_feature_snapshots"], counts["decision_cards"])
                self.assertEqual(counts["card_outcomes"], counts["decision_cards"])
                self.assertEqual(counts["action_annotations"], counts["decision_cards"])
                self.assertEqual(counts["action_queue_runs"], 1)
                self.assertEqual(counts["action_queue_items"], 2)

                queue_run = connection.execute(
                    "SELECT * FROM action_queue_runs WHERE profile_id='aolai1' "
                    "AND queue_date='2026-07-13'"
                ).fetchone()
                self.assertEqual(queue_run["status"], "ready")
                self.assertEqual(queue_run["as_of_at"], AS_OF)
                self.assertEqual(queue_run["policy_version"], "action-queue-rules-v2")
                self.assertEqual(json.loads(queue_run["block_reasons_json"]), [])
                self.assertEqual(
                    json.loads(queue_run["freshness_json"])["messages"]["state"],
                    "fresh",
                )
                self.assertEqual(
                    json.loads(queue_run["counts_json"]),
                    {"reply_now": 1, "proactive_today": 1, "suppressed": 0},
                )
                inbound_item = connection.execute(
                    "SELECT signals_json,missing_facts_json FROM action_queue_items "
                    "WHERE customer_key=?",
                    (CUSTOMER_ONE,),
                ).fetchone()
                signals = json.loads(inbound_item["signals_json"])
                self.assertEqual(signals["intent_signal"], "positive")
                self.assertEqual(json.loads(inbound_item["missing_facts_json"]), [])

                serialized_cards = "\n".join(
                    row[0]
                    for row in connection.execute(
                        "SELECT blind_context_json || observed_action_json FROM decision_cards"
                    )
                )
                self.assertNotIn("FUTURE-MESSAGE-MUST-NOT-LEAK", serialized_cards)
                self.assertNotIn("raw-wechat-id-must-not-copy", serialized_cards)
                self.assertNotIn(PHONE_ONE, serialized_cards)
                future_matches = connection.execute(
                    "SELECT COUNT(*) FROM card_outcomes WHERE matched_orders_json LIKE '%order_future%'"
                ).fetchone()[0]
                self.assertEqual(future_matches, 0)
                customer_one_profiles = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT profile_json FROM customer_value_snapshots WHERE customer_key=?",
                        (CUSTOMER_ONE,),
                    )
                ]
                self.assertTrue(customer_one_profiles)
                self.assertTrue(
                    all(item.get("rfm_frequency") in (None, 0) for item in customer_one_profiles)
                )
                self.assertEqual(
                    connection.execute("PRAGMA foreign_key_check").fetchall(), []
                )
            finally:
                connection.close()

    def test_partially_upgraded_v3_schema_is_repaired_only_when_incomplete(self) -> None:
        for missing_part in ("queue_run_table", "queue_item_columns", "suppression_table"):
            with self.subTest(missing_part=missing_part), tempfile.TemporaryDirectory() as temp_dir:
                db_path = self._database(Path(temp_dir).resolve())
                connection = open_store(str(db_path))
                try:
                    with connection:
                        if missing_part == "queue_run_table":
                            connection.execute("DROP TABLE action_queue_runs")
                        elif missing_part == "queue_item_columns":
                            connection.execute(
                                "ALTER TABLE action_queue_items DROP COLUMN signals_json"
                            )
                            connection.execute(
                                "ALTER TABLE action_queue_items DROP COLUMN missing_facts_json"
                            )
                        else:
                            connection.execute("DROP TABLE contact_suppressions")
                        connection.execute("PRAGMA user_version = 3")
                        connection.execute(
                            "UPDATE build_meta SET updated_at='partial-v3-meta' "
                            "WHERE key='schema_version'"
                        )
                finally:
                    connection.close()

                build_action_artifacts(
                    db_path,
                    as_of_at=AS_OF,
                    collector_status="stopped",
                    profile_id="aolai1",
                    secret=SECRET,
                )
                connection = open_store(str(db_path))
                try:
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='action_queue_runs'"
                        ).fetchone()
                    )
                    columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(action_queue_items)"
                        )
                    }
                    self.assertTrue(
                        {"signals_json", "missing_facts_json"}.issubset(columns)
                    )
                    repaired_at = connection.execute(
                        "SELECT updated_at FROM build_meta WHERE key='schema_version'"
                    ).fetchone()[0]
                    self.assertNotEqual(repaired_at, "partial-v3-meta")
                    with connection:
                        connection.execute(
                            "UPDATE build_meta SET updated_at='stable-after-repair' "
                            "WHERE key='schema_version'"
                        )
                finally:
                    connection.close()

                build_action_artifacts(
                    db_path,
                    as_of_at=AS_OF,
                    collector_status="stopped",
                    profile_id="aolai1",
                    secret=SECRET,
                )
                connection = open_store(str(db_path), read_only=True)
                try:
                    self.assertEqual(
                        connection.execute(
                            "SELECT updated_at FROM build_meta WHERE key='schema_version'"
                        ).fetchone()[0],
                        "stable-after-repair",
                    )
                finally:
                    connection.close()

    def test_stopped_collector_blocks_inbound_but_keeps_historical_proactive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            result = build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="stopped",
                profile_id="aolai1",
                secret=SECRET,
            )
            profile = result["profiles"]["aolai1"]
            self.assertEqual(profile["queue_status"], "ready")
            self.assertEqual(profile["queue_counts"], {
                "reply_now": 0,
                "proactive_today": 1,
                "suppressed": 1,
            })
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM action_queue_items WHERE lane!='suppressed'"
                    ).fetchone()[0],
                    1,
                )
                reasons = "\n".join(
                    row[0]
                    for row in connection.execute(
                        "SELECT reason_codes_json FROM action_queue_items"
                    )
                )
                self.assertIn("message_collection_unhealthy", reasons)
                self.assertIn("historical_snapshot_only", reasons)
                self.assertIn("contact_precheck_required", reasons)
                queue_run = connection.execute(
                    "SELECT status,block_reasons_json,freshness_json,counts_json "
                    "FROM action_queue_runs WHERE profile_id='aolai1' "
                    "AND queue_date='2026-07-13'"
                ).fetchone()
                self.assertEqual(queue_run["status"], "ready")
                self.assertEqual(
                    json.loads(queue_run["block_reasons_json"]),
                    [],
                )
                self.assertEqual(
                    json.loads(queue_run["freshness_json"])["messages"]["state"],
                    "unhealthy",
                )
                self.assertEqual(
                    json.loads(queue_run["counts_json"]),
                    {"reply_now": 0, "proactive_today": 1, "suppressed": 1},
                )
            finally:
                connection.close()

    def test_null_phone_conflict_is_suppressed_without_joining_order_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            connection = open_store(str(db_path))
            try:
                with connection:
                    connection.execute(
                        "UPDATE conversation_links SET state='conflict',phone_hmac=NULL "
                        "WHERE customer_key=?",
                        (CUSTOMER_ONE,),
                    )
            finally:
                connection.close()
            result = build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            self.assertEqual(result["profiles"]["aolai1"]["queue_counts"]["reply_now"], 0)
            connection = open_store(str(db_path), read_only=True)
            try:
                row = connection.execute(
                    "SELECT lane,reason_codes_json FROM action_queue_items WHERE customer_key=?",
                    (CUSTOMER_ONE,),
                ).fetchone()
                self.assertEqual(row["lane"], "suppressed")
                self.assertIn("identity_conflict", json.loads(row["reason_codes_json"]))
                profile = json.loads(
                    connection.execute(
                        "SELECT profile_json FROM customer_value_snapshots "
                        "WHERE customer_key=? AND as_of_at=?",
                        (CUSTOMER_ONE, AS_OF),
                    ).fetchone()[0]
                )
                self.assertEqual(profile["identity_state"], "conflict")
                self.assertFalse(profile["order_features_available"])
            finally:
                connection.close()

    def test_explicit_contact_refusal_persists_until_human_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            connection = open_store(str(db_path))
            try:
                with connection:
                    connection.executemany(
                        """
                        INSERT INTO messages(
                            message_key,customer_key,role,timestamp,text,source_file,source_ordinal
                        ) VALUES(?,?,?,?,?,'events.jsonl',?)
                        """,
                        (
                            (
                                "message_refusal_111111111111",
                                CUSTOMER_ONE,
                                "customer",
                                "2026-07-13T10:00:00+08:00",
                                "请不要联系我",
                                10,
                            ),
                            (
                                "message_refusal_reply_11111",
                                CUSTOMER_ONE,
                                "studio",
                                "2026-07-13T10:01:00+08:00",
                                "收到",
                                11,
                            ),
                            (
                                "message_refusal_later_11111",
                                CUSTOMER_ONE,
                                "customer",
                                "2026-07-13T11:56:00+08:00",
                                "我想看看别的款",
                                12,
                            ),
                        ),
                    )
            finally:
                connection.close()

            build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            connection = open_store(str(db_path))
            try:
                row = connection.execute(
                    "SELECT lane,reason_codes_json FROM action_queue_items WHERE customer_key=?",
                    (CUSTOMER_ONE,),
                ).fetchone()
                self.assertEqual(row["lane"], "suppressed")
                self.assertIn("explicit_rejection", json.loads(row["reason_codes_json"]))
                suppression = connection.execute(
                    "SELECT suppression_id,ends_at FROM contact_suppressions "
                    "WHERE customer_key=? AND reason_code='explicit_rejection'",
                    (CUSTOMER_ONE,),
                ).fetchone()
                self.assertIsNotNone(suppression)
                self.assertIsNone(suppression["ends_at"])
                with connection:
                    connection.execute(
                        "UPDATE contact_suppressions SET ends_at='2026-07-13T11:00:00+08:00' "
                        "WHERE suppression_id=?",
                        (suppression["suppression_id"],),
                    )
            finally:
                connection.close()

            build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            connection = open_store(str(db_path), read_only=True)
            try:
                row = connection.execute(
                    "SELECT lane,reason_codes_json FROM action_queue_items WHERE customer_key=?",
                    (CUSTOMER_ONE,),
                ).fetchone()
                self.assertEqual(row["lane"], "reply_now")
                self.assertNotIn("explicit_rejection", json.loads(row["reason_codes_json"]))
            finally:
                connection.close()

    def test_same_artifact_keeps_feedback_but_new_cutoff_resets_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            connection = open_store(str(db_path))
            try:
                action_id = connection.execute(
                    "SELECT action_id FROM action_queue_items WHERE customer_key=?",
                    (CUSTOMER_ONE,),
                ).fetchone()[0]
                with connection:
                    connection.execute(
                        "UPDATE action_queue_items SET human_confirmation_state='adopted' "
                        "WHERE action_id=?",
                        (action_id,),
                    )
                    connection.execute(
                        "INSERT INTO action_queue_feedback(feedback_id,action_id,outcome,final_text,"
                        "reason_codes_json,reviewer,created_at) VALUES(?,?,'adopted',NULL,'[]',?,?)",
                        (
                            "feedback-stable-artifact",
                            action_id,
                            "reviewer-test",
                            "2026-07-13T12:00:10+08:00",
                        ),
                    )
            finally:
                connection.close()

            build_action_artifacts(
                db_path,
                as_of_at=AS_OF,
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT human_confirmation_state FROM action_queue_items WHERE action_id=?",
                        (action_id,),
                    ).fetchone()[0],
                    "adopted",
                )
            finally:
                connection.close()

            build_action_artifacts(
                db_path,
                as_of_at="2026-07-13T12:01:00+08:00",
                collector_status="running",
                profile_id="aolai1",
                secret=SECRET,
            )
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT human_confirmation_state FROM action_queue_items WHERE action_id=?",
                        (action_id,),
                    ).fetchone()[0],
                    "pending",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM action_queue_feedback WHERE action_id=?",
                        (action_id,),
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_cli_build_action_queue_uses_same_safe_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir).resolve())
            prior = os.environ.get("WECHAT_CS_HMAC_SECRET")
            os.environ["WECHAT_CS_HMAC_SECRET"] = SECRET
            output = StringIO()
            try:
                with redirect_stdout(output):
                    code = main(
                        [
                            "build-action-queue",
                            "--db",
                            str(db_path),
                            "--as-of",
                            AS_OF,
                            "--collector-status",
                            "stopped",
                            "--profile",
                            "aolai1",
                        ]
                    )
            finally:
                if prior is None:
                    os.environ.pop("WECHAT_CS_HMAC_SECRET", None)
                else:
                    os.environ["WECHAT_CS_HMAC_SECRET"] = prior
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["profiles"]["aolai1"]["queue_status"], "ready")
            self.assertEqual(
                payload["profiles"]["aolai1"]["queue_counts"],
                {"reply_now": 0, "proactive_today": 1, "suppressed": 1},
            )
            self.assertNotIn(PHONE_ONE, output.getvalue())


if __name__ == "__main__":
    unittest.main()
