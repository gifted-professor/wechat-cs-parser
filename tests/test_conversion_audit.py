from __future__ import annotations

import unittest
from datetime import date, datetime

from wechat_cs.conversion_audit import (
    ContactEpisode,
    PurchaseEvent,
    _merge_purchase_event_sources,
    attribute_purchase_event,
    classify_actual_talk_track,
    classify_customer_signal,
)


def episode(identifier: str, ended_at: str, *, customer: str = "customer-a") -> ContactEpisode:
    moment = datetime.fromisoformat(ended_at)
    return ContactEpisode(
        customer_key=customer,
        episode_id=identifier,
        origin="customer_initiated",
        started_at=moment,
        ended_at=moment,
        intent="sales_inquiry",
        explicit_price_barrier="none",
        suspected_barrier="none",
        talk_track_tags=("price_quote",),
        talk_track_primary="price_quote",
        card_count=1,
    )


def purchase(paid_on: str) -> PurchaseEvent:
    return PurchaseEvent(
        purchase_event_id="purchase-a",
        phone_hmac="phone-a",
        paid_on=date.fromisoformat(paid_on),
        source_record_count=1,
        gross_revenue_minor=10000,
        net_revenue_minor=10000,
        quality_flags=(),
    )


class SignalClassificationTests(unittest.TestCase):
    def test_price_objection_is_high_intent_barrier_not_no_interest(self) -> None:
        result = classify_customer_signal("这个我想买，但是有点贵，预算不够")
        self.assertEqual(result["intent"], "sales_inquiry")
        self.assertEqual(result["explicit_price_barrier"], "explicit_price_objection")

    def test_aftersales_status_is_not_mislabeled_as_sales_inquiry(self) -> None:
        result = classify_customer_signal("请问快递单号什么时候能查到")
        self.assertEqual(result["intent"], "aftersales_or_order_status")

    def test_observed_reply_yields_multiple_talk_track_tags(self) -> None:
        tags = classify_actual_talk_track("活动到手价[金额]，这款很适合您")
        self.assertIn("price_quote", tags)
        self.assertIn("promotion_offer", tags)
        self.assertIn("product_recommendation", tags)


class PurchaseAttributionTests(unittest.TestCase):
    def test_unique_cross_day_episode_is_high_confidence_association(self) -> None:
        result = attribute_purchase_event(
            purchase("2026-07-03"),
            [episode("episode-a", "2026-07-01T10:00:00+08:00")],
            customer_key="customer-a",
            identity_verified=True,
        )
        self.assertEqual(result.attribution_state, "high_confidence_cross_day")
        self.assertTrue(result.eligible_for_method_learning)
        self.assertEqual(result.days_from_contact, 2)

    def test_same_day_date_only_order_stays_ambiguous(self) -> None:
        result = attribute_purchase_event(
            purchase("2026-07-01"),
            [episode("episode-a", "2026-07-01T10:00:00+08:00")],
            customer_key="customer-a",
            identity_verified=True,
        )
        self.assertEqual(result.attribution_state, "same_day_correlation")
        self.assertFalse(result.eligible_for_method_learning)

    def test_multiple_recent_episodes_are_competing_not_duplicated_successes(self) -> None:
        result = attribute_purchase_event(
            purchase("2026-07-05"),
            [
                episode("episode-a", "2026-07-01T10:00:00+08:00"),
                episode("episode-b", "2026-07-04T10:00:00+08:00"),
            ],
            customer_key="customer-a",
            identity_verified=True,
        )
        self.assertEqual(result.attribution_state, "competing_contact_episodes")
        self.assertEqual(result.episode_ids, ("episode-a", "episode-b"))

    def test_no_recent_contact_and_unverified_identity_are_distinct(self) -> None:
        no_contact = attribute_purchase_event(
            purchase("2026-07-10"),
            [episode("old", "2026-07-01T10:00:00+08:00")],
            customer_key="customer-a",
            identity_verified=True,
        )
        unverified = attribute_purchase_event(
            purchase("2026-07-10"),
            [],
            customer_key=None,
            identity_verified=False,
        )
        self.assertEqual(no_contact.attribution_state, "no_matching_contact")
        self.assertEqual(unverified.attribution_state, "identity_unverified")

    def test_history_and_current_events_merge_without_double_counting_repeat_dates(self) -> None:
        historical = purchase("2026-06-01")
        current_overlap = PurchaseEvent(
            purchase_event_id=historical.purchase_event_id,
            phone_hmac=historical.phone_hmac,
            paid_on=historical.paid_on,
            source_record_count=2,
            gross_revenue_minor=12000,
            net_revenue_minor=10000,
            quality_flags=(),
        )
        later = PurchaseEvent(
            purchase_event_id="purchase-b",
            phone_hmac=historical.phone_hmac,
            paid_on=date.fromisoformat("2026-06-20"),
            source_record_count=1,
            gross_revenue_minor=8000,
            net_revenue_minor=8000,
            quality_flags=(),
        )
        merged, metadata = _merge_purchase_event_sources(
            [current_overlap, later],
            [historical],
            observed_until=datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
        )

        self.assertEqual(len(merged), 2)
        self.assertEqual(metadata["cross_snapshot_overlap_event_count"], 1)
        first, second = merged
        self.assertTrue(first.repeat_30d)
        self.assertEqual(second.prior_purchase_count, 1)
        self.assertIn("cross_snapshot_overlap", first.quality_flags)


if __name__ == "__main__":
    unittest.main()
