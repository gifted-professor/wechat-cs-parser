from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from wechat_cs.identity import global_phone_hmac
from wechat_cs.review_portal import (
    _business_view,
    _ensure_opening_review_schema,
    create_server,
)
from wechat_cs.store import initialize_schema


HMAC_SECRET = "synthetic-review-hmac-secret-12345"
NOW = "2026-07-13T20:14:37+08:00"


class ReviewPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "portal.sqlite3"
        self.customer_data_path = root / "customer_action_data.json"
        self.hmac_secret_path = root / "hmac_secret"
        self.conversion_audit_dir = root / "conversion-audit"
        self.conversion_audit_dir.mkdir()
        self.hmac_secret_path.write_text(HMAC_SECRET, encoding="utf-8")
        self.customer_data_path.write_text(
            json.dumps(
                {
                    "generated_at": NOW,
                    "customers": [
                        {
                            "customer_name": "张三",
                            "phone": "13800138000",
                            "platform": "上海一店",
                        },
                        {
                            "customer_name": "李四",
                            "phone": "13900139000",
                            "platform": "杭州二店",
                        },
                        {
                            "customer_name": "王五",
                            "phone": "13700137000",
                            "platform": "南京三店",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        initialize_schema(conn)
        self._seed(conn)
        conn.close()
        self.conversion_audit_dir.joinpath("report.json").write_text(
            json.dumps(
                {
                    "audit_version": "conversion-attribution-audit-v1",
                    "requested_as_of": NOW,
                    "weights_trained": False,
                    "population": {
                        "contact_episode_count": 2,
                        "purchase_event_count": 3,
                        "repeat_purchase_event_count": 1,
                    },
                    "episode_sample_counts": {
                        "converted_7d": 1,
                        "non_converted_7d": 1,
                    },
                    "training_gate": {
                        "ready": False,
                        "weights_trained": False,
                        "manual_review_required": True,
                        "blockers": ["manual_sample_review_not_completed"],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        conversion_samples = [
            {
                "episode_id": "episode-price-review",
                "customer_key": "customer-1",
                "origin": "customer_initiated",
                "intent": "sales_inquiry",
                "explicit_price_barrier": "discount_request",
                "suspected_barrier": "quote_then_silence_suspected",
                "talk_track_primary": "price_quote",
                "talk_track_tags": ["price_quote"],
                "ended_on": "2026-07-03",
                "sample_state": "non_converted_7d",
                "purchase_event_ids": [],
                "repeat_90d": None,
                "eligible_for_sales_method": True,
            },
            {
                "episode_id": "episode-repeat-review",
                "customer_key": "customer-1",
                "origin": "customer_initiated",
                "intent": "sales_inquiry",
                "explicit_price_barrier": "none",
                "suspected_barrier": "none",
                "talk_track_primary": "product_recommendation",
                "talk_track_tags": ["product_recommendation"],
                "ended_on": "2026-07-03",
                "sample_state": "converted_7d",
                "purchase_event_ids": ["purchase-internal"],
                "repeat_90d": True,
                "eligible_for_sales_method": True,
            },
        ]
        self.conversion_audit_dir.joinpath("episode_samples.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False) + "\n"
                for item in conversion_samples
            ),
            encoding="utf-8",
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            db_path=self.db_path,
            run_id="sales-run",
            customer_data_path=self.customer_data_path,
            hmac_secret_path=self.hmac_secret_path,
            conversion_audit_dir=self.conversion_audit_dir,
            conversion_db_path=self.db_path,
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
    def _facts(*, blocked: bool, unknown_aftersales: bool = False) -> dict:
        phone_suffix = "blocked" if blocked else "eligible"
        base_orders = [
            {
                "order_line_id": f"order-line-{phone_suffix}-1",
                "ordered_at": "2026-05-01T00:00:00+08:00",
                "ordered_at_time_known": False,
                "paid_at": "2026-05-01T00:00:00+08:00",
                "paid_at_time_known": False,
                "paid_on": "2026-05-01",
                "revenue_minor": 26800 if not blocked else 88000,
                "currency": "CNY",
                "platform": "上海一店" if not blocked else "杭州二店",
                "sku_name": "棉质短袖 黑色 M" if not blocked else "外套 黑色 M",
                "factory": "内部厂家字段",
                "category": "上衣",
                "color": "黑色",
                "size": "M",
                "order_note": "熟客",
                "refund_type": "return_taro" if blocked else None,
                "refund_amount_minor": 88000 if blocked else None,
                "refund_on": "2026-05-04" if blocked else None,
                "refund_fact_at_cutoff": blocked,
                "quality_flags": ["internal_flag"],
            }
        ]
        if not blocked:
            base_orders.extend(
                [
                    {
                        "order_line_id": "order-line-eligible-2",
                        "ordered_at": "2026-06-01T00:00:00+08:00",
                        "ordered_at_time_known": False,
                        "paid_at": "2026-06-01T00:00:00+08:00",
                        "paid_at_time_known": False,
                        "paid_on": "2026-06-01",
                        "revenue_minor": 32000,
                        "currency": "CNY",
                        "platform": "上海一店",
                        "sku_name": "休闲裤",
                        "factory": "内部厂家字段",
                        "category": "裤装",
                        "color": "深蓝",
                        "size": "M",
                        "order_note": None,
                        "refund_type": "cancel",
                        "refund_amount_minor": 32000,
                        "refund_on": "2026-06-02",
                        "refund_fact_at_cutoff": True,
                        "quality_flags": [],
                    },
                    {
                        "order_line_id": "order-line-eligible-3",
                        "ordered_at": "2026-07-01T00:00:00+08:00",
                        "ordered_at_time_known": False,
                        "paid_at": "2026-07-01T00:00:00+08:00",
                        "paid_at_time_known": False,
                        "paid_on": "2026-07-01",
                        "revenue_minor": 41000,
                        "currency": "CNY",
                        "platform": "上海一店",
                        "sku_name": "运动套装",
                        "factory": "内部厂家字段",
                        "category": "套装",
                        "color": "灰色",
                        "size": "M",
                        "order_note": None,
                        "refund_type": None,
                        "refund_amount_minor": None,
                        "refund_on": None,
                        "refund_fact_at_cutoff": False,
                        "quality_flags": [],
                    },
                ]
            )
        total = sum(int(item["revenue_minor"]) for item in base_orders)
        return {
            "as_of_at": NOW,
            "contact_warning": "联系前核对最新状态",
            "customer_features": {
                "value_bucket": "high",
                "rfm_frequency": len(base_orders),
                "rfm_monetary_minor": total,
                "rfm_recency_days": 12,
                "median_repurchase_interval_days": 30 if not blocked else None,
                "recommended_contact_window": "18:00-24:00",
                "contact_window_evidence_count": 8,
                "median_reply_delay_seconds": 62,
                "unknown_aftersales_count": 1 if unknown_aftersales else 0,
                "preferred_skus": ["短袖"],
                "preferred_colors": ["黑色"],
                "preferred_sizes": ["M"],
                "order_rhythm": {
                    "preference_state": "insufficient_evidence",
                    "preferred_period": None,
                    "observation_count": 0,
                },
            },
            "member_facts": [
                {
                    "member_birthday": "1990-08-08",
                    "preferred_style": "休闲",
                    "expected_gift": "上衣",
                    "member_shop": "静安店" if not blocked else "西湖店",
                }
            ],
            "orders": base_orders,
            "factory_is_not_brand": True,
            "point_in_time_snapshots_frozen": True,
        }

    @classmethod
    def _seed(cls, conn: sqlite3.Connection) -> None:
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
            ("snapshot", "source-run", "wechat-live-inbox", "path", 1, 2, 3, 4, "a" * 64, 6, NOW, NOW, NOW, NOW, "stable", "{}"),
        )
        conn.execute(
            "INSERT INTO order_snapshots(order_snapshot_id,source_snapshot_id,synced_at,record_count,state,quality_json) "
            "VALUES(?,?,?,?,?,?)",
            ("orders-snapshot", "snapshot", NOW, 7, "active", "{}"),
        )
        conn.execute(
            "INSERT INTO sales_profile_runs(sales_profile_run_id,source_run_id,as_of_at,status,model,prompt_version,"
            "profile_schema_version,sampling_version,message_snapshot_id,order_snapshot_id,cohort_hash,config_json,counts_json,quality_json,"
            "created_at,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sales-run", "source-run", NOW, "complete", "kimi-for-coding", "events-v1", "card-v1", "sampling-v1", "snapshot", "orders-snapshot", "cohort", "{}", "{}", "{}", NOW, NOW, NOW),
        )
        fixtures = [
            ("1", "13800138000", "high_value", 1, False, False),
            ("2", "13900139000", "complex_risk", 1, True, False),
            ("3", "13700137000", "control", 1, False, True),
        ]
        for suffix, phone, stratum, rank, blocked, unknown_aftersales in fixtures:
            customer_id = f"customer-{suffix}"
            subject_id = f"subject-{suffix}"
            profile_id = f"sales-profile-{suffix}"
            conn.execute(
                "INSERT INTO customers(customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
                "summary,reasons_json,evidence_json,memory_json,source_file) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (customer_id, f"客户-{suffix}", NOW, 80, "high", "测试", "[]", "[]", "{}", "fixture"),
            )
            conn.execute(
                "INSERT INTO sales_profile_subjects(subject_id,sales_profile_run_id,customer_key,profile_id,phone_hmac,"
                "stratum,stratum_rank,selection_reason_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'{}',?,?,?)",
                (subject_id, "sales-run", customer_id, "account-1", global_phone_hmac(HMAC_SECRET, phone), stratum, rank, "succeeded", NOW, NOW),
            )
            card = {
                "customer_value": {"summary": "高价值客户", "facts": ["历史购买稳定"]},
                "time_rhythm": {"best_contact_time": "晚间联系"},
                "current_opportunity": {"summary": "适合自然回访"},
                "natural_opening": "晚上好，之前选的衣服穿着还合适吗？",
                "product_preferences": {"summary": "偏好舒适休闲款"},
                "purchase_drivers": ["舒适度"],
                "historical_commitments": ["晚点再来看看"],
                "contact_reason": "到了历史复购窗口",
                "risks": ["先确认上次体验"],
                "unknowns": [],
                "evidence": [],
            }
            conn.execute(
                "INSERT INTO sales_profiles(sales_profile_id,subject_id,status,input_hash,idempotency_key,model,prompt_version,"
                "profile_schema_version,card_version,deterministic_facts_json,profile_json,evidence_json,error_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (profile_id, subject_id, "succeeded", f"input-{suffix}", f"idem-{suffix}", "kimi-for-coding", "events-v1", "card-v1", f"card-version-{suffix}", json.dumps(cls._facts(blocked=blocked, unknown_aftersales=unknown_aftersales), ensure_ascii=False), json.dumps(card, ensure_ascii=False), "[]", "{}", NOW, NOW),
            )
        current_phone = global_phone_hmac(HMAC_SECRET, "13800138000")
        order_rows = [
            ("assoc-current", current_phone, "拉夫劳伦亚麻衬衫 白色 M", ""),
            ("assoc-buyer-a-anchor", "buyer-a", "拉夫劳伦亚麻款衬衫 绿色 S", ""),
            ("assoc-buyer-a-other", "buyer-a", "拉夫劳伦亚麻短裤 卡其色 S", ""),
            ("assoc-buyer-b-anchor", "buyer-b", "拉夫劳伦亚麻衬衫 蓝色 M", ""),
            ("assoc-buyer-b-other", "buyer-b", "拉夫劳伦亚麻短裤 白色 M", ""),
            ("assoc-buyer-c-anchor", "buyer-c", "拉夫劳伦亚麻衬衫 粉色 M", "return"),
            ("assoc-buyer-c-other", "buyer-c", "无效推荐商品 黑色 M", ""),
        ]
        conn.executemany(
            "INSERT INTO orders(order_line_id,order_snapshot_id,source_namespace,record_id,phone_hmac,paid_on,"
            "revenue_minor,currency,platform,refund_type,return_status,source_hash,quality_flags_json,sku_name) "
            "VALUES(?, 'orders-snapshot', 'fixture', ?, ?, '2026-07-01', 30000, 'CNY', '测试店', ?, '', ?, '[]', ?)",
            [
                (line_id, line_id, phone, refund_type, line_id + "-hash", sku)
                for line_id, phone, sku, refund_type in order_rows
            ],
        )
        conn.executemany(
            "INSERT INTO messages(message_key,customer_key,role,timestamp,text,source_file,source_ordinal) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                (
                    "message-c1-1",
                    "customer-1",
                    "customer",
                    "2026-07-01T09:00:00+08:00",
                    "电话 13800138000，想看看新款",
                    "events.jsonl",
                    1,
                ),
                (
                    "message-c1-2",
                    "customer-1",
                    "studio",
                    "2026-07-02T09:00:00+08:00",
                    "好的，我帮你留意",
                    "events.jsonl",
                    2,
                ),
                (
                    "message-c1-3",
                    "customer-1",
                    "customer",
                    "2026-07-03T09:00:00+08:00",
                    "想要黑色",
                    "events.jsonl",
                    3,
                ),
                (
                    "message-c1-4",
                    "customer-1",
                    "customer",
                    "2026-07-03T09:00:00+08:00",
                    "M 码优先",
                    "events.jsonl",
                    4,
                ),
                (
                    "message-c1-5",
                    "customer-1",
                    "customer",
                    "2026-07-03T09:00:00+08:00",
                    "过两天再来问问看，有活动也告诉我",
                    "events.jsonl",
                    5,
                ),
                (
                    "message-c1-future",
                    "customer-1",
                    "customer",
                    "2026-07-14T09:00:00+08:00",
                    "批次截止后才出现",
                    "events.jsonl",
                    6,
                ),
                (
                    "message-c1-after-snapshot",
                    "customer-1",
                    "customer",
                    "2026-07-04T09:00:00+08:00",
                    "快照行数之外",
                    "events.jsonl",
                    7,
                ),
                (
                    "message-c2-1",
                    "customer-2",
                    "customer",
                    "2026-07-03T08:00:00+08:00",
                    "这是另一位客户的消息",
                    "events.jsonl",
                    2,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO sales_profile_events(sales_profile_event_id,subject_id,chunk_index,event_type,event_json,"
            "evidence_json,confidence,validation_state,model,prompt_version,input_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("event-1", "subject-1", 0, "future_return", json.dumps({"summary": "有回访机会"}, ensure_ascii=False), json.dumps([{"kind": "message", "message_key": "message_abcdef", "quote": "电话 13800138000，晚点我再来"}], ensure_ascii=False), 0.9, "accepted", "kimi-for-coding", "events-v1", "event-input", NOW),
        )
        conn.commit()

    def request(self, method: str, path: str, payload=None, *, host: str = "127.0.0.1"):
        body = None
        headers = {"Accept": "application/json", "Host": host}
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
            "suggested_opening": "",
        }
        payload.update(overrides)
        return payload

    def test_no_access_code_and_host_boundary(self) -> None:
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("销售跟进工作台", page)
        self.assertIn("我来写一版", page)
        self.assertIn("这个客户画像或推荐还要怎么改", page)
        self.assertIn("历史订单", page)
        self.assertIn("客户跟进审核", page)
        self.assertIn("成交方法论审核", page)
        self.assertIn("两天后回访", page)
        self.assertIn("回访前准备", page)
        self.assertIn("历史相关性，不代表因果", page)
        self.assertNotIn("事实准确度", page)
        self.assertNotIn("访问码", page)
        status, payload = self.request("GET", "/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 3)
        status, payload = self.request("GET", "/api/summary", host="evil.example")
        self.assertEqual((status, payload["error"]["code"]), (403, "host_denied"))

    def test_conversion_review_api_is_separate_from_opening_reviews(self) -> None:
        status, summary = self.request("GET", "/api/conversion/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["method_sample_total"], 2)
        self.assertEqual(summary["reviewed"], 0)
        self.assertFalse(summary["weights_trained"])

        status, listing = self.request(
            "GET", "/api/conversion/samples?status=pending&signal=price_barrier"
        )
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["episode_id"], "episode-price-review")

        status, detail = self.request(
            "GET", "/api/conversion/samples/episode-price-review"
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["sample_state"], "non_converted_7d")
        self.assertTrue(detail["messages"])
        self.assertTrue(detail["followup_method"]["detected"])
        self.assertEqual(
            detail["followup_method"]["recommended_action"],
            "schedule_prepared_followup",
        )
        self.assertNotIn("customer-1", json.dumps(detail, ensure_ascii=False))

        with sqlite3.connect(str(self.db_path)) as conn:
            old_review_count = conn.execute(
                "SELECT COUNT(*) FROM sales_profile_opening_reviews"
            ).fetchone()[0]
        status, saved = self.request(
            "POST",
            "/api/conversion/samples/episode-price-review/review",
            {
                "audit_version": "conversion-attribution-audit-v1",
                "verdict": "approved",
                "corrected_origin": "",
                "corrected_intent": "",
                "corrected_explicit_price_barrier": "",
                "corrected_suspected_barrier": "",
                "corrected_talk_track_primary": "",
                "expected_followup_action": "schedule_prepared_followup",
                "followup_preparation_note": "核对活动、价格和客户上次顾虑。",
                "note": "标签和上下文一致。",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["review"]["verdict"], "approved")
        with sqlite3.connect(str(self.db_path)) as conn:
            new_review_count = conn.execute(
                "SELECT COUNT(*) FROM sales_profile_opening_reviews"
            ).fetchone()[0]
        self.assertEqual(new_review_count, old_review_count)
        status, summary = self.request("GET", "/api/conversion/summary")
        self.assertEqual((status, summary["reviewed"]), (200, 1))

    def test_internal_identity_business_metrics_and_enum_translation(self) -> None:
        status, listing = self.request("GET", "/api/profiles?promotion=all")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0]["label"], "张三")
        self.assertEqual(listing["items"][0]["phone_hint"], "138****8000")
        self.assertTrue(listing["items"][0]["promotion_eligible"])
        status, detail = self.request("GET", "/api/profiles/sales-profile-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["customer"]["name"], "张三")
        self.assertEqual(detail["customer"]["phone"], "13800138000")
        self.assertEqual(detail["customer"]["member_shop"], "静安店")
        self.assertEqual(detail["customer"]["last_order_channel"], "上海一店")
        self.assertEqual(detail["business"]["paid_order_count"], 2)
        self.assertEqual(detail["business"]["aftersales_count"], 0)
        self.assertEqual(detail["business"]["cancelled_count"], 1)
        self.assertEqual(detail["business"]["median_repurchase_interval_days"], 61.0)
        self.assertIn("暂无可信下单时段", detail["business"]["order_habit"])
        self.assertNotIn("休闲裤", detail["facts"]["preferred_products"])
        serialized_orders = json.dumps(detail["order_history"], ensure_ascii=False)
        self.assertEqual(detail["order_history"][-1]["product"], "棉质短袖 黑色 M")
        self.assertIn("订单取消（不计售后）", serialized_orders)
        self.assertNotIn("cancel", serialized_orders)
        self.assertNotIn("return_taro", serialized_orders)
        self.assertNotIn("order-line", serialized_orders)
        serialized_events = json.dumps(detail["events"], ensure_ascii=False)
        self.assertNotIn("13800138000", serialized_events)
        self.assertNotIn("message_abcdef", serialized_events)
        self.assertFalse(detail["send_allowed"])

    def test_future_return_signal_can_be_saved_as_a_prepared_followup_plan(self) -> None:
        status, detail = self.request("GET", "/api/profiles/sales-profile-1")
        self.assertEqual(status, 200)
        recommendation = detail["followup_recommendation"]
        self.assertTrue(recommendation["detected"])
        self.assertEqual(recommendation["recommended_action"], "schedule_prepared_followup")
        self.assertTrue(
            any("顾虑" in item for item in recommendation["preparation_checklist"])
        )

        status, saved = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(
                followup_status="scheduled",
                followup_due_on="2026-07-18",
                followup_reason="客户说过几天再来问问看",
                followup_preparation="核对最新活动和价格；准备符合历史偏好的两款商品。",
            ),
        )
        self.assertEqual(status, 200)
        review = saved["review"]
        self.assertEqual(review["followup_status"], "scheduled")
        self.assertEqual(review["followup_due_on"], "2026-07-18")
        self.assertIn("历史偏好", review["followup_preparation"])

        status, retried = self.request(
            "POST", "/api/profiles/sales-profile-1/review", self.valid_review()
        )
        self.assertEqual(status, 200)
        self.assertEqual(retried["review"]["followup_status"], "scheduled")
        self.assertEqual(retried["review"]["followup_due_on"], "2026-07-18")

        status, invalid = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(followup_status="scheduled", followup_due_on=""),
        )
        self.assertEqual(
            (status, invalid["error"]["code"]),
            (400, "missing_followup_due_on"),
        )

    def test_high_aftersales_customer_is_excluded_and_eligible_list_is_score_sorted(self) -> None:
        status, summary = self.request("GET", "/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(
            (summary["promotion_eligible"], summary["promotion_review"], summary["promotion_excluded"]),
            (1, 1, 1),
        )
        status, eligible = self.request("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertEqual([item["sales_profile_id"] for item in eligible["items"]], ["sales-profile-1"])
        status, excluded = self.request("GET", "/api/profiles?promotion=excluded")
        self.assertEqual(status, 200)
        self.assertEqual(excluded["items"][0]["sales_profile_id"], "sales-profile-2")
        self.assertFalse(excluded["items"][0]["promotion_eligible"])
        self.assertIn("100%", excluded["items"][0]["exclusion_reason"])
        status, needs_review = self.request("GET", "/api/profiles?promotion=review")
        self.assertEqual(status, 200)
        self.assertEqual(needs_review["items"][0]["sales_profile_id"], "sales-profile-3")
        self.assertEqual(needs_review["items"][0]["promotion_state"], "review")
        self.assertIn("售后事实待确认", needs_review["items"][0]["exclusion_reason"])
        status, all_profiles = self.request("GET", "/api/profiles?promotion=all")
        self.assertEqual(status, 200)
        self.assertTrue(all_profiles["items"][0]["promotion_eligible"])
        self.assertEqual(all_profiles["items"][1]["promotion_state"], "review")
        self.assertFalse(all_profiles["items"][-1]["promotion_eligible"])

    def test_more_than_one_year_since_last_payment_blocks_proactive_followup(self) -> None:
        def facts(paid_on: str) -> dict:
            return {
                "as_of_at": NOW,
                "customer_features": {},
                "orders": [
                    {
                        "paid_on": paid_on,
                        "paid_at": paid_on + "T00:00:00+08:00",
                        "revenue_minor": 600000,
                        "refund_type": None,
                        "refund_fact_at_cutoff": False,
                    }
                ],
            }

        boundary = _business_view(
            facts("2025-07-13"),
            contact_refusal=False,
            future_signal=True,
        )
        stale = _business_view(
            facts("2025-07-12"),
            contact_refusal=False,
            future_signal=True,
        )

        self.assertEqual(boundary["days_since_last_order"], 365)
        self.assertEqual(boundary["promotion_state"], "eligible")
        self.assertFalse(boundary["proactive_followup_blocked"])

        self.assertEqual(stale["days_since_last_order"], 366)
        self.assertEqual(stale["promotion_state"], "excluded")
        self.assertFalse(stale["promotion_eligible"])
        self.assertTrue(stale["proactive_followup_blocked"])
        self.assertEqual(stale["recency_penalty"], 60)
        self.assertEqual(
            stale["priority_score"],
            max(stale["priority_score_before_recency_penalty"] - 60, 0),
        )
        self.assertEqual(stale["priority_label"], "超过一年，不跟进")
        self.assertIn("超过 365 天", stale["exclusion_reason"])

    def test_opening_review_upsert_version_conflict_and_no_placeholder_scores(self) -> None:
        status, created = self.request("POST", "/api/profiles/sales-profile-1/review", self.valid_review())
        self.assertEqual(status, 200)
        review_id = created["review"]["review_id"]
        status, updated = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(verdict="edited", suggested_opening="晚上好，上次那件穿着还舒服吗？"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["review"]["review_id"], review_id)
        status, stale = self.request(
            "POST", "/api/profiles/sales-profile-1/review", self.valid_review(card_version="old")
        )
        self.assertEqual((status, stale["error"]["code"]), (409, "card_version_conflict"))
        conn = sqlite3.connect(str(self.db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM sales_profile_opening_reviews").fetchone()[0]
            legacy_count = conn.execute("SELECT COUNT(*) FROM sales_profile_reviews").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((count, legacy_count), (1, 0))

    def test_legacy_device_reviews_are_not_mixed_into_the_shared_team_verdict(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO sales_profile_opening_reviews(review_id,sales_profile_id,card_version,verdict,"
                "source_opening,suggested_opening,reviewer_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-device-review",
                    "sales-profile-1",
                    "card-version-1",
                    "rejected",
                    "旧开场",
                    "旧建议",
                    "operator-legacy-device",
                    NOW,
                    NOW,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        status, summary = self.request("GET", "/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual((summary["reviewed"], summary["verdicts"]), (0, {}))
        status, detail = self.request("GET", "/api/profiles/sales-profile-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["reviews"], [])

        status, _ = self.request("POST", "/api/profiles/sales-profile-1/review", self.valid_review())
        self.assertEqual(status, 200)
        status, detail = self.request("GET", "/api/profiles/sales-profile-1")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["reviews"]), 1)
        self.assertEqual(detail["reviews"][0]["verdict"], "approved")

    def test_edited_and_rejected_reviews_require_a_better_opening(self) -> None:
        for verdict in ("edited", "rejected"):
            status, payload = self.request(
                "POST",
                "/api/profiles/sales-profile-1/review",
                self.valid_review(verdict=verdict, suggested_opening=""),
            )
            self.assertEqual((status, payload["error"]["code"]), (400, "missing_opening_suggestion"))
        status, unchanged = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(
                verdict="edited",
                suggested_opening="晚上好，之前选的衣服穿着还合适吗？",
            ),
        )
        self.assertEqual((status, unchanged["error"]["code"]), (400, "unchanged_opening_suggestion"))
        status, sanitized = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(
                verdict="edited",
                suggested_opening=(
                    "张 三你好，请不要把 138-0013-8000 或 "
                    "310101-19900101-123X 写进训练反馈。"
                ),
            ),
        )
        self.assertEqual(status, 200)
        self.assertNotIn("张三", sanitized["review"]["suggested_opening"])
        self.assertNotIn("张 三", sanitized["review"]["suggested_opening"])
        self.assertNotIn("138-0013-8000", sanitized["review"]["suggested_opening"])
        self.assertNotIn("310101-19900101-123X", sanitized["review"]["suggested_opening"])
        conn = sqlite3.connect(str(self.db_path))
        try:
            stored_opening = conn.execute(
                "SELECT suggested_opening FROM sales_profile_opening_reviews "
                "WHERE sales_profile_id='sales-profile-1'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn("张 三", stored_opening)
        self.assertNotIn("138-0013-8000", stored_opening)
        self.assertNotIn("310101-19900101-123X", stored_opening)

    def test_message_snapshot_pagination_and_safe_fields(self) -> None:
        status, first = self.request(
            "GET", "/api/profiles/sales-profile-1/messages?limit=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(first["total"], 5)
        self.assertTrue(first["has_more"])
        self.assertTrue(first["next_cursor"])
        self.assertEqual(
            [item["message_ref"] for item in first["items"]],
            ["message-c1-4", "message-c1-5"],
        )
        self.assertEqual(
            set(first["items"][0]),
            {"message_ref", "role", "timestamp", "text"},
        )
        cursor = first["next_cursor"]
        status, second = self.request(
            "GET", f"/api/profiles/sales-profile-1/messages?limit=2&before={cursor}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["message_ref"] for item in second["items"]],
            ["message-c1-2", "message-c1-3"],
        )
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("source_file", serialized)
        self.assertNotIn("source_ordinal", serialized)
        self.assertNotIn("批次截止后才出现", serialized)
        self.assertNotIn("快照行数之外", serialized)

    def test_priority_revision_and_message_evidence_are_saved_and_legacy_retry_preserves_them(self) -> None:
        status, created = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(
                verdict="edited",
                suggested_opening="晚上好，最近到了适合你的休闲新款，要不要先看两件？",
                priority_assessment="too_low",
                priority_reason_code="clear_intent",
                priority_note="客户明确说过想看新款",
                evidence_message_ref="message-c1-5",
                revision_notes="张三不需要再问 13800138000 是否顺利收到，要直接给搭配建议。",
            ),
        )
        self.assertEqual(status, 200)
        review = created["review"]
        self.assertEqual(review["priority_assessment"], "too_low")
        self.assertEqual(review["evidence_message_ref"], "message-c1-5")
        self.assertNotIn("张三", review["revision_notes"])
        self.assertNotIn("13800138000", review["revision_notes"])

        status, retried = self.request(
            "POST", "/api/profiles/sales-profile-1/review", self.valid_review()
        )
        self.assertEqual(status, 200)
        preserved = retried["review"]
        self.assertEqual(preserved["priority_assessment"], "too_low")
        self.assertEqual(preserved["priority_reason_code"], "clear_intent")
        self.assertEqual(preserved["evidence_message_ref"], "message-c1-5")
        self.assertTrue(preserved["revision_notes"])

        status, missing_reason = self.request(
            "POST",
            "/api/profiles/sales-profile-1/review",
            self.valid_review(priority_assessment="not_suitable"),
        )
        self.assertEqual(
            (status, missing_reason["error"]["code"]),
            (400, "missing_priority_reason"),
        )

    def test_cross_sell_uses_only_valid_other_buyers(self) -> None:
        status, detail = self.request("GET", "/api/profiles/sales-profile-1")
        self.assertEqual(status, 200)
        cross_sell = detail["cross_sell"]
        self.assertTrue(cross_sell["available"])
        self.assertEqual(cross_sell["anchor_product"], "拉夫劳伦亚麻衬衫")
        self.assertEqual((cross_sell["buyer_count"], cross_sell["other_buyer_count"]), (3, 2))
        self.assertEqual(
            cross_sell["recommendations"][0],
            {"product": "拉夫劳伦亚麻短裤", "supporting_buyers": 2},
        )
        self.assertNotIn("无效推荐商品", json.dumps(cross_sell, ensure_ascii=False))

    def test_additive_review_schema_migration_preserves_legacy_row(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "legacy.sqlite3"
            conn = sqlite3.connect(str(path))
            conn.execute(
                "CREATE TABLE sales_profile_opening_reviews("
                "review_id TEXT PRIMARY KEY,sales_profile_id TEXT NOT NULL,card_version TEXT NOT NULL,"
                "verdict TEXT NOT NULL,source_opening TEXT NOT NULL,suggested_opening TEXT NOT NULL DEFAULT '',"
                "reviewer_key TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
                "UNIQUE(sales_profile_id,reviewer_key))"
            )
            legacy = (
                "legacy-review", "sales-profile-legacy", "card-v1", "rejected",
                "旧开场", "旧建议", "shared", NOW, NOW,
            )
            conn.execute(
                "INSERT INTO sales_profile_opening_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                legacy,
            )
            conn.commit()
            conn.close()

            _ensure_opening_review_schema(path)
            conn = sqlite3.connect(str(path))
            try:
                actual = conn.execute(
                    "SELECT review_id,sales_profile_id,card_version,verdict,source_opening,"
                    "suggested_opening,reviewer_key,created_at,updated_at "
                    "FROM sales_profile_opening_reviews"
                ).fetchone()
                additions = conn.execute(
                    "SELECT priority_assessment,priority_reason_code,priority_note,"
                    "evidence_message_ref,chat_snapshot_at,revision_notes,"
                    "followup_status,followup_due_on,followup_reason,followup_preparation "
                    "FROM sales_profile_opening_reviews"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(actual, legacy)
            self.assertEqual(additions, ("", "", "", "", "", "", "", "", "", ""))

    def test_no_model_or_send_route(self) -> None:
        for path in ("/api/profiles/sales-profile-1/send", "/api/run", "/v1/sales-profile-pilot"):
            status, payload = self.request("POST", path, {})
            self.assertIn(status, {404, 405})
            self.assertIn(payload["error"]["code"], {"not_found", "method_not_allowed"})


if __name__ == "__main__":
    unittest.main()
