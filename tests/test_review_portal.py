from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from wechat_cs.review_portal import create_server
from wechat_cs.store import initialize_schema


ACCESS_CODE = "synthetic-review-access-code-12345"
NOW = "2026-07-13T20:14:37+08:00"


class ReviewPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "portal.sqlite3"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        initialize_schema(conn)
        self._seed(conn)
        conn.close()
        self.server = create_server(
            "127.0.0.1",
            0,
            db_path=self.db_path,
            access_code=ACCESS_CODE,
            run_id="sales-run",
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
            ("source-run", "complete", "test", "fingerprint", "accounts", "m0-order-v3", "m0-card-v1", NOW, NOW, "{}"),
        )
        conn.execute(
            "INSERT INTO source_snapshots(snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,"
            "mtime_ns,sha256,record_count,first_at,last_at,observed_until,captured_at,consistency_state,quality_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("snapshot", "source-run", "wechat-live-inbox", "path", 1, 2, 3, 4, "a" * 64, 2, NOW, NOW, NOW, NOW, "stable", "{}"),
        )
        conn.execute(
            "INSERT INTO customers(customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
            "summary,reasons_json,evidence_json,memory_json,source_file) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("customer-1", "张三 13800138000", NOW, 80, "high", "测试", "[]", "[]", "{}", "fixture"),
        )
        conn.execute(
            "INSERT INTO sales_profile_runs(sales_profile_run_id,source_run_id,as_of_at,status,model,prompt_version,"
            "profile_schema_version,sampling_version,message_snapshot_id,cohort_hash,config_json,counts_json,quality_json,"
            "created_at,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sales-run", "source-run", NOW, "complete", "kimi-for-coding", "events-v1", "card-v1", "sampling-v1", "snapshot", "cohort", "{}", "{}", "{}", NOW, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO sales_profile_subjects(subject_id,sales_profile_run_id,customer_key,profile_id,phone_hmac,"
            "stratum,stratum_rank,selection_reason_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'{}',?,?,?)",
            ("subject-1", "sales-run", "customer-1", "account-1", "phone-hmac", "high_value", 1, "succeeded", NOW, NOW),
        )
        facts = {
            "customer_features": {
                "value_bucket": "high", "rfm_frequency": 3, "rfm_monetary_minor": 26800,
                "rfm_recency_days": 12, "recommended_contact_window": "18:00-24:00",
                "contact_window_evidence_count": 8, "median_reply_delay_seconds": 62,
                "preferred_skus": ["短袖"], "preferred_colors": ["黑色"], "preferred_sizes": ["M"],
            },
            "member_facts": [],
        }
        card = {
            "customer_value": {"summary": "高价值客户", "facts": ["手机号 13800138000 已隐藏"]},
            "current_opportunity": {"summary": "适合自然回访"},
            "natural_opening": "晚上好，晚点方便看看吗？",
            "evidence": [{"sales_profile_event_id": "sales-profile-event-abcdef1234567890"}],
        }
        conn.execute(
            "INSERT INTO sales_profiles(sales_profile_id,subject_id,status,input_hash,idempotency_key,model,prompt_version,"
            "profile_schema_version,card_version,deterministic_facts_json,profile_json,evidence_json,error_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sales-profile-1", "subject-1", "succeeded", "input", "idem", "kimi-for-coding", "events-v1", "card-v1", "card-version-1", json.dumps(facts), json.dumps(card, ensure_ascii=False), "[]", "{}", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO sales_profile_events(sales_profile_event_id,subject_id,chunk_index,event_type,event_json,"
            "evidence_json,confidence,validation_state,model,prompt_version,input_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("event-1", "subject-1", 0, "future_return", json.dumps({"summary": "订单 order-line_abcdef123456 有回访机会"}, ensure_ascii=False), json.dumps([{"kind": "message", "message_key": "message_abcdef", "quote": "电话 13800138000，晚点我再来"}], ensure_ascii=False), 0.9, "accepted", "kimi-for-coding", "events-v1", "event-input", NOW),
        )
        conn.commit()

    def request(self, method: str, path: str, payload=None, *, token: str = ACCESS_CODE, host: str = "127.0.0.1"):
        body = None
        headers = {"Accept": "application/json", "Host": host}
        if token:
            headers["X-Review-Access-Code"] = token
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            data = json.loads(raw.decode("utf-8")) if "json" in content_type else raw.decode("utf-8")
            return response.status, data
        finally:
            connection.close()

    @staticmethod
    def valid_review(**overrides):
        payload = {
            "card_version": "card-version-1",
            "verdict": "approved",
            "scores": {
                "fact_accuracy": 5, "insight_usefulness": 4, "sales_realism": 5,
                "timing_quality": 4, "evidence_quality": 5,
            },
            "corrections": {}, "notes": "可以使用", "reviewer": "客户评审 A",
        }
        payload.update(overrides)
        return payload

    def test_access_code_host_and_static_boundary(self) -> None:
        status, page = self.request("GET", "/", token="")
        self.assertEqual(status, 200)
        self.assertIn("Kimi 销售画像验收", page)
        status, payload = self.request("GET", "/api/summary", token="wrong-code")
        self.assertEqual((status, payload["error"]["code"]), (401, "unauthorized"))
        status, payload = self.request("GET", "/api/summary", host="evil.example")
        self.assertEqual((status, payload["error"]["code"]), (403, "host_denied"))

    def test_anonymous_detail_and_evidence_redaction(self) -> None:
        status, listing = self.request("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0]["label"], "高价值客户 · 样本 01")
        self.assertNotIn("display_name", json.dumps(listing, ensure_ascii=False))
        status, detail = self.request("GET", "/api/profiles/sales-profile-1")
        self.assertEqual(status, 200)
        serialized = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("message_abcdef", serialized)
        self.assertNotIn("order-line_abcdef123456", serialized)
        self.assertNotIn("sales-profile-event-abcdef1234567890", serialized)
        self.assertEqual(detail["facts"]["inventory_assumption"], "默认满库存，可按历史偏好推荐商品")
        self.assertFalse(detail["send_allowed"])

    def test_review_upsert_and_version_conflict(self) -> None:
        status, created = self.request("POST", "/api/profiles/sales-profile-1/review", self.valid_review())
        self.assertEqual(status, 200)
        review_id = created["review"]["review_id"]
        status, updated = self.request("POST", "/api/profiles/sales-profile-1/review", self.valid_review(notes="第二次修改"))
        self.assertEqual(status, 200)
        self.assertEqual(updated["review"]["review_id"], review_id)
        status, stale = self.request("POST", "/api/profiles/sales-profile-1/review", self.valid_review(card_version="old"))
        self.assertEqual((status, stale["error"]["code"]), (409, "card_version_conflict"))
        conn = sqlite3.connect(str(self.db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM sales_profile_reviews").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_edited_and_rejected_reviews_require_actionable_comments(self) -> None:
        status, edited = self.request(
            "POST", "/api/profiles/sales-profile-1/review",
            self.valid_review(verdict="edited", corrections={}, notes=""),
        )
        self.assertEqual((status, edited["error"]["code"]), (400, "missing_corrections"))
        status, rejected = self.request(
            "POST", "/api/profiles/sales-profile-1/review",
            self.valid_review(verdict="rejected", corrections={}, notes=""),
        )
        self.assertEqual((status, rejected["error"]["code"]), (400, "missing_rejection_reason"))

    def test_no_model_or_send_route(self) -> None:
        for path in ("/api/profiles/sales-profile-1/send", "/api/run", "/v1/sales-profile-pilot"):
            status, payload = self.request("POST", path, {})
            self.assertIn(status, {404, 405})
            self.assertIn(payload["error"]["code"], {"not_found", "method_not_allowed"})


if __name__ == "__main__":
    unittest.main()
