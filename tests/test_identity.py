from __future__ import annotations

import json
import csv
import tempfile
import unittest
from pathlib import Path

from wechat_cs.build import build_live_inbox_database
from wechat_cs.identity import (
    classify_order_eligibility,
    extract_tracking_numbers,
    global_phone_hmac,
    import_bindings,
    import_feishu_order_bindings,
    load_binding_csv,
    normalize_phone,
)
from wechat_cs.store import initialize_m0_run, open_store


TEST_ROOT = Path(__file__).parent
IDENTITY_FIXTURE = TEST_ROOT / "fixtures" / "identity" / "safe_phone_bindings.csv"
FEISHU_ROOT = TEST_ROOT / "fixtures" / "identity_feishu"
LIVE_ROOT = TEST_ROOT / "fixtures" / "live_inbox"
SECRET = "identity-fixture-secret-with-at-least-32-characters"
OTHER_SECRET = "other-identity-secret-with-at-least-32-characters"
REGISTRY = {
    "fixture-a": "wechat-account-fixture-a-v1",
    "fixture-b": "wechat-account-fixture-b-v1",
}


class IdentityPrimitiveTests(unittest.TestCase):
    def test_order_eligibility_requires_explicit_customer_label(self) -> None:
        self.assertEqual(classify_order_eligibility("虚构 下单客户"), "order_customer")
        self.assertEqual(classify_order_eligibility("虚构 相册客户"), "album_customer")
        self.assertEqual(classify_order_eligibility("虚构普通客户"), "order_ineligible")
        self.assertEqual(extract_tracking_numbers("单号 SF1234567890"), ["SF1234567890"])
        self.assertEqual(extract_tracking_numbers("手机号 13800138000"), [])

    def test_account_scoped_raw_id_and_global_phone_hmac(self) -> None:
        loaded = load_binding_csv(IDENTITY_FIXTURE, registry=REGISTRY, secret=SECRET)
        first = loaded[("wechat-account-fixture-a-v1", "customer-raw-a")]
        second = loaded[("wechat-account-fixture-b-v1", "customer-raw-b")]
        self.assertEqual(first.state, "approved")
        self.assertEqual(first.match_method, "account_raw_exact")
        self.assertEqual(first.phone_hmac, second.phone_hmac)
        self.assertNotIn("13800138000", first.phone_hmac)

    def test_low_confidence_conflicts_missing_ids_and_invalid_phones_are_not_approved(self) -> None:
        loaded = load_binding_csv(IDENTITY_FIXTURE, registry=REGISTRY, secret=SECRET)
        conflict = loaded[("wechat-account-fixture-a-v1", "raw-conflict")]
        review = loaded[("wechat-account-fixture-a-v1", "raw-review")]
        self.assertEqual(conflict.state, "conflict")
        self.assertIsNone(conflict.phone_hmac)
        self.assertEqual(review.state, "review")
        self.assertEqual(loaded.stats["unknown_accounts"], 1)
        self.assertEqual(loaded.stats["missing_raw_id"], 1)
        self.assertEqual(loaded.stats["invalid_phones"], 1)
        self.assertEqual(loaded.stats["confidence_counts"], {"0.82": 1, "0.95": 7})

    def test_phone_normalization_is_strict_and_global(self) -> None:
        self.assertEqual(normalize_phone("+86 138-0013-8000"), "13800138000")
        self.assertIsNone(normalize_phone("12345678901"))
        self.assertEqual(
            global_phone_hmac(SECRET, "13800138000"),
            global_phone_hmac(SECRET, "+86 138 0013 8000"),
        )
        with self.assertRaisesRegex(ValueError, "invalid phone"):
            global_phone_hmac(SECRET, "invalid")

    def test_duplicate_lower_confidence_cannot_raise_a_key_to_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mixed-confidence.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["账号", "客户手机号", "微信原始ID", "绑定置信度"],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "账号": "fixture-a",
                            "客户手机号": "13800138000",
                            "微信原始ID": "duplicate-raw",
                            "绑定置信度": "0.95",
                        },
                        {
                            "账号": "fixture-a",
                            "客户手机号": "13800138000",
                            "微信原始ID": "duplicate-raw",
                            "绑定置信度": "0.82",
                        },
                    ]
                )
            loaded = load_binding_csv(path, registry=REGISTRY, secret=SECRET)
            self.assertEqual(
                loaded[("wechat-account-fixture-a-v1", "duplicate-raw")].state,
                "review",
            )


class IdentityImportTests(unittest.TestCase):
    def _working_db(self, root: Path) -> Path:
        created = initialize_m0_run(
            runs_dir=root / ".wechat-cs" / "runs",
            secret=SECRET,
            project_root=root,
            run_id="identity-run",
        )
        db_path = Path(created["db"])
        build_live_inbox_database(
            LIVE_ROOT / "events.jsonl",
            LIVE_ROOT / "state.json",
            LIVE_ROOT / "accounts.json",
            db_path,
            secret=SECRET,
        )
        return db_path

    def test_import_matches_persisted_conversation_refs_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            first = import_bindings(
                db_path=db_path,
                bindings_path=IDENTITY_FIXTURE,
                accounts_path=LIVE_ROOT / "accounts.json",
                secret=SECRET,
            )
            connection = open_store(str(db_path))
            try:
                with connection:
                    connection.execute(
                        "UPDATE conversation_links SET state='rejected',reviewed_at=? "
                        "WHERE link_id=(SELECT link_id FROM conversation_links ORDER BY link_id LIMIT 1)",
                        ("2026-07-13T14:00:00+08:00",),
                    )
            finally:
                connection.close()
            second = import_bindings(
                db_path=db_path,
                bindings_path=IDENTITY_FIXTURE,
                accounts_path=LIVE_ROOT / "accounts.json",
                secret=SECRET,
            )
            self.assertEqual(first["source_hash"], second["source_hash"])
            self.assertEqual(first["matched_conversations"], 2)
            connection = open_store(str(db_path), read_only=True)
            try:
                rows = list(
                    connection.execute(
                        "SELECT customer_key,phone_hmac,state,match_method FROM conversation_links"
                    )
                )
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["state"] for row in rows}, {"approved", "rejected"})
                self.assertEqual(len({row["phone_hmac"] for row in rows}), 1)
            finally:
                connection.close()
            serialized = db_path.read_bytes()
            for unsafe in (b"13800138000", b"customer-raw-a", b"customer-raw-b"):
                self.assertNotIn(unsafe, serialized)

    def test_import_rejects_a_different_hmac_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._working_db(root)
            with self.assertRaisesRegex(RuntimeError, "HMAC key fingerprint mismatch"):
                import_bindings(
                    db_path=db_path,
                    bindings_path=IDENTITY_FIXTURE,
                    accounts_path=LIVE_ROOT / "accounts.json",
                    secret=OTHER_SECRET,
                )

    def test_feishu_bridge_applies_label_gate_and_persists_hmac_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            created = initialize_m0_run(
                runs_dir=root / ".wechat-cs" / "runs",
                secret=SECRET,
                project_root=root,
                run_id="feishu-identity-run",
            )
            db_path = Path(created["db"])
            build_live_inbox_database(
                FEISHU_ROOT / "events.jsonl",
                FEISHU_ROOT / "state.json",
                FEISHU_ROOT / "accounts.json",
                db_path,
                secret=SECRET,
            )
            sources = [FEISHU_ROOT / "orders_full.json", FEISHU_ROOT / "orders_realtime.json"]
            before = {path: path.read_bytes() for path in [FEISHU_ROOT / "events.jsonl", *sources]}
            first = import_feishu_order_bindings(
                db_path,
                FEISHU_ROOT / "events.jsonl",
                FEISHU_ROOT / "accounts.json",
                sources,
                target_profile_id="aolai4",
                secret=SECRET,
            )
            second = import_feishu_order_bindings(
                db_path,
                FEISHU_ROOT / "events.jsonl",
                FEISHU_ROOT / "accounts.json",
                sources,
                target_profile_id="aolai4",
                secret=SECRET,
            )
            self.assertEqual(first, second)
            self.assertEqual(
                first["eligibility_counts"],
                {"album_customer": 1, "order_customer": 3, "order_ineligible": 1},
            )
            self.assertEqual(first["link_state_counts"], {"approved": 2, "conflict": 1})
            connection = open_store(str(db_path), read_only=True)
            try:
                eligible = list(
                    connection.execute(
                        "SELECT eligibility,COUNT(*) AS count FROM conversation_order_eligibility "
                        "GROUP BY eligibility ORDER BY eligibility"
                    )
                )
                links = list(
                    connection.execute(
                        "SELECT state,match_method,phone_hmac FROM conversation_links "
                        "WHERE version='identity-feishu-v1' ORDER BY match_method"
                    )
                )
                self.assertEqual(sum(row["count"] for row in eligible), 5)
                self.assertEqual(len(links), 3)
                self.assertEqual(sum(row["phone_hmac"] is not None for row in links), 2)
            finally:
                connection.close()
            serialized = db_path.read_bytes()
            for unsafe in (
                b"13800138000",
                b"SF1234567890",
                "虚构甲".encode("utf-8"),
                b"raw-phone",
            ):
                self.assertNotIn(unsafe, serialized)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
