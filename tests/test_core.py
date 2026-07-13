from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from wechat_cs.build import build_database, export_chatml, load_messages
from wechat_cs.core import (
    Message,
    PairCandidate,
    analyze_customer,
    extract_mainland_phones,
    hmac_id,
    merge_turns,
    pair_turns,
    redact_text,
    select_candidates,
    stable_split,
)
from wechat_cs.knowledge import KnowledgeRegistry
from wechat_cs.store import get_health, open_store


FIXTURE_EXPORT = Path(__file__).parent / "fixtures" / "export"
BINDING_FIXTURE_EXPORT = Path(__file__).parent / "fixtures" / "bindings"
PROJECT_ROOT = Path(__file__).parents[1]
SECRET = "unit-test-secret-that-is-not-used-outside-tests"


def message(
    key: str,
    customer: str,
    role: str,
    timestamp: str,
    text: str,
    ordinal: int,
) -> Message:
    return Message(
        message_key=key,
        customer_key=customer,
        role=role,
        timestamp=timestamp,
        text=text,
        source_file="conversations/synthetic.jsonl",
        source_ordinal=ordinal,
    )


def candidate(customer: str, timestamp: str, quality: int = 10) -> PairCandidate:
    return PairCandidate(
        customer_key=customer,
        trigger_text="虚构客户问题" + customer,
        reply_text="虚构工作室回复" + customer,
        context=[],
        trigger_keys=["trigger_" + customer],
        reply_keys=["reply_" + customer],
        timestamp=timestamp,
        intent_stage="general",
        risk_flags=[],
        risk_level="low",
        quality_score=quality,
    )


class CorePrimitiveTests(unittest.TestCase):
    def test_hmac_id_is_stable_scoped_and_opaque(self) -> None:
        first = hmac_id(SECRET, "customer", "account-one", "raw-contact-id")
        self.assertEqual(first, hmac_id(SECRET, "customer", "account-one", "raw-contact-id"))
        self.assertNotEqual(first, hmac_id(SECRET, "customer", "account-two", "raw-contact-id"))
        self.assertTrue(first.startswith("customer_"))
        self.assertNotIn("raw-contact-id", first)

    def test_redaction_removes_pii_and_dynamic_values(self) -> None:
        raw = (
            "测试电话13800138000，邮箱tester@example.invalid，金额128元，"
            "日期2026-07-01，链接https://example.invalid/path 单号TEST0000000001。"
        )
        redacted, flags = redact_text(raw)
        for unsafe in ("13800138000", "tester@example.invalid", "128元", "2026-07-01", "TEST0000000001"):
            self.assertNotIn(unsafe, redacted)
        self.assertTrue({"phone", "email", "money", "date", "url"}.issubset(flags))

    def test_phone_extraction_is_unique_and_does_not_accept_arbitrary_digits(self) -> None:
        phones = extract_mainland_phones(
            "虚构号码13800138000重复13800138000，固定编号1234567890，另一个13900139000。"
        )
        self.assertEqual(phones, ["13800138000", "13900139000"])

    def test_merge_and_pair_turns_respect_windows_roles_and_redact(self) -> None:
        rows = [
            message("m1", "customer_a", "customer", "2026-07-01T10:00:00", "这个虚构款式怎么订？", 1),
            message("m2", "customer_a", "customer", "2026-07-01T10:05:00", "测试电话13800138000。", 2),
            message("m3", "customer_a", "studio", "2026-07-01T10:12:00", "您好，我先核对虚构规格。", 3),
            message("m4", "customer_a", "studio", "2026-07-01T10:15:00", "确认后再给您建议。", 4),
            message("m5", "customer_a", "customer", "2026-07-01T11:00:01", "超过窗口的新问题。", 5),
            message("m6", "customer_a", "studio", "2026-07-01T11:31:00", "超过三十分钟不应配对。", 6),
        ]
        turns = merge_turns(rows)
        self.assertEqual([turn.role for turn in turns], ["customer", "studio", "customer", "studio"])
        self.assertEqual(len(turns[0].message_keys), 2)
        pairs = pair_turns(turns)
        self.assertEqual(len(pairs), 1)
        self.assertIn("[手机号]", pairs[0].trigger_text)
        self.assertNotIn("13800138000", pairs[0].trigger_text)
        self.assertEqual(pairs[0].customer_key, "customer_a")

    def test_group_split_never_splits_one_customer(self) -> None:
        values = {stable_split(SECRET, "customer_a") for _ in range(20)}
        self.assertEqual(len(values), 1)
        self.assertIn(values.pop(), {"train", "validation", "test"})

    def test_candidate_selection_round_robins_customers(self) -> None:
        rows = [
            candidate("customer_a", "2026-07-01T10:00:00", 12),
            candidate("customer_a", "2026-07-01T10:01:00", 11),
            candidate("customer_a", "2026-07-01T10:02:00", 10),
            candidate("customer_b", "2026-07-01T10:03:00", 9),
        ]
        selected = select_candidates(rows, limit=2)
        self.assertEqual({item.customer_key for item in selected}, {"customer_a", "customer_b"})

    def test_reply_template_cannot_cross_customer_splits(self) -> None:
        customers_by_split = {}
        index = 0
        while len(customers_by_split) < 3 and index < 10000:
            customer_key = "synthetic_customer_%d" % index
            customers_by_split.setdefault(stable_split(SECRET, customer_key), customer_key)
            index += 1
        self.assertEqual(set(customers_by_split), {"train", "validation", "test"})
        rows = []
        for offset, customer_key in enumerate(customers_by_split.values()):
            item = candidate(customer_key, "2026-07-01T10:%02d:00" % offset, 12 - offset)
            item.trigger_text = "不同的虚构问题_%s" % customer_key
            item.reply_text = "相同的虚构回复模板"
            rows.append(item)
        selected = select_candidates(rows, limit=10, secret=SECRET)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len({stable_split(SECRET, item.customer_key) for item in selected}), 1)

    def test_aftersales_is_separate_and_only_customer_closure_closes_it(self) -> None:
        incident = message(
            "m1", "customer_a", "customer", "2026-07-01T10:00:00", "虚构商品破损，我要退款投诉。", 1
        )
        studio_promise = message(
            "m2", "customer_a", "studio", "2026-07-01T10:05:00", "我会为您处理。", 2
        )
        open_case = analyze_customer([incident, studio_promise], datetime.fromisoformat("2026-07-02T00:00:00"))
        self.assertEqual(open_case["aftersales_priority"], "P0")
        self.assertEqual(open_case["queue"], "aftersales")

        closure = message(
            "m3", "customer_a", "customer", "2026-07-01T11:00:00", "问题解决了，谢谢处理。", 3
        )
        closed_case = analyze_customer(
            [incident, studio_promise, closure], datetime.fromisoformat("2026-07-02T00:00:00")
        )
        self.assertIsNone(closed_case["aftersales_priority"])
        self.assertEqual(closed_case["queue"], "presales")


class LocalBuildTests(unittest.TestCase):
    def test_loader_strictly_filters_to_friend_plaintext_status_two_or_three(self) -> None:
        by_customer, statuses, first_at, last_at = load_messages(
            FIXTURE_EXPORT, SECRET, "synthetic-account"
        )
        self.assertEqual(len(by_customer), 2)
        self.assertEqual(sum(map(len, by_customer.values())), 7)
        self.assertEqual(set(statuses.values()), {2, 3})
        self.assertEqual(sum(status == 2 for status in statuses.values()), 3)
        self.assertEqual(sum(status == 3 for status in statuses.values()), 4)
        self.assertEqual(first_at, datetime.fromisoformat("2026-07-01T10:00:00"))
        self.assertEqual(last_at, datetime.fromisoformat("2026-07-02T09:10:00"))
        self.assertTrue(all(item.role in {"studio", "customer"} for rows in by_customer.values() for item in rows))

    def test_database_contains_only_opaque_customer_ids_and_expected_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "analysis.sqlite3"
            build_database(
                str(FIXTURE_EXPORT),
                str(db_path),
                limit_pairs=500,
                account_id="synthetic-account",
                secret=SECRET,
            )
            connection = open_store(str(db_path), read_only=True)
            try:
                health = get_health(connection)
                self.assertEqual(health["counts"]["customers"], 2)
                self.assertEqual(health["counts"]["messages"], 7)
                self.assertGreaterEqual(health["counts"]["style_pairs"], 1)
                rows = list(connection.execute("SELECT customer_key,display_name FROM customers"))
                self.assertTrue(all(row["customer_key"].startswith("customer_") for row in rows))
                serialized = json.dumps([dict(row) for row in rows], ensure_ascii=False)
                self.assertNotIn("synthetic_friend", serialized)
                self.assertNotIn("13800138000", serialized)
            finally:
                connection.close()

    def test_chatml_export_is_blocked_before_role_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "analysis.sqlite3"
            output_path = Path(temp_dir) / "style.jsonl"
            build_database(
                str(FIXTURE_EXPORT),
                str(db_path),
                account_id="synthetic-account",
                secret=SECRET,
            )
            with self.assertRaisesRegex(RuntimeError, "role calibration"):
                export_chatml(str(db_path), str(output_path))
            self.assertFalse(output_path.exists())

    def test_phone_bindings_are_hmac_only_and_shared_numbers_need_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bindings.sqlite3"
            health = build_database(
                str(BINDING_FIXTURE_EXPORT),
                str(db_path),
                account_id="synthetic-bindings",
                secret=SECRET,
            )
            self.assertEqual(health["counts"]["identity_binding_customers_with_candidates"], 3)
            self.assertEqual(health["identity_binding_states"], {"ambiguous_shared": 2, "candidate_unique": 1})
            connection = sqlite3.connect(str(db_path))
            try:
                rows = list(
                    connection.execute(
                        "SELECT phone_hmac,masked_hint,state FROM identity_bindings ORDER BY state,masked_hint"
                    )
                )
            finally:
                connection.close()
        serialized = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("13900139000", serialized)
        self.assertTrue(all(str(row[0]).startswith("phone_") for row in rows))
        self.assertTrue(all(len(str(row[1])) == 11 and "******" in str(row[1]) for row in rows))

    def test_short_hmac_secret_keeps_service_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "weak-secret.sqlite3"
            health = build_database(
                str(FIXTURE_EXPORT),
                str(db_path),
                account_id="synthetic-account",
                secret="too-short",
            )
        self.assertTrue(health["weak_hmac_secret"])
        self.assertIn("weak_hmac_secret", health["warnings"])
        self.assertEqual(health["status"], "degraded")

    def test_knowledge_registry_reports_ready_missing_and_expired_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "policy.json"
            cache.write_text(
                json.dumps(
                    [
                        {"title": "虚构售后规则", "content": "虚构破损场景需人工核实", "source_ref": "doc:test"},
                        {
                            "title": "过期虚构规则",
                            "content": "不应被检索",
                            "expires_at": "2000-01-01T00:00:00Z",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            registry_path = root / "sources.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sources": [
                            {
                                "id": "synthetic-policy",
                                "type": "doc",
                                "title": "虚构规则",
                                "url": "https://example.invalid/synthetic",
                                "enabled": True,
                                "cache_file": "policy.json",
                                "max_age_seconds": 3600,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            registry = KnowledgeRegistry(registry_path, workspace_root=root)
            result = registry.search("破损核实", now=cache.stat().st_mtime + 1)
            self.assertFalse(result["grounding_missing"])
            self.assertEqual(len(result["hits"]), 1)
            self.assertEqual(result["hits"][0]["source_id"], "synthetic-policy")
            self.assertNotIn("过期", result["hits"][0]["title"])

            cache.unlink()
            missing = registry.search("破损核实", now=cache.stat().st_mtime if cache.exists() else None)
            self.assertTrue(missing["grounding_missing"])
            self.assertEqual(missing["sources"][0]["state"], "missing_cache")


@unittest.skipUnless(
    (PROJECT_ROOT / "summary.json").is_file()
    and (PROJECT_ROOT / "messages.jsonl").is_file()
    and (PROJECT_ROOT / "conversation_index.json").is_file(),
    "real local export is not present",
)
class RealExportContractTests(unittest.TestCase):
    """Safe aggregate contract for the current local export; never prints chat text."""

    def test_current_export_aggregates_and_strict_role_filter(self) -> None:
        summary = json.loads((PROJECT_ROOT / "summary.json").read_text(encoding="utf-8"))
        index = json.loads((PROJECT_ROOT / "conversation_index.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["conversation_files"], 392)
        self.assertEqual(summary["messages"], 49770)
        self.assertEqual(len(index), 392)
        self.assertEqual(sum(int(row["message_count"]) for row in index), 49770)

        friend_rows = [row for row in index if row["conversation_type"] == "friend"]
        group_rows = [row for row in index if row["conversation_type"] == "group"]
        self.assertEqual(len(friend_rows), 347)
        self.assertEqual(sum(int(row["message_count"]) for row in friend_rows), 8445)
        self.assertEqual(len([row for row in friend_rows if int(row["message_count"]) > 0]), 303)
        self.assertEqual(len(group_rows), 33)

        by_customer, statuses, _, _ = load_messages(
            PROJECT_ROOT, "real-export-contract-secret", "current-local-export"
        )
        self.assertEqual(sum(map(len, by_customer.values())), 6262)
        self.assertEqual(len(by_customer), 259)
        self.assertEqual(sum(value == 2 for value in statuses.values()), 3027)
        self.assertEqual(sum(value == 3 for value in statuses.values()), 3235)
        self.assertEqual(set(statuses.values()), {2, 3})

        allowed_friend_files = {str(row["file"]) for row in friend_rows}
        forbidden_group_files = {str(row["file"]) for row in group_rows}
        loaded_files = {
            item.source_file for messages in by_customer.values() for item in messages
        }
        self.assertTrue(loaded_files.issubset(allowed_friend_files))
        self.assertTrue(loaded_files.isdisjoint(forbidden_group_files))

    def test_selected_style_text_has_no_raw_common_pii_or_dynamic_values(self) -> None:
        patterns = [
            re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
            re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
            re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
            re.compile(r"https?://\S+", re.IGNORECASE),
            re.compile(r"(?:[¥￥]\s*\d|\d+(?:\.\d+)?\s*(?:元|块))"),
            re.compile(r"(?<!\d)(?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}(?:日|号)?(?!\d)"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "real-contract.sqlite3"
            build_database(
                str(PROJECT_ROOT),
                str(db_path),
                limit_pairs=500,
                account_id="current-local-export",
                secret="real-export-contract-secret",
            )
            connection = sqlite3.connect(str(db_path))
            try:
                rows = list(
                    connection.execute(
                        "SELECT trigger_text,reply_text,context_json FROM style_pairs"
                    )
                )
            finally:
                connection.close()
        self.assertEqual(len(rows), 500)
        hit_counts = [0 for _ in patterns]
        for trigger, reply, context_json in rows:
            texts = [trigger, reply]
            texts.extend(str(item.get("text") or "") for item in json.loads(context_json))
            for index, pattern in enumerate(patterns):
                hit_counts[index] += sum(bool(pattern.search(text)) for text in texts)
        self.assertEqual(hit_counts, [0, 0, 0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
