from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path

from wechat_cs.__main__ import main
from wechat_cs.review_stages import (
    ReviewStageBlockedError,
    get_review_status,
    import_review_annotations,
    prepare_review_batch,
    to_public_review_payload,
)
from wechat_cs.store import initialize_schema, open_store


STAGE_TARGETS = {
    "protocol_20": 20,
    "acceptance_100": 100,
    "gold_500": 500,
}


def _opaque(prefix: str, index: int) -> str:
    return "%s_%s" % (
        prefix,
        sha256((prefix + ":" + str(index)).encode("utf-8")).hexdigest()[:24],
    )


class ReviewStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "review.sqlite3"
        connection = open_store(str(self.db_path))
        initialize_schema(connection)
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO pipeline_runs(
                        run_id,state,parser_version,hmac_key_fingerprint,
                        started_at,quality_json
                    ) VALUES('run-review','complete','test','f' || printf('%063d', 0),
                             '2026-07-13T09:00:00+08:00',?)
                    """,
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
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,
                        size,mtime_ns,sha256,record_count,first_at,last_at,
                        observed_until,captured_at,consistency_state,quality_json
                    ) VALUES('snapshot-review','run-review','live-inbox-events','opaque',
                             1,1,1,1,?,630,'2026-07-01T09:00:00+08:00',
                             '2026-07-13T09:00:00+08:00',
                             '2026-07-13T09:00:00+08:00',
                             '2026-07-13T09:00:00+08:00','consistent','{}')
                    """,
                    ("a" * 64,),
                )
                customer_rows = []
                card_rows = []
                audit_rows = []
                card_types = ("inbound", "proactive_followup")
                signals = ("positive", "negative", "mixed", "unknown")
                strategies = (
                    "answer_fact",
                    "clarify",
                    "recommend",
                    "quote",
                    "trust_proof",
                    "light_followup",
                    "aftersales_repair",
                    "handoff_human",
                    "other",
                )
                reuse_states = ("direct", "fill_slots", "case_only", "prohibited")
                splits = ("train", "validation", "test")
                for index in range(630):
                    customer_key = _opaque("customer", index)
                    card_id = _opaque("card", index)
                    customer_rows.append(
                        (
                            customer_key,
                            "匿名客户",
                            "2026-07-13T08:00:00+08:00",
                            "fixture.jsonl",
                        )
                    )
                    context_text = "想了解款式"
                    if index == 0:
                        context_text = "电话 13800138000，想了解款式"
                    card_rows.append(
                        (
                            card_id,
                            customer_key,
                            _opaque("episode", index),
                            card_types[index % len(card_types)],
                            "2026-07-13T08:%02d:00+08:00" % (index % 60),
                            index,
                            "snapshot-review",
                            "2026-07-14T08:00:00+08:00",
                            json.dumps(
                                [
                                    {
                                        "role": "customer",
                                        "text": context_text,
                                        "started_at": "2026-07-13T08:00:00+08:00",
                                        "ended_at": "2026-07-13T08:00:00+08:00",
                                    }
                                ],
                                ensure_ascii=False,
                            ),
                            json.dumps(
                                {
                                    "state": "immediate_reply",
                                    "reply_delay_seconds": 60,
                                    "message_keys": [_opaque("message", index)],
                                    "text": "我帮您核对库存",
                                },
                                ensure_ascii=False,
                            ),
                            splits[index % len(splits)],
                        )
                    )
                    audit_rows.append(
                        (
                            card_id,
                            signals[index % len(signals)],
                            strategies[index % len(strategies)],
                            reuse_states[index % len(reuse_states)],
                        )
                    )
                connection.executemany(
                    """
                    INSERT INTO customers(
                        customer_key,display_name,last_active_at,opportunity_score,
                        opportunity_level,aftersales_priority,summary,reasons_json,
                        evidence_json,memory_json,source_file
                    ) VALUES(?,?,?,0,'low',NULL,'review fixture','[]','[]','{}',?)
                    """,
                    customer_rows,
                )
                connection.executemany(
                    """
                    INSERT INTO decision_cards(
                        card_id,customer_key,episode_id,card_type,as_of_at,
                        boundary_ordinal,source_snapshot_id,action_window_end,
                        observation_until,blind_context_json,observed_action_json,
                        context_message_keys_json,action_message_keys_json,split,
                        review_status,rule_version,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,'2026-07-14T08:00:00+08:00',?,?,
                             '[]','[]',?,'pending','decision-card-v1',
                             '2026-07-13T09:00:00+08:00')
                    """,
                    card_rows,
                )
                connection.executemany(
                    """
                    INSERT INTO action_annotations(
                        card_id,customer_signal,reply_strategy,reuse_status,
                        required_facts_json,prohibited_claims_json,annotation_json,
                        rule_version,created_at
                    ) VALUES(?,?,?,?,'[]','[]','{}','reply-audit-rules-v1',
                             '2026-07-13T09:00:00+08:00')
                    """,
                    audit_rows,
                )
                connection.execute(
                    """
                    INSERT INTO card_outcomes(
                        card_id,paid_1d,paid_3d,paid_7d,retained_30d,
                        aftersale_30d,exchange_30d,compensation_30d,
                        refund_loss_ratio,attribution_state,attribution_flags_json,
                        matched_orders_json,computed_at
                    ) VALUES(?,1,1,1,1,0,0,0,0.0,'associated','[]',?,
                             '2026-07-13T09:00:00+08:00')
                    """,
                    (_opaque("card", 0), '["ORDER-SECRET-MUST-NOT-LEAK"]'),
                )
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _annotations(self, batch: dict) -> list[dict]:
        return [
            {
                "card_id": item["card_id"],
                "verdict": "approved",
                "labels": {
                    "customer_signal": item["reply_audit"]["customer_signal"],
                    "policy_compliant": True,
                },
                "notes": "协议一致",
            }
            for item in batch["items"]
        ]

    def test_exact_stratified_batches_are_deterministic_and_customer_isolated(self) -> None:
        protocol = prepare_review_batch(self.db_path, "protocol_20")
        self.assertEqual(protocol["count"], STAGE_TARGETS["protocol_20"])
        self.assertEqual(protocol, prepare_review_batch(self.db_path, "protocol_20"))

        with self.assertRaises(ReviewStageBlockedError):
            prepare_review_batch(self.db_path, "acceptance_100")
        import_review_annotations(
            self.db_path,
            stage="protocol_20",
            reviewer="reviewer_qa1",
            annotations=self._annotations(protocol),
        )

        acceptance = prepare_review_batch(self.db_path, "acceptance_100")
        self.assertEqual(acceptance["count"], STAGE_TARGETS["acceptance_100"])
        with self.assertRaises(ReviewStageBlockedError):
            prepare_review_batch(self.db_path, "gold_500")
        import_review_annotations(
            self.db_path,
            stage="acceptance_100",
            reviewer="reviewer_qa1",
            annotations=self._annotations(acceptance),
        )

        gold = prepare_review_batch(self.db_path, "gold_500")
        self.assertEqual(gold["count"], STAGE_TARGETS["gold_500"])
        customer_sets = [
            {item["customer_key"] for item in batch["items"]}
            for batch in (protocol, acceptance, gold)
        ]
        self.assertEqual([len(values) for values in customer_sets], [20, 100, 500])
        self.assertFalse(customer_sets[0] & customer_sets[1])
        self.assertFalse(customer_sets[0] & customer_sets[2])
        self.assertFalse(customer_sets[1] & customer_sets[2])
        for batch in (protocol, acceptance, gold):
            self.assertGreater(len(batch["strata"]), 1)

    def test_payload_separates_observed_action_and_never_loads_outcomes(self) -> None:
        batch = prepare_review_batch(self.db_path, "protocol_20")
        selected_id = batch["items"][0]["card_id"]
        connection = open_store(str(self.db_path))
        try:
            with connection:
                connection.execute(
                    "UPDATE decision_cards SET blind_context_json=? WHERE card_id=?",
                    (
                        json.dumps(
                            [
                                {
                                    "role": "customer",
                                    "text": "电话 13800138000，想了解款式",
                                    "started_at": "2026-07-13T08:00:00+08:00",
                                    "ended_at": "2026-07-13T08:00:00+08:00",
                                }
                            ],
                            ensure_ascii=False,
                        ),
                        selected_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO card_outcomes(
                        card_id,paid_1d,paid_3d,paid_7d,retained_30d,
                        aftersale_30d,exchange_30d,compensation_30d,
                        refund_loss_ratio,attribution_state,attribution_flags_json,
                        matched_orders_json,computed_at
                    ) VALUES(?,1,1,1,1,0,0,0,0.0,'associated','[]',?,
                             '2026-07-13T09:00:00+08:00')
                    ON CONFLICT(card_id) DO UPDATE SET
                        paid_1d=1,matched_orders_json=excluded.matched_orders_json
                    """,
                    (selected_id, '["ORDER-SECRET-MUST-NOT-LEAK"]'),
                )
        finally:
            connection.close()
        batch = prepare_review_batch(self.db_path, "protocol_20")
        serialized = json.dumps(batch, ensure_ascii=False, sort_keys=True)
        self.assertIn("observed_action", batch["items"][0])
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("ORDER-SECRET-MUST-NOT-LEAK", serialized)
        self.assertNotIn("paid_1d", serialized)
        self.assertNotIn("matched_orders", serialized)

        public = to_public_review_payload(batch["items"][0])
        public_serialized = json.dumps(public, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("observed_action", public)
        self.assertNotIn("reply_audit", public)
        self.assertNotIn("ORDER-SECRET-MUST-NOT-LEAK", public_serialized)
        self.assertEqual(set(public), {"card_id", "card_type", "as_of_at", "context"})

    def test_annotation_import_is_idempotent_safe_and_drives_status(self) -> None:
        batch = prepare_review_batch(self.db_path, "protocol_20")
        annotations = self._annotations(batch)
        first = import_review_annotations(
            self.db_path,
            stage="protocol_20",
            reviewer="reviewer_qa1",
            annotations=annotations,
        )
        second = import_review_annotations(
            self.db_path,
            stage="protocol_20",
            reviewer="reviewer_qa1",
            annotations=annotations,
        )
        self.assertEqual(first["rows_total"], 20)
        self.assertEqual(second["rows_total"], 20)
        connection = open_store(str(self.db_path), read_only=True)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM card_annotations WHERE review_stage='protocol_20'"
            ).fetchone()[0]
            gates = json.loads(
                connection.execute(
                    "SELECT quality_json FROM pipeline_runs WHERE run_id='run-review'"
                ).fetchone()[0]
            )["acceptance_gates"]
        finally:
            connection.close()
        self.assertEqual(count, 20)
        self.assertFalse(gates["m0_c"])

        status = get_review_status(self.db_path)
        self.assertEqual(status["stages"]["protocol_20"]["status"], "complete")
        self.assertEqual(status["stages"]["acceptance_100"]["status"], "not_started")
        self.assertFalse(status["automatic_approval"])

        with self.assertRaises(ValueError):
            import_review_annotations(
                self.db_path,
                stage="protocol_20",
                reviewer="reviewer_qa1",
                annotations=[
                    {
                        "card_id": batch["items"][0]["card_id"],
                        "verdict": "approved",
                        "labels": {"paid_1d": True},
                    }
                ],
            )

    def test_cli_review_batch_and_status_emit_json_without_changing_gates(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "review-batch",
                    "--db",
                    str(self.db_path),
                    "--stage",
                    "protocol_20",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["count"], 20)

        output = StringIO()
        with redirect_stdout(output):
            code = main(["review-status", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["stages"]["protocol_20"]["status"], "not_started")
        self.assertFalse(status["automatic_approval"])


if __name__ == "__main__":
    unittest.main()
