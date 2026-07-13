from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime
from typing import Optional

from wechat_cs.cards import DecisionCard, to_blind_payload
from wechat_cs.orders import CanonicalOrder
from wechat_cs.outcomes import ConversationLink, attach_outcomes


def card(card_id: str, customer: str, as_of_at: str) -> DecisionCard:
    return DecisionCard(
        card_id=card_id,
        customer_key=customer,
        episode_id="episode-" + card_id,
        card_type="inbound",
        as_of_at=as_of_at,
        boundary_ordinal=1,
        boundary_message_key="trigger-" + card_id,
        source_snapshot_id="messages-snapshot",
        action_window_end="2026-07-02T10:00:00+08:00",
        observation_until="2026-07-03T10:00:00+08:00",
        blind_context=[{"role": "customer", "text": "虚构问题", "started_at": as_of_at, "ended_at": as_of_at}],
        observed_action={"state": "no_reply", "message_keys": [], "text": None, "reply_delay_seconds": None},
        context_message_keys=("trigger-" + card_id,),
        action_message_keys=(),
        split="train",
        rule_version="decision-card-v1",
    )


def link(
    customer: str,
    phone: Optional[str],
    *,
    state: str = "approved",
    eligibility: str = "order_customer",
) -> ConversationLink:
    return ConversationLink(
        customer_key=customer,
        phone_hmac=phone,
        state=state,
        eligibility=eligibility,
    )


def order(
    order_id: str,
    phone: str,
    paid_on: str,
    *,
    revenue_minor: int = 10000,
    refund_type: Optional[str] = None,
    refund_amount_minor: Optional[int] = None,
    refund_on: Optional[str] = None,
    return_status: Optional[str] = None,
    quality_flags: tuple[str, ...] = (),
) -> CanonicalOrder:
    return CanonicalOrder(
        order_line_id=order_id,
        source_namespace="fixture-orders",
        record_id="record-" + order_id,
        phone_hmac=phone,
        paid_on=paid_on,
        revenue_minor=revenue_minor,
        currency="CNY",
        platform="虚构平台",
        refund_type=refund_type,
        refund_reason=None,
        refund_amount_minor=refund_amount_minor,
        refund_on=refund_on,
        return_status=return_status,
        source_hash="fixture-source-hash",
        quality_flags=quality_flags,
    )


def payload_hash(cards: list[DecisionCard]) -> str:
    payload = json.dumps(
        [to_blind_payload(item) for item in cards],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PaymentWindowTests(unittest.TestCase):
    def test_calendar_day_windows_are_tri_state_and_use_shanghai_end_of_day(self) -> None:
        cards = [card("card-a", "customer-a", "2026-07-01T18:00:00+08:00")]
        results = attach_outcomes(
            cards,
            [link("customer-a", "phone-a")],
            [order("order-day-3", "phone-a", "2026-07-04")],
            orders_observed_until=datetime.fromisoformat("2026-07-05T00:00:00+08:00"),
        )

        result = results["card-a"]
        self.assertIs(result.paid_1d, False)
        self.assertIs(result.paid_3d, True)
        self.assertIs(result.paid_7d, True)
        self.assertIsNone(result.retained_30d)
        self.assertEqual(result.attribution_state, "associated")

    def test_day_zero_one_three_seven_are_inclusive_and_day_eight_is_excluded(self) -> None:
        cards = [
            card("day-0", "customer-0", "2026-07-01T23:59:00+08:00"),
            card("day-1", "customer-1", "2026-07-01T10:00:00+08:00"),
            card("day-3", "customer-3", "2026-07-01T10:00:00+08:00"),
            card("day-7", "customer-7", "2026-07-01T10:00:00+08:00"),
            card("day-8", "customer-8", "2026-07-01T10:00:00+08:00"),
        ]
        links = [link(item.customer_key, "phone-" + item.customer_key) for item in cards]
        orders = [
            order("o0", "phone-customer-0", "2026-07-01"),
            order("o1", "phone-customer-1", "2026-07-02"),
            order("o3", "phone-customer-3", "2026-07-04"),
            order("o7", "phone-customer-7", "2026-07-08"),
            order("o8", "phone-customer-8", "2026-07-09"),
        ]

        results = attach_outcomes(
            cards,
            links,
            orders,
            orders_observed_until=datetime.fromisoformat("2026-07-10T00:00:00+08:00"),
        )

        self.assertIs(results["day-0"].paid_1d, True)
        self.assertIn("same_day", results["day-0"].attribution_flags)
        self.assertIs(results["day-1"].paid_1d, True)
        self.assertIs(results["day-3"].paid_3d, True)
        self.assertIs(results["day-7"].paid_7d, True)
        self.assertIs(results["day-8"].paid_7d, False)
        self.assertEqual(results["day-8"].matched_orders, ())

    def test_absence_is_unknown_until_the_entire_window_is_observed(self) -> None:
        cards = [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")]
        result = attach_outcomes(
            cards,
            [link("customer-a", "phone-a")],
            [],
            orders_observed_until=datetime.fromisoformat("2026-07-04T12:00:00+08:00"),
        )["card-a"]

        self.assertIs(result.paid_1d, False)
        self.assertIsNone(result.paid_3d)
        self.assertIsNone(result.paid_7d)
        self.assertEqual(result.attribution_state, "none")


class AttributionTests(unittest.TestCase):
    def test_only_approved_order_eligible_identity_links_can_attach(self) -> None:
        cards = [
            card("review", "customer-review", "2026-07-01T10:00:00+08:00"),
            card("ineligible", "customer-ineligible", "2026-07-01T10:00:00+08:00"),
            card("missing", "customer-missing", "2026-07-01T10:00:00+08:00"),
        ]
        results = attach_outcomes(
            cards,
            [
                link("customer-review", "phone-a", state="review"),
                link("customer-ineligible", "phone-b", eligibility="order_ineligible"),
                link("customer-missing", None),
            ],
            [
                order("order-a", "phone-a", "2026-07-02"),
                order("order-b", "phone-b", "2026-07-02"),
            ],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )

        for result in results.values():
            self.assertEqual(result.attribution_state, "identity_unverified")
            self.assertIsNone(result.paid_1d)
            self.assertIsNone(result.retained_30d)
            self.assertEqual(result.matched_orders, ())

    def test_two_distinct_approved_phone_links_for_one_customer_are_unverified(self) -> None:
        result = attach_outcomes(
            [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")],
            [link("customer-a", "phone-a"), link("customer-a", "phone-b")],
            [order("order-a", "phone-a", "2026-07-02")],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )["card-a"]

        self.assertEqual(result.attribution_state, "identity_unverified")
        self.assertIsNone(result.paid_1d)

    def test_same_order_across_shared_phone_cards_is_ambiguous_not_double_rewarded(self) -> None:
        cards = [
            card("card-a", "customer-a", "2026-07-01T10:00:00+08:00"),
            card("card-b", "customer-b", "2026-07-01T11:00:00+08:00"),
        ]
        results = attach_outcomes(
            cards,
            [link("customer-a", "shared-phone"), link("customer-b", "shared-phone")],
            [order("shared-order", "shared-phone", "2026-07-01")],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )

        for result in results.values():
            self.assertIs(result.paid_1d, True)
            self.assertEqual(result.attribution_state, "ambiguous")
            self.assertEqual(
                set(result.attribution_flags),
                {"same_day", "multiple_cards", "shared_phone_multiple_conversations"},
            )
            self.assertEqual(result.matched_orders, ("shared-order",))
        self.assertFalse(any(item.attribution_state == "associated" for item in results.values()))

    def test_multiple_orders_is_an_independent_ambiguity_flag(self) -> None:
        result = attach_outcomes(
            [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")],
            [link("customer-a", "phone-a")],
            [
                order("order-a", "phone-a", "2026-07-02"),
                order("order-b", "phone-a", "2026-07-03"),
            ],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )["card-a"]

        self.assertEqual(result.attribution_state, "ambiguous")
        self.assertEqual(result.attribution_flags, ("multiple_orders",))
        self.assertEqual(result.matched_orders, ("order-a", "order-b"))

    def test_duplicate_order_line_is_deduplicated_before_attribution(self) -> None:
        duplicate = order("order-a", "phone-a", "2026-07-02")
        result = attach_outcomes(
            [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")],
            [link("customer-a", "phone-a")],
            [duplicate, duplicate],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )["card-a"]

        self.assertEqual(result.attribution_state, "associated")
        self.assertEqual(result.attribution_flags, ())
        self.assertEqual(result.matched_orders, ("order-a",))


class ThirtyDayOutcomeTests(unittest.TestCase):
    def test_normal_full_return_partial_return_exchange_and_compensation_semantics(self) -> None:
        cards = [
            card("normal", "normal", "2026-07-01T10:00:00+08:00"),
            card("cancel", "cancel", "2026-07-01T10:00:00+08:00"),
            card("full", "full", "2026-07-01T10:00:00+08:00"),
            card("partial", "partial", "2026-07-01T10:00:00+08:00"),
            card("exchange", "exchange", "2026-07-01T10:00:00+08:00"),
            card("compensation", "compensation", "2026-07-01T10:00:00+08:00"),
        ]
        links = [link(item.customer_key, "phone-" + item.customer_key) for item in cards]
        orders = [
            order("o-normal", "phone-normal", "2026-07-02"),
            order(
                "o-cancel",
                "phone-cancel",
                "2026-07-02",
                refund_type="cancel",
                refund_amount_minor=10000,
                refund_on="2026-07-03",
                return_status="close",
            ),
            order(
                "o-full",
                "phone-full",
                "2026-07-02",
                refund_type="return",
                refund_amount_minor=10000,
                refund_on="2026-07-03",
                return_status="close",
            ),
            order(
                "o-partial",
                "phone-partial",
                "2026-07-02",
                refund_type="return",
                refund_amount_minor=4000,
                refund_on="2026-07-03",
                return_status="close",
            ),
            order(
                "o-exchange",
                "phone-exchange",
                "2026-07-02",
                refund_type="exchange",
                return_status="close",
            ),
            order(
                "o-compensation",
                "phone-compensation",
                "2026-07-02",
                refund_type="compensation",
                return_status="close",
            ),
        ]

        results = attach_outcomes(
            cards,
            links,
            orders,
            orders_observed_until=datetime.fromisoformat("2026-08-02T00:00:00+08:00"),
        )

        self.assertIs(results["normal"].retained_30d, True)
        self.assertIs(results["normal"].aftersale_30d, False)
        self.assertIs(results["cancel"].retained_30d, False)
        self.assertIs(results["cancel"].aftersale_30d, True)
        self.assertIs(results["full"].retained_30d, False)
        self.assertIs(results["full"].aftersale_30d, True)
        self.assertEqual(results["full"].refund_loss_ratio, 1.0)
        self.assertIs(results["partial"].retained_30d, True)
        self.assertEqual(results["partial"].refund_loss_ratio, 0.4)
        self.assertIs(results["exchange"].exchange_30d, True)
        self.assertIs(results["exchange"].retained_30d, True)
        self.assertIs(results["compensation"].compensation_30d, True)
        self.assertIs(results["compensation"].retained_30d, True)

    def test_missing_return_facts_are_quality_unknown(self) -> None:
        result = attach_outcomes(
            [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")],
            [link("customer-a", "phone-a")],
            [
                order(
                    "order-a",
                    "phone-a",
                    "2026-07-02",
                    refund_type="return",
                    quality_flags=("missing_refund_on", "missing_refund_amount"),
                )
            ],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )["card-a"]

        self.assertIs(result.paid_1d, True)
        self.assertIsNone(result.retained_30d)
        self.assertIsNone(result.aftersale_30d)
        self.assertIsNone(result.refund_loss_ratio)
        self.assertEqual(result.attribution_state, "quality_unknown")

    def test_unfinished_thirty_day_window_never_writes_false_absence(self) -> None:
        cards = [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")]
        result = attach_outcomes(
            cards,
            [link("customer-a", "phone-a")],
            [order("order-a", "phone-a", "2026-07-02")],
            orders_observed_until=datetime.fromisoformat("2026-07-20T00:00:00+08:00"),
        )["card-a"]

        self.assertIsNone(result.retained_30d)
        self.assertIsNone(result.aftersale_30d)
        self.assertIsNone(result.exchange_30d)
        self.assertIsNone(result.compensation_30d)

    def test_refund_after_day_thirty_does_not_rewrite_the_thirty_day_result(self) -> None:
        result = attach_outcomes(
            [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")],
            [link("customer-a", "phone-a")],
            [
                order(
                    "order-a",
                    "phone-a",
                    "2026-07-02",
                    refund_type="return",
                    refund_amount_minor=10000,
                    refund_on="2026-08-05",
                    return_status="close",
                )
            ],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )["card-a"]

        self.assertIs(result.retained_30d, True)
        self.assertIs(result.aftersale_30d, False)
        self.assertEqual(result.refund_loss_ratio, 0.0)

    def test_outcome_attachment_never_mutates_the_blind_payload(self) -> None:
        cards = [card("card-a", "customer-a", "2026-07-01T10:00:00+08:00")]
        before = payload_hash(cards)
        attach_outcomes(
            cards,
            [link("customer-a", "phone-a")],
            [order("order-a", "phone-a", "2026-07-02")],
            orders_observed_until=datetime.fromisoformat("2026-08-10T00:00:00+08:00"),
        )
        self.assertEqual(before, payload_hash(cards))


if __name__ == "__main__":
    unittest.main()
