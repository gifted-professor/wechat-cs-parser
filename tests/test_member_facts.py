from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_cs.identity import global_phone_hmac
from wechat_cs.member_facts import import_member_facts
from wechat_cs.store import initialize_m0_run, open_store


SECRET = "member-facts-fixture-secret-with-at-least-32-characters"
OTHER_SECRET = "other-member-facts-secret-with-at-least-32-characters"
PROFILE_ID = "aolai1"
CUSTOMER_OK = "customer_member_ok_111111111"
CUSTOMER_CONFLICT = "customer_member_conflict_222"
CUSTOMER_PHONE_CONFLICT = "customer_member_phone_conflict_333"
CUSTOMER_CONFLICT_OWNER = "customer_conflict_owner_444"
PHONE_OK = "13800138000"
PHONE_CONFLICT = "13900139000"
PHONE_SHARED_CONFLICT = "13700137000"


class MemberFactsImportTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        created = initialize_m0_run(
            runs_dir=root / ".wechat-cs" / "runs",
            secret=SECRET,
            project_root=root,
            run_id="member-facts-run",
        )
        db_path = Path(created["db"])
        connection = open_store(str(db_path))
        try:
            with connection:
                # The production table is added by schema v4.  Define the same
                # contract here so this importer remains independently testable.
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS customer_aux_facts (
                        aux_fact_id TEXT PRIMARY KEY,
                        source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
                        source_namespace TEXT NOT NULL,
                        source_record_id TEXT NOT NULL,
                        customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
                        profile_id TEXT NOT NULL,
                        phone_hmac TEXT NOT NULL,
                        member_birthday TEXT,
                        preferred_style TEXT,
                        expected_gift TEXT,
                        member_shop TEXT,
                        source_hash TEXT NOT NULL,
                        quality_flags_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        UNIQUE(
                            source_snapshot_id,source_namespace,source_record_id,customer_key
                        )
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                        mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                        captured_at,consistency_state,quality_json
                    ) VALUES(
                        'snapshot-events','member-facts-run','live-inbox-events','opaque',
                        1,1,1,1,?,4,NULL,NULL,'2026-07-13T12:00:00+08:00',
                        '2026-07-13T12:00:01+08:00','consistent','{}'
                    )
                    """,
                    ("e" * 64,),
                )
                connection.execute(
                    """
                    INSERT INTO account_registry(
                        profile_id,canonical_account_id,state,confidence,evidence_json,
                        config_hash,version
                    ) VALUES(?, 'account-one', 'approved', 1.0, '{}', 'cfg', 'accounts-v1')
                    """,
                    (PROFILE_ID,),
                )
                customers = (
                    CUSTOMER_OK,
                    CUSTOMER_CONFLICT,
                    CUSTOMER_PHONE_CONFLICT,
                    CUSTOMER_CONFLICT_OWNER,
                )
                for ordinal, customer_key in enumerate(customers, start=1):
                    connection.execute(
                        """
                        INSERT INTO customers(
                            customer_key,display_name,last_active_at,opportunity_score,
                            opportunity_level,aftersales_priority,summary,reasons_json,
                            evidence_json,memory_json,source_file
                        ) VALUES(?, 'fixture', '2026-07-13T12:00:00+08:00', 0, 'low',
                                 NULL, 'fixture', '[]', '[]', '{}', 'events.jsonl')
                        """,
                        (customer_key,),
                    )
                    connection.execute(
                        """
                        INSERT INTO conversation_refs(
                            customer_key,profile_id,canonical_account_id,raw_wechat_id_hash,
                            source_snapshot_id
                        ) VALUES(?, ?, 'account-one', ?, 'snapshot-events')
                        """,
                        (customer_key, PROFILE_ID, "raw-hash-%d" % ordinal),
                    )

                links = (
                    ("link-ok", CUSTOMER_OK, global_phone_hmac(SECRET, PHONE_OK), "approved"),
                    (
                        "link-customer-approved",
                        CUSTOMER_CONFLICT,
                        global_phone_hmac(SECRET, PHONE_CONFLICT),
                        "approved",
                    ),
                    ("link-customer-conflict", CUSTOMER_CONFLICT, None, "conflict"),
                    (
                        "link-phone-approved",
                        CUSTOMER_PHONE_CONFLICT,
                        global_phone_hmac(SECRET, PHONE_SHARED_CONFLICT),
                        "approved",
                    ),
                    (
                        "link-phone-conflict",
                        CUSTOMER_CONFLICT_OWNER,
                        global_phone_hmac(SECRET, PHONE_SHARED_CONFLICT),
                        "conflict",
                    ),
                )
                for ordinal, (link_id, customer_key, phone_hmac, state) in enumerate(
                    links, start=1
                ):
                    connection.execute(
                        """
                        INSERT INTO conversation_links(
                            link_id,customer_key,profile_id,raw_wechat_id_hash,phone_hmac,
                            match_method,confidence,state,source_hash,version,reviewed_at
                        ) VALUES(?, ?, ?, ?, ?, 'fixture', 1.0, ?, 'identity-source',
                                 'identity-v1', '2026-07-13T11:00:00+08:00')
                        """,
                        (
                            link_id,
                            customer_key,
                            PROFILE_ID,
                            "link-raw-hash-%d" % ordinal,
                            phone_hmac,
                            state,
                        ),
                    )
        finally:
            connection.close()
        return db_path

    @staticmethod
    def _record(record_id: str, phone: str, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "record_id": record_id,
            "member_name": "绝不落库的会员姓名",
            "member_phone": phone,
            "member_birthday": "46267",
            "member_shop": "芋圆奥莱一店",
            "preferred_style": "短袖，联系电话 13800138000，mail@example.com",
            "expected_gift": "联系人张三：超值福袋盲盒",
            "wechat": "unsafe_wechat_id",
        }
        record.update(overrides)
        return record

    def test_records_envelope_is_read_only_idempotent_redacted_and_conflict_gated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._database(root)
            source = root / "birthday_members.json"
            source.write_text(
                json.dumps(
                    {
                        "synced_at": "2026-07-13T10:03:51.608Z",
                        "total_records": 5,
                        "records": [
                            self._record("member-ok", PHONE_OK),
                            self._record("member-customer-conflict", PHONE_CONFLICT),
                            self._record("member-phone-conflict", PHONE_SHARED_CONFLICT),
                            self._record("member-unmatched", "13600136000"),
                            self._record("member-invalid-phone", "not-a-phone"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            before = source.read_bytes()

            first = import_member_facts(db_path, source, secret=SECRET)
            second = import_member_facts(db_path, source, secret=SECRET)

            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["source_snapshot_id"], second["source_snapshot_id"])
            self.assertEqual(first["quality"]["source_records"], 5)
            self.assertEqual(first["quality"]["matched_records"], 1)
            self.assertEqual(first["quality"]["persisted_facts"], 1)
            self.assertEqual(first["quality"]["conflict_filtered_records"], 2)
            self.assertEqual(first["quality"]["unmatched_records"], 1)
            self.assertEqual(first["quality"]["invalid_phone_records"], 1)
            self.assertEqual(source.read_bytes(), before)

            connection = open_store(str(db_path), read_only=True)
            try:
                row = connection.execute(
                    "SELECT * FROM customer_aux_facts"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["customer_key"], CUSTOMER_OK)
                self.assertEqual(row["profile_id"], PROFILE_ID)
                self.assertEqual(row["phone_hmac"], global_phone_hmac(SECRET, PHONE_OK))
                self.assertEqual(row["member_birthday"], "2026-09-02")
                self.assertEqual(row["member_shop"], "芋圆奥莱一店")
                self.assertNotIn(PHONE_OK, row["preferred_style"])
                self.assertNotIn("mail@example.com", row["preferred_style"])
                self.assertIn("[手机号]", row["preferred_style"])
                self.assertIn("[邮箱]", row["preferred_style"])
                self.assertIn("[姓名]", row["expected_gift"])
                flags = json.loads(row["quality_flags_json"])
                self.assertIn("preferred_style:redacted_phone", flags)
                self.assertIn("preferred_style:redacted_email", flags)
                self.assertIn("expected_gift:redacted_person_name", flags)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM source_snapshots WHERE source_kind='birthday_members'"
                    ).fetchone()[0],
                    1,
                )
                snapshot = connection.execute(
                    "SELECT observed_until,quality_json FROM source_snapshots "
                    "WHERE source_kind='birthday_members'"
                ).fetchone()
                self.assertEqual(snapshot["observed_until"], "2026-07-13T18:03:51+08:00")
                self.assertEqual(json.loads(snapshot["quality_json"]), first["quality"])
            finally:
                connection.close()

            database_bytes = db_path.read_bytes()
            for unsafe in (
                PHONE_OK.encode("utf-8"),
                "绝不落库的会员姓名".encode("utf-8"),
                b"unsafe_wechat_id",
                b"mail@example.com",
            ):
                self.assertNotIn(unsafe, database_bytes)

    def test_top_level_array_is_supported_and_changed_sources_are_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._database(root)
            first_source = root / "members-array.json"
            first_source.write_text(
                json.dumps([self._record("member-array", PHONE_OK)], ensure_ascii=False),
                encoding="utf-8",
            )
            first = import_member_facts(db_path, first_source, secret=SECRET)
            self.assertFalse(first["idempotent"])

            second_source = root / "members-array-next.json"
            second_source.write_text(
                json.dumps(
                    [
                        self._record(
                            "member-array",
                            PHONE_OK,
                            preferred_style="长款外套",
                        )
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            second = import_member_facts(db_path, second_source, secret=SECRET)
            self.assertFalse(second["idempotent"])
            self.assertNotEqual(first["source_snapshot_id"], second["source_snapshot_id"])

            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM customer_aux_facts").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM source_snapshots WHERE source_kind='birthday_members'"
                    ).fetchone()[0],
                    2,
                )
                styles = {
                    row[0]
                    for row in connection.execute(
                        "SELECT preferred_style FROM customer_aux_facts"
                    )
                }
                self.assertEqual(styles, {"短袖，联系电话 [手机号]，[邮箱]", "长款外套"})
            finally:
                connection.close()

    def test_hmac_fingerprint_mismatch_is_rejected_without_persisting_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._database(root)
            source = root / "birthday_members.json"
            source.write_text(
                json.dumps([self._record("member-ok", PHONE_OK)], ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "HMAC key fingerprint mismatch"):
                import_member_facts(db_path, source, secret=OTHER_SECRET)

            connection = open_store(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM customer_aux_facts").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM source_snapshots WHERE source_kind='birthday_members'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_document_must_be_a_records_envelope_or_top_level_array(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = self._database(root)
            source = root / "bad.json"
            source.write_text('{"members": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "records envelope or top-level array"):
                import_member_facts(db_path, source, secret=SECRET)


if __name__ == "__main__":
    unittest.main()
