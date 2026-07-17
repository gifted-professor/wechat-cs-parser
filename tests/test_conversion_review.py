from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_cs.conversion_review import ConversionReviewService, ConversionReviewValidationError


class ConversionReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.audit_dir = root / "audit"
        self.audit_dir.mkdir()
        self.message_db = root / "messages.sqlite3"
        self.audit_dir.joinpath("report.json").write_text(
            json.dumps(
                {
                    "audit_version": "conversion-attribution-audit-v1",
                    "requested_as_of": "2026-07-15T11:50:00+08:00",
                    "weights_trained": False,
                    "population": {
                        "contact_episode_count": 5,
                        "purchase_event_count": 8,
                        "repeat_purchase_event_count": 3,
                    },
                    "episode_sample_counts": {
                        "converted_7d": 2,
                        "non_converted_7d": 2,
                        "identity_unverified": 1,
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
        samples = [
            self.sample(
                "episode-price-loss",
                "customer-price",
                sample_state="non_converted_7d",
                explicit_price_barrier="explicit_price_objection",
                talk_track_primary="price_quote",
            ),
            self.sample(
                "episode-quote-silence",
                "customer-silence",
                sample_state="non_converted_7d",
                suspected_barrier="quote_then_silence_suspected",
                talk_track_primary="price_quote",
            ),
            self.sample(
                "episode-converted-repeat",
                "customer-repeat",
                sample_state="converted_7d",
                repeat_90d=True,
                talk_track_primary="product_recommendation",
            ),
            self.sample(
                "episode-studio",
                "customer-studio",
                sample_state="converted_7d",
                origin="studio_initiated",
                intent="general_or_unknown",
            ),
            self.sample(
                "episode-ineligible",
                "customer-hidden",
                sample_state="identity_unverified",
                eligible=False,
            ),
        ]
        self.audit_dir.joinpath("episode_samples.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in samples),
            encoding="utf-8",
        )
        conn = sqlite3.connect(str(self.message_db))
        conn.execute(
            "CREATE TABLE messages(message_key TEXT PRIMARY KEY,customer_key TEXT,role TEXT,timestamp TEXT,text TEXT,source_ordinal INTEGER)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?)",
            [
                (
                    "message-1",
                    "customer-price",
                    "customer",
                    "2026-07-10T09:00:00+08:00",
                    "这个有点贵，我过几天再来问问看，电话 13800138000",
                    1,
                ),
                (
                    "message-2",
                    "customer-price",
                    "studio",
                    "2026-07-10T09:01:00+08:00",
                    "现在价格是 299 元",
                    2,
                ),
                (
                    "message-old",
                    "customer-price",
                    "customer",
                    "2026-07-01T09:00:00+08:00",
                    "太早的消息",
                    3,
                ),
                (
                    "message-other",
                    "customer-other",
                    "customer",
                    "2026-07-10T09:00:00+08:00",
                    "其他客户",
                    4,
                ),
            ],
        )
        conn.commit()
        conn.close()
        self.service = ConversionReviewService(
            self.audit_dir,
            self.message_db,
            cleaner=lambda value: str(value).replace("13800138000", "[手机号已隐藏]"),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def sample(
        episode_id: str,
        customer_key: str,
        *,
        sample_state: str,
        origin: str = "customer_initiated",
        intent: str = "sales_inquiry",
        explicit_price_barrier: str = "none",
        suspected_barrier: str = "none",
        talk_track_primary: str = "other_observed_reply",
        repeat_90d=None,
        eligible: bool = True,
    ) -> dict:
        return {
            "episode_id": episode_id,
            "customer_key": customer_key,
            "origin": origin,
            "intent": intent,
            "explicit_price_barrier": explicit_price_barrier,
            "suspected_barrier": suspected_barrier,
            "talk_track_primary": talk_track_primary,
            "talk_track_tags": [talk_track_primary],
            "ended_on": "2026-07-12",
            "sample_state": sample_state,
            "purchase_event_ids": ["purchase-internal"] if sample_state == "converted_7d" else [],
            "repeat_90d": repeat_90d,
            "eligible_for_sales_method": eligible,
        }

    def test_summary_and_default_queue_only_expose_method_eligible_samples(self) -> None:
        summary = self.service.summary()
        self.assertEqual(summary["method_sample_total"], 4)
        self.assertEqual(summary["reviewed"], 0)
        self.assertFalse(summary["weights_trained"])
        listing = self.service.list_samples()
        self.assertEqual(listing["total"], 4)
        self.assertEqual(listing["items"][0]["episode_id"], "episode-price-loss")
        serialized = json.dumps(listing, ensure_ascii=False)
        self.assertNotIn("customer-price", serialized)
        self.assertNotIn("purchase-internal", serialized)
        self.assertNotIn("episode-ineligible", serialized)

    def test_filters_cover_positive_negative_price_silence_and_repeat_signals(self) -> None:
        converted = self.service.list_samples(sample_state="converted_7d")
        self.assertEqual(converted["total"], 2)
        price = self.service.list_samples(signal="price_barrier")
        self.assertEqual([item["episode_id"] for item in price["items"]], ["episode-price-loss"])
        silence = self.service.list_samples(signal="quote_silence")
        self.assertEqual(
            [item["episode_id"] for item in silence["items"]],
            ["episode-quote-silence"],
        )
        repeat = self.service.list_samples(signal="repeat_90d")
        self.assertEqual(
            [item["episode_id"] for item in repeat["items"]],
            ["episode-converted-repeat"],
        )

    def test_detail_returns_only_nearby_sanitized_chat_and_saved_review(self) -> None:
        detail = self.service.detail("episode-price-loss")
        self.assertEqual(len(detail["messages"]), 2)
        self.assertIn("[手机号已隐藏]", detail["messages"][0]["text"])
        self.assertTrue(detail["followup_method"]["detected"])
        self.assertEqual(
            detail["followup_method"]["recommended_action"],
            "schedule_prepared_followup",
        )
        self.assertIn("最新活动和价格", detail["followup_method"]["preparation_checklist"])
        self.assertNotIn("customer-price", json.dumps(detail, ensure_ascii=False))
        review = self.service.save_review(
            "episode-price-loss",
            {
                "audit_version": "conversion-attribution-audit-v1",
                "verdict": "corrected",
                "corrected_origin": "customer_initiated",
                "corrected_intent": "sales_inquiry",
                "corrected_explicit_price_barrier": "discount_request",
                "corrected_suspected_barrier": "none",
                "corrected_talk_track_primary": "price_quote",
                "expected_followup_action": "schedule_prepared_followup",
                "followup_preparation_note": "先核对最新活动、价格和上次嫌贵的顾虑。",
                "note": "客户问优惠，不是明确说太贵。",
            },
        )
        self.assertEqual(review["verdict"], "corrected")
        stored = self.service.detail("episode-price-loss")["review"]
        self.assertEqual(stored["corrected_explicit_price_barrier"], "discount_request")
        self.assertEqual(stored["expected_followup_action"], "schedule_prepared_followup")
        self.assertIn("最新活动", stored["followup_preparation_note"])
        self.assertEqual(self.service.summary()["reviewed"], 1)

    def test_rejects_stale_versions_and_empty_corrections(self) -> None:
        with self.assertRaises(ConversionReviewValidationError):
            self.service.save_review(
                "episode-price-loss",
                {"audit_version": "old", "verdict": "approved", "note": ""},
            )
        with self.assertRaises(ConversionReviewValidationError):
            self.service.save_review(
                "episode-price-loss",
                {
                    "audit_version": "conversion-attribution-audit-v1",
                    "verdict": "corrected",
                    "corrected_origin": "",
                    "corrected_intent": "",
                    "corrected_explicit_price_barrier": "",
                    "corrected_suspected_barrier": "",
                    "corrected_talk_track_primary": "",
                    "note": "",
                },
            )


if __name__ == "__main__":
    unittest.main()
