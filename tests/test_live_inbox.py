from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_cs.build import build_live_inbox_database
from wechat_cs.live_inbox import load_live_inbox
from wechat_cs.store import initialize_m0_run, open_store


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "live_inbox"
EVENTS = FIXTURE_ROOT / "events.jsonl"
STATE = FIXTURE_ROOT / "state.json"
ACCOUNTS = FIXTURE_ROOT / "accounts.json"
SECRET = "live-inbox-fixture-secret-with-at-least-32-characters"


class LiveInboxAdapterTests(unittest.TestCase):
    def test_filtering_role_time_and_quarantine_contract(self) -> None:
        snapshot = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        self.assertEqual(len(snapshot.messages), 4)
        self.assertEqual(snapshot.quarantine_counts["duplicate_exact"], 1)
        self.assertEqual(snapshot.quarantine_counts["duplicate_conflict"], 1)
        self.assertEqual(snapshot.quarantine_counts["unknown_sender"], 1)
        self.assertEqual(snapshot.quarantine_counts["unknown_profile"], 1)
        self.assertEqual(snapshot.quarantine_counts["timestamp_mismatch"], 1)
        self.assertEqual({item.role for item in snapshot.messages}, {"customer", "studio"})
        self.assertTrue(all(item.timestamp.endswith("+08:00") for item in snapshot.messages))
        self.assertEqual(len(snapshot.conversations), 2)
        self.assertTrue(all(ref.raw_wechat_id for ref in snapshot.conversations.values()))
        self.assertTrue(all("customer-raw" not in ref.raw_wechat_id_hash for ref in snapshot.conversations.values()))
        self.assertIsNotNone(snapshot.observed_until_by_profile["profile-a"])
        self.assertIsNotNone(snapshot.observed_until_by_profile["profile-b"])
        self.assertIsNone(snapshot.observed_until_by_profile["profile-d"])

    def test_structured_semantic_pii_is_redacted_before_persistence(self) -> None:
        snapshot = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        serialized = json.dumps([item.text for item in snapshot.messages], ensure_ascii=False)
        for unsafe in (
            "某学校",
            "某某",
            "虚构值",
            "studio-sender-a",
            "customer-raw-a",
        ):
            self.assertNotIn(unsafe, serialized)
        self.assertIn("[地址]", serialized)
        self.assertIn("[手机号]", serialized)
        self.assertIn("[姓名]", serialized)

    def test_source_files_remain_byte_identical(self) -> None:
        before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
            for path in (EVENTS, STATE, ACCOUNTS)
        }
        load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
            for path in (EVENTS, STATE, ACCOUNTS)
        }
        self.assertEqual(before, after)

    def test_build_persists_only_redacted_messages_and_hashed_conversation_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            created = initialize_m0_run(
                runs_dir=root / ".wechat-cs" / "runs",
                secret=SECRET,
                project_root=root,
                run_id="live-fixture-run",
            )
            db_path = Path(created["db"])
            result = build_live_inbox_database(
                events_path=EVENTS,
                state_path=STATE,
                accounts_path=ACCOUNTS,
                db_path=db_path,
                secret=SECRET,
            )
            self.assertEqual(result["accepted_messages"], 4)
            self.assertEqual(result["conversations"], 2)
            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM conversation_refs").fetchone()[0], 2
                )
                roles = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT source_status FROM role_calibration"
                    )
                }
                self.assertEqual(roles, {None})
            finally:
                connection.close()
            serialized = db_path.read_bytes()
            for unsafe in (
                b"customer-raw-a",
                b"customer-raw-b",
                "某学校".encode("utf-8"),
                "虚构值".encode("utf-8"),
            ):
                self.assertNotIn(unsafe, serialized)

    def test_repeated_build_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            created = initialize_m0_run(
                runs_dir=root / ".wechat-cs" / "runs",
                secret=SECRET,
                project_root=root,
                run_id="idempotent-run",
            )
            db_path = Path(created["db"])
            first = build_live_inbox_database(EVENTS, STATE, ACCOUNTS, db_path, secret=SECRET)
            second = build_live_inbox_database(EVENTS, STATE, ACCOUNTS, db_path, secret=SECRET)
            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            connection = sqlite3.connect(str(db_path))
            try:
                counts = {
                    table: connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                    for table in ("customers", "messages", "conversation_refs", "source_snapshots")
                }
            finally:
                connection.close()
            self.assertEqual(counts, {"customers": 2, "messages": 4, "conversation_refs": 2, "source_snapshots": 2})


if __name__ == "__main__":
    unittest.main()
