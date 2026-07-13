from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from wechat_cs.action_queue import (
    POLICY_VERSION,
    ActionCandidate,
    QueueContext,
    build_action_queue,
)


UTC8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC8)


def opaque_customer(number: int) -> str:
    return "customer_%024x" % number


def opaque_phone(number: int) -> str:
    return "phone_%024x" % number


def context(**overrides: object) -> QueueContext:
    values = {
        "profile_id": "aolai1",
        "queue_date": date(2026, 7, 13),
        "as_of_at": NOW,
        "message_snapshot_at": NOW - timedelta(minutes=5),
        "message_status": "running",
        "order_snapshot_at": NOW - timedelta(hours=2),
    }
    values.update(overrides)
    return QueueContext(**values)


def candidate(number: int, **overrides: object) -> ActionCandidate:
    values = {
        "customer_key": opaque_customer(number),
        "profile_id": "aolai1",
        "phone_hmac": opaque_phone(number),
        "facts_sufficient": True,
    }
    values.update(overrides)
    return ActionCandidate(**values)


def flatten(queue: dict) -> list[dict]:
    return [
        item
        for lane in ("reply_now", "proactive_today", "suppressed")
        for item in queue["lanes"][lane]
    ]


class ActionQueueTests(unittest.TestCase):
    def test_unhealthy_or_stale_messages_block_reply_only_and_keep_historical_proactive(self) -> None:
        rows = [
            candidate(1, unresolved_inbound=True),
            candidate(2, proactive_eligible=True, repurchase_score=90),
        ]
        stale = build_action_queue(
            context(message_snapshot_at=NOW - timedelta(minutes=15, seconds=1)), rows
        )
        self.assertEqual(stale["status"], "ready")
        self.assertEqual(stale["block_reasons"], [])
        self.assertEqual(
            stale["lane_restrictions"]["reply_now"], ["message_snapshot_stale"]
        )
        self.assertEqual(stale["counts"], {"reply_now": 0, "proactive_today": 1, "suppressed": 1})
        self.assertIn(
            "message_snapshot_stale",
            stale["lanes"]["suppressed"][0]["reason_codes"],
        )
        historical = stale["lanes"]["proactive_today"][0]
        self.assertEqual(historical["data_mode"], "historical_snapshot")
        self.assertTrue(historical["contact_precheck_required"])
        self.assertFalse(historical["realtime_reply_available"])
        self.assertIn("historical_snapshot_only", historical["reason_codes"])
        self.assertEqual(
            historical["snapshot_cutoff"],
            (NOW - timedelta(minutes=15, seconds=1)).isoformat(),
        )

        stopped = build_action_queue(context(message_status="stopped"), rows)
        self.assertEqual(stopped["status"], "ready")
        self.assertEqual(stopped["block_reasons"], [])
        self.assertEqual(
            stopped["lane_restrictions"]["reply_now"],
            ["message_collection_unhealthy"],
        )
        self.assertEqual(stopped["counts"], {"reply_now": 0, "proactive_today": 1, "suppressed": 1})
        self.assertTrue(
            stopped["lanes"]["proactive_today"][0]["contact_precheck_required"]
        )

        boundary = build_action_queue(
            context(message_snapshot_at=NOW - timedelta(minutes=15)),
            [candidate(3, unresolved_inbound=True)],
        )
        self.assertEqual(boundary["status"], "ready")
        self.assertEqual(len(boundary["lanes"]["reply_now"]), 1)

    def test_three_lanes_and_hard_suppression_reasons(self) -> None:
        rows = [
            candidate(1, unresolved_inbound=True),
            candidate(2, proactive_eligible=True, promised_followup_at=NOW - timedelta(minutes=1)),
            candidate(3, proactive_eligible=True, aftersales_open=True),
            candidate(4, proactive_eligible=True, explicit_rejection=True),
            candidate(5, proactive_eligible=True, recently_ordered=True),
            candidate(6, proactive_eligible=True, consecutive_no_reply=2),
            candidate(7, unresolved_inbound=True, identity_conflict=True),
            candidate(8, unresolved_inbound=True, facts_sufficient=False),
            candidate(
                9,
                unresolved_inbound=True,
                required_fact_codes=("current_price", "inventory"),
                available_fact_codes=("inventory",),
            ),
            candidate(10),
        ]
        queue = build_action_queue(context(), rows)
        self.assertEqual(queue["counts"], {"reply_now": 1, "proactive_today": 1, "suppressed": 8})
        self.assertEqual(queue["lanes"]["reply_now"][0]["recommended_action"], "reply_to_inbound")
        self.assertEqual(
            queue["lanes"]["proactive_today"][0]["recommended_action"],
            "follow_up_as_promised",
        )
        reasons = {
            item["customer_key"]: set(item["reason_codes"])
            for item in queue["lanes"]["suppressed"]
        }
        self.assertIn("aftersales_open", reasons[opaque_customer(3)])
        self.assertIn("explicit_rejection", reasons[opaque_customer(4)])
        self.assertIn("recently_ordered", reasons[opaque_customer(5)])
        self.assertIn("consecutive_no_reply", reasons[opaque_customer(6)])
        self.assertIn("identity_conflict", reasons[opaque_customer(7)])
        self.assertIn("facts_insufficient", reasons[opaque_customer(8)])
        self.assertIn("required_facts_missing", reasons[opaque_customer(9)])
        self.assertIn("not_actionable_today", reasons[opaque_customer(10)])

    def test_sorting_is_lexicographic_by_promises_value_repurchase_intent_product(self) -> None:
        rows = [
            candidate(
                1,
                proactive_eligible=True,
                value_score=100,
                repurchase_score=100,
                intent_signal="positive",
                product_candidate_score=100,
            ),
            candidate(2, proactive_eligible=True, promised_followup_at=NOW - timedelta(minutes=1)),
            candidate(
                3,
                proactive_eligible=True,
                value_score=90,
                repurchase_score=100,
                intent_signal="positive",
                product_candidate_score=100,
            ),
            candidate(
                4,
                proactive_eligible=True,
                value_score=90,
                repurchase_score=90,
                intent_signal="positive",
                product_candidate_score=0,
            ),
            candidate(
                5,
                proactive_eligible=True,
                value_score=90,
                repurchase_score=90,
                intent_signal="mixed",
                product_candidate_score=100,
            ),
        ]
        queue = build_action_queue(context(), rows)
        self.assertEqual(
            [item["customer_key"] for item in queue["lanes"]["proactive_today"]],
            [
                opaque_customer(2),
                opaque_customer(1),
                opaque_customer(3),
                opaque_customer(4),
                opaque_customer(5),
            ],
        )
        self.assertTrue(
            all(item["priority_version"] == POLICY_VERSION for item in flatten(queue))
        )

    def test_phone_dedup_cooldown_and_daily_limit(self) -> None:
        shared = opaque_phone(999)
        rows = [
            candidate(1, phone_hmac=shared, proactive_eligible=True, value_score=10),
            candidate(2, phone_hmac=shared, proactive_eligible=True, value_score=90),
            candidate(
                3,
                proactive_eligible=True,
                last_proactive_at=NOW - timedelta(days=6, hours=23),
            ),
            candidate(4, proactive_eligible=True, last_proactive_at=NOW - timedelta(days=7)),
        ]
        queue = build_action_queue(context(), rows)
        proactive_keys = {item["customer_key"] for item in queue["lanes"]["proactive_today"]}
        self.assertEqual(proactive_keys, {opaque_customer(2), opaque_customer(4)})
        reasons = {
            item["customer_key"]: item["reason_codes"]
            for item in queue["lanes"]["suppressed"]
        }
        self.assertIn("duplicate_phone_today", reasons[opaque_customer(1)])
        self.assertIn("proactive_cooldown", reasons[opaque_customer(3)])

        overflow = build_action_queue(
            context(),
            [
                candidate(
                    100 + number,
                    proactive_eligible=True,
                    value_score=100 - number,
                )
                for number in range(21)
            ],
        )
        self.assertEqual(len(overflow["lanes"]["proactive_today"]), 20)
        self.assertEqual(len(overflow["lanes"]["suppressed"]), 1)
        self.assertIn("daily_proactive_limit", overflow["lanes"]["suppressed"][0]["reason_codes"])

    def test_stale_orders_hide_value_repurchase_and_product_without_blocking_inbound(self) -> None:
        queue = build_action_queue(
            context(order_snapshot_at=NOW - timedelta(hours=24, seconds=1)),
            [
                candidate(
                    1,
                    unresolved_inbound=True,
                    value_score=100,
                    repurchase_score=100,
                    product_candidate_score=100,
                ),
                candidate(2, proactive_eligible=True, repurchase_score=100),
                candidate(
                    3,
                    proactive_eligible=True,
                    promised_followup_at=NOW - timedelta(minutes=1),
                    value_score=100,
                ),
            ],
        )
        self.assertEqual(queue["status"], "degraded_order_data")
        inbound = queue["lanes"]["reply_now"][0]
        promised = queue["lanes"]["proactive_today"][0]
        for item in (inbound, promised):
            self.assertEqual(
                item["signals"],
                {
                    "value_score": None,
                    "repurchase_score": None,
                    "intent_signal": "unknown",
                    "product_candidate_score": None,
                },
            )
            self.assertEqual(item["freshness"]["orders"]["state"], "stale")
        stale_proactive = next(
            item
            for item in queue["lanes"]["suppressed"]
            if item["customer_key"] == opaque_customer(2)
        )
        self.assertIn("order_snapshot_stale_for_proactive", stale_proactive["reason_codes"])

    def test_rule_draft_is_deterministic_review_only_and_contains_no_pii(self) -> None:
        row = candidate(
            1,
            unresolved_inbound=True,
            required_fact_codes=("current_price", "inventory", "size_availability"),
            available_fact_codes=("current_price", "inventory", "size_availability"),
            preferred_contact_hour=21,
            active_hour_observations=5,
        )
        first = build_action_queue(context(), [row])
        second = build_action_queue(context(), [replace(row)])
        self.assertEqual(first, second)
        item = first["lanes"]["reply_now"][0]
        self.assertEqual(item["draft"]["mode"], "rule_skeleton")
        self.assertFalse(item["draft"]["model_used"])
        self.assertFalse(item["send_allowed"])
        self.assertTrue(item["human_confirmation_required"])
        self.assertEqual(
            item["required_facts"],
            ["current_price", "inventory", "size_availability"],
        )
        self.assertIn("unverified_price", item["prohibited_claims"])
        self.assertIn("guaranteed_delivery", item["prohibited_claims"])
        self.assertEqual(item["contact_window"]["mode"], "as_soon_as_possible")

        serialized = json.dumps(first, ensure_ascii=False)
        for forbidden in ("phone_hmac", "13800138000", "wxid_", "微信号", "客户姓名"):
            self.assertNotIn(forbidden, serialized)

    def test_proactive_contact_window_uses_history_only_with_enough_observations(self) -> None:
        queue = build_action_queue(
            context(),
            [
                candidate(
                    1,
                    proactive_eligible=True,
                    preferred_contact_hour=21,
                    active_hour_observations=2,
                ),
                candidate(
                    2,
                    proactive_eligible=True,
                    preferred_contact_hour=21,
                    active_hour_observations=3,
                ),
            ],
        )
        by_key = {item["customer_key"]: item for item in queue["lanes"]["proactive_today"]}
        self.assertEqual(
            by_key[opaque_customer(1)]["contact_window"]["mode"],
            "work_hours_manual_choice",
        )
        self.assertEqual(
            by_key[opaque_customer(2)]["contact_window"],
            {
                "mode": "personal_history",
                "timezone": "Asia/Shanghai",
                "start_hour": 21,
                "end_hour": 22,
            },
        )

    def test_rejects_nonopaque_identifiers_naive_times_and_invalid_scores(self) -> None:
        with self.assertRaisesRegex(ValueError, "opaque customer_key"):
            build_action_queue(context(), [candidate(1, customer_key="customer_13800138000")])
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_action_queue(context(as_of_at=NOW.replace(tzinfo=None)), [candidate(1)])
        with self.assertRaisesRegex(ValueError, "value_score"):
            build_action_queue(context(), [candidate(1, proactive_eligible=True, value_score=101)])


if __name__ == "__main__":
    unittest.main()
