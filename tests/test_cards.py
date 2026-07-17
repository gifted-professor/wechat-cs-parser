from __future__ import annotations

import json
import unittest
from typing import Dict, Optional

from wechat_cs.cards import (
    CardSource,
    build_decision_cards,
    build_decision_turns,
    segment_episodes,
    to_blind_payload,
)
from wechat_cs.core import Message


SECRET = "cards-test-secret-with-at-least-32-characters"


def message(
    key: str,
    customer: str,
    role: str,
    timestamp: str,
    ordinal: int,
    text: str,
    *,
    source_file: str = "events-a.jsonl",
) -> Message:
    return Message(
        message_key=key,
        customer_key=customer,
        role=role,
        timestamp=timestamp,
        text=text,
        source_file=source_file,
        source_ordinal=ordinal,
    )


def source(customer: str, observed_until: Optional[str]) -> Dict[str, CardSource]:
    return {
        customer: CardSource(
            profile_id="aolai1",
            source_snapshot_id="snapshot-aolai1",
            observation_until=observed_until,
        )
    }


class DecisionTurnTests(unittest.TestCase):
    def test_same_role_turns_merge_at_15_minutes_and_episodes_split_after_24_hours(self) -> None:
        rows = [
            message("m1", "customer-a", "customer", "2026-07-01T10:00:00+08:00", 1, "第一条"),
            message("m2", "customer-a", "customer", "2026-07-01T10:15:00+08:00", 2, "十五分钟边界"),
            message("m3", "customer-a", "studio", "2026-07-01T10:16:00+08:00", 3, "角色变化"),
            message("m4", "customer-a", "customer", "2026-07-02T10:16:00+08:00", 4, "二十四小时边界"),
            message("m5", "customer-a", "studio", "2026-07-03T10:16:01+08:00", 5, "超过二十四小时"),
        ]

        turns = build_decision_turns(rows)
        self.assertEqual(len(turns), 4)
        self.assertEqual(turns[0].message_keys, ("m1", "m2"))
        episodes = segment_episodes(turns)
        self.assertEqual([len(item) for item in episodes], [3, 1])


class DecisionCardTests(unittest.TestCase):
    def test_observed_action_has_five_states_and_profile_specific_observation_boundary(self) -> None:
        rows = [
            message("i-c", "immediate", "customer", "2026-07-01T10:00:00+08:00", 1, "立即回复测试"),
            message("i-s", "immediate", "studio", "2026-07-01T10:30:00+08:00", 2, "三十分钟回复"),
            message("d-c", "delayed", "customer", "2026-07-01T10:00:00+08:00", 3, "延迟回复测试"),
            message("d-s", "delayed", "studio", "2026-07-01T10:30:01+08:00", 4, "超过三十分钟回复"),
            message("e-c", "edge", "customer", "2026-07-01T10:00:00+08:00", 9, "二十四小时回复边界"),
            message("e-s", "edge", "studio", "2026-07-02T10:00:00+08:00", 10, "整二十四小时回复"),
            message("n-c", "none", "customer", "2026-07-01T10:00:00+08:00", 5, "无回复测试"),
            message("u-c", "unknown", "customer", "2026-07-01T10:00:00+08:00", 6, "观察不足测试"),
            message("p-c", "proactive", "customer", "2026-07-01T10:00:00+08:00", 7, "之前的客户消息"),
            message("p-s", "proactive", "studio", "2026-07-02T10:00:01+08:00", 8, "长间隔后主动跟进"),
        ]
        sources = {
            "immediate": CardSource("aolai1", "snapshot-aolai1", "2026-07-03T00:00:00+08:00"),
            "delayed": CardSource("aolai1", "snapshot-aolai1", "2026-07-03T00:00:00+08:00"),
            "edge": CardSource("aolai1", "snapshot-aolai1", "2026-07-03T00:00:00+08:00"),
            "none": CardSource("aolai1", "snapshot-aolai1", "2026-07-02T10:00:00+08:00"),
            "unknown": CardSource("aolai2", "snapshot-aolai2", "2026-07-02T09:59:59+08:00"),
            "proactive": CardSource("aolai1", "snapshot-aolai1", "2026-07-03T00:00:00+08:00"),
        }

        cards = build_decision_cards(rows, sources, secret=SECRET)
        by_customer_type = {(card.customer_key, card.card_type): card for card in cards}

        self.assertEqual(
            by_customer_type[("immediate", "inbound")].observed_action["state"],
            "immediate_reply",
        )
        self.assertEqual(
            by_customer_type[("immediate", "inbound")].observed_action["reply_delay_seconds"],
            1800,
        )
        self.assertEqual(
            by_customer_type[("delayed", "inbound")].observed_action["state"],
            "delayed_reply",
        )
        self.assertEqual(
            by_customer_type[("delayed", "inbound")].observed_action["reply_delay_seconds"],
            1801,
        )
        self.assertEqual(
            by_customer_type[("edge", "inbound")].observed_action["state"],
            "delayed_reply",
        )
        self.assertEqual(
            by_customer_type[("edge", "inbound")].observed_action["reply_delay_seconds"],
            86400,
        )
        self.assertNotIn(("edge", "proactive_followup"), by_customer_type)
        self.assertEqual(by_customer_type[("none", "inbound")].observed_action["state"], "no_reply")
        self.assertEqual(
            by_customer_type[("unknown", "inbound")].observed_action["state"],
            "unobserved",
        )
        proactive = by_customer_type[("proactive", "proactive_followup")]
        self.assertEqual(proactive.observed_action["state"], "proactive_followup")
        self.assertEqual(proactive.observed_action["gap_seconds"], 86401)
        self.assertEqual(proactive.action_message_keys, ("p-s",))

    def test_unknown_profile_boundary_never_falls_back_to_global_last_message(self) -> None:
        rows = [
            message("old-c", "customer-a", "customer", "2026-06-01T10:00:00+08:00", 1, "很早的消息"),
            message("old-s", "customer-a", "studio", "2026-06-01T10:10:00+08:00", 2, "实际存在但边界未知的回复"),
            message("global-new", "customer-b", "customer", "2026-07-01T10:00:00+08:00", 2, "其他账号的新消息"),
        ]
        sources = {
            "customer-a": CardSource("aolai1", "snapshot-aolai1", None),
            "customer-b": CardSource("aolai2", "snapshot-aolai2", "2026-07-02T10:00:00+08:00"),
        }

        cards = build_decision_cards(rows, sources, secret=SECRET)
        first = next(card for card in cards if card.customer_key == "customer-a")
        self.assertEqual(first.observed_action["state"], "unobserved")
        self.assertEqual(first.action_message_keys, ())
        self.assertIsNone(first.observation_until)

    def test_same_second_order_uses_ordinal_then_message_key_without_future_leakage(self) -> None:
        rows = [
            message(
                "a-customer",
                "customer-a",
                "customer",
                "2026-07-01T10:00:00+08:00",
                10,
                "客户边界消息",
                source_file="first.jsonl",
            ),
            message(
                "z-studio",
                "customer-a",
                "studio",
                "2026-07-01T10:00:00+08:00",
                10,
                "不能进入盲上下文的同秒回复",
                source_file="second.jsonl",
            ),
        ]

        card = build_decision_cards(
            rows,
            source("customer-a", "2026-07-02T10:00:00+08:00"),
            secret=SECRET,
        )[0]
        serialized = json.dumps(to_blind_payload(card), ensure_ascii=False, sort_keys=True)
        self.assertNotIn("不能进入盲上下文", serialized)
        self.assertEqual(card.boundary_ordinal, 10)
        self.assertEqual(card.boundary_message_key, "a-customer")
        self.assertEqual(card.action_message_keys, ("z-studio",))
        self.assertEqual(card.observed_action["reply_delay_seconds"], 0)

    def test_context_is_capped_at_eight_turns_and_proactive_context_comes_from_previous_episode(self) -> None:
        rows = []
        for index in range(10):
            rows.append(
                message(
                    "m%02d" % index,
                    "customer-a",
                    "customer" if index % 2 == 0 else "studio",
                    "2026-07-01T%02d:00:00+08:00" % index,
                    index + 1,
                    "历史 turn %d" % index,
                )
            )
        rows.append(
            message(
                "followup",
                "customer-a",
                "studio",
                "2026-07-03T12:00:01+08:00",
                20,
                "主动触达正文",
            )
        )

        cards = build_decision_cards(
            rows,
            source("customer-a", "2026-07-04T12:00:00+08:00"),
            secret=SECRET,
        )
        proactive = next(card for card in cards if card.card_type == "proactive_followup")
        self.assertEqual(len(proactive.blind_context), 8)
        serialized = json.dumps(proactive.blind_context, ensure_ascii=False)
        self.assertIn("历史 turn 9", serialized)
        self.assertNotIn("历史 turn 0", serialized)
        self.assertNotIn("主动触达正文", serialized)

    def test_card_id_and_blind_payload_do_not_change_when_a_future_reply_arrives(self) -> None:
        trigger = message(
            "trigger",
            "customer-a",
            "customer",
            "2026-07-01T10:00:00+08:00",
            1,
            "想买这个，电话 13800138000",
        )
        first = build_decision_cards(
            [trigger],
            source("customer-a", "2026-07-01T10:05:00+08:00"),
            secret=SECRET,
        )[0]
        reply = message(
            "reply",
            "customer-a",
            "studio",
            "2026-07-01T10:10:00+08:00",
            2,
            "未来才观察到的客服动作",
        )
        second = build_decision_cards(
            [trigger, reply],
            source("customer-a", "2026-07-01T10:15:00+08:00"),
            secret=SECRET,
        )[0]

        self.assertEqual(first.card_id, second.card_id)
        self.assertEqual(to_blind_payload(first), to_blind_payload(second))
        self.assertEqual(first.observed_action["state"], "unobserved")
        self.assertEqual(second.observed_action["state"], "immediate_reply")
        payload = json.dumps(to_blind_payload(second), ensure_ascii=False, sort_keys=True)
        self.assertNotIn("13800138000", payload)
        self.assertNotIn("observed_action", payload)
        self.assertNotIn("action_message_keys", payload)
        self.assertNotIn("order", payload.lower())

    def test_proactive_card_id_does_not_change_when_the_action_turn_gains_a_message(self) -> None:
        prior = message(
            "prior",
            "customer-a",
            "customer",
            "2026-07-01T10:00:00+08:00",
            1,
            "上一段会话",
        )
        followup = message(
            "followup-first",
            "customer-a",
            "studio",
            "2026-07-02T10:00:01+08:00",
            2,
            "主动触达第一条",
        )
        first = next(
            item
            for item in build_decision_cards(
                [prior, followup],
                source("customer-a", "2026-07-02T10:01:00+08:00"),
                secret=SECRET,
            )
            if item.card_type == "proactive_followup"
        )
        continuation = message(
            "followup-second",
            "customer-a",
            "studio",
            "2026-07-02T10:05:00+08:00",
            3,
            "主动触达第二条",
        )
        second = next(
            item
            for item in build_decision_cards(
                [prior, followup, continuation],
                source("customer-a", "2026-07-02T10:06:00+08:00"),
                secret=SECRET,
            )
            if item.card_type == "proactive_followup"
        )

        self.assertEqual(first.card_id, second.card_id)
        self.assertEqual(to_blind_payload(first), to_blind_payload(second))
        self.assertEqual(first.action_message_keys, ("followup-first",))
        self.assertEqual(second.action_message_keys, ("followup-first", "followup-second"))


if __name__ == "__main__":
    unittest.main()
