from __future__ import annotations

import http.client
import datetime as dt
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from wechat_cs.api import ApiHandler, create_server
from wechat_cs.store import initialize_schema, open_store


TOKEN = "synthetic-action-api-token"
QUEUE_DATE = "2026-07-13"
CUSTOMER_KEY = "customer_0123456789abcdef"
PHONE_HMAC = "phone_0123456789abcdef"


def nested_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key).lower()
            yield from nested_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_keys(item)


class ActionQueueApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "action-api.sqlite3"
        conn = open_store(str(self.db_path))
        initialize_schema(conn)
        conn.execute(
            "INSERT INTO customers(customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
            "aftersales_priority,summary,reasons_json,evidence_json,memory_json,source_file) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                CUSTOMER_KEY,
                "不应公开的姓名",
                "2026-07-13T09:00:00+08:00",
                80,
                "high",
                None,
                "",
                "[]",
                "[]",
                "{}",
                "private-source.json",
            ),
        )
        conn.execute(
            "INSERT INTO pipeline_runs(run_id,state,parser_version,hmac_key_fingerprint,started_at,completed_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "run_action_api",
                "complete",
                "test",
                "private-fingerprint",
                "2026-07-13T09:00:00+08:00",
                "2026-07-13T09:01:00+08:00",
            ),
        )
        self._insert_queue_run(
            conn,
            queue_date=QUEUE_DATE,
            status="ready",
            block_reasons=[],
            freshness={
                "messages": {"state": "fresh", "age_seconds": 120},
                "orders": {"state": "fresh", "age_seconds": 600},
            },
            counts={"reply_now": 1, "proactive_today": 1, "suppressed": 1},
        )
        self._insert_action(
            conn,
            action_id="action_reply",
            lane="reply_now",
            priority=95,
            required_facts=["inventory", "current_price"],
            signals={"intent_signal": "positive", "value_score": 82},
            missing_facts=["inventory"],
            draft={
                "mode": "rule_skeleton",
                "text": "收到，我先核对库存和价格后准确回复。",
                "model_used": False,
                "hmac_secret": "must-not-leak",
            },
        )
        self._insert_action(
            conn,
            action_id="action_proactive",
            lane="proactive_today",
            priority=70,
            customer_key="customer_abcdef0123456789",
            phone_hmac="phone_abcdef0123456789",
            required_facts=[],
            draft={"mode": "rule_skeleton", "text": "想跟进您之前关注的需求。"},
        )
        self._insert_action(
            conn,
            action_id="action_suppressed",
            lane="suppressed",
            priority=0,
            customer_key="customer_fedcba9876543210",
            phone_hmac="phone_fedcba9876543210",
            required_facts=[],
            draft={"mode": "rule_skeleton", "text": "【不发送】请联系 13800138000。"},
        )
        conn.commit()
        conn.close()

        self.now_patcher = mock.patch(
            "wechat_cs.api._now",
            return_value=dt.datetime(
                2026, 7, 13, 12, 5, tzinfo=dt.timezone(dt.timedelta(hours=8))
            ),
        )
        self.mock_now = self.now_patcher.start()
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
        self.now_patcher.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _insert_queue_run(
        conn: sqlite3.Connection,
        *,
        queue_date: str,
        status: str,
        block_reasons,
        freshness,
        counts,
    ) -> None:
        conn.execute(
            "INSERT INTO action_queue_runs(queue_run_id,run_id,profile_id,queue_date,as_of_at,status,"
            "policy_version,block_reasons_json,freshness_json,counts_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "queue_run_" + queue_date.replace("-", ""),
                "run_action_api",
                "aolai1",
                queue_date,
                queue_date + "T12:00:00+08:00",
                status,
                "action-queue-rules-v2",
                json.dumps(block_reasons),
                json.dumps(freshness),
                json.dumps(counts),
                queue_date + "T12:00:01+08:00",
            ),
        )

    @staticmethod
    def _insert_action(
        conn: sqlite3.Connection,
        *,
        action_id: str,
        lane: str,
        priority: int,
        required_facts,
        draft,
        signals=None,
        missing_facts=None,
        customer_key: str = CUSTOMER_KEY,
        phone_hmac: str = PHONE_HMAC,
    ) -> None:
        if customer_key != CUSTOMER_KEY:
            conn.execute(
                "INSERT INTO customers(customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
                "aftersales_priority,summary,reasons_json,evidence_json,memory_json,source_file) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    customer_key,
                    "私密姓名",
                    "2026-07-13T09:00:00+08:00",
                    50,
                    "medium",
                    None,
                    "",
                    "[]",
                    "[]",
                    "{}",
                    "private-source.json",
                ),
            )
        conn.execute(
            "INSERT INTO action_queue_items(action_id,run_id,customer_key,profile_id,queue_date,lane,priority_score,"
            "priority_version,phone_hmac,reason_codes_json,contact_window_json,recommended_action,strategy_version,"
            "signals_json,required_facts_json,missing_facts_json,prohibited_claims_json,draft_json,confidence,"
            "freshness_json,human_confirmation_state,send_allowed,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                action_id,
                "run_action_api",
                customer_key,
                "aolai1",
                QUEUE_DATE,
                lane,
                priority,
                "priority-v1",
                phone_hmac,
                json.dumps(["unanswered_inbound"] if lane == "reply_now" else [lane]),
                json.dumps({"mode": "as_soon_as_possible"}),
                "reply_to_inbound" if lane == "reply_now" else "manual_review",
                "strategy-v1",
                json.dumps(signals or {}),
                json.dumps(required_facts),
                json.dumps(missing_facts or []),
                json.dumps(["unverified_price", "guaranteed_delivery"]),
                json.dumps(draft, ensure_ascii=False),
                0.9,
                json.dumps({"messages": {"state": "fresh"}, "orders": {"state": "fresh"}}),
                "pending",
                0,
                "2026-07-13T10:00:00+08:00",
                "2026-07-13T10:00:00+08:00",
            ),
        )

    def request(self, method, path, payload=None):
        body = None
        headers = {"Accept": "application/json", "Authorization": "Bearer " + TOKEN}
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

    def test_list_detail_and_query_validation_are_private_and_include_all_lanes(self) -> None:
        status, payload = self.request(
            "GET", "/v1/action-queue?profile=aolai1&date=2026-07-13&limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["block_reasons"], [])
        self.assertEqual(payload["freshness"]["messages"]["state"], "fresh")
        self.assertEqual(payload["policy_version"], "action-queue-rules-v2")
        self.assertEqual(payload["generated_at"], "2026-07-13T12:00:00+08:00")
        self.assertEqual(set(payload["lanes"]), {"reply_now", "proactive_today", "suppressed"})
        self.assertTrue(all(payload["lanes"][lane] for lane in payload["lanes"]))
        self.assertFalse(payload["send_allowed"])
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn(PHONE_HMAC, serialized)
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("must-not-leak", serialized)
        self.assertFalse(
            any("phone" in key or "hmac" in key or "wxid" in key for key in nested_keys(payload))
        )

        detail_status, detail = self.request("GET", "/v1/action-queue/action_reply")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["item"]["customer_key"], CUSTOMER_KEY)
        self.assertEqual(detail["item"]["signals"]["intent_signal"], "positive")
        self.assertEqual(detail["item"]["required_facts"], ["inventory", "current_price"])
        self.assertEqual(detail["item"]["missing_facts"], ["inventory"])
        self.assertTrue(detail["item"]["human_confirmation_required"])
        self.assertFalse(detail["item"]["send_allowed"])

        for path, code in (
            ("/v1/action-queue?date=2026-07-13", "invalid_profile"),
            ("/v1/action-queue?profile=aolai1&date=13-07-2026", "invalid_date"),
            ("/v1/action-queue?profile=aolai1&date=2026-07-13&limit=0", "invalid_limit"),
            ("/v1/action-queue?profile=aolai1&date=2026-07-13&limit=21", "invalid_limit"),
            ("/v1/action-queue?profile=aolai1&date=2026-07-13&unknown=1", "invalid_query"),
        ):
            invalid_status, invalid = self.request("GET", path)
            self.assertEqual(invalid_status, 400)
            self.assertEqual(invalid["error"]["code"], code)

    def test_limit_applies_only_to_proactive_and_never_hides_inbound_or_suppressed(self) -> None:
        conn = open_store(str(self.db_path))
        try:
            for index in range(1, 22):
                self._insert_action(
                    conn,
                    action_id="action_%024x" % index,
                    lane="reply_now",
                    priority=90 - index,
                    customer_key="customer_%024x" % (100 + index),
                    phone_hmac=None,
                    required_facts=["customer_request"],
                    missing_facts=[],
                    draft={"mode": "rule_skeleton", "text": "收到，我先核对。"},
                )
            conn.execute(
                "UPDATE action_queue_runs SET counts_json=? WHERE profile_id='aolai1' AND queue_date=?",
                (
                    json.dumps(
                        {"reply_now": 22, "proactive_today": 1, "suppressed": 1}
                    ),
                    QUEUE_DATE,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        status, payload = self.request(
            "GET", "/v1/action-queue?profile=aolai1&date=2026-07-13&limit=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["lanes"]["reply_now"]), 22)
        self.assertEqual(len(payload["lanes"]["proactive_today"]), 1)
        self.assertEqual(len(payload["lanes"]["suppressed"]), 1)
        self.assertEqual(
            payload["returned_counts"],
            {"reply_now": 22, "proactive_today": 1, "suppressed": 1},
        )

    def test_zero_item_blocked_run_is_returned_and_missing_metadata_fails_closed(self) -> None:
        conn = open_store(str(self.db_path))
        try:
            self._insert_queue_run(
                conn,
                queue_date="2026-07-14",
                status="blocked",
                block_reasons=["message_collection_unhealthy"],
                freshness={
                    "messages": {"state": "unhealthy", "age_seconds": 0},
                    "orders": {"state": "stale", "age_seconds": 90000},
                },
                counts={"reply_now": 0, "proactive_today": 0, "suppressed": 0},
            )
            conn.commit()
        finally:
            conn.close()

        blocked_status, blocked = self.request(
            "GET", "/v1/action-queue?profile=aolai1&date=2026-07-14&limit=20"
        )
        self.assertEqual(blocked_status, 200)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("message_collection_unhealthy", blocked["block_reasons"])
        self.assertEqual(blocked["items"], [])
        self.assertEqual(blocked["counts"], {lane: 0 for lane in blocked["lanes"]})
        self.assertFalse(blocked["send_allowed"])

        missing_status, missing = self.request(
            "GET", "/v1/action-queue?profile=aolai1&date=2026-07-15&limit=20"
        )
        self.assertEqual(missing_status, 503)
        self.assertEqual(missing["error"]["code"], "queue_metadata_missing")
        self.assertEqual(missing["status"], "blocked")
        self.assertNotEqual(missing.get("status"), "ready")

    def test_missing_or_failed_kimi_returns_and_persists_rule_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ""}):
            status, payload = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(status, 200)
        self.assertFalse(payload["draft"]["model_used"])
        self.assertEqual(payload["draft"]["fallback_for"], "kimi_unavailable")
        self.assertFalse(payload["send_allowed"])

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", side_effect=RuntimeError("provider unavailable")
        ):
            failed_status, failed = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(failed_status, 200)
        self.assertFalse(failed["draft"]["model_used"])
        self.assertEqual(failed["draft"]["fallback_for"], "kimi_failed_or_rejected")

        detail_status, detail = self.request("GET", "/v1/action-queue/action_reply")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["item"]["draft"]["mode"], "rule_skeleton")

    def test_safe_kimi_polish_uses_only_explicit_allowlisted_facts(self) -> None:
        model_result = {
            "draft_text": "您好，这款目前有货，价格为100元，我可以先帮您核对。",
            "used_fact_codes": ["inventory", "current_price"],
        }
        with mock.patch.dict(
            os.environ, {"KIMI_API_KEY": "synthetic-key", "KIMI_MODEL": "kimi-k2.6"}
        ), mock.patch.object(ApiHandler, "_call_kimi", return_value=model_result) as call:
            status, payload = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["draft"]["model_used"])
        self.assertEqual(payload["draft"]["model"], "kimi-k2.6")
        self.assertEqual(
            payload["draft"]["grounding_fact_codes"], ["inventory", "current_price"]
        )
        prompt = json.dumps(call.call_args.args[0], ensure_ascii=False).lower()
        self.assertIn("current_fact_allowlist", prompt)
        for forbidden in (
            CUSTOMER_KEY,
            PHONE_HMAC,
            "action_reply",
            "observed_action",
            "paid_30d",
            "outcome",
            "13800138000",
        ):
            self.assertNotIn(forbidden.lower(), prompt)

    def test_empty_fact_allowlist_never_calls_the_model(self) -> None:
        safe_style_only = {
            "draft_text": "想跟进一下您之前关注的需求；如果仍需要，我先帮您核对。",
            "used_fact_codes": [],
        }
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", return_value=safe_style_only
        ) as call:
            status, payload = self.request(
                "POST", "/v1/action-queue/action_proactive/draft", {"facts": {}}
            )
        self.assertEqual(status, 200)
        self.assertFalse(payload["draft"]["model_used"])
        self.assertEqual(payload["draft"]["fallback_for"], "no_grounded_facts")
        call.assert_not_called()

    def test_prior_model_facts_are_never_reused_as_rule_fallback(self) -> None:
        model_result = {
            "draft_text": "您好，这款目前有货，价格为100元，我可以先帮您核对。",
            "used_fact_codes": ["inventory", "current_price"],
        }
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", return_value=model_result
        ):
            generated_status, generated = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(generated_status, 200)
        self.assertIn("100", generated["draft"]["text"])

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ""}):
            fallback_status, fallback = self.request(
                "POST", "/v1/action-queue/action_reply/draft", {"facts": {}}
            )
        self.assertEqual(fallback_status, 200)
        self.assertFalse(fallback["draft"]["model_used"])
        self.assertNotIn("100", fallback["draft"]["text"])
        self.assertNotIn("有货", fallback["draft"]["text"])

    def test_draft_rechecks_freshness_atomically_after_model_work(self) -> None:
        def become_stale(_messages):
            self.mock_now.return_value = dt.datetime(
                2026, 7, 13, 12, 20, tzinfo=dt.timezone(dt.timedelta(hours=8))
            )
            return {
                "draft_text": "您好，这款目前有货，价格为100元。",
                "used_fact_codes": ["inventory", "current_price"],
            }

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", side_effect=become_stale
        ):
            status, payload = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(status, 200)
        self.assertFalse(payload["draft"]["model_used"])
        self.assertEqual(payload["draft"]["fallback_for"], "action_suppressed")
        self.assertNotIn("100", payload["draft"]["text"])

    def test_draft_compare_and_swap_rejects_active_action_drift(self) -> None:
        def mutate_action(_messages):
            conn = open_store(str(self.db_path))
            try:
                conn.execute(
                    "UPDATE action_queue_items SET lane='proactive_today',required_facts_json='[]',"
                    "missing_facts_json='[]',updated_at='2026-07-13T12:05:30+08:00' "
                    "WHERE action_id='action_reply'"
                )
                conn.commit()
            finally:
                conn.close()
            return {
                "draft_text": "您好，这款目前有货，价格为100元。",
                "used_fact_codes": ["inventory", "current_price"],
            }

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", side_effect=mutate_action
        ):
            status, payload = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "action_version_changed")
        conn = open_store(str(self.db_path), read_only=True)
        try:
            stored = conn.execute(
                "SELECT draft_json FROM action_queue_items WHERE action_id='action_reply'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn("100", stored)

    def test_feedback_compare_and_swap_rejects_active_action_drift(self) -> None:
        original = ApiHandler._load_action_queue_row

        def load_then_mutate(handler, action_id):
            row = original(handler, action_id)
            conn = open_store(str(self.db_path))
            try:
                conn.execute(
                    "UPDATE action_queue_items SET lane='proactive_today',required_facts_json='[]',"
                    "updated_at='2026-07-13T12:05:30+08:00' WHERE action_id=?",
                    (action_id,),
                )
                conn.commit()
            finally:
                conn.close()
            return row

        with mock.patch.object(ApiHandler, "_load_action_queue_row", new=load_then_mutate):
            status, payload = self.request(
                "POST", "/v1/action-queue/action_reply/feedback", {"outcome": "adopted"}
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "action_version_changed")
        conn = open_store(str(self.db_path), read_only=True)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM action_queue_feedback WHERE action_id='action_reply'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_runtime_staleness_blocks_reply_but_keeps_historical_proactive(self) -> None:
        conn = open_store(str(self.db_path))
        try:
            conn.execute(
                "UPDATE action_queue_runs SET as_of_at='2026-07-13T11:40:00+08:00' "
                "WHERE profile_id='aolai1' AND queue_date=?",
                (QUEUE_DATE,),
            )
            conn.commit()
        finally:
            conn.close()

        status, queue = self.request(
            "GET", "/v1/action-queue?profile=aolai1&date=2026-07-13&limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(queue["status"], "historical_snapshot_ready")
        self.assertEqual(queue["lanes"]["reply_now"], [])
        self.assertEqual(len(queue["lanes"]["proactive_today"]), 1)
        self.assertEqual(len(queue["lanes"]["suppressed"]), 2)
        self.assertIn("message_snapshot_stale", queue["block_reasons"])
        self.assertEqual(
            queue["lane_restrictions"]["reply_now"], ["message_snapshot_stale"]
        )
        self.assertFalse(queue["realtime_reply_available"])
        self.assertTrue(queue["contact_precheck_required"])
        proactive = queue["lanes"]["proactive_today"][0]
        self.assertEqual(proactive["data_mode"], "historical_snapshot")
        self.assertTrue(proactive["contact_precheck_required"])
        self.assertIn("historical_snapshot_only", proactive["reason_codes"])
        self.assertIsNotNone(proactive["snapshot_cutoff"])

        detail_status, detail = self.request("GET", "/v1/action-queue/action_reply")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["item"]["lane"], "suppressed")
        self.assertFalse(detail["item"]["send_allowed"])

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi"
        ) as call:
            draft_status, draft = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(draft_status, 200)
        self.assertEqual(draft["draft"]["fallback_for"], "action_suppressed")
        call.assert_not_called()

        proactive_detail_status, proactive_detail = self.request(
            "GET", "/v1/action-queue/action_proactive"
        )
        self.assertEqual(proactive_detail_status, 200)
        self.assertEqual(proactive_detail["item"]["lane"], "proactive_today")
        self.assertTrue(proactive_detail["item"]["contact_precheck_required"])

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi"
        ) as proactive_call:
            proactive_draft_status, proactive_draft = self.request(
                "POST", "/v1/action-queue/action_proactive/draft", {"facts": {}}
            )
        self.assertEqual(proactive_draft_status, 200)
        self.assertEqual(proactive_draft["draft"]["fallback_for"], "no_grounded_facts")
        proactive_call.assert_not_called()

        proactive_feedback_status, _ = self.request(
            "POST", "/v1/action-queue/action_proactive/feedback", {"outcome": "adopted"}
        )
        self.assertEqual(proactive_feedback_status, 201)

        feedback_status, feedback = self.request(
            "POST", "/v1/action-queue/action_reply/feedback", {"outcome": "adopted"}
        )
        self.assertEqual(feedback_status, 409)
        self.assertEqual(feedback["error"]["code"], "action_suppressed")

    def test_persisted_blocked_status_cannot_expose_active_rows(self) -> None:
        conn = open_store(str(self.db_path))
        try:
            conn.execute(
                "UPDATE action_queue_runs SET status='blocked',block_reasons_json=? "
                "WHERE profile_id='aolai1' AND queue_date=?",
                (json.dumps(["human_release_gate_closed"]), QUEUE_DATE),
            )
            conn.commit()
        finally:
            conn.close()

        status, queue = self.request(
            "GET", "/v1/action-queue?profile=aolai1&date=2026-07-13&limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(queue["status"], "blocked")
        self.assertEqual(queue["lanes"]["reply_now"], [])
        self.assertEqual(queue["lanes"]["proactive_today"], [])
        self.assertEqual(len(queue["lanes"]["suppressed"]), 3)

        detail_status, detail = self.request("GET", "/v1/action-queue/action_reply")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["item"]["lane"], "suppressed")

    def test_hallucinated_dynamic_claim_is_rejected_to_rule_fallback(self) -> None:
        hallucination = {
            "draft_text": "您好，这款有现货，价格只要99元，并保证明天送达。",
            "used_fact_codes": ["inventory", "current_price"],
        }
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", return_value=hallucination
        ):
            status, payload = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "有货", "current_price": "100元"}},
            )
        self.assertEqual(status, 200)
        self.assertFalse(payload["draft"]["model_used"])
        self.assertEqual(payload["draft"]["fallback_for"], "kimi_failed_or_rejected")
        self.assertNotIn("99", payload["draft"]["text"])

        private_status, private = self.request(
            "POST",
            "/v1/action-queue/action_reply/draft",
            {"facts": {"inventory": "有货，联系 13800138000", "current_price": "100元"}},
        )
        self.assertEqual(private_status, 400)
        self.assertEqual(private["error"]["code"], "private_fact_rejected")

    def test_ungrounded_qualitative_claim_is_rejected(self) -> None:
        hallucination = {
            "draft_text": "您好，这款质量很好，很适合您，我可以先帮您核对。",
            "used_fact_codes": ["inventory", "current_price"],
        }
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", return_value=hallucination
        ):
            status, payload = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "待确认", "current_price": "待确认"}},
            )
        self.assertEqual(status, 200)
        self.assertFalse(payload["draft"]["model_used"])
        self.assertNotIn("质量很好", payload["draft"]["text"])

    def test_feedback_only_accepts_review_states_and_updates_confirmation(self) -> None:
        missing_status, missing = self.request(
            "POST", "/v1/action-queue/action_reply/feedback", {"outcome": "edited"}
        )
        self.assertEqual(missing_status, 400)
        self.assertEqual(missing["error"]["code"], "missing_final_text")

        invalid_status, invalid = self.request(
            "POST", "/v1/action-queue/action_reply/feedback", {"outcome": "sent"}
        )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_outcome")

        status, payload = self.request(
            "POST",
            "/v1/action-queue/action_reply/feedback",
            {
                "outcome": "edited",
                "final_text": "人工修改后的安全回复",
                "reason_codes": ["tone_adjusted"],
                "reviewer": "unit-test",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["human_confirmation_state"], "edited")
        self.assertFalse(payload["send_allowed"])
        self.assertNotIn("final_text", payload)

        conn = sqlite3.connect(str(self.db_path))
        try:
            state = conn.execute(
                "SELECT human_confirmation_state,send_allowed FROM action_queue_items WHERE action_id='action_reply'"
            ).fetchone()
            feedback = conn.execute(
                "SELECT outcome,final_text,reviewer FROM action_queue_feedback WHERE action_id='action_reply'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(state, ("edited", 0))
        self.assertEqual(feedback, ("edited", "人工修改后的安全回复", "unit-test"))

        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ""}):
            draft_status, draft = self.request(
                "POST",
                "/v1/action-queue/action_reply/draft",
                {"facts": {"inventory": "待确认", "current_price": "待确认"}},
            )
        self.assertEqual(draft_status, 200)
        self.assertEqual(draft["human_confirmation_state"], "pending")
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT human_confirmation_state FROM action_queue_items "
                    "WHERE action_id='action_reply'"
                ).fetchone()[0],
                "pending",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
