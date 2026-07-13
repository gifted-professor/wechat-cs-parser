from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from wechat_cs.core import Message
from wechat_cs.customer_features import (
    ApprovedIdentityLink,
    build_customer_profiles,
    day_of_month_bucket,
)
from wechat_cs.orders import CanonicalOrder


def message(
    key: str,
    customer_key: str,
    role: str,
    timestamp: str,
    ordinal: int,
    text: str = "synthetic message",
) -> Message:
    return Message(
        message_key=key,
        customer_key=customer_key,
        role=role,
        timestamp=timestamp,
        text=text,
        source_file="synthetic/events.jsonl",
        source_ordinal=ordinal,
    )


def order(
    order_id: str,
    phone_hmac: str,
    paid_on: Optional[str],
    revenue_minor: Optional[int],
    *,
    refund_type: Optional[str] = None,
    refund_on: Optional[str] = None,
    quality_flags: tuple[str, ...] = (),
    sku_name: Optional[str] = None,
    factory: Optional[str] = None,
    category: Optional[str] = None,
    color: Optional[str] = None,
    size: Optional[str] = None,
    ordered_at: Optional[str] = None,
    paid_at: Optional[str] = None,
    order_note: Optional[str] = None,
) -> CanonicalOrder:
    return CanonicalOrder(
        order_line_id=order_id,
        source_namespace="synthetic",
        record_id="record-" + order_id,
        phone_hmac=phone_hmac,
        ordered_at=ordered_at,
        paid_at=paid_at,
        paid_on=paid_on,
        revenue_minor=revenue_minor,
        currency="CNY",
        platform=None,
        refund_type=refund_type,
        refund_reason=None,
        refund_amount_minor=None,
        refund_on=refund_on,
        return_status=None,
        source_hash="synthetic-source",
        quality_flags=quality_flags,
        sku_name=sku_name,
        factory=factory,
        category=category,
        color=color,
        size=size,
        order_note=order_note,
    )


AS_OF = datetime.fromisoformat("2026-07-13T20:00:00+08:00")
MESSAGE_OBSERVED = datetime.fromisoformat("2026-07-13T19:55:00+08:00")
ORDER_SYNCED = datetime.fromisoformat("2026-07-13T09:00:00+08:00")


def opaque_customer(number: int) -> str:
    return "customer_%024x" % number


def opaque_phone(number: int) -> str:
    return "phone_%024x" % number


class CustomerFeatureTests(unittest.TestCase):
    def build(self, links, orders=(), messages=(), **overrides):
        parameters = {
            "as_of_at": AS_OF,
            "message_observed_until": MESSAGE_OBSERVED,
            "order_synced_at": ORDER_SYNCED,
            "collector_status": "running",
        }
        parameters.update(overrides)
        return build_customer_profiles(links, orders, messages, **parameters)

    def test_day_of_month_bucket_boundaries(self) -> None:
        self.assertEqual(day_of_month_bucket("2026-07-01"), "early")
        self.assertEqual(day_of_month_bucket("2026-07-10"), "early")
        self.assertEqual(day_of_month_bucket("2026-07-11"), "mid")
        self.assertEqual(day_of_month_bucket("2026-07-20"), "mid")
        self.assertEqual(day_of_month_bucket("2026-07-21"), "late")
        self.assertEqual(day_of_month_bucket("2026-07-31"), "late")

    def test_point_in_time_rfm_excludes_same_day_future_and_duplicate_orders(self) -> None:
        phone_hmac = opaque_phone(1)
        baseline = self.build(
            [ApprovedIdentityLink(opaque_customer(1), phone_hmac)],
            [
                order("order-1", phone_hmac, "2026-07-01", 30_000),
                order("order-2", phone_hmac, "2026-07-05", 120_000),
                order("order-2", phone_hmac, "2026-07-05", 120_000),
            ],
        )
        with_future_rows = self.build(
            [ApprovedIdentityLink(opaque_customer(1), phone_hmac)],
            [
                order("order-1", phone_hmac, "2026-07-01", 30_000),
                order("order-2", phone_hmac, "2026-07-05", 120_000),
                order("order-2", phone_hmac, "2026-07-05", 120_000),
                order("same-day", phone_hmac, "2026-07-13", 900_000),
                order("future", phone_hmac, "2026-07-14", 900_000),
            ],
        )
        profile = with_future_rows.profiles[0]
        self.assertEqual(profile.rfm_frequency, 2)
        self.assertEqual(profile.rfm_monetary_minor, 150_000)
        self.assertEqual(profile.rfm_recency_days, 8)
        self.assertEqual(profile.value_bucket, "medium")
        self.assertEqual(profile.median_repurchase_interval_days, 4.0)
        self.assertEqual(with_future_rows, baseline)

    def test_product_preferences_use_historical_paid_orders_only(self) -> None:
        phone_hmac = opaque_phone(1)
        snapshot = self.build(
            [ApprovedIdentityLink(opaque_customer(1), phone_hmac)],
            [
                order(
                    "order-1",
                    phone_hmac,
                    "2026-07-01",
                    30_000,
                    sku_name="羊毛开衫",
                    factory="样衣工厂",
                    category="针织衫",
                    color="雾霾蓝",
                    size="M",
                ),
                order(
                    "order-2",
                    phone_hmac,
                    "2026-07-05",
                    40_000,
                    sku_name="羊毛开衫",
                    factory="样衣工厂",
                    category="针织衫",
                    color="米白",
                    size="M",
                ),
                order(
                    "same-day",
                    phone_hmac,
                    "2026-07-13",
                    90_000,
                    sku_name="不能泄漏的新款",
                    category="未来品类",
                ),
            ],
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.preferred_skus, ("羊毛开衫",))
        self.assertEqual(profile.preferred_factories, ("样衣工厂",))
        self.assertEqual(profile.preferred_categories, ("针织衫",))
        self.assertEqual(profile.preferred_colors, ("米白", "雾霾蓝"))
        self.assertEqual(profile.preferred_sizes, ("M",))
        self.assertNotIn("不能泄漏", json.dumps(asdict(snapshot), ensure_ascii=False))

    def test_phone_hmac_deduplicates_profiles_and_selects_latest_conversation(self) -> None:
        phone_hmac = opaque_phone(1)
        snapshot = self.build(
            [
                ApprovedIdentityLink(opaque_customer(1), phone_hmac),
                ApprovedIdentityLink(opaque_customer(2), phone_hmac),
                ApprovedIdentityLink(opaque_customer(3), opaque_phone(2), state="review"),
            ],
            [order("order-shared", phone_hmac, "2026-07-01", 50_000)],
            [
                message("m1", opaque_customer(1), "customer", "2026-07-10T10:00:00+08:00", 1),
                message("m2", opaque_customer(2), "customer", "2026-07-12T10:00:00+08:00", 2),
            ],
        )
        self.assertEqual(len(snapshot.profiles), 1)
        profile = snapshot.profiles[0]
        self.assertEqual(profile.customer_key, opaque_customer(2))
        self.assertEqual(profile.linked_customer_count, 2)
        self.assertEqual(profile.rfm_frequency, 1)
        self.assertEqual(snapshot.quality.approved_identity_link_count, 2)
        self.assertEqual(snapshot.quality.excluded_identity_link_count, 1)

    def test_contact_window_uses_only_customer_wechat_timestamps_in_shanghai(self) -> None:
        phone_hmac = opaque_phone(1)
        rows = [
            message("m1", opaque_customer(1), "customer", "2026-07-01T11:05:00Z", 1),
            message("m2", opaque_customer(1), "customer", "2026-07-02T19:15:00+08:00", 2),
            message("m3", opaque_customer(1), "customer", "2026-07-03T19:25:00+08:00", 3),
            message("m4", opaque_customer(1), "customer", "2026-07-04T20:00:00+08:00", 4),
            message("m5", opaque_customer(1), "customer", "2026-07-05T20:30:00+08:00", 5),
            message("studio", opaque_customer(1), "studio", "2026-07-06T10:00:00+08:00", 6),
            message("future", opaque_customer(1), "customer", "2026-07-14T19:00:00+08:00", 7),
        ]
        snapshot = self.build(
            [ApprovedIdentityLink(opaque_customer(1), phone_hmac)],
            messages=rows,
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.customer_message_count, 5)
        self.assertEqual(profile.active_day_count, 5)
        self.assertEqual(profile.active_hour_counts[19], 3)
        self.assertEqual(profile.active_hour_counts[20], 2)
        self.assertEqual(profile.active_hour_counts[10], 0)
        self.assertEqual(profile.recommended_contact_window, "18:00-22:00")
        self.assertEqual(profile.contact_window_basis, "wechat_customer_messages")
        self.assertGreaterEqual(profile.contact_window_confidence or 0, 0.99)
        self.assertTrue(snapshot.freshness.queue_ready)
        self.assertEqual(snapshot.quality.invalid_message_count, 0)

    def test_reply_rhythm_merges_turns_deduplicates_replies_and_prefers_reply_time(self) -> None:
        customer_key = opaque_customer(1)
        rows = []
        ordinal = 0
        # Five distinct service -> customer replies. Same-role messages within
        # 15 minutes are one turn and therefore one reply observation.
        pairs = (
            ("2026-07-01T08:00:00+08:00", "2026-07-01T09:00:00+08:00"),
            ("2026-07-02T08:00:00+08:00", "2026-07-02T09:30:00+08:00"),
            ("2026-07-03T18:00:00+08:00", "2026-07-03T19:00:00+08:00"),
            ("2026-07-04T18:00:00+08:00", "2026-07-04T20:00:00+08:00"),
            ("2026-07-05T18:00:00+08:00", "2026-07-05T20:30:00+08:00"),
        )
        for index, (studio_at, customer_at) in enumerate(pairs, start=1):
            ordinal += 1
            rows.append(message(f"s{index}a", customer_key, "studio", studio_at, ordinal))
            ordinal += 1
            studio_followup = (datetime.fromisoformat(studio_at) + timedelta(minutes=5)).isoformat()
            rows.append(message(f"s{index}b", customer_key, "studio", studio_followup, ordinal))
            ordinal += 1
            rows.append(message(f"c{index}a", customer_key, "customer", customer_at, ordinal))
            ordinal += 1
            customer_followup = (datetime.fromisoformat(customer_at) + timedelta(minutes=10)).isoformat()
            rows.append(message(f"c{index}b", customer_key, "customer", customer_followup, ordinal))
        # A customer message without a new studio turn is not another reply.
        rows.append(message("free-customer", customer_key, "customer", "2026-07-06T15:00:00+08:00", ordinal + 1))

        snapshot = self.build(
            [ApprovedIdentityLink(customer_key, opaque_phone(1))],
            messages=rows,
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.feature_rule_version, "customer-features-v2")
        self.assertEqual(profile.customer_reply_rhythm.observation_count, 5)
        self.assertEqual(profile.customer_reply_rhythm.hour_counts[9], 2)
        self.assertEqual(profile.customer_reply_rhythm.hour_counts[19], 1)
        self.assertEqual(profile.customer_reply_rhythm.hour_counts[20], 2)
        self.assertEqual(profile.customer_reply_rhythm.preferred_period, "evening")
        self.assertEqual(profile.customer_reply_rhythm.preference_state, "supported")
        self.assertAlmostEqual(profile.customer_reply_rhythm.confidence or 0, 0.6)
        self.assertEqual(profile.reply_delay_observation_count, 5)
        self.assertEqual(profile.median_reply_delay_seconds, 5_100.0)
        self.assertEqual(profile.recommended_contact_window, "18:00-22:00")
        self.assertEqual(profile.contact_window_basis, "wechat_customer_replies")

    def test_reply_after_seven_days_and_future_messages_do_not_enter_rhythm(self) -> None:
        customer_key = opaque_customer(1)
        snapshot = self.build(
            [ApprovedIdentityLink(customer_key, opaque_phone(1))],
            messages=[
                message("studio-old", customer_key, "studio", "2026-07-01T09:00:00+08:00", 1),
                message("customer-late", customer_key, "customer", "2026-07-09T09:00:01+08:00", 2),
                message("studio-future", customer_key, "studio", "2026-07-14T09:00:00+08:00", 3),
                message("customer-future", customer_key, "customer", "2026-07-14T10:00:00+08:00", 4),
            ],
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.customer_reply_rhythm.observation_count, 0)
        self.assertEqual(profile.customer_reply_rhythm.preference_state, "insufficient_evidence")
        self.assertIsNone(profile.median_reply_delay_seconds)
        self.assertEqual(profile.customer_message_rhythm.observation_count, 1)

    def test_message_and_order_month_buckets_and_exact_hours_are_point_in_time(self) -> None:
        customer_key = opaque_customer(1)
        phone_hmac = opaque_phone(1)
        orders = [
            order(
                f"order-{index}",
                phone_hmac,
                paid_at[:10],
                10_000,
                ordered_at=ordered_at,
                paid_at=paid_at,
            )
            for index, (ordered_at, paid_at) in enumerate(
                (
                    ("2026-06-01T08:00:00+08:00", "2026-06-01T09:00:00+08:00"),
                    ("2026-06-09T08:30:00+08:00", "2026-06-09T09:30:00+08:00"),
                    ("2026-06-15T08:00:00+08:00", "2026-06-15T21:00:00+08:00"),
                    ("2026-06-22T08:00:00+08:00", "2026-06-22T21:30:00+08:00"),
                    ("2026-07-13T19:00:00+08:00", "2026-07-13T19:30:00+08:00"),
                    # After the 20:00 cutoff and therefore excluded.
                    ("2026-07-13T20:30:00+08:00", "2026-07-13T20:45:00+08:00"),
                ),
                start=1,
            )
        ]
        snapshot = self.build(
            [ApprovedIdentityLink(customer_key, phone_hmac)],
            orders=orders,
            messages=[
                message("early", customer_key, "customer", "2026-07-05T07:00:00+08:00", 1),
                message("mid", customer_key, "customer", "2026-06-15T12:30:00+08:00", 2),
                message("late", customer_key, "customer", "2026-06-25T19:00:00+08:00", 3),
            ],
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.customer_message_rhythm.month_bucket_counts, (1, 1, 1))
        self.assertEqual(profile.order_rhythm.observation_count, 5)
        self.assertEqual(profile.order_rhythm.hour_counts[8], 4)
        self.assertEqual(profile.order_rhythm.hour_counts[19], 1)
        self.assertEqual(profile.order_rhythm.month_bucket_counts, (2, 2, 1))
        self.assertEqual(profile.order_rhythm.preferred_period, "morning")
        self.assertEqual(profile.payment_rhythm.observation_count, 5)
        self.assertEqual(profile.payment_rhythm.hour_counts[9], 2)
        self.assertEqual(profile.payment_rhythm.hour_counts[21], 2)
        self.assertEqual(profile.payment_rhythm.hour_counts[19], 1)
        self.assertEqual(profile.payment_rhythm.month_bucket_counts, (2, 2, 1))
        self.assertEqual(profile.rfm_frequency, 5)

    def test_zeroed_dashboard_clocks_never_become_midnight_preferences(self) -> None:
        customer_key = opaque_customer(1)
        phone_hmac = opaque_phone(1)
        rows = [
            order(
                "order-%d" % index,
                phone_hmac,
                paid_at[:10],
                10_000,
                ordered_at=ordered_at,
                paid_at=paid_at,
            )
            for index, (ordered_at, paid_at) in enumerate(
                (
                    ("2026-06-01T00:00:00+08:00", "2026-06-01T00:00:00+08:00"),
                    ("2026-06-05T00:00:00+08:00", "2026-06-05T00:00:00+08:00"),
                    ("2026-06-11T00:00:00+08:00", "2026-06-11T00:00:00+08:00"),
                    ("2026-06-18T00:00:00+08:00", "2026-06-18T00:00:00+08:00"),
                    ("2026-06-25T00:00:00+08:00", "2026-06-25T00:00:00+08:00"),
                    # Same calendar day as the cutoff: the unknown clock must
                    # not admit it into a historical point-in-time profile.
                    ("2026-07-13T00:00:00+08:00", "2026-07-13T00:00:00+08:00"),
                ),
                start=1,
            )
        ]
        profile = self.build(
            [ApprovedIdentityLink(customer_key, phone_hmac)],
            orders=rows,
            messages=[],
        ).profiles[0]
        self.assertEqual(profile.rfm_frequency, 5)
        self.assertEqual(profile.order_rhythm.observation_count, 0)
        self.assertEqual(profile.payment_rhythm.observation_count, 0)
        self.assertEqual(profile.order_rhythm.preference_state, "insufficient_evidence")
        self.assertEqual(profile.payment_rhythm.preference_state, "insufficient_evidence")
        self.assertIn("order_clock_time_unknown", profile.quality_flags)
        self.assertIn("payment_clock_time_unknown", profile.quality_flags)

    def test_insufficient_contact_evidence_returns_manual_business_hours(self) -> None:
        snapshot = self.build(
            [ApprovedIdentityLink(opaque_customer(1), opaque_phone(1))],
            messages=[
                message("m1", opaque_customer(1), "customer", "2026-07-11T09:00:00+08:00", 1),
                message("m2", opaque_customer(1), "customer", "2026-07-12T18:00:00+08:00", 2),
            ],
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.recommended_contact_window, "工作时段人工选择")
        self.assertEqual(profile.contact_window_basis, "insufficient_evidence")
        self.assertIsNone(profile.contact_window_confidence)

    def test_message_health_fails_closed_and_stale_orders_hide_order_features(self) -> None:
        snapshot = self.build(
            [ApprovedIdentityLink(opaque_customer(1), opaque_phone(1))],
            [order("order-1", opaque_phone(1), "2026-07-01", 500_000)],
            collector_status="stopped",
            message_observed_until=datetime.fromisoformat("2026-07-13T19:00:00+08:00"),
            order_synced_at=datetime.fromisoformat("2026-07-11T10:00:00+08:00"),
        )
        profile = snapshot.profiles[0]
        self.assertFalse(snapshot.freshness.messages_fresh)
        self.assertFalse(snapshot.freshness.orders_fresh)
        self.assertFalse(snapshot.freshness.queue_ready)
        self.assertFalse(profile.order_features_available)
        self.assertIsNone(profile.rfm_frequency)
        self.assertIsNone(profile.rfm_monetary_minor)
        self.assertEqual(profile.value_bucket, "unavailable")
        self.assertEqual(profile.aftersales_risk, "unavailable")
        self.assertIn("collector_unhealthy", snapshot.freshness.quality_flags)
        self.assertIn("message_snapshot_stale", snapshot.freshness.quality_flags)
        self.assertIn("order_snapshot_stale", snapshot.freshness.quality_flags)

    def test_aftersales_risk_is_point_in_time_and_ignores_same_day_status(self) -> None:
        phone_hmac = opaque_phone(1)
        snapshot = self.build(
            [ApprovedIdentityLink(opaque_customer(1), phone_hmac)],
            [
                order("return-before", phone_hmac, "2026-06-01", 100_000, refund_type="return", refund_on="2026-06-05"),
                order("return-same-day", phone_hmac, "2026-06-10", 100_000, refund_type="return", refund_on="2026-07-13"),
                order(
                    "open-current-status",
                    phone_hmac,
                    "2026-06-20",
                    100_000,
                    quality_flags=("aftersale_open",),
                ),
            ],
        )
        profile = snapshot.profiles[0]
        self.assertEqual(profile.aftersales_count, 1)
        self.assertAlmostEqual(profile.aftersales_rate or 0, 1 / 3)
        self.assertEqual(profile.aftersales_risk, "high")
        without_same_day_status = self.build(
            [ApprovedIdentityLink(opaque_customer(1), phone_hmac)],
            [
                order("return-before", phone_hmac, "2026-06-01", 100_000, refund_type="return", refund_on="2026-06-05"),
                order("return-same-day", phone_hmac, "2026-06-10", 100_000),
                order(
                    "open-current-status",
                    phone_hmac,
                    "2026-06-20",
                    100_000,
                    quality_flags=("aftersale_open",),
                ),
            ],
        )
        self.assertEqual(snapshot, without_same_day_status)

    def test_conflicting_approved_phone_links_are_excluded(self) -> None:
        snapshot = self.build(
            [
                ApprovedIdentityLink(opaque_customer(1), opaque_phone(1)),
                ApprovedIdentityLink(opaque_customer(1), opaque_phone(2)),
            ]
        )
        self.assertEqual(snapshot.profiles, ())
        self.assertEqual(snapshot.quality.identity_conflict_count, 1)
        self.assertEqual(snapshot.quality.approved_identity_link_count, 0)

    def test_later_order_snapshot_cannot_backfill_a_historical_profile(self) -> None:
        snapshot = self.build(
            [ApprovedIdentityLink(opaque_customer(1), opaque_phone(1))],
            [order("order-1", opaque_phone(1), "2026-07-01", 500_000)],
            order_synced_at=datetime.fromisoformat("2026-07-14T09:00:00+08:00"),
        )
        profile = snapshot.profiles[0]
        self.assertFalse(snapshot.freshness.orders_fresh)
        self.assertFalse(profile.order_features_available)
        self.assertIsNone(profile.rfm_frequency)
        self.assertEqual(profile.value_bucket, "unavailable")
        self.assertIn("order_snapshot_after_as_of", snapshot.freshness.quality_flags)

    def test_output_never_contains_phone_raw_id_name_or_message_text(self) -> None:
        raw_phone = "13800138000"
        raw_wechat_id = "wxid_private_synthetic"
        raw_name = "虚构张三"
        snapshot = self.build(
            [
                {
                    "customer_key": opaque_customer(1),
                    "phone_hmac": opaque_phone(1),
                    "state": "approved",
                    "phone": raw_phone,
                    "raw_wechat_id": raw_wechat_id,
                    "name": raw_name,
                }
            ],
            messages=[
                message(
                    "m1",
                    opaque_customer(1),
                    "customer",
                    "2026-07-12T10:00:00+08:00",
                    1,
                    text=f"{raw_name} {raw_phone} {raw_wechat_id}",
                )
            ],
        )
        serialized = json.dumps(asdict(snapshot), ensure_ascii=False)
        for unsafe in (raw_phone, raw_wechat_id, raw_name, opaque_phone(1), "synthetic message"):
            self.assertNotIn(unsafe, serialized)
        self.assertIn(opaque_customer(1), serialized)

        with self.assertRaisesRegex(ValueError, "opaque"):
            self.build(
                [
                    {
                        "customer_key": "customer_13800138000",
                        "phone_hmac": raw_phone,
                        "state": "approved",
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
