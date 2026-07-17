from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from wechat_cs.api import create_server
from wechat_cs.store import initialize_schema


TOKEN = "synthetic-sales-profile-api-token"
NOW = "2026-07-13T20:14:37+08:00"


class SalesProfileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sales-profile-api.sqlite3"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        initialize_schema(conn)
        self._seed(conn)
        conn.close()

        self.server = create_server(
            host="127.0.0.1", port=0, db_path=self.db_path, token=TOKEN
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp_dir.cleanup()

    @staticmethod
    def _seed(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO pipeline_runs(run_id,state,parser_version,hmac_key_fingerprint,"
            "account_config_hash,order_rule_version,card_rule_version,started_at,completed_at,quality_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "source-run",
                "complete",
                "test-parser",
                "test-fingerprint",
                "test-accounts",
                "m0-order-v3",
                "m0-card-v1",
                "2026-07-13T14:07:30+08:00",
                NOW,
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO source_snapshots(snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,"
            "mtime_ns,sha256,record_count,first_at,last_at,observed_until,captured_at,consistency_state,quality_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "message-snapshot",
                "source-run",
                "wechat-live-inbox",
                "path-hash",
                1,
                2,
                3,
                4,
                "a" * 64,
                10,
                "2026-01-01T00:00:00+08:00",
                NOW,
                NOW,
                NOW,
                "stable",
                "{}",
            ),
        )
        for index, name in enumerate(("客户甲", "客户乙", "历史客户"), 1):
            conn.execute(
                "INSERT INTO customers(customer_key,display_name,last_active_at,opportunity_score,"
                "opportunity_level,aftersales_priority,summary,reasons_json,evidence_json,memory_json,source_file) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "customer-%s" % index,
                    name,
                    NOW,
                    50,
                    "medium",
                    None,
                    "测试客户",
                    "[]",
                    "[]",
                    "{}",
                    "fixture",
                ),
            )

        run_values = (
            "source-run",
            "complete",
            "kimi-k2.6",
            "sales-events-v1",
            "sales-card-v1",
            "sales-profile-sampling-v1",
            "message-snapshot",
            "cohort-hash",
            "{}",
            "{}",
            "{}",
        )
        conn.execute(
            "INSERT INTO sales_profile_runs(sales_profile_run_id,source_run_id,as_of_at,status,model,"
            "prompt_version,profile_schema_version,sampling_version,message_snapshot_id,cohort_hash,"
            "config_json,counts_json,quality_json,created_at,started_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sales-run-old",
                *run_values[:1],
                "2026-06-30T23:59:59+08:00",
                *run_values[1:],
                "2026-07-01T00:00:00+08:00",
                "2026-07-01T00:00:00+08:00",
                "2026-07-01T00:01:00+08:00",
            ),
        )
        conn.execute(
            "INSERT INTO sales_profile_runs(sales_profile_run_id,source_run_id,as_of_at,status,model,"
            "prompt_version,profile_schema_version,sampling_version,message_snapshot_id,cohort_hash,"
            "config_json,counts_json,quality_json,created_at,started_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sales-run-latest",
                *run_values[:1],
                NOW,
                *run_values[1:7],
                "cohort-hash-latest",
                *run_values[8:],
                "2026-07-13T20:15:00+08:00",
                "2026-07-13T20:15:00+08:00",
                "2026-07-13T20:20:00+08:00",
            ),
        )

        subjects = (
            (
                "subject-1",
                "sales-run-latest",
                "customer-1",
                "aolai1",
                "phone-hmac-1",
                "complex_risk",
                1,
                "succeeded",
            ),
            (
                "subject-2",
                "sales-run-latest",
                "customer-2",
                "aolai2",
                "phone-hmac-2",
                "high_value",
                1,
                "failed",
            ),
            (
                "subject-old",
                "sales-run-old",
                "customer-3",
                "service",
                "phone-hmac-3",
                "control",
                1,
                "succeeded",
            ),
        )
        conn.executemany(
            "INSERT INTO sales_profile_subjects(subject_id,sales_profile_run_id,customer_key,profile_id,"
            "phone_hmac,stratum,stratum_rank,selection_reason_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'{}',?,?,?)",
            [(*row, NOW, NOW) for row in subjects],
        )

        profiles = (
            (
                "sales-profile-1",
                "subject-1",
                "succeeded",
                "input-1",
                "idempotency-1",
                "card-version-1",
                '{"rhythm":{"preferred":"evening"}}',
                '{"customer_value":"high","natural_opening":"晚上好，之前您提到的款式到了。"}',
                '[{"message_key":"message-1","quote":"晚点我再来"}]',
                None,
            ),
            (
                "sales-profile-2",
                "subject-2",
                "failed",
                "input-2",
                "idempotency-2",
                None,
                "{}",
                "{}",
                "[]",
                "profile_failed",
            ),
            (
                "sales-profile-old",
                "subject-old",
                "succeeded",
                "input-old",
                "idempotency-old",
                "card-version-old",
                "{}",
                '{"customer_value":"control"}',
                "[]",
                None,
            ),
        )
        conn.executemany(
            "INSERT INTO sales_profiles(sales_profile_id,subject_id,status,input_hash,idempotency_key,model,"
            "prompt_version,profile_schema_version,card_version,deterministic_facts_json,profile_json,"
            "evidence_json,error_code,error_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'kimi-k2.6','sales-events-v1','sales-card-v1',?,?,?,?,?,'{}',?,?)",
            [(*row, NOW, NOW) for row in profiles],
        )
        conn.executemany(
            "INSERT INTO sales_profile_events(sales_profile_event_id,subject_id,chunk_index,event_type,"
            "event_json,evidence_json,confidence,validation_state,rejection_reason,model,prompt_version,input_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'kimi-k2.6','sales-events-v1','event-input',?)",
            (
                (
                    "event-accepted",
                    "subject-1",
                    0,
                    "future_return",
                    '{"summary":"客户说晚点再来"}',
                    '[{"message_key":"message-1","quote":"晚点我再来"}]',
                    0.9,
                    "accepted",
                    None,
                    NOW,
                ),
                (
                    "event-rejected",
                    "subject-1",
                    0,
                    "brand_preference",
                    '{"summary":"无证据品牌"}',
                    "[]",
                    0.4,
                    "rejected",
                    "missing_evidence",
                    NOW,
                ),
            ),
        )
        conn.commit()

    def request(self, method: str, path: str, payload=None):
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + TOKEN,
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else None
        finally:
            connection.close()

    @staticmethod
    def valid_review(**overrides):
        payload = {
            "card_version": "card-version-1",
            "verdict": "approved",
            "scores": {
                "fact_accuracy": 5,
                "insight_usefulness": 4,
                "sales_realism": 5,
                "timing_quality": 4,
                "evidence_quality": 5,
            },
            "corrections": {},
            "notes": "事实与开场可用",
            "reviewer": "operator_1",
        }
        payload.update(overrides)
        return payload

    def test_list_defaults_to_latest_run_and_supports_filters(self) -> None:
        status, payload = self.request("GET", "/v1/sales-profile-pilot")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["sales_profile_run_id"], "sales-run-latest")
        self.assertEqual(payload["run"]["as_of_at"], NOW)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(
            [item["stratum"] for item in payload["items"]],
            ["complex_risk", "high_value"],
        )
        self.assertEqual(payload["contact_warning"], "联系前核对最新状态")
        self.assertFalse(payload["send_allowed"])

        status, filtered = self.request(
            "GET",
            "/v1/sales-profile-pilot?run_id=latest&status=failed&stratum=high_value&limit=1&offset=0",
        )
        self.assertEqual(status, 200)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["sales_profile_id"], "sales-profile-2")

        status, old = self.request(
            "GET", "/v1/sales-profile-pilot?run_id=sales-run-old"
        )
        self.assertEqual(status, 200)
        self.assertEqual(old["total"], 1)
        self.assertEqual(old["items"][0]["sales_profile_id"], "sales-profile-old")

    def test_list_supports_reviewed_and_unreviewed_status(self) -> None:
        created, _ = self.request(
            "POST",
            "/v1/sales-profile-pilot/sales-profile-1/review",
            self.valid_review(),
        )
        self.assertEqual(created, 201)

        status, reviewed = self.request(
            "GET", "/v1/sales-profile-pilot?status=reviewed"
        )
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["total"], 1)
        self.assertEqual(reviewed["items"][0]["sales_profile_id"], "sales-profile-1")

        status, unreviewed = self.request(
            "GET", "/v1/sales-profile-pilot?status=unreviewed"
        )
        self.assertEqual(status, 200)
        self.assertEqual(unreviewed["total"], 1)
        self.assertEqual(unreviewed["items"][0]["sales_profile_id"], "sales-profile-2")

    def test_list_rejects_unknown_or_invalid_filters(self) -> None:
        cases = (
            ("?unknown=1", "invalid_query"),
            ("?limit=101", "invalid_pagination"),
            ("?limit=0", "invalid_pagination"),
            ("?offset=-1", "invalid_pagination"),
            ("?status=complete", "invalid_status"),
            ("?stratum=vip", "invalid_stratum"),
        )
        for suffix, code in cases:
            with self.subTest(suffix=suffix):
                status, payload = self.request(
                    "GET", "/v1/sales-profile-pilot" + suffix
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], code)

    def test_detail_returns_complete_card_accepted_events_and_reviews(self) -> None:
        status, payload = self.request(
            "GET", "/v1/sales-profile-pilot/sales-profile-1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["profile"]["sales_profile_id"], "sales-profile-1")
        self.assertEqual(payload["profile"]["profile_json"]["customer_value"], "high")
        self.assertEqual(
            payload["profile"]["deterministic_facts_json"]["rhythm"]["preferred"],
            "evening",
        )
        self.assertEqual(payload["profile"]["evidence_json"][0]["message_key"], "message-1")
        self.assertEqual(
            [event["sales_profile_event_id"] for event in payload["accepted_events"]],
            ["event-accepted"],
        )
        self.assertEqual(payload["reviews"], [])
        self.assertEqual(payload["run"]["as_of_at"], NOW)
        self.assertEqual(payload["contact_warning"], "联系前核对最新状态")
        self.assertFalse(payload["send_allowed"])

    def test_review_create_and_same_reviewer_upsert(self) -> None:
        status, created = self.request(
            "POST",
            "/v1/sales-profile-pilot/sales-profile-1/review",
            self.valid_review(),
        )
        self.assertEqual(status, 201)
        review_id = created["review"]["review_id"]
        self.assertFalse(created["send_allowed"])

        updated_payload = self.valid_review(
            verdict="edited",
            corrections={"natural_opening": "换成更自然的老客开场"},
            notes="已人工改写",
        )
        status, updated = self.request(
            "POST",
            "/v1/sales-profile-pilot/sales-profile-1/review",
            updated_payload,
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["review"]["review_id"], review_id)
        self.assertEqual(updated["review"]["verdict"], "edited")
        self.assertEqual(updated["review"]["corrections"], updated_payload["corrections"])

        conn = sqlite3.connect(str(self.db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sales_profile_reviews WHERE sales_profile_id=? AND reviewer=?",
                ("sales-profile-1", "operator_1"),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_review_rejects_stale_version_and_invalid_payloads(self) -> None:
        status, stale = self.request(
            "POST",
            "/v1/sales-profile-pilot/sales-profile-1/review",
            self.valid_review(card_version="stale-card"),
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"]["code"], "card_version_conflict")

        invalid_payloads = (
            (self.valid_review(verdict="maybe"), "invalid_verdict"),
            (
                self.valid_review(scores={"fact_accuracy": 5}),
                "invalid_scores",
            ),
            (
                self.valid_review(
                    scores={
                        "fact_accuracy": 0,
                        "insight_usefulness": 4,
                        "sales_realism": 5,
                        "timing_quality": 4,
                        "evidence_quality": 5,
                    }
                ),
                "invalid_scores",
            ),
            (
                self.valid_review(verdict="edited", corrections={}),
                "missing_corrections",
            ),
            (self.valid_review(reviewer=""), "invalid_reviewer"),
            (
                dict(self.valid_review(), unexpected=True),
                "invalid_review",
            ),
        )
        for body, code in invalid_payloads:
            with self.subTest(code=code):
                status, payload = self.request(
                    "POST",
                    "/v1/sales-profile-pilot/sales-profile-1/review",
                    body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], code)

    def test_profile_without_card_version_cannot_be_reviewed(self) -> None:
        status, payload = self.request(
            "POST",
            "/v1/sales-profile-pilot/sales-profile-2/review",
            self.valid_review(card_version="anything"),
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "profile_not_ready")

    def test_no_model_trigger_or_send_endpoint_is_exposed(self) -> None:
        for path in (
            "/v1/sales-profile-pilot",
            "/v1/sales-profile-pilot/sales-profile-1/send",
        ):
            status, payload = self.request("POST", path, {})
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
