from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from wechat_cs.orders import import_orders, normalize_order, parse_synced_at
from wechat_cs.store import initialize_m0_run, open_store


TEST_ROOT = Path(__file__).parent
ORDERS_FIXTURE = TEST_ROOT / "fixtures" / "orders" / "orders_live.json"
SECRET = "orders-fixture-secret-with-at-least-32-characters"


class OrderPrimitiveTests(unittest.TestCase):
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
                    "SELECT paid_on,revenue_minor FROM orders WHERE record_id='order-supplier-only'"
                ).fetchone()
                self.assertIsNone(supplier["paid_on"])
                self.assertIsNone(supplier["revenue_minor"])
            finally:
                connection.close()
            self.assertEqual(ORDERS_FIXTURE.read_bytes(), before)
            serialized = db_path.read_bytes()
            for unsafe in (b"13800138000", "联系人张三".encode("utf-8"), b"YT1111111111"):
                self.assertNotIn(unsafe, serialized)

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


if __name__ == "__main__":
    unittest.main()
