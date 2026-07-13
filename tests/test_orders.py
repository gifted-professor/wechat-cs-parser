from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from wechat_cs.orders import ORDER_RULE_VERSION, import_orders, normalize_order, parse_synced_at
from wechat_cs.store import initialize_m0_run, open_store


TEST_ROOT = Path(__file__).parent
ORDERS_FIXTURE = TEST_ROOT / "fixtures" / "orders" / "orders_live.json"
SECRET = "orders-fixture-secret-with-at-least-32-characters"


class OrderPrimitiveTests(unittest.TestCase):
    def test_order_and_customer_payment_timestamps_keep_time_and_timezone(self) -> None:
        order = normalize_order(
            {
                "record_id": "timestamp-facts",
                "phone": "13800138000",
                "order_date": "2026-07-01 09:12:34",
                "pay_date": "2026/07/02 21:43:56",
                # This is the supplier remittance date and must never replace
                # the customer's payment timestamp.
                "pay_date_actual": "2026/07/03 08:00:00",
                "revenue": 299,
            },
            synced_at=parse_synced_at("2026-07-13T12:00:00+08:00"),
            secret=SECRET,
            source_hash="fixture-source-hash",
        )
        self.assertEqual(order.ordered_at, "2026-07-01T09:12:34+08:00")
        self.assertEqual(order.paid_at, "2026-07-02T21:43:56+08:00")
        self.assertEqual(order.paid_on, "2026-07-02")
        self.assertEqual(ORDER_RULE_VERSION, "m0-order-v3")

    def test_future_order_and_payment_timestamps_are_not_admitted(self) -> None:
        order = normalize_order(
            {
                "record_id": "future-timestamps",
                "phone": "13800138000",
                "order_date": "2026-07-14 09:00:00",
                "pay_date": "2026-07-14 09:05:00",
                "revenue": 299,
            },
            synced_at=parse_synced_at("2026-07-13T12:00:00+08:00"),
            secret=SECRET,
            source_hash="fixture-source-hash",
        )
        self.assertIsNone(order.ordered_at)
        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.paid_on)
        self.assertIsNone(order.revenue_minor)
        self.assertIn("future_ordered_at", order.quality_flags)
        self.assertIn("future_paid_at", order.quality_flags)

    def test_order_note_is_bounded_redacted_and_uses_supported_note_fields(self) -> None:
        order = normalize_order(
            {
                "record_id": "order-note",
                "phone": "13800138000",
                "pay_date": "2026-07-01 10:00:00",
                "revenue": 299,
                "remark": "客户 13800138000 等七夕活动再拍",
                "goods_remark": "换成蓝色 " + ("很长" * 300),
            },
            synced_at=parse_synced_at("2026-07-13T12:00:00+08:00"),
            secret=SECRET,
            source_hash="fixture-source-hash",
        )
        self.assertIn("七夕活动", order.order_note or "")
        self.assertIn("换成蓝色", order.order_note or "")
        self.assertNotIn("13800138000", order.order_note or "")
        self.assertLessEqual(len(order.order_note or ""), 500)

    def test_product_candidate_fields_are_normalized_without_customer_pii(self) -> None:
        order = normalize_order(
            {
                "record_id": "product-facts",
                "phone": "13800138000",
                "pay_date": "2026-07-01",
                "revenue": 299,
                "sku_name": "羊毛开衫",
                "factory": "样衣工厂",
                "category": "针织衫",
                "color": "雾霾蓝",
                "size": "M",
            },
            synced_at=parse_synced_at("2026-07-13T12:00:00+08:00"),
            secret=SECRET,
            source_hash="fixture-source-hash",
        )
        self.assertEqual(order.sku_name, "羊毛开衫")
        self.assertEqual(order.factory, "样衣工厂")
        self.assertEqual(order.category, "针织衫")
        self.assertEqual(order.color, "雾霾蓝")
        self.assertEqual(order.size, "M")
        serialized = json.dumps(order.__dict__, ensure_ascii=False)
        self.assertNotIn("13800138000", serialized)

    def test_supplier_payment_never_creates_customer_purchase(self) -> None:
        order = normalize_order(
            {
                "record_id": "supplier-only",
                "pay_date": "",
                "revenue": None,
                "pay_amount": 199,
                "pay_date_actual": "2026-07-01",
                "is_paid": True,
            },
            synced_at=parse_synced_at("2026-07-13T12:00:00+08:00"),
            secret=SECRET,
            source_hash="fixture-source-hash",
        )
        self.assertIsNone(order.paid_on)
        self.assertIsNone(order.revenue_minor)

    def test_refund_types_money_and_quality_flags_are_normalized(self) -> None:
        document = json.loads(ORDERS_FIXTURE.read_text(encoding="utf-8"))
        synced_at = parse_synced_at(document["synced_at"])
        orders = {
            row["record_id"]: normalize_order(
                row,
                synced_at=synced_at,
                secret=SECRET,
                source_hash="fixture-source-hash",
            )
            for row in document["records"]
        }
        self.assertEqual(orders["order-normal"].revenue_minor, 19900)
        self.assertEqual(orders["order-full-return"].refund_type, "return")
        self.assertEqual(orders["order-return-taro"].refund_type, "return_taro")
        self.assertEqual(orders["order-exchange"].refund_type, "exchange")
        self.assertEqual(orders["order-compensation"].refund_type, "compensation")
        self.assertEqual(orders["order-other"].refund_type, "other")
        self.assertNotIn("missing_refund_on", orders["order-exchange"].quality_flags)
        self.assertNotIn("missing_refund_amount", orders["order-compensation"].quality_flags)
        anomaly = orders["order-anomaly"]
        self.assertIsNone(anomaly.paid_on)
        self.assertIn("invalid_paid_on", anomaly.quality_flags)
        self.assertIn("future_refund_on", anomaly.quality_flags)
        self.assertIn("aftersale_open", anomaly.quality_flags)


class OrderImportTests(unittest.TestCase):
    def _working_db(self, root: Path) -> Path:
        created = initialize_m0_run(
            runs_dir=root / ".wechat-cs" / "runs",
            secret=SECRET,
            project_root=root,
            run_id="orders-run",
        )
        return Path(created["db"])

    def test_import_is_read_only_idempotent_and_keeps_duplicate_tracking_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            before = ORDERS_FIXTURE.read_bytes()
            first = import_orders(db_path, ORDERS_FIXTURE, secret=SECRET)
            second = import_orders(db_path, ORDERS_FIXTURE, secret=SECRET)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["order_snapshot_id"], second["order_snapshot_id"])
            self.assertEqual(first["quality"]["accepted_records"], 11)
            self.assertEqual(first["quality"]["quarantined_records"], 0)
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 11)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM orders WHERE record_id IN ('order-normal','order-duplicate-tracking')"
                    ).fetchone()[0],
                    2,
                )
                supplier = connection.execute(
                    "SELECT paid_on,paid_at,revenue_minor FROM orders "
                    "WHERE record_id='order-supplier-only'"
                ).fetchone()
                self.assertIsNone(supplier["paid_on"])
                self.assertIsNone(supplier["paid_at"])
                self.assertIsNone(supplier["revenue_minor"])
            finally:
                connection.close()
            self.assertEqual(ORDERS_FIXTURE.read_bytes(), before)
            serialized = db_path.read_bytes()
            for unsafe in (b"13800138000", "联系人张三".encode("utf-8"), b"YT1111111111"):
                self.assertNotIn(unsafe, serialized)

    def test_import_persists_product_candidates_as_local_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            document = json.loads(ORDERS_FIXTURE.read_text(encoding="utf-8"))
            document["records"][0].update(
                {
                    "sku_name": "羊毛开衫",
                    "factory": "样衣工厂",
                    "category": "针织衫",
                    "color": "雾霾蓝",
                    "size": "M",
                }
            )
            source = root / "orders-with-product.json"
            source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            import_orders(db_path, source, secret=SECRET)
            connection = open_store(str(db_path), read_only=True)
            try:
                row = connection.execute(
                    "SELECT sku_name,factory,category,color,size FROM orders "
                    "WHERE record_id='order-normal'"
                ).fetchone()
                self.assertEqual(tuple(row), ("羊毛开衫", "样衣工厂", "针织衫", "雾霾蓝", "M"))
            finally:
                connection.close()

    def test_import_persists_exact_timestamps_and_order_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            document = json.loads(ORDERS_FIXTURE.read_text(encoding="utf-8"))
            document["records"][0].update(
                {
                    "order_date": "2026-07-01 09:12:34",
                    "pay_date": "2026-07-01 21:43:56",
                    "remark": "等周年活动再联系",
                }
            )
            source = root / "orders-with-timestamps.json"
            source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            import_orders(db_path, source, secret=SECRET)
            connection = open_store(str(db_path), read_only=True)
            try:
                row = connection.execute(
                    "SELECT ordered_at,paid_at,paid_on,order_note FROM orders "
                    "WHERE record_id='order-normal'"
                ).fetchone()
                self.assertEqual(row["ordered_at"], "2026-07-01T09:12:34+08:00")
                self.assertEqual(row["paid_at"], "2026-07-01T21:43:56+08:00")
                self.assertEqual(row["paid_on"], "2026-07-01")
                self.assertEqual(row["order_note"], "等周年活动再联系")
            finally:
                connection.close()

    def test_failed_import_preserves_previous_active_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            imported = import_orders(db_path, ORDERS_FIXTURE, secret=SECRET)
            bad = root / "bad-orders.json"
            bad.write_text('{"synced_at":"bad","records":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "synced_at"):
                import_orders(db_path, bad, secret=SECRET)
            connection = open_store(str(db_path), read_only=True)
            try:
                active = connection.execute(
                    "SELECT order_snapshot_id,state FROM order_snapshots WHERE state='active'"
                ).fetchone()
                self.assertEqual(active["order_snapshot_id"], imported["order_snapshot_id"])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 11)
            finally:
                connection.close()

    def test_new_import_versions_instead_of_rewriting_the_prior_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            first = import_orders(db_path, ORDERS_FIXTURE, secret=SECRET)
            connection = open_store(str(db_path))
            try:
                source_snapshot_id = connection.execute(
                    "SELECT source_snapshot_id FROM order_snapshots "
                    "WHERE order_snapshot_id=?",
                    (first["order_snapshot_id"],),
                ).fetchone()[0]
                with connection:
                    connection.execute(
                        """
                        INSERT INTO customers(
                            customer_key,display_name,last_active_at,opportunity_score,
                            opportunity_level,summary,reasons_json,evidence_json,
                            memory_json,source_file
                        ) VALUES('customer-order-history','fixture',
                                 '2026-07-01T10:00:00+08:00',0,'low','fixture',
                                 '[]','[]','{}','fixture')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO decision_cards(
                            card_id,customer_key,episode_id,card_type,as_of_at,
                            boundary_ordinal,source_snapshot_id,action_window_end,
                            blind_context_json,observed_action_json,
                            context_message_keys_json,action_message_keys_json,
                            split,rule_version,created_at
                        ) VALUES(
                            'card-order-history','customer-order-history','episode-1',
                            'proactive','2026-07-01T10:00:00+08:00',1,?,
                            '2026-07-02T10:00:00+08:00','{}','{}','[]','[]',
                            'test','fixture','2026-07-01T10:00:00+08:00'
                        )
                        """,
                        (source_snapshot_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO card_outcomes(
                            card_id,paid_1d,attribution_state,
                            attribution_flags_json,matched_orders_json,computed_at
                        ) VALUES('card-order-history',1,'associated','[]','[]',
                                 '2026-07-13T12:00:00+08:00')
                        """
                    )
            finally:
                connection.close()
            document = json.loads(ORDERS_FIXTURE.read_text(encoding="utf-8"))
            document["synced_at"] = "2026-07-13T13:00:00+08:00"
            document["records"][0]["revenue"] = 209
            changed = root / "orders-next.json"
            changed.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            second = import_orders(db_path, changed, secret=SECRET)

            self.assertNotEqual(first["order_snapshot_id"], second["order_snapshot_id"])
            connection = open_store(str(db_path), read_only=True)
            try:
                states = {
                    row["order_snapshot_id"]: row["state"]
                    for row in connection.execute(
                        "SELECT order_snapshot_id,state FROM order_snapshots"
                    )
                }
                self.assertEqual(states[first["order_snapshot_id"]], "superseded")
                self.assertEqual(states[second["order_snapshot_id"]], "active")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                    22,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT paid_1d FROM card_outcomes "
                        "WHERE card_id='card-order-history'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
