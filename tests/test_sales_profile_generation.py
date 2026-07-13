from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_cs.kimi_client import KimiSchemaError
from wechat_cs.sales_profile_generation import (
    _load_deterministic_facts,
    run_sales_profile_pilot,
    validate_extracted_events,
)
from wechat_cs.sales_profile_raw import RawConversationSnapshot, RawSalesMessage
from wechat_cs.sales_profile_sampling import SAMPLING_VERSION
from wechat_cs.store import initialize_m0_run, open_store


SECRET = "generation-fixture-secret-with-at-least-32-characters"
SOURCE_RUN_ID = "generation-source-run"
PILOT_RUN_ID = "sales-profile-run-fixture"
AS_OF = "2026-07-13T20:14:37+08:00"


def raw_message(customer_key: str, message_key: str, text: str) -> RawSalesMessage:
    return RawSalesMessage(
        message_key=message_key,
        customer_key=customer_key,
        profile_id="aolai1",
        role="customer",
        timestamp="2026-07-01T10:00:00+08:00",
        text=text,
        event_id="event-" + message_key,
        source_ordinal=1,
    )


class FakeKimiClient:
    def __init__(self, *, fail_text: str | None = None) -> None:
        self.fail_text = fail_text
        self.calls = []

    def complete_json(self, messages, model, temperature, timeout_seconds, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        self.calls.append((payload, model, temperature, timeout_seconds))
        if payload["task"] == "extract_sales_events":
            first = payload["messages"][0]
            if self.fail_text and self.fail_text in first["text"]:
                return {"events": "invalid"}
            return {
                "events": [
                    {
                        "event_type": "future_return",
                        "summary": "客户表示稍后再看",
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "kind": "message",
                                "message_key": first["message_key"],
                                "quote": first["text"],
                            }
                        ],
                    }
                ]
            }
        return {
            "customer_value": {"summary": "有历史订单"},
            "product_preferences": {"summary": "未知"},
            "time_rhythm": {"summary": "证据不足"},
            "purchase_drivers": ["回访承诺"],
            "historical_commitments": ["稍后再看"],
            "current_opportunity": {"summary": "可人工核对后联系"},
            "contact_reason": "承接客户自己的回访表达",
            "natural_opening": "上次您说晚点再看看，我来帮您接着看一下。",
            "risks": ["联系前核对最新状态"],
            "unknowns": ["当前是否仍有需求"],
            "evidence": [],
        }


class EventValidationTests(unittest.TestCase):
    def test_fabricated_quote_order_id_and_numeric_value_are_rejected_per_event(self) -> None:
        customer = "customer_" + "1" * 24
        messages = [raw_message(customer, "message-real", "过几天我再来看看蓝色外套")]
        orders = {
            "order-real": {
                "order_line_id": "order-real",
                "revenue_minor": 29900,
                "sku_name": "蓝色外套",
            }
        }
        payload = {
            "events": [
                {
                    "event_type": "future_return",
                    "summary": "稍后回访",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "kind": "message",
                            "message_key": "message-real",
                            "quote": "过几天我再来看看",
                        }
                    ],
                },
                {
                    "event_type": "product_preference",
                    "summary": "伪造原话",
                    "confidence": 0.8,
                    "evidence": [
                        {
                            "kind": "message",
                            "message_key": "message-real",
                            "quote": "从未说过红色裙子",
                        }
                    ],
                },
                {
                    "event_type": "product_preference",
                    "summary": "伪造订单",
                    "confidence": 0.8,
                    "evidence": [
                        {
                            "kind": "order",
                            "order_line_id": "order-fake",
                            "field": "sku_name",
                            "value": "蓝色外套",
                        }
                    ],
                },
                {
                    "event_type": "price_hesitation",
                    "summary": "金额不一致",
                    "confidence": 0.8,
                    "evidence": [
                        {
                            "kind": "order",
                            "order_line_id": "order-real",
                            "field": "revenue_minor",
                            "value": 39900,
                        }
                    ],
                },
            ]
        }
        events = validate_extracted_events(
            payload,
            messages=messages,
            orders=orders,
            subject_id="subject-1",
            chunk_index=0,
        )
        self.assertEqual(sum(item["validation_state"] == "accepted" for item in events), 1)
        rejected = [item for item in events if item["validation_state"] == "rejected"]
        self.assertEqual(len(rejected), 3)
        self.assertEqual(
            {item["rejection_reason"] for item in rejected},
            {"message_quote_mismatch", "unknown_order_line_id", "order_value_mismatch"},
        )

    def test_brand_preference_rejects_factory_and_requires_message_or_sku(self) -> None:
        customer = "customer_" + "1" * 24
        messages = [raw_message(customer, "message-brand", "我更喜欢耐克的外套")]
        orders = {
            "order-real": {
                "order_line_id": "order-real",
                "factory": "某服装厂",
                "sku_name": "耐克蓝色外套",
                "order_note": "老客户",
            }
        }
        payload = {
            "events": [
                {
                    "event_type": "brand_preference",
                    "summary": "把工厂当品牌",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "kind": "order",
                            "order_line_id": "order-real",
                            "field": "factory",
                            "value": "某服装厂",
                        }
                    ],
                },
                {
                    "event_type": "brand_preference",
                    "summary": "订单备注不是品牌来源",
                    "confidence": 0.8,
                    "evidence": [
                        {
                            "kind": "order",
                            "order_line_id": "order-real",
                            "field": "order_note",
                            "value": "老客户",
                        }
                    ],
                },
                {
                    "event_type": "brand_preference",
                    "summary": "聊天明确表达品牌",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "kind": "message",
                            "message_key": "message-brand",
                            "quote": "喜欢耐克",
                        }
                    ],
                },
                {
                    "event_type": "brand_preference",
                    "summary": "SKU 明确包含品牌",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "kind": "order",
                            "order_line_id": "order-real",
                            "field": "sku_name",
                            "value": "耐克蓝色外套",
                        }
                    ],
                },
            ]
        }

        events = validate_extracted_events(
            payload,
            messages=messages,
            orders=orders,
            subject_id="subject-1",
            chunk_index=0,
        )

        self.assertEqual(
            [item["validation_state"] for item in events],
            ["rejected", "rejected", "accepted", "accepted"],
        )
        self.assertEqual(
            [item["rejection_reason"] for item in events[:2]],
            [
                "factory_is_not_brand_evidence",
                "brand_requires_message_or_sku_evidence",
            ],
        )

    def test_invalid_stage_shape_is_schema_error(self) -> None:
        with self.assertRaises(KimiSchemaError):
            validate_extracted_events(
                {"events": "not-a-list"},
                messages=[],
                orders={},
                subject_id="subject-1",
                chunk_index=0,
            )


class SalesProfileGenerationTests(unittest.TestCase):
    def _database(self, root: Path) -> tuple[Path, tuple[str, str]]:
        created = initialize_m0_run(
            runs_dir=root / ".wechat-cs" / "runs",
            secret=SECRET,
            project_root=root,
            run_id=SOURCE_RUN_ID,
        )
        db_path = Path(created["db"])
        customers = ("customer_" + "1" * 24, "customer_" + "2" * 24)
        connection = open_store(str(db_path))
        try:
            with connection:
                for snapshot_id, kind, sha in (
                    ("events-snapshot", "live-inbox-events", "e" * 64),
                    ("orders-source", "orders_live", "o" * 64),
                ):
                    connection.execute(
                        """
                        INSERT INTO source_snapshots(
                            snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                            mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                            captured_at,consistency_state,quality_json
                        ) VALUES(?,?,?,'opaque',1,1,1,1,?,0,NULL,NULL,?,?,
                                 'consistent','{}')
                        """,
                        (snapshot_id, SOURCE_RUN_ID, kind, sha, AS_OF, AS_OF),
                    )
                connection.execute(
                    "INSERT INTO order_snapshots(order_snapshot_id,source_snapshot_id,synced_at,"
                    "record_count,state,quality_json) VALUES('orders-active','orders-source',?,0,"
                    "'active','{}')",
                    (AS_OF,),
                )
                for index, customer in enumerate(customers, start=1):
                    connection.execute(
                        """
                        INSERT INTO customers(
                            customer_key,display_name,last_active_at,opportunity_score,
                            opportunity_level,summary,reasons_json,evidence_json,memory_json,
                            source_file
                        ) VALUES(?, 'fixture', ?, 0, 'low', 'fixture', '[]', '[]', '{}',
                                 'events.jsonl')
                        """,
                        (customer, AS_OF),
                    )
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_key,customer_key,role,timestamp,text,source_file,source_ordinal
                        ) VALUES(?,?,'customer','2026-07-01T10:00:00+08:00','[已脱敏]',
                                 'events.jsonl',1)
                        """,
                        (f"message-{index}", customer),
                    )
                connection.execute(
                    """
                    INSERT INTO sales_profile_runs(
                        sales_profile_run_id,source_run_id,as_of_at,status,model,prompt_version,
                        profile_schema_version,sampling_version,message_snapshot_id,
                        order_snapshot_id,cohort_hash,created_at
                    ) VALUES(?,?,?,'prepared','kimi-k2.6','events+profile','card-v1',?,
                             'events-snapshot','orders-active','cohort',?)
                    """,
                    (PILOT_RUN_ID, SOURCE_RUN_ID, AS_OF, SAMPLING_VERSION, AS_OF),
                )
                for index, customer in enumerate(customers, start=1):
                    subject = f"subject-{index}"
                    profile = f"sales-profile-{index}"
                    connection.execute(
                        """
                        INSERT INTO sales_profile_subjects(
                            subject_id,sales_profile_run_id,customer_key,profile_id,phone_hmac,
                            stratum,stratum_rank,selection_reason_json,status,created_at,updated_at
                        ) VALUES(?,?,?,'aolai1',?,'future_return_wait',?,'{}','prepared',?,?)
                        """,
                        (
                            subject,
                            PILOT_RUN_ID,
                            customer,
                            "phone_" + str(index) * 24,
                            index,
                            AS_OF,
                            AS_OF,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO sales_profiles(
                            sales_profile_id,subject_id,status,model,prompt_version,
                            profile_schema_version,created_at,updated_at
                        ) VALUES(?,?,'pending','kimi-k2.6','profile-v1','card-v1',?,?)
                        """,
                        (profile, subject, AS_OF, AS_OF),
                    )
        finally:
            connection.close()
        return db_path, customers

    @staticmethod
    def _raw_snapshot(customer_keys, *, failing_customer=None):
        rows = {}
        for index, customer in enumerate(customer_keys, start=1):
            text = "FAIL 客户" if customer == failing_customer else "晚点我再来看看"
            rows[customer] = (raw_message(customer, f"message-{index}", text),)
        return RawConversationSnapshot(
            source_hash="e" * 64,
            messages_by_customer=rows,
            missing_customer_keys=(),
            scanned_record_count=len(rows),
        )

    def test_failure_is_isolated_resume_only_retries_failure_and_success_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path, customers = self._database(Path(temp_dir).resolve())
            first_client = FakeKimiClient(fail_text="FAIL")
            first_snapshot = self._raw_snapshot(customers, failing_customer=customers[1])
            with patch(
                "wechat_cs.sales_profile_generation.load_raw_sales_conversations",
                return_value=first_snapshot,
            ):
                first = run_sales_profile_pilot(
                    db_path,
                    events_path=Path("events.jsonl"),
                    accounts_path=Path("accounts.json"),
                    sales_profile_run_id=PILOT_RUN_ID,
                    secret=SECRET,
                    client=first_client,
                    concurrency=2,
                )
            self.assertEqual(first["succeeded"], 1)
            self.assertEqual(first["failed"], 1)
            self.assertEqual(first["status"], "partial")
            self.assertEqual(sorted(call[2] for call in first_client.calls), [0.0, 0.0, 0.2])

            second_client = FakeKimiClient()
            with patch(
                "wechat_cs.sales_profile_generation.load_raw_sales_conversations",
                return_value=self._raw_snapshot(customers),
            ):
                second = run_sales_profile_pilot(
                    db_path,
                    events_path=Path("events.jsonl"),
                    accounts_path=Path("accounts.json"),
                    sales_profile_run_id=PILOT_RUN_ID,
                    secret=SECRET,
                    client=second_client,
                    resume=True,
                    concurrency=2,
                )
            self.assertEqual(second["succeeded"], 2)
            self.assertEqual(second["failed"], 0)
            self.assertEqual(second["status"], "complete")
            self.assertEqual(len(second_client.calls), 2)

            third_client = FakeKimiClient()
            with patch(
                "wechat_cs.sales_profile_generation.load_raw_sales_conversations",
                return_value=self._raw_snapshot(customers),
            ):
                third = run_sales_profile_pilot(
                    db_path,
                    events_path=Path("events.jsonl"),
                    accounts_path=Path("accounts.json"),
                    sales_profile_run_id=PILOT_RUN_ID,
                    secret=SECRET,
                    client=third_client,
                )
            self.assertEqual(third["processed"], 0)
            self.assertEqual(third_client.calls, [])

            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sales_profiles WHERE status='succeeded'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sales_profile_events WHERE validation_state='accepted'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sales_profiles WHERE card_version IS NOT NULL"
                    ).fetchone()[0],
                    2,
                )
                stored_counts = json.loads(
                    connection.execute(
                        "SELECT counts_json FROM sales_profile_runs WHERE sales_profile_run_id=?",
                        (PILOT_RUN_ID,),
                    ).fetchone()[0]
                )
                self.assertEqual(stored_counts["generation_statuses"], {"succeeded": 2})
            finally:
                connection.close()

    def test_raw_message_key_mismatch_fails_only_that_customer_before_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path, customers = self._database(Path(temp_dir).resolve())
            snapshot = self._raw_snapshot(customers)
            rows = dict(snapshot.messages_by_customer)
            rows[customers[1]] = (raw_message(customers[1], "message-unfrozen", "晚点再看"),)
            mismatched = RawConversationSnapshot(
                source_hash=snapshot.source_hash,
                messages_by_customer=rows,
                missing_customer_keys=(),
                scanned_record_count=2,
            )
            client = FakeKimiClient()
            with patch(
                "wechat_cs.sales_profile_generation.load_raw_sales_conversations",
                return_value=mismatched,
            ):
                result = run_sales_profile_pilot(
                    db_path,
                    events_path=Path("events.jsonl"),
                    accounts_path=Path("accounts.json"),
                    sales_profile_run_id=PILOT_RUN_ID,
                    secret=SECRET,
                    client=client,
                )
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(len(client.calls), 2)
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT error_code FROM sales_profiles WHERE sales_profile_id='sales-profile-2'"
                    ).fetchone()[0],
                    "raw_conversation_mismatch",
                )
            finally:
                connection.close()

    def test_facts_use_frozen_order_and_member_snapshots_and_hide_future_refund(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path, customers = self._database(Path(temp_dir).resolve())
            connection = open_store(str(db_path))
            try:
                subject = connection.execute(
                    "SELECT * FROM sales_profile_subjects WHERE customer_key=?",
                    (customers[0],),
                ).fetchone()
                phone_hmac = str(subject["phone_hmac"])
                with connection:
                    connection.execute(
                        """
                        INSERT INTO orders(
                            order_line_id,order_snapshot_id,source_namespace,record_id,
                            phone_hmac,ordered_at,paid_at,paid_on,revenue_minor,currency,
                            refund_type,refund_amount_minor,refund_on,source_hash,
                            quality_flags_json
                        ) VALUES(
                            'order-frozen','orders-active','fixture','record-frozen',?,
                            '2026-06-01T00:00:00+08:00','2026-06-01T00:00:00+08:00',
                            '2026-06-01',10000,'CNY','return',10000,'2026-08-01',
                            'frozen-source','["future_refund_on"]'
                        )
                        """,
                        (phone_hmac,),
                    )
                    for snapshot_id, kind, sha, captured in (
                        ("members-frozen", "birthday_members", "m" * 64, AS_OF),
                        (
                            "orders-later-source",
                            "orders_live",
                            "l" * 64,
                            "2026-07-14T10:00:00+08:00",
                        ),
                        (
                            "members-later",
                            "birthday_members",
                            "n" * 64,
                            "2026-07-14T10:00:00+08:00",
                        ),
                    ):
                        connection.execute(
                            """
                            INSERT INTO source_snapshots(
                                snapshot_id,run_id,source_kind,source_path_hash,device,inode,
                                size,mtime_ns,sha256,record_count,observed_until,captured_at,
                                consistency_state,quality_json
                            ) VALUES(?,?,?,'opaque',1,1,1,1,?,1,?,?,'consistent','{}')
                            """,
                            (
                                snapshot_id,
                                SOURCE_RUN_ID,
                                kind,
                                sha,
                                captured,
                                captured,
                            ),
                        )
                    connection.execute(
                        "UPDATE order_snapshots SET state='superseded' "
                        "WHERE order_snapshot_id='orders-active'"
                    )
                    connection.execute(
                        """
                        INSERT INTO order_snapshots(
                            order_snapshot_id,source_snapshot_id,synced_at,record_count,
                            state,quality_json
                        ) VALUES('orders-later','orders-later-source',
                                 '2026-07-14T10:00:00+08:00',1,'active','{}')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO orders(
                            order_line_id,order_snapshot_id,source_namespace,record_id,
                            phone_hmac,paid_at,paid_on,revenue_minor,currency,source_hash,
                            quality_flags_json
                        ) VALUES('order-later','orders-later','fixture','record-later',?,
                                 '2026-06-02T12:00:00+08:00','2026-06-02',20000,'CNY',
                                 'later-source','[]')
                        """,
                        (phone_hmac,),
                    )
                    for fact_id, snapshot_id, birthday in (
                        ("fact-frozen", "members-frozen", "1990-01-01"),
                        ("fact-later", "members-later", "2000-02-02"),
                    ):
                        connection.execute(
                            """
                            INSERT INTO customer_aux_facts(
                                aux_fact_id,source_snapshot_id,source_namespace,
                                source_record_id,customer_key,profile_id,phone_hmac,
                                member_birthday,source_hash,created_at
                            ) VALUES(?,?, 'fixture',?,?, 'aolai1',?,?,?,?)
                            """,
                            (
                                fact_id,
                                snapshot_id,
                                "record-" + fact_id,
                                customers[0],
                                phone_hmac,
                                birthday,
                                "source-" + fact_id,
                                AS_OF,
                            ),
                        )

                facts, orders = _load_deterministic_facts(
                    connection,
                    subject,
                    as_of_at=AS_OF,
                    order_snapshot_id="orders-active",
                    aux_snapshot_id="members-frozen",
                )
            finally:
                connection.close()

            self.assertEqual(set(orders), {"order-frozen"})
            self.assertIsNone(orders["order-frozen"]["refund_type"])
            self.assertIsNone(orders["order-frozen"]["refund_on"])
            self.assertFalse(orders["order-frozen"]["refund_fact_at_cutoff"])
            self.assertNotIn("future_refund_on", orders["order-frozen"]["quality_flags"])
            self.assertFalse(
                any(
                    flag.startswith("future_")
                    for flag in orders["order-frozen"]["quality_flags"]
                )
            )
            self.assertEqual(
                facts["member_facts"],
                [
                    {
                        "member_birthday": "1990-01-01",
                        "preferred_style": None,
                        "expected_gift": None,
                        "member_shop": None,
                    }
                ],
            )

    def test_missing_api_key_stops_before_run_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path, _customers = self._database(Path(temp_dir).resolve())
            with patch.dict(os.environ, {"KIMI_API_KEY": ""}):
                with self.assertRaisesRegex(RuntimeError, "KIMI_API_KEY"):
                    run_sales_profile_pilot(
                        db_path,
                        events_path=Path("events.jsonl"),
                        accounts_path=Path("accounts.json"),
                        sales_profile_run_id=PILOT_RUN_ID,
                        secret=SECRET,
                    )
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM sales_profile_runs WHERE sales_profile_run_id=?",
                        (PILOT_RUN_ID,),
                    ).fetchone()[0],
                    "prepared",
                )
            finally:
                connection.close()

    def test_obsolete_duplicate_person_cohort_is_never_generated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path, _customers = self._database(Path(temp_dir).resolve())
            connection = open_store(str(db_path))
            try:
                with connection:
                    connection.execute(
                        "UPDATE sales_profile_runs SET sampling_version='sales-profile-sampling-v1' "
                        "WHERE sales_profile_run_id=?",
                        (PILOT_RUN_ID,),
                    )
            finally:
                connection.close()
            client = FakeKimiClient()
            with self.assertRaisesRegex(RuntimeError, "obsolete sampling"):
                run_sales_profile_pilot(
                    db_path,
                    events_path=Path("events.jsonl"),
                    accounts_path=Path("accounts.json"),
                    sales_profile_run_id=PILOT_RUN_ID,
                    secret=SECRET,
                    client=client,
                )
            self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
